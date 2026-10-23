# riskbot: risk governance, logging and disciplined execution

An asynchronous Python 3.11+ bot that sits between a crash-style game feed and an
executor and decides **whether** a fixed-target bet is allowed and **how small** it
must be. It never decides what will happen next.

**Directive, enforced in code:**

| Rule | Where it is enforced |
|---|---|
| No outcome prediction | `RiskEngine` never receives crash points. The cash-out target is a fixed config value (`RiskLimits.target_cashout`). |
| No martingale | `ALLOW_STAKE_INCREASE_AFTER_LOSS: Final = False`. After a loss the stake is clamped to the previous stake, and an explicit invariant check denies the bet if that is ever violated. Every de-risking multiplier can only shrink the stake. |
| No ML on RNG data | There is no model. Crash points are only written to the `rounds` audit table. |

**v2** adds a real WebSocket transport (against a local mock server), an
independent outer pre-trade guard, a hash-chained journal with deterministic
replay, a local ops endpoint, and a Monte Carlo simulator for the risk policy.
See [ARCHITECTURE.md](ARCHITECTURE.md) for the full technical design.

Only the mock feed (in-process or over a localhost socket) and a paper executor are included. There is no
adapter for any real operator, and none should be added without checking that
operator's terms, since automated play is commonly prohibited. Risk governance
caps losses; it does not create an edge. Over 60 mock rounds with a 3% house edge
the normal scenario finished at about -2.7%, which is what you should expect.

---

## 1. Folder structure

```
risk-bot/
├── pyproject.toml            # package metadata, pytest config (asyncio_mode=auto)
├── requirements.txt          # runtime: aiosqlite
├── requirements-dev.txt      # + pytest, pytest-asyncio
├── README.md                 # this file: usage, v1 audit
├── ARCHITECTURE.md           # v2 technical design, verification runs, Monte Carlo
├── riskbot/
│   ├── __init__.py
│   ├── __main__.py           # CLI: python -m riskbot --scenario ...
│   ├── config.py             # frozen, validated limits (risk, latency, feed, storage)
│   ├── models.py             # BotState, FeedEvent (strict parser), Position, Settlement
│   ├── state_machine.py      # async FSM: lock, compare-and-set, lock-free trip()
│   ├── risk_engine.py        # fractional + volatility/drawdown/streak-scaled sizing
│   ├── db_logger.py          # aiosqlite audit log: queue, batching, retries, JSONL fallback
│   ├── latch.py              # persistent circuit-breaker latch (survives restarts)
│   ├── feed.py               # FeedConnection protocol + MockCrashFeed with fault injection
│   ├── ws_transport.py       # v2: real WebSocket client (loopback only), keepalive, limits
│   ├── mock_server.py        # v2: serves the mock feed over a localhost WebSocket
│   ├── executor.py           # Executor protocol + PaperExecutor
│   ├── guard.py              # v2: independent outer pre-trade guard + kill-switch file
│   ├── journal.py            # v2: SHA-256 hash-chained decision journal
│   ├── replay.py             # v2: verify chain and re-derive every decision
│   ├── ops_server.py         # v2: local /status, /metrics (Prometheus), /kill
│   ├── montecarlo.py         # v2: policy simulation vs. a flat-stake baseline
│   └── bot_core.py           # RiskBot: consumer, execution gate, settlement, watchdog
└── tests/
    ├── conftest.py
    ├── test_state_machine.py # transitions, absorbing breaker, 100-way CAS race
    ├── test_risk_engine.py   # caps, randomised anti-martingale property, no overshoot
    ├── test_db_logger.py     # round trip, real SQLite EXCLUSIVE lock -> fallback
    ├── test_bot_core.py      # end-to-end runs, one per safety mechanism
    ├── test_guard.py         # v2: outer guard limits
    └── test_v2_integration.py# v2: real socket, guard trip, kill file, journal/replay, ops, Monte Carlo
```

