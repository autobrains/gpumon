import boto3
import re
import time
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
REGIONS = ["eu-west-1", "eu-central-1", "us-east-1"]
IAM_ROLE_NAME  = "EC2IAMRole"
TAG_KEY        = "GPUMON"
BRANCH_TAG_KEY = "GPUMON_BRANCH"       # optional per-instance branch override

# All new installs and migrations use DOCKER_BRANCH.
# feature/dockerize was promoted to main on 2026-07-21; main is now canonical.
DOCKER_BRANCH  = "main"

_VALID_BRANCH_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9/_.-]{0,99}$')

def _validate_branch(branch: str) -> str:
    """Raise ValueError unless branch is a safe, well-formed git branch name.

    fullmatch (not match) so a trailing newline can't smuggle a line break into
    the shell strings this value is interpolated into; the extra checks reject
    names the charset allows but git refuses ('..', trailing '/', '.lock') so
    the app-level guard, not git's downstream error, is the security boundary.
    """
    if (not _VALID_BRANCH_RE.fullmatch(branch)
            or ".." in branch
            or branch.endswith(("/", ".", ".lock"))):
        raise ValueError(f"Unsafe branch name rejected: {branch!r}")
    return branch

SSM_COMMAND_POLL_INTERVAL = 5    # seconds between status polls
SSM_CHECK_TIMEOUT   = 30         # quick is-running checks
SSM_STATUS_TIMEOUT  = 45         # combined status probe (includes a 20s-bounded git fetch)
SSM_FIX_TIMEOUT     = 180        # re-clone + rebuild
SSM_REFRESH_TIMEOUT = 540        # refresh execution window — inside the 10-min sweep cadence
SSM_INSTALL_TIMEOUT = 900        # full Docker install (apt + image pull + build)
SSM_MIGRATE_TIMEOUT = 1200       # migration: stop old + full reinstall

GPUMON_REPO = "https://github.com/autobrains/gpumon.git"
GPUMON_DIR  = "/root/gpumon"
SENTINEL    = "/var/log/gpumon.finished"

# Staleness refresh: a running Docker box whose clone is off DOCKER_BRANCH or
# behind its origin head gets converged in place.  The refresh target is ALWAYS
# DOCKER_BRANCH — never the GPUMON_BRANCH tag, which is not SCP-protected and
# must not steer what the sweep executes as root (a set GPUMON_BRANCH instead
# opts the box out of auto-refresh entirely: the sanctioned pin mechanism).
# The stamp file is the per-box cooldown; the per-sweep cap bounds total sweep
# duration (stragglers are picked up by the next 10-minute sweep).
REFRESH_STAMP           = "/var/log/gpumon-refresh.stamp"
REFRESH_COOLDOWN        = 3600   # min seconds between refresh attempts per box
MAX_REFRESHES_PER_SWEEP = 3

# ---------------------------------------------------------------------------
# SSM command bundles
# ---------------------------------------------------------------------------

def install_commands(branch: str) -> list[str]:
    """Clone the given branch and run autoinstall.sh."""
    branch = _validate_branch(branch)
    return [
        # Stop automatic update services so they don't hold the apt lock for
        # longer than our DPkg::Lock::Timeout can wait.
        "sudo systemctl stop unattended-upgrades apt-daily.service apt-daily-upgrade.service 2>/dev/null || true",
        "while sudo fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend /var/cache/apt/archives/lock >/dev/null 2>&1; do echo 'waiting for apt lock...'; sleep 5; done",
        "sudo apt-get -o DPkg::Lock::Timeout=120 update -q",
        "sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y git",
        f"sudo rm -f {SENTINEL}",
        f"sudo rm -rf {GPUMON_DIR}",
        f"sudo git clone --branch {branch} {GPUMON_REPO} {GPUMON_DIR}",
        f"sudo bash {GPUMON_DIR}/autoinstall.sh",
    ]

