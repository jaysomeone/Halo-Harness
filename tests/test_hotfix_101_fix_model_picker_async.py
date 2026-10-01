"""tests.test_hotfix_101_fix_model_picker_async -- 1.0.1 fixpass finding 1:
`Controller.list_models()` loads models-dev.json/the vendored Databricks
fallback ONCE per call (not once per endpoint) and never spawns `claude
auth status` itself (only a cached read) -- together, the two costs that
made `/model` freeze the TUI for 2-5s on a real catalog. The TUI-visible
half (the picker now opens through a worker thread) is pinned in
test_tui.py.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()

test, TESTS = new_registry()


class _Env:
    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE",
                        "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                        "OPENROUTER_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                        "BRIDGE_TEST_CC_AUTH_STATUS")}
        d = Path(tempfile.mkdtemp(prefix="hotfix101-model-async-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)
        self.state_dir = d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _FakeModelRef:
    raw = "or:mock/current"
    provider = "openrouter"


class _FakeModelProfile:
    context_tokens = 128000
    max_output_tokens = 8192


class _FakeSession:
    model_ref = _FakeModelRef()
    model_profile = _FakeModelProfile()


def _controller(state_dir):
    from halo_harness.controller import Controller
    return Controller(session=_FakeSession(), cwd=Path.cwd(), state_dir=state_dir, routes={})


def _parsed(name):
    return {"name": name, "task": "llm/v1/chat", "ready": None, "permission_level": None,
            "endpoint_type": None, "ai_gateway_v2_supported": None,
            "api_types": ["mlflow/v1/chat/completions"], "foundation_model_name": name, "model_class": None}


# ---------------------------------------------------------------------------
# databricks_row_fields: accepts pre-loaded dicts, never re-reads when given.
# ---------------------------------------------------------------------------

@test
def test_databricks_row_fields_uses_the_passed_in_live_dict_without_reloading(ctx: Ctx):
    from halo_harness.model_display import databricks_row_fields
    import halo_harness.providers.models_dev as models_dev_mod

    calls = {"n": 0}
    real_load = models_dev_mod.load_models_dev_json

    def counting_load(state_dir):
        calls["n"] += 1
        return real_load(state_dir)

    models_dev_mod.load_models_dev_json = counting_load
    try:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)  # no models-dev.json cache written here at all
            live = {"databricks-glm-5-3": {"limit": {"context": 128000, "output": 64000},
                                            "cost": {"input": 1.0, "output": 5.0}}}
            fields = databricks_row_fields("databricks-glm-5-3", state_dir=state_dir,
                                            live_models_dev=live, vendored_fallback={})
            ctx.check(f"resolved from the PASSED-IN dict, got {fields}", fields.get("context_tokens") == 128000)
            ctx.check(f"load_models_dev_json was never called (dict was already provided), got {calls['n']} calls",
                      calls["n"] == 0)
    finally:
        models_dev_mod.load_models_dev_json = real_load


# ---------------------------------------------------------------------------
# Controller.list_models(): loads models-dev.json ONCE for the whole
# Databricks loop, regardless of endpoint count.
# ---------------------------------------------------------------------------

@test
def test_list_models_loads_models_dev_json_exactly_once_for_many_endpoints(ctx: Ctx):
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    import halo_harness.providers.models_dev as models_dev_mod

    calls = {"n": 0}
    real_load = models_dev_mod.load_models_dev_json

    def counting_load(state_dir):
        calls["n"] += 1
        return real_load(state_dir)

    models_dev_mod.load_models_dev_json = counting_load
    try:
        with _Env() as env:
            names = [f"databricks-glm-5-{i}" for i in range(8)]
            write_dbx_endpoints_json(env.state_dir, [_parsed(n) for n in names])
            ctrl = _controller(env.state_dir)
            models = ctrl.list_models()
            dbx_rows = [m for m in models if m.get("provider") == "databricks"]
            ctx.check(f"all 8 Databricks endpoints are listed, got {len(dbx_rows)}", len(dbx_rows) == 8)
            ctx.check(f"models-dev.json was read AT MOST once for the whole call (was once per endpoint -- "
                      f"8 calls -- before this fix), got {calls['n']} calls", calls["n"] <= 1)
    finally:
        models_dev_mod.load_models_dev_json = real_load


# ---------------------------------------------------------------------------
# list_models() never spawns `claude auth status` itself -- only a cached
# read; a startup worker is the only thing that ever populates the cache.
# ---------------------------------------------------------------------------

@test
def test_list_models_never_calls_the_real_auth_status_subprocess(ctx: Ctx):
    import halo_harness.providers.cc_models as cc_models_mod

    cc_models_mod.reset_cached_claude_auth_status()
    real_status_fn = cc_models_mod.claude_auth_status

    def _boom(*a, **kw):
        raise AssertionError("list_models() must never call claude_auth_status() directly")

    cc_models_mod.claude_auth_status = _boom
    try:
        with _Env() as env:
            ctrl = _controller(env.state_dir)
            models = ctrl.list_models()  # must not raise
            ctx.check("still returns a real list (cache empty -> no cc: group, not a crash)",
                      isinstance(models, list))
            ctx.check("no cc: rows without a primed cache", not any(m.get("provider") == "cc" for m in models))
    finally:
        cc_models_mod.claude_auth_status = real_status_fn
        cc_models_mod.reset_cached_claude_auth_status()


@test
def test_refresh_then_cached_read_reflects_the_primed_status(ctx: Ctx):
    """The startup-worker side of the cache pair: refresh_cached_claude_
    auth_status() (the ONE function that spawns anything) populates what
    cached_claude_auth_status() (list_models()'s own reader) then sees."""
    import halo_harness.providers.cc_models as cc_models_mod

    cc_models_mod.reset_cached_claude_auth_status()
    try:
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        try:
            status = cc_models_mod.refresh_cached_claude_auth_status()
        finally:
            os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)
        ctx.check(f"refresh returns the (test-seam) status, got {status}", status is not None and status.logged_in)
        # Cache now holds it -- BRIDGE_TEST_CC_AUTH_STATUS is unset again, so
        # a plain cached_claude_auth_status() read reaches the real
        # in-process cache, not the (now-gone) env override.
        cached = cc_models_mod.cached_claude_auth_status()
        ctx.check(f"cached read reflects what was just primed, got {cached}",
                  cached is not None and cached.logged_in and cached.auth_method == "claude.ai")
    finally:
        cc_models_mod.reset_cached_claude_auth_status()


@test
def test_cached_auth_status_bypasses_cache_under_the_test_seam(ctx: Ctx):
    """A test that sets/changes BRIDGE_TEST_CC_AUTH_STATUS must see its OWN
    current value immediately, never a stale value a previous call cached
    in this same process."""
    import halo_harness.providers.cc_models as cc_models_mod

    cc_models_mod.reset_cached_claude_auth_status()
    try:
        cc_models_mod._auth_status_cache = cc_models_mod.ClaudeAuthStatus(logged_in=True, auth_method="claude.ai")
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
        got = cc_models_mod.cached_claude_auth_status()
        ctx.check(f"the test seam wins over a stale cached value, got {got}", got is not None and not got.logged_in)
    finally:
        os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)
        cc_models_mod.reset_cached_claude_auth_status()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
