"""Backtester: replay historical bars through the bot's own decision rules, with costs.

    python backtest.py --source sim --bars 20000                       # offline demo
    python backtest.py --source alpaca --start 2026-04-01 --end 2026-10-01   # real history (keys in .env)
    python backtest.py --source csv --csv data/my_bars.csv             # your own data

What it answers
---------------
"If the bot had run over this period, would it have made money AFTER costs?"
It reports gross profit (before costs), every cost, net profit, Sharpe ratio,
maximum drawdown, and the **break-even cost**: the largest per-order cost
(in basis points) at which the strategy still breaks even. If your real
trading costs are above that number, the strategy has no edge.

Fidelity to the live bot
------------------------
* Same code: ``compute_spread_stats`` for the model, ``decide`` for the signal
  and ``plan_pair`` for sizing are imported from the live modules, not copied.
* No look-ahead: the decision at bar t sees only bars up to and including t.
* Realistic timing: by default an order decided on bar t fills at bar t+1's
  close (``--fill next``). Live, the bot sends a market order after the bar
  closes, so the price it gets is a later one.
* Market hours: decisions are made only during the regular session
  (9:30-16:00 New York), as live, where the bot skips cycles while the market
  is closed. Extended-hours bars still feed the model window, as they do live.
* The cointegration test is slow (~0.1 s), so it runs only on the bars where
  ``decide`` reads it (flat and |z| between the entry and stop bands). The
  decisions are identical to running it on every bar.
* Daily loss limit: live, the bot halts for good and waits for a human. Here
  it flattens and stops trading for the rest of that day, and the report
  counts how many times a live bot would have halted.

Costs modelled (per order)
--------------------------
* ``spread_bps``: half the bid/ask spread plus slippage, as a fraction of the
  order's value. Alpaca charges no commission, so this is the main cost.
* SEC fee on sells (``sec_fee_rate`` x value) and FINRA TAF on sells
  (per share, capped per order).
* Borrow fee on the short leg: annual rate x value x time held.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from config import Settings, load_settings
from execution import pair_pnl, plan_pair
from strategy import Action, Position, coint_pvalue, compute_spread_stats, decide

NY = "America/New_York"
SESSION_OPEN, SESSION_CLOSE = dtime(9, 30), dtime(16, 0)


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Costs:
    spread_bps: float = 2.0             # half-spread + slippage per order, in basis points (0.01%)
    sec_fee_rate: float = 27.80e-6      # SEC Section 31 fee on sell value ($27.80 per $1M)
    taf_per_share: float = 0.000166     # FINRA trading activity fee on shares sold
    taf_max: float = 8.30               # TAF cap per order
    borrow_rate_annual: float = 0.005   # 0.5%/yr: typical for easy-to-borrow large caps

    def order_cost(self, qty: float, price: float, side: str) -> tuple[float, float]:
        """Return (spread_and_slippage, regulatory_fees) for one order."""
        value = qty * price
        spread = value * self.spread_bps / 1e4
        fees = value * self.sec_fee_rate + min(qty * self.taf_per_share, self.taf_max) if side == "sell" else 0.0
        return spread, fees

    def borrow_cost(self, short_value: float, seconds_held: float) -> float:
        return short_value * self.borrow_rate_annual * seconds_held / (365 * 86_400)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Trade:
    direction: str
    opened_at: pd.Timestamp
    qty_a: int
    qty_b: int
    entry_a: float
    entry_b: float
    entry_z: float
    spread_cost: float = 0.0
    fees: float = 0.0
    closed_at: pd.Timestamp | None = None
    exit_a: float = math.nan
    exit_b: float = math.nan
    exit_z: float = math.nan
    bars_held: int = 0
    borrow: float = 0.0
    gross_pnl: float = 0.0
    exit_reason: str = ""

    @property
    def costs(self) -> float:
        return self.spread_cost + self.fees + self.borrow

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.costs

    @property
    def turnover(self) -> float:
        """Total value of the four orders (two to open, two to close)."""
        return (self.qty_a * (self.entry_a + self.exit_a) + self.qty_b * (self.entry_b + self.exit_b))


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity: pd.Series                   # mark-to-market equity per bar
    zscore: pd.Series                   # z per decision bar
    blocked: Counter = field(default_factory=Counter)
    loss_limit_days: list[str] = field(default_factory=list)
    starting_equity: float = 100_000.0
    costs: Costs = field(default_factory=Costs)

    def trades_frame(self) -> pd.DataFrame:
        rows = []
        for t in self.trades:
            d = {f.name: getattr(t, f.name) for f in dataclasses.fields(t)}
            d.update(costs=t.costs, net_pnl=t.net_pnl, turnover=t.turnover)
            rows.append(d)
        return pd.DataFrame(rows)

    def summary(self) -> dict[str, object]:
        closed = [t for t in self.trades if t.closed_at is not None]
        eq = self.equity
        gross = sum(t.gross_pnl for t in closed)
        spread = sum(t.spread_cost for t in closed)
        fees = sum(t.fees for t in closed)
        borrow = sum(t.borrow for t in closed)
        net = gross - spread - fees - borrow
        turnover = sum(t.turnover for t in closed)
        wins = [t.net_pnl for t in closed if t.net_pnl > 0]
        losses = [t.net_pnl for t in closed if t.net_pnl <= 0]

        day_close = eq.groupby(eq.index.tz_convert(NY).date).last() if len(eq) else pd.Series(dtype=float)
        daily = pd.concat([pd.Series([self.starting_equity]), day_close.reset_index(drop=True)]).diff().dropna()
        sharpe = float(daily.mean() / daily.std() * math.sqrt(252)) if len(daily) > 1 and daily.std() > 0 else math.nan
        dd = (eq - eq.cummax()) if len(eq) else pd.Series([0.0])
        # Break-even: the spread_bps at which net P&L would be exactly zero.
        breakeven = (gross - fees - borrow) / turnover * 1e4 if turnover else math.nan
        sweep = {f"{bps:g} bps": round(float(gross - fees - borrow - turnover * bps / 1e4), 2)
                 for bps in (0, 1, 2, 3, 5, 10)}

        n = len(closed)
        if n == 0:
            verdict = "No trades. Nothing to judge: try a longer period or another pair."
        elif net <= 0:
            verdict = "NO EDGE: loses money after costs. Do not trade this setup."
        elif n < 30:
            verdict = "Profitable, but fewer than 30 trades: too few to tell skill from luck."
        elif not math.isnan(sharpe) and sharpe < 1:
            verdict = "Profitable after costs, but Sharpe < 1: weak edge, test on more data and other periods."
        else:
            verdict = "Profitable after costs with Sharpe >= 1. Next: confirm on a different period (out of sample)."

        return {
            "period": f"{eq.index[0]:%Y-%m-%d %H:%M} -> {eq.index[-1]:%Y-%m-%d %H:%M} UTC" if len(eq) else "n/a",
            "bars": int(len(eq)),
            "trading_days": int(len(day_close)),
            "trades": n,
            "win_rate": round(len(wins) / n, 3) if n else None,
            "gross_pnl": round(gross, 2),
            "spread_and_slippage": round(spread, 2),
            "regulatory_fees": round(fees, 2),
            "borrow_fees": round(borrow, 2),
            "net_pnl": round(net, 2),
            "avg_gross_per_trade": round(gross / n, 2) if n else None,
            "avg_cost_per_trade": round((spread + fees + borrow) / n, 2) if n else None,
            "avg_net_per_trade": round(net / n, 2) if n else None,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "sharpe_annualised": round(sharpe, 2) if not math.isnan(sharpe) else None,
            "max_drawdown": round(float(dd.min()), 2),
            "breakeven_cost_bps": round(breakeven, 2) if not math.isnan(breakeven) else None,
            "assumed_cost_bps": self.costs.spread_bps,
            "net_pnl_by_cost": sweep,
            "avg_bars_held": round(float(np.mean([t.bars_held for t in closed])), 1) if n else None,
            "exit_reasons": dict(Counter(t.exit_reason.split(":")[0] for t in closed)),
            "blocked_entries": dict(self.blocked),
            "daily_loss_limit_hits": len(self.loss_limit_days),
            "verdict": verdict,
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
def regular_session(index: pd.DatetimeIndex) -> np.ndarray:
    """True for bars inside 9:30-16:00 New York time on weekdays."""
    local = index.tz_convert(NY)
    t = np.array([ts.time() for ts in local])
    return (local.weekday < 5) & (t >= SESSION_OPEN) & (t < SESSION_CLOSE)


def run_backtest(closes: pd.DataFrame, cfg: Settings, costs: Costs = Costs(), *, fill: str = "next",
                 starting_equity: float = 100_000.0, all_hours: bool = False) -> BacktestResult:
    """Walk forward bar by bar. ``closes``: UTC DatetimeIndex, one column per symbol, oldest first."""
    if fill not in ("next", "close"):
        raise ValueError("fill must be 'next' or 'close'")
    a_sym, b_sym = cfg.symbol_a, cfg.symbol_b
    closes = closes[[a_sym, b_sym]].dropna()
    if closes.index.tz is None:
        closes.index = closes.index.tz_localize(timezone.utc)
    if len(closes) <= cfg.hedge_lookback:
        raise ValueError(f"need more than {cfg.hedge_lookback} bars, got {len(closes)}")

    idx = closes.index
    pa, pb = closes[a_sym].to_numpy(float), closes[b_sym].to_numpy(float)
    in_session = np.ones(len(idx), bool) if all_hours else regular_session(idx)
    day = idx.tz_convert(NY).date

    position, bars_held = Position.FLAT, 0
    trade: Trade | None = None
    trades: list[Trade] = []
    realised = 0.0
    pending: tuple[str, object] | None = None        # ("open", plan, z) or ("close", reason, z)
    blocked: Counter = Counter()
    loss_days: list[str] = []
    eq_vals, eq_idx, z_vals, z_idx = [], [], [], []
    cur_day, day_start_eq, day_stopped = None, starting_equity, False

    def open_trade(plan, z: float, i: int) -> None:
        nonlocal position, bars_held, trade, realised
        t = Trade(str(plan.direction), idx[i], plan.qty_a, plan.qty_b, pa[i], pb[i], z)
        for qty, px, side in ((plan.qty_a, pa[i], plan.side_a), (plan.qty_b, pb[i], plan.side_b)):
            s, f = costs.order_cost(qty, px, side)
            t.spread_cost += s
            t.fees += f
        realised -= t.spread_cost + t.fees
        trade, position, bars_held = t, plan.direction, 0

    def close_trade(reason: str, z: float, i: int) -> None:
        nonlocal position, bars_held, trade, realised
        t = trade
        long_a = t.direction == Position.LONG_SPREAD
        side_a, side_b = ("sell", "buy") if long_a else ("buy", "sell")
        before = t.spread_cost + t.fees
        for qty, px, side in ((t.qty_a, pa[i], side_a), (t.qty_b, pb[i], side_b)):
            s, f = costs.order_cost(qty, px, side)
            t.spread_cost += s
            t.fees += f
        short_value = t.qty_b * t.entry_b if long_a else t.qty_a * t.entry_a
        t.borrow = costs.borrow_cost(short_value, (idx[i] - t.opened_at).total_seconds())
        t.closed_at, t.exit_a, t.exit_b, t.exit_z = idx[i], pa[i], pb[i], z
        t.bars_held, t.exit_reason = bars_held, reason
        t.gross_pnl = pair_pnl(Position(t.direction), t.qty_a, t.qty_b, t.entry_a, t.entry_b, pa[i], pb[i])
        realised += t.gross_pnl - (t.spread_cost + t.fees - before) - t.borrow
        trades.append(t)
        trade, position, bars_held = None, Position.FLAT, 0

    def mark(i: int) -> float:
        if trade is None:
            return starting_equity + realised
        unreal = pair_pnl(Position(trade.direction), trade.qty_a, trade.qty_b, trade.entry_a, trade.entry_b,
                          pa[i], pb[i])
        return starting_equity + realised + unreal

    start = cfg.hedge_lookback - 1
    for i in range(start, len(idx)):
        # 1. Fill the order decided on the previous bar.
        if pending is not None:
            kind, arg, z = pending
            pending = None
            if kind == "open":
                open_trade(arg, z, i)
            elif trade is not None:
                close_trade(arg, z, i)

        # 2. Mark to market and apply the daily loss limit (vs. previous day's close).
        eq = mark(i)
        if day[i] != cur_day:
            cur_day, day_start_eq, day_stopped = day[i], (eq_vals[-1] if eq_vals else starting_equity), False
        eq_vals.append(eq)
        eq_idx.append(idx[i])
        limit = min(cfg.max_daily_loss_usd, cfg.max_daily_loss_pct * day_start_eq)
        if not day_stopped and eq - day_start_eq <= -limit:
            day_stopped = True
            loss_days.append(str(cur_day))
            if trade is not None:
                close_trade("halt: daily loss limit", math.nan, i)
                eq_vals[-1] = mark(i)
            continue

        if not in_session[i]:
            continue

        # 3. Model on data up to and including bar i only.
        window = closes.iloc[i - cfg.hedge_lookback + 1:i + 1]
        stats = compute_spread_stats(window, cfg, with_coint=False)
        if position is Position.FLAT and cfg.entry_z <= abs(stats.zscore) < cfg.stop_z:
            win_a, win_b = np.log(window[a_sym].to_numpy(float)), np.log(window[b_sym].to_numpy(float))
            stats = dataclasses.replace(stats, coint_pvalue=coint_pvalue(win_a, win_b))
        z_vals.append(stats.zscore)
        z_idx.append(idx[i])

        # 4. Decide, exactly as live.
        if position is not Position.FLAT:
            bars_held += 1
        action, note = decide(position, stats, bars_held, cfg)
        if action is Action.BLOCKED:
            blocked["beyond stop band" if "stop band" in note else
                    "cointegration test failed" if "cointegration" in note else "hedge ratio <= 0"] += 1
            continue
        if action in (Action.OPEN_LONG, Action.OPEN_SHORT):
            if day_stopped:
                blocked["daily loss limit"] += 1
                continue
            direction = Position.LONG_SPREAD if action is Action.OPEN_LONG else Position.SHORT_SPREAD
            try:
                plan = plan_pair(direction, stats, cfg)
            except ValueError:
                blocked["sizing"] += 1
                continue
            order = ("open", plan, stats.zscore)
        elif action is Action.CLOSE:
            order = ("close", note, stats.zscore)
        else:
            continue

        if fill == "close":
            (open_trade if order[0] == "open" else close_trade)(order[1], order[2], i)
            eq_vals[-1] = mark(i)
        else:
            pending = order

    if trade is not None:                                   # close anything left at the last price
        close_trade("end of data", math.nan, len(idx) - 1)
        eq_vals[-1] = mark(len(idx) - 1)

    return BacktestResult(trades, pd.Series(eq_vals, index=pd.DatetimeIndex(eq_idx)),
                          pd.Series(z_vals, index=pd.DatetimeIndex(z_idx), dtype=float),
                          blocked, loss_days, starting_equity, costs)


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------
def load_csv(path: str | Path, cfg: Settings) -> pd.DataFrame:
    """CSV with a timestamp column (first column) and one close column per symbol."""
    df = pd.read_csv(path, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True)
    missing = {cfg.symbol_a, cfg.symbol_b} - set(df.columns)
    if missing:
        raise ValueError(f"{path} has no column(s) {sorted(missing)}; columns are {list(df.columns)}")
    return df[[cfg.symbol_a, cfg.symbol_b]].sort_index()


def fetch_alpaca(cfg: Settings, start: str, end: str, cache_dir: str | Path = "data") -> pd.DataFrame:
    """Download split- and dividend-adjusted bars from Alpaca once, then reuse the cached CSV."""
    cache = Path(cache_dir) / f"{cfg.symbol_a}_{cfg.symbol_b}_{cfg.timeframe_minutes}m_{start}_{end}_{cfg.data_feed}.csv"
    if cache.exists():
        return load_csv(cache, cfg)
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    client = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
    req = StockBarsRequest(symbol_or_symbols=[cfg.symbol_a, cfg.symbol_b],
                           timeframe=TimeFrame(cfg.timeframe_minutes, TimeFrameUnit.Minute),
                           start=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
                           end=datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
                           adjustment=Adjustment.ALL,
                           feed=DataFeed.IEX if cfg.data_feed == "iex" else DataFeed.SIP)
    df = client.get_stock_bars(req).df                      # the SDK follows pagination for us
    if df.empty:
        raise ValueError("Alpaca returned no bars for that period")
    closes = df["close"].unstack(level="symbol")[[cfg.symbol_a, cfg.symbol_b]].dropna()
    cache.parent.mkdir(parents=True, exist_ok=True)
    closes.to_csv(cache)
    return closes


def sim_closes(cfg: Settings, bars: int, seed: int = 11, **market_kw) -> pd.DataFrame:
    from sim import SimMarket

    m = SimMarket(symbol_a=cfg.symbol_a, symbol_b=cfg.symbol_b, seed=seed, **market_kw)
    m.warm_up(bars)
    return m.closes(bars)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def write_report(result: BacktestResult, cfg: Settings, out_dir: str | Path) -> Path:
    """Write trades.csv, equity.csv, summary.json and an interactive report.html."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = result.summary()
    result.trades_frame().to_csv(out / "trades.csv", index=False)
    result.equity.rename("equity").to_csv(out / "equity.csv")
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08,
                        subplot_titles=("Equity after costs ($)", "Spread z-score"))
    fig.add_trace(go.Scattergl(x=result.equity.index, y=result.equity, name="equity", mode="lines"), 1, 1)
    fig.add_trace(go.Scattergl(x=result.zscore.index, y=result.zscore, name="z", mode="lines",
                               line=dict(width=1)), 2, 1)
    for level, dash in ((cfg.entry_z, "dash"), (cfg.exit_z, "dot"), (cfg.stop_z, "dashdot")):
        for sgn in (1, -1):
            fig.add_hline(y=sgn * level, line_dash=dash, line_width=1, row=2, col=1)
    tf = result.trades_frame()
    if not tf.empty:
        fig.add_trace(go.Scattergl(x=tf.opened_at, y=tf.entry_z, mode="markers", name="open",
                                   marker=dict(symbol="triangle-right", size=9)), 2, 1)
    fig.update_layout(height=700, legend=dict(orientation="h", y=1.06))

    rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in summary.items())
    html = (f"<html><head><meta charset='utf-8'><title>Backtest {cfg.symbol_a}/{cfg.symbol_b}</title>"
            "<style>body{font-family:sans-serif;max-width:1100px;margin:auto;padding:16px}"
            "td{padding:4px 12px;border-bottom:1px solid #ddd;vertical-align:top}</style></head><body>"
            f"<h1>Backtest {cfg.symbol_a} / {cfg.symbol_b}</h1><p><b>{summary['verdict']}</b></p>"
            f"<table>{rows}</table>{fig.to_html(full_html=False, include_plotlyjs='cdn')}</body></html>")
    path = out / "report.html"
    path.write_text(html)
    return path


