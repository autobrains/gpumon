#!/usr/bin/env python3
# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Some changes to script have been made by Paul Seifer to adapt to python3.9,
# such as conversion of values to utf-8 strings.
# Additional changes here keep the legacy CloudWatch/text-log behavior while
# adding batched JSONL exports to S3 for metrics and events.

try:
    from urllib.request import Request, urlopen
except ImportError:
    from urllib2 import Request, urlopen

import gzip
import json
import os
import socket
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import sleep

import boto3
import psutil
import requests
from botocore.exceptions import ClientError
from pynvml import *  # noqa: F403

# Constants
CACHE_DURATION = 300  # 5 minutes in seconds
THRESHOLD_PERCENTAGE = 10  # default threshold for average core utilization
S3_BUCKET = 'ab-gpumon-logs'
S3_PREFIX = 'v1'
S3_UPLOAD_INTERVAL_SEC = 300
S3_SPOOL_ROOT = '/tmp/gpumon_s3'
TMP_FILE = '/tmp/GPU_TEMP_'
MY_NAMESPACE = 'GPU-metrics-with-team-tag'
SLEEP_INTERVAL = 10
STORE_RESO = 60

# Variables
core_utilization_cache = [[] for _ in range(psutil.cpu_count())]
cpu_util_tripped = 0


def utc_now():
    return datetime.now(timezone.utc)


