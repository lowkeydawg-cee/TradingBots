"""Buy-only strategies for crypto and gold, run with demo money next to the pairs book so you can compare.

Pairs trading needs a short side, and Alpaca can't short crypto. So for crypto
and gold this file tries approaches that only ever buy, then sell what they
bought:

  trend     buy when the fast average (20 bars) is above the slow one (100 bars) and price is above
            both; sell when the fast one drops below, or on a trailing stop
  dip       in an uptrend (price above the 200-bar average), buy when RSI(14) falls under 30 (a sharp
            dip); sell when RSI recovers past 55, on a stop, or after a day
  breakout  buy when price closes above its highest close of the last day; sell on a trailing stop
  hold      the benchmark: buy once and keep it. A strategy that can't beat this isn't worth running

Every strategy uses the same ``decide()`` function in the backtest below and
in the live watcher (``LongBook``). The backtest fills on the next bar; the
watcher fills at the price it just saw, which is as close as it can get live.
Both pay the same costs (about 0.3% per order on crypto). The trailing and
fixed stops are sized from the symbol's own recent volatility.

    python longonly.py --source sim                          # offline demo
    python longonly.py --source alpaca --days 60             # crypto (no keys needed) on the last 60 days
    python longonly.py --source alpaca --days 60 --symbols BTCUSD ETHUSD GLD SLV   # gold needs Alpaca keys

The watcher runs these on every scan with demo money (table ``long_trades``),
and ``python review.py`` / Jarvis ("which strategy is working?") compare them
with the pairs book. Past results, demo money, not a forecast.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from backtest import Costs
from scanner import ALL_SECTORS, CRYPTO_SYMBOLS, MARKET_OF

STRATEGIES = ("trend", "dip", "breakout", "hold")
LONG_SYMBOLS = ("BTCUSD", "ETHUSD", "SOLUSD", "LTCUSD", "GLD", "SLV", "GDX")
NOTIONAL = 2_000.0                 # demo dollars per trade, like a pairs leg
LOOKBACK = 300                     # bars of history a decision looks at
CRYPTO_COSTS = Costs(spread_bps=30.0, sec_fee_rate=0.0, taf_per_share=0.0, taf_max=0.0, borrow_rate_annual=0.0)


def market_of(symbol: str) -> str:
    for sector, syms in ALL_SECTORS.items():
        if symbol in syms:
            return MARKET_OF.get(sector, sector)
    return "?"


def costs_for(symbol: str) -> Costs:
    return CRYPTO_COSTS if symbol in CRYPTO_SYMBOLS else Costs()


# ---------------------------------------------------------------------------
# Indicators on a plain array of closes (oldest first)
# ---------------------------------------------------------------------------
def ema(x: np.ndarray, span: int) -> float:
    a = 2.0 / (span + 1)
    w = (1 - a) ** np.arange(len(x) - 1, -1, -1)
    return float(np.sum(w * x) / np.sum(w))


def rsi(x: np.ndarray, n: int = 14) -> float:
    d = np.diff(x[-(n * 5):])
    up, down = np.clip(d, 0, None), np.clip(-d, 0, None)
    a = 1.0 / n                                        # Wilder smoothing
    w = (1 - a) ** np.arange(len(d) - 1, -1, -1)
    au, ad = np.sum(w * up) / np.sum(w), np.sum(w * down) / np.sum(w)
    return 100.0 if ad == 0 else float(100 - 100 / (1 + au / ad))


def bar_vol(x: np.ndarray, n: int = 100) -> float:
    """Typical size of one bar's move, as a fraction of price."""
    r = np.diff(np.log(x[-(n + 1):]))
    return float(np.std(r)) if len(r) > 2 else 0.01


@dataclass
class Holding:
    entry: float
    peak: float
    bars: int = 0


