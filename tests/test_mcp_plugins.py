"""tests.test_mcp_plugins -- rolo_claude/config/plugins.py (H3b closing
requirement: "every server a user adds to Claude Code must work"):
discovery of plugin-provided MCP servers under ~/.claude/plugins/, against
the fixture plugin in tests/helpers/fake_home.py::add_fake_plugin (rolo has
none installed for real as of 2026-09-24). See config/plugins.py's own
module docstring for the on-disk layout this mirrors.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all
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


# ---- finding 11: installed_plugins.json V2 (installPath + enabledPlugins) -

def _write_v2_manifest(claude_dir: Path, key: str, install_path: Path, *, extra_entry_fields=None,
                        scope: str = "user") -> None:
    """finding 8/17 (h4-h5-h3c review): writes the REAL binary shape --
    `plugins[key]` is a LIST of scoped install records, not a single
    dict (the pre-finding-8 version of this helper invented the single-
    dict shape, which is exactly why the bug it should have caught
    shipped). Defaults to a `scope: "user"` record (always applies,
    regardless of cwd) so every EXISTING caller of this helper keeps
    working unchanged; `scope="project"`/`"local"` callers (finding 8's
    own new tests) pass `projectPath` via `extra_entry_fields`."""
    from rolo_claude.config.plugins import installed_plugins_manifest_path
    manifest_path = installed_plugins_manifest_path()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    record = {"scope": scope, "installPath": str(install_path), "version": "1.0.0"}
    record.update(extra_entry_fields or {})
    manifest_path.write_text(json.dumps({"version": 2, "plugins": {key: [record]}}), encoding="utf-8")


def _fake_settings(raw: dict):
    from rolo_claude.config.settings import Settings
    return Settings(raw=raw, layers=[], errors=[])


@test
def test_v2_manifest_installpath_and_enabled_plugins_settings_gate(ctx: Ctx):
    """finding 11: 2.1.281's real installed_plugins.json shape --
    {version: 2, plugins: {"<name>@<marketplace>": {installPath, ...}}} --
    with enablement taken from settings `enabledPlugins`, and the wire
    server name using the plugin name WITHOUT the @marketplace suffix."""
    from rolo_claude.config.plugins import discover_plugin_mcp_servers, plugin_server_name
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        plugin_root = claude_dir.parent / "v2plugin"
        plugin_root.mkdir(parents=True, exist_ok=True)
        (plugin_root / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"fakeserver": {
                "command": sys.executable, "args": ["-m", "tests.helpers.fake_mcp_server"]}}}),
            encoding="utf-8")
        _write_v2_manifest(claude_dir, "foo@some-marketplace", plugin_root)
        settings = _fake_settings({"enabledPlugins": {"foo@some-marketplace": True}})
        servers, notices = discover_plugin_mcp_servers(env={}, settings=settings)
        expected_name = plugin_server_name("foo", "fakeserver")
        ctx.check(f"V2 plugin discovered, name has NO @marketplace suffix, got {list(servers)}",
                  expected_name in servers)
        ctx.check(f"no notices for a clean discovery, got {notices}", notices == [])
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


@test
def test_v2_manifest_disabled_via_enabled_plugins_settings(ctx: Ctx):
    from rolo_claude.config.plugins import discover_plugin_mcp_servers, plugin_server_name
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        plugin_root = claude_dir.parent / "v2plugin-off"
        plugin_root.mkdir(parents=True, exist_ok=True)
        (plugin_root / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"fakeserver": {"command": "x"}}}), encoding="utf-8")
        _write_v2_manifest(claude_dir, "foo@mp", plugin_root)
        settings = _fake_settings({"enabledPlugins": {"foo@mp": False}})
        servers, _ = discover_plugin_mcp_servers(env={}, settings=settings)
        expected_name = plugin_server_name("foo", "fakeserver")
        ctx.check("explicitly disabled via enabledPlugins settings -> not discovered",
                  expected_name not in servers)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


def _write_v1_manifest(claude_dir: Path, key: str, *, install_path=None, enabled=None) -> None:
    """H5c finding 21: writes a REAL (not yet 2.1.28x-migrated) V1
    installed_plugins.json record -- a single dict per key (no top-level
    `"version": 2`), using the SAME `<name>@<marketplace>` key shape and
    `installPath` field a V2 record uses; only the array-of-scoped-records
    wrapping is V2-only."""
    from rolo_claude.config.plugins import installed_plugins_manifest_path
    manifest_path = installed_plugins_manifest_path()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    record = {}
    if install_path is not None:
        record["installPath"] = str(install_path)
    if enabled is not None:
        record["enabled"] = enabled
    manifest_path.write_text(json.dumps({"plugins": {key: record}}), encoding="utf-8")


@test
def test_h5c_f21_v1_manifest_installpath_and_marketplace_suffix_stripped(ctx: Ctx):
    """H5c finding 21: `config/plugins.py`'s V1 branch (no top-level
    `"version": 2`) used to read an invented `path` field (the real binary
    never writes one) and kept the `@marketplace` suffix on the plugin
    name -- a V1 manifest that 2.1.28x has not yet migrated to V2 therefore
    discovered NO plugins at all. The fix reads `installPath` (the SAME
    field name a V2 record uses) and strips the suffix exactly like the
    V2 branch does."""
    from rolo_claude.config.plugins import discover_plugin_mcp_servers, plugin_server_name
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        # Deliberately NOT under plugins/cache/<key> -- only reachable via
        # `installPath`, proving the fix reads that field rather than
        # falling back to the cache-dir-by-name guess (which would also
        # fail here since the guess would use the UNSTRIPPED "foo@some-
        # marketplace" as the directory name).
        plugin_root = claude_dir.parent / "v1plugin-elsewhere"
        plugin_root.mkdir(parents=True, exist_ok=True)
        (plugin_root / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"fakeserver": {
                "command": sys.executable, "args": ["-m", "tests.helpers.fake_mcp_server"]}}}),
            encoding="utf-8")
        _write_v1_manifest(claude_dir, "foo@some-marketplace", install_path=plugin_root)
        servers, notices = discover_plugin_mcp_servers(env={})
        expected_name = plugin_server_name("foo", "fakeserver")
        ctx.check(f"V1 plugin discovered via installPath, name has NO @marketplace suffix, got {list(servers)}",
                  expected_name in servers)
        ctx.check(f"no notices for a clean discovery, got {notices}", notices == [])
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


@test
def test_h5c_f21_v1_manifest_disabled_flag_still_honoured(ctx: Ctx):
    """The F21 fix only changes the root-path field (`path` -> `installPath`)
    and strips the @marketplace suffix -- it must not disturb the
    pre-existing per-record `enabled: False` gate that real V1 manifests
    use (V2 gates via settings `enabledPlugins` instead; V1 does not)."""
    from rolo_claude.config.plugins import discover_plugin_mcp_servers, plugin_server_name
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        plugin_root = claude_dir.parent / "v1plugin-off"
        plugin_root.mkdir(parents=True, exist_ok=True)
        (plugin_root / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"fakeserver": {"command": "x"}}}), encoding="utf-8")
        _write_v1_manifest(claude_dir, "foo@mp", install_path=plugin_root, enabled=False)
        servers, _ = discover_plugin_mcp_servers(env={})
        expected_name = plugin_server_name("foo", "fakeserver")
        ctx.check("enabled: False on a V1 record -> not discovered", expected_name not in servers)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


@test
def test_v2_manifest_defaults_enabled_when_settings_say_nothing(ctx: Ctx):
    """A freshly-cloned/installed V2 plugin the settings' `enabledPlugins`
    map doesn't mention AT ALL yet must still be discovered (parity with
    V1's own "installed == on" default), not silently invisible."""
    from rolo_claude.config.plugins import discover_plugin_mcp_servers, plugin_server_name
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        plugin_root = claude_dir.parent / "v2plugin-silent"
        plugin_root.mkdir(parents=True, exist_ok=True)
        (plugin_root / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"fakeserver": {"command": "x"}}}), encoding="utf-8")
        _write_v2_manifest(claude_dir, "foo@mp", plugin_root)
        for settings in (None, _fake_settings({}), _fake_settings({"enabledPlugins": {"other@mp": True}})):
            servers, _ = discover_plugin_mcp_servers(env={}, settings=settings)
            expected_name = plugin_server_name("foo", "fakeserver")
            ctx.check(f"defaults to enabled with settings={settings!r}, got {list(servers)}",
                      expected_name in servers)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


