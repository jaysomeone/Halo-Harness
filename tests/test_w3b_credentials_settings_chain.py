"""tests.test_w3b_credentials_settings_chain -- W3b findings 20/21
(docs/harness/RECOMMENDATIONS.md): OpenRouter/Anthropic/TypeSafe
`credentials_present` now gets the same settings-env-chain fallback
`resolve_databricks()` already had (a key living ONLY in a trusted
~/.claude/settings.json `env` block, never shell env/the harness's own env
file, must still be detected) -- and a dedicated end-to-end check that a
settings-only key actually reaches the catalog refreshers
(`refresh_openrouter_catalog_if_stale`/`refresh_anthropic_catalog_if_stale`),
not just the bare `resolve_*` functions, per finding 21's own "worth a
dedicated verification pass... rather than assuming it from the plumbing
alone."

Same `_EnvSandbox`-style convention as tests/test_dbx_work_routing.py's own
settings-chain tests (user-level ~/.claude/settings.json under a fresh fake
BRIDGE_TEST_HOME -- never trust-gated, so no ~/.claude.json project-trust
fixture is needed here).
"""
from __future__ import annotations

import json
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
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "TYPESAFE_API_KEY",
)


class _Env:
    """Fresh fake home (BRIDGE_TEST_HOME/BRIDGE_STATE_DIR), every provider
    env var scoped away, and a helper to plant a user-level
    ~/.claude/settings.json `env` block -- the ONE layer `load_settings_env_
    chain` always includes regardless of cwd/trust, matching test_dbx_work_
    routing.py's own pattern for the equivalent Databricks fallback."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in (("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR",
                                                         "BRIDGE_ENV_FILE") + _PROVIDER_ENV_VARS)}
        d = Path(tempfile.mkdtemp(prefix="w3b-settings-chain-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-such-env-file")
        for k in _PROVIDER_ENV_VARS:
            os.environ.pop(k, None)
        self.home = d
        self.state_dir = d / ".halo"
        return self

    def write_user_settings_env(self, env_block: dict) -> None:
        claude_dir = self.home / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "settings.json").write_text(json.dumps({"env": env_block}), encoding="utf-8")

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# Finding 20: resolve_openrouter/resolve_anthropic/TypeSafe's own
# credentials_present branch all get the settings-env-chain fallback.
# ---------------------------------------------------------------------------

@test
def test_resolve_openrouter_bare_call_falls_back_to_settings_env_chain(ctx: Ctx):
    from halo_harness.providers.config import resolve_openrouter
    with _Env() as env:
        env.write_user_settings_env({"OPENROUTER_API_KEY": "sk-or-from-settings"})
        ctx.check("never set in os.environ", "OPENROUTER_API_KEY" not in os.environ)
        orc = resolve_openrouter()
        ctx.check("resolved from the settings.json env block alone", orc is not None)
        if orc is not None:
            ctx.check(f"key came from settings, got {orc.api_key!r}", orc.api_key == "sk-or-from-settings")


@test
def test_resolve_anthropic_bare_call_falls_back_to_settings_env_chain(ctx: Ctx):
    from halo_harness.providers.config import resolve_anthropic
    with _Env() as env:
        env.write_user_settings_env({"ANTHROPIC_API_KEY": "sk-ant-from-settings"})
        anc = resolve_anthropic()
        ctx.check("resolved from the settings.json env block alone", anc is not None)
        if anc is not None:
            ctx.check(f"key came from settings, got {anc.api_key!r}", anc.api_key == "sk-ant-from-settings")


@test
def test_credentials_present_typesafe_falls_back_to_settings_env_chain(ctx: Ctx):
    from halo_harness.providers.enablement import credentials_present
    with _Env() as env:
        env.write_user_settings_env({"TYPESAFE_API_KEY": "ts-from-settings"})
        ctx.check("TypeSafe detected from the settings.json env block alone",
                  credentials_present("typesafe") is True)


@test
def test_settings_fallback_never_fires_for_a_caller_supplied_env(ctx: Ctx):
    """The fallback is gated on `env is os.environ` (a BARE call) -- a
    caller that already passed its OWN merged env dict (a real session's
    `Settings.effective_env`, or a listing surface's `listing_effective_
    env()`) must never have it silently re-derived a second, possibly
    different way."""
    from halo_harness.providers.config import resolve_openrouter
    with _Env() as env:
        env.write_user_settings_env({"OPENROUTER_API_KEY": "sk-or-from-settings"})
        orc = resolve_openrouter(env={})  # caller's own (empty) env -- not os.environ
        ctx.check("an explicit (even empty) env dict is never augmented by the settings chain",
                  orc is None)


# ---------------------------------------------------------------------------
# Finding 21: a settings-only key actually reaches the catalog refreshers,
# verified end to end against a mock upstream (not just resolve_* alone).
# ---------------------------------------------------------------------------

@test
def test_refresh_openrouter_catalog_if_stale_reaches_a_settings_only_key(ctx: Ctx):
    from halo_harness.providers.databricks import load_models_json, refresh_openrouter_catalog_if_stale
    mock = MockGetEndpoints({"/api/v1/models": (200, {"data": [
        {"id": "deepseek/deepseek-v3.2", "context_length": 128000},
    ]})}).start()
    try:
        with _Env() as env:
            env.write_user_settings_env({"OPENROUTER_API_KEY": "sk-or-from-settings"})
            # base_url override stays a plain env var -- only the API KEY
            # itself (the credential finding 21 is actually about) lives in
            # settings here; the bare call (env=None, the refresher's own
            # default) must still reach it via resolve_openrouter()'s new
            # settings-chain fallback.
            os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url + "/api/v1"
            ctx.check("models.json starts empty", load_models_json(env.state_dir) == {})
            ok = refresh_openrouter_catalog_if_stale(env.state_dir)
            ctx.check(f"refresh reports success from a settings-only key, got {ok!r}", ok is True)
            ctx.check("the mock's /models endpoint was actually hit",
                      any(r["path"] == "/api/v1/models" for r in mock.requests))
    finally:
        mock.stop()


@test
def test_refresh_anthropic_catalog_if_stale_reaches_a_settings_only_key(ctx: Ctx):
    from halo_harness.providers.anthropic_catalog import load_ant_models_json, refresh_anthropic_catalog_if_stale
    mock = MockGetEndpoints({"/v1/models": (200, {"data": [
        {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5"},
    ]})}).start()
    try:
        with _Env() as env:
            env.write_user_settings_env({"ANTHROPIC_API_KEY": "sk-ant-from-settings"})
            os.environ["BRIDGE_ANTHROPIC_BASE_URL"] = mock.base_url
            ok = refresh_anthropic_catalog_if_stale(env.state_dir)
            ctx.check(f"refresh reports success from a settings-only key, got {ok!r}", ok is True)
            cached = load_ant_models_json(env.state_dir)
            ctx.check(f"cache written, got {sorted(cached)}", set(cached) == {"claude-opus-5-5"})
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
