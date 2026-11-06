"""Buy-only strategies for crypto and gold, run with demo money next to the pairs book so you can compare.

Pairs trading needs a short side, and Alpaca can't short crypto. So for crypto and gold this file tries
approaches that only ever buy, then sell what they bought. They decide on closed 4-hour bars (fewer,
better trades: on 15-minute bars the fees ate everything), and only when the market itself is rising:

  trend     buy when the fast average (20 bars) is above the slow one (100 bars) and price is above both;
            sell when the fast one drops below, the market turns down, or on a trailing stop
  dip       buy a sharp fall (RSI under 30) in a rising, calm market; sell when RSI recovers past 55, on a
            stop, or after two days
  breakout  buy a close clearly above the highest close of the last five days (by half a typical bar);
            sell on a trailing stop; after a losing breakout, wait a day before trying that coin again
  hold      the benchmark: buy once, spread evenly, and keep it. A strategy that can't beat this isn't
            worth running

"The market is rising" (the regime filter) means price is above its 20-day average and that average is
higher than a day ago. A strategy only proposes a trade. ``governance.Sleeve`` (one $10,000 demo account
per strategy) decides the size from the coin's own volatility, enforces the caps, and steps down through
normal / caution / defense / stopped as the account falls from its high point. Every decision, including
"waited" and "refused", goes to the ``long_decisions`` journal so the dashboard can say why it did nothing.

The same ``decide()`` and the same ``Sleeve`` run the backtest (``simulate``, next-bar fills) and the live
demo book (``LongBook``, driven by the watcher). Both pay the same costs (about 0.3% per order on crypto).

    python longonly.py --source sim                              # offline demo
    python longonly.py --source alpaca --days 365                # crypto (no keys needed), last year
    python longonly.py --validate --source alpaca --days 365     # the out-of-sample test with pass/fail gates

Past results, demo money, not a forecast and not financial advice.
"""

from __future__ import annotations

import argparse
import dataclasses
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import Costs
from governance import BARS_PER_DAY, START_EQUITY, STOPPED, Position, Sleeve
from scanner import ALL_SECTORS, CRYPTO_SYMBOLS, MARKET_OF

STRATEGIES = ("trend", "dip", "breakout", "hold")
LONG_SYMBOLS = ("BTCUSD", "ETHUSD", "SOLUSD", "LTCUSD", "GLD", "SLV", "GDX")
BAR = pd.Timedelta(hours=4)        # decisions are made on closed 4-hour bars
LOOKBACK = 300                     # 4-hour bars a decision looks at (50 days)
WARMUP = 130                       # bars needed before any rule but hold may act
CRYPTO_COSTS = Costs(spread_bps=30.0, sec_fee_rate=0.0, taf_per_share=0.0, taf_max=0.0, borrow_rate_annual=0.0)


@dataclass(frozen=True)
class Params:
    """Every rule setting in one place. Fixed before looking at results; ``validate`` nudges them ±20%."""
    fast: int = 20                 # bars (about 3 days)
    slow: int = 100                # bars (about 17 days)
    regime: int = 120              # bars (20 days): the market-state average
    slope_bars: int = 6            # the regime average must be higher than this many bars ago
    trail_days: float = 2.0        # trailing stop: this many days of typical movement below the high
    dip_rsi: float = 30.0
    dip_exit_rsi: float = 55.0
    dip_stop_days: float = 1.5
    dip_bars: int = 12             # give a dip two days to bounce
    breakout_bars: int = 30        # five days
    breakout_margin: float = 0.5   # must clear the old high by this many typical bar moves
    cooldown: int = 6              # bars to wait after a losing breakout
    wild: float = 2.0              # recent movement this many times the usual = a wild market


PARAMS = Params()


def market_of(symbol: str) -> str:
    for sector, syms in ALL_SECTORS.items():
        if symbol in syms:
            return MARKET_OF.get(sector, sector)
    return "?"


def costs_for(symbol: str) -> Costs:
    return CRYPTO_COSTS if symbol in CRYPTO_SYMBOLS else Costs()


def scaled(costs: Costs, mult: float) -> Costs:
    if mult == 1.0:
        return costs
    return dataclasses.replace(costs, spread_bps=costs.spread_bps * mult, sec_fee_rate=costs.sec_fee_rate * mult,
                               taf_per_share=costs.taf_per_share * mult, taf_max=costs.taf_max * mult)


# ---------------------------------------------------------------------------
# Bars and indicators on a plain array of closes (oldest first)
# ---------------------------------------------------------------------------
def to_bars(closes: pd.DataFrame | pd.Series) -> pd.DataFrame:
    """15-minute (or any) closes -> closed 4-hour bars, labelled by the time each bar closed.

    The newest 4-hour bar is dropped while it is still forming, so a decision never sees a half-made candle.
    """
    df = closes.to_frame() if isinstance(closes, pd.Series) else closes
    df = df.sort_index()
    if df.empty:
        return df
    step = df.index.to_series().diff().median()
    step = step if isinstance(step, pd.Timedelta) and step > pd.Timedelta(0) else pd.Timedelta(minutes=15)
    out = df.resample(BAR, label="left", closed="left").last().dropna(how="all")
    out.index = out.index + BAR
    if len(out) and df.index[-1] + step < out.index[-1]:
        out = out.iloc[:-1]
    return out


