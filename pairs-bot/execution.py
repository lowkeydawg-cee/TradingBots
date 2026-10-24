"""Order execution: dual-leg pair entries and exits on the Alpaca PAPER account.

Two layers:

* ``Broker`` is a thin async interface over the venue. ``AlpacaBroker`` wraps
  alpaca-py's ``TradingClient(paper=True)``; ``sim.SimBroker`` implements the
  same interface offline.
* ``PairExecutor`` holds the pair logic: sizing, leg ordering, the
  "if leg 2 fails, unwind leg 1" rule, and P&L on exit.

Sizing (beta-weighted dollar hedge)
-----------------------------------
The spread is log(A) - beta*log(B), so a 1% move in B is offset by a beta% move
in A. To make the position insensitive to moves the pair shares, the dollar
value of the B leg is beta times the dollar value of the A leg:

    qty_a = floor(leg_notional / price_a)
    qty_b = floor(beta * qty_a * price_a / price_b)

Whole shares only: Alpaca does not allow fractional short sales.

Leg risk
--------
Two orders cannot be atomic. The short leg is sent first because it is the one
most likely to be refused (no borrow). If the second leg fails, the first is
closed immediately and the attempt is reported as failed, so the bot never
sits on a naked single-leg position.

Cheaper entries (``ORDER_TYPE=limit``, the default)
---------------------------------------------------
Market orders pay half the bid/ask spread on every leg. With limit entries:

1. Read live quotes. If either stock's spread is wider than
   ``max_quote_spread_bps``, skip the trade: it would be too expensive.
2. Leg 1 (the short) rests at the **mid price** for up to
   ``limit_timeout_seconds``. If nobody takes it, it is cancelled and nothing
   was traded. A partial fill is kept and leg 2 is scaled to match.
3. Leg 2 is a *marketable* limit: it crosses the spread so it fills now, but
   never more than ``max_slippage_bps`` beyond the quote. Anything left over is
   topped up at market, and if leg 2 fails entirely, leg 1 is unwound.

Exits still use market orders: getting out reliably matters more than a cent.

Every fill records the price we expected (the mid, or the decision price for
market orders), so the database shows the slippage you actually paid. The
backtester can read it back with ``--costs-from-db``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from dataclasses import dataclass, replace
from typing import Protocol

from config import Settings
from strategy import Position, SpreadStats

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Broker interface
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Account:
    equity: float
    last_equity: float          # equity at the previous trading day's close
    cash: float
    trading_blocked: bool


@dataclass(frozen=True, slots=True)
class Holding:
    symbol: str
    qty: float                  # signed: negative = short
    market_value: float         # signed
    avg_entry_price: float
    current_price: float


@dataclass(frozen=True, slots=True)
class Fill:
    symbol: str
    side: str                   # "buy" / "sell"
    qty: float
    avg_price: float | None
    order_id: str | None
    status: str
    expected_price: float | None = None   # mid / decision price, to measure slippage


@dataclass(frozen=True, slots=True)
class Quote:
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 1e4 if self.mid > 0 else math.inf


class OrderFailed(RuntimeError):
    pass


class EntrySkipped(OrderFailed):
    """Nothing was traded: a passive order didn't fill or the quote was too wide. Normal, not an error."""


class Broker(Protocol):
    async def account(self) -> Account: ...
    async def holdings(self) -> dict[str, Holding]: ...
    async def market_open(self) -> bool: ...
    async def can_short(self, symbol: str) -> bool: ...
    async def market_order(self, symbol: str, qty: float, side: str, client_order_id: str) -> Fill: ...
    async def quote(self, symbol: str) -> Quote: ...
    async def limit_order(self, symbol: str, qty: float, side: str, limit_price: float,
                          client_order_id: str, timeout: float) -> Fill: ...
    async def close_position(self, symbol: str) -> Fill | None: ...
    async def close_all(self) -> None: ...


def tick(price: float) -> float:
    """Round to a valid US equity price increment ($0.01 above $1)."""
    return round(price, 2) if price >= 1 else round(price, 4)


