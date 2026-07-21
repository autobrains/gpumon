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
# Update this constant to "main" once feature/dockerize is merged.
DOCKER_BRANCH  = "feature/dockerize"

_VALID_BRANCH_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9/_.-]{0,99}$')

def _validate_branch(branch: str) -> str:
    """Raise ValueError if branch name contains shell-unsafe characters."""
    if not _VALID_BRANCH_RE.match(branch):
        raise ValueError(f"Unsafe branch name rejected: {branch!r}")
    return branch

SSM_COMMAND_POLL_INTERVAL = 5    # seconds between status polls
SSM_CHECK_TIMEOUT   = 30         # quick is-running checks
SSM_FIX_TIMEOUT     = 180        # re-clone + rebuild
SSM_INSTALL_TIMEOUT = 900        # full Docker install (apt + image pull + build)
SSM_MIGRATE_TIMEOUT = 1200       # migration: stop old + full reinstall

GPUMON_REPO = "https://github.com/autobrains/gpumon.git"
GPUMON_DIR  = "/root/gpumon"
SENTINEL    = "/var/log/gpumon.finished"

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


def handle_check(ec2, ssm, instance_id: str) -> None:
    """Verify gpumon is running (Docker or legacy) and update the tag.
    Migration from legacy to Docker is triggered manually via GPUMON=MIGRATE.
    """
    if is_gpumon_running(ssm, instance_id):
        update_tag(ec2, instance_id, "ACTIVE")
    else:
        update_tag(ec2, instance_id, "FAILED")


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

    for region in REGIONS:
        ec2, ssm = get_clients(region)

        try:
            instances = get_all_instances(ec2)
        except ClientError as e:
            print(f"[{region}] error listing instances: {e}")
            continue

        for instance in instances:
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
                    handle_check(ec2, ssm, instance_id)

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
