# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Licensed under the Apache License, Version 2.0
# Shared utilities for gpumon.py, cpumon.py, and hostmon.py — (c) Paul Seifer, Autobrains LTD

from __future__ import annotations

import fcntl
import glob
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.request import Request, urlopen

import boto3
import psutil

if TYPE_CHECKING:
    from slack_dm import SlackDMClient

log = logging.getLogger(__name__)

# ── IMDS ──────────────────────────────────────────────────────────────────────

_IMDS_BASE = "http://169.254.169.254/latest/meta-data/"
_IMDS_TOKEN_URL = "http://169.254.169.254/latest/api/token"
_TOKEN_TTL_SEC = 21600       # 6 h
_TOKEN_REFRESH_AT = 19800    # refresh at 5.5 h to stay ahead of expiry

_token: str = ""
_token_fetched_at: float = 0.0


def _get_token() -> str:
    global _token, _token_fetched_at
    if time.time() - _token_fetched_at > _TOKEN_REFRESH_AT:
        req = Request(_IMDS_TOKEN_URL, None,
                      {"X-aws-ec2-metadata-token-ttl-seconds": str(_TOKEN_TTL_SEC)},
                      method="PUT")
        _token = urlopen(req, timeout=5).read().decode()
        _token_fetched_at = time.time()
    return _token


def _imds(path: str) -> str:
    req = Request(_IMDS_BASE + path, None,
                  {"X-aws-ec2-metadata-token": _get_token()}, method="GET")
    return urlopen(req, timeout=5).read().decode()


def fetch_instance_metadata() -> dict[str, str]:
    az = _imds("placement/availability-zone")
    return {
        "instance_id":   _imds("instance-id"),
        "image_id":      _imds("ami-id"),
        "instance_type": _imds("instance-type"),
        "az":            az,
        "region":        az[:-1],
        "hostname":      _imds("hostname"),
    }


# ── Policy ────────────────────────────────────────────────────────────────────

POLICIES: dict[str, dict[str, Any]] = {
    "RELAXED":  {"restart_backoff": 7200,   "cpu_threshold": 5,  "gpu_threshold": 10, "network_threshold": 10000, "shutdown_eta": "~2 hours"},
    "SEVERE":   {"restart_backoff": 0,      "cpu_threshold": 20, "gpu_threshold": 2,  "network_threshold": 15000, "shutdown_eta": "~15 minutes"},
    "SPOT":     {"restart_backoff": 0,      "cpu_threshold": 20, "gpu_threshold": 10, "network_threshold": 30000, "shutdown_eta": "~15 minutes"},
    "SUSPEND":  {"restart_backoff": 864000, "cpu_threshold": 10, "gpu_threshold": 10, "network_threshold": 15000, "shutdown_eta": "~2 hours"},
    "STANDARD": {"restart_backoff": 0,      "cpu_threshold": 10, "gpu_threshold": 10, "network_threshold": 15000, "shutdown_eta": "~1 hour"},
}


def get_policy_config(policy: str) -> dict[str, int]:
    cfg = POLICIES.get(policy, POLICIES["STANDARD"])
    print(f"POLICY TAG detected: {policy} → {cfg}")
    return cfg


# ── Tags ──────────────────────────────────────────────────────────────────────

def get_instance_tags(ec2_client: Any, instance_id: str) -> dict[str, str]:
    response = ec2_client.describe_tags(
        Filters=[{"Name": "resource-id", "Values": [instance_id]}]
    )
    return {t["Key"]: t["Value"] for t in response["Tags"]}


def create_tag(ec2_client: Any, instance_id: str, tag_name: str, default_value: str) -> None:
    ec2_client.create_tags(
        Resources=[instance_id],
        Tags=[{"Key": tag_name, "Value": default_value}],
    )
    print(f"Tag '{tag_name}' = '{default_value}' added to {instance_id}.")


# ── Network ───────────────────────────────────────────────────────────────────

def get_network_stats(cw_client: Any, instance_id: str, prev_network: float) -> float:
    """Return combined NetworkPacketsIn + NetworkPacketsOut over the last 5 min."""
    end = datetime.utcnow()
    start = end - timedelta(minutes=5)
    fmt = "%Y-%m-%dT%H:%M:%SZ"

    def _query(metric_name: str) -> float | None:
        data = cw_client.get_metric_statistics(
            Period=60,
            StartTime=start.strftime(fmt),
            EndTime=end.strftime(fmt),
            MetricName=metric_name,
            Namespace="AWS/EC2",
            Statistics=["Maximum"],
            Unit="Count",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        )
        # GetMetricStatistics does not guarantee chronological order — sort first.
        pts = sorted(data.get("Datapoints", []), key=lambda p: p["Timestamp"], reverse=True)
        return pts[0]["Maximum"] if pts else None

    packets_in = _query("NetworkPacketsIn")
    packets_out = _query("NetworkPacketsOut")

    if packets_in is None or packets_out is None:
        # CW basic monitoring has a ~1-2 min publication lag; brief gaps are normal.
        # Return the previous value so a short CW gap doesn't invalidate the halt window.
        # A sustained 15-min CW outage (needed to trigger a false idle) is far less
        # likely than the old 2M sentinel repeatedly blocking legitimate shutdowns.
        print(f"NetworkPacketsIn={packets_in} NetworkPacketsOut={packets_out}: "
              f"CW datapoint missing — reusing prev_network={prev_network}")
        return prev_network

    return round(packets_in) + round(packets_out)


