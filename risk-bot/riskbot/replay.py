"""Deterministic replay of a journal.

Rebuilds a fresh ``RiskEngine`` from the journal's ``session_start`` record and
feeds it the recorded settlements in order. At every recorded ``decision`` it
asks the engine to decide again and compares approval, stake and reason.

A clean replay proves two things about a session:
    1. the journal is intact (hash chain verified first), and
    2. every bet the bot sized follows from the recorded limits and the bot's
       own results alone, i.e. no hidden input influenced the stake.

    python -m riskbot.replay data/journal.jsonl
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field

from .config import RiskLimits
from .journal import read_records, verify_chain
from .models import Settlement
from .risk_engine import RiskEngine


@dataclass
class ReplayResult:
    chain_ok: bool
    sessions: int = 0
    decisions: int = 0
    settlements: int = 0
    mismatches: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.chain_ok and not self.mismatches and self.error is None


def replay(path: str) -> ReplayResult:
    check = verify_chain(path)
    if not check.ok:
        return ReplayResult(False, error=check.error)
    res = ReplayResult(True)
    now = [0.0]
    engine: RiskEngine | None = None
    for rec in read_records(path):
        now[0] = rec["at"]
        kind, data = rec["kind"], rec["data"]
        if kind == "session_start":
            res.sessions += 1
            engine = RiskEngine(RiskLimits(**data["risk_limits"]), clock=lambda: now[0])
            engine.restore(data["day_start_equity"], data["equity"])
        elif engine is None:
            continue
        elif kind == "decision":
            res.decisions += 1
            d = engine.evaluate()
            got = (d.approved, round(d.stake, 2), d.reason)
            want = (data["approved"], round(data["stake"], 2), data["reason"])
            if got != want:
                res.mismatches.append(f"seq {rec['seq']} round {data['round_id']}: "
                                      f"replayed {got} but journal has {want}")
        elif kind == "settlement":
            res.settlements += 1
            engine.record_settlement(Settlement(**data))
        elif kind == "cooldown_complete":
            engine.reset_streak_after_cooldown()
    return res


def main() -> None:
    ap = argparse.ArgumentParser(prog="riskbot.replay")
    ap.add_argument("journal")
    args = ap.parse_args()
    res = replay(args.journal)
    print(json.dumps({**res.__dict__, "ok": res.ok}, indent=2))
    raise SystemExit(0 if res.ok else 1)


if __name__ == "__main__":
    main()
