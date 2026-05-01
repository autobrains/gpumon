FROM nvidia/cuda:12.2.0-base-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
        python3 \
        python3-pip \
        curl \
        awscli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY gpumon.py cpumon.py hostmon.py mon_utils.py slack_dm.py docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD pgrep -f "gpumon.py\|cpumon.py" > /dev/null || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
