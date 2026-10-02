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
# 2.0.1 W3a/W3b: the PER-TURN timeline (`halo bugreport`, `/timeline`, `halo
# timeline --last N`) -- extends the startup-only timeline above into one
# record KEPT PER TURN (last 20, any process, `--debug` or not: unlike
# `mark()` above, this recording always runs, so /timeline has something to
# show even on a plain launch -- only the LIVE stderr/log printing is gated
# on `is_enabled()`/`--debug`, same as the startup lines). Fed from TWO
# places now: the ONE choke point every turn's events already pass through
# regardless of dialect or caller (TUI or -p) -- `agent.loop.Session.turn`'s
# own thin wrapper around the renamed `_turn_inner` -- for request/header/
# first-token/tool/message-end timing, steers, retries/errors and (W3b)
# auto-compactions (already flow through that SAME stream mid-turn); and
# (W3b) two new direct call sites in agent/loop.py itself for hooks (no
# event of its own exists for "a hook ran") and permission waits (the
# `permission_request` event marks the ASK, never the answer/duration).
#
# W3b (item 11): `TurnTimeline` is now a class, not bare module state -- a
# `Session` keeps its OWN instance (`self._timeline`), so two PARALLEL
# sub-agent turns (agent/loop.py's own ThreadPoolExecutor batch dispatch)
# never interleave into the SAME record the way shared module-level state
# used to risk (one thread's `start_turn`/`end_turn` racing another's
# `record_turn_event` calls). The bare module-level functions below remain
# as a thin convenience shim over one shared DEFAULT instance -- existing
# callers/tests that never cared about per-session isolation (this module's
# own startup-timeline half, and any test exercising the mechanism in the
# abstract) keep working unchanged; `Session`/`agent/subagent.py` use their
# own instances exclusively, never this default one.
# ---------------------------------------------------------------------------
import collections as _collections  # noqa: E402 -- grouped with this section, not the module's startup-timeline half

_MAX_TURNS_KEPT = 20


