"""Market watcher grades, the demo-money shadow book, the Jarvis (MCP) tools and ntfy alerts."""

import asyncio
import dataclasses
import sqlite3

import pytest

import mcp_server
import watcher as w
from backtest import Costs
from db_logger import DBLogger
from notify import Notifier
from strategy import SpreadStats


def stats(z=2.5, p=0.01, beta=0.9, hl=10.0, vol=1.0, pa=50.0, pb=60.0, t=1_760_000_000.0):
    return SpreadStats(t, pa, pb, 0, beta, 0, 0, 0.01, z, p, hl, vol)


GOOD = w.History(trades=20, wins=15, avg_net=4.0, breakeven_bps=8.0)


@pytest.fixture
def wcfg(cfg):
    return w.watch_settings(cfg(), 15)


def grade(monkeypatch, wcfg, s, hist=GOOD, event=None):
    monkeypatch.setattr(w, "compute_spread_stats", lambda *a, **k: s)
    import pandas as pd
    df = pd.DataFrame({"AAA": [1.0] * wcfg.hedge_lookback, "BBB": [1.0] * wcfg.hedge_lookback})
    return w.assess(("AAA", "BBB"), "sim", df, wcfg, hist, event)


def test_grades(monkeypatch, wcfg):
    assert grade(monkeypatch, wcfg, stats(z=2.5)).grade == "STRONG"
    assert grade(monkeypatch, wcfg, stats(z=2.5)).side == "SHORT_SPREAD"
    assert grade(monkeypatch, wcfg, stats(z=-2.5)).side == "LONG_SPREAD"
    # soft warnings -> RISKY
    for s, hist, event in ((stats(p=0.07), GOOD, None), (stats(vol=4), GOOD, None),
                           (stats(), w.History(3, 3, 5.0, 9.0), None),
                           (stats(), w.History(20, 15, -1.0, 1.0), None),
                           (stats(), GOOD, "KO Q3 earnings")):
        o = grade(monkeypatch, wcfg, s, hist, event)
        assert o.grade == "RISKY" and o.flags, (s, hist, event)
    # broken pair -> AVOID
    for s in (stats(p=0.5), stats(beta=-0.3), stats(z=4.0), stats(hl=float("inf"))):
        assert grade(monkeypatch, wcfg, s).grade == "AVOID", s
    # no signal
    assert grade(monkeypatch, wcfg, stats(z=1.0)).grade == "WATCH"
    assert grade(monkeypatch, wcfg, stats(z=1.0, p=0.5)).grade == "QUIET"


def test_probability_is_shrunk_toward_half():
    assert w.History(0, 0, None, None).prob == 0.5
    assert w.History(2, 2, 1.0, 1.0).prob == 0.75            # 2 for 2 is not 100%
    assert w.History(100, 70, 1.0, 1.0).prob == pytest.approx(71 / 102)


async def test_shadow_book_opens_and_closes_with_costs(monkeypatch, wcfg, tmp_path):
    db = DBLogger(str(tmp_path / "w.db"))
    await db.open()
    book = w.ShadowBook(db, wcfg, Costs(spread_bps=2.0))
    import pandas as pd
    df = pd.DataFrame({"AAA": [50.0] * wcfg.hedge_lookback, "BBB": [60.0] * wcfg.hedge_lookback})
    opp = grade(monkeypatch, wcfg, stats(z=2.5))
    assert await book.update(opp, df) == "opened"
    assert await book.update(opp, df) is None                 # still open, not reverted
    quiet = grade(monkeypatch, wcfg, stats(z=0.2, pa=49.5, t=1_760_003_600.0))
    assert await book.update(quiet, df) == "closed"
    row = (await db.fetchall("SELECT grade, entry_hour, gross_pnl, costs, net_pnl, exit_reason FROM shadow_trades"))[0]
    g, hour, gross, costs, net, reason = row
    assert g == "STRONG" and 0 <= hour <= 23
    assert gross > 0 and costs > 0 and net == pytest.approx(gross - costs, abs=0.02)
    assert "revert" in reason
    # QUIET and WATCH never trade; AVOID does (it's the control group)
    assert await book.update(grade(monkeypatch, wcfg, stats(z=1.0)), df) is None
    assert await book.update(grade(monkeypatch, wcfg, stats(z=2.5, p=0.5)), df) == "opened"
    await db.close()


