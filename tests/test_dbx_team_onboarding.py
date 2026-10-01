"""tests.test_dbx_team_onboarding -- H14 scope I: team.json discovery/
parsing (never a token, never an endpoint list) and `init --preset work`'s
token-only onboarding (host already known via Claude Code's settings env or
a shared team.json -> only the token is asked for). End-to-end `init` cases
run the real CLI as a subprocess (same pattern as tests/test_init_cli.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()
_NOT_LOGGED_IN = json.dumps({"loggedIn": False})


def _fresh_home() -> Path:
    return Path(tempfile.mkdtemp(prefix="halo-team-"))


def _run(argv, home: Path, *, stdin: str = "", extra_env: "dict | None" = None, timeout: int = 40):
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(home), "PYTHONPATH": str(REPO_DIR),
                "BRIDGE_TEST_CC_AUTH_STATUS": _NOT_LOGGED_IN})
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "halo_harness"] + argv, env=env, cwd=str(REPO_DIR),
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           input=stdin, timeout=timeout)


def _env_file(home: Path) -> Path:
    """2.0.0 rename: `halo init` now writes the NEW default path."""
    return home / ".config" / "halo" / "env"


# ---------------------------------------------------------------------------
# team.json discovery + validation (pure functions, no subprocess needed).
# ---------------------------------------------------------------------------

@test
def test_load_team_config_none_when_nothing_configured(ctx: Ctx):
    from halo_harness.team_config import load_team_config
    cwd = Path(tempfile.mkdtemp(prefix="team-none-"))
    old = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_STATE_DIR"] = str(Path(tempfile.mkdtemp(prefix="team-none-state-")))
    try:
        cfg, warnings = load_team_config(cwd)
        ctx.check("no team config -> None", cfg is None)
        ctx.check("no warnings", warnings == [])
    finally:
        if old is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = old


@test
def test_load_team_config_project_scope(ctx: Ctx):
    from halo_harness.team_config import load_team_config
    cwd = Path(tempfile.mkdtemp(prefix="team-project-"))
    (cwd / ".halo").mkdir()
    (cwd / ".halo" / "team.json").write_text(json.dumps({
        "host": "https://your-workspace.cloud.databricks.com", "default_model": "dbx:databricks-glm-5-3",
    }), encoding="utf-8")
    cfg, warnings = load_team_config(cwd)
    ctx.check(f"project team.json found, got {cfg}", cfg is not None and
              cfg.get("host") == "https://your-workspace.cloud.databricks.com")
    ctx.check("no warnings for a clean file", warnings == [])


@test
def test_load_team_config_falls_back_to_legacy_project_path(ctx: Ctx):
    """2.0.0 fixpass finding 5: a project preset checked in BEFORE this
    rename lives at `.rolo-claude/team.json` -- `init --provider
    databricks` in such a repo must still find it (with one deprecation
    line on stderr), not silently ignore it just because `.halo/
    team.json` doesn't exist yet."""
    import contextlib
    import io

    from halo_harness.team_config import load_team_config
    cwd = Path(tempfile.mkdtemp(prefix="team-legacy-"))
    (cwd / ".rolo-claude").mkdir()
    legacy_path = cwd / ".rolo-claude" / "team.json"
    legacy_path.write_text(json.dumps({
        "host": "https://your-workspace.cloud.databricks.com", "default_model": "dbx:databricks-glm-5-3",
    }), encoding="utf-8")

    stderr_buf = io.StringIO()
    with contextlib.redirect_stderr(stderr_buf):
        cfg, warnings = load_team_config(cwd)
    ctx.check(f"legacy .rolo-claude/team.json found, got {cfg}",
              cfg is not None and cfg.get("host") == "https://your-workspace.cloud.databricks.com")
    ctx.check("no load WARNINGS for a clean legacy file (the deprecation notice is separate)", warnings == [])
    msg = stderr_buf.getvalue()
    ctx.check(f"exactly one deprecation line naming the legacy path, got {msg!r}",
              msg.count("\n") == 1 and str(legacy_path) in msg and "deprecated" in msg)

    # A NEW .halo/team.json, once it exists, wins outright -- the legacy
    # fallback only ever fires when the new path is absent.
    (cwd / ".halo").mkdir()
    (cwd / ".halo" / "team.json").write_text(json.dumps({"host": "https://new-wins.cloud.databricks.com"}),
                                              encoding="utf-8")
    cfg2, _warnings2 = load_team_config(cwd)
    ctx.check(f"the new path wins once it exists, got {cfg2}",
              cfg2 is not None and cfg2.get("host") == "https://new-wins.cloud.databricks.com")