class TurnTimeline:
    """One session's own per-turn timeline history -- see the module
    docstring above for the full rationale. Every public method mirrors
    what used to be a bare module-level function of the same name (now a
    thin wrapper over a shared default instance, kept for callers that
    don't need per-session isolation)."""

    def __init__(self) -> None:
        self._turns: "_collections.deque" = _collections.deque(maxlen=_MAX_TURNS_KEPT)
        self._current: "Optional[dict]" = None
        self._current_t0: Optional[float] = None
        self._current_tool_starts: dict = {}

    def start_turn(self, turn: int) -> None:
        """Called once per `Session.turn()` call -- resets the in-progress
        record. Never raises; a caller that somehow starts a new turn
        before the previous one ended just drops the unfinished one
        (best-effort diagnostics, never allowed to affect the turn
        itself)."""
        self._current = {
            "turn": turn, "request_sent_ms": None, "headers_ms": None, "first_reasoning_ms": None,
            "first_text_ms": None, "first_tool_call_ms": None, "message_end_ms": None,
            "tools": [], "steers": [], "retries": [],
            # W3b item 11: hooks (one entry per RUN, not per registered
            # hook def -- a matcher with zero matching hooks this turn
            # never appears), permission waits (start/end/decision) and
            # compactions (auto only -- see the module docstring).
            "hooks": [], "permission_waits": [], "compactions": [],
        }
        self._current_t0 = time.monotonic()
        self._current_tool_starts = {}

    def elapsed_ms(self) -> int:
        """W3b: public now (was `_elapsed_ms`) -- `Session`'s own direct
        hook/permission-wait recording needs a "now" timestamp relative to
        turn start from OUTSIDE this class, same clock `record_turn_event`
        itself already uses. 0 when no turn is in progress (a hook that
        fires outside any turn's lifecycle -- SessionStart/SessionEnd --
        has no turn to attach a real figure to; callers only ever record
        against an active turn in practice, this is just a safe default)."""
        if self._current_t0 is None:
            return 0
        return int((time.monotonic() - self._current_t0) * 1000)

    def _debug_print(self, line: str) -> None:
        _debug_print(line)

    def record_turn_event(self, event) -> None:
        """The ONE place every turn's own events (whatever dialect/
        provider, TUI or -p) are inspected for timeline purposes -- never
        raises (a malformed/unexpected event shape is silently skipped;
        this is diagnostics, never allowed to break a real turn)."""
        if self._current is None:
            return
        try:
            kind, data = event.kind, (event.data or {})
            if kind == "phase":
                state, phase_kind = data.get("state"), data.get("kind")
                if state == "request_sent" and self._current["request_sent_ms"] is None:
                    self._current["request_sent_ms"] = self.elapsed_ms()
                    self._debug_print(f"turn {self._current['turn']} request_sent +{self._current['request_sent_ms']}ms")
                elif state == "headers" and self._current["headers_ms"] is None:
                    self._current["headers_ms"] = self.elapsed_ms()
                    self._debug_print(f"turn {self._current['turn']} headers +{self._current['headers_ms']}ms")
                elif state == "first_token":
                    ms = self.elapsed_ms()
                    if phase_kind == "reasoning" and self._current["first_reasoning_ms"] is None:
                        self._current["first_reasoning_ms"] = ms
                        self._debug_print(f"turn {self._current['turn']} first_reasoning +{ms}ms")
                    elif phase_kind == "text" and self._current["first_text_ms"] is None:
                        self._current["first_text_ms"] = ms
                        self._debug_print(f"turn {self._current['turn']} first_text +{ms}ms")
                    elif phase_kind == "tool" and self._current["first_tool_call_ms"] is None:
                        self._current["first_tool_call_ms"] = ms
                        self._debug_print(f"turn {self._current['turn']} first_tool_call +{ms}ms")
            elif kind == "tool_use_ready":
                tool_id, name = data.get("id"), data.get("name") or "?"
                if tool_id is not None:
                    self._current_tool_starts[tool_id] = (name, self.elapsed_ms())
                    self._debug_print(f"turn {self._current['turn']} tool {name} start "
                                       f"+{self._current_tool_starts[tool_id][1]}ms")
            elif kind == "tool_result":
                tool_id = data.get("id")
                start = self._current_tool_starts.pop(tool_id, None) if tool_id is not None else None
                name = start[0] if start else "?"
                start_ms = start[1] if start else None
                end_ms = self.elapsed_ms()
                status = "ok" if data.get("ok") else "error"
                self._current["tools"].append({"name": name, "start_ms": start_ms, "end_ms": end_ms, "status": status})
                self._debug_print(f"turn {self._current['turn']} tool {name} {status} +{end_ms}ms")
            elif kind == "steer_restart":
                self._current["steers"].append(data.get("text", ""))
                self._debug_print(f"turn {self._current['turn']} steer_restart +{self.elapsed_ms()}ms")
            elif kind == "error":
                entry = {"status": data.get("err_type"), "error": data.get("message"), "ms": self.elapsed_ms()}
                self._current["retries"].append(entry)
                self._debug_print(f"turn {self._current['turn']} error {entry['status']} +{entry['ms']}ms")
            elif kind == "compaction":
                # W3b item 11: auto-compaction only -- a manual `/compact`
                # runs through `Session._pump_compaction` -> `_run_
                # compaction` DIRECTLY, never through `turn()`'s own
                # wrapper (it isn't a turn), so it never reaches here; an
                # AUTO-compaction (`_maybe_auto_compact`, called from
                # inside a turn's own body) yields this SAME event kind
                # through the SAME stream `turn()` wraps, same as every
                # other case in this method.
                phase_name = data.get("phase")
                entry = {"phase": phase_name, "trigger": data.get("trigger"), "ms": self.elapsed_ms()}
                if phase_name == "done":
                    entry["tokens_before"] = data.get("tokens_before")
                    entry["tokens_after"] = data.get("tokens_after")
                elif phase_name == "failed":
                    entry["reason"] = data.get("reason")
                self._current["compactions"].append(entry)
                self._debug_print(f"turn {self._current['turn']} compaction {phase_name} +{entry['ms']}ms")
            elif kind == "turn_done":
                self._current["message_end_ms"] = self.elapsed_ms()
                self._debug_print(f"turn {self._current['turn']} message_end +{self._current['message_end_ms']}ms "
                                   f"(reason={data.get('reason')})")
        except Exception:
            pass

    def record_hook(self, event_name: str, duration_ms: float) -> None:
        """W3b item 11: one entry per hook RUN (`Session._run_hook`'s own
        wrapper around every `hook_runner.run(...)` call site -- the ONE
        choke point every hook invocation already shares, regardless of
        which of the ~10 call sites in agent/loop.py/agent/subagent.py
        fired it). A no-op outside an active turn (SessionStart/SessionEnd
        fire before/after any turn exists) -- same "nothing to attach to"
        rule `record_turn_event` already follows."""
        if self._current is None:
            return
        try:
            entry = {"event": event_name, "ms": self.elapsed_ms(), "duration_ms": round(duration_ms, 1)}
            self._current["hooks"].append(entry)
            self._debug_print(f"turn {self._current['turn']} hook {event_name} +{entry['ms']}ms "
                               f"({entry['duration_ms']}ms)")
        except Exception:
            pass

    def record_permission_wait(self, start_ms: int, end_ms: int, decision: "Optional[str]") -> None:
        """W3b item 11: one entry per blocking permission ask that actually
        parked the turn (`Session`'s own `pending_ask`/`_await_permission_
        decision` call site) -- `decision` is the resolved `Decision.
        action` ("allow"/"deny"), or "dismissed"/"interrupted" when no real
        answer ever came back (Esc, abort). The turn's own `permission_
        request` EVENT marks only the ASK, with no duration or outcome of
        its own, and `record_turn_event` never special-cased it -- this is
        the answer and how long it took, which no existing event carries."""
        if self._current is None:
            return
        try:
            entry = {"start_ms": start_ms, "end_ms": end_ms, "decision": decision}
            self._current["permission_waits"].append(entry)
            self._debug_print(f"turn {self._current['turn']} permission_wait {decision} "
                               f"+{start_ms}ms -> +{end_ms}ms")
        except Exception:
            pass

    def end_turn(self) -> "Optional[dict]":
        """Finalizes and stores the in-progress record (last
        `_MAX_TURNS_KEPT` kept); returns it (a plain dict, JSON-safe) for
        the caller to also log (`Session.turn`'s own wrapper appends it as
        one `timeline` session-log node) -- None if `start_turn` was never
        called (defensive only)."""
        if self._current is None:
            return None
        record = self._current
        self._turns.append(record)
        self._current = None
        return record

    def last_turn(self) -> "Optional[dict]":
        return self._turns[-1] if self._turns else None

    def last_n_turns(self, n: int) -> list:
        if n <= 0:
            return []
        return list(self._turns)[-n:]

    def reset(self) -> None:
        """Test seam (and `Session.clear()`'s own use, a fresh conversation
        starting a fresh timeline): clear the per-turn history."""
        self._turns.clear()
        self._current = None
        self._current_t0 = None
        self._current_tool_starts = {}


def _debug_print(line: str) -> None:
    if not _enabled:
        return
    try:
        print(f"halo: [timeline] {line}", file=sys.stderr)
    except Exception:
        pass


# Back-compat shim (see the class docstring/module comment above): one
# shared DEFAULT instance, and the exact same bare function names this
# module exposed before the W3b per-session refactor.
_default_timeline = TurnTimeline()


def start_turn(turn: int) -> None:
    _default_timeline.start_turn(turn)


def record_turn_event(event) -> None:
    _default_timeline.record_turn_event(event)


def record_hook(event_name: str, duration_ms: float) -> None:
    _default_timeline.record_hook(event_name, duration_ms)


def record_permission_wait(start_ms: int, end_ms: int, decision: "Optional[str]") -> None:
    _default_timeline.record_permission_wait(start_ms, end_ms, decision)


def end_turn() -> "Optional[dict]":
    return _default_timeline.end_turn()


def last_turn() -> "Optional[dict]":
    return _default_timeline.last_turn()


def last_n_turns(n: int) -> list:
    return _default_timeline.last_n_turns(n)


def reset_turns() -> None:
    """Test seam: clear the per-turn history between tests."""
    _default_timeline.reset()
