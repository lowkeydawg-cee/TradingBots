"""Position sizing and pre-trade safety evaluation.

The engine only ever looks at the bot's own account: equity, drawdown, loss
streak and the volatility of its own per-round returns. It never reads crash
points, so it cannot be turned into an outcome predictor.

Sizing pipeline (each step can only shrink the stake):
    1. fractional risk      stake = equity * base_risk_fraction
    2. volatility scaling   * min(1, vol_target / realized_vol_of_own_returns)
    3. drawdown de-risking  * (1 - drawdown / max_daily_drawdown)
    4. loss-streak scaling  * 1 / (1 + consecutive_losses)
       (steps 2-4 are each floored at ``min_scale``)
    5. hard caps            min(max_stake_fraction * equity, max_stake_abs,
                                remaining daily drawdown budget)
    6. anti-martingale      never above the previous stake after a loss
    7. floor to cents; deny if below min_stake
"""

from __future__ import annotations

import enum
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import ALLOW_STAKE_INCREASE_AFTER_LOSS, RiskLimits
from .models import Settlement


class Breach(enum.StrEnum):
    DAILY_DRAWDOWN = "daily_drawdown"      # -> CIRCUIT_BREAKER
    LOSS_STREAK = "loss_streak"            # -> COOLING_DOWN
    SESSION_BET_LIMIT = "session_bet_limit"  # -> graceful IDLE
    BANKRUPT = "bankrupt"                  # -> CIRCUIT_BREAKER


@dataclass(frozen=True, slots=True)
class Decision:
    approved: bool
    stake: float
    reason: str
    factors: dict[str, float] = field(default_factory=dict)


def _floor_cents(x: float) -> float:
    return math.floor(x * 100 + 1e-9) / 100


