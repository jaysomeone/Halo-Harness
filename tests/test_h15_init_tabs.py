"""tests.test_h15_init_tabs -- H15 Part A: `halo init`'s tabbed
provider setup. Pure-logic pieces (`init_providers.tab_credential_state`/
`save_tab_credentials`/`refresh_tab_catalog`) and `cmd_init`'s own
tabs-vs-sequential-picker wiring (the numbered/sequential fallback with no
real terminal, and when the tabs app itself fails to run). The Textual
pilot tests (tab navigation, masked credentials, live status, mocked
reachability, the catalog cache write) live in `test_tui.py`, per house
convention.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_PROVIDER_ENV_VARS = (
    "OPENROUTER_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN", "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "BRIDGE_ANTHROPIC_BASE_URL",
    "TYPESAFE_API_KEY", "BRIDGE_TEST_CC_AUTH_STATUS",
)


class _Env:
    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       (("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE") + _PROVIDER_ENV_VARS)}
        d = Path(tempfile.mkdtemp(prefix="h15-init-tabs-logic-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = '{"loggedIn": false}'
        for k in _PROVIDER_ENV_VARS:
            if k != "BRIDGE_TEST_CC_AUTH_STATUS":
                os.environ.pop(k, None)
        self.home = d
        self.state_dir = d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _IsattyProxy:
    """Proxies every attribute to the REAL stream except `isatty()` --
    `cmd_init` builds its own `rich.console.Console()` tied to the live
    `sys.stdout` (unlike a function that takes an explicit `console=`
    argument), so a fake stdin/stdout here must still support `.write()`/
    `.flush()`/etc. for real, not just `.isatty()`."""

    def __init__(self, value: bool, real):
        self._value = value
        self._real = real

    def isatty(self) -> bool:
        return self._value

    def __getattr__(self, name):
        return getattr(self._real, name)


@contextlib.contextmanager
def _fake_tty(stdin_tty: bool, stdout_tty: bool):
    real_stdin, real_stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = _IsattyProxy(stdin_tty, real_stdin), _IsattyProxy(stdout_tty, real_stdout)
    try:
        yield
    finally:
        sys.stdin, sys.stdout = real_stdin, real_stdout


def _args(**kw):
    base = dict(yes=False, provider=None, preset=None, model=None, no_live=True, no_fixes=True, team=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _console():
    from rich.console import Console
    return Console(file=io.StringIO(), width=200)


# ---------------------------------------------------------------------------
# tab_credential_state
# ---------------------------------------------------------------------------

@test
def test_tab_state_databricks_not_set_up_needs_both_fields(ctx: Ctx):
    from halo_harness.init_providers import tab_credential_state
    with _Env():
        state = tab_credential_state("databricks")
        ctx.check(f"not configured, got {state}", state["configured"] is False)
        names = {f["name"] for f in state["fields"]}
        ctx.check(f"needs host and token, got {names}", names == {"host", "token"})


@test
def test_tab_state_databricks_configured_shows_masked_source(ctx: Ctx):
    from halo_harness.init_providers import tab_credential_state
    with _Env():
        os.environ["DATABRICKS_HOST"] = "https://fake-ws.cloud.databricks.com"
        os.environ["DATABRICKS_TOKEN"] = "fake-token-value"
        state = tab_credential_state("databricks")
        ctx.check(f"configured, got {state}", state["configured"] is True)
        ctx.check(f"host visible (not secret), got {state}", "fake-ws.cloud.databricks.com" in state["masked"])
        ctx.check(f"token MASKED, got {state}", "fake-token-value" not in state["masked"])
        ctx.check(f"no fields left to fill, got {state}", state["fields"] == [])


@test
def test_tab_state_openrouter_anthropic_typesafe_shapes(ctx: Ctx):
    from halo_harness.init_providers import tab_credential_state
    with _Env():
        for provider, env_key in (("openrouter", "OPENROUTER_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY"),
                                    ("typesafe", "TYPESAFE_API_KEY")):
            not_set = tab_credential_state(provider)
            ctx.check(f"{provider} not set up, got {not_set}", not_set["configured"] is False)
            ctx.check(f"{provider} needs a single 'key' field, got {not_set}",
                      [f["name"] for f in not_set["fields"]] == ["key"])
            os.environ[env_key] = "fake-secret-value"
            configured = tab_credential_state(provider)
            ctx.check(f"{provider} now configured, got {configured}", configured["configured"] is True)
            ctx.check(f"{provider} masked, got {configured}", "fake-secret-value" not in configured["masked"])
            os.environ.pop(env_key, None)


@test
def test_tab_state_claude_reflects_login(ctx: Ctx):
    import json as json_mod
    from halo_harness.init_providers import tab_credential_state
    with _Env():
        ctx.check("not logged in by default", tab_credential_state("claude")["configured"] is False)
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json_mod.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        ctx.check("logged in once the fake status says so", tab_credential_state("claude")["configured"] is True)


# ---------------------------------------------------------------------------
# save_tab_credentials
# ---------------------------------------------------------------------------

@test
def test_save_databricks_needs_both_fields(ctx: Ctx):
    from halo_harness.init_providers import save_tab_credentials
    with _Env():
        ok, msg = save_tab_credentials("databricks", {"host": "https://x.cloud.databricks.com", "token": ""})
        ctx.check(f"refused without a token, got {(ok, msg)}", ok is False)
        ctx.check("neither var got set", "DATABRICKS_TOKEN" not in os.environ)


@test
def test_save_databricks_writes_env_file_and_process_env(ctx: Ctx):
    from halo_harness.init_providers import save_tab_credentials
    with _Env() as env:
        ok, msg = save_tab_credentials("databricks", {"host": "https://x.cloud.databricks.com", "token": "tok-1"})
        ctx.check(f"accepted, got {(ok, msg)}", ok is True)
        ctx.check("live env updated", os.environ.get("DATABRICKS_TOKEN") == "tok-1")
        env_file = Path(os.environ["BRIDGE_ENV_FILE"])
        ctx.check("env file actually written", env_file.exists())
        content = env_file.read_text(encoding="utf-8")
        ctx.check(f"host written, got {content!r}", "DATABRICKS_HOST=https://x.cloud.databricks.com" in content)
        ctx.check(f"token written, got {content!r}", "DATABRICKS_TOKEN=tok-1" in content)


@test
def test_save_openrouter_blank_key_refused(ctx: Ctx):
    from halo_harness.init_providers import save_tab_credentials
    with _Env():
        ok, msg = save_tab_credentials("openrouter", {"key": "   "})
        ctx.check(f"refused, got {(ok, msg)}", ok is False)


@test
def test_save_claude_reflects_login_without_writing_anything(ctx: Ctx):
    import json as json_mod
    from halo_harness.init_providers import save_tab_credentials
    with _Env():
        ok, msg = save_tab_credentials("claude", {})
        ctx.check(f"refused -- not logged in, got {(ok, msg)}", ok is False)
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json_mod.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        ok2, msg2 = save_tab_credentials("claude", {})
        ctx.check(f"accepted once logged in, got {(ok2, msg2)}", ok2 is True)
        env_file = Path(os.environ["BRIDGE_ENV_FILE"])
        ctx.check("claude never writes an env file (nothing to store)", not env_file.exists())


# ---------------------------------------------------------------------------
# refresh_tab_catalog
# ---------------------------------------------------------------------------

@test
def test_refresh_tab_catalog_openrouter_writes_the_cache(ctx: Ctx):
    import halo_harness.providers.databricks as dbx_mod
    from halo_harness.init_providers import refresh_tab_catalog
    from halo_harness.providers.databricks import load_models_json

    def _fake_probe(base_url, api_key):
        return [{"id": "vendor/x", "context_length": 1000, "max_output_tokens": 100}]

    with _Env() as env:
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
        real = dbx_mod.probe_openrouter_models
        dbx_mod.probe_openrouter_models = _fake_probe
        try:
            ok, note = refresh_tab_catalog("openrouter")
            ctx.check(f"ok, got {(ok, note)}", ok is True)
            cached = load_models_json(env.state_dir)
            ctx.check(f"cached for real, got {list(cached)}", "vendor/x" in cached)
        finally:
            dbx_mod.probe_openrouter_models = real


@test
def test_refresh_tab_catalog_not_configured_is_a_clean_failure(ctx: Ctx):
    from halo_harness.init_providers import refresh_tab_catalog
    with _Env():
        ok, note = refresh_tab_catalog("databricks")
        ctx.check(f"not ok, got {(ok, note)}", ok is False)


@test
def test_refresh_tab_catalog_noop_for_claude_and_typesafe(ctx: Ctx):
    from halo_harness.init_providers import refresh_tab_catalog
    with _Env():
        for provider in ("claude", "typesafe"):
            ok, note = refresh_tab_catalog(provider)
            ctx.check(f"{provider}: a harmless no-op, got {(ok, note)}", ok is True)


# ---------------------------------------------------------------------------
# cmd_init wiring: the tabs app only runs on a REAL terminal; a piped/non-
# tty run (every scripted test, every CI invocation) keeps using the
# sequential picker + its own numbered fallback, byte for byte unchanged.
# ---------------------------------------------------------------------------

@test
def test_numbered_fallback_without_a_tty_never_launches_the_tabs_app(ctx: Ctx):
    import halo_harness.init_cli as init_cli
    called = []
    real_run_tabs = None
    import halo_harness.tui.dialogs.init_tabs as tabs_mod
    real_run_tabs = tabs_mod.run_init_tabs

    def _poison(*a, **kw):
        called.append(True)
        raise AssertionError("the tabs app must never run with no real terminal")

    tabs_mod.run_init_tabs = _poison
    try:
        with _Env():
            with _fake_tty(stdin_tty=False, stdout_tty=False):
                rc = init_cli.cmd_init(["--no-live", "--no-fixes", "--yes"])
            ctx.check(f"exit 0, got {rc}", rc == 0)
            ctx.check("the tabs app was never even attempted", called == [])
    finally:
        tabs_mod.run_init_tabs = real_run_tabs


@test
def test_tabs_app_used_on_a_real_terminal_feeds_the_rest_of_init(ctx: Ctx):
    """Scripts the tabs app itself (never a real Textual run here -- this
    test is about cmd_init's OWN wiring, not the widget) to prove its
    result actually drives the default-model/permission-mode/summary steps
    that follow, exactly like the old one-provider-at-a-time loop did."""
    import halo_harness.init_cli as init_cli
    import halo_harness.tui.dialogs.init_tabs as tabs_mod
    from halo_harness.theme import get_config_value

    class _FakeTabsApp:
        configured_this_run = ["openrouter"]
        catalog_notes = {"openrouter": "3 model(s) cached"}
        written = []

    real_run_tabs = tabs_mod.run_init_tabs
    real_perm_mode = init_cli._step_pick_permission_mode
    # 1.0.1 part 2 fixpass finding 6: `_run_init_tabs` now calls
    # `run_init_tabs(team=..., no_live=...)` -- `**kw` so this stand-in
    # keeps accepting whatever `_run_init_tabs` passes.
    tabs_mod.run_init_tabs = lambda **kw: _FakeTabsApp()
    # The permission-mode step is its OWN separate interactive picker --
    # stubbed here so this test (a real-tty simulation) only ever exercises
    # the tabs-vs-sequential-loop DECISION this test is actually about,
    # never a second real Textual sub-app with nothing driving it.
    init_cli._step_pick_permission_mode = lambda args, console: ""
    try:
        with _Env():
            os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
            with _fake_tty(stdin_tty=True, stdout_tty=True):
                rc = init_cli.cmd_init(["--no-live", "--no-fixes"])
            ctx.check(f"exit 0, got {rc}", rc == 0)
            ctx.check("the default model picked up the tabs app's own provider",
                      str(get_config_value("model", default="")).startswith("or:"))
    finally:
        tabs_mod.run_init_tabs = real_run_tabs
        init_cli._step_pick_permission_mode = real_perm_mode


@test
def test_tabs_app_failure_falls_back_to_the_sequential_picker(ctx: Ctx):
    """The tabs app raising (a Textual runtime failure) must fall back to
    the OLD one-provider-at-a-time loop instead of crashing `init`
    outright -- same "interactive picker failed" shape every other picker
    in this file already uses."""
    import halo_harness.init_cli as init_cli
    import halo_harness.tui.dialogs.init_tabs as tabs_mod

    def _boom(**kw):  # finding 6: see the previous test's own **kw note
        raise RuntimeError("no real terminal available (simulated)")

    real_run_tabs = tabs_mod.run_init_tabs
    tabs_mod.run_init_tabs = _boom
    real_select = init_cli._step_select_provider
    select_calls = []

    def _fake_select(args, console, *, header):
        select_calls.append(header)
        return "done"

    init_cli._step_select_provider = _fake_select
    real_perm_mode = init_cli._step_pick_permission_mode
    init_cli._step_pick_permission_mode = lambda args, console: ""
    try:
        with _Env():
            with _fake_tty(stdin_tty=True, stdout_tty=True):
                rc = init_cli.cmd_init(["--no-live", "--no-fixes"])
            ctx.check(f"exit 0, got {rc}", rc == 0)
            ctx.check("fell back to the sequential picker (it was actually called)", select_calls)
    finally:
        tabs_mod.run_init_tabs = real_run_tabs
        init_cli._step_select_provider = real_select
        init_cli._step_pick_permission_mode = real_perm_mode


# ---------------------------------------------------------------------------
# 1.0.1 part 2 fixpass finding 5: a TypeSafe-only `configured_this_run`
# must not KeyError (PROVIDER_LABEL/PROVIDER_DEFAULT_MODEL have no entry
# for it -- TypeSafe is in TAB_PROVIDERS but not PROVIDERS).
# ---------------------------------------------------------------------------

@test
def test_run_init_tabs_skips_typesafe_in_the_default_model_loop(ctx: Ctx):
    import halo_harness.init_cli as init_cli
    import halo_harness.tui.dialogs.init_tabs as tabs_mod

    class _FakeTypesafeOnlyApp:
        configured_this_run = ["typesafe"]
        catalog_notes: dict = {}
        written: list = []
        team_warnings: list = []

    real_run_tabs = tabs_mod.run_init_tabs
    real_perm_mode = init_cli._step_pick_permission_mode
    tabs_mod.run_init_tabs = lambda **kw: _FakeTypesafeOnlyApp()
    init_cli._step_pick_permission_mode = lambda args, console: ""
    try:
        with _Env():
            os.environ["TYPESAFE_API_KEY"] = "fake-typesafe-key"
            with _fake_tty(stdin_tty=True, stdout_tty=True):
                # Before the fix: PROVIDER_LABEL["typesafe"] raised KeyError
                # here, taking the whole `init` run down with it.
                rc = init_cli.cmd_init(["--no-live", "--no-fixes"])
            ctx.check(f"exit 0 (no KeyError), got {rc}", rc == 0)
    finally:
        tabs_mod.run_init_tabs = real_run_tabs
        init_cli._step_pick_permission_mode = real_perm_mode


# ---------------------------------------------------------------------------
# 1.0.1 part 2 fixpass finding 6: token-only Databricks save (a host already
# discovered -- Claude Code's own work-env signal, or team.json) must
# succeed; team.json's gateway/role preferences apply from the tabs path too.
# ---------------------------------------------------------------------------

@test
def test_tab_state_databricks_known_host_renders_token_only_and_team_fallback(ctx: Ctx):
    from halo_harness.init_providers import tab_credential_state
    with _Env():
        # No Claude-Code-settings host discoverable -- team_cfg is the ONLY
        # source for the host hint.
        state = tab_credential_state("databricks", team_cfg={"host": "https://team-ws.cloud.databricks.com"})
        ctx.check(f"not configured, got {state}", state["configured"] is False)
        ctx.check(f"only a token field (host already known from team_cfg), got {state['fields']}",
                  [f["name"] for f in state["fields"]] == ["token"])
        ctx.check(f"known_host is the team host, got {state}",
                  state["known_host"] == "https://team-ws.cloud.databricks.com")
        ctx.check(f"source names the team config, got {state}", state["source"] == "the team config")


@test
def test_tab_state_databricks_discovered_host_wins_over_team_cfg(ctx: Ctx):
    from halo_harness.init_providers import tab_credential_state
    with _Env():
        os.environ["ANTHROPIC_MODEL"] = "claude-opus-4-6"
        os.environ["DATABRICKS_HOST"] = "https://discovered-ws.cloud.databricks.com"
        state = tab_credential_state("databricks", team_cfg={"host": "https://team-ws.cloud.databricks.com"})
        ctx.check(f"the DISCOVERED host wins, got {state}",
                  state["known_host"] == "https://discovered-ws.cloud.databricks.com")
        ctx.check(f"source names Claude Code's settings, got {state}", state["source"] == "Claude Code's settings")


@test
def test_save_databricks_token_only_falls_back_to_the_known_discovered_host(ctx: Ctx):
    """The exact finding 6 bug: `tab_credential_state` renders ONLY a token
    field once a host is already known, so `_collect_values` (and this
    test, standing in for it) never has a "host" key to pass at all --
    `save_tab_credentials` used to demand both outright and refuse every
    such save."""
    from halo_harness.init_providers import save_tab_credentials
    with _Env() as env:
        os.environ["ANTHROPIC_MODEL"] = "claude-opus-4-6"
        os.environ["DATABRICKS_HOST"] = "https://discovered-ws.cloud.databricks.com"
        ok, msg = save_tab_credentials("databricks", {"token": "tok-discovered"})
        ctx.check(f"accepted (host recovered from discovery), got {(ok, msg)}", ok is True)
        env_file = Path(os.environ["BRIDGE_ENV_FILE"])
        content = env_file.read_text(encoding="utf-8")
        ctx.check(f"the discovered host was written, got {content!r}",
                  "DATABRICKS_HOST=https://discovered-ws.cloud.databricks.com" in content)
        ctx.check(f"the token was written, got {content!r}", "DATABRICKS_TOKEN=tok-discovered" in content)


@test
def test_save_databricks_token_only_falls_back_to_the_team_host(ctx: Ctx):
    from halo_harness.init_providers import save_tab_credentials
    with _Env():
        ok, msg = save_tab_credentials("databricks", {"token": "tok-team"},
                                        team_cfg={"host": "https://team-ws.cloud.databricks.com"})
        ctx.check(f"accepted (host recovered from team_cfg), got {(ok, msg)}", ok is True)
        env_file = Path(os.environ["BRIDGE_ENV_FILE"])
        content = env_file.read_text(encoding="utf-8")
        ctx.check(f"the team host was written, got {content!r}",
                  "DATABRICKS_HOST=https://team-ws.cloud.databricks.com" in content)


@test
def test_save_databricks_still_refuses_with_no_host_anywhere(ctx: Ctx):
    """Regression guard: the fallback chain must not silently invent a host
    -- genuinely missing everywhere is still refused."""
    from halo_harness.init_providers import save_tab_credentials
    with _Env():
        ok, msg = save_tab_credentials("databricks", {"token": "tok-only"})
        ctx.check(f"still refused, got {(ok, msg)}", ok is False)
        ctx.check(f"names what's needed, got {msg!r}", "DATABRICKS_HOST" in msg)


@test
def test_save_databricks_applies_team_gateway_and_role_preferences(ctx: Ctx):
    from halo_harness.init_providers import save_tab_credentials
    from halo_harness.theme import get_config_value
    with _Env():
        team_cfg = {
            "host": "https://team-ws.cloud.databricks.com",
            "gateway_preference": {"databricks-claude-opus-4-6": "anthropic"},
            "roles": {"coder": "dbx:databricks-glm-5-3"},
        }
        ok, _msg = save_tab_credentials("databricks", {"token": "tok-prefs"}, team_cfg=team_cfg)
        ctx.check(f"accepted, got {ok}", ok is True)
        ctx.check("gateway preference seeded into config.json",
                  get_config_value("databricks.gateway.databricks-claude-opus-4-6", default=None) == "anthropic")
        roles = get_config_value("roles", default={})
        ctx.check(f"role preference seeded into config.json, got {roles}", roles.get("coder") == "dbx:databricks-glm-5-3")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
