"""tests.test_h15_catalog_refresh -- H15 part 2 addendum 3.2: "catalogs must
exist without init, and /model must list every enabled provider regardless
of the current model." Covers `providers.databricks.
refresh_openrouter_catalog_if_stale`, `providers.anthropic_catalog`'s new
live `/v1/models` cache, the combined `providers.catalog_refresh.
refresh_all_enabled_catalogs`, `Controller.list_models()` building a
provider's group from its cache regardless of the CURRENT model, and
`halo models --refresh`/`/models refresh` hitting every enabled
provider.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_get_endpoints import MockGetEndpoints

test, TESTS = new_registry()

_PROVIDER_ENV_VARS = (
    "OPENROUTER_API_KEY", "BRIDGE_OPENROUTER_BASE_URL", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
    "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "ANTHROPIC_API_KEY", "BRIDGE_ANTHROPIC_BASE_URL",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "TYPESAFE_API_KEY", "BRIDGE_TEST_CC_AUTH_STATUS",
)


class _Env:
    """Snapshots/restores every provider variable this module touches, plus
    BRIDGE_TEST_HOME/BRIDGE_STATE_DIR -- the real `~/.halo` is never
    written. `BRIDGE_TEST_CC_AUTH_STATUS` defaults to a deterministic
    not-logged-in shape (see test_h15_provider_enablement.py's own `_Env`
    for why merely popping it is not enough on a box with a real claude.ai
    login)."""

    def __enter__(self):
        import json
        self._saved = {k: os.environ.get(k) for k in
                       (("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_TEST_NO_BACKGROUND_NET")
                        + _PROVIDER_ENV_VARS)}
        d = Path(tempfile.mkdtemp(prefix="h15-catalog-refresh-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        for k in _PROVIDER_ENV_VARS:
            os.environ.pop(k, None)
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
        # W6b section E fallout: tests/run_all.py's own whole-run default
        # (closing a WSL hang in an unrelated module) now leaves this set
        # ambiently for every module -- this one's own worker tests need
        # it genuinely ABSENT to exercise "the background worker actually
        # ran", so it is cleared here, not just left to whatever happened
        # to be ambient before this scope.
        os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        self.home = d
        self.state_dir = d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_OR_MODELS_BODY = {"data": [
    {"id": "deepseek/deepseek-v3.2", "context_length": 128000, "top_provider": {"max_completion_tokens": 8192},
     "pricing": {"prompt": "0.0000008", "completion": "0.0000024"}},
    {"id": "qwen/qwen3-max", "context_length": 256000, "top_provider": {"max_completion_tokens": 16384},
     "pricing": {"prompt": "0.000001", "completion": "0.000003"}},
]}

_ANT_MODELS_BODY = {"data": [
    {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5"},
    {"id": "claude-sonnet-5-5", "display_name": "Claude Sonnet 5.5"},
]}


class _FakeModelRef:
    def __init__(self, raw, provider):
        self.raw = raw
        self.provider = provider


class _FakeModelProfile:
    context_tokens = 128000
    max_output_tokens = 8192


class _FakeSession:
    def __init__(self, raw, provider):
        self.model_ref = _FakeModelRef(raw, provider)
        self.model_profile = _FakeModelProfile()


# ---------------------------------------------------------------------------
# refresh_openrouter_catalog_if_stale / refresh_anthropic_catalog_if_stale
# ---------------------------------------------------------------------------

@test
def test_refresh_openrouter_catalog_if_stale_populates_an_empty_cache(ctx: Ctx):
    from halo_harness.providers.databricks import load_models_json, refresh_openrouter_catalog_if_stale
    mock = MockGetEndpoints({"/api/v1/models": (200, _OR_MODELS_BODY)}).start()
    try:
        with _Env() as env:
            os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
            os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url + "/api/v1"
            ctx.check("models.json starts empty", load_models_json(env.state_dir) == {})
            ok = refresh_openrouter_catalog_if_stale(env.state_dir)
            ctx.check(f"refresh reports success, got {ok!r}", ok is True)
            models = load_models_json(env.state_dir)
            ctx.check(f"both mock models now cached, got {sorted(models)}",
                      set(models) == {"deepseek/deepseek-v3.2", "qwen/qwen3-max"})
            ctx.check("the mock's /models endpoint was actually hit",
                      any(r["path"] == "/api/v1/models" for r in mock.requests))
    finally:
        mock.stop()


@test
def test_refresh_openrouter_catalog_if_stale_skips_a_fresh_cache(ctx: Ctx):
    from halo_harness.providers.databricks import refresh_openrouter_catalog_if_stale, write_models_json
    with _Env() as env:
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        write_models_json(env.state_dir, [{"id": "already/cached", "context_length": 1000}])
        result = refresh_openrouter_catalog_if_stale(env.state_dir, max_age_hours=24)
        ctx.check(f"a just-written cache is not stale -> None, got {result!r}", result is None)


@test
def test_refresh_openrouter_catalog_if_stale_none_when_not_configured(ctx: Ctx):
    from halo_harness.providers.databricks import refresh_openrouter_catalog_if_stale
    with _Env() as env:
        ctx.check("no key -> None (nothing to refresh)",
                  refresh_openrouter_catalog_if_stale(env.state_dir) is None)


@test
def test_fetch_anthropic_models_and_cache_round_trip(ctx: Ctx):
    from halo_harness.providers.anthropic_catalog import (
        fetch_anthropic_models, load_ant_models_json, refresh_anthropic_catalog_if_stale,
    )
    mock = MockGetEndpoints({"/v1/models": (200, _ANT_MODELS_BODY)}).start()
    try:
        with _Env() as env:
            fetched = fetch_anthropic_models(mock.base_url, "sk-ant-fake")
            ctx.check(f"both mock models fetched, got {fetched}", len(fetched) == 2)
            os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"
            os.environ["BRIDGE_ANTHROPIC_BASE_URL"] = mock.base_url
            ok = refresh_anthropic_catalog_if_stale(env.state_dir)
            ctx.check(f"refresh reports success, got {ok!r}", ok is True)
            cached = load_ant_models_json(env.state_dir)
            ctx.check(f"cache written with both ids, got {sorted(cached)}",
                      set(cached) == {"claude-opus-5-5", "claude-sonnet-5-5"})
    finally:
        mock.stop()


@test
def test_refresh_anthropic_catalog_if_stale_none_when_not_configured(ctx: Ctx):
    from halo_harness.providers.anthropic_catalog import refresh_anthropic_catalog_if_stale
    with _Env() as env:
        ctx.check("no key -> None", refresh_anthropic_catalog_if_stale(env.state_dir) is None)


@test
def test_refresh_anthropic_catalog_if_stale_boot_time_monotonic_is_never_a_false_backoff(ctx: Ctx):
    """M1 (1.0.1 final pass): same boot-time monotonic() fix as
    providers.databricks's own refresh pair -- a state_dir with NO recorded
    failure must never be treated as having just failed at t=0."""
    import halo_harness.providers.anthropic_catalog as ant_mod
    mock = MockGetEndpoints({"/v1/models": (200, _ANT_MODELS_BODY)}).start()
    try:
        with _Env() as env:
            os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"
            os.environ["BRIDGE_ANTHROPIC_BASE_URL"] = mock.base_url
            ctx.check("nothing recorded as a failure for this state_dir yet",
                      str(env.state_dir) not in ant_mod._ant_last_failure_at)
            real_monotonic = ant_mod.time.monotonic
            ant_mod.time.monotonic = lambda: 120.0  # "120s since boot"
            try:
                ok = ant_mod.refresh_anthropic_catalog_if_stale(env.state_dir)
            finally:
                ant_mod.time.monotonic = real_monotonic
            ctx.check(f"the refresh actually proceeded (no false backoff), got {ok!r}", ok is True)
    finally:
        mock.stop()


# ---------------------------------------------------------------------------
# Controller.list_models(): a provider's group comes from its own cache
# regardless of which provider the CURRENT model belongs to.
# ---------------------------------------------------------------------------

@test
def test_list_models_openrouter_group_populated_after_catalog_refresh(ctx: Ctx):
    """the owner's own Mac report: "/model showed one OpenRouter row" because
    models.json had never been fetched -- after a refresh, the real
    catalog populates the group, not just a synthesized current-model row."""
    from halo_harness.controller import Controller
    from halo_harness.providers.databricks import refresh_openrouter_catalog_if_stale
    mock = MockGetEndpoints({"/api/v1/models": (200, _OR_MODELS_BODY)}).start()
    try:
        with _Env() as env:
            os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
            os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url + "/api/v1"
            refresh_openrouter_catalog_if_stale(env.state_dir)
            session = _FakeSession("or:deepseek/deepseek-v3.2", "openrouter")
            ctrl = Controller(session=session, cwd=Path.cwd(), state_dir=env.state_dir, routes={})
            or_refs = {m["ref"] for m in ctrl.list_models() if m.get("provider") == "openrouter"}
            ctx.check(f"both real catalog entries present, got {or_refs}",
                      {"or:deepseek/deepseek-v3.2", "or:qwen/qwen3-max"} <= or_refs)
    finally:
        mock.stop()


@test
def test_list_models_openrouter_group_unchanged_after_switching_to_cc_opus(ctx: Ctx):
    """The addendum's own named regression: switching the session's
    current model to a DIFFERENT provider (cc:opus) must never make an
    already-cached OpenRouter group disappear."""
    import json as json_mod
    from halo_harness.controller import Controller
    from halo_harness.providers.cc_models import refresh_cached_claude_auth_status
    from halo_harness.providers.databricks import refresh_openrouter_catalog_if_stale
    from halo_harness.providers.enablement import enable
    mock = MockGetEndpoints({"/api/v1/models": (200, _OR_MODELS_BODY)}).start()
    try:
        with _Env() as env:
            os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
            os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url + "/api/v1"
            refresh_openrouter_catalog_if_stale(env.state_dir)
            # Confirm the group exists BEFORE switching, so the next
            # assertion is a real "stayed," not "was never there."
            session_before = _FakeSession("or:deepseek/deepseek-v3.2", "openrouter")
            ctrl_before = Controller(session=session_before, cwd=Path.cwd(), state_dir=env.state_dir, routes={})
            before_refs = {m["ref"] for m in ctrl_before.list_models() if m.get("provider") == "openrouter"}
            ctx.check(f"populated before switching, got {before_refs}", len(before_refs) == 2)

            os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json_mod.dumps({"loggedIn": True, "authMethod": "claude.ai"})
            refresh_cached_claude_auth_status()
            enable("claude_subscription")
            session_after = _FakeSession("cc:opus", "cc")
            ctrl_after = Controller(session=session_after, cwd=Path.cwd(), state_dir=env.state_dir, routes={})
            after_refs = {m["ref"] for m in ctrl_after.list_models() if m.get("provider") == "openrouter"}
            ctx.check(f"OpenRouter group unchanged after switching to cc:opus, got {after_refs}",
                      after_refs == before_refs)
    finally:
        mock.stop()


# ---------------------------------------------------------------------------
# halo models --refresh / /models refresh hit every enabled provider.
# ---------------------------------------------------------------------------

@test
def test_cli_models_refresh_hits_both_openrouter_and_databricks_mocks(ctx: Ctx):
    import io
    from contextlib import redirect_stdout
    from halo_harness.catalog_cli import cmd_models
    from tests.helpers.mock_databricks import MockDatabricks
    or_mock = MockGetEndpoints({"/api/v1/models": (200, _OR_MODELS_BODY)}).start()
    dbx_mock = MockDatabricks().start()
    dbx_mock.set_endpoints_catalog([])
    try:
        with _Env() as env:
            os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
            os.environ["BRIDGE_OPENROUTER_BASE_URL"] = or_mock.base_url + "/api/v1"
            os.environ["BRIDGE_DBX_BASE_URL"] = dbx_mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            with redirect_stdout(io.StringIO()):
                code = cmd_models(["--refresh"])
            ctx.check(f"exits 0, got {code}", code == 0)
            ctx.check("the OpenRouter mock's /models endpoint was hit",
                      any(r["path"] == "/api/v1/models" for r in or_mock.requests))
            ctx.check("the Databricks mock's endpoints-list was also hit", len(dbx_mock.requests) > 0)
    finally:
        or_mock.stop()
        dbx_mock.stop()


@test
def test_slash_models_refresh_hits_both_openrouter_and_databricks_mocks(ctx: Ctx):
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_models
    from tests.helpers.mock_databricks import MockDatabricks
    or_mock = MockGetEndpoints({"/api/v1/models": (200, _OR_MODELS_BODY)}).start()
    dbx_mock = MockDatabricks().start()
    dbx_mock.set_endpoints_catalog([])
    try:
        with _Env():
            os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
            os.environ["BRIDGE_OPENROUTER_BASE_URL"] = or_mock.base_url + "/api/v1"
            os.environ["BRIDGE_DBX_BASE_URL"] = dbx_mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            out = _cmd_models("refresh", HeadlessFacade(cwd=Path.cwd()))
            ctx.check(f"mentions OpenRouter refreshed, got {out!r}", "OpenRouter refreshed" in out)
            ctx.check("the OpenRouter mock was actually hit",
                      any(r["path"] == "/api/v1/models" for r in or_mock.requests))
            ctx.check("the Databricks mock was also hit", len(dbx_mock.requests) > 0)
    finally:
        or_mock.stop()
        dbx_mock.stop()


# ---------------------------------------------------------------------------
# N2c (1.0.1 final pass): catalog_auto_refresh_worker -- the SAME function
# both the launch-time worker and /model's own open-time refresh call --
# must resolve credentials from the attached controller's own trust-
# filtered Settings.effective_env, never bare os.environ.
# ---------------------------------------------------------------------------

class _FakeSettingsForWorker:
    def __init__(self, env: dict):
        self.effective_env = env


class _FakeControllerForCatalogWorker:
    def __init__(self, state_dir, env: dict):
        self.state_dir = state_dir
        self.settings = _FakeSettingsForWorker(env)


class _FakeAppForCatalogWorker:
    def __init__(self, state_dir, env: dict):
        self.controller = _FakeControllerForCatalogWorker(state_dir, env)
        self.notifications: list = []

    def call_from_thread(self, fn, *a, **kw):
        fn(*a, **kw)

    def notify(self, text, **kw):
        self.notifications.append(text)


@test
def test_catalog_auto_refresh_worker_reads_the_controllers_effective_env_not_bare_os_environ(ctx: Ctx):
    from halo_harness.providers.enablement import enable
    from halo_harness.providers.databricks import load_models_json
    from halo_harness.tui.slash import catalog_auto_refresh_worker
    mock = MockGetEndpoints({"/api/v1/models": (200, _OR_MODELS_BODY)}).start()
    try:
        with _Env() as env:
            # Deliberately nothing in the REAL process env -- the key only
            # ever lives in the fake controller's own effective_env; an
            # explicit override is needed since is_enabled()'s own
            # auto-detection reads bare os.environ, unaffected by this fix.
            enable("openrouter")
            app = _FakeAppForCatalogWorker(env.state_dir, {
                "OPENROUTER_API_KEY": "sk-or-from-effective-env",
                "BRIDGE_OPENROUTER_BASE_URL": mock.base_url + "/api/v1",
            })
            catalog_auto_refresh_worker(app)
            cached = load_models_json(env.state_dir)
            ctx.check(f"resolved and fetched using the controller's own effective_env, got {sorted(cached)}",
                      set(cached) == {"deepseek/deepseek-v3.2", "qwen/qwen3-max"})
            sent = [r for r in mock.requests if r["path"] == "/api/v1/models"]
            ctx.check(f"the key from effective_env was actually sent, got {sent}",
                      sent and sent[0]["headers"].get("Authorization") == "Bearer sk-or-from-effective-env")
    finally:
        mock.stop()


@test
def test_catalog_auto_refresh_worker_never_resolves_from_bare_os_environ_when_effective_env_has_nothing(ctx: Ctx):
    """The mirror case: a REAL ambient OPENROUTER_API_KEY in os.environ must
    NOT be picked up once a controller/settings is attached -- only its own
    effective_env counts (monkeypatch-free: proven by a real, otherwise-
    valid ambient key being ignored)."""
    from halo_harness.providers.enablement import enable
    from halo_harness.providers.databricks import load_models_json
    from halo_harness.tui.slash import catalog_auto_refresh_worker
    with _Env() as env:
        enable("openrouter")
        os.environ["OPENROUTER_API_KEY"] = "sk-or-should-be-ignored"
        # The fake controller's own effective_env has NO OpenRouter key at
        # all (an empty dict stands in for a trust-filtered env that
        # dropped it) -- the refresh must find nothing to do, never fall
        # back to the real ambient os.environ.
        app = _FakeAppForCatalogWorker(env.state_dir, {})
        catalog_auto_refresh_worker(app)
        ctx.check("nothing cached -- the ambient os.environ key was never consulted",
                  load_models_json(env.state_dir) == {})


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