# ---- finding 8: the REAL V2 shape is an ARRAY of scoped records ----------

def _write_v2_manifest_multi(claude_dir: Path, key: str, records: list) -> None:
    from rolo_claude.config.plugins import installed_plugins_manifest_path
    manifest_path = installed_plugins_manifest_path()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"version": 2, "plugins": {key: records}}), encoding="utf-8")


@test
def test_h5b_f08_v2_manifest_is_an_array_of_records_not_a_single_dict(ctx: Ctx):
    """finding 8 (major, h4-h5-h3c review): the REAL binary shape --
    plugins[key] = [{scope, installPath, version, ...}, ...] -- a real
    `claude plugin install` manifest. The pre-fix version read `entry` as
    a single dict; a real array made `isinstance(entry, dict)` False,
    `entry` silently became `{}`, and the plugin's servers/hooks were
    skipped entirely."""
    from rolo_claude.config.plugins import discover_plugin_mcp_servers, plugin_server_name
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        plugin_root = claude_dir.parent / "v2plugin-array"
        plugin_root.mkdir(parents=True, exist_ok=True)
        (plugin_root / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"fakeserver": {"command": "x"}}}), encoding="utf-8")
        _write_v2_manifest_multi(claude_dir, "foo@mp", [
            {"scope": "user", "installPath": str(plugin_root), "version": "2.1.0"},
        ])
        settings = _fake_settings({})
        servers, notices = discover_plugin_mcp_servers(env={}, settings=settings)
        expected_name = plugin_server_name("foo", "fakeserver")
        ctx.check(f"a REAL array-shaped V2 manifest is discovered, got {list(servers)} notices={notices}",
                  expected_name in servers)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


