# How to test the pairs bot, step by step

Work through the steps in order. Each one checks something the previous step
couldn't. **Nothing here uses real money.** Alpaca's paper account is pretend
money on real market prices.

## Step 0. Set up (once, about 15 minutes)

You need Python 3.11 or newer.

```bash
git clone https://github.com/lowkeydawg-cee/TradingBots.git
cd TradingBots/pairs-bot
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

## Step 1. Automatic tests (2 minutes, offline)

```bash
python -m pytest -q
```

**Pass:** `58 passed` (about 4 minutes). This checks the maths, order sizing, limit-order
legging, the safety brakes (daily, weekly, pair retirement), the multi-pair portfolio,
the database, the dashboard, the scanner, Monte Carlo and the backtester. If anything fails, send me the output.

## Step 2. Try the whole system on fake prices (10 minutes, offline)

Terminal 1:
```bash
python main.py --mode sim --fast --cycles 800
PAIRS=KO:PEP,XOM:CVX,V:MA python main.py --mode sim --fast --cycles 800   # portfolio (new database: delete pairs_bot.db first)
python scanner.py --source sim                                             # the scanner on a fake sector
```
Terminal 2:
```bash
streamlit run app.py                 # opens a page in your browser
```

**Check:**
- The equity curve, the z-score chart with its trigger lines, and the trades table all fill in.
- With several pairs: the sidebar has a pair picker and the performance section has a "By pair" table.
- "Avg cost per order (bps)" shows a small positive number: the simulated spread you paid.
- The scanner recommends AAA/BBB, rejects GGG/HHH (never a pair), and shows EEE/FFF failing out of sample (it breaks halfway).
- Tick the confirm box and press **EMERGENCY STOP**. The page should show **HALTED**.
- Run `python main.py --mode sim --fast --cycles 50` again. It should refuse to trade.
- Run it with `--reset-halt` and it trades again.

Simulated profits mean nothing; the fake pair is built to work. This step only shows that the machinery works.

## Step 3. Get Alpaca paper keys (10 minutes)

1. Sign up at alpaca.markets and open the **Paper** dashboard.
2. Generate an API key and secret.
3. `cp .env.example .env`, then paste the two keys into `.env`. **Never commit `.env`**; it's already in `.gitignore`.

## Step 4. Backtest on real prices (the most important test)

```bash
python backtest.py --source alpaca --start 2026-04-01 --end 2026-10-01
python backtest.py --source alpaca --start 2026-04-01 --end 2026-10-01 --timeframe 15
python backtest.py --source alpaca --start 2026-04-01 --end 2026-10-01 --spread-bps 1
```

Open `backtest_out/report.html` and read two lines:
- **`verdict`** says whether there's an edge after costs.
- **`breakeven_cost_bps`** is the most each order can cost before profit disappears. It needs to be clearly above 2.

Also read the **`monte_carlo`** line: if `pnl_p5` is negative, a losing result was
quite possible even with these trades. Then try the adaptive model and your earnings dates:

```bash
python backtest.py --source alpaca --start 2026-04-01 --end 2026-10-01 --timeframe 15 --hedge kalman
python backtest.py --source alpaca --start 2026-04-01 --end 2026-10-01 --timeframe 15 --blackout blackout.csv
```

**Then the honesty check (out of sample):** run the same command on a *different*
period, for example `--start 2025-10-01 --end 2026-04-01`. If the result only works
in one period, it was probably luck.

## Step 4b. Find better pairs with the scanner

```bash
python scanner.py --source alpaca --start 2026-01-01 --end 2026-10-01 --timeframe 15
```

It chooses pairs on the first 60% of the period and tests them on the last 40%.
Read the in-sample vs out-of-sample columns: a big drop means the pair was luck.
It prints a `PAIRS=...` line for `.env`. "Nothing to recommend" is a real
answer too.

## Step 5. One-cycle smoke test on paper (during market hours)

The US market is open 9:30–16:00 New York time, Monday to Friday. Until
1 November 2026 that's 13:30–20:00 UTC; after that, 14:30–21:00 UTC.

```bash
python main.py --mode alpaca --cycles 1
```

**Pass:** no errors in the terminal, and the dashboard shows one row of prices and
a z-score. This is the first time the Alpaca connection runs for real, so if
anything breaks, send me the error text.

When a trade opens, check in Alpaca's web app that the first leg was a **limit**
order at about the mid price. To compare, set `ORDER_TYPE=market` for a day.

## Step 6. Let it run on paper for a full day, then for weeks

```bash
python main.py --mode alpaca          # Ctrl-C stops it cleanly
streamlit run app.py
```

**Check after the first day:**
- Every trade appears in the Alpaca web dashboard with **both** legs.
- The P&L in our dashboard matches Alpaca's.
- During market hours, press the emergency stop once. Alpaca should show zero positions.

**Check every week:**
- Compare your real costs with what the backtest assumed:
  ```bash
  python backtest.py --source alpaca --start <monday> --end <saturday> --costs-from-db pairs_bot.db
  ```
- Run the backtester over the same week:
  ```bash
  python backtest.py --source alpaca --start <monday> --end <saturday>
  ```
- Paper and backtest results should be roughly similar, within about 30%. A big gap means the backtest is too optimistic about costs. That's useful to know before any real money.

## Step 7. Move it to a server

Once a few paper days look sane on your computer, follow [DEPLOY.md](DEPLOY.md)
to run it on a small server with Telegram alerts.

## Common problems

| You see | What it means |
|---|---|
| `not shortable / easy to borrow` | Alpaca can't lend that stock right now, so the bot correctly skipped the trade |
| `stale data` | No new bar arrived. The free IEX feed is thin early and late in the day |
| `cointegration p=...` in BLOCKED | The pair isn't behaving like a pair today, so the bot is protecting you |
| `HALTED: daily loss limit` | The brake worked. Review what happened, then use `--reset-halt` |
| Pattern-day-trader warning | Only applies to accounts under $25k. Paper accounts start at $100k |
| `entry skipped: limit ... not filled` | The patient first leg didn't fill at the mid. Nothing traded; it tries again later. Normal |
| `volatility spike` in BLOCKED | The spread is moving 3x more than usual, so the bot waits |
| `blackout:` in BLOCKED | An earnings date in `blackout.csv` or a fresh headline |
| `pair retired` | The pair stopped behaving like a pair for 5 days. Check it, then `--reset-halt` if you want it back |
| `HALTED: weekly loss limit` | Down 4% since Monday. Review before resetting |
