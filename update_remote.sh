#!/bin/bash
instance_ip="${1}"
key_path="${2}"
if [ "${instance_ip}" == "" ]; then
echo "Provide IP of the instance,exiting"
exit 1
fi
if [ "${key_path}" == "" ]; then
echo "Provide path to .pem key that can be used to SSH to this instance:${instance_ip}"
exit 1
fi
echo "Will copy script for running locally on instance..."
scp -i ${key_path} ~/work/gpumon/update_gpumon.txt ubuntu@${instance_ip}:/home/ubuntu/update_gpumon.sh
error=$?
if [ "${error}" != "0" ]; then
echo "COPY FAILED:${error}"
exit 1
fi
echo "Copy done, moving on to moving file to /root/"
ssh -i ${key_path} ubuntu@${instance_ip} sudo mv -v /home/ubuntu/update_gpumon.sh /root/
error=$?
if [ "${error}" != "0" ]; then
echo "MOVE FAILED:${error}"
exit 1
fi
echo "Moving file done, moving on to running the script on instance"
ssh -i ${key_path} ubuntu@${instance_ip} sudo bash /root/update_gpumon.sh
error=$?
if [ "${error}" != "0" ]; then
echo "SCRIPT ACTIVATION FAILED:${error}"
exit 1
fi
echo "We are done, instance:${instance_ip} updated with new version of gpumon.py"
