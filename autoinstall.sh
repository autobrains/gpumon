#!/bin/bash
# gpumon installer for AWS EC2 — (c) Paul Seifer, Autobrains LTD
# Idempotent: re-running is safe.  Remove /var/log/gpumon.finished to reinstall.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SENTINEL="/var/log/gpumon.finished"
HALT_HOST="/usr/local/sbin/halt_it.sh"
UPDATE_SCRIPT="/usr/local/sbin/gpumon-update.sh"
BOOT_SCRIPT="/usr/local/sbin/gpumon-boot.sh"

if [ -f "$SENTINEL" ]; then
    echo "[autoinstall] Already installed. Remove $SENTINEL to reinstall."
    exit 0
fi

echo "[autoinstall] Starting gpumon installation from $REPO_DIR"

# ── Stop automatic update services so they don't hold the apt lock ───────────
systemctl stop unattended-upgrades apt-daily.service apt-daily-upgrade.service 2>/dev/null || true
while fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend /var/cache/apt/archives/lock >/dev/null 2>&1; do
    echo "[autoinstall] waiting for apt lock..."
    sleep 5
done

# ── System packages ───────────────────────────────────────────────────────────
apt-get -o DPkg::Lock::Timeout=120 update -q
apt-get -o DPkg::Lock::Timeout=120 install -y git cron curl gnupg ca-certificates

# ── Docker ────────────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    echo "[autoinstall] Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
fi

if ! docker compose version &>/dev/null; then
    echo "[autoinstall] Installing docker compose plugin..."
    apt-get -o DPkg::Lock::Timeout=120 install -y docker-compose-plugin
fi

# Wait up to 30 s for Docker daemon
timeout 30 bash -c 'until docker info &>/dev/null 2>&1; do sleep 1; done' \
    || { echo "[autoinstall] Docker daemon did not start"; exit 1; }

# ── NVIDIA Container Toolkit (GPU instances only) ─────────────────────────────
HAS_GPU=false
if command -v nvidia-smi &>/dev/null \
    && nvidia-smi --list-gpus >/dev/null 2>&1 \
    && [ "$(nvidia-smi --list-gpus | wc -l)" -gt 0 ]; then
    HAS_GPU=true
fi

if $HAS_GPU && ! dpkg -l nvidia-container-toolkit &>/dev/null 2>&1; then
    echo "[autoinstall] GPU detected — installing NVIDIA Container Toolkit..."
    distribution=$(. /etc/os-release && echo "${ID}${VERSION_ID}")
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | gpg --batch --no-tty --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL "https://nvidia.github.io/libnvidia-container/${distribution}/libnvidia-container.list" \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        | tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get -o DPkg::Lock::Timeout=120 update -q
    apt-get -o DPkg::Lock::Timeout=120 install -y nvidia-container-toolkit
    nvidia-ctk runtime configure --runtime=docker
    systemctl restart docker
    timeout 30 bash -c 'until docker info &>/dev/null 2>&1; do sleep 1; done'
elif $HAS_GPU; then
    echo "[autoinstall] NVIDIA Container Toolkit already installed — skipping"
fi

# ── Auto-generate .env from Secrets Manager if not present ────────────────────
# Never overwrites an existing .env so manual configuration is preserved.
if [ -f "${REPO_DIR}/.env" ]; then
    echo "[autoinstall] .env already exists — skipping auto-generation"
else
    echo "[autoinstall] .env not found — generating from Secrets Manager..."

    # Secret IDs — callers may override via env vars before invoking autoinstall.sh
    _SLACK_SECRET_ID="${GPUMON_SLACK_SECRET_ID:-IT/SLACK_BOT_TOKEN}"
    _SLACK_SECRET_REGION="${GPUMON_SLACK_SECRET_REGION:-eu-west-1}"
    _INST_SECRET_ID="${GPUMON_SECRET_ID:-AB/InstanceRole}"
    _INST_SECRET_REGION="${GPUMON_SECRET_REGION:-eu-west-1}"

    {
        echo "GPUMON_SLACK_SECRET_ID=${_SLACK_SECRET_ID}"
        echo "GPUMON_SLACK_SECRET_REGION=${_SLACK_SECRET_REGION}"
        echo "GPUMON_SECRET_ID=${_INST_SECRET_ID}"
        echo "GPUMON_SECRET_REGION=${_INST_SECRET_REGION}"
    } > "${REPO_DIR}/.env"
    echo "[autoinstall] .env written"
fi

# ── Build and start container ─────────────────────────────────────────────────
cd "$REPO_DIR"
if $HAS_GPU; then
    docker compose build
    docker compose up -d
else
    docker compose -f docker-compose.yml -f docker-compose.cpu.yml build
    docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d
fi

echo "[autoinstall] Container started:"
docker ps --filter "name=gpumon" --format "  {{.Names}}  {{.Status}}"

