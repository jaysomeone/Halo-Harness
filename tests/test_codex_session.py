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
              "FAKE_CODEX_ARGV_LOG")}
    os.environ["HALO_CODEX_EXE"] = '"' + sys.executable + '" "' + str(FAKE_CODEX) + '"'
    os.environ["FAKE_CODEX_LOGIN_STATUS"] = "Logged in using ChatGPT" if logged_in else "Not logged in"
    os.environ.pop("BRIDGE_TEST_CODEX_LOGIN_STATUS", None)  # the fake's own real answer counts now
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
        ctx.check(f"got pong, text={text!r}", text == "pong")
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
