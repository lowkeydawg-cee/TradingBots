"""Detailed review of a replay or a live week: every demo trade, what won and lost, and whether other rules did better.

    python timing.py --replay --universe crypto      # writes review/ next to this file
    python review.py --db replay.db                  # review an existing database (no rule comparison)

Writes ``review/review.md`` (the report) and ``review/trades.csv`` (one row
per demo trade). The report has:

1. **Verdict**: did the current approach make money after costs, and does
   another rule set or another way of picking signals look clearly better?
2. **Every entry**: when, which pair and market, the grade, the z-score at
   entry, when and why it closed, how long it was held and the result.
3. **What won and what lost**, split by grade (STRONG, RISKY, AVOID), market,
   direction, exit reason (reverted, stop, time limit) and entry hour.
4. **Other approaches on the same prices**: the backtester re-runs every
   pair with stricter and looser entry lines, a full-reversion exit, a
   tighter stop and a shorter time limit, and by which signals are traded.

All of it is one week of the past. A rule that wins on one week often loses
the next (that is curve fitting), so the verdict only recommends a switch
when the difference is large and holds across several markets.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, tzinfo
from pathlib import Path

import pandas as pd

from scanner import CRYPTO, MARKET_OF

log = logging.getLogger("review")

# name, what it changes, overrides on the watcher settings
VARIANTS: tuple[tuple[str, str, dict], ...] = (
    ("current", "the bot's rules now: enter at |z| 2.0, exit at 0.5, stop at 3.5", {}),
    ("patient entry", "enter only at |z| 2.5", {"entry_z": 2.5}),
    ("early entry", "enter at |z| 1.5", {"entry_z": 1.5}),
    ("full reversion", "hold until |z| 0.1 instead of 0.5", {"exit_z": 0.1}),
    ("tight stop", "stop at |z| 3.0 instead of 3.5", {"stop_z": 3.0}),
    ("short time limit", "close after half the holding time", {"max_holding_bars": 0.5}),
)


def _stats(net: pd.Series, hours: pd.Series | None = None) -> dict:
    wins, losses = net[net > 0], net[net <= 0]
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else float("inf") if len(wins) else float("nan")
    out = {"trades": len(net), "won": len(wins), "win_rate": len(wins) / len(net) if len(net) else float("nan"),
           "net": net.sum(), "avg": net.mean() if len(net) else float("nan"),
           "avg_win": wins.mean() if len(wins) else float("nan"),
           "avg_loss": losses.mean() if len(losses) else float("nan"), "profit_factor": pf}
    if hours is not None:
        out["avg_hold_h"] = hours.mean() if len(hours) else float("nan")
    return out


def journal(db_path: str, tz: tzinfo | None = None) -> pd.DataFrame:
    """Every closed demo trade with its market, hold time and result."""
    tz = tz or datetime.now().astimezone().tzinfo
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        trades = pd.read_sql("SELECT pair, grade, flags, direction, opened_at, entry_z, closed_at, exit_z, "
                             "gross_pnl, costs, net_pnl, exit_reason FROM shadow_trades ORDER BY opened_at", conn)
        sectors = dict(conn.execute("SELECT pair, sector FROM opportunities GROUP BY pair").fetchall())
    finally:
        conn.close()
    if trades.empty:
        return trades
    trades["market"] = trades["pair"].map(lambda p: MARKET_OF.get(sectors.get(p, ""), sectors.get(p, "?")))
    trades["opened"] = pd.to_datetime(trades["opened_at"], unit="s", utc=True).dt.tz_convert(tz)
    trades["closed"] = pd.to_datetime(trades["closed_at"], unit="s", utc=True).dt.tz_convert(tz)
    trades["hold_h"] = (trades["closed_at"] - trades["opened_at"]) / 3600
    trades["result"] = trades["net_pnl"].map(lambda x: "open" if pd.isna(x) else "won" if x > 0 else "lost")
    trades["hour"] = trades["opened"].dt.hour
    trades["exit_kind"] = trades["exit_reason"].fillna("").map(_exit_kind)
    return trades


def _exit_kind(reason: str) -> str:
    r = reason.lower()
    if "stop" in r and "time" not in r:
        return "stop loss"
    if "time" in r or "max" in r or "holding" in r:
        return "time limit"
    if "revert" in r or "exit" in r or "mean" in r:
        return "reverted (target)"
    return reason or "?"


def breakdown(trades: pd.DataFrame, by: str) -> pd.DataFrame:
    closed = trades[trades["result"] != "open"]
    rows = {k: _stats(g["net_pnl"], g["hold_h"]) for k, g in closed.groupby(by)}
    return pd.DataFrame(rows).T.sort_values("net", ascending=False) if rows else pd.DataFrame()


# ---------------------------------------------------------------------------
# Other approaches on the same prices
# ---------------------------------------------------------------------------
def _variant_job(args) -> list[tuple]:
    closes, cfg, costs, all_hours, start, pair, market, variants = args
    from backtest import run_backtest
    out = []
    for name, _desc, over in variants:
        o = {k: (round(getattr(cfg, k) * v) if isinstance(getattr(cfg, k), int) and isinstance(v, float) else v)
             for k, v in over.items()}
        try:
            vcfg = dataclasses.replace(cfg, **o)
            res = run_backtest(closes, vcfg, costs, all_hours=all_hours)
        except Exception:                       # e.g. a rule set the config rejects
            continue
        for t in res.trades:
            if t.closed_at is not None and t.opened_at.timestamp() >= start:
                out.append((name, market, pair, t.net_pnl))
    return out


def compare_rules(stocks: pd.DataFrame, crypto: pd.DataFrame, sectors: dict[str, list[str]], cfg, start: float,
                  costs_for) -> pd.DataFrame:
    """Every variant's trades opened inside the replay window, one row per trade."""
    import itertools
    jobs = []
    for sector, syms in sectors.items():
        frame = crypto if sector in CRYPTO else stocks
        for a, b in itertools.combinations(syms, 2):
            if a not in frame or b not in frame:
                continue
            df = frame[[a, b]].dropna()
            first = df.index.searchsorted(pd.Timestamp(start, unit="s", tz="UTC"))
            df = df.iloc[max(0, first - cfg.hedge_lookback - 5):]   # warm-up, then the replay window
            if len(df) <= cfg.hedge_lookback + 10:
                continue
            pcfg = cfg.for_pair(a, b)
            jobs.append((df, pcfg, costs_for(sector), sector in CRYPTO, start, f"{a}/{b}",
                         MARKET_OF.get(sector, sector), VARIANTS))
    log.info("re-running %d pairs under %d rule sets (uses every CPU core)", len(jobs), len(VARIANTS))
    rows: list[tuple] = []
    with ProcessPoolExecutor() as pool:
        for r in pool.map(_variant_job, jobs):
            rows += r
    return pd.DataFrame(rows, columns=["variant", "market", "pair", "net_pnl"])


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def _money(x) -> str:
    return "n/a" if pd.isna(x) else f"${x:+,.2f}"


