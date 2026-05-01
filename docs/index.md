---
title: gpumon
layout: default
---

# gpumon

**Containerised GPU/CPU fleet monitoring, idle-shutdown automation, and Slack alerting for AWS EC2.**

No SSH. No dashboards to maintain. Drop a tag on an instance — the fleet manages itself.

---

## What it does

Each monitored EC2 instance runs gpumon as a Docker container. Every 10–60 seconds it publishes custom CloudWatch metrics for GPU utilisation, CPU, memory, disk, power draw, and temperature. When a machine looks idle, it alerts the responsible employee on Slack and — if the idle condition persists for two hours — stops or terminates the instance automatically.

A scheduled AWS Lambda function sweeps every region, reads a single `GPUMON` EC2 tag, and installs, health-checks, fixes, or removes the container entirely, without any SSH access.

---

## Core features

| Feature | Detail |
|---------|--------|
| **GPU monitoring** | Per-GPU utilisation, VRAM, power, temperature via NVML |
| **CPU monitoring** | Per-core utilisation, network throughput |
| **Host metrics** | Root disk free, RAM usage, top memory/CPU process |
| **CloudWatch** | Three custom namespaces: `GPU-metrics-with-team-tag`, `CPU-metrics`, `Host-metrics` |
| **Idle shutdown** | Alarm pilot light → 2 h hold → stop/terminate via Secrets Manager credentials |
| **Slack DMs** | Employee paged on idle, low disk, high memory (per-alert cooldowns) |
| **Lambda fleet manager** | Install, health-check, progressive fix, migration, delete — via EC2 tags |
| **Auto-update** | Systemd timer: `git pull` + `docker compose up -d --build` on new commits |
| **SPOT-aware** | `GPUMON_POLICY=SPOT` disables employee DMs; raises idle thresholds |

---

## How deployment works

```
Set GPUMON=install on any EC2 instance
        │
        ▼
Lambda runs autoinstall.sh via SSM
        │
        ├── Installs Docker + NVIDIA Container Toolkit (if GPU)
        ├── Builds container from repo
        ├── Starts container (network_mode: host, pid: host)
        ├── Installs halt_it.sh to host crontab
        └── Registers systemd auto-update timer
        │
        ▼
GPUMON tag → ACTIVE
```

From that point, the Lambda health-checks the instance on every sweep. If it detects a failure it runs a progressive fix (fast git pull first, full reinstall second). If neither works, it sets `NOT_FIXED` for manual attention.

---

## Idle-shutdown flow

```
gpumon.py / cpumon.py
  └── every 10 s: is GPU/CPU below threshold AND network quiet?
        └── yes for restart_backoff seconds → alarm_pilot_light = 1
              └── Slack: team channel alert + employee DM (4 h cooldown)

halt_it.sh (host crontab, every 10 min)
  └── collects log lines timestamped within policy window
        └── no activity spike AND ≥ 90% sample coverage?
              └── fetch creds from Secrets Manager
                    └── aws ec2 stop-instances (or terminate)
```

To cancel: `sudo bash /root/gpumon/kill_halt.sh` — writes a 2-hour backoff file.

---

## Slack alerts

Alerts reach the right person automatically. The `Employee` EC2 tag (email or display name) is resolved to a Slack user via the Bot API. The `Team` tag routes team-channel messages.

| Event | Who gets it | Cooldown |
|-------|------------|---------|
| Instance idle, will stop in ~3 h | Team channel + Employee DM | 4 h |
| Activity resumed | Team channel only | — |
| Disk < threshold | Employee DM | 12 h |
| Memory > threshold | Employee DM | 12 h |

---

## GPUMON policy presets

| Policy | Idle backoff | CPU thresh | GPU thresh | Network thresh |
|--------|-------------|-----------|-----------|---------------|
| `STANDARD` | 1 h | 10 % | 10 % | 15 000 pkt |
| `RELAXED` | 2 h | 5 % | 10 % | 10 000 pkt |
| `SEVERE` | 0 | 20 % | 2 % | 15 000 pkt |
| `SPOT` | 0 | 20 % | 10 % | 30 000 pkt |
| `SUSPEND` | 10 days | 10 % | 10 % | 15 000 pkt |

---

## Getting started

### Single instance (manual)

```bash
git clone https://github.com/autobrains/gpumon.git /root/gpumon
cp /root/gpumon/.env.example /root/gpumon/.env
# edit .env — add Slack bot token secret name, thresholds
sudo bash /root/gpumon/autoinstall.sh
```

### Managed fleet (Lambda)

1. Deploy `lambda_manager.py` as a Lambda function (Python 3.12).
2. Grant the Lambda the EC2 and SSM permissions listed in the [README](https://github.com/autobrains/gpumon#lambda-fleet-manager).
3. Schedule it with EventBridge (every 5–10 minutes).
4. Tag any EC2 instance with `GPUMON = install` — the Lambda does the rest.

---

## Pages

- [Architecture](architecture) — component design, file roles, data flow
- [Operations guide](operations) — day-to-day tag operations, migration, troubleshooting

---

## Source

[github.com/autobrains/gpumon](https://github.com/autobrains/gpumon) — Apache 2.0
