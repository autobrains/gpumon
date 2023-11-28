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
import boto3
from pynvml import *
from datetime import datetime
from time import sleep

### CHOOSE REGION ####
EC2_REGION = 'eu-west-1'

###CHOOSE NAMESPACE PARMETERS HERE###
my_NameSpace = 'GPU-metrics-with-team-tag' 

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

EC2_REGION = INSTANCE_AZ[:-1]

TIMESTAMP = datetime.now().strftime('%Y-%m-%dT%H')
TMP_FILE = 'GPU_TEMP_'
TMP_FILE_SAVED = TMP_FILE + TIMESTAMP
# Create EC2 client to get tags
ec2 = boto3.client('ec2', region_name=EC2_REGION)
# Create CloudWatch client
cloudwatch = boto3.client('cloudwatch', region_name=EC2_REGION)


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

def logResults(team, i, util, gpu_util, mem_util, powDrawStr, temp):
    try:
        gpu_logs = open(TMP_FILE_SAVED, 'a+')
        writeString = 'tag:' + team + ',' + 'Employee:' + emp_name + ',' + str(i) + ',' + gpu_util + ',' + mem_util + ',' + powDrawStr + ',' + temp + '\n'
        #print(writeString)
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
        ],
            Namespace=my_NameSpace
        )
    

nvmlInit()
deviceCount = nvmlDeviceGetCount()

def main():
    try:
        while True:
            tags = get_instance_tags(INSTANCE_ID)
            if 'Team' in tags:
                team = str(tags['Team'])
            if 'Employee' in tags:
                emp_name = str(tags['Employee'])
            PUSH_TO_CW = True
            # Find the metrics for each GPU on instance
            for i in range(deviceCount):
                handle = nvmlDeviceGetHandleByIndex(i)

                powDrawStr = getPowerDraw(handle)
                temp = getTemp(handle)
                util, gpu_util, mem_util = getUtilization(handle)
                logResults(team, emp_name, i, util, gpu_util, mem_util, powDrawStr, temp)

            sleep(sleep_interval)

    finally:
        nvmlShutdown()

if __name__=='__main__':
    main()
