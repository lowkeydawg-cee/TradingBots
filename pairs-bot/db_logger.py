"""SQLite event log shared by the bot (writer) and the Streamlit dashboard (reader).

Concurrency design
------------------
* WAL journal mode: readers never block the writer and the writer never blocks
  readers. The dashboard can query while the bot is mid-transaction.
* ``busy_timeout``: if two writers collide (the bot and the dashboard's
  emergency-stop button are the only writers), SQLite waits instead of failing
  immediately with "database is locked".
* Every write is one short transaction, so a lock is held for milliseconds.
* The dashboard opens the database read-only (``mode=ro``) for all queries and
  uses a separate short write only for the control table.
* Writes that still fail after retries are appended to a JSONL fallback file
  so no event is lost; the bot treats repeated failures as a halt condition.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 5_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_checks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,          -- wall clock when we checked (epoch s)
    bar_time  REAL NOT NULL,          -- timestamp of the bar used
    symbol_a  TEXT NOT NULL, price_a REAL NOT NULL,
    symbol_b  TEXT NOT NULL, price_b REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    bar_time      REAL NOT NULL,
    alpha         REAL, beta REAL,
    spread        REAL, spread_mean REAL, spread_std REAL,
    zscore        REAL,
    coint_pvalue  REAL, half_life REAL,
    position      TEXT NOT NULL,      -- FLAT / LONG_SPREAD / SHORT_SPREAD before the action
    action        TEXT NOT NULL,      -- HOLD / OPEN_LONG / OPEN_SHORT / CLOSE / BLOCKED
    note          TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               REAL NOT NULL,
    pair_trade_id    INTEGER,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,   -- buy / sell
    qty              REAL NOT NULL,
    fill_price       REAL,
    client_order_id  TEXT,
    broker_order_id  TEXT,
    status           TEXT NOT NULL,
    purpose          TEXT NOT NULL    -- open / close / unwind
);
CREATE TABLE IF NOT EXISTS pair_trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    direction      TEXT NOT NULL,     -- LONG_SPREAD (long A, short B) / SHORT_SPREAD
    opened_at      REAL NOT NULL,
    entry_z        REAL NOT NULL,
    beta           REAL NOT NULL,
    qty_a          REAL NOT NULL, qty_b REAL NOT NULL,
    entry_price_a  REAL NOT NULL, entry_price_b REAL NOT NULL,
    closed_at      REAL,
    exit_z         REAL,
    exit_price_a   REAL, exit_price_b REAL,
    pnl            REAL,
    exit_reason    TEXT
);
CREATE TABLE IF NOT EXISTS circuit_events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    equity          REAL NOT NULL,
    last_equity     REAL NOT NULL,    -- previous trading day's closing equity
    daily_pnl       REAL NOT NULL,
    gross_exposure  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS control (
    key        TEXT PRIMARY KEY,      -- emergency_stop / halted / heartbeat
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS ix_equity_ts ON equity(ts);
"""

# Columns added after the first release. ``migrate`` adds them to older databases,
# so an existing pairs_bot.db keeps working. Rows written before have NULL there.
MIGRATIONS = [
    ("signals", "pair", "TEXT"),          # "KO/PEP": which pair a row belongs to (portfolio mode)
    ("signals", "vol_ratio", "REAL"),
    ("pair_trades", "pair", "TEXT"),
    ("orders", "expected_price", "REAL"),  # mid / decision price at order time
    ("orders", "slippage_bps", "REAL"),    # what the fill cost vs. expected (+ = paid more)
]


def migrate(conn: sqlite3.Connection) -> None:
    for table, column, kind in MIGRATIONS:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def slippage_bps(side: str, fill: float | None, expected: float | None) -> float | None:
    """Positive = the fill was worse than expected (paid more on a buy, got less on a sell)."""
    if not fill or not expected:
        return None
    return (fill - expected) / expected * 1e4 * (1 if side == "buy" else -1)


