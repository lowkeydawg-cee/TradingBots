"""Stay out of a pair around earnings and breaking news.

Pairs trading assumes the gap between two stocks closes again. A company
event (earnings, a merger, a lawsuit) moves one stock for its own reasons and
the gap may never close, which is exactly the trade you don't want.

Two guards:

* ``Blackout``: a CSV of dates you maintain, one row per event::

      date,symbol,note
      2026-10-21,KO,Q3 earnings
      2026-10-07,PEP,Q3 earnings

  On those days the bot opens nothing in a pair that contains the symbol and
  closes an open trade in it. Earnings dates are on each company's
  investor-relations page. The backtester uses the same file, so tests and
  live trading behave the same.

* ``NewsGuard`` (alpaca mode only): asks Alpaca's news API whether either
  symbol had a headline in the last ``news_pause_minutes``. If so, no new
  entries. This can't be replayed in a backtest, so keep it in mind when
  comparing paper results with backtests.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class Blackout:
    def __init__(self, path: str | Path | None = None) -> None:
        self.days: dict[tuple[date, str], str] = {}
        if path and Path(path).exists():
            with open(path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    try:
                        d = date.fromisoformat(row["date"].strip())
                    except (KeyError, ValueError):
                        log.warning("blackout: skipping bad row %s", row)
                        continue
                    self.days[(d, row["symbol"].strip().upper())] = (row.get("note") or "event").strip()

    def reason(self, day: date, symbols: tuple[str, ...]) -> str | None:
        for s in symbols:
            note = self.days.get((day, s))
            if note:
                return f"{s} {note}"
        return None

    def __len__(self) -> int:
        return len(self.days)


class NewsGuard:
    """Checks Alpaca news for recent headlines. Cached per symbol for a minute to respect rate limits."""

    def __init__(self, api_key: str, secret_key: str, minutes: int) -> None:
        from alpaca.data.historical.news import NewsClient

        self._client = NewsClient(api_key, secret_key)
        self.minutes = minutes
        self._cache: dict[str, tuple[float, str | None]] = {}

    def _latest(self, symbol: str) -> str | None:
        from alpaca.data.requests import NewsRequest

        start = datetime.now(timezone.utc) - timedelta(minutes=self.minutes)
        news = self._client.get_news(NewsRequest(symbols=symbol, start=start, limit=1))
        items = news.data.get("news", []) if hasattr(news, "data") else []
        return items[0].headline if items else None

    async def reason(self, symbols: tuple[str, ...]) -> str | None:
        if self.minutes <= 0:
            return None
        for s in symbols:
            ts, headline = self._cache.get(s, (0.0, None))
            if time.monotonic() - ts > 60:
                try:
                    headline = await asyncio.to_thread(self._latest, s)
                except Exception as exc:              # news is a nice-to-have: never block trading on it
                    log.warning("news check for %s failed: %s", s, exc)
                    headline = None
                self._cache[s] = (time.monotonic(), headline)
            if headline:
                return f"{s} news: {headline[:80]}"
        return None
