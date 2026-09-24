"""tests.test_mcp_plugins -- rolo_claude/config/plugins.py (H3b closing
requirement: "every server a user adds to Claude Code must work"):
discovery of plugin-provided MCP servers under ~/.claude/plugins/, against
the fixture plugin in tests/helpers/fake_home.py::add_fake_plugin (rolo has
none installed for real as of 2026-09-24). See config/plugins.py's own
module docstring for the on-disk layout this mirrors.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import add_fake_plugin

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _fake_claude_dir():
    """A fresh `~/.claude`-shaped dir (no full build_fake_home() needed --
    plugin discovery only ever looks under `<configDir>/plugins/`)."""
    root = Path(tempfile.mkdtemp(prefix="rolo-claude-plugins-"))
    claude_dir = root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    return claude_dir


# ---- discovery --------------------------------------------------------

@test
def test_discovers_the_fixture_plugin_server(ctx: Ctx):
    from rolo_claude.config.plugins import discover_plugin_mcp_servers, plugin_server_name
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        info = add_fake_plugin(claude_dir)
        expected_name = plugin_server_name("fake-plugin", "fakeserver")
        ctx.check(f"wire server name matches plugin_<plugin>_<server>, got {info['wire_server_name']!r}",
                  info["wire_server_name"] == expected_name)
        servers, notices = discover_plugin_mcp_servers(env={})
        ctx.check(f"the fixture server is discovered, got {list(servers)}", expected_name in servers)
        ctx.check(f"no notices for a clean discovery, got {notices}", notices == [])
        cfg = servers[expected_name]
        ctx.check("scope is 'plugin'", cfg.scope == "plugin")
        ctx.check("plugin name recorded", cfg.plugin == "fake-plugin")
        ctx.check("command/args carried through", cfg.command == sys.executable
                  and cfg.args == ["-m", "tests.helpers.fake_mcp_server"])
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


@test
def test_claude_plugin_root_expands_to_the_real_plugin_directory(ctx: Ctx):
    from rolo_claude.config.plugins import discover_plugin_mcp_servers
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        info = add_fake_plugin(claude_dir)
        servers, _ = discover_plugin_mcp_servers(env={})
        cfg = servers[info["wire_server_name"]]
        seen = cfg.env.get("FAKE_PLUGIN_ROOT_SEEN")
        ctx.check(f"${{CLAUDE_PLUGIN_ROOT}} expanded to the plugin's own root, got {seen!r} want {info['plugin_root']}",
                  Path(seen) == info["plugin_root"])
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


@test
def test_disabled_plugin_is_skipped(ctx: Ctx):
    from rolo_claude.config.plugins import discover_plugin_mcp_servers
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        info = add_fake_plugin(claude_dir, enabled=False)
        servers, _ = discover_plugin_mcp_servers(env={})
        ctx.check("a disabled plugin's servers are never discovered", info["wire_server_name"] not in servers)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


@test
def test_plugin_json_fallback_when_no_dot_mcp_json(ctx: Ctx):
    from rolo_claude.config.plugins import discover_plugin_mcp_servers
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        info = add_fake_plugin(claude_dir, via_plugin_json=True)
        ctx.check("no .mcp.json was written", not (info["plugin_root"] / ".mcp.json").exists())
        ctx.check(".claude-plugin/plugin.json was written",
                  (info["plugin_root"] / ".claude-plugin" / "plugin.json").exists())
        servers, _ = discover_plugin_mcp_servers(env={})
        ctx.check(f"still discovered via the plugin.json fallback, got {list(servers)}",
                  info["wire_server_name"] in servers)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


@test
def test_missing_manifest_degrades_gracefully(ctx: Ctx):
    from rolo_claude.config.plugins import discover_plugin_mcp_servers, load_installed_plugins
    claude_dir = _fake_claude_dir()  # no plugins/ dir at all
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        ctx.check("missing manifest -> {}", load_installed_plugins() == {})
        servers, notices = discover_plugin_mcp_servers(env={})
        ctx.check("no servers, no crash", servers == {} and notices == [])
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


@test
def test_malformed_manifest_json_degrades_gracefully(ctx: Ctx):
    from rolo_claude.config.plugins import discover_plugin_mcp_servers, plugins_dir
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        p = plugins_dir()
        p.mkdir(parents=True, exist_ok=True)
        (p / "installed_plugins.json").write_text("{not valid json", encoding="utf-8")
        servers, notices = discover_plugin_mcp_servers(env={})
        ctx.check("malformed manifest -> no servers, never raises", servers == {})
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


@test
def test_stale_manifest_entry_with_missing_directory_is_skipped(ctx: Ctx):
    from rolo_claude.config.plugins import discover_plugin_mcp_servers, installed_plugins_manifest_path
    import json
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        manifest_path = installed_plugins_manifest_path()
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({"plugins": {"ghost-plugin": {"enabled": True}}}), encoding="utf-8")
        servers, notices = discover_plugin_mcp_servers(env={})
        ctx.check("a plugin whose directory doesn't exist is silently skipped", servers == {} and notices == [])
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


# ---- end to end: resolve_server_configs + a REAL live connection ----------

@test
def test_resolve_server_configs_merges_plugin_servers(ctx: Ctx):
    from rolo_claude.config.plugins import discover_plugin_mcp_servers
    from rolo_claude.mcp import manager as M
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        info = add_fake_plugin(claude_dir)
        plugin_servers, _ = discover_plugin_mcp_servers(env={})
        resolved, _ = M.resolve_server_configs(cwd=Path("/tmp/plugintest"), claude_json={},
                                                  plugin_servers=plugin_servers)
        ctx.check(f"the plugin server is present in the final resolution, got {list(resolved)}",
                  info["wire_server_name"] in resolved)
        ctx.check("a same-named user-scope server still wins over the plugin one (lowest config-file precedence)",
                  True)  # documented by test_plugin_servers_are_lowest_config_precedence below
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


@test
def test_plugin_servers_are_lowest_config_precedence(ctx: Ctx):
    from rolo_claude.mcp import manager as M
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        info = add_fake_plugin(claude_dir)
        plugin_servers = {info["wire_server_name"]: M.McpServerConfig(
            name=info["wire_server_name"], type="stdio", command="plugin-cmd", scope="plugin")}
        claude_json = {"mcpServers": {info["wire_server_name"]: {"command": "user-cmd"}}}
        resolved, _ = M.resolve_server_configs(cwd=Path("/tmp/plugintest2"), claude_json=claude_json,
                                                  plugin_servers=plugin_servers)
        ctx.check("a real user-scope entry wins over a same-named plugin one",
                  resolved[info["wire_server_name"]].command == "user-cmd")
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


@test
def test_plugin_server_survives_a_real_connection_with_namespaced_tool_names(ctx: Ctx):
    """The full pipeline: discover -> resolve -> McpManager connects for
    real -> tool names on the wire are mcp__plugin_<plugin>_<server>__<tool>."""
    from rolo_claude.config.plugins import discover_plugin_mcp_servers
    from rolo_claude.mcp.manager import McpManager, resolve_server_configs
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        info = add_fake_plugin(claude_dir)
        plugin_servers, _ = discover_plugin_mcp_servers(env=dict(os.environ))
        resolved, _ = resolve_server_configs(cwd=REPO_DIR, claude_json={}, plugin_servers=plugin_servers,
                                               env_for_expansion=dict(os.environ))
        mgr = McpManager(resolved, tool_env=dict(os.environ), cwd=REPO_DIR)
        try:
            mgr.start_all()
            status = mgr.status()[0]
            ctx.check(f"the plugin server actually connects, got {status}", status["state"] == "connected")
            all_tools = mgr.all_tools()
            names = [t[1] for t in all_tools]
            expected_prefix = f"mcp__{info['wire_server_name']}__"
            ctx.check(f"tool names carry the plugin-namespaced server, got {names}",
                      any(n.startswith(expected_prefix) for n in names))
            echo_name = f"{expected_prefix}echo"
            ctx.check(f"the fake server's own 'echo' tool is reachable as {echo_name!r}", echo_name in names)
            result = mgr.call(info["wire_server_name"], "echo", {"text": "plugin round trip"})
            ctx.check("a real call through the plugin-provided server works",
                      result.content[0].text == "plugin round trip")
        finally:
            mgr.close_all()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
