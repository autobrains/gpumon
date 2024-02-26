# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#  
#  or in the "license" file accompanying this file. This file is distributed 
#  on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either 
#  express or implied. See the License for the specific language governing 
#  permissions and limitations under the License.
###########
# Some changes to script have been made by Paul Seifer to adapt to python3.9, such as conversion of values to utf-8 strings.

import urllib.request as urllib2
import psutil
import boto3
from pynvml import *
from datetime import datetime, timedelta
from time import sleep
import time
import requests

# Constants
CACHE_DURATION = 300  # 5 minutes in seconds
THRESHOLD_PERCENTAGE = 10  # Threshold for average core utilization

# Variables
core_utilization_cache = [[] for _ in range(psutil.cpu_count())]  # List to store utilization data for each core
cpu_util_tripped = 0

def seconds_elapsed():
    return time.time() - psutil.boot_time()

def get_per_core_cpu_utilization():
    return psutil.cpu_percent(interval=1, percpu=True)

def calculate_average_core_utilization():
    return [sum(core_data) / len(core_data) if core_data else 0 for core_data in core_utilization_cache]

### CHOOSE REGION ####
EC2_REGION = 'eu-west-1'

###CHOOSE NAMESPACE PARMETERS HERE###
my_NameSpace = 'GPU-metrics-with-team-tag' 
#my_NameSpace = 'Alarm-new-namespace'
### CHOOSE PUSH INTERVAL ####
sleep_interval = 10

### CHOOSE STORAGE RESOLUTION (BETWEEN 1-60) ####
store_reso = 60

#Instance information
BASE_URL = 'http://169.254.169.254/latest/meta-data/'
INSTANCE_ID = urllib2.urlopen(BASE_URL + 'instance-id').read().decode("utf-8") 

IMAGE_ID = urllib2.urlopen(BASE_URL + 'ami-id').read().decode("utf-8") 

INSTANCE_TYPE = urllib2.urlopen(BASE_URL + 'instance-type').read().decode("utf-8") 

INSTANCE_AZ = urllib2.urlopen(BASE_URL + 'placement/availability-zone').read().decode("utf-8") 

HOSTNAME = urllib2.urlopen(BASE_URL + 'hostname').read().decode("utf-8")

EC2_REGION = INSTANCE_AZ[:-1]

TIMESTAMP = datetime.now().strftime('%Y-%m-%dT%H')
TMP_FILE = '/tmp/GPU_TEMP_'
TMP_FILE_SAVED = TMP_FILE + TIMESTAMP
# Create EC2 client to get tags
ec2 = boto3.client('ec2', region_name=EC2_REGION)
# Create CloudWatch client
cloudwatch = boto3.client('cloudwatch', region_name=EC2_REGION)

def get_network_stats(instance_id,network):
    # Set the end time to the current time
    end = datetime.utcnow()
    # Set the start time to 5 minutes ago
    start = end - timedelta(minutes=5)
    # Format the time strings
    start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    #print("From:",start_str,"Till:",end_str,"instance:",instance_id)
    data_in = cloudwatch.get_metric_statistics(
        Period=60,
        StartTime=start_str,
        EndTime=end_str,
        MetricName="NetworkPacketsIn",
        Namespace="AWS/EC2",
        Statistics=["Maximum"],
        Unit="Count",
        Dimensions=[{'Name': 'InstanceId', 'Value': str(instance_id)}]
    )
    #print("data_in:",data_in)
    data_out = cloudwatch.get_metric_statistics(
        Period=60,
        StartTime=start_str,
        EndTime=end_str,
        MetricName="NetworkPacketsOut",
        Namespace="AWS/EC2",
        Statistics=["Maximum"],
        Unit="Count",
        Dimensions=[{'Name': 'InstanceId', 'Value': str(instance_id)}]
    )
    datapoints_in = data_in.get("Datapoints", [])
    #print("datapoints_in:",datapoints_in)
    datapoints_out = data_out.get("Datapoints",[])
    first_datapoint_in = 1 #we dont want to switch off instance if we werent able to bring metrics in
    first_datapoint_out = 1
    if datapoints_in:
        # Access the first data point (assuming there is at least one)
        first_datapoint_in = datapoints_in[0]["Maximum"]
        #print(f"packetsin:{round(first_datapoint_in)}")
    else:
        first_datapoint_in = network / 2
        print("No data points found.")
    if datapoints_out:
        # Access the first data point (assuming there is at least one)
        first_datapoint_out = datapoints_out[0]["Maximum"]
        #print(f"packetsout:{round(first_datapoint_out)}")
    else:
        first_datapoint_in = network / 2
        print("No data points found.")
    network_sum = round(first_datapoint_in) + round(first_datapoint_out)
    return network_sum