def decide(strategy: str, x: np.ndarray, h: Holding | None) -> tuple[str | None, str]:
    """('buy' | 'sell' | None, why) from the closes up to now. ``h``: the open position, if any."""
    price = float(x[-1])
    vol4h = bar_vol(x) * math.sqrt(16) * price                  # about four hours of typical movement
    if strategy == "hold":
        return ("buy", "benchmark: buy and hold") if h is None else (None, "")
    if h is not None:
        if strategy in ("trend", "breakout") and price < h.peak - 3 * vol4h:
            return "sell", "trailing stop"
        if strategy == "trend" and ema(x[-200:], 20) < ema(x, 100):
            return "sell", "trend turned down"
        if strategy == "dip":
            if price < h.entry - 2 * vol4h:
                return "sell", "stop: kept falling"
            if rsi(x) > 55:
                return "sell", "bounced (RSI back above 55)"
            if h.bars >= 96:
                return "sell", "time limit"
        return None, ""
    if strategy == "trend":
        fast, slow = ema(x[-200:], 20), ema(x, 100)
        if fast > slow and price > fast:
            return "buy", f"uptrend (fast avg {fast:.2f} > slow {slow:.2f})"
    elif strategy == "dip":
        if len(x) >= 200 and price > ema(x, 200) and (r := rsi(x)) < 30:
            return "buy", f"dip in an uptrend (RSI {r:.0f})"
    elif strategy == "breakout":
        if len(x) > 97 and price > np.max(x[-97:-1]):
            return "buy", "new one-day high"
    return None, ""


# ---------------------------------------------------------------------------
# Backtest: same decide(), next-bar fills, same costs
# ---------------------------------------------------------------------------
@dataclass
class LongTrade:
    strategy: str
    symbol: str
    opened_at: pd.Timestamp
    entry: float
    qty: float
    note: str
    closed_at: pd.Timestamp | None = None
    exit: float = math.nan
    reason: str = ""
    costs: float = 0.0

    @property
    def gross(self) -> float:
        return (self.exit - self.entry) * self.qty if self.closed_at is not None else math.nan

    @property
    def net(self) -> float:
        return self.gross - self.costs


