---
title: Architecture — gpumon
layout: default
---

# Architecture

[← Home](index)

---

## Component map

```
┌──────────────────────────── EC2 Instance ────────────────────────────────┐
│                                                                          │
│   ┌────────────────────────── Docker Container ──────────────────────┐  │
│   │                                                                  │  │
│   │  docker-entrypoint.sh                                            │  │
│   │    │                                                             │  │
│   │    ├── nvidia-smi present?                                       │  │
│   │    │     yes → python3 gpumon.py   (GPU loop, 10 s interval)     │  │
│   │    │     no  → python3 cpumon.py   (CPU loop, 10 s interval)     │  │
│   │    │                                                             │  │
│   │    └── python3 hostmon.py &        (host metrics, 60 s interval) │  │
│   │                                                                  │  │
│   │  Python monitors read from:                                      │  │
│   │    pynvml  → GPU stats (gpumon only)                             │  │
│   │    psutil  → CPU, memory, disk, process list                     │  │
│   │    boto3   → CloudWatch put_metric_data, EC2 tags                │  │
│   │    IMDS v2 → instance-id, region, type, AZ                       │  │
│   │                                                                  │  │
│   │  Shared state in /tmp (bind-mounted to host):                    │  │
│   │    GPU_TEMP_YYYY-MM-DDTHH       ← gpumon log (halt_it reads)     │  │
│   │    CPUMON_LOGS_YYYY-MM-DDTHH   ← cpumon log (halt_it reads)     │  │
│   │    HOSTMON_LOGS_YYYY-MM-DDTHH  ← hostmon log (never read by      │  │
│   │                                               halt_it)           │  │
│   │    gpumon_alert_state.json     ← DM cooldown timestamps          │  │
│   │    gpumon_crontab.lock         ← fcntl lock for crontab writes   │  │
│   │    timestamp.txt               ← kill_halt.sh backoff file       │  │
│   │                                                                  │  │
│   └──────────────────────────────────────────────────────────────────┘  │
│                             │  /tmp:/tmp                                 │
│   Host                      ▼                                           │
│   ┌─────────────────────────────────────────────────────────────────┐   │
│   │  crontab (*/10 * * * *)                                         │   │
│   │    └── /usr/local/sbin/halt_it.sh                               │   │
│   │         reads GPU_TEMP_* or CPUMON_LOGS_*                       │   │
│   │         pilot light held 2 h → stop/terminate instance          │   │
│   │                                                                 │   │
│   │  systemd: gpumon-update.timer (5 min post-boot, then hourly)    │   │
│   │    └── /usr/local/sbin/gpumon-update.sh                         │   │
│   │         git -C /root/gpumon pull                                │   │
│   │         new commits → docker compose up -d --build              │   │
│   └─────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────┘

         ▲                              ▲
         │ CloudWatch                   │ Slack Bot API
         │ put_metric_data              │ (Secrets Manager → xoxb- token)
         │                              │
┌────────────────┐             ┌─────────────────────┐
│  CloudWatch    │             │  Slack workspace     │
│  3 namespaces  │             │  employee DMs +      │
│  custom metrics│             │  team channel hooks  │
└────────────────┘             └─────────────────────┘

AWS Lambda (EventBridge schedule, every 5–10 min)
  └── lambda_manager.py
        reads GPUMON tag on all instances in all regions
        sends SSM RunShellScript commands
        updates GPUMON tag with result
```

---

## File roles

### `gpumon.py`

GPU metrics loop. Runs inside the container on GPU instances.

- Initialises NVML; iterates over all GPU handles every 10 seconds.
- Maintains a rolling 5-minute window of per-core CPU utilisation.
- Tracks `alarm_pilot_light` (0/1): set to 1 when GPU, CPU, and network are all below policy thresholds for the configured backoff period.
- Log line format is fixed for `halt_it.sh` compatibility: the `Alarm_Pilot_value` field falls at colon-split index 12.
- On pilot light ON: sends employee DM (4 h cooldown, suppressed on SPOT/PAGE_EMPLOYEE=False).

### `cpumon.py`

Same structure as `gpumon.py` without NVML. Runs on CPU-only instances.

- Log format: `Alarm_Pilot_value` at colon-split index 6 (fewer fields before it — no GPU columns).
- Installs `halt_it.sh` host crontab entry via `ensure_halt_it_crontab()` (fcntl-locked against the parallel `gpumon.py` start).

### `hostmon.py`

