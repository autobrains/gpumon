#!/bin/bash
# this scriptlet looks for halt_it.sh processes and stops them and then adds a
# timestamp into /tmp/timestamp.txt so that halt_it.sh backs off for 2 hours.

TIMESTAMP_FILE="/tmp/timestamp.txt"
HALT_INFO_FILE="/tmp/halt_it.info"
S3_BUCKET="ab-gpumon-logs"
S3_PREFIX="v1"
S3_SPOOL_ROOT="/tmp/gpumon_s3"
INSTANCE_ID=""
AWSREGION=""
TEAM="NO_TAG"
EMPLOYEE="NO_TAG"
POLICY="STANDARD"
INSTANCE_NAME="NO_NAME_TAG"

json_escape() {
  python3 - <<'PY' "$1"
import json,sys
print(json.dumps(sys.argv[1]))
PY
}

sanitize_partition() {
  echo "$1" | sed 's/[^A-Za-z0-9._=-]/_/g'
}

load_context() {
  if [ -s "$HALT_INFO_FILE" ]; then
    INSTANCE_ID=$(grep '^INSTANCE_ID=' "$HALT_INFO_FILE" | cut -d '=' -f2)
    AWSREGION=$(grep '^AWSREGION=' "$HALT_INFO_FILE" | cut -d '=' -f2)
  fi
  if [ -n "$INSTANCE_ID" ] && [ -n "$AWSREGION" ]; then
    tags_json=$(aws ec2 describe-tags --region "$AWSREGION" --filters "Name=resource-id,Values=${INSTANCE_ID}" --output json 2>/dev/null || true)
    if [ -n "$tags_json" ]; then
      INSTANCE_NAME=$(python3 - <<'PY' "$tags_json"
import json,sys
try:
    tags={t['Key']:t['Value'] for t in json.loads(sys.argv[1]).get('Tags',[])}
    print(tags.get('Name','NO_NAME_TAG'))
except Exception:
    print('NO_NAME_TAG')
PY
)
      TEAM=$(python3 - <<'PY' "$tags_json"
import json,sys
try:
    tags={t['Key']:t['Value'] for t in json.loads(sys.argv[1]).get('Tags',[])}
    print(tags.get('Team','NO_TAG'))
except Exception:
    print('NO_TAG')
PY
)
      EMPLOYEE=$(python3 - <<'PY' "$tags_json"
import json,sys
try:
    tags={t['Key']:t['Value'] for t in json.loads(sys.argv[1]).get('Tags',[])}
    print(tags.get('Employee','NO_TAG'))
except Exception:
    print('NO_TAG')
PY
)
      POLICY=$(python3 - <<'PY' "$tags_json"
import json,sys
try:
    tags={t['Key']:t['Value'] for t in json.loads(sys.argv[1]).get('Tags',[])}
    print(tags.get('GPUMON_POLICY','STANDARD'))
except Exception:
    print('STANDARD')
PY
)
    fi
  fi
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
  printf '{"record_type":"event","ts":"%s","event_type":"%s","message":%s,"severity":"INFO","details":%s,"source":"kill_halt.sh","instance_id":"%s","instance_name":"%s","team":"%s","employee":"%s","policy":"%s"}\n' \
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

load_context

targetproc=($(pgrep -f "halt_it.sh" || true))
if [ "${#targetproc[@]}" -eq 0 ]; then
  echo "No related process seems to be running, exiting without doing anything"
  emit_s3_event "kill_halt_noop" "No halt_it.sh process was running"
  exit 1
else
  echo "Currently running processes related to putting server to sleep:${targetproc[@]}"
  for tp in "${targetproc[@]}"; do
    kill "$tp" 2>/dev/null || true
    sleep 1
    if kill -0 "$tp" 2>/dev/null; then
      kill -9 "$tp" 2>/dev/null || true
    fi
    echo "Killed running process:${tp}"
  done
  date +%s > "$TIMESTAMP_FILE"
  echo "$(cat ${TIMESTAMP_FILE}) - the server shutdown will be delayed for 2 hours"
  emit_s3_event "kill_halt_backoff_set" "halt_it.sh processes killed and 2h backoff timestamp written" "{\"timestamp\":$(cat "$TIMESTAMP_FILE"),\"killed_count\":${#targetproc[@]}}"
fi
