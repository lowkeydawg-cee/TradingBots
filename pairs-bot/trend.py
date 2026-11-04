"""Long-only trend: the other approach, for markets where pairs can't work.

    python trend.py --universe crypto metals --days 30        # replay past prices (funds need Alpaca keys)
    python trend.py --universe crypto                         # the same with no keys
    python trend.py --watch --universe crypto                 # keep demo positions on live prices

Alpaca can't short crypto, so pairs can't run there. This needs no short leg:
each asset stands alone, and it is either held or not.

    IN   the 1-day average price is above the 5-day average
    OUT  it is not

A decision uses bars up to t and fills at bar t+1, like the backtester, and
pays the same costs as the watcher (about 0.3% an order on crypto). Nothing is
ever sent to a broker: these are demo trades in the ``trend_trades`` table.

Jarvis reads them through ``trend_results``, next to the pairs demo trades of
the same markets, so the two approaches can be compared on the same weeks.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from backtest import Costs
from config import ConfigError, load_settings
from db_logger import BUSY_TIMEOUT_MS, connect_readonly
from scanner import ALL_SECTORS, CRYPTO, CRYPTO_SYMBOLS, MARKET_OF
from watcher import CRYPTO_COSTS, AlpacaSource, watch_settings

log = logging.getLogger("trend")

NOTIONAL = 2_000.0          # demo dollars per asset, the same as one pairs leg
FAST_DAYS, SLOW_DAYS = 1, 5
MIN_TRADES = 100            # fewer than this and the comparison is mostly luck

SCHEMA = """
CREATE TABLE IF NOT EXISTS trend_trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    market      TEXT NOT NULL,
    opened_at   REAL NOT NULL,        -- bar time
    entry_price REAL NOT NULL,
    qty         REAL NOT NULL,
    closed_at   REAL,                 -- NULL while the position is open
    exit_price  REAL,
    gross_pnl   REAL,
    costs       REAL NOT NULL,        -- entry cost while open, both orders once closed
    net_pnl     REAL
);
"""

MARKET_OF_SYMBOL = {s: MARKET_OF[sector] for sector, syms in ALL_SECTORS.items() for s in syms}


def costs_for(symbol: str) -> Costs:
    return CRYPTO_COSTS if symbol in CRYPTO_SYMBOLS else Costs()


def bars_per_day(symbol: str, timeframe: int) -> int:
    return round((1440 if symbol in CRYPTO_SYMBOLS else 390) / timeframe)


def wants_long(closes: np.ndarray, fast: int, slow: int) -> bool:
    """True when the fast average is above the slow one. False until there are enough bars."""
    return len(closes) >= slow and float(closes[-fast:].mean()) > float(closes[-slow:].mean())


def order_cost(costs: Costs, qty: float, price: float, side: str) -> float:
    return sum(costs.order_cost(qty, price, side))


@dataclass
class TrendTrade:
    opened_at: float
    entry_price: float
    qty: float
    closed_at: float
    exit_price: float
    gross_pnl: float
    costs: float

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.costs


def replay(closes: pd.Series, start: float, fast: int, slow: int, costs: Costs,
           notional: float = NOTIONAL) -> list[TrendTrade]:
    """Every trade from `start` on. A position still open at the end is closed at the last price."""
    closes = closes.dropna()
    prices = closes.to_numpy(float)
    times = [t.timestamp() for t in closes.index]
    trades: list[TrendTrade] = []
    entry: tuple[float, float, float] | None = None       # opened_at, price, qty

    def close(i: int) -> None:
        nonlocal entry
        opened, price, qty = entry
        cost = order_cost(costs, qty, price, "buy") + order_cost(costs, qty, prices[i], "sell")
        trades.append(TrendTrade(opened, price, qty, times[i], float(prices[i]), qty * (prices[i] - price), cost))
        entry = None

    for i in range(1, len(prices)):
        if times[i] < start:
            continue
        want = wants_long(prices[:i], fast, slow)         # bars up to i-1, filled at bar i
        if want and entry is None:
            entry = (times[i], float(prices[i]), notional / prices[i])
        elif not want and entry is not None:
            close(i)
    if entry is not None:
        close(len(prices) - 1)
    return trades


def buy_and_hold(closes: pd.Series, start: float, costs: Costs, notional: float = NOTIONAL) -> float:
    """Net result of buying at `start` and selling at the end: the bar any active approach has to beat."""
    closes = closes.dropna()
    held = closes[[t.timestamp() >= start for t in closes.index]]
    if len(held) < 2:
        return 0.0
    first, last = float(held.iloc[0]), float(held.iloc[-1])
    qty = notional / first
    return qty * (last - first) - order_cost(costs, qty, first, "buy") - order_cost(costs, qty, last, "sell")


class TrendBook:
    """The live demo positions, kept in the bot's database so a restart picks them up."""

    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
        self.conn.executescript(SCHEMA)
        self.open = {r[0]: r[1:] for r in self.conn.execute(
            "SELECT symbol, id, entry_price, qty, costs FROM trend_trades WHERE closed_at IS NULL")}

    def step(self, symbol: str, closes: pd.Series, fast: int, slow: int, costs: Costs) -> str | None:
        """Decide on the bars before the last one and fill at the last one. Returns 'opened', 'closed' or None."""
        closes = closes.dropna()
        if len(closes) < 2:
            return None
        price, bar_time = float(closes.iloc[-1]), closes.index[-1].timestamp()
        want = wants_long(closes.iloc[:-1].to_numpy(float), fast, slow)
        if want and symbol not in self.open:
            qty = NOTIONAL / price
            cost = order_cost(costs, qty, price, "buy")
            cur = self.conn.execute(
                "INSERT INTO trend_trades (symbol, market, opened_at, entry_price, qty, costs) VALUES (?,?,?,?,?,?)",
                (symbol, MARKET_OF_SYMBOL.get(symbol, "Other"), bar_time, price, qty, cost))
            self.conn.commit()
            self.open[symbol] = (cur.lastrowid, price, qty, cost)
            return "opened"
        if not want and symbol in self.open:
            row, entry, qty, cost = self.open.pop(symbol)
            gross = qty * (price - entry)
            total = cost + order_cost(costs, qty, price, "sell")
            self.conn.execute("UPDATE trend_trades SET closed_at=?, exit_price=?, gross_pnl=?, costs=?, net_pnl=? "
                              "WHERE id=?", (bar_time, price, gross, total, gross - total, row))
            self.conn.commit()
            return "closed"
        return None

    def close(self) -> None:
        self.conn.close()