Patterns used: ports and adapters (`FeedConnection`, `Executor` are protocols),
immutable configuration, a single-writer actor for the database, and an explicit
FSM with a transition table as the single source of truth for what the bot may do.

## 2. Running it

```bash
pip install -r requirements-dev.txt
python -m pytest -q                               # 42 tests, ~11 s
python -m riskbot --scenario normal -v            # 60 mock rounds
python -m riskbot --scenario drawdown             # trips at exactly -3.00%
python -m riskbot --scenario latency-spike        # 2.5 s stale message -> trip
python -m riskbot --scenario disconnect           # socket drops mid-round -> trip
python -m riskbot --scenario loss-streak          # 3 losses -> cool-down -> resume

# v2
python -m riskbot --transport ws --ops-port 9100  # real localhost WebSocket + ops endpoint
curl -s localhost:9100/status ; curl -s localhost:9100/metrics ; curl -XPOST localhost:9100/kill
touch data/KILL                                   # out-of-band stop
python -m riskbot.replay data/journal.jsonl       # verify chain + re-derive every decision
python -m riskbot.montecarlo --sessions 5000      # outcome distribution of the policy
python -m riskbot.mock_server --port 8765         # standalone mock feed server
```

Each run prints a JSON report and writes `data/riskbot.sqlite3`
(`sessions`, `rounds`, `bets`, `state_transitions`, `risk_events`).

Results from the run used to verify this build:

| Scenario | Final state | Trip reason | Bets | Unresolved | P&L |
|---|---|---|---|---|---|
| normal | IDLE | none | 47 | 0 | -27.04 |
| latency-spike | CIRCUIT_BREAKER | extreme feed latency | 4 | 0 | -10.84 |
| disconnect | CIRCUIT_BREAKER | feed lost with open exposure | 6 | 1 (booked as full loss) | -13.85 |
| drawdown | CIRCUIT_BREAKER | daily drawdown limit reached | 24 | 0 | -30.00 (limit 3%) |
| loss-streak | IDLE | none, cool-down then resumed | 53 | 0 | +119.96 (scripted wins) |

Re-running any scenario against the same database after a trip exits with
`refusing to start: circuit breaker latch present ...`.

## 3. How it works

### State machine

```
IDLE ──start──▶ MONITORING ──bet──▶ ACTIVE ──settled──▶ MONITORING
                    ▲                  │
                    │            loss streak
               cool-down               ▼
               elapsed ◀──────── COOLING_DOWN

any state ──trip()──▶ CIRCUIT_BREAKER   (absorbing; no outgoing edges)
```

`ALLOWED_TRANSITIONS` in `state_machine.py` is the full table. `CIRCUIT_BREAKER`
maps to an empty set, so even a coding mistake elsewhere raises
`IllegalTransition` or returns False rather than leaving the breaker.

### Position sizing

Each step can only shrink the stake:

1. **Fractional risk:** `equity × base_risk_fraction` (default 0.5%).
2. **Volatility scaling:** `× min(1, vol_target / σ)`, where σ is the standard deviation of the bot's **own** per-round returns over the last 20 bets.
3. **Drawdown de-risking:** `× (1 − drawdown / max_daily_drawdown)`.
4. **Loss-streak scaling:** `× 1 / (1 + consecutive_losses)`.
   Steps 2 to 4 are each floored at `min_scale` (0.25) so the drawdown limit is actually reached and enforced instead of being approached asymptotically forever.
5. **Hard caps:** `min(1% of equity, 25.00 absolute, remaining daily drawdown budget)`. The budget cap means a single loss can never take the account past the daily limit.
6. **Anti-martingale clamp:** never above the previous stake after a loss.
7. Floor to cents and deny if below `min_stake`.

### Execution gate (`RiskBot._maybe_bet`)

