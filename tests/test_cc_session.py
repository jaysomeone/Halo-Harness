"""tests.test_cc_session -- H11 Part C: agent.loop.Session driving the
`cc:` route end to end against `tests/helpers/fake_claude_cc.py` (a real
`claude` stand-in: real stream-json protocol, a REAL MCP client spawning
the REAL `python -m halo_harness.ccbridge` child against a REAL
ToolBridgeServer -- nothing about the bridge itself is mocked, only the
"model" driving claude's own side of the stream-json protocol is
scripted). Covers: lazy start / one subprocess per session, tools/list ==
frozen catalog, tools/call obeys a deny rule and runs in auto, a
permission card in default mode (TUI-pilot-shaped: a real interactive
wait answered from another thread), hooks fire exactly once, pairing
invariants after Esc mid-call, a usage node with `estimate`, resume via
the uuid mapping, steer, model switch cc<->or, claude missing/not-logged-
in errors, and `--tools ""` argv shape.
"""
import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

# H11b finding 27: THIS module's own scratch home -- every real Session
# this file builds writes its session log (and, once cc: starts, its
# ccbridge run/ dir) under here, never the real ~/.halo. Set at
# IMPORT time (not per-test) so it's active before this module's very
# first test runs regardless of whether it's driven by `python tests/
# test_cc_session.py` directly or by tests/run_all.py's own per-module
# import-then-run loop -- this module no longer depends on test_bash_
# background_jobs.py (or any other module) happening to set this first.
os.environ["BRIDGE_TEST_HOME"] = tempfile.mkdtemp(prefix="cc-session-scratchhome-")

# H15 part 2 addendum 3.1: or:/ant:/dbx: refs below need a believable
# default credential now that parse_model_ref refuses an auto-detected-
# disabled provider. Deliberately NOT a blanket BRIDGE_TEST_CC_AUTH_STATUS
# default here (unlike other files) -- this file's own `_fake_claude_env`/
# bare `cc:` tests each manage that (and `BRIDGE_CLAUDE_EXE`) themselves,
# including two that deliberately simulate "not logged in"/"binary
# missing" and need the REAL absence, not a default masking it; those two
# instead call `enable("claude_subscription")` explicitly (an override
# bypasses auto-detection) so parse_model_ref succeeds and the specific
# failure is still exercised turn-time, inside cc_runtime's own
# `_preflight_cc`, exactly as these tests were written to check.
ensure_default_provider_credentials()
# W6b section E: that call now also defaults BRIDGE_TEST_CC_AUTH_STATUS
# (closes a WSL hang in an unrelated module that never managed it at all,
# tests/test_doctor_mcp_config_cli.py) -- popped right back off here, once,
# to preserve the "deliberately NOT a blanket default" contract documented
# just above: the two tests it names need the REAL absence of this var to
# exercise the actual claude-binary-missing/not-logged-in code paths
# turn-time, not a default masking them underneath `enable("claude_
# subscription")`'s own auto-detection bypass.
os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)

test, TESTS = new_registry()

REPO_DIR = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = REPO_DIR / "tests" / "helpers" / "fake_claude_cc.py"
_HOOK_SCRIPT_ARGV = [sys.executable, "-m", "tests.helpers.hook_scripts"]


@contextmanager
def _fake_claude_env(*, logged_in: bool = True):
    saved = {k: os.environ.get(k) for k in
             ("BRIDGE_CLAUDE_EXE", "FAKE_CLAUDE_CC_LOGGED_IN", "BRIDGE_TEST_CC_AUTH_STATUS",
              "FAKE_CLAUDE_CC_TOOL2_WINDOW_S")}
    os.environ["BRIDGE_CLAUDE_EXE"] = '"' + sys.executable + '" "' + str(FAKE_CLAUDE) + '"'
    os.environ["FAKE_CLAUDE_CC_LOGGED_IN"] = "1" if logged_in else "0"
    # The two-call fake waits up to this long for a steer to arrive between
    # its calls (it stops waiting the moment one does), so the absorption
    # test no longer depends on how fast this box moves a line through the
    # stream; a run without a steer pays the full window once.
    os.environ["FAKE_CLAUDE_CC_TOOL2_WINDOW_S"] = "4"
    os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)  # the fake's own real "auth status" answers this now
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _new_cc_session(*, permission_engine=None, hook_runner=None, cwd=None, model="cc:fable",
                     interactive=False, agents=None, session_catalog=None, mcp_manager=None):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import parse_model_ref, resolve_model_profile
    from halo_harness.permissions import PermissionEngine

    proj = cwd or Path(tempfile.mkdtemp(prefix="cc-sess-proj-"))
    proj.mkdir(parents=True, exist_ok=True)
    ref = parse_model_ref(model)
    profile = resolve_model_profile(ref, Path(tempfile.mkdtemp(prefix="cc-sess-state-")), {})
    session_ctx = SessionContext(cwd=proj, model_label=model)
    session = Session(
        cwd=proj, model_ref=ref, model_profile=profile, creds=None,
        state_dir=Path(tempfile.mkdtemp(prefix="cc-sess-sdir-")), model_label=model, session_context=session_ctx,
        max_turns=8, permission_engine=permission_engine or PermissionEngine(mode="auto", cwd=proj),
        hook_runner=hook_runner, agents=agents, session_catalog=session_catalog, mcp_manager=mcp_manager,
    )
    session.interactive = interactive
    return session, proj


