"""tests.test_codex_settings_merge -- Halo 2.0.3 round 5i part 2: the
Codex settings/AGENTS.md reader (`providers.codex_settings`), the merged
Claude-Code/Codex/halo settings view (`providers.settings_merge`), and
the `codex_subscription` enablement gates. Fixture trees only -- never the
real `~/.codex` or `~/.halo`.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_ENV_VARS = ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE", "CODEX_HOME",
             "BRIDGE_TEST_CODEX_LOGIN_STATUS", "HALO_CODEX_EXE")


class _Env:
    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_VARS}
        d = Path(tempfile.mkdtemp(prefix="cx-settings-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        os.environ.pop("CODEX_HOME", None)
        self.root = d
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _codex_home(env: _Env) -> Path:
    home = env.root / "codex_home"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["CODEX_HOME"] = str(home)
    return home


def _git_repo(env: _Env, name: str) -> Path:
    repo = env.root / name
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    return repo


# ---- codex_settings: config.toml reader --------------------------------

@test
def test_parse_simple_toml_scalars_and_tables(ctx: Ctx):
    from halo_harness.providers.codex_settings import parse_simple_toml
    text = (
        'model = "gpt-6-astra"\n'
        "approval_policy = \"on-request\"\n"
        "# a comment\n"
        "\n"
        "[mcp_servers.foo]\n"
        'command = "foo-bin"\n'
        'args = ["--a", "--b"]\n'
        "enabled = true\n"
    )
    cfg = parse_simple_toml(text)
    ctx.check(f"model parsed, got {cfg}", cfg.get("model") == "gpt-6-astra")
    ctx.check("approval_policy parsed", cfg.get("approval_policy") == "on-request")
    foo = (cfg.get("mcp_servers") or {}).get("foo") or {}
    ctx.check(f"mcp_servers.foo.command parsed, got {foo}", foo.get("command") == "foo-bin")
    ctx.check("mcp_servers.foo.args is a list", foo.get("args") == ["--a", "--b"])
    ctx.check("mcp_servers.foo.enabled is True", foo.get("enabled") is True)


@test
def test_load_codex_config_missing_file_returns_empty(ctx: Ctx):
    with _Env() as env:
        home = _codex_home(env)
        from halo_harness.providers.codex_settings import load_codex_config
        ctx.check("no config.toml -> {}", load_codex_config(home) == {})


@test
def test_project_codex_config_layers_over_home(ctx: Ctx):
    with _Env() as env:
        home = _codex_home(env)
        (home / "config.toml").write_text('model = "gpt-6-luna"\napproval_policy = "never"\n', encoding="utf-8")
        repo = _git_repo(env, "repo")
        (repo / ".codex").mkdir()
        (repo / ".codex" / "config.toml").write_text('model = "gpt-6-astra"\n', encoding="utf-8")
        from halo_harness.providers.codex_settings import load_codex_config, load_project_codex_config
        home_cfg = load_codex_config(home)
        project_cfg = load_project_codex_config(repo)
        ctx.check(f"home model is luna, got {home_cfg}", home_cfg.get("model") == "gpt-6-luna")
        ctx.check(f"project model overrides to astra, got {project_cfg}", project_cfg.get("model") == "gpt-6-astra")


# ---- codex_settings: AGENTS.md chain ------------------------------------

@test
def test_agents_md_chain_global_then_root_then_override(ctx: Ctx):
    with _Env() as env:
        home = _codex_home(env)
        (home / "AGENTS.md").write_text("GLOBAL", encoding="utf-8")
        repo = _git_repo(env, "repo")
        (repo / "AGENTS.md").write_text("ROOT", encoding="utf-8")
        sub = repo / "sub"
        sub.mkdir()
        (sub / "AGENTS.override.md").write_text("SUB OVERRIDE", encoding="utf-8")
        from halo_harness.providers.codex_settings import load_codex_agents_md_chain, render_codex_agents_md_chain
        chain = load_codex_agents_md_chain(sub, home=home)
        rendered = render_codex_agents_md_chain(chain)
        ctx.check(f"3 files in the chain, got {len(chain)}", len(chain) == 3)
        ctx.check(f"root-to-leaf merge order, got {rendered!r}",
                   rendered.index("GLOBAL") < rendered.index("ROOT") < rendered.index("SUB OVERRIDE"))


@test
def test_agents_override_beats_plain_at_same_level(ctx: Ctx):
    with _Env() as env:
        home = _codex_home(env)
        repo = _git_repo(env, "repo")
        (repo / "AGENTS.md").write_text("PLAIN", encoding="utf-8")
        (repo / "AGENTS.override.md").write_text("OVERRIDE", encoding="utf-8")
        from halo_harness.providers.codex_settings import load_codex_agents_md_chain
        chain = load_codex_agents_md_chain(repo, home=home)
        ctx.check(f"only the override file is used at that level, got {chain}",
                   len(chain) == 1 and chain[0]["text"] == "OVERRIDE")


@test
def test_agents_md_empty_file_skipped(ctx: Ctx):
    with _Env() as env:
        home = _codex_home(env)
        repo = _git_repo(env, "repo")
        (repo / "AGENTS.md").write_text("   \n", encoding="utf-8")
        from halo_harness.providers.codex_settings import load_codex_agents_md_chain
        chain = load_codex_agents_md_chain(repo, home=home)
        ctx.check(f"an all-whitespace file is skipped, got {chain}", chain == [])


@test
def test_agents_md_no_git_repo_degrades_to_cwd_only(ctx: Ctx):
    with _Env() as env:
        home = _codex_home(env)
        plain_dir = env.root / "no-git-here"
        plain_dir.mkdir()
        (plain_dir / "AGENTS.md").write_text("JUST HERE", encoding="utf-8")
        from halo_harness.providers.codex_settings import load_codex_agents_md_chain
        chain = load_codex_agents_md_chain(plain_dir, home=home)
        ctx.check(f"one file found with no git repo at all, got {chain}",
                   len(chain) == 1 and chain[0]["text"] == "JUST HERE")


# ---- enablement ----------------------------------------------------------

@test
def test_codex_subscription_credentials_present_follows_login_status(ctx: Ctx):
    with _Env():
        from halo_harness.providers.enablement import credentials_present
        os.environ["BRIDGE_TEST_CODEX_LOGIN_STATUS"] = "Not logged in"
        ctx.check("not logged in -> not present", credentials_present("codex_subscription") is False)
        os.environ["BRIDGE_TEST_CODEX_LOGIN_STATUS"] = "Logged in using ChatGPT"
        ctx.check("ChatGPT login -> present", credentials_present("codex_subscription") is True)
        os.environ["BRIDGE_TEST_CODEX_LOGIN_STATUS"] = "Logged in using an API key - sk-..."
        ctx.check("API-key login -> NOT present (that's oai:, not cx:)",
                   credentials_present("codex_subscription") is False)


@test
def test_codex_prefix_and_alias_tables(ctx: Ctx):
    from halo_harness.providers.enablement import PREFIXES, canonical, label_for
    ctx.check("cx: alias resolves to codex_subscription", canonical("cx") == "codex_subscription")
    ctx.check("codex alias resolves to codex_subscription", canonical("codex") == "codex_subscription")
    ctx.check("prefix is cx:", PREFIXES["codex_subscription"] == "cx:")
    ctx.check("label mentions ChatGPT", "ChatGPT" in label_for("codex_subscription"))


@test
def test_codex_subscription_disabled_message_refuses_parse(ctx: Ctx):
    with _Env():
        from halo_harness.providers.enablement import disable
        from halo_harness.model import parse_model_ref
        from halo_harness.providers.routing import InvalidModelError
        os.environ["BRIDGE_TEST_CODEX_LOGIN_STATUS"] = "Logged in using ChatGPT"
        disable("codex_subscription")
        try:
            parse_model_ref("cx:astra")
            ctx.check("disabled codex_subscription refuses cx: refs", False)
        except InvalidModelError as e:
            ctx.check(f"refusal names codex_subscription, got {e}", "codex_subscription" in str(e))


# ---- settings_merge: precedence -----------------------------------------

def _settings_fixture(env: _Env):
    home = _codex_home(env)
    (home / "config.toml").write_text(
        'model = "gpt-6-luna"\napproval_policy = "never"\nsandbox_mode = "danger-full-access"\n'
        "\n[mcp_servers.codex_only]\ncommand = \"codex-only-bin\"\n", encoding="utf-8")
    repo = _git_repo(env, "repo")
    return repo


@test
def test_merge_non_overlapping_mcp_server_joins_list(ctx: Ctx):
    with _Env() as env:
        repo = _settings_fixture(env)
        from halo_harness.providers.settings_merge import effective_settings
        view = effective_settings(repo)
        names = [m["name"] for m in view.mcp_servers]
        ctx.check(f"codex_only server surfaced, got {names}", "codex_only" in names)
        ctx.check("codex config.toml found", view.codex_config_found is True)


@test
def test_merge_precedence_default_claude_then_codex(ctx: Ctx):
    with _Env() as env:
        repo = _settings_fixture(env)
        from halo_harness.providers.settings_merge import effective_settings
        view = effective_settings(repo, primary="claude")
        model_row = next(r for r in view.rows if r.key == "model")
        # halo's own config has no "model" set in this fixture, Claude Code
        # has no settings.json here either -- codex's own value is the
        # only one present, so it must still win even with primary=claude
        # (nothing else to prefer over it).
        ctx.check(f"codex model used when nothing else is set, got {model_row}",
                   model_row.effective == "gpt-6-luna" and model_row.effective_source == "codex")


@test
def test_merge_settings_primary_round_trips(ctx: Ctx):
    with _Env():
        from halo_harness.providers.settings_merge import set_settings_primary, settings_primary
        ctx.check("default primary is claude", settings_primary() == "claude")
        set_settings_primary("codex")
        ctx.check("primary persisted as codex", settings_primary() == "codex")
        set_settings_primary("bogus")
        ctx.check("an invalid value falls back to claude", settings_primary() == "claude")


@test
def test_merge_cx_session_leads_for_model_and_approval(ctx: Ctx):
    with _Env() as env:
        repo = _settings_fixture(env)
        from halo_harness.providers.settings_merge import effective_settings
        view = effective_settings(repo, primary="claude", session_provider="codex")
        model_row = next(r for r in view.rows if r.key == "model")
        approval_row = next(r for r in view.rows if r.key == "permission_mode")
        ctx.check(f"a live cx: session makes codex's model lead, got {model_row}",
                   model_row.effective_source == "codex")
        ctx.check(f"and codex's approval policy too, got {approval_row}", approval_row.effective_source == "codex")


@test
def test_render_settings_text_includes_primary_and_sources(ctx: Ctx):
    with _Env() as env:
        repo = _settings_fixture(env)
        from halo_harness.providers.settings_merge import effective_settings, render_settings_text
        view = effective_settings(repo, primary="codex")
        text = render_settings_text(view)
        ctx.check(f"mentions primary, got {text!r}", "primary: codex" in text)
        ctx.check("mentions the codex-only MCP server", "codex_only" in text)


@test
def test_codex_agents_md_gets_a_one_line_header_in_the_merged_view(ctx: Ctx):
    with _Env() as env:
        home = _codex_home(env)
        repo = _git_repo(env, "repo")
        (repo / "AGENTS.md").write_text("REPO RULE", encoding="utf-8")
        from halo_harness.providers.settings_merge import effective_settings, render_settings_text
        view = effective_settings(repo)
        codex_headers = [e for e in view.instructions if e["source"] == "codex"]
        ctx.check(f"one codex instruction header found, got {view.instructions}", len(codex_headers) == 1)
        ctx.check(f"header names the file, got {codex_headers}", "AGENTS.md" in codex_headers[0]["header"])
        text = render_settings_text(view)
        ctx.check(f"rendered view shows the header line, got {text!r}", "AGENTS.md" in text and "(codex)" in text)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
