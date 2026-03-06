#!/bin/bash
# This script parses gpumon/cpumon logs and if it finds only Alarm_Pilot_value:1
# for the last 2 hours, it shuts down the instance. Intended to run from cron.

set -u

TIMESTAMP_FILE="/tmp/timestamp.txt"
HALT_INFO_FILE="/tmp/halt_it.info"
S3_BUCKET="ab-gpumon-logs"
S3_PREFIX="v1"
S3_SPOOL_ROOT="/tmp/gpumon_s3"
AWSREGION=""
INSTANCE_ID=""
DTYPE="0"
TEAM="NO_TAG"
EMPLOYEE="NO_TAG"
POLICY="STANDARD"
INSTANCE_NAME="NO_NAME_TAG"

log() {
  echo "[ $(date) ] $*"
}

sanitize_partition() {
  echo "$1" | sed 's/[^A-Za-z0-9._=-]/_/g'
}

json_escape() {
  python3 - <<'PY' "$1"
import json,sys
print(json.dumps(sys.argv[1]))
PY
}

emit_s3_event() {
  local event_type="$1"
  local message="$2"
  local details="${3:-{}}"
  local ts day hour team_safe instance_safe dir file tmp_gz key escaped_msg

  [ -n "$INSTANCE_ID" ] || return 0
  [ -n "$AWSREGION" ] || return 0

  ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  day=$(date -u +"%Y-%m-%d")
  hour=$(date -u +"%H")
  team_safe=$(sanitize_partition "$TEAM")
  instance_safe=$(sanitize_partition "$INSTANCE_ID")
  dir="${S3_SPOOL_ROOT}/events/dt=${day}/hour=${hour}/team=${team_safe}/instance_id=${instance_safe}"
  mkdir -p "$dir"
  file="${dir}/events_$(date -u +"%Y-%m-%dT%H").jsonl"
  escaped_msg=$(json_escape "$message")

  printf '{"record_type":"event","ts":"%s","event_type":"%s","message":%s,"severity":"INFO","details":%s,"source":"halt_it.sh","instance_id":"%s","instance_name":"%s","team":"%s","employee":"%s","policy":"%s"}\n' \
    "$ts" "$event_type" "$escaped_msg" "$details" "$INSTANCE_ID" "$INSTANCE_NAME" "$TEAM" "$EMPLOYEE" "$POLICY" >> "$file"

  if [ -s "$file" ]; then
    tmp_gz="${file}.gz"
    gzip -c "$file" > "$tmp_gz" 2>/dev/null || return 0
    key="${S3_PREFIX}/events/dt=${day}/hour=${hour}/team=${team_safe}/instance_id=${instance_safe}/$(basename "$tmp_gz")"
    aws s3 cp "$tmp_gz" "s3://${S3_BUCKET}/${key}" --region "$AWSREGION" >/dev/null 2>&1 || true
    : > "$file"
    rm -f "$tmp_gz"
  fi
}

