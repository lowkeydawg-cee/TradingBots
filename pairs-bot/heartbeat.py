"""Phone alert when the watcher stops scanning, and again when it's back.

The watcher can't warn you that it died, so this small separate process checks the database:
if the last scan is older than ``--stale`` minutes it sends one alert (ntfy / Telegram, as set
in .env), and one more when scanning resumes. start-trading.bat runs it next to the watcher.

    python heartbeat.py                 # check every 10 minutes, alert after 45 minutes of silence
    python heartbeat.py --once          # one check, print the state, exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("heartbeat")


def last_scan(path: str) -> float | None:
    if not Path(path).exists():
        return None
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT MAX(ts) FROM opportunities").fetchone()[0]
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


class Heartbeat:
    """Remembers whether it already warned, so one outage is one alert, not one every check."""

    def __init__(self, path: str, notifier, stale_minutes: float = 45) -> None:
        self.path, self.notifier, self.stale = path, notifier, stale_minutes * 60
        self.down = False
        self.started = time.time()          # a fresh start gets the same grace before "never scanned" counts

    async def check(self, now: float | None = None) -> str:
        now = now or time.time()
        last = last_scan(self.path)
        age = now - (last if last is not None else self.started)
        if age > self.stale:
            ago = "has never scanned" if last is None else f"last scanned {age / 60:.0f} minutes ago"
            if not self.down:
                self.down = True
                await self.notifier.send(f"Trading watcher stopped: it {ago}. Demo trades are not being "
                                         "recorded. Restart it with start-trading.bat (and keep the laptop awake).")
            return f"DOWN ({ago})"
        if self.down:
            self.down = False
            await self.notifier.send("Trading watcher is scanning again.")
        return "waiting for the first scan" if last is None else f"ok (last scan {age / 60:.0f} minutes ago)"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--every", type=float, default=10, help="minutes between checks")
    ap.add_argument("--stale", type=float, default=45, help="minutes without a scan before alerting")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--db", help="database (default: the bot's)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    from config import ConfigError, load_settings
    from notify import Notifier
    try:
        cfg = load_settings()
    except ConfigError:
        cfg = load_settings(mode="sim")
    notifier = Notifier.from_settings(cfg, prefix="trading")
    if not notifier.enabled:
        log.warning("no NTFY_TOPIC (or Telegram) in .env: alerts only show in this window")
    hb = Heartbeat(args.db or cfg.db_path, notifier, args.stale)

    async def loop() -> None:
        while True:
            log.info("watcher %s", await hb.check())
            if args.once:
                return
            await asyncio.sleep(args.every * 60)

    asyncio.run(loop())


if __name__ == "__main__":
    main()
