"""halo_harness.debug_timeline -- 2.0.1 launch-hang investigation: an
opt-in, one-line-per-phase startup timeline (settings, instructions,
session build, MCP discovery, first paint), each with a millisecond
elapsed-since-start figure, so a hang on a real box can be localized to a
phase without guesswork. Wired in ONLY under `--debug`/`-d`
(`cli.py::_enable_debug_logging` calls `enable()`) -- `mark()` is a cheap
no-op otherwise (a plain bool check, no clock read), so sprinkling it
through `headless.build_session`/`tui/app.py` costs nothing on an ordinary
run.

Lines land in TWO places at once, per the brief ("in bridge.log and on
stderr with --debug"): stderr directly (visible immediately, even if
file logging itself failed to set up) and the "halo_harness" DEBUG logger
(`<state dir>/bridge.log`, already rotated/redacted by `providers.config.
setup_logging`/`RedactingFormatter` -- this module never opens a file of
its own).
"""

from __future__ import annotations

import sys
import time
from typing import Optional

_enabled = False
_t0: Optional[float] = None


def enable() -> None:
    """Starts the clock. Idempotent-ish: a second call just restarts it
    (there is only ever one real startup per process), never raises."""
    global _enabled, _t0
    _enabled = True
    _t0 = time.monotonic()


def is_enabled() -> bool:
    return _enabled


def mark(phase: str) -> None:
    """No-op unless `enable()` ran first (plain `halo`, no `--debug`).
    `phase`: a short label -- "settings", "instructions", "mcp discovery",
    "session build", "first paint" are this milestone's own five, but any
    caller may add more without changing this function."""
    if not _enabled or _t0 is None:
        return
    elapsed_ms = int((time.monotonic() - _t0) * 1000)
    line = f"halo: [timeline] {phase}: +{elapsed_ms}ms"
    try:
        print(line, file=sys.stderr)
    except Exception:
        pass
    try:
        import logging
        logging.getLogger("halo_harness").debug("[timeline] %s: +%dms", phase, elapsed_ms)
    except Exception:
        pass


def reset() -> None:
    """Test seam: clear state between tests (mirrors `cc_models.reset_
    cached_claude_auth_status`'s own convention)."""
    global _enabled, _t0
    _enabled = False
    _t0 = None


# ---------------------------------------------------------------------------
# 2.0.1 W3a: the PER-TURN timeline (`halo bugreport`, `/timeline`, `halo
# timeline --last N`) -- extends the startup-only timeline above into one
# record KEPT PER TURN (last 20, any process, `--debug` or not: unlike
# `mark()` above, this recording always runs, so /timeline has something to
# show even on a plain launch -- only the LIVE stderr/log printing is gated
# on `is_enabled()`/`--debug`, same as the startup lines). Fed from ONE
# choke point every turn's events already pass through regardless of
# dialect or caller (TUI or -p): `agent.loop.Session.turn`'s own thin
# wrapper around the renamed `_turn_inner` -- see that method's docstring.
# Scope cut (reported, not silently dropped): hooks (event+duration),
# permission-wait start/end/decision, and a compaction counter are NOT
# tracked here -- no event in the existing stream below carries hook
# identity/duration or a permission answer's own timestamp, and adding
# that instrumentation to agent/loop.py itself was out of this round's
# budget; request/header/first-token/tool/message-end timing, every tool
# call's own start/end/status, steers and retries/errors ARE tracked.
# ---------------------------------------------------------------------------
import collections as _collections  # noqa: E402 -- grouped with this section, not the module's startup-timeline half

_MAX_TURNS_KEPT = 20
_turns: "_collections.deque" = _collections.deque(maxlen=_MAX_TURNS_KEPT)
_current: "Optional[dict]" = None
_current_t0: Optional[float] = None
_current_tool_starts: dict = {}


def start_turn(turn: int) -> None:
    """Called once per `Session.turn()` call -- resets the in-progress
    record. Never raises; a caller that somehow starts a new turn before
    the previous one ended just drops the unfinished one (best-effort
    diagnostics, never allowed to affect the turn itself)."""
    global _current, _current_t0, _current_tool_starts
    _current = {
        "turn": turn, "request_sent_ms": None, "headers_ms": None, "first_reasoning_ms": None,
        "first_text_ms": None, "first_tool_call_ms": None, "message_end_ms": None,
        "tools": [], "steers": [], "retries": [],
    }
    _current_t0 = time.monotonic()
    _current_tool_starts = {}


