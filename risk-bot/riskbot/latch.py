"""Persistent circuit-breaker latch.

The in-memory CIRCUIT_BREAKER state dies with the process, so restarting the
bot would otherwise be a way around the emergency stop. On every trip the bot
writes (and fsyncs) a latch file; ``RiskBot.run()`` refuses to start while it
exists. Clearing it is a deliberate, manual operator action:

    rm data/CIRCUIT_BREAKER.latch
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)


class LatchEngaged(RuntimeError):
    """A previous session tripped the circuit breaker and nobody has cleared it."""


def check_latch(path: str) -> None:
    p = Path(path)
    if p.exists():
        try:
            detail = p.read_text(encoding="utf-8").strip()
        except OSError as exc:
            detail = f"<unreadable: {exc}>"
        raise LatchEngaged(f"circuit breaker latch present at {p}: {detail}. "
                           "Review the incident, then delete the file to re-arm.")


def engage_latch(path: str, reason: str, session_id: str) -> None:
    """Synchronously persist the latch. Never raises: the in-memory trip has
    already happened and must not be undone by an I/O error here."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"reason": reason, "session_id": session_id, "at": time.time()}))
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        log.exception("could not write circuit-breaker latch at %s", path)