def prime_bars(timeframe_minutes: int = 15) -> int:
    """How many bars of ``timeframe_minutes`` to fetch so the 4-hour rules can decide from the first scan."""
    return math.ceil((WARMUP + 20) * BAR.total_seconds() / 60 / timeframe_minutes)


def ema(x: np.ndarray, span: int) -> float:
    a = 2.0 / (span + 1)
    w = (1 - a) ** np.arange(len(x) - 1, -1, -1)
    return float(np.sum(w * x) / np.sum(w))


def rsi(x: np.ndarray, n: int = 14) -> float:
    d = np.diff(x[-(n * 5):])
    up, down = np.clip(d, 0, None), np.clip(-d, 0, None)
    a = 1.0 / n                                        # Wilder smoothing
    w = (1 - a) ** np.arange(len(d) - 1, -1, -1)
    au, ad = np.sum(w * up) / np.sum(w), np.sum(w * down) / np.sum(w)
    return 100.0 if ad == 0 else float(100 - 100 / (1 + au / ad))


def bar_vol(x: np.ndarray, n: int = 100) -> float:
    """Typical size of one bar's move, as a fraction of price."""
    r = np.diff(np.log(x[-(n + 1):]))
    return float(np.std(r)) if len(r) > 2 else 0.01


def day_vol(x: np.ndarray) -> float:
    return bar_vol(x) * math.sqrt(BARS_PER_DAY)


def regime(x: np.ndarray, p: Params = PARAMS) -> tuple[str, str]:
    """('up' | 'down' | 'flat' | 'unknown', 'calm' | 'wild'): the market state the rules are allowed to trade in."""
    if len(x) < p.regime + p.slope_bars:
        return "unknown", "calm"
    now, before = ema(x, p.regime), ema(x[:-p.slope_bars], p.regime)
    price = x[-1]
    trend = "up" if price > now > before else "down" if price < now < before else "flat"
    mood = "wild" if len(x) > 60 and bar_vol(x, 30) > p.wild * bar_vol(x, 180) else "calm"
    return trend, mood


@dataclass
class Holding:
    entry: float
    peak: float
    bars: int = 0


def decide(strategy: str, x: np.ndarray, h: Holding | Position | None, p: Params = PARAMS,
           since_loss: int | None = None) -> tuple[str | None, str]:
    """('buy' | 'sell' | None, why) from the closed 4-hour bars up to now. ``h``: the open position, if any.

    ``since_loss``: bars since this strategy last lost money on this symbol (breakout waits after one).
    When it does nothing, ``why`` says what it is waiting for, for the decision journal.
    """
    price = float(x[-1])
    move = day_vol(x) * price                               # one day of typical movement, in $
    if strategy == "hold":
        return ("buy", "benchmark: buy and hold") if h is None else (None, "holding (benchmark)")
    if len(x) < WARMUP:
        return None, f"warming up ({len(x)} of {WARMUP} four-hour bars)"
    trend, mood = regime(x, p)
    fast, slow = ema(x[-200:], p.fast), ema(x, p.slow)
    if h is not None:
        if strategy in ("trend", "breakout") and price < h.peak - p.trail_days * move:
            return "sell", "trailing stop"
        if strategy == "trend" and fast < slow:
            return "sell", "trend turned down"
        if trend == "down":
            return "sell", "market turned down"
        if strategy == "dip":
            if price < h.entry - p.dip_stop_days * move:
                return "sell", "stop: kept falling"
            if rsi(x) > p.dip_exit_rsi:
                return "sell", f"bounced (RSI back above {p.dip_exit_rsi:.0f})"
            if h.bars >= p.dip_bars:
                return "sell", "time limit"
        return None, "holding"
    if trend != "up":
        return None, f"waiting: market not rising ({trend})"
    if strategy == "trend":
        if fast > slow and price > fast:
            return "buy", f"uptrend (fast avg {fast:,.2f} > slow {slow:,.2f})"
        return None, "waiting: averages not lined up"
    if strategy == "dip":
        if mood == "wild":
            return None, "waiting: market too wild to buy dips"
        r = rsi(x)
        if r < p.dip_rsi:
            return "buy", f"dip in a rising market (RSI {r:.0f})"
        return None, f"waiting: no dip (RSI {r:.0f})"
    if strategy == "breakout":
        if since_loss is not None and since_loss < p.cooldown:
            return None, "waiting: cooling down after a failed breakout"
        n = p.breakout_bars
        if len(x) > n + 1:
            level = float(np.max(x[-n - 1:-1])) + p.breakout_margin * bar_vol(x) * price
            if price > level:
                return "buy", f"clean new {n // BARS_PER_DAY}-day high"
        return None, "waiting: no clean new high"
    return None, "unknown strategy"


# ---------------------------------------------------------------------------
# Backtest: one sleeve per strategy across all its symbols, next-bar fills, same costs
# ---------------------------------------------------------------------------
@dataclass
class LongTrade:
    strategy: str
    symbol: str
    opened_at: pd.Timestamp
    entry: float
    qty: float
    note: str
    closed_at: pd.Timestamp | None = None
    exit: float = math.nan
    reason: str = ""
    costs: float = 0.0
    regime: str = ""

    @property
    def gross(self) -> float:
        return (self.exit - self.entry) * self.qty if self.closed_at is not None else math.nan

    @property
    def net(self) -> float:
        return self.gross - self.costs


