---
title: Operations Guide — gpumon
layout: default
---

# Operations Guide

[← Home](index)

---

## Day-to-day tag operations

All fleet management is done by setting the `GPUMON` EC2 tag. The Lambda fleet manager acts on the tag within 5–10 minutes.

### Install gpumon on a new instance

1. Ensure the instance has the `EC2IAMRole` instance profile attached (the Lambda will attach it if missing, but the instance must be running).
2. Set `GPUMON = install` on the instance.
3. `GPUMON_BRANCH` defaults to `feature/dockerize` for installs. Set it explicitly only if you need a different branch.
4. Lambda installs Docker, builds the container, starts it, and sets the tag to `ACTIVE`.

### Remove gpumon from an instance

Set `GPUMON = DELETE`. Lambda runs `docker compose down`, disables the update timer, removes the halt_it.sh crontab entry, and wipes the repo directory. The tag is cleared to `""`.

### Force a health check

The Lambda health-checks all `ACTIVE` and `INACTIVE` instances on every sweep — no manual action needed.

To force an immediate check outside the sweep schedule, you can invoke the Lambda manually from the AWS console with an empty event `{}`.

### Cancel a pending shutdown

If `halt_it.sh` is about to stop an instance and you need to keep it running:

```bash
sudo bash /root/gpumon/kill_halt.sh
```

This writes a timestamp file that suppresses `halt_it.sh` for 2 hours. The Alarm Pilot Light metric will reset when the instance becomes active again.

### Suspend idle-shutdown temporarily

Set `GPUMON_POLICY = SUSPEND` on the instance. The restart backoff becomes 10 days, effectively preventing automatic shutdown for an extended period of inactivity.

### Disable employee DMs for an instance

Set `PAGE_EMPLOYEE = False`. The idle DM is suppressed. Useful for shared or unattended instances.

---

## Migrating legacy instances to Docker

Legacy instances (running gpumon directly via systemd, not Docker) continue working unchanged until you decide to migrate.

**To migrate a single instance:**

1. Set `GPUMON = MIGRATE` on the instance.
2. Optionally set `GPUMON_BRANCH = feature/dockerize` (this is the default for MIGRATE).
3. Lambda will:
   - Stop all legacy systemd units (`gpumon`, `cpumon`, `gpumon-monitor`).
   - Kill any bare `python gpumon.py / cpumon.py / hostmon.py` processes.
   - Remove legacy crontab entries.
   - Re-clone the repo at the target branch.
   - Run `autoinstall.sh` end-to-end.
   - Tag the instance `ACTIVE` on success, `FAILED` on failure.

**Alert cooldown state** (`/tmp/gpumon_alert_state.json`) and **log files** survive the migration because `/tmp` is bind-mounted. The employee will not receive duplicate alerts triggered by the brief monitoring gap.

**If a legacy instance goes FAILED** (gpumon process crashed), the Lambda will set it to `NOT_FIXED` rather than auto-installing Docker. Manual action: either restart the legacy service manually, or set `GPUMON = MIGRATE` to move to Docker.

---

## Merging `feature/dockerize` into `main`

When the Docker branch is ready for promotion:

1. Merge the PR on GitHub.
2. In `lambda_manager.py`, update:
   ```python
   DOCKER_BRANCH = "main"   # was "feature/dockerize"
   ```
3. Redeploy the Lambda.
4. Retag existing Docker instances so their local repos switch to tracking `main`:
   - Set `GPUMON_BRANCH = main` and `GPUMON = MIGRATE` on each Docker instance.
   - Lambda re-clones from `main` and restarts the container (~5 min downtime per instance).
5. Delete the `feature/dockerize` branch.

From that point, all installs and migrations use `main`.

---

## Troubleshooting

### GPUMON tag stuck at PENDING_SSM

The SSM agent on the instance was not reachable when Lambda first tried to install. This happens when:
- The instance was not yet fully booted.
- The SSM agent is not installed or not running.
- The instance profile does not include SSM permissions.

**Fix:** Lambda will retry automatically on the next sweep. If it persists after 15 minutes, SSH in and check `systemctl status amazon-ssm-agent`.

