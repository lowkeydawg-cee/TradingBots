"""Pair scanner: find pairs that held up on data the scanner never tuned on.

    python scanner.py --source sim                                       # offline demo
    python scanner.py --source alpaca --start 2026-01-01 --end 2026-10-01 --timeframe 15
    python scanner.py --source alpaca --universe energy banks --start ... --end ...
    python scanner.py --source csv --csv data/closes.csv                 # one column per symbol

How it works (walk-forward, out of sample)
------------------------------------------
1. Split the history in two: a **formation** period (the first 60% by
   default) and a **test** period (the rest).
2. On the formation period only, test every pair in each sector:
   Engle-Granger cointegration p-value, spread half-life and return
   correlation. Pairs that fail are dropped.
3. Run the real backtester (same rules and costs as live) on the **test**
   period only. The scanner never looked at it while choosing, so this is
   the honest estimate of how a pair might do next.
4. Rank by test-period net profit and print the winners as a ``PAIRS=``
   line for ``.env``, skipping pairs that share a stock with a better one.

Also reported for each pair: the in-sample (formation) result. A pair that
was great in-sample but loses out of sample was curve-fit or broke down.
That gap is the main thing this tool exists to show you.

Output: ``scan_out/scan.csv`` with one row per pair tested.
"""

from __future__ import annotations

import argparse
import itertools
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import Costs, run_backtest
from config import Settings, load_settings
from strategy import coint_pvalue, half_life

# Pairs only make sense inside one business: same customers, same costs, same news.
UNIVERSES: dict[str, list[str]] = {
    "beverages": ["KO", "PEP", "KDP", "MNST"],
    "staples": ["PG", "CL", "KMB", "CHD"],
    "energy": ["XOM", "CVX", "COP", "EOG"],
    "banks": ["JPM", "BAC", "C", "WFC"],
    "payments": ["V", "MA", "AXP"],
    "retail": ["WMT", "TGT", "COST"],
    "telecom": ["T", "VZ", "TMUS"],
    "rails": ["UNP", "CSX", "NSC"],
    "chips": ["AMD", "NVDA", "INTC", "AVGO"],
}


@dataclass
class ScanRow:
    pair: str
    sector: str
    coint_p: float
    half_life_bars: float
    corr: float
    passed_filter: bool
    in_trades: int = 0
    in_net: float = math.nan
    out_trades: int = 0
    out_net: float = math.nan
    out_sharpe: float | None = None
    out_breakeven_bps: float | None = None
    out_max_dd: float = math.nan
    out_prob_loss: float | None = None
    note: str = ""


def formation_stats(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float]:
    """(cointegration p-value, half-life in bars, correlation of bar returns) on log prices."""
    la, lb = np.log(a), np.log(b)
    p = coint_pvalue(la, lb)
    beta, alpha = np.polyfit(lb, la, 1)
    hl = half_life(la - beta * lb - alpha)
    corr = float(np.corrcoef(np.diff(la), np.diff(lb))[0, 1])
    return p, hl, corr


def scan(closes: pd.DataFrame, sectors: dict[str, list[str]], base: Settings, costs: Costs, *,
         formation: float = 0.6, max_p: float = 0.05, min_corr: float = 0.5, all_hours: bool = False,
         log=print) -> list[ScanRow]:
    rows: list[ScanRow] = []
    for sector, symbols in sectors.items():
        symbols = [s for s in symbols if s in closes.columns]
        for a, b in itertools.combinations(symbols, 2):
            df = closes[[a, b]].dropna()
            cut = int(len(df) * formation)
            form, test = df.iloc[:cut], df.iloc[cut:]
            if cut <= base.hedge_lookback or len(test) <= base.hedge_lookback:
                rows.append(ScanRow(f"{a}/{b}", sector, math.nan, math.nan, math.nan, False,
                                    note="not enough bars"))
                continue
            p, hl, corr = formation_stats(form[a].to_numpy(float), form[b].to_numpy(float))
            ok = p <= max_p and corr >= min_corr and 1 <= hl <= base.max_holding_bars
            row = ScanRow(f"{a}/{b}", sector, round(p, 4), round(hl, 1), round(corr, 3), ok)
            if not ok:
                row.note = ("not cointegrated" if p > max_p else "weak correlation" if corr < min_corr
                            else "reverts too slowly" if hl > base.max_holding_bars else "half-life < 1 bar")
                rows.append(row)
                continue
            cfg = base.for_pair(a, b)
            ins = run_backtest(form, cfg, costs, all_hours=all_hours).summary()
            # Warm the model with the end of formation so trading starts on the first test bar.
            warm = pd.concat([form.iloc[-(cfg.hedge_lookback - 1):], test])
            out = run_backtest(warm, cfg, costs, all_hours=all_hours).summary()
            mc = out.get("monte_carlo") or {}
            row.in_trades, row.in_net = ins["trades"], float(ins["net_pnl"])
            row.out_trades, row.out_net = out["trades"], float(out["net_pnl"])
            row.out_sharpe = out["sharpe_annualised"]
            row.out_breakeven_bps = None if out["breakeven_cost_bps"] is None else float(out["breakeven_cost_bps"])
            row.out_max_dd, row.out_prob_loss = out["max_drawdown"], mc.get("prob_loss")
            row.note = "held up out of sample" if row.out_net > 0 else "failed out of sample"
            log(f"  {row.pair:<10} p={p:.3f} hl={hl:5.1f}  in-sample ${row.in_net:>9,.2f} "
                f"({row.in_trades} trades)  out-of-sample ${row.out_net:>9,.2f} ({row.out_trades} trades)")
            rows.append(row)
    return rows


