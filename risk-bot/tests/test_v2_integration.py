"""v2 features end to end: real WebSocket, guard, journal + replay, ops endpoint."""

import asyncio
import json

from riskbot.bot_core import RiskBot
from riskbot.config import ConfigError
from riskbot.executor import PaperExecutor
from riskbot.feed import MockCrashFeed, MockFeedScript
from riskbot.journal import verify_chain
from riskbot.mock_server import start_mock_server
from riskbot.montecarlo import simulate
from riskbot.config import RiskLimits
from riskbot.replay import replay
from riskbot.ws_transport import WebSocketFeed

import pytest


def script(**kw):
    kw.setdefault("rounds", 12)
    kw.setdefault("betting_window_s", 0.03)
    return MockFeedScript(**kw)


async def run_ws(cfg, feed_script):
    server, sim, port = await start_mock_server(feed_script)
    try:
        ws_feed = WebSocketFeed(f"ws://127.0.0.1:{port}/")
        bot = RiskBot(cfg, ws_feed.connect, PaperExecutor())
        rep = await asyncio.wait_for(bot.run(), timeout=20)
    finally:
        server.close()
        await server.wait_closed()
    return bot, rep, ws_feed


async def test_runs_over_real_websocket(make_cfg):
    _, rep, _ = await run_ws(make_cfg(), script(rounds=15))
    assert rep.final_state == "IDLE" and rep.rounds_seen == 15 and rep.bets > 0


async def test_real_socket_drop_mid_round_trips(make_cfg):
    _, rep, _ = await run_ws(make_cfg(), script(drop_mid_round={4}))
    assert rep.trip_reason == "feed lost with open exposure" and rep.unresolved == 1


async def test_real_socket_drop_while_flat_reconnects(make_cfg):
    cfg = make_cfg(risk={"max_consecutive_losses": 1, "cooldown_seconds": 0.3})
    _, rep, ws_feed = await run_ws(cfg, script(rounds=10, crash_points=[1.0] + [2.0] * 9,
                                               drop_mid_round={2}))
    assert ws_feed.connections == 2 and rep.final_state == "IDLE"


def test_non_loopback_endpoint_refused():
    with pytest.raises(ConfigError):
        WebSocketFeed("wss://example.com/feed")


async def test_guard_violation_trips_breaker(make_cfg):
    # Guard tighter than the risk engine's caps can only be reached if an inner
    # check is wrong; simulate that by giving the guard a tiny session-loss cap.
    cfg = make_cfg(guard={"max_session_loss_abs": 6.0, "max_order_abs": 25.0})
    feed = MockCrashFeed(script(rounds=20, crash_points=[1.0] * 20))
    bot = RiskBot(cfg, feed.connect, PaperExecutor())
    rep = await asyncio.wait_for(bot.run(), timeout=20)
    assert rep.trip_reason == "pre-trade guard violation"
    assert bot.guard.refusals


async def test_kill_switch_file_trips_running_bot(make_cfg, tmp_path):
    cfg = make_cfg()
    feed = MockCrashFeed(script(rounds=200))
    bot = RiskBot(cfg, feed.connect, PaperExecutor())
    task = asyncio.create_task(bot.run())
    await asyncio.sleep(0.4)
    (tmp_path / "KILL").write_text("operator")
    rep = await asyncio.wait_for(task, timeout=10)
    # Whichever sees the file first wins: the watchdog, or the guard on the next order.
    assert rep.trip_reason in ("kill switch file present", "pre-trade guard violation")
    assert not bot.guard.refusals or "kill switch" in bot.guard.refusals[0]


async def test_journal_chain_and_deterministic_replay(make_cfg):
    cfg = make_cfg(risk={"max_consecutive_losses": 2, "cooldown_seconds": 0.05})
    for seed in (3, 4):  # two sessions on one chain, second carries over equity
        bot = RiskBot(cfg, MockCrashFeed(script(rounds=25, seed=seed)).connect, PaperExecutor())
        await asyncio.wait_for(bot.run(), timeout=20)
    check = verify_chain(cfg.ops.journal_path)
    assert check.ok and check.records > 50
    res = replay(cfg.ops.journal_path)
    assert res.ok, res.mismatches
    assert res.sessions == 2 and res.decisions >= res.settlements > 0


async def test_tampered_journal_is_detected(make_cfg):
    cfg = make_cfg()
    bot = RiskBot(cfg, MockCrashFeed(script()).connect, PaperExecutor())
    await asyncio.wait_for(bot.run(), timeout=20)
    lines = open(cfg.ops.journal_path).read().splitlines()
    i = next(n for n, line in enumerate(lines) if '"kind":"settlement"' in line)
    rec = json.loads(lines[i])
    rec["data"]["pnl"] = 999.0
    lines[i] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    open(cfg.ops.journal_path, "w").write("\n".join(lines) + "\n")
    assert not verify_chain(cfg.ops.journal_path).ok
    assert not replay(cfg.ops.journal_path).ok


async def _http(port, method, path):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await w.drain()
    data = await r.read()
    w.close()
    head, body = data.split(b"\r\n\r\n", 1)
    return head.split(b" ")[1].decode(), body.decode()


async def test_ops_endpoint_status_metrics_kill(make_cfg):
    cfg = make_cfg(ops={"ops_port": 0})
    bot = RiskBot(cfg, MockCrashFeed(script(rounds=500)).connect, PaperExecutor())
    task = asyncio.create_task(bot.run())
    while bot.ops is None or not bot.counters["feed_messages"]:
        await asyncio.sleep(0.01)
    port = bot.ops.port
    code, body = await _http(port, "GET", "/status")
    assert code == "200" and json.loads(body)["state"] in ("MONITORING", "ACTIVE", "COOLING_DOWN")
    code, body = await _http(port, "GET", "/metrics")
    assert code == "200" and "riskbot_equity" in body and 'riskbot_state{state="CIRCUIT_BREAKER"} 0' in body
    assert (await _http(port, "GET", "/nope"))[0] == "404"
    code, _ = await _http(port, "POST", "/kill")
    assert code == "200"
    rep = await asyncio.wait_for(task, timeout=10)
    assert rep.trip_reason == "operator kill via ops endpoint"


def test_montecarlo_governed_loss_never_exceeds_daily_limit():
    out = simulate(RiskLimits(), sessions=300, rounds=200, edge=0.03, seed=5, cooldown_rounds=6)
    assert out["governed"]["worst_pnl_pct"] >= -5.0 - 1e-6
    assert out["governed"]["worst_max_drawdown_pct"] <= 5.0 + 1e-6
    assert out["expected_value_per_unit_staked"] < 0
