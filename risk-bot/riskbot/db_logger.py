"""Asynchronous SQLite audit logger (aiosqlite).

Hot-path callers never await the database. ``log_*`` methods enqueue a record
with ``put_nowait`` and return immediately; a single writer task drains the
queue in batches, each batch in one transaction.

Failure policy (fail-closed for trading, fail-safe for data):
    * "database is locked"/"busy": retry with exponential backoff, on top of
      SQLite's own ``busy_timeout``.
    * Retries exhausted, or queue full: the records are appended to a JSONL
      fallback file so nothing is lost, and ``on_failure`` is called once the
      number of consecutive failed batches reaches the configured limit. The bot
      wires ``on_failure`` to the circuit breaker: no audit trail, no trading.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiosqlite

from .config import StorageConfig
from .models import FeedEvent, Settlement
from .state_machine import Transition

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    start_equity REAL NOT NULL,
    end_equity   REAL,
    end_state    TEXT,
    trip_reason  TEXT,
    config_json  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
    session_id  TEXT NOT NULL,
    round_id    INTEGER NOT NULL,
    crash_point REAL NOT NULL,
    server_ts   REAL NOT NULL,
    recv_ts     REAL NOT NULL,
    latency_ms  REAL NOT NULL,
    PRIMARY KEY (session_id, round_id)
);
CREATE TABLE IF NOT EXISTS bets (
    session_id     TEXT NOT NULL,
    round_id       INTEGER NOT NULL,
    order_id       TEXT,
    stake          REAL NOT NULL,
    target_cashout REAL NOT NULL,
    crash_point    REAL,
    won            INTEGER NOT NULL,
    resolved       INTEGER NOT NULL,
    pnl            REAL NOT NULL,
    equity_after   REAL NOT NULL,
    settled_at     REAL NOT NULL,
    PRIMARY KEY (session_id, round_id)
);
CREATE TABLE IF NOT EXISTS state_transitions (
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    from_state TEXT NOT NULL,
    to_state   TEXT NOT NULL,
    reason     TEXT NOT NULL,
    at         REAL NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS risk_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    at         REAL NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_risk_events_session ON risk_events(session_id, at);
"""

Record = tuple[str, tuple[Any, ...]]
_STOP = object()