With the FSM lock held, and no `await` until the reservation is made:
state must be `MONITORING` → not halted → round not already bet → betting window
has at least `min_bet_margin_ms` left → `RiskEngine.evaluate()` approves → round
id added to `_bet_rounds` → compare-and-set `MONITORING → ACTIVE` → order recorded
as `_pending` → **only then** `await executor.place_bet(...)` under
`execution_timeout_s`.

---

## 4. Self-critique and audit

Reviewed as a senior security and systems engineer. Each claim below is backed by
a test in `tests/` unless marked as a known limitation.

### 4.1 Race conditions

The design keeps races small by construction: one consumer task reads the feed
and handles messages strictly in order, so two feed events are never processed
concurrently. The remaining concurrency is between the consumer, the watchdog,
the cool-down timer and the DB writer.

| Race | What could go wrong | How it is prevented | Evidence |
|---|---|---|---|
| Duplicate `betting_open` for the same round (WS retransmit, reconnect replay) | Two bets on one round, doubling exposure | `_bet_rounds` set is checked and updated under the FSM lock before the first `await`; the `MONITORING → ACTIVE` CAS also fails for the second attempt because the state is already `ACTIVE` | `test_duplicate_betting_open_places_one_bet` |
| Many coroutines trying to claim `ACTIVE` | More than one open position | `transition(..., expected=MONITORING)` is compare-and-set under `asyncio.Lock` | `test_compare_and_set_under_concurrency` (100-way race, exactly one winner) |
| Cool-down timer fires while a trip happens, or vice versa | Timer moves a tripped bot back to `MONITORING` | Timer re-checks `state is COOLING_DOWN and not halted` under the lock and uses CAS; transitions out of the breaker are impossible anyway | `test_circuit_breaker_is_absorbing` |
| Trip while another coroutine holds the lock (e.g. awaiting the executor) | Emergency stop delayed by up to `execution_timeout_s` | `trip()` is synchronous and lock-free. In asyncio a function with no `await` runs atomically relative to every other coroutine, so the state and `halted` event change immediately; every gate re-checks `halted` | `test_trip_does_not_wait_for_lock` |
| Trip arrives while an order is in flight | Order acknowledged after the stop, position unknown | `run()` cancels the consumer on trip; the in-flight order stays in `_pending` and is booked as a full loss | `test_execution_timeout_trips_and_assumes_loss`, `test_stalled_feed_with_open_exposure_trips` |
| Rounds out of order | Settling the wrong round | Round ids must be non-decreasing (backwards trips the breaker); a crash for a later round while our position is open is booked as a full loss and trips | `_handle`, `_settle` |
| Transition listeners | A slow or failing listener blocks a transition | Listeners must be synchronous enqueue calls; exceptions are caught and logged | `StateMachine._apply` |

**Known limitation.** The executor `await` happens while holding the FSM lock. That
is intentional (nothing else may change the state mid-order) but it means the
cool-down timer can be delayed by up to `execution_timeout_s` (2 s). The breaker
is not affected because it does not use the lock.

### 4.2 Fail-safes

**Network drops mid-round.** The outcome of the open bet can no longer be observed.
The bot books it as a full loss (`Settlement.worst_case`, `resolved=0` in the
`bets` table) and trips the breaker. Guessing that the auto cash-out "probably
fired" would understate risk. Verified by `test_disconnect_mid_round_books_worst_case`.

**Network drops while flat.** Nothing is at risk, so the bot reconnects with
exponential backoff (0.5 s, 1 s, 2 s, capped at 8 s). It trips after
`reconnect_max_attempts` (3) consecutive failures. The counter only resets after
a complete round is observed, so a feed that sends one message and drops cannot
loop forever. Verified by `test_disconnect_between_rounds_reconnects`.

**Silent feed (no messages, no error).** The watchdog checks every 250 ms. With
exposure open it trips; while flat it closes the socket to force a reconnect.

**Malformed or hostile messages.** `FeedEvent.parse` validates type, round id,
finite timestamps, `closes_at ≥ ts` and multipliers ≥ 1.0. Anything else trips
the breaker (`test_malformed_message_trips`). Booleans are rejected where ints
are expected and NaN/inf are rejected everywhere.

