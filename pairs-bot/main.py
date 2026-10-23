"""Async monitoring loop: fetch -> model -> risk checks -> execute -> log.

    python main.py                      # uses BOT_MODE from env/.env (default: sim)
    python main.py --mode sim --fast --cycles 600
    python main.py --reset-halt         # clear a previous halt after you have reviewed it

Each cycle, in this order (risk checks always come before any new order):
  1. heartbeat + emergency-stop flag from the dashboard  -> flatten and halt
  2. account: trading blocked, daily loss limit           -> flatten and halt
  3. market clock (closed -> skip)
  4. bars for both legs; stale data -> skip
  5. hedge ratio, spread, z-score, cointegration p-value
  6. decide(): HOLD / OPEN_LONG / OPEN_SHORT / CLOSE / BLOCKED
  7. execute, log orders and the pair trade
  8. log the signal and an equity snapshot
Any exception counts as an error; ``max_consecutive_errors`` in a row flattens and halts.

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

from config import Settings, load_settings
from db_logger import DBLogger
from execution import AlpacaBroker, Broker, OrderFailed, PairExecutor, pair_pnl, plan_pair
from strategy import Action, MarketData, Position, SpreadStats, compute_spread_stats, decide

log = logging.getLogger("pairs_bot")


class PairsBot:
    def __init__(self, cfg: Settings, data: MarketData, broker: Broker, db: DBLogger,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.data = data
        self.broker = broker
        self.exec = PairExecutor(broker, cfg)
        self.db = db
        self.clock = clock
        self.position = Position.FLAT
        self.trade: dict | None = None        # open pair_trades row
        self.bars_held = 0
        self.last_stats: SpreadStats | None = None
        self.consecutive_errors = 0
        self.halted: str | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ setup
    async def start(self, reset_halt: bool = False) -> None:
        await self.db.open()
        if reset_halt:
            await self.db.set_control("halted", "")
            await self.db.set_control("emergency_stop", "")
            await self.db.log_circuit_event("halt_reset", by="operator")
        prior = await self.db.get_control("halted")
        if prior:
            self.halted = prior
            log.error("bot is halted (%s); review, then run with --reset-halt", prior)
            return
        await self._reconcile()

    async def _reconcile(self) -> None:
        """Make the in-memory position agree with both the DB and the broker."""
        a, b = self.cfg.symbol_a, self.cfg.symbol_b
        held = await self.broker.holdings()
        open_trade = await self.db.open_trade()
        has_a, has_b = a in held, b in held
        if open_trade and has_a and has_b:
            self.trade, self.position = open_trade, Position(open_trade["direction"])
            log.info("resumed open %s trade #%s", self.position, open_trade["id"])
        elif open_trade:
            await self.db.close_pair_trade(open_trade["id"], None, 0.0, 0.0, 0.0,
                                           "reconciled: position not found at broker")
            await self.db.log_circuit_event("reconcile_closed_stale_trade", trade_id=open_trade["id"])
        elif has_a or has_b:
            # Positions in our symbols that we did not open: never trade on top of them.
            await self.halt("unknown positions in pair symbols at startup", flatten=False,
                            symbols=[s for s in (a, b) if s in held])

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
        await self.db.set_control("heartbeat", str(self.clock()))

        # 1. Dashboard emergency stop.
        stop = await self.db.get_control("emergency_stop")
        if stop:
            await self.halt(f"emergency stop: {stop}", flatten=True)
            return

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

        # 3. Market hours.
        if not await self.broker.market_open():
            return

        # 4-5. Data and model.
        closes = await self.data.get_closes((cfg.symbol_a, cfg.symbol_b), cfg.hedge_lookback)
        stats = compute_spread_stats(closes, cfg)
        self.last_stats = stats
        await self.db.log_price_check(stats.bar_time, cfg.symbol_a, stats.price_a, cfg.symbol_b, stats.price_b)
        age = self.clock() - stats.bar_time
        if age > cfg.max_bar_age_seconds:
            await self.db.log_signal(stats.bar_time, stats.as_dict(), self.position, Action.BLOCKED,
                                     f"stale data: last bar {age:.0f}s old")
            return

        # 6. Decide.
        if self.position is not Position.FLAT:
            self.bars_held += 1
        action, note = decide(self.position, stats, self.bars_held, cfg)
        position_before = self.position

        # 7. Execute.
        if action in (Action.OPEN_LONG, Action.OPEN_SHORT):
            action, note = await self._open(action, stats, gross, note)
        elif action is Action.CLOSE:
            await self._close(stats, note)

        # 8. Log the decision.
        await self.db.log_signal(stats.bar_time, stats.as_dict(), position_before, action, note)

    # -------------------------------------------------------------- execution
    async def _open(self, action: Action, stats: SpreadStats, gross_now: float,
                    note: str) -> tuple[Action, str]:
        direction = Position.LONG_SPREAD if action is Action.OPEN_LONG else Position.SHORT_SPREAD
        try:
            plan = plan_pair(direction, stats, self.cfg)
        except ValueError as exc:
            return Action.BLOCKED, str(exc)
        if gross_now + plan.gross_notional > self.cfg.max_gross_exposure:
            return Action.BLOCKED, "gross exposure cap"
        tag = f"pair-{int(self.clock() * 1000)}"
        try:
            pf = await self.exec.open_pair(plan, tag)
        except OrderFailed as exc:
            await self.db.log_circuit_event("open_failed", error=str(exc))
            return Action.BLOCKED, f"open failed: {exc}"
        trade_id = await self.db.open_pair_trade(direction, stats.zscore, stats.beta, pf.fill_a.qty,
                                                 pf.fill_b.qty, pf.fill_a.avg_price or stats.price_a,
                                                 pf.fill_b.avg_price or stats.price_b)
        for f in (pf.fill_a, pf.fill_b):
            await self.db.log_order(trade_id, f.symbol, f.side, f.qty, f.avg_price, f"{tag}-{f.symbol}-open",
                                    f.order_id, f.status, "open")
        self.trade = {"id": trade_id, "direction": str(direction), "qty_a": pf.fill_a.qty,
                      "qty_b": pf.fill_b.qty, "entry_price_a": pf.fill_a.avg_price or stats.price_a,
                      "entry_price_b": pf.fill_b.avg_price or stats.price_b}
        self.position, self.bars_held = direction, 0
        return action, f"{note}; {plan.qty_a} {self.cfg.symbol_a} / {plan.qty_b} {self.cfg.symbol_b}"

    async def _close(self, stats: SpreadStats | None, reason: str, already_flat: bool = False) -> None:
        fill_a = fill_b = None
        if not already_flat:
            fill_a, fill_b = await self.exec.close_pair()
        px_a = fill_a.avg_price if fill_a and fill_a.avg_price else (stats.price_a if stats else 0.0)
        px_b = fill_b.avg_price if fill_b and fill_b.avg_price else (stats.price_b if stats else 0.0)
        if self.trade is not None:
            t = self.trade
            pnl = pair_pnl(Position(t["direction"]), t["qty_a"], t["qty_b"], t["entry_price_a"],
                           t["entry_price_b"], px_a, px_b)
            await self.db.close_pair_trade(t["id"], stats.zscore if stats else None, px_a, px_b,
                                           round(pnl, 2), reason)
            for f in (fill_a, fill_b):
                if f is not None:
                    await self.db.log_order(t["id"], f.symbol, f.side, f.qty, f.avg_price, None,
                                            f.order_id, f.status, "close")
        self.trade, self.position, self.bars_held = None, Position.FLAT, 0

    async def halt(self, reason: str, flatten: bool, **detail: object) -> None:
        """Stop trading for good (persisted). Optionally flatten everything first."""
        if self.halted:
            return
        self.halted = reason
        log.critical("HALT: %s %s", reason, detail)
        if flatten:
            try:
                await self.exec.flatten_all()
                if self.position is not Position.FLAT:
                    await self._close(self.last_stats, f"halt: {reason}", already_flat=True)
            except Exception as exc:
                detail["flatten_error"] = repr(exc)
                log.exception("flatten failed: close positions manually in the Alpaca dashboard")
        await self.db.set_control("halted", reason)
        await self.db.log_circuit_event("halt", reason=reason, flattened=flatten, **detail)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build(cfg: Settings) -> tuple[MarketData, Broker, Callable[[], float]]:
    if cfg.mode == "alpaca":
        from strategy import AlpacaMarketData

        return AlpacaMarketData(cfg), AlpacaBroker(cfg), time.time
    from sim import SimBroker, SimMarket, SimMarketData

    market = SimMarket(symbol_a=cfg.symbol_a, symbol_b=cfg.symbol_b)
    market.warm_up(cfg.hedge_lookback)
    # Sim clock = the latest simulated bar, so the stale-data check behaves like live.
    return SimMarketData(market), SimBroker(market), lambda: market.last[0]


async def amain() -> None:
    ap = argparse.ArgumentParser(description="Pairs trading bot (Alpaca paper)")
    ap.add_argument("--mode", choices=("alpaca", "sim"))
    ap.add_argument("--cycles", type=int, default=None, help="stop after N cycles")
    ap.add_argument("--fast", action="store_true", help="sim only: no sleep between cycles")
    ap.add_argument("--reset-halt", action="store_true", help="clear a persisted halt")
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
    bot = PairsBot(cfg, data, broker, DBLogger(cfg.db_path), clock)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, bot.request_stop)
    await bot.start(reset_halt=args.reset_halt)
    try:
        if not bot.halted:
            log.info("running %s/%s in %s mode", cfg.symbol_a, cfg.symbol_b, cfg.mode)
            await bot.run(max_cycles=args.cycles)
    finally:
        await bot.db.close()
    log.info("stopped%s", f" (halted: {bot.halted})" if bot.halted else "")


if __name__ == "__main__":
    asyncio.run(amain())
