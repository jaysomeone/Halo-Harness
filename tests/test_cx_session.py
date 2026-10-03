"""tests.test_cx_session -- 2.0.2: agent.loop.Session driving the `cx:`
route (Codex subscription) end to end against `tests/helpers/fake_codex_cx.py`
(a `codex app-server` stand-in speaking the real JSON-RPC shapes, acting as
a REAL MCP client against the REAL `python -m halo_harness.ccbridge` child
and ToolBridgeServer). Covers: lazy start and reuse, the thread config
(bridge as the only MCP server, approvals on, read-only sandbox), bridged
tools under auto/deny, native file changes decided by Halo's engine, steer,
Esc, thread resume and its fallback, failures, preflight messages, and the
cx_models helpers (login parsing, catalog refresh, usage line, effort clamp).
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

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

os.environ["BRIDGE_TEST_HOME"] = tempfile.mkdtemp(prefix="cx-session-scratchhome-")
ensure_default_provider_credentials()

test, TESTS = new_registry()

REPO_DIR = Path(__file__).resolve().parent.parent
FAKE_CODEX = REPO_DIR / "tests" / "helpers" / "fake_codex_cx.py"
_ENV_KEYS = ("HALO_CODEX_EXE", "FAKE_CODEX_LOGIN", "FAKE_CODEX_LOG", "FAKE_CODEX_USER_MCP",
             "FAKE_CODEX_RESUME_FAILS", "BRIDGE_TEST_CX_LOGIN_STATUS", "OPENAI_API_KEY")


@contextmanager
def _fake_codex_env(**extra):
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    log_path = Path(tempfile.mkdtemp(prefix="cx-log-")) / "codex.jsonl"
    os.environ["HALO_CODEX_EXE"] = '"' + sys.executable + '" "' + str(FAKE_CODEX) + '"'
    os.environ["FAKE_CODEX_LOG"] = str(log_path)
    os.environ.pop("BRIDGE_TEST_CX_LOGIN_STATUS", None)
    for k, v in extra.items():
        os.environ[k] = v
    from halo_harness.providers.cx_models import reset_cached_codex_login_status
    reset_cached_codex_login_status()
    try:
        yield log_path
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_cached_codex_login_status()


def _logged(log_path: Path, method: str) -> list:
    if not log_path.exists():
        return []
    rows = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [r["params"] for r in rows if r["method"] == method]


def _new_cx_session(*, permission_engine=None, cwd=None, model="cx:gpt-test", interactive=False):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import parse_model_ref, resolve_model_profile
    from halo_harness.permissions import PermissionEngine

    proj = cwd or Path(tempfile.mkdtemp(prefix="cx-sess-proj-"))
    proj.mkdir(parents=True, exist_ok=True)
    ref = parse_model_ref(model)
    profile = resolve_model_profile(ref, Path(tempfile.mkdtemp(prefix="cx-sess-state-")), {})
    session = Session(
        cwd=proj, model_ref=ref, model_profile=profile, creds=None,
        state_dir=Path(tempfile.mkdtemp(prefix="cx-sess-sdir-")), model_label=model,
        session_context=SessionContext(cwd=proj, model_label=model), max_turns=8,
        permission_engine=permission_engine or PermissionEngine(mode="auto", cwd=proj),
    )
    session.interactive = interactive
    return session, proj


def _text(evs) -> str:
    return "".join(e.data.get("text", "") for e in evs if e.kind == "text_delta")


def _wait(pred, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ---- lifecycle ----------------------------------------------------------------

@test
def test_cx_lazy_start_pong_and_usage(ctx: Ctx):
    with _fake_codex_env():
        session, _ = _new_cx_session()
        ctx.check("no _cx_state before any turn", getattr(session, "_cx_state", None) is None)
        evs = list(session.turn("reply with the single word pong"))
        ctx.check(f"pong reply, got {_text(evs)!r}", _text(evs).strip() == "pong")
        ctx.check("turn_done end_turn", evs[-1].kind == "turn_done" and evs[-1].data["reason"] == "end_turn")
        ends = [e for e in evs if e.kind == "message_end"]
        ctx.check(f"one message_end with tokens, got {ends}", len(ends) == 1
                  and ends[0].data["usage"].get("input_tokens") == 100)
        usage = [n for n in session.log.nodes() if n.get("type") == "usage"]
        ctx.check("usage node: route cx, no cost", usage and usage[-1].get("route") == "cx"
                  and usage[-1].get("cost_usd") is None)
        session.close_cc()


@test
def test_cx_app_server_and_thread_reused_across_turns(ctx: Ctx):
    with _fake_codex_env() as log_path:
        session, _ = _new_cx_session()
        list(session.turn("pong"))
        pid1 = session._cx_state.server.pid
        list(session.turn("pong"))
        ctx.check("same app-server pid", session._cx_state.server.pid == pid1)
        ctx.check("one thread/start", len(_logged(log_path, "thread/start")) == 1)
        metas = [n for n in session.log.nodes() if n.get("type") == "meta" and n.get("cx_thread_id")]
        ctx.check("cx_thread_id logged once", len(metas) == 1)
        session.close_cc()
        ctx.check("close_cc also closes cx", getattr(session, "_cx_state", None) is None)


@test
def test_cx_thread_config_bridge_only_approvals_on(ctx: Ctx):
    with _fake_codex_env(FAKE_CODEX_USER_MCP="mine,other") as log_path:
        session, _ = _new_cx_session()
        list(session.turn("pong"))
        params = _logged(log_path, "thread/start")[0]
        servers = params["config"]["mcp_servers"]
        ctx.check("halo bridge configured", servers["halo"]["args"] == ["-m", "halo_harness.ccbridge"])
        ctx.check("user's own codex MCP servers switched off",
                  servers.get("mine") == {"enabled": False} and servers.get("other") == {"enabled": False})
        ctx.check("approvals on, read-only sandbox",
                  params["approvalPolicy"] == "untrusted" and params["sandbox"] == "read-only")
        ctx.check("instructions name the halo tools", "mcp__halo__" in params["developerInstructions"])
        ctx.check("bridge socket env rides in config, not argv", servers["halo"]["env"])
        session.close_cc()


# ---- bridged tools and native file changes -------------------------------------

@test
def test_cx_bridged_read_runs_in_auto(ctx: Ctx):
    with _fake_codex_env():
        session, proj = _new_cx_session()
        target = proj / "f.txt"
        target.write_text("line-one\n", encoding="utf-8")
        evs = list(session.turn("TOOL:Read:" + json.dumps({"file_path": str(target)})))
        results = [e for e in evs if e.kind == "tool_result"]
        ctx.check(f"one ok tool_result, got {results}", len(results) == 1 and results[0].data["ok"] is True)
        ctx.check(f"model saw the file, got {_text(evs)!r}", "line-one" in _text(evs))
        uses = [b for n in session.log.nodes() if n.get("type") == "assistant"
                for b in n.get("content") or [] if b.get("type") == "tool_use"]
        ctx.check("tool_use logged with Codex's own item id", uses and uses[0]["id"].startswith("exec-"))
        session.close_cc()


@test
def test_cx_bridged_call_obeys_deny_rule(ctx: Ctx):
    from halo_harness.permissions import PermissionEngine, parse_rule
    with _fake_codex_env():
        proj = Path(tempfile.mkdtemp(prefix="cx-deny-"))
        engine = PermissionEngine(mode="default", cwd=proj, deny_rules=[parse_rule("Read", source="t", action="deny")])
        session, _ = _new_cx_session(permission_engine=engine, cwd=proj)
        target = proj / "f.txt"
        target.write_text("x\n", encoding="utf-8")
        evs = list(session.turn("TOOL:Read:" + json.dumps({"file_path": str(target)})))
        results = [e for e in evs if e.kind == "tool_result"]
        ctx.check("denied", len(results) == 1 and results[0].data["ok"] is False)
        ctx.check("recorded as a permission denial", session.permission_denials
                  and session.permission_denials[0]["tool_name"] == "Read")
        session.close_cc()


@test
def test_cx_native_file_change_allowed_in_auto(ctx: Ctx):
    with _fake_codex_env():
        session, proj = _new_cx_session()
        target = proj / "native.txt"
        evs = list(session.turn(f"PATCH:{target}:hello"))
        ctx.check("Codex applied it after Halo allowed", target.exists() and target.read_text() == "hello")
        ctx.check(f"reply patched, got {_text(evs)!r}", "patched" in _text(evs))
        uses = [b for n in session.log.nodes() if n.get("type") == "assistant"
                for b in n.get("content") or [] if b.get("type") == "tool_use"]
        ctx.check("logged as a Write call", uses and uses[0]["name"] == "Write"
                  and uses[0]["input"]["file_path"] == str(target))
        from halo_harness.agent.invariants import find_unpaired_tool_use_ids
        ctx.check("paired", find_unpaired_tool_use_ids(session.log) == [])
        session.close_cc()


@test
def test_cx_native_file_change_declined_when_engine_says_no(ctx: Ctx):
    from halo_harness.permissions import PermissionEngine, parse_rule
    with _fake_codex_env():
        proj = Path(tempfile.mkdtemp(prefix="cx-native-deny-"))
        engine = PermissionEngine(mode="auto", cwd=proj, deny_rules=[parse_rule("Write", source="t", action="deny")])
        session, _ = _new_cx_session(permission_engine=engine, cwd=proj)
        target = proj / "nope.txt"
        evs = list(session.turn(f"PATCH:{target}:hello"))
        ctx.check("file not written", not target.exists())
        ctx.check(f"Codex told declined, got {_text(evs)!r}", "declined" in _text(evs))
        session.close_cc()


# ---- steer, Esc, resume ---------------------------------------------------------

@test
def test_cx_steer_reaches_running_turn_and_is_logged(ctx: Ctx):
    with _fake_codex_env() as log_path:
        session, _ = _new_cx_session()
        collected = []
        t = threading.Thread(target=lambda: collected.extend(session.turn("SLEEP:3")))
        t.start()
        ok = _wait(lambda: getattr(getattr(session, "_cx_state", None), "turn_id", None) is not None)
        ctx.check("turn started", ok)
        ctx.check("steer accepted", session.steer("also say hi") is True)
        t.join(timeout=15)
        ctx.check(f"turn saw the steer, got {_text(collected)!r}", "steered:also say hi" in _text(collected))
        steer_calls = _logged(log_path, "turn/steer")
        ctx.check("turn/steer named the running turn", len(steer_calls) == 1 and steer_calls[0]["expectedTurnId"])
        steers = [n for n in session.log.nodes() if n.get("type") == "user" and n.get("kind") == "steer"]
        ctx.check("steer logged once", len(steers) == 1)
        ctx.check("steer when idle returns False", session.steer("late") is False)
        session.close_cc()


@test
def test_cx_esc_interrupts_and_next_turn_works(ctx: Ctx):
    with _fake_codex_env() as log_path:
        session, _ = _new_cx_session()
        collected = []
        t = threading.Thread(target=lambda: collected.extend(session.turn("SLEEP:30")))
        t.start()
        _wait(lambda: getattr(getattr(session, "_cx_state", None), "turn_id", None) is not None)
        started = time.monotonic()
        session.abort.set()
        t.join(timeout=15)
        ctx.check(f"interrupted quickly ({time.monotonic() - started:.1f}s)", time.monotonic() - started < 5)
        ctx.check("turn_done interrupted", collected and collected[-1].data.get("reason") == "interrupted")
        ctx.check("turn/interrupt sent", len(_logged(log_path, "turn/interrupt")) == 1)
        session.abort.clear()
        evs = list(session.turn("pong"))
        ctx.check(f"next turn works, got {_text(evs)!r}", _text(evs).strip() == "pong")
        session.close_cc()


@test
def test_cx_restart_resumes_the_same_thread(ctx: Ctx):
    with _fake_codex_env() as log_path:
        session, _ = _new_cx_session()
        list(session.turn("pong"))
        thread_id = session._cx_state.thread_id
        from halo_harness.agent import cx_runtime
        cx_runtime.close_cx(session)
        list(session.turn("pong"))
        resumes = _logged(log_path, "thread/resume")
        ctx.check(f"thread/resume with the logged id, got {resumes}", resumes and resumes[0]["threadId"] == thread_id)
        session.close_cc()


@test
def test_cx_resume_failure_starts_fresh_with_conversation_so_far(ctx: Ctx):
    # Set before the session exists: the app-server inherits the session's
    # tool env, captured at construction. The first turn never resumes.
    with _fake_codex_env(FAKE_CODEX_RESUME_FAILS="1") as log_path:
        session, _ = _new_cx_session()
        list(session.turn("remember the word MARMOT"))
        from halo_harness.agent import cx_runtime
        cx_runtime.close_cx(session)
        evs = list(session.turn("pong"))
        ctx.check("the turn still succeeds", evs[-1].data.get("reason") == "end_turn")
        ctx.check("two thread/start calls", len(_logged(log_path, "thread/start")) == 2)
        first_input = _logged(log_path, "turn/start")[-1]["input"][0]["text"]
        ctx.check("conversation so far rides along", "<conversation-so-far>" in first_input and "MARMOT" in first_input)
        session.close_cc()


# ---- failures and preflight ------------------------------------------------------

@test
def test_cx_failed_turn_surfaces_the_error(ctx: Ctx):
    with _fake_codex_env():
        session, _ = _new_cx_session()
        evs = list(session.turn("FAIL:usage limit reached"))
        errs = [e for e in evs if e.kind == "error"]
        ctx.check(f"error event carries Codex's message, got {errs}",
                  errs and "usage limit reached" in errs[0].data.get("message", ""))
        ctx.check("turn_done error", evs[-1].data.get("reason") == "error")
        session.close_cc()


@test
def test_cx_preflight_messages(ctx: Ctx):
    from halo_harness.agent.cx_runtime import preflight_cx
    with _fake_codex_env(FAKE_CODEX_LOGIN="Not logged in"):
        ctx.check("not logged in -> codex login", "codex login" in (preflight_cx() or ""))
    with _fake_codex_env(FAKE_CODEX_LOGIN="Logged in using an API key - sk-***"):
        ctx.check("API key login -> explains ChatGPT", "API key" in (preflight_cx() or ""))
    missing = '"' + str(Path(tempfile.mkdtemp(prefix="cx-missing-")) / "no-such-codex") + '"'
    with _fake_codex_env(HALO_CODEX_EXE=missing):
        msg = preflight_cx() or ""
        ctx.check(f"missing binary is reported, got {msg!r}", "could not run" in msg or "install" in msg.lower())
    with _fake_codex_env():
        ctx.check("ChatGPT login -> usable", preflight_cx() is None)


@test
def test_cx_not_logged_in_turn_gives_one_line_error(ctx: Ctx):
    from halo_harness.providers.enablement import enable
    enable("codex_subscription")
    with _fake_codex_env(FAKE_CODEX_LOGIN="Not logged in"):
        session, _ = _new_cx_session()
        evs = list(session.turn("pong"))
        errs = [e for e in evs if e.kind == "error"]
        ctx.check("cx_unavailable error", errs and errs[0].data.get("err_type") == "cx_unavailable")
        ctx.check("no app-server started", getattr(session, "_cx_state", None) is None)


# ---- cx_models helpers -----------------------------------------------------------

@test
def test_cx_login_status_parsing(ctx: Ctx):
    from halo_harness.providers.cx_models import parse_login_status_text
    ok = parse_login_status_text("WARNING: something\nLogged in using ChatGPT\n")
    ctx.check("ChatGPT", ok.logged_in and ok.method == "chatgpt")
    key = parse_login_status_text("Logged in using an API key - sk-proj-***ABCD")
    ctx.check("API key", key.logged_in and key.method == "api_key")
    ctx.check("not logged in", not parse_login_status_text("Not logged in").logged_in)
    ctx.check("garbage", not parse_login_status_text("").logged_in)


@test
def test_cx_catalog_refresh_and_helpers(ctx: Ctx):
    from halo_harness.providers import cx_models as m
    state = Path(tempfile.mkdtemp(prefix="cx-cat-"))
    with _fake_codex_env():
        ctx.check("seed before refresh", m.cx_catalog_is_seed(state))
        data = m.refresh_cx_catalog(state_dir=state)
    ids = [x["id"] for x in data.get("models", [])]
    ctx.check(f"models from model/list, got {ids}", ids == ["gpt-test", "gpt-hidden"])
    ctx.check("hidden marked", data["models"][1].get("hidden") is True)
    ctx.check("plan kept, email not stored", data["account"] == {"type": "chatgpt", "plan": "pro"}
              and "example.com" not in json.dumps(data))
    ctx.check("default alias", m.resolve_cx_alias("default", state) == "gpt-test")
    fields = m.profile_fields_for_cx_model("gpt-test", state)
    ctx.check("efforts from model/list", fields["efforts"] == ("low", "medium", "high"))
    m.record_context_window("gpt-test", 123456, state)
    ctx.check("observed context window wins", m.profile_fields_for_cx_model("gpt-test", state)["context_tokens"] == 123456)
    line = m.format_rate_limits({"plan": "pro", "primary": {"used_percent": 12, "window_mins": 300},
                                 "secondary": {"used_percent": 40, "window_mins": 10080}})
    ctx.check(f"usage line, got {line!r}", line == "pro plan · 5 h: 12% used · weekly: 40% used")
    m.record_rate_limits({"planType": "pro", "primary": {"usedPercent": 1, "windowDurationMins": 300}}, state)
    ctx.check("fresh reading has no age", "as of" not in m.cached_rate_limits_line(state))
    ctx.check("old reading says when", "as of" in m.cached_rate_limits_line(state, now=time.time() + 3600))


@test
def test_cx_effort_clamped_to_model(ctx: Ctx):
    from halo_harness.agent.cx_runtime import _turn_effort
    from halo_harness.providers import cx_models as m
    state = Path(tempfile.mkdtemp(prefix="cx-eff-"))
    with _fake_codex_env():
        m.refresh_cx_catalog(state_dir=state)

    class _S:
        effort = "max"
    real = m._cx_models_cache_path
    m._cx_models_cache_path = lambda state_dir=None: Path(state_dir or state) / "cx-models.json"
    try:
        ctx.check("max clamps down to high", _turn_effort(_S(), "gpt-test") == "high")
        _S.effort = "low"
        ctx.check("supported passes through", _turn_effort(_S(), "gpt-test") == "low")
        _S.effort = None
        ctx.check("no effort -> model default", _turn_effort(_S(), "gpt-test") is None)
    finally:
        m._cx_models_cache_path = real


@test
def test_cx_model_ref_and_child_env(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    ref = parse_model_ref("cx:gpt-5.5")
    ctx.check("provider cx", ref.provider == "cx" and ref.model == "gpt-5.5" and ref.dialect == "cx-appserver")
    from halo_harness.providers.cx_models import cx_child_env
    env = cx_child_env({"OPENAI_API_KEY": "sk-x", "CODEX_API_KEY": "k", "OPENAI_BASE_URL": "u",
                        "CODEX_HOME": "/h", "PATH": "/bin"})
    ctx.check("API key and base URL stripped, CODEX_HOME kept",
              "OPENAI_API_KEY" not in env and "CODEX_API_KEY" not in env and "OPENAI_BASE_URL" not in env
              and env.get("CODEX_HOME") == "/h")
    from halo_harness.providers.enablement import canonical, label_for
    ctx.check("cx and codex name the provider", canonical("cx") == canonical("codex") == "codex_subscription")
    ctx.check("label", label_for("cx") == "Codex subscription (ChatGPT)")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
