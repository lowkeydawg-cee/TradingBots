"""Monte Carlo reshuffle: how lucky was this backtest?

A backtest shows one ordering of trades. With the same trades in a different
order (or a slightly different mix), the final profit and especially the
worst drawdown could look very different. This module resamples the list of
per-trade profits with replacement many times and reports the spread of
outcomes.

How to read it:

* ``pnl_p5`` .. ``pnl_p95``: 90% of reshuffled runs ended between these.
  If ``pnl_p5`` is below zero, a losing result was quite possible.
* ``max_drawdown_p95``: a bad-but-plausible drawdown. Size the account so
  you could sit through it without panicking.
* ``prob_loss``: share of runs that ended below zero.

It assumes trades are independent, which pairs trades mostly are (one trade
at a time per pair). It can't fix a backtest that's wrong to begin with:
garbage trades in, garbage ranges out.
"""

from __future__ import annotations

import numpy as np


def monte_carlo(trade_pnls: list[float], starting_equity: float = 100_000.0, runs: int = 2000,
                seed: int = 7) -> dict[str, float | int] | None:
    pnl = np.asarray(trade_pnls, dtype=float)
    if len(pnl) < 5:
        return None                                   # too few trades to say anything
    rng = np.random.default_rng(seed)
    samples = rng.choice(pnl, size=(runs, len(pnl)), replace=True)
    paths = starting_equity + np.cumsum(samples, axis=1)
    paths = np.hstack([np.full((runs, 1), starting_equity), paths])
    drawdowns = (paths - np.maximum.accumulate(paths, axis=1)).min(axis=1)
    final = paths[:, -1] - starting_equity
    pct = lambda a, q: round(float(np.percentile(a, q)), 2)  # noqa: E731
    return {
        "runs": runs,
        "pnl_p5": pct(final, 5),
        "pnl_p50": pct(final, 50),
        "pnl_p95": pct(final, 95),
        "max_drawdown_p50": pct(drawdowns, 50),
        "max_drawdown_p95": pct(drawdowns, 5),       # 5th percentile of a negative number = the bad tail
        "prob_loss": round(float((final < 0).mean()), 3),
    }