class DBLogger:
    def __init__(self, cfg: StorageConfig, on_failure: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.session_id = uuid.uuid4().hex
        self._on_failure = on_failure or (lambda reason: None)
        self._clock = clock
        self._queue: asyncio.Queue[Record | object] = asyncio.Queue(maxsize=cfg.queue_max)
        self._conn: aiosqlite.Connection | None = None
        self._writer: asyncio.Task[None] | None = None
        self.consecutive_failures = 0
        self.records_written = 0
        self.records_to_fallback = 0
        self._failure_signalled = False

    # -- lifecycle -------------------------------------------------------
    class UncleanPreviousSession(RuntimeError):
        """A session earlier today never recorded its closing equity."""

    async def start(self, start_equity: float, config: dict[str, Any]) -> None:
        """Convenience: ``open()`` followed by ``begin_session()``."""
        await self.open()
        self.begin_session(start_equity, config)

    async def open(self) -> None:
        Path(self.cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.cfg.fallback_path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: we issue BEGIN/COMMIT ourselves so each batch is one explicit transaction.
        self._conn = await aiosqlite.connect(
            self.cfg.db_path, timeout=self.cfg.busy_timeout_ms / 1000, isolation_level=None
        )
        await self._conn.execute(f"PRAGMA busy_timeout={int(self.cfg.busy_timeout_ms)}")
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.executescript(SCHEMA)

    async def today_carryover(self, day_start_ts: float) -> tuple[float, float] | None:
        """Return (day_start_equity, last_end_equity) from earlier sessions today.

        Prevents a restart from resetting the daily drawdown baseline. Raises
        ``UncleanPreviousSession`` if a session today has no closing equity,
        because the true current equity is then unknown (fail closed).
        """
        assert self._conn is not None
        rows = list(await self._conn.execute_fetchall(
            "SELECT start_equity, end_equity FROM sessions WHERE started_at >= ? ORDER BY started_at",
            (day_start_ts,),
        ))
        if not rows:
            return None
        if any(end is None for _, end in rows):
            raise DBLogger.UncleanPreviousSession(
                "a session earlier today did not record its closing equity; reconcile the "
                "sessions table before restarting")
        return float(rows[0][0]), float(rows[-1][1])

    def begin_session(self, start_equity: float, config: dict[str, Any]) -> None:
        self._enqueue(
            "INSERT INTO sessions (session_id, started_at, start_equity, config_json) VALUES (?,?,?,?)",
            (self.session_id, self._clock(), start_equity, json.dumps(config, default=str)),
        )
        self._writer = asyncio.create_task(self._run_writer(), name="db-writer")

    async def close(self, end_equity: float, end_state: str, trip_reason: str | None) -> None:
        self._enqueue(
            "UPDATE sessions SET ended_at=?, end_equity=?, end_state=?, trip_reason=? WHERE session_id=?",
            (self._clock(), end_equity, end_state, trip_reason, self.session_id),
        )
        if self._writer is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(_STOP)
            try:
                await asyncio.wait_for(asyncio.shield(self._writer), self.cfg.close_timeout_s)
            except (asyncio.TimeoutError, Exception):
                log.error("db writer did not drain in time; spilling remaining records to fallback")
                self._writer.cancel()
                self._spill_remaining()
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()

    # -- public, non-blocking log API ------------------------------------
    def log_transition(self, t: Transition) -> None:
        self._enqueue(
            "INSERT OR REPLACE INTO state_transitions VALUES (?,?,?,?,?,?)",
            (self.session_id, t.seq, str(t.from_state), str(t.to_state), t.reason, t.at),
        )

    def log_round(self, ev: FeedEvent) -> None:
        self._enqueue(
            "INSERT OR IGNORE INTO rounds VALUES (?,?,?,?,?,?)",
            (self.session_id, ev.round_id, ev.crash_point, ev.server_ts, ev.recv_ts, round(ev.latency_ms, 3)),
        )

    def log_bet(self, s: Settlement, order_id: str | None, equity_after: float) -> None:
        self._enqueue(
            "INSERT OR REPLACE INTO bets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.session_id, s.round_id, order_id, s.stake, s.target_cashout, s.crash_point,
             int(s.won), int(s.resolved), s.pnl, equity_after, self._clock()),
        )

    def log_risk_event(self, kind: str, **detail: Any) -> None:
        self._enqueue(
            "INSERT INTO risk_events (session_id, at, kind, detail) VALUES (?,?,?,?)",
            (self.session_id, self._clock(), kind, json.dumps(detail, default=str)),
        )

    # -- internals -------------------------------------------------------
    def _enqueue(self, sql: str, params: tuple[Any, ...]) -> None:
        try:
            self._queue.put_nowait((sql, params))
        except asyncio.QueueFull:
            # Backpressure policy: never block the trading loop on the logger.
            self._write_fallback([(sql, params)], "queue_full")
            self._register_failure("audit queue full")

    async def _run_writer(self) -> None:
        while True:
            item = await self._queue.get()
            batch: list[Record] = []
            stop = item is _STOP
            if not stop:
                batch.append(item)  # type: ignore[arg-type]
            while not stop and len(batch) < self.cfg.batch_max:
                try:
                    nxt = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if nxt is _STOP:
                    stop = True
                else:
                    batch.append(nxt)  # type: ignore[arg-type]
            if batch:
                await self._write_batch(batch)
            if stop:
                # Drain anything enqueued after the stop marker.
                rest: list[Record] = []
                while not self._queue.empty():
                    nxt = self._queue.get_nowait()
                    if nxt is not _STOP:
                        rest.append(nxt)  # type: ignore[arg-type]
                if rest:
                    await self._write_batch(rest)
                return

    async def _write_batch(self, batch: list[Record]) -> None:
        assert self._conn is not None
        delay = self.cfg.retry_backoff_base_s
        last_error: Exception | None = None
        for attempt in range(self.cfg.write_retries + 1):
            try:
                await self._conn.execute("BEGIN IMMEDIATE")
                for sql, params in batch:
                    await self._conn.execute(sql, params)
                await self._conn.execute("COMMIT")
                self.records_written += len(batch)
                self.consecutive_failures = 0
                return
            except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
                last_error = exc
                with contextlib.suppress(Exception):
                    await self._conn.execute("ROLLBACK")
                log.warning("db write failed (attempt %d/%d): %s",
                            attempt + 1, self.cfg.write_retries + 1, exc)
                if attempt < self.cfg.write_retries:
                    await asyncio.sleep(delay)
                    delay *= 2
        self._write_fallback(batch, f"write_failed: {last_error}")
        self._register_failure(f"sqlite write failed after retries: {last_error}")

    def _register_failure(self, reason: str) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.cfg.max_consecutive_write_failures and not self._failure_signalled:
            self._failure_signalled = True
            self._on_failure(reason)

    def _write_fallback(self, batch: list[Record], why: str) -> None:
        try:
            with open(self.cfg.fallback_path, "a", encoding="utf-8") as fh:
                for sql, params in batch:
                    fh.write(json.dumps({"why": why, "sql": sql, "params": params}, default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self.records_to_fallback += len(batch)
        except OSError:
            log.exception("fallback log write failed; %d records lost", len(batch))

    def _spill_remaining(self) -> None:
        rest: list[Record] = []
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is not _STOP:
                rest.append(item)  # type: ignore[arg-type]
        if rest:
            self._write_fallback(rest, "shutdown_timeout")