def backtest(closes: pd.Series, strategy: str, costs: Costs, start: pd.Timestamp | None = None,
             close_at_end: bool = True) -> list[LongTrade]:
    """Trades of one strategy on one symbol. Decisions from ``start`` on; earlier bars are only history."""
    s = closes.dropna()
    x = s.to_numpy(float)
    first = max(LOOKBACK // 2, int(s.index.searchsorted(start)) if start is not None else 0)
    trades: list[LongTrade] = []
    h: Holding | None = None
    for i in range(first, len(x) - 1):
        win = x[max(0, i + 1 - LOOKBACK):i + 1]
        if h is not None:
            h.bars += 1
            h.peak = max(h.peak, x[i])
        action, why = decide(strategy, win, h)
        fill, t = x[i + 1], s.index[i + 1]                      # next bar
        if action == "buy" and h is None:
            qty = NOTIONAL / fill
            trades.append(LongTrade(strategy, str(s.name), t, fill, qty, why,
                                    costs=sum(costs.order_cost(qty, fill, "buy"))))
            h = Holding(fill, fill)
        elif action == "sell" and h is not None:
            tr = trades[-1]
            tr.closed_at, tr.exit, tr.reason = t, fill, why
            tr.costs += sum(costs.order_cost(tr.qty, fill, "sell"))
            h = None
    if close_at_end and h is not None and trades:
        tr = trades[-1]
        tr.closed_at, tr.exit, tr.reason = s.index[-1], x[-1], "end of test (still open)"
        tr.costs += sum(costs.order_cost(tr.qty, x[-1], "sell"))
    return trades


def compare(closes: pd.DataFrame, symbols: list[str], start: pd.Timestamp | None = None) -> pd.DataFrame:
    rows = []
    for sym in symbols:
        if sym not in closes:
            continue
        for strat in STRATEGIES:
            for t in backtest(closes[sym].rename(sym), strat, costs_for(sym), start):
                rows.append((strat, sym, market_of(sym), t.opened_at, t.closed_at, t.net, t.reason))
    return pd.DataFrame(rows, columns=["strategy", "symbol", "market", "opened", "closed", "net_pnl", "reason"])


def summary_table(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    g = trades.groupby("strategy")["net_pnl"]
    out = pd.DataFrame({"trades": g.size(), "won": g.apply(lambda s: int((s > 0).sum())),
                        "net": g.sum(), "avg": g.mean()})
    out["win_rate"] = out["won"] / out["trades"]
    return out.sort_values("net", ascending=False)


# ---------------------------------------------------------------------------
# Live demo book, driven by the watcher on every scan
# ---------------------------------------------------------------------------
class LongBook:
    def __init__(self, db, symbols=LONG_SYMBOLS, strategies=STRATEGIES) -> None:
        self.db, self.symbols, self.strategies = db, tuple(symbols), tuple(strategies)
        self.open: dict[tuple[str, str], tuple[int, Holding, float]] = {}   # (strategy, symbol) -> (id, holding, qty)
        self.last_bar: dict[str, float] = {}

    async def load(self) -> None:
        rows = await self.db.fetchall("SELECT id, strategy, symbol, entry, qty FROM long_trades WHERE closed_at IS NULL")
        for i, strat, sym, entry, qty in rows:
            self.open[(strat, sym)] = (i, Holding(entry, entry), qty)

    async def update(self, closes: pd.DataFrame, tradable: set[str]) -> list[str]:
        """One step on each symbol's newest bar. Fills at that bar's close (the watcher has no next bar yet)."""
        events = []
        for sym in self.symbols:
            if sym not in closes or sym not in tradable:
                continue
            s = closes[sym].dropna()
            if len(s) < LOOKBACK // 2:
                continue
            bar_ts = s.index[-1].timestamp()
            if self.last_bar.get(sym) == bar_ts:                    # no new bar since last scan
                continue
            self.last_bar[sym] = bar_ts
            x = s.to_numpy(float)[-LOOKBACK:]
            price, costs = float(x[-1]), costs_for(sym)
            for strat in self.strategies:
                key = (strat, sym)
                held = self.open.get(key)
                if held:
                    held[1].bars += 1
                    held[1].peak = max(held[1].peak, price)
                action, why = decide(strat, x, held[1] if held else None)
                if action == "buy" and not held:
                    qty = NOTIONAL / price
                    tid = await self.db._write(
                        "INSERT INTO long_trades (strategy, symbol, market, opened_at, entry, qty, entry_note, costs, "
                        "last_price, last_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (strat, sym, market_of(sym), bar_ts, price, qty, why, sum(costs.order_cost(qty, price, "buy")),
                         price, bar_ts))
                    self.open[key] = (tid, Holding(price, price), qty)
                    events.append(f"{strat} bought {sym} at {price:,.2f} ({why})")
                elif action == "sell" and held:
                    tid, h, qty = held
                    row = await self.db.fetchall("SELECT costs FROM long_trades WHERE id = ?", (tid,))
                    total_costs = (row[0][0] if row else 0.0) + sum(costs.order_cost(qty, price, "sell"))
                    gross = (price - h.entry) * qty
                    await self.db._write(
                        "UPDATE long_trades SET closed_at=?, exit=?, gross_pnl=?, costs=?, net_pnl=?, exit_reason=? "
                        "WHERE id=?", (bar_ts, price, round(gross, 2), round(total_costs, 2),
                                       round(gross - total_costs, 2), why, tid))
                    del self.open[key]
                    held = None
                    events.append(f"{strat} sold {sym} at {price:,.2f} ({why}), net ${gross - total_costs:+,.2f}")
            if any(k[1] == sym for k in self.open):          # live value of what's still held
                await self.db._write("UPDATE long_trades SET last_price=?, last_ts=? WHERE symbol=? AND closed_at IS NULL",
                                     (price, bar_ts, sym))
        return events

    async def close_all(self, closes: pd.DataFrame, reason: str = "end of replay (marked at last price)") -> None:
        """Mark every open demo position at its last price, so a replay's comparison includes them (hold too)."""
        for (strat, sym), (tid, h, qty) in list(self.open.items()):
            if sym not in closes or closes[sym].dropna().empty:
                continue
            s = closes[sym].dropna()
            price, bar_ts = float(s.iloc[-1]), s.index[-1].timestamp()
            row = await self.db.fetchall("SELECT costs FROM long_trades WHERE id = ?", (tid,))
            total = (row[0][0] if row else 0.0) + sum(costs_for(sym).order_cost(qty, price, "sell"))
            gross = (price - h.entry) * qty
            await self.db._write("UPDATE long_trades SET closed_at=?, exit=?, gross_pnl=?, costs=?, net_pnl=?, "
                                 "exit_reason=? WHERE id=?", (bar_ts, price, round(gross, 2), round(total, 2),
                                                              round(gross - total, 2), reason, tid))
            del self.open[(strat, sym)]


# ---------------------------------------------------------------------------
# Side by side: the pairs demo book against each buy-only strategy
# ---------------------------------------------------------------------------
def comparison_rows(path: str, since: float = 0.0) -> list[tuple[str, int, int, float]]:
    """(approach, closed trades, won, net) for the pairs book and each buy-only strategy."""
    import sqlite3
    from pathlib import Path
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    rows = []
    try:
        for label, where in (("pairs, STRONG only", "grade = 'STRONG'"),
                             ("pairs, STRONG + RISKY", "grade IN ('STRONG', 'RISKY')")):
            try:
                n, w, net = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(net_pnl > 0), 0), COALESCE(SUM(net_pnl), 0) "
                                         f"FROM shadow_trades WHERE closed_at IS NOT NULL AND {where} AND opened_at >= ?",
                                         (since,)).fetchone()
                rows.append((label, n, w, net))
            except sqlite3.OperationalError:
                pass
        try:
            for strat, n, w, net in conn.execute(
                    "SELECT strategy, COUNT(*), COALESCE(SUM(net_pnl > 0), 0), COALESCE(SUM(net_pnl), 0) FROM long_trades "
                    "WHERE closed_at IS NOT NULL AND opened_at >= ? GROUP BY strategy", (since,)):
                rows.append((f"buy-only {strat}", n, w, net))
            for strat, n, unreal in conn.execute(
                    "SELECT strategy, COUNT(*), 0 FROM long_trades WHERE closed_at IS NULL GROUP BY strategy"):
                if not any(r[0] == f"buy-only {strat}" for r in rows):
                    rows.append((f"buy-only {strat} (still open)", 0, 0, 0.0))
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    return sorted(rows, key=lambda r: -r[3])


