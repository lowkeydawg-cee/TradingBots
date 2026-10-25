"""Async monitoring loop: fetch -> model -> risk checks -> execute -> log, for one or many pairs.

    python main.py                      # uses BOT_MODE from env/.env (default: sim)
    python main.py --mode sim --fast --cycles 600
    python main.py --reset-halt         # clear a previous halt (and pair retirements) after review

Each cycle, in this order (account-wide risk checks always come before any new order):
  1. heartbeat + emergency-stop flag from the dashboard  -> flatten and halt
  2. account: trading blocked, daily / weekly / monthly loss limits -> flatten and halt
  3. after the close: daily report + reconciliation check (once a day)
  4. market clock (closed -> skip)
  5. for every pair (``PairEngine``):
     bars -> hedge ratio, spread, z-score, cointegration p-value -> earnings/news blackout
     -> decide(): HOLD / OPEN_LONG / OPEN_SHORT / CLOSE / BLOCKED -> execute -> log
Any exception counts as an error; ``max_consecutive_errors`` in a row flattens and halts.

Portfolio mode (``PAIRS=KO:PEP,XOM:CVX,...``)
--------------------------------------------
Every pair has its own position, model and kill switch (it *retires* when its
cointegration p-value stays above ``retire_pvalue`` for ``retire_days``
trading days). Risk is shared: one gross-exposure cap and one set of loss
limits for the whole account, and a halt flattens everything.

A halt is persisted in the database (``control.halted``) so restarting the
process does not resume trading. Clear it with ``--reset-halt``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from blackout import Blackout, NewsGuard
from config import Settings, load_settings
from db_logger import DBLogger
from execution import AlpacaBroker, Broker, EntrySkipped, OrderFailed, PairExecutor, pair_pnl, plan_pair
from notify import Notifier
from telegram_commands import TelegramCommands
from reconcile import mismatches
from strategy import Action, MarketData, Position, SpreadStats, compute_spread_stats, decide

log = logging.getLogger("pairs_bot")
NY = ZoneInfo("America/New_York")


class PairEngine:
    """Everything about one pair: its position, model, decisions and orders."""

    def __init__(self, bot: "PairsBot", cfg: Settings) -> None:
        self.bot = bot
        self.cfg = cfg
        self.name = f"{cfg.symbol_a}/{cfg.symbol_b}"
        self.exec = PairExecutor(bot.broker, cfg)
        self.position = Position.FLAT
        self.trade: dict | None = None        # open pair_trades row
        self.bars_held = 0
        self.last_stats: SpreadStats | None = None
        self.retired: str | None = None
        self._day = None                      # trading day of the last bar seen
        self._bad_days = 0                    # consecutive days the cointegration test failed

    @property
    def symbols(self) -> tuple[str, str]:
        return self.cfg.symbol_a, self.cfg.symbol_b

    async def reconcile(self, held: dict) -> set[str]:
        """Resume an open trade if the broker still holds it. Returns the symbols it accounts for."""
        db = self.bot.db
        a, b = self.symbols
        open_trade = await db.open_trade(self.name)
        if open_trade and a in held and b in held:
            self.trade, self.position = open_trade, Position(open_trade["direction"])
            log.info("%s: resumed open %s trade #%s", self.name, self.position, open_trade["id"])
            return {a, b}
        if open_trade:
            await db.close_pair_trade(open_trade["id"], None, 0.0, 0.0, 0.0,
                                      "reconciled: position not found at broker")
            await db.log_circuit_event("reconcile_closed_stale_trade", pair=self.name, trade_id=open_trade["id"])
        return set()

    # ------------------------------------------------------------------ cycle
    async def step(self, gross_now: float) -> float:
        """One bar for this pair. Returns the gross exposure it added (for the shared cap)."""
        cfg, bot = self.cfg, self.bot
        closes = await bot.data.get_closes(self.symbols, cfg.hedge_lookback)
        stats = compute_spread_stats(closes, cfg)
        self.last_stats = stats
        await bot.db.log_price_check(stats.bar_time, cfg.symbol_a, stats.price_a, cfg.symbol_b, stats.price_b)
        age = bot.clock() - stats.bar_time
        if age > cfg.max_bar_age_seconds:
            await self._log(stats, self.position, Action.BLOCKED, f"stale data: last bar {age:.0f}s old")
            return 0.0

        day = datetime.fromtimestamp(stats.bar_time, NY).date()
        await self._check_retirement(day, stats)
        blackout = bot.blackout.reason(day, self.symbols)

        if self.position is not Position.FLAT:
            self.bars_held += 1
        action, note = decide(self.position, stats, self.bars_held, cfg, blackout)
        if action in (Action.OPEN_LONG, Action.OPEN_SHORT):
            if self.retired:
                action, note = Action.BLOCKED, f"pair retired: {self.retired}"
            elif bot.paused:
                action, note = Action.BLOCKED, f"paused by {bot.paused}"
            elif bot.news is not None and (news := await bot.news.reason(self.symbols)):
                action, note = Action.BLOCKED, f"blackout: {news}"
        position_before = self.position

        added = 0.0
        if action in (Action.OPEN_LONG, Action.OPEN_SHORT):
            action, note, added = await self._open(action, stats, gross_now, note)
        elif action is Action.CLOSE:
            await self._close(stats, note)
        await self._log(stats, position_before, action, note)
        return added

    async def _log(self, stats: SpreadStats, position: Position, action: Action, note: str) -> None:
        await self.bot.db.log_signal(stats.bar_time, stats.as_dict(), position, action, note, pair=self.name)

    async def _check_retirement(self, day, stats: SpreadStats) -> None:
        """Once per trading day: is the pair still cointegrated? Retire it after retire_days bad days."""
        if day == self._day:
            return
        self._day = day
        self._bad_days = self._bad_days + 1 if stats.coint_pvalue >= self.cfg.retire_pvalue else 0
        if self._bad_days >= self.cfg.retire_days and not self.retired:
            self.retired = f"cointegration p >= {self.cfg.retire_pvalue} for {self._bad_days} days"
            await self.bot.db.set_control(f"retired:{self.name}", self.retired)
            await self.bot.db.log_circuit_event("pair_retired", pair=self.name, reason=self.retired)
            await self.bot.notifier.send(f"{self.name} retired: {self.retired}. No new trades in it.")

    # -------------------------------------------------------------- execution
    async def _open(self, action: Action, stats: SpreadStats, gross_now: float,
                    note: str) -> tuple[Action, str, float]:
        bot, cfg = self.bot, self.cfg
        direction = Position.LONG_SPREAD if action is Action.OPEN_LONG else Position.SHORT_SPREAD
        try:
            plan = plan_pair(direction, stats, cfg)
        except ValueError as exc:
            return Action.BLOCKED, str(exc), 0.0
        if gross_now + plan.gross_notional > cfg.max_gross_exposure:
            return Action.BLOCKED, "gross exposure cap", 0.0
        tag = f"pair-{int(bot.clock() * 1000)}-{cfg.symbol_a}"
        try:
            pf = await self.exec.open_pair(plan, tag)
        except EntrySkipped as exc:                  # nothing traded; try again on a later bar
            return Action.BLOCKED, f"entry skipped: {exc}", 0.0
        except OrderFailed as exc:
            await bot.db.log_circuit_event("open_failed", pair=self.name, error=str(exc))
            return Action.BLOCKED, f"open failed: {exc}", 0.0
        px_a = pf.fill_a.avg_price or stats.price_a
        px_b = pf.fill_b.avg_price or stats.price_b
        trade_id = await bot.db.open_pair_trade(direction, stats.zscore, stats.beta, pf.fill_a.qty,
                                                pf.fill_b.qty, px_a, px_b, pair=self.name)
        for f in (pf.fill_a, pf.fill_b):
            await bot.db.log_order(trade_id, f.symbol, f.side, f.qty, f.avg_price, f"{tag}-{f.symbol}-open",
                                   f.order_id, f.status, "open", expected_price=f.expected_price)
        self.trade = {"id": trade_id, "direction": str(direction), "qty_a": pf.fill_a.qty,
                      "qty_b": pf.fill_b.qty, "entry_price_a": px_a, "entry_price_b": px_b}
        self.position, self.bars_held = direction, 0
        if cfg.notify_trades:
            await bot.notifier.send(f"OPEN {self.name} {direction} z={stats.zscore:+.2f}: "
                                    f"{pf.fill_a.qty:g} {cfg.symbol_a} / {pf.fill_b.qty:g} {cfg.symbol_b}")
        added = pf.fill_a.qty * px_a + pf.fill_b.qty * px_b
        return action, f"{note}; {pf.fill_a.qty:g} {cfg.symbol_a} / {pf.fill_b.qty:g} {cfg.symbol_b}", added

    async def _close(self, stats: SpreadStats | None, reason: str, already_flat: bool = False) -> None:
        bot = self.bot
        fill_a = fill_b = None
        if not already_flat:
            fill_a, fill_b = await self.exec.close_pair()
        px_a = fill_a.avg_price if fill_a and fill_a.avg_price else (stats.price_a if stats else 0.0)
        px_b = fill_b.avg_price if fill_b and fill_b.avg_price else (stats.price_b if stats else 0.0)
        if self.trade is not None:
            t = self.trade
            pnl = pair_pnl(Position(t["direction"]), t["qty_a"], t["qty_b"], t["entry_price_a"],
                           t["entry_price_b"], px_a, px_b)
            await bot.db.close_pair_trade(t["id"], stats.zscore if stats else None, px_a, px_b,
                                          round(pnl, 2), reason)
            expected = {self.cfg.symbol_a: stats.price_a if stats else None,
                        self.cfg.symbol_b: stats.price_b if stats else None}
            for f in (fill_a, fill_b):
                if f is not None:
                    await bot.db.log_order(t["id"], f.symbol, f.side, f.qty, f.avg_price, None,
                                           f.order_id, f.status, "close", expected_price=expected[f.symbol])
            if self.cfg.notify_trades:
                await bot.notifier.send(f"CLOSE {self.name} ({reason}): P&L ${pnl:+.2f}")
        self.trade, self.position, self.bars_held = None, Position.FLAT, 0


class PairsBot:
    """The portfolio: shared account-level risk around one ``PairEngine`` per pair."""

    def __init__(self, cfg: Settings, data: MarketData, broker: Broker, db: DBLogger,
                 clock: Callable[[], float] = time.time, notifier: Notifier | None = None,
                 blackout: Blackout | None = None, news: NewsGuard | None = None) -> None:
        self.cfg = cfg
        self.data = data
        self.broker = broker
        self.db = db
        self.clock = clock
        self.notifier = notifier or Notifier()
        self.blackout = blackout if blackout is not None else Blackout(cfg.blackout_file)
        self.news = news
        self.engines = [PairEngine(self, cfg.for_pair(a, b)) for a, b in cfg.all_pairs]
        self.exec = PairExecutor(broker, cfg)
        self.consecutive_errors = 0
        self.halted: str | None = None
        self.paused: str | None = None        # set from Telegram /pause: no new entries
        self._report_day = None
        self._stop = asyncio.Event()

    # Single-pair conveniences (and backwards compatibility).
    @property
    def trade(self) -> dict | None:
        return self.engines[0].trade

    @property
    def position(self) -> Position:
        return self.engines[0].position

    @property
    def last_stats(self) -> SpreadStats | None:
        return self.engines[0].last_stats

    # ------------------------------------------------------------------ setup
    async def start(self, reset_halt: bool = False) -> None:
        await self.db.open()
        if reset_halt:
            await self.db.set_control("halted", "")
            await self.db.set_control("emergency_stop", "")
            await self.db.set_control("paused", "")
            for e in self.engines:
                await self.db.set_control(f"retired:{e.name}", "")
            await self.db.log_circuit_event("halt_reset", by="operator")
        for e in self.engines:
            e.retired = await self.db.get_control(f"retired:{e.name}") or None
        prior = await self.db.get_control("halted")
        if prior:
            self.halted = prior
            log.error("bot is halted (%s); review, then run with --reset-halt", prior)
            return
        await self._reconcile()

    async def _reconcile(self) -> None:
        """Make each engine's in-memory position agree with both the DB and the broker."""
        held = await self.broker.holdings()
        accounted: set[str] = set()
        for e in self.engines:
            accounted |= await e.reconcile(held)
        ours = {s for e in self.engines for s in e.symbols}
        unknown = sorted((ours & set(held)) - accounted)
        if unknown:
            # Positions in our symbols that we did not open: never trade on top of them.
            await self.halt("unknown positions in pair symbols at startup", flatten=False, symbols=unknown)

    # ------------------------------------------------------------------- loop
    def request_stop(self) -> None:
        self._stop.set()

    async def run(self, max_cycles: int | None = None) -> None:
        cycles = 0
        while not self.halted and not self._stop.is_set():
            await self.cycle()
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.poll_seconds)

    async def cycle(self) -> None:
        try:
            await self._cycle()
            self.consecutive_errors = 0
        except Exception as exc:
            self.consecutive_errors += 1
            log.exception("cycle failed (%d in a row)", self.consecutive_errors)
            await self.db.log_circuit_event("cycle_error", error=repr(exc), count=self.consecutive_errors)
            if self.consecutive_errors >= self.cfg.max_consecutive_errors:
                await self.halt(f"{self.consecutive_errors} consecutive errors", flatten=True)
        if self.db.consecutive_failures >= 3:
            await self.halt("event log unavailable", flatten=True)

    async def _cycle(self) -> None:
        cfg = self.cfg
        now = self.clock()
        await self.db.set_control("heartbeat", str(now))

        # 1. Dashboard emergency stop.
        stop = await self.db.get_control("emergency_stop")
        if stop:
            await self.halt(f"emergency stop: {stop}", flatten=True)
            return
        self.paused = await self.db.get_control("paused") or None

        # 2. Account-level risk. last_equity is the previous close, so this is today's P&L.
        acct = await self.broker.account()
        held = await self.broker.holdings()
        gross = sum(abs(h.market_value) for h in held.values())
        await self.db.log_equity(acct.equity, acct.last_equity, gross)
        if acct.trading_blocked:
            await self.halt("account trading blocked", flatten=False)
            return
        loss_limit = min(cfg.max_daily_loss_usd, cfg.max_daily_loss_pct * acct.last_equity)
        daily_pnl = acct.equity - acct.last_equity
        if daily_pnl <= -loss_limit:
            await self.halt("daily loss limit", flatten=True, daily_pnl=round(daily_pnl, 2),
                            limit=round(loss_limit, 2))
            return
        if await self._period_limit_hit(acct.equity, now):
            return

        # 3. After the close: one report and one reconciliation check per day.
        local = datetime.fromtimestamp(now, NY)
        if local.hour >= 16 and self._report_day != local.date():
            self._report_day = local.date()
            await self._daily_report(local, acct.equity, acct.last_equity, held)

        # 4. Market hours.
        if not await self.broker.market_open():
            return

        # 5. Every pair. One pair's error doesn't stop the others this cycle, but still counts.
        errors = []
        for e in self.engines:
            try:
                gross += await e.step(gross)
            except Exception as exc:
                log.exception("%s failed", e.name)
                errors.append(f"{e.name}: {exc!r}")
        if errors:
            raise RuntimeError("; ".join(errors))

    async def _period_limit_hit(self, equity: float, now: float) -> bool:
        local = datetime.fromtimestamp(now, NY)
        week_start = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for label, start, pct in (("weekly", week_start, self.cfg.max_weekly_loss_pct),
                                  ("monthly", month_start, self.cfg.max_monthly_loss_pct)):
            base = await self.db.period_start_equity(start.timestamp())
            if base and equity - base <= -pct * base:
                await self.halt(f"{label} loss limit", flatten=True, pnl=round(equity - base, 2),
                                limit=round(pct * base, 2))
                return True
        return False

    async def _daily_report(self, local: datetime, equity: float, last_equity: float, held: dict) -> None:
        since = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        s = await self.db.today_summary(since)
        slip = f", avg slippage {s['avg_slippage_bps']:.1f} bps" if s["avg_slippage_bps"] is not None else ""
        await self.notifier.send(
            f"Daily report {local:%Y-%m-%d}: equity ${equity:,.2f} ({equity - last_equity:+,.2f} today), "
            f"{s['trades']} trades closed ({s['wins']} wins, ${s['pnl']:+,.2f}){slip}. "
            f"Open: {', '.join(e.name for e in self.engines if e.trade) or 'none'}. "
            f"Retired: {', '.join(e.name for e in self.engines if e.retired) or 'none'}.")
        # Reconciliation: does the broker hold exactly what our records say?
        problems = mismatches([(e.name, e.symbols, e.trade is not None) for e in self.engines], set(held))
        if problems:
            await self.db.log_circuit_event("reconcile_mismatch", problems=problems)
            await self.notifier.send("RECONCILE MISMATCH, please check: " + "; ".join(problems))

    async def halt(self, reason: str, flatten: bool, **detail: object) -> None:
        """Stop trading for good (persisted). Optionally flatten everything first."""
        if self.halted:
            return
        self.halted = reason
        log.critical("HALT: %s %s", reason, detail)
        if flatten:
            try:
                await self.exec.flatten_all()
                for e in self.engines:
                    if e.position is not Position.FLAT:
                        await e._close(e.last_stats, f"halt: {reason}", already_flat=True)
            except Exception as exc:
                detail["flatten_error"] = repr(exc)
                log.exception("flatten failed: close positions manually in the Alpaca dashboard")
        await self.db.set_control("halted", reason)
        await self.db.log_circuit_event("halt", reason=reason, flattened=flatten, **detail)
        await self.notifier.send(f"HALTED: {reason} {detail or ''}".strip())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build(cfg: Settings) -> tuple[MarketData, Broker, Callable[[], float]]:
    if cfg.mode == "alpaca":
        if cfg.data_source == "stream":
            from streaming import StreamingMarketData

            return StreamingMarketData(cfg), AlpacaBroker(cfg), time.time
        from strategy import AlpacaMarketData

        return AlpacaMarketData(cfg), AlpacaBroker(cfg), time.time
    from sim import SimBroker, SimMarketData, SimUniverse

    universe = SimUniverse.for_pairs(cfg.all_pairs)
    universe.warm_up(cfg.hedge_lookback)
    # A 1 bp half-spread and passive orders that fill 70% of the time, so sim mode
    # shows the costs and misses that real trading has.
    broker = SimBroker(universe, half_spread_bps=1.0, passive_fill_prob=0.7)
    # Sim clock = the latest simulated bar, so the stale-data check behaves like live.
    return SimMarketData(universe), broker, lambda: universe.last[0]