# Flag to push to CloudWatch
PUSH_TO_CW = True
def get_instance_tags(instance_id):
    try:
        response = ec2.describe_tags(
            Filters=[
                {
                    'Name': 'resource-id',
                    'Values': [instance_id]
                }
            ]
        )
        tags = {tag['Key']: tag['Value'] for tag in response['Tags']}
        return tags
    except ClientError:
        print(f"Couldn't get tags for instance {instance_id}")
        raise
def send_slack(webhook_url,message):
    message=f"{message}"   
    payload = {
        "text": message
    }

    response = requests.post(webhook_url, json=payload)

    if response.status_code == 200:
        print("Message sent to Slack successfully.")
    else:
        print(f"Failed to send message to Slack. Status code: {response.status_code}")

def getPowerDraw(handle):
    try:
        powDraw = nvmlDeviceGetPowerUsage(handle) / 1000.0
        powDrawStr = '%.2f' % powDraw
    except NVMLError as err:
        powDrawStr = handleError(err)
        PUSH_TO_CW = False
    return powDrawStr

def getTemp(handle):
    try:
        temp = str(nvmlDeviceGetTemperature(handle, NVML_TEMPERATURE_GPU))
    except NVMLError as err:
        temp = handleError(err) 
        PUSH_TO_CW = False
    return temp

def getUtilization(handle):
    try:
        util = nvmlDeviceGetUtilizationRates(handle)
        gpu_util = str(util.gpu)
        mem_util = str(util.memory)
    except NVMLError as err:
        error = handleError(err)
        gpu_util = error
        mem_util = error
        PUSH_TO_CW = False
    return util, gpu_util, mem_util