def ensure_parent_dir(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def now_hour_stamp(dt=None):
    dt = dt or datetime.now()
    return dt.strftime('%Y-%m-%dT%H')


def seconds_elapsed():
    return time.time() - psutil.boot_time()


def get_per_core_cpu_utilization():
    return psutil.cpu_percent(interval=1, percpu=True)


def calculate_average_core_utilization():
    return [sum(core_data) / len(core_data) if core_data else 0 for core_data in core_utilization_cache]


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
        if process.returncode == 0:
            print('New cron job added successfully.')
            return True
        print('Failed to add new cron job.')
        return False
    except subprocess.CalledProcessError as exc:
        print(f'An error occurred: {exc}')
        return False
    except PermissionError:
        print('Permission denied. Make sure you have sudo privileges.')
        return False


def sanitize_partition(value):
    value = str(value or 'NO_TAG')
    safe = []
    for ch in value:
        if ch.isalnum() or ch in ('-', '_', '.', '='):
            safe.append(ch)
        else:
            safe.append('_')
    return ''.join(safe)


class S3JsonlExporter:
    def __init__(self, bucket, prefix, root_dir, region_name, context):
        self.bucket = bucket
        self.prefix = prefix.strip('/')
        self.root_dir = root_dir.rstrip('/')
        self.context = dict(context)
        self.s3 = boto3.client('s3', region_name=region_name)
        self.last_flush = 0

    def _local_path(self, kind, dt):
        day = dt.strftime('%Y-%m-%d')
        hour = dt.strftime('%H')
        team = sanitize_partition(self.context.get('team', 'NO_TAG'))
        instance_id = sanitize_partition(self.context.get('instance_id', 'unknown'))
        base = (
            f"{self.root_dir}/{kind}/dt={day}/hour={hour}/team={team}/"
            f"instance_id={instance_id}"
        )
        Path(base).mkdir(parents=True, exist_ok=True)
        fname = f"{kind}_{dt.strftime('%Y-%m-%dT%H')}.jsonl"
        return f'{base}/{fname}'

    def _s3_key(self, local_path):
        rel = local_path.replace(self.root_dir + '/', '', 1)
        return f'{self.prefix}/{rel}.gz'

    def append(self, kind, record):
        dt = utc_now()
        payload = dict(self.context)
        payload.update(record)
        local_path = self._local_path(kind, dt)
        ensure_parent_dir(local_path)
        with open(local_path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(payload, separators=(',', ':'), default=str) + '\n')

    def emit_metric(self, record):
        self.append('metrics', record)

    def emit_event(self, event_type, message, details=None, severity='INFO', source='gpumon.py'):
        record = {
            'record_type': 'event',
            'ts': utc_now().isoformat(),
            'event_type': event_type,
            'message': message,
            'severity': severity,
            'details': details or {},
            'source': source,
        }
        self.append('events', record)

    def flush_file(self, local_path):
        if not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
            return
        gz_path = local_path + '.gz'
        with open(local_path, 'rb') as src, gzip.open(gz_path, 'wb') as dst:
            dst.writelines(src)
        self.s3.upload_file(gz_path, self.bucket, self._s3_key(local_path))
        os.remove(gz_path)
        open(local_path, 'w').close()

    def maybe_flush(self, force=False):
        now_ts = time.time()
        if not force and (now_ts - self.last_flush) < S3_UPLOAD_INTERVAL_SEC:
            return
        for root, _, files in os.walk(self.root_dir):
            for name in files:
                if name.endswith('.jsonl'):
                    try:
                        self.flush_file(os.path.join(root, name))
                    except Exception as exc:  # noqa: BLE001
                        print(f'Failed flushing {name} to S3: {exc}')
        self.last_flush = now_ts


TOKEN = get_imds_token()
INSTANCE_ID = get_metadata('instance-id', TOKEN)
IMAGE_ID = get_metadata('ami-id', TOKEN)
INSTANCE_TYPE = get_metadata('instance-type', TOKEN)
INSTANCE_AZ = get_metadata('placement/availability-zone', TOKEN)
HOSTNAME = get_metadata('hostname', TOKEN)
EC2_REGION = INSTANCE_AZ[:-1]
TIMESTAMP = now_hour_stamp()
TMP_FILE_SAVED = TMP_FILE + TIMESTAMP

# Create clients
_ec2 = boto3.client('ec2', region_name=EC2_REGION)
_cloudwatch = boto3.client('cloudwatch', region_name=EC2_REGION)


class RuntimeContext:
    exporter = None
    team = 'NO_TAG'
    employee = 'NO_TAG'
    policy = 'STANDARD'
    instance_name = 'NO_NAME_TAG'
    gpu_threshold = 10
    network_threshold = 10000
    restart_backoff = 7200


CTX = RuntimeContext()


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
    datapoints_in = data_in.get('Datapoints', [])
    datapoints_out = data_out.get('Datapoints', [])
    first_datapoint_in = round(datapoints_in[0]['Maximum']) if datapoints_in else round(network_last / 2)
    first_datapoint_out = round(datapoints_out[0]['Maximum']) if datapoints_out else round(network_last / 2)
    return first_datapoint_in + first_datapoint_out


PUSH_TO_CW = True


def get_instance_tags(instance_id):
    try:
        response = _ec2.describe_tags(Filters=[{'Name': 'resource-id', 'Values': [instance_id]}])
        return {tag['Key']: tag['Value'] for tag in response['Tags']}
    except ClientError:
        print(f"Couldn't get tags for instance {instance_id}")
        raise


def send_slack(webhook_url, message):
    if not webhook_url:
        return
    payload = {'text': f'{message}'}
    response = requests.post(webhook_url, json=payload, timeout=10)
    if response.status_code == 200:
        print('Message sent to Slack successfully.')
    else:
        print(f'Failed to send message to Slack. Status code: {response.status_code}')


def create_tag(instance_id, tag_name, default_value):
    _ec2.create_tags(Resources=[instance_id], Tags=[{'Key': tag_name, 'Value': default_value}])
    print(f"Tag '{tag_name}' with value '{default_value}' added to instance {instance_id}.")


def get_power_draw(handle):
    global PUSH_TO_CW
    try:
        return '%.2f' % (nvmlDeviceGetPowerUsage(handle) / 1000.0)
    except NVMLError as err:  # noqa: F405
        PUSH_TO_CW = False
        return str(err)


def get_temp(handle):
    global PUSH_TO_CW
    try:
        return str(nvmlDeviceGetTemperature(handle, NVML_TEMPERATURE_GPU))  # noqa: F405
    except NVMLError as err:  # noqa: F405
        PUSH_TO_CW = False
        return str(err)


def get_utilization(handle):
    global PUSH_TO_CW
    try:
        util = nvmlDeviceGetUtilizationRates(handle)
        return util, str(util.gpu), str(util.memory)
    except NVMLError as err:  # noqa: F405
        PUSH_TO_CW = False
        err_str = str(err)
        return None, err_str, err_str


def emit_event(event_type, message, details=None, severity='INFO'):
    print(message)
    if CTX.exporter:
        try:
            CTX.exporter.emit_event(event_type, message, details=details, severity=severity, source='gpumon.py')
        except Exception as exc:  # noqa: BLE001
            print(f'Failed to emit S3 event: {exc}')


def rotate_tmp_file_if_needed(current_time):
    global TMP_FILE_SAVED
    current_path = TMP_FILE + current_time.strftime('%Y-%m-%dT%H')
    if TMP_FILE_SAVED != current_path:
        TMP_FILE_SAVED = current_path


def log_results(
    team,
    emp_name,
    gpu_id,
    util,
    gpu_util,
    mem_util,
    pow_draw_str,
    temp,
    average_gpu_util,
    alarm_pilot_light,
    cpu_util_tripped,
    seconds,
    current_time,
    per_core_utilization,
    network,
    network_tripped,
):
    rotate_tmp_file_if_needed(current_time)
    write_string = (
        '[ ' + str(current_time) + ' ] '
        + 'tag:' + team + ','
        + 'Employee:' + emp_name + ','
        + 'GPU_ID:' + str(gpu_id) + ','
        + 'GPU_Util:' + gpu_util + ','
        + 'MemUtil:' + mem_util + ','
        + 'powDrawStr:' + pow_draw_str + ','
        + 'Temp:' + temp + ','
        + 'AverageGPUUtil:' + str(average_gpu_util) + ','
        + 'Alarm_Pilot_value:' + str(alarm_pilot_light) + ','
        + 'CPU_Util_Tripped:' + str(cpu_util_tripped) + ','
        + 'Seconds Elapsed since reboot:' + str(seconds) + ','
        + 'Per-Core CPU Util:' + str(per_core_utilization) + ','
        + 'NetworkStats:' + str(network) + ','
        + 'Network_Tripped:' + str(network_tripped) + '\n'
    )
    try:
        with open(TMP_FILE_SAVED, 'a+', encoding='utf-8') as gpu_logs:
            gpu_logs.write(write_string)
    except Exception as exc:  # noqa: BLE001
        print(f'Error writing to file {TMP_FILE_SAVED}: {exc}')

    if util is not None and CTX.exporter:
        try:
            memory = psutil.virtual_memory()
            CTX.exporter.emit_metric(
                {
                    'record_type': 'metric',
                    'ts': utc_now().isoformat(),
                    'source': 'gpumon.py',
                    'gpu_id': gpu_id,
                    'gpu_util': util.gpu,
                    'gpu_mem_util': util.memory,
                    'gpu_power_watts': float(pow_draw_str),
                    'gpu_temp_c': int(temp),
                    'average_gpu_util': float(average_gpu_util),
                    'alarm_pilot_light': int(alarm_pilot_light),
                    'cpu_util_tripped': int(bool(cpu_util_tripped)),
                    'per_core_cpu_util': list(per_core_utilization),
                    'network_packets_sum': int(network),
                    'network_tripped': int(bool(network_tripped)),
                    'seconds_since_boot': float(seconds),
                    'instance_memory_percent': float(memory.percent),
                    'active_gpu': bool(float(average_gpu_util) > float(CTX.gpu_threshold)),
                }
            )
        except Exception as exc:  # noqa: BLE001
            print(f'Failed to write S3 metric spool: {exc}')

    if PUSH_TO_CW and util is not None:
        dimensions = [
            {'Name': 'InstanceId', 'Value': INSTANCE_ID},
            {'Name': 'ImageId', 'Value': IMAGE_ID},
            {'Name': 'InstanceType', 'Value': INSTANCE_TYPE},
            {'Name': 'GPUNumber', 'Value': str(gpu_id)},
            {'Name': 'InstanceTag', 'Value': str(team)},
            {'Name': 'EmployeeTag', 'Value': str(emp_name)},
        ]
        _cloudwatch.put_metric_data(
            MetricData=[
                {'MetricName': 'GPU Usage', 'Dimensions': dimensions, 'Unit': 'Percent', 'StorageResolution': STORE_RESO, 'Value': util.gpu},
                {'MetricName': 'Memory Usage', 'Dimensions': dimensions, 'Unit': 'Percent', 'StorageResolution': STORE_RESO, 'Value': util.memory},
                {'MetricName': 'Power Usage (Watts)', 'Dimensions': dimensions, 'Unit': 'None', 'StorageResolution': STORE_RESO, 'Value': float(pow_draw_str)},
                {'MetricName': 'Temperature (C)', 'Dimensions': dimensions, 'Unit': 'None', 'StorageResolution': STORE_RESO, 'Value': int(temp)},
                {'MetricName': 'Alarm Pilot Light (1/0)', 'Dimensions': dimensions, 'Unit': 'None', 'StorageResolution': STORE_RESO, 'Value': float(alarm_pilot_light)},
                {'MetricName': 'Average GPU Utilization', 'Dimensions': dimensions, 'Unit': 'Percent', 'StorageResolution': STORE_RESO, 'Value': float(average_gpu_util)},
                {'MetricName': 'CPU Utilization Low Tripped', 'Dimensions': dimensions, 'Unit': 'None', 'StorageResolution': STORE_RESO, 'Value': float(cpu_util_tripped)},
                {'MetricName': 'Network Tripped', 'Dimensions': dimensions, 'Unit': 'None', 'StorageResolution': STORE_RESO, 'Value': float(network_tripped)},
            ],
            Namespace=MY_NAMESPACE,
        )


nvmlInit()
device_count = nvmlDeviceGetCount()


def main():
    result = check_root_crontab('halt_it.sh')
    if result:
        print('halt_it.sh presence in crontab detected, continue')
    else:
        print('updating crontab with new halt_it.sh call')
        new_job = '*/10 * * * * bash /root/gpumon/halt_it.sh | tee -a /tmp/halt_it_log.txt'
        add_to_root_crontab(new_job)

    global core_utilization_cache
    alarm_pilot_light = 0
    network_tripped = 0
    network = 99
    policy = 'STANDARD'
    tags = get_instance_tags(INSTANCE_ID)

    instance_name = str(tags.get('Name', 'NO_NAME_TAG'))
    team = str(tags.get('Team', 'NO_TAG'))
    emp_name = str(tags.get('Employee', 'NO_TAG'))
    if 'GPUMON_POLICY' in tags:
        policy = str(tags['GPUMON_POLICY'])
    else:
        create_tag(INSTANCE_ID, 'GPUMON_POLICY', policy)

    restart_backoff = 7200
    gpu_threshold = 10
    network_threshold = 10000
    core_threshold = THRESHOLD_PERCENTAGE

    if policy == 'RELAXED':
        print('POLICY TAG detected:', {policy})
        restart_backoff = 7200
        core_threshold = 5
        gpu_threshold = 10
        network_threshold = 5000
    elif policy == 'SEVERE':
        print('POLICY TAG detected:', {policy})
        restart_backoff = 300
        core_threshold = 40
        gpu_threshold = 2
        network_threshold = 2000000
    elif policy == 'SUSPEND':
        print('POLICY TAG detected:', {policy})
        restart_backoff = 864000
        core_threshold = 0
        gpu_threshold = 0
        network_threshold = 0
    else:
        print('POLICY TAG detected:', {policy})

    CTX.team = team
    CTX.employee = emp_name
    CTX.policy = policy
    CTX.instance_name = instance_name
    CTX.gpu_threshold = gpu_threshold
    CTX.network_threshold = network_threshold
    CTX.restart_backoff = restart_backoff
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
    emit_event(
        'gpumon_started',
        f'gpumon started on {instance_name} {INSTANCE_ID}',
        details={'device_count': int(device_count), 'policy': policy, 'sleep_interval': SLEEP_INTERVAL},
    )

    debug_webhook = os.getenv('DEBUG_WEBHOOK_URL')
    team_var = str(team) + '_TEAM_WEBHOOK_URL'
    try:
        team_webhook = os.getenv(team_var)
    except Exception:  # noqa: BLE001
        team_webhook = debug_webhook
    if not team_webhook and debug_webhook:
        team_webhook = debug_webhook

    last_alarm_state = None
    last_network_tripped = None
    last_cpu_tripped = None

    try:
        while True:
            total_gpu_util = 0
            cpu_util_tripped = False
            per_core_utilization = get_per_core_cpu_utilization()
            for idx, core_util in enumerate(per_core_utilization):
                core_utilization_cache[idx].append(core_util)
            core_utilization_cache = [core_data[-int(CACHE_DURATION):] for core_data in core_utilization_cache]
            average_core_utilization = calculate_average_core_utilization()
            if any(utilization > core_threshold for utilization in average_core_utilization):
                cpu_util_tripped = True

            current_time = datetime.now()
            seconds = seconds_elapsed()
            network = get_network_stats(INSTANCE_ID, network)
            network_tripped = int(network > network_threshold)
            gpu_samples = []

            for gpu_id in range(device_count):
                handle = nvmlDeviceGetHandleByIndex(gpu_id)
                util, gpu_util, mem_util = get_utilization(handle)
                pow_draw_str = get_power_draw(handle)
                temp = get_temp(handle)
                if util is not None:
                    total_gpu_util += util.gpu
                    gpu_samples.append(util.gpu)
                log_results(
                    team,
                    emp_name,
                    gpu_id,
                    util,
                    gpu_util,
                    mem_util,
                    pow_draw_str,
                    temp,
                    0,
                    alarm_pilot_light,
                    cpu_util_tripped,
                    seconds,
                    current_time,
                    per_core_utilization,
                    network,
                    network_tripped,
                )

            average_gpu_util = round(total_gpu_util / max(device_count, 1), 2)

            for gpu_id in range(device_count):
                handle = nvmlDeviceGetHandleByIndex(gpu_id)
                util, gpu_util, mem_util = get_utilization(handle)
                pow_draw_str = get_power_draw(handle)
                temp = get_temp(handle)
                log_results(
                    team,
                    emp_name,
                    gpu_id,
                    util,
                    gpu_util,
                    mem_util,
                    pow_draw_str,
                    temp,
                    average_gpu_util,
                    alarm_pilot_light,
                    cpu_util_tripped,
                    seconds,
                    current_time,
                    per_core_utilization,
                    network,
                    network_tripped,
                )

            desired_alarm = int((not cpu_util_tripped) and average_gpu_util <= gpu_threshold and not network_tripped)
            if desired_alarm != alarm_pilot_light:
                alarm_pilot_light = desired_alarm
                if alarm_pilot_light == 1:
                    message = (
                        f'{instance_name} {INSTANCE_ID}: CPU, GPU and NETWORK seem idle, '
                        'TURNED ALARM PILOT LIGHT to: ON'
                    )
                    emit_event(
                        'alarm_pilot_on',
                        message,
                        details={
                            'average_gpu_util': average_gpu_util,
                            'cpu_util_tripped': int(cpu_util_tripped),
                            'network': network,
                            'network_tripped': network_tripped,
                            'gpu_threshold': gpu_threshold,
                            'network_threshold': network_threshold,
                        },
                    )
                    try:
                        send_slack(team_webhook, message)
                    except Exception as exc:  # noqa: BLE001
                        emit_event('slack_send_failed', f'Failed sending Slack message: {exc}', severity='WARN')
                else:
                    message = f'{instance_name} {INSTANCE_ID}: activity detected, TURNED ALARM PILOT LIGHT to: OFF'
                    emit_event(
                        'alarm_pilot_off',
                        message,
                        details={
                            'average_gpu_util': average_gpu_util,
                            'cpu_util_tripped': int(cpu_util_tripped),
                            'network': network,
                            'network_tripped': network_tripped,
                        },
                    )

            if last_cpu_tripped is None or last_cpu_tripped != int(cpu_util_tripped):
                emit_event(
                    'cpu_trip_state_changed',
                    f'cpu_util_tripped changed to {int(cpu_util_tripped)}',
                    details={'cpu_util_tripped': int(cpu_util_tripped), 'per_core_cpu_util': per_core_utilization},
                )
                last_cpu_tripped = int(cpu_util_tripped)

            if last_network_tripped is None or last_network_tripped != int(network_tripped):
                emit_event(
                    'network_trip_state_changed',
                    f'network_tripped changed to {int(network_tripped)}',
                    details={'network_tripped': int(network_tripped), 'network': network, 'network_threshold': network_threshold},
                )
                last_network_tripped = int(network_tripped)

            last_alarm_state = alarm_pilot_light
            CTX.exporter.maybe_flush()
            sleep(SLEEP_INTERVAL)
    except KeyboardInterrupt:
        emit_event('gpumon_stopped', 'gpumon interrupted by keyboard', severity='WARN')
        raise
    except Exception as exc:  # noqa: BLE001
        emit_event('gpumon_error', f'gpumon crashed: {exc}', severity='ERROR')
        try:
            send_slack(debug_webhook, f'gpumon crashed on {instance_name} {INSTANCE_ID}: {exc}')
        except Exception:
            pass
        raise
    finally:
        if CTX.exporter:
            CTX.exporter.maybe_flush(force=True)
        try:
            nvmlShutdown()
        except Exception:
            pass


if __name__ == '__main__':
    main()