**SQLite write lock / disk errors.** The trading loop never awaits the database;
`log_*` calls are `put_nowait` into a bounded queue. The writer runs each batch in
one `BEGIN IMMEDIATE` transaction with WAL mode and `busy_timeout`, then retries
with exponential backoff. If retries are exhausted, the batch is appended to a
JSONL fallback file (fsynced) so nothing is lost, and after
`max_consecutive_write_failures` the bot trips: **no audit trail, no trading**.
A full queue takes the same path rather than blocking. Verified with a real
second connection holding `BEGIN EXCLUSIVE` in
`test_locked_database_falls_back_and_signals` and `test_db_lock_trips_breaker`.

**Restart as a bypass.** Found during self-review and fixed: in-memory state dies
with the process, so restarting would have cleared both the breaker and the daily
drawdown baseline. Now:
- every trip writes and fsyncs `data/CIRCUIT_BREAKER.latch`, and `run()` refuses to start while it exists (`test_restart_cannot_bypass_tripped_breaker`);
- on start, the day's opening equity and latest equity are restored from earlier sessions today, so the daily limit spans restarts (`test_restart_keeps_daily_drawdown_baseline`);
- if an earlier session today never recorded its closing equity (killed process), start is refused until an operator reconciles it.

**`python -O`.** Also found in review and fixed: the sizing invariants were
`assert` statements, which `-O` strips. They are now explicit checks that deny the
bet.

### 4.3 Circuit-breaker verification

Trip conditions, all of them irreversible in-process and latched across restarts:

| Trigger | Threshold (default) | Test |
|---|---|---|
| Daily drawdown | equity ≤ day open × (1 − 5%), or remaining budget < min stake | `test_daily_drawdown_trips_without_overshoot` |
| Extreme latency | any message > 1500 ms old | `test_extreme_latency_trips_and_blocks_execution` |
| Sustained latency | 3 consecutive messages > 400 ms | `test_sustained_soft_latency_trips` |
| Clock skew | server timestamp > 250 ms in the future | `test_clock_skew_trips` |
| Feed lost with exposure | disconnect, or silent > 5 s, while a bet is open | `test_disconnect_mid_round_books_worst_case`, `test_stalled_feed_with_open_exposure_trips` |
| Execution ack timeout | > 2 s | `test_execution_timeout_trips_and_assumes_loss` |
| Protocol violation | malformed message, round id going backwards, missing settlement | `test_malformed_message_trips` |
| Audit log unavailable | 2 consecutive failed batches or queue full | `test_db_lock_trips_breaker` |
| Reconnect budget | 3 consecutive failed reconnects | `_consume_loop` |
| Unhandled exception | any error escaping the consumer | `run()` |

Why it cannot be bypassed in-process:
1. `trip()` sets `halted` **first**, before any logging or I/O.
2. `CIRCUIT_BREAKER` has no outgoing edges, and `transition_locked` returns False whenever `halted` is set.
3. The execution gate checks `state is MONITORING and not halted` atomically before reserving; `run()` returns on `halted` and cancels the consumer.
4. The tests assert that no order has `accepted_at` later than the trip time and that no order exists for the round that caused the trip.
5. Latency messages above the soft limit are never traded, even before the breaker trips.

What it cannot do, stated plainly:
- It cannot cancel an order the venue already accepted. It can only stop new ones and book the in-flight one as a full loss.
- Latency is measured as local clock minus server timestamp, so it depends on both clocks being NTP-synced. Large skew trips the breaker, but small consistent skew shifts every latency reading by the same amount.
- Settlement trusts the feed's crash event. A real deployment would also reconcile against the venue's own bet history.
- The latch file write is a few milliseconds of blocking I/O on the event loop. That only happens once, at the moment of the trip.
- Day boundaries are UTC midnight.
