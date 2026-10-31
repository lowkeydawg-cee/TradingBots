"""Control the bot from your phone: Telegram commands.

Uses the same bot token and chat id as the alerts in notify.py. Messages from
any other chat are ignored, so a stranger who finds your bot can't use it.

    /status          running or halted, equity, today's P&L, open trades
    /trades          the last 5 closed trades
    /pnl             profit today, this week, and all time
    /market          how the market looks right now, and today's alerts
    /watch           the market watcher's best opportunities right now
    /pause           no new trades (open ones still exit normally)
    /resume          allow new trades again
    /stop confirm    EMERGENCY: close every position and halt
    /help            this list

The listener long-polls Telegram's getUpdates (no webhook, so no open port on
the server) and runs inside the bot process. If Telegram is unreachable it
just retries; trading never waits for it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from main import PairsBot

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

HELP = ("/status  running or halted, equity, open trades\n"
        "/trades  last 5 closed trades\n"
        "/pnl  profit today, this week, all time\n"
        "/market  how the market looks, today's alerts\n"
        "/watch  best opportunities right now\n"
        "/pause  no new trades\n"
        "/resume  allow new trades\n"
        "/stop confirm  close everything and halt\n"
        "/help  this list")


class TelegramCommands:
    def __init__(self, bot: "PairsBot", token: str, chat_id: str,
                 fetch: Callable[[str, dict], Any] | None = None) -> None:
        self.bot, self.token, self.chat_id = bot, token, str(chat_id)
        self._fetch = fetch or self._http
        self._offset = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    # ------------------------------------------------------------------ transport
    def _http(self, method: str, params: dict) -> Any:
        url = f"https://api.telegram.org/bot{self.token}/{method}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=params.get("timeout", 10) + 10) as resp:
            return json.loads(resp.read())

    async def poll_once(self, timeout: int = 25) -> int:
        """Fetch new messages, answer them. Returns how many were handled."""
        data = await asyncio.to_thread(self._fetch, "getUpdates", {"offset": self._offset, "timeout": timeout})
        handled = 0
        for update in data.get("result", []):
            self._offset = max(self._offset, update["update_id"] + 1)
            msg = update.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != self.chat_id:
                continue                                    # not you: ignore silently
            text = (msg.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            answer = await self.handle(text)
            await self.bot.notifier.send(answer)
            handled += 1
        return handled

    async def run(self) -> None:
        if not self.enabled:
            return
        log.info("telegram commands: listening")
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:                        # network blip: wait and retry
                log.warning("telegram poll failed: %s", exc)
                await asyncio.sleep(15)

    # ------------------------------------------------------------------ commands
    async def handle(self, text: str) -> str:
        cmd, *args = text.split()
        cmd = cmd.split("@")[0].lower()                     # /status@MyBot in group chats
        handler = {"/status": self._status, "/trades": self._trades, "/pnl": self._pnl, "/watch": self._watch, "/market": self._market,
                   "/pause": self._pause, "/resume": self._resume, "/stop": self._stop,
                   "/help": self._help, "/start": self._help}.get(cmd)
        if handler is None:
            return f"Unknown command {cmd}.\n{HELP}"
        try:
            return await handler(args)
        except Exception as exc:
            log.exception("telegram command %s failed", cmd)
            return f"{cmd} failed: {exc}"

    async def _help(self, args: list[str]) -> str:
        return HELP

    async def _status(self, args: list[str]) -> str:
        bot, db = self.bot, self.bot.db
        eq = await db.fetchone("SELECT equity, daily_pnl, gross_exposure FROM equity ORDER BY id DESC LIMIT 1")
        paused = await db.get_control("paused")
        state = f"HALTED: {bot.halted}" if bot.halted else "PAUSED (no new trades)" if paused else "Running"
        lines = [state]
        if eq:
            lines.append(f"Equity ${eq[0]:,.2f}, today {eq[1]:+,.2f}, exposure ${eq[2]:,.0f}")
        for e in bot.engines:
            z = f"z {e.last_stats.zscore:+.2f}" if e.last_stats else "no data yet"
            pos = f"{e.position} since z {e.trade['entry_z']:+.2f}" if e.trade and e.trade.get("entry_z") is not None \
                else str(e.position)
            lines.append(f"{e.name}: {pos}, {z}" + (f", RETIRED ({e.retired})" if e.retired else ""))
        return "\n".join(lines)

    async def _trades(self, args: list[str]) -> str:
        rows = await self.bot.db.fetchall(
            "SELECT pair, direction, pnl, exit_reason, closed_at FROM pair_trades "
            "WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 5")
        if not rows:
            return "No closed trades yet."
        out = []
        for pair, direction, pnl, reason, closed in rows:
            when = datetime.fromtimestamp(closed, NY).strftime("%m-%d %H:%M")
            out.append(f"{when} {pair or ''} {direction}: ${pnl or 0:+.2f} ({(reason or '').split(':')[0]})")
        return "\n".join(out)

    async def _pnl(self, args: list[str]) -> str:
        db = self.bot.db
        now = datetime.fromtimestamp(time.time(), NY)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week = day - timedelta(days=now.weekday())
        parts = []
        for label, since in (("Today", day.timestamp()), ("This week", week.timestamp()), ("All time", 0.0)):
            row = await db.fetchone("SELECT COUNT(*), COALESCE(SUM(pnl), 0), COALESCE(SUM(pnl > 0), 0) "
                                    "FROM pair_trades WHERE closed_at >= ?", (since,))
            n, pnl, wins = row
            parts.append(f"{label}: ${pnl:+,.2f} from {n} trades ({wins} wins)")
        return "\n".join(parts)

    async def _watch(self, args: list[str]) -> str:
        db = self.bot.db
        rows = await db.fetchall(
            "SELECT o.grade, o.pair, o.side, o.zscore, o.prob, o.hist_trades, o.flags FROM opportunities o "
            "JOIN (SELECT pair, MAX(id) AS id FROM opportunities GROUP BY pair) m ON o.id = m.id "
            "WHERE o.grade != 'QUIET' ORDER BY CASE o.grade WHEN 'STRONG' THEN 0 WHEN 'RISKY' THEN 1 WHEN 'AVOID' THEN 2 ELSE 3 END, "
            "o.prob DESC LIMIT 8")
        if not rows:
            return "No opportunities right now (or the watcher isn't running: python watcher.py)."
        out = []
        for grade, pair, side, z, prob, n, flags in rows:
            line = f"{grade} {pair} z={z:+.2f} win rate {prob:.0%} ({n} trades)"
            if side:
                line += f" -> {side}"
            if flags:
                line += f"\n   ! {flags}"
            out.append(line)
        shadow = await db.fetchall("SELECT grade, COUNT(*), COALESCE(SUM(net_pnl), 0) FROM shadow_trades "
                                   "WHERE closed_at IS NOT NULL GROUP BY grade")
        if shadow:
            out.append("Demo results: " + ", ".join(f"{g} {n} trades ${p:+,.2f}" for g, n, p in shadow))
        return "\n".join(out)

    async def _market(self, args: list[str]) -> str:
        import mcp_server
        return await asyncio.to_thread(mcp_server.market_brief, self.bot.cfg.db_path)

    async def _pause(self, args: list[str]) -> str:
        await self.bot.db.set_control("paused", "telegram")
        await self.bot.db.log_circuit_event("paused", by="telegram")
        return "Paused: no new trades. Open trades still exit by their rules. /resume to undo."

    async def _resume(self, args: list[str]) -> str:
        await self.bot.db.set_control("paused", "")
        return "Resumed: new trades allowed."

    async def _stop(self, args: list[str]) -> str:
        if args[:1] != ["confirm"]:
            return "This closes EVERY position and halts the bot. Send '/stop confirm' to do it."
        await self.bot.db.set_control("emergency_stop", "telegram")
        return "Emergency stop sent. The bot will flatten and halt on its next cycle (within a minute)."
