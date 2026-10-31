"""Talk to the bot from Jarvis (or any MCP app): "Jarvis, how's the trading bot doing?"

This is a Model Context Protocol server. Jarvis starts it, sees the tools
below, and its model calls them when you ask about the bot:

    bot_status       running, paused or halted; equity; open trades; last heartbeat
    market_brief     how the market looks: mood, sectors, what changed, recent alerts
    market_watch     the watcher's best opportunities right now, with their warnings
    explain_pair     why a pair is graded the way it is, and what the bot did with it lately
    recent_trades    the last closed trades, real paper and demo (shadow)
    profit_summary   P&L today, this week and all time
    demo_results     do STRONG signals beat RISKY and AVOID ones? which hours work best?
    pause_trading / resume_trading / emergency_stop

Everything is read from ``pairs_bot.db`` through a read-only connection, so it
works whether the bot is running or not. The three actions only write a flag
the bot reads on its next cycle, exactly like the dashboard button. Set
``confirm: true`` for this server in Jarvis so it asks you out loud first.

Jarvis config.yaml::

    mcp:
      servers:
        trading:
          command: C:/Users/SAINT/TradingBots/pairs-bot/.venv/Scripts/python.exe
          args: ["C:/Users/SAINT/TradingBots/pairs-bot/mcp_server.py"]
          env: {PAIRS_DB_PATH: "C:/Users/SAINT/TradingBots/pairs-bot/pairs_bot.db"}
          confirm: true

Run it by hand to check it starts: ``python mcp_server.py`` (it waits for an MCP client on stdin).
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from db_logger import connect_readonly

NY = ZoneInfo("America/New_York")
GRADE_SQL = "CASE grade WHEN 'STRONG' THEN 0 WHEN 'RISKY' THEN 1 WHEN 'AVOID' THEN 2 WHEN 'WATCH' THEN 3 ELSE 4 END"


def db_path() -> str:
    return os.environ.get("PAIRS_DB_PATH", str(Path(__file__).with_name("pairs_bot.db")))


def _rows(sql: str, params: tuple = (), path: str | None = None) -> list[tuple]:
    path = path or db_path()
    if not Path(path).exists():
        raise FileNotFoundError(f"no bot database at {path}; start the bot or the watcher first")
    conn = connect_readonly(path)
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:            # table missing: that part never ran
        if "no such table" in str(exc):
            return []
        raise
    finally:
        conn.close()


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = time.time() - float(ts)
    return f"{s:.0f} seconds ago" if s < 120 else f"{s / 60:.0f} minutes ago" if s < 7200 else f"{s / 3600:.1f} hours ago" if s < 172800 else f"{s / 86400:.0f} days ago"


# ---------------------------------------------------------------------------
# Read-only reports (plain functions, so they're easy to test and reuse)
# ---------------------------------------------------------------------------
def bot_status(path: str | None = None) -> str:
    ctl = dict(_rows("SELECT key, value FROM control", path=path))
    state = (f"HALTED: {ctl['halted']}" if ctl.get("halted") else
             f"PAUSED by {ctl['paused']}: no new trades" if ctl.get("paused") else "Running")
    lines = [f"{state}. Last heartbeat {_ago(float(ctl.get('heartbeat') or 0))}."]
    eq = _rows("SELECT equity, daily_pnl, gross_exposure FROM equity ORDER BY id DESC LIMIT 1", path=path)
    if eq:
        e, d, g = eq[0]
        lines.append(f"Paper equity ${e:,.2f}, today {d:+,.2f}, exposure ${g:,.0f}.")
    open_trades = _rows("SELECT pair, direction, entry_z, opened_at FROM pair_trades WHERE closed_at IS NULL", path=path)
    if open_trades:
        lines += [f"Open: {p or 'pair'} {d} from z {z:+.2f}, opened {_ago(t)}." for p, d, z, t in open_trades]
    else:
        lines.append("No open trades.")
    retired = [k.split(":", 1)[1] for k, v in ctl.items() if k.startswith("retired:") and v]
    if retired:
        lines.append("Retired pairs: " + ", ".join(retired) + ".")
    return " ".join(lines)


def market_watch(limit: int = 8, path: str | None = None) -> str:
    rows = _rows(
        "SELECT o.grade, o.pair, o.side, o.zscore, o.prob, o.hist_trades, o.flags, o.bar_time FROM opportunities o "
        "JOIN (SELECT pair, MAX(id) AS id FROM opportunities GROUP BY pair) m ON o.id = m.id "
        f"WHERE o.grade != 'QUIET' ORDER BY {GRADE_SQL.replace('grade', 'o.grade')}, o.prob DESC LIMIT ?",
        (int(limit),), path)
    if not rows:
        return "The market watcher has nothing to report. It may not be running: start it with python watcher.py."
    out = [f"Market watch, updated {_ago(max(r[7] for r in rows))}:"]
    for grade, pair, side, z, prob, n, flags, _ in rows:
        line = f"{grade} {pair}: z {z:+.2f}, recent win rate {prob:.0%} over {n} trades"
        line += f", signal {side}" if side else ""
        line += f". Warnings: {flags}" if flags else ""
        out.append(line + ".")
    return "\n".join(out)


def market_brief(path: str | None = None) -> str:
    """The watcher's latest read of the market, plus the alerts of the last 24 hours."""
    brief = _rows("SELECT ts, text FROM market_notes WHERE kind = 'brief' ORDER BY id DESC LIMIT 1", (), path)
    if not brief:
        return "No market brief yet. The market watcher writes one every scan: start it with python watcher.py."
    ts, text = brief[0]
    alerts = _rows("SELECT ts, text FROM market_notes WHERE kind = 'alert' AND ts >= ? ORDER BY id DESC LIMIT 6",
                   (ts - 86_400,), path)
    out = [f"(written {_ago(ts)})", text]
    if alerts:
        out.append("Alerts in the last day:")
        out += [f"- {_ago(t)}: {a}" for t, a in alerts]
    return "\n".join(out)


