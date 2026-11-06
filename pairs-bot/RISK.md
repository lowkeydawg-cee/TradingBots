# Risk rules and testing for the buy-only approaches

This is the "Personal Trading Bot Upgrade Recommendations" roadmap, mapped to the code: what each part of it
became, and what is still to build. Everything here runs with demo money. No buy-only approach places a real
or paper order yet; one has to pass the validation gates below first.

## How a trade happens

1. **Strategy proposes** (`longonly.decide`). On each closed 4-hour candle, each approach says buy, sell or wait,
   with the reason in words.
2. **Risk layer decides** (`governance.Sleeve`). Each approach has its own $10,000 demo account. The account
   decides whether the buy is allowed and how big it is.
3. **Execution** (`longonly.simulate` for the backtest, `longonly.LongBook` live). The trade is filled with
   costs (about 0.3% per crypto order). Every decision is written to the `long_decisions` journal, including
   waits and refusals.

The backtest and the live demo book use the same functions, so what is tested is what runs.

## The rules

| | Rule | Why |
|---|---|---|
| Timeframe | Decide on closed 4-hour candles only | On 15-minute bars, fees ate the profits: Trend lost $14,100 in a year, $11,900 of it in fees. On 4-hour bars the loss was $462 |
| Market filter | Buy only while price is above its 20-day average and that average is rising | Most losses came from buying into falling markets |
| Trend | Fast average (20 bars) above slow (100 bars) and price above both. Sells when they cross back, when the market turns down, or on a trailing stop 2 days of normal movement below the high | The core approach |
| Dip | RSI under 30 in a rising and calm market. Sells when RSI is back above 55, on a stop 1.5 days of movement lower, when the market turns down, or after 2 days | Only buys dips when the bigger picture is up |
| Breakout | A close above the 5-day high by at least half a normal bar's move. Waits a day after a losing breakout | Filters out false breakouts |
| Hold | The same $10,000 spread evenly across the markets, bought once | The yardstick, with the same costs and cash reserve |
| Size | Each trade risks 1% of the account, measured as 2 days of the coin's normal movement | Wild coins get smaller positions |
| Caps | 25% of the account per asset, 50% in crypto together, 20% always in cash | Coins tend to fall together |
| Drawdown | 5% below the high: half size. 10%: no new buys. 15%: sell everything and stop until restarted | It never takes more risk because it is losing |

The settings were fixed before testing (`longonly.Params`, `governance.py`), not tuned to the results.

## Testing: `python longonly.py --validate --source alpaca --days 365`

- The test period is split by time into development, validation and out-of-sample thirds, so no test peeks at
  the future.
- Walk-forward: six consecutive windows, each compared with Hold.
- Stress tests: costs ×1.5, and the averages 20% slower and 20% faster.
- Measures: net and return, worst fall, Sharpe, win rate, profit factor, time invested, turnover, longest
  losing streak, costs, days stopped, and money by market state at entry.

**Gates.** An approach is ready for paper trading only if it passes all six:

1. It makes money out of sample.
2. It beats Hold in 2 of the 3 periods.
3. It still makes money with costs ×1.5.
4. It still makes money with the averages 20% slower and 20% faster.
5. Its worst fall stays under 15%.
6. It has at least 30 trades.

The report is saved to `review/validation.md`, and the dashboard's Approaches page shows it under Validation.

## Roadmap status

| Roadmap item | Status |
|---|---|
| Strategy → intent → risk → execution layers | Done for the buy-only approaches |
| Trend as the core, with a market-state filter | Done |
| Dip gated by the bigger trend | Done (20-day trend, plus no dip buying in wild markets) |
| Breakout false-signal filters | Minimum distance and cooldown done. Volume filter still to do, because the bot stores prices, not volume |
| Hold as a fair benchmark | Done (same account size, costs and cash reserve) |
| Volatility sizing and portfolio caps | Done (per asset, per approach, crypto cluster, cash reserve) |
| Drawdown state machine | Done (normal, caution, defense, stopped), shown on the page |
| Chronological splits, walk-forward, stress tests, metrics | Done (`--validate`) |
| Decision journal and "why it did nothing" | Done (`long_decisions`, the page's first tab) |
| Pairs as a small sleeve under the same risk layer | Still to do. Pairs keeps its own demo book on US stocks |
| Drift detector (live results against the backtest) | Still to do. It needs a few weeks of live journal first |
| Reconciliation | Exists for the pairs paper orders (`reconcile.py`). The buy-only approaches place no orders yet |
| Kill switch | Each buy-only account stops itself at 15% down. A manual stop and restart command is still to do |
| Paper orders for an approach that passes | Still to do, and only after a pass on a year of real prices |

This is demo money on past and live prices. It is not a forecast and not financial advice.
