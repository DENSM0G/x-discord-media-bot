#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="x-discord-bot"

cd "$PROJECT_DIR"
echo "Обновление кода из GitHub..."
git pull

echo "Обновление библиотек и экстракторов (gallery-dl, yt-dlp)..."
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install --upgrade gallery-dl yt-dlp

echo "Перезапуск сервиса $SERVICE_NAME..."
sudo systemctl restart "$SERVICE_NAME"
sudo systemctl status "$SERVICE_NAME" --no-pager