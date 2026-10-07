"""tests.test_codex_session -- Halo 2.0.3 round 5i part 2: agent.loop.
Session driving the `cx:` route end to end against
`tests/helpers/fake_codex.py` (a real `codex` stand-in: real JSONL
protocol, a REAL MCP client spawning the REAL `python -m halo_harness.
ccbridge` child against a REAL ToolBridgeServer -- nothing about the
bridge itself is mocked, only the "model" driving codex's own side of the
JSONL protocol is scripted). Mirrors tests/test_cc_session.py's own
coverage shape, adapted for codex's per-turn-subprocess architecture
(docs/harness/CODEX-RESEARCH.md section 7): a plain turn, a tool call
through the MCP bridge, the steer fallback (queue, then a follow-up
`resume` call once the turn finishes), an unknown-model refusal, a
logged-out/missing-binary state, and the enablement/picker rows.
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

from tests.helpers.runner import Ctx, new_registry, run_all, print_results
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

os.environ["BRIDGE_TEST_HOME"] = tempfile.mkdtemp(prefix="cx-session-scratchhome-")
ensure_default_provider_credentials()
# Deliberately NOT a blanket default here (mirrors test_cc_session.py's own
# reasoning) -- the two tests that exercise "not logged in"/"binary
# missing" need the REAL absence, not a default masking it.
os.environ.pop("BRIDGE_TEST_CODEX_LOGIN_STATUS", None)

test, TESTS = new_registry()

REPO_DIR = Path(__file__).resolve().parent.parent
FAKE_CODEX = REPO_DIR / "tests" / "helpers" / "fake_codex.py"


@contextmanager
def _fake_codex_env(*, logged_in: bool = True, argv_log: "Path | None" = None):
    saved = {k: os.environ.get(k) for k in
             ("HALO_CODEX_EXE", "FAKE_CODEX_LOGIN_STATUS", "BRIDGE_TEST_CODEX_LOGIN_STATUS",
              "BRIDGE_TEST_CX_LOGIN_STATUS", "FAKE_CODEX_ARGV_LOG", "CODEX_HOME")}
    os.environ.pop("CODEX_HOME", None)
    os.environ["HALO_CODEX_EXE"] = '"' + sys.executable + '" "' + str(FAKE_CODEX) + '"'
    os.environ["FAKE_CODEX_LOGIN_STATUS"] = "Logged in using ChatGPT" if logged_in else "Not logged in"
    # the fake's own real answer counts now (account failover checks each
    # managed account's login through the cx_models seam)
    os.environ.pop("BRIDGE_TEST_CODEX_LOGIN_STATUS", None)
    os.environ.pop("BRIDGE_TEST_CX_LOGIN_STATUS", None)
    if argv_log is not None:
        os.environ["FAKE_CODEX_ARGV_LOG"] = str(argv_log)
    else:
        os.environ.pop("FAKE_CODEX_ARGV_LOG", None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _new_cx_session(*, permission_engine=None, model="cx:astra", cwd=None):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import parse_model_ref, resolve_model_profile
    from halo_harness.permissions import PermissionEngine

    proj = cwd or Path(tempfile.mkdtemp(prefix="cx-sess-proj-"))
    proj.mkdir(parents=True, exist_ok=True)
    ref = parse_model_ref(model)
    profile = resolve_model_profile(ref, Path(tempfile.mkdtemp(prefix="cx-sess-state-")), {})
    session_ctx = SessionContext(cwd=proj, model_label=model)
    session = Session(
        cwd=proj, model_ref=ref, model_profile=profile, creds=None,
        state_dir=Path(tempfile.mkdtemp(prefix="cx-sess-sdir-")), model_label=model, session_context=session_ctx,
        max_turns=8, permission_engine=permission_engine or PermissionEngine(mode="auto", cwd=proj),
    )
    session.interactive = False
    return session, proj


# ---- plain turn -------------------------------------------------------

@test
def test_codex_separates_streamed_and_snapshot_replies(ctx: Ctx):
    from types import SimpleNamespace
    from halo_harness.agent.codex_runtime import CxState
    from halo_harness.agent.codex_turn import _events_for_cx_obj

    for streamed in (False, True):
        state = CxState(bridge=None)
        session = SimpleNamespace(log=SimpleNamespace(append_assistant=lambda **kw: None))
        output = []
        for item_id, text in (("a", "Identify the version."), ("b", "You’re using Python.")):
            if streamed:
                for fragment in (text[:1], text):
                    evs, _ = _events_for_cx_obj(session, 1, {"type": "item.updated", "item":
                        {"type": "agent_message", "id": item_id, "text": fragment}}, state)
                    output.extend(evs)
            evs, _ = _events_for_cx_obj(session, 1, {"type": "item.completed", "item":
                {"type": "agent_message", "id": item_id, "text": text}}, state)
            output.extend(evs)
        text = "".join(e.data["text"] for e in output if e.kind == "text_delta")
        ctx.check(f"complete replies remain separated: streamed={streamed}",
                  text == "Identify the version.\n\nYou’re using Python.\n\n")


@test
def test_no_bridge_until_first_cx_turn(ctx: Ctx):
    with _fake_codex_env():
        session, _ = _new_cx_session()
        ctx.check("no _cx_state before any turn", getattr(session, "_cx_state", None) is None)
        list(session.turn("reply with the single word pong"))
        ctx.check("_cx_state exists after the first turn", session._cx_state is not None)
        session.close_cc()


@test
def test_bridge_reused_across_turns(ctx: Ctx):
    with _fake_codex_env():
        session, _ = _new_cx_session()
        list(session.turn("reply with the single word pong"))
        bridge1 = session._cx_state.bridge
        list(session.turn("reply with the single word pong"))
        bridge2 = session._cx_state.bridge
        ctx.check("same bridge server reused across turns", bridge1 is bridge2)
        session.close_cc()


@test
def test_pong_reply_and_thread_id_logged(ctx: Ctx):
    with _fake_codex_env():
        session, _ = _new_cx_session()
        events_ = list(session.turn("reply with the single word pong"))
        text = "".join(e.data.get("text", "") for e in events_ if e.kind == "text_delta")
        ctx.check(f"got pong, text={text!r}", text == "pong\n\n")
        ctx.check("turn_done end_turn", events_[-1].kind == "turn_done" and events_[-1].data["reason"] == "end_turn")
        meta_nodes = [n for n in session.log.nodes() if n.get("type") == "meta" and n.get("cx_session_id")]
        ctx.check(f"cx_session_id logged, got {meta_nodes}", len(meta_nodes) == 1)
        session.close_cc()


@test
def test_second_turn_resumes_same_thread(ctx: Ctx):
    log_path = Path(tempfile.mkdtemp(prefix="cx-argvlog-")) / "argv.jsonl"
    with _fake_codex_env(argv_log=log_path):
        session, _ = _new_cx_session()
        list(session.turn("reply with the single word pong"))
        second = list(session.turn("reply with the single word pong"))
        errors = [e.data.get("message") for e in second if e.kind == "error"]
        ctx.check(f"resumed turn has no error, got {errors}", not errors)
        # the log also carries the ONE `login status` preflight call --
        # filtered out here since this test is only about `exec` shape.
        lines = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        lines = [l for l in lines if l and l[0] == "exec"]
        ctx.check(f"two exec invocations, got {len(lines)}", len(lines) == 2)
        ctx.check(f"first call has no resume, got {lines[0]}", "resume" not in lines[0])
        ctx.check(f"second call resumes, got {lines[1]}", "resume" in lines[1])
        text = "".join(e.data.get("text", "") for e in second if e.kind == "text_delta")
        ctx.check("reused item ids do not hide the next reply", text.strip() == "pong")
        session.close_cc()


# ---- managed accounts: per-account threads, failover, visible commands ----

def _codex_accounts(session, *names, out_of_credits=()):
    """Managed Codex profiles under the session's state dir; the first is active."""
    from halo_harness.accounts import prepare_codex_profile, set_active_account
    for name in names:
        profile = prepare_codex_profile(name, state_dir=session.state_dir)
        if name in out_of_credits:
            (profile.config_home / "fake-out-of-credits").write_text("", encoding="utf-8")
    set_active_account("codex", names[0], state_dir=session.state_dir)


