# The Governor — cross-process adaptive rate limiter

A machine-global, cross-process adaptive rate limiter for shared API gateways.
Pure Python stdlib. Works on Windows and POSIX. Python 3.9+.

## The problem it solves

When many processes on one machine (multiple terminals, sessions, agent workers)
all call models through one API gateway, the gateway rate-limits the whole machine
— and every model behind one gateway shares that limit, so "different providers"
that live on the same host stomp each other. Per-process retry logic makes it
WORSE: each process backs off alone, then they all retry in sync and re-trip the
limit together.

The fix coordinates at the **machine level**: one shared, adaptive limiter per
gateway host, visible to every process, that learns the real rate the gateway will
tolerate.

## The five properties

1. **Cross-process** — state lives on disk under `GOVERNOR_STATE_DIR`
   (default: `%LOCALAPPDATA%\governor` on Windows, `~/.local/state/governor` on
   POSIX), guarded by a file lock (`msvcrt` byte-range locks on Windows, `flock`
   on POSIX), so every process shares one limit.
2. **Adaptive (AIMD)** — an overload (429/529/5xx, or a gateway timeout)
   *multiplicatively* cuts the allowed rate (`rate *= backoff_mult`, default ×0.5)
   and sets a cooldown (honoring `Retry-After` if sent, else exponential
   `cooldown_base * 2^(n-1)`, capped at 2^6). Sustained success — `ramp_after`
   consecutive OKs — *additively* ramps the rate back up toward the ceiling.
   Classic AIMD: crash fast, recover slow, never oscillate at the ceiling.
3. **Concurrency-capped** — a self-healing in-flight set caps simultaneous requests
   across ALL processes (`max_inflight`). Dead pids are reaped immediately; a LIVE
   pid's permit is only reaped after 30 minutes, so a slow in-flight call is never
   silently double-booked.
4. **Priority-fair** — when permits are scarce, the highest-priority live waiter
   goes first (orchestrator=0, reviewer/writer=1, worker/logger=2, FIFO within a
   level), so at least one caller always progresses and records state the others
   resume on.
5. **Observable + fail-loud** — every call is logged to a per-gateway
   `*.log.jsonl`; `inspect()`/`inspect_all()`/`recent_calls()` read it back live.
   If the state directory becomes unwritable, the governor warns ONCE
   (`warnings.warn`) and degrades to in-process-only limiting — it never silently
   resets to full budget. `governor.is_degraded()` returns the reason or None.

## Quick start

Copy the `governor/` package into your project, then route every remote-model HTTP
call through `governed()`:

```python
from governor import governor

entry = {"name": "my-gateway", "base_url": "https://gw.example.com/api/v1"}
key    = governor.key_for(entry)          # "host:gw.example.com"
params = governor.params_for(entry)       # per-gateway tuning (see below)

def do_call():
    status, body, headers = my_http_client.post(...)
    return status, body, headers

# one-shot (recommended): acquire + call + report + transparent retry
status, body = governor.governed(key, params, do_call, agent="w1", role="worker",
                                 on_event=print, max_retries=5, max_wait=120.0)
```

`do_call()` must return `(status:int, body:any, headers:dict)`. Raise a
`TimeoutError`/`socket.timeout` when the call timed out — the governor treats that
as an overload signal and cuts the rate (a saturated gateway stops answering before
it starts refusing). Any other transport exception releases the permit *neutrally*:
DNS/refused/TLS failures say nothing about the gateway's load, and counting them as
successes would ramp the rate up exactly while the gateway is failing.