def explain_pair(pair: str, path: str | None = None) -> str:
    pair = pair.upper().replace(":", "/").replace(" ", "")
    row = _rows("SELECT grade, side, zscore, coint_pvalue, half_life, vol_ratio, prob, hist_trades, hist_avg_net, "
                "flags, bar_time FROM opportunities WHERE pair = ? ORDER BY id DESC LIMIT 1", (pair,), path)
    out = []
    if row:
        g, side, z, p, hl, vr, prob, n, avg, flags, t = row[0]
        out.append(f"{pair} is {g} (as of {_ago(t)}). z-score {z:+.2f}, cointegration p-value {p:.3f}, "
                   f"half-life {hl:.0f} bars, volatility {vr:.1f}x normal. Recently it won {prob:.0%} of {n} "
                   f"trades, averaging ${avg or 0:+.2f} each after costs.")
        if flags:
            out.append(f"Warnings: {flags}.")
        if side:
            out.append(f"Signal: {side}.")
    sig = _rows("SELECT action, note, ts FROM signals WHERE pair = ? AND action != 'HOLD' ORDER BY id DESC LIMIT 3",
                (pair,), path)
    if sig:
        out.append("The trading bot lately: " + "; ".join(f"{a} {_ago(t)} ({n})" for a, n, t in sig) + ".")
    shadow = _rows("SELECT COUNT(*), COALESCE(SUM(net_pnl), 0) FROM shadow_trades WHERE pair = ? "
                   "AND closed_at IS NOT NULL", (pair,), path)
    if shadow and shadow[0][0]:
        out.append(f"Demo trades on it: {shadow[0][0]}, net ${shadow[0][1]:+,.2f}.")
    return " ".join(out) or f"I have no data on {pair}. Pairs are written like KO/PEP."


def recent_trades(limit: int = 5, path: str | None = None) -> str:
    real = _rows("SELECT pair, direction, pnl, exit_reason, closed_at FROM pair_trades WHERE closed_at IS NOT NULL "
                 "ORDER BY closed_at DESC LIMIT ?", (int(limit),), path)
    demo = _rows("SELECT pair, grade, direction, net_pnl, exit_reason, closed_at FROM shadow_trades "
                 "WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT ?", (int(limit),), path)
    out = []
    if real:
        out.append("Paper trades: " + "; ".join(
            f"{p or 'pair'} {d} ${pnl or 0:+.2f} ({(r or '').split(':')[0]}, {_ago(t)})" for p, d, pnl, r, t in real))
    if demo:
        out.append("Demo trades: " + "; ".join(
            f"{g} {p} ${pnl:+.2f} ({(r or '').split(':')[0]})" for p, g, d, pnl, r, t in demo))
    return "\n".join(out) or "No closed trades yet."


def profit_summary(path: str | None = None) -> str:
    now = datetime.now(NY)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=now.weekday())
    parts = []
    for label, since in (("today", day.timestamp()), ("this week", week.timestamp()), ("all time", 0.0)):
        n, pnl, wins = _rows("SELECT COUNT(*), COALESCE(SUM(pnl), 0), COALESCE(SUM(pnl > 0), 0) FROM pair_trades "
                             "WHERE closed_at >= ?", (since,), path)[0]
        parts.append(f"{label} ${pnl:+,.2f} from {n} trades ({wins} wins)")
    return "Paper P&L: " + "; ".join(parts) + "."


