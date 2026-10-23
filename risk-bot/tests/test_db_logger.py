import asyncio
import json
import sqlite3

from riskbot.db_logger import DBLogger


async def _until(cond, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_writes_and_reads_back(make_cfg):
    cfg = make_cfg()
    db = DBLogger(cfg.storage)
    await db.start(1000.0, {"k": 1})
    for i in range(50):
        db.log_risk_event("probe", i=i)
    await db.close(1000.0, "IDLE", None)

    con = sqlite3.connect(cfg.storage.db_path)
    assert con.execute("select count(*) from risk_events").fetchone()[0] == 50
    assert con.execute("select end_state from sessions").fetchone()[0] == "IDLE"


async def test_locked_database_falls_back_and_signals(make_cfg):
    """Another process holds an exclusive lock: records go to JSONL and the
    failure callback (wired to the circuit breaker in the bot) fires."""
    cfg = make_cfg(storage={"max_consecutive_write_failures": 1})
    failures: list[str] = []
    db = DBLogger(cfg.storage, on_failure=failures.append)
    await db.start(1000.0, {})
    assert await _until(lambda: db.records_written >= 1)  # session row landed

    locker = sqlite3.connect(cfg.storage.db_path, timeout=0, isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        db.log_risk_event("while_locked", n=1)
        assert await _until(lambda: bool(failures)), "failure callback should fire"
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    await db.close(1000.0, "CIRCUIT_BREAKER", "test")

    lines = [json.loads(x) for x in open(cfg.storage.fallback_path)]
    assert any("while_locked" in str(rec["params"]) for rec in lines)
    assert db.records_to_fallback >= 1
