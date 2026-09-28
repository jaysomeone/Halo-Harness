"""tests.test_cc_session -- H11 Part C: agent.loop.Session driving the
`cc:` route end to end against `tests/helpers/fake_claude_cc.py` (a real
`claude` stand-in: real stream-json protocol, a REAL MCP client spawning
the REAL `python -m rolo_claude.ccbridge` child against a REAL
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

test, TESTS = new_registry()

REPO_DIR = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = REPO_DIR / "tests" / "helpers" / "fake_claude_cc.py"
_HOOK_SCRIPT_ARGV = [sys.executable, "-m", "tests.helpers.hook_scripts"]


@contextmanager
def _fake_claude_env(*, logged_in: bool = True):
    saved = {k: os.environ.get(k) for k in
             ("BRIDGE_CLAUDE_EXE", "FAKE_CLAUDE_CC_LOGGED_IN", "BRIDGE_TEST_CC_AUTH_STATUS")}
    os.environ["BRIDGE_CLAUDE_EXE"] = '"' + sys.executable + '" "' + str(FAKE_CLAUDE) + '"'
    os.environ["FAKE_CLAUDE_CC_LOGGED_IN"] = "1" if logged_in else "0"
    os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)  # the fake's own real "auth status" answers this now
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _new_cc_session(*, permission_engine=None, hook_runner=None, cwd=None, model="cc:fable"):
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import parse_model_ref, resolve_model_profile
    from rolo_claude.permissions import PermissionEngine

    proj = cwd or Path(tempfile.mkdtemp(prefix="cc-sess-proj-"))
    proj.mkdir(parents=True, exist_ok=True)
    ref = parse_model_ref(model)
    profile = resolve_model_profile(ref, Path(tempfile.mkdtemp(prefix="cc-sess-state-")), {})
    session_ctx = SessionContext(cwd=proj, model_label=model)
    return Session(
        cwd=proj, model_ref=ref, model_profile=profile, creds=None,
        state_dir=Path(tempfile.mkdtemp(prefix="cc-sess-sdir-")), model_label=model, session_context=session_ctx,
        max_turns=8, permission_engine=permission_engine or PermissionEngine(mode="auto", cwd=proj),
        hook_runner=hook_runner,
    ), proj


def _wait_for_event_kind(collected, kind, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for e in collected:
            if e.kind == kind:
                return e
        time.sleep(0.02)
    return None


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
    from rolo_claude.agent.cc_runtime import bridge_list_tools
    with _fake_claude_env():
        session, _ = _new_cc_session()
        expected_names = sorted(t["name"] for t in session.tool_registry.definitions())
        got_names = sorted(t["name"] for t in bridge_list_tools(session))
        ctx.check(f"names match, expected={expected_names[:3]}..., got={got_names[:3]}...",
                   expected_names == got_names)
        session.close_cc()


@test
def test_tools_call_obeys_deny_rule(ctx: Ctx):
    from rolo_claude.permissions import PermissionEngine, parse_rule
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
    from rolo_claude.permissions import Decision, PermissionEngine
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
    from rolo_claude.hooks import HookDef, HookRunner

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
    from rolo_claude.agent.invariants import find_unpaired_tool_use_ids
    with _fake_claude_env():
        session, _ = _new_cc_session()
        collected = []
        t = threading.Thread(target=lambda: collected.extend(session.turn("SLEEP:5 nothing to see")))
        t.start()
        time.sleep(0.3)
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
        time.sleep(0.3)
        session.abort.set()
        t.join(timeout=10)
        old_pid = session._cc_state.process.pid

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
    from rolo_claude.agent.cc_process import cc_session_uuid
    import uuid as uuid_mod
    a = cc_session_uuid("abc123")
    b = cc_session_uuid("abc123")
    c = cc_session_uuid("different")
    ctx.check("same input -> same uuid", a == b)
    ctx.check("different input -> different uuid", a != c)
    ctx.check("valid uuid string", str(uuid_mod.UUID(a)) == a)


@test
def test_new_process_with_prior_cc_history_uses_resume_not_session_id(ctx: Ctx):
    """A brand new Session object (as a fresh `rolo-claude -r`/`--continue`
    process would build) wrapping a log that ALREADY has a prior cc: usage
    node must use --resume on its very first ensure_cc_state call, never
    --session-id (which claude rejects once that id already exists)."""
    from rolo_claude.agent.log import SessionLog
    from rolo_claude.agent.cc_runtime import ensure_cc_state
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
    from rolo_claude.model import ModelRef, ModelProfile
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
    with _fake_claude_env(logged_in=False):
        session, _ = _new_cc_session()
        events_ = list(session.turn("reply with the single word pong"))
        errors = [e for e in events_ if e.kind == "error"]
        ctx.check(f"one error event, got {errors}", len(errors) == 1)
        ctx.check(f"mentions logging in, got {errors[0].data['message']!r}", "log in" in errors[0].data["message"])
        ctx.check("turn_done reason error", events_[-1].data["reason"] == "error")
        session.close_cc()


@test
def test_claude_binary_missing_gives_a_precise_error(ctx: Ctx):
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


# ---- --tools "" argv shape / disallowed-tools fallback ----------------------

@test
def test_argv_uses_empty_tools_flag_and_strict_mcp_config(ctx: Ctx):
    from rolo_claude.agent.cc_process import build_cc_argv, build_mcp_config
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
    from rolo_claude.agent.cc_process import build_cc_argv, build_mcp_config
    argv = build_cc_argv(model="claude-fable-5-1", session_id="11111111-1111-1111-1111-111111111111",
                          resume=True, mcp_config=build_mcp_config({}))
    ctx.check("--resume present", "--resume" in argv)
    ctx.check("--session-id absent when resuming", "--session-id" not in argv)


@test
def test_disallowed_tools_fallback_swaps_tools_flag(ctx: Ctx):
    from rolo_claude.agent.cc_process import build_cc_argv, build_mcp_config, cc_disallowed_tools_fallback_argv
    argv = build_cc_argv(model="claude-fable-5-1", session_id="11111111-1111-1111-1111-111111111111",
                          resume=False, mcp_config=build_mcp_config({}))
    fallback = cc_disallowed_tools_fallback_argv(argv, ["Bash", "Read", "Edit"])
    ctx.check("--tools removed", "--tools" not in fallback)
    ctx.check("--disallowedTools present", "--disallowedTools" in fallback)
    idx = fallback.index("--disallowedTools")
    ctx.check(f"comma list of names, got {fallback[idx+1]!r}", fallback[idx + 1] == "Bash,Read,Edit")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