# Fix step 1 (fast path): pull latest code and rebuild the running container.
# Use fetch + reset --hard @{upstream} rather than git pull --force: it explicitly
# resets to the tracked remote branch and does not drift if the local HEAD somehow
# moved (e.g. a legacy update timer switched the branch to main).
# The docker compose command mirrors autoinstall.sh's GPU vs CPU selection.
FIX_STEP1_COMMANDS = [
    f"cd {GPUMON_DIR} && sudo git fetch origin && sudo git reset --hard @{{upstream}}",
    f"if nvidia-smi --list-gpus >/dev/null 2>&1 && [ \"$(nvidia-smi --list-gpus | wc -l)\" -gt 0 ]; then "
    f"  sudo docker compose -f {GPUMON_DIR}/docker-compose.yml up -d --build; "
    f"else "
    f"  sudo docker compose -f {GPUMON_DIR}/docker-compose.cpu.yml up -d --build; "
    f"fi",
]

def check_status_commands(branch: str) -> list[str]:
    """Single status probe for a running box: monitor state, deployment flavor,
    and whether the clone is current on the desired branch.

    Emits key=value lines.  Every probe degrades to a value ("inactive",
    "legacy", "unknown", "missing") instead of failing, so the script always
    exits 0 and a partial result never masquerades as an SSM failure.
    """
    branch = _validate_branch(branch)
    return [
        # running: Docker container (new) or direct process / systemd unit (legacy)
        "( docker ps --filter 'name=gpumon' --filter 'status=running' --quiet 2>/dev/null | grep -q . && echo running=active ) || "
        "( pgrep -f 'python.*gpumon\\.py' >/dev/null 2>&1 && echo running=active ) || "
        "( pgrep -f 'python.*cpumon\\.py' >/dev/null 2>&1 && echo running=active ) || "
        "( systemctl is-active --quiet gpumon 2>/dev/null && echo running=active ) || "
        "( systemctl is-active --quiet cpumon 2>/dev/null && echo running=active ) || "
        "echo running=inactive",
        f"{{ test -f {SENTINEL} || test -f {GPUMON_DIR}/docker-compose.yml; }} && echo deploy=docker || echo deploy=legacy",
        f"echo branch=$(git -C {GPUMON_DIR} rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)",
        f"echo head=$(git -C {GPUMON_DIR} rev-parse HEAD 2>/dev/null || echo unknown)",
        # Bounded fetch: on network trouble upstream stays 'unknown' and the box
        # is simply not refreshed this sweep — never a FAILED verdict.
        f"timeout 20 git -C {GPUMON_DIR} fetch origin --quiet 2>/dev/null || true",
        f"echo upstream=$(git -C {GPUMON_DIR} rev-parse origin/{branch} 2>/dev/null || echo unknown)",
        "systemctl is-enabled --quiet gpumon-update.timer 2>/dev/null && echo timer=enabled || echo timer=missing",
        f"echo stamp_age=$(( $(date +%s) - $(cat {REFRESH_STAMP} 2>/dev/null || echo 0) ))",
    ]


def refresh_commands(branch: str) -> list[str]:
    """Converge a stale Docker box to origin/<branch> and rebuild the container.

    The cooldown stamp is written FIRST so a hung run cannot retrigger a
    refresh storm.  autoinstall.sh regenerates halt_it.sh, the boot/update
    scripts, and the timer (its sentinel short-circuits the heavy apt/docker
    part) — but it exits early WITHOUT rebuilding the image, and the update
    script sees no delta after the checkout below, hence the explicit compose
    build mirroring FIX_STEP1's GPU/CPU selection.
    """
    branch = _validate_branch(branch)
    return [
        f"date +%s | sudo tee {REFRESH_STAMP}",
        f"sudo timeout 60 git -C {GPUMON_DIR} fetch origin",
        f"sudo git -C {GPUMON_DIR} checkout -B {branch} origin/{branch}",
        f"sudo bash {GPUMON_DIR}/autoinstall.sh",
        f"if nvidia-smi --list-gpus >/dev/null 2>&1 && [ \"$(nvidia-smi --list-gpus | wc -l)\" -gt 0 ]; then "
        f"  sudo docker compose -f {GPUMON_DIR}/docker-compose.yml up -d --build; "
        f"else "
        f"  sudo docker compose -f {GPUMON_DIR}/docker-compose.cpu.yml up -d --build; "
        f"fi",
    ]


