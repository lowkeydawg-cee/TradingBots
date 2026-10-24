"""Streaming prices: bars pushed by Alpaca's websocket instead of polled.

Turn it on with ``DATA_SOURCE=stream`` in ``.env`` (alpaca mode only).

Why: polling asks Alpaca for the whole price history every cycle. That is
slow, uses up the API rate limit with several pairs, and sees a new bar only
when the next poll happens. With the stream, Alpaca sends each 1-minute bar
the moment it closes, and the bot only downloads history once at start-up
(and every 30 minutes as a safety net).

How: ``StockDataStream`` runs in a background thread and appends every
1-minute bar it receives. ``get_closes`` merges that with the history and
groups the minutes into the bot's bar size (5, 15 minutes...). Only finished
bars are used, exactly like the polling version.

If the stream goes quiet (connection dropped, thin IEX feed) the data simply
stops updating and the bot's existing stale-data check pauses trading, so a
broken stream can't cause bad trades.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import defaultdict

import pandas as pd

from config import Settings
from strategy import AlpacaMarketData

log = logging.getLogger(__name__)

HISTORY_REFRESH_SECONDS = 30 * 60


class StreamingMarketData:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self._rest = AlpacaMarketData(cfg)
        self._symbols = sorted({s for pair in cfg.all_pairs for s in pair})
        self._lock = threading.Lock()
        self._minutes: dict[str, dict[pd.Timestamp, float]] = defaultdict(dict)   # symbol -> {minute: close}
        self._history: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
        self._stream = None
        self._thread: threading.Thread | None = None
        self.bars_received = 0

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        from alpaca.data.enums import DataFeed
        from alpaca.data.live import StockDataStream

        feed = DataFeed.IEX if self.cfg.data_feed == "iex" else DataFeed.SIP
        self._stream = StockDataStream(self.cfg.api_key, self.cfg.secret_key, feed=feed)
        self._stream.subscribe_bars(self._on_bar, *self._symbols)
        self._thread = threading.Thread(target=self._stream.run, name="alpaca-stream", daemon=True)
        self._thread.start()
        log.info("streaming 1-minute bars for %s", ", ".join(self._symbols))

    async def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception as exc:                  # shutting down anyway
                log.debug("stream stop: %s", exc)

    async def _on_bar(self, bar) -> None:
        ts = pd.Timestamp(bar.timestamp)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        self.add_bar(bar.symbol, ts, float(bar.close))

    def add_bar(self, symbol: str, ts: pd.Timestamp, close: float) -> None:
        with self._lock:
            self._minutes[symbol][ts] = close
            self.bars_received += 1

    # -- MarketData ------------------------------------------------------------
    async def get_closes(self, symbols: tuple[str, str], bars: int) -> pd.DataFrame:
        fetched, history = self._history.get(symbols, (0.0, None))
        if history is None or time.time() - fetched > HISTORY_REFRESH_SECONDS:
            history = await self._rest.get_closes(symbols, bars)
            self._history[symbols] = (time.time(), history)
        return self.merge(history, symbols, bars)

    def merge(self, history: pd.DataFrame, symbols: tuple[str, str], bars: int,
              now: pd.Timestamp | None = None) -> pd.DataFrame:
        """History plus finished streamed bars, resampled to the bot's bar size."""
        with self._lock:
            cols = {s: pd.Series(self._minutes[s], dtype=float) for s in symbols}
        if any(c.empty for c in cols.values()):
            return history
        frame = pd.DataFrame(cols).sort_index()
        size = f"{self.cfg.timeframe_minutes}min"
        live = frame.resample(size, label="left", closed="left").last()
        now = now if now is not None else pd.Timestamp.now(tz="UTC")
        live = live[live.index + pd.Timedelta(size) <= now]                    # finished bars only
        merged = pd.concat([history, live])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        return merged[list(symbols)].dropna().tail(bars)


def _demo() -> None:   # pragma: no cover - manual check: python streaming.py (needs keys, market hours)
    from config import load_settings

    logging.basicConfig(level=logging.INFO)

    async def run() -> None:
        cfg = load_settings(mode="alpaca", data_source="stream")
        md = StreamingMarketData(cfg)
        await md.start()
        for _ in range(5):
            await asyncio.sleep(65)
            df = await md.get_closes(cfg.all_pairs[0], 5)
            print(f"{md.bars_received} streamed bars so far; latest:\n{df.tail(2)}")
        await md.stop()

    asyncio.run(run())


if __name__ == "__main__":
    _demo()