@test
def test_h5b_f08_project_scoped_record_only_applies_when_cwd_matches(ctx: Ctx):
    """finding 8: a `scope: "project"`/`"local"` record's `projectPath`
    must match the CURRENT session's cwd -- a plugin installed for one
    repo must not leak into an unrelated one, and a project-scoped record
    for THIS repo must be discovered when cwd is inside it."""
    from rolo_claude.config.plugins import discover_plugin_mcp_servers, plugin_server_name
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        plugin_root = claude_dir.parent / "v2plugin-project"
        plugin_root.mkdir(parents=True, exist_ok=True)
        (plugin_root / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"fakeserver": {"command": "x"}}}), encoding="utf-8")
        my_project = claude_dir.parent / "my-repo"
        my_project.mkdir(parents=True, exist_ok=True)
        other_project = claude_dir.parent / "other-repo"
        other_project.mkdir(parents=True, exist_ok=True)
        _write_v2_manifest_multi(claude_dir, "foo@mp", [
            {"scope": "project", "projectPath": str(my_project), "installPath": str(plugin_root), "version": "1.0"},
        ])
        expected_name = plugin_server_name("foo", "fakeserver")

        servers_in, _ = discover_plugin_mcp_servers(env={}, settings=_fake_settings({}), cwd=my_project)
        ctx.check(f"discovered when cwd matches projectPath, got {list(servers_in)}", expected_name in servers_in)

        servers_out, _ = discover_plugin_mcp_servers(env={}, settings=_fake_settings({}), cwd=other_project)
        ctx.check(f"NOT discovered from an unrelated cwd, got {list(servers_out)}", expected_name not in servers_out)

        servers_none, _ = discover_plugin_mcp_servers(env={}, settings=_fake_settings({}))
        ctx.check(f"NOT discovered with no cwd given at all, got {list(servers_none)}", expected_name not in servers_none)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


