"""When was each market good to trade? A look back over the last week (or any number of days).

    python timing.py                                   # report from what the watcher logged
    python timing.py --days 14
    python timing.py --replay --universe crypto        # no week of logs yet: replay the last 7 days of
                                                       # real prices through the watcher (crypto needs no keys)
    python timing.py --replay                          # every market (stocks and funds need Alpaca keys)
    python timing.py --replay --source sim             # offline demo

A replay of every market takes roughly 10-20 minutes (it grades every pair at
every 15-minute bar of the week); crypto alone is faster.

The report answers, per market (US stocks, world stocks, bonds, gold & metals,
oil, currencies, crypto):

* **Best hours** (your local time): the hours when its pairs were healthy and
  gave clean signals, and how the demo trades entered in that hour did after costs.
* **Best days** of the week, the same way.
* **Entries you could have taken**: each STRONG signal, when it fired, and what
  the demo trade made or lost after costs.

Everything here is measured on the past, on few trades. It shows when
conditions *were* good, which is a hint for where to look, not a promise that
the same hours will work next week. The weekly report also goes to your phone
on Friday after the US close, and Jarvis reads it when you ask "when was the
best time to trade this week?".
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import numpy as np
import pandas as pd

from insights import NY
from scanner import ALL_SECTORS, CRYPTO, MARKET_OF

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MIN_SCANS = 4            # an hour needs this many scans of a market before it can rank


def _local_tz() -> tzinfo:
    return datetime.now().astimezone().tzinfo


def _tz_name(tz: tzinfo) -> str:
    return getattr(tz, "key", None) or datetime.now(tz).tzname() or "local time"


def _read(path: str, since: float) -> tuple[list[tuple], list[tuple]]:
    if not Path(path).exists():
        raise FileNotFoundError(f"no bot database at {path}; start the watcher first")
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        opps = conn.execute("SELECT bar_time, pair, sector, grade, zscore FROM opportunities WHERE bar_time >= ?",
                            (since,)).fetchall()
        trades = conn.execute("SELECT pair, grade, opened_at, closed_at, net_pnl FROM shadow_trades "
                              "WHERE opened_at >= ?", (since,)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return [], []
        raise
    finally:
        conn.close()
    return opps, trades


class _Cell:
    __slots__ = ("scans", "healthy", "strong", "trades", "wins", "net")

    def __init__(self) -> None:
        self.scans = self.healthy = self.strong = self.trades = self.wins = 0
        self.net = 0.0

    @property
    def score(self) -> float:
        """Healthy share, plus clean signals, plus what the demo trades actually made (capped so a lucky trade can't dominate)."""
        share = self.healthy / self.scans if self.scans else 0.0
        signals = min(self.strong / max(self.scans, 1) * 20, 0.5)
        money = float(np.clip(self.net / 50.0, -0.5, 0.5)) if self.trades else 0.0
        return share + signals + money

    def text(self) -> str:
        share = self.healthy / self.scans if self.scans else 0.0
        parts = [f"{share:.0%} of pairs healthy"]
        if self.strong:
            parts.append(f"{self.strong} strong signal{'s' if self.strong != 1 else ''}")
        if self.trades:
            parts.append(f"{self.trades} demo trade{'s' if self.trades != 1 else ''}, {self.wins} won, "
                         f"net ${self.net:+,.2f}")
        return ", ".join(parts)


def timing_report(path: str, days: int = 7, tz: tzinfo | None = None, now: float | None = None,
                  max_entries: int = 8) -> str:
    """The look-back report as plain text."""
    tz = tz or _local_tz()
    now = now or time.time()
    since = now - days * 86_400
    opps, trades = _read(path, since)
    if not opps:
        return (f"Nothing logged in the last {days} days. Leave the watcher running (python watcher.py "
                "--source alpaca) or replay last week now: python timing.py --replay --universe crypto")

    sector_of: dict[str, str] = {}
    hours: dict[str, dict[int, _Cell]] = defaultdict(lambda: defaultdict(_Cell))
    weekdays: dict[str, dict[int, _Cell]] = defaultdict(lambda: defaultdict(_Cell))
    first_ts, last_ts = min(o[0] for o in opps), max(o[0] for o in opps)
    for bar_time, pair, sector, grade, _z in opps:
        sector_of[pair] = sector or "?"
        market = MARKET_OF.get(sector, sector or "?")
        local = datetime.fromtimestamp(bar_time, tz)
        for cell in (hours[market][local.hour], weekdays[market][local.weekday()]):
            cell.scans += 1
            cell.healthy += grade in ("STRONG", "WATCH")
            cell.strong += grade == "STRONG"

    closed = [t for t in trades if t[3] is not None and t[4] is not None]
    for pair, _grade, opened_at, _closed_at, net in closed:
        market = MARKET_OF.get(sector_of.get(pair, ""), sector_of.get(pair, "?"))
        local = datetime.fromtimestamp(opened_at, tz)
        for cell in (hours[market][local.hour], weekdays[market][local.weekday()]):
            cell.trades += 1
            cell.wins += net > 0
            cell.net += net

    span_days = (last_ts - first_ts) / 86_400
    out = [f"When the markets were good, last {days} days ({_tz_name(tz)} time; "
           f"logs cover {span_days:.1f} days)."]
    ranked = []
    for market, by_hour in hours.items():
        usable = {h: c for h, c in by_hour.items() if c.scans >= MIN_SCANS}
        if not usable:
            continue
        total = _Cell()
        for c in by_hour.values():
            for k in _Cell.__slots__:
                setattr(total, k, getattr(total, k) + getattr(c, k))
        ranked.append((total.score, market, usable, total))
    ranked.sort(key=lambda r: -r[0])
    for i, (_s, market, usable, total) in enumerate(ranked, 1):
        best = sorted(usable.items(), key=lambda kv: -kv[1].score)[:max(1, min(3, len(usable) // 2))]
        worst = min(usable.items(), key=lambda kv: kv[1].score)
        line = (f"{i}. {market} ({total.text()}). Best hours: "
                + "; ".join(f"{h:02d}:00-{(h + 1) % 24:02d}:00 ({c.text()})" for h, c in best))
        if worst[0] not in dict(best):
            line += f". Weakest: {worst[0]:02d}:00 ({worst[1].text()})"
        wd = {d: c for d, c in weekdays[market].items() if c.scans >= MIN_SCANS}
        if len(wd) > 1:
            d, c = max(wd.items(), key=lambda kv: kv[1].score)
            line += f". Best day: {DAYS[d]}"
        out.append(line + ".")

    by_open = {(p, round(o)): (c, n) for p, _g, o, c, n in trades}
    strong = sorted({(o[0], o[1]): o for o in opps if o[3] == "STRONG"}.values(), key=lambda o: o[0])
    entries, seen = [], set()
    for bar_time, pair, sector, _g, z in strong:
        episode = (pair, int(bar_time // 7200))          # one entry per pair per 2 hours
        if episode in seen:
            continue
        seen.add(episode)
        when = datetime.fromtimestamp(bar_time, tz).strftime("%a %H:%M")
        result = by_open.get((pair, round(bar_time)))
        if result is None:
            outcome = "no demo trade (already in one)"
        elif result[0] is None:
            outcome = "demo trade still open"
        else:
            held = (result[0] - bar_time) / 3600
            outcome = f"demo trade {'made' if result[1] > 0 else 'lost'} ${abs(result[1]):,.2f} in {held:.1f}h"
        entries.append(f"{when} {pair} (z {z:+.1f}): {outcome}")
    if entries:
        shown = entries[-max_entries:]
        out.append(f"Entries you could have taken ({len(entries)} STRONG signals"
                   + (f", last {len(shown)} shown" if len(shown) < len(entries) else "") + "): " + "; ".join(shown) + ".")
    else:
        out.append("No STRONG signals in this period: no clean entry would have passed every check.")
    if len(closed) < 30:
        out.append(f"Only {len(closed)} demo trades closed: a hint for where to look, not proof.")
    out.append("This is what happened, not a forecast.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Replay: run the watcher over the last N days of real prices, bar by bar
# ---------------------------------------------------------------------------
class ReplaySource:
    """Feeds the watcher historical prices one bar at a time, opening and closing markets like the real clock."""

    def __init__(self, stocks: pd.DataFrame, crypto: pd.DataFrame, sectors: dict[str, list[str]], start: float):
        from backtest import regular_session

        self.sectors = sectors
        self.stock_sectors = {k: v for k, v in sectors.items() if k not in CRYPTO}
        self.crypto_sectors = {k: v for k, v in sectors.items() if k in CRYPTO}
        self.stocks, self.crypto = stocks, crypto
        stock_steps = stocks.index[regular_session(stocks.index)] if not stocks.empty else stocks.index
        steps = stock_steps.union(crypto.index) if not crypto.empty else stock_steps
        self.steps = [t for t in steps if t.timestamp() >= start]
        self._stock_times = set(stock_steps)
        self.t: pd.Timestamp | None = None

    @property
    def stocks_open(self) -> bool:
        return self.t in self._stock_times

    @property
    def active_sectors(self) -> dict[str, list[str]]:
        return {**(self.stock_sectors if self.stocks_open else {}), **self.crypto_sectors}

    @property
    def closed_markets(self) -> list[str]:
        return [] if self.stocks_open else sorted({MARKET_OF.get(k, k) for k in self.stock_sectors})

    async def market_open(self) -> bool:
        return True

    async def closes(self, bars: int) -> pd.DataFrame:
        frames = [f.loc[:self.t].tail(bars) for f in (self.stocks, self.crypto) if not f.empty]
        return pd.concat(frames, axis=1).sort_index() if frames else pd.DataFrame()

    def now(self) -> float:
        return self.t.timestamp()


def _fetch_history(cfg, sectors: dict[str, list[str]], days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    from watcher import AlpacaSource

    src = AlpacaSource(cfg, sectors)
    stocks = crypto = pd.DataFrame()
    if src.stock_sectors:          # watcher history (40 trading days) + the replay window
        stocks = src._fetch_stocks((40 + days) * round(390 / cfg.timeframe_minutes))
    if src.crypto_sectors:
        crypto = src._fetch_crypto((40 + days) * round(1440 / cfg.timeframe_minutes))
    return stocks, crypto


def _sim_history(days: int, timeframe: int) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, list[str]]]:
    from sim import SimMarket

    bars = (40 + days) * round(1440 / timeframe)
    markets = [SimMarket("AAA", "BBB", seed=11, bar_seconds=timeframe * 60),
               SimMarket("CCC", "DDD", seed=18, theta=0.03, bar_seconds=timeframe * 60),
               SimMarket("EEE", "FFF", seed=25, break_at=bars - 300, bar_seconds=timeframe * 60)]
    for m in markets:
        m.warm_up(bars)
    crypto = pd.concat([m.closes(bars) for m in markets], axis=1)
    return pd.DataFrame(), crypto, {"crypto": list(crypto.columns)}


def _history_job(args):
    from watcher import history_of
    return history_of(*args)


async def _prefill_history(watcher, closes: pd.DataFrame, now: float) -> None:
    """Each pair's track record before the replay starts, on all CPU cores (the slow part of a replay)."""
    from concurrent.futures import ProcessPoolExecutor

    jobs = {}
    for (a, b), sector in watcher.pairs(every=True):
        if a in closes and b in closes:
            df = closes[[a, b]].dropna()
            if len(df) >= watcher.cfg.hedge_lookback:
                jobs[f"{a}/{b}"] = (df, watcher.cfg.for_pair(a, b), watcher.costs_for(sector),
                                    watcher.all_hours or sector in CRYPTO)
    log = logging.getLogger("timing")
    log.info("measuring the track record of %d pairs before the replay (uses every CPU core)", len(jobs))
    loop = asyncio.get_running_loop()
    with ProcessPoolExecutor() as pool:
        results = await asyncio.gather(*(loop.run_in_executor(pool, _history_job, args) for args in jobs.values()))
    for name, hist in zip(jobs, results):
        watcher._history[name] = (now, hist)


async def replay(days: int, sectors: dict[str, list[str]], db_path: str, source: str = "alpaca",
                 timeframe: int = 15, quiet: bool = True, review_dir: str | Path | None = None) -> str:
    """Replay the last ``days`` days into a fresh database and return the report."""
    from backtest import Costs
    from config import load_settings
    from db_logger import DBLogger
    from notify import Notifier
    from watcher import Watcher, watch_settings

    crypto_only = set(sectors) <= set(CRYPTO)
    base = load_settings(mode="alpaca" if source == "alpaca" and not crypto_only else "sim")
    cfg = watch_settings(base, timeframe)
    if source == "sim":
        stocks, crypto, sectors = _sim_history(days, timeframe)
    else:
        stocks, crypto = await asyncio.to_thread(_fetch_history, cfg, sectors, days)
    last = max([f.index[-1] for f in (stocks, crypto) if not f.empty], default=None)
    if last is None:
        raise SystemExit("no prices came back for that period")
    start = last.timestamp() - days * 86_400
    src = ReplaySource(stocks, crypto, sectors, start)
    Path(db_path).unlink(missing_ok=True)
    db = DBLogger(db_path)
    await db.open()
    try:
        # each pair's track record is measured once, on the 40 days before the replay (fast, and no peeking ahead)
        watcher = Watcher(cfg, src, db, costs=Costs(), refresh_seconds=10 ** 9, all_hours=False,
                          history_bars=40 * round(390 / timeframe), notifier=Notifier(prefix="replay"))
        if quiet:
            logging.getLogger("notify").setLevel(logging.WARNING)
        if src.steps:
            src.t = src.steps[0]
            await _prefill_history(watcher, await src.closes(watcher.history_bars), src.t.timestamp())
        for i, t in enumerate(src.steps, 1):
            src.t = t
            await watcher.scan()
            if i == len(src.steps):
                await watcher.long_book.close_all(await src.closes(watcher.history_bars))
            if i % 100 == 0:
                logging.getLogger("timing").info("replayed %d of %d bars (%s)", i, len(src.steps),
                                                 t.tz_convert(NY).strftime("%a %d %b %H:%M New York"))
    finally:
        await db.close()
    report = timing_report(db_path, days=days, now=last.timestamp() + 1)
    if review_dir:
        from review import compare_rules, write_review
        variants = await asyncio.to_thread(compare_rules, stocks, crypto, src.sectors, cfg, start, watcher.costs_for)
        path = write_review(db_path, review_dir, variants, title=f"Replay review, last {days} days")
        report += f"\n\nDetailed review (every entry, wins and losses, other rule sets): {path}"
    return report


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="When was each market good to trade, over the last days")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--replay", action="store_true", help="replay past prices now instead of reading the logs")
    ap.add_argument("--source", choices=("alpaca", "sim"), default="alpaca", help="replay prices from")
    ap.add_argument("--universe", nargs="+", choices=sorted(ALL_SECTORS), help="markets to replay (default: all)")
    ap.add_argument("--db", help="database to read (default: the bot's)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    if args.replay:
        sectors = {k: ALL_SECTORS[k] for k in (args.universe or ALL_SECTORS)}
        db = args.db or str(Path(__file__).with_name("replay.db"))
        print(asyncio.run(replay(args.days, sectors, db, args.source,
                                 review_dir=Path(__file__).with_name("review"))))
        print(f"\n(replay saved in {db}; your live logs were not touched)")
    else:
        from mcp_server import db_path
        print(timing_report(args.db or db_path(), days=args.days))


if __name__ == "__main__":
    main()
