"""Market watcher: grades every pair all day and tracks every signal with demo money.

    python watcher.py --source sim --fast --scans 500     # offline demo
    python watcher.py --source alpaca                      # all day on real prices (keys in .env)

What it watches
---------------
US stock sectors, plus US-listed funds for world stocks, bonds, gold and
silver, oil and currencies (scanner.UNIVERSES), and crypto (scanner.CRYPTO),
all from the same free Alpaca paper account. Stocks and funds are scanned
while the US market is open; crypto trades 24/7, so it is scanned at night
and on weekends too.

What it does, every bar (15 minutes by default)
------------------------------------------------
1. Downloads recent prices for every symbol it watches.
2. For every pair in each sector it computes the live z-score, cointegration,
   half-life and volatility, and looks back over the last ~40 trading days to
   see how this exact rule set did on that pair after costs (the backtester).
3. It gives each pair a grade:

   ======  ======================================================================
   STRONG  a signal now (|z| past the entry line) on a pair that passes every check
           and won at least 60% of its recent trades
   RISKY   a signal now, with a soft warning: weak cointegration, a volatility
           spike, slow reversion, an earnings day, few or losing past trades
   AVOID   a signal now on a pair that is broken: not cointegrated, negative
           hedge ratio, no mean reversion, or already past the stop line
   WATCH   a good pair that isn't at its entry line yet
   QUIET   nothing to do
   ======  ======================================================================

4. It opens a **shadow trade** (demo money, no orders sent anywhere) on every
   STRONG, RISKY and AVOID signal, closes it by the same exit rules as the live
   bot, and charges the same costs as the backtester. AVOID trades are the
   control group: if they do as well as STRONG ones, the filters are useless.

After a few weeks the dashboard answers the questions that matter: do STRONG
signals really beat RISKY and AVOID ones? At what time of day do signals work best?
Which pairs earn their place in ``PAIRS``?

The "probability" is a measured win rate, not a prediction
-------------------------------------------------------------
``prob`` is the pair's recent win rate, shrunk toward 50% when there are few
trades: (wins + 1) / (trades + 2). It says how often this setup worked lately,
on this pair, after costs. It can't know the future, and the shadow book is
there precisely to check whether it holds up.

It never sends orders. The trading bot (main.py) trades only the pairs in
``PAIRS``; the watcher tells you which pairs deserve to be there.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import itertools
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from backtest import Costs, run_backtest
from blackout import Blackout
from config import ConfigError, Settings, load_settings
from db_logger import DBLogger
from execution import pair_pnl, plan_pair
from insights import Insights
from longonly import LongBook
from notify import Notifier
from scanner import ALL_SECTORS, CRYPTO, MARKET_OF, alpaca_crypto
from strategy import Action, Position, compute_spread_stats, decide

log = logging.getLogger("watcher")
NY = ZoneInfo("America/New_York")

GRADES = ("STRONG", "RISKY", "AVOID", "WATCH", "QUIET")
TRADED = ("STRONG", "RISKY", "AVOID")          # grades the shadow book trades
MIN_HISTORY_TRADES = 5
# Alpaca crypto: ~0.25% taker fee plus spread per order, no SEC/FINRA fees, no shorting (so no borrow)
CRYPTO_COSTS = Costs(spread_bps=30.0, sec_fee_rate=0.0, taf_per_share=0.0, taf_max=0.0, borrow_rate_annual=0.0)


@dataclass
class Opportunity:
    pair: str
    sector: str
    bar_time: float
    grade: str
    side: str | None
    zscore: float
    coint_pvalue: float
    half_life: float
    vol_ratio: float
    beta: float
    hist_trades: int
    hist_win_rate: float | None
    prob: float
    hist_avg_net: float | None
    breakeven_bps: float | None
    flags: list[str] = field(default_factory=list)
    price_a: float = 0.0
    price_b: float = 0.0
    spread_std: float = 0.0


@dataclass
class History:
    trades: int
    wins: int
    avg_net: float | None
    breakeven_bps: float | None

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.trades if self.trades else None

    @property
    def prob(self) -> float:
        return (self.wins + 1) / (self.trades + 2)      # Laplace: few trades -> close to 50%


def watch_settings(base: Settings, timeframe: int) -> Settings:
    """Lookbacks in bars scaled for the watcher's bar size (defaults are for 15-minute bars)."""
    per_day = max(1, round(390 / timeframe))
    return dataclasses.replace(base, timeframe_minutes=timeframe, hedge_lookback=10 * per_day,
                               z_lookback=2 * per_day, max_holding_bars=5 * per_day, pairs=())


