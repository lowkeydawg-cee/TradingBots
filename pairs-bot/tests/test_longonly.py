"""Buy-only strategies for crypto and gold: decisions, costs, the live demo book and the comparison."""

import asyncio

import numpy as np
import pandas as pd

import longonly as lo
from db_logger import DBLogger


def series(values, sym="BTCUSD"):
    idx = pd.date_range("2026-10-01", periods=len(values), freq="15min", tz="UTC")
    return pd.Series(np.asarray(values, float), index=idx, name=sym)


def up_then_down(n=900):
    rng = np.random.default_rng(1)
    r = np.where(np.arange(n) < n // 2, 0.002, -0.002) + rng.normal(0, 0.0015, n)
    return 100 * np.exp(np.cumsum(r))


def test_trend_buys_in_an_uptrend_and_sells_when_it_turns():
    x = up_then_down()
    assert lo.decide("trend", x[:300], None)[0] == "buy"
    h = lo.Holding(entry=x[300], peak=x[449])
    assert lo.decide("trend", x[:700], h)[0] == "sell"
    assert lo.decide("hold", x[:300], None)[0] == "buy" and lo.decide("hold", x[:700], h)[0] is None


def test_dip_only_buys_a_sharp_drop_inside_an_uptrend():
    x = list(100 * np.exp(np.cumsum(np.full(260, 0.003))))
    for _ in range(12):                                           # twelve bars down 1% each: RSI collapses
        x.append(x[-1] * 0.99)
    assert lo.rsi(np.array(x)) < 30
    assert lo.decide("dip", np.array(x), None)[0] == "buy"
    down = np.array(x[::-1])                                      # same dip in a downtrend: no buy
    assert lo.decide("dip", down, None)[0] is None


def test_backtest_fills_next_bar_and_charges_crypto_costs():
    s = series(up_then_down())
    trades = lo.backtest(s, "trend", lo.CRYPTO_COSTS)
    assert trades and all(t.closed_at is not None for t in trades)
    first = trades[0]
    i = s.index.get_loc(first.opened_at)
    assert first.entry == s.iloc[i]                               # the fill bar's own close, decided one bar earlier
    assert abs(first.costs - 2 * 0.003 * 2000) < 2 * 0.003 * 2000 * 0.5 + 1
    total = sum(t.net for t in trades)
    hold = sum(t.net for t in lo.backtest(s, "hold", lo.CRYPTO_COSTS))
    assert total > hold                                           # it got out before the fall; holding gave it back


def test_live_book_opens_closes_and_compares(tmp_path):
    async def run():
        db = DBLogger(str(tmp_path / "bot.db"))
        await db.open()
        try:
            book = lo.LongBook(db, symbols=("BTCUSD",), strategies=("trend", "hold"))
            x = up_then_down()
            events = []
            for i in range(300, 900, 4):
                events += await book.update(series(x[:i]).to_frame(), {"BTCUSD"})
            again = await book.update(series(x[:896]).to_frame(), {"BTCUSD"})   # same bar: no double trading
            assert again == []
            assert any("trend bought BTCUSD" in e for e in events) and any("trend sold BTCUSD" in e for e in events)
            closed = await db.fetchall("SELECT strategy, net_pnl FROM long_trades WHERE closed_at IS NOT NULL")
            assert closed and all(s == "trend" for s, _ in closed)
            assert await db.fetchall("SELECT 1 FROM long_trades WHERE strategy='hold' AND closed_at IS NULL")
            assert await book.update(series(x[:950]).to_frame(), set()) == []          # market closed: nothing
            await book.close_all(series(x).to_frame())
            assert await db.fetchall("SELECT 1 FROM long_trades WHERE strategy='hold' AND closed_at IS NOT NULL")
        finally:
            await db.close()
    asyncio.run(run())
    text = lo.comparison_text(str(tmp_path / "bot.db"))
    assert "buy-only trend" in text and "benchmark" in text and "not a forecast" in text


def test_compare_on_past_prices():
    closes = pd.DataFrame({"BTCUSD": up_then_down(), "GLD": up_then_down()[::-1]},
                          index=series(up_then_down()).index)
    trades = lo.compare(closes, ["BTCUSD", "GLD"])
    assert set(trades["strategy"]) <= set(lo.STRATEGIES) and {"Crypto", "Gold & metals"} <= set(trades["market"])
    assert list(lo.summary_table(trades).columns) == ["trades", "won", "net", "avg", "win_rate"]
