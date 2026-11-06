"""The Approaches page: every demo strategy scored the same way, and the page itself renders."""

import sqlite3
from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest

import approaches as A
from db_logger import SCHEMA

PAGE = str(Path(__file__).resolve().parents[1] / "pages" / "1_Approaches.py")


def make_db(path, pairs=(), longs=()):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    for i, (grade, net) in enumerate(pairs):
        c.execute("INSERT INTO shadow_trades (pair, grade, direction, opened_at, entry_z, qty_a, qty_b, entry_a, entry_b,"
                  " closed_at, gross_pnl, costs, net_pnl, exit_reason) VALUES ('KO/PEP', ?, 'long', ?, 2, 1, 1, 1, 1, ?,"
                  " ?, 5, ?, 'reverted')", (grade, 1000 + i * 100, 1050 + i * 100, net + 5, net))
    c.execute("INSERT INTO opportunities (ts, pair, sector, bar_time, grade, zscore, prob) "
              "VALUES (1, 'KO/PEP', 'beverages', 1, 'STRONG', 2, 0.6)")
    for i, (strat, net) in enumerate(longs):
        closed = None if net is None else 1100 + i * 100
        c.execute("INSERT INTO long_trades (strategy, symbol, market, opened_at, entry, qty, closed_at, gross_pnl, costs,"
                  " net_pnl, exit_reason) VALUES (?, 'BTCUSD', 'Crypto', ?, 100, 1, ?, ?, 12, ?, 'stop')",
                  (strat, 1000 + i * 100, closed, None if net is None else net + 12, net))
    c.commit()
    c.close()


def test_scoreboard_puts_pairs_and_buy_only_side_by_side(tmp_path):
    db = str(tmp_path / "w.db")
    make_db(db, pairs=[("STRONG", 10), ("STRONG", -4), ("RISKY", -20), ("AVOID", 99)],
            longs=[("trend", 50), ("trend", -10), ("hold", -30), ("dip", None)])
    trades = A.load_trades(db)
    assert set(trades["approach"]) == {"Pairs (STRONG)", "Pairs (all signals)", "Trend", "Hold", "Dip"}
    assert trades.loc[trades["approach"] == "Pairs (STRONG)", "market"].iloc[0] == "US stocks"
    board = A.scoreboard(trades).set_index("approach")
    assert board.loc["Trend", "net"] == 40 and board.loc["Trend", "trades"] == 2
    assert board.loc["Pairs (STRONG)", "net"] == 6
    assert board.loc["Pairs (all signals)", "trades"] == 3            # AVOID is the control group, left out
    assert board.loc["Dip", "trades"] == 0 and board.loc["Dip", "open now"] == 1
    assert board.loc["Trend", "worst drawdown"] == -10
    head, lines = A.verdict(A.scoreboard(trades))
    assert head.startswith("Trend is ahead: +$40.00 over 2 trades") and "Too few" in head
    assert any("Buy-and-hold is at -$30.00" in x and x.endswith("Trend, Pairs (STRONG), Pairs (all signals).") for x in lines)


def test_verdict_with_nothing_yet_and_missing_db(tmp_path):
    assert A.load_trades(str(tmp_path / "none.db")).empty
    head, _ = A.verdict(A.scoreboard(A.load_trades(str(tmp_path / "none.db"))))
    assert head == "No demo trade has closed yet."
    assert A.money(-33.067) == "-$33.07" and A.money(652.6, 0) == "+$653"


def test_page_renders(tmp_path, monkeypatch):
    db = str(tmp_path / "w.db")
    make_db(db, pairs=[("STRONG", 10), ("RISKY", -20)], longs=[("trend", 50), ("hold", -30)])
    monkeypatch.setenv("PAIRS_DB_PATH", db)
    monkeypatch.setenv("BOT_MODE", "sim")
    st.cache_resource.clear()
    at = AppTest.from_file(PAGE, default_timeout=60).run()
    assert not at.exception
    assert any("Trend is ahead" in m.value for m in at.markdown)
    assert {m.label for m in at.metric} >= {"Trend", "Hold", "Pairs (STRONG)"}
    assert [t.label for t in at.tabs][:3] == ["Why it did (or didn't) trade", "Validation", "By market"]


