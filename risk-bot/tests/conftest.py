from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from riskbot.config import AppConfig, GuardLimits, LatencyLimits, OpsConfig, RiskLimits, StorageConfig


@pytest.fixture
def make_cfg(tmp_path: Path):
    """Build a fast test config; keyword groups override each section."""

    def _make(risk: dict | None = None, latency: dict | None = None,
              storage: dict | None = None, guard: dict | None = None,
              ops: dict | None = None) -> AppConfig:
        base = AppConfig(
            risk=RiskLimits(cooldown_seconds=0.05),
            latency=LatencyLimits(min_bet_margin_ms=5.0, heartbeat_timeout_s=1.0,
                                  execution_timeout_s=0.2),
            storage=StorageConfig(db_path=str(tmp_path / "bot.sqlite3"),
                                  fallback_path=str(tmp_path / "fallback.jsonl"),
                                  latch_path=str(tmp_path / "CIRCUIT_BREAKER.latch"),
                                  busy_timeout_ms=50, write_retries=1, retry_backoff_base_s=0.01),
        )
        return AppConfig(
            risk=dataclasses.replace(base.risk, **(risk or {})),
            latency=dataclasses.replace(base.latency, **(latency or {})),
            feed=dataclasses.replace(base.feed, reconnect_backoff_base_s=0.01),
            storage=dataclasses.replace(base.storage, **(storage or {})),
            guard=dataclasses.replace(GuardLimits(kill_switch_path=str(tmp_path / "KILL"),
                                                    # mock rounds run ~100x faster than real ones
                                                    max_orders_per_minute=10_000), **(guard or {})),
            ops=dataclasses.replace(OpsConfig(journal_path=str(tmp_path / "journal.jsonl")), **(ops or {})),
        )

    return _make