def comparison_text(path: str, since: float = 0.0) -> str:
    rows = comparison_rows(path, since)
    if not rows or all(r[1] == 0 for r in rows):
        return ("No closed demo trades to compare yet. The watcher trades the pairs book and the buy-only strategies "
                "(trend, dip, breakout, hold) with demo money on every scan; check back after a few days.")
    lines = ["Approaches compared on demo money, after costs (best first):"]
    for label, n, w, net in rows:
        lines.append(f"- {label}: {n} closed trades, {w / n:.0%} won, net ${net:+,.2f}" if n else f"- {label}: no closed trades yet")
    total = sum(r[1] for r in rows)
    lines.append("Buy-only 'hold' is the benchmark: an approach that can't beat it isn't worth running."
                 + (f" Only {total} trades so far: too few to pick a winner." if total < 100 else ""))
    lines.append("Past demo results, not a forecast.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI: compare on past prices
# ---------------------------------------------------------------------------
def _sim_closes(bars: int) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    idx = pd.date_range("2026-08-01", periods=bars, freq="15min", tz="UTC")
    drift = np.where(np.arange(bars) < bars // 2, 0.0004, -0.0003)        # up, then down
    out = {}
    for i, sym in enumerate(("BTCUSD", "ETHUSD", "GLD")):
        r = drift * (1 + 0.3 * i) + rng.normal(0, 0.004 if sym != "GLD" else 0.0015, bars)
        out[sym] = 100 * np.exp(np.cumsum(r))
    return pd.DataFrame(out, index=idx)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Compare buy-only strategies on past prices")
    ap.add_argument("--source", choices=("sim", "alpaca"), default="sim")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--symbols", nargs="+", default=["BTCUSD", "ETHUSD", "SOLUSD", "LTCUSD"])
    args = ap.parse_args(argv)
    if args.source == "sim":
        closes = _sim_closes(args.days * 96 + LOOKBACK)
    else:
        from config import load_settings
        from watcher import AlpacaSource, watch_settings
        crypto = [s for s in args.symbols if s in CRYPTO_SYMBOLS]
        stocks = [s for s in args.symbols if s not in CRYPTO_SYMBOLS]
        base = load_settings(mode="alpaca" if stocks else "sim")
        cfg = watch_settings(base, 15)
        src = AlpacaSource(cfg, {**({"crypto": crypto} if crypto else {}), **({"mixed": stocks} if stocks else {})})
        frames = []
        if crypto:
            frames.append(src._fetch_crypto(args.days * 96 + LOOKBACK))
        if stocks:
            frames.append(src._fetch_stocks(args.days * 26 + LOOKBACK))
        closes = pd.concat(frames, axis=1)
    trades = compare(closes, args.symbols)
    if trades.empty:
        print("No trades: not enough prices.")
        return
    print(f"Buy-only strategies, ${NOTIONAL:,.0f} per trade, costs included, {args.days} days:\n")
    tab = summary_table(trades)
    for strat, r in tab.iterrows():
        print(f"  {strat:<9} {int(r['trades']):>4} trades  {r['win_rate']:>4.0%} won  net ${r['net']:>+10,.2f}  "
              f"(${r['avg']:+,.2f} a trade)")
    print("\nBy symbol (net $):")
    print(trades.pivot_table(index="strategy", columns="symbol", values="net_pnl", aggfunc="sum").round(2).to_string())
    print("\nPast prices and demo money: a strategy that won here can lose next month. Not a forecast.")


if __name__ == "__main__":
    main()