def _elapsed_ms() -> int:
    return int((time.monotonic() - _current_t0) * 1000)


def _debug_print(line: str) -> None:
    if not _enabled:
        return
    try:
        print(f"halo: [timeline] {line}", file=sys.stderr)
    except Exception:
        pass


def record_turn_event(event) -> None:
    """The ONE place every turn's own events (whatever dialect/provider,
    TUI or -p) are inspected for timeline purposes -- never raises (a
    malformed/unexpected event shape is silently skipped; this is
    diagnostics, never allowed to break a real turn)."""
    if _current is None:
        return
    try:
        kind, data = event.kind, (event.data or {})
        if kind == "phase":
            state, phase_kind = data.get("state"), data.get("kind")
            if state == "request_sent" and _current["request_sent_ms"] is None:
                _current["request_sent_ms"] = _elapsed_ms()
                _debug_print(f"turn {_current['turn']} request_sent +{_current['request_sent_ms']}ms")
            elif state == "headers" and _current["headers_ms"] is None:
                _current["headers_ms"] = _elapsed_ms()
                _debug_print(f"turn {_current['turn']} headers +{_current['headers_ms']}ms")
            elif state == "first_token":
                ms = _elapsed_ms()
                if phase_kind == "reasoning" and _current["first_reasoning_ms"] is None:
                    _current["first_reasoning_ms"] = ms
                    _debug_print(f"turn {_current['turn']} first_reasoning +{ms}ms")
                elif phase_kind == "text" and _current["first_text_ms"] is None:
                    _current["first_text_ms"] = ms
                    _debug_print(f"turn {_current['turn']} first_text +{ms}ms")
                elif phase_kind == "tool" and _current["first_tool_call_ms"] is None:
                    _current["first_tool_call_ms"] = ms
                    _debug_print(f"turn {_current['turn']} first_tool_call +{ms}ms")
        elif kind == "tool_use_ready":
            tool_id, name = data.get("id"), data.get("name") or "?"
            if tool_id is not None:
                _current_tool_starts[tool_id] = (name, _elapsed_ms())
                _debug_print(f"turn {_current['turn']} tool {name} start +{_current_tool_starts[tool_id][1]}ms")
        elif kind == "tool_result":
            tool_id = data.get("id")
            start = _current_tool_starts.pop(tool_id, None) if tool_id is not None else None
            name = start[0] if start else "?"
            start_ms = start[1] if start else None
            end_ms = _elapsed_ms()
            status = "ok" if data.get("ok") else "error"
            _current["tools"].append({"name": name, "start_ms": start_ms, "end_ms": end_ms, "status": status})
            _debug_print(f"turn {_current['turn']} tool {name} {status} +{end_ms}ms")
        elif kind == "steer_restart":
            _current["steers"].append(data.get("text", ""))
            _debug_print(f"turn {_current['turn']} steer_restart +{_elapsed_ms()}ms")
        elif kind == "error":
            entry = {"status": data.get("err_type"), "error": data.get("message"), "ms": _elapsed_ms()}
            _current["retries"].append(entry)
            _debug_print(f"turn {_current['turn']} error {entry['status']} +{entry['ms']}ms")
        elif kind == "turn_done":
            _current["message_end_ms"] = _elapsed_ms()
            _debug_print(f"turn {_current['turn']} message_end +{_current['message_end_ms']}ms "
                         f"(reason={data.get('reason')})")
    except Exception:
        pass


def end_turn() -> "Optional[dict]":
    """Finalizes and stores the in-progress record (last `_MAX_TURNS_KEPT`
    kept); returns it (a plain dict, JSON-safe) for the caller to also log
    (`Session.turn`'s own wrapper appends it as one `timeline` session-log
    node) -- None if `start_turn` was never called (defensive only)."""
    global _current
    if _current is None:
        return None
    record = _current
    _turns.append(record)
    _current = None
    return record


def last_turn() -> "Optional[dict]":
    return _turns[-1] if _turns else None


def last_n_turns(n: int) -> list:
    if n <= 0:
        return []
    return list(_turns)[-n:]


def reset_turns() -> None:
    """Test seam: clear the per-turn history between tests."""
    global _current, _current_t0, _current_tool_starts
    _turns.clear()
    _current = None
    _current_t0 = None
    _current_tool_starts = {}
