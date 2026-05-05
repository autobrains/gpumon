# gpumon — AWS EC2 GPU/CPU Fleet Monitor

Containerised CloudWatch metrics, idle-shutdown automation, and Slack alerting for GPU and CPU EC2 instances. Deployed and managed entirely through EC2 tags — no SSH required.

---

## Overview

gpumon runs as a Docker container on each monitored EC2 instance. It continuously publishes custom CloudWatch metrics, watches for idle conditions, and pages the responsible employee via Slack DM when intervention is needed. The host-side `halt_it.sh` script, driven by those metrics, stops or terminates the instance after a sustained idle period.

A Lambda function acts as a fleet manager: it reads the `GPUMON` tag on every EC2 instance and installs, health-checks, fixes, migrates, or removes gpumon entirely — all without operator SSH access.

```
┌───────────────────────────────────────────────────────────────────────┐
│  EC2 Instance                                                         │
│                                                                       │
│  ┌─────────────────────────────────────────────────┐                  │
│  │  Docker Container (gpumon)                      │                  │
│  │                                                 │                  │
│  │  docker-entrypoint.sh                           │                  │
│  │    ├── gpumon.py  (GPU instances)  ─────────────┼──► CloudWatch   │
│  │    │   or cpumon.py (CPU only)    ─────────────┼──► GPU/CPU-metrics│
│  │    └── hostmon.py (always)        ─────────────┼──► Host-metrics  │
│  │                                                 │                  │
│  │  /tmp:/tmp  bind-mount ◄────────────────────────┼─ shared logs     │
│  └─────────────────────────────────────────────────┘                  │
│                                                                       │
│  Host crontab (every 10 min)                                          │
│    └── halt_it.sh  ──► reads /tmp/GPU_TEMP_* or CPUMON_LOGS_*        │
│                    ──► if idle 2 h: stop/terminate instance           │
│                                                                       │
│  Systemd timer (5 min post-boot, then hourly)                         │
│    └── gpumon-update.sh  ──► git pull + docker compose up -d --build  │
└───────────────────────────────────────────────────────────────────────┘

       Lambda (scheduled, all regions)
         └── reads GPUMON tag ──► install / check / fix / migrate / delete
```

---

## Quick Start — single instance

```bash
# 1. Clone the repo (Lambda does this automatically for managed fleets)
git clone https://github.com/autobrains/gpumon.git /root/gpumon

# 2. (Optional) configure Slack and alert thresholds
cp /root/gpumon/.env.example /root/gpumon/.env
$EDITOR /root/gpumon/.env

# 3. Run the idempotent installer
sudo bash /root/gpumon/autoinstall.sh
```

`autoinstall.sh` installs Docker, the NVIDIA Container Toolkit (GPU instances only), builds the image, starts the container, installs `halt_it.sh` to the host crontab, and registers a systemd timer for hourly auto-updates. Re-running it is safe — remove `/var/log/gpumon.finished` to force a full reinstall.

---

## EC2 Tags Reference

| Tag | Required | Values / Default | Purpose |
|-----|----------|-----------------|---------|
| `GPUMON` | **Yes** | see state machine below | Drives Lambda fleet actions |
| `GPUMON_BRANCH` | No | default: `feature/dockerize` | Branch to clone during install or migrate; update `DOCKER_BRANCH` constant after merge |
| `GPUMON_POLICY` | No | `STANDARD` *(default)* | Idle-shutdown sensitivity |
| `Team` | No | e.g. `ML_TEAM` | CloudWatch dimension |
| `Employee` | No | Name or `user@email.com` | Slack DM recipient for personal alerts |
| `PAGE_EMPLOYEE` | No | `True` *(default)* / `False` | Set to `False` to suppress employee DMs |
| `Name` | No | human-readable name | Used in Slack alert messages instead of instance ID |

### GPUMON tag state machine

