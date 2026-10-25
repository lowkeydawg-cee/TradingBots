"""Phone control (Telegram commands) and the dashboard password."""

from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest

from telegram_commands import TelegramCommands

APP = str(Path(__file__).resolve().parents[1] / "app.py")


def updates(*msgs, chat=42):
    return {"ok": True, "result": [{"update_id": i + 1, "message": {"chat": {"id": chat}, "text": t}}
                                   for i, t in enumerate(msgs)]}


async def started_bot(make_bot, cycles=300):
    bot, market, broker = make_bot()
    await bot.start()
    await bot.run(max_cycles=cycles)
    return bot, broker


async def test_commands_answer_only_your_chat(make_bot):
    bot, _ = await started_bot(make_bot)
    calls = []
    tc = TelegramCommands(bot, "token", "42", fetch=lambda m, p: calls.append(p) or updates("/status", chat=999))
    assert await tc.poll_once(timeout=0) == 0             # a stranger's chat is ignored
    tc._fetch = lambda m, p: calls.append(p) or updates("/status", "hello", "/help")
    assert await tc.poll_once(timeout=0) == 2             # plain text is ignored too
    assert tc._offset == 4                                # next poll skips what was read
    status = [m for m in bot.notifier.sent if "Running" in m or "HALTED" in m]
    assert status and "KO/PEP" in status[-1] and "Equity $" in status[-1]
    await bot.db.close()


async def test_trades_pnl_and_unknown(make_bot):
    bot, _ = await started_bot(make_bot, cycles=600)
    tc = TelegramCommands(bot, "t", "42")
    trades = await tc.handle("/trades")
    assert "$" in trades and "KO/PEP" in trades
    pnl = await tc.handle("/pnl")
    assert pnl.count("trades") == 3 and "All time" in pnl
    assert "Unknown command" in await tc.handle("/nope")
    assert "/status" in await tc.handle("/help@MyPairsBot")
    await bot.db.close()


async def test_pause_blocks_new_trades_and_resume_allows_them(make_bot):
    bot, market, broker = make_bot()
    await bot.start()
    tc = TelegramCommands(bot, "t", "42")
    assert "Paused" in await tc.handle("/pause")
    await bot.run(max_cycles=400)
    assert bot.trade is None and not broker.orders
    import sqlite3
    with sqlite3.connect(bot.cfg.db_path) as c:
        assert c.execute("select count(*) from signals where note like 'paused by telegram%'").fetchone()[0] > 0
    await tc.handle("/resume")
    await bot.run(max_cycles=400)
    assert broker.orders                                 # trading again
    await bot.db.close()


async def test_stop_needs_confirmation_then_flattens(make_bot):
    bot, market, broker = make_bot()
    await bot.start()
    for _ in range(400):
        await bot.cycle()
        if bot.trade:
            break
    tc = TelegramCommands(bot, "t", "42")
    assert "confirm" in await tc.handle("/stop")
    await bot.cycle()
    assert not bot.halted                                 # nothing happened without confirm
    await tc.handle("/stop confirm")
    await bot.cycle()
    assert bot.halted == "emergency stop: telegram"
    assert all(q == 0 for q in broker.qty.values())
    assert "HALTED" in await tc.handle("/status")
    await bot.db.close()


async def test_dashboard_password(make_bot, monkeypatch):
    bot, _ = await started_bot(make_bot, cycles=50)
    await bot.db.close()
    monkeypatch.setenv("PAIRS_DB_PATH", bot.cfg.db_path)
    monkeypatch.setenv("BOT_MODE", "sim")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    st.cache_resource.clear()                             # settings are cached per process
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.metric                                  # nothing shown before the password
    at.text_input[0].input("wrong").run()
    assert at.error and not at.metric
    at.text_input[0].input("s3cret").run()
    assert not at.exception
    assert {"Equity", "Z-score"} <= {m.label for m in at.metric}