# Fix step 2 (full reinstall): re-clone the repo (in case it is corrupted) then
# run autoinstall.sh end-to-end.  Removing SENTINEL alone is not enough if the
# repo itself is the broken artifact.
FIX_STEP2_COMMANDS = [
    f"sudo rm -f {SENTINEL}",
    f"sudo rm -rf {GPUMON_DIR}",
    f"sudo git clone --branch {DOCKER_BRANCH} {GPUMON_REPO} {GPUMON_DIR}",
    f"sudo bash {GPUMON_DIR}/autoinstall.sh",
]

# Delete: stop stack, remove timer and crontab entry, wipe repo.
DELETE_COMMANDS = [
    f"cd {GPUMON_DIR} && sudo docker compose down || true",
    # Fallback: stop/remove any lingering containers by name label regardless of compose state
    "sudo docker ps -a --filter 'label=com.docker.compose.project=gpumon' -q | xargs -r sudo docker rm -f || true",
    "sudo systemctl disable --now gpumon-update.timer gpumon-boot.service 2>/dev/null || true",
    "sudo rm -f /etc/systemd/system/gpumon-update.service /etc/systemd/system/gpumon-update.timer "
    "           /etc/systemd/system/gpumon-boot.service",
    "sudo systemctl daemon-reload 2>/dev/null || true",
    "crontab -l 2>/dev/null | grep -v halt_it.sh | crontab - || true",
    "sudo rm -f /usr/local/sbin/halt_it.sh /usr/local/sbin/gpumon-update.sh /usr/local/sbin/gpumon-boot.sh",
    f"sudo rm -rf {GPUMON_DIR}",
    f"sudo rm -f {SENTINEL}",
]

def migrate_commands(branch: str) -> list[str]:
    """Stop all legacy gpumon artifacts and install the Docker deployment from branch."""
    branch = _validate_branch(branch)
    return [
        # ── Stop and remove legacy systemd units ──
        "for svc in gpumon cpumon gpumon-monitor; do "
        "  sudo systemctl stop $svc 2>/dev/null || true; "
        "  sudo systemctl disable $svc 2>/dev/null || true; "
        "done",
        "sudo rm -f /etc/systemd/system/gpumon.service "
        "          /etc/systemd/system/cpumon.service "
        "          /etc/systemd/system/gpumon-monitor.service",
        "sudo systemctl daemon-reload 2>/dev/null || true",
        # ── Kill any directly-running monitor processes ──
        "sudo pkill -f 'python.*gpumon\\.py' 2>/dev/null || true",
        "sudo pkill -f 'python.*cpumon\\.py'  2>/dev/null || true",
        "sudo pkill -f 'python.*hostmon\\.py' 2>/dev/null || true",
        # ── Remove legacy crontab entries (halt_it.sh re-added by autoinstall) ──
        "crontab -l 2>/dev/null | grep -v halt_it.sh | grep -v gpumon | crontab - || true",
        # ── Drop sentinel so autoinstall.sh always runs end-to-end ──
        f"sudo rm -f {SENTINEL}",
        # ── Stop automatic update services so they don't hold the apt lock ──
        "sudo systemctl stop unattended-upgrades apt-daily.service apt-daily-upgrade.service 2>/dev/null || true",
        "while sudo fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend /var/cache/apt/archives/lock >/dev/null 2>&1; do echo 'waiting for apt lock...'; sleep 5; done",
        # ── Fresh clone at the Docker branch ──
        f"sudo rm -rf {GPUMON_DIR}",
        "sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y git -q",
        f"sudo git clone --branch {branch} {GPUMON_REPO} {GPUMON_DIR}",
        # ── Full Docker install ──
        f"sudo bash {GPUMON_DIR}/autoinstall.sh",
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_clients(region: str):
    return (
        boto3.client("ec2", region_name=region),
        boto3.client("ssm", region_name=region),
    )


def get_all_instances(ec2) -> list:
    instances = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate():
        for reservation in page["Reservations"]:
            instances.extend(reservation["Instances"])
    return instances


def get_gpumon_tag(instance) -> dict | None:
    for tag in instance.get("Tags", []):
        if tag["Key"] == TAG_KEY:
            return tag
    return None


def update_tag(ec2, instance_id: str, value: str) -> None:
    try:
        ec2.create_tags(
            Resources=[instance_id],
            Tags=[{"Key": TAG_KEY, "Value": value}],
        )
        print(f"[{instance_id}] tag → {value!r}")
    except ClientError as e:
        print(f"[{instance_id}] error updating tag: {e}")


# ---------------------------------------------------------------------------
# SSM
# ---------------------------------------------------------------------------

def run_ssm_command(
    ssm,
    instance_id: str,
    commands: list[str],
    poll_timeout: int = SSM_CHECK_TIMEOUT,
    execution_timeout: int = 300,
) -> dict | None:
    """Send SSM RunShellScript and poll until terminal status.

    poll_timeout       – how long to wait for the command to reach a terminal
                         state before giving up.
    execution_timeout  – passed to SSM as TimeoutSeconds (delivery + execution
                         window).  Increase for long-running commands.
    """
    try:
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": commands},
            TimeoutSeconds=execution_timeout,
        )
    except ClientError as e:
        print(f"[{instance_id}] SSM send_command failed: {e}")
        return None

    command_id = resp["Command"]["CommandId"]
    print(f"[{instance_id}] SSM command {command_id} sent")

    elapsed = 0
    while elapsed < poll_timeout:
        time.sleep(SSM_COMMAND_POLL_INTERVAL)
        elapsed += SSM_COMMAND_POLL_INTERVAL
        try:
            inv = ssm.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id
            )
            status = inv["Status"]
            if status in ("Success", "Failed", "TimedOut", "Cancelled"):
                print(f"[{instance_id}] SSM finished: {status}")
                return inv
        except ssm.exceptions.InvocationDoesNotExist:
            continue  # command hasn't registered yet
        except ClientError as e:
            print(f"[{instance_id}] polling error: {e}")
            return None

    print(f"[{instance_id}] SSM poll timed out after {poll_timeout}s")
    return None


