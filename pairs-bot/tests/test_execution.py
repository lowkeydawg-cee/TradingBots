import pytest

from execution import OrderFailed, PairExecutor, pair_pnl, plan_pair
from sim import SimBroker, SimMarket
from strategy import Position, SpreadStats


def stats(pa=50.0, pb=100.0, beta=0.9):
    return SpreadStats(0, pa, pb, 0, beta, 0, 0, 1, 2.5, 0.01, 10)


def test_beta_weighted_whole_share_sizing(cfg):
    c = cfg(leg_notional=2_000)
    p = plan_pair(Position.LONG_SPREAD, stats(), c)
    assert (p.qty_a, p.qty_b) == (40, 18)      # 2000/50 = 40 A; 0.9*40*50/100 = 18 B
    assert (p.side_a, p.side_b) == ("buy", "sell")
    assert plan_pair(Position.SHORT_SPREAD, stats(), c).side_a == "sell"


def test_gross_cap_and_tiny_legs(cfg):
    with pytest.raises(ValueError, match="gross"):
        plan_pair(Position.LONG_SPREAD, stats(beta=5.0), cfg(leg_notional=2_000, max_gross_exposure=5_000))
    with pytest.raises(ValueError, match="whole shares"):
        plan_pair(Position.LONG_SPREAD, stats(pa=5_000), cfg())


def test_pnl_signs():
    # long spread: A up, B flat -> profit; short spread mirrors it
    assert pair_pnl(Position.LONG_SPREAD, 10, 5, 50, 100, 51, 100) == 10
    assert pair_pnl(Position.SHORT_SPREAD, 10, 5, 50, 100, 51, 100) == -10


async def test_second_leg_failure_unwinds_first(cfg):
    c = cfg()
    m = SimMarket()
    m.warm_up(5)
    broker = SimBroker(m, fail_symbols={c.symbol_a})   # A is the buy leg in LONG_SPREAD -> sent second
    ex = PairExecutor(broker, c)
    plan = plan_pair(Position.LONG_SPREAD, stats(m.price(c.symbol_a), m.price(c.symbol_b)), c)
    with pytest.raises(OrderFailed, match="unwound"):
        await ex.open_pair(plan, "t1")
    assert all(q == 0 for q in broker.qty.values())     # no naked leg left
    assert [o.side for o in broker.orders] == ["sell", "buy"]  # short B, then unwind (buy back) B


async def test_unshortable_leg_sends_nothing(cfg):
    c = cfg()
    m = SimMarket()
    m.warm_up(5)
    broker = SimBroker(m, shortable=set())
    plan = plan_pair(Position.SHORT_SPREAD, stats(m.price(c.symbol_a), m.price(c.symbol_b)), c)
    with pytest.raises(OrderFailed, match="shortable"):
        await PairExecutor(broker, c).open_pair(plan, "t2")
    assert broker.orders == []