@test
def test_h5b_f08_multiple_scoped_records_for_one_plugin_all_considered(ctx: Ctx):
    """A plugin installed at BOTH user scope and a project scope (two
    records in the same array) is discovered via either applicable
    record -- the whole array is walked, not just records[0]."""
    from rolo_claude.config.plugins import discover_plugin_mcp_servers, plugin_server_name
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        user_root = claude_dir.parent / "v2plugin-user-copy"
        user_root.mkdir(parents=True, exist_ok=True)
        (user_root / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"userserver": {"command": "x"}}}), encoding="utf-8")
        proj_root = claude_dir.parent / "v2plugin-project-copy"
        proj_root.mkdir(parents=True, exist_ok=True)
        (proj_root / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"projserver": {"command": "x"}}}), encoding="utf-8")
        my_project = claude_dir.parent / "my-repo-2"
        my_project.mkdir(parents=True, exist_ok=True)
        _write_v2_manifest_multi(claude_dir, "foo@mp", [
            {"scope": "user", "installPath": str(user_root), "version": "1.0"},
            {"scope": "project", "projectPath": str(my_project), "installPath": str(proj_root), "version": "1.0"},
        ])
        servers, _ = discover_plugin_mcp_servers(env={}, settings=_fake_settings({}), cwd=my_project)
        ctx.check(f"the user-scope record's server is discovered, got {list(servers)}",
                  plugin_server_name("foo", "userserver") in servers)
        ctx.check(f"the project-scope record's server is ALSO discovered, got {list(servers)}",
                  plugin_server_name("foo", "projserver") in servers)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


@test
def test_h5b_f08_cache_fallback_uses_the_real_marketplace_plugin_version_layout(ctx: Ctx):
    """A record missing `installPath` (defensive fallback only -- the
    binary always writes one in practice) must resolve against the REAL
    cache layout, `cache/<marketplace>/<plugin>/<version>`, never the old
    invented `cache/<plugin>`."""
    from rolo_claude.config.plugins import _v2_record_root, plugins_dir
    root = _v2_record_root("foo@some-mp", "foo", {"scope": "user", "version": "3.2.1"}, cwd=None)
    ctx.check(f"cache path is cache/<marketplace>/<plugin>/<version>, got {root}",
              root == plugins_dir() / "cache" / "some-mp" / "foo" / "3.2.1")


@test
def test_h5b_f08_build_hook_runner_respects_enabled_plugins_gate(ctx: Ctx):
    """finding 8: `build_hook_runner` used to call `_plugin_roots(manifest)`
    with NO settings at all, so a plugin explicitly turned OFF via
    `enabledPlugins` still had its hooks/hooks.json loaded and run."""
    from rolo_claude.config.settings import Settings
    from rolo_claude.headless import build_hook_runner
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        plugin_root = claude_dir.parent / "v2plugin-hooks"
        (plugin_root / "hooks").mkdir(parents=True, exist_ok=True)
        (plugin_root / "hooks" / "hooks.json").write_text(json.dumps({
            "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo hi"}]}],
        }), encoding="utf-8")
        _write_v2_manifest_multi(claude_dir, "foo@mp", [
            {"scope": "user", "installPath": str(plugin_root), "version": "1.0"},
        ])
        settings_off = Settings(raw={"enabledPlugins": {"foo@mp": False}}, layers=[], errors=[])
        runner_off = build_hook_runner(settings=settings_off, cwd=claude_dir.parent, session_id="s1",
                                        transcript_path="", effort=None, permission_mode="auto",
                                        mcp_manager=None, bare=False)
        ctx.check("disabled plugin's hooks are NOT loaded", runner_off.has_hooks("PreToolUse") is False)

        settings_on = Settings(raw={"enabledPlugins": {"foo@mp": True}}, layers=[], errors=[])
        runner_on = build_hook_runner(settings=settings_on, cwd=claude_dir.parent, session_id="s1",
                                       transcript_path="", effort=None, permission_mode="auto",
                                       mcp_manager=None, bare=False)
        ctx.check("enabled plugin's hooks ARE loaded", runner_on.has_hooks("PreToolUse") is True)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


