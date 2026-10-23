import asyncio

import pytest

from riskbot.models import BotState
from riskbot.state_machine import IllegalTransition, StateMachine


async def test_happy_path_and_history():
    fsm = StateMachine()
    seen = []
    fsm.add_listener(seen.append)
    for to in (BotState.MONITORING, BotState.ACTIVE, BotState.COOLING_DOWN, BotState.MONITORING):
        assert await fsm.transition(to, "test")
    assert fsm.state is BotState.MONITORING
    assert [t.seq for t in seen] == [1, 2, 3, 4]


async def test_illegal_edge_raises():
    fsm = StateMachine()
    with pytest.raises(IllegalTransition):
        await fsm.transition(BotState.ACTIVE, "skip monitoring")


async def test_circuit_breaker_is_absorbing():
    fsm = StateMachine()
    await fsm.transition(BotState.MONITORING, "start")
    assert fsm.trip("boom") is not None
    assert fsm.trip("again") is None  # idempotent, first reason kept
    assert fsm.trip_reason == "boom"
    for to in BotState:
        if to is not BotState.CIRCUIT_BREAKER:
            assert not await fsm.transition(to, "try to escape")
    assert fsm.state is BotState.CIRCUIT_BREAKER and fsm.is_halted


async def test_compare_and_set_under_concurrency():
    """100 coroutines race to claim ACTIVE; exactly one may win."""
    fsm = StateMachine()
    await fsm.transition(BotState.MONITORING, "start")

    async def claim():
        await asyncio.sleep(0)
        return await fsm.transition(BotState.ACTIVE, "race", expected=BotState.MONITORING)

    results = await asyncio.gather(*(claim() for _ in range(100)))
    assert results.count(True) == 1


async def test_trip_does_not_wait_for_lock():
    """The emergency stop must work while another coroutine holds the lock."""
    fsm = StateMachine()
    await fsm.transition(BotState.MONITORING, "start")
    async with fsm.lock:
        fsm.trip("while locked")
        assert fsm.state is BotState.CIRCUIT_BREAKER
        assert not fsm.transition_locked(BotState.ACTIVE, "late", expected=BotState.MONITORING)