def _wait_for_event_kind(collected, kind, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for e in collected:
            if e.kind == kind:
                return e
        time.sleep(0.02)
    return None


def _join_bounded_by_progress(t: threading.Thread, session, *, stall_budget_s: float = 10.0) -> None:
    """Halo 2.0.2 round C: "make the wait bounded by progress, not a
    fixed deadline, so a loaded machine makes it slower, never red" --
    seen flaky under load in three separate worker runs, a flat `t.join
    (timeout=10)` loses a race against real CPU contention even though
    the turn is still genuinely moving forward, just slower. Polls in
    short slices instead, resetting the stall budget every time the
    session's own log visibly grows (proof of real, ongoing activity --
    `cc_runtime.py` appends a node per tool_use/tool_result/steer-
    consumed/final-text/usage as a `cc:` turn actually progresses, not
    just once at the very end) -- only a stretch with NO log growth AT
    ALL for the full `stall_budget_s` counts as "actually stuck", same
    distinction a liveness timeout makes everywhere else in this
    codebase. A genuinely hung thread still gives up in bounded time;
    a merely slow one never loses the race against an arbitrary
    deadline just because the box happened to be busy."""
    last_progress = time.monotonic()
    last_node_count = len(session.log.nodes())
    while t.is_alive():
        t.join(timeout=0.1)
        node_count = len(session.log.nodes())
        if node_count != last_node_count:
            last_node_count = node_count
            last_progress = time.monotonic()
        elif time.monotonic() - last_progress > stall_budget_s:
            break


# ---- lazy start / one subprocess per session -------------------------------

@test
def test_no_subprocess_until_first_cc_turn(ctx: Ctx):
    with _fake_claude_env():
        session, _ = _new_cc_session()
        ctx.check("no _cc_state before any turn", getattr(session, "_cc_state", None) is None)
        list(session.turn("reply with the single word pong"))
        ctx.check("_cc_state exists after the first turn", session._cc_state is not None)
        ctx.check("subprocess is alive", session._cc_state.process.alive)
        session.close_cc()


@test
def test_one_subprocess_reused_across_turns(ctx: Ctx):
    with _fake_claude_env():
        session, _ = _new_cc_session()
        list(session.turn("reply with the single word pong"))
        pid1 = session._cc_state.process.pid
        list(session.turn("reply with the single word pong"))
        pid2 = session._cc_state.process.pid
        ctx.check(f"same pid reused, got {pid1} then {pid2}", pid1 == pid2)
        session.close_cc()


@test
def test_pong_reply_and_session_id_stable(ctx: Ctx):
    with _fake_claude_env():
        session, _ = _new_cc_session()
        events_ = list(session.turn("reply with the single word pong"))
        text = "".join(e.data.get("text", "") for e in events_ if e.kind == "text_delta")
        ctx.check(f"got pong, text={text!r}", text == "pong")
        me = [e for e in events_ if e.kind == "message_end"]
        ctx.check("message_end present", len(me) == 1)
        ctx.check("turn_done end_turn", events_[-1].kind == "turn_done" and events_[-1].data["reason"] == "end_turn")
        session.close_cc()


# ---- tools/list == frozen catalog, tools/call paths ------------------------

@test
def test_bridge_tools_list_equals_frozen_catalog(ctx: Ctx):
    from halo_harness.agent.cc_runtime import bridge_list_tools
    with _fake_claude_env():
        session, _ = _new_cc_session()
        expected_names = sorted(t["name"] for t in session.tool_registry.definitions())
        got_names = sorted(t["name"] for t in bridge_list_tools(session))
        ctx.check(f"names match, expected={expected_names[:3]}..., got={got_names[:3]}...",
                   expected_names == got_names)
        session.close_cc()


@test
def test_tools_call_obeys_deny_rule(ctx: Ctx):
    from halo_harness.permissions import PermissionEngine, parse_rule
    with _fake_claude_env():
        engine = PermissionEngine(mode="default", cwd=Path(tempfile.mkdtemp(prefix="cc-deny-")),
                                    deny_rules=[parse_rule("Read", source="test", action="deny")])
        session, proj = _new_cc_session(permission_engine=engine)
        target = proj / "f.txt"
        target.write_text("hi\n", encoding="utf-8")
        events_ = list(session.turn("TOOL:Read:" + json.dumps({"file_path": str(target)})))
        results = [e for e in events_ if e.kind == "tool_result"]
        ctx.check(f"one tool_result, got {results}", len(results) == 1)
        ctx.check("denied (ok False)", results[0].data["ok"] is False)
        ctx.check("logged as a permission_denial", len(session.permission_denials) == 1
                   and session.permission_denials[0]["tool_name"] == "Read")
        session.close_cc()


@test
def test_tools_call_runs_in_auto_without_asking(ctx: Ctx):
    with _fake_claude_env():
        session, proj = _new_cc_session()  # default permission_engine is mode="auto"
        target = proj / "f.txt"
        target.write_text("line1\nline2\n", encoding="utf-8")
        events_ = list(session.turn("TOOL:Read:" + json.dumps({"file_path": str(target)})))
        ctx.check("no permission_request in auto", not any(e.kind == "permission_request" for e in events_))
        results = [e for e in events_ if e.kind == "tool_result"]
        ctx.check(f"the Read actually ran, got {results}", len(results) == 1 and results[0].data["ok"] is True)
        session.close_cc()


@test
def test_tools_call_shows_permission_card_in_default_mode_and_runs_after_allow(ctx: Ctx):
    """TUI-pilot-shaped: a real interactive session parked on
    resolve_permission, answered from another thread -- mirrors how the
    real TUI's PermissionCard callback reaches Controller.answer_permission."""
    from halo_harness.permissions import Decision, PermissionEngine
    with _fake_claude_env():
        engine = PermissionEngine(mode="default", cwd=Path(tempfile.mkdtemp(prefix="cc-ask-")))
        session, proj = _new_cc_session(permission_engine=engine)
        session.interactive = True
        newfile = proj / "new.txt"

        collected = []
        t = threading.Thread(target=lambda: collected.extend(
            session.turn("TOOL:Write:" + json.dumps({"file_path": str(newfile), "content": "hi\n"}))))
        t.start()
        req = _wait_for_event_kind(collected, "permission_request")
        ctx.check("permission_request card shown in default mode", req is not None)
        ok = session.resolve_permission(req.data["id"], Decision("allow", "test allow")) if req else False
        ctx.check("resolve_permission accepted", ok)
        t.join(timeout=10)
        ctx.check("Write actually ran after allow", newfile.exists() and newfile.read_text() == "hi\n")
        session.close_cc()


# ---- hooks fire exactly once ------------------------------------------------

@test
def test_hooks_fire_exactly_once_pre_and_post_tool_use(ctx: Ctx):
    """`once_counter` (tests/helpers/hook_scripts.py) appends one line per
    REAL invocation to HOOK_ONCE_COUNTER_FILE -- counting real invocations
    is the only reliable way to prove a hook fired exactly once. PreToolUse
    and PostToolUse use SEPARATE counter files (HookRunner._env_for builds
    each hook's env from the SAME effective_env, so both hooks would share
    one file/count if not for this)."""
    from halo_harness.hooks import HookDef, HookRunner

    proj = Path(tempfile.mkdtemp(prefix="cc-hook-proj-"))
    pre_counter = proj / "pre.count"
    post_counter = proj / "post.count"
    target = proj / "f.txt"
    target.write_text("hi\n", encoding="utf-8")

    pre_env = dict(os.environ, PYTHONPATH=str(REPO_DIR), HOOK_ONCE_COUNTER_FILE=str(pre_counter))
    post_env = dict(os.environ, PYTHONPATH=str(REPO_DIR), HOOK_ONCE_COUNTER_FILE=str(post_counter))
    # Two HookRunners (one per event, each with its OWN effective_env) --
    # combine_outcomes/HookRunner.run only needs `has_hooks`/`run` per
    # event, so a session.hook_runner proxy that dispatches to whichever
    # of the two actually owns that event keeps this simple.
    pre_runner = HookRunner({"PreToolUse": [HookDef(type="command", matcher="Read",
                                                       args=_HOOK_SCRIPT_ARGV + ["once_counter"])]},
                              cwd=proj, session_id="hook-once-pre", transcript_path=str(proj / "t1.jsonl"),
                              effective_env=pre_env)
    post_runner = HookRunner({"PostToolUse": [HookDef(type="command", matcher="Read",
                                                          args=_HOOK_SCRIPT_ARGV + ["once_counter"])]},
                               cwd=proj, session_id="hook-once-post", transcript_path=str(proj / "t2.jsonl"),
                               effective_env=post_env)

    class _CombinedRunner:
        def has_hooks(self, event):
            return pre_runner.has_hooks(event) or post_runner.has_hooks(event)

        def payload(self, event, **kw):
            return (pre_runner if event == "PreToolUse" else post_runner).payload(event, **kw)

        def run(self, event, payload, **kw):
            return (pre_runner if event == "PreToolUse" else post_runner).run(event, payload, **kw)

        def run_session_end(self, reason):
            pass

    with _fake_claude_env():
        session, _ = _new_cc_session(cwd=proj, hook_runner=_CombinedRunner())
        list(session.turn("TOOL:Read:" + json.dumps({"file_path": str(target)})))
        session.close_cc()

    def _count(p: Path) -> int:
        if not p.exists():
            return 0
        return len([ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()])

    ctx.check(f"PreToolUse fired exactly once, count={_count(pre_counter)}", _count(pre_counter) == 1)
    ctx.check(f"PostToolUse fired exactly once, count={_count(post_counter)}", _count(post_counter) == 1)


# ---- pairing invariants after Esc mid-call --------------------------------

@test
def test_pairing_invariant_after_esc_before_any_tool_call(ctx: Ctx):
    from halo_harness.agent.invariants import find_unpaired_tool_use_ids
    with _fake_claude_env():
        session, _ = _new_cc_session()
        collected = []
        t = threading.Thread(target=lambda: collected.extend(session.turn("SLEEP:5 nothing to see")))
        t.start()
        # W3b test-determinism: same fix as test_esc_then_next_turn_
        # restarts_and_resumes above -- a fixed sleep here raced ensure_cc_
        # state() under load; poll for the real readiness signal instead.
        deadline = time.monotonic() + 10.0
        while getattr(session, "_cc_state", None) is None and time.monotonic() < deadline:
            time.sleep(0.02)
        ctx.check("the cc: process actually started before aborting it",
                  getattr(session, "_cc_state", None) is not None)
        session.abort.set()
        t.join(timeout=10)
        ctx.check("turn ended interrupted", collected[-1].kind == "turn_done"
                   and collected[-1].data["reason"] == "interrupted")
        unpaired = find_unpaired_tool_use_ids(session.log)
        ctx.check(f"no unpaired tool_use ids, got {unpaired}", unpaired == [])
        # "ABORTED" reaches the turn generator the INSTANT interrupt() is
        # sent (by design -- Esc must feel instant); the actual kill-if-
        # still-alive runs in a background cleanup thread, published to
        # state.cleanup_thread a moment later -- poll for it, then join it,
        # before asserting the process is actually gone.
        deadline = time.monotonic() + 10
        cleanup = None
        while time.monotonic() < deadline:
            cleanup = session._cc_state.cleanup_thread
            if cleanup is not None:
                break
            time.sleep(0.02)
        if cleanup is not None:
            cleanup.join(timeout=10)
        else:
            time.sleep(1.0)  # best-effort fallback, should be unreachable
        ctx.check("subprocess actually gone", not session._cc_state.process.alive)
        session.close_cc()


@test
def test_esc_then_next_turn_restarts_and_resumes(ctx: Ctx):
    with _fake_claude_env():
        session, _ = _new_cc_session()
        collected = []
        t = threading.Thread(target=lambda: collected.extend(session.turn("SLEEP:5 nothing")))
        t.start()
        # W3b test-determinism: a fixed `time.sleep(0.3)` here assumed
        # ensure_cc_state() (spawns the real fake-claude subprocess, sets
        # session._cc_state) always finishes within 0.3s -- under load
        # (the Kali VM running concurrently with the Windows suites) the
        # worker thread above can take longer just to get SCHEDULED, so
        # `abort.set()` fired before `_cc_state` existed, the abort
        # watcher thread (cc_runtime.py's `watch_abort`, started only
        # AFTER ensure_cc_state returns) never saw it, and the turn ran to
        # completion unaborted -- the exact "restarted with a new pid,
        # X -> X" (no restart at all) failure mode. Poll on the real
        # readiness signal instead: `watch_abort` starts checking
        # `session.abort` within `_ABORT_POLL_S` (0.05s) of `_cc_state`
        # existing, so once that attribute appears, setting `abort` is
        # guaranteed to be seen regardless of how slow the system is.
        deadline = time.monotonic() + 10.0
        while getattr(session, "_cc_state", None) is None and time.monotonic() < deadline:
            time.sleep(0.02)
        ctx.check("the cc: process actually started before aborting it",
                  getattr(session, "_cc_state", None) is not None)
        session.abort.set()
        t.join(timeout=10)
        old_pid = session._cc_state.process.pid

        # W5b Linux determinism (carried from W3b): `t.join()` only proves
        # the turn GENERATOR finished -- it reads "ABORTED" off watch_abort's
        # queue the instant that's put there, which happens BEFORE
        # watch_abort goes on to create the cleanup thread and publish it to
        # `state.cleanup_thread` (see cc_runtime.py's own watch_abort). On a
        # cold/loaded box that gap can outlast `t.join()` returning, so
        # ensure_cc_state() on the very next turn (below) could still find
        # `cleanup_thread` is None and `process.alive` is True (the kill-if-
        # needed hasn't landed yet) and silently REUSE the old process
        # instead of restarting -- the exact "restarted with a new pid,
        # X -> X" (no restart at all) failure seen on a fresh Kali process.
        # Poll for the real readiness signal (cleanup_thread appearing, then
        # actually finishing) before touching abort/turn again.
        deadline = time.monotonic() + 10.0
        cleanup = None
        while time.monotonic() < deadline:
            cleanup = session._cc_state.cleanup_thread
            if cleanup is not None:
                break
            time.sleep(0.02)
        ctx.check("the abort cleanup thread actually started", cleanup is not None)
        if cleanup is not None:
            cleanup.join(timeout=10)
        ctx.check("the aborted process is actually gone before restarting",
                  not session._cc_state.process.alive)

        session.abort.clear()
        events_ = list(session.turn("reply with the single word pong"))
        new_pid = session._cc_state.process.pid
        text = "".join(e.data.get("text", "") for e in events_ if e.kind == "text_delta")
        ctx.check(f"restarted with a new pid, {old_pid} -> {new_pid}", old_pid != new_pid)
        ctx.check(f"still works after restart, got {text!r}", text == "pong")
        session.close_cc()


# ---- usage node / estimate --------------------------------------------------

@test
def test_usage_node_marked_estimate_route_cc(ctx: Ctx):
    with _fake_claude_env():
        session, _ = _new_cc_session()
        list(session.turn("reply with the single word pong"))
        usage_nodes = [n for n in session.log.nodes() if n.get("type") == "usage"]
        ctx.check(f"one usage node, got {len(usage_nodes)}", len(usage_nodes) == 1)
        n = usage_nodes[0]
        ctx.check("estimate True", n.get("estimate") is True)
        ctx.check("route cc", n.get("route") == "cc")
        ctx.check("provider cc", n.get("provider") == "cc")
        ctx.check("cost_usd is a number", isinstance(n.get("cost_usd"), (int, float)))
        session.close_cc()


# ---- resume via the uuid mapping / --continue ------------------------------

@test
def test_cc_session_uuid_is_deterministic(ctx: Ctx):
    from halo_harness.agent.cc_process import cc_session_uuid
    import uuid as uuid_mod
    a = cc_session_uuid("abc123")
    b = cc_session_uuid("abc123")
    c = cc_session_uuid("different")
    ctx.check("same input -> same uuid", a == b)
    ctx.check("different input -> different uuid", a != c)
    ctx.check("valid uuid string", str(uuid_mod.UUID(a)) == a)


@test
def test_new_process_with_prior_cc_history_uses_resume_not_session_id(ctx: Ctx):
    """A brand new Session object (as a fresh `halo -r`/`--continue`
    process would build) wrapping a log that ALREADY has a prior cc: usage
    node must use --resume on its very first ensure_cc_state call, never
    --session-id (which claude rejects once that id already exists)."""
    from halo_harness.agent.log import SessionLog
    from halo_harness.agent.cc_runtime import ensure_cc_state
    with _fake_claude_env():
        session, proj = _new_cc_session()
        list(session.turn("reply with the single word pong"))
        old_session_id = session.log.session_id
        session.close_cc()

        # simulate a FRESH process: a new Session wrapping the SAME log id
        log2 = SessionLog(proj, session_id=old_session_id)
        log2._nodes = log2.read_all()
        session2, _ = _new_cc_session(cwd=proj)
        session2.log = log2
        state = ensure_cc_state(session2)
        ctx.check("--resume in argv, not --session-id", "--resume" in state.process.argv
                   and "--session-id" not in state.process.argv)
        session2.close_cc()


# ---- steer -------------------------------------------------------------------

@test
def test_steer_sends_immediately_and_is_logged(ctx: Ctx):
    with _fake_claude_env():
        session, _ = _new_cc_session()
        collected = []
        t = threading.Thread(target=lambda: collected.extend(session.turn("SLEEP:2 first")))
        t.start()
        # wait for the subprocess to actually be up (first-ever start can
        # take a while -- importing the mcp SDK etc.) before steering, so
        # this test exercises "sent while busy", not a startup-timing race.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and getattr(session, "_cc_state", None) is None:
            time.sleep(0.02)
        ok = session.steer("reply with the single word pong")
        ctx.check("steer accepted while busy", ok)
        t.join(timeout=10)
        steer_nodes = [n for n in session.log.nodes() if n.get("type") == "user" and n.get("kind") == "steer"]
        ctx.check(f"steer text logged as a 'steer'-kind user node, got {steer_nodes}", len(steer_nodes) == 1)
        session.close_cc()


@test
def test_steer_returns_false_when_not_busy(ctx: Ctx):
    with _fake_claude_env():
        session, _ = _new_cc_session()
        ctx.check("steer with no turn running returns False", session.steer("hello") is False)
        session.close_cc()


# ---- model switch cc <-> or -------------------------------------------------

@test
def test_switch_from_cc_to_openrouter_and_back_queues_conversation_so_far(ctx: Ctx):
    from halo_harness.model import ModelRef, ModelProfile
    with _fake_claude_env():
        session, _ = _new_cc_session()
        list(session.turn("reply with the single word pong"))
        session.close_cc()

        or_ref = ModelRef(raw="or:mock/x", provider="openrouter", model="mock/x", dialect="openai-chat")
        session.set_model(or_ref, ModelProfile())
        ctx.check("switched away from cc cleanly", session.model_ref.provider == "openrouter")

        cc_ref = ModelRef(raw="cc:fable", provider="cc", model="claude-fable-5-1", dialect="cc-subprocess")
        session.set_model(cc_ref, ModelProfile(context_tokens=1_000_000, max_output_tokens=64_000))
        ctx.check("switching back into cc: queued a conversation-so-far context",
                   bool(getattr(session, "_cc_pending_context", None)))
        ctx.check("prior turn's text is in the queued context", "pong" in session._cc_pending_context
                   or "reply with the single word pong" in session._cc_pending_context)
        session.close_cc()


# ---- claude missing / not logged in ----------------------------------------

@test
def test_claude_not_logged_in_gives_a_precise_error(ctx: Ctx):
    from halo_harness.providers.enablement import enable
    from halo_harness.theme import get_config_value, set_config_value
    # 1.0.1 part 2 fixpass finding 10: this module's own BRIDGE_TEST_HOME is
    # set ONCE at import time (never reset between tests) -- `enable()`'s
    # override must be restored here, or it leaks into every cc: test that
    # runs afterward in this same process (masking a real auto-detection
    # regression behind a permanent override).
    providers_before = get_config_value("providers", default=None)
    enable("claude_subscription")  # bypass auto-detection -- see this test's own module-level note
    try:
        with _fake_claude_env(logged_in=False):
            session, _ = _new_cc_session()
            events_ = list(session.turn("reply with the single word pong"))
            errors = [e for e in events_ if e.kind == "error"]
            ctx.check(f"one error event, got {errors}", len(errors) == 1)
            ctx.check(f"mentions logging in, got {errors[0].data['message']!r}",
                      "log in" in errors[0].data["message"])
            ctx.check("turn_done reason error", events_[-1].data["reason"] == "error")
            session.close_cc()
    finally:
        set_config_value("providers", providers_before)


@test
def test_claude_binary_missing_gives_a_precise_error(ctx: Ctx):
    from halo_harness.providers.enablement import enable
    from halo_harness.theme import get_config_value, set_config_value
    providers_before = get_config_value("providers", default=None)  # finding 10: see the previous test's own note
    enable("claude_subscription")  # bypass auto-detection -- see this test's own module-level note
    saved = os.environ.get("BRIDGE_CLAUDE_EXE")
    missing = Path(tempfile.gettempdir()) / "definitely-not-claude-xyz.exe"
    os.environ["BRIDGE_CLAUDE_EXE"] = '"' + str(missing) + '"'
    try:
        session, _ = _new_cc_session()
        events_ = list(session.turn("reply with the single word pong"))
        errors = [e for e in events_ if e.kind == "error"]
        ctx.check(f"one error event, got {errors}", len(errors) == 1)
        ctx.check(f"mentions installing, got {errors[0].data['message']!r}", "install" in errors[0].data["message"].lower())
        session.close_cc()
    finally:
        if saved is None:
            os.environ.pop("BRIDGE_CLAUDE_EXE", None)
        else:
            os.environ["BRIDGE_CLAUDE_EXE"] = saved
        set_config_value("providers", providers_before)


# ---- --tools "" argv shape / disallowed-tools fallback ----------------------

@test
def test_argv_uses_empty_tools_flag_and_strict_mcp_config(ctx: Ctx):
    from halo_harness.agent.cc_process import build_cc_argv, build_mcp_config
    argv = build_cc_argv(model="claude-fable-5-1", session_id="11111111-1111-1111-1111-111111111111",
                          resume=False, mcp_config=build_mcp_config({}))
    ctx.check("--tools present", "--tools" in argv)
    idx = argv.index("--tools")
    ctx.check(f"--tools value is empty string, got {argv[idx+1]!r}", argv[idx + 1] == "")
    ctx.check("--strict-mcp-config present", "--strict-mcp-config" in argv)
    ctx.check("--permission-mode bypassPermissions", "bypassPermissions" in argv)
    ctx.check("--session-id present (not resuming)", "--session-id" in argv and "--resume" not in argv)


@test
def test_argv_resume_uses_resume_flag_not_session_id(ctx: Ctx):
    from halo_harness.agent.cc_process import build_cc_argv, build_mcp_config
    argv = build_cc_argv(model="claude-fable-5-1", session_id="11111111-1111-1111-1111-111111111111",
                          resume=True, mcp_config=build_mcp_config({}))
    ctx.check("--resume present", "--resume" in argv)
    ctx.check("--session-id absent when resuming", "--session-id" not in argv)


@test
def test_disallowed_tools_fallback_swaps_tools_flag(ctx: Ctx):
    from halo_harness.agent.cc_process import build_cc_argv, build_mcp_config, cc_disallowed_tools_fallback_argv
    argv = build_cc_argv(model="claude-fable-5-1", session_id="11111111-1111-1111-1111-111111111111",
                          resume=False, mcp_config=build_mcp_config({}))
    fallback = cc_disallowed_tools_fallback_argv(argv, ["Bash", "Read", "Edit"])
    ctx.check("--tools removed", "--tools" not in fallback)
    ctx.check("--disallowedTools present", "--disallowedTools" in fallback)
    idx = fallback.index("--disallowedTools")
    ctx.check(f"comma list of names, got {fallback[idx+1]!r}", fallback[idx + 1] == "Bash,Read,Edit")


# ---- H11b critical finding 2: child env never carries provider secrets
# or an outer Claude Code session's own identity ----------------------------

@test
def test_cc_child_env_strips_provider_secrets_and_outer_claude_vars(ctx: Ctx):
    saved = {k: os.environ.get(k) for k in
             ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDECODE",
              "CLAUDE_CODE_SESSION_ID", "CLAUDE_EFFORT")}
    os.environ["OPENROUTER_API_KEY"] = "sk-sentinel-openrouter"
    os.environ["ANTHROPIC_API_KEY"] = "sk-sentinel-anthropic"
    os.environ["ANTHROPIC_BASE_URL"] = "https://sentinel.invalid"
    os.environ["CLAUDECODE"] = "1"
    os.environ["CLAUDE_CODE_SESSION_ID"] = "outer-sentinel-session"
    os.environ["CLAUDE_EFFORT"] = "sentinel-effort"
    try:
        with _fake_claude_env():
            session, _ = _new_cc_session()
            events_ = list(session.turn("ENVDUMP"))
            text = "".join(e.data.get("text", "") for e in events_ if e.kind == "text_delta")
            ctx.check(f"ENVDUMP reply parses, got {text!r}", text.startswith("ENV:"))
            seen = json.loads(text[len("ENV:"):])
            ctx.check(f"no sentinel keys reached the fake claude process, got {seen}", seen == {})
            session.close_cc()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---- H11b finding 3: bridged dispatch IS the loop's dispatch --------------