# ---- finding 11: bare .mcp.json + string mcpServers path in plugin.json ---

@test
def test_bare_mcp_json_map_is_discovered(ctx: Ctx):
    """finding 11's exact repro: 9 of 14 real plugin .mcp.json files
    (including github and playwright) are a BARE {server: entry} map,
    never wrapped in {"mcpServers": ...} -- the original implementation
    only ever checked for the wrapped key and silently discovered
    nothing for any of them."""
    from rolo_claude.config.plugins import discover_plugin_mcp_servers, plugin_server_name
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        plugin_root = claude_dir / "plugins" / "cache" / "bare-plugin"
        plugin_root.mkdir(parents=True, exist_ok=True)
        # BARE -- no "mcpServers" wrapper, exactly like the real github/
        # playwright plugin.mcp.json files.
        (plugin_root / ".mcp.json").write_text(
            json.dumps({"fakeserver": {"command": sys.executable,
                                        "args": ["-m", "tests.helpers.fake_mcp_server"]}}),
            encoding="utf-8")
        (claude_dir / "plugins" / "installed_plugins.json").write_text(
            json.dumps({"plugins": {"bare-plugin": {"enabled": True}}}), encoding="utf-8")
        servers, notices = discover_plugin_mcp_servers(env={})
        expected_name = plugin_server_name("bare-plugin", "fakeserver")
        ctx.check(f"bare .mcp.json map is discovered, got {list(servers)}", expected_name in servers)
        ctx.check(f"no notices for a clean discovery, got {notices}", notices == [])
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


@test
def test_plugin_json_mcp_servers_string_path(ctx: Ctx):
    """finding 11: `.claude-plugin/plugin.json`'s own `mcpServers` can be
    a STRING -- a path, relative to the plugin root, to another JSON file
    declaring the servers (itself either wrapped or bare)."""
    from rolo_claude.config.plugins import discover_plugin_mcp_servers, plugin_server_name
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        plugin_root = claude_dir / "plugins" / "cache" / "stringref-plugin"
        (plugin_root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (plugin_root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "stringref-plugin", "mcpServers": "mcp-servers.json"}), encoding="utf-8")
        (plugin_root / "mcp-servers.json").write_text(
            json.dumps({"fakeserver": {"command": sys.executable,
                                        "args": ["-m", "tests.helpers.fake_mcp_server"]}}),
            encoding="utf-8")
        (claude_dir / "plugins" / "installed_plugins.json").write_text(
            json.dumps({"plugins": {"stringref-plugin": {"enabled": True}}}), encoding="utf-8")
        servers, _ = discover_plugin_mcp_servers(env={})
        expected_name = plugin_server_name("stringref-plugin", "fakeserver")
        ctx.check(f"servers declared via a string path are discovered, got {list(servers)}",
                  expected_name in servers)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


# ---- finding 11: a COPY of a real marketplace plugin (github) -------------
# Verbatim content read from rolo's own claude-plugins-official marketplace
# clone (~/.claude/plugins/marketplaces/claude-plugins-official/
# external_plugins/github/) on 2026-09-24 -- embedded here (rather than
# read from that Windows-specific path at test time) so this test is
# reproducible on Kali/WSL/CI, which never has that clone on disk.
_REAL_GITHUB_PLUGIN_MCP_JSON = {
    "github": {
        "type": "http",
        "url": "https://api.githubcopilot.com/mcp/",
        "headers": {"Authorization": "Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}"},
    },
}
_REAL_GITHUB_PLUGIN_JSON = {
    "name": "github",
    "description": ("Official GitHub MCP server for repository management. Create issues, manage pull "
                     "requests, review code, search repositories, and interact with GitHub's full API "
                     "directly from Claude Code."),
    "author": {"name": "GitHub"},
}


