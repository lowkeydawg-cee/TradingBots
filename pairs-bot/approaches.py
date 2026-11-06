"""Every demo approach in one table, scored the same way, for the dashboard's Approaches page.

The pairs book (``shadow_trades``) and the buy-only strategies (``long_trades``) are written by the
watcher with demo money. This module puts them side by side: one row per closed trade, then a
scoreboard (trades, win rate, net, profit factor, worst drawdown) and a plain-words verdict.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from scanner import MARKET_OF

MIN_TRADES = 30          # below this an approach's record is too short to judge
ENOUGH_TRADES = 100      # the total at which picking a winner starts to mean something

ABOUT = {
    "Pairs (STRONG)": "Two related prices drift apart; bet they come back together. Only the best-graded signals.",
    "Pairs (all signals)": "The same pairs rule on every STRONG and RISKY signal, to check that the grading helps.",
    "Trend": "Buy when the 20-bar average is above the 100-bar average; sell when it flips or a trailing stop hits.",
    "Dip": "Buy a sharp dip (RSI under 30) while the long trend is up; sell when it bounces, stops out or after a day.",
    "Breakout": "Buy when price makes a new one-day high; sell on a trailing stop.",
    "Hold": "Buy once and keep it. The benchmark: an approach that can't beat this isn't worth running.",
}
NAMES = {"trend": "Trend", "dip": "Dip", "breakout": "Breakout", "hold": "Hold"}


def money(x: float, decimals: int = 2) -> str:
    """+$652.68 / -$33.07: the sign before the dollar sign, as people write it."""
    return f"{'-' if x < 0 else '+'}${abs(x):,.{decimals}f}"


def _connect(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)


def _read(conn: sqlite3.Connection, sql: str) -> pd.DataFrame:
    try:
        return pd.read_sql(sql, conn)
    except Exception:                          # table not there yet: that part of the bot never ran
        return pd.DataFrame()


def load_trades(path: str) -> pd.DataFrame:
    """One row per demo trade, open or closed: approach, market, what, opened, closed, net, result."""
    cols = ["approach", "market", "what", "opened_at", "closed_at", "net_pnl", "gross_pnl", "costs", "note"]
    empty = pd.DataFrame(columns=cols + ["result"])
    if not Path(path).exists():
        return empty
    conn = _connect(path)
    try:
        pairs = _read(conn, "SELECT pair AS what, grade, opened_at, closed_at, gross_pnl, costs, net_pnl, "
                            "exit_reason AS note FROM shadow_trades WHERE grade IN ('STRONG', 'RISKY')")
        sectors = _read(conn, "SELECT pair, sector FROM opportunities GROUP BY pair")
        longs = _read(conn, "SELECT strategy, symbol AS what, market, opened_at, closed_at, gross_pnl, costs, net_pnl, "
                            "COALESCE(exit_reason, entry_note) AS note FROM long_trades")
    finally:
        conn.close()
    frames = []
    if not pairs.empty:
        sector_of = dict(zip(sectors.get("pair", []), sectors.get("sector", [])))
        pairs["market"] = pairs["what"].map(lambda p: MARKET_OF.get(sector_of.get(p, ""), sector_of.get(p, "?")))
        strong = pairs[pairs["grade"] == "STRONG"].assign(approach="Pairs (STRONG)")
        frames += [strong, pairs.assign(approach="Pairs (all signals)")]
    if not longs.empty:
        longs["approach"] = longs["strategy"].map(lambda s: NAMES.get(s, s.title()))
        frames.append(longs)
    if not frames:
        return empty
    out = pd.concat([f[cols] for f in frames], ignore_index=True)
    out["result"] = np.where(out["closed_at"].isna(), "open", np.where(out["net_pnl"] > 0, "won", "lost"))
    return out.sort_values("opened_at", ignore_index=True)


def _drawdown(net: pd.Series) -> float:
    curve = net.cumsum()
    return float((curve - curve.cummax().clip(lower=0)).min()) if len(curve) else 0.0


def scoreboard(trades: pd.DataFrame) -> pd.DataFrame:
    """One row per approach, best net first. Only closed trades count."""
    closed = trades[trades["result"] != "open"].sort_values("closed_at")
    rows = []
    for name, g in closed.groupby("approach"):
        net = g["net_pnl"]
        wins, losses = net[net > 0], net[net <= 0]
        rows.append({"approach": name, "trades": len(g), "win rate": (net > 0).mean(), "net": net.sum(),
                     "per trade": net.mean(), "avg win": wins.mean() if len(wins) else np.nan,
                     "avg loss": losses.mean() if len(losses) else np.nan,
                     "profit factor": wins.sum() / -losses.sum() if losses.sum() < 0 else np.inf if len(wins) else np.nan,
                     "worst drawdown": _drawdown(net),
                     "costs": g["costs"].sum(),
                     "open now": int(((trades["approach"] == name) & (trades["result"] == "open")).sum())})
    for name in trades.loc[trades["result"] == "open", "approach"].unique():
        if name not in {r["approach"] for r in rows}:
            rows.append({"approach": name, "trades": 0, "net": 0.0,
                         "open now": int(((trades["approach"] == name) & (trades["result"] == "open")).sum())})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["net", "trades"], ascending=False, ignore_index=True)


def equity_curves(trades: pd.DataFrame) -> pd.DataFrame:
    """Cumulative net $ per approach, stepped at each trade's close."""
    closed = trades[trades["result"] != "open"].sort_values("closed_at")
    if closed.empty:
        return pd.DataFrame()
    closed = closed.assign(when=pd.to_datetime(closed["closed_at"], unit="s", utc=True))
    closed["cum"] = closed.groupby("approach")["net_pnl"].cumsum()
    return closed[["approach", "when", "cum"]]


