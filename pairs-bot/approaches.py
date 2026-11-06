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

# How the approaches differ, one row each, for the page's comparison table.
COMPARE_COLUMNS = ("Approach", "Bets on", "Gets in when", "Gets out when", "Does well when", "Does badly when",
                   "Markets", "Real orders on Alpaca")
COMPARE = (
    ("Pairs", "Two related prices that drifted apart coming back together",
     "The gap between them is 2+ normal swings wide (z-score ±2), graded STRONG",
     "The gap closes to ±0.5, or widens past ±3.5 (stop), or takes too long",
     "Markets go sideways and related prices move together", "One of the pair has real news and the link breaks",
     "US stocks and funds; crypto as demo only", "Yes on stocks (buys one, sells the other short); no on crypto"),
    ("Trend", "A price that's rising keeps rising",
     "On a closed 4-hour bar: the market is rising, the 20-bar average is above the 100-bar one, price above both",
     "The averages cross back, the market turns down, or price falls 2 days of normal movement from its high",
     "Long one-way moves up", "Choppy markets: it buys late and sells late, paying fees each time",
     "Crypto, gold, silver, gold miners", "Yes (buy only)"),
    ("Dip", "A sharp fall in a rising market bounces back",
     "RSI drops under 30 while the market is rising and calm (not in a wild swing)",
     "RSI recovers above 55, it falls 1.5 more days of movement (stop), the market turns down, or 2 days pass",
     "Rising markets with quick pullbacks", "A real crash: the dip keeps dipping",
     "Crypto, gold, silver, gold miners", "Yes (buy only)"),
    ("Breakout", "A new high leads to more highs",
     "Price clears its 5-day high by half a normal bar's move, in a rising market; waits a day after a failed one",
     "Price falls 2 days of normal movement from its high (trailing stop), or the market turns down",
     "Strong news-driven runs", "False breakouts that reverse straight away",
     "Crypto, gold, silver, gold miners", "Yes (buy only)"),
    ("Hold", "Nothing: it's the yardstick", "At the start, the same money spread evenly", "Never (marked at the last price at the end of a replay)",
     "Markets that go up", "Markets that go down", "Crypto, gold, silver, gold miners", "Yes (buy only)"),
)

# The risk rules every buy-only approach (not pairs) runs under, from governance.py, for the page.
RISK_RULES = (
    "Each approach has its own $10,000 demo account, so they're compared on equal terms.",
    "Size: each trade risks 1% of the account, measured as two days of the coin's normal movement. "
    "Wild coins get smaller positions than calm ones.",
    "Caps: at most 25% of the account in one asset, 50% in crypto together, and 20% always kept in cash.",
    "Losing streaks: 5% below its high point it trades half size (caution), 10% below it stops buying (defense), "
    "15% below it sells everything and stops (stopped) until someone restarts it. It never bets more to win back losses.",
)

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
    cols = ["approach", "market", "what", "opened_at", "closed_at", "net_pnl", "gross_pnl", "costs", "note", "open_pnl"]
    empty = pd.DataFrame(columns=cols + ["result"])
    if not Path(path).exists():
        return empty
    conn = _connect(path)
    try:
        pairs = _read(conn, "SELECT pair AS what, grade, opened_at, closed_at, gross_pnl, costs, net_pnl, "
                            "exit_reason AS note FROM shadow_trades WHERE grade IN ('STRONG', 'RISKY')")
        sectors = _read(conn, "SELECT pair, sector FROM opportunities GROUP BY pair")
        longs = _read(conn, "SELECT strategy, symbol AS what, market, opened_at, closed_at, gross_pnl, costs, net_pnl, "
                            "COALESCE(exit_reason, entry_note) AS note, entry, qty, last_price FROM long_trades")
        if longs.empty:                        # a database from before last_price existed
            longs = _read(conn, "SELECT strategy, symbol AS what, market, opened_at, closed_at, gross_pnl, costs, "
                                "net_pnl, COALESCE(exit_reason, entry_note) AS note FROM long_trades")
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
        if "last_price" in longs:              # live value if sold now: price move minus buy and (about the same) sell cost
            held = longs["closed_at"].isna() & longs["last_price"].notna()
            longs.loc[held, "open_pnl"] = ((longs["last_price"] - longs["entry"]) * longs["qty"] - 2 * longs["costs"])[held]
        frames.append(longs)
    if not frames:
        return empty
    out = pd.concat([f.reindex(columns=cols) for f in frames], ignore_index=True)
    out["result"] = np.where(out["closed_at"].isna(), "open", np.where(out["net_pnl"] > 0, "won", "lost"))
    return out.sort_values("opened_at", ignore_index=True)


def _drawdown(net: pd.Series) -> float:
    curve = net.cumsum()
    return float((curve - curve.cummax().clip(lower=0)).min()) if len(curve) else 0.0


BOARD_COLUMNS = ["approach", "trades", "win rate", "net", "per trade", "avg win", "avg loss", "profit factor",
                 "worst drawdown", "costs", "open now"]


