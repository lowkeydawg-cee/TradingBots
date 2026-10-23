"""Main event loop: consume the feed, govern risk, execute with discipline.

Task layout (all on one asyncio loop):

    consumer  - reads the feed sequentially, validates each message, checks
                latency, sizes and places bets, settles rounds.
    watchdog  - detects a silent feed; trips the breaker if exposure is open,
                otherwise forces a reconnect.
    cooldown  - one short-lived task per COOLING_DOWN period.
    db-writer - owned by DBLogger; drains the audit queue.

``run()`` returns as soon as the feed ends, a graceful stop completes, or the
circuit breaker trips (``fsm.halted``), whichever happens first. On a trip it
cancels the consumer immediately rather than waiting for the current message.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .config import AppConfig
from .db_logger import DBLogger
from .executor import ExecutionRejected, Executor
from .feed import FeedClosed, FeedConnection, FeedDisconnected
from .guard import GuardedExecutor, GuardViolation
from .journal import Journal
from .latch import check_latch, engage_latch
from .ops_server import OpsServer
from .models import BotState, EventType, FeedEvent, FeedProtocolError, Position, Settlement
from .risk_engine import Breach, RiskEngine
from .state_machine import StateMachine

log = logging.getLogger(__name__)

FeedFactory = Callable[[], Awaitable[FeedConnection]]


@dataclass
class BotReport:
    session_id: str
    final_state: str
    trip_reason: str | None
    start_equity: float
    end_equity: float
    pnl: float
    bets: int
    wins: int
    losses: int
    unresolved: int
    rounds_seen: int
    transitions: list[str] = field(default_factory=list)


class RiskBot:
    def __init__(self, cfg: AppConfig, feed_factory: FeedFactory, executor: Executor,
                 *, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self._clock = clock
        self._feed_factory = feed_factory
        # Every order passes the independent outer guard, whatever executor is given.
        self.guard = executor if isinstance(executor, GuardedExecutor) else GuardedExecutor(executor, cfg.guard)
        self._executor: Executor = self.guard
        self.fsm = StateMachine(clock)
        self.risk = RiskEngine(cfg.risk, clock)
        self.db = DBLogger(cfg.storage, on_failure=self._on_db_failure, clock=clock)
        self.journal: Journal | None = None
        self.ops: OpsServer | None = None
        self.fsm.add_listener(self.db.log_transition)
        self.fsm.add_listener(self._journal_transition)
        self.counters = {"feed_messages": 0, "orders": 0, "orders_rejected": 0,
                         "latency_soft_breaches": 0, "reconnects": 0}
        self.last_latency_ms = 0.0
        self.max_latency_ms = 0.0

        self.position: Position | None = None
        # Order sent but not yet acknowledged. Exposure is unknown -> worst case.
        self._pending: Position | None = None
        self._bet_rounds: set[int] = set()
        self._last_round_id: int | None = None
        self._soft_breaches = 0
        self._reconnect_attempts = 0
        self._last_msg_mono = time.monotonic()
        self._conn: FeedConnection | None = None
        self._stop = asyncio.Event()
        self._cooldown_task: asyncio.Task[None] | None = None
        self.rounds_seen = 0
        self.unresolved = 0

    # ------------------------------------------------------------------ API
    def request_stop(self) -> None:
        """Graceful stop: finish the open round (if any), then go IDLE."""
        self._stop.set()

    async def run(self) -> BotReport:
        # Refuse to start while a previous trip has not been cleared by an operator.
        check_latch(self.cfg.storage.latch_path)
        await self.db.open()
        now = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        carry = await self.db.today_carryover(midnight)
        if carry is not None:
            self.risk.restore(*carry)
        start_equity = self.risk.equity
        self.db.begin_session(start_equity, asdict(self.cfg))
        self.journal = Journal(self.cfg.ops.journal_path, self._clock)
        self.journal.append("session_start", session_id=self.db.session_id,
                            risk_limits=asdict(self.cfg.risk),
                            day_start_equity=self.risk.day_start_equity, equity=self.risk.equity)
        if self.cfg.ops.ops_port is not None:
            self.ops = OpsServer(self.snapshot, self.prometheus_metrics, self._trip,
                                 self.cfg.ops.ops_host, self.cfg.ops.ops_port)
            await self.ops.start()
        await self.fsm.transition(BotState.MONITORING, "session start", expected=BotState.IDLE)
        if self.risk.drawdown_budget < self.cfg.risk.min_stake:
            self._trip("daily drawdown limit already reached today", equity=self.risk.equity)

        consumer = asyncio.create_task(self._consume_loop(), name="consumer")
        watchdog = asyncio.create_task(self._watchdog(), name="watchdog")
        halted = asyncio.create_task(self.fsm.halted.wait(), name="halted")
        try:
            await asyncio.wait({consumer, halted}, return_when=asyncio.FIRST_COMPLETED)
            if consumer.done() and not consumer.cancelled() and consumer.exception():
                self._trip(f"unhandled error in consumer: {consumer.exception()!r}")
        finally:
            tasks = [t for t in (consumer, watchdog, halted, self._cooldown_task) if t]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            # Exposure that never settled is booked as a full loss.
            for exposure in (self._pending, self.position):
                if exposure is not None:
                    self._book_unresolved(exposure, "shutdown with unresolved exposure")
            self._pending = self.position = None
            if self.unresolved and not self.fsm.is_halted:
                self._trip("session ended with unresolved exposure")
            if not self.fsm.is_halted and self.fsm.state is not BotState.IDLE:
                await self.fsm.transition(BotState.IDLE, "session end")
            if self._conn is not None:
                with contextlib.suppress(Exception):
                    await self._conn.close()
            await self.db.close(self.risk.equity, str(self.fsm.state), self.fsm.trip_reason)
            if self.ops is not None:
                await self.ops.close()
            self._jot("session_end", state=str(self.fsm.state), equity=self.risk.equity,
                      trip_reason=self.fsm.trip_reason)
            if self.journal is not None:
                self.journal.close()

        return BotReport(
            session_id=self.db.session_id,
            final_state=str(self.fsm.state),
            trip_reason=self.fsm.trip_reason,
            start_equity=start_equity,
            end_equity=self.risk.equity,
            pnl=round(self.risk.equity - start_equity, 2),
            bets=self.risk.bets, wins=self.risk.wins, losses=self.risk.losses,
            unresolved=self.unresolved, rounds_seen=self.rounds_seen,
            transitions=[f"{t.from_state}->{t.to_state}: {t.reason}" for t in self.fsm.history],
        )

    # ------------------------------------------------------- observability
    def snapshot(self) -> dict[str, object]:
        exposure = self.position or self._pending
        return {
            "session_id": self.db.session_id, "state": str(self.fsm.state),
            "trip_reason": self.fsm.trip_reason, "equity": self.risk.equity,
            "day_start_equity": self.risk.day_start_equity,
            "drawdown_pct": round(self.risk.drawdown * 100, 4),
            "drawdown_budget": round(self.risk.drawdown_budget, 2),
            "consecutive_losses": self.risk.consecutive_losses,
            "open_exposure": None if exposure is None else {"round_id": exposure.round_id,
                                                            "stake": exposure.stake},
            "bets": self.risk.bets, "rounds_seen": self.rounds_seen,
            "last_latency_ms": round(self.last_latency_ms, 1),
            "journal_head": self.journal.head if self.journal else None,
            **self.counters,
        }

    def prometheus_metrics(self) -> dict[str, tuple[str, str, float]]:
        m: dict[str, tuple[str, str, float]] = {
            "riskbot_equity": ("gauge", "Current paper equity", self.risk.equity),
            "riskbot_drawdown_ratio": ("gauge", "Drawdown vs day open", self.risk.drawdown),
            "riskbot_consecutive_losses": ("gauge", "Current loss streak", self.risk.consecutive_losses),
            "riskbot_halted": ("gauge", "1 if the circuit breaker has tripped", float(self.fsm.is_halted)),
            "riskbot_feed_latency_ms": ("gauge", "Latency of the last feed message", self.last_latency_ms),
            "riskbot_feed_latency_max_ms": ("gauge", "Max latency seen this session", self.max_latency_ms),
            "riskbot_rounds_seen_total": ("counter", "Rounds observed", self.rounds_seen),
            "riskbot_bets_settled_total": ("counter", "Bets settled", self.risk.bets),
            "riskbot_unresolved_total": ("counter", "Bets booked as worst case", self.unresolved),
        }
        for name, val in self.counters.items():
            m[f"riskbot_{name}_total"] = ("counter", name.replace("_", " "), val)
        for st in BotState:
            m[f'riskbot_state{{state="{st}"}}'] = ("gauge", "Current FSM state", float(self.fsm.state is st))
        return m

    # ------------------------------------------------------- safety helpers
    def _jot(self, kind: str, **data: object) -> None:
        if self.journal is not None:
            self.journal.append(kind, **data)

    def _journal_transition(self, t) -> None:
        self._jot("transition", seq=t.seq, from_state=str(t.from_state), to_state=str(t.to_state),
                  reason=t.reason)

    def _trip(self, reason: str, **detail: object) -> None:
        if self.fsm.trip(reason) is not None:
            engage_latch(self.cfg.storage.latch_path, reason, self.db.session_id)
            self.db.log_risk_event("circuit_breaker", reason=reason, **detail)
            if self.journal is not None:
                self.journal.flush(fsync=True)

    def _on_db_failure(self, reason: str) -> None:
        # No audit trail means no trading.
        self._trip(f"audit log unavailable: {reason}")

    def _book_unresolved(self, pos: Position, why: str) -> list[Breach]:
        s = Settlement.worst_case(pos)
        breaches = self.risk.record_settlement(s)
        self.unresolved += 1
        self._record_settlement(s, pos.order_id)
        self.db.log_risk_event("unresolved_exposure", why=why, round_id=pos.round_id, stake=pos.stake)
        return breaches

    # --------------------------------------------------------- feed reading
    async def _consume_loop(self) -> None:
        fcfg = self.cfg.feed
        while not self.fsm.is_halted:
            try:
                self._conn = await asyncio.wait_for(self._feed_factory(), self.cfg.latency.heartbeat_timeout_s)
                self._last_msg_mono = time.monotonic()
                await self._read_connection(self._conn)
                return  # graceful stop
            except FeedClosed:
                log.info("feed closed by server")
                return
            except (FeedDisconnected, OSError, asyncio.TimeoutError) as exc:
                self.db.log_risk_event("feed_disconnected", error=repr(exc))
                if self._pending or self.position:
                    exposure = self._pending or self.position
                    assert exposure is not None
                    self._book_unresolved(exposure, "disconnect mid-round")
                    self._pending = self.position = None
                    self._trip("feed lost with open exposure", round_id=exposure.round_id)
                    return
                self._reconnect_attempts += 1
                self.counters["reconnects"] += 1
                if self._reconnect_attempts > fcfg.reconnect_max_attempts:
                    self._trip("reconnect budget exhausted", attempts=self._reconnect_attempts)
                    return
                delay = min(fcfg.reconnect_backoff_max_s,
                            fcfg.reconnect_backoff_base_s * 2 ** (self._reconnect_attempts - 1))
                log.warning("feed dropped (%s); reconnect %d in %.2fs", exc, self._reconnect_attempts, delay)
                await asyncio.sleep(delay)
            finally:
                if self._conn is not None:
                    with contextlib.suppress(Exception):
                        await self._conn.close()
                    self._conn = None

    async def _read_connection(self, conn: FeedConnection) -> None:
        while not self.fsm.is_halted:
            if self._stop.is_set() and self.position is None and self._pending is None:
                return
            raw = await conn.recv()
            self._last_msg_mono = time.monotonic()
            self.counters["feed_messages"] += 1
            try:
                ev = FeedEvent.parse(raw, self._clock())
            except FeedProtocolError as exc:
                self._trip("feed protocol violation", error=str(exc))
                return
            await self._handle(ev)

    # ------------------------------------------------------- event handling
    async def _handle(self, ev: FeedEvent) -> None:
        fresh = self._check_latency(ev)
        if self.fsm.is_halted:
            return
        if ev.type is EventType.BETTING_OPEN:
            if self._last_round_id is not None and ev.round_id < self._last_round_id:
                self._trip("round id went backwards", got=ev.round_id, last=self._last_round_id)
                return
            self._last_round_id = ev.round_id
            if fresh:
                await self._maybe_bet(ev)
            else:
                self.db.log_risk_event("bet_skipped", round_id=ev.round_id, reason="stale_data",
                                       latency_ms=round(ev.latency_ms, 1))
        elif ev.type is EventType.CRASH:
            self.rounds_seen += 1
            self._reconnect_attempts = 0  # a complete round proves the link is healthy
            self.db.log_round(ev)
            self._jot("round", round_id=ev.round_id, crash_point=ev.crash_point,
                      latency_ms=round(ev.latency_ms, 3))
            await self._settle(ev)
            if self.journal is not None:
                self.journal.flush()

    def _check_latency(self, ev: FeedEvent) -> bool:
        """Return True if the message is fresh enough to trade on. May trip."""
        lim = self.cfg.latency
        lat = ev.latency_ms
        self.last_latency_ms = lat
        self.max_latency_ms = max(self.max_latency_ms, lat)
        if lat < -lim.max_clock_skew_ms:
            self._trip("clock skew beyond tolerance", latency_ms=round(lat, 1), round_id=ev.round_id)
            return False
        if lat > lim.hard_latency_ms:
            self._trip("extreme feed latency", latency_ms=round(lat, 1), round_id=ev.round_id)
            return False
        if lat > lim.soft_latency_ms:
            self._soft_breaches += 1
            self.counters["latency_soft_breaches"] += 1
            self.db.log_risk_event("latency_soft_breach", latency_ms=round(lat, 1),
                                   count=self._soft_breaches, round_id=ev.round_id)
            if self._soft_breaches >= lim.max_consecutive_soft_breaches:
                self._trip("sustained feed latency", count=self._soft_breaches)
            return False
        self._soft_breaches = 0
        return True

    # ---------------------------------------------------- execution gate
    async def _maybe_bet(self, ev: FeedEvent) -> None:
        rid = ev.round_id
        async with self.fsm.lock:
            # Every check below runs with no await in between, so it is atomic.
            if self.fsm.state is not BotState.MONITORING or self.fsm.is_halted or self._stop.is_set():
                return
            if rid in self._bet_rounds:
                self.db.log_risk_event("duplicate_round_ignored", round_id=rid)
                return
            assert ev.closes_at is not None
            margin_ms = (ev.closes_at - self._clock()) * 1000
            if margin_ms < self.cfg.latency.min_bet_margin_ms:
                self.db.log_risk_event("bet_skipped", round_id=rid, reason="window_too_short",
                                       margin_ms=round(margin_ms, 1))
                return
            decision = self.risk.evaluate()
            self._jot("decision", round_id=rid, approved=decision.approved, stake=decision.stake,
                      reason=decision.reason, factors=decision.factors)
            if not decision.approved:
                self.db.log_risk_event("bet_denied", round_id=rid, reason=decision.reason, **decision.factors)
                if decision.reason == "session_bet_limit":
                    self.request_stop()
                return

            # Reserve the round and the single ACTIVE slot before the first await.
            self._bet_rounds.add(rid)
            if not self.fsm.transition_locked(BotState.ACTIVE, f"bet round {rid}", expected=BotState.MONITORING):
                return
            target = self.cfg.risk.target_cashout
            self._pending = Position(rid, decision.stake, target, self._clock(), "unacknowledged")
            try:
                receipt = await asyncio.wait_for(
                    self._executor.place_bet(rid, decision.stake, target, ev.closes_at),
                    self.cfg.latency.execution_timeout_s,
                )
            except GuardViolation as exc:
                # Reaching the outer guard with a bad order means an inner check failed.
                self._pending = None
                self._trip("pre-trade guard violation", error=str(exc), round_id=rid)
                return
            except ExecutionRejected as exc:
                self._pending = None
                self.guard.on_rejected(rid)
                self.counters["orders_rejected"] += 1
                self.db.log_risk_event("order_rejected", round_id=rid, error=str(exc))
                self.fsm.transition_locked(BotState.MONITORING, "order rejected", expected=BotState.ACTIVE)
                return
            except asyncio.TimeoutError:
                # The order may or may not exist at the venue: assume it filled and lost.
                pending, self._pending = self._pending, None
                self._book_unresolved(pending, "execution ack timeout")
                self._trip("execution acknowledgement timeout", round_id=rid)
                return
            self._pending = None
            self.counters["orders"] += 1
            self.position = Position(rid, receipt.stake, receipt.target_cashout, receipt.accepted_at,
                                     receipt.order_id)
            self.db.log_risk_event("bet_placed", round_id=rid, stake=receipt.stake,
                                   target=receipt.target_cashout, order_id=receipt.order_id,
                                   **decision.factors)

    async def _settle(self, ev: FeedEvent) -> None:
        async with self.fsm.lock:
            pos = self.position
            if pos is None or ev.round_id < pos.round_id:
                return
            self.position = None
            if ev.round_id > pos.round_id:
                # We never saw our own round's result.
                self._book_unresolved(pos, "missed settlement event")
                self._trip("settlement event missing for open position", round_id=pos.round_id)
                return
            assert ev.crash_point is not None
            s = Settlement.from_crash(pos, ev.crash_point)
            breaches = self.risk.record_settlement(s)
            self._record_settlement(s, pos.order_id)
            self._apply_breaches(breaches)

    def _record_settlement(self, s: Settlement, order_id: str | None) -> None:
        """Fan a settlement out to the audit log, the journal and the guard ledger."""
        self.db.log_bet(s, order_id, self.risk.equity)
        self._jot("settlement", **asdict(s))
        self.guard.on_settlement(s.round_id, s.pnl)

    def _apply_breaches(self, breaches: list[Breach]) -> None:
        """Map risk breaches to FSM transitions. Caller holds the FSM lock."""
        if Breach.BANKRUPT in breaches or Breach.DAILY_DRAWDOWN in breaches:
            self._trip("daily drawdown limit reached", drawdown=round(self.risk.drawdown, 6),
                       equity=self.risk.equity)
        elif Breach.SESSION_BET_LIMIT in breaches:
            self.fsm.transition_locked(BotState.IDLE, "session bet limit reached", expected=BotState.ACTIVE)
            self.request_stop()
        elif Breach.LOSS_STREAK in breaches:
            if self.fsm.transition_locked(
                BotState.COOLING_DOWN, f"{self.risk.consecutive_losses} consecutive losses",
                expected=BotState.ACTIVE,
            ):
                self._cooldown_task = asyncio.create_task(self._cooldown(), name="cooldown")
        else:
            self.fsm.transition_locked(BotState.MONITORING, "round settled", expected=BotState.ACTIVE)

    async def _cooldown(self) -> None:
        secs = self.cfg.risk.cooldown_seconds
        self.db.log_risk_event("cooldown_started", seconds=secs)
        await asyncio.sleep(secs)
        async with self.fsm.lock:
            if self.fsm.state is BotState.COOLING_DOWN and not self.fsm.is_halted:
                self.risk.reset_streak_after_cooldown()
                self._jot("cooldown_complete")
                self.fsm.transition_locked(BotState.MONITORING, "cool-down elapsed",
                                           expected=BotState.COOLING_DOWN)

    # ------------------------------------------------------------ watchdog
    async def _watchdog(self) -> None:
        timeout = self.cfg.latency.heartbeat_timeout_s
        interval = min(0.25, timeout / 4)
        while not self.fsm.is_halted:
            await asyncio.sleep(interval)
            if self.guard.kill_switch_engaged():
                self._trip("kill switch file present", path=self.cfg.guard.kill_switch_path)
                return
            silent = time.monotonic() - self._last_msg_mono
            if silent <= timeout:
                continue
            if self.position is not None or self._pending is not None:
                self._trip("feed stale with open exposure", silent_s=round(silent, 2))
                return
            self.db.log_risk_event("feed_stale_reconnect", silent_s=round(silent, 2))
            self._last_msg_mono = time.monotonic()
            if self._conn is not None:
                await self._conn.close()  # consumer sees FeedDisconnected and reconnects
