"""Real WebSocket transport (``websockets`` >= 13, asyncio API).

``WebSocketFeed`` implements the same ``FeedConnection`` protocol as the
in-process mock, so the bot runs unchanged over a real socket. This build only
accepts loopback endpoints, so it can be exercised against ``mock_server.py``
but is not wired to any third-party service.

Transport hardening:
    * ``ping_interval`` / ``ping_timeout``: protocol-level keepalive detects a
      dead peer even when the application is quiet.
    * ``max_size``: oversized frames are refused before they are parsed.
    * ``max_queue``: bounded receive buffer, so a flood applies backpressure at
      the TCP layer instead of growing memory.
    * Close codes are mapped: 1000 (normal) -> ``FeedClosed``; anything else,
      including 1006 (abnormal, no close frame) -> ``FeedDisconnected``.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK, InvalidHandshake

from .config import LOOPBACK_HOSTS, ConfigError
from .feed import FeedClosed, FeedDisconnected


@dataclass(frozen=True, slots=True)
class WsOptions:
    open_timeout_s: float = 3.0
    ping_interval_s: float = 2.0
    ping_timeout_s: float = 2.0
    max_size_bytes: int = 64 * 1024
    max_queue: int = 256


class WebSocketConnection:
    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws

    async def recv(self) -> str:
        try:
            msg = await self._ws.recv()
        except ConnectionClosedOK as exc:
            raise FeedClosed(f"server closed normally ({exc.rcvd.code if exc.rcvd else 1000})") from exc
        except ConnectionClosed as exc:
            raise FeedDisconnected(f"websocket closed abnormally: {exc}") from exc
        return msg if isinstance(msg, str) else msg.decode("utf-8")

    async def close(self) -> None:
        await self._ws.close()


class WebSocketFeed:
    def __init__(self, url: str, options: WsOptions | None = None) -> None:
        host = urlparse(url).hostname
        if host not in LOOPBACK_HOSTS:
            raise ConfigError(f"refusing non-loopback endpoint {url!r}: this build targets the local mock only")
        self.url = url
        self.opts = options or WsOptions()
        self.connections = 0

    async def connect(self) -> WebSocketConnection:
        o = self.opts
        try:
            ws = await connect(
                self.url,
                open_timeout=o.open_timeout_s,
                ping_interval=o.ping_interval_s,
                ping_timeout=o.ping_timeout_s,
                max_size=o.max_size_bytes,
                max_queue=o.max_queue,
                compression=None,  # avoids decompression-bomb exposure and CPU jitter
            )
        except (OSError, InvalidHandshake, TimeoutError) as exc:
            raise FeedDisconnected(f"connect failed: {exc}") from exc
        self.connections += 1
        return WebSocketConnection(ws)