Host-level metrics. Always runs as a background process alongside gpumon or cpumon.

- Reports to `Host-metrics` CloudWatch namespace every 60 seconds.
- Writes its own log (`HOSTMON_LOGS_*`) which `halt_it.sh` **never reads** — isolation is intentional.
- Sends employee DMs for disk and memory alerts independently of the pilot light.

### `mon_utils.py`

Shared utility module imported by all three monitors.

Key responsibilities:
- **IMDS v2**: token fetch with 5.5-hour proactive refresh (before 6 h TTL expiry).
- **Policy config**: `POLICIES` dict maps tag value → thresholds.
- **Alert rate limiting**: reads/writes `/tmp/gpumon_alert_state.json` with atomic `os.replace()` + `fcntl.LOCK_EX` to prevent concurrent corruption by gpumon + cpumon.
- **`ensure_halt_it_crontab`**: `fcntl.LOCK_EX` prevents the double-add race when both gpumon and cpumon start simultaneously.
- **`build_slack_dm_client`**: fetches bot token from Secrets Manager and initialises `SlackDMClient`; returns `None` on any failure so callers never crash.

### `slack_dm.py`

Slack Bot DM client. Stateful within one process lifetime; caches user IDs and DM channel IDs.

- `find_user_id`: email in `Employee` tag → `users.lookupByEmail` (fast); otherwise paginates `users.list` and matches `real_name` / `display_name` (case-insensitive).
- `_open_dm_channel`: `conversations.open` result cached per `user_id`.
- `send_dm`: orchestrates lookup → open → `chat.postMessage`.
- Gracefully degrades if `slack-sdk` is not installed (ImportError caught at import time).

### `halt_it.sh`

Runs on the **host** crontab, not inside the container. Reads from `/tmp` which is shared via the bind-mount.

- Parses log files line by line; uses `cut -d: -f6` (CPU) or `cut -d: -f12` (GPU) to extract `Alarm_Pilot_value`.
- Counts recent lines where pilot = 1; if that count spans ≥ 2 hours → stop/terminate.
- Credentials fetched from Secrets Manager at `GPUMON_SECRET_REGION` (defaults `eu-west-1`) — deliberately separate from the instance region.
- AWS CLI health-checked with `aws sts get-caller-identity` before attempting any stop.

### `lambda_manager.py`

Fleet manager Lambda. No persistent state — everything is derived from EC2 tags.

- Sweeps all configured `REGIONS` on every invocation.
- Dispatches by `GPUMON` tag value to handler functions.
- All SSM commands are sent as `AWS-RunShellScript` and polled until terminal status.
- Progressive fix: step 1 (`git pull` + `docker compose up -d --build`) before step 2 (full `autoinstall.sh`).
- Migration guard: refuses to run Docker fix commands on instances without Docker installed (avoids inadvertent migration).

---

## Data flow — metrics to shutdown

```
pynvml / psutil
    │
    ▼
gpumon.py / cpumon.py
    │  10 s loop
    ├──► CloudWatch put_metric_data
    └──► /tmp/GPU_TEMP_* (or CPUMON_LOGS_*)
              │
              │  every 10 min
              ▼
         halt_it.sh (host)
              │
              └── pilot light held 2 h?
                    │
                    ├── no  → exit 0
                    └── yes → Secrets Manager → aws ec2 stop-instances
```

```
psutil (disk, memory, processes)
    │
    ▼
hostmon.py
    │  60 s loop
    ├──► CloudWatch put_metric_data (Host-metrics namespace)
    ├──► /tmp/HOSTMON_LOGS_*
    └──► Slack DM (disk/memory thresholds, 12 h cooldown each)
```

---

## Security notes

- **No secrets in env vars at build time.** All credentials fetched from Secrets Manager at runtime via IAM instance role.
- **Bot token region**: the Slack secret may live in a different region (`GPUMON_SLACK_SECRET_REGION`) from the instance. The SM client is constructed with the explicit secret region.
- **Stop credentials region**: similarly, `GPUMON_SECRET_REGION` (defaults `eu-west-1`) is used for stop/terminate credentials regardless of the instance's own region.
- **Alert state atomicity**: `/tmp/gpumon_alert_state.json` is written via `os.replace()` on a temp file under `LOCK_EX` — safe for concurrent gpumon + cpumon access.
- **halt_it.sh AWS CLI check**: `aws sts get-caller-identity` with a 30 s timeout before any stop attempt — prevents actions when the AWS CLI is broken or the IAM role has been detached.
