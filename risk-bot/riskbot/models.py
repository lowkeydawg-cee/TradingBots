"""Shared domain types: FSM states, feed events, positions and outcomes."""

from __future__ import annotations

import enum
import json
import math
from dataclasses import dataclass
from typing import Any


class BotState(enum.StrEnum):
    IDLE = "IDLE"
    MONITORING = "MONITORING"
    ACTIVE = "ACTIVE"
    COOLING_DOWN = "COOLING_DOWN"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"


class EventType(enum.StrEnum):
    BETTING_OPEN = "betting_open"
    ROUND_START = "round_start"
    TICK = "tick"
    CRASH = "crash"
    HEARTBEAT = "heartbeat"


class FeedProtocolError(ValueError):
    """A feed message is malformed or violates the protocol."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise FeedProtocolError(f"{name} must be a finite number, got {value!r}")
    return float(value)


@dataclass(frozen=True, slots=True)
class FeedEvent:
    """One validated message from the feed.

    ``server_ts`` and ``closes_at`` are wall-clock epoch seconds from the server;
    ``recv_ts`` is our wall clock at receipt. Latency is the difference.
    """

    type: EventType
    round_id: int
    server_ts: float
    recv_ts: float
    closes_at: float | None = None
    multiplier: float | None = None
    crash_point: float | None = None

    @property
    def latency_ms(self) -> float:
        return (self.recv_ts - self.server_ts) * 1000.0

    @classmethod
    def parse(cls, raw: str | bytes, recv_ts: float) -> FeedEvent:
        """Strictly validate a raw JSON message. Anything unexpected is rejected."""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FeedProtocolError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise FeedProtocolError("message must be a JSON object")
        try:
            etype = EventType(data.get("type"))
        except ValueError as exc:
            raise FeedProtocolError(f"unknown event type {data.get('type')!r}") from exc

        round_id = data.get("round_id")
        if isinstance(round_id, bool) or not isinstance(round_id, int) or round_id < 0:
            raise FeedProtocolError(f"round_id must be a non-negative int, got {round_id!r}")
        server_ts = _finite(data.get("ts"), "ts")

        closes_at = multiplier = crash_point = None
        if etype is EventType.BETTING_OPEN:
            closes_at = _finite(data.get("closes_at"), "closes_at")
            if closes_at < server_ts:
                raise FeedProtocolError("closes_at precedes ts")
        elif etype is EventType.TICK:
            multiplier = _finite(data.get("multiplier"), "multiplier")
            if multiplier < 1.0:
                raise FeedProtocolError("multiplier < 1.0")
        elif etype is EventType.CRASH:
            crash_point = _finite(data.get("crash_point"), "crash_point")
            if crash_point < 1.0:
                raise FeedProtocolError("crash_point < 1.0")

        return cls(etype, round_id, server_ts, recv_ts, closes_at, multiplier, crash_point)


@dataclass(frozen=True, slots=True)
class Position:
    """An open bet. At most one exists at a time (enforced by the FSM)."""

    round_id: int
    stake: float
    target_cashout: float
    placed_at: float
    order_id: str


@dataclass(frozen=True, slots=True)
class Settlement:
    round_id: int
    stake: float
    target_cashout: float
    crash_point: float | None  # None when the outcome is unknown (booked as worst case)
    won: bool
    pnl: float
    resolved: bool = True

    @classmethod
    def from_crash(cls, pos: Position, crash_point: float) -> Settlement:
        # Auto cash-out is set at placement, so the bet wins iff the round
        # reaches the target before crashing.
        won = crash_point >= pos.target_cashout
        pnl = round(pos.stake * (pos.target_cashout - 1.0), 2) if won else -pos.stake
        return cls(pos.round_id, pos.stake, pos.target_cashout, crash_point, won, pnl)

    @classmethod
    def worst_case(cls, pos: Position) -> Settlement:
        """Used when the outcome cannot be observed (e.g. disconnect mid-round)."""
        return cls(pos.round_id, pos.stake, pos.target_cashout, None, False, -pos.stake, resolved=False)
