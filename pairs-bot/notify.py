"""Alerts to your phone through a Telegram bot.

Setup (5 minutes):
1. In Telegram, talk to @BotFather, send ``/newbot`` and copy the token.
2. Send any message to your new bot, then open
   ``https://api.telegram.org/bot<TOKEN>/getUpdates`` and copy ``chat.id``.
3. Put both in ``.env``: ``TELEGRAM_BOT_TOKEN=...`` and ``TELEGRAM_CHAT_ID=...``.

Without them every call is a no-op, so the bot runs the same either way.
Sending never raises: a failed alert is logged and trading carries on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, token: str = "", chat_id: str = "", prefix: str = "pairs-bot") -> None:
        self.token, self.chat_id, self.prefix = token, chat_id, prefix
        self.sent: list[str] = []            # what went out (useful in tests and logs)

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _post(self, text: str) -> None:
        body = json.dumps({"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()

    async def send(self, text: str) -> None:
        text = f"[{self.prefix}] {text}"
        self.sent.append(text)
        log.info("notify: %s", text)
        if not self.enabled:
            return
        try:
            await asyncio.to_thread(self._post, text)
        except Exception as exc:
            log.warning("telegram alert failed: %s", exc)
