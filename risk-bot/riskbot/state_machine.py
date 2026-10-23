"""Asynchronous finite state machine.

    IDLE -> MONITORING -> ACTIVE -> MONITORING ...
                  |          |
                  v          v
              COOLING_DOWN <-+
    (any) -> CIRCUIT_BREAKER   (absorbing: no transition ever leaves it)

Concurrency model
-----------------
* ``transition()`` serialises on an ``asyncio.Lock`` and supports compare-and-set
  through ``expected=``, so two coroutines racing on the same event cannot both
  win (e.g. two duplicate "betting_open" messages cannot both move the bot into
  ACTIVE).
* ``lock`` is exposed so callers can hold it across a check -> await -> act
  sequence (the execution gate does this).
* ``trip()`` is deliberately synchronous and lock-free. In asyncio a function
  with no ``await`` runs atomically with respect to every other coroutine, so the
  emergency stop takes effect immediately even if another coroutine is holding
  the lock while awaiting I/O. It sets the ``halted`` event before anything else.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .models import BotState

log = logging.getLogger(__name__)

S = BotState
ALLOWED_TRANSITIONS: dict[BotState, frozenset[BotState]] = {
    S.IDLE: frozenset({S.MONITORING, S.CIRCUIT_BREAKER}),
    S.MONITORING: frozenset({S.ACTIVE, S.COOLING_DOWN, S.IDLE, S.CIRCUIT_BREAKER}),
    S.ACTIVE: frozenset({S.MONITORING, S.COOLING_DOWN, S.IDLE, S.CIRCUIT_BREAKER}),
    S.COOLING_DOWN: frozenset({S.MONITORING, S.IDLE, S.CIRCUIT_BREAKER}),
    S.CIRCUIT_BREAKER: frozenset(),  # absorbing; recovery requires a new process
}


class IllegalTransition(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Transition:
    seq: int
    from_state: BotState
    to_state: BotState
    reason: str
    at: float


# Listeners must be synchronous and non-blocking (e.g. enqueue to a logger).
TransitionListener = Callable[[Transition], None]


class StateMachine:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._state = BotState.IDLE
        self._lock = asyncio.Lock()
        self._halted = asyncio.Event()
        self._listeners: list[TransitionListener] = []
        self._history: list[Transition] = []
        self._seq = 0
        self._clock = clock
        self.trip_reason: str | None = None

    # -- read-only views -------------------------------------------------
    @property
    def state(self) -> BotState:
        return self._state

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    @property
    def halted(self) -> asyncio.Event:
        return self._halted

    @property
    def is_halted(self) -> bool:
        return self._halted.is_set()

    @property
    def history(self) -> tuple[Transition, ...]:
        return tuple(self._history)

    def add_listener(self, listener: TransitionListener) -> None:
        self._listeners.append(listener)

    # -- transitions -----------------------------------------------------
    async def transition(self, to: BotState, reason: str, *, expected: BotState | None = None) -> bool:
        """Acquire the lock and transition. See ``transition_locked``."""
        async with self._lock:
            return self.transition_locked(to, reason, expected=expected)

    def transition_locked(self, to: BotState, reason: str, *, expected: BotState | None = None) -> bool:
        """Transition while the caller already holds ``lock``.

        Returns False (no change) when ``expected`` is given and does not match
        the current state, or when the machine is halted. Raises
        ``IllegalTransition`` for edges not in ``ALLOWED_TRANSITIONS``.
        """
        if to is BotState.CIRCUIT_BREAKER:
            return self.trip(reason) is not None
        if self._halted.is_set():
            return False
        if expected is not None and self._state is not expected:
            return False
        if to not in ALLOWED_TRANSITIONS[self._state]:
            raise IllegalTransition(f"{self._state} -> {to} is not allowed ({reason})")
        self._apply(to, reason)
        return True

    def trip(self, reason: str) -> Transition | None:
        """Emergency stop. Synchronous, lock-free, idempotent, irreversible."""
        self._halted.set()  # first: every execution path checks this flag
        if self._state is BotState.CIRCUIT_BREAKER:
            return None
        self.trip_reason = reason
        log.critical("CIRCUIT BREAKER TRIPPED: %s", reason)
        return self._apply(BotState.CIRCUIT_BREAKER, reason)

    def _apply(self, to: BotState, reason: str) -> Transition:
        self._seq += 1
        t = Transition(self._seq, self._state, to, reason, self._clock())
        self._state = to
        self._history.append(t)
        log.info("FSM %s -> %s (%s)", t.from_state, t.to_state, reason)
        for listener in self._listeners:
            try:
                listener(t)
            except Exception:  # a broken listener must never block a transition
                log.exception("transition listener failed")
        return t