def by_market(trades: pd.DataFrame) -> pd.DataFrame:
    closed = trades[trades["result"] != "open"]
    if closed.empty:
        return pd.DataFrame()
    return closed.pivot_table(index="approach", columns="market", values="net_pnl", aggfunc="sum").round(2)


def verdict(board: pd.DataFrame) -> tuple[str, list[str]]:
    """A one-line headline and the reasons behind it, in plain words."""
    if board.empty or board["trades"].sum() == 0:
        return ("No demo trade has closed yet.",
                ["Start the watcher and leave it running: it trades every approach with demo money on each scan."])
    total = int(board.loc[board["approach"] != "Pairs (STRONG)", "trades"].sum())   # STRONG is inside "all signals"
    hold = board[board["approach"] == "Hold"]
    lines = []
    lead = board[board["trades"] > 0].iloc[0]
    if lead["net"] <= 0:
        head = f"Nothing is making money yet. Least bad: {lead['approach']} at {money(lead['net'])}."
    else:
        head = (f"{lead['approach']} is ahead: {money(lead['net'])} over {int(lead['trades'])} trades, "
                f"{lead['win rate']:.0%} won.")
        if lead["trades"] < MIN_TRADES:
            head += " Too few trades to trust yet."
    if total < ENOUGH_TRADES:
        lines.append(f"{total} closed trades in total. Keep the demos running until about {ENOUGH_TRADES} before "
                     f"switching anything; each approach needs about {MIN_TRADES} of its own to mean much.")
    if not hold.empty and hold.iloc[0]["trades"] > 0:
        h = hold.iloc[0]["net"]
        beat = board[(board["approach"] != "Hold") & (board["net"] > h) & (board["trades"] > 0)]["approach"].tolist()
        lines.append("Beating the buy-and-hold benchmark: " + (", ".join(beat) if beat else "none so far") + ".")
    elif not hold.empty:
        lines.append("The buy-and-hold benchmark is still holding, so it only gets a score when it sells "
                     "(a replay closes it at the end).")
    for _, r in board.iterrows():
        if r["trades"] >= MIN_TRADES and r["net"] < 0 and r.get("costs", 0) > -r["net"]:
            lines.append(f"{r['approach']} lost ${-r['net']:,.2f}, less than the ${r['costs']:,.2f} it paid in costs: "
                         "the idea is roughly right, but it trades too often for the fees.")
    lines.append("Demo money on past or live prices, after costs. This is what happened, not a forecast.")
    return head, lines
