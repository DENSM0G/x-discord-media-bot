#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="x-discord-bot"

echo "[1/5] Установка системных пакетов (Python, venv, ffmpeg, git)..."
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg git

echo "[2/5] Создание виртуального окружения Python..."
cd "$PROJECT_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "[3/5] Подготовка конфигурации и директорий..."
mkdir -p data config
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Создан файл .env из шаблона. Обязательно впишите туда DISCORD_TOKEN!"
fi

echo "[4/5] Регистрация systemd-сервиса ($SERVICE_NAME)..."
CURRENT_USER="$(whoami)"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=X (Twitter) to Discord Media Bot & Gallery
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${PROJECT_DIR}/.env
ExecStart=${PROJECT_DIR}/.venv/bin/python -m app.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "[5/5] Перезагрузка конфигурации systemd..."
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo "Установка завершена."
echo "1) Отредактируйте файл: nano ${PROJECT_DIR}/.env"
echo "2) Положите cookies от X в: ${PROJECT_DIR}/config/cookies.txt"
echo "3) Запустите бота: sudo systemctl start ${SERVICE_NAME}"