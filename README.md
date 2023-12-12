 Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.

 Licensed under the Apache License, Version 2.0 (the "License").
 You may not use this file except in compliance with the License.
 A copy of the License is located at

     http://www.apache.org/licenses/LICENSE-2.0
  
  or in the "license" file accompanying this file. This file is distributed 
  on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either 
  express or implied. See the License for the specific language governing 
  permissions and limitations under the License.

Some changes to script have been made by Paul Seifer to adapt to python3.9, such as conversion of values to utf-8 strings.

To install the script on an EC2 instance that has GPUs, please follow the steps:
```
apt update -y && apt install curl -y && apt install python3-pip -y && pip install boto3 && pip install pynvml && pip install psutil
git clone https://github.com/autobrains/gpumon.git
rm -f /etc/systemd/system/gpumon.service
touch /etc/systemd/system/gpumon.service
chmod 664 /etc/systemd/system/gpumon.service

tee -a /etc/systemd/system/gpumon.service > /dev/null <<EOT
#gpumon-service
[Unit]
Description=gpumon_proccess
After=network.target
Wants=network.target

[Service]
User=root
Group=root
Type=simple
ExecStart=python3 /root/gpumon/gpumon.py
[Install]
WantedBy=multi-user.target
EOT


systemctl daemon-reload
systemctl start gpumon
systemctl enable gpumon
systemctl status gpumon
```

To update the instance with a new version of code after merge to master use `update_remote.sh`

Syntax: 

`update_remote.sh <ip_of_the_ec2_instance> <path_to_ssh_key>`