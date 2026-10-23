"""Order execution interface and a paper (simulated) executor.

The bot only talks to ``Executor``. ``PaperExecutor`` records orders locally
and never touches a network; it is the only implementation shipped.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Protocol


class ExecutionRejected(Exception):
    """The venue refused the order cleanly (no exposure was created)."""


@dataclass(frozen=True, slots=True)
class Receipt:
    order_id: str
    round_id: int
    stake: float
    target_cashout: float
    accepted_at: float


class Executor(Protocol):
    async def place_bet(self, round_id: int, stake: float, target_cashout: float,
                        closes_at: float) -> Receipt: ...


@dataclass
class PaperExecutor:
    ack_delay_s: float = 0.0
    # Fault injection: never acknowledge orders for these rounds.
    hang_rounds: set[int] = field(default_factory=set)
    reject_rounds: set[int] = field(default_factory=set)
    orders: list[Receipt] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    async def place_bet(self, round_id: int, stake: float, target_cashout: float,
                        closes_at: float) -> Receipt:
        if round_id in self.hang_rounds:
            await asyncio.Event().wait()  # simulates a venue that never answers
        if self.ack_delay_s:
            await asyncio.sleep(self.ack_delay_s)
        now = time.time()
        if round_id in self.reject_rounds or now > closes_at:
            raise ExecutionRejected(f"round {round_id} not accepting bets")
        receipt = Receipt(f"paper-{next(self._ids)}", round_id, stake, target_cashout, now)
        self.orders.append(receipt)
        return receipt
