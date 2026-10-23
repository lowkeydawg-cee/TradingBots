# riskbot v2: technical architecture

This document explains how the system works at an engineering level: the
defence-in-depth layers, the per-round data flow and its latency budget, the
concurrency model, the persistence tiers, and how a session can be proven
correct after the fact. Section 9 lists what a further production step would
add; everything before it is built and tested (42 tests).

The directive from v1 still holds and is now checked by replay:
**no outcome prediction, no martingale, no ML on RNG data.**

---

## 1. Defence in depth

An order has to pass seven independent layers. Each layer is a different piece
of code with its own state, so one bug cannot open the whole path.

```
 WebSocket frame
   │
   ▼
 L1  Transport          ws_transport.py   max frame 64 KiB, bounded queue, ping/pong
   │                                       keepalive, close-code mapping, loopback only
   ▼
 L2  Schema validation  models.FeedEvent   strict types, finite numbers, closes_at ≥ ts,
   │                                       multiplier ≥ 1; any violation → TRIP
   ▼
 L3  Feed health        bot_core          latency soft/hard limits, clock-skew limit,
   │                                       round-id monotonicity, heartbeat watchdog
   ▼
 L4  FSM gate           state_machine     state must be MONITORING, not halted;
   │                                       CAS MONITORING→ACTIVE reserves the one slot
   ▼
 L5  Risk engine        risk_engine       fractional sizing, de-risking, daily budget cap,
   │                                       anti-martingale clamp, invariant checks
   ▼
 L6  Outer guard        guard.py          own ledger: hard stake cap, one order/round,
   │                                       rate limit, worst-case session loss, kill file
   ▼
 L7  Executor           executor.py       paper only; ack timeout → assume full loss
```

L5 and L6 deliberately overlap. L6 shares no state with L5, counts open stake as
already lost, and its limits must be **at least as wide** as L5's (`AppConfig`
refuses to load otherwise). In normal operation L6 never fires. If it does, an
inner layer is wrong, so the bot trips instead of carrying on
(`test_guard_violation_trips_breaker`).

## 2. One round, step by step

| # | Event | What happens | Can trip? |
|---|---|---|---|
| 1 | `betting_open` arrives | parse (L2) → latency and skew check (L3) → round id must not go backwards | yes |
| 2 | | under the FSM lock: state, halted, duplicate round, betting-window margin | no (skips) |
| 3 | | `RiskEngine.evaluate()`; the decision is written to the journal **whether approved or not** | no |
| 4 | | add round to `_bet_rounds`, CAS → `ACTIVE`, record `_pending` | no |
| 5 | | `GuardedExecutor.place_bet` (L6) → paper executor (L7), under a 2 s timeout | yes |
| 6 | `round_start` / `tick` | latency checks only; ticks are never used for decisions | yes |
| 7 | `crash` | log the round → settle (win iff crash ≥ fixed target) → journal + SQLite + guard ledger → breach mapping | yes (drawdown) |
| 8 | | `ACTIVE → MONITORING`, or `→ COOLING_DOWN` (loss streak), or `→ IDLE` (session cap) | |

**Measured CPU cost of the decision path** on the build container:
`FeedEvent.parse` ≈ 5 µs and `RiskEngine.evaluate` ≈ 9 µs. The bot's own work
per message is roughly four orders of magnitude below the 300 ms minimum
betting margin, so network latency is the only budget that matters, and that is
what L3 polices. (v1 used `statistics.pstdev`, which does exact rational
arithmetic. Replacing it with a float computation made sizing about 100× cheaper.)

## 3. Concurrency model

There is one asyncio event loop and these tasks:

| Task | Responsibility | Shares |
|---|---|---|
| consumer | reads the socket and handles messages **strictly in order** | FSM lock |
| watchdog | silent feed, kill-switch file | trip only |
| cooldown | one per `COOLING_DOWN` period | FSM lock |
| db-writer | drains the audit queue in batches | queue only |
| ops server | `/status`, `/metrics`, `/kill` | read-only snapshot, trip |

Three rules make this safe:
1. **One sequential consumer.** Two feed messages are never handled at the same time, so whole classes of ordering races cannot occur.
2. **Check-then-act under the lock, with no `await` between the check and the reservation.** In asyncio, code without an `await` cannot be interleaved, so the reservation is atomic.
3. **`trip()` is synchronous and lock-free.** It is never blocked by a lock holder. It sets `halted` first, and every gate reads `halted`.

The hot path never awaits the database. Logging is `put_nowait`.

## 4. Transport layer

`WebSocketFeed` (websockets ≥ 13, asyncio API) implements the same
`FeedConnection` protocol as the in-process mock, so the core cannot tell the
two apart. All scenarios produce the same outcomes over a real socket (section 7).

| Concern | Mechanism |
|---|---|
| Dead peer that sends nothing | protocol ping every 2 s, 2 s pong timeout → `FeedDisconnected` |
| Application silence | watchdog: trip if exposure is open, otherwise force a reconnect |
| Memory exhaustion or floods | `max_size` 64 KiB per frame, `max_queue` 256 frames (TCP backpressure beyond that) |
| Decompression bombs and jitter | `compression=None` |
| Clean end vs. failure | close 1000 → `FeedClosed` (graceful IDLE); 1006 or anything else → `FeedDisconnected` |
| Reconnect storms | exponential backoff 0.5 s to 8 s, budget of 3 attempts, reset only after a complete round |
| Scope | non-loopback URLs are refused both in `FeedConfig` and in `WebSocketFeed` |

`mock_server.py` serves the simulator on localhost. A scripted mid-round drop
calls `transport.abort()`, so the client sees a real 1006 with no close frame,
which is the same thing a pulled cable produces.

## 5. Persistence tiers

