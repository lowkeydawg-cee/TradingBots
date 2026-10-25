#!/usr/bin/env bash
# One-command server setup for the pairs bot (paper trading only).
#
# On a fresh Ubuntu 22.04/24.04 server, as root:
#   curl -fsSL https://raw.githubusercontent.com/lowkeydawg-cee/TradingBots/main/pairs-bot/deploy/setup.sh -o setup.sh
#   less setup.sh        # read it first: it's short
#   bash setup.sh
#
# What it does:
#   1. creates a 'bot' user, installs Docker, turns on the firewall (SSH only)
#   2. downloads the code and asks for your Alpaca PAPER keys, Telegram and a dashboard password
#   3. starts the bot + dashboard with Docker (restarts on crash and on reboot)
#   4. installs Tailscale so the dashboard opens on your laptop and phone, privately
#   5. schedules the nightly reconciliation
# Safe to run again: it keeps an existing .env.
set -euo pipefail

REPO=https://github.com/lowkeydawg-cee/TradingBots.git
HOME_DIR=/home/bot
APP=$HOME_DIR/TradingBots/pairs-bot

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo bash setup.sh)"; exit 1; }

echo "==> 1/5 System: user, Docker, firewall"
id bot >/dev/null 2>&1 || adduser --disabled-password --gecos "" bot
apt-get update -qq
apt-get install -y -qq git curl ca-certificates docker.io docker-compose-v2 ufw >/dev/null
usermod -aG docker bot
systemctl enable --now docker >/dev/null
ufw allow OpenSSH >/dev/null && ufw --force enable >/dev/null   # nothing else is open to the internet

echo "==> 2/5 Code and settings"
if [ -d "$HOME_DIR/TradingBots/.git" ]; then
  sudo -u bot git -C "$HOME_DIR/TradingBots" pull --ff-only
else
  sudo -u bot git clone -q "$REPO" "$HOME_DIR/TradingBots"
fi
cd "$APP"
if [ ! -f .env ]; then
  cp .env.example .env
  read -rp  "Alpaca PAPER API key (starts with PK): " KEY
  read -rsp "Alpaca PAPER secret key: " SECRET; echo
  read -rp  "Telegram bot token (Enter to skip): " TG_TOKEN
  read -rp  "Telegram chat id (Enter to skip): " TG_CHAT
  read -rsp "Dashboard password: " DASH_PW; echo
  set_env() { if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi; }
  set_env BOT_MODE alpaca
  set_env ALPACA_API_KEY "$KEY"
  set_env ALPACA_SECRET_KEY "$SECRET"
  set_env TELEGRAM_BOT_TOKEN "$TG_TOKEN"
  set_env TELEGRAM_CHAT_ID "$TG_CHAT"
  set_env DASHBOARD_PASSWORD "$DASH_PW"
  chown bot .env && chmod 600 .env       # only the bot user can read your keys
else
  echo "    keeping existing .env"
fi

echo "==> 3/5 Start the bot and dashboard"
sudo -u bot docker compose up -d --build
[ -f blackout.csv ] && sudo -u bot docker compose cp blackout.csv bot:/data/blackout.csv || true

echo "==> 4/5 Private access from your laptop and phone (Tailscale)"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
tailscale status >/dev/null 2>&1 || {
  echo "    Open the link below and log in with the same account you'll use on your phone:"
  tailscale up
}
# Serve the dashboard over HTTPS, only to devices on your tailnet. If this asks you to
# enable HTTPS certificates, open the link it prints, click enable, and run this script again.
tailscale serve --bg 8501 || echo "    (tailscale serve needs HTTPS enabled in your tailnet; see DEPLOY.md)"

echo "==> 5/5 Nightly reconciliation (22:30 UTC, weekdays)"
CRON="30 22 * * 1-5 cd $APP && docker compose run --rm reconcile >> $HOME_DIR/reconcile.log 2>&1"
( crontab -u bot -l 2>/dev/null | grep -v 'compose run --rm reconcile' ; echo "$CRON" ) | crontab -u bot -

NAME=$(tailscale status --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null || true)
echo
echo "Done. The bot is running on your Alpaca PAPER account."
echo "  Dashboard (laptop + phone, after installing Tailscale there): https://${NAME:-<this-server>.ts.net}"
echo "  Logs:    cd $APP && docker compose logs -f bot"
echo "  Phone:   send /status to your Telegram bot"