def history_of(closes: pd.DataFrame, cfg: Settings, costs: Costs, all_hours: bool) -> History:
    if len(closes) <= cfg.hedge_lookback + 10:
        return History(0, 0, None, None)
    s = run_backtest(closes, cfg, costs, all_hours=all_hours).summary()
    n = s["trades"]
    wins = round((s["win_rate"] or 0) * n)
    return History(n, wins, s["avg_net_per_trade"], s["breakeven_cost_bps"])


def assess(pair: tuple[str, str], sector: str, closes: pd.DataFrame, cfg: Settings, hist: History,
           blackout_reason: str | None = None, min_prob: float = 0.6) -> Opportunity:
    """Grade one pair on its latest bar. ``closes``: aligned closes for the two symbols."""
    a, b = pair
    pcfg = cfg.for_pair(a, b)
    stats = compute_spread_stats(closes[[a, b]].dropna().tail(pcfg.hedge_lookback), pcfg)
    hard: list[str] = []                       # the pair is broken
    soft: list[str] = []                       # the pair works, but something is off
    p = stats.coint_pvalue
    if not p < 2 * pcfg.min_coint_pvalue:
        hard.append(f"not cointegrated now (p={p:.2f})")
    elif not p < pcfg.min_coint_pvalue:
        soft.append(f"weakly cointegrated (p={p:.2f})")
    if stats.beta <= 0:
        hard.append("hedge ratio <= 0")
    if not math.isfinite(stats.half_life):
        hard.append("doesn't revert")
    elif stats.half_life > pcfg.max_holding_bars / 2:
        soft.append(f"reverts slowly (half-life {stats.half_life:.0f} bars)")
    if stats.vol_ratio > pcfg.max_vol_ratio:
        soft.append(f"volatility spike ({stats.vol_ratio:.1f}x normal)")
    if blackout_reason:
        soft.append(f"event: {blackout_reason}")
    if sector in CRYPTO and abs(stats.zscore) >= pcfg.entry_z:
        soft.append("Alpaca can't short crypto: demo only")
    if hist.trades < MIN_HISTORY_TRADES:
        soft.append(f"only {hist.trades} past trades")
    elif hist.avg_net is not None and hist.avg_net <= 0:
        soft.append("lost money recently after costs")
    elif hist.prob < min_prob:
        soft.append(f"won only {hist.win_rate:.0%} recently")

    z = stats.zscore
    side = None
    if abs(z) >= pcfg.entry_z:
        side = str(Position.SHORT_SPREAD if z > 0 else Position.LONG_SPREAD)
        if abs(z) >= pcfg.stop_z:
            hard.append(f"past the stop line (|z|={abs(z):.1f})")

    flags = hard + soft
    if side:
        grade = "AVOID" if hard else "RISKY" if soft else "STRONG"
    else:
        grade = "WATCH" if not hard and not soft else "QUIET"
    return Opportunity(f"{a}/{b}", sector, stats.bar_time, grade, side, z, stats.coint_pvalue,
                       stats.half_life, stats.vol_ratio, stats.beta, hist.trades, hist.win_rate,
                       round(hist.prob, 3), hist.avg_net, hist.breakeven_bps, flags,
                       stats.price_a, stats.price_b, stats.spread_std)


# ---------------------------------------------------------------------------
# Shadow book: demo-money trades on every STRONG and RISKY signal
# ---------------------------------------------------------------------------
@dataclass
class Shadow:
    id: int | None
    pair: str
    direction: Position
    qty_a: int
    qty_b: int
    entry_a: float
    entry_b: float
    opened_at: float
    costs: float
    bars_held: int = 0


