"""Alerts to your phone: ntfy push notifications and/or Telegram.

ntfy (recommended, 2 minutes, no account, no phone number):
1. Install the free "ntfy" app (iPhone App Store / Google Play) or open https://ntfy.sh/app.
2. Pick a long random topic name nobody can guess, e.g. ``pairs-saint-7f3k9q2x``,
   and subscribe to it in the app.
3. Put it in ``.env``: ``NTFY_TOPIC=pairs-saint-7f3k9q2x``. Halts arrive as
   urgent notifications; trades and reports as normal ones.

Anyone who knows the topic name can read it, so keep it long and private.
For full privacy run your own ntfy server and set ``NTFY_SERVER``.

Telegram (optional, also lets you send commands back): talk to @BotFather,
send ``/newbot``, copy the token; message your bot, open
``https://api.telegram.org/bot<TOKEN>/getUpdates`` and copy ``chat.id``; put
both in ``.env`` as ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID``.

With neither configured every call is a no-op, so the bot runs the same.
Sending never raises: a failed alert is logged and trading carries on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, token: str = "", chat_id: str = "", prefix: str = "pairs-bot",
                 ntfy_topic: str = "", ntfy_server: str = "https://ntfy.sh") -> None:
        self.token, self.chat_id, self.prefix = token, chat_id, prefix
        self.ntfy_topic, self.ntfy_server = ntfy_topic, ntfy_server.rstrip("/")
        self.sent: list[str] = []            # what went out (useful in tests and logs)

    @classmethod
    def from_settings(cls, cfg, prefix: str = "pairs-bot") -> "Notifier":
        return cls(cfg.telegram_token, cfg.telegram_chat_id, prefix, cfg.ntfy_topic, cfg.ntfy_server)

    @property
    def telegram(self) -> bool:
        return bool(self.token and self.chat_id)

    @property
    def enabled(self) -> bool:
        return self.telegram or bool(self.ntfy_topic)

    def _ntfy(self, text: str) -> None:
        urgent = text.split("] ", 1)[-1].startswith(("HALTED", "RECONCILE MISMATCH", "MISMATCH"))
        req = urllib.request.Request(f"{self.ntfy_server}/{self.ntfy_topic}", data=text.encode(),
                                     headers={"Title": self.prefix, "Priority": "urgent" if urgent else "default",
                                              "Tags": "rotating_light" if urgent else "chart_with_upwards_trend"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()

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
        for on, send, name in ((self.telegram, self._post, "telegram"), (bool(self.ntfy_topic), self._ntfy, "ntfy")):
            if not on:
                continue
            try:
                await asyncio.to_thread(send, text)
            except Exception as exc:
                log.warning("%s alert failed: %s", name, exc)
