# Understanding the pairs bot: what to learn, and how it all works

## Part 1. How the whole system works, in plain words

### The idea in one picture

Think of two dogs from the same house walking through a park, KO (Coca-Cola) and
PEP (Pepsi), both on long leashes held by the same owner. They wander apart and
come back together all day. If you notice they're unusually far apart, you can bet
they'll drift back together soon. That's all pairs trading is: **bet on the gap
closing, not on where the market goes.**

### The four parts, and what each one does

| Part | File | Plain-words job |
|---|---|---|
| **The brain** | `strategy.py` | Looks at the last trading day of prices and works out three things: the normal relationship between the two stocks, how far apart they are right now, and whether the leash still seems to exist. |
| **The hands** | `execution.py` | Places the buy and sell orders on Alpaca's pretend-money (paper) account, and makes sure you never end up with only half a trade. |
| **The heart** | `main.py` | Wakes up every minute and runs the routine below, with safety checks first. |
| **The diary** | `db_logger.py` | Writes down everything it saw and did in a database file. |
| **The window** | `app.py` | A web page that shows the diary as charts, plus a big red stop button. |
| **The time machine** *(new)* | `backtest.py` | Replays months of *past* prices through the same brain to show what *would* have happened, including the costs. |

### One minute in the life of the bot

1. **"Is it safe to work?"** Has someone pressed stop? Have we lost more than the daily limit ($300 or 2%)? If so, it closes everything and stops until you restart it with `--reset-halt`.
2. **"What are the prices?"** It downloads the latest one-minute prices for KO and PEP.
3. **"How far apart are they, compared to normal?"** That gap, measured in standard deviations, is the **z-score**:
   - near **0**: normal, so it does nothing;
   - **±2**: unusually far apart, so it opens a trade (buy the cheap one, short the expensive one);
   - back to **±0.5**: the gap has closed, so it takes the profit;
   - **±3.5**: still getting wider, so it cuts the loss because the leash may have snapped.
4. **"Is the leash real?"** Before opening, a statistical test (*cointegration*) checks that the two still move together. If they don't, it doesn't trade.
5. **"Do it, and write it down."** It places the orders and records everything.

### Why the time machine matters most

The bot can follow its rules perfectly and still lose money, because every trade
costs a little: the gap between buy and sell prices (the *spread*), slippage, and
fees. With pairs trading, each win is small, so those costs can eat the whole profit.

The backtester answers one question before you spend months watching paper trades:
**"After costs, would this have made money?"** The number to look at is the
**break-even cost**. Say it reports `breakeven_cost_bps: 1.5` and your real cost
is about 2 bps per order. Then the strategy loses money, however good the chart
looks before costs.

It uses the *exact same* brain code as the live bot, not a copy. A test proves
that, on the same prices, the backtest makes the same trades for the same profit
as the live loop. It also never peeks at future prices.

### Your next three steps

1. Put your Alpaca **paper** keys in `.env`.
2. Run `python backtest.py --source alpaca --start 2026-04-01 --end 2026-10-01`, then the same command with `--timeframe 15`.
3. Read the `verdict` and `breakeven_cost_bps` lines, and send them to me. That tells us whether to keep KO/PEP, go to slower bars, or look for other pairs.

---

## Part 2. What to learn, in order

You don't need all of this before using the bot. Each stage maps to a part of the
code, so you'll understand that part once you've done the stage. I've listed real,
well-known courses and books, but I couldn't check today's prices or links from
here, so search for them by name.

### Stage 1. Python for data (about 4–6 weeks, free)
*Unlocks: reading every file in the project.*
- **Python for Everybody** (Dr. Chuck Severance, py4e.com or on Coursera). The friendliest start.
- **Kaggle Learn: Python, then Pandas** (kaggle.com/learn). Short, hands-on micro-courses. Pandas is the table library the bot uses for prices.

### Stage 2. Statistics basics (about 3–4 weeks, free)
*Unlocks: z-score, mean, standard deviation, regression, p-values, the core of `strategy.py`.*
- **Khan Academy: Statistics and Probability.** Focus on the units on describing data, the normal distribution, linear regression and significance tests (p-values).

