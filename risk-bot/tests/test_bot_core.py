"""End-to-end runs against the mock feed, one per safety mechanism."""

import asyncio
import sqlite3

from riskbot.bot_core import RiskBot
from riskbot.executor import PaperExecutor
from riskbot.feed import MockCrashFeed, MockFeedScript
from riskbot.models import BotState


def script(**kw) -> MockFeedScript:
    kw.setdefault("rounds", 12)
    kw.setdefault("betting_window_s", 0.03)
    return MockFeedScript(**kw)


async def run(cfg, feed_script, executor=None):
    executor = executor or PaperExecutor()
    feed = MockCrashFeed(feed_script)
    bot = RiskBot(cfg, feed.connect, executor)
    report = await asyncio.wait_for(bot.run(), timeout=20)
    return bot, report, executor


def trip_time(bot):
    return next(t.at for t in bot.fsm.history if t.to_state is BotState.CIRCUIT_BREAKER)


async def test_normal_session_ends_idle_and_is_fully_logged(make_cfg):
    cfg = make_cfg()
    bot, rep, ex = await run(cfg, script(rounds=15))
    assert rep.final_state == "IDLE" and rep.trip_reason is None
    assert rep.rounds_seen == 15 and rep.bets == len(ex.orders) > 0
    con = sqlite3.connect(cfg.storage.db_path)
    assert con.execute("select count(*) from bets").fetchone()[0] == rep.bets
    assert con.execute("select count(*) from rounds").fetchone()[0] == 15
    assert con.execute("select count(*) from state_transitions").fetchone()[0] == len(rep.transitions)


async def test_daily_drawdown_trips_without_overshoot(make_cfg):
    cfg = make_cfg(risk={"max_consecutive_losses": 1000, "max_daily_drawdown_pct": 0.02,
                         "base_risk_fraction": 0.01, "max_stake_fraction": 0.02})
    bot, rep, ex = await run(cfg, script(rounds=80, crash_points=[1.0] * 80))
    assert rep.final_state == "CIRCUIT_BREAKER"
    assert rep.trip_reason == "daily drawdown limit reached"
    assert rep.end_equity >= 1000 * (1 - 0.02) - 1e-6  # never past the limit
    assert all(o.accepted_at <= trip_time(bot) for o in ex.orders)  # nothing after the trip


async def test_extreme_latency_trips_and_blocks_execution(make_cfg):
    cfg = make_cfg()
    bot, rep, ex = await run(cfg, script(latency_ms={4: 5_000.0}))
    assert rep.trip_reason == "extreme feed latency"
    assert all(o.round_id < 4 for o in ex.orders)


async def test_sustained_soft_latency_trips(make_cfg):
    cfg = make_cfg()
    bot, rep, ex = await run(cfg, script(latency_ms={3: 600.0, 4: 600.0}))
    assert rep.trip_reason == "sustained feed latency"
    assert all(o.round_id < 3 for o in ex.orders)  # stale rounds are never traded


async def test_clock_skew_trips(make_cfg):
    cfg = make_cfg()
    _, rep, _ = await run(cfg, script(clock_skew_ms={2: 1_000.0}))
    assert rep.trip_reason == "clock skew beyond tolerance"


async def test_disconnect_mid_round_books_worst_case(make_cfg):
    cfg = make_cfg()
    bot, rep, ex = await run(cfg, script(drop_mid_round={5}))
    assert rep.trip_reason == "feed lost with open exposure"
    assert rep.unresolved == 1
    con = sqlite3.connect(cfg.storage.db_path)
    stake, pnl, resolved = con.execute(
        "select stake, pnl, resolved from bets where round_id=5").fetchone()
    assert pnl == -stake and resolved == 0


