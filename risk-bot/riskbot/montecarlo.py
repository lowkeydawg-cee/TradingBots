"""Monte Carlo evaluation of the risk POLICY (not of any prediction).

Runs thousands of synthetic sessions through the real ``RiskEngine`` and
reports the distribution of outcomes, so limits can be tuned on evidence:
how often the daily stop is hit, how deep drawdowns go, and what a session
costs on average. A flat-stake policy with no governance is run on the same
random rounds as a baseline.

Crash points are drawn from the standard house-edge distribution
P(crash >= m) = (1 - edge) / m. The policy never sees them; they only decide
whether each fixed-target bet wins.

    python -m riskbot.montecarlo --sessions 5000 --rounds 300
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import random
import statistics
from dataclasses import dataclass

from .config import RiskLimits
from .models import Position, Settlement
from .risk_engine import Breach, RiskEngine


def crash_point(rng: random.Random, edge: float) -> float:
    u = rng.random()
    return max(1.0, math.floor(100 * (1 - edge) / (1 - u)) / 100)


@dataclass
class SessionResult:
    pnl_pct: float
    max_drawdown_pct: float
    bets: int
    stopped_by: str


def run_governed(limits: RiskLimits, crashes: list[float], cooldown_rounds: int) -> SessionResult:
    eng = RiskEngine(limits, clock=lambda: 0.0)  # fixed clock: one trading day
    start = eng.equity
    peak_dd, skip, stopped = 0.0, 0, "rounds_exhausted"
    for i, cp in enumerate(crashes):
        if skip:
            skip -= 1
            if skip == 0:
                eng.reset_streak_after_cooldown()
            continue
        d = eng.evaluate()
        if not d.approved:
            stopped = d.reason
            if d.reason in ("daily_drawdown_reached", "session_bet_limit", "no_equity"):
                break
            continue
        s = Settlement.from_crash(Position(i, d.stake, limits.target_cashout, 0.0, "mc"), cp)
        breaches = eng.record_settlement(s)
        peak_dd = max(peak_dd, eng.drawdown)
        if Breach.DAILY_DRAWDOWN in breaches or Breach.BANKRUPT in breaches:
            stopped = "circuit_breaker:daily_drawdown"
            break
        if Breach.SESSION_BET_LIMIT in breaches:
            stopped = "session_bet_limit"
            break
        if Breach.LOSS_STREAK in breaches:
            skip = cooldown_rounds
            if skip == 0:
                eng.reset_streak_after_cooldown()
    return SessionResult((eng.equity - start) / start * 100, peak_dd * 100, eng.bets, stopped)


def run_flat(bankroll: float, stake_frac: float, target: float, crashes: list[float]) -> SessionResult:
    """Baseline: fixed stake every round, no limits at all."""
    eq, peak, max_dd, bets = bankroll, bankroll, 0.0, 0
    stake = round(bankroll * stake_frac, 2)
    for cp in crashes:
        if eq < stake:
            break
        bets += 1
        eq += stake * (target - 1) if cp >= target else -stake
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)
    return SessionResult((eq - bankroll) / bankroll * 100, max_dd * 100, bets, "none")


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(q * len(xs))))]


def summarise(results: list[SessionResult]) -> dict[str, float]:
    pnl = [r.pnl_pct for r in results]
    dd = [r.max_drawdown_pct for r in results]
    stops = [r.stopped_by for r in results]
    return {
        "mean_pnl_pct": round(statistics.fmean(pnl), 3),
        "median_pnl_pct": round(statistics.median(pnl), 3),
        "p05_pnl_pct": round(pct(pnl, 0.05), 3),
        "p95_pnl_pct": round(pct(pnl, 0.95), 3),
        "worst_pnl_pct": round(min(pnl), 3),
        "p95_max_drawdown_pct": round(pct(dd, 0.95), 3),
        "worst_max_drawdown_pct": round(max(dd), 3),
        "mean_bets": round(statistics.fmean(r.bets for r in results), 1),
        "share_hit_daily_stop": round(stops.count("circuit_breaker:daily_drawdown") / len(stops), 4),
        "share_profitable": round(sum(p > 0 for p in pnl) / len(pnl), 4),
    }


def simulate(limits: RiskLimits, sessions: int, rounds: int, edge: float, seed: int,
             cooldown_rounds: int) -> dict[str, object]:
    rng = random.Random(seed)
    governed, flat = [], []
    for _ in range(sessions):
        crashes = [crash_point(rng, edge) for _ in range(rounds)]
        governed.append(run_governed(limits, crashes, cooldown_rounds))
        flat.append(run_flat(limits.starting_bankroll, limits.max_stake_fraction, limits.target_cashout, crashes))
    p_win = (1 - edge) / limits.target_cashout
    return {
        "inputs": {"sessions": sessions, "rounds": rounds, "house_edge": edge, "seed": seed,
                   "cooldown_rounds": cooldown_rounds, "target_cashout": limits.target_cashout},
        "expected_value_per_unit_staked": round(p_win * limits.target_cashout - 1, 4),
        "governed": summarise(governed),
        "flat_stake_baseline": summarise(flat),
    }


def main() -> None:
    ap = argparse.ArgumentParser(prog="riskbot.montecarlo")
    ap.add_argument("--sessions", type=int, default=5000)
    ap.add_argument("--rounds", type=int, default=300)
    ap.add_argument("--edge", type=float, default=0.03)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--cooldown-rounds", type=int, default=6)
    ap.add_argument("--target", type=float, default=None, help="override target_cashout")
    ap.add_argument("--max-dd", type=float, default=None, help="override max_daily_drawdown_pct")
    args = ap.parse_args()
    limits = RiskLimits()
    if args.target is not None:
        limits = dataclasses.replace(limits, target_cashout=args.target)
    if args.max_dd is not None:
        limits = dataclasses.replace(limits, max_daily_drawdown_pct=args.max_dd)
    print(json.dumps(simulate(limits, args.sessions, args.rounds, args.edge, args.seed,
                              args.cooldown_rounds), indent=2))


if __name__ == "__main__":
    main()
