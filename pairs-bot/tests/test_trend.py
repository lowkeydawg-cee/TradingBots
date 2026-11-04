"""The long-only trend demo: entries and exits, next-bar fills, costs, restarts and the comparison report."""

import sqlite3

import numpy as np
import pandas as pd

import trend as t
from backtest import Costs
from watcher import CRYPTO_COSTS

FREE = Costs(spread_bps=0.0, sec_fee_rate=0.0, taf_per_share=0.0, taf_max=0.0, borrow_rate_annual=0.0)


def series(prices):
    prices = list(prices)
    return pd.Series(prices, index=pd.date_range("2026-10-01", periods=len(prices), freq="15min", tz="UTC"), dtype=float)


def test_wants_long_needs_enough_bars_and_a_rising_average():
    assert not t.wants_long(np.array([1.0, 2.0]), 1, 3)
    assert t.wants_long(np.array([1.0, 2.0, 3.0]), 1, 3)
    assert not t.wants_long(np.array([3.0, 2.0, 1.0]), 1, 3)


def test_replay_fills_on_the_bar_after_the_signal():
    trades = t.replay(series([10, 10, 10, 11, 12, 13, 9, 8]), 0, 1, 3, FREE, notional=100)
    assert len(trades) == 1
    first = trades[0]
    assert first.entry_price == 12        # 11 is the first rise; the fill is the next bar
    assert first.exit_price == 8          # 9 turns it off; the fill is the next bar
    assert abs(first.gross_pnl - (100 / 12) * (8 - 12)) < 1e-9


def test_a_flat_market_never_trades_and_a_rise_held_to_the_end_is_closed():
    assert t.replay(series([5] * 20), 0, 2, 5, FREE) == []
    trades = t.replay(series(range(1, 21)), 0, 2, 5, FREE)
    assert len(trades) == 1 and trades[0].exit_price == 20


def test_costs_come_off_every_trade():
    prices = series([10, 10, 10, 11, 12, 13, 9, 8])
    free = t.replay(prices, 0, 1, 3, FREE)[0]
    paid = t.replay(prices, 0, 1, 3, CRYPTO_COSTS)[0]
    assert free.gross_pnl == paid.gross_pnl and free.costs == 0
    assert abs(paid.gross_pnl - paid.net_pnl - paid.costs) < 1e-9
    assert paid.costs > 2_000 * 0.003     # 0.3% on the $2,000 entry alone


def test_trades_before_the_start_are_ignored():
    prices = series(range(1, 21))
    start = prices.index[10].timestamp()
    assert t.replay(prices, start, 2, 5, FREE)[0].opened_at == start


def test_buy_and_hold_is_the_move_less_costs():
    assert t.buy_and_hold(series([100, 110]), 0, FREE, notional=1_000) == 100
    assert t.buy_and_hold(series([100, 110]), 0, CRYPTO_COSTS, notional=1_000) < 100


def test_book_opens_closes_and_survives_a_restart(tmp_path):
    path = str(tmp_path / "bot.db")
    book = t.TrendBook(path)
    assert book.step("BTCUSD", series([10, 10, 11, 12]), 1, 3, FREE) == "opened"
    assert book.step("BTCUSD", series([10, 10, 11, 12, 13]), 1, 3, FREE) is None     # already in
    book.close()
    book = t.TrendBook(path)
    assert "BTCUSD" in book.open
    assert book.step("BTCUSD", series([13, 12, 11, 8, 8]), 1, 3, FREE) == "closed"
    book.close()
    row = sqlite3.connect(path).execute("SELECT market, entry_price, exit_price, net_pnl FROM trend_trades").fetchone()
    assert row[:3] == ("Crypto", 12, 8)
    assert abs(row[3] - (2_000 / 12) * (8 - 12)) < 1e-9


def test_results_put_trend_beside_pairs_and_skip_simulated_pairs(tmp_path):
    path = str(tmp_path / "bot.db")
    t.TrendBook(path).close()
    assert "No trend demo trades yet" in t.trend_results(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO trend_trades (symbol, market, opened_at, entry_price, qty, closed_at, exit_price, "
                 "gross_pnl, costs, net_pnl) VALUES ('BTCUSD','Crypto',1,10,1,2,12,2,0.5,1.5)")
    conn.execute("INSERT INTO trend_trades (symbol, market, opened_at, entry_price, qty, costs) "
                 "VALUES ('ETHUSD','Crypto',1,10,1,0.5)")
    conn.execute("CREATE TABLE shadow_trades (pair TEXT, closed_at REAL, net_pnl REAL)")
    conn.executemany("INSERT INTO shadow_trades VALUES (?,?,?)", [("BTCUSD/ETHUSD", 1, -4.0), ("SIMA/SIMB", 1, 99.0)])
    conn.commit()
    conn.close()
    report = t.trend_results(path)
    assert "Crypto: trend 1 closed trades, 100% won, net $+1.50; pairs 1 demo trades, net $-4.00." in report
    assert "Holding now: ETHUSD." in report and "too few" in report
