"""Buy-only strategies: 4-hour decisions, the market filter, the risk layer, the live demo book and validation."""

import asyncio
import sqlite3

import numpy as np
import pandas as pd

import governance as gv
import longonly as lo
from db_logger import DBLogger


def series(values, sym="BTCUSD", freq="4h"):
    idx = pd.date_range("2026-01-01", periods=len(values), freq=freq, tz="UTC")
    return pd.Series(np.asarray(values, float), index=idx, name=sym)


def up_then_down(n=600, seed=1):
    rng = np.random.default_rng(seed)
    r = np.where(np.arange(n) < n // 2, 0.006, -0.006) + rng.normal(0, 0.004, n)
    return 100 * np.exp(np.cumsum(r))


# -- bars and the market filter -------------------------------------------------
def test_four_hour_bars_drop_the_candle_still_forming():
    s = series(np.arange(1.0, 20.0), freq="15min")        # 19 bars: 04:00 bin is complete, 08:00 bin is not
    bars = lo.to_bars(s)
    assert list(bars.index) == [pd.Timestamp("2026-01-01 04:00", tz="UTC")] and bars.iloc[0, 0] == 16.0
    full = lo.to_bars(series(np.arange(1.0, 33.0), freq="15min"))
    assert len(full) == 2 and full.iloc[-1, 0] == 32.0


def test_regime_reads_rising_falling_and_wild_markets():
    x = up_then_down()
    assert lo.regime(x[:290])[0] == "up" and lo.regime(x[:590])[0] == "down"
    calm = 100 * np.exp(np.cumsum(np.random.default_rng(2).normal(0.002, 0.003, 300)))
    wild = calm.copy()
    wild[-30:] = wild[-31] * np.exp(np.cumsum(np.random.default_rng(3).normal(0, 0.03, 30)))
    assert lo.regime(calm)[1] == "calm" and lo.regime(wild)[1] == "wild"


# -- rules -----------------------------------------------------------------------
def test_trend_buys_in_a_rising_market_and_sells_when_it_turns():
    x = up_then_down()
    assert lo.decide("trend", x[:280], None)[0] == "buy"
    h = lo.Holding(entry=x[280], peak=x[299])
    assert lo.decide("trend", x[:420], h)[0] == "sell"
    action, why = lo.decide("trend", x[:500], None)
    assert action is None and "not rising" in why                     # no buying into a falling market
    assert lo.decide("hold", x[:10], None)[0] == "buy" and lo.decide("hold", x[:500], h)[0] is None
    assert "warming up" in lo.decide("trend", x[:50], None)[1]


def test_dip_only_buys_a_sharp_drop_in_a_rising_calm_market():
    x = list(100 * np.exp(np.cumsum(np.full(260, 0.004) + np.random.default_rng(4).normal(0, 0.003, 260))))
    for _ in range(8):                                                # eight bars down 2%: RSI collapses
        x.append(x[-1] * 0.98)
    x = np.array(x)
    assert lo.rsi(x) < 30 and lo.regime(x)[0] == "up"
    assert lo.decide("dip", x, None)[0] == "buy"
    action, why = lo.decide("dip", x[::-1].copy(), None)             # same shape falling: no buy
    assert action is None and "not rising" in why


def test_breakout_needs_a_clear_new_high_and_waits_after_a_loss():
    x = list(100 * np.exp(np.cumsum(np.full(250, 0.003) + np.random.default_rng(5).normal(0, 0.002, 250))))
    x.append(max(x[-31:]) * 1.03)
    x = np.array(x)
    assert lo.decide("breakout", x, None)[0] == "buy"
    assert "cooling down" in lo.decide("breakout", x, None, since_loss=2)[1]
    assert lo.decide("breakout", x, None, since_loss=10)[0] == "buy"
    just_above = x.copy()
    just_above[-1] = max(x[-31:-1]) * 1.0001                          # a hair above the old high: not enough
    assert lo.decide("breakout", just_above, None)[0] is None


# -- risk layer ------------------------------------------------------------------
def test_sizing_risks_one_percent_and_respects_the_caps():
    s = gv.Sleeve("trend")
    qty, why = s.size("GLD", 100.0, 0.03, crypto=False)               # 6% adverse move: $100 risk / $6 a share
    assert abs(qty - 100 / 6) < 1e-9 and "risk 1%" in why
    qty, why = s.size("GLD", 100.0, 0.001, crypto=False)              # calm asset: capped at 25% of the sleeve
    assert abs(qty * 100 - 2_500) < 1e-6 and "25% per asset" in why
    s.positions["BTCUSD"] = gv.Position("BTCUSD", 40, 100, 100, last=100)   # $4,000 in crypto
    qty, why = s.size("ETHUSD", 100.0, 0.001, crypto=True)
    assert abs(qty * 100 - 1_000) < 1e-6 and "crypto" in why          # 50% crypto cap leaves $1,000
    wild_qty, _ = gv.Sleeve("trend").size("SOLUSD", 100.0, 0.05, crypto=True)
    calm_qty, _ = gv.Sleeve("trend").size("SOLUSD", 100.0, 0.02, crypto=True)
    assert wild_qty < calm_qty                                        # wilder coin, smaller position


def test_drawdown_states_step_down_and_never_add_risk_while_losing():
    s = gv.Sleeve("trend")
    s.positions["GLD"] = gv.Position("GLD", 50, 100, 100, last=100)  # $5,000 of a $10,000 sleeve
    full, _ = s.size("SLV", 50.0, 0.03, crypto=False)
    assert s.mark({"GLD": 88}) == "caution"                           # -$600 = 6% down
    half, why = s.size("SLV", 50.0, 0.03, crypto=False)
    assert "halved" in why and half < full
    assert s.mark({"GLD": 79}) == "defense"
    assert s.size("SLV", 50.0, 0.03, crypto=False)[0] == 0
    assert s.mark({"GLD": 69}) == "stopped"
    assert s.mark({"GLD": 100}) == "stopped"                          # stays stopped until a manual reset
    s.reset()
    assert s.state() == "normal"


# -- backtest --------------------------------------------------------------------
def test_simulation_fills_next_bar_charges_costs_and_journals_why():
    s = series(up_then_down())
    bars = lo.to_bars(s)
    res = lo.simulate(bars, "trend", journal=True)
    assert res.trades and all(t.closed_at is not None for t in res.trades)
    first = res.trades[0]
    assert first.entry == bars.loc[first.opened_at, "BTCUSD"]
    buy_row = next(d for d in res.decisions if d.action == "buy")
    assert buy_row.ts < first.opened_at                               # decided one bar before the fill
    assert first.costs > 0 and abs(first.costs - 2 * 0.003 * first.qty * first.entry) < first.costs
    assert any(d.action == "wait" and "not rising" in d.reason for d in res.decisions)
    hold = lo.simulate(bars, "hold")
    assert res.equity.iloc[-1] > hold.equity.iloc[-1]                 # it got out before the fall; holding gave it back
    assert lo.metrics(res)["trades"] == len(res.trades)


def test_a_stopped_sleeve_sells_everything_and_buys_nothing(tmp_path):
    async def run():
        db = DBLogger(str(tmp_path / "s.db"))
        await db.open()
        try:
            book = lo.LongBook(db, symbols=("BTCUSD",), strategies=("trend",))
            x = up_then_down()
            for i in range(150, 300, 3):
                await book.update(series(x[:i]).to_frame(), {"BTCUSD"})
            assert book.open                                          # bought in the rise
            book.sleeves["trend"].stopped = True
            events = await book.update(series(x[:303]).to_frame(), {"BTCUSD"})
            assert any("drawdown stop" in e for e in events) and not book.open
            for i in range(303, 320, 3):
                assert not any("bought" in e for e in await book.update(series(x[:i]).to_frame(), {"BTCUSD"}))
            assert dict(await db.fetchall("SELECT strategy, state FROM long_sleeves")) == {"trend": "stopped"}
        finally:
            await db.close()
    asyncio.run(run())


def test_compare_and_validate_on_past_prices():
    closes = lo._sim_closes(2600, freq="1h")
    trades = lo.compare(closes, list(closes.columns))
    assert set(trades["strategy"]) <= set(lo.STRATEGIES) and {"Crypto", "Gold & metals"} <= set(trades["market"])
    assert list(lo.summary_table(trades).columns) == ["trades", "won", "net", "avg", "win_rate"]
    report, results = lo.validate(closes)
    assert "## Verdict" in report and "out of sample" in report and "Not financial advice" in report
    assert set(results["trend"]["gates"]) == set(lo.GATES)
    assert len(results["trend"]["walk"]) == 6 and "costs x1.5" in results["dip"]
    assert results["trend"]["costs x1.5"]["costs"] > results["trend"]["full"]["costs"] * 0.9


# -- live demo book --------------------------------------------------------------
def test_live_book_decides_once_per_closed_bar_and_journals(tmp_path):
    async def run():
        db = DBLogger(str(tmp_path / "bot.db"))
        await db.open()
        try:
            book = lo.LongBook(db, symbols=("BTCUSD",), strategies=("trend", "hold"))
            x = up_then_down()
            events = []
            for i in range(150, 600, 3):
                events += await book.update(series(x[:i]).to_frame(), {"BTCUSD"})
            again = await book.update(series(x[:597]).to_frame(), {"BTCUSD"})   # same bar: no double trading
            assert again == []
            assert any("trend bought BTCUSD" in e for e in events) and any("trend sold BTCUSD" in e for e in events)
            closed = await db.fetchall("SELECT strategy, net_pnl FROM long_trades WHERE closed_at IS NOT NULL")
            assert closed and all(s == "trend" for s, _ in closed)
            assert await db.fetchall("SELECT 1 FROM long_trades WHERE strategy='hold' AND closed_at IS NULL")
            assert await book.update(series(x[:620]).to_frame(), set()) == []         # market closed: nothing
            journal = await db.fetchall("SELECT action, reason, state FROM long_decisions WHERE strategy='trend'")
            assert {"buy", "sell", "wait"} <= {a for a, _, _ in journal}
            sleeves = dict(await db.fetchall("SELECT strategy, state FROM long_sleeves"))
            assert set(sleeves) == {"trend", "hold"}

            restarted = lo.LongBook(db, symbols=("BTCUSD",), strategies=("trend", "hold"))
            await restarted.load()                                    # picks up positions, P&L and the last bar
            assert set(restarted.open) == set(book.open)
            assert abs(restarted.sleeves["trend"].realized - book.sleeves["trend"].realized) < 0.1
            assert await restarted.update(series(x[:597]).to_frame(), {"BTCUSD"}) == []
            await book.close_all(series(x).to_frame())
            assert await db.fetchall("SELECT 1 FROM long_trades WHERE strategy='hold' AND closed_at IS NOT NULL")
        finally:
            await db.close()
    asyncio.run(run())
    text = lo.comparison_text(str(tmp_path / "bot.db"))
    assert "buy-only trend" in text and "benchmark" in text and "Risk state" in text and "not a forecast" in text


def test_old_database_without_the_new_tables_still_loads(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE long_trades (id INTEGER PRIMARY KEY, strategy TEXT, symbol TEXT, market TEXT, opened_at REAL,"
              " entry REAL, qty REAL, entry_note TEXT, closed_at REAL, exit REAL, gross_pnl REAL, costs REAL, "
              "net_pnl REAL, exit_reason TEXT)")
    c.execute("INSERT INTO long_trades (strategy, symbol, opened_at, entry, qty, costs) VALUES ('trend','BTCUSD',1,100,2,1)")
    c.commit()
    c.close()

    async def run():
        db = DBLogger(str(path))
        await db.open()                                               # creates the new tables, adds new columns
        book = lo.LongBook(db, symbols=("BTCUSD",))
        await book.load()
        await db.close()
        return book
    book = asyncio.run(run())
    assert book.sleeves["trend"].positions["BTCUSD"].qty == 2
    assert lo.sleeve_rows(str(path)) == []
