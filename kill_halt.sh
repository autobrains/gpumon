#!/bin/bash
# Write a policy-aware shutdown backoff and kill any running halt_it.sh process.
# Safe to run proactively — does not require halt_it.sh to be running.
# (c) Paul Seifer, Autobrains LTD

# Fetch POLICY so the backoff duration matches the idle window for this instance.
TOKEN=$(curl -s --max-time 5 -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" 2>/dev/null)
AWSREGION=$(curl -s --max-time 5 -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null)
INSTANCE_ID=$(curl -s --max-time 5 -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null)
POLICY=$(aws ec2 describe-tags \
    --filters "Name=resource-id,Values=${INSTANCE_ID}" "Name=key,Values=GPUMON_POLICY" \
    --query "Tags[0].Value" --output text --region "${AWSREGION}" 2>/dev/null)
if [ -z "${POLICY}" ] || [ "${POLICY}" = "None" ]; then
    POLICY="STANDARD"
fi

# SPOT/SEVERE: 30-min backoff (2× the 15-min idle window)
# All others:  2-hour backoff (matches the 2-hour idle window)
case "$POLICY" in
    SPOT|SEVERE) BACKOFF_SECS=$((30 * 60)) ;;
    *)           BACKOFF_SECS=$((2 * 60 * 60)) ;;
esac

TIMESTAMP_FILE="/tmp/timestamp.txt"

# Always set the backoff timestamp first so shutdown is delayed regardless
# of whether halt_it.sh is currently executing.
date > "${TIMESTAMP_FILE}"
echo "[ $(date) ] Shutdown backoff set — policy=${POLICY} duration=${BACKOFF_SECS}s — halt_it.sh suppressed."

targetproc=($(pgrep -f "halt_it.sh" 2>/dev/null))

if [ "${#targetproc[@]}" -eq 0 ]; then
    echo "No halt_it.sh process currently running."
    exit 0
fi

echo "Killing running halt_it.sh processes: ${targetproc[*]}"
for tp in "${targetproc[@]}"; do
    kill "$tp" 2>/dev/null && sleep 1 || true
    if kill -0 "$tp" 2>/dev/null; then
        kill -9 "$tp" 2>/dev/null || true
    fi
    echo "Killed process: ${tp}"
done
