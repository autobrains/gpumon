#!/bin/bash
set -e

# Host metrics reporter runs alongside the GPU/CPU monitor
python3 /app/hostmon.py &

# Detect GPUs and launch the appropriate monitor
if nvidia-smi --list-gpus >/dev/null 2>&1 && [ "$(nvidia-smi --list-gpus | wc -l)" -gt 0 ]; then
    echo "[entrypoint] GPU(s) detected — starting gpumon.py"
    exec python3 /app/gpumon.py
else
    echo "[entrypoint] No GPUs detected — starting cpumon.py"
    exec python3 /app/cpumon.py
fi
