# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Licensed under the Apache License, Version 2.0
# Adapted for Python 3 and extended by Paul Seifer, Autobrains LTD

from __future__ import annotations

import os
from datetime import datetime
from time import sleep

import boto3
import psutil

from mon_utils import (
    build_slack_dm_client,
    calculate_average_core_utilization,
    cleanup_old_logs,
    create_tag,
    fetch_instance_metadata,
    get_instance_tags,
    get_network_stats,
    get_per_core_cpu_utilization,
    get_policy_config,
    seconds_elapsed,
    try_record_alert,
)

NAMESPACE = "CPU-metrics"
TMP_FILE_PREFIX = "/tmp/CPUMON_LOGS_"
SLEEP_INTERVAL = 10
STORE_RESO = 60
CACHE_DURATION = 300  # seconds of CPU history to keep
POLICY_REFRESH_LOOPS = 60   # re-read GPUMON_POLICY tag every ~10 min


def _log_results(
    log_file: str,
    cw_client,
    instance_id: str,
    image_id: str,
    instance_type: str,
    team: str,
    emp_name: str,
    alarm_pilot_light: int,
    cpu_util_tripped: bool,
    seconds: int,
    current_time: datetime,
    per_core_utilization: list[float],
    network: float,
    network_tripped: int,
) -> None:
    line = (
        f"[ {current_time} ] "
        f"tag:{team},"
        f"Employee:{emp_name},"
        f"Alarm_Pilot_value:{alarm_pilot_light},"
        f"CPU_Util_Tripped:{cpu_util_tripped},"
        f"Seconds Elapsed since reboot:{seconds},"
        f"Per-Core CPU Util:{per_core_utilization},"
        f"NetworkStats:{network},"
        f"Network_Tripped:{network_tripped}\n"
    )
    try:
        with open(log_file, "a") as fh:
            fh.write(line)
    except OSError as exc:
        print(f"log write error: {exc}")

    dims = [
        {"Name": "InstanceId",    "Value": instance_id},
        {"Name": "ImageId",       "Value": image_id},
        {"Name": "InstanceType",  "Value": instance_type},
        {"Name": "InstanceTag",   "Value": team},
        {"Name": "EmployeeTag",   "Value": emp_name},
    ]
    try:
        cw_client.put_metric_data(
            Namespace=NAMESPACE,
            MetricData=[
                {"MetricName": "Alarm Pilot Light (1/0)",     "Dimensions": dims, "Unit": "None", "StorageResolution": STORE_RESO, "Value": float(alarm_pilot_light)},
                {"MetricName": "CPU Utilization Low Tripped", "Dimensions": dims, "Unit": "None", "StorageResolution": STORE_RESO, "Value": float(cpu_util_tripped)},
                {"MetricName": "Network Tripped",             "Dimensions": dims, "Unit": "None", "StorageResolution": STORE_RESO, "Value": float(network_tripped)},
            ],
        )
    except Exception as exc:
        print(f"CloudWatch put_metric_data error: {exc}")


def main() -> None:
    cleanup_old_logs(TMP_FILE_PREFIX, max_age_hours=48)

    meta = fetch_instance_metadata()
    region        = meta["region"]
    instance_id   = meta["instance_id"]
    image_id      = meta["image_id"]
    instance_type = meta["instance_type"]
    hostname      = meta["hostname"]

    ec2 = boto3.client("ec2",        region_name=region)
    cw  = boto3.client("cloudwatch", region_name=region)

    tags = get_instance_tags(ec2, instance_id)
    instance_name = tags.get("Name", "NO_NAME_TAG")
    team          = tags.get("Team", "NO_TAG")
    emp_name      = tags.get("Employee", "NO_TAG")
    policy        = tags.get("GPUMON_POLICY")
    if policy is None:
        policy = "STANDARD"
        create_tag(ec2, instance_id, "GPUMON_POLICY", policy)

    # SPOT instances: never DM the employee
    page_employee = (
        policy != "SPOT"
        and tags.get("PAGE_EMPLOYEE", "True").lower() != "false"
    )

    cfg = get_policy_config(policy)
    restart_backoff   = cfg["restart_backoff"]
    cpu_threshold     = cfg["cpu_threshold"]
    network_threshold = cfg["network_threshold"]

    shutdown_cooldown_hours = float(os.getenv("SHUTDOWN_ALERT_COOLDOWN_HOURS", "4"))

    # Slack DM client — None if secret not configured or unreachable
    slack_secret_id     = os.getenv("GPUMON_SLACK_SECRET_ID", "IT/SLACK_BOT_TOKEN")
    slack_secret_region = os.getenv("GPUMON_SLACK_SECRET_REGION", os.getenv("GPUMON_SECRET_REGION", "eu-west-1"))
    dm_client = build_slack_dm_client(slack_secret_id, slack_secret_region) if page_employee else None

    core_cache: list[list[float]] = [[] for _ in range(psutil.cpu_count())]
    alarm_pilot_light = 0
    network_tripped   = 0
    network: float    = 99.0

    loop_count = 0
    while True:
        loop_count += 1
        if loop_count % POLICY_REFRESH_LOOPS == 0:
            try:
                fresh_tags = get_instance_tags(ec2, instance_id)
                new_policy = fresh_tags.get("GPUMON_POLICY", policy)
                if new_policy != policy:
                    policy = new_policy
                    cfg = get_policy_config(policy)
                    restart_backoff   = cfg["restart_backoff"]
                    cpu_threshold     = cfg["cpu_threshold"]
                    network_threshold = cfg["network_threshold"]
            except Exception as exc:
                print(f"policy refresh error: {exc}")

        current_time = datetime.now()
        log_file = TMP_FILE_PREFIX + current_time.strftime("%Y-%m-%dT%H")
        cpu_util_tripped = False

        # ── CPU utilization cache ────────────────────────────────────────────
        try:
            per_core = get_per_core_cpu_utilization()
            for i, v in enumerate(per_core):
                core_cache[i].append(v)
            core_cache = [d[-int(CACHE_DURATION / SLEEP_INTERVAL):] for d in core_cache]
            avg_cores = calculate_average_core_utilization(core_cache)
            if any(u > cpu_threshold for u in avg_cores):
                cpu_util_tripped = True
        except Exception as exc:
            print(f"CPU utilization error: {exc}")
            per_core = []

        seconds = round(seconds_elapsed())

        # ── Network ──────────────────────────────────────────────────────────
        network = get_network_stats(cw, instance_id, network)
        if network_tripped == 0 and network <= network_threshold:
            network_tripped = 1

        # ── Alarm pilot light ────────────────────────────────────────────────
        if seconds >= restart_backoff:
            if not cpu_util_tripped and network <= network_threshold:
                if alarm_pilot_light == 0:
                    alarm_pilot_light = 1
                    if dm_client and try_record_alert("shutdown_alert", shutdown_cooldown_hours):
                        dm_client.send_dm(
                            emp_name,
                            f":alarm_clock: Your instance *{instance_name}* appears idle "
                            f"and is scheduled to shut down in ~3 hours.",
                        )
            else:
                if alarm_pilot_light == 1:
                    alarm_pilot_light = 0
        else:
            alarm_pilot_light = 0

        # ── Log & push ───────────────────────────────────────────────────────
        _log_results(
            log_file, cw, instance_id, image_id, instance_type,
            team, emp_name, alarm_pilot_light, cpu_util_tripped,
            seconds, current_time, per_core, network, network_tripped,
        )

        sleep(SLEEP_INTERVAL)


if __name__ == "__main__":
    main()