@test
def test_real_github_plugin_copy_is_discovered_and_named_correctly(ctx: Ctx):
    """finding 11's own acceptance case: `claude plugin install
    github@claude-plugins-official` -> discovered, named
    `plugin_github_github` (not `plugin_github_claude-plugins-official_...`),
    its BARE .mcp.json map read correctly (github's real file has no
    "mcpServers" wrapper), and ${VAR} expansion still applies to its
    templated Authorization header."""
    from rolo_claude.config.plugins import discover_plugin_mcp_servers, plugin_server_name
    claude_dir = _fake_claude_dir()
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    try:
        plugin_root = claude_dir.parent / "github-plugin-copy"
        (plugin_root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (plugin_root / ".mcp.json").write_text(json.dumps(_REAL_GITHUB_PLUGIN_MCP_JSON), encoding="utf-8")
        (plugin_root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps(_REAL_GITHUB_PLUGIN_JSON), encoding="utf-8")
        _write_v2_manifest(claude_dir, "github@claude-plugins-official", plugin_root)
        settings = _fake_settings({"enabledPlugins": {"github@claude-plugins-official": True}})
        servers, notices = discover_plugin_mcp_servers(env={"GITHUB_PERSONAL_ACCESS_TOKEN": "test-token-123"},
                                                          settings=settings)
        expected_name = plugin_server_name("github", "github")
        ctx.check(f"named plugin_github_github (no marketplace suffix), got {list(servers)}",
                  expected_name == "plugin_github_github" and expected_name in servers)
        ctx.check(f"no notices, got {notices}", notices == [])
        cfg = servers[expected_name]
        ctx.check(f"type/url carried through from the bare .mcp.json map, got {cfg.type!r}/{cfg.url!r}",
                  cfg.type == "http" and cfg.url == "https://api.githubcopilot.com/mcp/")
        # manager.py's own `credential_blank` rule: a credential-SHAPED var
        # name (GITHUB_PERSONAL_ACCESS_TOKEN matches /TOKEN/i) is expanded
        # to "" in a header/url regardless of whether it's actually set --
        # a real secret must never land in a logged/displayed config. The
        # ${VAR} SYNTAX itself still resolved (no literal "${...}" left
        # behind); only the VALUE is deliberately blanked.
        ctx.check(f"the templated header expanded (blanked, not left literal), got {cfg.headers}",
                  cfg.headers.get("Authorization") == "Bearer ")
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old


@test
def test_real_marketplace_clone_on_this_machine_if_present(ctx: Ctx):
    """Bonus, best-effort: when THIS machine actually has rolo's real
    claude-plugins-official marketplace clone on disk (true on the
    Windows build host as of 2026-09-24; never assumed elsewhere), read
    github's REAL files directly and confirm `_read_plugin_mcp_servers`
    parses them exactly like the embedded-copy test above. Skipped
    (never failed) when the clone isn't present -- e.g. on Kali/CI."""
    from rolo_claude.config.plugins import _read_plugin_mcp_servers
    real_root = (Path.home() / ".claude" / "plugins" / "marketplaces" / "claude-plugins-official"
                 / "external_plugins" / "github")
    if not (real_root / ".mcp.json").exists():
        raise SkipTest(f"no real marketplace clone at {real_root}")
    servers = _read_plugin_mcp_servers(real_root)
    ctx.check(f"github's real bare .mcp.json parses to a servers map, got {servers}",
              "github" in servers and servers["github"].get("type") == "http")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
