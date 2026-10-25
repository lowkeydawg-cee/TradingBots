"""Tests for the five upgrades: model, execution, portfolio, scanner/Monte Carlo, service."""

import sqlite3
from datetime import date

import numpy as np
import pandas as pd
import pytest

from backtest import Costs, block_category, measured_slippage_bps, run_backtest, sim_closes
from blackout import Blackout
from db_logger import DBLogger, slippage_bps
from execution import EntrySkipped, OrderFailed, PairExecutor, plan_pair
from main import PairsBot
from montecarlo import monte_carlo
from notify import Notifier
from reconcile import mismatches
from sim import SimBroker, SimMarket, SimMarketData, SimUniverse
from strategy import Action, Position, SpreadStats, compute_spread_stats, decide, kalman_hedge


def rows(db, sql):
    with sqlite3.connect(db) as c:
        return c.execute(sql).fetchall()


def stats(z=2.5, p=0.01, beta=1.0, vol=1.0, pa=50.0, pb=50.0):
    return SpreadStats(0, pa, pb, 0, beta, 0, 0, 1, z, p, 10, vol)


def make_portfolio(cfg, pairs, broker_kw=None, **overrides):
    c = cfg(pairs=tuple(pairs), **overrides)
    uni = SimUniverse.for_pairs(c.all_pairs)
    uni.warm_up(c.hedge_lookback)
    broker = SimBroker(uni, **(broker_kw or {}))
    bot = PairsBot(c, SimMarketData(uni), broker, DBLogger(c.db_path), clock=lambda: uni.last[0],
                   notifier=Notifier())
    return bot, uni, broker


# --------------------------------------------------------------------------- model
def test_kalman_tracks_the_true_hedge_ratio():
    m = SimMarket(beta=0.95)
    m.warm_up(1500)
    df = m.closes(1500)
    _, beta, e, sq = kalman_hedge(np.log(df["KO"].to_numpy()), np.log(df["PEP"].to_numpy()), 1e-4, 0.0, 60)
    assert abs(beta - 0.95) < 0.1
    assert sq > 0 and np.isfinite(e)


def test_kalman_stats_give_a_usable_zscore(cfg):
    c = cfg(hedge_method="kalman")
    m = SimMarket()
    m.warm_up(c.hedge_lookback)
    s = compute_spread_stats(m.closes(c.hedge_lookback), c)
    assert np.isfinite(s.zscore) and s.spread_std > 0 and s.spread_mean == 0


def test_volatility_spike_blocks_entries_but_not_exits(cfg):
    c = cfg(max_vol_ratio=3.0)
    act, note = decide(Position.FLAT, stats(z=2.5, vol=4.0), 0, c)
    assert act is Action.BLOCKED and "volatility" in note
    assert decide(Position.LONG_SPREAD, stats(z=-0.1, vol=4.0), 0, c)[0] is Action.CLOSE


def test_vol_ratio_jumps_when_the_spread_goes_wild(cfg):
    c = cfg()
    m = SimMarket()
    m.warm_up(c.hedge_lookback)
    calm = compute_spread_stats(m.closes(c.hedge_lookback), c).vol_ratio
    m.sigma_s *= 8
    for _ in range(20):
        m.step()
    wild = compute_spread_stats(m.closes(c.hedge_lookback), c).vol_ratio
    assert calm < 2 < wild


def test_blackout_file_and_rules(cfg, tmp_path):
    f = tmp_path / "b.csv"
    f.write_text("date,symbol,note\n2026-10-21,ko,Q3 earnings\nnot-a-date,PEP,x\n")
    b = Blackout(f)
    assert len(b) == 1
    assert b.reason(date(2026, 10, 21), ("KO", "PEP")) == "KO Q3 earnings"
    assert b.reason(date(2026, 10, 22), ("KO", "PEP")) is None
    c = cfg()
    assert decide(Position.FLAT, stats(z=2.5), 0, c, "KO Q3 earnings")[0] is Action.BLOCKED
    assert decide(Position.FLAT, stats(z=0.1), 0, c, "KO Q3 earnings")[0] is Action.HOLD
    act, note = decide(Position.SHORT_SPREAD, stats(z=1.5), 0, c, "KO Q3 earnings")
    assert act is Action.CLOSE and note.startswith("blackout")
    assert Blackout(tmp_path / "missing.csv").reason(date(2026, 10, 21), ("KO",)) is None


