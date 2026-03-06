#!/usr/bin/env python3
# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Some changes to script have been made by Paul Seifer to adapt to python3.9.
# Additional changes here keep the legacy CloudWatch/text-log behavior while
# adding batched JSONL exports to S3 for metrics and events.

try:
    from urllib.request import Request, urlopen
except ImportError:
    from urllib2 import Request, urlopen

import gzip
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import sleep

import boto3
import psutil
import requests
from botocore.exceptions import ClientError

CACHE_DURATION = 300
THRESHOLD_PERCENTAGE = 10
S3_BUCKET = 'ab-gpumon-logs'
S3_PREFIX = 'v1'
S3_UPLOAD_INTERVAL_SEC = 300
S3_SPOOL_ROOT = '/tmp/gpumon_s3'
TMP_FILE = '/tmp/CPUMON_LOGS_'
MY_NAMESPACE = 'GPU-metrics-with-team-tag'
SLEEP_INTERVAL = 10
STORE_RESO = 60

core_utilization_cache = [[] for _ in range(psutil.cpu_count())]


def utc_now():
    return datetime.now(timezone.utc)


def ensure_parent_dir(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def get_imds_token():
    req = Request(
        'http://169.254.169.254/latest/api/token',
        None,
        {'X-aws-ec2-metadata-token-ttl-seconds': '21600'},
        method='PUT',
    )
    return urlopen(req, timeout=3).read().decode('utf-8')


def get_metadata(path, token):
    req = Request(
        'http://169.254.169.254/latest/meta-data/' + path,
        None,
        {'X-aws-ec2-metadata-token': token},
        method='GET',
    )
    return urlopen(req, timeout=3).read().decode('utf-8')


def check_root_crontab(search_string):
    try:
        result = subprocess.run(['sudo', 'crontab', '-l'], capture_output=True, text=True, check=True)
        return search_string in result.stdout
    except subprocess.CalledProcessError as exc:
        print(f'An error occurred: {exc}')
        return False
    except PermissionError:
        print('Permission denied. Make sure you have sudo privileges.')
        return False


def add_to_root_crontab(new_cron_job):
    try:
        current_crontab = subprocess.run(['sudo', 'crontab', '-l'], capture_output=True, text=True, check=True)
        new_crontab = current_crontab.stdout + new_cron_job + '\n'
        process = subprocess.Popen(['sudo', 'crontab', '-'], stdin=subprocess.PIPE, text=True)
        process.communicate(input=new_crontab)
        return process.returncode == 0
    except Exception as exc:  # noqa: BLE001
        print(f'Failed to update crontab: {exc}')
        return False


def seconds_elapsed():
    return time.time() - psutil.boot_time()


def get_per_core_cpu_utilization():
    return psutil.cpu_percent(interval=1, percpu=True)


def calculate_average_core_utilization():
    return [sum(core_data) / len(core_data) if core_data else 0 for core_data in core_utilization_cache]


def sanitize_partition(value):
    value = str(value or 'NO_TAG')
    return ''.join(ch if (ch.isalnum() or ch in ('-', '_', '.', '=')) else '_' for ch in value)


class S3JsonlExporter:
    def __init__(self, bucket, prefix, root_dir, region_name, context):
        self.bucket = bucket
        self.prefix = prefix.strip('/')
        self.root_dir = root_dir.rstrip('/')
        self.context = dict(context)
        self.s3 = boto3.client('s3', region_name=region_name)
        self.last_flush = 0

    def _local_path(self, kind, dt):
        base = (
            f"{self.root_dir}/{kind}/dt={dt.strftime('%Y-%m-%d')}/hour={dt.strftime('%H')}/"
            f"team={sanitize_partition(self.context.get('team'))}/"
            f"instance_id={sanitize_partition(self.context.get('instance_id'))}"
        )
        Path(base).mkdir(parents=True, exist_ok=True)
        return f"{base}/{kind}_{dt.strftime('%Y-%m-%dT%H')}.jsonl"

    def _s3_key(self, local_path):
        rel = local_path.replace(self.root_dir + '/', '', 1)
        return f'{self.prefix}/{rel}.gz'

    def append(self, kind, record):
        dt = utc_now()
        path = self._local_path(kind, dt)
        payload = dict(self.context)
        payload.update(record)
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(payload, separators=(',', ':'), default=str) + '\n')

    def emit_metric(self, record):
        self.append('metrics', record)

    def emit_event(self, event_type, message, details=None, severity='INFO', source='cpumon.py'):
        self.append(
            'events',
            {
                'record_type': 'event',
                'ts': utc_now().isoformat(),
                'event_type': event_type,
                'message': message,
                'severity': severity,
                'details': details or {},
                'source': source,
            },
        )

    def maybe_flush(self, force=False):
        now_ts = time.time()
        if not force and now_ts - self.last_flush < S3_UPLOAD_INTERVAL_SEC:
            return
        for root, _, files in os.walk(self.root_dir):
            for name in files:
                if not name.endswith('.jsonl'):
                    continue
                path = os.path.join(root, name)
                if not os.path.exists(path) or os.path.getsize(path) == 0:
                    continue
                gz_path = path + '.gz'
                try:
                    with open(path, 'rb') as src, gzip.open(gz_path, 'wb') as dst:
                        dst.writelines(src)
                    self.s3.upload_file(gz_path, self.bucket, self._s3_key(path))
                    os.remove(gz_path)
                    open(path, 'w').close()
                except Exception as exc:  # noqa: BLE001
                    print(f'Failed uploading {path}: {exc}')
        self.last_flush = now_ts