@test
def test_load_team_config_explicit_team_flag_wins(ctx: Ctx):
    from halo_harness.team_config import load_team_config
    cwd = Path(tempfile.mkdtemp(prefix="team-explicit-"))
    (cwd / ".halo").mkdir()
    (cwd / ".halo" / "team.json").write_text(json.dumps({"host": "https://loser.cloud.databricks.com"}),
                                                      encoding="utf-8")
    explicit = cwd / "explicit-team.json"
    explicit.write_text(json.dumps({"host": "https://your-workspace.cloud.databricks.com"}), encoding="utf-8")
    cfg, _w = load_team_config(cwd, team_flag=str(explicit))
    ctx.check(f"--team wins over project discovery, got {cfg}",
              cfg["host"] == "https://your-workspace.cloud.databricks.com")


@test
def test_load_team_config_strips_token_shaped_keys(ctx: Ctx):
    from halo_harness.team_config import load_team_config
    cwd = Path(tempfile.mkdtemp(prefix="team-forbidden-"))
    p = cwd / "team.json"
    p.write_text(json.dumps({
        "host": "https://your-workspace.cloud.databricks.com",
        "token": "dapiSHOULD_NEVER_SURVIVE",
        "api_key": "also-should-not-survive",
        "some_unrecognized_key": "ignored too",
    }), encoding="utf-8")
    cfg, warnings = load_team_config(cwd, team_flag=str(p))
    ctx.check(f"host kept, got {cfg}", cfg is not None and cfg.get("host"))
    ctx.check("token stripped", "token" not in cfg)
    ctx.check("api_key stripped", "api_key" not in cfg)
    ctx.check("unrecognized key stripped", "some_unrecognized_key" not in cfg)
    ctx.check(f"warnings mention the dropped token, got {warnings}",
              any("token" in w for w in warnings) and any("api_key" in w for w in warnings))


@test
def test_load_team_config_roles_key_roundtrip(ctx: Ctx):
    """V2c (H15): team.json's own `roles` map (never a token, never an
    endpoint list -- just like every other allowed key) survives `load_
    team_config` unchanged."""
    from halo_harness.team_config import load_team_config
    cwd = Path(tempfile.mkdtemp(prefix="team-roles-"))
    p = cwd / "team.json"
    roles = {"researcher": "dbx:databricks-deepseek-v4-1-flash", "small": "dbx:databricks-deepseek-v4-1-flash"}
    p.write_text(json.dumps({"host": "https://your-workspace.cloud.databricks.com", "roles": roles}),
                 encoding="utf-8")
    cfg, warnings = load_team_config(cwd, team_flag=str(p))
    ctx.check(f"roles roundtrip unchanged, got {cfg}", cfg is not None and cfg.get("roles") == roles)
    ctx.check("no warnings for a clean roles key", warnings == [])


@test
def test_load_team_config_bad_json_reports_a_warning_not_a_crash(ctx: Ctx):
    from halo_harness.team_config import load_team_config
    cwd = Path(tempfile.mkdtemp(prefix="team-badjson-"))
    p = cwd / "team.json"
    p.write_text("{not json", encoding="utf-8")
    cfg, warnings = load_team_config(cwd, team_flag=str(p))
    ctx.check("unparseable -> None", cfg is None)
    ctx.check(f"a warning names the problem, got {warnings}", len(warnings) == 1)


@test
def test_apply_gateway_preference_seeds_config_but_never_overwrites(ctx: Ctx):
    from halo_harness.team_config import apply_gateway_preference
    from halo_harness.theme import get_config_value, set_config_value
    state_dir = Path(tempfile.mkdtemp(prefix="team-gwpref-"))
    old = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_STATE_DIR"] = str(state_dir)
    try:
        set_config_value("databricks.gateway.databricks-already-set", "mlflow")
        apply_gateway_preference({"databricks-kimi-k3": "anthropic", "databricks-already-set": "anthropic"})
        ctx.check("new endpoint seeded", get_config_value("databricks.gateway.databricks-kimi-k3", default=None)
                  == "anthropic")
        ctx.check("existing local override never clobbered",
                  get_config_value("databricks.gateway.databricks-already-set", default=None) == "mlflow")
    finally:
        if old is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = old