def is_gpumon_running(ssm, instance_id: str) -> bool:
    """Return True if gpumon is running — either the Docker container (new) or
    the legacy direct-Python process / systemd service (old)."""
    inv = run_ssm_command(
        ssm,
        instance_id,
        [
            # New: Docker container named gpumon is running
            "( docker ps --filter 'name=gpumon' --filter 'status=running' --quiet 2>/dev/null | grep -q . && echo active ) || "
            # Legacy: gpumon.py or cpumon.py process running directly
            "( pgrep -f 'python.*gpumon\\.py' >/dev/null 2>&1 && echo active ) || "
            "( pgrep -f 'python.*cpumon\\.py' >/dev/null 2>&1 && echo active ) || "
            # Legacy: systemd service active (gpu or cpu variant)
            "( systemctl is-active --quiet gpumon 2>/dev/null && echo active ) || "
            "( systemctl is-active --quiet cpumon 2>/dev/null && echo active ) || "
            "echo inactive"
        ],
        poll_timeout=SSM_CHECK_TIMEOUT,
        execution_timeout=30,
    )
    if inv is None:
        return False
    return inv.get("StandardOutputContent", "").strip() == "active"


def is_gpumon_dockerized(ssm, instance_id: str) -> bool:
    """Return True only if the Docker container (new deployment) is running."""
    inv = run_ssm_command(
        ssm,
        instance_id,
        ["docker ps --filter 'name=gpumon' --filter 'status=running' --quiet | grep -q . && echo active || echo inactive"],
        poll_timeout=SSM_CHECK_TIMEOUT,
        execution_timeout=30,
    )
    if inv is None:
        return False
    return inv.get("StandardOutputContent", "").strip() == "active"


def _has_docker_deployment(ssm, instance_id: str) -> bool:
    """Return True if a Docker gpumon deployment exists on this instance.

    Accepts either the sentinel file (written at end of autoinstall.sh) OR the
    presence of docker-compose.yml in GPUMON_DIR.  The boot service restarts the
    container without recreating the sentinel, so either marker is sufficient.
    A legacy instance (never Dockerized) will have neither.
    """
    # Check sentinel OR compose file: gpumon-boot.service starts the container
    # without recreating the sentinel, so a running post-boot instance may lack
    # the sentinel while docker-compose.yml is present in GPUMON_DIR.
    inv = run_ssm_command(
        ssm,
        instance_id,
        [f"{{ test -f {SENTINEL} || test -f {GPUMON_DIR}/docker-compose.yml; }} && echo yes || echo no"],
        poll_timeout=SSM_CHECK_TIMEOUT,
        execution_timeout=30,
    )
    if inv is None:
        return False
    return inv.get("StandardOutputContent", "").strip() == "yes"