class DBLogger:
    """Async writer used by the bot."""

    def __init__(self, path: str, retries: int = 3) -> None:
        self.path = path
        self.fallback_path = str(Path(path).with_suffix(".fallback.jsonl"))
        self.retries = retries
        self.consecutive_failures = 0
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()  # one transaction at a time on this connection

    async def open(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000,
                                             isolation_level=None)
        await self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.executescript(SCHEMA)
        for table, column, kind in MIGRATIONS:
            async with self._conn.execute(f"PRAGMA table_info({table})") as cur:
                cols = {row[1] for row in await cur.fetchall()}
            if column not in cols:
                await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # -- generic write with retry + fallback --------------------------------
    async def _write(self, sql: str, params: tuple[Any, ...]) -> int | None:
        assert self._conn is not None, "DBLogger.open() not called"
        delay = 0.05
        async with self._lock:
            for attempt in range(self.retries + 1):
                try:
                    await self._conn.execute("BEGIN IMMEDIATE")
                    cur = await self._conn.execute(sql, params)
                    await self._conn.execute("COMMIT")
                    self.consecutive_failures = 0
                    return cur.lastrowid
                except sqlite3.Error as exc:
                    try:
                        await self._conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    log.warning("db write failed (%d/%d): %s", attempt + 1, self.retries + 1, exc)
                    if attempt < self.retries:
                        await asyncio.sleep(delay)
                        delay *= 2
        self.consecutive_failures += 1
        with open(self.fallback_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "sql": sql, "params": params}, default=str) + "\n")
        return None

    # -- typed log methods --------------------------------------------------
    async def log_price_check(self, bar_time: float, sym_a: str, px_a: float, sym_b: str, px_b: float) -> None:
        await self._write("INSERT INTO price_checks (ts, bar_time, symbol_a, price_a, symbol_b, price_b) "
                          "VALUES (?,?,?,?,?,?)", (time.time(), bar_time, sym_a, px_a, sym_b, px_b))

    async def log_signal(self, bar_time: float, stats: dict[str, float | None], position: str,
                         action: str, note: str = "", pair: str | None = None) -> None:
        await self._write(
            "INSERT INTO signals (ts, bar_time, alpha, beta, spread, spread_mean, spread_std, zscore, "
            "coint_pvalue, half_life, position, action, note, pair, vol_ratio) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), bar_time, stats.get("alpha"), stats.get("beta"), stats.get("spread"),
             stats.get("spread_mean"), stats.get("spread_std"), stats.get("zscore"),
             stats.get("coint_pvalue"), stats.get("half_life"), position, action, note, pair,
             stats.get("vol_ratio")))

    async def log_order(self, pair_trade_id: int | None, symbol: str, side: str, qty: float,
                        fill_price: float | None, client_order_id: str | None,
                        broker_order_id: str | None, status: str, purpose: str,
                        expected_price: float | None = None) -> None:
        await self._write(
            "INSERT INTO orders (ts, pair_trade_id, symbol, side, qty, fill_price, client_order_id, "
            "broker_order_id, status, purpose, expected_price, slippage_bps) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), pair_trade_id, symbol, side, qty, fill_price, client_order_id,
             broker_order_id, status, purpose, expected_price,
             slippage_bps(side, fill_price, expected_price)))

    async def open_pair_trade(self, direction: str, entry_z: float, beta: float, qty_a: float,
                              qty_b: float, px_a: float, px_b: float, pair: str | None = None) -> int | None:
        return await self._write(
            "INSERT INTO pair_trades (direction, opened_at, entry_z, beta, qty_a, qty_b, entry_price_a, "
            "entry_price_b, pair) VALUES (?,?,?,?,?,?,?,?,?)",
            (direction, time.time(), entry_z, beta, qty_a, qty_b, px_a, px_b, pair))

    async def close_pair_trade(self, trade_id: int, exit_z: float | None, px_a: float, px_b: float,
                               pnl: float, reason: str) -> None:
        await self._write(
            "UPDATE pair_trades SET closed_at=?, exit_z=?, exit_price_a=?, exit_price_b=?, pnl=?, "
            "exit_reason=? WHERE id=?", (time.time(), exit_z, px_a, px_b, pnl, reason, trade_id))

    async def log_circuit_event(self, kind: str, **detail: Any) -> None:
        log.warning("CIRCUIT EVENT %s %s", kind, detail)
        await self._write("INSERT INTO circuit_events (ts, kind, detail) VALUES (?,?,?)",
                          (time.time(), kind, json.dumps(detail, default=str)))

    async def log_equity(self, equity: float, last_equity: float, gross_exposure: float) -> None:
        await self._write("INSERT INTO equity (ts, equity, last_equity, daily_pnl, gross_exposure) "
                          "VALUES (?,?,?,?,?)",
                          (time.time(), equity, last_equity, equity - last_equity, gross_exposure))

    async def set_control(self, key: str, value: str) -> None:
        await self._write("INSERT INTO control (key, value, updated_at) VALUES (?,?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                          (key, value, time.time()))

    async def get_control(self, key: str) -> str | None:
        assert self._conn is not None
        async with self._lock:
            async with self._conn.execute("SELECT value FROM control WHERE key=?", (key,)) as cur:
                row = await cur.fetchone()
        return None if row is None else row[0]

    async def open_trade(self, pair: str | None = None) -> dict[str, Any] | None:
        """The pair trade that is still open, if any (used to resume after a restart).

        Rows from before portfolio mode have no pair; they belong to whichever pair asks first.
        """
        assert self._conn is not None
        sql, params = "SELECT * FROM pair_trades WHERE closed_at IS NULL", ()
        if pair is not None:
            sql, params = sql + " AND (pair = ? OR pair IS NULL)", (pair,)
        async with self._lock:
            self._conn.row_factory = aiosqlite.Row
            async with self._conn.execute(sql + " ORDER BY id DESC LIMIT 1", params) as cur:
                row = await cur.fetchone()
            self._conn.row_factory = None
        return None if row is None else dict(row)

    async def period_start_equity(self, since_ts: float) -> float | None:
        """Previous-close equity at the first snapshot since ``since_ts`` (start of a week or month)."""
        assert self._conn is not None
        async with self._lock:
            async with self._conn.execute(
                    "SELECT last_equity FROM equity WHERE ts >= ? ORDER BY id LIMIT 1", (since_ts,)) as cur:
                row = await cur.fetchone()
        return None if row is None else float(row[0])

    async def today_summary(self, since_ts: float) -> dict[str, Any]:
        """Closed trades and P&L since ``since_ts``, for the daily report."""
        assert self._conn is not None
        async with self._lock:
            async with self._conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(pnl),0), COALESCE(SUM(pnl>0),0) FROM pair_trades "
                    "WHERE closed_at >= ?", (since_ts,)) as cur:
                n, pnl, wins = await cur.fetchone()
            async with self._conn.execute(
                    "SELECT AVG(slippage_bps) FROM orders WHERE ts >= ? AND slippage_bps IS NOT NULL",
                    (since_ts,)) as cur:
                (slip,) = await cur.fetchone()
        return {"trades": n, "pnl": float(pnl), "wins": int(wins), "avg_slippage_bps": slip}


# ---------------------------------------------------------------------------
# Synchronous helpers for the Streamlit dashboard (separate process)
# ---------------------------------------------------------------------------
def connect_readonly(path: str) -> sqlite3.Connection:
    """Read-only connection: the dashboard can never corrupt or lock the bot's data."""
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True,
                           timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def request_emergency_stop(path: str, reason: str) -> None:
    """Write the stop flag. The bot polls it every cycle, flattens and halts."""
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.executescript(SCHEMA)  # harmless if it already exists
        migrate(conn)
        with conn:  # one short transaction
            conn.execute(
                "INSERT INTO control (key, value, updated_at) VALUES ('emergency_stop', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (reason, time.time()))
            conn.execute("INSERT INTO circuit_events (ts, kind, detail) VALUES (?,?,?)",
                         (time.time(), "emergency_stop_requested", json.dumps({"reason": reason})))
    finally:
        conn.close()
