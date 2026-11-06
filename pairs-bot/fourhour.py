"""Slower challengers: Trend and Breakout on 4-hour bars, with a market gate and volatility sizing.

    python fourhour.py                # keep demo positions on live prices (crypto, no keys needed)
    python fourhour.py --replay       # the same rules on all past prices, next to buy and hold

On 15-minute bars costs ate the buy-only strategies, so these run the same
``longonly.decide()`` rules on closed 4-hour bars, with two additions:

    market gate   only buy when the last closed day is above its 50-day average
    vol sizing    trade less than $2,000 when a coin moves more than 3% a day

They write to ``long_trades`` as ``trend4h`` and ``breakout4h``, so the
Approaches page scores them next to the originals. Selling is never gated.
Nothing is ever sent to a broker: demo money only.

Past prices can hold holes. Alpaca's SOL/USD has a 417-day gap ending in August
2024 with moves of +683% in one bar, and a position held across it books a
fake profit. ``usable()`` drops everything before the last gap or impossible
move, so these rules only ever see clean history.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from db_logger import BUSY_TIMEOUT_MS, SCHEMA
from longonly import LONG_SYMBOLS, LOOKBACK, NOTIONAL, Holding, costs_for, decide, market_of
from scanner import CRYPTO_SYMBOLS, alpaca_crypto

log = logging.getLogger("fourhour")

CHALLENGERS = {"trend4h": "trend", "breakout4h": "breakout"}
SYMBOLS = tuple(s for s in LONG_SYMBOLS if s in CRYPTO_SYMBOLS)
BAR = timedelta(hours=4)
GATE_DAYS = 50              # fixed before testing, not tuned
VOL_DAYS = 20
TARGET_DAILY_VOL = 0.03
MAX_GAP = timedelta(days=2)
MAX_MOVE = 0.5              # a 50% move in one 4-hour bar is a data error, not a trade
HISTORY_DAYS = 120          # enough for the gate and for LOOKBACK // 2 bars
POLL_SECONDS = 900


def usable(closes: pd.Series) -> pd.Series:
    """The closes after the last gap or impossible move: everything before it can't be trusted."""
    s = closes.dropna()
    if len(s) < 2:
        return s
    bad = (s.index.to_series().diff() > MAX_GAP) | (s.pct_change().abs() > MAX_MOVE)
    return s[s.index >= bad[bad].index[-1]] if bad.any() else s


def closed_bars(closes: pd.Series, now: datetime) -> pd.Series:
    """Only bars that have finished: a signal from a bar still forming can vanish before it closes."""
    return closes[closes.index + BAR <= now]


def daily(closes: pd.Series) -> pd.Series:
    """Closed days only: the day still in progress is left out."""
    d = closes.resample("1D").last().dropna()
    return d[d.index + timedelta(days=1) <= closes.index[-1] + BAR]


def market_open_to_buy(closes: pd.Series) -> bool:
    d = daily(closes)
    return len(d) >= GATE_DAYS and float(d.iloc[-1]) > float(d.iloc[-GATE_DAYS:].mean())


def position_size(closes: pd.Series) -> float:
    """$2,000 at 3% daily volatility or less, scaled down when the coin moves more."""
    moves = np.log(daily(closes)).diff().dropna().tail(VOL_DAYS)
    if len(moves) < VOL_DAYS:
        return NOTIONAL
    return NOTIONAL * min(1.0, TARGET_DAILY_VOL / max(float(moves.std()), 1e-9))


def step(strategy: str, closes: pd.Series, held: Holding | None) -> tuple[str | None, str, float]:
    """(action, why, dollars) from the bars before the last one. The caller fills at the last bar."""
    seen = closes.iloc[:-1]
    x = seen.to_numpy(float)[-LOOKBACK:]
    if len(x) < LOOKBACK // 2:
        return None, "", 0.0
    action, why = decide(CHALLENGERS[strategy], x, held)
    if action != "buy":
        return action, why, 0.0
    if not market_open_to_buy(seen):
        return None, f"blocked: below its {GATE_DAYS}-day average", 0.0
    return "buy", why, position_size(seen)


