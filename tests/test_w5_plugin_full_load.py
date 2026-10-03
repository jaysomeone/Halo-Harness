"""tests.test_w5_plugin_full_load -- W5 (carried from W4a): `--plugin-dir`/
`--plugin-url` loads a plugin's skills, hooks and MCP servers, not only its
agents (plugin_fetch.py's own prior v1 scope). One fixture plugin directory,
Claude Code's real layout (`.claude-plugin/plugin.json`, `.mcp.json`,
`hooks/hooks.json` with `${CLAUDE_PLUGIN_ROOT}`, `skills/`, `commands/`,
`agents/`), exercised against each of the four newly-wired precedence tiers
(`config.plugins.discover_plugin_mcp_servers`'s own `extra_roots=`,
`headless.build_hook_runner`'s own `extra_plugin_roots=`,
`commands.skills.discover_all_skills`/`find_skill`'s own `plugin_roots=`,
`commands.custom.register_custom_commands`'s own `plugin_roots=`) plus the
Skill TOOL's own end-to-end path (`Session.plugin_roots` ->
`ToolContext.plugin_roots` -> `tools/skill.py`).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials, ensure_scoped_state_dir_once

# `test_build_manager_wires_extra_plugin_roots_through_to_a_real_connection`
# below goes through the REAL `mcp_setup.build_manager`, which (via its own
# lazy-server bootstrap) reads/writes `tools_cache` under `bridge_home()` --
# scoped here BEFORE that test ever runs so it never touches the real
# `~/.halo/mcp/tools-cache/`.
ensure_scoped_state_dir_once()
ensure_default_provider_credentials()
REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _build_fixture_plugin(base: Path, *, name: str = "fixtureplugin") -> Path:
    """Claude Code's real plugin layout, minimal but complete: a declared
    name (so MCP/skill/command naming comes from `plugin.json`, never just
    the tempdir's own random basename), one MCP server (the real stdio
    `tests/helpers/fake_mcp_server`, for an actual connection), one hook,
    one skill, one command, one agent."""
    root = base / "plugin-src"
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "description": "W5 fixture plugin"}), encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"fakeserver": {
        "command": sys.executable, "args": ["-m", "tests.helpers.fake_mcp_server"],
        "mcpLazy": False,  # forces a real eager connection (not "cached" from a prior run's tools_cache)
    }}}), encoding="utf-8")
    (root / "hooks").mkdir(parents=True, exist_ok=True)
    (root / "hooks" / "hooks.json").write_text(json.dumps({
        "PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "echo ${CLAUDE_PLUGIN_ROOT}"},
        ]}],
    }), encoding="utf-8")
    skill_dir = root / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\ndescription: a fixture skill\n---\nFixture plugin skill body, $ARGUMENTS.\n",
        encoding="utf-8")
    (root / "commands").mkdir(parents=True, exist_ok=True)
    (root / "commands" / "demo-cmd.md").write_text(
        "---\ndescription: a fixture command\n---\nFixture plugin command body.\n", encoding="utf-8")
    (root / "agents").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "demo-agent.md").write_text(
        "---\nname: demo-agent\ndescription: a fixture agent\n---\nYou are the fixture agent.\n",
        encoding="utf-8")
    return root


# ---- MCP servers ------------------------------------------------------

@test
def test_cli_plugin_root_mcp_server_is_discovered_and_named(ctx: Ctx):
    from halo_harness.config.plugins import discover_plugin_mcp_servers, plugin_server_name
    base = Path(tempfile.mkdtemp(prefix="w5-plugin-"))
    root = _build_fixture_plugin(base)
    servers, notices = discover_plugin_mcp_servers(env={}, extra_roots=[str(root)])
    expected_name = plugin_server_name("fixtureplugin", "fakeserver")
    ctx.check(f"named plugin_<declared-name>_<server>, got {list(servers)}", expected_name in servers)
    ctx.check(f"no notices for a clean discovery, got {notices}", notices == [])
    cfg = servers[expected_name]
    ctx.check("scope is 'plugin'", cfg.scope == "plugin")
    ctx.check("plugin name recorded from plugin.json, not the tempdir basename", cfg.plugin == "fixtureplugin")


@test
def test_build_manager_wires_extra_plugin_roots_through_to_a_real_connection(ctx: Ctx):
    """The actual integration point this round changed: `mcp_setup.
    build_manager`'s new `extra_plugin_roots=` kwarg, proven with a REAL
    stdio connection and the namespaced wire tool name (same acceptance
    bar test_mcp_plugins.py's own installed-plugin equivalent uses)."""
    from halo_harness.config.plugins import plugin_server_name
    from halo_harness.mcp_setup import build_manager
    base = Path(tempfile.mkdtemp(prefix="w5-plugin-"))
    root = _build_fixture_plugin(base)
    mgr, notices = build_manager(cwd=REPO_DIR, claude_json={}, extra_plugin_roots=[str(root)], start=True)
    ctx.check(f"a real manager came back, notices={notices}", mgr is not None)
    try:
        wire_server_name = plugin_server_name("fixtureplugin", "fakeserver")
        status = {s["name"]: s for s in mgr.status()}
        ctx.check(f"the plugin server actually connects, got {status.get(wire_server_name)}",
                  status.get(wire_server_name, {}).get("state") == "connected")
        names = [t[1] for t in mgr.all_tools()]
        expected_tool = f"mcp__{wire_server_name}__echo"
        ctx.check(f"tool name carries the plugin-namespaced server, got {names}", expected_tool in names)
    finally:
        mgr.close_all()


# ---- hooks --------------------------------------------------------------

@test
def test_cli_plugin_root_hooks_load_with_plugin_root_substituted(ctx: Ctx):
    from halo_harness.headless import build_hook_runner
    base = Path(tempfile.mkdtemp(prefix="w5-plugin-"))
    root = _build_fixture_plugin(base)
    runner = build_hook_runner(settings=None, cwd=base, session_id="w5-plugin", transcript_path="t.jsonl",
                                effort=None, permission_mode="auto", mcp_manager=None, bare=False,
                                extra_plugin_roots=[str(root)])
    ctx.check("PreToolUse hooks loaded from the CLI plugin root", runner.has_hooks("PreToolUse") is True)
    hdef = runner.hooks_by_event["PreToolUse"][0]
    ctx.check(f"${{CLAUDE_PLUGIN_ROOT}} was substituted, got {hdef.command!r}",
              str(root) in (hdef.command or "") and "${CLAUDE_PLUGIN_ROOT}" not in (hdef.command or ""))
    ctx.check("plugin_root recorded on the HookDef", hdef.plugin_root == str(root))


# ---- skills + commands (slash-command surface) ---------------------------

@test
def test_cli_plugin_root_skill_is_namespaced_plugin_colon_skill(ctx: Ctx):
    from halo_harness.commands.skills import discover_all_skills, find_skill
    base = Path(tempfile.mkdtemp(prefix="w5-plugin-"))
    root = _build_fixture_plugin(base)
    proj = base / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    skills = discover_all_skills(proj, home=home, plugin_roots=[str(root)])
    ctx.check(f"namespaced <plugin>:<skill> (binary-facts sec.11), got {list(skills)}",
              "fixtureplugin:demo-skill" in skills)
    found = find_skill("fixtureplugin:demo-skill", proj, home=home, plugin_roots=[str(root)])
    ctx.check("find_skill resolves the same plugin skill", found is not None and found.source == "skill")


@test
def test_cli_plugin_root_command_is_namespaced_and_registered(ctx: Ctx):
    from halo_harness.commands.registry import Registry
    base = Path(tempfile.mkdtemp(prefix="w5-plugin-"))
    root = _build_fixture_plugin(base)
    proj = base / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    reg = Registry.discover(proj, home, plugin_roots=[str(root)])
    cmd = reg.resolve("fixtureplugin:demo-cmd")
    ctx.check(f"the plugin command is registered and resolvable, got {reg.help_rows()}", cmd is not None)
    # `_discover_dir`'s own `source` KEYWORD is vestigial for every caller
    # (project/user/plugin all land as the literal "custom" -- see
    # test_commands_registry.py::test_custom_command_namespaced_by_subdir's
    # own deliberate assertion of the SAME value for a project command);
    # not something this round's wiring changes.
    ctx.check("source is 'custom' (same vestigial value every _discover_dir caller gets)",
              cmd.source == "custom")


# ---- Skill TOOL end to end (Session.plugin_roots -> ToolContext) ---------

@test
def test_skill_tool_runs_a_cli_plugin_roots_skill_through_a_real_session(ctx: Ctx):
    """Proves the OTHER half of plugin skill loading: a model-invoked
    `Skill` tool call (not just the `/` slash-command surface) sees a CLI
    `--plugin-dir`/`--plugin-url` skill, via `Session.plugin_roots` (read
    from `cli_flags["resolved_plugin_roots"]`) -> `ToolContext.
    plugin_roots` -> `tools/skill.py`."""
    import tempfile as _tempfile
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds
    from halo_harness.tools.base import ToolContext
    from halo_harness.tools.skill import SkillTool

    base = Path(tempfile.mkdtemp(prefix="w5-plugin-"))
    root = _build_fixture_plugin(base)
    cwd = base / "proj"
    cwd.mkdir(parents=True, exist_ok=True)
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(Path(_tempfile.mkdtemp(prefix="w5-plugin-home-")))
    try:
        session_ctx = SessionContext(cwd=cwd, model_label="or:mock/w5-plugin")
        model_ref = parse_model_ref("or:mock/w5-plugin")
        session = Session(
            cwd=cwd, model_ref=model_ref, model_profile=ModelProfile(),
            creds=ProviderCreds(base_url="http://127.0.0.1:1", api_key="k"),
            state_dir=Path(_tempfile.mkdtemp(prefix="w5-plugin-state-")), model_label=model_ref.raw,
            session_context=session_ctx, max_turns=1,
            cli_flags={"resolved_plugin_roots": [str(root)]},
        )
        ctx.check("Session.plugin_roots picked up from cli_flags", session.plugin_roots == [str(root)])

        tool = SkillTool()
        tool_ctx = ToolContext(cwd=cwd, plugin_roots=session.plugin_roots)
        result = tool.run({"skill": "fixtureplugin:demo-skill"}, tool_ctx)
        ctx.check(f"the plugin skill ran (not an error), got {result.content!r}", result.is_error is False)
        ctx.check("the skill's own body text came back", "Fixture plugin skill body" in str(result.content))
    finally:
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
