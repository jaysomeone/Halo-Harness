"""tests.test_h15b_fixpass -- 1.0.1 "part 2" fix pass (H15b): pinning tests
for review findings that don't already have a natural home in an existing
file. Covers: `cmd_providers` loading the env file (finding 8), the
OpenRouter balance fetch's official-host gate (finding 9), catalog-refresh
single-flight/atomic-write/read-timeout (finding 11), and the TUI's
`/models`/`/dbx` worker refreshing every enabled provider (finding 12).

See also: tests/test_h15_provider_enablement.py (criticals #2/#3, the
override-only gate), tests/test_h15_path_check.py (finding 16),
tests/test_h15_init_tabs.py (findings 5/6), tests/test_hotfix_101_fix_
init_preserve.py (finding 7), tests/test_hotfix_101_fix_effort_retry.py
(finding 13), test_cc_session.py/test_init_cli.py (finding 10's test-hygiene
half), test_tui.py (findings 1/4/10/14/17, the Textual-pilot half).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_PROVIDER_ENV_VARS = (
    "OPENROUTER_API_KEY", "OPENROUTER_MANAGEMENT_KEY", "BRIDGE_OPENROUTER_BASE_URL",
    "DATABRICKS_HOST", "DATABRICKS_TOKEN", "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "BRIDGE_ANTHROPIC_BASE_URL",
    "TYPESAFE_API_KEY", "BRIDGE_TEST_CC_AUTH_STATUS",
    # Deliberately scoped/cleared (never set) here -- finding 9's own
    # pinning test below needs the REAL strict openrouter.ai-only check, so
    # this must never leak in from another module's own test seam.
    "BRIDGE_TEST_OPENROUTER_HOST_OVERRIDE",
)


class _Env:
    """Snapshots/restores every provider variable this module touches, plus
    BRIDGE_TEST_HOME/BRIDGE_STATE_DIR/BRIDGE_ENV_FILE -- the real
    `~/.halo` is never written."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       (("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE") + _PROVIDER_ENV_VARS)}
        d = Path(tempfile.mkdtemp(prefix="h15b-fixpass-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        for k in _PROVIDER_ENV_VARS:
            os.environ.pop(k, None)
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
        self.home = d
        self.state_dir = d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# N1 (1.0.1 final pass): tool_child_env/cc_child_env must also strip
# OPENROUTER_MANAGEMENT_KEY (a separate, higher-privilege OpenRouter
# credential) and TYPESAFE_API_KEY (the TypeSafe provider's own key) --
# neither was in the original fixed secret list, so either one would have
# reached a Bash/PowerShell/MCP/hooks child, or the `claude` subprocess,
# verbatim.
# ---------------------------------------------------------------------------

@test
def test_tool_child_env_strips_openrouter_management_and_typesafe_keys(ctx: Ctx):
    from halo_harness.providers.config import tool_child_env
    raw_env = {
        "OPENROUTER_MANAGEMENT_KEY": "sk-or-mgmt-super-secret",
        "TYPESAFE_API_KEY": "ts-super-secret",
        "SOME_ORDINARY_VAR": "kept",
    }
    with _Env() as env:
        # A nonexistent env_file_path -- only the FIXED secret-key list
        # (never an env-file key) is what's responsible for stripping these.
        stripped = tool_child_env(raw_env, env_file_path=env.home / "no-such-env-file")
        ctx.check(f"OPENROUTER_MANAGEMENT_KEY stripped, got {stripped}",
                  "OPENROUTER_MANAGEMENT_KEY" not in stripped)
        ctx.check(f"TYPESAFE_API_KEY stripped, got {stripped}", "TYPESAFE_API_KEY" not in stripped)
        ctx.check(f"an ordinary var survives, got {stripped}", stripped.get("SOME_ORDINARY_VAR") == "kept")


@test
def test_cc_child_env_also_strips_them(ctx: Ctx):
    """cc_child_env calls tool_child_env first -- the cc: route's own child
    env (the installed `claude` subprocess, and transitively ccbridge) must
    never see either key either; no separate list to fix there."""
    from halo_harness.providers.config import cc_child_env
    raw_env = {"OPENROUTER_MANAGEMENT_KEY": "sk-or-mgmt-secret", "TYPESAFE_API_KEY": "ts-secret"}
    with _Env() as env:
        stripped = cc_child_env(raw_env, env_file_path=env.home / "no-such-env-file")
        ctx.check(f"both still stripped via cc_child_env, got {stripped}",
                  "OPENROUTER_MANAGEMENT_KEY" not in stripped and "TYPESAFE_API_KEY" not in stripped)


# ---------------------------------------------------------------------------
# finding 8: `halo providers` must load the env file, same as
# `catalog_cli`/`doctor` already do.
# ---------------------------------------------------------------------------

@test
def test_cmd_providers_loads_the_env_file(ctx: Ctx):
    import io
    from contextlib import redirect_stdout
    from halo_harness.providers_cli import cmd_providers
    with _Env():
        env_file = Path(os.environ["BRIDGE_ENV_FILE"])
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("OPENROUTER_API_KEY=sk-or-from-env-file\n", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_providers([])
        ctx.check(f"exit 0, got {rc}", rc == 0)
        out = buf.getvalue()
        or_line = next(line for line in out.splitlines() if line.startswith("OpenRouter"))
        ctx.check(f"env-file-only key shows as auto-detected (not 'not set up'), got {or_line!r}",
                  "auto (detected from" in or_line)
        ctx.check("the key actually reached this process's env (not just the printed line)",
                  os.environ.get("OPENROUTER_API_KEY") == "sk-or-from-env-file")


# ---------------------------------------------------------------------------
# finding 3 (remainder): the small-model ref is parsed leniently -- a
# refusal (an explicit `enabled: false` on ITS OWN provider, e.g. a shared
# routes.json's "small" pinned to a provider this box never set up) falls
# back to the already-resolved MAIN ref instead of failing the whole session.
#
# M4 (1.0.1 final pass): the routes.json/routes.example "small" default
# falling back stays SILENT (nobody typed this -- it's a shared file's own
# lenient choice); an EXPLICIT --small-model/HALO_MODEL_SMALL that gets
# refused must print one stderr line saying so instead.
# ---------------------------------------------------------------------------

def _close_build(build) -> None:
    try:
        build.session._fire_session_end("quit")
    except Exception:
        pass
    try:
        build.session.job_registry.kill_all()
    except Exception:
        pass
    if build.mcp_manager is not None:
        try:
            build.mcp_manager.close_all()
        except Exception:
            pass


@test
def test_build_session_small_model_refusal_falls_back_to_the_main_ref(ctx: Ctx):
    import io
    from contextlib import redirect_stderr
    from halo_harness.headless import build_session
    from halo_harness.providers.enablement import disable
    with _Env() as env:
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        disable("databricks")  # the small ref's own provider, explicitly off
        routes_path = env.state_dir / "routes.json"
        env.state_dir.mkdir(parents=True, exist_ok=True)
        routes_path.write_text(json.dumps({"small": "dbx:databricks-some-endpoint"}), encoding="utf-8")
        cwd = Path(tempfile.mkdtemp(prefix="h15b-small-model-cwd-"))
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            build = build_session(cwd=cwd, model_ref_raw="or:deepseek/deepseek-v3.2", bare=True, print_mode=True,
                                   max_turns=1)
        try:
            ctx.check(f"main model resolved as requested, got {build.session.model_ref.raw!r}",
                      build.session.model_ref.provider == "openrouter")
            ctx.check("the refused small ref fell back to the main ref (never raised)",
                      build.session.small_model_ref is build.session.model_ref)
            ctx.check(f"the routes.json default's own fallback stays SILENT, got {stderr_buf.getvalue()!r}",
                      stderr_buf.getvalue() == "")
        finally:
            _close_build(build)


@test
def test_build_session_explicit_small_model_refusal_warns_on_stderr(ctx: Ctx):
    """M4: --small-model (small_model_ref_raw=, build_session's own CLI-flag
    parameter) is an EXPLICIT ask -- a refusal here must never be silent."""
    import io
    from contextlib import redirect_stderr
    from halo_harness.headless import build_session
    from halo_harness.providers.enablement import disable
    with _Env() as env:
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        disable("databricks")
        cwd = Path(tempfile.mkdtemp(prefix="h15b-explicit-small-model-cwd-"))
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            build = build_session(cwd=cwd, model_ref_raw="or:deepseek/deepseek-v3.2", bare=True, print_mode=True,
                                   max_turns=1, small_model_ref_raw="dbx:databricks-explicitly-refused")
        try:
            ctx.check("still falls back to the main ref (never raises)",
                      build.session.small_model_ref is build.session.model_ref)
            stderr_text = stderr_buf.getvalue()
            ctx.check(f"a stderr line was printed, got {stderr_text!r}", stderr_text.strip() != "")
            ctx.check(f"names the refused explicit ref, got {stderr_text!r}",
                      "dbx:databricks-explicitly-refused" in stderr_text)
            ctx.check(f"names --small-model/HALO_MODEL_SMALL, got {stderr_text!r}",
                      "--small-model" in stderr_text and "HALO_MODEL_SMALL" in stderr_text)
            ctx.check(f"names the main model it fell back to, got {stderr_text!r}",
                      "or:deepseek/deepseek-v3.2" in stderr_text)
        finally:
            _close_build(build)


# ---------------------------------------------------------------------------
# 2.0.2 review finding 12 (major): nothing ever read the `small` role --
# the session's own small model came only from --small-model/HALO_MODEL_
# SMALL/routes.json's own "small"/the main model, even though both the
# built-in presets and the Databricks cost-aware table set `roles.small`.
# ---------------------------------------------------------------------------

@test
def test_build_session_persisted_roles_small_wins_over_routes_default(ctx: Ctx):
    from halo_harness.headless import build_session
    from halo_harness.theme import set_config_value
    with _Env() as env:
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        set_config_value("roles.small", "or:vendor/cheap-small")
        env.state_dir.mkdir(parents=True, exist_ok=True)
        (env.state_dir / "routes.json").write_text(
            json.dumps({"small": "or:vendor/routes-default-small"}), encoding="utf-8")
        cwd = Path(tempfile.mkdtemp(prefix="h15b-roles-small-cwd-"))
        build = build_session(cwd=cwd, model_ref_raw="or:deepseek/deepseek-v3.2", bare=True, print_mode=True,
                               max_turns=1)
        try:
            ctx.check(f"roles.small wins over routes.json's own default, got {build.session.small_model_ref.raw!r}",
                      build.session.small_model_ref.raw == "or:vendor/cheap-small")
        finally:
            _close_build(build)


@test
def test_build_session_cli_role_small_wins_and_carries_its_own_effort(ctx: Ctx):
    """`--role small=MODEL:EFFORT` (the CLI override, one rung above the
    persisted table) wins outright, and its own effort now reaches
    `session.small_model_effort` -- "per-role effort" was partial
    before this (the second half of finding 12)."""
    from halo_harness.headless import build_session
    from halo_harness.theme import set_config_value
    with _Env() as env:
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        set_config_value("roles.small", "or:vendor/persisted-small")
        cwd = Path(tempfile.mkdtemp(prefix="h15b-cli-role-small-cwd-"))
        build = build_session(cwd=cwd, model_ref_raw="or:deepseek/deepseek-v3.2", bare=True, print_mode=True,
                               max_turns=1, roles_flag=["small=or:vendor/cli-small:low"])
        try:
            ctx.check(f"the CLI --role override wins, got {build.session.small_model_ref.raw!r}",
                      build.session.small_model_ref.raw == "or:vendor/cli-small")
            ctx.check(f"its own effort reached the session, got {build.session.small_model_effort!r}",
                      build.session.small_model_effort == "low")
        finally:
            _close_build(build)


# ---------------------------------------------------------------------------
# finding 9: the OpenRouter balance fetch only ever targets the real
# openrouter.ai host.
# ---------------------------------------------------------------------------

@test
def test_is_openrouter_official_host(ctx: Ctx):
    from halo_harness.providers.openrouter_account import is_openrouter_official_host
    ctx.check("the real host, https", is_openrouter_official_host("https://openrouter.ai/api/v1") is True)
    ctx.check("plain http refused", is_openrouter_official_host("http://openrouter.ai/api/v1") is False)
    ctx.check("a self-hosted proxy refused", is_openrouter_official_host("https://my-proxy.example.com/v1") is False)
    ctx.check("a lookalike subdomain refused", is_openrouter_official_host("https://openrouter.ai.evil.com/v1") is False)
    ctx.check("garbage input never raises", is_openrouter_official_host("not a url at all") is False)


@test
def test_is_openrouter_official_host_override_seam_requires_loopback(ctx: Ctx):
    """M2 (1.0.1 final pass): BRIDGE_TEST_OPENROUTER_HOST_OVERRIDE=1 must
    only ever waive the check for an ACTUAL loopback base_url -- a real
    (non-loopback) host is still refused even with the seam set, so a
    leaked/misconfigured env var in a real environment can never wave a
    hostile base_url through."""
    import halo_harness.providers.openrouter_account as or_mod
    saved = os.environ.get("BRIDGE_TEST_OPENROUTER_HOST_OVERRIDE")
    try:
        os.environ["BRIDGE_TEST_OPENROUTER_HOST_OVERRIDE"] = "1"
        ctx.check("a real, non-loopback host is still refused despite the seam",
                  or_mod.is_openrouter_official_host("https://evil.example/v1") is False)
        ctx.check("loopback IS accepted with the seam set (the seam's own intended use)",
                  or_mod.is_openrouter_official_host("http://127.0.0.1:9999/v1") is True)
        ctx.check("localhost by name also accepted with the seam set",
                  or_mod.is_openrouter_official_host("http://localhost:9999/v1") is True)
    finally:
        if saved is None:
            os.environ.pop("BRIDGE_TEST_OPENROUTER_HOST_OVERRIDE", None)
        else:
            os.environ["BRIDGE_TEST_OPENROUTER_HOST_OVERRIDE"] = saved


@test
def test_balance_refresh_refuses_a_non_official_host_before_touching_either_key(ctx: Ctx):
    """The critical part: NEITHER `fetch_key_info` NOR `fetch_credits` may
    even be CALLED for a non-official base_url -- poisoned to prove it."""
    import halo_harness.providers.openrouter_account as or_mod
    with _Env():
        or_mod.reset_cached_openrouter_balance()
        real_fetch_key_info = or_mod.fetch_key_info
        real_fetch_credits = or_mod.fetch_credits

        def _poison_key(*a, **kw):
            raise AssertionError("must never fetch /key for a non-official host")

        def _poison_credits(*a, **kw):
            raise AssertionError("must never fetch /credits for a non-official host")

        or_mod.fetch_key_info = _poison_key
        or_mod.fetch_credits = _poison_credits
        try:
            result = or_mod.refresh_cached_openrouter_balance(
                "https://self-hosted-proxy.example.com/v1", "sk-or-fake", management_key="mgmt-fake")
            ctx.check(f"returns None, got {result!r}", result is None)
            ctx.check("the cache stays empty", or_mod.cached_openrouter_balance() is None)
        finally:
            or_mod.fetch_key_info = real_fetch_key_info
            or_mod.fetch_credits = real_fetch_credits
            or_mod.reset_cached_openrouter_balance()


# ---------------------------------------------------------------------------
# finding 11a: catalog writes are atomic (tmp file + os.replace).
# ---------------------------------------------------------------------------

@test
def test_write_dbx_endpoints_json_leaves_no_tmp_file_behind(ctx: Ctx):
    from halo_harness.providers.databricks import dbx_endpoints_path, load_dbx_endpoints_json, write_dbx_endpoints_json
    with _Env() as env:
        write_dbx_endpoints_json(env.state_dir, [{"name": "databricks-x", "task": "llm/v1/chat"}])
        ctx.check("the real file exists", dbx_endpoints_path(env.state_dir).exists())
        leftovers = [p for p in env.state_dir.iterdir() if p.name.startswith(".dbx-endpoints.json.")]
        ctx.check(f"no leftover tmp file, got {leftovers}", leftovers == [])
        ctx.check("content round-trips", "databricks-x" in load_dbx_endpoints_json(env.state_dir))


@test
def test_write_dbx_endpoints_json_uses_os_replace(ctx: Ctx):
    import halo_harness.providers.databricks as dbx_mod
    calls = []
    real_replace = os.replace

    def _spy(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    os.replace = _spy
    try:
        with _Env() as env:
            dbx_mod.write_dbx_endpoints_json(env.state_dir, [{"name": "databricks-y", "task": "llm/v1/chat"}])
            ctx.check(f"os.replace used exactly once, got {calls}", len(calls) == 1)
            ctx.check(f"replaced INTO the real dbx-endpoints.json path, got {calls}",
                      calls[0][1].endswith("dbx-endpoints.json"))
    finally:
        os.replace = real_replace


@test
def test_write_ant_models_json_also_atomic(ctx: Ctx):
    from halo_harness.providers.anthropic_catalog import ant_models_json_path, load_ant_models_json, write_ant_models_json
    with _Env() as env:
        write_ant_models_json(env.state_dir, [{"id": "claude-opus-5-5", "display_name": "Opus 5.5"}])
        leftovers = [p for p in env.state_dir.iterdir() if p.name.startswith(".ant-models.json.")]
        ctx.check(f"no leftover tmp file, got {leftovers}", leftovers == [])
        ctx.check("content round-trips", "claude-opus-5-5" in load_ant_models_json(env.state_dir))
        ctx.check("the real file exists", ant_models_json_path(env.state_dir).exists())


# ---------------------------------------------------------------------------
# finding 11b: single-flight -- a concurrent refresh never runs a second
# probe at once.
# ---------------------------------------------------------------------------

@test
def test_refresh_dbx_catalog_single_flight_refuses_a_concurrent_call(ctx: Ctx):
    import halo_harness.providers.databricks as dbx_mod
    with _Env() as env:
        dbx_mod._dbx_refresh_lock.acquire()
        try:
            ok, diff, note = dbx_mod.refresh_dbx_catalog(env.state_dir, "https://doesnt-matter.invalid", "tok")
            ctx.check(f"refused as already-running, got {(ok, note)}", ok is False)
            # 2.0.1 finding 24: the exact wording changed from "already in
            # progress -- keeping the cached catalog" to this -- never
            # dressed up as "refresh failed" by a caller (tui/slash.py,
            # commands/builtins.py both special-case this exact note).
            ctx.check(f"says a refresh is already running, try again in a second, got {note!r}",
                      note == dbx_mod.REFRESH_BUSY_NOTE and "already running" in note)
            ctx.check("the diff is empty (no write happened)", diff == {})
        finally:
            dbx_mod._dbx_refresh_lock.release()


@test
def test_refresh_openrouter_catalog_single_flight_refuses_a_concurrent_call(ctx: Ctx):
    import halo_harness.providers.databricks as dbx_mod
    with _Env() as env:
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        dbx_mod._or_refresh_lock.acquire()
        try:
            result = dbx_mod.refresh_openrouter_catalog_if_stale(env.state_dir, force=True)
            # 2.0.1 finding 24: distinguishable from a genuine failure --
            # CATALOG_REFRESH_BUSY is falsy, so this is still "not ok" for
            # any caller that only checks truthiness...
            ctx.check(f"refused (falsy) while a refresh is already in flight, got {result!r}", not result)
            # ... but a caller that wants the distinction (the TUI's
            # /models refresh, halo models --refresh) can tell it apart
            # from a plain False failure by identity.
            ctx.check(f"specifically the CATALOG_REFRESH_BUSY sentinel, not a plain False, got {result!r}",
                      result is dbx_mod.CATALOG_REFRESH_BUSY and result is not False)
        finally:
            dbx_mod._or_refresh_lock.release()


@test
def test_refresh_anthropic_catalog_single_flight_refuses_a_concurrent_call(ctx: Ctx):
    import halo_harness.providers.anthropic_catalog as ant_mod
    import halo_harness.providers.databricks as dbx_mod
    with _Env() as env:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"
        ant_mod._ant_refresh_lock.acquire()
        try:
            result = ant_mod.refresh_anthropic_catalog_if_stale(env.state_dir, force=True)
            ctx.check(f"refused (falsy) while a refresh is already in flight, got {result!r}", not result)
            ctx.check(f"the SAME shared sentinel as the OpenRouter/Databricks refreshers, got {result!r}",
                      result is dbx_mod.CATALOG_REFRESH_BUSY)
        finally:
            ant_mod._ant_refresh_lock.release()


# ---------------------------------------------------------------------------
# finding 11c: a recent failure backs an AUTO-refresh off for 5 minutes;
# an explicit force=True is never subject to it.
# ---------------------------------------------------------------------------

@test
def test_refresh_dbx_catalog_if_stale_backs_off_after_a_recent_failure(ctx: Ctx):
    from tests.helpers.mock_databricks import MockDatabricks
    mock = MockDatabricks().start()
    mock.set_endpoints_error("403-ip")
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            from halo_harness.providers.databricks import refresh_dbx_catalog_if_stale
            first = refresh_dbx_catalog_if_stale(env.state_dir, max_age_hours=0)
            ctx.check(f"the first (forced) attempt fails and hits the mock, got {first}",
                      first is not None and first[0] is False)
            hits_after_first = len(mock.requests)
            second = refresh_dbx_catalog_if_stale(env.state_dir, max_age_hours=0)
            ctx.check(f"the second AUTO attempt (force=False via max_age_hours=0, no explicit force) "
                      f"backs off instead of hitting the mock again, got {second!r}", second is None)
            ctx.check(f"the mock was NOT hit a second time, got {len(mock.requests)} (was {hits_after_first})",
                      len(mock.requests) == hits_after_first)
    finally:
        mock.stop()


@test
def test_refresh_dbx_catalog_explicit_force_ignores_the_backoff(ctx: Ctx):
    from tests.helpers.mock_databricks import MockDatabricks
    mock = MockDatabricks().start()
    mock.set_endpoints_error("403-ip")
    try:
        with _Env() as env:
            from halo_harness.providers.databricks import refresh_dbx_catalog
            first = refresh_dbx_catalog(env.state_dir, mock.root, "tok")
            ctx.check(f"first attempt fails, got {first}", first[0] is False)
            mock.set_endpoints_catalog([])
            second = refresh_dbx_catalog(env.state_dir, mock.root, "tok")
            ctx.check(f"a direct refresh_dbx_catalog call (what /models refresh, /dbx use) is NEVER "
                      f"backed off, got {second}", second[0] is True)
    finally:
        mock.stop()


# ---------------------------------------------------------------------------
# M1 (1.0.1 final pass): time.monotonic() counts from OS boot, not from this
# process's own start -- `_dbx_last_failure_at.get(key, 0.0)` made the first
# 300s after boot look like "a failure happened at t=0" for a state_dir that
# has NEVER actually failed, silently skipping its very first auto-refresh.
# ---------------------------------------------------------------------------

@test
def test_refresh_dbx_catalog_if_stale_boot_time_monotonic_is_never_a_false_backoff(ctx: Ctx):
    import halo_harness.providers.databricks as dbx_mod
    from tests.helpers.mock_databricks import MockDatabricks
    mock = MockDatabricks().start()
    mock.set_endpoints_catalog([])
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            ctx.check("nothing recorded as a failure for this state_dir yet",
                      str(env.state_dir) not in dbx_mod._dbx_last_failure_at)
            real_monotonic = dbx_mod.time.monotonic
            dbx_mod.time.monotonic = lambda: 120.0  # "120s since boot"
            try:
                result = dbx_mod.refresh_dbx_catalog_if_stale(env.state_dir, max_age_hours=0)
            finally:
                dbx_mod.time.monotonic = real_monotonic
            ctx.check(f"the refresh actually proceeded (no false backoff), got {result!r}",
                      result is not None and result[0] is True)
            ctx.check("the mock was actually hit", len(mock.requests) > 0)
    finally:
        mock.stop()


# ---------------------------------------------------------------------------
# finding 11d: a quick catalog probe never inherits open_upstream's own 300s
# idle timeout -- 30s instead, set via conn.sock.settimeout right after
# connecting.
# ---------------------------------------------------------------------------

class _FakeSock:
    def __init__(self):
        self.timeouts: list = []

    def settimeout(self, value):
        self.timeouts.append(value)


class _FakeResp:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


class _FakeConn:
    def __init__(self, body: bytes):
        self.sock = _FakeSock()
        self._body = body

    def request(self, method, path, headers=None):
        pass

    def getresponse(self):
        return _FakeResp(200, self._body)

    def close(self):
        pass


@test
def test_probe_databricks_status_sets_a_30s_read_timeout(ctx: Ctx):
    import halo_harness.providers.http as http_mod
    from halo_harness.providers.databricks import probe_databricks_status
    fake_conn = _FakeConn(json.dumps({"endpoints": []}).encode("utf-8"))
    real_open_upstream = http_mod.open_upstream
    http_mod.open_upstream = lambda host, port, tls, *a, **kw: fake_conn
    try:
        status, _raw = probe_databricks_status("https://fake-ws.cloud.databricks.com", "tok")
        ctx.check(f"status 200, got {status}", status == 200)
        ctx.check(f"the 300s idle timeout was overridden to 30s, got {fake_conn.sock.timeouts}",
                  fake_conn.sock.timeouts == [30])
    finally:
        http_mod.open_upstream = real_open_upstream


@test
def test_probe_openrouter_models_sets_a_30s_read_timeout(ctx: Ctx):
    import halo_harness.providers.http as http_mod
    from halo_harness.providers.databricks import probe_openrouter_models
    fake_conn = _FakeConn(json.dumps({"data": []}).encode("utf-8"))
    real_open_upstream = http_mod.open_upstream
    http_mod.open_upstream = lambda host, port, tls, *a, **kw: fake_conn
    try:
        models = probe_openrouter_models("https://openrouter.ai/api/v1", "sk-or-fake")
        ctx.check(f"parses fine, got {models}", models == [])
        ctx.check(f"a 30s read timeout was set, got {fake_conn.sock.timeouts}", fake_conn.sock.timeouts == [30])
    finally:
        http_mod.open_upstream = real_open_upstream


@test
def test_fetch_anthropic_models_sets_a_30s_read_timeout(ctx: Ctx):
    import halo_harness.providers.http as http_mod
    from halo_harness.providers.anthropic_catalog import fetch_anthropic_models
    fake_conn = _FakeConn(json.dumps({"data": []}).encode("utf-8"))
    real_open_upstream = http_mod.open_upstream
    http_mod.open_upstream = lambda host, port, tls, *a, **kw: fake_conn
    try:
        models = fetch_anthropic_models("https://api.anthropic.com", "sk-ant-fake")
        ctx.check(f"parses fine, got {models}", models == [])
        ctx.check(f"a 30s read timeout was set, got {fake_conn.sock.timeouts}", fake_conn.sock.timeouts == [30])
    finally:
        http_mod.open_upstream = real_open_upstream


# ---------------------------------------------------------------------------
# finding 12: the TUI's /models [refresh] and /dbx worker refreshes every
# ENABLED provider, not just Databricks; the "Databricks is not configured"
# wording stays reserved for /dbx specifically.
# ---------------------------------------------------------------------------

class _FakeTranscript:
    def __init__(self):
        self.notes: list = []

    def add_note(self, text, **_kw):
        self.notes.append(text)


class _FakeAppForModelsWorker:
    def __init__(self, state_dir):
        self.controller = SimpleNamespace(state_dir=state_dir)
        self.transcript = _FakeTranscript()
        self.notifications: list = []

    def call_from_thread(self, fn, *a, **kw):
        fn(*a, **kw)

    def notify(self, text, **kw):
        self.notifications.append(text)


@test
def test_models_refresh_worker_bare_reports_every_enabled_provider(ctx: Ctx):
    from halo_harness.providers.databricks import write_models_json
    from halo_harness.tui.slash import _models_refresh_worker
    with _Env() as env:
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        write_models_json(env.state_dir, [{"id": "vendor/x", "context_length": 1000, "max_output_tokens": 100}])
        app = _FakeAppForModelsWorker(env.state_dir)
        _models_refresh_worker(app, False)  # bare /models -- OpenRouter enabled, Databricks is not
        note = "\n\n".join(app.transcript.notes)
        ctx.check(f"mentions OpenRouter's cached count, got {note!r}", "OpenRouter: 1 model(s) cached" in note)
        ctx.check(f"never the Databricks-not-configured line for a plain /models, got {note!r}",
                  "Databricks is not configured" not in note)


@test
def test_models_refresh_worker_refresh_hits_both_enabled_providers(ctx: Ctx):
    from tests.helpers.mock_databricks import MockDatabricks
    from tests.helpers.mock_get_endpoints import MockGetEndpoints
    from halo_harness.tui.slash import _models_refresh_worker
    or_mock = MockGetEndpoints({"/api/v1/models": (200, {"data": [
        {"id": "deepseek/deepseek-v3.2", "context_length": 128000},
    ]})}).start()
    dbx_mock = MockDatabricks().start()
    dbx_mock.set_endpoints_catalog([])
    try:
        with _Env() as env:
            os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
            os.environ["BRIDGE_OPENROUTER_BASE_URL"] = or_mock.base_url + "/api/v1"
            os.environ["BRIDGE_DBX_BASE_URL"] = dbx_mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            app = _FakeAppForModelsWorker(env.state_dir)
            _models_refresh_worker(app, True)  # plain /models refresh (never typed /dbx)
            note = "\n\n".join(app.transcript.notes)
            ctx.check(f"OpenRouter refreshed, got {note!r}", "OpenRouter refreshed" in note)
            ctx.check(f"Databricks ALSO refreshed (not Databricks-only any more), got {note!r}",
                      "Refreshed 0 Databricks endpoint(s)" in note)
            ctx.check("the OpenRouter mock was actually hit",
                      any(r["path"] == "/api/v1/models" for r in or_mock.requests))
            ctx.check("the Databricks mock was also hit", len(dbx_mock.requests) > 0)
    finally:
        or_mock.stop()
        dbx_mock.stop()


@test
def test_dbx_alias_still_names_databricks_not_configured_explicitly(ctx: Ctx):
    from halo_harness.tui.slash import _models_refresh_worker
    with _Env() as env:
        app = _FakeAppForModelsWorker(env.state_dir)
        _models_refresh_worker(app, True, dbx_explicit=True)  # /dbx -- nothing configured at all
        note = "\n\n".join(app.transcript.notes)
        ctx.check(f"/dbx still names Databricks specifically, got {note!r}", "Databricks is not configured" in note)
        ctx.check(f"a warning toast was also posted, got {app.notifications}",
                  any("Databricks is not configured" in n for n in app.notifications))


@test
def test_statusline_command_never_inherits_provider_secrets(ctx: Ctx):
    """The user's statusLine script runs with the same stripped child env
    as hooks and tools: no provider key or token reaches it."""
    import subprocess
    from halo_harness import statusline
    captured = {}
    real_run = subprocess.run

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")

    saved = {k: os.environ.get(k) for k in ("OPENROUTER_API_KEY", "OPENROUTER_MANAGEMENT_KEY", "TYPESAFE_API_KEY")}
    os.environ["OPENROUTER_API_KEY"] = "sk-or-sentinel"
    os.environ["OPENROUTER_MANAGEMENT_KEY"] = "sk-or-mgmt-sentinel"
    os.environ["TYPESAFE_API_KEY"] = "ts-sentinel"
    subprocess.run = fake_run
    try:
        out = statusline.run_statusline_command("echo ok", {"model": "x"}, cwd=tempfile.gettempdir())
    finally:
        subprocess.run = real_run
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    ctx.check(f"command ran, got {out!r}", out == "ok")
    env = captured.get("env") or {}
    for k in ("OPENROUTER_API_KEY", "OPENROUTER_MANAGEMENT_KEY", "TYPESAFE_API_KEY"):
        ctx.check(f"{k} stripped from the statusLine child env", k not in env)


@test
def test_is_enabled_with_env_detects_a_settings_only_key(ctx: Ctx):
    """A key that lives only in a settings.json env block (so never in bare
    os.environ) enables the provider for the background workers when they
    pass the session's effective env."""
    from halo_harness.providers.enablement import is_enabled_with_env
    saved = os.environ.get("OPENROUTER_API_KEY")
    os.environ.pop("OPENROUTER_API_KEY", None)
    home = Path(tempfile.mkdtemp(prefix="h15b-enabled-env-"))
    saved_state = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_STATE_DIR"] = str(home / ".halo")
    try:
        ctx.check("enabled through the supplied env",
                  is_enabled_with_env("openrouter", {"OPENROUTER_API_KEY": "sk-or-from-settings"}) is True)
        ctx.check("not enabled when the supplied env lacks the key",
                  is_enabled_with_env("openrouter", {"PATH": "/usr/bin"}) is False)
    finally:
        if saved is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = saved
        if saved_state is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = saved_state


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
