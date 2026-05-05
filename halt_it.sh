#!/bin/bash
# Parse gpumon logs; if Alarm_Pilot was "1" for the idle window, stop/terminate
# the instance.  Intended to run as a cron job every 10 minutes.
# (c) Paul Seifer, Autobrains LTD
#
# Example cron entry:
#   */10 * * * * bash /root/gpumon/halt_it.sh | tee -a /tmp/halt_it_log.txt

# ── IMDS + identity ───────────────────────────────────────────────────────────
# Always fetch fresh — IMDS calls are fast (<5 ms on EC2) and caching caused
# stale-DTYPE bugs after instance type changes and stale INSTANCE_ID after AMI cloning.
TOKEN=$(curl -s --max-time 5 -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" 2>/dev/null)
AWSREGION=$(curl -s --max-time 5 -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null)
INSTANCE_ID=$(curl -s --max-time 5 -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null)

if [[ -z "$AWSREGION" ]] || [[ "${INSTANCE_ID}" == "" ]] || [[ "$(echo "${INSTANCE_ID}" | grep -E 'i-[a-f0-9]{8,17}')" == "" ]]; then
    echo "[ $(date) ] IMDS unreachable or returned bad values (INSTANCE_ID='${INSTANCE_ID}' AWSREGION='${AWSREGION}'), exiting"
    exit 1
fi

if nvidia-smi --list-gpus >/dev/null 2>&1; then
    DTYPE=$(nvidia-smi --list-gpus | wc -l)
else
    DTYPE="0"
fi

# POLICY is always fetched fresh so tag changes take effect on the next cron run
POLICY=$(aws ec2 describe-tags \
    --filters "Name=resource-id,Values=${INSTANCE_ID}" "Name=key,Values=GPUMON_POLICY" \
    --query "Tags[0].Value" --output text --region "${AWSREGION}" 2>/dev/null)
# Treat missing/None tag as STANDARD
if [ -z "${POLICY}" ] || [ "${POLICY}" = "None" ]; then
    POLICY="STANDARD"
fi

# Project tag — fetched fresh; used to select ASG-aware termination for SPOT
PROJECT=$(aws ec2 describe-tags \
    --filters "Name=resource-id,Values=${INSTANCE_ID}" "Name=key,Values=Project" \
    --query "Tags[0].Value" --output text --region "${AWSREGION}" 2>/dev/null)
if [ -z "${PROJECT}" ] || [ "${PROJECT}" = "None" ]; then
    PROJECT=""
fi

# ── Policy-derived durations ──────────────────────────────────────────────────
# SPOT/SEVERE have a 15-min idle window; their kill-halt backoff is 2x that.
# All other policies use a 2-hour idle window with a matching 2-hour backoff.
case "$POLICY" in
    SPOT|SEVERE)
        WINDOW_SECONDS=$((15 * 60))
        KILL_HALT_BACKOFF=$((30 * 60))
        MIN_UPTIME_SECS=0
        ;;
    STANDARD)
        WINDOW_SECONDS=$((60 * 60))
        KILL_HALT_BACKOFF=$((2 * 60 * 60))
        MIN_UPTIME_SECS=$((2 * 60 * 60))
        ;;
    *)
        WINDOW_SECONDS=$((2 * 60 * 60))
        KILL_HALT_BACKOFF=$((2 * 60 * 60))
        MIN_UPTIME_SECS=0
        ;;
esac
echo "[ $(date) ] Instance: ${INSTANCE_ID}  Policy: ${POLICY}  Project: ${PROJECT:-none}  Window: ${WINDOW_SECONDS}s  KillHaltBackoff: ${KILL_HALT_BACKOFF}s  MinUptime: ${MIN_UPTIME_SECS}s"

# ── kill-halt backoff check ───────────────────────────────────────────────────
TIMESTAMP_FILE="/tmp/timestamp.txt"
if [ ! -f "${TIMESTAMP_FILE}" ]; then
    echo "[ $(date) ] No kill-halt backoff active, continuing"