@test
def test_enter_plan_mode_switches_permission_mode_and_logs_note(ctx: Ctx):
    from halo_harness.permissions import PermissionEngine
    with _fake_claude_env():
        engine = PermissionEngine(mode="default", cwd=Path(tempfile.mkdtemp(prefix="cc-plan-")))
        session, _ = _new_cc_session(permission_engine=engine)
        events_ = list(session.turn("TOOL:EnterPlanMode:" + json.dumps({})))
        ctx.check("permission_engine.mode is now plan", session.permission_engine.mode == "plan")
        results = [e for e in events_ if e.kind == "tool_result"]
        ctx.check(f"EnterPlanMode succeeded, got {results}", results and results[0].data["ok"] is True)
        session.close_cc()


@test
def test_exit_plan_mode_interactive_shows_plan_review_and_approves(ctx: Ctx):
    from halo_harness.permissions import PermissionEngine
    with _fake_claude_env():
        engine = PermissionEngine(mode="plan", cwd=Path(tempfile.mkdtemp(prefix="cc-exitplan-")))
        session, _ = _new_cc_session(permission_engine=engine, interactive=True)
        collected = []
        t = threading.Thread(target=lambda: collected.extend(
            session.turn("TOOL:ExitPlanMode:" + json.dumps({"plan": "Do the thing."}))))
        t.start()
        review = _wait_for_event_kind(collected, "plan_review")
        ctx.check("plan_review card shown", review is not None and review.data.get("plan") == "Do the thing.")
        ok = session.resolve_plan({"approved": True, "feedback": "", "mode_after": "acceptEdits"}) if review else False
        ctx.check("resolve_plan accepted", ok)
        t.join(timeout=10)
        ctx.check("mode switched to acceptEdits after approval", session.permission_engine.mode == "acceptEdits")
        session.close_cc()