def test_page_shows_risk_state_and_why_it_did_nothing(tmp_path, monkeypatch):
    db = str(tmp_path / "w.db")
    make_db(db)
    c = sqlite3.connect(db)
    c.execute("INSERT INTO long_sleeves (strategy, value, peak, drawdown, state, stopped, updated_at) "
              "VALUES ('trend', 9300, 10000, 0.07, 'caution', 0, 5000)")
    c.executemany("INSERT INTO long_decisions (ts, strategy, symbol, action, reason, price, qty, state) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  [(4000, "trend", "BTCUSD", "buy", "uptrend; risk 1% of the sleeve", 100, 3, "normal"),
                   (5000, "trend", "BTCUSD", "wait", "waiting: market not rising (down)", 90, 0, "caution"),
                   (5000, "dip", "ETHUSD", "refused", "dip, but refused: sleeve is in defense", 50, 0, "defense")])
    c.commit()
    c.close()
    last = A.latest_decisions(db).set_index("strategy")
    assert last.loc["trend", "action"] == "wait" and "not rising" in last.loc["trend", "reason"]
    assert list(A.recent_actions(db)["action"]) == ["refused", "buy"]
    monkeypatch.setenv("PAIRS_DB_PATH", db)
    monkeypatch.setenv("BOT_MODE", "sim")
    st.cache_resource.clear()
    at = AppTest.from_file(PAGE, default_timeout=60).run()
    assert not at.exception
    assert any("Trend" in m.label and m.value == "$9,300" for m in at.metric)
    assert any(s.value == "Why it hasn't traded yet" for s in at.subheader)
    assert any("not rising" in str(df.value) for df in at.dataframe)


def test_page_explains_the_approaches_before_any_trade(tmp_path, monkeypatch):
    monkeypatch.setenv("PAIRS_DB_PATH", str(tmp_path / "none.db"))
    monkeypatch.setenv("PAIRS_REPLAY_DB", str(tmp_path / "no-replay.db"))
    monkeypatch.setenv("BOT_MODE", "sim")
    st.cache_resource.clear()
    at = AppTest.from_file(PAGE, default_timeout=60).run()
    assert not at.exception
    table = at.table[0].value
    assert list(table.index) == ["Pairs", "Trend", "Dip", "Breakout", "Hold"]
    assert "Real orders on Alpaca" in table.columns
    assert any("No results yet" in i.value for i in at.info)



def test_page_works_when_positions_are_open_but_none_has_closed(tmp_path, monkeypatch):
    db = str(tmp_path / "w.db")
    make_db(db, longs=[("hold", None), ("trend", None)])         # a fresh start: hold buys on the first scan
    board = A.scoreboard(A.load_trades(db))
    assert set(board["approach"]) == {"Hold", "Trend"} and board["trades"].sum() == 0
    head, lines = A.verdict(board)
    assert head == "No demo trade has closed yet." and lines[0].startswith("2 demo positions are open")
    monkeypatch.setenv("PAIRS_DB_PATH", db)
    monkeypatch.setenv("BOT_MODE", "sim")
    st.cache_resource.clear()
    at = AppTest.from_file(PAGE, default_timeout=60).run()
    assert not at.exception
    assert {m.label for m in at.metric} >= {"Hold", "Trend"}


def test_open_buy_only_positions_count_at_their_live_value(tmp_path):
    db = str(tmp_path / "w.db")
    make_db(db, pairs=[("STRONG", 10)], longs=[("trend", 50)])
    c = sqlite3.connect(db)
    c.execute("INSERT INTO long_trades (strategy, symbol, market, opened_at, entry, qty, costs, last_price) "
              "VALUES ('hold', 'BTCUSD', 'Crypto', 1000, 100, 2, 1, 130)")      # +60 move, 2 x 1 costs
    c.commit()
    c.close()
    board = A.scoreboard(A.load_trades(db)).set_index("approach")
    assert board.loc["Hold", "open P&L"] == 58 and board.loc["Hold", "total"] == 58
    assert board.loc["Trend", "total"] == 50
    _, lines = A.verdict(board.reset_index())
    assert any("Buy-and-hold is at +$58.00" in x and "none so far" in x for x in lines)


