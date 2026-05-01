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
    } > "$HALT_FILE"
else
    INSTANCE_ID=$(grep "^INSTANCE_ID=" "$HALT_FILE" | cut -d= -f2)
    DTYPE=$(grep "^DTYPE=" "$HALT_FILE" | cut -d= -f2)
    AWSREGION=$(grep "^AWSREGION=" "$HALT_FILE" | cut -d= -f2)
fi

# If any cached value is empty the file is bad — drop it and refetch
if [ -z "$INSTANCE_ID" ] || [ -z "$DTYPE" ] || [ -z "$AWSREGION" ]; then
    rm -f "$HALT_FILE"
    fetch_metadata
    {
        echo "INSTANCE_ID=${INSTANCE_ID}"
        echo "DTYPE=${DTYPE}"
        echo "AWSREGION=${AWSREGION}"
    } > "$HALT_FILE"
fi

# POLICY is always fetched fresh so tag changes take effect on the next cron run
POLICY=$(aws ec2 describe-tags \
    --filters "Name=resource-id,Values=${INSTANCE_ID}" "Name=key,Values=GPUMON_POLICY" \
    --query "Tags[0].Value" --output text --region "${AWSREGION}" 2>/dev/null)
# Treat missing/None tag as STANDARD
if [ -z "${POLICY}" ] || [ "${POLICY}" = "None" ]; then
    POLICY="STANDARD"
fi

if [[ "${INSTANCE_ID}" == "" ]] || [[ "$(echo "${INSTANCE_ID}" | grep -E 'i-[a-f0-9]{8,17}')" == "" ]]; then
    echo "[ $(date) ] Need INSTANCE_ID like i-02cb1c2e2dececfcd, exiting"
    exit 1
fi

if [[ -z "$DTYPE" || "$DTYPE" == "0" ]]; then
    SEP=6
    FILE="CPUMON_LOGS_"
else
    SEP=12
    FILE="GPU_TEMP_"
fi

# Time-based idle window — SPOT/SEVERE get 15 min; everything else 2 h
case "$POLICY" in
    SPOT|SEVERE) WINDOW_SECONDS=$((15 * 60)) ;;
    *)           WINDOW_SECONDS=$((2 * 60 * 60)) ;;
esac
echo "[ $(date) ] Instance: ${INSTANCE_ID}  Policy: ${POLICY}  Window: ${WINDOW_SECONDS}s"

# ── AWS CLI health check ──────────────────────────────────────────────────────
echo "[ $(date) ] Checking AWS CLI..."
if ! timeout 30 aws sts get-caller-identity --region "${AWSREGION}" &>/dev/null; then
    echo "[ $(date) ] AWS CLI not working, attempting reinstall..."
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y awscli || \
        timeout 120 python3 -m pip install --upgrade awscli boto3 botocore || true
fi

# ── Credentials via Secrets Manager ──────────────────────────────────────────
# The secret lives in a specific region (may differ from the instance region).
# Override with GPUMON_SECRET_REGION / GPUMON_SECRET_ID env vars as needed.
SECRET_ID="${GPUMON_SECRET_ID:-AB/InstanceRole}"
SECRET_REGION="${GPUMON_SECRET_REGION:-eu-west-1}"
echo "[ $(date) ] Getting creds from SM (secret: ${SECRET_ID} region: ${SECRET_REGION})..."

# Ensure jq is available for robust JSON parsing
if ! command -v jq &>/dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y jq -q 2>/dev/null || true
fi

secret_json=$(aws secretsmanager get-secret-value \
    --secret-id "${SECRET_ID}" \
    --region "${SECRET_REGION}" \
    --query "SecretString" \
    --output text 2>/dev/null)

if [ -z "${secret_json}" ] || [ "${secret_json}" = "None" ]; then
    echo "[ $(date) ] Warning: could not get creds from SM, using instance role: $(aws sts get-caller-identity 2>/dev/null)"
