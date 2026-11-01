"""How the watcher sees the market: a plain-language brief and alerts when it changes.

Everything here DESCRIBES what the prices are doing now. Nothing predicts where
they go next. The brief answers four questions a pairs trader cares about:

1. Mood: are price moves calm, normal or stressed compared with recent weeks?
   (median volatility ratio across all pairs: the last hour of spread moves
   vs. their usual size)
2. Relationships: how many pairs are still cointegrated? When that share
   drops, stocks have stopped moving together and pairs signals get less reliable.
3. Where the action is: each sector's move today, its mood, and its signals.
4. What changed since the last brief: pairs that broke or recovered, new
   STRONG signals, a mood change.

Alerts go out the moment something material changes (with a cooldown so the
same alert doesn't repeat): the market turns stressed or calms down, a sector
gets stressed, a pair the bot trades (or one that was STRONG) breaks, or the
whole market moves sharply today.

The watcher sends a full brief to your phone three times a day (after the
open, midday, at the close) and stores every brief and alert in the database,
where the dashboard and Jarvis read them.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo("America/New_York")

CALM, STRESSED, SECTOR_STRESSED = 0.8, 1.5, 2.0     # median vol_ratio thresholds
BIG_MOVE = 0.015                                    # 1.5% average move today
HEALTHY = ("STRONG", "WATCH")                       # graded on a working pair
BRIEF_TIMES = ((9, 45, "open"), (12, 30, "midday"), (16, 0, "close"))   # New York
ALERT_COOLDOWN = 2 * 3600                           # seconds before the same alert repeats


def mood_of(vol_ratio: float) -> str:
    if vol_ratio != vol_ratio:          # NaN
        return "unknown"
    return "stressed" if vol_ratio >= STRESSED else "calm" if vol_ratio <= CALM else "normal"


def day_moves(closes: pd.DataFrame) -> pd.Series:
    """Each stock's move since the previous New York day's last close (empty if not enough data)."""
    if closes.empty or not isinstance(closes.index, pd.DatetimeIndex):
        return pd.Series(dtype=float)
    idx = closes.index if closes.index.tz else closes.index.tz_localize("UTC")
    days = idx.tz_convert(NY).date
    today = days[-1]
    before = closes[days < today]
    if before.empty:
        return pd.Series(dtype=float)
    return (closes.iloc[-1] / before.iloc[-1] - 1).dropna()


@dataclass
class SectorRead:
    name: str
    pairs: int
    healthy: int
    vol_ratio: float
    move: float | None
    signals: dict[str, int]

    @property
    def mood(self) -> str:
        return mood_of(self.vol_ratio)


@dataclass
class MarketRead:
    ts: float
    pairs: int
    cointegrated: int
    vol_ratio: float
    move: float | None
    grades: dict[str, int]
    sectors: list[SectorRead]
    changes: list[str] = field(default_factory=list)

    @property
    def mood(self) -> str:
        return mood_of(self.vol_ratio)

    @property
    def coint_share(self) -> float:
        return self.cointegrated / self.pairs if self.pairs else 0.0

    def text(self, label: str = "") -> str:
        when = datetime.fromtimestamp(self.ts, NY).strftime("%a %H:%M")
        head = f"Market brief{f' ({label})' if label else ''}, {when} New York"
        move = f", stocks {self.move:+.1%} today on average" if self.move is not None else ""
        lines = [head,
                 f"Mood: {self.mood} (moves {self.vol_ratio:.1f}x their usual size){move}.",
                 f"Relationships: {self.cointegrated} of {self.pairs} pairs still move together "
                 f"({self.coint_share:.0%}). " + _coint_comment(self.coint_share)]
        sig = ", ".join(f"{self.grades.get(g, 0)} {g.lower()}" for g in ("STRONG", "RISKY", "AVOID"))
        lines.append(f"Signals: {sig}; {self.grades.get('WATCH', 0)} healthy pairs waiting.")
        for s in sorted(self.sectors, key=lambda s: -s.vol_ratio):
            mv = f" {s.move:+.1%}" if s.move is not None else ""
            sg = ", ".join(f"{n} {g.lower()}" for g, n in s.signals.items() if n)
            lines.append(f"- {s.name}:{mv}, {s.mood}, {s.healthy}/{s.pairs} pairs healthy" + (f", {sg}" if sg else ""))
        if self.changes:
            lines.append("Since last brief: " + "; ".join(self.changes[:8]) + ("; ..." if len(self.changes) > 8 else ""))
        lines.append("This describes the market now; it is not a forecast.")
        return "\n".join(lines)


def _coint_comment(share: float) -> str:
    if share >= 0.5:
        return "Good conditions for pairs trading."
    if share >= 0.25:
        return "Mixed: trust only STRONG signals."
    return "Most pairs have decoupled: a poor day for pairs trading."


def read_market(opps, closes: pd.DataFrame, ts: float) -> MarketRead:
    """Summarize one scan (a list of watcher.Opportunity) and the prices behind it."""
    moves = day_moves(closes)
    by_sector: dict[str, list] = {}
    for o in opps:
        by_sector.setdefault(o.sector, []).append(o)

    def med(xs):
        xs = [x for x in xs if x == x and x != float("inf")]
        return statistics.median(xs) if xs else float("nan")

    def sector_move(sector_opps) -> float | None:
        syms = {s for o in sector_opps for s in o.pair.split("/")} & set(moves.index)
        return float(moves[sorted(syms)].mean()) if syms else None

    sectors = []
    for name, so in sorted(by_sector.items()):
        signals = {g: sum(o.grade == g for o in so) for g in ("STRONG", "RISKY", "AVOID")}
        sectors.append(SectorRead(name, len(so), sum(o.grade in HEALTHY for o in so),
                                  med([o.vol_ratio for o in so]), sector_move(so), signals))
    grades: dict[str, int] = {}
    for o in opps:
        grades[o.grade] = grades.get(o.grade, 0) + 1
    return MarketRead(ts=ts, pairs=len(opps), cointegrated=sum(o.coint_pvalue < 0.05 for o in opps),
                      vol_ratio=med([o.vol_ratio for o in opps]),
                      move=float(moves.mean()) if len(moves) else None, grades=grades, sectors=sectors)


class Insights:
    """Remembers the last scan, spots what changed, and decides which alerts and briefs to send."""

    def __init__(self, watched_pairs: set[str] | None = None) -> None:
        self.watched = watched_pairs or set()          # pairs the trading bot trades: always alert if they break
        self.prev_grades: dict[str, str] = {}
        self.prev: MarketRead | None = None
        self.last_alert: dict[str, float] = {}
        self.briefs_sent: set[tuple[str, str]] = set()  # (date, slot)
        self.pending_changes: list[str] = []            # accumulated between briefs
        self.mood_at_brief: str | None = None           # mood when the last brief went out

    def update(self, opps, closes: pd.DataFrame, ts: float) -> tuple[MarketRead, list[str]]:
        """Returns this scan's read and the alerts to send now."""
        read = read_market(opps, closes, ts)
        alerts: list[str] = []
        changes: list[str] = []
        for o in opps:
            before = self.prev_grades.get(o.pair)
            if before is None or before == o.grade:
                continue
            if before in HEALTHY + ("RISKY",) and o.grade == "AVOID":
                why = o.flags[0] if o.flags else "failed a hard check"
                changes.append(f"{o.pair} broke ({why})")
                if o.pair in self.watched or before == "STRONG":
                    alerts.append(self._once(f"broke:{o.pair}", ts,
                                             f"PAIR BROKE {o.pair}: was {before}, now AVOID ({why})."
                                             + (" The bot trades this pair." if o.pair in self.watched else "")))
            elif before == "AVOID" and o.grade in HEALTHY:
                changes.append(f"{o.pair} recovered")
            elif o.grade == "STRONG":
                changes.append(f"{o.pair} turned STRONG")
        if self.prev is not None:
            if read.mood != self.prev.mood and "unknown" not in (read.mood, self.prev.mood):
                if read.mood == "stressed":
                    alerts.append(self._once("mood", ts, f"MARKET STRESSED: spread moves are {read.vol_ratio:.1f}x "
                                                         f"normal across {read.pairs} pairs. Signals are less reliable "
                                                         "until it calms; the bot already skips entries in vol spikes."))
                elif self.prev.mood == "stressed":
                    alerts.append(self._once("mood", ts, f"Market calmer: moves back to {read.vol_ratio:.1f}x normal."))
            drop = self.prev.coint_share - read.coint_share
            if drop >= 0.2:
                alerts.append(self._once("coint", ts, f"DECOUPLING: only {read.cointegrated} of {read.pairs} pairs still "
                                                      f"move together (was {self.prev.cointegrated})."))
            prev_moods = {s.name: s.mood for s in self.prev.sectors}
            for s in read.sectors:
                if s.vol_ratio >= SECTOR_STRESSED and prev_moods.get(s.name) != "stressed":
                    alerts.append(self._once(f"sector:{s.name}", ts, f"{s.name.upper()} STRESSED: moves {s.vol_ratio:.1f}x "
                                                                     f"normal" + (f", sector {s.move:+.1%} today" if s.move is not None else "") + "."))
        if read.move is not None and abs(read.move) >= BIG_MOVE:
            # once per day and direction, so a sell-off doesn't buzz your phone every 15 minutes
            day = datetime.fromtimestamp(ts, NY).date().isoformat()
            key = f"bigmove:{day}:{'up' if read.move > 0 else 'down'}"
            if key not in self.last_alert:
                self.last_alert[key] = ts
                alerts.append(f"BIG MOVE: stocks {read.move:+.1%} today on average. Watch for pairs breaking.")
        for c in changes:                               # a pair flapping between grades is listed once
            if c not in self.pending_changes:
                self.pending_changes.append(c)
        if self.mood_at_brief is None and read.mood != "unknown":
            self.mood_at_brief = read.mood
        mood_change = ([f"mood {self.mood_at_brief} -> {read.mood}"]
                       if self.mood_at_brief not in (None, read.mood) and read.mood != "unknown" else [])
        read.changes = mood_change + list(self.pending_changes)
        self.prev, self.prev_grades = read, {o.pair: o.grade for o in opps}
        return read, [a for a in alerts if a]

    def _once(self, key: str, ts: float, text: str) -> str:
        if ts - self.last_alert.get(key, -1e18) < ALERT_COOLDOWN:
            return ""
        self.last_alert[key] = ts
        return text

    def brief_due(self, ts: float) -> str | None:
        """The brief slot ('open', 'midday', 'close') that is due now and not sent yet today."""
        now = datetime.fromtimestamp(ts, NY)
        due = None
        for hour, minute, label in BRIEF_TIMES:
            if (now.hour, now.minute) >= (hour, minute):
                due = label
        if due is None or (now.date().isoformat(), due) in self.briefs_sent:
            return None
        return due

    def mark_sent(self, ts: float, slot: str) -> None:
        self.briefs_sent.add((datetime.fromtimestamp(ts, NY).date().isoformat(), slot))
        self.pending_changes.clear()
        if self.prev is not None and self.prev.mood != "unknown":
            self.mood_at_brief = self.prev.mood
