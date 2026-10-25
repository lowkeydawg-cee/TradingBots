"""Render the real Streamlit app headlessly against a database the bot produced."""

from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "app.py")


async def test_dashboard_renders_and_stop_button_writes_flag(make_bot, monkeypatch):
    bot, _, _ = make_bot()
    await bot.start()
    await bot.run(max_cycles=300)
    await bot.db.close()
    monkeypatch.setenv("PAIRS_DB_PATH", bot.cfg.db_path)
    monkeypatch.setenv("BOT_MODE", "sim")

    st.cache_resource.clear()                             # settings are cached per process
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    labels = {m.label for m in at.metric}
    assert {"Equity", "Today's P&L", "Z-score", "Coint. p-value"} <= labels
    assert at.table[0].value.shape[0] == 7           # performance metrics table

    at.checkbox[0].check().run()
    at.button[0].click().run()
    assert not at.exception
    from db_logger import connect_readonly
    c = connect_readonly(bot.cfg.db_path)
    assert c.execute("select value from control where key='emergency_stop'").fetchone()[0] == "dashboard button"
