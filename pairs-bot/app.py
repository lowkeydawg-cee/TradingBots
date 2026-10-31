"""Streamlit dashboard for the pairs bot.

    streamlit run app.py

Reads ``pairs_bot.db`` through a read-only SQLite connection (WAL mode lets it
read while the bot writes) and, in alpaca mode, the paper account itself.
The only thing it ever writes is the emergency-stop flag.
"""

from __future__ import annotations

import hmac
import os
import time
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import ConfigError, Settings, load_settings
from db_logger import connect_readonly, request_emergency_stop

st.set_page_config(page_title="Pairs Bot", page_icon="📈", layout="wide")


# ---------------------------------------------------------------------------
# Data access (all read-only)
# ---------------------------------------------------------------------------
@st.cache_resource
def settings() -> Settings:
    try:
        return load_settings()
    except ConfigError:
        return load_settings(mode="sim")  # dashboard still works without keys


def query(db_path: str, sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = connect_readonly(db_path)
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


def to_time(df: pd.DataFrame, col: str = "ts") -> pd.DataFrame:
    if not df.empty:
        df[col] = pd.to_datetime(df[col], unit="s", utc=True)
    return df


@st.cache_data(ttl=10)
def alpaca_snapshot(api_key: str, secret_key: str) -> dict:
    """Live paper-account view straight from Alpaca (cached 10 s to respect rate limits)."""
    from alpaca.trading.client import TradingClient

    tc = TradingClient(api_key, secret_key, paper=True)
    acct = tc.get_account()
    positions = [{"symbol": p.symbol, "qty": float(p.qty), "market_value": float(p.market_value),
                  "avg_entry": float(p.avg_entry_price), "unrealized_pl": float(p.unrealized_pl)}
                 for p in tc.get_all_positions()]
    return {"equity": float(acct.equity), "last_equity": float(acct.last_equity),
            "buying_power": float(acct.buying_power), "positions": positions}


def performance(trades: pd.DataFrame, slippage: float | None = None) -> dict[str, object]:
    closed = trades.dropna(subset=["closed_at"])
    closed = closed[~closed["exit_reason"].fillna("").str.startswith("reconciled")]
    wins = int((closed["pnl"] > 0).sum())
    return {
        "Total trades (closed)": len(closed),
        "Win rate": f"{wins / len(closed):.0%}" if len(closed) else "n/a",
        "Total P&L ($)": round(float(closed["pnl"].sum()), 2) if len(closed) else 0.0,
        "Avg P&L per trade ($)": round(float(closed["pnl"].mean()), 2) if len(closed) else 0.0,
        "Best / worst ($)": (f"{closed['pnl'].max():.2f} / {closed['pnl'].min():.2f}"
                             if len(closed) else "n/a"),
        "Active positions": int(trades["closed_at"].isna().sum()),
        "Avg cost per order (bps)": f"{slippage:.2f}" if slippage is not None and pd.notna(slippage) else "n/a",
    }


def per_pair(trades: pd.DataFrame) -> pd.DataFrame:
    """One row per pair: trades, win rate, P&L. Old rows without a pair count as the first pair."""
    closed = trades.dropna(subset=["closed_at"]).copy()
    closed = closed[~closed["exit_reason"].fillna("").str.startswith("reconciled")]
    if closed.empty:
        return pd.DataFrame(columns=["pair", "trades", "win rate", "P&L ($)", "avg ($)"])
    closed["pair"] = closed["pair"].fillna(PAIRS[0])
    g = closed.groupby("pair")["pnl"]
    return pd.DataFrame({"trades": g.size(), "win rate": g.apply(lambda x: f"{(x > 0).mean():.0%}"),
                         "P&L ($)": g.sum().round(2), "avg ($)": g.mean().round(2)}).reset_index()


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
cfg = settings()
PAIRS = [f"{a}/{b}" for a, b in cfg.all_pairs]

# Optional password (set DASHBOARD_PASSWORD in .env). Recommended whenever the
# dashboard is reachable from other devices, e.g. your phone over Tailscale.
_password = os.environ.get("DASHBOARD_PASSWORD", "")
if _password and not st.session_state.get("authed"):
    st.title("Pairs bot")
    entered = st.text_input("Password", type="password")
    if entered and hmac.compare_digest(entered.encode(), _password.encode()):
        st.session_state["authed"] = True
        st.rerun()
    elif entered:
        st.error("Wrong password")
    st.stop()
st.sidebar.header("Settings")
pair = st.sidebar.selectbox("Pair (charts)", PAIRS)
db_path = st.sidebar.text_input("Database", cfg.db_path)
refresh = st.sidebar.slider("Refresh every (s)", 2, 60, 5)
window = st.sidebar.slider("Chart window (bars)", 60, 2_000, 400, step=20)
st.sidebar.caption(f"{len(PAIRS)} pair(s) · mode **{cfg.mode}** · hedge **{cfg.hedge_method}** · "
                   f"orders **{cfg.order_type}** · entry ±{cfg.entry_z} · exit ±{cfg.exit_z} · stop ±{cfg.stop_z}")

st.title(f"Pairs bot · {pair}" if len(PAIRS) == 1 else f"Pairs bot · {len(PAIRS)} pairs · charts for {pair}")

if not Path(db_path).exists():
    st.info(f"No database at `{db_path}` yet. Start the bot with `python main.py`.")
    st.stop()


@st.fragment(run_every=refresh)
def live_panel() -> None:
    control = query(db_path, "SELECT key, value, updated_at FROM control")
    ctl = dict(zip(control["key"], control["value"])) if not control.empty else {}
    heartbeat = float(ctl.get("heartbeat") or 0)
    halted = ctl.get("halted") or ""
    eq = to_time(query(db_path, "SELECT ts, equity, last_equity, daily_pnl, gross_exposure FROM equity "
                                "ORDER BY id DESC LIMIT ?", (window,)).iloc[::-1])
    # Rows written before multi-pair support have no pair: they belong to the first pair.
    pair_sql = "(pair = ? OR (pair IS NULL AND ? = ?))"
    pair_args = (pair, pair, PAIRS[0])
    sig = to_time(query(db_path, "SELECT ts, bar_time, zscore, beta, coint_pvalue, half_life, vol_ratio, position, "
                                 f"action, note FROM signals WHERE {pair_sql} ORDER BY id DESC LIMIT ?",
                        (*pair_args, window)).iloc[::-1])
    trades = query(db_path, "SELECT * FROM pair_trades ORDER BY id")
    slippage = query(db_path, "SELECT AVG(slippage_bps) AS s FROM orders WHERE slippage_bps IS NOT NULL")["s"].iloc[0]
    retired = {k.split(":", 1)[1]: v for k, v in ctl.items() if k.startswith("retired:") and v}

    # --- status strip ----------------------------------------------------
    if halted:
        st.error(f"HALTED: {halted}. Review, then restart the bot with `--reset-halt`.")
    elif cfg.mode == "alpaca" and heartbeat and time.time() - heartbeat > 3 * cfg.poll_seconds:
        st.warning(f"No heartbeat for {time.time() - heartbeat:.0f}s. Is `main.py` running?")
    else:
        st.success("Running")
    if ctl.get("paused") and not halted:
        st.info(f"PAUSED by {ctl['paused']}: no new trades. Send /resume in Telegram to allow them again.")
    for name, why in retired.items():
        st.warning(f"{name} is retired ({why}). It won't open new trades until you restart with `--reset-halt`.")

    c = st.columns(5)
    if not eq.empty:
        last = eq.iloc[-1]
        c[0].metric("Equity", f"${last.equity:,.2f}")
        c[1].metric("Today's P&L", f"${last.daily_pnl:,.2f}",
                    f"{last.daily_pnl / last.last_equity:.2%}" if last.last_equity else None)
        c[2].metric("Gross exposure", f"${last.gross_exposure:,.0f}")
    if not sig.empty:
        s = sig.iloc[-1]
        c[3].metric("Z-score", f"{s.zscore:+.2f}" if pd.notna(s.zscore) else "n/a")
        c[4].metric("Coint. p-value", f"{s.coint_pvalue:.3f}" if pd.notna(s.coint_pvalue) else "n/a",
                    f"half-life {s.half_life:.0f} bars" if pd.notna(s.half_life) else None,
                    delta_color="off")

    left, right = st.columns(2)

    # --- equity curve ----------------------------------------------------
    with left:
        st.subheader("Equity curve")
        if eq.empty:
            st.caption("No equity snapshots yet.")
        else:
            fig = go.Figure(go.Scatter(x=eq.ts, y=eq.equity, mode="lines", name="Equity"))
            fig.add_hline(y=float(eq.last_equity.iloc[-1]), line_dash="dot",
                          annotation_text="previous close", line_color="gray")
            fig.update_layout(height=340, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="$")
            st.plotly_chart(fig, width="stretch")

    # --- z-score with thresholds and trade markers -------------------------
    with right:
        st.subheader("Spread z-score")
        if sig.empty:
            st.caption("No signals yet.")
        else:
            fig = go.Figure(go.Scatter(x=sig.ts, y=sig.zscore, mode="lines", name="z"))
            for level, name, dash in ((cfg.entry_z, "entry", "dash"), (cfg.exit_z, "exit", "dot"),
                                      (cfg.stop_z, "stop", "dashdot")):
                for sgn in (1, -1):
                    fig.add_hline(y=sgn * level, line_dash=dash, line_width=1,
                                  annotation_text=f"{name} {sgn * level:+.1f}", annotation_position="right")
            for act, symbol, color in (("OPEN_LONG", "triangle-up", "green"),
                                       ("OPEN_SHORT", "triangle-down", "red"),
                                       ("CLOSE", "x", "black")):
                pts = sig[sig.action == act]
                if not pts.empty:
                    fig.add_trace(go.Scatter(x=pts.ts, y=pts.zscore, mode="markers", name=act,
                                             marker=dict(symbol=symbol, size=11, color=color)))
            fig.update_layout(height=340, margin=dict(l=10, r=60, t=30, b=10), yaxis_title="σ",
                              legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0))
            st.plotly_chart(fig, width="stretch")

    # --- performance -----------------------------------------------------
    st.subheader("Performance")
    perf = performance(trades, slippage) if not trades.empty else performance(
        pd.DataFrame(columns=["closed_at", "pnl", "exit_reason"]), slippage)
    st.table(pd.DataFrame({"Metric": list(perf), "Value": [str(v) for v in perf.values()]}))
    if len(PAIRS) > 1 and not trades.empty:
        st.caption("By pair")
        st.dataframe(per_pair(trades), width="stretch", hide_index=True)

    if cfg.mode == "alpaca":
        try:
            snap = alpaca_snapshot(cfg.api_key, cfg.secret_key)
            st.caption(f"Alpaca paper account: equity ${snap['equity']:,.2f}, "
                       f"buying power ${snap['buying_power']:,.2f}")
            st.dataframe(pd.DataFrame(snap["positions"]), width="stretch", hide_index=True)
        except Exception as exc:
            st.warning(f"Could not reach Alpaca: {exc}")

    with st.expander("Recent pair trades", expanded=False):
        if not trades.empty:
            show = trades.sort_values("id", ascending=False).head(50).copy()
            for col in ("opened_at", "closed_at"):
                show[col] = pd.to_datetime(show[col], unit="s", utc=True)
            st.dataframe(show, width="stretch", hide_index=True)
    with st.expander("Circuit-breaker events", expanded=bool(halted)):
        ev = to_time(query(db_path, "SELECT ts, kind, detail FROM circuit_events ORDER BY id DESC LIMIT 100"))
        st.dataframe(ev, width="stretch", hide_index=True)


