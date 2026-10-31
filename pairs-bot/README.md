# Pairs trading bot: Alpaca paper + Streamlit

An asynchronous statistical-arbitrage bot for one or several stock pairs. It runs on the
**Alpaca paper** account through `alpaca-py`, logs every event to SQLite, and
includes a live Streamlit dashboard with an emergency stop.

New here? Start with [LEARN.md](LEARN.md), then test it with [TESTING.md](TESTING.md), which has a plain-words walkthrough and a learning path. [GUIDE.md](GUIDE.md) has the rating and the roadmap.

![Dashboard](docs/dashboard.png)
*Dashboard rendered against the offline simulator after 800 cycles. These are simulated numbers, not real results.*

## Project layout

```
pairs-bot/
├── config.py        # env-driven frozen Settings: keys, pairs, z thresholds, risk limits
├── db_logger.py     # aiosqlite writer (WAL, busy_timeout, retry, JSONL fallback, migrations) + dashboard helpers
├── strategy.py      # Alpaca bar fetch, OLS or Kalman hedge ratio, z-score, Engle-Granger, vol filter, decide()
├── execution.py     # AlpacaBroker (paper=True), sizing, limit-order legging, leg-failure unwind
├── main.py          # portfolio loop: account limits -> one PairEngine per pair -> log; persisted halt
├── app.py           # Streamlit: equity, z-score per pair, performance by pair, slippage, emergency stop
├── sim.py           # offline cointegrated markets + simulated broker with spread and missed fills
├── backtest.py      # replays history through the same rules, with costs, blackout and Monte Carlo
├── scanner.py       # finds pairs: chooses on one period, tests on the next (out of sample)
├── montecarlo.py    # reshuffles trades to show the range of outcomes and drawdowns
├── blackout.py      # earnings-date file and Alpaca news pause
├── streaming.py     # websocket bars instead of polling (DATA_SOURCE=stream)
├── notify.py        # phone alerts: ntfy push (no account needed) and/or Telegram
├── telegram_commands.py  # control from your phone: /status /trades /pnl /pause /resume /stop
├── reconcile.py     # nightly check that our records match the broker
├── watcher.py       # all-day market watch: grades every pair, trades every signal with demo money
├── insights.py      # the watcher's market brief and alerts: mood, sectors, pairs breaking
├── mcp_server.py    # lets Jarvis (or any MCP assistant) read and control the bot by voice
├── Dockerfile, docker-compose.yml, deploy/   # run it on a server (see DEPLOY.md)
├── tests/           # 71 tests: math, sizing, leg risk, halts, portfolio, scanner, dashboard, backtester
├── .env.example     # every setting, explained
└── requirements.txt
```

## Quick start

```bash
pip install -r requirements-dev.txt

# 1. Try everything offline (no keys, no market hours)
python main.py --mode sim --fast --cycles 800
streamlit run app.py                         # in another terminal

# 2. Paper trading
cp .env.example .env                          # add your PAPER keys
python main.py --mode alpaca --cycles 1      # smoke test: one cycle, check the log
python main.py                                # run; Ctrl-C stops cleanly
streamlit run app.py

python -m pytest -q                           # 58 tests, ~4 min

# 3. Find pairs, then run several at once
python scanner.py --source alpaca --start 2026-01-01 --end 2026-10-01 --timeframe 15
#    copy the PAIRS=... line it prints into .env
```

## The five upgrades, and the switches for each

