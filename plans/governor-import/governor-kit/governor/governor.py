"""The Governor — a MACHINE-GLOBAL, cross-process adaptive rate limiter that keeps a
fleet of callers from stomping a shared API gateway (the "every process on the box
trips the same 429" problem).

Every caller, in every terminal / session / process, coordinates through ONE shared
token bucket per gateway HOST (two models served by one gateway share one limit,
because the gateway enforces it per machine, not per model). It is:

  * cross-process   — state lives on disk under $GOVERNOR_STATE_DIR, file-lock-guarded,
                      so three terminals share one limit. POSIX uses flock; Windows
                      uses msvcrt byte-range locks.
  * adaptive (AIMD) — a 429/overload/timeout multiplicatively cuts the allowed rate and
                      sets a cooldown (honoring Retry-After); sustained success
                      additively ramps it back toward the ceiling.
  * concurrency-capped — a self-healing in-flight set (dead pids / abandoned calls
                      reaped) caps simultaneous requests across processes.
  * priority-fair   — when permits are scarce, the highest-priority live waiter goes
                      first (orchestrator before workers), so at least one caller
                      always progresses and records state for the others to resume on.
  * observable      — every call is logged (agent, role, call_id, status) so you can
                      see each thread's calls to one endpoint.
  * fail-loud       — if the state directory becomes unwritable it warns ONCE and
                      degrades to in-process-only limiting instead of silently
                      resetting to full budget on every call.

Windows notes:
  * `os.kill(pid, 0)` on Windows TERMINATES the target process — it is never a safe
    liveness probe there. pid_alive() uses OpenProcess/GetExitCodeProcess on Windows
    and os.kill(pid, 0) on POSIX.
  * os.replace() can transiently fail with PermissionError while an unlocked reader
    (inspect()) has the state file open; _save() retries through that contention.
  * State lives under %LOCALAPPDATA%\\governor by default (set GOVERNOR_STATE_DIR
    to override); on POSIX it is ~/.local/state/governor.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import uuid
import warnings
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import fcntl
    _HAVE_FLOCK = True
except ImportError:  # pragma: no cover (Windows)
    _HAVE_FLOCK = False

if os.name == "nt":  # pragma: no cover (Windows)
    import msvcrt
    _HAVE_MSVCRT = True
else:
    _HAVE_MSVCRT = False

# lower number = higher priority; the orchestrator is served first under scarcity
PRIORITY = {"orchestrator": 0, "reviewer": 1, "writer": 1, "worker": 2, "logger": 2}
DEFAULT_PRIORITY = 3

_STALE_CALL_SECS = 180.0     # an in-flight entry from a DEAD pid older than this is reaped
_ABANDONED_CALL_SECS = 1800.0 # a LIVE pid's permit is only reaped after this long — reaping
                              # it earlier silently exceeded max_inflight for any call slower
                              # than the threshold, and a slow call is exactly when the cap matters
_WAITER_STALE = 30.0         # a waiter that stops heartbeating is dropped
_MAX_SLEEP = 2.0             # never sleep longer than this per loop (stay responsive)

# Set when the governor cannot persist state. A limiter that isn't persisting isn't
# limiting across sessions — surface it, never swallow it.
DEGRADED: Dict[str, Any] = {"degraded": False, "reason": None}
_MEM_STATE: Dict[str, Dict[str, Any]] = {}   # in-process fallback view


class GovernorError(Exception):
    pass


def pid_alive(pid: Optional[int]) -> bool:
    """Liveness probe for the reaper. NEVER use os.kill(pid, 0) on Windows — there it
    terminates the target instead of probing it (any non-CTRL signal maps to
    TerminateProcess)."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover (Windows)
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False   # gone (or access-denied — treated as gone: conservative reap)
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True        # exists, owned by another user
    except OSError:
        return False


def state_dir() -> str:
    base = os.environ.get("GOVERNOR_STATE_DIR")
    if not base:
        if os.name == "nt":  # pragma: no cover (Windows)
            base = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "governor")
        else:
            base = os.path.expanduser("~/.local/state/governor")
    os.makedirs(base, exist_ok=True)
    return base


def key_for(entry: Dict[str, Any]) -> str:
    """Bucket key = the gateway HOST, so every model on one gateway shares one limit.
    Falls back to the provider name for keyless/local kinds."""
    url = entry.get("base_url") or ""
    host = urlparse(url).netloc.lower() if url else ""
    if not host:
        return "provider:" + str(entry.get("name") or entry.get("kind") or "unknown")
    return "host:" + host