async def test_disconnect_between_rounds_reconnects(make_cfg):
    """Drop while flat (bot in cool-down, so no position) -> reconnect, keep going."""
    cfg = make_cfg(risk={"max_consecutive_losses": 1, "cooldown_seconds": 0.3})
    feed_script = script(rounds=10, crash_points=[1.0] + [2.0] * 9, drop_mid_round={2})
    feed = MockCrashFeed(feed_script)
    bot = RiskBot(cfg, feed.connect, PaperExecutor())
    rep = await asyncio.wait_for(bot.run(), timeout=20)
    assert feed.connections == 2
    assert rep.final_state == "IDLE" and rep.unresolved == 0


async def test_stalled_feed_with_open_exposure_trips(make_cfg):
    cfg = make_cfg(latency={"heartbeat_timeout_s": 0.2, "min_bet_margin_ms": 5.0,
                            "execution_timeout_s": 0.2})
    _, rep, _ = await run(cfg, script(stall_mid_round_s={3: 2.0}))
    assert rep.trip_reason == "feed stale with open exposure"
    assert rep.unresolved == 1


async def test_execution_timeout_trips_and_assumes_loss(make_cfg):
    cfg = make_cfg()
    _, rep, _ = await run(cfg, script(), PaperExecutor(hang_rounds={3}))
    assert rep.trip_reason == "execution acknowledgement timeout"
    assert rep.unresolved == 1


async def test_duplicate_betting_open_places_one_bet(make_cfg):
    cfg = make_cfg()
    _, rep, ex = await run(cfg, script(rounds=6, duplicate_betting_open={1, 2, 3, 4, 5, 6}))
    ids = [o.round_id for o in ex.orders]
    assert len(ids) == len(set(ids))


async def test_malformed_message_trips(make_cfg):
    cfg = make_cfg()
    _, rep, _ = await run(cfg, script(malformed_in_round={2}))
    assert rep.trip_reason == "feed protocol violation"


async def test_loss_streak_cools_down_then_resumes(make_cfg):
    cfg = make_cfg(risk={"max_consecutive_losses": 2, "cooldown_seconds": 0.15})
    bot, rep, ex = await run(cfg, script(rounds=12, crash_points=[1.0, 1.0] + [2.0] * 10))
    states = [t.to_state for t in bot.fsm.history]
    i = states.index(BotState.COOLING_DOWN)
    assert states[i + 1] is BotState.MONITORING
    assert rep.final_state == "IDLE"
    # Anti-martingale: the first stake after the cool-down is not above the last losing stake.
    assert ex.orders[2].stake <= ex.orders[1].stake


async def test_db_lock_trips_breaker(make_cfg, tmp_path):
    cfg = make_cfg(storage={"max_consecutive_write_failures": 1})
    feed = MockCrashFeed(script(rounds=40))
    bot = RiskBot(cfg, feed.connect, PaperExecutor())
    task = asyncio.create_task(bot.run())
    while bot.db.records_written < 1:
        await asyncio.sleep(0.01)
    locker = sqlite3.connect(cfg.storage.db_path, timeout=0, isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        rep = await asyncio.wait_for(task, timeout=20)
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    assert rep.final_state == "CIRCUIT_BREAKER"
    assert rep.trip_reason.startswith("audit log unavailable")


async def test_restart_cannot_bypass_tripped_breaker(make_cfg):
    import pytest
    from riskbot.latch import LatchEngaged

    cfg = make_cfg()
    _, rep, _ = await run(cfg, script(latency_ms={2: 5_000.0}))
    assert rep.final_state == "CIRCUIT_BREAKER"
    with pytest.raises(LatchEngaged):
        await run(cfg, script())


async def test_restart_keeps_daily_drawdown_baseline(make_cfg):
    cfg = make_cfg(risk={"max_consecutive_losses": 1000})
    _, first, _ = await run(cfg, script(rounds=5, crash_points=[1.0] * 5))
    assert first.end_equity < first.start_equity
    bot, second, _ = await run(cfg, script(rounds=3, crash_points=[2.0] * 3))
    assert second.start_equity == first.end_equity
    assert bot.risk.day_start_equity == first.start_equity
