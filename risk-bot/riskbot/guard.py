"""Independent pre-trade guard (second line of defence).

``GuardedExecutor`` wraps any ``Executor``. It shares no state with
``RiskEngine``: it keeps its own order ledger and enforces hard limits right
before an order leaves the process. If the risk engine, the FSM or the gate in
``bot_core`` had a bug, this layer still refuses:

    * a kill-switch file is present
    * a non-finite, non-positive or oversized stake
    * a second order for the same round
    * more than N orders in any rolling 60 s window
    * an order whose loss would push the guard's own session-loss ledger past
      its ceiling (open stake is counted as lost until settled)

A refusal raises ``GuardViolation``. The bot treats that as a trip condition,
because reaching this layer with a bad order means an upstream check failed.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from .config import GuardLimits
from .executor import Executor, Receipt


class GuardViolation(Exception):
    """An order reached the outer guard and broke a hard limit."""


class GuardedExecutor:
    def __init__(self, inner: Executor, limits: GuardLimits,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._inner = inner
        self.limits = limits
        self._clock = clock
        self._rounds: set[int] = set()
        self._window: deque[float] = deque()
        self.realised_loss = 0.0     # net loss from settled bets (never negative)
        self.open_stake: dict[int, float] = {}
        self.refusals: list[str] = []

    # -- checks ---------------------------------------------------------
    def kill_switch_engaged(self) -> bool:
        return Path(self.limits.kill_switch_path).exists()

    def _check(self, round_id: int, stake: float) -> None:
        lim = self.limits
        if self.kill_switch_engaged():
            raise GuardViolation(f"kill switch present at {lim.kill_switch_path}")
        if not math.isfinite(stake) or stake <= 0:
            raise GuardViolation(f"invalid stake {stake!r}")
        if stake > lim.max_order_abs:
            raise GuardViolation(f"stake {stake} above hard cap {lim.max_order_abs}")
        if round_id in self._rounds:
            raise GuardViolation(f"second order for round {round_id}")
        now = self._clock()
        while self._window and now - self._window[0] > 60.0:
            self._window.popleft()
        if len(self._window) >= lim.max_orders_per_minute:
            raise GuardViolation(f"order rate above {lim.max_orders_per_minute}/min")
        worst = self.realised_loss + sum(self.open_stake.values()) + stake
        if worst > lim.max_session_loss_abs + 1e-9:
            raise GuardViolation(
                f"worst-case session loss {worst:.2f} would exceed {lim.max_session_loss_abs:.2f}")

    # -- Executor protocol ---------------------------------------------
    async def place_bet(self, round_id: int, stake: float, target_cashout: float,
                        closes_at: float) -> Receipt:
        try:
            self._check(round_id, stake)
        except GuardViolation as exc:
            self.refusals.append(str(exc))
            raise
        # Record before the await: if the inner call hangs or is cancelled the
        # exposure is still counted.
        self._rounds.add(round_id)
        self._window.append(self._clock())
        self.open_stake[round_id] = stake
        return await self._inner.place_bet(round_id, stake, target_cashout, closes_at)

    # -- settlement feedback (called by the bot) -----------------------
    def on_settlement(self, round_id: int, pnl: float) -> None:
        self.open_stake.pop(round_id, None)
        self.realised_loss = max(0.0, self.realised_loss - pnl)

    def on_rejected(self, round_id: int) -> None:
        """Inner executor refused cleanly: no exposure was created."""
        self.open_stake.pop(round_id, None)