def print_summary(summary: dict[str, object]) -> None:
    width = max(len(k) for k in summary)
    for k, v in summary.items():
        print(f"{k:<{width}}  {v}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> dict[str, object]:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--source", choices=("sim", "csv", "alpaca"), default="sim")
    p.add_argument("--csv", help="CSV path for --source csv")
    p.add_argument("--start", help="YYYY-MM-DD, for --source alpaca")
    p.add_argument("--end", help="YYYY-MM-DD, for --source alpaca")
    p.add_argument("--bars", type=int, default=20_000, help="bars to simulate for --source sim")
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--pair", nargs=2, metavar=("A", "B"))
    p.add_argument("--timeframe", type=int, help="bar size in minutes")
    p.add_argument("--entry-z", type=float)
    p.add_argument("--exit-z", type=float)
    p.add_argument("--stop-z", type=float)
    p.add_argument("--hedge-lookback", type=int)
    p.add_argument("--z-lookback", type=int)
    p.add_argument("--max-holding-bars", type=int)
    p.add_argument("--leg-notional", type=float)
    p.add_argument("--spread-bps", type=float, default=Costs().spread_bps,
                   help="half-spread + slippage per order, basis points (default 2)")
    p.add_argument("--borrow-rate", type=float, default=Costs().borrow_rate_annual)
    p.add_argument("--fill", choices=("next", "close"), default="next",
                   help="next: fill at the next bar (realistic); close: fill at the signal bar (optimistic)")
    p.add_argument("--equity", type=float, default=100_000.0)
    p.add_argument("--all-hours", action="store_true", help="trade every bar (implied for --source sim)")
    p.add_argument("--out", default="backtest_out")
    args = p.parse_args(argv)

    overrides: dict[str, object] = {}
    if args.pair:
        overrides.update(symbol_a=args.pair[0].upper(), symbol_b=args.pair[1].upper())
    for name in ("timeframe", "entry_z", "exit_z", "stop_z", "hedge_lookback", "z_lookback",
                 "max_holding_bars", "leg_notional"):
        value = getattr(args, name)
        if value is not None:
            overrides["timeframe_minutes" if name == "timeframe" else name] = value
    if args.leg_notional:
        overrides.setdefault("max_gross_exposure", max(10_000.0, 5 * args.leg_notional))
    cfg = load_settings(mode="alpaca" if args.source == "alpaca" else "sim", **overrides)

    if args.source == "csv":
        if not args.csv:
            p.error("--source csv needs --csv PATH")
        closes = load_csv(args.csv, cfg)
    elif args.source == "alpaca":
        if not (args.start and args.end):
            p.error("--source alpaca needs --start and --end")
        closes = fetch_alpaca(cfg, args.start, args.end)
    else:
        closes = sim_closes(cfg, args.bars, args.seed)

    costs = Costs(spread_bps=args.spread_bps, borrow_rate_annual=args.borrow_rate)
    result = run_backtest(closes, cfg, costs, fill=args.fill, starting_equity=args.equity,
                          all_hours=args.all_hours or args.source == "sim")
    report = write_report(result, cfg, args.out)
    summary = result.summary()
    print_summary(summary)
    print(f"\nReport: {report}")
    return summary


if __name__ == "__main__":
    main()
