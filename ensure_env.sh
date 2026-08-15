#!/bin/bash
# gpumon .env bootstrapper — idempotent, safe to run before every `docker compose`.
#
# docker-compose.yml and docker-compose.cpu.yml both declare `env_file: - .env`,
# which Compose treats as REQUIRED: a missing /root/gpumon/.env aborts
# `docker compose up` with "env file .../.env not found: ... no such file"
# (exit 1). Because .env is gitignored it is never cloned and lives outside the
# image, so a fresh clone, a new SPOT launch, or an AMI captured without it
# arrives with no .env — and any code path that runs `docker compose up` without
# first creating one fails. This is that guarantee, factored into one place.
#
# Idempotent by design: when a .env already exists it is LEFT UNTOUCHED (manual
# edits and custom secret IDs survive), so re-running — on every boot, update,
# and fix — is a no-op. Every value here is optional at the app level (all are
# commented out in .env.example, the app has matching in-code defaults); it is
# the file's EXISTENCE that Compose requires. The defaults written here are byte
# for byte what autoinstall.sh / gpumon-boot.sh have always written, so a box
# that lands here is configured exactly as a healthy one. Callers may override
# any value via environment variables before invoking (the same knobs
# autoinstall.sh documents).
set -euo pipefail

# Always invoked by absolute path (never via a symlink), so plain dirname/pwd is
# enough and stays portable — no GNU-only `readlink -f`.
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${REPO_DIR}/.env"

if [ -f "$ENV_FILE" ]; then
    echo "[ensure_env] ${ENV_FILE} already present — leaving it untouched"
    exit 0
fi

echo "[ensure_env] ${ENV_FILE} missing — writing defaults"
{
    echo "GPUMON_SLACK_SECRET_ID=${GPUMON_SLACK_SECRET_ID:-IT/SLACK_BOT_TOKEN}"
    echo "GPUMON_SLACK_SECRET_REGION=${GPUMON_SLACK_SECRET_REGION:-eu-west-1}"
    echo "GPUMON_SECRET_ID=${GPUMON_SECRET_ID:-AB/InstanceRole}"
    echo "GPUMON_SECRET_REGION=${GPUMON_SECRET_REGION:-eu-west-1}"
} > "$ENV_FILE"
echo "[ensure_env] wrote ${ENV_FILE}"
