"""Market data, spread model and trading signal.

The math, in one place
----------------------
1. Hedge ratio. Regress log prices over the last ``hedge_lookback`` bars:

       log(A_t) = alpha + beta * log(B_t) + e_t        (ordinary least squares)

   beta is how many "units of B" (in log/percentage terms) move with one unit
   of A. Using logs makes the spread scale-free and turns beta into an
   elasticity, which is what dollar-weighting the legs needs.

2. Spread. The residual of that regression:

       s_t = log(A_t) - beta * log(B_t) - alpha

   If A and B are cointegrated, s_t is stationary: it wanders but is pulled
   back towards a constant mean.

3. Z-score. How unusual today's spread is, versus its recent behaviour:

       z_t = (s_t - mean(s over last z_lookback bars)) / std(s over last z_lookback bars)

   |z| >= 2 means the spread is about two standard deviations from normal.

4. Cointegration check (Engle-Granger). ``statsmodels.tsa.stattools.coint``
   tests the null "no cointegration". A small p-value (< 0.05) is evidence
   the spread mean-reverts; above that, the bot will not open new trades.

5. Half-life of mean reversion. Fit an AR(1) on spread changes:

       Δs_t = lambda * s_{t-1} + c + u_t ,   half_life = -ln(2) / lambda

   It is logged for the dashboard; a huge or negative half-life is another
   sign the pair is not behaving.

Trading rule (z thresholds from config):
    FLAT          z >= +entry  -> OPEN_SHORT  (spread rich:  short A, long B)
                  z <= -entry  -> OPEN_LONG   (spread cheap: long A, short B)
                  |z| >= stop  -> no entry (too extreme, likely a break)
    LONG_SPREAD   z >= -exit   -> CLOSE (reverted)   |  z <= -stop -> CLOSE (stop)
    SHORT_SPREAD  z <= +exit   -> CLOSE (reverted)   |  z >= +stop -> CLOSE (stop)
    any position  held >= max_holding_bars -> CLOSE (time stop)
"""

from __future__ import annotations

import asyncio
import enum
import math
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

import numpy as np
import pandas as pd

from config import Settings


class Position(enum.StrEnum):
    FLAT = "FLAT"
    LONG_SPREAD = "LONG_SPREAD"     # long A, short B: profits when the spread rises
    SHORT_SPREAD = "SHORT_SPREAD"   # short A, long B: profits when the spread falls


class Action(enum.StrEnum):
    HOLD = "HOLD"
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE = "CLOSE"
    BLOCKED = "BLOCKED"             # would have opened, but a filter said no


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
class MarketData(Protocol):
    async def get_closes(self, symbols: tuple[str, str], bars: int) -> pd.DataFrame:
        """Return a DataFrame indexed by bar time (UTC) with one close column per symbol,
        oldest first, aligned on timestamps both symbols traded."""


class AlpacaMarketData:
    """Historical + latest bars from Alpaca's market data API (alpaca-py).

    The SDK is synchronous, so calls run in a worker thread via ``asyncio.to_thread``
    and never block the event loop.
    """

    def __init__(self, cfg: Settings) -> None:
        from alpaca.data.enums import DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        self._client = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
        self._timeframe = TimeFrame(cfg.timeframe_minutes, TimeFrameUnit.Minute)
        self._feed = DataFeed.IEX if cfg.data_feed == "iex" else DataFeed.SIP
        self._minutes = cfg.timeframe_minutes

    def _fetch(self, symbols: tuple[str, str], bars: int) -> pd.DataFrame:
        from alpaca.data.requests import StockBarsRequest

        # Ask for a calendar window wide enough to contain `bars` trading bars
        # (6.5 trading hours a day, plus weekends/holidays), then keep the tail.
        trading_days = math.ceil(bars * self._minutes / 390) + 1
        start = datetime.now(timezone.utc) - timedelta(days=trading_days * 7 // 5 + 4)
        req = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=self._timeframe,
                               start=start, feed=self._feed)
        df = self._client.get_stock_bars(req).df  # MultiIndex (symbol, timestamp)
        if df.empty:
            return pd.DataFrame(columns=list(symbols))
        closes = df["close"].unstack(level="symbol")
        # Inner-join on time: only bars where BOTH legs printed are comparable.
        return closes[list(symbols)].dropna().tail(bars)

    async def get_closes(self, symbols: tuple[str, str], bars: int) -> pd.DataFrame:
        return await asyncio.to_thread(self._fetch, symbols, bars)