class AlpacaBroker:
    """alpaca-py ``TradingClient`` pinned to the paper endpoint.

    alpaca-py is synchronous; every call runs in a worker thread so the event
    loop (and the bot's timers) are never blocked by HTTP.
    """

    FILL_TIMEOUT_S = 15.0

    def __init__(self, cfg: Settings) -> None:
        from alpaca.trading.client import TradingClient

        if cfg.paper is not True:  # defence in depth; Settings already refuses this
            raise RuntimeError("live trading is not supported")
        self._tc = TradingClient(cfg.api_key, cfg.secret_key, paper=True)
        self._cfg = cfg
        self._data = None                                    # created on first quote

    async def account(self) -> Account:
        a = await asyncio.to_thread(self._tc.get_account)
        return Account(float(a.equity), float(a.last_equity), float(a.cash),
                       bool(a.trading_blocked or a.account_blocked))

    async def holdings(self) -> dict[str, Holding]:
        positions = await asyncio.to_thread(self._tc.get_all_positions)
        return {p.symbol: Holding(p.symbol, float(p.qty), float(p.market_value),
                                  float(p.avg_entry_price), float(p.current_price or 0.0))
                for p in positions}

    async def market_open(self) -> bool:
        clock = await asyncio.to_thread(self._tc.get_clock)
        return bool(clock.is_open)

    async def can_short(self, symbol: str) -> bool:
        asset = await asyncio.to_thread(self._tc.get_asset, symbol)
        return bool(asset.tradable and asset.shortable and asset.easy_to_borrow)

    async def market_order(self, symbol: str, qty: float, side: str, client_order_id: str) -> Fill:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        req = MarketOrderRequest(symbol=symbol, qty=qty,
                                 side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                                 time_in_force=TimeInForce.DAY, client_order_id=client_order_id)
        try:
            order = await asyncio.to_thread(self._tc.submit_order, req)
        except Exception as exc:
            raise OrderFailed(f"{side} {qty} {symbol} rejected: {exc}") from exc
        return await self._await_fill(str(order.id), symbol, side, qty)

    async def quote(self, symbol: str) -> Quote:
        from alpaca.data.enums import DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest

        if self._data is None:
            self._data = StockHistoricalDataClient(self._cfg.api_key, self._cfg.secret_key)
        feed = DataFeed.IEX if self._cfg.data_feed == "iex" else DataFeed.SIP
        q = (await asyncio.to_thread(self._data.get_stock_latest_quote,
                                     StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=feed)))[symbol]
        bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            raise OrderFailed(f"no usable quote for {symbol} (bid {bid}, ask {ask})")
        return Quote(bid, ask)

    async def limit_order(self, symbol: str, qty: float, side: str, limit_price: float,
                          client_order_id: str, timeout: float) -> Fill:
        """Rest a limit order for up to ``timeout`` seconds, then cancel what is left.

        Returns what filled (possibly part of ``qty``); raises OrderFailed if nothing did.
        """
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        req = LimitOrderRequest(symbol=symbol, qty=qty, limit_price=tick(limit_price),
                                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                                time_in_force=TimeInForce.DAY, client_order_id=client_order_id)
        try:
            order = await asyncio.to_thread(self._tc.submit_order, req)
        except Exception as exc:
            raise OrderFailed(f"limit {side} {qty} {symbol} rejected: {exc}") from exc
        oid = str(order.id)
        deadline = time.monotonic() + timeout
        while True:
            o = await asyncio.to_thread(self._tc.get_order_by_id, oid)
            status = str(o.status.value if hasattr(o.status, "value") else o.status)
            if status == "filled":
                return Fill(symbol, side, float(o.filled_qty), float(o.filled_avg_price), oid, status)
            if status in ("canceled", "expired", "rejected", "suspended") or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.5)
        # Cancel, then read the final state: it may have filled (fully or partly) in the meantime.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self._tc.cancel_order_by_id, oid)
        for _ in range(10):
            o = await asyncio.to_thread(self._tc.get_order_by_id, oid)
            status = str(o.status.value if hasattr(o.status, "value") else o.status)
            if status in ("filled", "canceled", "expired", "rejected"):
                break
            await asyncio.sleep(0.3)
        filled = float(o.filled_qty or 0)
        if filled <= 0:
            raise EntrySkipped(f"limit {side} {symbol} @ {tick(limit_price)} not filled within {timeout:.0f}s")
        return Fill(symbol, side, filled, float(o.filled_avg_price), oid, status)

    async def _await_fill(self, order_id: str, symbol: str, side: str, qty: float) -> Fill:
        """Poll until the order reaches a final state. Market orders normally fill in < 1 s."""
        deadline = time.monotonic() + self.FILL_TIMEOUT_S
        while True:
            o = await asyncio.to_thread(self._tc.get_order_by_id, order_id)
            status = str(o.status.value if hasattr(o.status, "value") else o.status)
            if status == "filled":
                return Fill(symbol, side, float(o.filled_qty), float(o.filled_avg_price), order_id, status)
            if status in ("canceled", "expired", "rejected", "suspended"):
                raise OrderFailed(f"order {order_id} {status}")
            if time.monotonic() > deadline:
                raise OrderFailed(f"order {order_id} not filled within {self.FILL_TIMEOUT_S}s (status {status})")
            await asyncio.sleep(0.5)

    async def close_position(self, symbol: str) -> Fill | None:
        try:
            order = await asyncio.to_thread(self._tc.close_position, symbol)
        except Exception as exc:
            if "position does not exist" in str(exc).lower() or "404" in str(exc):
                return None
            raise OrderFailed(f"close {symbol} failed: {exc}") from exc
        side = str(order.side.value if hasattr(order.side, "value") else order.side)
        return await self._await_fill(str(order.id), symbol, side, float(order.qty or 0))

    async def close_all(self) -> None:
        await asyncio.to_thread(self._tc.close_all_positions, True)