class RiskEngine:
    def __init__(self, limits: RiskLimits, clock: Callable[[], float] = time.time) -> None:
        self.limits = limits
        self._clock = clock
        self.equity = limits.starting_bankroll
        self.day_start_equity = limits.starting_bankroll
        self._day = self._today()
        self.consecutive_losses = 0
        self.bets = 0
        self.wins = 0
        self.losses = 0
        self.last_stake: float | None = None
        self.last_was_loss = False
        self._returns: deque[float] = deque(maxlen=limits.vol_lookback_rounds)

    # -- derived metrics -------------------------------------------------
    def _today(self) -> int:
        # UTC day number; cheaper than building a datetime on every decision.
        return int(self._clock() // 86_400)

    def _roll_day(self) -> None:
        """Reset the daily drawdown baseline at UTC midnight.

        A tripped circuit breaker is NOT cleared by this; it is absorbing.
        """
        today = self._today()
        if today != self._day:
            self._day = today
            self.day_start_equity = self.equity

    @property
    def drawdown(self) -> float:
        """Fractional loss versus the day's opening equity (0 when in profit)."""
        return max(0.0, (self.day_start_equity - self.equity) / self.day_start_equity)

    @property
    def drawdown_budget(self) -> float:
        """Further loss allowed before the daily limit is reached."""
        floor = self.day_start_equity * (1.0 - self.limits.max_daily_drawdown_pct)
        return max(0.0, self.equity - floor)

    @property
    def realized_vol(self) -> float | None:
        n = len(self._returns)
        if n < self.limits.vol_min_samples:
            return None
        # Plain float population std-dev. statistics.pstdev uses exact rational
        # arithmetic and costs ~100x more, which matters on the hot path.
        mean = math.fsum(self._returns) / n
        return math.sqrt(math.fsum((r - mean) ** 2 for r in self._returns) / n)

    # -- pre-trade -------------------------------------------------------
    def evaluate(self) -> Decision:
        """Size the next bet. Pure with respect to account state."""
        self._roll_day()
        lim = self.limits
        if self.equity <= 0:
            return Decision(False, 0.0, "no_equity")
        if self.bets >= lim.max_bets_per_session:
            return Decision(False, 0.0, "session_bet_limit")
        if self.drawdown >= lim.max_daily_drawdown_pct:
            return Decision(False, 0.0, "daily_drawdown_reached")
        if self.consecutive_losses >= lim.max_consecutive_losses:
            return Decision(False, 0.0, "loss_streak_reached")

        fractional = self.equity * lim.base_risk_fraction
        vol = self.realized_vol
        vol_scale = 1.0 if not vol else max(lim.min_scale, min(1.0, lim.vol_target / vol))
        dd_scale = max(lim.min_scale, 1.0 - self.drawdown / lim.max_daily_drawdown_pct)
        streak_scale = max(lim.min_scale, 1.0 / (1.0 + self.consecutive_losses))
        raw = fractional * vol_scale * dd_scale * streak_scale

        cap = min(self.equity * lim.max_stake_fraction, lim.max_stake_abs, self.drawdown_budget)
        stake = min(raw, cap)

        # Anti-martingale: after a loss the stake may only stay flat or shrink.
        if self.last_was_loss and self.last_stake is not None and stake > self.last_stake:
            stake = self.last_stake
        stake = _floor_cents(stake)

        factors = {
            "equity": round(self.equity, 2), "fractional": round(fractional, 4),
            "vol_scale": round(vol_scale, 4), "dd_scale": round(dd_scale, 4),
            "streak_scale": round(streak_scale, 4), "cap": round(cap, 4),
            "drawdown": round(self.drawdown, 6),
        }
        if stake < lim.min_stake:
            return Decision(False, stake, "below_min_stake", factors)

        # Defence in depth: explicit checks (not ``assert``, which ``python -O``
        # strips). Any violation denies the bet rather than raising.
        eps = 1e-9
        if stake > lim.max_stake_abs or stake > self.equity * lim.max_stake_fraction + eps:
            return Decision(False, stake, "invariant_violation:stake_cap", factors)
        if stake > self.drawdown_budget + eps:
            return Decision(False, stake, "invariant_violation:drawdown_budget", factors)
        if (not ALLOW_STAKE_INCREASE_AFTER_LOSS and self.last_was_loss
                and self.last_stake is not None and stake > self.last_stake + eps):
            return Decision(False, stake, "invariant_violation:martingale", factors)
        return Decision(True, stake, "approved", factors)

    # -- post-trade ------------------------------------------------------
    def record_settlement(self, s: Settlement) -> list[Breach]:
        """Apply a settled (or worst-case booked) bet and report limit breaches."""
        self._roll_day()
        equity_before = self.equity
        self.equity = round(self.equity + s.pnl, 2)
        self.bets += 1
        self.last_stake = s.stake
        self.last_was_loss = not s.won
        if s.won:
            self.wins += 1
            self.consecutive_losses = 0
        else:
            self.losses += 1
            self.consecutive_losses += 1
        if equity_before > 0:
            self._returns.append(s.pnl / equity_before)

        breaches: list[Breach] = []
        if self.equity <= 0:
            breaches.append(Breach.BANKRUPT)
        # Also a breach when the remaining budget is too small for any legal bet.
        if (self.drawdown >= self.limits.max_daily_drawdown_pct - 1e-12
                or self.drawdown_budget < self.limits.min_stake):
            breaches.append(Breach.DAILY_DRAWDOWN)
        if self.consecutive_losses >= self.limits.max_consecutive_losses:
            breaches.append(Breach.LOSS_STREAK)
        if self.bets >= self.limits.max_bets_per_session:
            breaches.append(Breach.SESSION_BET_LIMIT)
        return breaches

    def restore(self, day_start_equity: float, equity: float) -> None:
        """Resume today's baseline and equity from earlier sessions."""
        self.day_start_equity = day_start_equity
        self.equity = equity
        self._day = self._today()

    def reset_streak_after_cooldown(self) -> None:
        """Called when a cool-down completes. Stake stays capped by last_stake."""
        self.consecutive_losses = 0