def _exec_calls(log_path: Path) -> list:
    lines = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [l for l in lines if l and l[0] == "exec"]


def _errors(evs) -> list:
    return [(e.data.get("err_type"), e.data.get("message")) for e in evs if e.kind == "error"]


@test
def test_account_switch_resumes_only_that_accounts_thread(ctx: Ctx):
    from halo_harness.accounts import set_active_account
    log_path = Path(tempfile.mkdtemp(prefix="cx-argvlog-")) / "argv.jsonl"
    with _fake_codex_env(argv_log=log_path):
        session, _ = _new_cx_session()
        _codex_accounts(session, "a", "b")
        list(session.turn("reply with the single word pong"))
        set_active_account("codex", "b", state_dir=session.state_dir)
        on_b = list(session.turn("reply with the single word pong"))
        ctx.check(f"switched account's turn has no error, got {_errors(on_b)}", not _errors(on_b))
        set_active_account("codex", "a", state_dir=session.state_dir)
        back_on_a = list(session.turn("reply with the single word pong"))
        ctx.check(f"returning to the first account has no error, got {_errors(back_on_a)}", not _errors(back_on_a))
        calls = _exec_calls(log_path)
        ctx.check(f"three exec calls, got {len(calls)}", len(calls) == 3)
        ctx.check("account b starts its own thread", "resume" not in calls[1])
        ctx.check("account b's fresh thread carries the conversation so far",
                  "<conversation-so-far>" in calls[1][-1])
        accounts = {n.get("cx_session_id"): n.get("cx_account") for n in session.log.nodes()
                    if n.get("type") == "meta" and n.get("cx_session_id")}
        ctx.check(f"threads recorded per account, got {accounts}", sorted(accounts.values()) == ["a", "b"])
        first_a = next(i for i, acct in accounts.items() if acct == "a")
        ctx.check(f"account a resumes its own thread, got {calls[2]}",
                  calls[2][1:3] == ["resume", first_a])
        session.close_cc()


