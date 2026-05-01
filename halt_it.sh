#!/bin/bash
# Parse gpumon logs; if Alarm_Pilot was "1" for the last 2 hours, stop/terminate
# the instance.  Intended to run as a cron job every 10 minutes.
# (c) Paul Seifer, Autobrains LTD
#
# Example cron entry:
#   */10 * * * * bash /root/gpumon/halt_it.sh | tee -a /tmp/halt_it_log.txt

TIMESTAMP_FILE="/tmp/timestamp.txt"
if [ ! -f "${TIMESTAMP_FILE}" ]; then
    echo "[ $(date) ] Stop file not found, will continue the script"
else
    current_time=$(date +%s)
    _timestamp=$(cat "${TIMESTAMP_FILE}")
    timestamp=$(date -d "${_timestamp}" +%s)
    time_diff=$((current_time - timestamp))
    two_hours=$((2 * 60 * 60))

    if [ "$time_diff" -ge "$two_hours" ]; then
        echo "[ $(date) ] More than 2 hours have passed since the timestamp."
        rm -f "${TIMESTAMP_FILE}" 2>/dev/null
    else
        remaining=$((two_hours - time_diff))
        echo "[ $(date) ] Less than 2 hours have passed. $remaining seconds remaining. Will not halt now."
        exit 1
    fi
fi

HALT_FILE=/tmp/halt_it.info

fetch_metadata() {
    TOKEN=$(curl -s --max-time 5 -X PUT "http://169.254.169.254/latest/api/token" \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" 2>/dev/null)
    AWSREGION=$(curl -s --max-time 5 -H "X-aws-ec2-metadata-token: $TOKEN" \
        http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null)
    INSTANCE_ID=$(curl -s --max-time 5 -H "X-aws-ec2-metadata-token: $TOKEN" \
        http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null)
    POLICY=$(aws ec2 describe-tags \
        --filters "Name=resource-id,Values=$INSTANCE_ID" "Name=key,Values=GPUMON_POLICY" \
        --query "Tags[0].Value" --output text --region "$AWSREGION")

    if nvidia-smi --list-gpus >/dev/null 2>&1; then
        DTYPE=$(nvidia-smi --list-gpus | wc -l)
    else
        DTYPE="0"
    fi
}

if [ ! -s "$HALT_FILE" ]; then
    fetch_metadata
    {
        echo "INSTANCE_ID=${INSTANCE_ID}"
        echo "DTYPE=${DTYPE}"
        echo "AWSREGION=${AWSREGION}"
        echo "POLICY=${POLICY}"
    } > "$HALT_FILE"
else
    INSTANCE_ID=$(grep "^INSTANCE_ID=" "$HALT_FILE" | cut -d= -f2)
    DTYPE=$(grep "^DTYPE=" "$HALT_FILE" | cut -d= -f2)
    AWSREGION=$(grep "^AWSREGION=" "$HALT_FILE" | cut -d= -f2)
    POLICY=$(grep "^POLICY=" "$HALT_FILE" | cut -d= -f2)
fi

# If any value is empty the cached file is bad — drop it and refetch
if [ -z "$INSTANCE_ID" ] || [ -z "$DTYPE" ] || [ -z "$AWSREGION" ] || [ -z "$POLICY" ]; then
    rm -f "$HALT_FILE"
    fetch_metadata
    {
        echo "INSTANCE_ID=${INSTANCE_ID}"
        echo "DTYPE=${DTYPE}"
        echo "AWSREGION=${AWSREGION}"
        echo "POLICY=${POLICY}"
    } > "$HALT_FILE"
fi

if [[ "${INSTANCE_ID}" == "" ]] || [[ "$(echo "${INSTANCE_ID}" | grep -E 'i-[a-f0-9]{8,17}')" == "" ]]; then
    echo "[ $(date) ] Need INSTANCE_ID like i-02cb1c2e2dececfcd, exiting"
    exit 1
fi

if [[ -z "$DTYPE" || "$DTYPE" == "0" ]]; then
    SEP=6
    FILE="CPUMON_LOGS_"
    DEFAULT_STEP=500
else
    SEP=12
    FILE="GPU_TEMP_"
    if [ "$DTYPE" -lt 4 ]; then
        DEFAULT_STEP=500
    else
        DEFAULT_STEP=2000
    fi
fi

case "$POLICY" in
    SPOT|SEVERE)
        STEP=90
        ;;
    *)
        STEP=$DEFAULT_STEP
        ;;
esac
echo "[ $(date) ] Instance: ${INSTANCE_ID}  Policy: ${POLICY}  Step: ${STEP}"

# ── AWS CLI health check ──────────────────────────────────────────────────────
echo "[ $(date) ] Checking AWS CLI..."
if ! timeout 30 aws sts get-caller-identity --region "${AWSREGION}" &>/dev/null; then
    echo "[ $(date) ] AWS CLI not working, attempting reinstall..."
    DEBIAN_FRONTEND=noninteractive timeout 120 apt install -y awscli || \
        timeout 120 python3 -m pip install --upgrade awscli boto3 botocore || true