```
                  ┌─────────┐
  (set manually)  │ install │──────────────────────────────────┐
                  └─────────┘                                  │
                                                               ▼
  (set manually)  ┌─────────┐     install OK?     ┌────────────────┐
                  │PENDING_ │──── Docker running ─►│    ACTIVE      │◄──┐
                  │   SSM   │                      └────────────────┘   │
                  └─────────┘                             │ check fails  │
                                                          ▼             │
                  ┌─────────┐     fix succeeds     ┌────────────────┐  │
                  │ FAILED  │◄────────────────────  │    FAILED      │  │
                  └─────────┘     step 1 or 2 OK──►│                │  │
                       │                            └────────────────┘  │
                       │ no Docker (legacy)                 │ fix OK     │
                       ▼                                    └───────────┘
                  ┌─────────┐
                  │NOT_FIXED│  (manual attention required)
                  └─────────┘

  (set manually)  ┌─────────┐     migration OK    ┌────────────────┐
                  │ MIGRATE │────────────────────►│    ACTIVE      │
                  └─────────┘                      └────────────────┘

  (set manually)  ┌─────────┐     always clears
                  │ DELETE  │────────────────────► tag = ""
                  └─────────┘

  instance not running ──► INACTIVE (any non-empty tag)
```

**Lambda sweep actions by tag value:**

| Tag value | Lambda action |
|-----------|--------------|
| `install` | Clone `GPUMON_BRANCH` (default `feature/dockerize`), run `autoinstall.sh` |
| `PENDING_SSM` | Retry install (SSM agent was absent on first attempt) |
| `ACTIVE` | Health-check; set `FAILED` if not running |
| `INACTIVE` | Health-check when instance comes back up |
| `FAILED` | Progressive fix: step 1 = `git pull` + rebuild; step 2 = full reinstall |
| `NOT_FIXED` | Skipped — requires manual investigation |
| `MIGRATE` | Stop legacy processes/units, clone Docker branch, run `autoinstall.sh` |
| `DELETE` | `docker compose down`, remove timer, crontab entry, and repo |
| *(empty)* | Ignored |

---

## GPUMON_POLICY Tag

Controls idle-detection sensitivity in `halt_it.sh` and the Python monitors.

| Policy | Restart backoff | CPU threshold | GPU threshold | Network threshold |
|--------|----------------|--------------|--------------|------------------|
| `STANDARD` | 1 h | 10 % | 10 % | 15 000 pkts |
| `RELAXED` | 2 h | 5 % | 10 % | 10 000 pkts |
| `SEVERE` | 0 | 20 % | 2 % | 15 000 pkts |
| `SPOT` | 0 | 20 % | 10 % | 30 000 pkts |
| `SUSPEND` | 10 days | 10 % | 10 % | 15 000 pkts |

`SPOT` additionally suppresses all employee Slack DMs regardless of the `PAGE_EMPLOYEE` tag.

---

## Environment Variables (`.env`)

Copy `.env.example` to `.env` in the repo directory before starting the container.

| Variable | Default | Description |
|----------|---------|-------------|
| `GPUMON_SLACK_SECRET_ID` | `IT/SLACK_BOT_TOKEN` | Secrets Manager secret for the Slack Bot token |
| `GPUMON_SLACK_SECRET_REGION` | `eu-west-1` | Region where the Slack secret lives |
| `GPUMON_SECRET_ID` | `AB/InstanceRole` | Secrets Manager secret for stop/terminate credentials |
| `GPUMON_SECRET_REGION` | `eu-west-1` | Region where the stop-credential secret lives |
| `DISK_ALERT_FREE_PCT` | `10` | DM employee when root disk free falls below this % |
| `MEMORY_ALERT_USED_PCT` | `90` | DM employee when RAM usage exceeds this % |
| `ALERT_COOLDOWN_HOURS` | `12` | Minimum hours between repeat disk/memory DMs |
| `SHUTDOWN_ALERT_COOLDOWN_HOURS` | `4` | Minimum hours between repeat idle-shutdown DMs |

---

## Slack Notifications

### Setup — Slack Bot (recommended)

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps).
2. Grant OAuth scopes: `users:read`, `users:read.email`, `im:write`, `chat:write`.
3. Install to workspace; copy the **Bot User OAuth Token** (`xoxb-…`).
4. Store it in AWS Secrets Manager as a plaintext secret in `eu-west-1` (or the region set by `GPUMON_SLACK_SECRET_REGION`).

The `Employee` EC2 tag can be an email address (`user@company.com`) for exact lookup, or a display name for fuzzy match. Email is recommended — it's unambiguous and faster.

### Alert types and routing

