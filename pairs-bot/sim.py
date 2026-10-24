"""Offline simulation: a synthetic cointegrated pair and a paper broker.

Used by tests and by ``BOT_MODE=sim`` so the whole system (bot + dashboard)
can run without Alpaca keys or market hours.

Price model
-----------
    log(B_t) = log(B_{t-1}) + mu + sigma_b * eps_t                (random walk)
    s_t      = s_{t-1} + theta * (0 - s_{t-1}) + sigma_s * eta_t   (Ornstein-Uhlenbeck spread)
    log(A_t) = alpha + beta * log(B_t) + s_t

so A and B are cointegrated with hedge ratio ``beta`` and a mean-reverting
spread whose half-life is ln(2)/theta bars. ``break_at`` turns theta to 0 from
that bar on, i.e. the relationship breaks, to exercise the stop logic.
"""

from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass, field

import pandas as pd

from execution import Account, EntrySkipped, Fill, Holding, OrderFailed, Quote


@dataclass
class SimMarket:
    symbol_a: str = "KO"
    symbol_b: str = "PEP"
    alpha: float = -0.9
    beta: float = 0.95
    theta: float = 0.05            # half-life ≈ 14 bars
    sigma_s: float = 0.002
    sigma_b: float = 0.004
    start_b: float = 170.0
    seed: int = 11
    break_at: int | None = None
    bar_seconds: float = 60.0
    t0: float = 1_760_000_000.0
    _rng: random.Random = field(init=False)
    _rows: list[tuple[float, float, float]] = field(init=False, default_factory=list)
    _log_b: float = field(init=False)
    _s: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self._log_b = math.log(self.start_b)

    def step(self) -> None:
        """Append one bar."""
        n = len(self._rows)
        theta = 0.0 if self.break_at is not None and n >= self.break_at else self.theta
        self._log_b += self._rng.gauss(0, self.sigma_b)
        self._s += -theta * self._s + self._rng.gauss(0, self.sigma_s)
        log_a = self.alpha + self.beta * self._log_b + self._s
        self._rows.append((self.t0 + n * self.bar_seconds, math.exp(log_a), math.exp(self._log_b)))

    def warm_up(self, bars: int) -> None:
        for _ in range(bars):
            self.step()

    @property
    def last(self) -> tuple[float, float, float]:
        return self._rows[-1]

    def closes(self, bars: int) -> pd.DataFrame:
        rows = self._rows[-bars:]
        idx = pd.to_datetime([r[0] for r in rows], unit="s", utc=True)
        return pd.DataFrame({self.symbol_a: [r[1] for r in rows], self.symbol_b: [r[2] for r in rows]},
                            index=idx)

    def price(self, symbol: str) -> float:
        _, a, b = self.last
        return a if symbol == self.symbol_a else b


class SimUniverse:
    """Several independent simulated pairs that move in lockstep (portfolio mode)."""

    def __init__(self, markets: list[SimMarket]) -> None:
        self.markets = markets
        self._by_symbol = {s: m for m in markets for s in (m.symbol_a, m.symbol_b)}

    @classmethod
    def for_pairs(cls, pairs, **kw) -> "SimUniverse":
        return cls([SimMarket(symbol_a=a, symbol_b=b, seed=11 + 7 * k, **kw) for k, (a, b) in enumerate(pairs)])

    def warm_up(self, bars: int) -> None:
        for m in self.markets:
            m.warm_up(bars)

    def market(self, symbol: str) -> SimMarket:
        return self._by_symbol[symbol]

    def price(self, symbol: str) -> float:
        return self._by_symbol[symbol].price(symbol)

    @property
    def last(self) -> tuple[float, float, float]:
        return max((m.last for m in self.markets), key=lambda r: r[0])


class SimMarketData:
    """MarketData implementation: every call advances that pair's simulated market one bar."""

    def __init__(self, market: SimMarket | SimUniverse, advance: bool = True) -> None:
        self.market = market
        self.advance = advance

    async def get_closes(self, symbols: tuple[str, str], bars: int) -> pd.DataFrame:
        m = self.market.market(symbols[0]) if isinstance(self.market, SimUniverse) else self.market
        if self.advance:
            m.step()
        return m.closes(bars)


