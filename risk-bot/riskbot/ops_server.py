"""Local operations endpoint (stdlib asyncio, loopback only).

    GET  /status   JSON snapshot: state, equity, drawdown, open exposure, ...
    GET  /metrics  Prometheus text exposition format
    POST /kill     trips the circuit breaker immediately

There is deliberately no endpoint that resumes trading or changes a limit: the
only remote action is the one that makes things safer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 8 * 1024


def render_prometheus(metrics: dict[str, tuple[str, str, float]]) -> str:
    """metrics: name -> (type, help, value)."""
    lines = []
    for name, (mtype, help_text, value) in sorted(metrics.items()):
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {mtype}", f"{name} {value:g}"]
    return "\n".join(lines) + "\n"


class OpsServer:
    def __init__(self, snapshot: Callable[[], dict[str, Any]],
                 metrics: Callable[[], dict[str, tuple[str, str, float]]],
                 kill: Callable[[str], None], host: str = "127.0.0.1", port: int = 0) -> None:
        self._snapshot, self._metrics, self._kill = snapshot, metrics, kill
        self.host, self.port = host, port
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("ops endpoint on http://%s:%d", self.host, self.port)
        return self.port

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2.0)
            if len(head) > MAX_REQUEST_BYTES:
                raise ValueError("request too large")
            method, path, _ = head.split(b"\r\n", 1)[0].decode("ascii").split(" ", 2)
            status, ctype, body = self._route(method, path)
        except Exception as exc:  # malformed request, timeout, oversize
            status, ctype, body = "400 Bad Request", "text/plain", f"bad request: {exc}\n"
        payload = body.encode()
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n".encode() + payload
        )
        with contextlib.suppress(Exception):
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    def _route(self, method: str, path: str) -> tuple[str, str, str]:
        if method == "GET" and path == "/status":
            return "200 OK", "application/json", json.dumps(self._snapshot(), default=str)
        if method == "GET" and path == "/metrics":
            return "200 OK", "text/plain; version=0.0.4", render_prometheus(self._metrics())
        if method == "POST" and path == "/kill":
            self._kill("operator kill via ops endpoint")
            return "200 OK", "application/json", json.dumps({"killed": True})
        return "404 Not Found", "text/plain", "not found\n"
