import random

import numpy as np
import pandas as pd

from sim import SimMarket
from strategy import (Action, Position, SpreadStats, coint_pvalue, compute_spread_stats, decide,
                      half_life, hedge_ratio)


def test_hedge_ratio_recovers_true_beta():
    m = SimMarket(beta=0.8, alpha=0.3, sigma_b=0.01, sigma_s=0.001, seed=3)
    m.warm_up(2_000)
    df = m.closes(2_000)
    alpha, beta = hedge_ratio(np.log(df["KO"].to_numpy()), np.log(df["PEP"].to_numpy()))
    assert abs(beta - 0.8) < 0.05


def test_zscore_matches_manual_formula(cfg):
    c = cfg()
    m = SimMarket(seed=5)
    m.warm_up(c.hedge_lookback)
    df = m.closes(c.hedge_lookback)
    s = compute_spread_stats(df, c)
    la, lb = np.log(df["KO"].to_numpy()), np.log(df["PEP"].to_numpy())
    spread = la - s.beta * lb - s.alpha
    recent = spread[-c.z_lookback:]
    assert abs(s.zscore - (spread[-1] - recent.mean()) / recent.std(ddof=1)) < 1e-9


def test_cointegration_test_separates_pairs():
    m = SimMarket(seed=8, sigma_b=0.004, theta=0.2)
    m.warm_up(400)
    df = m.closes(400)
    assert coint_pvalue(np.log(df["KO"].to_numpy()), np.log(df["PEP"].to_numpy())) < 0.05

    rng = random.Random(1)  # two independent random walks: should NOT look cointegrated (usually)
    a = np.cumsum([rng.gauss(0, 0.01) for _ in range(400)])
    b = np.cumsum([rng.gauss(0, 0.01) for _ in range(400)])
    assert coint_pvalue(a, b) > 0.05


def test_half_life_matches_ou_theta():
    m = SimMarket(theta=0.1, seed=4)
    m.warm_up(5_000)
    df = m.closes(5_000)
    la, lb = np.log(df["KO"].to_numpy()), np.log(df["PEP"].to_numpy())
    alpha, beta = hedge_ratio(la, lb)
    hl = half_life(la - beta * lb - alpha)
    assert 4 < hl < 10  # ln2/0.1 ≈ 6.9 bars


def stats(z, p=0.01, beta=1.0):
    return SpreadStats(0, 50, 50, 0, beta, 0, 0, 1, z, p, 10)


def test_decide_rules(cfg):
    c = cfg()
    F, L, S = Position.FLAT, Position.LONG_SPREAD, Position.SHORT_SPREAD
    cases = [
        (F, 1.9, Action.HOLD), (F, 2.1, Action.OPEN_SHORT), (F, -2.1, Action.OPEN_LONG),
        (F, 3.6, Action.BLOCKED), (F, -3.6, Action.BLOCKED),
        (L, -1.5, Action.HOLD), (L, -0.4, Action.CLOSE), (L, -3.6, Action.CLOSE),
        (S, 1.5, Action.HOLD), (S, 0.4, Action.CLOSE), (S, 3.6, Action.CLOSE),
    ]
    for pos, z, want in cases:
        assert decide(pos, stats(z), 0, c)[0] is want, (pos, z)
    assert decide(F, stats(2.5, p=0.4), 0, c)[0] is Action.BLOCKED      # not cointegrated
    assert decide(L, stats(-1.5), c.max_holding_bars, c)[0] is Action.CLOSE  # time stop


def test_rejects_bad_prices(cfg):
    c = cfg()
    idx = pd.date_range("2026-01-01", periods=c.hedge_lookback, freq="min", tz="UTC")
    df = pd.DataFrame({"KO": 50.0, "PEP": 60.0}, index=idx)
    df.iloc[-1, 0] = 0.0
    try:
        compute_spread_stats(df, c)
    except ValueError:
        return
    raise AssertionError("zero price should be rejected")