| Event | Recipient | Cooldown |
|-------|-----------|---------|
| Alarm pilot light ON (instance idle, will stop) | Employee DM | 4 h |
| Disk free < `DISK_ALERT_FREE_PCT` | Employee DM | 12 h |
| Memory used > `MEMORY_ALERT_USED_PCT` | Employee DM | 12 h |

Employee DMs are suppressed when `GPUMON_POLICY=SPOT` or `PAGE_EMPLOYEE=False`.

---

## CloudWatch Metrics

### GPU instances — namespace `GPU-metrics-with-team-tag`

| Metric | Unit | Notes |
|--------|------|-------|
| GPU Usage | Percent | Per-GPU utilisation |
| Memory Usage | Percent | GPU VRAM utilisation |
| Power Usage (Watts) | None | Via NVML |
| Temperature (C) | None | Via NVML |
| Average GPU Utilization | Percent | Mean across all GPUs |
| Alarm Pilot Light (1/0) | None | 1 = idle threshold crossed |
| CPU Utilization Low Tripped | None | 1 = CPU below threshold |
| Network Tripped | None | 1 = network below threshold |

Dimensions: `InstanceId`, `ImageId`, `InstanceType`, `GPUNumber`, `InstanceTag`, `EmployeeTag`

### CPU-only instances — namespace `CPU-metrics`

| Metric | Unit |
|--------|------|
| Alarm Pilot Light (1/0) | None |
| CPU Utilization Low Tripped | None |
| Network Tripped | None |

Dimensions: `InstanceId`, `ImageId`, `InstanceType`, `InstanceTag`, `EmployeeTag`

### All instances — namespace `Host-metrics`

| Metric | Unit |
|--------|------|
| Host Disk Free Bytes | Bytes |
| Host Disk Free Percent | Percent |
| Host Memory Used Percent | Percent |
| Host Top Memory Process Percent | Percent |
| Host Top CPU Process Percent | Percent |

`Host Top Memory Process Percent` includes a `TopMemoryProcessName` dimension; `Host Top CPU Process Percent` includes `TopCPUProcessName`. Dimensions also include `InstanceId`, `InstanceType`, `InstanceTag`, `EmployeeTag`.

---

## Idle-Shutdown — `halt_it.sh`

`halt_it.sh` runs on the **host** crontab every 10 minutes. It parses the monitor log files in `/tmp` and evaluates all log lines timestamped within the policy window. **Shutdown requires both conditions:**

1. **No activity spike** — every sample in the window shows `Alarm_Pilot_value:1` (GPU/CPU and network all below threshold for the entire window).
2. **≥ 90% sample coverage** — at least 90% of the expected 10-second samples exist in the window, preventing a brief monitoring gap or a 1-hour-idle / 1-hour-active pattern from triggering a false shutdown.

The policy window is 2 hours for STANDARD / RELAXED / SUSPEND and 15 minutes for SPOT / SEVERE.

If both conditions are met, `halt_it.sh`:

1. Writes a wall message to the terminal.
2. Fetches stop/terminate credentials from Secrets Manager (`GPUMON_SECRET_ID`).
3. Calls `aws ec2 stop-instances` or `terminate-instances` depending on policy.

To cancel a pending shutdown, run `/root/gpumon/kill_halt.sh` on the instance — it writes a backoff timestamp that `halt_it.sh` respects for 2 hours.

Log files written by the container are shared with the host via the `/tmp:/tmp` bind-mount, so `halt_it.sh` can read them without entering the container.

---

## Lambda Fleet Manager

`lambda_manager.py` is deployed as an AWS Lambda function (Python 3.12, recommended schedule: every 5–10 minutes via EventBridge).

**IAM permissions required:**

```json
{
  "Action": [
    "ec2:DescribeInstances",
    "ec2:DescribeTags",
    "ec2:CreateTags",
    "ec2:DescribeIamInstanceProfileAssociations",
    "ec2:AssociateIamInstanceProfile",
    "ec2:DisassociateIamInstanceProfile",
    "ssm:SendCommand",
    "ssm:GetCommandInvocation"
  ],
  "Resource": "*"
}
```

**SSM timeouts by operation:**

