"""Regression coverage for the fork's upstream Codex integration boundaries."""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.helpers.runner import Ctx, new_registry, run_all, print_results
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()
test, TESTS = new_registry()


@test
def test_legacy_threads_preserve_account_ownership(ctx):
    from halo_harness.agent.codex_runtime import _last_cx_session_id
    rows = [dict(type="meta", cx_thread_id="legacy", cx_account="a"),
            dict(type="meta", cx_session_id="modern", cx_account="b")]
    log = SimpleNamespace(nodes=lambda: rows)
    ctx.check("old field read for owner", _last_cx_session_id(log, "a") == "legacy")
    ctx.check("new field read for owner", _last_cx_session_id(log, "b") == "modern")
    ctx.check("no cross-account resume", _last_cx_session_id(log, "c") is None)
    rows.append(dict(type="meta", cx_thread_boundary=True))
    ctx.check("fork excludes all original accounts", all(_last_cx_session_id(log, a) is None for a in (None, "a", "b")))
    rows.append(dict(type="meta", cx_session_id="branch", cx_account="a"))
    ctx.check("branch can resume its own thread", _last_cx_session_id(log, "a") == "branch")


@test
def test_fork_persists_an_independent_thread_boundary(ctx):
    from halo_harness.agent.log import SessionLog
    from halo_harness.agent.codex_runtime import _last_cx_session_id
    from halo_harness.controller import Controller
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        log = SessionLog(cwd)
        log.append_meta(cx_session_id="original", cx_account="a")
        log.append_user([{"type": "text", "text": "remember this"}])
        closed = []
        session = SimpleNamespace(log=log, _cx_state=SimpleNamespace(bridge=SimpleNamespace(close=lambda: closed.append(True))))
        controller = SimpleNamespace(session=session, cwd=cwd)
        Controller.fork_session(controller)
        ctx.check("bridge closed", closed == [True] and session._cx_state is None)
        ctx.check("new branch cannot resume original", _last_cx_session_id(session.log, "a") is None)
        ctx.check("original untouched", _last_cx_session_id(log, "a") == "original")
        ctx.check("conversation copied", any(n.get("type") == "user" for n in session.log.nodes()))
        reopened = SessionLog(cwd, session_id=session.log.session_id)
        ctx.check("boundary survives reopening", _last_cx_session_id(reopened, "a") is None)


@test
def test_profile_and_alias_use_selected_accounts_catalog(ctx):
    from halo_harness.accounts import prepare_codex_profile, set_active_account
    from halo_harness.providers.profiles import resolve_profile
    from halo_harness.providers.routing import Route
    from halo_harness.providers.codex_models import resolve_codex_alias
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp)
        acct = prepare_codex_profile("a", state_dir=state)
        set_active_account("codex", "a", state_dir=state)
        (acct.profile_dir / "cx-models.json").write_text(json.dumps({"models": [
            {"id": "gpt-custom-sol", "efforts": ["low", "high"], "default_effort": "high"}]}))
        profile = resolve_profile(Route("codex", "gpt-custom-sol", "codex-subprocess"), state_dir=state)
        ctx.check("active route uses account efforts", profile.effort_values_supported == ("low", "high"))
        ctx.check("alias uses same account", resolve_codex_alias("sol", state) == "gpt-custom-sol")
        ctx.check("legacy default alias supported", resolve_codex_alias("default", state) == "gpt-custom-sol")


@test
def test_usage_probe_reads_only_limits_and_closes(ctx):
    from halo_harness.providers.sub_usage import probe_cx_usage, subscription_usage
    from halo_harness.agent.cx_process import CodexAppServer
    calls = []
    server = SimpleNamespace(request=lambda method, params, **kw: calls.append(method) or {
        "rateLimits": {"primary": {"usedPercent": 42}, "secondary": {"usedPercent": 17}}},
        close=lambda: calls.append("close"))
    with tempfile.TemporaryDirectory() as tmp, patch.object(CodexAppServer, "start", return_value=server) as start:
        ctx.check("probe succeeded", probe_cx_usage(Path(tmp), {"CODEX_HOME": "account-a"}))
        ctx.check("correct credentials passed", start.call_args.kwargs["env"]["CODEX_HOME"] == "account-a")
        ctx.check("no model turn", calls == ["account/rateLimits/read", "close"])
        ctx.check("fresh values cached", subscription_usage("codex", Path(tmp))["session_pct"] == 42)
        server.request = lambda *a, **kw: {}
        ctx.check("empty result does not erase cache", not probe_cx_usage(Path(tmp), {}))
        ctx.check("last success preserved", subscription_usage("codex", Path(tmp))["weekly_pct"] == 17)