# ---------------------------------------------------------------------------
# Pair logic
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PairPlan:
    direction: Position
    qty_a: int
    qty_b: int
    side_a: str
    side_b: str
    gross_notional: float
    price_a: float = 0.0        # decision prices: what we expect to pay before slippage
    price_b: float = 0.0


@dataclass(frozen=True, slots=True)
class PairFill:
    plan: PairPlan
    fill_a: Fill
    fill_b: Fill


def plan_pair(direction: Position, stats: SpreadStats, cfg: Settings) -> PairPlan:
    """Size both legs. Raises ValueError if the trade cannot be sized safely."""
    if direction is Position.FLAT:
        raise ValueError("cannot open FLAT")
    notional = cfg.leg_notional
    if cfg.risk_per_trade_usd > 0 and stats.spread_std > 0:
        # Volatility sizing: from entry to stop the spread moves about (stop_z - entry_z) * std
        # (in log terms, i.e. as a fraction of the A leg's value). Size so that move costs
        # about risk_per_trade_usd, never more than leg_notional.
        notional = min(notional, cfg.risk_per_trade_usd / ((cfg.stop_z - cfg.entry_z) * stats.spread_std))
    qty_a = math.floor(notional / stats.price_a)
    qty_b = math.floor(stats.beta * qty_a * stats.price_a / stats.price_b)
    if qty_a < 1 or qty_b < 1:
        raise ValueError(f"leg too small to trade whole shares (qty_a={qty_a}, qty_b={qty_b})")
    gross = qty_a * stats.price_a + qty_b * stats.price_b
    if gross > cfg.max_gross_exposure:
        raise ValueError(f"gross exposure {gross:.0f} above cap {cfg.max_gross_exposure:.0f}")
    # LONG_SPREAD: we expect log(A) - beta*log(B) to rise -> buy A, sell B.
    side_a, side_b = ("buy", "sell") if direction is Position.LONG_SPREAD else ("sell", "buy")
    return PairPlan(direction, qty_a, qty_b, side_a, side_b, gross, stats.price_a, stats.price_b)


def pair_pnl(direction: Position, qty_a: float, qty_b: float, entry_a: float, entry_b: float,
             exit_a: float, exit_b: float) -> float:
    """Realised P&L of a closed pair (before fees; Alpaca charges no commission on equities)."""
    sign = 1 if direction is Position.LONG_SPREAD else -1
    return sign * (qty_a * (exit_a - entry_a) - qty_b * (exit_b - entry_b))


