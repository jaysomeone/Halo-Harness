# Review of the Governor kit (2026-10-03), with the changes to make while porting

Reviewed: `governor-kit/governor/governor.py` (603 lines), `routing.py`,
`__init__.py`, `tests/test_governor.py` (9 tests), `tests/test_routing.py`
(3), `conftest.py`. Verdict: solid, stdlib-only, already cross-platform
(msvcrt byte-range locks, OpenProcess-based pid liveness, atomic replace
with retry, fail-loud degraded mode, neutral release for transport errors,
timeout as overload). The algorithm is right (token bucket + hard in-flight
cap + AIMD + priority FIFO + single half-open probe). The items below are
what to change when it becomes `halo_harness/providers/governor.py`.

## Correctness and robustness

1. Clock jumps. Cooldowns and the token bucket use wall time
   (`time.time()`), which is necessary across processes, but a jump (NTP,
   sleep/resume, a VM pause) can leave `cooldown_until` far in the future
   or refill a huge token burst. On load, clamp the remaining cooldown to
   the maximum the ladder can produce (`cooldown_base * 2^6`, or the
   Retry-After cap below) and clamp a refill interval to 60 s.
2. `Retry-After` only parses delta-seconds; the HTTP-date form returns
   None and the exponential ladder is used instead. Parse both
   (`email.utils.parsedate_to_datetime`), and cap the honoured value
   (`retry_after_cap`, default 120 s) so one hostile or absurd header
   cannot freeze the whole fleet.
3. No way to cancel a wait. `acquire()` can block up to `max_wait`, and
   `governed()` can loop `max_retries` times, so a single call may hold an
   interactive session for minutes with nothing on screen. Add an
   `abort` callable (Halo passes the turn's abort event, Esc) checked every
   loop; on abort deregister the waiter and raise a `GovernorAborted` the
   loop reports as "interrupted". Emit a wait event (`waiting`, with
   position in the queue, cooldown remaining and current rate) through
   `on_event` so the liveness line can show "gateway cooling, 12 s, 2 ahead
   of you".
4. Lock timeout. POSIX `flock` has none and the Windows loop retries
   forever, so one process suspended while holding the lock (a debugger, a
   stopped container) hangs every other process. Add a lock timeout
   (default 30 s); on expiry warn once, mark degraded and fall through to
   the in-process view for that call rather than hang.
5. Threads in one process. Halo runs sub-agents on threads inside one
   process. `flock` across separate descriptors in one process does
   exclude, and Windows byte-range locks are per handle, so the file lock
   holds, but the `_MEM_STATE` fallback object can be shared and mutated by
   two threads when the disk is unwritable. Serialize the critical sections
   with one process-level `threading.RLock` per key (cheap, held only
   around the locked file sections) and deep-copy on the fallback read.
6. Status classification. `500` counts as overload today; a plain server
   bug then cuts the rate and sets a cooldown for everyone. Default
   overload set 429, 529, 503, 502, 504; treat 500 as neutral unless the
   body says overloaded (`"overloaded"`, `"rate limit"`, `"capacity"`);
   make the set configurable (`overload_statuses`). Keep 529.
7. Heartbeat writes. Every waiter rewrites the state file every loop
   (0.15 to 2 s) only to refresh its heartbeat. Write the heartbeat at most
   once per second per waiter and skip the save when nothing else changed;
   at fleet scale this is the difference between a quiet disk and constant
   churn on a laptop.
8. Log growth. The per-gateway `*.log.jsonl` grows without bound; rotate
   at 1 MB (one `.1` generation), the way Halo's MCP logs do, and add
   `session_id` and `model` to each record so `/gov` can say which session
   and model made the call.

## Fit to Halo

9. Priorities come from roles, configurable: main session and orchestrator
   0; judge, reviewer, tester, planner 1; every other sub-agent 2
   (`governor.priorities` in config). The kit's static brood roles (writer,
   logger) go away.
10. State under Halo's state dir (`<state dir>/governor/`), so
    `BRIDGE_TEST_HOME` / `BRIDGE_STATE_DIR` scope it in tests and `halo
    doctor` can find it; keep `GOVERNOR_STATE_DIR` as an override for
    sharing one limiter between Halo and another tool on the same box
    (that is exactly the owner's fleet case).
11. `pid_alive` comes from the existing `bg_run.pid_alive` (zombie-aware
    on Linux, OpenProcess on Windows); one implementation in the tree.
12. The bucket key stays the gateway host. Document that two accounts on
    one host share a bucket on purpose, and that `fallback_models` entries
    on the same host as the primary cannot help (the config loader warns).
13. Integrate in `providers/http.py` only (one choke point), with the
    agent id, role and session id from the caller; the loop's own retry
    ladder stops retrying overload statuses on governed routes.
14. `/gov` (TUI and CLI) prints `inspect_all()` and `recent_calls()` for one
    host; the status bar shows `gov <rate> rps / cooldown <s>` in the
    overflow cascade while a bucket is below its ceiling; `halo doctor`
    shows each gateway's health and whether the limiter is persisting.
15. Tests: port the twelve to the suite's own runner (`tests/helpers/
    runner.py`, no pytest), keep the real-sleep ones bounded, and add the
    owner's gate (a six-process fake fleet against the mock upstream's 429
    storm with Retry-After: rate cut, cooldown set, the priority-0 waiter
    completes, failover lands on the fallback host) plus tests for the
    changes above (clock clamp, HTTP-date, abort, lock timeout, thread
    serialization, 500 neutral, heartbeat throttling, rotation).

## Routing layer

`routing.resolve()` is fine as the health classifier; in Halo it feeds the
2.0.1 fallback-model mechanism rather than its own config shape (candidates
= the role's model plus `roles.<name>.fallbacks`, each with its route's
host), returns the health that caused a switch for the notice, and the
lanes rule (verifier roles never on a weaker tier than coder) is validated
separately in the roles table.
