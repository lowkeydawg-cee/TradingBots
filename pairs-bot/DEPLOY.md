# Running the bot on a server, and watching it from laptop, web and phone

**Still paper trading:** nothing here switches to real money.

## How the pieces fit

```
                 ┌──────────────── your $5/month server (always on) ────────────────┐
 Alpaca paper ◄──┤  bot (Docker)  ──writes──►  pairs_bot.db  ◄──reads──  dashboard   │
  (fake money,   │     │  ▲                                                  │        │
   real prices)  │     │  └── Telegram commands (/status /pause /stop)       │        │
                 └─────┼──────────────────────────────────────────────────────┼───────┘
                       │ alerts + replies                       private HTTPS │ (Tailscale)
                       ▼                                                      ▼
                 📱 Telegram app                          💻 laptop browser   📱 phone browser
                                                          https://<server>.ts.net
```

| Where you are | What you use | What you can do |
|---|---|---|
| **Phone** | Telegram | Get every trade, halt and the daily report. Send `/status`, `/trades`, `/pnl`, `/pause`, `/resume`, `/stop confirm` |
| **Phone** | Browser + Tailscale app | The full dashboard: charts, trades, emergency stop |
| **Laptop** | Browser + Tailscale app | The same dashboard |
| **Any browser** | app.alpaca.markets | Alpaca's own view of your paper account: orders, positions, balance |

**Why there is no Alpaca app in the App Store:** Alpaca is made for
programs like this bot, not for tapping buttons, so in most countries it has
no phone app. Its website works fine on a phone: open
https://app.alpaca.markets, log in, switch to **Paper**, and use "Add to Home
Screen" to get an app-like icon. The same works for our dashboard.

**Why Tailscale:** it's a free app that puts your server, laptop and phone on
a private network only you can enter. The dashboard is then reachable from
your devices and invisible to everyone else, with no ports open on the server.
The dashboard password (`DASHBOARD_PASSWORD`) is a second lock.

## What you need (once, about 30 minutes)

1. **Alpaca paper account**: sign up at https://alpaca.markets (free), open the
   **Paper** dashboard, generate an API key and secret.
2. **Telegram bot** (5 minutes): message **@BotFather**, send `/newbot`, copy the
   token. Send any message to your new bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the number after
   `"chat":{"id":`.
3. **Tailscale account**: install the Tailscale app on your phone and laptop and
   log in (free for personal use, https://tailscale.com/download).
4. **A server**: Hetzner (CX22), DigitalOcean or Vultr, Ubuntu 24.04, 1–2 GB RAM,
   a US East region (closer to the market). About $5–6 a month. Add your SSH key
   when creating it.

## Set it up (one command)

```bash
ssh root@YOUR_SERVER_IP
curl -fsSL https://raw.githubusercontent.com/lowkeydawg-cee/TradingBots/main/pairs-bot/deploy/setup.sh -o setup.sh
less setup.sh          # read it first; it's short
bash setup.sh
```

It asks for your Alpaca paper keys, the Telegram token and chat id, and a
dashboard password. It then:

1. creates a `bot` user, installs Docker, and closes every port except SSH;
2. downloads the code and writes `.env` (readable only by the `bot` user);
3. starts the bot and the dashboard (they restart on crashes and reboots);
4. installs Tailscale and prints a login link: log in with the **same account**
   as your phone and laptop. If it asks you to enable HTTPS certificates, click
   the link, enable them, and run `bash setup.sh` again;
5. schedules the nightly reconciliation (22:30 UTC, weekdays).

At the end it prints your dashboard address, like
`https://pairs-server.tail1234.ts.net`. Open it on your laptop and phone
(with Tailscale switched on), enter the password, and add it to your home screen.

Then send `/status` to your Telegram bot. It should answer with "Running" and your paper equity.

## Day-to-day

| Task | How |
|---|---|
| Is it running? | `/status` in Telegram, or `docker compose ps` on the server |
| Stop new trades for a while | `/pause` (open trades still exit normally), later `/resume` |
| Emergency | `/stop confirm` in Telegram, or the red button on the dashboard: flattens everything and halts |
| Clear a halt after reviewing it | on the server: `cd ~/TradingBots/pairs-bot && docker compose run --rm bot python main.py --reset-halt --cycles 1 && docker compose restart bot` |
| See the log | `docker compose logs -f bot` |
| Upgrade | `git pull && docker compose up -d --build` (or run `setup.sh` again) |
| Back up the database | `docker compose cp bot:/data/pairs_bot.db ./backup.db` |
| Earnings dates | edit `blackout.csv`, then `docker compose cp blackout.csv bot:/data/blackout.csv` |
| Measure your real costs | `docker compose run --rm bot python backtest.py --source alpaca --start ... --end ... --costs-from-db /data/pairs_bot.db` |

The bot only acts during US market hours (9:30–16:00 New York). Outside them,
`/status` still answers, and the daily report arrives shortly after the close.

## Without Docker (systemd)

```bash
sudo mkdir -p /opt && sudo mv ~/TradingBots /opt/ && sudo chown -R bot /opt/TradingBots
cd /opt/TradingBots/pairs-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pairs-bot pairs-dashboard pairs-reconcile.timer
sudo tailscale serve --bg 8501
journalctl -u pairs-bot -f
```

## Without Tailscale (SSH tunnel)

From your laptop: `ssh -L 8501:localhost:8501 bot@YOUR_SERVER_IP`, then open
http://localhost:8501. This works on a laptop but is awkward on a phone, which is why Tailscale is the default.

Never open port 8501 to the whole internet: the dashboard has an emergency-stop button.

## Streaming prices

Set `DATA_SOURCE=stream` in `.env` once the polling version has run cleanly
for a few days, then `docker compose up -d`. The bot then receives each
1-minute bar over a websocket instead of downloading history every cycle,
which matters with several pairs (Alpaca's free plan allows 200 requests a
minute). If the stream drops, the stale-data check pauses trading until bars
arrive again. To check the stream on its own during market hours: `python streaming.py`.