class ShadowBook:
    def __init__(self, db: DBLogger, cfg: Settings, costs: Costs) -> None:
        self.db, self.cfg, self.costs = db, cfg, costs
        self.open: dict[str, Shadow] = {}

    async def load(self) -> None:
        rows = await self.db.fetchall("SELECT id, pair, direction, qty_a, qty_b, entry_a, entry_b, opened_at "
                                      "FROM shadow_trades WHERE closed_at IS NULL")
        for i, pair, d, qa, qb, ea, eb, at in rows:
            self.open[pair] = Shadow(i, pair, Position(d), int(qa), int(qb), ea, eb, at, 0.0)

    def _order_costs(self, qty_a: int, qty_b: int, pa: float, pb: float, sides: tuple[str, str],
                     costs: Costs) -> float:
        return sum(sum(costs.order_cost(q, p, s)) for q, p, s in ((qty_a, pa, sides[0]), (qty_b, pb, sides[1])))

    async def update(self, opp: Opportunity, stats_closes: pd.DataFrame, costs: Costs | None = None) -> str | None:
        """Advance this pair's shadow position by one bar. Returns "opened"/"closed"/None."""
        costs = costs or self.costs
        a, b = opp.pair.split("/")
        pcfg = self.cfg.for_pair(a, b)
        sh = self.open.get(opp.pair)
        if sh is not None:
            sh.bars_held += 1
            stats = compute_spread_stats(stats_closes[[a, b]].dropna().tail(pcfg.hedge_lookback), pcfg,
                                         with_coint=False)
            action, note = decide(sh.direction, stats, sh.bars_held, pcfg)
            if action is not Action.CLOSE:
                return None
            long_a = sh.direction is Position.LONG_SPREAD
            exit_costs = self._order_costs(sh.qty_a, sh.qty_b, stats.price_a, stats.price_b,
                                           ("sell", "buy") if long_a else ("buy", "sell"), costs)
            short_value = sh.qty_b * sh.entry_b if long_a else sh.qty_a * sh.entry_a
            borrow = costs.borrow_cost(short_value, stats.bar_time - sh.opened_at)
            gross = pair_pnl(sh.direction, sh.qty_a, sh.qty_b, sh.entry_a, sh.entry_b, stats.price_a, stats.price_b)
            total_costs = sh.costs + exit_costs + borrow
            await self.db._write(
                "UPDATE shadow_trades SET closed_at=?, exit_z=?, exit_a=?, exit_b=?, gross_pnl=?, costs=?, "
                "net_pnl=?, exit_reason=? WHERE id=?",
                (stats.bar_time, stats.zscore, stats.price_a, stats.price_b, round(gross, 2),
                 round(total_costs, 2), round(gross - total_costs, 2), note, sh.id))
            del self.open[opp.pair]
            return "closed"
        if opp.grade not in TRADED or not opp.side or abs(opp.zscore) >= pcfg.stop_z:
            return None
        direction = Position(opp.side)
        from strategy import SpreadStats
        st = SpreadStats(opp.bar_time, opp.price_a, opp.price_b, 0, opp.beta, 0, 0, opp.spread_std,
                         opp.zscore, opp.coint_pvalue, opp.half_life, opp.vol_ratio)
        try:
            plan = plan_pair(direction, st, pcfg)
        except ValueError:
            return None
        entry_costs = self._order_costs(plan.qty_a, plan.qty_b, opp.price_a, opp.price_b, (plan.side_a, plan.side_b), costs)
        hour = datetime.fromtimestamp(opp.bar_time, NY).hour
        trade_id = await self.db._write(
            "INSERT INTO shadow_trades (pair, grade, flags, direction, opened_at, entry_hour, entry_z, prob, "
            "qty_a, qty_b, entry_a, entry_b) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (opp.pair, opp.grade, "; ".join(opp.flags), str(direction), opp.bar_time, hour, opp.zscore,
             opp.prob, plan.qty_a, plan.qty_b, opp.price_a, opp.price_b))
        self.open[opp.pair] = Shadow(trade_id, opp.pair, direction, plan.qty_a, plan.qty_b,
                                     opp.price_a, opp.price_b, opp.bar_time, entry_costs)
        return "opened"


# ---------------------------------------------------------------------------
# Price sources
# ---------------------------------------------------------------------------
class SimSource:
    """Fake sectors with known answers, stepped one bar per scan."""

    def __init__(self, warm: int) -> None:
        from sim import SimMarket

        self.markets = [SimMarket("AAA", "BBB", seed=11, bar_seconds=900),
                        SimMarket("CCC", "DDD", seed=18, theta=0.03, bar_seconds=900),
                        SimMarket("EEE", "FFF", seed=25, break_at=warm + 300, bar_seconds=900),
                        SimMarket("GGG", "HHH", seed=32, theta=0.0, bar_seconds=900)]
        for m in self.markets:
            m.warm_up(warm)
        self.sectors = {"sim": ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]}
        self.active_sectors, self.closed_markets = self.sectors, []

    async def market_open(self) -> bool:
        return True

    async def closes(self, bars: int) -> pd.DataFrame:
        for m in self.markets:
            m.step()
        return pd.concat([m.closes(bars) for m in self.markets], axis=1)

    def now(self) -> float:
        return self.markets[0].last[0]