def _table(df: pd.DataFrame, label: str) -> list[str]:
    if df.empty:
        return ["(none)"]
    lines = [f"| {label} | trades | won | win rate | net after costs | avg trade | avg win | avg loss | avg hold |",
             "|---|---|---|---|---|---|---|---|---|"]
    for k, r in df.iterrows():
        hold = "" if pd.isna(r.get("avg_hold_h", float("nan"))) else f"{r['avg_hold_h']:.1f}h"
        lines.append(f"| {k} | {int(r['trades'])} | {int(r['won'])} | {r['win_rate']:.0%} | {_money(r['net'])} | "
                     f"{_money(r['avg'])} | {_money(r['avg_win'])} | {_money(r['avg_loss'])} | {hold} |")
    return lines


def verdict(trades: pd.DataFrame, variants: pd.DataFrame | None) -> list[str]:
    out = []
    closed = trades[trades["result"] != "open"] if not trades.empty else trades
    if closed.empty:
        return ["No demo trade closed in this period, so there is nothing to judge yet."]
    total = closed["net_pnl"].sum()
    by_grade = closed.groupby("grade")["net_pnl"].agg(["sum", "count", lambda s: (s > 0).mean()])
    out.append(f"The watcher's demo book took {len(closed)} trades (every STRONG, RISKY and AVOID signal) and "
               f"{'made' if total > 0 else 'lost'} ${abs(total):,.2f} after costs ({(closed['net_pnl'] > 0).mean():.0%} won).")
    gross, costs = closed["gross_pnl"].mean(), closed["costs"].mean()
    out.append(f"The average trade made {_money(gross)} before costs and paid ${costs:,.2f} in costs"
               + (": costs ate the edge. Fewer, bigger moves (or cheaper markets) would help more than more trades."
                  if gross > 0 and gross < costs else
                  ": the price moves themselves went against the bot, not just the costs." if gross <= 0 else "."))
    if "STRONG" in by_grade.index:
        s = by_grade.loc["STRONG"]
        others = closed[closed["grade"] != "STRONG"]["net_pnl"]
        out.append(f"STRONG signals only: {int(s['count'])} trades, {_money(s['sum'])}, {s.iloc[2]:.0%} won; "
                   f"everything else: {len(others)} trades, {_money(others.sum())}. "
                   + ("The grading works: STRONG beat the rest." if s["sum"] > others.sum() / max(len(others), 1) * s["count"]
                      else "STRONG did not beat the rest this week: the filters aren't proving themselves yet."))
    else:
        out.append("There were no STRONG signals, so the grading can't be judged on this week.")
    by_market = closed.groupby("market")["net_pnl"].sum().sort_values(ascending=False)
    if len(by_market) > 1:
        out.append("By market: " + ", ".join(f"{m} {_money(v)}" for m, v in by_market.items()) + ".")
    if variants is not None and not variants.empty:
        tot = variants.groupby("variant")["net_pnl"].agg(["sum", "count"])
        mkt = variants.pivot_table(index="variant", columns="market", values="net_pnl", aggfunc="sum").fillna(0)
        cur = tot.loc["current", "sum"] if "current" in tot.index else 0.0
        best = tot["sum"].idxmax()
        out.append("Same prices, other rules (every pair, all signals): " + ", ".join(
            f"{v} {_money(tot.loc[v, 'sum'])} ({int(tot.loc[v, 'count'])} trades)" for v in tot.sort_values("sum", ascending=False).index) + ".")
        if best != "current" and "current" in mkt.index:
            beats = int((mkt.loc[best] > mkt.loc["current"]).sum())
            gap = tot.loc[best, "sum"] - cur
            if beats >= max(2, (len(mkt.columns) + 1) // 2) and gap > max(50.0, abs(cur) * 0.5):
                out.append(f"'{best}' beat the current rules by {_money(gap)} and in {beats} of {len(mkt.columns)} "
                           f"markets. That is worth testing next: run the backtester on a longer period with it "
                           f"before switching, because one week can flatter any rule.")
            else:
                out.append(f"'{best}' did best, but only by {_money(gap)} or in few markets: not enough to switch on "
                           f"one week. Keep the current rules and re-check after more weeks.")
        else:
            out.append("The current rules did as well as or better than every alternative this week: no reason to switch.")
        if cur < 0 and tot["sum"].max() < 0:
            out.append("Every rule set lost money on these prices: pairs trading had no edge this week after costs. "
                       "Trading less (only the best market, only STRONG) matters more than tuning the rules.")
    return out


def write_review(db_path: str, out_dir: str | Path, variants: pd.DataFrame | None = None,
                 tz: tzinfo | None = None, title: str = "Replay review") -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    trades = journal(db_path, tz)
    if not trades.empty:
        trades.drop(columns=["opened_at", "closed_at"]).to_csv(out_dir / "trades.csv", index=False)
    tzname = getattr(tz, "key", None) or datetime.now().astimezone().tzname()
    lines = [f"# {title}", "", f"Times are {tzname}. Demo money only; costs included. This is the past, not a forecast.",
             "", "## Verdict", ""] + [f"- {v}" for v in verdict(trades, variants)]
    if not trades.empty:
        lines += ["", "## What won and what lost", "", "### By grade", ""] + _table(breakdown(trades, "grade"), "grade")
        lines += ["", "### By market", ""] + _table(breakdown(trades, "market"), "market")
        lines += ["", "### By direction", ""] + _table(breakdown(trades, "direction"), "direction")
        lines += ["", "### By how the trade ended", ""] + _table(breakdown(trades, "exit_kind"), "exit")
        lines += ["", "### By entry hour", ""] + _table(breakdown(trades, "hour").sort_index(), "hour")
        closed = trades[trades["result"] != "open"]
        if len(closed):
            lines += ["", "### Biggest wins", ""] + [_trade_line(r) for _, r in closed.nlargest(5, "net_pnl").iterrows()]
            lines += ["", "### Biggest losses", ""] + [_trade_line(r) for _, r in closed.nsmallest(5, "net_pnl").iterrows()]
        lines += ["", "## Every entry", "", "| opened | pair | market | grade | side | z in | closed | ended | held | result |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for _, r in trades.iterrows():
            closed_at = "" if pd.isna(r["closed"]) else r["closed"].strftime("%a %H:%M")
            held = "" if pd.isna(r["hold_h"]) else f"{r['hold_h']:.1f}h"
            lines.append(f"| {r['opened'].strftime('%a %d %b %H:%M')} | {r['pair']} | {r['market']} | {r['grade']} | "
                         f"{r['direction']} | {r['entry_z']:+.2f} | {closed_at} | {r['exit_kind'] if closed_at else 'still open'} | "
                         f"{held} | {_money(r['net_pnl']) if closed_at else ''} |")
    if variants is not None and not variants.empty:
        lines += ["", "## Other approaches on the same prices", "",
                  "Each pair re-run by the backtester with one rule changed; every signal traded, costs included.", ""]
        for name, desc, _ in VARIANTS:
            v = variants[variants["variant"] == name]["net_pnl"]
            lines.append(f"- **{name}** ({desc}): {len(v)} trades, {(v > 0).mean() if len(v) else 0:.0%} won, "
                         f"net {_money(v.sum())}")
        piv = variants.pivot_table(index="variant", columns="market", values="net_pnl", aggfunc="sum").fillna(0)
        lines += ["", "| rule set | " + " | ".join(piv.columns) + " |", "|---|" + "---|" * len(piv.columns)]
        for name in piv.index:
            lines.append(f"| {name} | " + " | ".join(_money(x) for x in piv.loc[name]) + " |")
    try:
        from longonly import comparison_rows
        rows = comparison_rows(db_path)
    except Exception:
        rows = []
    if any(r[1] for r in rows):
        lines += ["", "## Pairs against buy-only strategies (same period, demo money)", "",
                  "| approach | closed trades | won | net after costs |", "|---|---|---|---|"]
        lines += [f"| {label} | {n} | {w / n:.0%} | {_money(net)} |" if n else f"| {label} | 0 | | |"
                  for label, n, w, net in rows]
    lines += ["", "Every trade is in trades.csv next to this file."]
    path = out_dir / "review.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _trade_line(r) -> str:
    return (f"- {r['opened'].strftime('%a %d %b %H:%M')} {r['pair']} ({r['market']}, {r['grade']}, z {r['entry_z']:+.2f}): "
            f"{_money(r['net_pnl'])} after {r['hold_h']:.1f}h, {r['exit_kind']}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Detailed review of the demo trades in a database")
    ap.add_argument("--db", help="database (default: replay.db next to this file)")
    ap.add_argument("--out", default=str(Path(__file__).with_name("review")))
    args = ap.parse_args(argv)
    path = write_review(args.db or str(Path(__file__).with_name("replay.db")), args.out)
    print(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