# ---------------------------------------------------------------------------
# IAM role management
# ---------------------------------------------------------------------------

def ensure_iam_role(ec2, instance_id: str) -> bool:
    """Attach IAM_ROLE_NAME instance profile if not already present."""
    try:
        details = ec2.describe_instances(InstanceIds=[instance_id])
        instance = details["Reservations"][0]["Instances"][0]
    except (ClientError, IndexError, KeyError) as e:
        print(f"[{instance_id}] error describing instance: {e}")
        return False

    profile = instance.get("IamInstanceProfile")
    if profile and IAM_ROLE_NAME in profile["Arn"]:
        print(f"[{instance_id}] IAM role already attached")
        return True

    if profile:
        try:
            assocs = ec2.describe_iam_instance_profile_associations(
                Filters=[{"Name": "instance-id", "Values": [instance_id]}]
            )
            for assoc in assocs["IamInstanceProfileAssociations"]:
                if assoc["State"] == "associated":
                    ec2.disassociate_iam_instance_profile(
                        AssociationId=assoc["AssociationId"]
                    )
                    print(f"[{instance_id}] disassociated existing IAM profile")
                    time.sleep(3)
        except ClientError as e:
            print(f"[{instance_id}] error disassociating profile: {e}")
            return False

    try:
        ec2.associate_iam_instance_profile(
            IamInstanceProfile={"Name": IAM_ROLE_NAME},
            InstanceId=instance_id,
        )
        print(f"[{instance_id}] attached IAM role '{IAM_ROLE_NAME}'")
        return True
    except ClientError as e:
        print(f"[{instance_id}] error attaching IAM role: {e}")
        return False


# ---------------------------------------------------------------------------
# Core actions
# ---------------------------------------------------------------------------

def handle_install(ec2, ssm, instance_id: str, branch: str) -> None:
    """Ensure IAM role, clone branch, run autoinstall.sh, verify container."""
    if not ensure_iam_role(ec2, instance_id):
        update_tag(ec2, instance_id, "FAILED")
        return

    print(f"[{instance_id}] installing from branch '{branch}'")
    inv = run_ssm_command(
        ssm,
        instance_id,
        install_commands(branch),
        poll_timeout=SSM_INSTALL_TIMEOUT,
        execution_timeout=SSM_INSTALL_TIMEOUT,
    )
    if inv is None:
        print(f"[{instance_id}] install command could not be sent — SSM agent may be absent")
        update_tag(ec2, instance_id, "PENDING_SSM")
        return

    if inv["Status"] != "Success":
        print(f"[{instance_id}] install script exited non-zero: {inv.get('StandardErrorContent', '')[:300]}")
        update_tag(ec2, instance_id, "FAILED")
        return

    time.sleep(5)
    if is_gpumon_dockerized(ssm, instance_id):
        update_tag(ec2, instance_id, "ACTIVE")
    else:
        update_tag(ec2, instance_id, "FAILED")


def _time_left(context, needed_ms: int) -> bool:
    """True when the lambda has at least needed_ms of runtime left.

    Degrades to True when no real Lambda context is available (local tests,
    manual invocation harnesses).
    """
    try:
        return context.get_remaining_time_in_millis() > needed_ms
    except AttributeError:
        return True


def _parse_kv(output: str) -> dict:
    """Parse key=value lines from a status probe into a dict."""
    facts = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            facts[key.strip()] = value.strip()
    return facts


