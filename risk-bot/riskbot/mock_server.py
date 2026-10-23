"""Serve ``MockCrashFeed`` over a real localhost WebSocket.

Each client connection gets the next rounds of the same simulated server, so a
reconnecting client resumes like it would against a real feed. A scripted
``drop_mid_round`` aborts the TCP transport without a close frame, which the
client sees as close code 1006, the same as a real network failure.

    python -m riskbot.mock_server --port 8765
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging

from websockets.asyncio.server import Server, ServerConnection, serve

from .feed import FeedClosed, FeedDisconnected, MockCrashFeed, MockFeedScript

log = logging.getLogger(__name__)


async def _pump(server_feed: MockCrashFeed, ws: ServerConnection) -> None:
    conn = await server_feed.connect()
    try:
        while True:
            try:
                msg = await conn.recv()
            except FeedClosed:
                await ws.close(1000, "no more rounds")
                return
            except FeedDisconnected:
                ws.transport.abort()  # hard drop: no close frame -> 1006 on the client
                return
            await ws.send(msg)
    finally:
        with contextlib.suppress(Exception):
            await conn.close()


async def start_mock_server(script: MockFeedScript, host: str = "127.0.0.1", port: int = 0
                            ) -> tuple[Server, MockCrashFeed, int]:
    """Start the server; returns (server, simulated feed, bound port)."""
    feed = MockCrashFeed(script)
    server = await serve(lambda ws: _pump(feed, ws), host, port, compression=None)
    bound = server.sockets[0].getsockname()[1]
    return server, feed, bound


async def _main() -> None:
    ap = argparse.ArgumentParser(prog="riskbot.mock_server")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--rounds", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    server, _, port = await start_mock_server(
        MockFeedScript(rounds=args.rounds, seed=args.seed, betting_window_s=0.5), port=args.port)
    log.info("mock crash feed on ws://127.0.0.1:%d", port)
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_main())
