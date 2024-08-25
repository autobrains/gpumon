#!/bin/bash
#This script parses gpumon logs and if it finds out that Alarm_Pilot was 
#present ONLY with value "1" for last 2 hours it will shut down the instance it runs on, 
#it is intended to be run as cronjob every 10 minutes. (c)Paul Seifer, Autobrains LTD
# something like: */10 * * * * bash /root/gpumon/halt_it.sh | tee -a /tmp/halt_it_log.txt
TIMESTAMP_FILE="/tmp/timestamp.txt"
if [ ! -f "${TIMESTAMP_FILE}" ]; then
    echo "[ $(date) ] Stop file not found, will continue the script"
else
    current_time=$(date +%s)
    _timestamp=$(cat "${TIMESTAMP_FILE}")
    timestamp=$(date -d "${_timestamp}" +%s)
    time_diff=$((current_time - timestamp))
    two_hours=$((2 * 60 * 60))  # 2 hours in seconds

    if [ $time_diff -ge $two_hours ]; then
        echo "[ $(date) ] More than 2 hours have passed since the timestamp."
        rm -f ${TIMESTAMP_FILE} 2>/dev/null
    else
        remaining=$((two_hours - time_diff))
        echo "[ $(date) ] Less than 2 hours have passed. $remaining seconds remaining. will not halt the server now"
        exit 1
    fi
fi
if [ ! -s "/tmp/halt_it.info" ]; then
   TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" 2>/dev/null)
   AWSREGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" -v http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null)
   INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" -v http://169.254.169.254/latest/meta-data/instance-id/ 2>/dev/null | cut -d "." -f2)
   nvidia-smi --list-gpus 1>/dev/null
   error=$?
   if [ "${error}" != "0" ]; then
      DTYPE="0"
   else
     DTYPE=$(nvidia-smi --list-gpus | wc -l)
   fi

   echo "INSTANCE_ID=${INSTANCE_ID}" > /tmp/halt_it.info
   echo "DTYPE=${DTYPE}" >> /tmp/halt_it.info
   echo "AWSREGION=${AWSREGION}" >> /tmp/halt_it.info
  else
   INSTANCE_ID=$(cat /tmp/halt_it.info | grep "INSTANCE_ID" | cut -d "=" -f2)
   DTYPE=$(cat /tmp/halt_it.info | grep DTYPE | cut -d "=" -f2)
   AWSREGION=$(cat /tmp/halt_it.info | grep AWSREGION | cut -d "=" -f2)
fi

if [[ "${INSTANCE_ID}" == "" ]] || [[ "$(echo ${INSTANCE_ID} | grep -E 'i-[a-f0-9]{8,17}')" == "" ]]; then
        echo "[ $(date) ] Need INSTANCE_ID, like: i-02cb1c2e2dececfcd, exiting"
        exit 1
fi
if [[ "${DTYPE}" == "" ]] || [[ "${DTYPE}" == "0" ]]; then
        SEP=6
    FILE="CPUMON_LOGS_"
    STEP=500
else
        SEP=12
        FILE="GPU_TEMP_"
        if [ "${DTYPE}" -lt "4" ]; then
                STEP=500 #single GPU gives out single line in log, 500 lines = 2 hours
        else
                STEP=2000 #4 gpus give 4 lines in log
        fi
fi
echo "[ $(date) ] Checking awscli validity"
check=$(aws s3 ls 2>&1 | grep docevents | grep cannot);
if [ "${check}" != "" ]; then
   echo "[ $(date) ] need to fix, will run: python3 -m pip install --upgrade boto3 botocore awscli"
   python3 -m pip install --upgrade boto3 botocore awscli --break-system-packages
   echo "[ $(date) ] done fixin"
 else
   echo "[ $(date) ] no problem with awscli...continue..."
fi
check=$(sudo aws s3 ls 2>&1 | grep docevents | grep cannot);
if [ "${check}" != "" ]; then
   echo "[ $(date) ] need to fix, will run: sudo python3 -m pip install --upgrade boto3 botocore awscli"
   sudo python3 -m pip install --upgrade boto3 botocore awscli --break-system-packages
   echo "[ $(date) ] done fixin"
 else
   echo "[ $(date) ] no problem with sudo awscli...continue..."
