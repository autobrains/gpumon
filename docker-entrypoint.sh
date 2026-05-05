#!/bin/bash
# If either monitor exits unexpectedly, fail the container so Docker restarts it.
set -euo pipefail

# Host metrics reporter runs alongside the GPU/CPU monitor
python3 /app/hostmon.py &
hostmon_pid=$!

# Detect GPUs and launch the appropriate monitor
if nvidia-smi --list-gpus >/dev/null 2>&1 && [ "$(nvidia-smi --list-gpus | wc -l)" -gt 0 ]; then
    echo "[entrypoint] GPU(s) detected — starting gpumon.py"
    python3 /app/gpumon.py &
else
    echo "[entrypoint] No GPUs detected — starting cpumon.py"
    python3 /app/cpumon.py &
fi
main_pid=$!

# Block until either child exits, then exit 1 so Docker restarts the container.
# Portable replacement for 'wait -n' (bash >= 4.3): poll with kill -0 every second.
while kill -0 "$hostmon_pid" 2>/dev/null && kill -0 "$main_pid" 2>/dev/null; do
    sleep 1
done
echo "[entrypoint] a monitor process exited — restarting container"
exit 1
