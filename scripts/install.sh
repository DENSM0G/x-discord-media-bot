#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="x-discord-bot"
BOT_USER="xbot"

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
else
    SUDO="sudo"
fi

echo "[1/7] Installing system packages (Python, venv, ffmpeg, git, locales)..."
$SUDO apt-get update
$SUDO apt-get install -y python3 python3-venv python3-pip ffmpeg git locales

echo "[2/7] Configuring UTF-8 system locale (en_US.UTF-8 & ru_RU.UTF-8)..."
$SUDO sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
$SUDO sed -i 's/^# *ru_RU.UTF-8 UTF-8/ru_RU.UTF-8 UTF-8/' /etc/locale.gen
$SUDO locale-gen
$SUDO update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8

echo "[3/7] Creating unprivileged system user (${BOT_USER})..."
if ! id -u "$BOT_USER" >/dev/null 2>&1; then
    $SUDO useradd --system --no-create-home --shell /usr/sbin/nologin "$BOT_USER"
fi

echo "[4/7] Creating Python virtual environment..."
cd "$PROJECT_DIR"
git config core.fileMode false || true
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "[5/7] Preparing directories, .env, and hardened permissions..."
mkdir -p data config
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example. Make sure to set DISCORD_TOKEN and PUBLIC_BASE_URL!"
fi

# Resolve DATA_DIR and TEMP_DIR from .env (with safe defaults)
read_env_var() {
    local key="$1"
    local default_val="$2"
    local raw
    raw="$(grep -E "^[[:space:]]*${key}=" "$PROJECT_DIR/.env" 2>/dev/null | tail -n 1 | cut -d '=' -f 2- | tr -d '\r' | sed "s/^[[:space:]]*//;s/[[:space:]]*$//;s/^['\"]//;s/['\"]$//" || true)"
    if [ -n "$raw" ]; then
        echo "$raw"
    else
        echo "$default_val"
    fi
}

DATA_DIR_RAW="$(read_env_var "DATA_DIR" "./data")"
TEMP_DIR_RAW="$(read_env_var "TEMP_DIR" "/tmp/xbot_compress")"

DATA_DIR_ABS="$(python3 -c 'import sys; from pathlib import Path; base, raw = Path(sys.argv[1]), Path(sys.argv[2]); print((raw if raw.is_absolute() else (base / raw)).resolve())' "$PROJECT_DIR" "$DATA_DIR_RAW")"
TEMP_DIR_ABS="$(python3 -c 'import sys; from pathlib import Path; base, raw = Path(sys.argv[1]), Path(sys.argv[2]); print((raw if raw.is_absolute() else (base / raw)).resolve())' "$PROJECT_DIR" "$TEMP_DIR_RAW")"

$SUDO mkdir -p "$DATA_DIR_ABS"

# Code and virtualenv owned by root (read-only for xbot)
$SUDO chown -R root:root "$PROJECT_DIR"
# .env readable ONLY by root (systemd loads EnvironmentFile before dropping privileges to xbot)
$SUDO chmod 600 "$PROJECT_DIR/.env"

# config/ readable by xbot via group (SGID ensures new cookies.txt inherits group xbot)
$SUDO chown root:"$BOT_USER" "$PROJECT_DIR/config"
$SUDO chmod 2750 "$PROJECT_DIR/config"
if [ -f "$PROJECT_DIR/config/cookies.txt" ]; then
    $SUDO chown root:"$BOT_USER" "$PROJECT_DIR/config/cookies.txt"
    $SUDO chmod 640 "$PROJECT_DIR/config/cookies.txt"
fi

# Data directory writable exclusively by xbot
$SUDO chown -R "${BOT_USER}:${BOT_USER}" "$DATA_DIR_ABS"
$SUDO chmod 750 "$DATA_DIR_ABS"

# Allow extra ReadWritePaths only if TEMP_DIR is configured outside /tmp (PrivateTmp handles /tmp)
READ_WRITE_PATHS="$DATA_DIR_ABS"
case "$TEMP_DIR_ABS" in
    /tmp|/tmp/*) ;;
    *)
        $SUDO mkdir -p "$TEMP_DIR_ABS"
        $SUDO chown -R "${BOT_USER}:${BOT_USER}" "$TEMP_DIR_ABS"
        $SUDO chmod 750 "$TEMP_DIR_ABS"
        READ_WRITE_PATHS="${READ_WRITE_PATHS} ${TEMP_DIR_ABS}"
        ;;
esac

case "$PROJECT_DIR" in
    /home/*|/root/*) PROTECT_HOME="read-only" ;;
    *) PROTECT_HOME="true" ;;
esac

echo "[6/7] Registering hardened systemd service (${SERVICE_NAME})..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

$SUDO tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=X (Twitter) to Discord Media Bot & Gallery
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${BOT_USER}
Group=${BOT_USER}
WorkingDirectory=${PROJECT_DIR}
Environment=LANG=en_US.UTF-8
Environment=LC_ALL=en_US.UTF-8
Environment=PYTHONUTF8=1
EnvironmentFile=${PROJECT_DIR}/.env
ExecStart=${PROJECT_DIR}/.venv/bin/python -m app.main
Restart=always
RestartSec=5

# Sandboxing & privilege isolation
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=${PROTECT_HOME}
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
ReadWritePaths=${READ_WRITE_PATHS}

[Install]
WantedBy=multi-user.target
EOF

echo "[7/7] Reloading systemd daemon..."
$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE_NAME"

echo "Installation complete (running as isolated user: ${BOT_USER})."
echo "1) Edit configuration: nano ${PROJECT_DIR}/.env"
echo "2) Optional X cookies: ${PROJECT_DIR}/config/cookies.txt (chmod 640, group ${BOT_USER})"
echo "3) Start / restart:    systemctl restart ${SERVICE_NAME}"