def test_backtest_respects_blackout_days(cfg, tmp_path):
    c = cfg(hedge_lookback=200, z_lookback=40)
    closes = sim_closes(c, 3000)
    base = run_backtest(closes, c, Costs(), all_hours=True)
    days = sorted({ts.tz_convert("America/New_York").date() for ts in closes.index})
    f = tmp_path / "b.csv"
    f.write_text("date,symbol,note\n" + "".join(f"{d},KO,event\n" for d in days))
    blocked = run_backtest(closes, c, Costs(), all_hours=True, blackout=Blackout(f))
    assert base.summary()["trades"] > 0
    assert blocked.summary()["trades"] == 0
    assert blocked.blocked["earnings/news blackout"] > 0
    assert block_category("volatility spike: recent moves 4.0x normal") == "volatility spike"


# ----------------------------------------------------------------------- execution
def limit_setup(cfg, **broker_kw):
    c = cfg(order_type="limit")
    m = SimMarket()
    m.warm_up(5)
    broker = SimBroker(m, half_spread_bps=2.0, **broker_kw)
    plan = plan_pair(Position.LONG_SPREAD, stats(pa=m.price(c.symbol_a), pb=m.price(c.symbol_b), beta=0.9), c)
    return c, m, broker, plan


async def test_limit_entry_fills_first_leg_at_the_mid(cfg):
    c, m, broker, plan = limit_setup(cfg)
    pf = await PairExecutor(broker, c).open_pair(plan, "t")
    short = pf.fill_b                                   # LONG_SPREAD: B is the short leg, sent first
    assert short.side == "sell" and short.avg_price == pytest.approx(m.price("PEP"))   # at the mid
    assert short.expected_price == pytest.approx(m.price("PEP"))
    assert pf.fill_a.avg_price > m.price("KO")          # second leg crossed the spread (paid the ask)
    assert (pf.fill_a.qty, pf.fill_b.qty) == (plan.qty_a, plan.qty_b)


async def test_missed_limit_entry_trades_nothing(cfg):
    c, _, broker, plan = limit_setup(cfg, passive_fill_prob=0.0)
    with pytest.raises(EntrySkipped):
        await PairExecutor(broker, c).open_pair(plan, "t")
    assert broker.orders == []


async def test_partial_first_leg_scales_the_hedge(cfg):
    c, _, broker, plan = limit_setup(cfg, passive_fill_ratio=0.5)
    pf = await PairExecutor(broker, c).open_pair(plan, "t")
    assert pf.fill_b.qty == plan.qty_b // 2
    assert pf.fill_a.qty == plan.qty_a * pf.fill_b.qty // plan.qty_b


async def test_limit_second_leg_failure_unwinds(cfg):
    c, _, broker, plan = limit_setup(cfg, fail_symbols={"KO"})
    with pytest.raises(OrderFailed, match="unwound"):
        await PairExecutor(broker, c).open_pair(plan, "t")
    assert all(q == 0 for q in broker.qty.values())


async def test_wide_quote_skips_entry(cfg):
    c, _, broker, plan = limit_setup(cfg)
    broker.half_spread_bps = 50.0                       # 100 bps wide
    with pytest.raises(EntrySkipped, match="spread"):
        await PairExecutor(broker, c).open_pair(plan, "t")
    assert broker.orders == []


def test_slippage_sign():
    assert slippage_bps("buy", 100.1, 100.0) == pytest.approx(10.0)
    assert slippage_bps("sell", 99.9, 100.0) == pytest.approx(10.0)
    assert slippage_bps("buy", 99.9, 100.0) == pytest.approx(-10.0)    # price improvement
    assert slippage_bps("buy", None, 100.0) is None