Manual mode (when the call can't be wrapped in one closure):

```python
h = governor.acquire(key, params, agent="w1", role="worker", max_wait=120.0)
try:
    status, body, headers = do_call()
finally:
    governor.report(h, ok=(status == 200), status=status,
                    retry_after=governor._retry_after(headers), params=params)
```

`report()` semantics: `ok=True` ramps after `ramp_after` in a row; `ok=False`
cuts + cooldown; `ok=None` is a NEUTRAL release (frees the permit, touches neither
counter — use for transport failures that aren't load evidence).

## Key mechanics worth knowing before integrating

- **Bucket key = gateway HOST, not provider name.** `key_for()` derives the key
  from `urlparse(base_url).netloc` — every model behind one gateway shares one
  bucket. Getting this wrong reintroduces the N-providers-stomping-one-gateway
  problem.
- **Token bucket for pacing + hard inflight cap.** Tokens refill at the current
  adaptive `rate` up to `burst`. The inflight cap is a HARD cap — never bypassed,
  not even by fail-open.
- **Fail-open is a single half-open probe, not a bail-out.** If a waiter has
  waited out `max_wait` (default 120s) AND is the front-of-queue waiter, it is let
  through despite cooldown/token state — exactly one probe so the orchestrator can
  get through and record state. Everyone else keeps waiting. This prevents both
  deadlock and thundering-herd.
- **Overload statuses:** 429, 529, 500, 502, 503, 504 (`is_overload_status()`).
  529 matters — some gateways report "overloaded" that way.
- **Waiters heartbeat.** A waiting process refreshes its ticket every loop (~≤2s)
  or it's reaped as dead after 30s — no leaked queue entries from killed processes.
- **All state is a file.** Live control state must never live only in memory or in
  a config field; processes reload it from disk under the lock.
- **Integrate at the HTTP layer, not the business layer** — one choke point per
  client, every remote call goes through it.

## Configuration

`governor.params_for(entry, tuning=None)` builds the per-gateway params dict.
Provider-level keys win, then the shared `tuning` dict, then defaults:

| Key | Meaning | Default |
|-----|---------|---------|
| `rate_rps` | sustained requests/sec ceiling per gateway | 8.0 |
| `burst` | token-bucket burst size | = `rate_rps` |
| `max_inflight` | max concurrent in-flight across ALL sessions | 6 |
| `rate_floor` | AIMD never cuts below this | 0.25 |
| `ramp_after` | consecutive OKs before additive ramp | 5 |
| `ramp_inc` | rps added per ramp | 0.5 |
| `backoff_mult` | multiplicative cut on overload | 0.5 |
| `cooldown_base` | first cooldown, seconds (then ×2 exponential) | 2.0 |

## The failover layer (`routing.py`)

The governor paces ONE gateway; `routing.py` decides WHICH gateway. Config is a
plain dict:

```python
config = {
    "providers": {                       # name -> entry
        "remote": {"base_url": "https://gw.example.com/api/v1", "rate_rps": 8.0},
        "local":  {"base_url": "http://127.0.0.1:11434"},
    },
    "roles":     {"worker": "remote"},
    "fallbacks": {"worker": ["local"]},  # must be a DIFFERENT host
}

name, entry, pivoted, health = routing.resolve(config, "worker")
```

Each candidate is classified via `governor.health()`:

- `open` — circuit tripped (in cooldown > 8s, or ≥3 consecutive errors): skip
- `degraded` — alive but paced below half its ceiling: usable
- `ok` — at or near ceiling: preferred

First non-open gateway wins; if ALL are open, it picks the **least-cooled** one
and lets the governor pace it. Fallbacks must point at different hosts (e.g. a
local model server), not another model on the same overloaded gateway.

## Introspection API

```python
governor.inspect(key)         # rate, ceiling, inflight, waiting, cooldown_remaining, last_event
governor.inspect_all()        # every gateway seen on this machine
governor.recent_calls(key)    # last N calls from the jsonl log
governor.health(key)          # "open" | "degraded" | "ok" | "unknown"
governor.is_degraded()        # None, or why state isn't persisting
```

## Windows specifics

- **Never use `os.kill(pid, 0)` on Windows** — any non-CTRL signal terminates the
  target process there. `pid_alive()` uses `OpenProcess`/`GetExitCodeProcess` on
  Windows and `os.kill(pid, 0)` on POSIX; the reaper calls `pid_alive()`.
- The file lock uses `msvcrt.locking` (byte-range) on Windows; the lock file gets
  one byte written so the range lock has something to bite on.
- `os.replace()` can transiently fail while an unlocked reader (`inspect()`) holds
  the state file open — `_save()` retries through ~1s of contention.
- Log appends happen under the lock: Windows append-mode writes are not atomic
  the way POSIX `O_APPEND` writes are.

## Running the tests

From this folder (the one containing `governor/` and `tests/`):

```
python -m pytest tests/ -v
```

The 12 tests prove: shared bucket by host, 429 cuts rate + transparent retry,
sustained success ramps back, orchestrator-before-worker priority, hard
concurrency cap, fail-open only for the front waiter, neutral release on
transport errors, timeout cuts the rate, pid liveness, pivot on circuit-open,
least-cooled selection when everything is open, and unconfigured-role errors.