def _stale_reason(facts: dict, want_branch: str) -> str | None:
    """Return why a Docker box needs a refresh, or None when it is current.

    Legacy installs and unreadable clones return None: legacy migration stays
    a manual GPUMON=MIGRATE decision, and a wiped/corrupt clone is the
    FAILED → handle_fix flow's job (it re-clones; refresh only checkouts).

    A missing auto-update timer is deliberately NOT a trigger: an operator may
    have disabled it on purpose (same respect the halt_it.sh cron guard gives a
    commented-out entry), and autoinstall.sh re-enables it as a side effect of
    the next commit-driven refresh anyway.  The probe still reports timer= for
    observability.
    """
    if facts.get("deploy") != "docker":
        return None
    branch   = facts.get("branch", "unknown")
    head     = facts.get("head", "unknown")
    upstream = facts.get("upstream", "unknown")
    if head == "unknown":
        return None
    if branch != want_branch:
        return f"on branch {branch!r}, want {want_branch!r}"
    if upstream != "unknown" and head != upstream:
        return f"at {head[:12]}, origin/{want_branch} is at {upstream[:12]}"
    return None


def handle_check(ec2, ssm, instance_id: str, want_branch: str | None,
                 allow_refresh: bool = True) -> bool:
    """Verify gpumon is running (Docker or legacy) and update the tag.

    With want_branch set, Docker boxes additionally get a staleness check —
    clone on want_branch at the fetched origin head — and a stale box is
    converged in place via refresh_commands().  Returns True when a refresh was
    fired so the caller can budget refreshes per sweep.  want_branch=None means
    plain running check only (boxes pinned via GPUMON_BRANCH).  Migration from
    legacy to Docker is still triggered manually via GPUMON=MIGRATE.
    """
    if want_branch is None:
        running = is_gpumon_running(ssm, instance_id)
        update_tag(ec2, instance_id, "ACTIVE" if running else "FAILED")
        return False

    inv = run_ssm_command(
        ssm, instance_id, check_status_commands(want_branch),
        poll_timeout=SSM_STATUS_TIMEOUT,
        execution_timeout=SSM_STATUS_TIMEOUT,
    )
    if inv is None:
        # Probe poll expired — degraded SSM or a slow box.  Fall back to the
        # cheap legacy check before declaring FAILED: a slow-but-healthy box
        # must not be sent through handle_fix's disruptive re-clone.
        running = is_gpumon_running(ssm, instance_id)
        update_tag(ec2, instance_id, "ACTIVE" if running else "FAILED")
        return False

    facts = _parse_kv(inv.get("StandardOutputContent", ""))
    if facts.get("running") != "active":
        update_tag(ec2, instance_id, "FAILED")
        return False
    update_tag(ec2, instance_id, "ACTIVE")

    reason = _stale_reason(facts, want_branch)
    if reason is None:
        return False

    stamp_raw = facts.get("stamp_age", "")
    try:
        stamp_age = int(stamp_raw)
    except ValueError:
        # Fail closed: a box that can't report a sane stamp age must not be
        # able to earn a refresh every sweep by garbling it.
        print(f"[{instance_id}] stale ({reason}) but stamp age unreadable "
              f"({stamp_raw!r}) — skipping refresh")
        return False
    if stamp_age < REFRESH_COOLDOWN:
        print(f"[{instance_id}] stale ({reason}) but refreshed {stamp_age}s ago — cooldown")
        return False
    if not allow_refresh:
        print(f"[{instance_id}] stale ({reason}) but sweep refresh budget exhausted — next sweep")
        return False

    print(f"[{instance_id}] stale — {reason} — refreshing to origin/{want_branch}")
    inv = run_ssm_command(
        ssm, instance_id, refresh_commands(want_branch),
        poll_timeout=SSM_FIX_TIMEOUT,          # stop polling early; box keeps executing
        execution_timeout=SSM_REFRESH_TIMEOUT,
    )
    if inv is not None and inv.get("Status") == "Success":
        print(f"[{instance_id}] refresh complete")
    else:
        status = inv.get("Status") if inv else "no response"
        print(f"[{instance_id}] refresh unconfirmed ({status}) — cooldown stamp "
              "prevents re-fire; next sweep re-verifies")
    return True


