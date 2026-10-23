"""Hash-chained, append-only decision journal.

Every record carries the SHA-256 of the previous record, so editing, deleting
or reordering any line breaks verification from that point on. The journal is
the input to ``replay.py``, which re-derives every sizing decision from it.

Record layout (one JSON object per line, canonical encoding):
    {"seq": n, "at": epoch_s, "kind": "...", "data": {...}, "prev": hex, "hash": hex}

Writes are buffered and flushed at round boundaries and on close; the SQLite
audit log remains the durable store, the journal is the tamper-evident one.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _digest(rec: dict[str, Any]) -> str:
    body = {k: rec[k] for k in ("seq", "at", "kind", "data", "prev")}
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


class Journal:
    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._seq, self._prev = 0, GENESIS
        if self.path.exists() and self.path.stat().st_size:
            # Continue an existing chain rather than starting a second genesis.
            last = self.path.read_text(encoding="utf-8").rstrip("\n").rsplit("\n", 1)[-1]
            rec = json.loads(last)
            self._seq, self._prev = rec["seq"], rec["hash"]
        self._fh = open(self.path, "a", encoding="utf-8")

    def append(self, kind: str, **data: Any) -> str:
        self._seq += 1
        rec = {"seq": self._seq, "at": self._clock(), "kind": kind, "data": data, "prev": self._prev}
        # Round-trip through JSON so the hashed form is exactly what is stored.
        rec = json.loads(_canonical(rec))
        rec["hash"] = _digest(rec)
        self._fh.write(_canonical(rec) + "\n")
        self._prev = rec["hash"]
        return rec["hash"]

    def flush(self, fsync: bool = False) -> None:
        self._fh.flush()
        if fsync:
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        if not self._fh.closed:
            self.flush(fsync=True)
            self._fh.close()

    @property
    def head(self) -> str:
        return self._prev


@dataclass(frozen=True, slots=True)
class ChainCheck:
    ok: bool
    records: int
    error: str | None = None


def read_records(path: str) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def verify_chain(path: str) -> ChainCheck:
    prev, n = GENESIS, 0
    for rec in read_records(path):
        n += 1
        if rec.get("prev") != prev:
            return ChainCheck(False, n, f"seq {rec.get('seq')}: prev hash mismatch")
        if _digest(rec) != rec.get("hash"):
            return ChainCheck(False, n, f"seq {rec.get('seq')}: content hash mismatch")
        if rec.get("seq") != n:
            return ChainCheck(False, n, f"seq {rec.get('seq')}: expected {n}")
        prev = rec["hash"]
    return ChainCheck(True, n)