@test
def test_ask_user_question_shows_card_and_answer_becomes_tool_result(ctx: Ctx):
    with _fake_claude_env():
        session, _ = _new_cc_session(interactive=True)
        question_input = {"questions": [{"question": "pick one", "options": [{"label": "a"}, {"label": "b"}]}]}
        collected = []
        t = threading.Thread(target=lambda: collected.extend(
            session.turn("TOOL:AskUserQuestion:" + json.dumps(question_input))))
        t.start()
        q = _wait_for_event_kind(collected, "question")
        ctx.check("question card shown", q is not None)
        ok = session.resolve_question(q.data["id"], "a") if q else False
        ctx.check("resolve_question accepted", ok)
        t.join(timeout=10)
        results = [e for e in collected if e.kind == "tool_result"]
        ctx.check(f"answer became the tool result, got {results}",
                   results and results[0].data["ok"] is True and "a" in results[0].data.get("content", ""))
        session.close_cc()


@test
def test_ask_user_question_for_a_cc_subagent_uses_the_namespaced_request_id(ctx: Ctx):
    """Release review finding 22: for a `cc:` SUB-AGENT (agent_id set),
    `_resolve_tool_call` parks the question under `question_request_id`
    (`f"{agent_id}:{tool_id}"`, namespaced -- same reasoning as the
    `ask_request_id` permission branch right above it in cc_runtime.py) and
    registers the waiter dict under THAT key. Before the fix, cc_runtime.py
    emitted and awaited the bare tool_use_id instead, so the waiter it
    looked up was never the one actually registered: `resolve_question`
    with the id the card itself reported would silently fail (no such
    waiter), and the tool returned "did not answer" on its own, with no
    way for the answer to ever reach it."""
    with _fake_claude_env():
        session, _ = _new_cc_session(interactive=True)
        session.agent_id = "child-1"  # makes this a cc: SUB-agent, not top-level
        question_input = {"questions": [{"question": "pick one", "options": [{"label": "a"}, {"label": "b"}]}]}
        collected = []
        t = threading.Thread(target=lambda: collected.extend(
            session.turn("TOOL:AskUserQuestion:" + json.dumps(question_input))))
        t.start()
        q = _wait_for_event_kind(collected, "question")
        ctx.check("question card shown", q is not None)
        ctx.check(f"the event's own id is namespaced with the agent_id, got {q.data.get('id') if q else None}",
                   q is not None and q.data.get("id", "").startswith("child-1:"))
        ok = session.resolve_question(q.data["id"], "a") if q else False
        ctx.check("resolve_question finds the waiter registered under that SAME namespaced id", ok)
        t.join(timeout=10)
        results = [e for e in collected if e.kind == "tool_result"]
        ctx.check(f"the real answer reached the tool result (not 'did not answer'), got {results}",
                   results and results[0].data["ok"] is True and "a" in results[0].data.get("content", ""))
        session.close_cc()