def demo_results(path: str | None = None) -> str:
    by_grade = _rows(f"SELECT grade, COUNT(*), AVG(net_pnl > 0), SUM(net_pnl), AVG(net_pnl) FROM shadow_trades "
                     f"WHERE closed_at IS NOT NULL GROUP BY grade ORDER BY {GRADE_SQL}", path=path)
    if not by_grade:
        return "No demo trades have closed yet. The watcher opens them on STRONG, RISKY and AVOID signals."
    out = ["Demo results by grade: " + "; ".join(
        f"{g} {n} trades, {w:.0%} won, net ${s:+,.2f} (${a:+.2f} each)" for g, n, w, s, a in by_grade) + "."]
    hours = _rows("SELECT entry_hour, COUNT(*), AVG(net_pnl) FROM shadow_trades WHERE closed_at IS NOT NULL "
                  "GROUP BY entry_hour HAVING COUNT(*) >= 3 ORDER BY AVG(net_pnl) DESC", path=path)
    if hours:
        best, worst = hours[0], hours[-1]
        out.append(f"Best entry hour so far: {best[0]}:00 New York (${best[2]:+.2f} a trade over {best[1]}). "
                   f"Worst: {worst[0]}:00 (${worst[2]:+.2f} over {worst[1]}).")
    total = sum(r[1] for r in by_grade)
    if total < 100:
        out.append(f"Only {total} demo trades so far: too few to trust these differences yet.")
    return " ".join(out)


# ---------------------------------------------------------------------------
# Actions: write a flag, the bot acts on its next cycle
# ---------------------------------------------------------------------------
def _set_control(key: str, value: str, path: str | None = None) -> None:
    from db_logger import BUSY_TIMEOUT_MS

    conn = sqlite3.connect(path or db_path(), timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute("INSERT INTO control (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
                     "SET value=excluded.value, updated_at=excluded.updated_at", (key, value, time.time()))
        conn.commit()
    finally:
        conn.close()


def pause_trading(path: str | None = None) -> str:
    _set_control("paused", "jarvis", path)
    return "Paused. The bot opens no new trades; open ones still exit by their rules."


def resume_trading(path: str | None = None) -> str:
    _set_control("paused", "", path)
    return "Resumed. New trades are allowed again."


def emergency_stop(confirm: bool = False, path: str | None = None) -> str:
    if not confirm:
        return "Not done. This closes every position and halts the bot. Call again with confirm=true to do it."
    from db_logger import request_emergency_stop

    request_emergency_stop(path or db_path(), "jarvis")
    return "Emergency stop sent. The bot flattens everything and halts on its next cycle."


# ---------------------------------------------------------------------------
# MCP wiring (works with mcp 1.x FastMCP and 2.x MCPServer)
# ---------------------------------------------------------------------------
def build_server():
    try:
        from mcp.server.mcpserver import MCPServer as Server    # mcp >= 2
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server        # mcp 1.x

    server = Server("trading", instructions="Paper-trading pairs bot: status, market watch, trades and controls. "
                                            "Demo money only; numbers are measured history, not predictions.")

    @server.tool()
    def get_bot_status() -> str:
        """Is the trading bot running, paused or halted? Equity, today's P&L and open trades."""
        return bot_status()

    @server.tool()
    def get_market_watch(limit: int = 8) -> str:
        """The best trading opportunities right now (STRONG, RISKY, AVOID, WATCH) with their warnings."""
        return market_watch(limit)

    @server.tool()
    def get_market_brief() -> str:
        """How the market looks right now: calm or stressed, which sectors move, how many pairs still move
        together, what changed today, and recent alerts. Descriptive, not a forecast."""
        return market_brief()

    @server.tool()
    def explain(pair: str) -> str:
        """Explain one pair, e.g. KO/PEP: its grade, statistics, warnings and what the bot did with it lately."""
        return explain_pair(pair)

    @server.tool()
    def get_recent_trades(limit: int = 5) -> str:
        """The last closed trades, paper and demo."""
        return recent_trades(limit)

    @server.tool()
    def get_profit() -> str:
        """Paper profit and loss today, this week and all time."""
        return profit_summary()

    @server.tool()
    def get_demo_results() -> str:
        """Whether STRONG signals beat RISKY and AVOID ones in demo trading, and the best time of day."""
        return demo_results()

    @server.tool()
    def pause() -> str:
        """Stop the bot opening new trades. Open trades still exit normally."""
        return pause_trading()

    @server.tool()
    def resume() -> str:
        """Allow the bot to open new trades again."""
        return resume_trading()

    @server.tool()
    def stop_everything(confirm: bool = False) -> str:
        """EMERGENCY: close every position and halt the bot. Needs confirm=true."""
        return emergency_stop(confirm)

    return server


if __name__ == "__main__":
    build_server().run()