async def test_live_book_keeps_the_latest_price_of_what_it_holds(tmp_path):
    import numpy as np
    import pandas as pd

    import longonly as L
    from db_logger import DBLogger
    db = DBLogger(str(tmp_path / "l.db"))
    await db.open()
    book = L.LongBook(db, symbols=("BTCUSD",), strategies=("hold",))
    idx = pd.date_range("2026-11-01", periods=L.LOOKBACK, freq="15min", tz="UTC")
    closes = pd.DataFrame({"BTCUSD": np.linspace(100, 110, L.LOOKBACK)}, index=idx)
    await book.update(closes, {"BTCUSD"})
    nxt = pd.concat([closes, pd.DataFrame({"BTCUSD": [120.0]}, index=[idx[-1] + pd.Timedelta(minutes=15)])])
    await book.update(nxt, {"BTCUSD"})
    rows = await db.fetchall("SELECT entry, last_price FROM long_trades WHERE closed_at IS NULL")
    await db.close()
    assert rows == [(110.0, 120.0)]


async def test_heartbeat_alerts_once_when_the_watcher_stops_and_once_when_back(tmp_path):
    from heartbeat import Heartbeat
    db = str(tmp_path / "w.db")
    make_db(db)                                       # one scan at ts=1

    class Phone:
        sent = []

        async def send(self, text):
            self.sent.append(text)
    phone = Phone()
    hb = Heartbeat(db, phone, stale_minutes=45)
    assert (await hb.check(now=1 + 60)).startswith("ok")
    assert (await hb.check(now=1 + 3600)).startswith("DOWN")
    await hb.check(now=1 + 7200)
    assert len(phone.sent) == 1 and "stopped" in phone.sent[0]
    c = sqlite3.connect(db)
    c.execute("INSERT INTO opportunities (ts, pair, sector, bar_time, grade, zscore, prob) "
              "VALUES (7300, 'KO/PEP', 'beverages', 1, 'WATCH', 0, 0.5)")
    c.commit()
    c.close()
    assert (await hb.check(now=7400)).startswith("ok")
    assert phone.sent[-1] == "Trading watcher is scanning again." and len(phone.sent) == 2
    fresh = Heartbeat(str(tmp_path / "none.db"), phone)
    assert await fresh.check() == "waiting for the first scan"


def test_right_now_shows_what_is_held_and_where(tmp_path, monkeypatch):
    db = str(tmp_path / "w.db")
    make_db(db, longs=[("hold", None)])
    c = sqlite3.connect(db)
    c.execute("UPDATE long_trades SET last_price = 110, costs = 1")
    c.execute("INSERT INTO market_notes (ts, kind, mood, text) VALUES (1, 'brief', 'normal', 'Title\nMood: normal.\n- crypto: -1%\nNot a forecast.')")
    c.commit()
    c.close()
    held = A.holdings(db)
    assert list(held["What"]) == ["BTCUSD"] and list(held["Market"]) == ["Crypto"]
    assert held["Value"].iloc[0] == 110 and held["Profit if sold now"].iloc[0] == 8    # up $10, less about 2 x $1 costs
    assert A.latest_brief(db) == ["Mood: normal.", "- crypto: -1%"]
    monkeypatch.setenv("PAIRS_DB_PATH", db)
    monkeypatch.setenv("BOT_MODE", "sim")
    st.cache_resource.clear()
    at = AppTest.from_file(PAGE, default_timeout=60).run()
    assert not at.exception
    assert at.subheader[0].value == "Right now"
    assert {m.label for m in at.metric} >= {"Open positions", "Worth now", "Profit if sold now"}
