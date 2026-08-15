#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/domainbot}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SERVICE_FILE="/etc/systemd/system/domainbot.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run with sudo: sudo ./scripts/install.sh"
  exit 1
fi

if [[ ! -f "pyproject.toml" ]]; then
  echo "Run this script from the domainbot repository root."
  exit 1
fi

apt-get update
apt-get install -y git rsync python3 python3-venv python3-pip

mkdir -p "${APP_DIR}"
rsync -a \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude ".env" \
  --exclude "domainbot.db" \
  --exclude "domainbot.db-*" \
  --exclude "logs" \
  ./ "${APP_DIR}/"

if [[ -f ".env" && ! -f "${APP_DIR}/.env" ]]; then
  install -m 0600 .env "${APP_DIR}/.env"
fi

cd "${APP_DIR}"

if [[ ! -f ".env" ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created ${APP_DIR}/.env. Edit it before live registration."
fi

"${PYTHON_BIN}" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .

mkdir -p logs
.venv/bin/domainbot init

install -m 0644 deploy/domainbot.service "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable --now domainbot

echo "Installed domainbot to ${APP_DIR}"
echo "Next:"
echo "  sudo nano ${APP_DIR}/.env"
echo "  sudo systemctl restart domainbot"
echo "  ${APP_DIR}/.venv/bin/domainbot test-api"