elif command -v jq &>/dev/null; then
    export AWS_ACCESS_KEY_ID=$(echo "${secret_json}" | jq -r '.AccessKeyId // empty')
    export AWS_SECRET_ACCESS_KEY=$(echo "${secret_json}" | jq -r '.SecretAccessKey // empty')
    export AWS_SESSION_TOKEN=$(echo "${secret_json}" | jq -r '.SessionToken // empty')
    if [ -z "${AWS_ACCESS_KEY_ID}" ]; then
        echo "[ $(date) ] Warning: SM secret parsed but AccessKeyId not found — using instance role"
        unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
    else
        echo "[ $(date) ] SM credentials loaded"
    fi
else
    echo "[ $(date) ] jq unavailable — using instance role"
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

# ── Collect log lines within the idle window ──────────────────────────────────
# Log files rotate hourly and are named <PREFIX>YYYY-MM-DDTHH.
# Build the list of candidate files by stepping through each hour in the window,
# then filter lines by the embedded ISO timestamp using awk string comparison
# (ISO format is lexicographically ordered so >= works without date arithmetic).
now=$(date +%s)
cutoff=$((now - WINDOW_SECONDS))
cutoff_str=$(date -d "@${cutoff}" +'%Y-%m-%d %H:%M:%S')

candidate_files=""
t=$cutoff
while [ "$t" -le "$((now + 3600))" ]; do
    f="/tmp/${FILE}$(date -d @${t} +'%Y-%m-%dT%H')"
    [ -f "$f" ] && candidate_files="${candidate_files} ${f}"
    t=$((t + 3600))
done

if [ -z "${candidate_files}" ]; then
    NOGO="TRUE"
    REASON="NO_LOG_FILES_FOUND"
else
    # awk substr(line,3,19) extracts "YYYY-MM-DD HH:MM:SS" from "[ YYYY-MM-DD HH:MM:SS..."
    # shellcheck disable=SC2086
    window_lines=$(cat ${candidate_files} 2>/dev/null | awk -v cutoff="${cutoff_str}" '{
        ts = substr($0, 3, 19)
        if (ts >= cutoff) print
    }')

    line_count=$(echo "${window_lines}" | grep -c .)

    # Require 90% of expected samples so the full window is covered.
    # At 10 s/sample, 90% of WINDOW_SECONDS = WINDOW_SECONDS * 9 / 100.
    # Multi-GPU instances write DTYPE lines per tick so multiply accordingly.
    # This prevents a 1-h-idle / 1-h-active pattern from passing the 2-h check.
    if [ "${DTYPE}" -gt 0 ]; then
        min_lines=$(( WINDOW_SECONDS * 9 / 100 * DTYPE ))
    else
        min_lines=$(( WINDOW_SECONDS * 9 / 100 ))
    fi
    echo "[ $(date) ] Lines in window: ${line_count}  min required: ${min_lines}"

    if [ "${line_count}" -lt "${min_lines}" ]; then
        NOGO="TRUE"
        REASON="INSUFFICIENT_DATA:${line_count}_lines_in_${WINDOW_SECONDS}s_window_(need_${min_lines})"
    else
        # ── Analyse window lines ───────────────────────────────────────────────
        check=$(echo "${window_lines}" | cut -d ":" -f"${SEP}" | sort | uniq -c | grep "CPU_Util")
        if [ "${check}" == "" ]; then
            NOGO="TRUE"
            REASON="LOG_EXISTS_BUT_NO_VALID_DATA"
        else
            result=$(echo "${window_lines}" | cut -d ":" -f"${SEP}" | sort | uniq -c | grep "0,CPU_Util_Tripped")
            if [ "${result}" == "" ]; then
                positive=$(echo "${window_lines}" | cut -d ":" -f"${SEP}" | sort | uniq -c | grep "1,CPU_Util_Tripped")
                if [ "${positive}" == "" ]; then
                    NOGO="TRUE"
                    REASON="ALARM_WASNT_ON_DURING_WINDOW,INCONCLUSIVE"
                else
                    NOGO="FALSE"
                    REASON="PILOT_ON_FOR_FULL_${WINDOW_SECONDS}s_WINDOW:${positive}"
                fi
            else
                NOGO="TRUE"
                REASON="ACTIVITY_SPIKE_IN_WINDOW:${result}"
            fi
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
