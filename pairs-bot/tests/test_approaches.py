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
    assert any("Beating the buy-and-hold benchmark: Trend, Pairs (STRONG), Pairs (all signals)" in x for x in lines)


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
    assert [t.label for t in at.tabs][:2] == ["By market", "When to trade"]


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


def test_page_renders_with_only_open_trades(tmp_path, monkeypatch):
    """A fresh start: hold buys on the first scan, so trades are open and none has closed."""
    path = str(tmp_path / "bot.db")
    make_db(path, longs=[("hold", None), ("trend", None)])
    monkeypatch.setenv("BOT_MODE", "sim")
    monkeypatch.setenv("PAIRS_DB_PATH", path)
    st.cache_resource.clear()
    at = AppTest.from_file(PAGE, default_timeout=30).run()
    assert not at.exception