else
    current_time=$(date +%s)
    _timestamp=$(cat "${TIMESTAMP_FILE}")
    timestamp=$(date -d "${_timestamp}" +%s)
    time_diff=$((current_time - timestamp))

    if [ "$time_diff" -ge "$KILL_HALT_BACKOFF" ]; then
        echo "[ $(date) ] Kill-halt backoff expired (${time_diff}s >= ${KILL_HALT_BACKOFF}s) — resuming"
        rm -f "${TIMESTAMP_FILE}" 2>/dev/null
    else
        remaining=$((KILL_HALT_BACKOFF - time_diff))
        echo "[ $(date) ] Kill-halt active (${POLICY}: ${KILL_HALT_BACKOFF}s). ${remaining}s remaining. Will not halt now."
        exit 1
    fi
fi

# ── Boot-time uptime guard ────────────────────────────────────────────────────
# Guarantees a minimum on-time after boot regardless of idle state.
# Separate from kill_halt (user-triggered); this fires automatically.
# restart_backoff=0 in mon_utils.py lets the pilot track real idleness from
# boot so that idle time logged during boot counts toward the halt window.
if [ "${MIN_UPTIME_SECS}" -gt 0 ]; then
    uptime_secs=$(awk '{print int($1)}' /proc/uptime)
    if [ "${uptime_secs}" -lt "${MIN_UPTIME_SECS}" ]; then
        remaining=$((MIN_UPTIME_SECS - uptime_secs))
        echo "[ $(date) ] Boot guard active (${POLICY}: uptime=${uptime_secs}s < ${MIN_UPTIME_SECS}s). ${remaining}s remaining. Will not halt now."
        exit 1
    fi
fi

if [[ -z "$DTYPE" || "$DTYPE" == "0" ]]; then
    FILE="CPUMON_LOGS_"
else
    FILE="GPU_TEMP_"
fi

# ── AWS CLI health check ──────────────────────────────────────────────────────
echo "[ $(date) ] Checking AWS CLI..."
if ! timeout 30 aws sts get-caller-identity --region "${AWSREGION}" &>/dev/null; then
    echo "[ $(date) ] AWS CLI not working, attempting reinstall..."
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 update -q && \
        apt-get -o DPkg::Lock::Timeout=120 install -y awscli || \
        timeout 120 python3 -m pip install --break-system-packages --upgrade awscli boto3 botocore || \
        snap install aws-cli --classic 2>/dev/null || true
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
    export AWS_ACCESS_KEY_ID=$(echo "${secret_json}" | jq -r '.AccessKeyId // empty' 2>/dev/null)
    export AWS_SECRET_ACCESS_KEY=$(echo "${secret_json}" | jq -r '.SecretAccessKey // empty' 2>/dev/null)
    export AWS_SESSION_TOKEN=$(echo "${secret_json}" | jq -r '.SessionToken // empty' 2>/dev/null)
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

# Policy-specific wall message strings
case "$POLICY" in
    SPOT|SEVERE)
        _idle_str="15 minutes"
        _pause_str="30 minutes"
        ;;
    STANDARD)
        _idle_str="1 hour"
        _pause_str="2 hours"
        ;;
    *)
        _idle_str="2 hours"
        _pause_str="2 hours"
        ;;
esac
wall_message="
++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
This instance:${INSTANCE_ID} (${POLICY}) seems to have been idle for the last
${_idle_str}, will shut it down in 3 minutes from now. If you just logged in,
please wait a couple of minutes and restart it from the AWS console, or type:

    sudo bash /root/gpumon/kill_halt.sh