load_instance_context() {
  local token tags_json
  if [ ! -s "$HALT_INFO_FILE" ]; then
    token=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" 2>/dev/null)
    AWSREGION=$(curl -s -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/placement/region/ 2>/dev/null)
    INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/instance-id/ 2>/dev/null)
    nvidia-smi --list-gpus >/dev/null 2>&1
    if [ "$?" != "0" ]; then
      DTYPE="0"
    else
      DTYPE=$(nvidia-smi --list-gpus | wc -l | awk '{print $1}')
    fi
    {
      echo "INSTANCE_ID=${INSTANCE_ID}"
      echo "DTYPE=${DTYPE}"
      echo "AWSREGION=${AWSREGION}"
    } > "$HALT_INFO_FILE"
  else
    INSTANCE_ID=$(grep '^INSTANCE_ID=' "$HALT_INFO_FILE" | cut -d '=' -f2)
    DTYPE=$(grep '^DTYPE=' "$HALT_INFO_FILE" | cut -d '=' -f2)
    AWSREGION=$(grep '^AWSREGION=' "$HALT_INFO_FILE" | cut -d '=' -f2)
  fi

  if [[ -z "$INSTANCE_ID" ]] || ! echo "$INSTANCE_ID" | grep -Eq 'i-[a-f0-9]{8,17}'; then
    log "Need INSTANCE_ID, like i-02cb1c2e2dececfcd, exiting"
    exit 1
  fi

  tags_json=$(aws ec2 describe-tags --region "$AWSREGION" --filters "Name=resource-id,Values=${INSTANCE_ID}" --output json 2>/dev/null || true)
  if [ -n "$tags_json" ]; then
    INSTANCE_NAME=$(python3 - <<'PY' "$tags_json"
import json,sys
try:
    data=json.loads(sys.argv[1])
    tags={t['Key']:t['Value'] for t in data.get('Tags',[])}
    print(tags.get('Name','NO_NAME_TAG'))
except Exception:
    print('NO_NAME_TAG')
PY
)
    TEAM=$(python3 - <<'PY' "$tags_json"
import json,sys
try:
    data=json.loads(sys.argv[1])
    tags={t['Key']:t['Value'] for t in data.get('Tags',[])}
    print(tags.get('Team','NO_TAG'))
except Exception:
    print('NO_TAG')
PY
)
    EMPLOYEE=$(python3 - <<'PY' "$tags_json"
import json,sys
try:
    data=json.loads(sys.argv[1])
    tags={t['Key']:t['Value'] for t in data.get('Tags',[])}
    print(tags.get('Employee','NO_TAG'))
except Exception:
    print('NO_TAG')
PY
)
    POLICY=$(python3 - <<'PY' "$tags_json"
import json,sys
try:
    data=json.loads(sys.argv[1])
    tags={t['Key']:t['Value'] for t in data.get('Tags',[])}
    print(tags.get('GPUMON_POLICY','STANDARD'))
except Exception:
    print('STANDARD')
PY
)
  fi
}

respect_backoff() {
  if [ -f "$TIMESTAMP_FILE" ]; then
    current_time=$(date +%s)
    saved_timestamp=$(cat "$TIMESTAMP_FILE")
    time_diff=$((current_time - saved_timestamp))
    two_hours=$((2 * 60 * 60))
    if [ "$time_diff" -lt "$two_hours" ]; then
      remaining=$((two_hours - time_diff))
      log "Less than 2 hours have passed. ${remaining} seconds remaining. will not halt the server now"
      return 1
    fi
    log "More than 2 hours have passed since the timestamp."
    rm -f "$TIMESTAMP_FILE" 2>/dev/null
  else
    log "Stop file not found, will continue the script"
  fi
  return 0
}

ensure_awscli() {
  log "Is AWS cli installed?"
  if ! command -v aws >/dev/null 2>&1; then
    log "need to fix, will run: apt install awscli"
    DEBIAN_FRONTEND=noninteractive apt install -y awscli >/dev/null 2>&1 || true
    log "done fixin"
  else
    log "no problem with awscli...continue..."
  fi
}

maybe_load_secret_credentials() {
  local creds_tmp
  log "Getting creds from SM..."
  creds_tmp=$(aws secretsmanager get-secret-value --secret-id "AB/InstanceRole" --region eu-west-1 2>/dev/null | grep SecretString | rev | cut -c2- | rev | cut -d ':' -f2- | tr -d ' ' | tr -d '"')
  if [ -z "$creds_tmp" ]; then
    log "Warning, could not get creds from SM, will use whatever Role current user has"
  else
    export AWS_ACCESS_KEY_ID=$(echo "$creds_tmp" | cut -d ':' -f1)
    export AWS_SECRET_ACCESS_KEY=$(echo "$creds_tmp" | cut -d ':' -f2-)
  fi
}