### Stage 3. How markets and trading actually work (about 3–4 weeks)
*Unlocks: short selling, bid/ask spreads, slippage, margin, and why costs matter.*
- **Financial Markets** (Robert Shiller, Yale, on Coursera). Free to audit.
- **Alpaca Learn** (alpaca.markets/learn), plus the Alpaca API docs. These cover paper trading, order types and short selling, as used in `execution.py`.
- Investopedia articles on *short selling*, *bid-ask spread*, *slippage* and the *pattern day trader rule*.

### Stage 4. Pairs trading and mean reversion (the heart of this bot)
*Unlocks: cointegration, hedge ratio, half-life, and why pairs stop working.*
- **Ernest Chan, *Algorithmic Trading: Winning Strategies and Their Rationale*** (2013). Chapters 2–3 on mean reversion and cointegration are almost exactly this bot, and they also cover the Kalman filter from roadmap step 5. **If you read one thing, read this.**
- **Ernest Chan, *Quantitative Trading*.** An easier first book on backtesting and how to avoid fooling yourself.
- **Ganapathy Vidyamurthy, *Pairs Trading: Quantitative Methods and Analysis*.** Deeper on the maths.
- Optional paid course: **Quantra (QuantInsti)**, its mean-reversion and statistical-arbitrage courses in Python.

### Stage 5. Time series (when you want to go deeper)
*Unlocks: stationarity, AR(1), why the leash test works.*
- ***Forecasting: Principles and Practice*** (Hyndman & Athanasopoulos). A free online book at otexts.com. Read the chapters on stationarity and ARIMA.

### Stage 6. Backtesting honestly (before any real money)
*Unlocks: reading the backtest report critically.*
- Chan's *Quantitative Trading*, the chapter on backtesting pitfalls: look-ahead bias, survivorship bias, data snooping.
- **Machine Learning for Trading** (Georgia Tech, Tucker Balch, free on Udacity). Ignore the ML parts at first. The early lessons on portfolio statistics, Sharpe ratio and backtesting are what you need.
- Advanced and optional: **MIT OpenCourseWare 18.S096, Topics in Mathematics with Applications in Finance.**

### A sensible schedule
- **Months 1–2:** Stages 1–2, while the bot runs on paper in the background.
- **Month 3:** Stage 3, and read the backtest reports with that knowledge.
- **Months 3–4:** Stage 4 (Chan's book), and try the roadmap ideas in `GUIDE.md` yourself.
- **After that:** Stages 5–6, before real money is even discussed.

### Words you'll keep seeing

| Word | Plain meaning |
|---|---|
| Spread (of a pair) | The gap between the two stocks after adjusting for their normal relationship |
| Bid/ask spread | The difference between the price you can buy at and the price you can sell at. A cost on every order |
| Z-score | How unusual the current gap is, in "standard deviations". 2 means rare, 0 means normal |
| Hedge ratio (beta) | How many dollars of PEP to hold against each dollar of KO so the market's moves cancel out |
| Cointegration | A statistical test of whether the leash exists, so the gap keeps coming back |
| Half-life | The typical time for the gap to close halfway |
| Short selling | Borrowing a stock, selling it, and buying it back later. You profit if it falls |
| Basis point (bp) | 0.01%. Costs are measured in these: 2 bps on $2,000 is 40 cents |
| Sharpe ratio | Profit divided by how bumpy the ride was. Above 1 is decent, above 2 is good |
| Drawdown | The biggest drop from a peak, or how bad the worst stretch felt |
| Backtest | Replaying history to see what the rules would have done |
| Out-of-sample | Testing on a period you didn't use to choose the settings. The only honest kind of test |
| Paper trading | Real market, pretend money |
| Kalman filter | A way to let the hedge ratio drift a little every bar instead of re-fitting it from scratch. Smoother, fewer false signals |
| Walk-forward | Choose on one period, test on the next, then move forward and repeat. What `scanner.py` does |
| Monte Carlo | Reshuffling the backtest's trades thousands of times to see how lucky or unlucky the one real ordering was |
| Limit order | "Buy, but only at this price or better." Cheaper than a market order, but it may not fill |
| Mid price | Halfway between the bid and the ask. Where the bot rests its first order |
| Slippage | How much worse your fill was than the price you expected. The bot measures it on every order |
| Blackout | A day (earnings) or moment (breaking news) when the bot stays out of a pair on purpose |