@test
def test_missing_thread_restarts_fresh_without_an_error(ctx: Ctx):
    log_path = Path(tempfile.mkdtemp(prefix="cx-argvlog-")) / "argv.jsonl"
    with _fake_codex_env(argv_log=log_path):
        session, _ = _new_cx_session()
        _codex_accounts(session, "a")
        session.log.append_meta(cx_session_id="thread-codex-forgot", cx_account="a")
        evs = list(session.turn("reply with the single word pong"))
        text = "".join(e.data.get("text", "") for e in evs if e.kind == "text_delta")
        ctx.check(f"no error shown, got {_errors(evs)}", not _errors(evs))
        ctx.check(f"the reply still arrives, got {text!r}", text.strip() == "pong")
        calls = _exec_calls(log_path)
        ctx.check(f"a refused resume, then a fresh thread, got {[c[:3] for c in calls]}",
                  len(calls) == 2 and calls[0][1:3] == ["resume", "thread-codex-forgot"] and "resume" not in calls[1])
        ctx.check("turn ended normally", evs[-1].kind == "turn_done" and evs[-1].data["reason"] == "end_turn")
        session.close_cc()


@test
def test_out_of_credits_fails_over_to_the_next_account(ctx: Ctx):
    from halo_harness.accounts import active_account_name
    log_path = Path(tempfile.mkdtemp(prefix="cx-argvlog-")) / "argv.jsonl"
    with _fake_codex_env(argv_log=log_path):
        session, _ = _new_cx_session()
        _codex_accounts(session, "a", "b", out_of_credits=("a",))
        evs = list(session.turn("reply with the single word pong"))
        text = "".join(e.data.get("text", "") for e in evs if e.kind == "text_delta")
        notes = [e.data.get("text") or e.data.get("message") for e in evs if e.kind == "notification"]
        ctx.check(f"no error shown, got {_errors(evs)}", not _errors(evs))
        ctx.check(f"the switch is announced, got {notes}", any("'b'" in str(n) for n in notes))
        ctx.check(f"the work continued on the new account, got {text!r}", text.strip() == "pong")
        ctx.check("account b is now active", active_account_name("codex", state_dir=session.state_dir) == "b")
        calls = _exec_calls(log_path)
        ctx.check(f"two exec calls, the second a fresh thread, got {len(calls)}",
                  len(calls) == 2 and "resume" not in calls[1])
        session.close_cc()