# ── CPU ───────────────────────────────────────────────────────────────────────

def seconds_elapsed() -> float:
    return time.time() - psutil.boot_time()


def get_per_core_cpu_utilization() -> list[float]:
    return psutil.cpu_percent(interval=1, percpu=True)


def calculate_average_core_utilization(cache: list[list[float]]) -> list[float]:
    return [sum(d) / len(d) if d else 0.0 for d in cache]


# ── Crontab ───────────────────────────────────────────────────────────────────

_CRON_LOCK = "/tmp/gpumon_crontab.lock"


def ensure_halt_it_crontab(cron_job: str) -> None:
    """Add halt_it.sh cron entry exactly once, even if gpumon and cpumon start simultaneously."""
    try:
        with open(_CRON_LOCK, "w") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            result = subprocess.run(
                ["crontab", "-l"], capture_output=True, text=True
            )
            existing = result.stdout if result.returncode == 0 else ""
            if "halt_it.sh" in existing:
                print("halt_it.sh already in crontab, skipping")
                return
            new_crontab = existing.rstrip("\n") + "\n" + cron_job + "\n"
            proc = subprocess.Popen(["crontab", "-"], stdin=subprocess.PIPE, text=True)
            proc.communicate(input=new_crontab)
            if proc.returncode == 0:
                print("halt_it.sh cron job added")
            else:
                print("Failed to update crontab")
    except Exception as exc:
        print(f"ensure_halt_it_crontab error: {exc}")


# ── Log cleanup ───────────────────────────────────────────────────────────────

def cleanup_old_logs(prefix: str, max_age_hours: int = 48) -> None:
    """Delete log files matching prefix that are older than max_age_hours."""
    cutoff = time.time() - max_age_hours * 3600
    for path in glob.glob(f"{prefix}*"):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                print(f"Removed old log: {path}")
        except OSError:
            pass


# ── Alert rate limiting ───────────────────────────────────────────────────────
# State persisted to /tmp so it survives container restarts via /tmp:/tmp mount.
# The lock file serialises concurrent access between gpumon and cpumon so the
# check and record happen atomically — preventing duplicate DMs.

_ALERT_STATE_FILE = "/tmp/gpumon_alert_state.json"
_ALERT_LOCK_FILE  = "/tmp/gpumon_alert_state.lock"


def try_record_alert(key: str, cooldown_hours: float) -> bool:
    """Atomically check cooldown and record the alert if it should be sent.

    Returns True if the alert should be sent (cooldown elapsed or first time).
    Holds an exclusive lock for the entire read-check-write sequence so
    concurrent gpumon and cpumon processes cannot both pass the cooldown check.
    """
    try:
        with open(_ALERT_LOCK_FILE, "w") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            # Read current state under lock
            try:
                with open(_ALERT_STATE_FILE) as fh:
                    state = json.load(fh)
            except (FileNotFoundError, json.JSONDecodeError):
                state = {}
            # Check cooldown
            last_sent = state.get(key)
            if last_sent is not None and time.time() - float(last_sent) <= cooldown_hours * 3600:
                return False
            # Record and write atomically
            state[key] = time.time()
            tmp = _ALERT_STATE_FILE + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(state, fh)
            os.replace(tmp, _ALERT_STATE_FILE)
            return True
    except OSError as exc:
        print(f"try_record_alert error: {exc}")
        return False


# ── Slack DM client factory ───────────────────────────────────────────────────

def fetch_slack_bot_token(secret_id: str, secret_region: str) -> str | None:
    """Fetch the Slack Bot OAuth token from Secrets Manager. Returns None on failure."""
    try:
        sm = boto3.client("secretsmanager", region_name=secret_region)
        resp = sm.get_secret_value(SecretId=secret_id)
        token = (resp.get("SecretString") or "").strip()
        if not token:
            print(f"fetch_slack_bot_token: secret '{secret_id}' is empty")
            return None
        if not token.startswith("xoxb-"):
            print(f"fetch_slack_bot_token: value does not look like a Bot token (xoxb-...)")
        return token
    except Exception as exc:
        print(f"fetch_slack_bot_token: could not fetch '{secret_id}' from {secret_region}: {exc}")
        return None


def build_slack_dm_client(secret_id: str, secret_region: str) -> SlackDMClient | None:
    """Fetch bot token and build a SlackDMClient. Returns None if unavailable."""
    token = fetch_slack_bot_token(secret_id, secret_region)
    if not token:
        return None
    try:
        from slack_dm import SlackDMClient  # local import to avoid circular dep
        return SlackDMClient(token)
    except Exception as exc:
        print(f"build_slack_dm_client: failed to initialise: {exc}")
        return None