live_panel()


# ---------------------------------------------------------------------------
# Market watch (filled by watcher.py): grades for every pair + demo-money results
# ---------------------------------------------------------------------------
GRADE_ORDER = {"STRONG": 0, "RISKY": 1, "AVOID": 2, "WATCH": 3, "QUIET": 4}


def shadow_stats(shadow: pd.DataFrame, by: str) -> pd.DataFrame:
    closed = shadow.dropna(subset=["closed_at"])
    if closed.empty:
        return pd.DataFrame()
    g = closed.groupby(by)["net_pnl"]
    return pd.DataFrame({"trades": g.size(), "win rate": g.apply(lambda x: f"{(x > 0).mean():.0%}"),
                         "net P&L ($)": g.sum().round(2), "avg ($)": g.mean().round(2)}).reset_index()


@st.fragment(run_every=max(refresh, 30))
def watch_panel() -> None:
    try:
        latest = query(db_path, "SELECT o.* FROM opportunities o JOIN (SELECT pair, MAX(id) AS id FROM opportunities "
                                "GROUP BY pair) m ON o.id = m.id")
        shadow = query(db_path, "SELECT * FROM shadow_trades ORDER BY id")
    except Exception:
        return                                       # watcher never ran: no tables yet
    if latest.empty:
        return
    st.divider()
    st.subheader("🔭 Market watch")
    st.caption("Every pair in every sector, graded on its latest bar by `watcher.py`. The win rate is how this "
               "rule set did on the pair recently, after costs, not a prediction. Every STRONG, RISKY and AVOID signal "
               "is traded with demo money below (AVOID is the control group), so you can see whether the grades hold up.")
    try:
        notes = query(db_path, "SELECT ts, kind, text FROM market_notes WHERE id IN (SELECT MAX(id) FROM market_notes "
                               "WHERE kind = 'brief') OR (kind = 'alert' AND ts >= (SELECT MAX(ts) FROM market_notes) - 86400) "
                               "ORDER BY id DESC LIMIT 7")
    except Exception:
        notes = pd.DataFrame()
    if not notes.empty:
        brief = notes[notes["kind"] == "brief"]
        if not brief.empty:
            st.info(brief.iloc[0]["text"])
        for _, alert in notes[notes["kind"] == "alert"].iterrows():
            when = pd.to_datetime(alert["ts"], unit="s", utc=True).tz_convert("America/New_York").strftime("%a %H:%M")
            st.warning(f"{when} NY: {alert['text']}")
    counts = latest["grade"].value_counts()
    cols = st.columns(len(GRADE_ORDER))
    for col, grade in zip(cols, GRADE_ORDER):
        col.metric(grade.title(), int(counts.get(grade, 0)))
    board = latest.assign(order=latest["grade"].map(GRADE_ORDER)).sort_values(["order", "prob"], ascending=[True, False])
    board["updated"] = pd.to_datetime(board["bar_time"], unit="s", utc=True)
    show = board[["grade", "pair", "sector", "side", "zscore", "prob", "hist_trades", "coint_pvalue", "half_life",
                  "flags", "updated"]].rename(columns={"prob": "recent win rate", "hist_trades": "past trades"})
    st.dataframe(show.round({"zscore": 2, "recent win rate": 2, "coint_pvalue": 3, "half_life": 1}),
                 width="stretch", hide_index=True)

    st.markdown("**Demo-money results by grade**: do STRONG signals really beat RISKY ones?")
    by_grade = shadow_stats(shadow, "grade")
    if by_grade.empty:
        st.caption("No shadow trades closed yet. They appear after the first signals revert or stop out.")
        return
    left, right = st.columns(2)
    left.dataframe(by_grade, width="stretch", hide_index=True)
    by_hour = shadow_stats(shadow, "entry_hour")
    if not by_hour.empty:
        fig = go.Figure(go.Bar(x=by_hour["entry_hour"], y=by_hour["avg ($)"],
                               text=by_hour["trades"].astype(str) + " trades", textposition="outside"))
        fig.update_layout(height=260, margin=dict(l=10, r=10, t=30, b=10), title="Avg demo P&L by entry hour (New York)",
                          xaxis_title="hour", yaxis_title="$ per trade")
        right.plotly_chart(fig, width="stretch")
    with st.expander("By pair"):
        st.dataframe(shadow_stats(shadow, "pair").sort_values("net P&L ($)", ascending=False),
                     width="stretch", hide_index=True)
    with st.expander("Open demo trades"):
        st.dataframe(shadow[shadow["closed_at"].isna()], width="stretch", hide_index=True)


watch_panel()

# ---------------------------------------------------------------------------
# Emergency stop (outside the auto-refreshing fragment so it is not re-rendered mid-click)
# ---------------------------------------------------------------------------
st.divider()
st.subheader("🛑 Emergency stop")
st.caption("Sets a stop flag the bot reads every cycle: it closes both legs and halts. "
           + ("In alpaca mode this also closes every position on the paper account directly, "
              "so it works even if the bot is not running." if cfg.mode == "alpaca" else ""))
confirm = st.checkbox("I want to flatten all positions and halt the bot")
if st.button("EMERGENCY STOP", type="primary", disabled=not confirm):
    request_emergency_stop(db_path, "dashboard button")
    msg = "Stop flag written."
    if cfg.mode == "alpaca":
        try:
            from alpaca.trading.client import TradingClient

            TradingClient(cfg.api_key, cfg.secret_key, paper=True).close_all_positions(cancel_orders=True)
            msg += " Close-all sent to Alpaca."
        except Exception as exc:
            msg += f" Direct close-all FAILED ({exc}); close positions in the Alpaca web app."
    st.success(msg)
