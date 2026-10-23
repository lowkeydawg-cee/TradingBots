"""Run the bot against the in-process mock feed.

    python -m riskbot --scenario normal --rounds 200
    python -m riskbot --scenario latency-spike
    python -m riskbot --scenario disconnect
    python -m riskbot --scenario drawdown
    python -m riskbot --transport ws --ops-port 9100   # real localhost WebSocket + ops endpoint
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
from pathlib import Path

from .bot_core import RiskBot
from .config import AppConfig, GuardLimits, LatencyLimits, OpsConfig, RiskLimits, StorageConfig
from .db_logger import DBLogger
from .executor import PaperExecutor
from .feed import MockCrashFeed, MockFeedScript
from .latch import LatchEngaged
from .mock_server import start_mock_server
from .ws_transport import WebSocketFeed

SCENARIOS = ("normal", "latency-spike", "disconnect", "drawdown", "loss-streak")


def build(scenario: str, rounds: int, seed: int, db_path: str,
          ops_port: int | None = None) -> tuple[AppConfig, MockFeedScript]:
    script = MockFeedScript(rounds=rounds, seed=seed, betting_window_s=0.15)
    risk = RiskLimits(cooldown_seconds=1.0)
    if scenario == "latency-spike":
        script.latency_ms = {5: 2_500.0}
    elif scenario == "disconnect":
        script.drop_mid_round = {6}
    elif scenario == "drawdown":
        script.crash_points = [1.0] * rounds  # every round busts below target
        risk = dataclasses.replace(risk, max_consecutive_losses=1_000, base_risk_fraction=0.01,
                                   max_daily_drawdown_pct=0.03)
    elif scenario == "loss-streak":
        script.crash_points = [1.0, 1.0, 1.0] + [2.0] * (rounds - 3)
    data = Path(db_path).parent
    cfg = AppConfig(
        risk=risk,
        latency=LatencyLimits(min_bet_margin_ms=50.0),
        storage=StorageConfig(db_path=db_path, fallback_path=db_path + ".fallback.jsonl",
                              latch_path=db_path + ".CIRCUIT_BREAKER.latch"),
        # Mock rounds are ~50x faster than real ones, so the order-rate cap is raised.
        guard=GuardLimits(kill_switch_path=str(data / "KILL"), max_orders_per_minute=10_000),
        ops=OpsConfig(journal_path=str(data / "journal.jsonl"), ops_port=ops_port),
    )
    return cfg, script


async def main() -> None:
    ap = argparse.ArgumentParser(prog="riskbot")
    ap.add_argument("--scenario", choices=SCENARIOS, default="normal")
    ap.add_argument("--rounds", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--db", default="data/riskbot.sqlite3")
    ap.add_argument("--transport", choices=("inproc", "ws"), default="inproc",
                    help="inproc: in-memory mock; ws: real WebSocket to a local mock server")
    ap.add_argument("--ops-port", type=int, default=None, help="serve /status, /metrics, /kill on 127.0.0.1")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    cfg, script = build(args.scenario, args.rounds, args.seed, args.db, args.ops_port)
    server = None
    if args.transport == "ws":
        server, _, port = await start_mock_server(script)
        feed_connect = WebSocketFeed(f"ws://127.0.0.1:{port}/").connect
    else:
        feed_connect = MockCrashFeed(script).connect
    bot = RiskBot(cfg, feed_connect, PaperExecutor())
    try:
        report = await bot.run()
    except (LatchEngaged, DBLogger.UncleanPreviousSession) as exc:
        raise SystemExit(f"refusing to start: {exc}") from None
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
    print(json.dumps(dataclasses.asdict(report), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
