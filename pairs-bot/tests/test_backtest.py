import sqlite3

import pandas as pd
import pytest

from backtest import Costs, load_csv, regular_session, run_backtest, sim_closes

FREE = Costs(spread_bps=0, sec_fee_rate=0, taf_per_share=0, borrow_rate_annual=0)


@pytest.fixture
def closes(cfg):
    return sim_closes(cfg(), 3_000)


def test_accounting_adds_up(cfg, closes):
    res = run_backtest(closes, cfg(), Costs(spread_bps=3), all_hours=True)
    s = res.summary()
    assert s["trades"] >= 5
    assert all(t.closed_at is not None for t in res.trades)
    net = sum(t.net_pnl for t in res.trades)
    assert res.equity.iloc[-1] == pytest.approx(res.starting_equity + net)
    assert s["net_pnl"] == pytest.approx(s["gross_pnl"] - s["spread_and_slippage"]
                                         - s["regulatory_fees"] - s["borrow_fees"], abs=0.02)
    assert s["net_pnl_by_cost"]["3 bps"] == pytest.approx(s["net_pnl"], abs=0.02)


def test_costs_only_ever_reduce_profit(cfg, closes):
    c = cfg()
    free = run_backtest(closes, c, FREE, all_hours=True).summary()
    assert free["net_pnl"] == pytest.approx(free["gross_pnl"])
    nets = [run_backtest(closes, c, Costs(spread_bps=bps), all_hours=True).summary()["net_pnl"]
            for bps in (0, 2, 5)]
    assert nets[0] > nets[1] > nets[2]


def test_breakeven_cost_gives_zero_net(cfg, closes):
    c = cfg()
    be = run_backtest(closes, c, Costs(), all_hours=True).summary()["breakeven_cost_bps"]
    at_be = run_backtest(closes, c, Costs(spread_bps=be), all_hours=True).summary()
    assert at_be["net_pnl"] == pytest.approx(0, abs=1.0)


def test_no_lookahead(cfg, closes):
    """Wrecking prices after bar k must not change any trade opened before k."""
    c = cfg()
    k = 2_000
    future_broken = closes.copy()
    future_broken.iloc[k:, 0] *= 1.5
    before = [t for t in run_backtest(closes, c, FREE, all_hours=True).trades if t.opened_at < closes.index[k - 1]]
    after = [t for t in run_backtest(future_broken, c, FREE, all_hours=True).trades
             if t.opened_at < closes.index[k - 1]]
    assert [(t.opened_at, t.qty_a, t.qty_b) for t in before] == [(t.opened_at, t.qty_a, t.qty_b) for t in after]


async def test_matches_the_live_bot_trade_for_trade(make_bot):
    """Same market, same rules: the backtester reproduces the live loop's trades and P&L."""
    bot, market, _ = make_bot()
    await bot.start()
    await bot.run(max_cycles=800)
    await bot.db.close()
    with sqlite3.connect(bot.cfg.db_path) as conn:
        live = conn.execute("select qty_a, qty_b, pnl from pair_trades where closed_at is not null "
                            "order by id").fetchall()
    assert len(live) >= 3

    # The live loop's first decision is on the bar after warm-up; drop bar 0 to line up.
    history = market.closes(len(market._rows)).iloc[1:]
    res = run_backtest(history, bot.cfg, FREE, fill="close", all_hours=True)
    replay = [(t.qty_a, t.qty_b, round(t.gross_pnl, 2)) for t in res.trades[:len(live)]]
    assert replay == [(qa, qb, pnl) for qa, qb, pnl in live]


def test_next_bar_fill_is_not_the_signal_bar(cfg, closes):
    res = run_backtest(closes, cfg(), FREE, fill="next", all_hours=True)
    z_times = set(res.zscore.index)
    for t in res.trades:
        assert t.opened_at in z_times                       # a real bar
        assert t.entry_z == pytest.approx(res.zscore[res.zscore.index < t.opened_at].iloc[-1])


def test_regular_session_mask():
    idx = pd.DatetimeIndex(["2026-10-21 09:29", "2026-10-21 09:30", "2026-10-21 15:59",
                            "2026-10-21 16:00", "2026-10-24 10:00"]).tz_localize("America/New_York")
    assert regular_session(idx.tz_convert("UTC")).tolist() == [False, True, True, False, False]


def test_load_csv(cfg, closes, tmp_path):
    path = tmp_path / "bars.csv"
    closes.to_csv(path)
    back = load_csv(path, cfg())
    pd.testing.assert_frame_equal(back, closes, check_freq=False, check_index_type=False)
    with pytest.raises(ValueError, match="no column"):
        load_csv(path, cfg(symbol_a="XOM", symbol_b="CVX"))