# ── halt_it.sh on host crontab ────────────────────────────────────────────────
cp "${REPO_DIR}/halt_it.sh" "${HALT_HOST}"
chmod +x "${HALT_HOST}"

CRON_JOB="*/10 * * * * ${HALT_HOST} >> /var/log/halt_it.log 2>&1"
if ! crontab -l 2>/dev/null | grep -q "halt_it.sh"; then
    ( crontab -l 2>/dev/null; echo "$CRON_JOB" ) | crontab -
    echo "[autoinstall] halt_it.sh cron job installed"
else
    echo "[autoinstall] halt_it.sh already in crontab"
fi

# ── Auto-update script ────────────────────────────────────────────────────────
cat > "${UPDATE_SCRIPT}" << UPDATESCRIPT
#!/bin/bash
set -euo pipefail
REPO_DIR="${REPO_DIR}"

BEFORE=\$(git -C "\$REPO_DIR" rev-parse HEAD)
git -C "\$REPO_DIR" fetch origin --quiet
git -C "\$REPO_DIR" reset --hard @{upstream} --quiet
AFTER=\$(git -C "\$REPO_DIR" rev-parse HEAD)

if [ "\$BEFORE" = "\$AFTER" ]; then
    echo "[\$(date)] gpumon-update: no changes"
    exit 0
fi

echo "[\$(date)] gpumon-update: \$BEFORE -> \$AFTER — rebuilding container"
cp "\${REPO_DIR}/halt_it.sh" "${HALT_HOST}"
chmod +x "${HALT_HOST}"

cd "\$REPO_DIR"
if command -v nvidia-smi &>/dev/null \\
        && nvidia-smi --list-gpus >/dev/null 2>&1 \\
        && [ "\$(nvidia-smi --list-gpus | wc -l)" -gt 0 ]; then
    docker compose up -d --build
else
    docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d --build
fi
echo "[\$(date)] gpumon-update: done"
UPDATESCRIPT
chmod +x "${UPDATE_SCRIPT}"

# ── Boot reconciliation service ───────────────────────────────────────────────
# Runs once after Docker starts on every boot.  Detects GPU presence and calls
# docker compose up -d with the correct config so the container is always right
# regardless of instance type changes (GPU ↔ CPU).  No image rebuild — fast.
cat > "${BOOT_SCRIPT}" << BOOTSCRIPT
#!/bin/bash
set -euo pipefail
REPO_DIR="${REPO_DIR}"
cd "\$REPO_DIR"
if command -v nvidia-smi &>/dev/null \\
        && nvidia-smi --list-gpus >/dev/null 2>&1 \\
        && [ "\$(nvidia-smi --list-gpus | wc -l)" -gt 0 ]; then
    echo "[\$(date)] gpumon-boot: GPU detected"
    # Install NVIDIA Container Toolkit if missing (handles CPU→GPU instance type change).
    if ! dpkg -l nvidia-container-toolkit >/dev/null 2>&1; then
        echo "[\$(date)] gpumon-boot: NVIDIA Container Toolkit missing — installing..."
        distribution=\$(. /etc/os-release && echo "\${ID}\${VERSION_ID}")
        curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \\
            | gpg --batch --no-tty --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
        curl -fsSL "https://nvidia.github.io/libnvidia-container/\${distribution}/libnvidia-container.list" \\
            | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \\
            | tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
        apt-get -o DPkg::Lock::Timeout=120 update -q
        apt-get -o DPkg::Lock::Timeout=120 install -y nvidia-container-toolkit
        nvidia-ctk runtime configure --runtime=docker
        systemctl restart docker
        timeout 30 bash -c 'until docker info >/dev/null 2>&1; do sleep 1; done'
    fi
    echo "[\$(date)] gpumon-boot: ensuring GPU compose"
    docker compose up -d
else
    echo "[\$(date)] gpumon-boot: no GPU — ensuring CPU compose"
    docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d
fi
BOOTSCRIPT
chmod +x "${BOOT_SCRIPT}"

cat > /etc/systemd/system/gpumon-boot.service << 'EOF'
[Unit]
Description=gpumon boot-time container reconciliation
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/gpumon-boot.sh
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable gpumon-boot.service
echo "[autoinstall] Boot reconciliation service enabled"

# ── Systemd timer for hourly auto-updates ─────────────────────────────────────
cat > /etc/systemd/system/gpumon-update.service << 'EOF'
[Unit]
Description=gpumon auto-update
After=network-online.target gpumon-boot.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/gpumon-update.sh
StandardOutput=journal
StandardError=journal
EOF

cat > /etc/systemd/system/gpumon-update.timer << 'EOF'
[Unit]
Description=gpumon auto-update timer

[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now gpumon-update.timer

echo "[autoinstall] Auto-update timer enabled (fires 5 min post-boot, then every 1 h)"
echo "[autoinstall] Installation complete."
touch "$SENTINEL"