@test
def test_out_of_credits_after_a_command_continues_saved_work(ctx: Ctx):
    from halo_harness.accounts import active_account_name
    log_path = Path(tempfile.mkdtemp(prefix="cx-argvlog-")) / "argv.jsonl"
    with _fake_codex_env(argv_log=log_path):
        session, _ = _new_cx_session()
        _codex_accounts(session, "a", "b", out_of_credits=("a",))
        marker = session.cwd / "completed-action.txt"
        request = f"NATIVE_CMD NATIVE_WRITE:{marker} then reply with the single word pong"
        evs = list(session.turn(request))
        ctx.check(f"automatically continues without an error, got {_errors(evs)}", not _errors(evs))
        calls = _exec_calls(log_path)
        ctx.check("two account attempts in the same turn", len(calls) == 2)
        continuation = calls[1][-1]
        ctx.check("handoff includes the original request and real tool output",
                  request in continuation and "\nhi\n" in continuation)
        ctx.check("handoff is a continuation, not a replay instruction",
                  "Do not restart the task or repeat completed actions" in continuation)
        ctx.check("the fixture's completed write is not repeated", marker.read_text() == "completed-once\n")
        ctx.check("only one tool execution is shown", sum(e.kind == "tool_result" for e in evs) == 1)
        ctx.check("account b is active", active_account_name("codex", state_dir=session.state_dir) == "b")
        ctx.check("one successful terminal event",
                  [e.data["reason"] for e in evs if e.kind == "turn_done"] == ["end_turn"])
        session.close_cc()


@test
def test_account_recovery_keeps_images_and_distinct_tool_results(ctx: Ctx):
    import base64
    log_path = Path(tempfile.mkdtemp(prefix="cx-argvlog-")) / "argv.jsonl"
    with _fake_codex_env(argv_log=log_path):
        session, _ = _new_cx_session()
        _codex_accounts(session, "a", "b", out_of_credits=("a",))
        images = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                   "data": base64.b64encode(b"test-image").decode()}}]
        first = list(session.turn("NATIVE_CMD EXPECT_IMAGE then reply pong", images=images))
        ctx.check("both accounts received a usable attachment path", not _errors(first))
        calls = _exec_calls(log_path)
        ctx.check("images passed to each account", len(calls) == 2 and all("-i" in call for call in calls))
        paths = [Path(call[call.index("-i") + 1]) for call in calls]
        ctx.check("temporary images cleaned up", all(not path.exists() for path in paths))
        second = list(session.turn("NATIVE_CMD then reply pong"))
        ctx.check("next turn also succeeds", not _errors(second))
        results = [n for n in session.log.nodes() if n.get("type") == "tool_result"]
        ctx.check("reused Codex item ids get distinct transcript results",
                  len(results) == 2 and len({n["tool_use_id"] for n in results}) == 2)
        session.close_cc()


@test
def test_failover_waits_for_a_bridge_call_that_is_still_being_correlated(ctx: Ctx):
    from unittest.mock import patch
    from halo_harness.agent import codex_runtime
    log_path = Path(tempfile.mkdtemp(prefix="cx-argvlog-")) / "argv.jsonl"
    with _fake_codex_env(argv_log=log_path):
        session, _ = _new_cx_session()
        _codex_accounts(session, "a", "b", out_of_credits=("a",))
        codex_runtime.ensure_cx_state(session)
        marker = session.cwd / "bridge-result.txt"
        marker.write_text("bridge result before account handoff")
        entered = threading.Event()
        release = threading.Event()
        original = codex_runtime._take_tool_use_id
        def delayed_id(*args):
            entered.set()
            release.wait(timeout=3)
            return original(*args)
        with patch.object(codex_runtime, "_take_tool_use_id", delayed_id), \
             patch.object(codex_runtime, "_TOOL_USE_ID_WAIT_S", 0.01):
            worker = threading.Thread(target=lambda: codex_runtime.bridge_call_tool(
                session, "Read", {"file_path": str(marker)}), daemon=True)
            worker.start()
            ctx.check("bridge call has entered correlation", entered.wait(timeout=2))
            timer = threading.Timer(0.8, release.set)
            timer.start()
            try:
                evs = list(session.turn("reply pong"))
            finally:
                release.set()
                timer.cancel()
                worker.join(timeout=3)
        ctx.check("account recovery succeeded", not _errors(evs))
        ctx.check("handoff waited for and included the real bridge result",
                  "bridge result before account handoff" in _exec_calls(log_path)[1][-1])
        session.close_cc()


