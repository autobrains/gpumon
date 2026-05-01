# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Licensed under the Apache License, Version 2.0
# Host-level metrics reporter for CloudWatch — (c) Paul Seifer, Autobrains LTD
#
# Reports to namespace "Host-metrics" every 60 seconds.  Completely independent
# from gpumon.py/cpumon.py — writes its own log file (/tmp/HOSTMON_LOGS_*) and
# never touches the GPU_TEMP_* or CPUMON_LOGS_* files that halt_it.sh reads.

from __future__ import annotations

import os
from datetime import datetime
from time import sleep

import boto3
import psutil

from mon_utils import (
    build_slack_dm_client,
    cleanup_old_logs,
    fetch_instance_metadata,
    get_instance_tags,
    record_alert_sent,
    should_send_alert,
)

NAMESPACE = "Host-metrics"
TMP_FILE_PREFIX = "/tmp/HOSTMON_LOGS_"
PUSH_INTERVAL = 60
STORE_RESO = 60


def _top_process_by_memory() -> tuple[str, float]:
    best_name, best_pct = "unknown", 0.0
    for proc in psutil.process_iter(["name", "memory_percent"]):
        try:
            pct = proc.info["memory_percent"] or 0.0
            if pct > best_pct:
                best_pct = pct
                best_name = proc.info["name"] or "unknown"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return best_name, round(best_pct, 2)


def _top_process_by_cpu() -> tuple[str, float]:
    # Seed cpu_percent counters, then sample 1 s later for meaningful values
    for proc in psutil.process_iter(["name"]):
        try:
            proc.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    sleep(1)
    best_name, best_pct = "unknown", 0.0
    for proc in psutil.process_iter(["name"]):
        try:
            pct = proc.cpu_percent(interval=None)
            if pct > best_pct:
                best_pct = pct
                best_name = proc.info["name"] or "unknown"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return best_name, round(best_pct, 2)


def _push_metrics(
    cw_client,
    dims: list[dict],
    disk_free_bytes: float,
    disk_free_pct: float,
    mem_used_pct: float,
    top_mem_proc: str,
    top_mem_pct: float,
    top_cpu_proc: str,
    top_cpu_pct: float,
) -> None:
    mem_dims = dims + [{"Name": "TopMemoryProcessName", "Value": top_mem_proc[:256]}]
    cpu_dims = dims + [{"Name": "TopCPUProcessName",    "Value": top_cpu_proc[:256]}]

    cw_client.put_metric_data(
        Namespace=NAMESPACE,
        MetricData=[
            {"MetricName": "Host Disk Free Bytes",              "Dimensions": dims,     "Unit": "Bytes",   "StorageResolution": STORE_RESO, "Value": disk_free_bytes},
            {"MetricName": "Host Disk Free Percent",            "Dimensions": dims,     "Unit": "Percent", "StorageResolution": STORE_RESO, "Value": disk_free_pct},
            {"MetricName": "Host Memory Used Percent",          "Dimensions": dims,     "Unit": "Percent", "StorageResolution": STORE_RESO, "Value": mem_used_pct},
            {"MetricName": "Host Top Memory Process Percent",   "Dimensions": mem_dims, "Unit": "Percent", "StorageResolution": STORE_RESO, "Value": top_mem_pct},
            {"MetricName": "Host Top CPU Process Percent",      "Dimensions": cpu_dims, "Unit": "Percent", "StorageResolution": STORE_RESO, "Value": top_cpu_pct},
        ],
    )


def _log_results(
    log_file: str,
    current_time: datetime,
    team: str,
    emp_name: str,
    disk_free_bytes: float,
    disk_free_pct: float,
    mem_used_pct: float,
    top_mem_proc: str,
    top_mem_pct: float,
    top_cpu_proc: str,
    top_cpu_pct: float,
) -> None:
    line = (
        f"[ {current_time} ] "
        f"tag:{team},"
        f"Employee:{emp_name},"
        f"DiskFreeBytes:{disk_free_bytes:.0f},"
        f"DiskFreePct:{disk_free_pct:.1f},"
        f"MemUsedPct:{mem_used_pct:.1f},"
        f"TopMemProc:{top_mem_proc}({top_mem_pct:.1f}%),"
        f"TopCPUProc:{top_cpu_proc}({top_cpu_pct:.1f}%)\n"
    )
    try:
        with open(log_file, "a") as fh:
            fh.write(line)
    except OSError as exc:
        print(f"hostmon log write error: {exc}")