| Upgrade | What it does | Turn it on / tune it |
|---|---|---|
| **Pair scanner + Monte Carlo** | `scanner.py` tests every pair in a sector on the first 60% of history and keeps only those that also made money on the last 40%. Every backtest now ends with a Monte Carlo line: the 5th–95th percentile profit, a bad-case drawdown and the chance of a loss | `python scanner.py --help` |
| **Real execution** | Entries rest a limit order at the mid for the first leg, then take the second leg with a capped marketable limit. Wide quotes are skipped. Every fill's slippage is stored, and `backtest.py --costs-from-db pairs_bot.db` reuses it as the cost | `ORDER_TYPE`, `LIMIT_TIMEOUT_SECONDS`, `MAX_SLIPPAGE_BPS`, `MAX_QUOTE_SPREAD_BPS` |
| **Smarter model** | Kalman-filter hedge ratio that adapts bar by bar; 5–15 minute bars; no entries while spread moves are 3x normal; stay out on earnings days and for 30 minutes after a headline | `HEDGE_METHOD=kalman`, `TIMEFRAME_MINUTES`, `MAX_VOL_RATIO`, `BLACKOUT_FILE`, `NEWS_PAUSE_MINUTES` |
| **Multi-pair portfolio** | One engine per pair under shared exposure and loss caps. Optional sizing so every stop-out loses about the same dollars. Weekly and monthly loss halts. A pair whose cointegration stays broken for N days is retired | `PAIRS`, `RISK_PER_TRADE_USD`, `MAX_WEEKLY_LOSS_PCT`, `MAX_MONTHLY_LOSS_PCT`, `RETIRE_PVALUE`, `RETIRE_DAYS` |
| **Run as a service** | One-command server setup (`deploy/setup.sh`), Docker or systemd, streaming prices, Telegram alerts **and commands** (`/status`, `/pause`, `/stop confirm`...), a password-protected dashboard reachable privately from your laptop and phone through Tailscale, a daily report and a nightly reconciliation | [DEPLOY.md](DEPLOY.md), `DATA_SOURCE=stream`, `NTFY_TOPIC`, `TELEGRAM_*`, `DASHBOARD_PASSWORD` |
| **Market watch** | `watcher.py` scans every pair in every sector all day on 15-minute bars and grades each one STRONG, RISKY, AVOID, WATCH or QUIET, with the reasons. Its "probability" is the pair's measured recent win rate after costs, not a forecast. Every STRONG, RISKY and AVOID signal is traded with demo money, so the dashboard shows whether the grades and the entry hours really matter. It also writes a market brief (mood, sectors, pairs breaking) and alerts your phone when the market changes | `python watcher.py --help`, dashboard "Market watch", [DEPLOY.md](DEPLOY.md#market-watch) |
| **Jarvis and no-Telegram control** | Ask your Jarvis assistant "how's the trading bot?", "how does the market look?", "what's strong right now?" or "pause trading" through `mcp_server.py`. Phone alerts through the free ntfy app, no account | [DEPLOY.md](DEPLOY.md#jarvis) |

Everything is off or conservative by default except limit orders, which are
cheaper. With no `PAIRS` set the bot trades `PAIR_SYMBOL_A/B` exactly as before.

## Backtesting: does it make money after costs?

```bash
python backtest.py --source alpaca --start 2026-04-01 --end 2026-10-01   # real history, needs keys
python backtest.py --source alpaca --start 2026-04-01 --end 2026-10-01 --timeframe 15
python backtest.py --source csv --csv data/my_bars.csv                    # your own bars
python backtest.py --source sim --bars 20000                              # offline demo only
python backtest.py --source alpaca --start ... --end ... --hedge kalman --blackout blackout.csv
python backtest.py --source alpaca --start ... --end ... --costs-from-db pairs_bot.db   # your real costs
```

The backtester runs history through the **same** `compute_spread_stats`, `decide`
and `plan_pair` the live bot uses. A test checks that, on the same prices, it
reproduces the live loop's trades and P&L exactly. It has no look-ahead, and by
default each order fills on the bar *after* the signal. It charges every order
`--spread-bps` (half the bid/ask spread plus slippage, 2 bps by default), the
SEC and FINRA fees on sells, and a borrow fee on the short leg.

It prints a summary and writes `backtest_out/` (`report.html`, `trades.csv`,
`equity.csv`, `summary.json`). The two lines to read first:

- **`breakeven_cost_bps`**: the largest cost per order at which the strategy still
  breaks even. If your real cost is close to or above it, there is no edge.
- **`verdict`**: no edge / too few trades / weak edge / worth checking out of sample.

Downloaded bars are cached in `data/`, so re-runs with different thresholds are
offline and fast. Sim results are meaningless for profitability (the simulator is
cointegrated by construction); use real history.

## How it trades

| Step | Formula | Where |
|---|---|---|
| Hedge ratio | OLS on the last 390 bars: `log A = α + β·log B` | `strategy.hedge_ratio` |
| Spread | `s = log A − β·log B − α` | `compute_spread_stats` |
| Z-score | `(s_t − mean(s, 60)) / std(s, 60)` | `compute_spread_stats` |
| Filter | Engle-Granger p-value < 0.05, β > 0, \|z\| < stop | `decide` |
| Entry | z ≥ +2.0: short A / long B. z ≤ −2.0: long A / short B | `decide` |
| Exit | \|z\| ≤ 0.5 (reverted), \|z\| ≥ 3.5 (stop), or 390 bars held (time stop) | `decide` |
| Sizing | `qty_A = ⌊$2,000 / P_A⌋`, `qty_B = ⌊β·qty_A·P_A / P_B⌋`, whole shares | `execution.plan_pair` |

The exit threshold defaults to ±0.5. Your brief gave ±2.0 for entry and exit; exiting
at the same level you entered at would close trades immediately, so exit
is set at the point where the spread has mostly mean-reverted. Change `EXIT_Z`
if you want something else.

## Safety mechanisms

| Risk | What the bot does | Test |
|---|---|---|
| Daily loss | Daily P&L = `equity − last_equity` (yesterday's close). At −$300 or −2%, whichever is tighter, it flattens all positions and halts | `test_daily_loss_limit_flattens_and_halts` |
| Slow bleed | Down 4% since Monday or 8% since the 1st of the month: flatten and halt | `test_weekly_loss_limit_halts` |
| Broken pair | Cointegration p-value ≥ 0.10 for 5 trading days: the pair stops opening trades (dashboard shows it; `--reset-halt` clears it) | `test_broken_pair_is_retired...` |
| Events | Earnings days in `blackout.csv` close open trades and block entries; a fresh headline blocks entries | `test_blackout_file_and_rules` |
| Limit-order legs | A missed first leg trades nothing. A partial first leg scales the second. A failed second leg unwinds the first | `test_missed_limit_entry...`, `test_partial_first_leg...`, `test_limit_second_leg_failure_unwinds` |
| Records drift | After the close (and nightly via `reconcile.py`) the bot compares its open trades with the broker and alerts on any mismatch | `test_nightly_reconcile_job` |
| Emergency stop | The dashboard button writes a flag the bot reads every cycle (flatten + halt). In alpaca mode the button also calls `close_all_positions` directly, so it works even if the bot is down | `test_dashboard_stop_button...`, `test_app` |
| Restart after a halt | The halt is persisted in the database, so the bot refuses to trade until you run `--reset-halt` | same |
| Leg risk | The short leg is sent first. If the second leg fails, the first is closed immediately. If the short leg isn't borrowable, no orders are sent at all | `test_second_leg_failure_unwinds_first`, `test_unshortable_leg_sends_nothing` |
| Stale data | The bot won't act on a bar older than 180 s | `test_stale_data_blocks_trading` |
| API outages | Five errors in a row flatten and halt | `test_repeated_errors_halt` |
| Unknown positions | Existing positions in the pair's symbols at startup halt the bot without touching them | `test_unknown_positions_at_startup...` |
| Pair breaking down | Stop at \|z\| ≥ 3.5, no entries past the stop band, cointegration filter on entries | `test_broken_relationship...` |
| SQLite contention | WAL mode, 5 s busy_timeout, short transactions, a read-only dashboard connection, and a JSONL fallback | `test_dashboard_reads_while_bot_writes` |
| Live trading | `paper=True` is hard-wired and validated twice; there is no live switch | `config.py`, `execution.py` |

## What was and wasn't verified

- **Verified offline.** The full loop, every safety mechanism above, and the dashboard. The dashboard was checked with Streamlit's `AppTest` and the screenshot above.
- **Not verified against Alpaca.** This build environment can't reach Alpaca's API, and no keys were provided. The Alpaca classes and method signatures (orders, limit orders, quotes, news, the websocket stream) were checked against the installed `alpaca-py` 0.44.0, but have not been run against a live paper account. Run `python main.py --mode alpaca --cycles 1` during market hours first and check the log and the dashboard. Telegram and ntfy sending were not tested against the real services either, and the Jarvis connection was tested against the MCP library, not a running Jarvis.
- **The simulator is optimistic by construction.** Its pair is cointegrated by design, so the 79% win rate in the screenshot says nothing about KO/PEP.

## Real-world notes

- **Cointegration on 1-minute bars is fragile.** Expect the p-value filter to block many signals. Test candidate pairs on daily or 15-minute data before trusting a pair.
- **Market orders pay the spread on both legs, twice per round trip.** On a $2,000 leg with a few cents of spread per share, costs are a large share of a 0.5σ move. The free IEX feed is also thinner than SIP.
- **Shorting needs a margin account and a borrow.** Paper accounts are margin by default.
- **Pattern-day-trader rule.** Alpaca applies the PDT rule to paper accounts under $25k equity. Intraday round trips count as day trades.
- **The loss limit is account-wide.** It uses whole-account equity, so other positions on the same paper account count toward it.