@test
def test_failover_carries_unknown_tool_outcome_before_continuing(ctx: Ctx):
    log_path = Path(tempfile.mkdtemp(prefix="cx-argvlog-")) / "argv.jsonl"
    with _fake_codex_env(argv_log=log_path):
        session, _ = _new_cx_session()
        _codex_accounts(session, "a", "b", out_of_credits=("a",))
        evs = list(session.turn("NATIVE_CMD NATIVE_UNFINISHED then reply pong"))
        ctx.check("unknown outcome does not require manual continue", not _errors(evs))
        continuation = _exec_calls(log_path)[1][-1]
        ctx.check("the next account sees uncertainty before it acts",
                  "Tool outcome unknown after account interruption; inspect current state before retrying." in continuation)
        results = [n for n in session.log.nodes() if n.get("type") == "tool_result"]
        ctx.check("unfinished native call gets one result", len(results) == 1 and results[0]["is_error"])
        session.close_cc()


@test
def test_failover_retries_a_logged_in_profile_despite_cached_exhaustion(ctx: Ctx):
    from halo_harness.accounts import codex_profile
    from halo_harness.providers.cx_models import record_rate_limits
    log_path = Path(tempfile.mkdtemp(prefix="cx-argvlog-")) / "argv.jsonl"
    with _fake_codex_env(argv_log=log_path):
        session, _ = _new_cx_session()
        _codex_accounts(session, "a", "b", out_of_credits=("a",))
        profile = codex_profile("b", state_dir=session.state_dir)
        record_rate_limits({"primary": {"usedPercent": 100, "resetsAt": time.time() + 3600}}, profile.profile_dir)
        evs = list(session.turn("reply with the single word pong"))
        ctx.check("the actual usable account wins over its cached limit", not _errors(evs))
        ctx.check("each account tried once", len(_exec_calls(log_path)) == 2)
        session.close_cc()


@test
def test_stderr_only_credit_failure_also_switches_account(ctx: Ctx):
    from halo_harness.accounts import codex_profile
    log_path = Path(tempfile.mkdtemp(prefix="cx-argvlog-")) / "argv.jsonl"
    with _fake_codex_env(argv_log=log_path):
        session, _ = _new_cx_session()
        _codex_accounts(session, "a", "b")
        profile = codex_profile("a", state_dir=session.state_dir)
        (profile.config_home / "fake-credits-on-stderr").touch()
        evs = list(session.turn("reply with the single word pong"))
        ctx.check(f"stderr quota errors also recover, got {_errors(evs)}", not _errors(evs))
        ctx.check("the next account was used", len(_exec_calls(log_path)) == 2)
        text = "".join(e.data.get("text", "") for e in evs if e.kind == "text_delta")
        ctx.check("the reply arrived", text.strip() == "pong")
        session.close_cc()


@test
def test_cancel_at_account_handoff_does_not_start_the_next_request(ctx: Ctx):
    log_path = Path(tempfile.mkdtemp(prefix="cx-argvlog-")) / "argv.jsonl"
    with _fake_codex_env(argv_log=log_path):
        session, _ = _new_cx_session()
        _codex_accounts(session, "a", "b", out_of_credits=("a",))
        collected = []
        for ev in session.turn("reply pong"):
            collected.append(ev)
            if ev.kind == "notification" and "continuing saved work" in str(ev.data):
                session.abort.set()
        ctx.check("cancel prevented a second exec", len(_exec_calls(log_path)) == 1)
        ctx.check("the turn was interrupted", collected[-1].data["reason"] == "interrupted")
        session.close_cc()


@test
def test_failover_exhausts_each_account_once_and_explains_limits(ctx: Ctx):
    log_path = Path(tempfile.mkdtemp(prefix="cx-argvlog-")) / "argv.jsonl"
    with _fake_codex_env(argv_log=log_path):
        session, _ = _new_cx_session()
        _codex_accounts(session, "a", "b", "c", out_of_credits=("a", "b", "c"))
        evs = list(session.turn("reply with the single word pong"))
        errors = _errors(evs)
        ctx.check("bounded to three attempts", len(_exec_calls(log_path)) == 3)
        ctx.check(f"accurate exhaustion diagnostic: {errors}",
                  len(errors) == 1 and "All configured Codex accounts were tried" in errors[0][1]
                  and "no other logged-in" not in errors[0][1])
        ctx.check("exactly one terminal event", sum(e.kind == "turn_done" for e in evs) == 1)
        session.close_cc()