def pick(rows: list[ScanRow], top: int, min_trades: int) -> list[ScanRow]:
    """Best out-of-sample pairs, no stock used twice (the live bot can't hold one stock in two pairs)."""
    good = sorted((r for r in rows if r.passed_filter and r.out_net > 0 and r.out_trades >= min_trades),
                  key=lambda r: r.out_net, reverse=True)
    used: set[str] = set()
    chosen = []
    for r in good:
        a, b = r.pair.split("/")
        if a in used or b in used:
            continue
        chosen.append(r)
        used |= {a, b}
        if len(chosen) == top:
            break
    return chosen


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def fetch_alpaca_many(cfg: Settings, symbols: list[str], start: str, end: str,
                      cache_dir: str | Path = "data") -> pd.DataFrame:
    """Adjusted closes for many symbols, cached as one CSV per request."""
    key = "-".join(sorted(symbols))
    cache = Path(cache_dir) / f"scan_{abs(hash(key)) % 10**8}_{cfg.timeframe_minutes}m_{start}_{end}_{cfg.data_feed}.csv"
    if cache.exists():
        df = pd.read_csv(cache, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True)
        if set(symbols) <= set(df.columns):
            return df
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    client = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
    req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame(cfg.timeframe_minutes, TimeFrameUnit.Minute),
                           start=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
                           end=datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
                           adjustment=Adjustment.ALL,
                           feed=DataFeed.IEX if cfg.data_feed == "iex" else DataFeed.SIP)
    df = client.get_stock_bars(req).df
    if df.empty:
        raise ValueError("Alpaca returned no bars for that period")
    closes = df["close"].unstack(level="symbol").sort_index()
    cache.parent.mkdir(parents=True, exist_ok=True)
    closes.to_csv(cache)
    return closes


def sim_universe(bars: int) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    """Fake sector with a known answer: two real pairs, one that breaks halfway, one that never was."""
    from sim import SimMarket

    markets = [SimMarket("AAA", "BBB", seed=11), SimMarket("CCC", "DDD", seed=18, theta=0.03),
               SimMarket("EEE", "FFF", seed=25, break_at=int(bars * 0.6)),
               SimMarket("GGG", "HHH", seed=32, theta=0.0)]
    for m in markets:
        m.warm_up(bars)
    closes = pd.concat([m.closes(bars) for m in markets], axis=1)
    return closes, {"sim": list(closes.columns)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> list[ScanRow]:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--source", choices=("sim", "csv", "alpaca"), default="sim")
    p.add_argument("--csv", help="CSV with a timestamp column and one close column per symbol")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--bars", type=int, default=6000, help="bars per symbol for --source sim")
    p.add_argument("--universe", nargs="+", choices=sorted(UNIVERSES), help="sectors to scan (default: all)")
    p.add_argument("--timeframe", type=int, help="bar size in minutes")
    p.add_argument("--hedge", choices=("ols", "kalman"))
    p.add_argument("--formation", type=float, default=0.6, help="share of history used to choose pairs")
    p.add_argument("--max-p", type=float, default=0.05, help="max cointegration p-value in formation")
    p.add_argument("--min-trades", type=int, default=10, help="min out-of-sample trades to recommend a pair")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--spread-bps", type=float, default=Costs().spread_bps)
    p.add_argument("--out", default="scan_out")
    args = p.parse_args(argv)

    overrides: dict[str, object] = {}
    if args.timeframe:
        overrides["timeframe_minutes"] = args.timeframe
    if args.hedge:
        overrides["hedge_method"] = args.hedge
    base = load_settings(mode="alpaca" if args.source == "alpaca" else "sim", pairs=(), **overrides)
    sectors = {k: UNIVERSES[k] for k in (args.universe or UNIVERSES)}

    if args.source == "sim":
        closes, sectors = sim_universe(args.bars)
    elif args.source == "csv":
        if not args.csv:
            p.error("--source csv needs --csv PATH")
        closes = pd.read_csv(args.csv, index_col=0)
        closes.index = pd.to_datetime(closes.index, utc=True)
        if not args.universe:                         # scan every column against every other
            sectors = {"csv": list(closes.columns)}
    else:
        if not (args.start and args.end):
            p.error("--source alpaca needs --start and --end")
        closes = fetch_alpaca_many(base, sorted({s for v in sectors.values() for s in v}), args.start, args.end)

    print(f"Scanning {sum(math.comb(len(v), 2) for v in sectors.values())} pairs; "
          f"choosing on the first {args.formation:.0%} of {len(closes)} bars, testing on the rest\n")
    rows = scan(closes, sectors, base, Costs(spread_bps=args.spread_bps), formation=args.formation,
                max_p=args.max_p, all_hours=args.source == "sim")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([r.__dict__ for r in rows]).sort_values("out_net", ascending=False).to_csv(out / "scan.csv", index=False)

    chosen = pick(rows, args.top, args.min_trades)
    print(f"\n{sum(r.passed_filter for r in rows)} of {len(rows)} pairs passed the formation filter; "
          f"{len(chosen)} made money out of sample with at least {args.min_trades} trades.")
    if chosen:
        print("\nRecommended (paper-trade these first):")
        for r in chosen:
            print(f"  {r.pair:<10} out-of-sample ${r.out_net:,.2f} over {r.out_trades} trades, "
                  f"break-even {r.out_breakeven_bps} bps, chance of loss {r.out_prob_loss}")
        print("\nPut this line in .env:\nPAIRS=" + ",".join(r.pair.replace("/", ":") for r in chosen))
    else:
        print("Nothing to recommend. That's a real answer: don't force a pair that doesn't hold up.")
    print(f"\nAll results: {out / 'scan.csv'}")
    return rows


if __name__ == "__main__":
    main()