class Book:
    """The demo positions, in the bot's own table. Reopening it picks them up again."""

    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
        self.conn.executescript(SCHEMA)
        marks = ",".join("?" * len(CHALLENGERS))
        self.open = {(r[1], r[2]): r for r in self.conn.execute(
            f"SELECT id, strategy, symbol, opened_at, entry, qty, costs FROM long_trades "
            f"WHERE closed_at IS NULL AND strategy IN ({marks})", tuple(CHALLENGERS))}

    def holding(self, strategy: str, symbol: str, closes: pd.Series) -> Holding | None:
        row = self.open.get((strategy, symbol))
        if row is None:
            return None
        since = closes[closes.index >= pd.Timestamp(row[3], unit="s", tz="UTC")].iloc[:-1]
        return Holding(row[4], max([row[4], *since.to_numpy(float)]), len(since))

    def update(self, symbol: str, closes: pd.Series) -> list[str]:
        """One decision per strategy on the newest closed bar. Safe to call again on the same bar."""
        s = usable(closes)
        if len(s) < 2:
            return []
        price, bar_ts, costs = float(s.iloc[-1]), s.index[-1].timestamp(), costs_for(symbol)
        events = []
        for strategy in CHALLENGERS:
            row = self.open.get((strategy, symbol))
            if row and bar_ts <= row[3]:
                continue
            action, why, dollars = step(strategy, s, self.holding(strategy, symbol, s))
            if action == "buy" and row is None:
                if self.conn.execute("SELECT 1 FROM long_trades WHERE strategy=? AND symbol=? AND closed_at=?",
                                     (strategy, symbol, bar_ts)).fetchone():
                    continue                                 # sold on this very bar: wait for the next one
                qty = dollars / price
                cost = sum(costs.order_cost(qty, price, "buy"))
                note = f"{why}; ${dollars:,.0f}"
                cur = self.conn.execute(
                    "INSERT INTO long_trades (strategy, symbol, market, opened_at, entry, qty, entry_note, costs) "
                    "VALUES (?,?,?,?,?,?,?,?)", (strategy, symbol, market_of(symbol), bar_ts, price, qty, note, cost))
                self.conn.commit()
                self.open[(strategy, symbol)] = (cur.lastrowid, strategy, symbol, bar_ts, price, qty, cost)
                events.append(f"{strategy} bought {symbol} at {price:,.2f} ({note})")
            elif action == "sell" and row is not None:
                tid, _, _, _, entry, qty, paid = row
                total = paid + sum(costs.order_cost(qty, price, "sell"))
                gross = (price - entry) * qty
                self.conn.execute("UPDATE long_trades SET closed_at=?, exit=?, gross_pnl=?, costs=?, net_pnl=?, "
                                  "exit_reason=? WHERE id=?", (bar_ts, price, round(gross, 2), round(total, 2),
                                                               round(gross - total, 2), why, tid))
                self.conn.commit()
                del self.open[(strategy, symbol)]
                events.append(f"{strategy} sold {symbol} at {price:,.2f} ({why}), net ${gross - total:+,.2f}")
        return events

    def close(self) -> None:
        self.conn.close()


def replay(closes: pd.Series, strategy: str, symbol: str) -> list[float]:
    """Net result of each trade over all clean history, with next-bar fills."""
    s = usable(closes)
    costs = costs_for(symbol)
    nets: list[float] = []
    held: Holding | None = None
    qty = 0.0
    for i in range(LOOKBACK // 2 + 1, len(s)):
        window = s.iloc[max(0, i - LOOKBACK - 1):i + 1] if held is not None else s.iloc[:i + 1]
        action, _, dollars = step(strategy, window, held)
        price = float(s.iloc[i])
        if action == "buy" and held is None:
            qty = dollars / price
            held = Holding(price, price)
        elif action == "sell" and held is not None:
            nets.append(qty * (price - held.entry) - sum(costs.order_cost(qty, held.entry, "buy"))
                        - sum(costs.order_cost(qty, price, "sell")))
            held = None
        elif held is not None:
            held.bars += 1
            held.peak = max(held.peak, price)
    if held is not None:
        price = float(s.iloc[-1])
        nets.append(qty * (price - held.entry) - sum(costs.order_cost(qty, held.entry, "buy"))
                    - sum(costs.order_cost(qty, price, "sell")))
    return nets


def fetch(days: int) -> pd.DataFrame:
    from alpaca.data.historical import CryptoHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    req = CryptoBarsRequest(symbol_or_symbols=[alpaca_crypto(s) for s in SYMBOLS],
                            start=datetime.now(timezone.utc) - timedelta(days=days),
                            timeframe=TimeFrame(4, TimeFrameUnit.Hour))
    df = CryptoHistoricalDataClient().get_crypto_bars(req).df
    closes = df["close"].unstack(level="symbol").sort_index()
    return closes.rename(columns=lambda c: c.replace("/", ""))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Trend and Breakout on 4-hour bars with a market gate: demo money only")
    ap.add_argument("--replay", action="store_true", help="run the rules on past prices instead of watching")
    ap.add_argument("--days", type=int, default=1825, help="days of history for --replay")
    ap.add_argument("--db", default="pairs_bot.db")
    ap.add_argument("--scans", type=int, help="stop after N checks")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    if args.replay:
        closes = fetch(args.days)
        for strategy in CHALLENGERS:
            nets = [n for s in SYMBOLS if s in closes for n in replay(closes[s], strategy, s)]
            print(f"{strategy:<12}{len(nets):>5} trades {sum(n > 0 for n in nets):>4} won  net ${sum(nets):+,.2f}")
        hold = sum(NOTIONAL * (usable(closes[s]).iloc[-1] / usable(closes[s]).iloc[0] - 1) for s in SYMBOLS if s in closes)
        print(f"{'hold':<12}{'':>23}net ${hold:+,.2f}")
        print("Past prices, demo money, not a forecast.")
        return

    book = Book(args.db)
    log.info("4-hour challengers on %s, holding %s", ", ".join(SYMBOLS), sorted(book.open) or "nothing")
    scans = 0
    try:
        while args.scans is None or scans < args.scans:
            try:
                closes = fetch(HISTORY_DAYS)
                now = datetime.now(timezone.utc)
                for symbol in SYMBOLS:
                    if symbol in closes:
                        for event in book.update(symbol, closed_bars(closes[symbol].dropna(), now)):
                            log.info(event)
            except Exception as exc:                         # one failed fetch must not end the demo
                log.warning("prices unavailable: %s", exc)
            scans += 1
            if args.scans is None or scans < args.scans:
                time.sleep(POLL_SECONDS)
    finally:
        book.close()


if __name__ == "__main__":
    main()