def scoreboard(trades: pd.DataFrame) -> pd.DataFrame:
    """One row per approach, best first. ``net`` is closed trades; ``open P&L`` is what open ones are worth now."""
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
            rows.append({"approach": name, "trades": 0, "win rate": np.nan, "net": 0.0, "per trade": np.nan,
                         "avg win": np.nan, "avg loss": np.nan, "profit factor": np.nan, "worst drawdown": 0.0,
                         "costs": 0.0,
                         "open now": int(((trades["approach"] == name) & (trades["result"] == "open")).sum())})
    if not rows:
        return pd.DataFrame()
    board = pd.DataFrame(rows).reindex(columns=BOARD_COLUMNS)    # same columns even when nothing has closed
    board["trades"] = board["trades"].fillna(0).astype(int)
    open_pnl = trades.groupby("approach")["open_pnl"].sum(min_count=1)
    board["open P&L"] = board["approach"].map(open_pnl).fillna(0.0)
    board["total"] = board["net"] + board["open P&L"]
    return board.sort_values(["total", "trades"], ascending=False, ignore_index=True)


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
        lines = ["Start the watcher and leave it running: it trades every approach with demo money on each scan."]
        if not board.empty and board["open now"].sum():
            lines = [f"{int(board['open now'].sum())} demo positions are open. Results start once they sell; "
                     "the scoreboard below already counts what they're worth now."]
        return "No demo trade has closed yet.", lines
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
    if not hold.empty and (hold.iloc[0]["trades"] > 0 or hold.iloc[0]["open P&L"] != 0):
        h = hold.iloc[0]["total"]
        beat = board[(board["approach"] != "Hold") & (board["total"] > h) & (board["trades"] > 0)]["approach"].tolist()
        lines.append(f"Buy-and-hold is at {money(h)} counting what it holds now. Beating it, open positions "
                     "included: " + (", ".join(beat) if beat else "none so far") + ".")
    elif not hold.empty:
        lines.append("Buy-and-hold has no live price yet, so it can't be compared until the next scan.")
    for _, r in board.iterrows():
        if r["trades"] >= MIN_TRADES and r["net"] < 0 and r.get("costs", 0) > -r["net"]:
            lines.append(f"{r['approach']} lost ${-r['net']:,.2f}, less than the ${r['costs']:,.2f} it paid in costs: "
                         "the idea is roughly right, but it trades too often for the fees.")
    lines.append("Demo money on past or live prices, after costs. This is what happened, not a forecast.")
    return head, lines


def _table(path: str, sql: str) -> pd.DataFrame:
    if not Path(path).exists():
        return pd.DataFrame()
    conn = _connect(path)
    try:
        return _read(conn, sql)
    finally:
        conn.close()


def load_sleeves(path: str) -> pd.DataFrame:
    """Each buy-only demo account: value, how far below its high, and its risk state."""
    return _table(path, "SELECT strategy, value, drawdown, state, updated_at FROM long_sleeves ORDER BY strategy")


def latest_decisions(path: str) -> pd.DataFrame:
    """What each buy-only approach last decided on each market, and why: the 'why it did nothing' view."""
    return _table(path, "SELECT d.strategy, d.symbol, d.action, d.reason, d.state, d.ts FROM long_decisions d "
                        "JOIN (SELECT strategy, symbol, MAX(id) AS id FROM long_decisions GROUP BY strategy, symbol) m "
                        "ON d.id = m.id ORDER BY d.strategy, d.symbol")


def recent_actions(path: str, limit: int = 50) -> pd.DataFrame:
    """The latest buys, sells and refused trades (waits left out), newest first."""
    return _table(path, "SELECT ts, strategy, symbol, action, reason, price, qty, state FROM long_decisions "
                        f"WHERE action IN ('buy', 'sell', 'refused') ORDER BY id DESC LIMIT {int(limit)}")


def holdings(path: str) -> pd.DataFrame:
    """What the demo holds now: approach, coin, market, buy price, price now, value, and profit if sold now."""
    held = _table(path, "SELECT strategy, symbol, market, opened_at, entry, qty, last_price, costs FROM long_trades "
                        "WHERE closed_at IS NULL ORDER BY market, symbol, strategy")
    if held.empty:
        return held
    now = held["last_price"].astype(float).fillna(held["entry"])
    return pd.DataFrame({
        "Approach": held["strategy"].map(lambda s: NAMES.get(s, s.title())), "What": held["symbol"],
        "Market": held["market"], "Since": pd.to_datetime(held["opened_at"], unit="s", utc=True),
        "Bought at": held["entry"], "Price now": now, "Value": held["qty"] * now,
        "Profit if sold now": (now - held["entry"]) * held["qty"] - 2 * held["costs"]})


def latest_brief(path: str) -> list[str]:
    """The watcher's newest market read, as plain lines. The first line is its title and the last a disclaimer."""
    brief = _table(path, "SELECT text FROM market_notes WHERE kind = 'brief' ORDER BY id DESC LIMIT 1")
    return [] if brief.empty else str(brief["text"].iloc[0]).splitlines()[1:-1]


def markets_watched(path: str) -> list[str]:
    """The markets the last scan looked at, by name."""
    seen = _table(path, "SELECT DISTINCT sector FROM opportunities WHERE ts >= (SELECT MAX(ts) - 1800 FROM opportunities)")
    return sorted({MARKET_OF.get(s, s) for s in seen["sector"]}) if not seen.empty else []