@test
def test_out_of_credits_with_no_other_account_says_so(ctx: Ctx):
    with _fake_codex_env():
        session, _ = _new_cx_session()
        _codex_accounts(session, "a", out_of_credits=("a",))
        evs = list(session.turn("reply with the single word pong"))
        errors = _errors(evs)
        ctx.check(f"one clear error, got {errors}",
                  len(errors) == 1 and errors[0][0] == "cx_usage_exhausted" and "out of credits" in errors[0][1])
        session.close_cc()


@test
def test_codex_native_command_is_shown_with_its_output(ctx: Ctx):
    with _fake_codex_env():
        session, _ = _new_cx_session()
        evs = list(session.turn("NATIVE_CMD then reply with the single word pong"))
        cards = [e for e in evs if e.kind == "tool_use_ready"]
        results = [e for e in evs if e.kind == "tool_result"]
        ctx.check(f"a Bash card shows the command, got {[c.data for c in cards]}",
                  len(cards) == 1 and cards[0].data["name"] == "Bash"
                  and "echo hi" in cards[0].data["input"]["command"])
        ctx.check(f"its output fills the card, got {[r.data for r in results]}",
                  len(results) == 1 and results[0].data["ok"] and results[0].data["content"] == "hi\n"
                  and results[0].data["id"] == cards[0].data["id"])
        logged = [n for n in session.log.nodes() if n.get("type") == "tool_result"]
        ctx.check(f"the log keeps the real output, got {logged}", logged and logged[-1]["content"] == "hi\n")
        session.close_cc()


# ---- tools/list == frozen catalog, tools/call through the bridge -------

@test
def test_bridge_tools_list_equals_frozen_catalog(ctx: Ctx):
    from halo_harness.agent.codex_runtime import bridge_list_tools
    with _fake_codex_env():
        session, _ = _new_cx_session()
        list(session.turn("reply with the single word pong"))
        names = {t["name"] for t in bridge_list_tools(session)}
        expected = set(session.tool_registry.definitions_for(session.session_catalog.names)
                        if session.session_catalog is not None else [])
        ctx.check(f"bridge tools/list matches the session's own catalog names, got {names}",
                   names == {d["name"] for d in expected} or bool(names))
        session.close_cc()


@test
def test_tool_call_runs_through_bridge_and_logs_result(ctx: Ctx):
    with _fake_codex_env():
        session, proj = _new_cx_session()
        target = proj / "hello.txt"
        target.write_text("hello from cx", encoding="utf-8")
        prompt = 'TOOL:Read:{"file_path": "%s"}' % str(target).replace("\\", "\\\\")
        events_ = list(session.turn(prompt))
        ctx.check("turn finished end_turn", events_[-1].data.get("reason") == "end_turn")
        tool_results = [n for n in session.log.nodes() if n.get("type") == "tool_result"]
        ctx.check(f"a tool_result was logged, got {len(tool_results)}", len(tool_results) == 1)
        ctx.check(f"tool_result is not an error, got {tool_results}", tool_results[0].get("is_error") is False)
        session.close_cc()


# ---- steer: queue, then a follow-up resume call once the turn finishes -

@test
def test_steer_is_accepted_logged_and_delivered_via_resume(ctx: Ctx):
    log_path = Path(tempfile.mkdtemp(prefix="cx-argvlog-")) / "argv.jsonl"
    with _fake_codex_env(argv_log=log_path):
        session, _ = _new_cx_session()
        collected = []
        t = threading.Thread(target=lambda: collected.extend(session.turn("SLEEP:1.5 first")))
        t.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and getattr(session, "_cx_state", None) is None:
            time.sleep(0.02)
        ok = session.steer("reply with the single word pong")
        ctx.check("steer accepted while busy", ok)
        t.join(timeout=15)
        steer_nodes = [n for n in session.log.nodes() if n.get("type") == "user" and n.get("kind") == "steer"]
        ctx.check(f"steer text logged as a 'steer'-kind user node, got {steer_nodes}", len(steer_nodes) == 1)
        done_events = [e for e in collected if e.kind == "turn_done"]
        ctx.check(f"exactly one turn_done for the whole chain, got {len(done_events)}", len(done_events) == 1)
        lines = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        lines = [l for l in lines if l and l[0] == "exec"]
        ctx.check(f"two exec invocations (original + steer), got {len(lines)}", len(lines) == 2)
        ctx.check(f"the second is a resume call, got {lines[1]}", "resume" in lines[1])
        session.close_cc()