class AlpacaSource:
    """Stocks and funds from Alpaca's stock feed (US hours), crypto from its crypto feed (24/7)."""

    def __init__(self, cfg: Settings, sectors: dict[str, list[str]]) -> None:
        from alpaca.data.enums import DataFeed
        from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self.cfg, self.sectors = cfg, sectors
        self.stock_sectors = {k: v for k, v in sectors.items() if k not in CRYPTO}
        self.crypto_sectors = {k: v for k, v in sectors.items() if k in CRYPTO}
        if self.stock_sectors:                         # stock prices and the market clock need keys
            self._data = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
            self._trading = TradingClient(cfg.api_key, cfg.secret_key, paper=True)
        keys = (cfg.api_key, cfg.secret_key) if cfg.api_key and cfg.secret_key else (None, None)
        self._crypto = CryptoHistoricalDataClient(*keys)  # crypto prices work without keys
        self._feed = DataFeed.IEX if cfg.data_feed == "iex" else DataFeed.SIP
        self.stocks_open = False
        self._stock_cache: pd.DataFrame | None = None     # last session's prices while the US market is closed

    @property
    def active_sectors(self) -> dict[str, list[str]]:
        """Sectors trading right now: crypto always, stocks and funds only while the US market is open."""
        return {**(self.stock_sectors if self.stocks_open else {}), **self.crypto_sectors}

    @property
    def closed_markets(self) -> list[str]:
        if self.stocks_open:
            return []
        return sorted({MARKET_OF.get(k, k) for k in self.stock_sectors})

    async def market_open(self) -> bool:
        if self.stock_sectors:
            clock = await asyncio.to_thread(self._trading.get_clock)
            self.stocks_open = bool(clock.is_open)
        return self.stocks_open or bool(self.crypto_sectors)

    def _fetch_stocks(self, bars: int) -> pd.DataFrame:
        from alpaca.data.enums import Adjustment
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        days = math.ceil(bars * self.cfg.timeframe_minutes / 390) + 1
        start = datetime.now(timezone.utc) - timedelta(days=days * 7 // 5 + 5)
        symbols = sorted({s for v in self.stock_sectors.values() for s in v})
        req = StockBarsRequest(symbol_or_symbols=symbols, start=start, adjustment=Adjustment.ALL, feed=self._feed,
                               timeframe=TimeFrame(self.cfg.timeframe_minutes, TimeFrameUnit.Minute))
        df = self._data.get_stock_bars(req).df
        return df["close"].unstack(level="symbol").sort_index().tail(bars) if not df.empty else pd.DataFrame()

    def _fetch_crypto(self, bars: int) -> pd.DataFrame:
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        days = math.ceil(bars * self.cfg.timeframe_minutes / 1440) + 2
        symbols = sorted({alpaca_crypto(s) for v in self.crypto_sectors.values() for s in v})
        req = CryptoBarsRequest(symbol_or_symbols=symbols, start=datetime.now(timezone.utc) - timedelta(days=days),
                                timeframe=TimeFrame(self.cfg.timeframe_minutes, TimeFrameUnit.Minute))
        df = self._crypto.get_crypto_bars(req).df
        if df.empty:
            return pd.DataFrame()
        closes = df["close"].unstack(level="symbol").sort_index().tail(bars)
        return closes.rename(columns=lambda c: c.replace("/", ""))

    def _fetch(self, bars: int) -> pd.DataFrame:
        frames = []
        if self.stock_sectors:
            if self.stocks_open or self._stock_cache is None:
                try:
                    self._stock_cache = self._fetch_stocks(bars)
                except Exception as exc:          # one feed failing must not stop the other
                    log.warning("stock prices unavailable: %s", exc)
            if self._stock_cache is not None:
                frames.append(self._stock_cache)
        if self.crypto_sectors:
            try:
                frames.append(self._fetch_crypto(bars))
            except Exception as exc:
                log.warning("crypto prices unavailable: %s", exc)
        frames = [f for f in frames if not f.empty]
        return pd.concat(frames, axis=1).sort_index() if frames else pd.DataFrame()

    async def closes(self, bars: int) -> pd.DataFrame:
        return await asyncio.to_thread(self._fetch, bars)

    def now(self) -> float:
        return time.time()


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
class Watcher:
    def __init__(self, cfg: Settings, source, db: DBLogger, *, costs: Costs = Costs(), history_bars: int = 1560,
                 refresh_seconds: float = 86_400, all_hours: bool = False, notifier: Notifier | None = None,
                 blackout: Blackout | None = None, min_prob: float = 0.6,
                 watched_pairs: set[str] | None = None) -> None:
        self.cfg, self.source, self.db, self.costs = cfg, source, db, costs
        self.history_bars, self.refresh_seconds, self.all_hours = history_bars, refresh_seconds, all_hours
        self.notifier = notifier or Notifier(prefix="watcher")
        self.blackout = blackout or Blackout(None)
        self.min_prob = min_prob
        self.book = ShadowBook(db, cfg, costs)
        self.long_book = LongBook(db)           # buy-only strategies on crypto and gold, demo money, to compare
        self._history: dict[str, tuple[float, History]] = {}
        self._last_grade: dict[str, str] = {}
        self.insights = Insights({f"{a}/{b}" for a, b in (cfg.pairs or ())} | set(watched_pairs or ()))
        self.last_read = None

    def pairs(self, every: bool = False) -> list[tuple[tuple[str, str], str]]:
        """Pairs to scan now (only markets that are open), or every watched pair."""
        sectors = self.source.sectors if every else getattr(self.source, "active_sectors", self.source.sectors)
        return [((a, b), sector) for sector, syms in sectors.items()
                for a, b in itertools.combinations(syms, 2)]

    def costs_for(self, sector: str) -> Costs:
        return CRYPTO_COSTS if sector in CRYPTO else self.costs

    async def scan(self) -> list[Opportunity]:
        closes = await self.source.closes(self.history_bars)
        now = self.source.now()
        out: list[Opportunity] = []
        for (a, b), sector in self.pairs():
            if a not in closes or b not in closes:
                continue
            df = closes[[a, b]].dropna()
            if len(df) < self.cfg.hedge_lookback:
                continue
            name = f"{a}/{b}"
            cached = self._history.get(name)
            if cached is None or now - cached[0] >= self.refresh_seconds:
                hist = await asyncio.to_thread(history_of, df, self.cfg.for_pair(a, b), self.costs_for(sector),
                                               self.all_hours or sector in CRYPTO)
                self._history[name] = (now, hist)
            hist = self._history[name][1]
            day = datetime.fromtimestamp(df.index[-1].timestamp(), NY).date()
            try:
                opp = assess((a, b), sector, df, self.cfg, hist, self.blackout.reason(day, (a, b)), self.min_prob)
            except ValueError as exc:
                log.debug("%s skipped: %s", name, exc)
                continue
            await self._record(opp)
            change = await self.book.update(opp, df, self.costs_for(sector))
            if opp.grade == "STRONG" and self._last_grade.get(name) != "STRONG":
                await self.notifier.send(f"STRONG {name} {opp.side} z={opp.zscore:+.2f}, recent win rate "
                                         f"{opp.prob:.0%} over {opp.hist_trades} trades (shadow trade {change or 'held'})")
            self._last_grade[name] = opp.grade
            out.append(opp)
        tradable = {s for syms in getattr(self.source, "active_sectors", self.source.sectors).values() for s in syms}
        for event in await self.long_book.update(closes, tradable):
            log.info("buy-only demo: %s", event)
        if out:
            await self._insights(out, closes, now)
        return out

    async def _insights(self, opps: list[Opportunity], closes: pd.DataFrame, now: float) -> None:
        read, alerts = self.insights.update(opps, closes, now, closed=getattr(self.source, "closed_markets", []))
        self.last_read = read
        slot = self.insights.brief_due(now)
        text = read.text(slot or "")
        await self.db._write("INSERT INTO market_notes (ts, kind, mood, text) VALUES (?,?,?,?)",
                             (now, "brief", read.mood, text))
        for alert in alerts:
            await self.db._write("INSERT INTO market_notes (ts, kind, mood, text) VALUES (?,?,?,?)",
                                 (now, "alert", read.mood, alert))
            await self.notifier.send(alert)
        if slot:
            await self.notifier.send(text)
            self.insights.mark_sent(now, slot)
            if slot == "close" and datetime.fromtimestamp(now, NY).weekday() == 4:    # Friday: the week in review
                from timing import timing_report
                try:
                    await self.notifier.send(await asyncio.to_thread(timing_report, self.db.path, 7, None, now))
                except Exception as exc:                                         # a report must not stop the watcher
                    log.warning("weekly timing report failed: %s", exc)

    async def _record(self, o: Opportunity) -> None:
        await self.db._write(
            "INSERT INTO opportunities (ts, bar_time, pair, sector, grade, side, zscore, coint_pvalue, half_life, "
            "vol_ratio, beta, hist_trades, hist_win_rate, prob, hist_avg_net, breakeven_bps, flags) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), o.bar_time, o.pair, o.sector, o.grade, o.side, o.zscore, o.coint_pvalue, o.half_life,
             o.vol_ratio, o.beta, o.hist_trades, o.hist_win_rate, o.prob, o.hist_avg_net, o.breakeven_bps,
             "; ".join(o.flags)))


