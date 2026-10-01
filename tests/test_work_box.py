"""tests.test_work_box -- H8 scope F: ucode-settings.json discovery,
catalog-age reporting, and `rolo-claude doctor --work`'s output shape with
no/an unreachable Databricks host (never a real VPN call in this suite)."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials, ensure_scoped_state_dir_once

ensure_scoped_state_dir_once()
ensure_default_provider_credentials()

test, TESTS = new_registry()


@test
def test_load_ucode_settings_top_level_keys(ctx: Ctx):
    from rolo_claude.providers.config import load_ucode_settings
    d = Path(tempfile.mkdtemp(prefix="ucode-"))
    p = d / "ucode-settings.json"
    p.write_text(json.dumps({"gateway_url": "https://x.cloud.databricks.com", "token": "abc123"}), encoding="utf-8")
    cfg = load_ucode_settings(p)
    ctx.check("parsed a config", cfg is not None)
    ctx.check("host correct", cfg.host == "https://x.cloud.databricks.com")
    ctx.check("token correct", cfg.token == "abc123")


@test
def test_load_ucode_settings_nested_gateway_key(ctx: Ctx):
    from rolo_claude.providers.config import load_ucode_settings
    d = Path(tempfile.mkdtemp(prefix="ucode-nested-"))
    p = d / "ucode-settings.json"
    p.write_text(json.dumps({"gateway": {"host": "y.cloud.databricks.com", "api_token": "tok"}}), encoding="utf-8")
    cfg = load_ucode_settings(p)
    ctx.check("parsed a nested config", cfg is not None)
    ctx.check("host from nested key", cfg.host == "y.cloud.databricks.com")
    ctx.check("token from nested key", cfg.token == "tok")


@test
def test_load_ucode_settings_missing_or_incomplete_is_none(ctx: Ctx):
    from rolo_claude.providers.config import load_ucode_settings
    d = Path(tempfile.mkdtemp(prefix="ucode-missing-"))
    ctx.check("missing file -> None", load_ucode_settings(d / "nope.json") is None)
    incomplete = d / "incomplete.json"
    incomplete.write_text(json.dumps({"host": "only-a-host.databricks.com"}), encoding="utf-8")
    ctx.check("host with no token -> None", load_ucode_settings(incomplete) is None)
    garbage = d / "garbage.json"
    garbage.write_text("{not json", encoding="utf-8")
    ctx.check("unparseable JSON -> None", load_ucode_settings(garbage) is None)


@test
def test_h9b_f31_load_ucode_settings_reads_claude_settings_shaped_env_block(ctx: Ctx):
    """H9 whole-tree review finding 31: a ucode-settings.json shaped like a
    Claude Code `--settings` file (nested `env` block, ANTHROPIC_* names)
    must resolve, not just the gateway-config key spellings."""
    from rolo_claude.providers.config import load_ucode_settings
    d = Path(tempfile.mkdtemp(prefix="ucode-env-"))
    p = d / "ucode-settings.json"
    p.write_text(json.dumps({"env": {
        "ANTHROPIC_BASE_URL": "https://z.cloud.databricks.com",
        "ANTHROPIC_AUTH_TOKEN": "env-tok-1",
    }}), encoding="utf-8")
    cfg = load_ucode_settings(p)
    ctx.check("parsed a config from the env block", cfg is not None)
    ctx.check("host from env.ANTHROPIC_BASE_URL", cfg.host == "https://z.cloud.databricks.com")
    ctx.check("token from env.ANTHROPIC_AUTH_TOKEN", cfg.token == "env-tok-1")


@test
def test_h9b_f31_load_ucode_settings_top_level_anthropic_keys_also_resolve(ctx: Ctx):
    """H9 finding 31: ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN are plain
    candidate key names now, so they resolve top-level too, not only when
    nested under "env" -- some hand-edited or non-Claude-Code writer of
    this file could plausibly put them at either level."""
    from rolo_claude.providers.config import load_ucode_settings
    d = Path(tempfile.mkdtemp(prefix="ucode-env-top-"))
    p = d / "ucode-settings.json"
    p.write_text(json.dumps({
        "ANTHROPIC_BASE_URL": "https://w.cloud.databricks.com",
        "ANTHROPIC_AUTH_TOKEN": "env-tok-2",
    }), encoding="utf-8")
    cfg = load_ucode_settings(p)
    ctx.check("parsed a config", cfg is not None)
    ctx.check("host from top-level ANTHROPIC_BASE_URL", cfg.host == "https://w.cloud.databricks.com")
    ctx.check("token from top-level ANTHROPIC_AUTH_TOKEN", cfg.token == "env-tok-2")


@test
def test_h9b_f31_load_ucode_settings_runs_api_key_helper_when_token_missing(ctx: Ctx):
    """H9 finding 31: an `apiKeyHelper` command (Claude Code `--settings`
    shape) is run and its stdout used as the token when no key-spelling
    candidate supplies one directly."""
    from rolo_claude.providers.config import load_ucode_settings
    d = Path(tempfile.mkdtemp(prefix="ucode-helper-"))
    p = d / "ucode-settings.json"
    p.write_text(json.dumps({
        "host": "helper.cloud.databricks.com",
        "apiKeyHelper": "echo helper-tok-3",
    }), encoding="utf-8")
    cfg = load_ucode_settings(p)
    ctx.check("parsed a config via apiKeyHelper", cfg is not None)
    ctx.check("host unaffected", cfg.host == "helper.cloud.databricks.com")
    ctx.check("token from apiKeyHelper stdout, stripped", cfg.token == "helper-tok-3")


@test
def test_h9b_f31_load_ucode_settings_api_key_helper_failure_is_none(ctx: Ctx):
    """H9 finding 31: a failing (nonzero exit) apiKeyHelper must leave the
    token unset -- same "None if incomplete" contract as a missing key,
    never a raised exception from the subprocess call."""
    from rolo_claude.providers.config import load_ucode_settings
    d = Path(tempfile.mkdtemp(prefix="ucode-helper-fail-"))
    p = d / "ucode-settings.json"
    p.write_text(json.dumps({
        "host": "helper-fail.cloud.databricks.com",
        "apiKeyHelper": "this-command-does-not-exist-h9bf31",
    }), encoding="utf-8")
    ctx.check("failing helper -> None overall", load_ucode_settings(p) is None)


@test
def test_h9b_f31_load_ucode_settings_does_not_run_helper_when_key_token_found(ctx: Ctx):
    """H9 finding 31: apiKeyHelper is a LAST resort -- if any key-spelling
    candidate already supplied a token, the helper must never run (and
    must never override that token even if it would produce a different
    value)."""
    from rolo_claude.providers.config import load_ucode_settings
    d = Path(tempfile.mkdtemp(prefix="ucode-helper-skip-"))
    p = d / "ucode-settings.json"
    p.write_text(json.dumps({
        "host": "skip.cloud.databricks.com",
        "token": "key-tok-wins",
        "apiKeyHelper": "echo should-never-be-used",
    }), encoding="utf-8")
    cfg = load_ucode_settings(p)
    ctx.check("parsed a config", cfg is not None)
    ctx.check("key-spelling token wins over apiKeyHelper", cfg.token == "key-tok-wins")


@test
def test_resolve_databricks_falls_back_to_ucode_settings(ctx: Ctx):
    """Step 5 of the discovery chain: when nothing earlier (explicit
    BRIDGE_DBX_*, ANTHROPIC_*, DATABRICKS_*) resolves, ucode-settings.json
    under the (test) claude config dir wins before ~/.databrickscfg."""
    from rolo_claude.providers.config import resolve_databricks
    d = Path(tempfile.mkdtemp(prefix="ucode-resolve-"))
    claude_dir = d / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "ucode-settings.json").write_text(
        json.dumps({"gateway_url": "https://z.cloud.databricks.com", "token": "ztoken"}), encoding="utf-8")

    old_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(d)
    try:
        cfg = resolve_databricks(env={})  # an EMPTY env -- nothing wins before step 5
        ctx.check("resolved via ucode-settings.json", cfg is not None and cfg.host == "https://z.cloud.databricks.com")
        ctx.check("token from ucode-settings.json", cfg is not None and cfg.token == "ztoken")
    finally:
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home


@test
def test_resolve_databricks_explicit_override_beats_ucode_settings(ctx: Ctx):
    from rolo_claude.providers.config import resolve_databricks
    d = Path(tempfile.mkdtemp(prefix="ucode-precedence-"))
    claude_dir = d / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "ucode-settings.json").write_text(
        json.dumps({"gateway_url": "https://loser.cloud.databricks.com", "token": "loser"}), encoding="utf-8")

    old_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(d)
    try:
        cfg = resolve_databricks(env={"BRIDGE_DBX_BASE_URL": "https://winner.cloud.databricks.com",
                                       "BRIDGE_DBX_TOKEN": "winner"})
        ctx.check("explicit override still wins", cfg.host == "https://winner.cloud.databricks.com")
    finally:
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home


@test
def test_catalog_ages_reports_missing_and_fresh(ctx: Ctx):
    from rolo_claude.doctor import _check_catalog_ages
    d = Path(tempfile.mkdtemp(prefix="catalog-ages-"))
    old_state = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    try:
        lines = _check_catalog_ages()
        ctx.check(f"one line per catalog file, got {len(lines)}", len(lines) == 3)
        ctx.check("all missing/never-cached -- WARN, not MISSING (vendored fallback still applies)",
                  all("[WARN]" in l for l in lines))
        (d / "models.json").write_text("{}", encoding="utf-8")
        lines2 = _check_catalog_ages()
        ok_lines = [l for l in lines2 if "models.json" in l]
        ctx.check("a freshly-written models.json is now OK", ok_lines and "[OK]" in ok_lines[0])
    finally:
        if old_state is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = old_state


@test
def test_doctor_work_no_databricks_configured(ctx: Ctx):
    from rolo_claude.doctor import run_work_checks
    d = Path(tempfile.mkdtemp(prefix="work-none-"))
    _cred_keys = ("BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                  "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")
    _saved = {k: os.environ.get(k) for k in (("BRIDGE_TEST_HOME", "BRIDGE_ENV_FILE") + _cred_keys)}
    os.environ["BRIDGE_TEST_HOME"] = str(d)
    os.environ["BRIDGE_ENV_FILE"] = str(d / "no-such-env-file")
    for key in _cred_keys:
        os.environ.pop(key, None)
    try:
        lines, ok = run_work_checks()
        ctx.check("not ok (nothing configured)", ok is False)
        ctx.check("Databricks config line present", any("Databricks config" in l for l in lines))
        ctx.check("no live network call attempted (clean MISSING, no traceback)",
                  all("Traceback" not in l for l in lines))
    finally:
        # H15 part 2 addendum 3.1 fallout: this used to bare-pop the
        # credential keys above with no restore at all, permanently wiping
        # this file's own module-level default credentials (tests/helpers/
        # provider_env_defaults.py) for every test registered after this
        # one -- is_enabled("databricks") then refused every dbx: ref for
        # the REST of this file's run.
        for var, old in _saved.items():
            if old is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = old


@test
def test_doctor_work_unreachable_host_reports_vpn_hint(ctx: Ctx):
    """A syntactically valid but non-routable Databricks-shaped host: the
    VPN hint text must appear, and nothing downstream (token check, open
    questions) should raise just because the connect failed."""
    from rolo_claude.doctor import run_work_checks
    d = Path(tempfile.mkdtemp(prefix="work-unreachable-"))
    os.environ["BRIDGE_TEST_HOME"] = str(d)
    os.environ["BRIDGE_DBX_BASE_URL"] = "https://this-host-does-not-exist-rolo-claude-test.databricks.com"
    os.environ["BRIDGE_DBX_TOKEN"] = "fake-token-for-a-test"
    try:
        lines, ok = run_work_checks()
        ctx.check("not ok (unreachable)", ok is False)
        joined = "\n".join(lines)
        ctx.check("VPN hint present", "VPN" in joined)
        ctx.check("no traceback leaked into the output", "Traceback" not in joined)
    finally:
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_DBX_BASE_URL", None)
        os.environ.pop("BRIDGE_DBX_TOKEN", None)


# ---- H9 whole-tree review finding 33: probe the CONFIGURED model, with
# the production header -- never a real network call in these either. ----

@test
def test_h9b_f33_configured_databricks_thinking_model_reads_routes_json_default(ctx: Ctx):
    from rolo_claude.doctor import _configured_databricks_thinking_model

    state_dir = Path(tempfile.mkdtemp(prefix="work-f33-state-"))
    (state_dir / "routes.json").write_text(
        json.dumps({"default": "dbx:databricks-deepseek-v4-1-flash"}), encoding="utf-8")
    got = _configured_databricks_thinking_model(state_dir)
    ctx.check(f"resolved the configured thinking-family model, got {got!r}",
              got == "databricks-deepseek-v4-1-flash")


@test
def test_h9b_f33_configured_model_none_when_not_databricks_or_not_thinking_family(ctx: Ctx):
    from rolo_claude.doctor import _configured_databricks_thinking_model

    state_dir = Path(tempfile.mkdtemp(prefix="work-f33-state2-"))
    (state_dir / "routes.json").write_text(json.dumps({"default": "or:some-vendor/model"}), encoding="utf-8")
    ctx.check("an OpenRouter default is not a databricks thinking model -- None",
              _configured_databricks_thinking_model(state_dir) is None)

    state_dir2 = Path(tempfile.mkdtemp(prefix="work-f33-state3-"))
    (state_dir2 / "routes.json").write_text(
        json.dumps({"default": "dbx:databricks-claude-sonnet-4-5"}), encoding="utf-8")
    ctx.check("a non-thinking-family databricks model (Claude) is not thinking-family -- None",
              _configured_databricks_thinking_model(state_dir2) is None)


@test
def test_h9b_f33_reasoning_replay_probe_uses_the_configured_model_and_production_header(ctx: Ctx):
    """Monkeypatches the two live-network call sites (never a real VPN
    call, matching this file's own policy) to capture what candidate model
    and headers the probe actually used -- must be the CONFIGURED model
    (routes.json's default), not the catalog's first thinking-family
    match, and must carry x-databricks-use-coding-agent-mode."""
    import rolo_claude.doctor as doctor_mod
    import rolo_claude.providers.databricks as dbx_mod
    import rolo_claude.providers.http as http_mod

    state_dir = Path(tempfile.mkdtemp(prefix="work-f33-state4-"))
    (state_dir / "routes.json").write_text(
        json.dumps({"default": "dbx:databricks-kimi-k3"}), encoding="utf-8")

    def _fake_probe_full(root, token):
        # The catalog's FIRST thinking-family match is a DIFFERENT model --
        # proves the configured one wins, not this.
        return 200, [{"name": "databricks-glm-5-2"}, {"name": "databricks-kimi-k3"}]

    captured = {}

    def _fake_call_databricks_chat(root, token, body, extra_headers, state_dir_arg, model, on_connect=None):
        captured["model"] = model
        captured["extra_headers"] = dict(extra_headers)
        return type("Result", (), {"status": 200, "resp": None})()

    def _fake_route_candidates(model):
        return [("invocations", False)]

    def _fake_cache_get_route(model, state_dir_arg):
        return None

    old_probe = dbx_mod.probe_databricks_endpoints_full
    old_call = http_mod.call_databricks_chat
    old_candidates = dbx_mod.databricks_route_candidates
    old_cache_get = dbx_mod.dbx_cache_get_route
    dbx_mod.probe_databricks_endpoints_full = _fake_probe_full
    http_mod.call_databricks_chat = _fake_call_databricks_chat
    dbx_mod.databricks_route_candidates = _fake_route_candidates
    dbx_mod.dbx_cache_get_route = _fake_cache_get_route
    try:
        lines = doctor_mod._work_check_reasoning_replay_after_tool_call("fake-host", "fake-token", state_dir)
    finally:
        dbx_mod.probe_databricks_endpoints_full = old_probe
        http_mod.call_databricks_chat = old_call
        dbx_mod.databricks_route_candidates = old_candidates
        dbx_mod.dbx_cache_get_route = old_cache_get

    ctx.check(f"probed the CONFIGURED model (databricks-kimi-k3), not the catalog's first match "
              f"(databricks-glm-5-2), got {captured.get('model')!r}", captured.get("model") == "databricks-kimi-k3")
    ctx.check(f"carried the production header, got {captured.get('extra_headers')!r}",
              captured.get("extra_headers", {}).get("x-databricks-use-coding-agent-mode") == "true")
    ctx.check("output names the configured model as such", any("CONFIGURED model" in l for l in lines))


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