@test
def test_always_allow_rule_persists_via_apply_permission_decision(ctx: Ctx):
    """finding 17: the bridge reuses `_apply_permission_decision` -- an
    "always allow" answer must add a session rule, same as every other
    route, not just resolve the one pending call."""
    from halo_harness.permissions import Decision, PermissionEngine
    with _fake_claude_env():
        engine = PermissionEngine(mode="default", cwd=Path(tempfile.mkdtemp(prefix="cc-alwaysallow-")))
        session, proj = _new_cc_session(permission_engine=engine, interactive=True)
        target = proj / "f.txt"
        target.write_text("hi\n", encoding="utf-8")
        collected = []
        t = threading.Thread(target=lambda: collected.extend(
            session.turn("TOOL:Read:" + json.dumps({"file_path": str(target)}))))
        t.start()
        req = _wait_for_event_kind(collected, "permission_request")
        ctx.check("permission_request shown", req is not None)
        decision = Decision("allow", "test allow", rule="Read(./**)")
        ok = session.resolve_permission(req.data["id"], decision) if req else False
        ctx.check("resolve_permission accepted", ok)
        t.join(timeout=10)
        ctx.check("the always-allow rule was actually added to the engine",
                   any("Read" in r.raw for r in session.permission_engine.allow_rules))
        session.close_cc()


@test
def test_agent_tool_streams_subagent_events_live(ctx: Ctx):
    """finding 13: Agent/Task goes through the streaming path (on_event=
    _emit), so subagent_start/child events reach the parent's own stream
    instead of only the discarded return-value list."""
    from halo_harness.config.agents_md import _builtin_specs
    with _fake_claude_env():
        session, _ = _new_cc_session(agents=_builtin_specs())
        agent_input = {"description": "say pong", "prompt": "reply with the single word pong",
                        "subagent_type": "general-purpose"}
        events_ = list(session.turn("TOOL:Agent:" + json.dumps(agent_input)))
        starts = [e for e in events_ if e.kind == "subagent_start"]
        ctx.check(f"subagent_start reached the parent's stream, got {len(starts)}", len(starts) == 1)
        results = [e for e in events_ if e.kind == "tool_result"]
        ctx.check(f"agent tool result contains pong, got {results}",
                   bool(results) and "pong" in results[0].data.get("content", ""))
        session.close_cc()


@test
def test_agent_child_permission_ask_reaches_parent_waiters(ctx: Ctx):
    """finding 13: a foreground cc: child's own "ask" reaches the SAME
    live, answerable permission_request the parent's own calls use
    (agent-tagged), instead of being auto-denied with no UI attached."""
    from halo_harness.config.agents_md import _builtin_specs
    from halo_harness.permissions import Decision, PermissionEngine
    with _fake_claude_env():
        engine = PermissionEngine(mode="default", cwd=Path(tempfile.mkdtemp(prefix="cc-agent-ask-")))
        session, proj = _new_cc_session(permission_engine=engine, interactive=True, agents=_builtin_specs())
        newfile = proj / "child-new.txt"
        write_args = json.dumps({"file_path": str(newfile), "content": "hi\n"})
        agent_input = {"description": "write a file", "prompt": f"TOOL:Write:{write_args}",
                        "subagent_type": "general-purpose"}
        collected = []
        t = threading.Thread(target=lambda: collected.extend(session.turn("TOOL:Agent:" + json.dumps(agent_input))))
        t.start()
        req = _wait_for_event_kind(collected, "permission_request", timeout=15.0)
        ctx.check("a LIVE, agent-tagged permission_request reached the parent",
                   req is not None and bool(getattr(req, "agent_id", None)))
        ok = session.resolve_permission(req.data["id"], Decision("allow", "test allow")) if req else False
        ctx.check("resolve_permission (shared waiter dict) accepted it", ok)
        t.join(timeout=15)
        ctx.check("the child's Write actually ran after allow", newfile.exists())
        session.close_cc()


# ---- H11b finding 4: images, hook context and notices reach claude too ----

_TINY_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY"
                  "42YAAAAASUVORK5CYII=")


@test
def test_pasted_image_reaches_claude_in_the_stream_json_message(ctx: Ctx):
    with _fake_claude_env():
        session, _ = _new_cc_session()
        image_block = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                      "data": _TINY_PNG_B64}}
        events_ = list(session.turn("REPORT_IMAGE", images=[image_block]))
        text = "".join(e.data.get("text", "") for e in events_ if e.kind == "text_delta")
        ctx.check(f"claude saw the real image block, got {text!r}", text == "SAW_IMAGE:image/png")
        session.close_cc()


