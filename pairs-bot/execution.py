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
Two market orders cannot be atomic. The short leg is sent first because it is
the one most likely to be refused (no borrow). If the second leg fails, the
first is closed immediately and the attempt is reported as failed, so the bot
never sits on a naked single-leg position.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
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


class OrderFailed(RuntimeError):
    pass


class Broker(Protocol):
    async def account(self) -> Account: ...
    async def holdings(self) -> dict[str, Holding]: ...
    async def market_open(self) -> bool: ...
    async def can_short(self, symbol: str) -> bool: ...
    async def market_order(self, symbol: str, qty: float, side: str, client_order_id: str) -> Fill: ...
    async def close_position(self, symbol: str) -> Fill | None: ...
    async def close_all(self) -> None: ...


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


@dataclass(frozen=True, slots=True)
class PairFill:
    plan: PairPlan
    fill_a: Fill
    fill_b: Fill


def plan_pair(direction: Position, stats: SpreadStats, cfg: Settings) -> PairPlan:
    """Size both legs. Raises ValueError if the trade cannot be sized safely."""
    if direction is Position.FLAT:
        raise ValueError("cannot open FLAT")
    qty_a = math.floor(cfg.leg_notional / stats.price_a)
    qty_b = math.floor(stats.beta * qty_a * stats.price_a / stats.price_b)
    if qty_a < 1 or qty_b < 1:
        raise ValueError(f"leg too small to trade whole shares (qty_a={qty_a}, qty_b={qty_b})")
    gross = qty_a * stats.price_a + qty_b * stats.price_b
    if gross > cfg.max_gross_exposure:
        raise ValueError(f"gross exposure {gross:.0f} above cap {cfg.max_gross_exposure:.0f}")
    # LONG_SPREAD: we expect log(A) - beta*log(B) to rise -> buy A, sell B.
    side_a, side_b = ("buy", "sell") if direction is Position.LONG_SPREAD else ("sell", "buy")
    return PairPlan(direction, qty_a, qty_b, side_a, side_b, gross)


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

        first = await self.broker.market_order(legs[0][0], legs[0][1], legs[0][2],
                                               f"{trade_tag}-{legs[0][0]}-open")
        try:
            second = await self.broker.market_order(legs[1][0], legs[1][1], legs[1][2],
                                                    f"{trade_tag}-{legs[1][0]}-open")
        except Exception as exc:
            log.error("second leg failed (%s); unwinding %s", exc, first.symbol)
            await self.broker.close_position(first.symbol)
            raise OrderFailed(f"leg 2 failed and leg 1 was unwound: {exc}") from exc
        fills = {first.symbol: first, second.symbol: second}
        return PairFill(plan, fills[a], fills[b])

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