async def test_bot_logs_slippage_and_backtester_reads_it(make_bot):
    bot, _, _ = make_bot(order_type="limit", broker_kw=dict(half_spread_bps=2.0))
    await bot.start()
    await bot.run(max_cycles=600)
    await bot.db.close()
    slips = [r[0] for r in rows(bot.cfg.db_path, "select slippage_bps from orders where slippage_bps is not null")]
    assert slips and max(slips) > 0
    assert measured_slippage_bps(bot.cfg.db_path) == pytest.approx(float(np.median(slips)))


async def test_old_database_is_migrated(cfg):
    c = cfg()
    db = DBLogger(c.db_path)
    await db.open()
    await db.close()
    with sqlite3.connect(c.db_path) as conn:            # turn it back into a pre-upgrade database
        for table, col in (("signals", "pair"), ("signals", "vol_ratio"), ("orders", "slippage_bps")):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
    db = DBLogger(c.db_path)
    await db.open()
    await db.close()
    cols = {r[1] for r in rows(c.db_path, "pragma table_info(signals)")}
    assert {"pair", "vol_ratio"} <= cols
    assert "slippage_bps" in {r[1] for r in rows(c.db_path, "pragma table_info(orders)")}


# ----------------------------------------------------------------------- portfolio
async def test_portfolio_trades_every_pair_independently(cfg):
    bot, _, broker = make_portfolio(cfg, [("KO", "PEP"), ("XOM", "CVX")], max_gross_exposure=20_000)
    await bot.start()
    await bot.run(max_cycles=500)
    await bot.db.close()
    db = bot.cfg.db_path
    traded = {r[0] for r in rows(db, "select pair from pair_trades")}
    assert traded == {"KO/PEP", "XOM/CVX"}
    assert rows(db, "select count(distinct pair) from signals")[0][0] == 2
    open_now = rows(db, "select count(*) from pair_trades where closed_at is null")[0][0]
    assert rows(db, "select count(*) from orders")[0][0] == 4 * rows(
        db, "select count(*) from pair_trades where closed_at is not null")[0][0] + 2 * open_now


def test_pairs_may_not_share_a_stock(cfg):
    with pytest.raises(ValueError):
        cfg(pairs=(("KO", "PEP"), ("PEP", "KDP")))


def test_volatility_sizing_caps_the_loss_at_the_stop(cfg):
    c = cfg(risk_per_trade_usd=100, leg_notional=10_000, max_gross_exposure=50_000)
    s = SpreadStats(0, 50, 50, 0, 1.0, 0, 0, 0.01, 2.5, 0.01, 10)
    p = plan_pair(Position.LONG_SPREAD, s, c)
    budget = 100 / ((c.stop_z - c.entry_z) * 0.01)       # $ per leg so a stop-out costs ~$100
    assert p.qty_a * 50 <= budget + 50


async def test_weekly_loss_limit_halts(cfg):
    bot, _, broker = make_portfolio(cfg, [("KO", "PEP")], max_weekly_loss_pct=0.01)
    await bot.start()
    await bot.cycle()

    async def week_started_higher(since_ts):
        return 102_000.0                                  # equity is ~100k: down ~2% this week
    bot.db.period_start_equity = week_started_higher
    await bot.cycle()
    assert bot.halted == "weekly loss limit"
    assert any("HALTED: weekly loss limit" in m for m in bot.notifier.sent)
    await bot.db.close()


async def test_broken_pair_is_retired_and_stops_opening(cfg):
    bot, _, _ = make_portfolio(cfg, [("KO", "PEP")], retire_pvalue=0.0, retire_days=1)
    await bot.start()
    await bot.run(max_cycles=200)
    engine = bot.engines[0]
    assert engine.retired
    assert await bot.db.get_control("retired:KO/PEP")
    assert rows(bot.cfg.db_path, "select count(*) from pair_trades")[0][0] == 0
    assert rows(bot.cfg.db_path, "select count(*) from signals where note like 'pair retired%'")[0][0] > 0
    await bot.db.close()
    # --reset-halt clears it
    bot2, _, _ = make_portfolio(cfg, [("KO", "PEP")])
    await bot2.start(reset_halt=True)
    assert bot2.engines[0].retired is None
    await bot2.db.close()


