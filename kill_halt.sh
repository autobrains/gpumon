#!/bin/bash
# Write a 2-hour shutdown backoff and kill any running halt_it.sh process.
# Safe to run proactively — does not require halt_it.sh to be running.
# (c) Paul Seifer, Autobrains LTD

TIMESTAMP_FILE="/tmp/timestamp.txt"

# Always set the backoff timestamp first so shutdown is delayed regardless
# of whether halt_it.sh is currently executing.
date > "${TIMESTAMP_FILE}"
echo "[ $(date) ] Shutdown backoff set — halt_it.sh suppressed for 2 hours."

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
