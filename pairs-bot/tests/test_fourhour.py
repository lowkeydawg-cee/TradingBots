"""The 4-hour challengers: clean data only, closed bars only, the market gate, sizing and the demo book."""

import sqlite3
from datetime import timedelta

import numpy as np
import pandas as pd

import fourhour as f
from longonly import LOOKBACK, NOTIONAL, Holding


def bars(prices, start="2026-01-01"):
    prices = list(prices)
    return pd.Series(prices, index=pd.date_range(start, periods=len(prices), freq="4h", tz="UTC"), dtype=float)


def rising(n=600, step=0.002, start=100.0):
    return bars(start * np.exp(step * np.arange(n)))


def test_usable_drops_everything_before_a_gap_or_an_impossible_move():
    s = bars(range(100, 130))
    longer = pd.concat([s.iloc[:10], s.iloc[10:] .set_axis(s.index[10:] + timedelta(days=30))])
    assert f.usable(longer).index[0] == longer.index[10]
    jump = s.copy()
    jump.iloc[15] = jump.iloc[14] * 7
    assert f.usable(jump).index[0] >= s.index[15]
    assert len(f.usable(s)) == len(s)


def test_a_bar_still_forming_is_left_out():
    s = bars([1.0, 2.0, 3.0])
    assert len(f.closed_bars(s, s.index[-1].to_pydatetime() + timedelta(hours=3))) == 2
    assert len(f.closed_bars(s, s.index[-1].to_pydatetime() + timedelta(hours=4))) == 3


def test_market_gate_needs_a_rising_market_and_enough_history():
    assert f.market_open_to_buy(rising())
    assert not f.market_open_to_buy(bars(100 * np.exp(-0.002 * np.arange(600))))
    assert not f.market_open_to_buy(rising(n=60))


def test_position_size_shrinks_when_the_coin_is_wild():
    calm = bars(100 + np.sin(np.arange(600)) * 0.01)
    wild = bars(100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.08, 600))))
    assert f.position_size(calm) == NOTIONAL
    assert f.position_size(wild) < NOTIONAL / 2


def test_a_buy_signal_in_a_falling_market_is_blocked_with_a_reason(monkeypatch):
    monkeypatch.setattr(f, "decide", lambda *a: ("buy", "signal"))
    falling = bars(100 * np.exp(-0.002 * np.arange(600)))
    assert f.step("breakout4h", falling, None) == (None, "blocked: below its 50-day average", 0.0)
    action, why, dollars = f.step("breakout4h", rising(), None)
    assert (action, why) == ("buy", "signal") and 0 < dollars <= NOTIONAL


def test_selling_is_never_gated():
    falling = bars(100 * np.exp(-0.01 * np.arange(LOOKBACK + 5)))
    action, _, _ = f.step("trend4h", falling, Holding(200.0, 200.0, 3))
    assert action == "sell"


def test_book_buys_keeps_and_sells_across_a_restart(tmp_path):
    path = str(tmp_path / "bot.db")
    up = rising()
    book = f.Book(path)
    assert any("bought" in e for e in book.update("BTCUSD", up))
    assert book.update("BTCUSD", up) == []                       # same bar: nothing new
    book.close()
    book = f.Book(path)
    assert ("trend4h", "BTCUSD") in book.open
    fall = pd.concat([up, bars(up.iloc[-1] * np.exp(-0.03 * np.arange(1, 40)), start=str(up.index[-1] + timedelta(hours=4)))])
    events = [e for i in range(len(up) + 1, len(fall) + 1) for e in book.update("BTCUSD", fall.iloc[:i])]
    book.close()
    assert any("sold" in e for e in events)
    rows = sqlite3.connect(path).execute("SELECT strategy, market, net_pnl FROM long_trades WHERE closed_at IS NOT NULL").fetchall()
    assert {r[0] for r in rows} <= set(f.CHALLENGERS) and rows[0][1] == "Crypto"
    assert all(r[2] is not None for r in rows)


def test_replay_ignores_a_fake_jump_in_the_history():
    clean = rising(800)
    fake = clean.copy()
    fake.iloc[300:] = fake.iloc[300:] * 7                         # like the hole in SOL's history
    assert f.usable(fake).index[0] >= clean.index[300]
    assert sum(f.replay(fake, "trend4h", "BTCUSD")) < NOTIONAL * 6      # the 7x jump is never booked as profit