@dataclass
class Decision:
    ts: pd.Timestamp
    strategy: str
    symbol: str
    action: str                    # buy / sell / wait / refused / hold
    reason: str
    price: float
    qty: float
    state: str


@dataclass
class SimResult:
    strategy: str
    trades: list[LongTrade]
    equity: pd.Series                       # sleeve value after each bar
    invested: pd.Series                     # share of the sleeve in positions after each bar
    decisions: list[Decision] = field(default_factory=list)
    states: pd.Series = field(default_factory=lambda: pd.Series(dtype=object))


def simulate(bars: pd.DataFrame, strategy: str, symbols: list[str] | None = None, p: Params = PARAMS,
             cost_mult: float = 1.0, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None,
             close_at_end: bool = True, journal: bool = False) -> SimResult:
    """Trade one strategy's sleeve on closed 4-hour ``bars`` from ``start`` to ``end``.

    Earlier bars are only history. A decision on bar t fills at that symbol's next close, with the qty the
    sleeve approved at bar t. Exits are handled before entries.
    """
    symbols = [s for s in (symbols or list(bars.columns)) if s in bars and bars[s].notna().any()]
    sleeve = Sleeve(strategy)
    is_hold = strategy == "hold"
    data = {s: bars[s].dropna() for s in symbols}
    arrays = {s: d.to_numpy(float) for s, d in data.items()}
    where = {s: {t: k for k, t in enumerate(d.index)} for s, d in data.items()}
    costs = {s: scaled(costs_for(s), cost_mult) for s in symbols}
    index = bars.index
    if start is not None:
        index = index[index >= start]
    if end is not None:
        index = index[index < end]
    trades: dict[str, LongTrade] = {}
    done: list[LongTrade] = []
    pending: dict[str, tuple[str, float, str, str]] = {}          # symbol -> (action, qty, why, regime)
    last_loss: dict[str, int] = {}
    decisions: list[Decision] = []
    eq, inv, st = [], [], []
    last_px: dict[str, float] = {}

    def close(sym: str, px: float, t, why: str) -> None:
        tr, pos = trades.pop(sym), sleeve.positions.pop(sym)
        sell = sum(costs[sym].order_cost(tr.qty, px, "sell"))
        tr.closed_at, tr.exit, tr.reason, tr.costs = t, px, why, tr.costs + sell
        sleeve.realized += (px - pos.entry) * pos.qty - pos.cost_in - sell
        if tr.net <= 0:
            last_loss[sym] = where[sym][t]
        done.append(tr)

    for t in index:
        here = [s for s in symbols if t in where[s]]
        for s in here:
            last_px[s] = float(arrays[s][where[s][t]])
        # 1. fill what was decided on the previous bar
        for s in [s for s in pending if s in here]:
            action, qty, why, reg = pending.pop(s)
            px = last_px[s]
            if action == "sell" and s in trades:
                close(s, px, t, why)
            elif action == "buy" and s not in trades:
                buy = sum(costs[s].order_cost(qty, px, "buy"))
                trades[s] = LongTrade(strategy, s, t, px, qty, why, costs=buy, regime=reg)
                sleeve.positions[s] = Position(s, qty, px, px, cost_in=buy, last=px, regime=reg)
        # 2. mark to market and read the risk state
        state = sleeve.mark(last_px)
        if state == "stopped" and not is_hold:
            for s in list(sleeve.positions):
                if s not in pending:
                    pending[s] = ("sell", 0.0, f"drawdown stop: {STOPPED:.0%} below the high", "")
        # 3. decide on each symbol that has a new bar
        for s in here:
            if s in pending:
                continue
            k = where[s][t]
            x = arrays[s][max(0, k + 1 - LOOKBACK):k + 1]
            pos = sleeve.positions.get(s)
            if pos is not None:
                pos.bars += 1
                pos.peak = max(pos.peak, x[-1])
            since = k - last_loss[s] if s in last_loss else None
            action, why = decide(strategy, x, pos, p, since)
            qty, entry_state = 0.0, state
            if action == "sell" and pos is not None:
                pending[s] = ("sell", pos.qty, why, "")
                logged = "sell"
            elif action == "buy" and pos is None:
                qty, rule = sleeve.size(s, float(x[-1]), day_vol(x), s in CRYPTO_SYMBOLS, benchmark=is_hold,
                                        n_assets=len(symbols))
                if qty > 0:
                    trend, mood = regime(x, p)
                    pending[s] = ("buy", qty, f"{why}; {rule}", f"{trend}/{mood}")
                    logged = "buy"
                else:
                    logged, why = "refused", f"{why}, but {rule}"
            else:
                logged = "hold" if pos is not None else "wait"
            if journal:
                decisions.append(Decision(t, strategy, s, logged, why, float(x[-1]), qty, entry_state))
        eq.append(sleeve.value())
        inv.append(sum(p_.value for p_ in sleeve.positions.values()) / max(sleeve.value(), 1e-9))
        st.append(sleeve.state())
    if close_at_end and len(index):
        for s in list(trades):
            close(s, last_px[s], index[-1], "end of test (still open)")
    done += list(trades.values())
    return SimResult(strategy, sorted(done, key=lambda tr: tr.opened_at), pd.Series(eq, index=index, dtype=float),
                     pd.Series(inv, index=index, dtype=float), decisions, pd.Series(st, index=index, dtype=object))


