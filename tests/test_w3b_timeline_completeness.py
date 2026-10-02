"""tests.test_w3b_timeline_completeness -- W3b item 11: the per-turn
timeline (halo_harness/debug_timeline.py) now records hooks (event name +
duration), permission waits (start, end, decision) and compactions, and
each `Session` keeps its OWN `TurnTimeline` instance (never the module's
shared default) so parallel sub-agent turns can't interleave into one
record. Hook/permission-wait tests reuse test_cc_session.py's own `cc:` +
fake-claude infrastructure (lighter than a real mock HTTP server) -- see
that file's own docstring for what's real vs scripted there.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

os.environ["BRIDGE_TEST_HOME"] = tempfile.mkdtemp(prefix="w3b-timeline-scratchhome-")
ensure_default_provider_credentials()

test, TESTS = new_registry()

REPO_DIR = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = REPO_DIR / "tests" / "helpers" / "fake_claude_cc.py"
_HOOK_SCRIPT_ARGV = [sys.executable, "-m", "tests.helpers.hook_scripts"]


@contextmanager
def _fake_claude_env():
    saved = {k: os.environ.get(k) for k in ("BRIDGE_CLAUDE_EXE", "FAKE_CLAUDE_CC_LOGGED_IN", "BRIDGE_TEST_CC_AUTH_STATUS")}
    os.environ["BRIDGE_CLAUDE_EXE"] = '"' + sys.executable + '" "' + str(FAKE_CLAUDE) + '"'
    os.environ["FAKE_CLAUDE_CC_LOGGED_IN"] = "1"
    os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)
    try:
        yield
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _new_cc_session(*, permission_engine=None, hook_runner=None, model="cc:fable"):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import parse_model_ref, resolve_model_profile
    from halo_harness.permissions import PermissionEngine
    proj = Path(tempfile.mkdtemp(prefix="w3b-timeline-proj-"))
    ref = parse_model_ref(model)
    profile = resolve_model_profile(ref, Path(tempfile.mkdtemp(prefix="w3b-timeline-state-")), {})
    session_ctx = SessionContext(cwd=proj, model_label=model)
    session = Session(
        cwd=proj, model_ref=ref, model_profile=profile, creds=None,
        state_dir=Path(tempfile.mkdtemp(prefix="w3b-timeline-sdir-")), model_label=model, session_context=session_ctx,
        max_turns=8, permission_engine=permission_engine or PermissionEngine(mode="auto", cwd=proj),
        hook_runner=hook_runner,
    )
    return session, proj


def _wait_for_event_kind(collected, kind, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for e in collected:
            if e.kind == kind:
                return e
        time.sleep(0.02)
    return None


@test
def test_hook_duration_recorded_in_the_turns_timeline(ctx: Ctx):
    """A real PreToolUse hook (json_allow -- exit 0, no rewrite) fires
    during a real turn -- the timeline's own `hooks` list gets one entry
    naming the event and a real (>=0) duration, via `Session._run_hook`."""
    from halo_harness.hooks import HookDef, HookRunner
    with _fake_claude_env():
        session, proj = _new_cc_session()
        target = proj / "f.txt"
        target.write_text("hi\n", encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(REPO_DIR))
        session.hook_runner = HookRunner(
            {"PreToolUse": [HookDef(type="command", matcher="Read", args=_HOOK_SCRIPT_ARGV + ["json_allow"])]},
            cwd=proj, session_id="w3b-hook-timeline", transcript_path=str(proj / "t.jsonl"), effective_env=env,
        )
        list(session.turn("TOOL:Read:" + json.dumps({"file_path": str(target)})))
        record = session._timeline.last_turn()
        ctx.check(f"a timeline record was captured, got {record}", record is not None)
        hooks = (record or {}).get("hooks") or []
        pre_hooks = [h for h in hooks if h.get("event") == "PreToolUse"]
        ctx.check(f"PreToolUse recorded in the timeline's own hooks list, got {hooks}", len(pre_hooks) == 1)
        ctx.check(f"a real non-negative duration was recorded, got {pre_hooks[0]!r}",
                  isinstance(pre_hooks[0].get("duration_ms"), (int, float)) and pre_hooks[0]["duration_ms"] >= 0)
        session.close_cc()


@test
def test_permission_wait_recorded_with_its_decision_in_the_turns_timeline(ctx: Ctx):
    """A real interactive permission ask, answered 'allow' from another
    thread (the TUI-pilot shape test_cc_session.py's own always-allow test
    already covers) -- the timeline's own `permission_waits` gets one
    entry with that resolved decision and a real start/end pair."""
    from halo_harness.permissions import Decision, PermissionEngine
    with _fake_claude_env():
        engine = PermissionEngine(mode="default", cwd=Path(tempfile.mkdtemp(prefix="w3b-permwait-")))
        session, proj = _new_cc_session(permission_engine=engine)
        session.interactive = True
        target = proj / "f.txt"
        target.write_text("hi\n", encoding="utf-8")
        collected = []
        t = threading.Thread(target=lambda: collected.extend(
            session.turn("TOOL:Read:" + json.dumps({"file_path": str(target)}))))
        t.start()
        req = _wait_for_event_kind(collected, "permission_request")
        ctx.check("permission_request shown", req is not None)
        ok = session.resolve_permission(req.data["id"], Decision("allow", "test allow")) if req else False
        ctx.check("resolve_permission accepted", ok)
        t.join(timeout=10)
        record = session._timeline.last_turn()
        waits = (record or {}).get("permission_waits") or []
        ctx.check(f"exactly one permission wait recorded, got {waits}", len(waits) == 1)
        entry = waits[0]
        ctx.check(f"resolved decision is 'allow', got {entry!r}", entry.get("decision") == "allow")
        ctx.check(f"end_ms is at or after start_ms, got {entry!r}", entry["end_ms"] >= entry["start_ms"])
        session.close_cc()


@test
def test_dismissed_permission_wait_records_a_decision_of_dismissed(ctx: Ctx):
    """Esc/abort mid-ask (`_apply_permission_decision`'s own `decision is
    None` branch) must still leave a recognizable entry -- "dismissed",
    never a crash or a silently-missing wait."""
    from halo_harness.permissions import PermissionEngine
    with _fake_claude_env():
        engine = PermissionEngine(mode="default", cwd=Path(tempfile.mkdtemp(prefix="w3b-permwait-dismiss-")))
        session, proj = _new_cc_session(permission_engine=engine)
        session.interactive = True
        target = proj / "f.txt"
        target.write_text("hi\n", encoding="utf-8")
        collected = []
        t = threading.Thread(target=lambda: collected.extend(
            session.turn("TOOL:Read:" + json.dumps({"file_path": str(target)}))))
        t.start()
        req = _wait_for_event_kind(collected, "permission_request")
        ctx.check("permission_request shown", req is not None)
        session.abort.set()
        t.join(timeout=10)
        record = session._timeline.last_turn()
        waits = (record or {}).get("permission_waits") or []
        ctx.check(f"exactly one permission wait recorded, got {waits}", len(waits) == 1)
        ctx.check(f"an aborted/dismissed ask records 'dismissed', got {waits[0]!r}",
                  waits[0].get("decision") == "dismissed")
        session.close_cc()


@test
def test_each_session_keeps_its_own_timeline_instance_never_the_shared_default(ctx: Ctx):
    """W3b item 11's own core architectural fix: two Sessions (standing in
    for a parent and a parallel sub-agent -- agent/subagent.py's own child
    Session is built exactly the same way, a plain `Session(...)` call)
    must never share one `TurnTimeline` -- proven at the object-identity
    level (the cheapest, most direct proof there is no shared mutable
    state left to race over), and against the `debug_timeline` module's
    own shared default, which neither should ever touch."""
    from halo_harness import debug_timeline
    with _fake_claude_env():
        session_a, _ = _new_cc_session(model="cc:fable")
        session_b, _ = _new_cc_session(model="cc:fable")
        ctx.check("two independent TurnTimeline instances", session_a._timeline is not session_b._timeline)
        ctx.check("neither is the debug_timeline module's own shared default",
                  session_a._timeline is not debug_timeline._default_timeline
                  and session_b._timeline is not debug_timeline._default_timeline)
        session_a.close_cc()
        session_b.close_cc()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