def params_for(entry: Dict[str, Any], tuning: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Per-gateway tuning. Provider-level keys (rate_rps, burst, max_inflight,
    rate_floor) win; then the shared `tuning` dict (ramp/backoff knobs); then the
    documented defaults."""
    tuning = tuning or {}
    rate = float(entry.get("rate_rps", tuning.get("rate_rps", 8.0)))
    return {
        "rate_rps": rate,
        "burst": float(entry.get("burst", tuning.get("burst", rate))),
        "max_inflight": int(entry.get("max_inflight", tuning.get("max_inflight", 6))),
        "rate_floor": float(entry.get("rate_floor", tuning.get("rate_floor", 0.25))),
        "ramp_after": int(tuning.get("ramp_after", entry.get("ramp_after", 5))),
        "ramp_inc": float(tuning.get("ramp_inc", entry.get("ramp_inc", 0.5))),
        "backoff_mult": float(tuning.get("backoff_mult", entry.get("backoff_mult", 0.5))),
        "cooldown_base": float(tuning.get("cooldown_base", entry.get("cooldown_base", 2.0))),
    }


def _safe(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + "_" + "".join(
        c if c.isalnum() else "-" for c in key)[:40]


def _paths(key: str) -> Tuple[str, str, str]:
    d = state_dir()
    base = os.path.join(d, _safe(key))
    return base + ".json", base + ".lock", base + ".log.jsonl"


def priority_for(role: Optional[str]) -> int:
    return PRIORITY.get((role or "").lower(), DEFAULT_PRIORITY)


class _Lock:
    def __init__(self, key: str):
        self._p = _paths(key)[1]
        self._fd = None

    def __enter__(self):
        self._fd = os.open(self._p, os.O_CREAT | os.O_RDWR, 0o644)
        if _HAVE_FLOCK:
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        elif _HAVE_MSVCRT:  # pragma: no cover (Windows)
            if os.fstat(self._fd).st_size == 0:
                os.write(self._fd, b"\0")   # a byte-range lock needs a byte to bite on
            os.lseek(self._fd, 0, os.SEEK_SET)
            while True:
                try:
                    msvcrt.locking(self._fd, msvcrt.LK_LOCK, 1)   # waits ~10s, then raises
                    break
                except OSError:
                    time.sleep(0.1)          # contended longer than 10s: keep waiting
        return self

    def __exit__(self, *a):
        if self._fd is None:
            return
        try:
            if _HAVE_FLOCK:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            elif _HAVE_MSVCRT:  # pragma: no cover (Windows)
                os.lseek(self._fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass                     # closing the handle releases the lock anyway
        finally:
            os.close(self._fd)
            self._fd = None


def _default_state(key: str, params: Dict[str, Any], now: float) -> Dict[str, Any]:
    ceiling = float(params.get("rate_rps", 2.0))
    return {
        "key": key,
        "rate": ceiling, "rate_ceiling": ceiling,
        "rate_floor": float(params.get("rate_floor", 0.25)),
        "burst": float(params.get("burst", max(1, int(ceiling)))),
        "tokens": float(params.get("burst", max(1, int(ceiling)))),
        "last_refill": now,
        "max_inflight": int(params.get("max_inflight", 2)),
        "inflight": {},          # call_id -> {pid, ts, agent, role}
        "waiters": {},           # ticket -> {priority, ts, pid, agent, role}
        "cooldown_until": 0.0,
        "consecutive_ok": 0, "consecutive_err": 0,
        "last_status": None, "last_event": None, "updated": now,
    }


def _load(key: str, params: Dict[str, Any], now: float) -> Dict[str, Any]:
    p = _paths(key)[0]
    try:
        with open(p, "r", encoding="utf-8") as fh:
            st = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        # Fall back to this process's own view before giving up, so a governor that
        # cannot write to disk still enforces its limits here instead of resetting to
        # full budget every call.
        st = _MEM_STATE.get(key)
        if st is None:
            return _default_state(key, params, now)
    # keep the operator-tunable fields in sync if config changed (e.g. quota raised)
    ceiling = float(params.get("rate_rps", st.get("rate_ceiling", 2.0)))
    st["rate_ceiling"] = ceiling
    st["max_inflight"] = int(params.get("max_inflight", st.get("max_inflight", 2)))
    st["rate_floor"] = float(params.get("rate_floor", st.get("rate_floor", 0.25)))
    st["burst"] = float(params.get("burst", st.get("burst", ceiling)))
    st.setdefault("inflight", {})
    st.setdefault("waiters", {})
    st.setdefault("rate", ceiling)
    st.setdefault("tokens", 0.0)
    st.setdefault("last_refill", now)
    return st


def _save(key: str, st: Dict[str, Any]) -> None:
    p = _paths(key)[0]
    st["updated"] = time.time()
    tmp = p + ".tmp.%d" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        _replace(tmp, p)
        _MEM_STATE[key] = st          # mirror, so a later read still sees this view
    except OSError as e:
        if not DEGRADED["degraded"]:
            DEGRADED["degraded"] = True
            DEGRADED["reason"] = "cannot write %s: %s" % (p, e)
            warnings.warn(
                "governor cannot persist state (%s). Rate limiting falls back to "
                "IN-PROCESS only — it is NOT shared across sessions. Fix "
                "GOVERNOR_STATE_DIR or its permissions." % e)
        _MEM_STATE[key] = st          # keep enforcing within this process
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _replace(tmp: str, p: str) -> None:
    """os.replace with retry: on Windows the target can be transiently held open by an
    unlocked reader (inspect()), which makes replace raise PermissionError."""
    for _ in range(50):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.02)
    os.replace(tmp, p)   # still contended after ~1s: surface the error


def is_degraded() -> Optional[str]:
    """None when state is persisting normally; otherwise the reason it isn't. A limiter
    that isn't persisting isn't limiting across sessions — check this (or watch for the
    one-shot warning) if rate limits seem mysteriously generous."""
    return DEGRADED["reason"] if DEGRADED["degraded"] else None


def _reap(st: Dict[str, Any], now: float) -> None:
    """Self-healing: a killed caller cannot leak a permit — its pid is gone, so the slot
    is freed by whoever asks next.

    A LIVE process is only reaped at the much longer _ABANDONED_CALL_SECS. Reaping a
    live caller at the short threshold handed its permit to someone else while its
    request was still on the wire, so max_inflight was quietly exceeded by any call
    slower than that — and a slow call is exactly when the cap matters most.
    """
    for cid in list(st["inflight"].keys()):
        e = st["inflight"][cid]
        age = now - e.get("ts", now)
        if not pid_alive(e.get("pid")):
            del st["inflight"][cid]
        elif age > _ABANDONED_CALL_SECS:
            warnings.warn("governor: reaping an in-flight entry %.0fs old whose process "
                          "is still alive — assuming it was abandoned" % age)
            del st["inflight"][cid]
    for tk in list(st["waiters"].keys()):
        w = st["waiters"][tk]
        if not pid_alive(w.get("pid")) or (now - w.get("hb", w.get("ts", now))) > _WAITER_STALE:
            del st["waiters"][tk]


def _refill(st: Dict[str, Any], now: float) -> None:
    dt = max(0.0, now - st.get("last_refill", now))
    st["tokens"] = min(st["burst"], st.get("tokens", 0.0) + st["rate"] * dt)
    st["last_refill"] = now


def _is_front(st: Dict[str, Any], ticket: str) -> bool:
    """Am I the highest-priority (then earliest) live waiter? Determines who takes a
    scarce permit — orchestrator before worker, FIFO within a priority."""
    mine = st["waiters"].get(ticket)
    if mine is None:
        return True
    best = min(st["waiters"].values(), key=lambda w: (w.get("priority", 9), w.get("ts", 0.0)))
    return (mine.get("priority", 9), mine.get("ts", 0.0)) == (best.get("priority", 9), best.get("ts", 0.0))


class Handle:
    def __init__(self, key: str, call_id: str, agent: str, role: str, start: float, waited: float):
        self.key = key
        self.call_id = call_id
        self.agent = agent
        self.role = role
        self.start = start
        self.waited = waited


def acquire(key: str, params: Dict[str, Any], *, agent: str = "system", role: str = "worker",
            max_wait: float = 120.0) -> Handle:
    """Block (pacing, priority-fair) until a permit is granted; return a Handle to
    report() against. Fail-open after max_wait, but ONLY for the front-of-queue
    (highest-priority) waiter, so the orchestrator gets through and the gateway is
    never stomped by everyone at once."""
    ticket = uuid.uuid4().hex[:12]
    call_id = uuid.uuid4().hex[:12]
    prio = priority_for(role)
    t0 = time.time()
    # register as a waiter
    with _Lock(key):
        st = _load(key, params, t0)
        _reap(st, t0)
        st["waiters"][ticket] = {"priority": prio, "ts": t0, "hb": t0, "pid": os.getpid(),
                                 "agent": agent, "role": role}
        _save(key, st)
    try:
        while True:
            now = time.time()
            waited = now - t0
            with _Lock(key):
                st = _load(key, params, now)
                _reap(st, now)
                # heartbeat my waiter ticket so I'm not reaped
                if ticket in st["waiters"]:
                    st["waiters"][ticket]["hb"] = now
                else:
                    st["waiters"][ticket] = {"priority": prio, "ts": t0, "hb": now,
                                             "pid": os.getpid(), "agent": agent, "role": role}
                _refill(st, now)
                cooling = now < st.get("cooldown_until", 0.0)
                inflight_full = len(st["inflight"]) >= st["max_inflight"]
                have_token = st["tokens"] >= 1.0
                front = _is_front(st, ticket)
                failopen = waited >= max_wait and front
                # concurrency is a HARD cap always; fail-open (front waiter, waited out
                # max_wait) bypasses cooldown+tokens as a single half-open probe so the
                # orchestrator/first caller gets through and can record state.
                grant = front and not inflight_full and ((not cooling and have_token) or failopen)
                if grant:
                    if have_token:
                        st["tokens"] -= 1.0
                    st["inflight"][call_id] = {"pid": os.getpid(), "ts": now, "agent": agent, "role": role}
                    st["waiters"].pop(ticket, None)
                    _save(key, st)
                    return Handle(key, call_id, agent, role, now, waited)
                # not granted: compute how long to wait before rechecking
                if cooling:
                    wait = st["cooldown_until"] - now
                elif not front:
                    wait = 0.15                      # let the higher-priority waiter take it
                elif inflight_full:
                    wait = 0.2
                else:
                    wait = (1.0 - st["tokens"]) / max(st["rate"], 1e-6)
                _save(key, st)
            time.sleep(max(0.02, min(_MAX_SLEEP, wait)))
    except BaseException:
        with _Lock(key):
            st = _load(key, params, time.time())
            st["waiters"].pop(ticket, None)
            _save(key, st)
        raise


def report(handle: Handle, ok: Optional[bool], status: Optional[int] = None,
           retry_after: Optional[float] = None,
           params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Record the outcome and adapt. Returns telemetry describing any change (so the
    caller can surface a throttle/recover event).

    ok = True  -> success; ramps the rate back toward the ceiling after ramp_after in a row.
    ok = False -> overload; multiplicative cut + cooldown, bucket drained.
    ok = None  -> NEUTRAL release: free the permit, touch NEITHER counter. Use for a
                  transport failure that is not evidence about the server's load (DNS,
                  connection refused, TLS) — reporting those as successes ramps the rate
                  up while the gateway is failing.
    """
    params = params or {}
    now = time.time()
    event: Optional[str] = None
    with _Lock(handle.key):
        st = _load(handle.key, params, now)
        st["inflight"].pop(handle.call_id, None)
        st["last_status"] = status
        prev_rate = st["rate"]
        if ok is None:
            pass                                   # neutral: release only
        elif ok:
            st["consecutive_ok"] += 1
            st["consecutive_err"] = 0
            step = int(params.get("ramp_after", 5))
            if st["consecutive_ok"] >= step:
                st["consecutive_ok"] = 0
                inc = float(params.get("ramp_inc", 0.5))
                if st["rate"] < st["rate_ceiling"]:
                    st["rate"] = min(st["rate_ceiling"], st["rate"] + inc)
                    event = "recover"
        else:
            st["consecutive_err"] += 1
            st["consecutive_ok"] = 0
            st["rate"] = max(st["rate_floor"], st["rate"] * float(params.get("backoff_mult", 0.5)))
            base = float(params.get("cooldown_base", 2.0))
            backoff = base * (2 ** min(st["consecutive_err"] - 1, 6))
            cd = max(retry_after or 0.0, backoff)
            st["cooldown_until"] = max(st.get("cooldown_until", 0.0), now + cd)
            st["tokens"] = 0.0
            event = "throttle"
        telem = {
            "key": handle.key, "event": event, "ok": ok, "status": status,
            "rate": round(st["rate"], 3), "rate_ceiling": st["rate_ceiling"],
            "prev_rate": round(prev_rate, 3),
            "cooldown_remaining": round(max(0.0, st.get("cooldown_until", 0.0) - now), 1),
            "inflight": len(st["inflight"]), "waited": round(handle.waited, 2),
            "agent": handle.agent, "role": handle.role,
        }
        st["last_event"] = telem
        _save(handle.key, st)
        _log(handle, ok, status, telem)   # inside the lock: append-mode writes are not
                                          # atomic on Windows, unlike POSIX O_APPEND
    return telem


def _log(handle: Handle, ok: Optional[bool], status: Optional[int], telem: Dict[str, Any]) -> None:
    p = _paths(handle.key)[2]
    rec = {"ts": time.time(), "call_id": handle.call_id, "agent": handle.agent,
           "role": handle.role, "ok": ok, "status": status, "waited": round(handle.waited, 2),
           "rate": telem.get("rate"), "event": telem.get("event")}
    try:
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def is_overload_status(status: int) -> bool:
    return status in (429, 529) or status in (500, 502, 503, 504)


def governed(key: str, params: Dict[str, Any], do_call: Callable[[], Tuple[int, Any, Dict[str, str]]],
             *, agent: str = "system", role: str = "worker",
             on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
             max_retries: int = 5, max_wait: float = 120.0,
             timeout_is_overload: bool = True) -> Tuple[int, Any]:
    """Run one gateway call under the governor: acquire a permit, execute do_call()
    (which returns (status, body, headers)), report the outcome, and transparently
    pace+retry through 429/overload. Returns (status, body).

    Transport errors propagate (after releasing the permit). A TimeoutError is treated
    as an overload signal — a saturated gateway stops answering before it starts
    REFUSING, so cutting the rate on timeouts is correct — unless
    timeout_is_overload=False. Other transport errors release neutrally: a dead
    network is not an overloaded gateway, and counting them as successes would ramp
    the rate up exactly when the gateway is failing.
    """
    attempt = 0
    while True:
        h = acquire(key, params, agent=agent, role=role, max_wait=max_wait)
        try:
            status, body, headers = do_call()
        except (TimeoutError, socket.timeout):      # same class in py3.10+; distinct on 3.9
            telem = report(h, ok=(not timeout_is_overload), status=None, params=params)
            if telem.get("event") and on_event:
                on_event(telem)
            raise
        except Exception:
            report(h, ok=None, params=params)   # neutral release: not evidence about load
            raise
        overloaded = is_overload_status(status)
        retry_after = _retry_after(headers) if overloaded else None
        telem = report(h, ok=not overloaded, status=status, retry_after=retry_after, params=params)
        if telem.get("event") and on_event:
            on_event(telem)
        if not overloaded:
            return status, body
        attempt += 1
        if attempt > max_retries:
            return status, body   # give the caller the last 429 to raise on
        # loop: acquire() will now wait out the cooldown just set


def _retry_after(headers: Dict[str, str]) -> Optional[float]:
    for k, v in (headers or {}).items():
        if k.lower() == "retry-after":
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def inspect(key: str) -> Optional[Dict[str, Any]]:
    p = _paths(key)[0]
    try:
        with open(p, "r", encoding="utf-8") as fh:
            st = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        st = _MEM_STATE.get(key)
        if st is None:
            return None
    now = time.time()
    return {
        "key": st.get("key", key), "rate": round(st.get("rate", 0), 3),
        "rate_ceiling": st.get("rate_ceiling"), "max_inflight": st.get("max_inflight"),
        "inflight": len(st.get("inflight", {})), "waiting": len(st.get("waiters", {})),
        "cooldown_remaining": round(max(0.0, st.get("cooldown_until", 0.0) - now), 1),
        "consecutive_err": st.get("consecutive_err", 0),
        "consecutive_ok": st.get("consecutive_ok", 0), "last_status": st.get("last_status"),
        "last_event": st.get("last_event"),
        "degraded": is_degraded(),
    }


def health(key: str, open_cooldown: float = 8.0, degraded_err: int = 3) -> str:
    """Classify a gateway for pivot decisions: 'open' (circuit tripped — in real
    cooldown or erroring hard), 'degraded' (paced well below ceiling), or 'ok'."""
    st = inspect(key)
    if st is None:
        return "unknown"
    if st["cooldown_remaining"] > open_cooldown or st.get("consecutive_err", 0) >= degraded_err:
        return "open"
    ceiling = st.get("rate_ceiling") or 0
    if ceiling and st["rate"] < 0.5 * ceiling:
        return "degraded"
    return "ok"


def inspect_all() -> List[Dict[str, Any]]:
    out = []
    d = state_dir()
    for fn in os.listdir(d):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(d, fn), "r", encoding="utf-8") as fh:
                    st = json.load(fh)
            except (ValueError, OSError):
                continue
            got = inspect(st.get("key", fn))
            if got:
                out.append(got)
    return out


def recent_calls(key: str, limit: int = 30) -> List[Dict[str, Any]]:
    p = _paths(key)[2]
    try:
        with open(p, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out