TOKEN = get_imds_token()
INSTANCE_ID = get_metadata('instance-id', TOKEN)
IMAGE_ID = get_metadata('ami-id', TOKEN)
INSTANCE_TYPE = get_metadata('instance-type', TOKEN)
INSTANCE_AZ = get_metadata('placement/availability-zone', TOKEN)
HOSTNAME = get_metadata('hostname', TOKEN)
EC2_REGION = INSTANCE_AZ[:-1]
TMP_FILE_SAVED = TMP_FILE + datetime.now().strftime('%Y-%m-%dT%H')

_ec2 = boto3.client('ec2', region_name=EC2_REGION)
_cloudwatch = boto3.client('cloudwatch', region_name=EC2_REGION)


class RuntimeContext:
    exporter = None
    cpu_threshold = THRESHOLD_PERCENTAGE
    network_threshold = 10000


CTX = RuntimeContext()


def get_instance_tags(instance_id):
    try:
        response = _ec2.describe_tags(Filters=[{'Name': 'resource-id', 'Values': [instance_id]}])
        return {tag['Key']: tag['Value'] for tag in response['Tags']}
    except ClientError:
        print(f"Couldn't get tags for instance {instance_id}")
        raise


def create_tag(instance_id, tag_name, default_value):
    _ec2.create_tags(Resources=[instance_id], Tags=[{'Key': tag_name, 'Value': default_value}])


def send_slack(webhook_url, message):
    if not webhook_url:
        return
    requests.post(webhook_url, json={'text': message}, timeout=10)


def get_network_stats(instance_id, network_last):
    end = datetime.utcnow()
    start = end - timedelta(minutes=5)
    data_in = _cloudwatch.get_metric_statistics(
        Period=60,
        StartTime=start.strftime('%Y-%m-%dT%H:%M:%SZ'),
        EndTime=end.strftime('%Y-%m-%dT%H:%M:%SZ'),
        MetricName='NetworkPacketsIn',
        Namespace='AWS/EC2',
        Statistics=['Maximum'],
        Unit='Count',
        Dimensions=[{'Name': 'InstanceId', 'Value': str(instance_id)}],
    )
    data_out = _cloudwatch.get_metric_statistics(
        Period=60,
        StartTime=start.strftime('%Y-%m-%dT%H:%M:%SZ'),
        EndTime=end.strftime('%Y-%m-%dT%H:%M:%SZ'),
        MetricName='NetworkPacketsOut',
        Namespace='AWS/EC2',
        Statistics=['Maximum'],
        Unit='Count',
        Dimensions=[{'Name': 'InstanceId', 'Value': str(instance_id)}],
    )
    inp = round(data_in.get('Datapoints', [{}])[0].get('Maximum', network_last / 2)) if data_in.get('Datapoints') else round(network_last / 2)
    out = round(data_out.get('Datapoints', [{}])[0].get('Maximum', network_last / 2)) if data_out.get('Datapoints') else round(network_last / 2)
    return inp + out


