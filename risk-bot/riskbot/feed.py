"""Feed transport abstraction and a simulated crash-game WebSocket.

``FeedConnection`` is the only interface the bot depends on. ``MockCrashFeed``
implements it in-process so the whole system can run and be tested offline,
including fault injection (latency spikes, clock skew, mid-round disconnects,
stalls, duplicate and malformed messages).

The mock's RNG exists only to drive the simulation. The bot never sees it and
never uses crash history for decisions.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
from dataclasses import dataclass, field
from typing import Protocol


class FeedDisconnected(ConnectionError):
    """The transport dropped. Reconnecting may succeed."""


class FeedClosed(Exception):
    """The server ended the stream normally (no more rounds)."""


class FeedConnection(Protocol):
    async def recv(self) -> str: ...
    async def close(self) -> None: ...



@dataclass(slots=True)
class MockFeedScript:
    """Knobs for the simulated server. Round ids start at ``first_round_id``."""

    rounds: int = 50
    seed: int = 7
    house_edge: float = 0.03
    first_round_id: int = 1
    betting_window_s: float = 0.05
    tick_interval_s: float = 0.002
    ticks_per_round: int = 3
    gap_s: float = 0.005
    # Scripted crash points used in order before falling back to the RNG.
    crash_points: list[float] = field(default_factory=list)
    # Fault injection, keyed by round id.
    latency_ms: dict[int, float] = field(default_factory=dict)
    clock_skew_ms: dict[int, float] = field(default_factory=dict)
    drop_mid_round: set[int] = field(default_factory=set)
    stall_mid_round_s: dict[int, float] = field(default_factory=dict)
    duplicate_betting_open: set[int] = field(default_factory=set)
    malformed_in_round: set[int] = field(default_factory=set)


_EOF = object()
_DROP = object()


class MockCrashFeed:
    """Simulated server. ``connect()`` returns a fresh connection that resumes
    at the next round, like a real reconnect would."""

    def __init__(self, script: MockFeedScript | None = None) -> None:
        self.script = script or MockFeedScript()
        self._rng = random.Random(self.script.seed)
        self._next_round = self.script.first_round_id
        self._last_round = self.script.first_round_id + self.script.rounds - 1
        self._scripted = list(self.script.crash_points)
        self.connections = 0
        self.crash_points: dict[int, float] = {}

    def _crash_point(self) -> float:
        if self._scripted:
            return self._scripted.pop(0)
        u = self._rng.random()
        # Standard provably-fair style distribution with a house edge.
        return max(1.0, math.floor(100 * (1 - self.script.house_edge) / (1 - u)) / 100)

    async def connect(self) -> MockConnection:
        self.connections += 1
        return MockConnection(self)


class MockConnection:
    def __init__(self, server: MockCrashFeed) -> None:
        self._server = server
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._closed = False
        self._producer = asyncio.create_task(self._produce(), name="mock-feed")

    async def recv(self) -> str:
        if self._closed and self._queue.empty():
            raise FeedDisconnected("connection closed")
        item = await self._queue.get()
        if item is _EOF:
            raise FeedClosed("no more rounds")
        if item is _DROP:
            self._closed = True
            raise FeedDisconnected("socket dropped mid-round")
        return item  # type: ignore[return-value]

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._producer.cancel()
            self._queue.put_nowait(_DROP)

    def _send(self, rid: int, payload: dict) -> None:
        sc = self._server.script
        # Delivery latency makes ``ts`` older; server clock skew shifts every
        # server timestamp (ts and closes_at) forward.
        skew = sc.clock_skew_ms.get(rid, 0.0) / 1000
        payload["ts"] = round(payload.get("ts", time.time()) - sc.latency_ms.get(rid, 0.0) / 1000 + skew, 6)
        if "closes_at" in payload:
            payload["closes_at"] = round(payload["closes_at"] + skew, 6)
        self._queue.put_nowait(json.dumps(payload))

    async def _produce(self) -> None:
        srv, sc = self._server, self._server.script
        while srv._next_round <= srv._last_round:
            rid = srv._next_round
            srv._next_round += 1
            crash = srv._crash_point()
            srv.crash_points[rid] = crash

            now = time.time()
            opening = {"type": "betting_open", "round_id": rid, "ts": now,
                       "closes_at": now + sc.betting_window_s}
            self._send(rid, dict(opening))
            if rid in sc.duplicate_betting_open:
                self._send(rid, dict(opening))
            await asyncio.sleep(sc.betting_window_s)

            self._send(rid, {"type": "round_start", "round_id": rid})
            if rid in sc.malformed_in_round:
                self._queue.put_nowait('{"type": "crash", "round_id": "oops"}')
            if rid in sc.drop_mid_round:
                self._queue.put_nowait(_DROP)
                return
            if rid in sc.stall_mid_round_s:
                await asyncio.sleep(sc.stall_mid_round_s[rid])

            for i in range(1, sc.ticks_per_round + 1):
                mult = 1.0 + (crash - 1.0) * i / (sc.ticks_per_round + 1)
                self._send(rid, {"type": "tick", "round_id": rid, "multiplier": round(mult, 2)})
                await asyncio.sleep(sc.tick_interval_s)

            self._send(rid, {"type": "crash", "round_id": rid, "crash_point": crash})
            await asyncio.sleep(sc.gap_s)
        self._queue.put_nowait(_EOF)
