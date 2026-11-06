"""Risk layer for the buy-only strategies: how much to buy, and when not to buy at all.

A strategy only asks ("trend wants to buy BTC"). A ``Sleeve`` (one demo account per strategy, so they
are compared fairly) decides whether it may, and how much:

  size        risk-based: risk 1% of the sleeve on a trade, where the risk is two days of the
              coin's typical movement. Wild coins get smaller positions than calm ones.
  caps        at most 25% of the sleeve in one asset, 50% in crypto together (the coins tend to
              fall together), and 20% always kept in cash.
  drawdown    a state machine on the sleeve's value (closed + open) against its high point:
                normal    under 5% down       trade normally
                caution   5% to 10% down      half size
                defense   10% to 15% down     no new buys; open positions run their exits
                stopped   15% or more down    sell everything, no buys until reset
              A strategy never gets more risk because it is losing.

The same Sleeve runs the backtest and the live demo book, so what is tested is what runs.
Thresholds are fixed here before seeing results, not tuned afterwards.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

START_EQUITY = 10_000.0      # demo dollars per strategy
RISK_PER_TRADE = 0.01        # of sleeve equity
ADVERSE_DAYS = 2.0           # adverse move = this many days of typical movement
MAX_WEIGHT = 0.25            # of sleeve equity, per asset
MAX_CRYPTO = 0.50            # of sleeve equity, all crypto together
CASH_RESERVE = 0.20          # never invested
CAUTION, DEFENSE, STOPPED = 0.05, 0.10, 0.15
BARS_PER_DAY = 6             # 4-hour bars

STATES = ("normal", "caution", "defense", "stopped")


@dataclass
class Position:
    symbol: str
    qty: float
    entry: float
    peak: float
    bars: int = 0
    cost_in: float = 0.0
    last: float = 0.0
    regime: str = ""

    @property
    def value(self) -> float:
        return self.qty * (self.last or self.entry)


@dataclass
class Sleeve:
    strategy: str
    equity0: float = START_EQUITY
    realized: float = 0.0
    peak: float = START_EQUITY
    stopped: bool = False
    positions: dict[str, Position] = field(default_factory=dict)

    # -- value and state ------------------------------------------------------
    def value(self) -> float:
        return self.equity0 + self.realized + sum((p.last - p.entry) * p.qty - p.cost_in for p in self.positions.values())

    def drawdown(self) -> float:
        return max(0.0, 1 - self.value() / self.peak) if self.peak > 0 else 0.0

    def mark(self, prices: dict[str, float]) -> str:
        """Update open positions to the latest prices; returns the drawdown state."""
        for sym, p in self.positions.items():
            if sym in prices and not math.isnan(prices[sym]):
                p.last = prices[sym]
        v = self.value()
        self.peak = max(self.peak, v)
        if self.drawdown() >= STOPPED:
            self.stopped = True
        return self.state()

    def state(self) -> str:
        if self.stopped:
            return "stopped"
        dd = self.drawdown()
        return "defense" if dd >= DEFENSE else "caution" if dd >= CAUTION else "normal"

    def reset(self) -> None:
        """Manual restart after 'stopped': the current value becomes the new high point."""
        self.stopped = False
        self.peak = self.value()

    # -- sizing -----------------------------------------------------------------
    def size(self, symbol: str, price: float, day_vol: float, crypto: bool, benchmark: bool = False,
             n_assets: int = 1) -> tuple[float, str]:
        """(qty, why). qty 0 means the trade is refused, and why says which rule refused it."""
        state = self.state()
        cash_free = self.value() * (1 - CASH_RESERVE) - sum(p.value for p in self.positions.values())
        if benchmark:                                    # hold: same money, spread evenly, no risk rules
            notional = min(self.value() * (1 - CASH_RESERVE) / max(n_assets, 1), cash_free)
            return (notional / price, "benchmark: equal share") if notional > 0 else (0.0, "no cash left")
        if state in ("defense", "stopped"):
            return 0.0, f"refused: sleeve is in {state} ({self.drawdown():.0%} below its high)"
        adverse = max(day_vol, 0.002) * ADVERSE_DAYS * price
        qty = self.value() * RISK_PER_TRADE / adverse
        why = f"risk {RISK_PER_TRADE:.0%} of the sleeve over a {ADVERSE_DAYS * day_vol:.1%} adverse move"
        if state == "caution":
            qty /= 2
            why += ", halved (caution)"
        caps = [(self.value() * MAX_WEIGHT / price, f"capped at {MAX_WEIGHT:.0%} per asset"),
                (cash_free / price, "capped by cash (20% is kept back)")]
        if crypto:
            in_crypto = sum(p.value for p in self.positions.values() if p.symbol.endswith("USD"))
            caps.append(((self.value() * MAX_CRYPTO - in_crypto) / price, f"capped at {MAX_CRYPTO:.0%} in crypto"))
        for cap, reason in caps:
            if cap < qty:
                qty, why = max(cap, 0.0), reason
        if qty * price < 50:                             # too small to be worth the fees
            return 0.0, f"refused: {why} leaves less than $50"
        return qty, why