@test
def test_apply_role_preference_seeds_config_but_never_overwrites(ctx: Ctx):
    """V2c (H15): the exact same idiom as `apply_gateway_preference` above,
    applied to team.json's own `roles` map."""
    from halo_harness.roles import apply_role_preference
    from halo_harness.theme import get_config_value, set_config_value
    state_dir = Path(tempfile.mkdtemp(prefix="team-rolepref-"))
    old = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_STATE_DIR"] = str(state_dir)
    try:
        set_config_value("roles.coder", "dbx:databricks-claude-opus-4-6")
        apply_role_preference({"researcher": "dbx:databricks-deepseek-v4-1-flash",
                                "coder": "dbx:databricks-kimi-k3", "not-a-real-role": "ignored"})
        ctx.check("new role seeded", get_config_value("roles.researcher", default=None)
                  == "dbx:databricks-deepseek-v4-1-flash")
        ctx.check("existing local override never clobbered",
                  get_config_value("roles.coder", default=None) == "dbx:databricks-claude-opus-4-6")
        ctx.check("unrecognized role name never written",
                  get_config_value("roles.not-a-real-role", default=None) is None)
    finally:
        if old is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = old


# ---------------------------------------------------------------------------
# End-to-end `init --preset work` onboarding.
# ---------------------------------------------------------------------------

@test
def test_init_work_host_from_team_json_asks_only_for_token(ctx: Ctx):
    home = _fresh_home()
    (home / ".halo").mkdir(parents=True, exist_ok=True)
    (home / ".halo" / "team.json").write_text(json.dumps({
        "host": "https://your-workspace.cloud.databricks.com",
        "default_model": "dbx:databricks-glm-5-3",
        "gateway_preference": {"databricks-kimi-k3": "anthropic"},
        "roles": {"researcher": "dbx:databricks-deepseek-v4-1-flash"},
    }), encoding="utf-8")
    result = _run(["init", "--preset", "work", "--yes", "--no-live"], home, stdin="team-token-xyz\n")
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    ctx.check("only the token was asked for", "only the token is needed" in result.stdout)
    ctx.check("token never printed", "team-token-xyz" not in result.stdout)
    content = _env_file(home).read_text(encoding="utf-8")
    ctx.check(f"host from team.json written, got {content!r}",
              "DATABRICKS_HOST=https://your-workspace.cloud.databricks.com" in content)
    ctx.check(f"token written, got {content!r}", "DATABRICKS_TOKEN=team-token-xyz" in content)
    cfg = json.loads((home / ".halo" / "config.json").read_text(encoding="utf-8"))
    ctx.check(f"team.json default_model applied, got {cfg}", cfg.get("model") == "dbx:databricks-glm-5-3")
    ctx.check("gateway preference seeded", cfg.get("databricks", {}).get("gateway", {}).get("databricks-kimi-k3")
              == "anthropic")
    ctx.check(f"team.json roles seeded, got {cfg}",
              cfg.get("roles", {}).get("researcher") == "dbx:databricks-deepseek-v4-1-flash")


@test
def test_init_work_claude_code_settings_env_skips_all_prompts(ctx: Ctx):
    home = _fresh_home()
    result = _run(["init", "--preset", "work", "--yes", "--no-live"], home, extra_env={
        "ANTHROPIC_BASE_URL": "https://your-workspace.cloud.databricks.com/ai-gateway/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "dapiFAKE00000000000",
        "ANTHROPIC_MODEL": "databricks-claude-opus-4-6",
    })
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    ctx.check("reports configured via Claude Code's settings", "Claude Code's settings" in result.stdout)
    ctx.check("token never printed", "dapiFAKE00000000000" not in result.stdout)
    ctx.check("no env file needed", not _env_file(home).exists())


def _hermetic_child_env() -> dict:
    """2.0.0 fixpass item G: never forward a stray BRIDGE_STATE_DIR
    (would let bridge_home() escape this test's own BRIDGE_TEST_HOME
    scoping) or HALO_* (would out-rank the legacy BRIDGE_* name a
    fixture deliberately sets, per env_compat's own precedence) from
    the parent process into a spawned child -- same hermeticity
    tests/test_init_cli.py::_run already has, applied at each of this
    file's own `env = dict(os.environ)` call sites."""
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    return env


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