def backtest(closes: pd.Series, strategy: str, costs: Costs | None = None, start: pd.Timestamp | None = None,
             close_at_end: bool = True) -> list[LongTrade]:
    """Trades of one strategy on one symbol (its own sleeve). ``closes``: 15-minute or 4-hour closes."""
    bars = to_bars(closes.rename(closes.name or "X"))
    return simulate(bars, strategy, [bars.columns[0]], start=start, close_at_end=close_at_end).trades


def compare(closes: pd.DataFrame, symbols: list[str], start: pd.Timestamp | None = None,
            p: Params = PARAMS) -> pd.DataFrame:
    """Every strategy's trades on these symbols, one sleeve per strategy, as one table."""
    bars = to_bars(closes[[s for s in symbols if s in closes]])
    rows = []
    for strat in STRATEGIES:
        for t in simulate(bars, strat, start=start, p=p).trades:
            rows.append((strat, t.symbol, market_of(t.symbol), t.opened_at, t.closed_at, t.net, t.reason))
    return pd.DataFrame(rows, columns=["strategy", "symbol", "market", "opened", "closed", "net_pnl", "reason"])


def summary_table(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    g = trades.groupby("strategy")["net_pnl"]
    out = pd.DataFrame({"trades": g.size(), "won": g.apply(lambda s: int((s > 0).sum())),
                        "net": g.sum(), "avg": g.mean()})
    out["win_rate"] = out["won"] / out["trades"]
    return out.sort_values("net", ascending=False)


# ---------------------------------------------------------------------------
# Validation: chronological splits, walk-forward, stress tests, pass/fail gates
# ---------------------------------------------------------------------------
GATES = ("makes money out of sample", "beats buy-and-hold in 2 of 3 periods", "survives 1.5x costs",
         "survives rule settings ±20%", "worst fall under 15%", "at least 30 trades")


def metrics(res: SimResult) -> dict:
    eq = res.equity
    net = [t.net for t in res.trades]
    wins, losses = [n for n in net if n > 0], [n for n in net if n <= 0]
    rets = eq.pct_change().dropna()
    streak = cur = 0
    for n in net:
        cur = cur + 1 if n <= 0 else 0
        streak = max(streak, cur)
    traded = sum(t.qty * (t.entry + (t.exit if t.closed_at is not None else t.entry)) for t in res.trades)
    by_regime: dict[str, float] = {}
    for t in res.trades:
        by_regime[t.regime or "-"] = by_regime.get(t.regime or "-", 0.0) + t.net
    end = float(eq.iloc[-1]) if len(eq) else START_EQUITY
    return {
        "net": end - START_EQUITY,
        "return": end / START_EQUITY - 1,
        "max drawdown": float((1 - eq / eq.cummax().clip(lower=START_EQUITY)).max()) if len(eq) else 0.0,
        "sharpe": float(rets.mean() / rets.std() * math.sqrt(BARS_PER_DAY * 365)) if len(rets) > 2 and rets.std() > 0 else 0.0,
        "trades": len(net),
        "win rate": len(wins) / len(net) if net else 0.0,
        "profit factor": sum(wins) / -sum(losses) if sum(losses) < 0 else (math.inf if wins else 0.0),
        "time invested": float(res.invested.mean()) if len(res.invested) else 0.0,
        "turnover": traded / START_EQUITY,
        "losing streak": streak,
        "costs": sum(t.costs for t in res.trades),
        "by regime": by_regime,
        "days stopped": float((res.states == "stopped").sum()) / BARS_PER_DAY,
    }


def validate(closes: pd.DataFrame, symbols: list[str] | None = None, p: Params = PARAMS,
             windows: int = 6) -> tuple[str, dict]:
    """Test every strategy the way the roadmap asks, and return (markdown report, results by strategy).

    Periods are cut by time (oldest third = development, middle = validation, newest = out of sample), so no
    test ever peeks at the future. Rules are fixed: nothing here is tuned on the results.
    """
    bars = to_bars(closes if symbols is None else closes[[s for s in symbols if s in closes]])
    idx = bars.index[WARMUP:]
    if len(idx) < 3 * BARS_PER_DAY * 10:
        return "Not enough history to validate: need at least two months of prices after a three-week warm-up.", {}
    cuts = [idx[0], idx[len(idx) // 3], idx[2 * len(idx) // 3], None]
    periods = (("development", cuts[0], cuts[1]), ("validation", cuts[1], cuts[2]), ("out of sample", cuts[2], None))
    wf_edges = [idx[len(idx) * i // windows] for i in range(windows)] + [None]
    slower = dataclasses.replace(p, fast=round(p.fast * 1.2), slow=round(p.slow * 1.2), regime=round(p.regime * 1.2))
    faster = dataclasses.replace(p, fast=round(p.fast * 0.8), slow=round(p.slow * 0.8), regime=round(p.regime * 0.8))

    def run(strat, **kw):
        return metrics(simulate(bars, strat, p=kw.pop("p", p), **kw))

    results: dict[str, dict] = {}
    for strat in STRATEGIES:
        r = {"full": run(strat, start=idx[0])}
        for name, a, b in periods:
            r[name] = run(strat, start=a, end=b)
        r["walk"] = [run(strat, start=wf_edges[i], end=wf_edges[i + 1]) for i in range(windows)]
        if strat != "hold":
            r["costs x1.5"] = run(strat, start=idx[0], cost_mult=1.5)
            r["slower"] = run(strat, start=idx[0], p=slower)
            r["faster"] = run(strat, start=idx[0], p=faster)
        results[strat] = r
    hold = results["hold"]
    for strat, r in results.items():
        if strat == "hold":
            continue
        beat = sum(r[name]["return"] > hold[name]["return"] for name, _, _ in periods)
        r["walk beats hold"] = sum(w["return"] > h["return"] for w, h in zip(r["walk"], hold["walk"]))
        r["gates"] = dict(zip(GATES, (
            r["out of sample"]["net"] > 0, beat >= 2, r["costs x1.5"]["net"] > 0,
            r["slower"]["net"] > 0 and r["faster"]["net"] > 0, r["full"]["max drawdown"] < STOPPED,
            r["full"]["trades"] >= 30)))
        r["passed"] = all(r["gates"].values())
    return _validation_md(results, bars, idx, periods, windows), results


def _pct(x: float) -> str:
    return f"{x:+.1%}"


def _usd(x: float) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.0f}"


def _validation_md(results: dict, bars: pd.DataFrame, idx, periods, windows: int) -> str:
    def day(t):
        return (t if t is not None else bars.index[-1]).strftime("%d %b %Y")
    lines = [f"# Validation of the buy-only strategies", "",
             f"{day(idx[0])} to {day(None)} on {', '.join(bars.columns)}, closed 4-hour bars, "
             f"${START_EQUITY:,.0f} demo account per strategy, costs included. Rules were fixed before this test.", ""]
    passed = [s for s, r in results.items() if r.get("passed")]
    lines += ["## Verdict", "",
              (f"**Ready for paper trading:** {', '.join(passed)}. Everything else stays in demo." if passed else
               "**No strategy passed every gate.** Keep them all in demo; none is ready for paper money."), ""]
    for strat, r in results.items():
        if strat == "hold":
            continue
        failed = [g for g, ok in r["gates"].items() if not ok]
        lines.append(f"- **{strat}**: " + ("passed every gate." if not failed else "fails: " + "; ".join(failed) + "."))
    lines += ["", "## By period (return on the demo account)", "",
              "| strategy | " + " | ".join(f"{n} ({day(a)}–{day(b)})" for n, a, b in periods) + " | whole test | worst fall | trades |",
              "|---|" + "---|" * (len(periods) + 3)]
    for strat, r in results.items():
        lines.append(f"| {strat} | " + " | ".join(_pct(r[n]["return"]) for n, _, _ in periods)
                     + f" | {_pct(r['full']['return'])} | {r['full']['max drawdown']:.1%} | {r['full']['trades']} |")
    lines += ["", "## Whole test, in detail", "",
              "| strategy | net | Sharpe | won | profit factor | time invested | turnover | losing streak | costs | days stopped |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for strat, r in results.items():
        f = r["full"]
        pf = "∞" if f["profit factor"] == math.inf else f"{f['profit factor']:.2f}"
        lines.append(f"| {strat} | {_usd(f['net'])} | {f['sharpe']:.2f} | {f['win rate']:.0%} | {pf} | "
                     f"{f['time invested']:.0%} | {f['turnover']:.1f}x | {f['losing streak']} | {_usd(f['costs'])} | "
                     f"{f['days stopped']:.0f} |")
    lines += ["", "## Stress tests (net over the whole test)", "",
              "| strategy | as tested | costs x1.5 | slower averages (+20%) | faster averages (-20%) | "
              f"walk-forward: beat hold in |", "|---|---|---|---|---|---|"]
    for strat, r in results.items():
        if strat == "hold":
            continue
        lines.append(f"| {strat} | {_usd(r['full']['net'])} | {_usd(r['costs x1.5']['net'])} | {_usd(r['slower']['net'])} | "
                     f"{_usd(r['faster']['net'])} | {r['walk beats hold']} of {windows} windows |")
    lines += ["", "## Where the money came from (net by market state at entry)", ""]
    for strat, r in results.items():
        if strat != "hold" and r["full"]["by regime"]:
            lines.append(f"- **{strat}**: " + ", ".join(f"{k} {_usd(v)}" for k, v in sorted(r["full"]["by regime"].items())))
    lines += ["", "Gates: " + "; ".join(GATES) + ".", "",
              "Demo money on past prices. Passing means worth paper trading, not that it will make money. "
              "Not financial advice."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Live demo book, driven by the watcher on every scan
# ---------------------------------------------------------------------------
class LongBook:
    """The buy-only strategies on live prices, one ``Sleeve`` each, deciding on every newly closed 4-hour bar.

    Keeps its own price history (the watcher only fetches about ten days), journals every decision, and saves
    each sleeve's value, high point and risk state so a restart carries on where it left off.
    """

    def __init__(self, db, symbols=LONG_SYMBOLS, strategies=STRATEGIES, params: Params = PARAMS) -> None:
        self.db, self.symbols, self.strategies, self.params = db, tuple(symbols), tuple(strategies), params
        self.sleeves = {s: Sleeve(s) for s in self.strategies}
        self.ids: dict[tuple[str, str], int] = {}              # (strategy, symbol) -> long_trades id
        self.last_bar: dict[str, float] = {}
        self.loss_at: dict[tuple[str, str], float] = {}        # last losing exit's bar time
        self.hist = pd.DataFrame()

    @property
    def open(self) -> dict[tuple[str, str], Position]:
        return {(s, sym): p for s, sl in self.sleeves.items() for sym, p in sl.positions.items()}

    async def _rows(self, sql: str, args: tuple = ()) -> list:
        try:
            return await self.db.fetchall(sql, args)
        except Exception:                                       # a table from before this version
            return []

    async def load(self) -> None:
        for i, strat, sym, entry, qty, costs, last in await self._rows(
                "SELECT id, strategy, symbol, entry, qty, costs, last_price FROM long_trades WHERE closed_at IS NULL"):
            if strat in self.sleeves:
                last = last or entry
                self.sleeves[strat].positions[sym] = Position(sym, qty, entry, max(entry, last), cost_in=costs or 0.0,
                                                              last=last)
                self.ids[(strat, sym)] = i
        for strat, net in await self._rows("SELECT strategy, COALESCE(SUM(net_pnl), 0) FROM long_trades "
                                           "WHERE closed_at IS NOT NULL GROUP BY strategy"):
            if strat in self.sleeves:
                self.sleeves[strat].realized = float(net)
        for strat, peak, stopped in await self._rows("SELECT strategy, peak, stopped FROM long_sleeves"):
            if strat in self.sleeves:
                self.sleeves[strat].peak, self.sleeves[strat].stopped = float(peak), bool(stopped)
        for sym, ts in await self._rows("SELECT symbol, MAX(ts) FROM long_decisions GROUP BY symbol"):
            self.last_bar[sym] = float(ts)
        for strat, sym, ts in await self._rows("SELECT strategy, symbol, MAX(closed_at) FROM long_trades "
                                               "WHERE closed_at IS NOT NULL AND net_pnl <= 0 GROUP BY strategy, symbol"):
            self.loss_at[(strat, sym)] = float(ts)

    def prime(self, closes: pd.DataFrame) -> None:
        """Give the book a long history once at start (about 50 days), so it can decide straight away."""
        self._merge(closes)

    def _merge(self, closes: pd.DataFrame) -> None:
        cols = [s for s in self.symbols if s in closes]
        if not cols:
            return
        new = closes[cols]
        merged = new if self.hist.empty else pd.concat([self.hist, new])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        self.hist = merged[merged.index >= merged.index[-1] - (LOOKBACK + 2) * BAR]

    async def _journal(self, ts: float, strat: str, sym: str, action: str, why: str, price: float, qty: float,
                       state: str) -> None:
        await self.db._write("INSERT INTO long_decisions (ts, strategy, symbol, action, reason, price, qty, state) "
                             "VALUES (?,?,?,?,?,?,?,?)", (ts, strat, sym, action, why, price, qty, state))

    async def _buy(self, strat: str, sym: str, qty: float, price: float, ts: float, why: str) -> None:
        cost = sum(costs_for(sym).order_cost(qty, price, "buy"))
        tid = await self.db._write(
            "INSERT INTO long_trades (strategy, symbol, market, opened_at, entry, qty, entry_note, costs, last_price, "
            "last_ts) VALUES (?,?,?,?,?,?,?,?,?,?)", (strat, sym, market_of(sym), ts, price, qty, why, cost, price, ts))
        self.sleeves[strat].positions[sym] = Position(sym, qty, price, price, cost_in=cost, last=price)
        self.ids[(strat, sym)] = tid

    async def _sell(self, strat: str, sym: str, price: float, ts: float, why: str) -> float:
        sl = self.sleeves[strat]
        pos, tid = sl.positions.pop(sym), self.ids.pop((strat, sym))
        sell = sum(costs_for(sym).order_cost(pos.qty, price, "sell"))
        gross, total = (price - pos.entry) * pos.qty, pos.cost_in + sell
        await self.db._write("UPDATE long_trades SET closed_at=?, exit=?, gross_pnl=?, costs=?, net_pnl=?, exit_reason=? "
                             "WHERE id=?", (ts, price, round(gross, 2), round(total, 2), round(gross - total, 2), why, tid))
        sl.realized += gross - total
        if gross - total <= 0:
            self.loss_at[(strat, sym)] = ts
        return gross - total

    async def update(self, closes: pd.DataFrame, tradable: set[str]) -> list[str]:
        """Mark every sleeve at the newest prices, then decide on each symbol with a newly closed 4-hour bar.

        Fills at the newest price the watcher saw (it has no next bar yet).
        """
        self._merge(closes)
        if self.hist.empty:
            return []
        events: list[str] = []
        prices, seen = {}, {}
        for sym in self.hist.columns:
            s = self.hist[sym].dropna()
            if len(s):
                prices[sym], seen[sym] = float(s.iloc[-1]), s.index[-1].timestamp()
        for sl in self.sleeves.values():
            sl.mark(prices)
        # a stopped sleeve sells everything it can
        for strat, sl in self.sleeves.items():
            if strat != "hold" and sl.state() == "stopped":
                for sym in [s for s in sl.positions if s in tradable and s in prices]:
                    why = f"drawdown stop: {STOPPED:.0%} below the high"
                    net = await self._sell(strat, sym, prices[sym], seen[sym], why)
                    await self._journal(seen[sym], strat, sym, "sell", why, prices[sym], 0.0, "stopped")
                    events.append(f"{strat} sold {sym} at {prices[sym]:,.2f} ({why}), net ${net:+,.2f}")
        bars = to_bars(self.hist)
        for sym in self.symbols:
            if sym not in bars or sym not in tradable or sym not in prices:
                continue
            s = bars[sym].dropna()
            if s.empty:
                continue
            bar_ts = s.index[-1].timestamp()
            if self.last_bar.get(sym, 0.0) >= bar_ts:                # no newly closed bar since last time
                continue
            self.last_bar[sym] = bar_ts
            x = s.to_numpy(float)[-LOOKBACK:]
            price = prices[sym]
            for strat in self.strategies:
                sl = self.sleeves[strat]
                state = sl.state()
                pos = sl.positions.get(sym)
                if pos is not None:
                    pos.bars += 1
                    pos.peak = max(pos.peak, float(x[-1]))
                lost = self.loss_at.get((strat, sym))
                since = int((bar_ts - lost) // BAR.total_seconds()) if lost is not None else None
                action, why = decide(strat, x, pos, self.params, since)
                if action == "sell" and pos is not None:
                    net = await self._sell(strat, sym, price, bar_ts, why)
                    await self._journal(bar_ts, strat, sym, "sell", why, price, pos.qty, state)
                    events.append(f"{strat} sold {sym} at {price:,.2f} ({why}), net ${net:+,.2f}")
                elif action == "buy" and pos is None:
                    qty, rule = sl.size(sym, price, day_vol(x), sym in CRYPTO_SYMBOLS, benchmark=strat == "hold",
                                        n_assets=len(self.symbols))
                    if qty > 0:
                        await self._buy(strat, sym, qty, price, bar_ts, f"{why}; {rule}")
                        await self._journal(bar_ts, strat, sym, "buy", f"{why}; {rule}", price, qty, state)
                        events.append(f"{strat} bought {sym} at {price:,.2f} ({why}; {rule})")
                    else:
                        await self._journal(bar_ts, strat, sym, "refused", f"{why}, but {rule}", price, 0.0, state)
                else:
                    await self._journal(bar_ts, strat, sym, "hold" if pos is not None else "wait", why, price, 0.0,
                                        state)
        for sym in {s for sl in self.sleeves.values() for s in sl.positions} & set(prices):   # live value
            await self.db._write("UPDATE long_trades SET last_price=?, last_ts=? WHERE symbol=? AND closed_at IS NULL",
                                 (prices[sym], seen[sym], sym))
        await self._save_sleeves()
        return events

    async def _save_sleeves(self) -> None:
        for strat, sl in self.sleeves.items():
            await self.db._write(
                "INSERT INTO long_sleeves (strategy, value, peak, drawdown, state, stopped, updated_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(strategy) DO UPDATE SET value=excluded.value, peak=excluded.peak, "
                "drawdown=excluded.drawdown, state=excluded.state, stopped=excluded.stopped, updated_at=excluded.updated_at",
                (strat, round(sl.value(), 2), round(sl.peak, 2), round(sl.drawdown(), 4), sl.state(), int(sl.stopped),
                 self.last_bar and max(self.last_bar.values()) or None))

    async def close_all(self, closes: pd.DataFrame, reason: str = "end of replay (marked at last price)") -> None:
        """Mark every open demo position at its last price, so a replay's comparison includes them (hold too)."""
        for (strat, sym) in list(self.open):
            if sym not in closes or closes[sym].dropna().empty:
                continue
            s = closes[sym].dropna()
            await self._sell(strat, sym, float(s.iloc[-1]), s.index[-1].timestamp(), reason)
        await self._save_sleeves()


# ---------------------------------------------------------------------------
# Side by side: the pairs demo book against each buy-only strategy
# ---------------------------------------------------------------------------
def comparison_rows(path: str, since: float = 0.0) -> list[tuple[str, int, int, float]]:
    """(approach, closed trades, won, net) for the pairs book and each buy-only strategy."""
    import sqlite3
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    rows = []
    try:
        for label, where in (("pairs, STRONG only", "grade = 'STRONG'"),
                             ("pairs, STRONG + RISKY", "grade IN ('STRONG', 'RISKY')")):
            try:
                n, w, net = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(net_pnl > 0), 0), COALESCE(SUM(net_pnl), 0) "
                                         f"FROM shadow_trades WHERE closed_at IS NOT NULL AND {where} AND opened_at >= ?",
                                         (since,)).fetchone()
                rows.append((label, n, w, net))
            except sqlite3.OperationalError:
                pass
        try:
            for strat, n, w, net in conn.execute(
                    "SELECT strategy, COUNT(*), COALESCE(SUM(net_pnl > 0), 0), COALESCE(SUM(net_pnl), 0) FROM long_trades "
                    "WHERE closed_at IS NOT NULL AND opened_at >= ? GROUP BY strategy", (since,)):
                rows.append((f"buy-only {strat}", n, w, net))
            for strat, n, unreal in conn.execute(
                    "SELECT strategy, COUNT(*), 0 FROM long_trades WHERE closed_at IS NULL GROUP BY strategy"):
                if not any(r[0] == f"buy-only {strat}" for r in rows):
                    rows.append((f"buy-only {strat} (still open)", 0, 0, 0.0))
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    return sorted(rows, key=lambda r: -r[3])


def sleeve_rows(path: str) -> list[tuple[str, float, float, str]]:
    """(strategy, value, drawdown, state) for each buy-only demo account."""
    import sqlite3
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT strategy, value, drawdown, state FROM long_sleeves ORDER BY strategy").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def comparison_text(path: str, since: float = 0.0) -> str:
    rows = comparison_rows(path, since)
    if not rows or all(r[1] == 0 for r in rows):
        return ("No closed demo trades to compare yet. The watcher trades the pairs book and the buy-only strategies "
                "(trend, dip, breakout, hold) with demo money; the buy-only ones decide every four hours, so check back "
                "after a few days.")
    lines = ["Approaches compared on demo money, after costs (best first):"]
    for label, n, w, net in rows:
        lines.append(f"- {label}: {n} closed trades, {w / n:.0%} won, net ${net:+,.2f}" if n else f"- {label}: no closed trades yet")
    total = sum(r[1] for r in rows)
    lines.append("Buy-only 'hold' is the benchmark: an approach that can't beat it isn't worth running."
                 + (f" Only {total} trades so far: too few to pick a winner." if total < 100 else ""))
    sleeves = sleeve_rows(path)
    if sleeves:
        lines.append("Risk state of each buy-only account: " + ", ".join(
            f"{s} {state} (${v:,.0f}, {dd:.0%} below its high)" for s, v, dd, state in sleeves) + ".")
    lines.append("Past demo results, not a forecast.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI: compare (or validate) on past prices
# ---------------------------------------------------------------------------
def _sim_closes(bars: int, freq: str = "15min") -> pd.DataFrame:
    rng = np.random.default_rng(7)
    idx = pd.date_range("2025-11-01", periods=bars, freq=freq, tz="UTC")
    scale = math.sqrt(pd.Timedelta(freq) / pd.Timedelta(minutes=15))
    phase = np.sin(np.arange(bars) / bars * 6 * math.pi)                  # up, down, up... a few regimes
    out = {}
    for i, sym in enumerate(("BTCUSD", "ETHUSD", "GLD")):
        r = 0.00006 * scale ** 2 * phase * (1 + 0.3 * i) + rng.normal(0, (0.004 if sym != "GLD" else 0.0015) * scale, bars)
        out[sym] = 100 * np.exp(np.cumsum(r))
    return pd.DataFrame(out, index=idx)


def _fetch(source: str, days: int, symbols: list[str]) -> pd.DataFrame:
    warm = (WARMUP + 10) * 16
    if source == "sim":
        return _sim_closes(days * 96 + warm)
    from config import load_settings
    from watcher import AlpacaSource, watch_settings
    crypto = [s for s in symbols if s in CRYPTO_SYMBOLS]
    stocks = [s for s in symbols if s not in CRYPTO_SYMBOLS]
    base = load_settings(mode="alpaca" if stocks else "sim")
    cfg = watch_settings(base, 15)
    src = AlpacaSource(cfg, {**({"crypto": crypto} if crypto else {}), **({"mixed": stocks} if stocks else {})})
    frames = []
    if crypto:
        frames.append(src._fetch_crypto(days * 96 + warm))
    if stocks:
        frames.append(src._fetch_stocks(days * 26 + warm // 4))
    return pd.concat(frames, axis=1)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Compare or validate the buy-only strategies on past prices")
    ap.add_argument("--source", choices=("sim", "alpaca"), default="sim")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--symbols", nargs="+", default=["BTCUSD", "ETHUSD", "SOLUSD", "LTCUSD"])
    ap.add_argument("--validate", action="store_true", help="split by time, stress-test, and apply the pass/fail gates")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "review" / "validation.md"))
    args = ap.parse_args(argv)
    closes = _fetch(args.source, args.days, args.symbols)
    if args.source == "sim":
        args.symbols = list(closes.columns)
    if args.validate:
        report, _ = validate(closes, args.symbols)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(report)
        print(f"Saved to {out} (the dashboard's Approaches page shows it under Validation).")
        return
    trades = compare(closes, args.symbols)
    if trades.empty:
        print("No trades: not enough prices.")
        return
    print(f"Buy-only strategies on 4-hour bars, ${START_EQUITY:,.0f} demo account each, risk-sized, costs included, "
          f"{args.days} days:\n")
    tab = summary_table(trades)
    for strat, r in tab.iterrows():
        print(f"  {strat:<9} {int(r['trades']):>4} trades  {r['win_rate']:>4.0%} won  net ${r['net']:>+10,.2f}  "
              f"(${r['avg']:+,.2f} a trade)")
    print("\nBy symbol (net $):")
    print(trades.pivot_table(index="strategy", columns="symbol", values="net_pnl", aggfunc="sum").round(2).to_string())
    print("\nPast prices and demo money: a strategy that won here can lose next month. Not a forecast. "
          "Run with --validate for the out-of-sample test.")


if __name__ == "__main__":
    main()
