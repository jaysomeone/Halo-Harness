# The Governor — cross-process adaptive rate limiter (the Databricks 429 fix)

**A self-contained report on brood's answer to gateway overload, extracted so it can be
dropped into other projects.** Written 2026-10-03.

- Canonical source: `<the owner vault>/appDev/brood/brood/governor.py` (409 lines, stdlib-only)
- Failover layer: `<the owner vault>/appDev/brood/brood/routing.py` (47 lines)
- Tests: `<the owner vault>/appDev/brood/tests/test_governor.py` (8 tests, green in the 357-test suite)
- Second-generation port (already in production use): `<the owner vault>/appDev/work-mcp/work_mcp.py` (~line 143 onward) — adds timeout classification and a loud warning when the governor can't persist state
- Copies of all three files are vendored in THIS folder, unmodified, so this report is
  self-contained.

## The problem it solves

When a fleet of agents (multiple terminals, multiple sessions, one machine) all call models
through the **Databricks AI Gateway**, the gateway 429s the whole machine — and every model on
one gateway shares that limit, so `databricks-k3` and `databricks-qwen` stomp each other even
though they're "different providers" in config. Per-process retry logic makes it WORSE: each
process backs off alone, then they all retry in sync and re-trip the limit.

The fix coordinates at the **machine level**: one shared, adaptive limiter per gateway host,
visible to every process, that learns the real rate the gateway will tolerate.

## The five properties (from the module docstring, all implemented)

1. **Cross-process** — state lives on disk under `$BROOD_STATE_DIR` (default
   `~/.local/state/brood/governor/`), guarded by `flock`, so three terminals share one limit.
2. **Adaptive (AIMD)** — a 429/overload *multiplicatively* cuts the allowed rate
   (`rate *= backoff_mult`, default ×0.5) and sets a cooldown (honoring `Retry-After` if the
   gateway sent one, else exponential `cooldown_base * 2^(n-1)`, capped at 2^6). Sustained
   success — `ramp_after` consecutive OKs (default 5) — *additively* ramps the rate back up
   (`+ramp_inc`, default +0.5 rps) toward the ceiling. Classic AIMD: crash fast, recover slow,
   never oscillate at the ceiling.
3. **Concurrency-capped** — a self-healing in-flight set caps simultaneous requests across ALL
   processes (`max_inflight`, default 6). Dead pids and stale entries (>180s) are reaped, so a
   killed agent doesn't permanently eat a slot.
4. **Priority-fair** — when permits are scarce, the highest-priority live waiter goes first
   (queen/orchestrator=0, judge/writer=1, worker/scribe=2, FIFO within a level), so at least
   one agent always progresses and records state the others resume on.
5. **Observable** — every call is logged (agent, role, call_id, status, waited, rate, event)
   to a per-gateway `*.log.jsonl`; `inspect`/`inspect_all`/`recent_calls` read it back live.

## Key mechanics worth knowing before porting

- **Bucket key = gateway HOST, not provider name.** `key_for()` derives the key from
  `urlparse(base_url).netloc` — every model behind one gateway shares one bucket (falling back
  to the provider name for keyless/local kinds). Getting this wrong reintroduces the
  N-providers-stomping-one-gateway problem.
- **Token bucket for pacing + hard inflight cap.** Tokens refill at the current adaptive
  `rate` up to `burst`. The inflight cap is a HARD cap — it is never bypassed, not even by
  fail-open.
- **Fail-open is a single half-open probe, not a bail-out.** If a waiter has waited out
  `max_wait` (default 120s) AND is the front-of-queue (highest priority) waiter, it is let
  through despite cooldown/token state — exactly one probe so the orchestrator can get through
  and record state. Everyone else keeps waiting. This prevents both deadlock and thundering-herd.
- **Transport errors are reported `ok=True`.** If `do_call()` raises (network error, timeout
  in the caller), the permit is released but the rate is NOT cut — a dead network is not an
  overloaded gateway. Only HTTP overload statuses adapt the rate.
- **Overload statuses:** `429, 529, 500, 502, 503, 504` (`is_overload_status()`). 529 matters —
  Databricks/Anthropic-style "overloaded" lives there too.
- **`governed()` retries transparently.** Wrap one gateway call as a closure returning
  `(status, body, headers)`; `governed()` acquires a permit, executes, reports, and loops
  through 429/overload (up to `max_retries`, default 5), waiting out the cooldown it just set.
  After max retries it returns the last 429 for the caller to raise on. The caller's code never
  sees the backoff.
- **Waiters heartbeat.** A waiting agent refreshes its ticket every loop (~≤2s) or it's reaped
  as dead after 30s — no leaked queue entries from killed processes.
- **All state is a file.** Agents are subprocesses that reload config from disk, so live
  control state must never live in a config field or in-memory only.

## The failover layer (`routing.py`)