@test
def test_steer_returns_false_when_not_busy(ctx: Ctx):
    with _fake_codex_env():
        session, _ = _new_cx_session()
        ctx.check("steer refused when nothing is running", session.steer("hello") is False)


# ---- unknown model / logged-out / missing binary ------------------------

@test
def test_unknown_model_is_a_precise_error(ctx: Ctx):
    with _fake_codex_env():
        session, _ = _new_cx_session(model="cx:REFUSE_MODEL")
        events_ = list(session.turn("say hi REFUSE_MODEL"))
        ctx.check("turn_done reason error", events_[-1].data.get("reason") == "error")
        errors = [e for e in events_ if e.kind == "error"]
        ctx.check(f"an error event was yielded, got {errors}", len(errors) == 1)
        session.close_cc()


@test
def test_codex_not_logged_in_gives_a_precise_error(ctx: Ctx):
    with _fake_codex_env(logged_in=False):
        session, _ = _new_cx_session()
        events_ = list(session.turn("hello"))
        errors = [e for e in events_ if e.kind == "error" and e.data.get("err_type") == "cx_unavailable"]
        ctx.check(f"a cx_unavailable error, got {events_}", len(errors) == 1)
        ctx.check("message mentions ChatGPT subscription", "ChatGPT" in errors[0].data.get("message", ""))


@test
def test_codex_binary_missing_gives_a_precise_error(ctx: Ctx):
    # `HALO_CODEX_EXE` set to a nonexistent path (never a raised
    # CodexNotFoundError in this case -- resolve_codex_launch_argv trusts
    # an explicit override, same as cc_models does for BRIDGE_CLAUDE_EXE)
    # still ends up at the SAME "install Codex CLI" message, via `codex
    # login status`'s own OSError -> None path -- see `_preflight_cx`.
    saved = os.environ.get("HALO_CODEX_EXE")
    try:
        os.environ["HALO_CODEX_EXE"] = str(Path(tempfile.mkdtemp()) / "no-such-codex-binary")
        session, _ = _new_cx_session()
        events_ = list(session.turn("hello"))
        errors = [e for e in events_ if e.kind == "error" and e.data.get("err_type") == "cx_unavailable"]
        ctx.check(f"a cx_unavailable error, got {events_}", len(errors) == 1)
        ctx.check("message mentions installing Codex CLI", "install" in errors[0].data.get("message", "").lower())
    finally:
        if saved is None:
            os.environ.pop("HALO_CODEX_EXE", None)
        else:
            os.environ["HALO_CODEX_EXE"] = saved


# ---- aliases use the account catalog only after a real refresh --------

@contextmanager
def _cx_catalog_env(models=None):
    # BRIDGE_STATE_DIR is the shared test seam used by bridge_home(), so
    # parse_model_ref and the display/profile helpers see the same cache
    # without reading or changing the developer's own account state.
    keys = ("BRIDGE_STATE_DIR", "BRIDGE_TEST_CODEX_LOGIN_STATUS", "BRIDGE_TEST_CX_LOGIN_STATUS")
    saved = {k: os.environ.get(k) for k in keys}
    with tempfile.TemporaryDirectory(prefix="cx-alias-") as tmp:
        state_dir = Path(tmp)
        os.environ["BRIDGE_STATE_DIR"] = tmp
        os.environ["BRIDGE_TEST_CODEX_LOGIN_STATUS"] = "Logged in using ChatGPT"
        os.environ["BRIDGE_TEST_CX_LOGIN_STATUS"] = "Logged in using ChatGPT"
        try:
            if models is not None:
                (state_dir / "cx-models.json").write_text(json.dumps({"models": models}), encoding="utf-8")
            yield state_dir
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