# --------------------------------------------------------- scanner & Monte Carlo
def test_monte_carlo():
    assert monte_carlo([1, 2, 3]) is None
    mc = monte_carlo([10.0] * 20)
    assert mc["prob_loss"] == 0 and mc["pnl_p5"] == mc["pnl_p95"] == 200
    mixed = monte_carlo([30, -20, 25, -15, 10, -5, 20, -25, 15, -10])
    assert mixed["pnl_p5"] < mixed["pnl_p50"] < mixed["pnl_p95"]
    assert mixed["max_drawdown_p95"] <= mixed["max_drawdown_p50"] <= 0
    assert 0 < mixed["prob_loss"] < 1
    assert monte_carlo([5, -1, 4, 2, -3], seed=1) == monte_carlo([5, -1, 4, 2, -3], seed=1)


def test_scanner_finds_real_pairs_and_rejects_fakes(cfg):
    from scanner import pick, scan, sim_universe

    closes, sectors = sim_universe(4000)
    c = cfg(hedge_lookback=390, z_lookback=60)
    result = {r.pair: r for r in scan(closes, sectors, c, Costs(), all_hours=True, log=lambda *_: None)}
    assert result["AAA/BBB"].passed_filter and result["AAA/BBB"].out_net > 0
    assert not result["GGG/HHH"].passed_filter               # never cointegrated
    assert not result["AAA/CCC"].passed_filter               # two unrelated stocks
    chosen = [r.pair for r in pick(list(result.values()), top=5, min_trades=5)]
    assert "AAA/BBB" in chosen and "GGG/HHH" not in chosen


# ------------------------------------------------------------------------ service
async def test_notifier_without_token_is_a_safe_noop():
    n = Notifier()
    await n.send("hello")
    assert not n.enabled and n.sent == ["[pairs-bot] hello"]


def test_reconcile_mismatches():
    pairs = [("KO/PEP", ("KO", "PEP"), True), ("XOM/CVX", ("XOM", "CVX"), False), ("V/MA", ("V", "MA"), False)]
    problems = mismatches(pairs, {"KO", "XOM"})
    assert len(problems) == 2
    assert "KO/PEP" in problems[0] and "XOM/CVX" in problems[1]
    assert mismatches(pairs, {"KO", "PEP"}) == []


async def test_nightly_reconcile_job(cfg):
    import reconcile

    bot, _, broker = make_portfolio(cfg, [("KO", "PEP")])
    await bot.start()
    for _ in range(400):
        await bot.cycle()
        if bot.trade:
            break
    await bot.db.close()
    assert bot.trade
    n = Notifier()
    assert await reconcile.run(bot.cfg, broker, n) == []
    broker.qty["PEP"] = 0                               # someone closed a leg by hand
    problems = await reconcile.run(bot.cfg, broker, n)
    assert problems and "KO/PEP" in problems[0]
    assert "MISMATCH" in n.sent[-1]


def test_stream_bars_are_resampled_and_merged(cfg):
    from streaming import StreamingMarketData

    c = cfg(mode="alpaca", api_key="k", secret_key="s", timeframe_minutes=5)
    md = StreamingMarketData(c)
    t0 = pd.Timestamp("2026-10-23 14:00", tz="UTC")
    hist = pd.DataFrame({"KO": [60.0, 60.1], "PEP": [170.0, 170.2]},
                        index=[t0 - pd.Timedelta("10min"), t0 - pd.Timedelta("5min")])
    for k in range(8):                                  # 14:00..14:07, one bar per minute
        md.add_bar("KO", t0 + pd.Timedelta(minutes=k), 61.0 + k)
        md.add_bar("PEP", t0 + pd.Timedelta(minutes=k), 171.0 + k)
    out = md.merge(hist, ("KO", "PEP"), 10, now=t0 + pd.Timedelta("8min"))
    # The 14:00 bar (14:00-14:05) is finished; the 14:05 bar is still forming and left out.
    assert list(out.index) == [t0 - pd.Timedelta("10min"), t0 - pd.Timedelta("5min"), t0]
    assert out.loc[t0, "KO"] == 65.0                    # last close in the 14:00 bucket (14:04)