@test
def test_bridged_tool_image_result_reaches_claude_as_real_mcp_image(ctx: Ctx):
    """finding 7 "MCP image content out": a Read of a real PNG comes back
    as a genuine image block, converted to MCP ImageContent -- never
    flattened to the text "[image block]"."""
    with _fake_claude_env():
        session, proj = _new_cc_session()
        png_path = proj / "tiny.png"
        import base64
        png_path.write_bytes(base64.b64decode(_TINY_PNG_B64))
        events_ = list(session.turn("TOOL:Read:" + json.dumps({"file_path": str(png_path)})))
        results = [e for e in events_ if e.kind == "tool_result"]
        ctx.check(f"Read succeeded, got {results}", results and results[0].data["ok"] is True)
        text = "".join(e.data.get("text", "") for e in events_ if e.kind == "text_delta")
        ctx.check(f"claude's own reply shows a real MCP image (never a flattened '[image block]'), "
                    f"got {text!r}", "[image:image/png:" in text)
        session.close_cc()


@test
def test_background_job_notice_is_sent_to_claude_not_just_logged(ctx: Ctx):
    with _fake_claude_env():
        session, _ = _new_cc_session()
        session._pending_job_notices.append("Background job finished: echo hi -> hi")
        events_ = list(session.turn("ENVDUMP"))
        # the notice is sent as its OWN context line ahead of the real
        # turn -- claude echoes every line it consumes (isReplay), so the
        # notice text reaching claude shows up as its own logged "steer"-
        # shaped context, never just sitting in session.log alone.
        user_texts = [n for n in session.log.nodes() if n.get("type") == "user"]
        ctx.check("the notice was logged as its own user-role node (by the applier, as before)",
                   any(n.get("kind") == "job_notice" for n in user_texts))
        ctx.check("no notice left stranded across the turn", session._pending_job_notices == [])
        session.close_cc()


@test
def test_user_prompt_submit_hook_context_reaches_claude(ctx: Ctx):
    from halo_harness.hooks import HookDef, HookRunner
    proj = Path(tempfile.mkdtemp(prefix="cc-hookctx-proj-"))
    # PYTHONPATH=REPO_DIR: `-m tests.helpers.hook_scripts` must resolve
    # that module regardless of this process's own ambient PYTHONPATH
    # (cwd=proj, a scratch dir, has no such module on its own -- WSL/Kali
    # runs (tests/run_all.py, no shell-exported PYTHONPATH) surfaced this;
    # test_hooks_fire_exactly_once_pre_and_post_tool_use already does the
    # same for exactly this reason).
    hook_env = dict(os.environ, PYTHONPATH=str(REPO_DIR))
    hook_runner = HookRunner(
        {"UserPromptSubmit": [HookDef(type="command", matcher="",
                                        args=_HOOK_SCRIPT_ARGV + ["plain_context"])]},
        cwd=proj, session_id="hook-ctx", transcript_path=str(proj / "t.jsonl"), effective_env=hook_env,
    )
    with _fake_claude_env():
        session, _ = _new_cc_session(cwd=proj, hook_runner=hook_runner)
        events_ = list(session.turn("ENVDUMP"))
        snapshots = [n for n in session.log.nodes() if n.get("type") == "snapshot" and n.get("kind") == "hook_context"]
        ctx.check("hook context was logged", len(snapshots) == 1)
        session.close_cc()


# ---- critical finding 1: --replay-user-messages steering accounting -------

@test
def test_steer_between_tool_calls_is_absorbed_into_one_result(ctx: Ctx):
    """A steer sent WHILE a multi-tool-call turn is still running is
    folded into that SAME turn -- one result for both, never a hang
    waiting for a second result that was never coming (this is exactly
    critical finding 1's own verified-live repro)."""
    with _fake_claude_env():
        session, proj = _new_cc_session()
        f1, f2 = proj / "f1.txt", proj / "f2.txt"
        f1.write_text("a\n", encoding="utf-8")
        f2.write_text("b\n", encoding="utf-8")
        prompt = "TOOL2:Read:" + json.dumps({"file_path": str(f1)}) + "|Read:" + json.dumps({"file_path": str(f2)})
        collected = []
        t = threading.Thread(target=lambda: collected.extend(session.turn(prompt)))
        t.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and getattr(session, "_cc_state", None) is None:
            time.sleep(0.02)
        ok = session.steer("reply with the single word pong")
        ctx.check("steer accepted while busy", ok)
        _join_bounded_by_progress(t, session)
        ctx.check("turn ended normally (never hung)", collected and collected[-1].kind == "turn_done"
                   and collected[-1].data["reason"] == "end_turn")
        usage_nodes = [n for n in session.log.nodes() if n.get("type") == "usage"]
        ctx.check(f"exactly one usage node (absorbed into one result), got {len(usage_nodes)}", len(usage_nodes) == 1)
        steer_nodes = [n for n in session.log.nodes() if n.get("type") == "user" and n.get("kind") == "steer"]
        ctx.check(f"the steer is logged only once CONSUMED, got {len(steer_nodes)}", len(steer_nodes) == 1)
        session.close_cc()


@test
def test_two_steers_behind_a_running_turn_both_get_answered(ctx: Ctx):
    """Two steers sent while a plain (non-multi-call) turn is busy are
    never absorbed mid-flight -- they queue and are answered by a
    follow-up round (or rounds); the ORIGINAL rolo turn correctly waits
    for ALL of them, never ending at the first (unrelated) result."""
    with _fake_claude_env():
        session, _ = _new_cc_session()
        collected = []
        t = threading.Thread(target=lambda: collected.extend(session.turn("SLEEP:2 first")))
        t.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and getattr(session, "_cc_state", None) is None:
            time.sleep(0.02)
        ctx.check("steer 1 accepted", session.steer("reply with the single word pong"))
        ctx.check("steer 2 accepted", session.steer("also say the word banana"))
        _join_bounded_by_progress(t, session)
        ctx.check("turn ended normally (never hung waiting on a result that wasn't coming)",
                   collected and collected[-1].kind == "turn_done" and collected[-1].data["reason"] == "end_turn")
        usage_nodes = [n for n in session.log.nodes() if n.get("type") == "usage"]
        ctx.check(f"more than one result (original + follow-up), got {len(usage_nodes)}", len(usage_nodes) >= 2)
        steer_nodes = [n for n in session.log.nodes() if n.get("type") == "user" and n.get("kind") == "steer"]
        ctx.check(f"both steers logged as consumed, got {len(steer_nodes)}", len(steer_nodes) == 2)
        session.close_cc()


# ---- H11b finding 6: MCP catalog growth (list_changed) ---------------------

@test
def test_tool_search_load_sends_list_changed_and_new_tool_becomes_callable(ctx: Ctx):
    from halo_harness.agent.catalog import SessionCatalog, select_preload
    from halo_harness.mcp.manager import McpManager, McpServerConfig
    from halo_harness.tools.registry import ToolRegistry

    cfg = McpServerConfig(name="fake", type="stdio", command=sys.executable,
                           args=["-m", "tests.helpers.fake_mcp_server"], env={}, cwd=str(REPO_DIR))
    mgr = McpManager({"fake": cfg}, tool_env=dict(os.environ), cwd=REPO_DIR)
    mgr.start_all()
    try:
        triples = mgr.all_tools()
        core = ToolRegistry()
        preload, deferred = select_preload(triples, cap_budget=0)  # defer EVERYTHING from "fake"
        catalog = SessionCatalog(registry=core, deferred=deferred, manager=mgr, cap=128, names=core.names())
        with _fake_claude_env():
            session, _ = _new_cc_session(session_catalog=catalog, mcp_manager=mgr)
            # `_new_cc_session`'s own SessionContext builds its OWN real
            # ToolRegistry (built-ins) -- `catalog.load()` must mutate
            # THAT SAME registry (what `bridge_list_tools`/`tool_registry.
            # dispatch()` actually read from), not the placeholder `core`
            # this catalog was seeded with before the session existed.
            catalog.registry = session.tool_registry
            ctx.check("mcp__fake__echo starts deferred (not in the frozen catalog)",
                       "mcp__fake__echo" not in bridge_list_tools_names(session))
            # round 1: ToolSearch loads it -- bumps the bridge's own
            # generation (Session._on_catalog_grow -> notify_catalog_
            # changed), which the fake's OWN background watcher (running
            # for the whole process, not just one round) picks up.
            list(session.turn("TOOL:ToolSearch:" + json.dumps({"query": "select:mcp__fake__echo"})))
            ctx.check("mcp__fake__echo is now in the frozen catalog too",
                       "mcp__fake__echo" in bridge_list_tools_names(session))
            # round 2: claude (the fake) re-lists once notified and can
            # now name + call the tool it could not see a moment ago.
            events_ = list(session.turn("WAITFOR:mcp__fake__echo:" + json.dumps({"text": "hi"})))
            text = "".join(e.data.get("text", "") for e in events_ if e.kind == "text_delta")
            ctx.check(f"claude was notified AND the tool became callable, got {text!r}",
                       "changed=True" in text and "present=True" in text and "result=hi" in text)
            session.close_cc()
    finally:
        mgr.close_all()


