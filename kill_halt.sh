#!/bin/bash
# Kill any running halt_it.sh process and set a 2-hour shutdown backoff.
# (c) Paul Seifer, Autobrains LTD

TIMESTAMP_FILE="/tmp/timestamp.txt"

pgrep -f "halt_it.sh"
targetproc=($(pgrep -f "halt_it.sh"))

if [ "${#targetproc[@]}" -eq 0 ]; then
    echo "No related process seems to be running, exiting without doing anything"
    exit 1
fi

echo "Currently running processes related to putting server to sleep: ${targetproc[*]}"
for tp in "${targetproc[@]}"; do
    kill "$tp"
    sleep 1
    if kill -0 "$tp" 2>/dev/null; then
        kill -9 "$tp"
    fi
    echo "Killed running process: ${tp}"
done

# Write human-readable date so halt_it.sh can parse it with `date -d`
date > "${TIMESTAMP_FILE}"
echo "$(cat "${TIMESTAMP_FILE}") — server shutdown delayed for 2 hours"