# ---------------------------------------------------------------------------
# Spread model
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SpreadStats:
    bar_time: float          # epoch seconds of the latest bar
    price_a: float
    price_b: float
    alpha: float
    beta: float
    spread: float
    spread_mean: float
    spread_std: float
    zscore: float
    coint_pvalue: float
    half_life: float

    def as_dict(self) -> dict[str, float]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def hedge_ratio(log_a: np.ndarray, log_b: np.ndarray) -> tuple[float, float]:
    """OLS fit of log_a = alpha + beta * log_b. Returns (alpha, beta)."""
    X = np.column_stack([np.ones_like(log_b), log_b])        # design matrix [1, log_b]
    (alpha, beta), *_ = np.linalg.lstsq(X, log_a, rcond=None)
    return float(alpha), float(beta)


def half_life(spread: np.ndarray) -> float:
    """Mean-reversion half-life in bars from an AR(1) fit on spread changes."""
    lagged, delta = spread[:-1], np.diff(spread)
    X = np.column_stack([np.ones_like(lagged), lagged])
    (_, lam), *_ = np.linalg.lstsq(X, delta, rcond=None)
    # lam < 0 means the spread is pulled back; lam >= 0 means it drifts away (no reversion).
    return float(-math.log(2) / lam) if lam < 0 else math.inf


def coint_pvalue(log_a: np.ndarray, log_b: np.ndarray) -> float:
    """Engle-Granger cointegration test p-value (lower = stronger evidence of mean reversion)."""
    from statsmodels.tsa.stattools import coint

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # statsmodels warns on short samples
        _, pvalue, _ = coint(log_a, log_b)
    return float(pvalue)


def compute_spread_stats(closes: pd.DataFrame, cfg: Settings, with_coint: bool = True) -> SpreadStats:
    """Turn aligned closes into the current hedge ratio, spread and z-score.

    ``with_coint=False`` skips the slow Engle-Granger test and leaves
    ``coint_pvalue`` as NaN. The backtester uses it and runs the test only on
    bars where ``decide`` actually reads the p-value.
    """
    if len(closes) < cfg.hedge_lookback:
        raise ValueError(f"need {cfg.hedge_lookback} aligned bars, have {len(closes)}")
    window = closes.tail(cfg.hedge_lookback)
    a = window[cfg.symbol_a].to_numpy(dtype=float)
    b = window[cfg.symbol_b].to_numpy(dtype=float)
    if (a <= 0).any() or (b <= 0).any() or not (np.isfinite(a).all() and np.isfinite(b).all()):
        raise ValueError("non-positive or non-finite prices in window")
    log_a, log_b = np.log(a), np.log(b)

    alpha, beta = hedge_ratio(log_a, log_b)
    spread = log_a - beta * log_b - alpha                     # residual series
    recent = spread[-cfg.z_lookback:]
    mean, std = float(recent.mean()), float(recent.std(ddof=1))
    z = (float(spread[-1]) - mean) / std if std > 0 else 0.0

    ts = closes.index[-1]
    bar_time = ts.timestamp() if hasattr(ts, "timestamp") else float(ts)
    return SpreadStats(bar_time, float(a[-1]), float(b[-1]), alpha, beta, float(spread[-1]),
                       mean, std, z, coint_pvalue(log_a, log_b) if with_coint else math.nan,
                       half_life(spread))


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------
def decide(position: Position, stats: SpreadStats, bars_held: int, cfg: Settings) -> tuple[Action, str]:
    """Pure decision function: same inputs always give the same action."""
    z = stats.zscore
    if position is Position.FLAT:
        if abs(z) < cfg.entry_z:
            return Action.HOLD, "inside entry band"
        if abs(z) >= cfg.stop_z:
            return Action.BLOCKED, f"|z|={abs(z):.2f} beyond stop band; not entering a possible break"
        if stats.coint_pvalue >= cfg.min_coint_pvalue:
            return Action.BLOCKED, f"cointegration p={stats.coint_pvalue:.3f} >= {cfg.min_coint_pvalue}"
        if stats.beta <= 0:
            return Action.BLOCKED, f"hedge ratio {stats.beta:.3f} <= 0"
        return (Action.OPEN_SHORT, "spread rich") if z > 0 else (Action.OPEN_LONG, "spread cheap")

    sign = 1 if position is Position.LONG_SPREAD else -1     # +1: we want z to rise back to 0
    if sign * z >= -cfg.exit_z:
        return Action.CLOSE, "reverted"
    if sign * z <= -cfg.stop_z:
        return Action.CLOSE, "stop: spread kept diverging"
    if bars_held >= cfg.max_holding_bars:
        return Action.CLOSE, "time stop"
    return Action.HOLD, "waiting for reversion"
