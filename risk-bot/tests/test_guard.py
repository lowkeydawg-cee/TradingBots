import pytest

from riskbot.config import GuardLimits
from riskbot.executor import PaperExecutor
from riskbot.guard import GuardedExecutor, GuardViolation

FAR = 1e12  # betting window never closes


def guarded(tmp_path, clock=None, **kw):
    limits = GuardLimits(kill_switch_path=str(tmp_path / "KILL"), **kw)
    return GuardedExecutor(PaperExecutor(), limits, clock=clock or (lambda: 0.0))


async def test_refuses_oversized_and_invalid_stakes(tmp_path):
    g = guarded(tmp_path, max_order_abs=10)
    for bad in (10.01, 0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(GuardViolation):
            await g.place_bet(1, bad, 1.5, FAR)
    await g.place_bet(1, 10.0, 1.5, FAR)


async def test_refuses_second_order_same_round(tmp_path):
    g = guarded(tmp_path)
    await g.place_bet(7, 1.0, 1.5, FAR)
    with pytest.raises(GuardViolation, match="second order"):
        await g.place_bet(7, 1.0, 1.5, FAR)


async def test_rate_limit_rolls_over(tmp_path):
    now = [0.0]
    g = guarded(tmp_path, clock=lambda: now[0], max_orders_per_minute=3)
    for rid in range(3):
        await g.place_bet(rid, 1.0, 1.5, FAR)
    with pytest.raises(GuardViolation, match="rate"):
        await g.place_bet(3, 1.0, 1.5, FAR)
    now[0] = 61.0
    await g.place_bet(4, 1.0, 1.5, FAR)


async def test_session_loss_ledger_counts_open_stake(tmp_path):
    g = guarded(tmp_path, max_session_loss_abs=5.0)
    await g.place_bet(1, 3.0, 1.5, FAR)
    with pytest.raises(GuardViolation, match="worst-case session loss"):
        await g.place_bet(2, 3.0, 1.5, FAR)  # 3 open + 3 new > 5
    g.on_settlement(1, +1.5)                 # round 1 won: nothing lost
    await g.place_bet(2, 3.0, 1.5, FAR)


async def test_kill_switch_file(tmp_path):
    g = guarded(tmp_path)
    (tmp_path / "KILL").write_text("stop")
    with pytest.raises(GuardViolation, match="kill switch"):
        await g.place_bet(1, 1.0, 1.5, FAR)
