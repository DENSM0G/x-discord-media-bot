#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="x-discord-bot"

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
else
    SUDO="sudo"
fi

echo "[1/6] Installing system packages (Python, venv, ffmpeg, git, locales)..."
$SUDO apt-get update
$SUDO apt-get install -y python3 python3-venv python3-pip ffmpeg git locales

echo "[2/6] Configuring UTF-8 system locale (en_US.UTF-8 & ru_RU.UTF-8)..."
$SUDO sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
$SUDO sed -i 's/^# *ru_RU.UTF-8 UTF-8/ru_RU.UTF-8 UTF-8/' /etc/locale.gen
$SUDO locale-gen
$SUDO update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8

echo "[3/6] Creating Python virtual environment..."
cd "$PROJECT_DIR"
git config core.fileMode false || true
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "[4/6] Preparing directories and .env configuration..."
mkdir -p data config
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example. Make sure to set DISCORD_TOKEN and PUBLIC_BASE_URL!"
fi

echo "[5/6] Registering systemd service (${SERVICE_NAME})..."
CURRENT_USER="$(whoami)"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

$SUDO tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=X (Twitter) to Discord Media Bot & Gallery
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${PROJECT_DIR}
Environment=LANG=en_US.UTF-8
Environment=LC_ALL=en_US.UTF-8
Environment=PYTHONUTF8=1
EnvironmentFile=${PROJECT_DIR}/.env
ExecStart=${PROJECT_DIR}/.venv/bin/python -m app.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "[6/6] Reloading systemd daemon..."
$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE_NAME"

echo "Installation complete."
echo "1) Edit configuration: nano ${PROJECT_DIR}/.env"
echo "2) Put X cookies into: ${PROJECT_DIR}/config/cookies.txt"
echo "3) Start the service:  systemctl start ${SERVICE_NAME}"