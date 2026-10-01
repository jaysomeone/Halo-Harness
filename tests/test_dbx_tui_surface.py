"""tests.test_dbx_tui_surface -- H14 scope I/J: the headless `/models`
(`/dbx` alias) surface and `Controller.list_models()`'s Databricks section
(grouped by family, path type shown, non-chat endpoints hidden).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_databricks import MockDatabricks

test, TESTS = new_registry()


class _Env:
    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE",
                        "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS",
                        "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "TYPESAFE_API_KEY",
                        "BRIDGE_TEST_CC_AUTH_STATUS")}
        d = Path(tempfile.mkdtemp(prefix="dbx-tui-surface-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        # H15 part 2 addendum: `anthropic`/`openrouter`/`typesafe` now
        # auto-enable straight from these keys (providers/enablement.py) --
        # popped here too (never just left to whatever the real shell/an
        # earlier test happened to leave behind) so is_enabled()/
        # credentials_present() are as deterministic here as the Databricks
        # vars above already were.
        for k in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "TYPESAFE_API_KEY"):
            os.environ.pop(k, None)
        # 1.0.1 hotfix addendum 9: Controller.list_models() now calls
        # claude_auth_status() to decide whether to show the cc: group --
        # pinned to a deterministic "not logged in" here (same convention
        # test_init_cli.py/test_doctor_prescriptive_fixes.py already use) so
        # these tests never depend on whether THIS machine happens to have
        # a real `claude` binary/login, and never pay a real subprocess's
        # worth of latency.
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
        self.state_dir = d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _raw(name, fm_name, api_types, task="llm/v1/chat"):
    """The REAL listing shape (nested "foundation_model") -- feeds a mock
    server's `set_endpoints_catalog`, consumed via a live probe."""
    return {"name": name, "task": task, "foundation_model": {"name": fm_name, "api_types": api_types}}


def _parsed(name, fm_name, api_types, task="llm/v1/chat"):
    """The ALREADY-PARSED cache shape (what a probe of `_raw(...)` actually
    produces) -- for tests that call `write_dbx_endpoints_json` directly,
    with no live probe involved at all."""
    return {"name": name, "task": task, "ready": None, "permission_level": None, "endpoint_type": None,
            "ai_gateway_v2_supported": None, "api_types": api_types, "foundation_model_name": fm_name,
            "model_class": None}


# ---------------------------------------------------------------------------
# Headless /models [refresh] and /dbx.
# ---------------------------------------------------------------------------

def _facade():
    from halo_harness.commands.builtins import HeadlessFacade
    return HeadlessFacade(cwd=Path.cwd())


