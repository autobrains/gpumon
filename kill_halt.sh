#!/bin/bash
#this scriptlet looks for halt_it.sh processes and stops them and then adds timestamp into /tmp/timestamp.txt so that halt_it script can back off for 2 hours even if instance is idle. (c)Paul Seifer
pgrep -f "halt_it.sh"
targetproc=($(pgrep -f "halt_it.sh"))
if [ "${#targetproc[@]}" -eq 0 ]; then
        echo "No related process seems to be running, exiting without doing anything"
        exit 1
else
        echo "Currently running processes related to putting server to sleep:${targetproc[@]}"
        for tp in "${targetproc[@]}"; do 
                kill $tp
                sleep 1
                if kill -0 $tp 2>/dev/null; then
                    kill -9 $tp
                fi
                echo "Killed running process:${tp}"
        done
        TIMESTAMP_FILE="/tmp/timestamp.txt"
        date +%s > "${TIMESTAMP_FILE}"
        echo "$(cat ${TIMESTAMP_FILE}) - the server shutdown will be delayed for 2 hours"
fi
