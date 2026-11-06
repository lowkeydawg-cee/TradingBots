"""Approaches: every demo strategy side by side, with the analysis behind the verdict.

Opens from the dashboard's sidebar (streamlit run app.py). Read-only.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from approaches import (COMPARE, COMPARE_COLUMNS, MIN_TRADES, RISK_RULES, by_market, equity_curves, holdings,
                        latest_brief, latest_decisions, load_sleeves, load_trades, markets_watched, money,
                        recent_actions, scoreboard, verdict)
from config import ConfigError, load_settings

st.set_page_config(page_title="Approaches", page_icon="🧭", layout="wide")

_password = os.environ.get("DASHBOARD_PASSWORD", "")
if _password and not st.session_state.get("authed"):
    st.title("Approaches")
    entered = st.text_input("Password", type="password")
    if entered and hmac.compare_digest(entered.encode(), _password.encode()):
        st.session_state["authed"] = True
        st.rerun()
    elif entered:
        st.error("Wrong password")
    st.stop()


@st.cache_resource
def bot_db() -> str:
    try:
        return load_settings().db_path
    except ConfigError:
        return load_settings(mode="sim").db_path


def md(text: str) -> str:
    return text.replace("$", "\\$")           # a bare $ starts a formula in Streamlit markdown


HERE = Path(__file__).resolve().parents[1]
SOURCES = {"Live demo (the watcher)": bot_db(), "Replay (past prices)": os.environ.get("PAIRS_REPLAY_DB", str(HERE / "replay.db"))}
available = {k: v for k, v in SOURCES.items() if Path(v).exists()}

st.title("🧭 Approaches")
st.caption("Every strategy the bot tries with demo money, scored the same way after costs. "
           "Use it to decide which approach is worth keeping. No real orders are placed by any of these.")

# ---------------------------------------------------------------------------
# Right now: what it holds, in which market, and what it sees. The first thing on the page.
# ---------------------------------------------------------------------------
st.subheader("Right now")
live_db = SOURCES["Live demo (the watcher)"]
if not Path(live_db).exists():
    st.info("Nothing is running yet. Double-click start-trading.bat in the pairs-bot folder to start the demo.")
else:
    held = holdings(live_db)
    watched = markets_watched(live_db)
    next_bar = (pd.Timestamp.now(tz="UTC").floor("4h") + pd.Timedelta(hours=4)).tz_convert(None)
    local = pd.Timestamp.now().floor("s") + (next_bar - pd.Timestamp.now(tz="UTC").tz_convert(None))
    st.caption(f"Watching: {', '.join(watched) or 'nothing yet'}. Demo money only. "
               f"Next buy-or-sell decision at about {local:%H:%M} your time.")
    if held.empty:
        st.markdown("**Holding nothing right now.** The reasons are under Why it did (or didn't) trade, below.")
    else:
        value, profit = held["Value"].sum(), held["Profit if sold now"].sum()
        for col, (label, shown) in zip(st.columns(3), (("Open positions", str(len(held))), ("Worth now", f"${value:,.0f}"),
                                                       ("Profit if sold now", money(profit, 0)))):
            col.metric(label, shown)
        st.dataframe(held.assign(Since=held["Since"].dt.strftime("%d %b %H:%M UTC")), hide_index=True, width="stretch",
                     column_config={c: st.column_config.NumberColumn(format="$%.2f")
                                    for c in ("Bought at", "Price now", "Value", "Profit if sold now")})
    brief = latest_brief(live_db)
    if brief:
        with st.container(border=True):
            st.markdown("**What the market looks like**")
            for line in brief:
                st.markdown(f"- {md(line.lstrip('- '))}")

# ---------------------------------------------------------------------------
# How they differ: always there, folded away so the live picture comes first
# ---------------------------------------------------------------------------
with st.expander("How the approaches differ"):
    st.caption("Pairs bets on two prices coming back together. The other four only buy, so they can trade crypto and "
               "gold for real. Hold does nothing clever: it's the yardstick the others have to beat.")
    st.table(pd.DataFrame(COMPARE, columns=COMPARE_COLUMNS).set_index("Approach"))
    st.caption("Every approach pays the same trading costs (about 0.3% per crypto order), so one that trades a lot "
               "needs bigger wins to come out ahead. The buy-only ones decide once every four hours, on closed candles.")
with st.expander("Risk rules the buy-only approaches run under"):
    for rule in RISK_RULES:
        st.markdown(f"- {md(rule)}")

st.divider()
st.subheader("How they've done")
if not available:
    st.info("No results yet. Start the watcher (`python watcher.py --source alpaca --universe crypto`) and leave it "
            "running, or replay the last week with `python timing.py --replay`.")
    st.stop()

source = st.radio("Results from", list(available), horizontal=True, label_visibility="collapsed")
path = available[source]

if source.startswith("Live"):
    try:
        from mcp_server import freshness
        warn = freshness(path)
    except Exception:
        warn = ""
    if warn:
        st.warning(warn)

trades = load_trades(path)
board = scoreboard(trades)
head, reasons = verdict(board)

# ---------------------------------------------------------------------------
# The answer first
# ---------------------------------------------------------------------------
with st.container(border=True):
    st.markdown(f"### {md(head)}")
    for line in reasons:
        st.markdown(f"- {md(line)}")

STATE_ICON = {"normal": "🟢", "caution": "🟡", "defense": "🟠", "stopped": "🔴"}
sleeves = load_sleeves(path)
if not sleeves.empty:
    st.markdown("**Risk state of each buy-only account**")
    for col, (_, r) in zip(st.columns(len(sleeves)), sleeves.iterrows()):
        with col.container(border=True):
            st.metric(f"{STATE_ICON.get(r['state'], '')} {r['strategy'].title()}", f"${r['value']:,.0f}")
            st.caption(f"{r['state']} · {r['drawdown']:.1%} below its high")


def why_nothing() -> None:
    """The last decision for every approach and market, so a quiet day explains itself."""
    last = latest_decisions(path)
    if last.empty:
        st.caption("No buy-only decisions yet. They're made once every four hours, when a candle closes.")
        return
    when = pd.to_datetime(last["ts"].max(), unit="s", utc=True)
    st.caption(f"What each approach decided on the last closed 4-hour candle ({when:%d %b %H:%M} UTC), and why.")
    st.dataframe(last.drop(columns="ts").rename(columns={"strategy": "Approach", "symbol": "Market", "action": "Did",
                                                         "reason": "Why", "state": "Risk state"}),
                 hide_index=True, width="stretch")
    acts = recent_actions(path)
    if not acts.empty:
        st.markdown("**Latest buys, sells and refused trades**")
        acts = acts.assign(ts=pd.to_datetime(acts["ts"], unit="s", utc=True)).round({"price": 2, "qty": 6})
        st.dataframe(acts.rename(columns={"ts": "When", "strategy": "Approach", "symbol": "Market", "action": "Did",
                                          "reason": "Why", "price": "Price", "qty": "Qty", "state": "Risk state"}),
                     hide_index=True, width="stretch")


if board.empty:
    st.subheader("Why it hasn't traded yet")
    why_nothing()
    st.stop()

# ---------------------------------------------------------------------------
# Scoreboard
# ---------------------------------------------------------------------------
st.markdown("**Scoreboard**")
cards = board.head(6)
for col, (_, r) in zip(st.columns(len(cards)), cards.iterrows()):
    with col.container(border=True):
        st.metric(r["approach"], money(r["total"], 0))
        note = f"{int(r['trades'])} trades, {r['win rate']:.0%} won" if r["trades"] else "no closed trades yet"
        if r["trades"] and r["trades"] < MIN_TRADES:
            note += " · too few to judge"
        if r.get("open now"):
            note += f" · {int(r['open now'])} open"
            if r["open P&L"]:
                note += f" ({money(r['open P&L'], 0)} now)"
        st.caption(note)

table = board.assign(**{"win rate": board["win rate"] * 100}).rename(columns={"approach": "Approach", "trades": "Trades", "win rate": "Won", "net": "Net $",
                              "per trade": "$ per trade", "avg win": "Avg win $", "avg loss": "Avg loss $",
                              "profit factor": "Profit factor", "worst drawdown": "Worst drawdown $",
                              "costs": "Costs paid $", "open now": "Open now",
                              "open P&L": "Open P&L $", "total": "Total $"})
first = ["Approach", "Total $", "Net $", "Open P&L $", "Trades", "Won"]
table = table[first + [c for c in table.columns if c not in first]]
st.dataframe(table, hide_index=True, width="stretch", column_config={
    "Won": st.column_config.NumberColumn(format="%.0f%%"),
    **{c: st.column_config.NumberColumn(format="%.2f") for c in
       ("Total $", "Open P&L $", "Net $", "$ per trade", "Avg win $", "Avg loss $", "Profit factor", "Worst drawdown $", "Costs paid $")},
})
st.caption("Total = closed trades (Net) plus what open positions are worth if sold now (Open P&L, after costs). "
           "Profit factor is money won ÷ money lost: above 1 makes money. Worst drawdown is the deepest fall "
           "from a high point, the pain you'd have sat through.")

curves = equity_curves(trades)
if not curves.empty:
    fig = go.Figure()
    for name, g in curves.groupby("approach"):
        fig.add_trace(go.Scatter(x=g["when"], y=g["cum"], mode="lines", name=name, line_shape="hv"))
    fig.add_hline(y=0, line_width=1, line_color="gray")
    fig.update_layout(height=360, margin=dict(l=10, r=10, t=40, b=10), title="Demo money over time (net $, after costs)",
                      yaxis_title="$", legend=dict(orientation="h", y=-0.15))
    st.plotly_chart(fig, width="stretch")

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
st.subheader("Analysis")
t_why, t_valid, t_market, t_when, t_pairs, t_replay, t_log = st.tabs(
    ["Why it did (or didn't) trade", "Validation", "By market", "When to trade", "Pairs detail", "Replay review",
     "Trade log"])

with t_why:
    why_nothing()

with t_valid:
    valid_md = HERE / "review" / "validation.md"
    if valid_md.exists():
        made = pd.Timestamp(valid_md.stat().st_mtime, unit="s")
        st.caption(f"From the last `python longonly.py --validate` run ({made:%d %b %H:%M}).")
        st.markdown(md(valid_md.read_text(encoding="utf-8")))
    else:
        st.caption("Run `python longonly.py --validate --source alpaca --days 365` to test the buy-only approaches on a "
                   "year of real prices: split by time, stress-tested, with pass/fail gates.")

with t_market:
    m = by_market(trades)
    if m.empty:
        st.caption("No closed trades yet.")
    else:
        st.caption("Net $ per approach in each market. Green made money, red lost it.")
        shown = m.map(lambda v: "–" if pd.isna(v) else money(v))
        st.dataframe(shown.style.apply(lambda col: [
            "" if pd.isna(v) else f"color: {'#1a7f37' if v > 0 else '#cf222e'}" for v in m[col.name]]), width="stretch")

with t_when:
    try:
        from timing import timing_report
        st.text(timing_report(path, days=7 if source.startswith("Live") else 365))
    except Exception as exc:
        st.caption(f"No timing report: {exc}")

with t_pairs:
    try:
        from review import journal
        from review import verdict as pairs_verdict
        j = journal(path)
        lines = pairs_verdict(j, None) if not j.empty else ["No pairs demo trades yet."]
    except Exception as exc:
        lines = [f"No pairs detail: {exc}"]
    for line in lines:
        st.markdown(f"- {md(line)}")

with t_replay:
    review_md = HERE / "review" / "review.md"
    if review_md.exists():
        made = pd.Timestamp(review_md.stat().st_mtime, unit="s")
        st.caption(f"From the last `python timing.py --replay` run ({made:%d %b %H:%M}).")
        st.markdown(md(review_md.read_text(encoding="utf-8")))
    else:
        st.caption("Run `python timing.py --replay` to replay the last week and compare other rule settings here.")

with t_log:
    left, right = st.columns(2)
    pick = left.multiselect("Approach", sorted(trades["approach"].unique()))
    result = right.multiselect("Result", ["won", "lost", "open"])
    log = trades
    if pick:
        log = log[log["approach"].isin(pick)]
    if result:
        log = log[log["result"].isin(result)]
    log = log.assign(opened=pd.to_datetime(log["opened_at"], unit="s", utc=True),
                     closed=pd.to_datetime(log["closed_at"], unit="s", utc=True))
    st.dataframe(log[["approach", "market", "what", "opened", "closed", "result", "net_pnl", "costs", "note"]]
                 .sort_values("opened", ascending=False).round({"net_pnl": 2, "costs": 2})
                 .rename(columns={"net_pnl": "net $", "costs": "costs $"}),
                 hide_index=True, width="stretch")