async def test_watcher_end_to_end_on_sim(cfg, tmp_path):
    base = cfg(db_path=str(tmp_path / "w.db"))
    wc = dataclasses.replace(w.watch_settings(base, 15), hedge_lookback=120, z_lookback=30, max_holding_bars=60)
    src = w.SimSource(warm=600)
    db = DBLogger(base.db_path)
    await db.open()
    n = Notifier()
    watch = w.Watcher(wc, src, db, history_bars=300, refresh_seconds=1e12, all_hours=True, notifier=n)
    for _ in range(40):
        opps = await watch.scan()
    assert len(opps) == 28
    rows = await db.fetchall("SELECT grade, pair FROM opportunities")
    assert len(rows) == 40 * 28 and {g for g, _ in rows} <= set(w.GRADES)
    assert ("STRONG", "GGG/HHH") not in rows                   # never a real pair
    assert await db.fetchone("SELECT COUNT(*) FROM shadow_trades")
    await db.close()


async def test_jarvis_tools_read_and_control(make_bot, cfg, tmp_path):
    bot, _, broker = make_bot()
    await bot.start()
    try:
        await _jarvis_checks(bot, broker)
    finally:
        await bot.db.close()                                   # an open db thread would hang pytest on failure


async def _jarvis_checks(bot, broker):
    await bot.run(max_cycles=400)
    path = bot.cfg.db_path
    wc = dataclasses.replace(w.watch_settings(bot.cfg, 15), hedge_lookback=120, z_lookback=30, max_holding_bars=60)
    watch = w.Watcher(wc, w.SimSource(warm=600), bot.db, history_bars=300, refresh_seconds=1e12, all_hours=True)
    for _ in range(30):
        await watch.scan()

    assert mcp_server.bot_status(path).startswith("Running")
    assert "Paper equity $" in mcp_server.bot_status(path)
    assert "Market watch" in mcp_server.market_watch(path=path)
    assert "AAA/BBB is" in mcp_server.explain_pair("aaa:bbb", path)
    assert "trading bot lately" in mcp_server.explain_pair("KO/PEP", path)
    assert "Paper trades:" in mcp_server.recent_trades(path=path)
    assert mcp_server.profit_summary(path).count("trades") == 3
    assert "grade" in mcp_server.demo_results(path) or "No demo trades" in mcp_server.demo_results(path)

    assert "Not done" in mcp_server.emergency_stop(False, path)
    mcp_server.pause_trading(path)
    await bot.run(max_cycles=5)
    assert bot.paused == "jarvis"
    assert "PAUSED" in mcp_server.bot_status(path)
    mcp_server.resume_trading(path)
    mcp_server.emergency_stop(True, path)
    await bot.cycle()
    assert bot.halted == "emergency stop: jarvis"
    assert all(q == 0 for q in broker.qty.values())


def test_jarvis_tools_without_a_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        mcp_server.bot_status(str(tmp_path / "missing.db"))


def test_mcp_server_lists_its_tools():
    server = mcp_server.build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert {"get_bot_status", "get_market_watch", "explain", "pause", "resume", "stop_everything"} <= names


async def test_ntfy_push(monkeypatch):
    sent = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=10):
        sent.append(req)
        return Resp()
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    n = Notifier(ntfy_topic="pairs-test-123")
    assert n.enabled and not n.telegram
    await n.send("OPEN KO/PEP")
    await n.send("HALTED: daily loss limit")
    assert [r.full_url for r in sent] == ["https://ntfy.sh/pairs-test-123"] * 2
    assert sent[0].get_header("Priority") == "default" and sent[1].get_header("Priority") == "urgent"
    assert sent[1].data == b"[pairs-bot] HALTED: daily loss limit"