The governor paces ONE gateway; `routing.py` decides WHICH gateway. For each role it walks the
configured provider + `fallbacks[role]` (which must point at DIFFERENT hosts, e.g. local
Ollama, not another model on the same overloaded gateway), classifies each via
`governor.health()`:

- `open` — circuit tripped (in real cooldown > 8s, or ≥3 consecutive errors): skip
- `degraded` — alive but paced below half its ceiling: usable
- `ok` — at or near ceiling: preferred

First non-open gateway wins; if ALL are open, it picks the **least-cooled** one and lets the
governor pace it. So a Databricks meltdown fails over to the local box instead of stalling
the fleet.

## Configuration surface

Per-provider limits (from brood's `config.py`; defaults in parentheses):

| Key | Meaning | Default |
|-----|---------|---------|
| `rate_rps` | sustained requests/sec ceiling per gateway (8.0 in brood config; 2.0 as the in-code default) | 8.0 |
| `burst` | token-bucket burst size | 8 |
| `max_inflight` | max concurrent in-flight across ALL sessions | 6 |
| `rate_floor` | AIMD never cuts below this | 0.25 |
| `ramp_after` | consecutive OKs before additive ramp | 5 |
| `ramp_inc` | rps added per ramp | 0.5 |
| `backoff_mult` | multiplicative cut on overload | 0.5 |
| `cooldown_base` | first cooldown, seconds (then exponential ×2) | 2.0 |

In brood these are passed as `cfg.governor_params(entry)` → the `params` dict every governor
function takes. Nothing is global; each gateway bucket has its own.

## API surface (`governor.py`)

```python
key    = governor.key_for(provider_entry)          # "host:x.cloud.databricks.com"
params = {"rate_rps": 8.0, "burst": 8, "max_inflight": 6, ...}

# Option A — one-shot (recommended): wraps acquire + call + report + retry
status, body = governor.governed(key, params, do_call, agent="w1", role="worker",
                                  on_event=print, max_retries=5, max_wait=120.0)
# do_call() -> (status:int, body:any, headers:dict)

# Option B — manual (when you can't wrap the call in one closure)
h = governor.acquire(key, params, agent="w1", role="worker", max_wait=120.0)
try:
    status, body, headers = do_call()
finally:
    governor.report(h, ok=status==200, status=status,
                    retry_after=governor._retry_after(headers), params=params)

# Introspection
governor.inspect(key)         # rate, ceiling, inflight, waiting, cooldown_remaining, last_event
governor.inspect_all()        # every gateway seen on this machine
governor.recent_calls(key)    # last N calls from the jsonl log
governor.health(key)          # "open" | "degraded" | "ok" | "unknown"
```

## Porting guide

The module is deliberately stdlib-only (`json, os, time, uuid, hashlib, fcntl, urllib.parse`)
and py3.9-safe. To lift it into another project:

1. Copy `governor.py`. It has exactly ONE external dependency:
   `from . import runs` → `runs.pid_alive(pid)`. Inline this in its place:

   ```python
   def pid_alive(pid):
       if not pid:
           return False
       try:
           os.kill(int(pid), 0)
           return True
       except ProcessLookupError:
           return False
       except PermissionError:
           return True   # exists, owned by someone else
       except (OSError, ValueError):
           return False
   ```

2. Rename the state-dir env var to match your project (`BROOD_STATE_DIR` appears once, in
   `state_dir()`), so two projects don't share buckets unless you want them to.
3. Integrate at the HTTP layer, not the business layer: every request to a remote model goes
   through `governed()` with a `do_call` closure. In brood this is `providers/anthropic_api.py`,
   `providers/bedrock.py`, `providers/openai_compat.py` — three call sites total.
4. If you have multiple gateways, copy `routing.py` too and wire `fallbacks` per role.
5. Port the tests — `test_governor.py` needs only `BROOD_STATE_DIR` pointed at a tmp dir.
   The 8 tests prove: shared bucket by host, 429 cuts rate + transparent retry, sustained
   success ramps back, queen-before-worker priority, hard concurrency cap, fail-open only for
   the front waiter, pivot on circuit-open, and least-cooled selection when everything is open.

**Read the work-mcp port before finalizing** (`<the owner vault>/appDev/work-mcp/work_mcp.py`,
search "Ported from brood/governor.py"): it survived contact with real usage and added two
things worth keeping — treating caller-side timeouts as rate-cutting events, and surfacing a
loud warning whenever the governor silently falls back to non-persistent state (a limiter that
isn't persisting isn't limiting across sessions, and nobody notices an absence).

## Files in this folder

| File | What it is |
|------|-----------|
| `governor.py` | Verbatim copy of the canonical implementation (409 lines) |
| `routing.py` | Verbatim copy of the gateway failover layer (47 lines) |
| `test_governor.py` | Verbatim copy of the 8-test suite (130 lines) |
| `README.md` | This report |
