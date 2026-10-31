# Running the bot on a server, and watching it from laptop, web and phone

**Still paper trading:** nothing here switches to real money.

## How the pieces fit

```
                 ┌──────────────── your $5/month server (always on) ────────────────┐
 Alpaca paper ◄──┤  bot (Docker)  ──writes──►  pairs_bot.db  ◄──reads──  dashboard   │
  (fake money,   │  watcher (Docker) ──writes──┘      ▲                       │        │
   real prices)  │     │                              └── Jarvis (MCP)        │        │
                 └─────┼──────────────────────────────────────────────────────┼───────┘
                       │ alerts                                 private HTTPS │ (Tailscale)
                       ▼                                                      ▼
                 📱 ntfy app (or Telegram)                💻 laptop browser   📱 phone browser
                                                          https://<server>.ts.net
```

| Where you are | What you use | What you can do |
|---|---|---|
| **Phone** | ntfy app (free, no account) | Get every trade, halt, new STRONG signal and the daily report as push notifications. Halts ring as urgent |
| **Phone** | Browser + Tailscale app | The full dashboard: charts, trades, market watch, pause and emergency stop |
| **Laptop or phone** | Your Jarvis assistant | Ask by voice or in its web page: "how's the trading bot?", "what's strong right now?", "pause trading". See [Jarvis](#jarvis) |
| **Phone (optional)** | Telegram | Same alerts, plus commands: `/status`, `/trades`, `/pnl`, `/watch`, `/pause`, `/resume`, `/stop confirm` |
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
2. **ntfy app** (2 minutes, no account): install **ntfy** from the App Store or
   Play Store, tap **+**, and subscribe to a topic name only you know, like
   `pairs-saint-7f3k9q2x`. Anyone who guesses the name can read your alerts, so
   make it long and random. (Telegram also works if you ever want it: message
   **@BotFather**, `/newbot`, and copy the token and your chat id from
   `https://api.telegram.org/bot<TOKEN>/getUpdates`. It's optional.)
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

It asks for your Alpaca paper keys, your ntfy topic, a dashboard password,
and (optional, press Enter to skip) a Telegram token and chat id. It then:

1. creates a `bot` user, installs Docker, and closes every port except SSH;
2. downloads the code and writes `.env` (readable only by the `bot` user);
3. starts the bot, the market watcher and the dashboard (they restart on crashes and reboots);
4. installs Tailscale and prints a login link: log in with the **same account**
   as your phone and laptop. If it asks you to enable HTTPS certificates, click
   the link, enable them, and run `bash setup.sh` again;
5. schedules the nightly reconciliation (22:30 UTC, weekdays).

At the end it prints your dashboard address, like
`https://pairs-server.tail1234.ts.net`. Open it on your laptop and phone
(with Tailscale switched on), enter the password, and add it to your home screen.

Within a minute your phone should get an ntfy notification that the bot started.

## Day-to-day

| Task | How |
|---|---|
| Is it running? | the dashboard, "Jarvis, how's the trading bot?", or `docker compose ps` on the server |
| Stop new trades for a while | the dashboard's pause, "Jarvis, pause trading" (or `/pause` in Telegram); open trades still exit normally |
| Emergency | the red button on the dashboard, "Jarvis, stop everything" (it asks you to confirm), or `/stop confirm` in Telegram: flattens everything and halts |
| Clear a halt after reviewing it | on the server: `cd ~/TradingBots/pairs-bot && docker compose run --rm bot python main.py --reset-halt --cycles 1 && docker compose restart bot` |
| See the log | `docker compose logs -f bot` |
| Upgrade | `git pull && docker compose up -d --build` (or run `setup.sh` again) |
| Back up the database | `docker compose cp bot:/data/pairs_bot.db ./backup.db` |
| Earnings dates | edit `blackout.csv`, then `docker compose cp blackout.csv bot:/data/blackout.csv` |
| Measure your real costs | `docker compose run --rm bot python backtest.py --source alpaca --start ... --end ... --costs-from-db /data/pairs_bot.db` |

The bot only acts during US market hours (9:30–16:00 New York). Outside them,
the dashboard and Jarvis still answer, and the daily report arrives shortly after the close.

## Market watch

The `watcher` service scans every pair in every sector all day on 15-minute bars
and grades each one:

| Grade | Means | Demo trade? |
|---|---|---|
| STRONG | a signal, the pair is cointegrated, and this rule set has won often on it recently after costs | yes |
| RISKY | a signal with a warning: weak cointegration, few past trades, a volatility spike, earnings, a recent losing streak | yes |
| AVOID | a signal on a pair that looks broken: not cointegrated, past the stop line, or doesn't revert | yes, as the control group |
| WATCH | healthy pair, no signal yet | no |
| QUIET | nothing to do | no |

The "win rate" is how the bot's rules did on that pair over the last few weeks,
shrunk toward 50% when there are few trades. It is a measurement, not a
prediction. Every STRONG, RISKY and AVOID signal is traded with demo money
(no orders are sent anywhere), and the dashboard's **Market watch** section
shows the results by grade, by pair and by hour of entry. After a few weeks
that answers the real questions: do STRONG signals beat RISKY ones, does
AVOID really lose, and are some hours better than others? Until there are
dozens of demo trades per grade, treat the differences as noise.

**How it sees the market.** Every scan the watcher also writes a market brief:
the mood (are moves calm, normal or stressed compared with recent weeks), how
many pairs still move together, each sector's move today, and what changed
(pairs that broke or recovered, new STRONG signals). Your phone gets the full
brief three times a day (9:45, 12:30 and 16:00 New York) and an alert the moment
something material changes: the market turns stressed or calms down, a sector
gets stressed, most pairs decouple, a pair the bot trades (or a STRONG one)
breaks, or stocks move 1.5% or more on the day. The latest brief and the day's
alerts sit at the top of the dashboard's Market watch section, and Jarvis reads
them when you ask "how does the market look?". The brief describes what prices
are doing now; it is not a forecast.

Try it on your laptop with simulated prices: `python watcher.py --source sim --fast --scans 300`,
then `streamlit run app.py`.

## Jarvis

`mcp_server.py` lets your Jarvis assistant (or any assistant that speaks MCP,
including Claude Desktop) read and control the bot. Add one of these to
Jarvis's `config.yaml` under `mcp.servers`.

**Bot on the server, Jarvis on your PC** (the usual setup). Jarvis starts the
tool over SSH inside the bot's container; Tailscale makes the address reachable
only from your devices. Set up an SSH key from your PC to the `bot` user first.

```yaml
mcp:
  servers:
    trading:             # questions: answered straight away
      command: ssh
      args: [bot@<server>.ts.net, "cd TradingBots/pairs-bot && docker compose exec -T bot python mcp_server.py"]
      confirm: false
      allow_tools: [get_bot_status, get_market_brief, get_market_watch, explain, get_recent_trades, get_profit, get_demo_results]
    trading_control:     # actions: Jarvis asks you first
      command: ssh
      args: [bot@<server>.ts.net, "cd TradingBots/pairs-bot && docker compose exec -T bot python mcp_server.py"]
      confirm: true
      allow_tools: [pause, resume, stop_everything]
```

Jarvis's `confirm` setting covers a whole server, so the same tool is listed
twice: once for questions without a confirmation, once for actions with one.

**Bot and Jarvis on the same computer:**

```yaml
mcp:
  servers:
    trading:
      command: C:/Users/<you>/TradingBots/pairs-bot/.venv/Scripts/python.exe
      args: [C:/Users/<you>/TradingBots/pairs-bot/mcp_server.py]
      env: {PAIRS_DB_PATH: C:/Users/<you>/TradingBots/pairs-bot/pairs_bot.db}
      confirm: false
      allow_tools: [get_bot_status, get_market_brief, get_market_watch, explain, get_recent_trades, get_profit, get_demo_results]
    trading_control:
      command: C:/Users/<you>/TradingBots/pairs-bot/.venv/Scripts/python.exe
      args: [C:/Users/<you>/TradingBots/pairs-bot/mcp_server.py]
      env: {PAIRS_DB_PATH: C:/Users/<you>/TradingBots/pairs-bot/pairs_bot.db}
      confirm: true
      allow_tools: [pause, resume, stop_everything]
```

This works with Jarvis's default Groq brain (`llm.provider: hybrid`, `GROQ_API_KEY`):
Groq's `llama-3.3-70b-versatile` supports tool calling, and Jarvis's own MCP
client was checked against this server (it lists all 9 tools and calls them).

Jarvis then has these tools: `get_bot_status`, `get_market_brief` ("Jarvis, how does the market look?"), `get_market_watch`, `explain`
(why a pair is graded the way it is), `get_recent_trades`, `get_profit`,
`get_demo_results`, `pause`, `resume` and `stop_everything` (needs
`confirm=true`). Reading is all it does unless you ask it to pause or stop.

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