def handle_fix(ec2, ssm, instance_id: str) -> None:
    """Progressive fix for FAILED Docker instances.

    Step 1 (fast): pull latest code, rebuild and restart the container.
    Step 2 (full): re-clone the repo and run autoinstall.sh end-to-end.
    If still broken: tag NOT_FIXED for manual attention.

    Legacy (non-Docker) instances that go FAILED are not auto-fixed here —
    they are tagged NOT_FIXED with a message to set GPUMON=MIGRATE manually.
    """
    # Guard: require an existing Docker gpumon deployment (sentinel present).
    # docker info alone is insufficient — a legacy instance may have Docker
    # installed without gpumon being containerized.  The sentinel is written at
    # the end of autoinstall.sh and survives a stopped/broken container.
    if not _has_docker_deployment(ssm, instance_id):
        print(f"[{instance_id}] no Docker gpumon deployment found — "
              "set GPUMON=MIGRATE to upgrade this legacy instance")
        update_tag(ec2, instance_id, "NOT_FIXED")
        return

    print(f"[{instance_id}] starting fix — step 1: pull + rebuild")
    inv = run_ssm_command(
        ssm, instance_id, FIX_STEP1_COMMANDS,
        poll_timeout=SSM_FIX_TIMEOUT,
        execution_timeout=SSM_FIX_TIMEOUT,
    )
    if inv is None:
        update_tag(ec2, instance_id, "NOT_FIXED")
        return

    time.sleep(5)
    # Only declare step 1 success if the SSM command itself exited 0 AND
    # the Docker container is now running.  is_gpumon_running() is intentionally
    # NOT used here: a surviving legacy process must not mask a failed Docker fix.
    if inv["Status"] == "Success" and is_gpumon_dockerized(ssm, instance_id):
        print(f"[{instance_id}] fixed by step 1")
        update_tag(ec2, instance_id, "ACTIVE")
        return

    print(f"[{instance_id}] step 1 insufficient — step 2: full reinstall")
    inv = run_ssm_command(
        ssm, instance_id, FIX_STEP2_COMMANDS,
        poll_timeout=SSM_INSTALL_TIMEOUT,
        execution_timeout=SSM_INSTALL_TIMEOUT,
    )
    if inv is None or inv["Status"] != "Success":
        update_tag(ec2, instance_id, "NOT_FIXED")
        return

    time.sleep(5)
    if is_gpumon_dockerized(ssm, instance_id):
        print(f"[{instance_id}] fixed by step 2")
        update_tag(ec2, instance_id, "ACTIVE")
    else:
        print(f"[{instance_id}] could not be fixed — manual intervention required")
        update_tag(ec2, instance_id, "NOT_FIXED")


def handle_delete(ec2, ssm, instance_id: str) -> None:
    """Stop the Docker stack, remove systemd timer, crontab entry, and repo."""
    inv = run_ssm_command(
        ssm, instance_id, DELETE_COMMANDS,
        poll_timeout=SSM_FIX_TIMEOUT,
        execution_timeout=SSM_FIX_TIMEOUT,
    )
    if inv is not None and inv.get("Status") == "Success":
        update_tag(ec2, instance_id, "")
        print(f"[{instance_id}] gpumon removed")
    else:
        status = inv.get("Status") if inv else "no response"
        print(f"[{instance_id}] delete command failed (status: {status})")
        update_tag(ec2, instance_id, "FAILED")