fi
echo "[ $(date) ] Getting creds from SM..."
creds_tmp=$(aws secretsmanager get-secret-value --secret-id "AB/InstanceRole" --region eu-west-1 |grep SecretString | rev | cut -c2- | rev | cut -d ":" -f2- | tr -d " " | tr -d '"')
if [ "${creds_tmp}" == "" ]; then
        echo "[ $(date) ] Warning, could not get creds from SM, will use whatever Role current user has:$(aws sts get-caller-identity)"
else
        export AWS_ACCESS_KEY_ID=$(echo "${creds_tmp}" | cut -d ":" -f1)
        export AWS_SECRET_ACCESS_KEY=$(echo "${creds_tmp}" | cut -d ":" -f2-)
fi

NOGO="TRUE"
REASON="NO_REASON"
wall_message="""
++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
This instance:${INSTANCE_ID} seems to be have been idle for the last 
2 hours, will shut it down in 3 minutes from now. If you have been 
unfortunate to just logged in into it, please wait couple of minutes and 
start it again from AWS console or script or type:

sudo bash /root/gpumon/kill_halt.sh

to stop the shutdown now. The shutdown pause in this case will last 2 hours
after which the shutdown sequence will resume.
+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
"""
#lets check if we have TMP file
filename=$(ls -lit /tmp/${FILE}* | head -1 | rev | cut -d " " -f1 | rev)
if [ "${filename}" == "" ]; then
    NOGO="TRUE"
    REASON="LOG_NOT_FOUND:${filename}"
  else
    NOGO="FALSE"
    if [ "$(cat ${filename} | wc -l)" -lt "${STEP}" ]; then
       NOGO="TRUE"
       REASON="LOG_EXISTS_BUT_NOT_ENOUGH_DATA_IN_IT_SO_FAR:($(cat ${filename} | wc -l))_LINES"
    fi
fi
#Now lets check if we have correct output
if [ "${NOGO}" == "FALSE" ]; then 
    check=$(tail -${STEP} ${filename} | cut -d ":" -f$SEP | sort | uniq -c | grep "CPU_Util")
    if [ "${check}" == "" ]; then
       NOGO="TRUE"
       REASON="LOG_FILE_EXISTS_BUT_NO_VALID_DATA:${check}"
    else
    #we got data, now lets analyse it
      result=$(tail -${STEP} ${filename} | cut -d ":" -f$SEP | sort | uniq -c | grep "0,CPU_Util_Tripped")
      if [ "${result}" == "" ]; then
          #looks like there is data but only indication that Alarm was on for last two hours, lets make sure
          positive=$(tail -${STEP} ${filename} | cut -d ":" -f$SEP | sort | uniq -c | grep "1,CPU_Util_Tripped")
          if [ "${positive}" == "" ]; then
              NOGO="TRUE"
              REASON="ALARM_WASNT_ON_DURING_LAST_2_HOURS,INCONCLUSIVE,DEBUG:${positive} result:${result}"
              #this shouldnt happen, if we get this message in logs something is very wrong
          else
              NOGO="FALSE"
              REASON="WE_GOT_PILOT_ONLY_FOR_2_HOURS_ITS_A_GO:${positive} result:${result}"
              #this means there was good amount of data and not a single line of Alarm=0 in it, we should turn the instance off
          fi
      else
        NOGO="TRUE"
        REASON="DATA_IS_OK_BUT_GOT_ACTIVITY_SPIKE:${check}"
        #This is normal - at least a single Alarm=0 in last two hours = not turning instance off
      fi
    fi
fi

#main if
if [ "${NOGO}" == "TRUE" ]; then
   echo "[ $(date) ] WE ARE A NO-GO, BECAUSE:${REASON} TILL LATER, TA TA"
   #res=$(sudo aws ec2 describe-instances --instance-ids "${INSTANCE_ID}" --region $AWSREGION)
   #echo "[ $(date) ] debug: got result for aws describe-instances: ${res}"
else
   echo "[ $(date) ] LOOKS LIKE WE ARE A GO - WILL SHUT THE INSTANCE DOWN, REASON:${REASON}" | tee -a /root/gpumon_persistent.log
   wall "[ $(date) ] ${wall_message}"
   sleep 180
   wall "[ $(date) ] Well, 3 minutes have passed, shutdown is now...Bye Bye"
   res=$(sudo aws ec2 stop-instances --instance-ids "${INSTANCE_ID}" --region $AWSREGION 2>&1)
   echo "[ $(date) ] debug: got result for aws stop-instances: ${res}"
fi