fi

# ── Credentials via Secrets Manager ──────────────────────────────────────────
# The secret lives in a specific region (may differ from the instance region).
# Override with GPUMON_SECRET_REGION / GPUMON_SECRET_ID env vars as needed.
SECRET_ID="${GPUMON_SECRET_ID:-AB/InstanceRole}"
SECRET_REGION="${GPUMON_SECRET_REGION:-eu-west-1}"
echo "[ $(date) ] Getting creds from SM (secret: ${SECRET_ID} region: ${SECRET_REGION})..."
creds_tmp=$(aws secretsmanager get-secret-value \
    --secret-id "${SECRET_ID}" \
    --region "${SECRET_REGION}" \
    2>/dev/null \
    | grep SecretString | rev | cut -c2- | rev | cut -d ":" -f2- | tr -d " " | tr -d '"')
if [ "${creds_tmp}" == "" ]; then
    echo "[ $(date) ] Warning: could not get creds from SM, using instance role: $(aws sts get-caller-identity 2>/dev/null)"
else
    export AWS_ACCESS_KEY_ID=$(echo "${creds_tmp}" | cut -d ":" -f1)
    export AWS_SECRET_ACCESS_KEY=$(echo "${creds_tmp}" | cut -d ":" -f2-)
fi

NOGO="TRUE"
REASON="NO_REASON"
wall_message="
++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
This instance:${INSTANCE_ID} seems to have been idle for the last
2 hours, will shut it down in 3 minutes from now. If you just logged in,
please wait a couple of minutes and restart it from the AWS console, or type:

    sudo bash /root/gpumon/kill_halt.sh

to stop the shutdown now. The shutdown pause will last 2 hours.
++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++"

# ── Find latest log file ──────────────────────────────────────────────────────
filename=$(ls -lit /tmp/${FILE}* 2>/dev/null | head -1 | rev | cut -d " " -f1 | rev)
if [ "${filename}" == "" ]; then
    NOGO="TRUE"
    REASON="LOG_NOT_FOUND"
else
    NOGO="FALSE"
    if [ "$(wc -l < "${filename}")" -lt "${STEP}" ]; then
        NOGO="TRUE"
        REASON="LOG_EXISTS_BUT_NOT_ENOUGH_DATA_IN_IT_SO_FAR:($(wc -l < "${filename}"))_LINES"
    fi
fi

# ── Analyse log ───────────────────────────────────────────────────────────────
if [ "${NOGO}" == "FALSE" ]; then
    check=$(tail -"${STEP}" "${filename}" | cut -d ":" -f"${SEP}" | sort | uniq -c | grep "CPU_Util")
    if [ "${check}" == "" ]; then
        NOGO="TRUE"
        REASON="LOG_FILE_EXISTS_BUT_NO_VALID_DATA:${check}"
    else
        result=$(tail -"${STEP}" "${filename}" | cut -d ":" -f"${SEP}" | sort | uniq -c | grep "0,CPU_Util_Tripped")
        if [ "${result}" == "" ]; then
            positive=$(tail -"${STEP}" "${filename}" | cut -d ":" -f"${SEP}" | sort | uniq -c | grep "1,CPU_Util_Tripped")
            if [ "${positive}" == "" ]; then
                NOGO="TRUE"
                REASON="ALARM_WASNT_ON_DURING_LAST_2_HOURS,INCONCLUSIVE,DEBUG:${positive} result:${result}"
            else
                NOGO="FALSE"
                REASON="WE_GOT_PILOT_ONLY_FOR_2_HOURS_ITS_A_GO:${positive} result:${result}"
            fi
        else
            NOGO="TRUE"
            REASON="DATA_IS_OK_BUT_GOT_ACTIVITY_SPIKE:${check}"
        fi
    fi
fi

# ── Shutdown decision ─────────────────────────────────────────────────────────
if [ "${NOGO}" == "TRUE" ]; then
    echo "[ $(date) ] NO-GO: ${REASON}"
else
    echo "[ $(date) ] GO — shutting down. Reason: ${REASON}" | tee -a /root/gpumon_persistent.log
    wall "[ $(date) ] ${wall_message}"
    sleep 180
    wall "[ $(date) ] 3 minutes passed — shutting down now. Bye!"
    if [ "${POLICY}" == "SPOT" ]; then
        res=$(aws ec2 terminate-instances --instance-ids "${INSTANCE_ID}" --region "${AWSREGION}" 2>&1)
    else
        res=$(aws ec2 stop-instances --instance-ids "${INSTANCE_ID}" --region "${AWSREGION}" 2>&1)
    fi
    echo "[ $(date) ] AWS result: ${res}"
fi
