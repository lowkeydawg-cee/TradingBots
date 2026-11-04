"""The look-back report: when each market was good, and the entries you could have taken."""

import sqlite3
import time
from datetime import datetime, timezone

import pandas as pd

import mcp_server
from db_logger import SCHEMA
from timing import ReplaySource, timing_report

UTC = timezone.utc


def ts(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC).timestamp()


def make_db(tmp_path, opps, trades=(), scan_ts=None, name="bot.db"):
    path = str(tmp_path / name)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for bar, pair, sector, grade, z in opps:
        conn.execute("INSERT INTO opportunities (ts, bar_time, pair, sector, grade, zscore) VALUES (?,?,?,?,?,?)",
                     (scan_ts or bar, bar, pair, sector, grade, z))
    for pair, opened, closed, net in trades:
        conn.execute("INSERT INTO shadow_trades (pair, grade, direction, opened_at, entry_z, qty_a, qty_b, entry_a, "
                     "entry_b, closed_at, net_pnl) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (pair, "STRONG", "LONG_SPREAD", opened, -2.3, 1, 1, 1, 1, closed, net))
    conn.commit()
    conn.close()
    return path


def test_report_ranks_markets_hours_and_lists_entries(tmp_path):
    opps = []
    for day in (2, 3):
        for minute in (0, 15, 30, 45):
            opps += [(ts(day, 14, minute), "BTCUSD/ETHUSD", "crypto", "WATCH", 1.0),     # 14:00 healthy
                     (ts(day, 3, minute), "BTCUSD/ETHUSD", "crypto", "QUIET", 0.2),      # 03:00 broken
                     (ts(day, 15, minute), "GLD/SLV", "metals", "QUIET", 0.1)]
    opps.append((ts(3, 14, 15), "BTCUSD/ETHUSD", "crypto", "STRONG", -2.3))
    trades = [("BTCUSD/ETHUSD", ts(3, 14, 15), ts(3, 16), 42.5)]
    path = make_db(tmp_path, opps, trades)
    r = timing_report(path, days=7, tz=UTC, now=ts(4, 0))
    lines = r.splitlines()
    assert lines[1].startswith("1. Crypto") and "Best hours: 14:00-15:00" in lines[1] and "Weakest: 03:00" in lines[1]
    assert lines[2].startswith("2. Gold & metals")
    assert "Tue 14:15 BTCUSD/ETHUSD (z -2.3): demo trade made $42.50 in 1.8h" in r
    assert "not a forecast" in r and "Only 1 demo trades" in r


def test_report_with_nothing_logged_says_how_to_start(tmp_path):
    path = make_db(tmp_path, [])
    assert "timing.py --replay" in timing_report(path, now=ts(4, 0))


def test_old_or_simulated_data_is_flagged(tmp_path):
    path = make_db(tmp_path, [(ts(2, 14), "AAA/BBB", "sim", "WATCH", 0.0)], scan_ts=time.time() - 3 * 86_400)
    warn = mcp_server.freshness(path)
    assert "SIMULATED PRICES" in warn and "NOT LIVE" in warn
    fresh = make_db(tmp_path, [(ts(2, 14), "KO/PEP", "beverages", "WATCH", 0.0)], scan_ts=time.time(), name="b.db")
    assert mcp_server.freshness(fresh) == ""


def test_replay_source_opens_stocks_only_in_us_hours():
    idx = pd.date_range("2026-11-03 13:00", periods=16, freq="1h", tz="UTC")     # 8:00-23:00 New York
    stocks = pd.DataFrame({"KO": range(16), "PEP": range(16)}, index=idx, dtype=float)
    crypto = pd.DataFrame({"BTCUSD": range(16), "ETHUSD": range(16)}, index=idx, dtype=float)
    src = ReplaySource(stocks, crypto, {"beverages": ["KO", "PEP"], "crypto": ["BTCUSD", "ETHUSD"]}, idx[0].timestamp())
    src.t = idx[0]                                   # 8:00 New York: closed
    assert set(src.active_sectors) == {"crypto"} and src.closed_markets == ["US stocks"]
    src.t = idx[3]                                   # 11:00: open
    assert set(src.active_sectors) == {"beverages", "crypto"}
    import asyncio
    assert len(asyncio.run(src.closes(2))) == 2


def test_review_lists_every_entry_and_compares_rules(tmp_path):
    from review import verdict, write_review
    opps = [(ts(3, 14), "BTCUSD/ETHUSD", "crypto", "STRONG", -2.3), (ts(3, 15), "GLD/SLV", "metals", "RISKY", 2.4)]
    path = make_db(tmp_path, opps, [("BTCUSD/ETHUSD", ts(3, 14), ts(3, 16), 30.0),
                                    ("GLD/SLV", ts(3, 15), ts(3, 18), -12.0)])
    variants = pd.DataFrame([("current", "Crypto", "x", -5.0), ("current", "Gold & metals", "y", -5.0),
                             ("patient entry", "Crypto", "x", 200.0), ("patient entry", "Gold & metals", "y", 150.0)],
                            columns=["variant", "market", "pair", "net_pnl"])
    out = write_review(path, tmp_path / "review", variants, tz=UTC)
    text = out.read_text()
    assert "Tue 03 Nov 14:00 | BTCUSD/ETHUSD | Crypto | STRONG" in text and "$+30.00" in text and "$-12.00" in text
    assert "made $18.00 after costs" in text and "'patient entry' beat the current rules" in text
    assert (tmp_path / "review" / "trades.csv").exists()