def trend_results(path: str) -> str:
    """Trend demo results by market, beside the pairs demo trades of the same market."""
    conn = connect_readonly(path)
    try:
        try:
            closed = conn.execute("SELECT market, COUNT(*), SUM(net_pnl > 0), SUM(net_pnl) FROM trend_trades "
                                  "WHERE closed_at IS NOT NULL GROUP BY market").fetchall()
            held = conn.execute("SELECT symbol FROM trend_trades WHERE closed_at IS NULL ORDER BY symbol").fetchall()
        except sqlite3.OperationalError:
            closed, held = [], []
        try:
            shadow = conn.execute("SELECT pair, net_pnl FROM shadow_trades WHERE closed_at IS NOT NULL").fetchall()
        except sqlite3.OperationalError:
            shadow = []
    finally:
        conn.close()
    if not closed and not held:
        return ("No trend demo trades yet. Start them with: python trend.py --watch --universe crypto "
                "(crypto needs no Alpaca keys; gold and other funds do).")
    pairs: dict[str, list[float]] = {}
    for pair, net in shadow:
        market = MARKET_OF_SYMBOL.get(pair.split("/")[0])
        if market:                                         # simulated pairs have no real market
            pairs.setdefault(market, []).append(net)
    out = []
    for market, n, won, net in closed:
        line = f"{market}: trend {n} closed trades, {won / n:.0%} won, net ${net:+,.2f}"
        if market in pairs:
            line += f"; pairs {len(pairs[market])} demo trades, net ${sum(pairs[market]):+,.2f}"
        out.append(line + ".")
    if held:
        out.append("Holding now: " + ", ".join(r[0] for r in held) + ".")
    total = sum(r[1] for r in closed)
    if total < MIN_TRADES:
        out.append(f"Only {total} closed trend trades so far: too few to say which approach is better.")
    out.append("Demo trades only, measured on the past, not a forecast.")
    return "\n".join(out)