class PairExecutor:
    def __init__(self, broker: Broker, cfg: Settings) -> None:
        self.broker = broker
        self.cfg = cfg

    async def open_pair(self, plan: PairPlan, trade_tag: str) -> PairFill:
        """Open both legs, short leg first. Unwinds leg 1 if leg 2 fails."""
        a, b = self.cfg.symbol_a, self.cfg.symbol_b
        legs = [(a, plan.qty_a, plan.side_a), (b, plan.qty_b, plan.side_b)]
        legs.sort(key=lambda leg: leg[2] != "sell")           # "sell" (the short) first
        short_symbol = legs[0][0]
        if not await self.broker.can_short(short_symbol):
            raise OrderFailed(f"{short_symbol} is not shortable / easy to borrow right now")

        decision = {a: plan.price_a, b: plan.price_b}
        if self.cfg.order_type == "limit":
            first, second = await self._open_limit(legs, decision, trade_tag)
        else:
            first = await self.broker.market_order(legs[0][0], legs[0][1], legs[0][2],
                                                   f"{trade_tag}-{legs[0][0]}-open")
            first = replace(first, expected_price=decision[first.symbol] or None)
            try:
                second = await self.broker.market_order(legs[1][0], legs[1][1], legs[1][2],
                                                        f"{trade_tag}-{legs[1][0]}-open")
            except Exception as exc:
                await self._unwind(first, exc)
            second = replace(second, expected_price=decision[second.symbol] or None)
        fills = {first.symbol: first, second.symbol: second}
        return PairFill(plan, fills[a], fills[b])

    async def _unwind(self, first: Fill, exc: Exception) -> None:
        log.error("second leg failed (%s); unwinding %s", exc, first.symbol)
        await self.broker.close_position(first.symbol)
        raise OrderFailed(f"leg 2 failed and leg 1 was unwound: {exc}") from exc

    async def _open_limit(self, legs: list[tuple[str, int, str]], decision: dict[str, float],
                          tag: str) -> tuple[Fill, Fill]:
        (s1, q1, side1), (s2, q2, side2) = legs
        quotes = {s: await self.broker.quote(s) for s in (s1, s2)}
        for s, q in quotes.items():
            if q.spread_bps > self.cfg.max_quote_spread_bps:
                raise EntrySkipped(f"{s} bid/ask spread {q.spread_bps:.1f} bps is wider than "
                                  f"{self.cfg.max_quote_spread_bps:g}; skipping")

        # Leg 1: passive at the mid. Not filled -> nothing traded, nothing to undo.
        mid1 = quotes[s1].mid
        first = await self.broker.limit_order(s1, q1, side1, mid1, f"{tag}-{s1}-open",
                                              self.cfg.limit_timeout_seconds)
        first = replace(first, expected_price=mid1)
        if first.qty < q1:                                   # partial: keep the hedge ratio
            q2 = math.floor(q2 * first.qty / q1)
            if q2 < 1:
                await self._unwind(first, OrderFailed("partial fill too small to hedge"))

        # Leg 2: marketable limit, capped; top up any remainder at market.
        try:
            qt = await self.broker.quote(s2)
            slip = self.cfg.max_slippage_bps / 1e4
            cap = qt.ask * (1 + slip) if side2 == "buy" else qt.bid * (1 - slip)
            try:
                second = await self.broker.limit_order(s2, q2, side2, cap, f"{tag}-{s2}-open", 5.0)
            except EntrySkipped:                         # price ran away: the market top-up below takes it all
                second = Fill(s2, side2, 0, None, "", "unfilled")
            if second.qty < q2:
                rest = await self.broker.market_order(s2, q2 - second.qty, side2, f"{tag}-{s2}-open-rest")
                total = second.qty + rest.qty
                avg = ((second.avg_price or 0) * second.qty + (rest.avg_price or 0) * rest.qty) / total
                second = replace(rest, qty=total, avg_price=avg, status="filled") if not second.qty else \
                    replace(second, qty=total, avg_price=avg, status="filled")
        except Exception as exc:
            await self._unwind(first, exc)
        return first, replace(second, expected_price=qt.mid)

    async def close_pair(self) -> tuple[Fill | None, Fill | None]:
        """Close both legs. Each leg is attempted even if the other fails."""
        results: list[Fill | None] = []
        errors: list[str] = []
        for sym in (self.cfg.symbol_a, self.cfg.symbol_b):
            try:
                results.append(await self.broker.close_position(sym))
            except Exception as exc:
                results.append(None)
                errors.append(f"{sym}: {exc}")
        if errors:
            raise OrderFailed("; ".join(errors))
        return results[0], results[1]

    async def flatten_all(self) -> None:
        """Emergency: cancel every open order and close every position on the account."""
        await self.broker.close_all()