def logResults(team, emp_name, i, util, gpu_util, mem_util, powDrawStr, temp, average_gpu_util,alarm_pilot_light,cpu_util_tripped,seconds,current_time,per_core_utilization,network,network_tripped):
    try:
        gpu_logs = open(TMP_FILE_SAVED, 'a+')
        writeString = '[ ' + str(current_time) + ' ] ' + 'tag:' + team + ',' + 'Employee:' + emp_name + ',' + 'GPU_ID:' + str(i) + ',' + 'GPU_Util:' + gpu_util + ',' + 'MemUtil:' + mem_util + ',' + 'powDrawStr:' + powDrawStr + ',' + 'Temp:' + temp + ',' + 'AverageGPUUtil:' + str(average_gpu_util) + ',' + 'Alarm_Pilot_value:' + str(alarm_pilot_light) + ',' + 'CPU_Util_Tripped:' + str(cpu_util_tripped) + ',' + 'Seconds Elapsed since reboot:' + str(seconds) + ',' + 'Per-Core CPU Util:' + str(per_core_utilization) + ',' + 'NetworkStats:' + str(network) + ',' + 'Network_Tripped:' + str(network_tripped) + '\n'
        #print(writeString)
        #writeString = 'tag:' + team + ',' + 'Employee:' + emp_name + ',' + str(i) + ',' + gpu_util + ',' + mem_util + ',' + powDrawStr + ',' + temp + '\n'
        gpu_logs.write(writeString)
    except:
        print("Error writing to file ", gpu_logs)
    finally:
        gpu_logs.close()
    if (PUSH_TO_CW):
        MY_DIMENSIONS=[
                    {
                        'Name': 'InstanceId',
                        'Value': INSTANCE_ID
                    },
                    {
                        'Name': 'ImageId',
                        'Value': IMAGE_ID
                    },
                    {
                        'Name': 'InstanceType',
                        'Value': INSTANCE_TYPE
                    },
                    {
                        'Name': 'GPUNumber',
                        'Value': str(i)
                    },
                    {
                        'Name': 'InstanceTag',
                        'Value': str(team)
                    },
                    {
                        'Name': 'EmployeeTag',
                        'Value': str(emp_name)
                    }

                ]
        cloudwatch.put_metric_data(
            MetricData=[
                {
                    'MetricName': 'GPU Usage',
                    'Dimensions': MY_DIMENSIONS,
                    'Unit': 'Percent',
                    'StorageResolution': store_reso,
                    'Value': util.gpu
                },
                {
                    'MetricName': 'Memory Usage',
                    'Dimensions': MY_DIMENSIONS,
                    'Unit': 'Percent',
                    'StorageResolution': store_reso,
                    'Value': util.memory
                },
                {
                    'MetricName': 'Power Usage (Watts)',
                    'Dimensions': MY_DIMENSIONS,
                    'Unit': 'None',
                    'StorageResolution': store_reso,
                    'Value': float(powDrawStr)
                },
                {
                    'MetricName': 'Temperature (C)',
                    'Dimensions': MY_DIMENSIONS,
                    'Unit': 'None',
                    'StorageResolution': store_reso,
                    'Value': int(temp)
                },
                {
                    'MetricName': 'Alarm Pilot Light (1/0)',
                    'Dimensions': MY_DIMENSIONS,
                    'Unit': 'None',
                    'StorageResolution': store_reso,
                    'Value': float(alarm_pilot_light)
                },
                {
                    'MetricName': 'Average GPU Utilization',
                    'Dimensions': MY_DIMENSIONS,
                    'Unit': 'Percent',
                    'StorageResolution': store_reso,
                    'Value': float(average_gpu_util)
                },
                {
                    'MetricName': 'CPU Utilization Low Tripped',
                    'Dimensions': MY_DIMENSIONS,
                    'Unit': 'None',
                    'StorageResolution': store_reso,
                    'Value': float(cpu_util_tripped)
                },
                {
                    'MetricName': 'Network Tripped',
                    'Dimensions': MY_DIMENSIONS,
                    'Unit': 'None',
                    'StorageResolution': store_reso,
                    'Value': float(network_tripped)
                }


        ],
            Namespace=my_NameSpace
        )
    