### GPUMON tag is FAILED

Lambda attempted the progressive fix (git pull + rebuild, then full reinstall) and could not restore the container. Possible causes:
- Docker daemon crashed or ran out of disk space.
- Network issue prevented `git pull` or image pull.
- `autoinstall.sh` failed mid-way.

**Fix:**
1. SSH in, check `docker logs gpumon-gpumon-1` and `journalctl -u gpumon-update`.
2. Check `df -h` — full disk is common.
3. Resolve the root cause, then set `GPUMON = install` to trigger a fresh install.

### GPUMON tag is NOT_FIXED

This appears on legacy instances that go FAILED (no Docker installed) or on Docker instances where both fix steps failed and the issue requires manual attention.

**Fix:** investigate the root cause, then either:
- Set `GPUMON = MIGRATE` to switch to Docker (legacy instances).
- Set `GPUMON = install` for a clean reinstall (Docker instances).

### Slack DMs not arriving

1. Confirm the bot token in Secrets Manager is valid (`xoxb-...`) and has the required scopes: `users:read`, `users:read.email`, `im:write`, `chat:write`.
2. Confirm the `Employee` tag matches the employee's Slack display name or email exactly (case-insensitive for display names).
3. Check container logs: `docker logs gpumon-gpumon-1 2>&1 | grep slack_dm`.
4. Confirm `PAGE_EMPLOYEE` is not `False` and `GPUMON_POLICY` is not `SPOT`.

### halt_it.sh not running / instance not stopping

1. Check host crontab: `crontab -l | grep halt_it`.
2. Check halt_it log: `cat /var/log/halt_it.log`.
3. Verify the AWS CLI works from the host: `aws sts get-caller-identity`.
4. Verify Secrets Manager credentials: `aws secretsmanager get-secret-value --secret-id AB/InstanceRole --region eu-west-1`.
5. Check that `/tmp/timestamp.txt` does not exist (kill_halt.sh backoff active).

### Container not starting after autoinstall

```bash
docker compose logs         # see build/startup errors
docker ps -a                # confirm container state
sudo bash /root/gpumon/autoinstall.sh   # re-run (idempotent)
```

If Docker itself will not start: `systemctl status docker` and `journalctl -u docker`.

---

## Useful commands

```bash
# Container status
docker ps --filter name=gpumon

# Live container logs
docker logs gpumon-gpumon-1 -f

# Tail halt_it log
tail -f /var/log/halt_it.log

# Tail update log
journalctl -u gpumon-update -f

# Manually trigger an update
sudo /usr/local/sbin/gpumon-update.sh

# Force full reinstall
sudo rm /var/log/gpumon.finished
sudo bash /root/gpumon/autoinstall.sh

# Cancel pending shutdown
sudo bash /root/gpumon/kill_halt.sh

# Check current GPUMON tag
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
REGION=$(curl -s http://169.254.169.254/latest/meta-data/placement/region)
aws ec2 describe-tags \
  --filters "Name=resource-id,Values=$INSTANCE_ID" "Name=key,Values=GPUMON" \
  --region "$REGION" --query "Tags[0].Value" --output text
```

---

## Environment variable quick reference

| Variable | Default | Description |
|----------|---------|-------------|
| `GPUMON_SLACK_SECRET_ID` | `AB/SlackBotToken` | SM secret for Slack Bot token |
| `GPUMON_SLACK_SECRET_REGION` | `eu-west-1` | Region of Slack secret |
| `GPUMON_SECRET_ID` | `AB/InstanceRole` | SM secret for stop/terminate credentials |
| `GPUMON_SECRET_REGION` | `eu-west-1` | Region of stop-credential secret |
| `DISK_ALERT_FREE_PCT` | `10` | Alert threshold — disk free % |
| `MEMORY_ALERT_USED_PCT` | `90` | Alert threshold — memory used % |
| `ALERT_COOLDOWN_HOURS` | `12` | Hours between disk/memory repeat DMs |
| `SHUTDOWN_ALERT_COOLDOWN_HOURS` | `4` | Hours between idle-shutdown repeat DMs |