def summarize(opps: list[Opportunity]) -> str:
    by = {g: [o for o in opps if o.grade == g] for g in GRADES}
    lines = [" | ".join(f"{g} {len(by[g])}" for g in GRADES)]
    for o in by["STRONG"] + by["RISKY"]:
        lines.append(f"  {o.grade:<6} {o.pair:<10} {o.side} z={o.zscore:+.2f} prob={o.prob:.0%} "
                     f"{'; '.join(o.flags)}")
    return "\n".join(lines)


async def amain(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="All-day market watcher with demo-money shadow trades")
    ap.add_argument("--source", choices=("sim", "alpaca"), default="sim")
    ap.add_argument("--universe", nargs="+", choices=sorted(ALL_SECTORS),
                    help="sectors to watch (default: all, including crypto and world funds)")
    ap.add_argument("--timeframe", type=int, default=15, help="bar size in minutes (default 15)")
    ap.add_argument("--scans", type=int, help="stop after N scans")
    ap.add_argument("--fast", action="store_true", help="sim only: no waiting between scans")
    ap.add_argument("--spread-bps", type=float, default=Costs().spread_bps)
    ap.add_argument("--min-prob", type=float, default=0.6, help="recent win rate needed for STRONG")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    sectors = {k: ALL_SECTORS[k] for k in (args.universe or ALL_SECTORS)}
    crypto_only = set(sectors) <= set(CRYPTO)          # Alpaca's crypto prices need no keys
    try:
        base = load_settings(mode="alpaca" if args.source == "alpaca" and not crypto_only else "sim")
    except ConfigError as exc:
        raise SystemExit(f"{exc}\nStocks and funds need Alpaca paper keys in .env. Without keys you can "
                         "still watch crypto: python watcher.py --source alpaca --universe crypto")
    cfg = watch_settings(base, args.timeframe)
    if args.source == "sim":
        source, refresh, all_hours = SimSource(warm=cfg.hedge_lookback * 4), 200 * args.timeframe * 60, True
    else:
        source, refresh, all_hours = AlpacaSource(cfg, sectors), 86_400, False
    db = DBLogger(base.db_path)
    await db.open()
    watcher = Watcher(cfg, source, db, costs=Costs(spread_bps=args.spread_bps), refresh_seconds=refresh,
                      history_bars=cfg.hedge_lookback * 4 if args.source == "sim" else 40 * round(390 / args.timeframe),
                      all_hours=all_hours, notifier=Notifier.from_settings(base, "watcher"),
                      blackout=Blackout(base.blackout_file), min_prob=args.min_prob,
                      watched_pairs={f"{a}/{b}" for a, b in base.all_pairs})
    await watcher.book.load()
    await watcher.long_book.load()
    log.info("watching %d pairs in %d sectors on %d-minute bars", len(watcher.pairs(every=True)),
             len(source.sectors), args.timeframe)
    scans = 0
    try:
        while args.scans is None or scans < args.scans:
            if await source.market_open():
                opps = await watcher.scan()
                scans += 1
                if scans % (50 if args.fast else 1) == 0:
                    log.info("scan %d: %s", scans, summarize(opps))
            if not args.fast:
                await asyncio.sleep(args.timeframe * 60)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(amain())