nvmlInit()
deviceCount = nvmlDeviceGetCount()
def main():
    global core_utilization_cache
    alarm_pilot_light = 0
    network_tripped = 0
    network = 99
    cpu_util_tripped = False
    tags = get_instance_tags(INSTANCE_ID)
    if 'Name' in tags:
        instance_name = str(tags['Name'])
    else:
        instance_name = "NO_NAME_TAG"
    if 'Team' in tags:
        team = str(tags['Team'])
    else:
        team = "NO_TAG"
    if 'Employee' in tags:
        emp_name = str(tags['Employee'])
    else:
        emp_name = "NO_TAG"
    #print("team_var:",team_var)
    debug_webhook = os.getenv("DEBUG_WEBHOOK_URL")
    team_var = str(team) + "_TEAM_WEBHOOK_URL"
    try:
        team_webhook = os.getenv(team_var)
    except:
        try:
            send_slack(debug_webhook,f"achtung, could not resolve team_webhook_url on {instance_name} {INSTANCE_ID} - {team_var}")
            team_webhook = debug_webhook
        except:
            print(f"AAAARGH! DEBUG WEBHOOK is None:${DEBUG_WEBHOOK_URL} or cannot send data out, debug")
    try:
        while True:
            total_gpu_util = 0
            cpu_util_tripped = False
            try:
                # Get per-core CPU utilization
                per_core_utilization = get_per_core_cpu_utilization()

                # Cache utilization data for each core
                for i, core_util in enumerate(per_core_utilization):
                    core_utilization_cache[i].append(core_util)

                # Remove outdated data from the cache
                current_time = datetime.now()
                core_utilization_cache = [core_data[-int(CACHE_DURATION / 1):] for core_data in core_utilization_cache]
                # Calculate average core utilization
                average_core_utilization = calculate_average_core_utilization()
                #print(average_core_utilization)
                # Check if any core exceeds the threshold
                if any(utilization > THRESHOLD_PERCENTAGE for utilization in average_core_utilization):
                    cpu_util_tripped = True
                    #print("Threshold exceeded. Changing variable to True.")
            except:
                    print('Could not get cpu core utilization statistics, debug')


            PUSH_TO_CW = True

            #print("Teamwebhook:",team_webhook)
            #mmesage="testing 123..."
            #send_slack(team_webhook,mmesage)
            # Find the metrics for each GPU on instance
            for i in range(deviceCount):
                handle = nvmlDeviceGetHandleByIndex(i)

                powDrawStr = getPowerDraw(handle)
                temp = getTemp(handle)
                util, gpu_util, mem_util = getUtilization(handle)
                total_gpu_util += float(gpu_util)
                #logResults(team, emp_name, i, util, gpu_util, mem_util, powDrawStr, temp, average_gpu_util)

            average_gpu_util = total_gpu_util / deviceCount
            
            #print(f'Average GPU utilization:',average_gpu_util)
            seconds = round(float(seconds_elapsed()))
            #print('seconds:',seconds)
            #Calculate last 5 minutes for network metrics
            #end = current_time
            #start = end - timedelta(minutes=5)
            # calculate combined network in and out last 5 min
            network = get_network_stats(instance_id=INSTANCE_ID,network=network)
            if network_tripped == 0 and network <= 10000:
                network_tripped = 1
            #print(f"name_tag:{instance_name} network:{network}")
            if seconds >= 7200:
                if round(float(average_gpu_util)) <= 10 and cpu_util_tripped == False and network <= 10000:    
            #cpu util tripped == True means that there was higher than threshold cpu core activity and we cant stop the instance because of it
                    if alarm_pilot_light == 0:
                        #print(f'CPU or GPU below {THRESHOLD_PERCENTAGE}% threshold, turning pilot light ON')
                        alarm_pilot_light = 1
                        mmessage=f"[ {current_time} ] INSTANCE: {instance_name} - {INSTANCE_ID} ({HOSTNAME}) CPU, GPU and NETWORK seems idle, TURNED ALARM PILOT LIGHT to: ON, instance is expected to stop in: 3 hours"
                        try:
                            send_slack(team_webhook, mmessage)
                        except:
                            print(f"Could not send slack to webhook:{team_webhook}")
                else:
                    if alarm_pilot_light == 1:
                        alarm_pilot_light = 0
                        mmessage=f"[ {current_time} ] INSTANCE: {instance_name} - {INSTANCE_ID} ({HOSTNAME}) CPU, GPU and NETWORK over minimum threshold, TURNED ALARM PILOT LIGHT to: OFF"
                        try:
                            send_slack(team_webhook, mmessage)
                        except:
                            print(f"Could not send slack to webhook:{team_webhook}")
                    #print(f'CPU or GPU above {THRESHOLD_PERCENTAGE}% threshold, turning pilot light OFF')
            else:
                alarm_pilot_light = 0
            # Log the results
            for i in range(deviceCount):
                handle = nvmlDeviceGetHandleByIndex(i)
                try:
                    logResults(team, emp_name, i, util, gpu_util, mem_util, powDrawStr, temp, average_gpu_util, alarm_pilot_light, cpu_util_tripped, seconds,current_time,per_core_utilization,network,network_tripped)
                except:
                    print("could not write to disk")
            sleep(sleep_interval)
    finally:
        nvmlShutdown()

if __name__=='__main__':
    main()
