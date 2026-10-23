"""Hardcoded risk limits and runtime configuration.

Every limit lives in a frozen dataclass so it cannot be mutated at runtime.
Tests and scenarios derive variants with ``dataclasses.replace``; the running
bot never changes its own limits.

Design directive (enforced in code, not just documented):
    * No outcome prediction: the stake and cash-out target never depend on
      past crash points. The risk engine only sees the bot's own P&L.
    * No martingale: stake may never increase after a loss
      (``ALLOW_STAKE_INCREASE_AFTER_LOSS`` is a ``Final`` False and is asserted
      in ``risk_engine``).
    * No ML on RNG data: there is no model, and round outcomes are only logged.
"""

from __future__ import annotations

import math
from urllib.parse import urlparse
from dataclasses import dataclass, field
from typing import Final

LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})

# Anti-martingale invariant. Checked by RiskEngine on every sizing decision.
ALLOW_STAKE_INCREASE_AFTER_LOSS: Final[bool] = False


class ConfigError(ValueError):
    """Raised when a configuration value is outside its safe range."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Bankroll and position-sizing limits."""

    starting_bankroll: float = 1_000.00
    # Halt for the day once equity falls this far below the day's opening equity.
    max_daily_drawdown_pct: float = 0.05
    # Enter COOLING_DOWN after this many losses in a row.
    max_consecutive_losses: int = 3
    cooldown_seconds: float = 60.0
    # Fractional risk: base stake as a fraction of current equity.
    base_risk_fraction: float = 0.005
    # Hard caps applied after all scaling.
    max_stake_fraction: float = 0.01
    max_stake_abs: float = 25.00
    min_stake: float = 0.10
    # Fixed, operator-chosen auto cash-out multiplier. Never derived from data.
    target_cashout: float = 1.50
    # Volatility scaling on the bot's OWN per-round returns (not on crash points).
    vol_lookback_rounds: int = 20
    vol_min_samples: int = 5
    vol_target: float = 0.004
    # Floor for each de-risking multiplier (volatility, drawdown, loss streak).
    # Without a floor, stakes shrink geometrically and the drawdown limit is
    # approached forever without being hit; the remaining-budget cap already
    # guarantees a single loss cannot overshoot the limit.
    min_scale: float = 0.25
    # Session-level guard: graceful stop after this many bets.
    max_bets_per_session: int = 500

    def __post_init__(self) -> None:
        _require(self.starting_bankroll > 0, "starting_bankroll must be > 0")
        _require(0 < self.max_daily_drawdown_pct < 1, "max_daily_drawdown_pct must be in (0, 1)")
        _require(self.max_consecutive_losses >= 1, "max_consecutive_losses must be >= 1")
        _require(self.cooldown_seconds >= 0, "cooldown_seconds must be >= 0")
        _require(0 < self.base_risk_fraction <= self.max_stake_fraction <= 0.05,
                 "need 0 < base_risk_fraction <= max_stake_fraction <= 0.05")
        _require(0 < self.min_stake <= self.max_stake_abs, "need 0 < min_stake <= max_stake_abs")
        _require(math.isfinite(self.target_cashout) and self.target_cashout > 1.0,
                 "target_cashout must be a finite multiplier > 1.0")
        _require(self.vol_lookback_rounds >= self.vol_min_samples >= 2, "bad volatility window")
        _require(self.vol_target > 0, "vol_target must be > 0")
        _require(0 < self.min_scale <= 1, "min_scale must be in (0, 1]")
        _require(self.max_bets_per_session >= 1, "max_bets_per_session must be >= 1")


@dataclass(frozen=True, slots=True)
class LatencyLimits:
    """Feed-health thresholds. Breaching a hard limit trips the circuit breaker."""

    # Per-message latency (receive time minus server timestamp).
    soft_latency_ms: float = 400.0
    hard_latency_ms: float = 1_500.0
    max_consecutive_soft_breaches: int = 3
    # Negative latency beyond this means the clocks disagree; data cannot be trusted.
    max_clock_skew_ms: float = 250.0
    # No message at all for this long counts as a stale feed.
    heartbeat_timeout_s: float = 5.0
    # Do not place a bet if the betting window closes sooner than this.
    min_bet_margin_ms: float = 300.0
    # Maximum time to wait for an execution acknowledgement.
    execution_timeout_s: float = 2.0

    def __post_init__(self) -> None:
        _require(0 < self.soft_latency_ms < self.hard_latency_ms, "need 0 < soft < hard latency")
        _require(self.max_consecutive_soft_breaches >= 1, "max_consecutive_soft_breaches must be >= 1")
        _require(self.max_clock_skew_ms > 0, "max_clock_skew_ms must be > 0")
        _require(self.heartbeat_timeout_s > 0, "heartbeat_timeout_s must be > 0")
        _require(self.min_bet_margin_ms >= 0, "min_bet_margin_ms must be >= 0")
        _require(self.execution_timeout_s > 0, "execution_timeout_s must be > 0")