@dataclass
class SimBroker:
    """Fills market orders instantly at the simulated bid/ask (the last price +/- half a spread).

    Limit orders that cross the quote fill at once. Passive ones (e.g. resting at
    the mid) fill with probability ``passive_fill_prob`` and, if they do, for
    ``passive_fill_ratio`` of the quantity, which lets tests exercise misses and
    partial fills.
    """

    market: SimMarket | SimUniverse
    starting_equity: float = 100_000.0
    half_spread_bps: float = 0.0
    passive_fill_prob: float = 1.0
    passive_fill_ratio: float = 1.0
    seed: int = 5
    shortable: set[str] | None = None          # None = everything shortable
    fail_symbols: set[str] = field(default_factory=set)   # orders on these are rejected
    cash: float = field(init=False)
    qty: dict[str, float] = field(default_factory=dict)
    avg: dict[str, float] = field(default_factory=dict)
    orders: list[Fill] = field(default_factory=list)
    is_open: bool = True
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def __post_init__(self) -> None:
        self.cash = self.starting_equity
        self._rng = random.Random(self.seed)

    async def quote(self, symbol: str) -> Quote:
        px = self.market.price(symbol)
        h = px * self.half_spread_bps / 1e4
        return Quote(px - h, px + h)

    def _fill(self, symbol: str, qty: float, side: str, px: float, tag: str) -> Fill:
        signed = qty if side == "buy" else -qty
        old = self.qty.get(symbol, 0.0)
        new = old + signed
        if old == 0 or (old > 0) == (signed > 0):   # opening or adding: update average price
            self.avg[symbol] = (abs(old) * self.avg.get(symbol, px) + qty * px) / abs(new)
        self.qty[symbol] = new
        self.cash -= signed * px
        fill = Fill(symbol, side, qty, px, f"{tag}-{next(self._ids)}", "filled")
        self.orders.append(fill)
        return fill

    async def limit_order(self, symbol: str, qty: float, side: str, limit_price: float,
                          client_order_id: str, timeout: float) -> Fill:
        if symbol in self.fail_symbols:
            raise OrderFailed(f"simulated rejection for {symbol}")
        q = await self.quote(symbol)
        if (side == "buy" and limit_price >= q.ask) or (side == "sell" and limit_price <= q.bid):
            return self._fill(symbol, qty, side, q.ask if side == "buy" else q.bid, "sim")
        if self._rng.random() >= self.passive_fill_prob:
            raise EntrySkipped(f"limit {side} {symbol} @ {limit_price:.2f} not filled within {timeout:.0f}s")
        filled = max(1, math.floor(qty * self.passive_fill_ratio))
        return self._fill(symbol, filled, side, limit_price, "sim")

    def _equity(self) -> float:
        return self.cash + sum(q * self.market.price(s) for s, q in self.qty.items())

    async def account(self) -> Account:
        return Account(self._equity(), self.starting_equity, self.cash, False)

    async def holdings(self) -> dict[str, Holding]:
        out = {}
        for s, q in self.qty.items():
            if q:
                px = self.market.price(s)
                out[s] = Holding(s, q, q * px, self.avg[s], px)
        return out

    async def market_open(self) -> bool:
        return self.is_open

    async def can_short(self, symbol: str) -> bool:
        return self.shortable is None or symbol in self.shortable

    async def market_order(self, symbol: str, qty: float, side: str, client_order_id: str) -> Fill:
        if symbol in self.fail_symbols:
            raise OrderFailed(f"simulated rejection for {symbol}")
        q = await self.quote(symbol)
        return self._fill(symbol, qty, side, q.ask if side == "buy" else q.bid, "sim")

    async def close_position(self, symbol: str) -> Fill | None:
        q = self.qty.get(symbol, 0.0)
        if not q:
            return None
        return await self.market_order(symbol, abs(q), "sell" if q > 0 else "buy", f"close-{symbol}")

    async def close_all(self) -> None:
        for s in list(self.qty):
            await self.close_position(s)