def rotate_tmp_file_if_needed(current_time):
    global TMP_FILE_SAVED
    current_path = TMP_FILE + current_time.strftime('%Y-%m-%dT%H')
    if TMP_FILE_SAVED != current_path:
        TMP_FILE_SAVED = current_path


def log_results(team, emp_name, cpu_util, memory_percent, alarm_pilot_light, cpu_util_tripped, seconds, current_time, per_core_utilization, network, network_tripped):
    rotate_tmp_file_if_needed(current_time)
    write_string = (
        '[ ' + str(current_time) + ' ] '
        + 'tag:' + team + ','
        + 'Employee:' + emp_name + ','
        + 'CPU_Util:' + str(cpu_util) + ','
        + 'MemUtil:' + str(memory_percent) + ','
        + 'Alarm_Pilot_value:' + str(alarm_pilot_light) + ','
        + 'CPU_Util_Tripped:' + str(cpu_util_tripped) + ','
        + 'Seconds Elapsed since reboot:' + str(seconds) + ','
        + 'Per-Core CPU Util:' + str(per_core_utilization) + ','
        + 'NetworkStats:' + str(network) + ','
        + 'Network_Tripped:' + str(network_tripped) + '\n'
    )
    with open(TMP_FILE_SAVED, 'a+', encoding='utf-8') as handle:
        handle.write(write_string)

    CTX.exporter.emit_metric(
        {
            'record_type': 'metric',
            'ts': utc_now().isoformat(),
            'source': 'cpumon.py',
            'cpu_util': float(cpu_util),
            'instance_memory_percent': float(memory_percent),
            'alarm_pilot_light': int(alarm_pilot_light),
            'cpu_util_tripped': int(bool(cpu_util_tripped)),
            'per_core_cpu_util': list(per_core_utilization),
            'network_packets_sum': int(network),
            'network_tripped': int(bool(network_tripped)),
            'seconds_since_boot': float(seconds),
            'active_cpu': bool(cpu_util_tripped),
        }
    )

    dimensions = [
        {'Name': 'InstanceId', 'Value': INSTANCE_ID},
        {'Name': 'ImageId', 'Value': IMAGE_ID},
        {'Name': 'InstanceType', 'Value': INSTANCE_TYPE},
        {'Name': 'InstanceTag', 'Value': str(team)},
        {'Name': 'EmployeeTag', 'Value': str(emp_name)},
    ]
    _cloudwatch.put_metric_data(
        MetricData=[
            {'MetricName': 'Average CPU Utilization', 'Dimensions': dimensions, 'Unit': 'Percent', 'StorageResolution': STORE_RESO, 'Value': float(cpu_util)},
            {'MetricName': 'Instance Memory Usage', 'Dimensions': dimensions, 'Unit': 'Percent', 'StorageResolution': STORE_RESO, 'Value': float(memory_percent)},
            {'MetricName': 'Alarm Pilot Light (1/0)', 'Dimensions': dimensions, 'Unit': 'None', 'StorageResolution': STORE_RESO, 'Value': float(alarm_pilot_light)},
            {'MetricName': 'CPU Utilization Low Tripped', 'Dimensions': dimensions, 'Unit': 'None', 'StorageResolution': STORE_RESO, 'Value': float(cpu_util_tripped)},
            {'MetricName': 'Network Tripped', 'Dimensions': dimensions, 'Unit': 'None', 'StorageResolution': STORE_RESO, 'Value': float(network_tripped)},
        ],
        Namespace=MY_NAMESPACE,
    )


def emit_event(event_type, message, details=None, severity='INFO'):
    print(message)
    CTX.exporter.emit_event(event_type, message, details=details, severity=severity, source='cpumon.py')