| Operation | `poll_timeout` | `execution_timeout` |
|-----------|--------------|-------------------|
| Health check | 30 s | 30 s |
| Fix (pull + rebuild) | 180 s | 180 s |
| Install (full Docker) | 900 s | 900 s |
| Migrate (stop legacy + full Docker) | 1200 s | 1200 s |

**Branch control:**

Set `GPUMON_BRANCH` on an instance to override which git branch is cloned. Defaults:

| Action | Default branch |
|--------|---------------|
| `install` | `feature/dockerize` (update `DOCKER_BRANCH` constant after merge) |
| `MIGRATE` | `feature/dockerize` (update `DOCKER_BRANCH` constant after merge) |

---

## Migration — Legacy to Docker

Instances that ran gpumon directly via systemd (without Docker) continue to work unchanged. When you are ready to migrate a specific instance:

1. Set the `GPUMON` tag to `MIGRATE` (optionally set `GPUMON_BRANCH` if you want a non-default branch).
2. The Lambda fleet manager will, on the next sweep:
   - Stop all legacy `gpumon`/`cpumon`/`gpumon-monitor` systemd units.
   - Kill any directly-running `gpumon.py` / `cpumon.py` / `hostmon.py` processes.
   - Strip legacy crontab entries.
   - Re-clone the repo at the target branch.
   - Run `autoinstall.sh` to install Docker and start the container.
   - Tag the instance `ACTIVE` on success, `FAILED` on failure.

The `/tmp:/tmp` bind-mount means alert state (`gpumon_alert_state.json`) and log files survive the migration — cooldown timers are preserved.

---

## File Reference

| File | Purpose |
|------|---------|
| `gpumon.py` | GPU metrics loop (pynvml → CloudWatch) |
| `cpumon.py` | CPU-only metrics loop (psutil → CloudWatch) |
| `hostmon.py` | Host disk/memory metrics + process top (psutil → CloudWatch) |
| `mon_utils.py` | Shared utilities: IMDS v2, tags, Slack, network stats, crontab, alerting |
| `slack_dm.py` | Slack Bot DM client (user lookup, channel open, message send) |
| `halt_it.sh` | Host crontab script: parses logs, stops/terminates idle instances |
| `kill_halt.sh` | Writes a 2-hour backoff timestamp to cancel a pending shutdown |
| `autoinstall.sh` | Idempotent Docker installer + halt_it.sh + systemd update timer |
| `docker-entrypoint.sh` | Detects GPU, launches gpumon.py or cpumon.py + hostmon.py |
| `Dockerfile` | `nvidia/cuda:12.2.0-base-ubuntu22.04` base, Python 3, boto3, pynvml |
| `docker-compose.yml` | GPU variant: `network_mode: host`, `pid: host`, `/tmp:/tmp`, all GPUs |
| `docker-compose.cpu.yml` | CPU standalone compose: no GPU config, forces `runtime: runc` |
| `lambda_manager.py` | AWS Lambda fleet manager (SSM, EC2 tags, IAM) |
| `requirements.txt` | Pinned Python dependencies |
| `.env.example` | Template for all configurable environment variables |

---

## Development

### Manual Docker commands

```bash
# GPU instance
docker compose up -d

# CPU-only instance
docker compose -f docker-compose.cpu.yml up -d

# Rebuild after code change
docker compose up -d --build

# Logs
docker logs gpumon-gpumon-1 -f

# Force full reinstall
sudo rm /var/log/gpumon.finished && sudo bash /root/gpumon/autoinstall.sh
```

### Branch strategy during Docker rollout

- `main` — legacy (non-Docker) gpumon; existing instances track this branch.
- `feature/dockerize` — Docker deployment; new installs and migrations use this branch.

When `feature/dockerize` is merged to `main`:

1. Update `DOCKER_BRANCH = "main"` in `lambda_manager.py` and redeploy Lambda.
2. Retag existing Docker instances (`GPUMON_BRANCH=main`, `GPUMON=MIGRATE`) so they re-clone from `main`.
3. Delete the `feature/dockerize` branch.

Auto-updates (`gpumon-update.sh`, fired by the systemd timer) pull from whichever branch the instance was cloned at. No special logic is required — git tracks the upstream automatically.

---

## License

Apache License 2.0 — originally from Amazon Web Services, extended by Paul Seifer, Autobrains LTD.