@test
def test_usage_refresh_is_throttled_and_pinned_to_account(ctx):
    from halo_harness.accounts import prepare_codex_profile, set_active_account
    from halo_harness.providers import sub_usage as u
    calls, workers = [], []
    class Worker:
        def __init__(self, target, **kw):
            workers.append(target)
        def start(self):
            pass
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"BRIDGE_TEST_NO_BACKGROUND_NET": "0"}), patch.object(u.threading, "Thread", Worker), patch.object(u, "probe_cx_usage", side_effect=lambda dest, env: calls.append((dest, env))):
        state = Path(tmp)
        a = prepare_codex_profile("a", state_dir=state)
        b = prepare_codex_profile("b", state_dir=state)
        set_active_account("codex", "a", state_dir=state)
        ctx.check("first refresh starts", u.maybe_refresh_cx_usage("codex", state, base_env={}))
        ctx.check("overlapping refresh blocked", not u.maybe_refresh_cx_usage("codex", state, base_env={}))
        set_active_account("codex", "b", state_dir=state)
        workers.pop(0)()
        ctx.check("in-flight refresh stays on a", calls[0][0] == a.profile_dir and calls[0][1]["CODEX_HOME"] == str(a.config_home))
        ctx.check("new account refresh starts immediately", u.maybe_refresh_cx_usage("codex", state, base_env={}))
        workers.pop(0)()
        ctx.check("b has its own destination", calls[1][0] == b.profile_dir)
        ctx.check("repeat throttled", not u.maybe_refresh_cx_usage("codex", state, base_env={}))
        ctx.check("API provider does not probe", not u.maybe_refresh_cx_usage("openai", state, base_env={}))


@test
def test_usage_probe_failure_releases_running_flag(ctx):
    from halo_harness.providers import sub_usage as u
    workers = []
    class Worker:
        def __init__(self, target, **kw): workers.append(target)
        def start(self): pass
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"BRIDGE_TEST_NO_BACKGROUND_NET": "0"}), patch.object(u.threading, "Thread", Worker), patch.object(u, "probe_cx_usage", side_effect=OSError("offline")):
        state = Path(tmp)
        u.maybe_refresh_cx_usage("codex", state, base_env={})
        workers.pop()()
        ctx.check("failed worker released", str(state.resolve()) not in u._cx_probes_running)
        with patch.object(u.time, "monotonic", return_value=u._cx_last_probe_at[str(state.resolve())] + 61):
            ctx.check("failure retried later", u.maybe_refresh_cx_usage("codex", state, base_env={}))
            workers.pop()()


@test
def test_toolbar_never_reuses_another_accounts_reading(ctx):
    from halo_harness.tui.widgets.statusbar import StatusBar
    with patch.object(StatusBar, "_refresh_display"):
        bar = StatusBar(cwd="/tmp")
        bar.apply_status({"model": "cx:astra", "subscription_account": "a", "subscription_usage": {"session_pct": 42}})
        bar.apply_status({"model": "cx:astra", "subscription_account": "b", "subscription_usage": None})
        ctx.check("new account has no stale percent", bar.subscription_usage is None)
        bar.apply_status({"model": "cx:astra", "subscription_account": "a", "subscription_usage": None})
        ctx.check("same account retains last success", bar.subscription_usage["session_pct"] == 42)


@test
def test_usage_read_over_real_stdio_without_model_turn(ctx):
    from halo_harness.providers.sub_usage import probe_cx_usage, subscription_usage
    from tests.test_cx_session import _fake_codex_env
    with tempfile.TemporaryDirectory() as tmp, _fake_codex_env():
        ctx.check("JSON-RPC usage read succeeds", probe_cx_usage(Path(tmp), dict(os.environ)))
        ctx.check("wire value reaches cache", subscription_usage("codex", Path(tmp))["session_pct"] == 3)


@test
def test_exec_fork_and_effort_with_transport_stub(ctx):
    # This tests the real exec subprocess and session/controller paths.
    # The MCP listener is stubbed because these turns never call tools.
    from tests.test_codex_session import _fake_codex_env, _new_cx_session, _codex_accounts
    from halo_harness.ccbridge.server import ToolBridgeServer
    from halo_harness.controller import Controller
    with tempfile.TemporaryDirectory() as tmp, _fake_codex_env(argv_log=Path(tmp)/"argv"), patch.object(ToolBridgeServer, "start"):
        session, cwd = _new_cx_session()
        _codex_accounts(session, "a", "b")
        session.effort = "high"
        try:
            evs = list(session.turn("remember pong"))
            ctx.check("first turn succeeds", not any(e.kind == "error" for e in evs))
            original = session._cx_state.cx_session_id
            Controller(session=session, cwd=cwd).fork_session()
            evs = list(session.turn("continue pong"))
            ctx.check("fork turn succeeds", not any(e.kind == "error" for e in evs))
            ctx.check("different Codex thread", session._cx_state.cx_session_id != original)
            calls = [json.loads(line) for line in (Path(tmp)/"argv").read_text().splitlines()]
            calls = [c for c in calls if "exec" in c]
            ctx.check("fork starts without resume", "resume" not in calls[-1])
            ctx.check("fork carries conversation", "remember pong" in calls[-1][-1])
            ctx.check("effort reaches Codex", "model_reasoning_effort=high" in calls[-1])
        finally:
            session.close_cc()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