to stop the shutdown now. The shutdown pause will last ${_pause_str}.
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
        # Match Alarm_Pilot_value by name rather than by colon-delimited field
        # position — positional cut breaks when tag values (Team, Employee, etc.)
        # contain colons, which shifts every subsequent field index.
        valid_count=$(echo "${window_lines}" | grep -cE 'Alarm_Pilot_value:[01],' || true)
        if [ "${valid_count}" -eq 0 ]; then
            NOGO="TRUE"
            REASON="LOG_EXISTS_BUT_NO_VALID_DATA"
        else
            pilot_off=$(echo "${window_lines}" | grep -cE 'Alarm_Pilot_value:0,' || true)
            pilot_on=$(echo "${window_lines}"  | grep -cE 'Alarm_Pilot_value:1,' || true)
            if [ "${pilot_off}" -gt 0 ]; then
                NOGO="TRUE"
                REASON="ACTIVITY_SPIKE_IN_WINDOW:${pilot_off}_lines_pilot_off"
            elif [ "${pilot_on}" -gt 0 ]; then
                NOGO="FALSE"
                REASON="PILOT_ON_FOR_FULL_${WINDOW_SECONDS}s_WINDOW:${pilot_on}_lines"
            else
                NOGO="TRUE"
                REASON="ALARM_WASNT_ON_DURING_WINDOW,INCONCLUSIVE"
            fi
        fi
    fi
fi

# ── Shutdown decision ─────────────────────────────────────────────────────────
if [ "${NOGO}" == "TRUE" ]; then
    echo "[ $(date) ] NO-GO: ${REASON}"
else
    echo "[ $(date) ] GO — shutting down. Reason: ${REASON}" | tee -a /root/gpumon_persistent.log

    # Dry-run before broadcasting — prevents wall-message spam when creds lack permission.
    # FH-PIPELINE SPOT instances live in an ASG; use describe-auto-scaling-instances as a
    # permission probe (ASG terminate has no --dry-run flag). For all other cases use the
    # standard EC2 --dry-run: DryRunOperation = authorized, UnauthorizedOperation = denied.
    if [ "${POLICY}" == "SPOT" ] && [ "${PROJECT}" == "FH-PIPELINE" ]; then
        _dryrun=$(aws autoscaling describe-auto-scaling-instances \
            --instance-ids "${INSTANCE_ID}" --region "${AWSREGION}" 2>&1)
        if echo "${_dryrun}" | grep -qi "error\|AccessDenied\|AuthFailure"; then
            echo "[ $(date) ] ASG permission check failed — aborting broadcast: ${_dryrun}" | tee -a /root/gpumon_persistent.log
            exit 1
        fi
    elif [ "${POLICY}" == "SPOT" ]; then
        _dryrun=$(aws ec2 terminate-instances --instance-ids "${INSTANCE_ID}" --region "${AWSREGION}" --dry-run 2>&1)
        if echo "${_dryrun}" | grep -qi "UnauthorizedOperation"; then
            echo "[ $(date) ] AWS permission check failed — aborting broadcast: ${_dryrun}" | tee -a /root/gpumon_persistent.log
            exit 1
        fi
    else
        _dryrun=$(aws ec2 stop-instances --instance-ids "${INSTANCE_ID}" --region "${AWSREGION}" --dry-run 2>&1)
        if echo "${_dryrun}" | grep -qi "UnauthorizedOperation"; then
            echo "[ $(date) ] AWS permission check failed — aborting broadcast: ${_dryrun}" | tee -a /root/gpumon_persistent.log
            exit 1
        fi
    fi

    wall "[ $(date) ] ${wall_message}"
    sleep 180
    wall "[ $(date) ] 3 minutes passed — shutting down now. Bye!"
    if [ "${POLICY}" == "SPOT" ] && [ "${PROJECT}" == "FH-PIPELINE" ]; then
        res=$(aws autoscaling terminate-instance-in-auto-scaling-group \
            --instance-id "${INSTANCE_ID}" \
            --should-decrement-desired-capacity \
            --region "${AWSREGION}" 2>&1)
    elif [ "${POLICY}" == "SPOT" ]; then
        res=$(aws ec2 terminate-instances --instance-ids "${INSTANCE_ID}" --region "${AWSREGION}" 2>&1)
    else
        res=$(aws ec2 stop-instances --instance-ids "${INSTANCE_ID}" --region "${AWSREGION}" 2>&1)
    fi
    if echo "${res}" | grep -qi "error\|Exception"; then
        echo "[ $(date) ] AWS shutdown call failed: ${res}" | tee -a /root/gpumon_persistent.log
        exit 1
    fi
    echo "[ $(date) ] AWS result: ${res}"
fi