@test
def test_cmd_models_headless_not_configured(ctx: Ctx):
    from halo_harness.commands.builtins import _cmd_models
    with _Env():
        for k in ("BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                  "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
            os.environ.pop(k, None)
        out = _cmd_models("", _facade())
        ctx.check(f"clear not-configured message, got {out!r}", "not configured" in out)


@test
def test_cmd_models_headless_bare_reports_cache_state(ctx: Ctx):
    from halo_harness.commands.builtins import _cmd_models
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    with _Env() as env:
        os.environ["BRIDGE_DBX_BASE_URL"] = "https://your-workspace.cloud.databricks.com"
        os.environ["BRIDGE_DBX_TOKEN"] = "tok"
        write_dbx_endpoints_json(env.state_dir, [_parsed("databricks-glm-5-3", "glm-5-3",
                                                           ["mlflow/v1/chat/completions"])])
        out = _cmd_models("", _facade())
        ctx.check(f"reports 1 cached endpoint, got {out!r}", "1 Databricks endpoint(s) cached" in out)
        ctx.check("points at /models refresh or /dbx", "/models refresh" in out and "/dbx" in out)


@test
def test_cmd_models_and_dbx_refresh_against_mock(ctx: Ctx):
    from halo_harness.commands.builtins import _cmd_dbx, _cmd_models
    mock = MockDatabricks().start()
    mock.set_endpoints_catalog([_raw("databricks-kimi-k3", "kimi-k3", ["mlflow/v1/chat/completions"])])
    try:
        with _Env():
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            out = _cmd_models("refresh", _facade())
            ctx.check(f"/models refresh reports the refreshed count, got {out!r}", "Refreshed: 1" in out)
            out2 = _cmd_dbx("ignored args", _facade())
            ctx.check(f"/dbx always refreshes regardless of args, got {out2!r}", "Refreshed: 1" in out2)
    finally:
        mock.stop()


@test
def test_cmd_models_refresh_failure_reports_and_keeps_cache_count(ctx: Ctx):
    from halo_harness.commands.builtins import _cmd_models
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    mock = MockDatabricks().start()
    mock.set_endpoints_error("403-ip")
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            write_dbx_endpoints_json(env.state_dir, [_parsed("databricks-glm-5-3", "glm-5-3",
                                                              ["mlflow/v1/chat/completions"])])
            out = _cmd_models("refresh", _facade())
            ctx.check(f"failure reported, still shows the cached count, got {out!r}",
                      "Refresh failed" in out and "1 endpoint(s) still cached" in out)
    finally:
        mock.stop()


# ---------------------------------------------------------------------------
# Controller.list_models() Databricks section.
# ---------------------------------------------------------------------------

class _FakeModelRef:
    raw = "or:deepseek/deepseek-v4.1-flash"
    provider = "openrouter"


class _FakeModelProfile:
    context_tokens = 128000
    max_output_tokens = 16384


class _FakeSession:
    model_ref = _FakeModelRef()
    model_profile = _FakeModelProfile()


def _controller(state_dir):
    from halo_harness.controller import Controller
    return Controller(session=_FakeSession(), cwd=Path.cwd(), state_dir=state_dir, routes={})


@test
def test_list_models_includes_databricks_grouped_by_family_hides_non_chat(ctx: Ctx):
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    with _Env() as env:
        # H15 part 2 addendum: Databricks now auto-enables from real
        # credentials, not "no providers block at all" -- this test cares
        # about grouping/rendering an already-cached catalog, not live
        # credential resolution, so a fake host/token is enough.
        os.environ["BRIDGE_DBX_BASE_URL"] = "https://your-workspace.cloud.databricks.com"
        os.environ["BRIDGE_DBX_TOKEN"] = "tok"
        write_dbx_endpoints_json(env.state_dir, [
            _parsed("databricks-glm-5-3", "glm-5-3", ["mlflow/v1/chat/completions", "anthropic/v1/messages"]),
            _parsed("databricks-claude-opus-4-6", "claude-opus-4-6",
                    ["mlflow/v1/chat/completions", "anthropic/v1/messages"]),
            _parsed("databricks-gte-large-en", "gte-large-en", ["mlflow/v1/embeddings"], task="llm/v1/embeddings"),
        ])
        ctrl = _controller(env.state_dir)
        rows = {m["ref"]: m for m in ctrl.list_models()}
        ctx.check(f"glm listed, got {sorted(rows)}", "dbx:databricks-glm-5-3" in rows)
        ctx.check(f"claude foundation listed", "dbx:databricks-claude-opus-4-6" in rows)
        ctx.check("embeddings endpoint hidden from the picker",
                  "dbx:databricks-gte-large-en" not in rows)
        ctx.check(f"glm grouped by family, got {rows['dbx:databricks-glm-5-3'].get('group')}",
                  rows["dbx:databricks-glm-5-3"]["group"] == "Databricks (glm)")
        ctx.check(f"glm path type is mlflow (default), got {rows['dbx:databricks-glm-5-3'].get('path_type')}",
                  rows["dbx:databricks-glm-5-3"]["path_type"] == "mlflow")
        ctx.check(f"claude foundation path type is anthropic, got "
                  f"{rows['dbx:databricks-claude-opus-4-6'].get('path_type')}",
                  rows["dbx:databricks-claude-opus-4-6"]["path_type"] == "anthropic")


@test
def test_list_models_empty_catalog_does_not_crash(ctx: Ctx):
    with _Env() as env:
        ctrl = _controller(env.state_dir)
        models = ctrl.list_models()
        ctx.check("still returns the OpenRouter/current-model rows without crashing", isinstance(models, list))


# ---------------------------------------------------------------------------
# 1.0.1 hotfix addendum 9: the cc: group appears only when the subscription
# route is actually available.
# ---------------------------------------------------------------------------

@test
def test_list_models_hides_cc_group_when_not_logged_in(ctx: Ctx):
    with _Env() as env:  # _Env already pins BRIDGE_TEST_CC_AUTH_STATUS to "not logged in"
        ctrl = _controller(env.state_dir)
        models = ctrl.list_models()
        ctx.check(f"no cc: rows at all, got {[m['ref'] for m in models if m.get('provider') == 'cc']}",
                  not any(m.get("provider") == "cc" for m in models))


@test
def test_list_models_shows_cc_group_when_logged_in_via_claude_ai(ctx: Ctx):
    old = os.environ.get("BRIDGE_TEST_CC_AUTH_STATUS")
    os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
    try:
        with _Env() as env:
            os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
            ctrl = _controller(env.state_dir)
            models = ctrl.list_models()
            cc_rows = [m for m in models if m.get("provider") == "cc"]
            ctx.check(f"cc: rows present, got {len(cc_rows)}", len(cc_rows) > 0)
            ctx.check(f"grouped under the subscription label, got {cc_rows[0].get('group')}",
                      cc_rows[0].get("group") == "Claude Code subscription")
    finally:
        if old is None:
            os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)
        else:
            os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = old


@test
def test_list_models_hides_cc_group_when_logged_in_via_work_env_not_subscription(ctx: Ctx):
    """The exact owner-reported bug: a Databricks WORK box's `claude` is
    logged in via ITS OWN work settings (authMethod != "claude.ai"), not a
    personal subscription -- the picker must not offer nine cc: models a
    real `cc:<name>` call would never be able to use there."""
    old = os.environ.get("BRIDGE_TEST_CC_AUTH_STATUS")
    os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "api_key"})
    try:
        with _Env() as env:
            os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "api_key"})
            ctrl = _controller(env.state_dir)
            models = ctrl.list_models()
            ctx.check("no cc: rows when logged in via a non-subscription authMethod",
                      not any(m.get("provider") == "cc" for m in models))
    finally:
        if old is None:
            os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)
        else:
            os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = old


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
