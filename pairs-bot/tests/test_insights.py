"""Market brief and alerts: what the watcher says about the market, and when it speaks up."""

from datetime import datetime

import pandas as pd
import pytest

import mcp_server
import watcher as w
from db_logger import DBLogger
from insights import NY, Insights, mood_of, read_market


def opp(pair, grade, sector="tech", vol=1.0, p=0.01, flags=()):
    return w.Opportunity(pair=pair, sector=sector, bar_time=0, grade=grade, side=None, zscore=0.0, coint_pvalue=p,
                         half_life=10, vol_ratio=vol, beta=1, hist_trades=10, hist_win_rate=0.6, prob=0.6,
                         hist_avg_net=1.0, breakeven_bps=5.0, flags=list(flags))


def ny(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=NY).timestamp()


def closes(today_move=0.0):
    idx = pd.DatetimeIndex([datetime(2026, 11, 2, 15, 45, tzinfo=NY), datetime(2026, 11, 3, 10, 0, tzinfo=NY)])
    return pd.DataFrame({s: [100.0, 100.0 * (1 + today_move)] for s in ("AAA", "BBB", "CCC", "DDD")}, index=idx)


def test_mood_and_read():
    assert (mood_of(0.5), mood_of(1.0), mood_of(2.0)) == ("calm", "normal", "stressed")
    opps = [opp("AAA/BBB", "STRONG"), opp("CCC/DDD", "AVOID", sector="banks", vol=2.5, p=0.4)]
    r = read_market(opps, closes(0.02), ny(3, 10))
    assert r.pairs == 2 and r.cointegrated == 1 and r.move == pytest.approx(0.02)
    text = r.text("open")
    assert "Market brief (open)" in text and "1 of 2 pairs" in text and "not a forecast" in text
    assert text.index("banks") < text.index("tech")          # most stressed sector first


def test_alerts_fire_once_on_real_changes():
    ins = Insights({"AAA/BBB"})
    _, alerts = ins.update([opp("AAA/BBB", "WATCH"), opp("CCC/DDD", "STRONG")], closes(), ny(3, 10))
    assert alerts == []                                       # first scan: nothing to compare with
    broken = [opp("AAA/BBB", "AVOID", p=0.3, flags=["not cointegrated now (p=0.30)"]),
              opp("CCC/DDD", "AVOID", vol=3, p=0.5)]
    read, alerts = ins.update(broken, closes(-0.02), ny(3, 10, 15))
    text = " | ".join(alerts)
    assert "PAIR BROKE AAA/BBB" in text and "The bot trades this pair" in text   # a traded pair
    assert "PAIR BROKE CCC/DDD" in text                                          # was STRONG
    assert "MARKET STRESSED" in text and "DECOUPLING" in text and "BIG MOVE" in text
    assert "AAA/BBB broke" in "; ".join(read.changes)
    _, again = ins.update(broken, closes(-0.025), ny(3, 10, 30))
    assert again == []                                        # same state, same day: quiet


def test_briefs_go_out_three_times_a_day():
    ins = Insights()
    assert ins.brief_due(ny(3, 9, 30)) is None                # before the first slot
    assert ins.brief_due(ny(3, 9, 45)) == "open"
    ins.mark_sent(ny(3, 9, 45), "open")
    assert ins.brief_due(ny(3, 11)) is None
    assert ins.brief_due(ny(3, 12, 30)) == "midday"
    ins.mark_sent(ny(3, 12, 30), "midday")
    assert ins.brief_due(ny(3, 16, 5)) == "close"
    assert ins.brief_due(ny(4, 9, 50)) == "open"              # next day starts again


async def test_watcher_stores_briefs_and_jarvis_reads_them(tmp_path):
    import dataclasses
    from config import load_settings
    base = load_settings(mode="sim", db_path=str(tmp_path / "w.db"))
    wc = dataclasses.replace(w.watch_settings(base, 15), hedge_lookback=120, z_lookback=30, max_holding_bars=60)
    db = DBLogger(base.db_path)
    await db.open()
    sent = []

    class Rec:
        async def send(self, text):
            sent.append(text)
    try:
        watch = w.Watcher(wc, w.SimSource(warm=600), db, history_bars=300, refresh_seconds=1e12,
                          all_hours=True, notifier=Rec())
        for _ in range(5):
            await watch.scan()
        assert watch.last_read is not None
        n = await db.fetchone("SELECT COUNT(*) FROM market_notes WHERE kind = 'brief'")
        assert n[0] == 5
    finally:
        await db.close()
    out = mcp_server.market_brief(base.db_path)
    assert "Market brief" in out and "Mood:" in out


async def test_telegram_market_command(make_bot):
    from telegram_commands import TelegramCommands
    bot, _, _ = make_bot()
    await bot.start()
    try:
        tg = TelegramCommands(bot, "t", "1", fetch=lambda m, p: {"result": []})
        assert "No market brief yet" in await tg.handle("/market")
        assert "/market" in await tg.handle("/help")
    finally:
        await bot.db.close()


def test_brief_lists_a_flapping_mood_once():
    ins = Insights()
    calm, normal = [opp("AAA/BBB", "WATCH", vol=0.5)], [opp("AAA/BBB", "WATCH", vol=1.0)]
    for i, o in enumerate([calm, normal, calm, normal, calm, normal]):
        read, _ = ins.update(o, closes(), ny(3, 10, i))
    assert [c for c in read.changes if c.startswith("mood")] == ["mood calm -> normal"]
    ins.mark_sent(ny(3, 10, 6), "open")
    read, _ = ins.update(calm, closes(), ny(3, 10, 7))
    assert [c for c in read.changes if c.startswith("mood")] == ["mood normal -> calm"]
    read, _ = ins.update(normal, closes(), ny(3, 10, 8))
    assert not [c for c in read.changes if c.startswith("mood")]        # back where the last brief left it