def bridge_list_tools_names(session) -> set:
    from halo_harness.agent.cc_runtime import bridge_list_tools
    return {t["name"] for t in bridge_list_tools(session)}


# ---- H11b findings 8/9/10: session identity -------------------------------

@test
def test_cc_session_id_logged_as_meta_node(ctx: Ctx):
    with _fake_claude_env():
        session, _ = _new_cc_session()
        list(session.turn("reply with the single word pong"))
        metas = [n for n in session.log.nodes() if n.get("type") == "meta" and n.get("cc_session_id")]
        ctx.check(f"a meta node logged the cc session id, got {metas}", len(metas) == 1)
        ctx.check("it matches the live process's own id", metas[0]["cc_session_id"] == session._cc_state.cc_session_id)
        session.close_cc()


@test
def test_clear_closes_cc_and_next_turn_starts_a_fresh_conversation(ctx: Ctx):
    with _fake_claude_env():
        session, _ = _new_cc_session()
        list(session.turn("reply with the single word pong"))
        old_pid = session._cc_state.process.pid
        session.clear()
        ctx.check("_cc_state dropped by /clear", getattr(session, "_cc_state", None) is None)
        events_ = list(session.turn("reply with the single word pong"))
        ctx.check("still works after /clear", "pong" in "".join(
            e.data.get("text", "") for e in events_ if e.kind == "text_delta"))
        new_pid = session._cc_state.process.pid
        ctx.check(f"a genuinely new process, {old_pid} -> {new_pid}", old_pid != new_pid)
        argv = session._cc_state.process.argv
        ctx.check("started fresh (--session-id, not --resume) since the log has no prior cc_session_id",
                   "--session-id" in argv and "--resume" not in argv)
        session.close_cc()


@test
def test_fork_session_closes_cc_and_restarts_with_fork_session_flag(ctx: Ctx):
    from halo_harness.controller import Controller
    with _fake_claude_env():
        session, proj = _new_cc_session()
        controller = Controller(session=session, cwd=proj)
        list(session.turn("reply with the single word pong"))
        old_pid = session._cc_state.process.pid
        old_cc_id = session._cc_state.cc_session_id
        controller.fork_session()
        ctx.check("_cc_state dropped by fork", getattr(session, "_cc_state", None) is None)
        list(session.turn("reply with the single word pong"))
        argv = session._cc_state.process.argv
        ctx.check(f"restarted with --resume <old id> --fork-session, got {argv}",
                   "--resume" in argv and old_cc_id in argv and "--fork-session" in argv)
        ctx.check("a genuinely new process", session._cc_state.process.pid != old_pid)
        session.close_cc()


@test
def test_resume_failure_falls_back_to_a_fresh_session(ctx: Ctx):
    """finding 9: a --resume that claude answers "No conversation found"
    for (verified live wording) falls back to a fresh --session-id,
    primed with the prior log, so the CURRENT turn still succeeds."""
    registry_path = str(Path(tempfile.mkdtemp(prefix="cc-registry-")) / "ids.json")
    saved = os.environ.get("FAKE_CLAUDE_CC_REGISTRY")
    os.environ["FAKE_CLAUDE_CC_REGISTRY"] = registry_path
    try:
        with _fake_claude_env():
            session, _ = _new_cc_session()
            # Log a cc_session_id meta node for an id the fake's registry
            # has never seen (simulating a pruned/never-created conversation)
            # WITHOUT ever actually starting a process for it.
            session.log.append_meta(cc_session_id="11111111-1111-1111-1111-111111111111", cc_resumed=False)
            events_ = list(session.turn("reply with the single word pong"))
            text = "".join(e.data.get("text", "") for e in events_ if e.kind == "text_delta")
            ctx.check(f"the turn still succeeded via a fresh session, got {text!r}", text == "pong")
            ctx.check("no error surfaced to the user", not any(e.kind == "error" for e in events_))
            session.close_cc()
    finally:
        if saved is None:
            os.environ.pop("FAKE_CLAUDE_CC_REGISTRY", None)
        else:
            os.environ["FAKE_CLAUDE_CC_REGISTRY"] = saved


@test
def test_model_switch_cc_to_cc_restarts_with_resume_and_new_model(ctx: Ctx):
    from halo_harness.model import parse_model_ref, resolve_model_profile
    with _fake_claude_env():
        session, _ = _new_cc_session(model="cc:fable")
        list(session.turn("reply with the single word pong"))
        old_pid = session._cc_state.process.pid
        old_cc_id = session._cc_state.cc_session_id
        new_ref = parse_model_ref("cc:sonnet-5")
        new_profile = resolve_model_profile(new_ref, Path(tempfile.mkdtemp(prefix="cc-switch-state-")), {})
        session.set_model(new_ref, new_profile)
        ctx.check("_cc_state closed on a cc:->cc: model change", getattr(session, "_cc_state", None) is None)
        list(session.turn("reply with the single word pong"))
        ctx.check("restarted (new pid)", session._cc_state.process.pid != old_pid)
        argv = session._cc_state.process.argv
        ctx.check(f"resumed the SAME conversation under the new model, got {argv}",
                   "--resume" in argv and old_cc_id in argv and "claude-sonnet-5" in argv)
        session.close_cc()


# ---- H11b findings 11/12: accounting and errors ----------------------------

@test
def test_cost_logged_as_per_turn_delta_not_the_raw_cumulative_total(ctx: Ctx):
    with _fake_claude_env():
        session, _ = _new_cc_session()
        list(session.turn("reply with the single word pong"))
        list(session.turn("reply with the single word pong"))
        list(session.turn("reply with the single word pong"))
        usage_nodes = [n for n in session.log.nodes() if n.get("type") == "usage"]
        ctx.check(f"three usage nodes, got {len(usage_nodes)}", len(usage_nodes) == 3)
        total_logged = sum(n["cost_usd"] for n in usage_nodes)
        real_cumulative = session._cc_state.last_total_cost
        ctx.check(f"the SUM of logged per-turn deltas matches the real cumulative total "
                    f"(never double-counted), got sum={total_logged} real={real_cumulative}",
                   abs(total_logged - real_cumulative) < 1e-9)
        ctx.check("meter total matches too", abs(session.cost_meter.total_usd - real_cumulative) < 1e-9)
        session.close_cc()


@test
def test_error_result_becomes_a_visible_error_event(ctx: Ctx):
    with _fake_claude_env():
        session, _ = _new_cc_session()
        events_ = list(session.turn("ERROR:overloaded_error"))
        errors = [e for e in events_ if e.kind == "error"]
        ctx.check(f"one error event, got {errors}", len(errors) == 1)
        ctx.check("turn_done reason is error, not end_turn", events_[-1].data["reason"] == "error")
        usage_nodes = [n for n in session.log.nodes() if n.get("type") == "usage"]
        ctx.check(f"the usage node is marked status=error, got {usage_nodes}",
                   usage_nodes and usage_nodes[-1].get("status") == "error")
        session.close_cc()


