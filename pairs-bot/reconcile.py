"""Nightly reconciliation: do our records match what the broker actually holds?

    python reconcile.py            # run by cron / the systemd timer / docker compose run --rm reconcile

The bot already checks this once a day after the close, but only while it is
running. This job runs separately, so a crashed or stopped bot is noticed
too. It compares:

* each pair's open trade in the database with the positions at Alpaca, and
* any position in one of our symbols that no open trade explains.

Mismatches go to Telegram (if configured), are logged as a circuit-breaker
event the dashboard shows, and make the job exit with status 1.
It never places or cancels orders: fixing a mismatch is a human decision.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from config import Settings, load_settings
from db_logger import DBLogger
from notify import Notifier

log = logging.getLogger("reconcile")


def mismatches(pairs: list[tuple[str, tuple[str, str], bool]], held: set[str]) -> list[str]:
    """``pairs``: (name, (symbol_a, symbol_b), has_open_trade). ``held``: symbols the broker holds."""
    problems = []
    for name, symbols, is_open in pairs:
        have = [s for s in symbols if s in held]
        if is_open and len(have) < 2:
            problems.append(f"{name}: open in our records, broker holds {have or 'nothing'}")
        if not is_open and have:
            problems.append(f"{name}: no open trade recorded, broker holds {have}")
    return problems


async def run(cfg: Settings, broker=None, notifier: Notifier | None = None) -> list[str]:
    if broker is None:
        from execution import AlpacaBroker

        broker = AlpacaBroker(cfg)
    notifier = notifier or Notifier(cfg.telegram_token, cfg.telegram_chat_id)
    db = DBLogger(cfg.db_path)
    await db.open()
    try:
        held = set(await broker.holdings())
        pairs = []
        for a, b in cfg.all_pairs:
            name = f"{a}/{b}"
            pairs.append((name, (a, b), await db.open_trade(name) is not None))
        problems = mismatches(pairs, held)
        acct = await broker.account()
        since = time.time() - 86_400
        s = await db.today_summary(since)
        heartbeat = float(await db.get_control("heartbeat") or 0)
        quiet = time.time() - heartbeat if heartbeat else None
        msg = (f"Reconcile: equity ${acct.equity:,.2f} ({acct.equity - acct.last_equity:+,.2f} today), "
               f"{s['trades']} trades closed in 24h (${s['pnl']:+,.2f}). ")
        if quiet is not None and quiet > 3600:
            msg += f"Bot heartbeat is {quiet / 3600:.1f} h old: is it running? "
        if problems:
            await db.log_circuit_event("reconcile_mismatch", problems=problems, source="nightly job")
            msg += "MISMATCH: " + "; ".join(problems)
        else:
            msg += "Records match the broker."
        await notifier.send(msg)
        print(msg)
        return problems
    finally:
        await db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    cfg = load_settings(mode="alpaca")
    problems = asyncio.run(run(cfg))
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
