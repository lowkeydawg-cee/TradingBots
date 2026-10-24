# Running the bot on a server

A bot on a laptop stops when the laptop sleeps. This guide puts it on a small
always-on Linux server, with alerts to your phone. **Still paper trading:**
nothing here switches to real money.

Budget: a $5–10/month VPS (Hetzner, DigitalOcean, Vultr, Lightsail) with
1 GB RAM and Ubuntu 24.04. Pick a region near New York (US East) for lower
latency to Alpaca.

## 1. Prepare the server (once)

```bash
ssh root@YOUR_SERVER_IP
adduser --disabled-password --gecos "" bot
apt update && apt install -y git python3-venv docker.io docker-compose-v2
usermod -aG docker bot
ufw allow OpenSSH && ufw enable            # only SSH is open to the internet
```

## 2. Get the code and your settings

```bash
su - bot
git clone https://github.com/lowkeydawg-cee/TradingBots.git
cd TradingBots/pairs-bot
cp .env.example .env && nano .env          # paper keys, PAIRS, limits, Telegram
cp blackout.csv.example blackout.csv && nano blackout.csv   # this quarter's earnings dates
```

## 3. Phone alerts with Telegram (5 minutes, optional but recommended)

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the number after `"chat":{"id":`.
4. Put both in `.env`: `TELEGRAM_BOT_TOKEN=...` and `TELEGRAM_CHAT_ID=...`.

You'll get a message when the bot starts, on every trade (turn off with
`NOTIFY_TRADES=0`), on any halt, a daily report after the close, and the
nightly reconciliation result.

## 4a. Run with Docker (simplest)

```bash
docker compose up -d --build               # starts the bot and the dashboard
docker compose logs -f bot                 # watch it; Ctrl-C leaves it running
```

The database lives in the `botdata` volume, so upgrades keep your history.
Copy your blackout file into it once:

```bash
docker compose cp blackout.csv bot:/data/blackout.csv
```

Nightly reconciliation (add to `crontab -e` as the `bot` user):

```
30 22 * * 1-5 cd ~/TradingBots/pairs-bot && docker compose run --rm reconcile
```

Upgrade later with `git pull && docker compose up -d --build`.

## 4b. Or run with systemd (no Docker)

```bash
sudo mkdir -p /opt && sudo mv ~/TradingBots /opt/ && sudo chown -R bot /opt/TradingBots
cd /opt/TradingBots/pairs-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pairs-bot pairs-dashboard pairs-reconcile.timer
journalctl -u pairs-bot -f                 # watch the log
```

systemd restarts the bot if it crashes. A halt is different: it's saved in
the database, so a restarted bot stays halted until you decide.

## 5. Open the dashboard safely

The dashboard listens only on the server itself, never on the open internet
(it has an emergency-stop button, so it must not be public). From your own
computer, open an SSH tunnel:

```bash
ssh -L 8501:localhost:8501 bot@YOUR_SERVER_IP
```

then browse to http://localhost:8501. Close the terminal to close the tunnel.

## 6. Day-to-day

| Task | Docker | systemd |
|---|---|---|
| Is it running? | `docker compose ps` | `systemctl status pairs-bot` |
| Log | `docker compose logs -f bot` | `journalctl -u pairs-bot -f` |
| Stop / start | `docker compose stop bot` / `start bot` | `sudo systemctl stop pairs-bot` / `start` |
| Clear a halt after reviewing it | `docker compose run --rm bot python main.py --reset-halt --cycles 1` then `docker compose restart bot` | `sudo -u bot .venv/bin/python main.py --reset-halt --cycles 1` then `sudo systemctl restart pairs-bot` |
| Back up the database | `docker compose cp bot:/data/pairs_bot.db ./backup.db` | `cp pairs_bot.db backup.db` |
| Measure your real costs | `docker compose run --rm bot python backtest.py --source alpaca --start ... --end ... --costs-from-db /data/pairs_bot.db` | same without the docker part |

## Streaming prices

Set `DATA_SOURCE=stream` in `.env` once the polling version has run cleanly
for a few days. The bot then receives each 1-minute bar over a websocket
instead of downloading history every cycle, which matters with several pairs
(Alpaca's free plan allows 200 requests a minute). If the stream drops, the
bot's stale-data check pauses trading until bars arrive again.

To check the stream on its own during market hours: `python streaming.py`.
