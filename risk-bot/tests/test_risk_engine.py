import random

from riskbot.config import RiskLimits
from riskbot.models import Position, Settlement
from riskbot.risk_engine import Breach, RiskEngine


def settle(engine: RiskEngine, stake: float, crash: float) -> list[Breach]:
    pos = Position(0, stake, engine.limits.target_cashout, 0.0, "t")
    return engine.record_settlement(Settlement.from_crash(pos, crash))


def test_fractional_stake_and_caps():
    eng = RiskEngine(RiskLimits(starting_bankroll=10_000, base_risk_fraction=0.01,
                                max_stake_fraction=0.02, max_stake_abs=25))
    d = eng.evaluate()
    assert d.approved and d.stake == 25.0  # 1% of 10k = 100, capped at 25 abs


def test_never_increases_stake_after_loss_randomised():
    """Anti-martingale property over many random win/loss sequences."""
    rng = random.Random(1)
    for _ in range(200):
        eng = RiskEngine(RiskLimits(max_consecutive_losses=50, max_daily_drawdown_pct=0.5,
                                    max_stake_fraction=0.05, base_risk_fraction=0.05,
                                    max_stake_abs=1_000))
        prev_stake, prev_loss = None, False
        for _ in range(40):
            d = eng.evaluate()
            if not d.approved:
                break
            if prev_loss:
                assert d.stake <= prev_stake + 1e-9
            crash = rng.choice([1.0, 3.0])
            settle(eng, d.stake, crash)
            prev_stake, prev_loss = d.stake, crash < eng.limits.target_cashout


def test_single_loss_cannot_overshoot_daily_limit():
    eng = RiskEngine(RiskLimits(starting_bankroll=1_000, max_daily_drawdown_pct=0.02,
                                base_risk_fraction=0.01, max_stake_fraction=0.05,
                                max_consecutive_losses=100))
    while True:
        d = eng.evaluate()
        if not d.approved:
            break
        assert d.stake <= eng.drawdown_budget + 1e-9
        breaches = settle(eng, d.stake, 1.0)
        assert eng.drawdown <= 0.02 + 1e-9
        if Breach.DAILY_DRAWDOWN in breaches:
            break
    assert Breach.DAILY_DRAWDOWN in breaches


def test_loss_streak_breach_and_reset():
    eng = RiskEngine(RiskLimits(max_consecutive_losses=2))
    assert Breach.LOSS_STREAK not in settle(eng, eng.evaluate().stake, 1.0)
    assert Breach.LOSS_STREAK in settle(eng, eng.evaluate().stake, 1.0)
    assert eng.evaluate().reason == "loss_streak_reached"
    eng.reset_streak_after_cooldown()
    assert eng.evaluate().approved


def test_win_pays_target_multiple():
    eng = RiskEngine(RiskLimits(target_cashout=2.0))
    settle(eng, 10.0, 5.0)
    assert eng.equity == 1_010.0