| Tier | File | Purpose | Written | On failure |
|---|---|---|---|---|
| Audit DB | `riskbot.sqlite3` (WAL) | queryable history: sessions, rounds, bets, transitions, risk events | async batches, one transaction each | retry with backoff → JSONL fallback → trip |
| Fallback | `*.fallback.jsonl` | records the DB refused | sync append + fsync | logged; last resort |
| Journal | `journal.jsonl` | tamper-evident decision trail (SHA-256 chain) | buffered, flushed per round, fsynced on trip and close | n/a |
| Latch | `*.CIRCUIT_BREAKER.latch` | safety state that survives restarts | sync + fsync at the moment of the trip | never raises |
| Kill file | `data/KILL` | out-of-band operator stop | by a human | checked before every order and every 250 ms |

Restarts cannot weaken the limits:
- the latch blocks startup;
- the daily drawdown baseline is reloaded from today's sessions;
- an earlier unclean session (no closing equity recorded) blocks startup until someone reconciles it.

## 6. Proving a session afterwards: journal and replay

Each journal record stores `prev` (the previous record's hash) and `hash`
(SHA-256 of its own canonical JSON). `verify_chain` walks the file and fails at
the first edited, deleted or reordered line (`test_tampered_journal_is_detected`).

`python -m riskbot.replay data/journal.jsonl` then:
1. verifies the chain;
2. rebuilds a fresh `RiskEngine` from each `session_start` record (limits, day-open equity, equity);
3. applies the recorded settlements in order;
4. at every recorded `decision`, asks the engine to decide again and compares approval, stake and reason.

A clean replay shows that every stake was a function of the configured limits
and the bot's own past results, and of nothing else. That is the
"no prediction" directive turned into something you can check. It works across
multiple sessions on one chain, including day carry-over
(`test_journal_chain_and_deterministic_replay`).

## 7. Verification runs

Every scenario was run over both transports and then replayed:

| Transport | Scenario | Final state | Trip reason | Bets | Unresolved | P&L | Replay |
|---|---|---|---|---|---|---|---|
| in-proc | normal | IDLE | none | 46 | 0 | -28.08 | ok, 46/46 |
| in-proc | disconnect | CIRCUIT_BREAKER | feed lost with open exposure | 6 | 1 | -13.85 | ok |
| in-proc | drawdown | CIRCUIT_BREAKER | daily drawdown limit reached | 24 | 0 | -30.00 | ok |
| in-proc | latency-spike | CIRCUIT_BREAKER | extreme feed latency | 4 | 0 | -10.84 | ok |
| WebSocket | normal | IDLE | none | 47 | 0 | -29.51 | ok, 47/47 |
| WebSocket | disconnect | CIRCUIT_BREAKER | feed lost with open exposure | 6 | 1 | -13.85 | ok |
| WebSocket | drawdown | CIRCUIT_BREAKER | daily drawdown limit reached | 24 | 0 | -30.00 | ok |
| WebSocket | latency-spike | CIRCUIT_BREAKER | extreme feed latency | 4 | 0 | -10.84 | ok |

## 8. What the policy actually does: Monte Carlo

`python -m riskbot.montecarlo --sessions 5000 --rounds 300` runs the real
`RiskEngine` over 5,000 synthetic sessions (3% house edge, 1.5× fixed target,
cool-down of 6 rounds). As a baseline, it also runs a flat 1%-of-bankroll stake
with no limits on **the same rounds**.

| Metric | Governed (defaults) | Flat stake, no limits |
|---|---|---|
| Mean P&L | **-2.07%** | -8.88% |
| Median P&L | -3.15% | -9.00% |
| 5th percentile | -5.00% | -28.5% |
| Worst session | **-5.00%** | -51.0% |
| 95th-percentile max drawdown | 5.00% | 33.5% |
| Sessions that hit the daily stop | 6.4% | n/a |
| Sessions that ended in profit | 18.6% | 21.8% |
| Expected value per unit staked | **-0.03** | -0.03 |

How to read this:
- **The governance works as specified.** Across 5,000 sessions no session lost more than the 5% daily limit, against -51% without limits.
- **Governance does not create an edge.** Every unit staked still loses 3% on average, whatever the sizing. The lower mean loss comes from staking less and stopping earlier, not from winning more. About four sessions in five end down.
- The simulator exists to tune limits on evidence, for example how often a given drawdown limit stops you, before running anything live.

## 9. Further production hardening (not built)

These are the next steps for a production-grade deployment, in priority order:

1. **Process isolation.** Run L5/L6 as a separate risk-kernel process that owns the only credential able to place orders. The strategy process sends order requests over a Unix socket, and the kernel signs or forwards only what passes. A crash or compromise of the strategy process then cannot bypass limits.
2. **Supervisor watchdog.** Use systemd `WatchdogSec` with `sd_notify` pings from the event loop. A stalled loop gets killed and, because of the latch, stays down.
3. **Venue reconciliation.** Compare settled bets against the venue's own history every N rounds, and trip on any mismatch.
4. **Time discipline.** Run chrony with monitored offset and feed the offset into L3, so latency readings are trustworthy.
5. **External journal anchoring.** Periodically publish the journal head hash somewhere the bot cannot write, so a whole-file rewrite is also detectable.
6. **Chaos and property testing.** Use toxiproxy for latency, jitter and packet loss on the real socket, and hypothesis for state-machine and sizing properties.
7. **Performance, if ever needed.** uvloop and orjson. At about 15 µs per message there is no current need.
8. **Alerting.** Scrape `/metrics` and alert on `riskbot_halted == 1`, `riskbot_feed_latency_ms` above the soft limit, and any increase in `riskbot_unresolved_total`.

Integrating with a real operator is intentionally out of scope. The endpoint
validation enforces that, and many operators' terms prohibit automated play.