async def amain() -> None:
    ap = argparse.ArgumentParser(description="Pairs trading bot (Alpaca paper)")
    ap.add_argument("--mode", choices=("alpaca", "sim"))
    ap.add_argument("--cycles", type=int, default=None, help="stop after N cycles")
    ap.add_argument("--fast", action="store_true", help="sim only: no sleep between cycles")
    ap.add_argument("--reset-halt", action="store_true", help="clear a persisted halt and pair retirements")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    overrides: dict[str, object] = {}
    if args.mode:
        overrides["mode"] = args.mode
    if args.fast:
        overrides["poll_seconds"] = 0.0
    cfg = load_settings(**overrides)
    if args.fast and cfg.mode != "sim":
        raise SystemExit("--fast is only allowed in sim mode")

    data, broker, clock = build(cfg)
    notifier = Notifier(cfg.telegram_token, cfg.telegram_chat_id)
    news = (NewsGuard(cfg.api_key, cfg.secret_key, cfg.news_pause_minutes)
            if cfg.mode == "alpaca" and cfg.news_pause_minutes > 0 else None)
    bot = PairsBot(cfg, data, broker, DBLogger(cfg.db_path), clock, notifier=notifier, news=news)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, bot.request_stop)
    await bot.start(reset_halt=args.reset_halt)
    commands = TelegramCommands(bot, cfg.telegram_token, cfg.telegram_chat_id)
    listener = asyncio.create_task(commands.run()) if commands.enabled and args.cycles is None else None
    try:
        if not bot.halted:
            if hasattr(data, "start"):
                await data.start()
            log.info("running %s in %s mode (%d blackout dates loaded)",
                     ", ".join(e.name for e in bot.engines), cfg.mode, len(bot.blackout))
            await notifier.send(f"started: {', '.join(e.name for e in bot.engines)} ({cfg.mode})")
            await bot.run(max_cycles=args.cycles)
        if bot.halted and listener is not None:
            # Stay up so /status and /trades still answer from your phone while halted.
            log.info("halted; still answering Telegram commands until stopped")
            await bot._stop.wait()
    finally:
        if listener is not None:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener
        if hasattr(data, "stop"):
            await data.stop()
        await bot.db.close()
    log.info("stopped%s", f" (halted: {bot.halted})" if bot.halted else "")


if __name__ == "__main__":
    asyncio.run(amain())