@test
def test_stop_hook_fires_on_result(ctx: Ctx):
    from halo_harness.hooks import HookDef, HookRunner
    proj = Path(tempfile.mkdtemp(prefix="cc-stophook-proj-"))
    counter = proj / "stop.count"
    # PYTHONPATH=REPO_DIR: see test_user_prompt_submit_hook_context_reaches_
    # claude's own comment -- a WSL/Kali run_all.py process has no shell-
    # exported PYTHONPATH, so `-m tests.helpers.hook_scripts` (cwd=proj, a
    # scratch dir) can't resolve that module without this.
    hook_env = dict(os.environ, PYTHONPATH=str(REPO_DIR), HOOK_ONCE_COUNTER_FILE=str(counter))
    hook_runner = HookRunner(
        {"Stop": [HookDef(type="command", matcher="", args=_HOOK_SCRIPT_ARGV + ["once_counter"])]},
        cwd=proj, session_id="hook-stop", transcript_path=str(proj / "t.jsonl"),
        effective_env=hook_env,
    )
    with _fake_claude_env():
        session, _ = _new_cc_session(cwd=proj, hook_runner=hook_runner)
        list(session.turn("reply with the single word pong"))
        session.close_cc()
    count = len([ln for ln in counter.read_text(encoding="utf-8").splitlines() if ln.strip()]) if counter.exists() else 0
    ctx.check(f"Stop hook fired exactly once on the result, got count={count}", count == 1)


# ---- H11b finding 14: lifecycle --------------------------------------------

@test
def test_agent_call_closes_the_childs_own_cc_state(ctx: Ctx):
    from halo_harness.config.agents_md import _builtin_specs
    with _fake_claude_env():
        session, _ = _new_cc_session(agents=_builtin_specs())
        agent_input = {"description": "say pong", "prompt": "reply with the single word pong",
                        "subagent_type": "general-purpose"}
        list(session.turn("TOOL:Agent:" + json.dumps(agent_input)))
        ctx.check("no live children left registered after the call ends",
                   session.agent_runtime.live_children == {})
        session.close_cc()


# ---- brief C / finding 28: pgrep + SIGHUP (POSIX only) ---------------------

def _pgrep_count(pattern: str) -> int:
    import subprocess
    proc = subprocess.run(["pgrep", "-fc", pattern], capture_output=True, text=True)
    try:
        return int((proc.stdout or "0").strip() or 0)
    except ValueError:
        return 0


def _require_pgrep() -> None:
    import shutil
    if sys.platform == "win32" or not hasattr(__import__("signal"), "SIGHUP"):
        raise SkipTest("pgrep/SIGHUP are POSIX-only")
    if shutil.which("pgrep") is None:
        raise SkipTest("pgrep not available on this box")


@test
def test_posix_close_cc_leaves_no_claude_or_bridge_process(ctx: Ctx):
    """The fake claude's own argv carries `--model <marker>`, so `pgrep -f`
    finds exactly THIS session's subprocess and nothing another test left
    behind; the ccbridge child has no marker, so it's a before/after delta."""
    _require_pgrep()
    import uuid
    marker = f"cc-pgrep-marker-{uuid.uuid4().hex[:10]}"
    bridge_before = _pgrep_count("-m halo_harness.ccbridge")
    with _fake_claude_env():
        session, _ = _new_cc_session(model=f"cc:{marker}")
        list(session.turn("reply with the single word pong"))
        ctx.check("the fake claude is running mid-session", _pgrep_count(f"fake_claude_cc.py -p --model {marker}") == 1)
        run_dir = session._cc_state.bridge.socket_path.parent
        session.close_cc()
    # W5b Linux determinism (carried from W3b): an 8s bound is plenty once
    # the process/filesystem caches are warm, but a FIRST run in a fresh
    # interpreter on Linux (the very scenario a standalone three-run
    # verification exercises) saw the socket-file poll below still give up
    # at 8.88s with the file not yet gone -- a genuine cold-start cost
    # (first subprocess teardown, first unlink of this kind), not a wrong
    # mechanism. All three bounded polls here get the same, longer bound
    # rather than special-casing just the one that was observed to lose the
    # race, since the other two are exactly as exposed to the same cold
    # start in principle.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and _pgrep_count(f"fake_claude_cc.py -p --model {marker}") > 0:
        time.sleep(0.2)
    ctx.check("no claude survivor after close_cc", _pgrep_count(f"fake_claude_cc.py -p --model {marker}") == 0)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and _pgrep_count("-m halo_harness.ccbridge") > bridge_before:
        time.sleep(0.2)
    ctx.check("no ccbridge child survivor after close_cc", _pgrep_count("-m halo_harness.ccbridge") <= bridge_before)
    # W3b test-determinism: the socket file is unlinked by the ccbridge
    # child's OWN cleanup as it exits, which can trail `pgrep` no longer
    # seeing that pid by a beat (the process table entry disappears before
    # its last filesystem cleanup is guaranteed to have landed, especially
    # under load) -- a bare, unpolled glob() right after the process-
    # absence checks above raced that gap. Bounded poll instead of a bare
    # check, same deadline style as the two process-absence waits above.
    deadline = time.monotonic() + 20
    sock_files = list(run_dir.glob("*.sock"))
    while time.monotonic() < deadline and sock_files:
        time.sleep(0.2)
        sock_files = list(run_dir.glob("*.sock"))
    ctx.check(f"the bridge socket file is gone, got {sock_files}", not sock_files)


@test
def test_posix_sighup_to_a_headless_cc_run_kills_claude_and_bridge(ctx: Ctx):
    """Brief B: "SIGHUP/SIGTERM kill it; no orphans (pgrep on Linux)" -- a
    real `halo -p` process on a cc: model (the fake, kept busy by a
    SLEEP prompt) gets SIGHUP; halo's own handler (cli.py) unwinds
    through `close_cc`, so nothing of the claude/ccbridge pair survives."""
    _require_pgrep()
    import signal
    import subprocess
    import uuid
    marker = f"cc-sighup-marker-{uuid.uuid4().hex[:10]}"
    home = Path(tempfile.mkdtemp(prefix="cc-sighup-home-"))
    env = dict(os.environ, PYTHONPATH=str(REPO_DIR), BRIDGE_TEST_HOME=str(home),
               BRIDGE_CLAUDE_EXE='"' + sys.executable + '" "' + str(FAKE_CLAUDE) + '"',
               FAKE_CLAUDE_CC_LOGGED_IN="1")
    env.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)
    bridge_before = _pgrep_count("-m halo_harness.ccbridge")
    proc = subprocess.Popen([sys.executable, "-m", "halo_harness", "--model", f"cc:{marker}", "-p", "SLEEP:30 nothing"],
                             cwd=str(REPO_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and _pgrep_count(f"fake_claude_cc.py -p --model {marker}") == 0:
            if proc.poll() is not None:
                break
            time.sleep(0.25)
        ctx.check("the fake claude started under the headless run",
                   _pgrep_count(f"fake_claude_cc.py -p --model {marker}") == 1)
        proc.send_signal(signal.SIGHUP)
        try:
            code = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = None
        ctx.check(f"halo exited on SIGHUP (129), got {code}", code == 129)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and _pgrep_count(f"fake_claude_cc.py -p --model {marker}") > 0:
            time.sleep(0.2)
        ctx.check("no claude survivor after SIGHUP", _pgrep_count(f"fake_claude_cc.py -p --model {marker}") == 0)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and _pgrep_count("-m halo_harness.ccbridge") > bridge_before:
            time.sleep(0.2)
        ctx.check("no ccbridge survivor after SIGHUP", _pgrep_count("-m halo_harness.ccbridge") <= bridge_before)
        ctx.check("no stale socket left in the run dir", not list((home / ".halo" / "run").glob("*.sock")))
    finally:
        subprocess.run(["pkill", "-f", f"fake_claude_cc.py -p --model {marker}"], capture_output=True)
        if proc.poll() is None:
            proc.kill()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