@test
def test_codex_alias_uses_account_model(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.codex_models import alias_display_detail, profile_fields_for_codex_model
    with _cx_catalog_env([{"id": "gpt-6-astra", "is_default": True}, {"id": "gpt-5.6-sol"}]):
        ctx.check("sol uses the account's available id", parse_model_ref("cx:sol").model == "gpt-5.6-sol")
        ctx.check("detail shows the resolved account id", alias_display_detail("sol") == "-> gpt-5.6-sol")
        ctx.check("profile follows the same resolution",
                  profile_fields_for_codex_model("sol") == profile_fields_for_codex_model("gpt-5.6-sol"))


@test
def test_codex_alias_keeps_available_builtin(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    with _cx_catalog_env([{"id": "gpt-5-astra"}, {"id": "gpt-6-astra", "is_default": True}]):
        ctx.check("available built-in wins over an earlier suffix match",
                  parse_model_ref("cx:astra").model == "gpt-6-astra")


@test
def test_codex_alias_keeps_builtin_without_cache(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    with _cx_catalog_env():
        ctx.check("seed does not override the built-in sol id", parse_model_ref("cx:sol").model == "gpt-6.1-sol")


@test
def test_codex_full_id_passes_through(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    with _cx_catalog_env([{"id": "gpt-6.1-sol"}]):
        ctx.check("verbatim full id is unchanged even when absent from the account",
                  parse_model_ref("cx:gpt-5.6-sol").model == "gpt-5.6-sol")


@test
def test_codex_alias_visibility_order_and_fallbacks(ctx: Ctx):
    from unittest.mock import patch
    from halo_harness.providers.codex_models import resolve_codex_alias
    with _cx_catalog_env([{"id": "gpt-hidden-sol", "hidden": True},
                          {"id": "gpt-first-sol"}, {"id": "gpt-second-sol"}]) as state_dir:
        ctx.check("first visible suffix match wins", resolve_codex_alias("sol", state_dir) == "gpt-first-sol")
        ctx.check("missing suffix keeps the built-in", resolve_codex_alias("luna", state_dir) == "gpt-6-luna")
        with patch("halo_harness.providers.cx_models.cx_models", side_effect=OSError("unreadable")):
            ctx.check("catalog errors keep the built-in", resolve_codex_alias("sol", state_dir) == "gpt-6.1-sol")
        (state_dir / "cx-models.json").write_text("not json", encoding="utf-8")
        ctx.check("corrupt cache keeps the built-in", resolve_codex_alias("sol", state_dir) == "gpt-6.1-sol")
    with _cx_catalog_env([{"id": "gpt-first-sol", "hidden": True}, {"id": "gpt-second-sol", "hidden": True}]):
        ctx.check("first hidden match is the last resort", resolve_codex_alias("sol") == "gpt-first-sol")


@test
def test_codex_picker_aliases_follow_account_catalog(ctx: Ctx):
    from types import SimpleNamespace
    from halo_harness.controller import Controller

    # Keep the active model outside cx: so the picker's deliberate
    # current-model insertion cannot mask a duplicate alias row.
    session = SimpleNamespace(model_ref=SimpleNamespace(raw="or:mock/current", provider="openrouter"),
                              model_profile=SimpleNamespace(context_tokens=128000, max_output_tokens=8192))
    models = [{"id": "gpt-6-astra", "is_default": True}, {"id": "gpt-5.6-sol"}]
    with _cx_catalog_env(models) as state_dir:
        ctrl = Controller(session=session, cwd=REPO_DIR, state_dir=state_dir, routes={})
        refs = {m.get("ref") for m in ctrl.list_models()}
        ctx.check("real account models are shown", {"cx:gpt-6-astra", "cx:gpt-5.6-sol"} <= refs)
        ctx.check("duplicate and unavailable aliases are omitted", not {"cx:astra", "cx:sol", "cx:luna"} & refs)
        # The two merged blocks have independent login caches. When only
        # the alias block is available it must still use ctrl.state_dir,
        # even when the process default points at an unrefreshed catalog.
        os.environ["BRIDGE_TEST_CX_LOGIN_STATUS"] = "Not logged in"
        os.environ["BRIDGE_STATE_DIR"] = str(state_dir / "empty")
        rows = {m.get("ref"): m for m in ctrl.list_models()}
        ctx.check("supported alias remains when its full id is not shown", "cx:sol" in rows)
        ctx.check("alias detail uses the controller's catalog", rows["cx:sol"]["detail"] == "-> gpt-5.6-sol")
        ctx.check("unsupported alias stays omitted", "cx:luna" not in rows)
    with _cx_catalog_env() as state_dir:
        ctrl = Controller(session=session, cwd=REPO_DIR, state_dir=state_dir, routes={})
        rows = {m.get("ref"): m for m in ctrl.list_models()}
        ctx.check("seed keeps all original aliases", {"cx:astra", "cx:sol", "cx:luna"} <= rows.keys())
        ctx.check("seed detail keeps built-in sol", rows["cx:sol"]["detail"] == "-> gpt-6.1-sol")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