def main():
    if not check_root_crontab('halt_it.sh'):
        add_to_root_crontab('*/10 * * * * bash /root/gpumon/halt_it.sh | tee -a /tmp/halt_it_log.txt')

    tags = get_instance_tags(INSTANCE_ID)
    instance_name = str(tags.get('Name', 'NO_NAME_TAG'))
    team = str(tags.get('Team', 'NO_TAG'))
    emp_name = str(tags.get('Employee', 'NO_TAG'))
    policy = str(tags.get('GPUMON_POLICY', 'STANDARD'))
    if 'GPUMON_POLICY' not in tags:
        create_tag(INSTANCE_ID, 'GPUMON_POLICY', policy)

    cpu_threshold = THRESHOLD_PERCENTAGE
    network_threshold = 10000
    if policy == 'RELAXED':
        cpu_threshold = 5
        network_threshold = 5000
    elif policy == 'SEVERE':
        cpu_threshold = 40
        network_threshold = 2000000
    elif policy == 'SUSPEND':
        cpu_threshold = 0
        network_threshold = 0

    CTX.cpu_threshold = cpu_threshold
    CTX.network_threshold = network_threshold
    CTX.exporter = S3JsonlExporter(
        bucket=S3_BUCKET,
        prefix=S3_PREFIX,
        root_dir=S3_SPOOL_ROOT,
        region_name=EC2_REGION,
        context={
            'instance_id': INSTANCE_ID,
            'instance_name': instance_name,
            'team': team,
            'employee': emp_name,
            'policy': policy,
            'image_id': IMAGE_ID,
            'instance_type': INSTANCE_TYPE,
            'hostname': HOSTNAME,
        },
    )
    emit_event('cpumon_started', f'cpumon started on {instance_name} {INSTANCE_ID}', details={'policy': policy, 'sleep_interval': SLEEP_INTERVAL})

    debug_webhook = os.getenv('DEBUG_WEBHOOK_URL')
    team_webhook = os.getenv(str(team) + '_TEAM_WEBHOOK_URL') or debug_webhook
    alarm_pilot_light = 0
    network = 99
    last_alarm_state = None

    try:
        while True:
            per_core = get_per_core_cpu_utilization()
            for idx, core in enumerate(per_core):
                core_utilization_cache[idx].append(core)
            for idx, core_data in enumerate(core_utilization_cache):
                core_utilization_cache[idx] = core_data[-int(CACHE_DURATION):]

            avg_core = calculate_average_core_utilization()
            cpu_util_tripped = any(util > cpu_threshold for util in avg_core)
            memory_percent = psutil.virtual_memory().percent
            cpu_util = psutil.cpu_percent(interval=0)
            network = get_network_stats(INSTANCE_ID, network)
            network_tripped = int(network > network_threshold)
            desired_alarm = int((not cpu_util_tripped) and not network_tripped)
            if desired_alarm != alarm_pilot_light:
                alarm_pilot_light = desired_alarm
                if alarm_pilot_light == 1:
                    msg = f'{instance_name} {INSTANCE_ID}: CPU and NETWORK seem idle, TURNED ALARM PILOT LIGHT to: ON'
                    emit_event('alarm_pilot_on', msg, details={'cpu_util': cpu_util, 'network': network, 'network_tripped': network_tripped})
                    try:
                        send_slack(team_webhook, msg)
                    except Exception:
                        pass
                else:
                    emit_event('alarm_pilot_off', f'{instance_name} {INSTANCE_ID}: activity detected, TURNED ALARM PILOT LIGHT to: OFF', details={'cpu_util': cpu_util, 'network': network, 'network_tripped': network_tripped})
                last_alarm_state = alarm_pilot_light

            log_results(team, emp_name, cpu_util, memory_percent, alarm_pilot_light, cpu_util_tripped, seconds_elapsed(), datetime.now(), per_core, network, network_tripped)
            CTX.exporter.maybe_flush()
            sleep(SLEEP_INTERVAL)
    except Exception as exc:  # noqa: BLE001
        emit_event('cpumon_error', f'cpumon crashed: {exc}', severity='ERROR')
        try:
            send_slack(debug_webhook, f'cpumon crashed on {instance_name} {INSTANCE_ID}: {exc}')
        except Exception:
            pass
        raise
    finally:
        CTX.exporter.maybe_flush(force=True)


if __name__ == '__main__':
    main()