@dataclass(frozen=True, slots=True)
class FeedConfig:
    """WebSocket endpoint and reconnect policy.

    Only the local mock endpoint is configured. This project ships no adapter
    for any real operator's API.
    """

    ws_endpoint: str = "ws://127.0.0.1:8765/mock-crash"
    reconnect_max_attempts: int = 3
    reconnect_backoff_base_s: float = 0.5
    reconnect_backoff_max_s: float = 8.0

    def __post_init__(self) -> None:
        _require(self.ws_endpoint.startswith(("ws://", "wss://")), "ws_endpoint must be ws:// or wss://")
        _require(urlparse(self.ws_endpoint).hostname in LOOPBACK_HOSTS,
                 "ws_endpoint must be a loopback address in this build (local mock only)")
        _require(self.reconnect_max_attempts >= 0, "reconnect_max_attempts must be >= 0")
        _require(0 < self.reconnect_backoff_base_s <= self.reconnect_backoff_max_s, "bad backoff")


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """SQLite audit-log settings."""

    db_path: str = "data/riskbot.sqlite3"
    fallback_path: str = "data/riskbot.fallback.jsonl"
    queue_max: int = 10_000
    batch_max: int = 200
    busy_timeout_ms: int = 2_000
    write_retries: int = 4
    retry_backoff_base_s: float = 0.05
    # Consecutive failed batches before the bot is forced into CIRCUIT_BREAKER.
    max_consecutive_write_failures: int = 2
    close_timeout_s: float = 5.0
    # Written on every circuit-breaker trip; the bot will not start while it exists.
    latch_path: str = "data/CIRCUIT_BREAKER.latch"

    def __post_init__(self) -> None:
        _require(self.queue_max >= 10 and self.batch_max >= 1, "queue/batch sizes too small")
        _require(self.busy_timeout_ms >= 0 and self.write_retries >= 0, "bad retry settings")
        _require(self.max_consecutive_write_failures >= 1, "max_consecutive_write_failures must be >= 1")


@dataclass(frozen=True, slots=True)
class GuardLimits:
    """Independent hard limits enforced by ``guard.GuardedExecutor``.

    These duplicate the intent of ``RiskLimits`` on purpose, in separate code
    with its own ledger, so a bug in the risk engine cannot send an order that
    breaks them. Set them at or above the risk engine's limits.
    """

    max_order_abs: float = 25.00
    max_orders_per_minute: int = 30
    # Session loss ceiling counted by the guard's own ledger (stake at risk is
    # counted as lost until a settlement says otherwise).
    max_session_loss_abs: float = 100.00
    # If this file exists, every order is refused and the bot trips.
    kill_switch_path: str = "data/KILL"

    def __post_init__(self) -> None:
        _require(self.max_order_abs > 0, "max_order_abs must be > 0")
        _require(self.max_orders_per_minute >= 1, "max_orders_per_minute must be >= 1")
        _require(self.max_session_loss_abs > 0, "max_session_loss_abs must be > 0")


@dataclass(frozen=True, slots=True)
class OpsConfig:
    """Journal and the local operations endpoint."""

    journal_path: str = "data/journal.jsonl"
    # Local-only HTTP endpoint: GET /status, GET /metrics, POST /kill. 0 disables it.
    ops_host: str = "127.0.0.1"
    # None disables the endpoint; 0 binds an ephemeral port.
    ops_port: int | None = None

    def __post_init__(self) -> None:
        _require(self.ops_host in LOOPBACK_HOSTS, "ops endpoint must bind to a loopback address")
        _require(self.ops_port is None or 0 <= self.ops_port <= 65535, "ops_port out of range")


@dataclass(frozen=True, slots=True)
class AppConfig:
    risk: RiskLimits = field(default_factory=RiskLimits)
    latency: LatencyLimits = field(default_factory=LatencyLimits)
    feed: FeedConfig = field(default_factory=FeedConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    guard: GuardLimits = field(default_factory=GuardLimits)
    ops: OpsConfig = field(default_factory=OpsConfig)

    def __post_init__(self) -> None:
        _require(self.guard.max_order_abs >= self.risk.max_stake_abs,
                 "guard.max_order_abs must be >= risk.max_stake_abs (guard is the outer limit)")


DEFAULT_CONFIG: Final[AppConfig] = AppConfig()