def replay_report(closes: pd.DataFrame, sectors: dict[str, list[str]], start: float, timeframe: int) -> str:
    lines = [f"{'Asset':<10}{'Trades':>7}{'Won':>6}{'Trend net':>12}{'Buy and hold':>14}"]
    totals: dict[str, list[float]] = {}
    for sector, symbols in sectors.items():
        for symbol in symbols:
            if symbol not in closes:
                continue
            per_day = bars_per_day(symbol, timeframe)
            trades = replay(closes[symbol], start, FAST_DAYS * per_day, SLOW_DAYS * per_day, costs_for(symbol))
            net = sum(t.net_pnl for t in trades)
            hold = buy_and_hold(closes[symbol], start, costs_for(symbol))
            won = sum(t.net_pnl > 0 for t in trades)
            lines.append(f"{symbol:<10}{len(trades):>7}{won:>6}{net:>+12,.2f}{hold:>+14,.2f}")
            total = totals.setdefault(MARKET_OF[sector], [0.0, 0.0])
            total[0] += net
            total[1] += hold
    lines += [f"{market}: trend ${t:+,.2f}, buy and hold ${h:+,.2f}" for market, (t, h) in totals.items()]
    lines.append("Compare with pairs on the same prices: python timing.py --replay")
    return "\n".join(lines)


async def amain(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Long-only trend demo: replay past prices or keep live demo positions")
    ap.add_argument("--universe", nargs="+", choices=sorted(ALL_SECTORS), default=["crypto"])
    ap.add_argument("--days", type=int, default=30, help="days to replay")
    ap.add_argument("--watch", action="store_true", help="keep demo positions on live prices instead of replaying")
    ap.add_argument("--timeframe", type=int, default=15, help="bar size in minutes (default 15)")
    ap.add_argument("--scans", type=int, help="stop after N scans")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    sectors = {k: ALL_SECTORS[k] for k in args.universe}
    crypto_only = set(sectors) <= set(CRYPTO)              # Alpaca's crypto prices need no keys
    try:
        base = load_settings(mode="sim" if crypto_only else "alpaca")
    except ConfigError as exc:
        raise SystemExit(f"{exc}\nFunds need Alpaca paper keys in .env. Without keys you can still run crypto: "
                         "python trend.py --universe crypto")
    source = AlpacaSource(watch_settings(base, args.timeframe), sectors)
    day = round(1440 / args.timeframe)

    if not args.watch:
        closes = await source.closes((args.days + SLOW_DAYS + 2) * day)
        start = (datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp()
        print(replay_report(closes, sectors, start, args.timeframe))
        return

    book = TrendBook(base.db_path)
    log.info("trend demo on %d assets, holding %s", sum(map(len, sectors.values())), sorted(book.open) or "nothing")
    scans = 0
    try:
        while args.scans is None or scans < args.scans:
            if await source.market_open():
                closes = await source.closes((SLOW_DAYS + 2) * day)
                for symbols in source.active_sectors.values():
                    for symbol in symbols:
                        if symbol not in closes:
                            continue
                        per_day = bars_per_day(symbol, args.timeframe)
                        action = book.step(symbol, closes[symbol], FAST_DAYS * per_day, SLOW_DAYS * per_day,
                                           costs_for(symbol))
                        if action:
                            log.info("%s %s", action, symbol)
                scans += 1
            if args.scans is None or scans < args.scans:
                await asyncio.sleep(args.timeframe * 60)
    finally:
        book.close()


if __name__ == "__main__":
    asyncio.run(amain())