def handle_migrate(ec2, ssm, instance_id: str, branch: str) -> None:
    """Migrate a legacy (non-Docker) gpumon instance to the Docker deployment.

    Stops any old systemd units or bare Python processes, wipes the repo,
    re-clones at branch, and runs autoinstall.sh end-to-end.  Verification
    uses is_gpumon_dockerized() so a still-running legacy process does not
    mask a failed Docker install.
    """
    if not ensure_iam_role(ec2, instance_id):
        update_tag(ec2, instance_id, "FAILED")
        return

    print(f"[{instance_id}] starting migration to Docker deployment (branch '{branch}')")
    inv = run_ssm_command(
        ssm, instance_id, migrate_commands(branch),
        poll_timeout=SSM_MIGRATE_TIMEOUT,
        execution_timeout=SSM_MIGRATE_TIMEOUT,
    )
    if inv is None:
        print(f"[{instance_id}] migration command could not be sent — SSM agent may be absent")
        update_tag(ec2, instance_id, "PENDING_SSM")
        return

    if inv["Status"] != "Success":
        print(f"[{instance_id}] migration script failed: {inv.get('StandardErrorContent', '')[:300]}")
        update_tag(ec2, instance_id, "FAILED")
        return

    time.sleep(5)
    if is_gpumon_dockerized(ssm, instance_id):
        print(f"[{instance_id}] migration complete — Docker container running")
        update_tag(ec2, instance_id, "ACTIVE")
    else:
        print(f"[{instance_id}] migration finished but Docker container not detected — marking FAILED")
        update_tag(ec2, instance_id, "FAILED")


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    print("lambda_handler: starting fleet sweep")

    refreshes_left = MAX_REFRESHES_PER_SWEEP

    for region in REGIONS:
        ec2, ssm = get_clients(region)

        try:
            instances = get_all_instances(ec2)
        except ClientError as e:
            print(f"[{region}] error listing instances: {e}")
            continue

        for instance in instances:
            if not _time_left(context, 90_000):
                print("lambda_handler: <90s runtime left — deferring the rest "
                      "of the sweep to the next cycle")
                return

            instance_id = instance["InstanceId"]
            state       = instance["State"]["Name"]
            gpumon_tag  = get_gpumon_tag(instance)

            if gpumon_tag is None:
                continue

            tag_value = gpumon_tag["Value"].strip()

            # GPUMON_BRANCH overrides per instance; fallback differs by action:
            # INSTALL defaults to main (legacy path), MIGRATE defaults to
            # the Docker branch so the admin needn't set the tag explicitly.
            all_tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
            branch_override = all_tags.get(BRANCH_TAG_KEY)

            print(f"[{region}][{instance_id}] state={state} tag={tag_value!r}")

            try:
                if tag_value.lower() == "install" or tag_value == "PENDING_SSM":
                    branch = branch_override or DOCKER_BRANCH
                    if state == "running":
                        handle_install(ec2, ssm, instance_id, branch)
                    else:
                        print(f"[{instance_id}] waiting for instance to reach running state")

                elif tag_value == "MIGRATE":
                    branch = branch_override or DOCKER_BRANCH
                    if state == "running":
                        handle_migrate(ec2, ssm, instance_id, branch)
                    else:
                        print(f"[{instance_id}] MIGRATE skipped — instance not running")

                elif tag_value == "DELETE":
                    if state == "running":
                        handle_delete(ec2, ssm, instance_id)
                    else:
                        print(f"[{instance_id}] DELETE pending — instance not running, will retry when running")

                elif state != "running" and tag_value not in ("", "INACTIVE"):
                    update_tag(ec2, instance_id, "INACTIVE")

                elif state == "running" and tag_value == "FAILED":
                    handle_fix(ec2, ssm, instance_id)

                elif state == "running" and tag_value in ("ACTIVE", "INACTIVE"):
                    # Auto-refresh converges to DOCKER_BRANCH only.  A set
                    # GPUMON_BRANCH means "deliberately pinned": plain running
                    # check, no refresh.  The tag key is not SCP-protected, so
                    # it must never steer what the sweep executes as root —
                    # honoring it here would let any ec2:CreateTags principal
                    # point a box at an arbitrary repo branch.
                    if branch_override:
                        print(f"[{instance_id}] pinned via {BRANCH_TAG_KEY}="
                              f"{branch_override!r} — plain check, no auto-refresh")
                        handle_check(ec2, ssm, instance_id, None)
                    else:
                        allow = (refreshes_left > 0
                                 and _time_left(context, (SSM_FIX_TIMEOUT + 60) * 1000))
                        if handle_check(ec2, ssm, instance_id, DOCKER_BRANCH,
                                        allow_refresh=allow):
                            refreshes_left -= 1

                elif tag_value == "NOT_FIXED":
                    print(f"[{instance_id}] NOT_FIXED — skipping until manually resolved")

            except ValueError as e:
                print(f"[{region}][{instance_id}] invalid GPUMON_BRANCH tag — {e}")
                update_tag(ec2, instance_id, "FAILED")
            except ClientError as e:
                if e.response["Error"]["Code"] == "RequestLimitExceeded":
                    print(f"[{region}] rate limited — backing off 5 s")
                    time.sleep(5)
                else:
                    print(f"[{region}][{instance_id}] unexpected error: {e}")

    print("lambda_handler: fleet sweep complete")