select_log_source() {
  if [[ -z "$DTYPE" ]] || [[ "$DTYPE" == "0" ]]; then
    SEP=6
    FILE="CPUMON_LOGS_"
    STEP=500
  else
    SEP=12
    FILE="GPU_TEMP_"
    if [ "$DTYPE" -lt "4" ]; then
      STEP=500
    else
      STEP=2000
    fi
  fi
}

main() {
  respect_backoff || exit 1
  load_instance_context
  ensure_awscli
  maybe_load_secret_credentials
  select_log_source

  emit_s3_event "halt_it_started" "halt_it.sh started" "{\"file_prefix\":\"${FILE}\",\"step\":${STEP},\"dtype\":\"${DTYPE}\"}"

  latest_file=$(ls -1t /tmp/${FILE}* 2>/dev/null | head -n 1)
  if [ -z "$latest_file" ]; then
    log "No ${FILE} logs found, exiting"
    emit_s3_event "halt_it_no_logs" "No monitor logs found"
    exit 1
  fi

  sample=$(tail -n "$STEP" "$latest_file" 2>/dev/null || true)
  if [ -z "$sample" ]; then
    log "Could not read recent log sample, exiting"
    emit_s3_event "halt_it_empty_sample" "Recent log sample is empty" "{\"log_file\":$(json_escape "$latest_file")}"
    exit 1
  fi

  total_lines=$(printf '%s\n' "$sample" | sed '/^$/d' | wc -l | awk '{print $1}')
  alarm_one_lines=$(printf '%s\n' "$sample" | grep -c 'Alarm_Pilot_value:1' || true)
  alarm_zero_lines=$(printf '%s\n' "$sample" | grep -c 'Alarm_Pilot_value:0' || true)

  log "Recent sample lines=${total_lines}, alarm_one_lines=${alarm_one_lines}, alarm_zero_lines=${alarm_zero_lines}, source=${latest_file}"

  if [ "$total_lines" -gt 0 ] && [ "$alarm_one_lines" -eq "$total_lines" ]; then
    REASON="WE_GOT_PILOT_ONLY_FOR_2_HOURS_ITS_A_GO"
    wall_message="++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++ This instance:${INSTANCE_ID} seems to have been idle for the last 2 hours, will shut it down in 3 minutes from now. If you want to stop this, please execute: bash /root/gpumon/kill_halt.sh ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++"
    log "LOOKS LIKE WE ARE A GO - WILL SHUT THE INSTANCE DOWN"
    emit_s3_event "halt_decision_go" "Instance is idle for shutdown" "{\"reason\":$(json_escape "$REASON"),\"log_file\":$(json_escape "$latest_file"),\"sample_lines\":${total_lines}}"
    wall "$wall_message" >/dev/null 2>&1 || true
    sleep 180
    emit_s3_event "stop_instance_called" "Calling stop-instances" "{\"instance_id\":$(json_escape "$INSTANCE_ID"),\"aws_region\":$(json_escape "$AWSREGION") }"
    aws ec2 stop-instances --instance-ids "$INSTANCE_ID" --region "$AWSREGION"
    rc=$?
    if [ "$rc" -eq 0 ]; then
      log "Stop command completed successfully"
      emit_s3_event "stop_instance_success" "stop-instances completed successfully"
    else
      log "Stop command failed with rc=${rc}"
      emit_s3_event "stop_instance_failed" "stop-instances failed" "{\"rc\":${rc}}"
      exit "$rc"
    fi
  else
    REASON="NOT_IDLE_FOR_FULL_WINDOW"
    log "NOGO - instance is not idle for full decision window"
    emit_s3_event "halt_decision_no_go" "Instance is not idle for full decision window" "{\"reason\":$(json_escape "$REASON"),\"log_file\":$(json_escape "$latest_file"),\"sample_lines\":${total_lines},\"alarm_one_lines\":${alarm_one_lines},\"alarm_zero_lines\":${alarm_zero_lines}}"
    exit 1
  fi
}

main "$@"
