# Pairs bot: how it works, how good it is, and what to build next

## 1. How the whole system works, in plain words

**The idea.** Some stocks move together because their businesses are alike. Coca-Cola
(KO) and Pepsi (PEP) are the default pair. Think of them as a person walking a dog
on a leash: they wander, but they never get too far apart for long. When the gap
between them becomes unusually wide, the bot bets that it will shrink again. It
buys the one that looks cheap and short-sells the one that looks expensive. When
the gap returns to normal, it closes both sides. It doesn't care whether the
market goes up or down, only whether the gap closes.

**How "unusually wide" is measured.** The bot works out the normal relationship
between the two prices over the last trading day, then measures today's gap in
standard deviations. That number is the **z-score**:

- **0** means a perfectly normal gap.
- **±2** means the gap is unusually wide, so the bot opens a trade.
- **±0.5** means the gap is back to almost normal, so the bot takes the profit.
- **±3.5** means the gap keeps getting wider, so the bot accepts a loss (the leash may have snapped).

Before opening a trade, it also runs a statistical test, **cointegration**, to
check the leash still exists. If the test fails, it doesn't trade.

**What happens every minute:**

1. **Safety checks first.** Has someone pressed the emergency stop? Has today's loss passed the limit ($300 or 2%)? If so, it closes everything and stops for good, until you reset it.
2. **Prices.** It downloads the latest prices for both stocks from Alpaca.
3. **The maths.** It works out the normal relationship, the gap and the z-score.
4. **The decision.** Do nothing, open a trade, or close the trade.
5. **The orders.** To open, it sends two orders, short side first. If the second order fails, it immediately undoes the first, so you're never left holding half a trade.
6. **The record.** Everything goes into a local database file, `pairs_bot.db`: prices, z-score, decision, orders, profit or loss.

**The dashboard** is a separate web page (`streamlit run app.py`) that only reads
that database. It shows your money over time, the z-score with its trigger lines,
your win rate and open positions, and a big emergency-stop button.

**Where the money is.** Everything runs on Alpaca's **paper** (pretend-money)
account. There is no switch for real money in this code.

## 2. Rating

These are my judgment calls, not measurements.

| Question | Score | Why |
|---|---|---|
| Is the software well built? | **8.5/10** | Tested end to end (53 tests), with real safety rails: daily, weekly and monthly loss limits, stop button, leg-failure unwind, pair retirement, a halt that survives restarts, and no database lock-ups. It loses points because the Alpaca connection, limit orders and streaming haven't been run live yet. |
| Will it run on paper without problems? | **7/10** | Likely, after the first smoke test. Expect one or two small fixes on first contact with the real API. |
| Will it make money as it is today? | **2–3/10** | Nothing has been backtested yet. KO/PEP on 1-minute bars is the most-watched pair there is, the edge per trade is tiny, and paying the spread on two market orders twice per round trip can eat it completely. |
| Can it become profitable with the roadmap below? | **Maybe 20–30%** (now that steps 2–8 are built) | Pairs trading is a real, documented strategy, but its edge has shrunk as more people trade it. Success depends almost entirely on step 2 (backtesting) and step 3 (picking pairs). |

The honest summary: the machine is good, but there's no evidence yet that the
strategy it runs makes money. The next work should prove or disprove that
before anything else.

(For comparison, the crash-game risk bot from earlier can't be profitable by
design, because the house edge is built into every round. This bot at least has
a real chance.)

## 3. Roadmap: what to add, in order, and why

**Step 1. Smoke test on paper (this week).** Put your keys in `.env` and run
`python main.py --mode alpaca --cycles 1` during market hours, then let it run a
full day.
*Why:* the Alpaca code path is the one part that hasn't run yet. Fix that before
building more on top.

**Step 2. Backtester with realistic costs (most important). ✅ Built: `backtest.py`, see the README.** Replay months of
historical bars through the exact same `decide()` function. Charge the bid/ask
spread, slippage and borrow fees on every trade, and report Sharpe ratio, maximum
drawdown and profit per trade *after* costs.
*Why:* this tells you whether there's an edge before you spend months watching
paper trades. If the backtest loses money after costs, you've saved yourself the
trouble.

**Step 3. Pair scanner with out-of-sample testing. ✅ Built: `scanner.py`, plus a Monte Carlo reshuffle in every backtest (`montecarlo.py`).** Scan a sector, for example
consumer staples, banks or oil majors. Pick pairs that are cointegrated in one
period, then check that they *stay* cointegrated in the next period
(walk-forward testing).
*Why:* KO/PEP was just a default. Most pairs that look cointegrated by chance
stop working. Testing out of sample is how you avoid fooling yourself.

**Step 4. Cheaper execution. ✅ Built: limit entries at the mid, live quotes, a spread check and slippage logging (`ORDER_TYPE=limit`, the default). `backtest.py --costs-from-db` reuses the measured costs.** Use limit orders at or near the mid price
instead of market orders, read live quotes, and only enter when the expected
reversion is at least about twice the round-trip cost.
*Why:* for a strategy with small profits per trade, costs often decide whether
it wins or loses.

**Step 5. Adaptive hedge ratio (Kalman filter) and slower bars. ✅ Built: `HEDGE_METHOD=kalman`, `TIMEFRAME_MINUTES`, plus a volatility filter and an earnings/news pause (`blackout.py`).** Let beta update
smoothly instead of re-fitting a 390-bar window, and try 5- or 15-minute bars.
*Why:* a jumpy beta creates false signals, and slower bars carry less noise
relative to costs.

**Step 6. Multiple pairs with portfolio limits. ✅ Built: `PAIRS=KO:PEP,XOM:CVX,...` and optional volatility sizing (`RISK_PER_TRADE_USD`).** Run 5 to 10 uncorrelated pairs,
each with its own kill switch, under a shared cap on gross exposure and loss.
*Why:* one pair gives a few trades a week, which is too few to know if it works.
Several pairs smooth the results, and one broken pair can't sink everything.

**Step 7. More loss limits. ✅ Built: `MAX_WEEKLY_LOSS_PCT`, `MAX_MONTHLY_LOSS_PCT`, and pair retirement (`RETIRE_PVALUE`, `RETIRE_DAYS`).** Add a weekly and a monthly loss cap on top of the
daily one, plus automatic retirement of a pair whose cointegration p-value stays
high for N days.
*Why:* the daily limit resets every morning, so a slow bleed over several days
is currently unchecked.

**Step 8. Run it like a service. ✅ Built: Docker and systemd files, streaming prices, Telegram alerts, a daily report and a nightly reconciliation job. See [DEPLOY.md](DEPLOY.md).** Host it on a small always-on server
(a $5–10/month VPS) under systemd so it restarts after crashes. Send alerts
(Telegram or email) on any halt, plus a daily summary.
*Why:* a bot on a laptop stops when the laptop sleeps, and a halted bot that
nobody notices is just lost time.

**Step 9. Go-live gate, decided in advance.** Write down what has to be true
before real money is involved. For example:
- 3 months of paper trading with a positive result after costs;
- a Sharpe ratio above 1;
- maximum drawdown within the limits you set;
- paper results within about 30% of what the backtest predicted.

Then start with a small amount you can afford to lose. Adding a live mode is a
deliberate code change, not a flag.
*Why:* deciding the rules before you see the results stops excitement from
making the decision for you.