def main() -> None:
    cleanup_old_logs(TMP_FILE_PREFIX, max_age_hours=48)

    meta = fetch_instance_metadata()
    region      = meta["region"]
    instance_id = meta["instance_id"]

    ec2 = boto3.client("ec2",        region_name=region)
    cw  = boto3.client("cloudwatch", region_name=region)

    tags = get_instance_tags(ec2, instance_id)
    instance_name = tags.get("Name",     "NO_NAME_TAG")
    team          = tags.get("Team",     "NO_TAG")
    emp_name      = tags.get("Employee", "NO_TAG")
    policy        = tags.get("GPUMON_POLICY", "STANDARD")

    # SPOT instances: never DM the employee
    page_employee = (
        policy != "SPOT"
        and tags.get("PAGE_EMPLOYEE", "True").lower() != "false"
    )

    # Alert thresholds from env (with defaults)
    disk_alert_free_pct  = float(os.getenv("DISK_ALERT_FREE_PCT",    "10"))
    mem_alert_used_pct   = float(os.getenv("MEMORY_ALERT_USED_PCT",  "90"))
    alert_cooldown_hours = float(os.getenv("ALERT_COOLDOWN_HOURS",   "12"))

    # Slack DM client — None if secret not configured, unreachable, or SPOT/disabled
    slack_secret_id     = os.getenv("GPUMON_SLACK_SECRET_ID", "AB/SlackBotToken")
    slack_secret_region = os.getenv("GPUMON_SLACK_SECRET_REGION", os.getenv("GPUMON_SECRET_REGION", "eu-west-1"))
    dm_client = build_slack_dm_client(slack_secret_id, slack_secret_region) if page_employee else None

    dims = [
        {"Name": "InstanceId",   "Value": instance_id},
        {"Name": "InstanceType", "Value": meta["instance_type"]},
        {"Name": "InstanceTag",  "Value": team},
        {"Name": "EmployeeTag",  "Value": emp_name},
    ]

    print(f"hostmon started — instance {instance_id} ({instance_name}) region {region} "
          f"team {team} page_employee={page_employee}")

    while True:
        current_time = datetime.now()
        log_file = TMP_FILE_PREFIX + current_time.strftime("%Y-%m-%dT%H")

        try:
            disk = psutil.disk_usage("/")
            disk_free_bytes = float(disk.free)
            disk_free_pct   = round(100.0 - disk.percent, 1)
            disk_free_gb    = round(disk_free_bytes / 1024 ** 3, 1)

            mem_used_pct = round(psutil.virtual_memory().percent, 1)

            top_mem_proc, top_mem_pct = _top_process_by_memory()
            top_cpu_proc, top_cpu_pct = _top_process_by_cpu()

            _log_results(
                log_file, current_time, team, emp_name,
                disk_free_bytes, disk_free_pct, mem_used_pct,
                top_mem_proc, top_mem_pct, top_cpu_proc, top_cpu_pct,
            )

            _push_metrics(
                cw, dims,
                disk_free_bytes, disk_free_pct, mem_used_pct,
                top_mem_proc, top_mem_pct, top_cpu_proc, top_cpu_pct,
            )

            # ── Employee DM alerts (disk & memory only) ──────────────────────
            if dm_client:
                if disk_free_pct < disk_alert_free_pct and should_send_alert("disk_alert", alert_cooldown_hours):
                    dm_client.send_dm(
                        emp_name,
                        f":warning: Disk space low on *{instance_name}*: "
                        f"only {disk_free_pct:.1f}% free ({disk_free_gb} GB remaining).",
                    )
                    record_alert_sent("disk_alert")

                if mem_used_pct > mem_alert_used_pct and should_send_alert("memory_alert", alert_cooldown_hours):
                    dm_client.send_dm(
                        emp_name,
                        f":warning: Memory pressure on *{instance_name}*: "
                        f"{mem_used_pct:.1f}% in use. "
                        f"Top process: {top_mem_proc} ({top_mem_pct:.1f}%).",
                    )
                    record_alert_sent("memory_alert")

        except Exception as exc:
            print(f"hostmon iteration error: {exc}")

        sleep(PUSH_INTERVAL)


if __name__ == "__main__":
    main()
