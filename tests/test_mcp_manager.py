"""tests.test_mcp_manager -- halo_harness/mcp/{client,stdio,http_sse,
manager}.py (H3 scope A): name sanitising, ${VAR}/${VAR:-d} expansion +
credential blanking, timeout resolution, McpServerConfig parsing, scope
resolution precedence + approval states, McpLoop, and live connections
through tests/helpers/fake_mcp_server.py (a real stdio JSON-RPC server, not
a mock -- MCP_TIMEOUT-bounded failure, crash, parallel startup, call/
status/reconnect/close_all).
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.mcp import manager as M

REPO_DIR = Path(__file__).resolve().parent.parent
FAKE_SERVER_ARGS = [sys.executable, "-m", "tests.helpers.fake_mcp_server"]

test, TESTS = new_registry()


# ---- name sanitising --------------------------------------------------------

@test
def test_sanitize_name_replaces_invalid_chars(ctx: Ctx):
    ctx.check("dots/spaces/slashes -> underscore",
              M.sanitize_name("my.server name/v2") == "my_server_name_v2")
    ctx.check("hyphen and underscore pass through unchanged",
              M.sanitize_name("expanded-models_v1") == "expanded-models_v1")


@test
def test_mcp_tool_name_format(ctx: Ctx):
    ctx.check("mcp__<server>__<tool>", M.mcp_tool_name("srv", "get_x") == "mcp__srv__get_x")
    ctx.check("both halves sanitised", M.mcp_tool_name("my srv", "get x") == "mcp__my_srv__get_x")


@test
def test_split_mcp_tool_name_first_double_underscore(ctx: Ctx):
    ctx.check("splits on the FIRST __ after mcp__",
              M.split_mcp_tool_name("mcp__srv__get_the_thing") == ("srv", "get_the_thing"))
    ctx.check("non-mcp name -> None", M.split_mcp_tool_name("Bash") is None)


# ---- ${VAR} / ${VAR:-default} expansion ------------------------------------

@test
def test_expand_string_plain_var(ctx: Ctx):
    ctx.check("substitutes a set var", M.expand_string("hello ${NAME}", {"NAME": "world"}) == "hello world")


@test
def test_expand_string_default_when_unset(ctx: Ctx):
    ctx.check("uses the :-default when unset", M.expand_string("${MISSING:-fallback}", {}) == "fallback")


@test
def test_expand_string_unset_no_default_left_verbatim_plus_warning(ctx: Ctx):
    warnings = []
    result = M.expand_string("${MISSING}", {}, warnings=warnings)
    ctx.check("unset+no-default left verbatim", result == "${MISSING}")
    ctx.check("a warning was recorded", len(warnings) == 1 and "MISSING" in warnings[0])


@test
def test_expand_string_credential_blanking(ctx: Ctx):
    env = {"API_TOKEN": "super-secret", "HOST": "example.com"}
    blanked = M.expand_string("https://${HOST}/?key=${API_TOKEN}", env, credential_blank=True)
    ctx.check("credential-shaped var blanked", blanked == "https://example.com/?key=")
    unblanked = M.expand_string("${API_TOKEN}", env, credential_blank=False)
    ctx.check("without credential_blank, the real value comes through", unblanked == "super-secret")


@test
def test_expand_config_credential_blanking_only_url_and_headers(ctx: Ctx):
    cfg = M.McpServerConfig(name="s", type="http", url="https://x/${TOKEN}", headers={"Authorization": "Bearer ${TOKEN}"},
                             command="${TOKEN}", env={"K": "${TOKEN}"})
    expanded, warns = M.expand_config(cfg, {"TOKEN": "shh"})
    ctx.check("url blanks the credential var", expanded.url == "https://x/")
    ctx.check("headers blank the credential var", expanded.headers["Authorization"] == "Bearer ")
    ctx.check("command is NOT credential-blanked (not remote url/headers)", expanded.command == "shh")
    ctx.check("env is NOT credential-blanked", expanded.env["K"] == "shh")


# ---- timeouts ---------------------------------------------------------------

@test
def test_mcp_timeout_default_and_env_override(ctx: Ctx):
    old = os.environ.pop("MCP_TIMEOUT", None)
    try:
        ctx.check("default 30000ms", M.mcp_timeout_ms() == 30_000)
        os.environ["MCP_TIMEOUT"] = "5000"
        ctx.check("env override honoured", M.mcp_timeout_ms() == 5000)
        os.environ["MCP_TIMEOUT"] = "not-a-number"
        ctx.check("garbage value falls back to default", M.mcp_timeout_ms() == 30_000)
    finally:
        if old is None:
            os.environ.pop("MCP_TIMEOUT", None)
        else:
            os.environ["MCP_TIMEOUT"] = old


@test
def test_mcp_connect_timeout_default(ctx: Ctx):
    old = os.environ.pop("MCP_CONNECT_TIMEOUT_MS", None)
    try:
        ctx.check("default 5000ms -> 5.0s", M.mcp_connect_timeout_s() == 5.0)
    finally:
        if old is not None:
            os.environ["MCP_CONNECT_TIMEOUT_MS"] = old


@test
def test_tool_timeout_precedence(ctx: Ctx):
    old = os.environ.pop("MCP_TOOL_TIMEOUT", None)
    try:
        ctx.check("server timeout (>=1000ms) wins", M.tool_timeout_s(2000) == 2.0)
        ctx.check("server timeout <1000ms is ignored (falls through)",
                  M.tool_timeout_s(500) != 0.5)
        os.environ["MCP_TOOL_TIMEOUT"] = "9000"
        ctx.check("MCP_TOOL_TIMEOUT env used when no valid server timeout", M.tool_timeout_s(None) == 9.0)
        del os.environ["MCP_TOOL_TIMEOUT"]
        ctx.check("falls back to the ~27.8h default when nothing else set",
                  M.tool_timeout_s(None) == min(int(1e8), 2**31 - 1) / 1000.0)
    finally:
        if old is not None:
            os.environ["MCP_TOOL_TIMEOUT"] = old
        else:
            os.environ.pop("MCP_TOOL_TIMEOUT", None)


# ---- parse_server -----------------------------------------------------------

@test
def test_parse_server_infers_stdio_from_command(ctx: Ctx):
    cfg = M.parse_server("s", {"command": "python", "args": ["-m", "x"]}, scope="user")
    ctx.check("type inferred as stdio", cfg.type == "stdio")
    ctx.check("command/args carried through", cfg.command == "python" and cfg.args == ["-m", "x"])


@test
def test_parse_server_url_without_type_is_invalid(ctx: Ctx):
    cfg = M.parse_server("s", {"url": "https://x"}, scope="user")
    ctx.check("a url with no type is invalid [D-CFG]", cfg.type == "invalid")
    ctx.check("disabled_reason explains why", "url" in (cfg.disabled_reason or "").lower())


@test
def test_parse_server_streamable_http_alias(ctx: Ctx):
    cfg = M.parse_server("s", {"type": "streamable-http", "url": "https://x"}, scope="user")
    ctx.check("streamable-http normalised to http", cfg.type == "http")


@test
def test_parse_server_ws_and_sdk_skipped_with_reason(ctx: Ctx):
    for stype in ("ws", "websocket", "sdk"):
        cfg = M.parse_server("s", {"type": stype, "url": "wss://x"}, scope="user")
        ctx.check(f"{stype} server disabled with a reason", cfg.type == stype and cfg.disabled_reason)


@test
def test_websocket_client_available_is_false_against_the_real_pinned_sdk(ctx: Ctx):
    """Verified, not assumed: `mcp==2.2.0` (requirements.lock) has no
    `mcp.client.websocket` module at all."""
    ctx.check("mcp 2.2.0 has no websocket client", M.websocket_client_available() is False)


@test
def test_ws_and_sdk_get_distinct_reasons(ctx: Ctx):
    ws_cfg = M.parse_server("s", {"type": "ws", "url": "wss://x"}, scope="user")
    sdk_cfg = M.parse_server("s", {"type": "sdk", "url": "x"}, scope="user")
    ctx.check(f"ws names the SDK limitation, got {ws_cfg.disabled_reason!r}",
              "websocket client" in ws_cfg.disabled_reason.lower())
    ctx.check(f"sdk names the structural reason, got {sdk_cfg.disabled_reason!r}",
              "agent sdk" in sdk_cfg.disabled_reason.lower())
    ctx.check("the two reasons are not the same text", ws_cfg.disabled_reason != sdk_cfg.disabled_reason)


@test
def test_ws_becomes_a_real_config_once_the_sdk_supports_it(ctx: Ctx):
    """Forward-looking: once a future `mcp` version ships a websocket
    client, `ws`/`websocket` entries stop being disabled on their own --
    `sdk` never does (see test_ws_and_sdk_get_distinct_reasons)."""
    old = M.websocket_client_available
    M.websocket_client_available = lambda: True
    try:
        cfg = M.parse_server("s", {"type": "ws", "url": "wss://example/mcp"}, scope="user")
        ctx.check(f"no longer disabled, got disabled_reason={cfg.disabled_reason!r}", cfg.disabled_reason is None)
        ctx.check("type preserved", cfg.type == "ws")
        ctx.check("url carried through like http/sse", cfg.url == "wss://example/mcp")

        sdk_cfg = M.parse_server("s", {"type": "sdk", "url": "x"}, scope="user")
        ctx.check("'sdk' is STILL disabled even when ws becomes available",
                  sdk_cfg.disabled_reason is not None)
    finally:
        M.websocket_client_available = old


@test
def test_disabled_handle_state_and_error_carry_the_reason(ctx: Ctx):
    """W4b "explain the zero"/WHY: a disabled handle's `.error` is its own
    `disabled_reason`, so `mcp list`/`/mcp`/doctor's WHY display covers a
    disabled server the same way it covers a real connect failure."""
    import asyncio
    from halo_harness.mcp.client import McpLoop
    cfg = M.parse_server("s", {"type": "sdk", "url": "x"}, scope="user")
    loop = McpLoop()
    try:
        h = M.McpServerHandle(cfg, loop, tool_env={}, cwd=REPO_DIR)
        ctx.check(f"state is disabled, got {h.state!r}", h.state == "disabled")
        ctx.check(f"error carries the disabled_reason, got {h.error!r}", h.error == cfg.disabled_reason)
        h.start()  # must be a safe no-op, never attempt to connect
        ctx.check("start() on a disabled handle never changes its state", h.state == "disabled")
    finally:
        loop.close()


@test
def test_mcp_cli_shows_the_disabled_reason_next_to_the_status(ctx: Ctx):
    from halo_harness.mcp_cli import format_mcp_list_line
    cfg = M.parse_server("s", {"type": "sdk", "url": "x"}, scope="user")
    line = format_mcp_list_line({"name": "s", "type": "sdk", "state": "disabled", "error": cfg.disabled_reason})
    ctx.check(f"the structural reason is shown, got {line!r}", "agent sdk" in line.lower())


@test
def test_parse_server_neither_command_nor_url(ctx: Ctx):
    cfg = M.parse_server("s", {}, scope="user")
    ctx.check("no command and no url -> invalid", cfg.type == "invalid")


@test
def test_parse_server_malformed_entry_returns_none(ctx: Ctx):
    ctx.check("a non-dict entry returns None (caller skips it)", M.parse_server("s", "not-a-dict", scope="user") is None)


@test
def test_parse_server_always_load_field(ctx: Ctx):
    cfg = M.parse_server("s", {"command": "x", "alwaysLoad": True}, scope="user")
    ctx.check("server-level alwaysLoad carried through", cfg.always_load is True)


# ---- scope resolution precedence [D-CFG] ------------------------------------

def _claude_json(*, user=None, local=None, cwd=None):
    data = {"mcpServers": user or {}}
    if local is not None and cwd is not None:
        data["projects"] = {str(cwd).replace("\\", "/"): {"mcpServers": local}}
    return data


@test
def test_resolve_local_wins_over_user(ctx: Ctx):
    cwd = Path("/tmp/proj")
    claude_json = _claude_json(
        user={"srv": {"command": "user-cmd"}},
        local={"srv": {"command": "local-cmd"}}, cwd=cwd,
    )
    resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json=claude_json)
    ctx.check(f"local scope wins, got {resolved['srv'].command!r}", resolved["srv"].command == "local-cmd")
    ctx.check("scope recorded as local", resolved["srv"].scope == "local")


@test
def test_resolve_dot_mcp_json_between_local_and_user(ctx: Ctx):
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        (cwd / ".mcp.json").write_text('{"mcpServers": {"srv": {"command": "project-cmd"}}}', encoding="utf-8")
        claude_json = _claude_json(user={"srv": {"command": "user-cmd"}})
        resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json=claude_json, print_mode=True)
        ctx.check(".mcp.json wins over user scope", resolved["srv"].command == "project-cmd")
        ctx.check("scope recorded as project", resolved["srv"].scope == "project")


@test
def test_resolve_user_is_the_fallback(ctx: Ctx):
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        claude_json = _claude_json(user={"srv": {"command": "user-cmd"}})
        resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json=claude_json)
        ctx.check("falls all the way to user scope", resolved["srv"].command == "user-cmd")


@test
def test_resolve_strict_mcp_config_only_consults_the_flag(ctx: Ctx):
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        cfg_path = cwd / "extra.json"
        cfg_path.write_text('{"mcpServers": {"flagged": {"command": "flag-cmd"}}}', encoding="utf-8")
        claude_json = _claude_json(user={"srv": {"command": "user-cmd"}})
        resolved, _ = M.resolve_server_configs(
            cwd=cwd, claude_json=claude_json, mcp_config_flag=[str(cfg_path)], strict_mcp_config=True,
        )
        ctx.check("only the --mcp-config entry is present", set(resolved) == {"flagged"})
        ctx.check("user-scope server never consulted under --strict-mcp-config", "srv" not in resolved)


@test
def test_resolve_mcp_config_flag_inline_json(ctx: Ctx):
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        resolved, _ = M.resolve_server_configs(
            cwd=cwd, claude_json={}, mcp_config_flag=['{"mcpServers": {"inline": {"command": "c"}}}'],
        )
        ctx.check("inline JSON --mcp-config value accepted", resolved["inline"].command == "c")


@test
def test_resolve_mcp_config_flag_long_inline_json_no_slash(ctx: Ctx):
    """finding 10: a long inline JSON value with no '/' used to hit
    `Path(spec).exists()` FIRST, which raises OSError (errno 36, "File
    name too long") on Linux -- verified in WSL. A JSON-shaped value (it
    starts with '{') must never touch the filesystem at all."""
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        padding = "x" * 400
        spec = '{"mcpServers": {"' + padding + '": {"command": "c"}}}'
        resolved, notices = M.resolve_server_configs(cwd=cwd, claude_json={}, mcp_config_flag=[spec])
        ctx.check(f"parses as JSON without ever touching the filesystem, got notices={notices}",
                  resolved.get(padding) is not None and resolved[padding].command == "c")


@test
def test_load_mcp_config_arg_never_raises_on_a_bad_path(ctx: Ctx):
    from halo_harness.mcp.manager import _load_mcp_config_arg
    with tempfile.TemporaryDirectory() as td:
        # a non-JSON-looking value that ALSO can't be checked as a path
        # cleanly on every OS (a NUL byte is invalid on both Windows and
        # POSIX) -- exists() wrapped in try/except must never propagate.
        entries, err = _load_mcp_config_arg("not-json-and-has-a-\x00-nul-byte", Path(td))
        ctx.check(f"degrades to a clear error, never raises, got entries={entries} err={err!r}",
                  entries == {} and err is not None)


@test
def test_resolve_managed_mcp_json_is_exclusive(ctx: Ctx):
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        managed_path = Path(td) / "managed-mcp.json"
        managed_path.write_text('{"mcpServers": {"managed-only": {"command": "m"}}}', encoding="utf-8")
        claude_json = _claude_json(user={"srv": {"command": "user-cmd"}})
        resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json=claude_json, managed_mcp_path=managed_path)
        ctx.check("ONLY the managed entry survives", set(resolved) == {"managed-only"})


@test
def test_resolve_disabled_mcpjson_servers_always_wins(ctx: Ctx):
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        (cwd / ".mcp.json").write_text('{"mcpServers": {"srv": {"command": "project-cmd"}}}', encoding="utf-8")
        claude_json = {"mcpServers": {}, "projects": {str(cwd).replace("\\", "/"): {
            "disabledMcpjsonServers": ["srv"], "enableAllProjectMcpServers": True,
        }}}
        resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json=claude_json, print_mode=True)
        ctx.check("disabledMcpjsonServers wins even though enableAllProjectMcpServers is set",
                  "srv" not in resolved)


@test
def test_resolve_project_disabled_mcp_servers_removes_user_scope(ctx: Ctx):
    cwd = Path("/tmp/proj2")
    claude_json = {"mcpServers": {"srv": {"command": "user-cmd"}},
                    "projects": {str(cwd).replace("\\", "/"): {"disabledMcpServers": ["srv"]}}}
    resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json=claude_json)
    ctx.check("projects[cwd].disabledMcpServers removes a user-scope entry", "srv" not in resolved)


@test
def test_resolve_dot_mcp_json_approval_states(ctx: Ctx):
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        (cwd / ".mcp.json").write_text(
            '{"mcpServers": {"a": {"command": "a"}, "b": {"command": "b"}, "c": {"command": "c"}}}',
            encoding="utf-8")
        claude_json = {"projects": {str(cwd).replace("\\", "/"): {"enabledMcpjsonServers": ["a"]}}}
        # print_mode=False, no enableAllProjectMcpServers, no our_approvals -> "b"/"c" are pending
        resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json=claude_json, print_mode=False)
        ctx.check("explicitly enabled server is approved", resolved["a"].pending_approval is False)
        ctx.check("un-enabled server is pending approval", resolved["b"].pending_approval is True)
        # our own approvals store also grants approval -- must-do: keyed by
        # the ENTRY's sha256 (mcp_approval_key), never the bare name, so an
        # edited/tampered entry can't silently ride on a stale approval.
        approval_key = M.mcp_approval_key({"command": "c"})
        resolved2, _ = M.resolve_server_configs(cwd=cwd, claude_json=claude_json, print_mode=False,
                                                  approvals={approval_key: True})
        ctx.check("our own approvals.json entry approves it too", resolved2["c"].pending_approval is False)
        ctx.check("a name-keyed (pre-must-do-shape) approval entry no longer matches anything",
                  M.resolve_server_configs(cwd=cwd, claude_json=claude_json, print_mode=False,
                                            approvals={"c": True})[0]["c"].pending_approval is True)


@test
def test_resolve_print_mode_approves_dot_mcp_json_without_asking(ctx: Ctx):
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        (cwd / ".mcp.json").write_text('{"mcpServers": {"a": {"command": "a"}}}', encoding="utf-8")
        resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json={}, print_mode=True)
        ctx.check("-p loads .mcp.json servers without asking [D-CFG]", resolved["a"].pending_approval is False)


@test
def test_resolve_extra_dynamic_is_flag_tier_precedence(ctx: Ctx):
    """finding 14: --chrome/--playwright rank at the SAME precedence as
    --mcp-config (a flag the user passed THIS run), not below user scope
    -- the old "lowest precedence" behavior let a static user-scope
    "playwright" entry silently override --playwright-cdp/--headless."""
    cwd = Path("/tmp/proj3")
    claude_json = _claude_json(user={"srv": {"command": "real-cmd"}})
    dynamic = {"srv": M.McpServerConfig(name="srv", type="stdio", command="dynamic-cmd", scope="dynamic"),
               "claude-in-chrome": M.McpServerConfig(name="claude-in-chrome", type="stdio", command="claude", scope="dynamic")}
    resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json=claude_json, extra_dynamic=dynamic)
    ctx.check("the flag-created dynamic entry now wins over a same-named user-scope one",
              resolved["srv"].command == "dynamic-cmd")
    ctx.check("a dynamic server with no collision is still added", resolved["claude-in-chrome"].command == "claude")


@test
def test_resolve_extra_dynamic_dropped_under_strict_mcp_config_with_notice(ctx: Ctx):
    """finding 14: still excluded under --strict-mcp-config (unchanged,
    correct exclusivity) but no longer SILENTLY."""
    cwd = Path("/tmp/proj4")
    dynamic = {"claude-in-chrome": M.McpServerConfig(name="claude-in-chrome", type="stdio", command="claude", scope="dynamic")}
    resolved, notices = M.resolve_server_configs(cwd=cwd, claude_json={}, extra_dynamic=dynamic, strict_mcp_config=True)
    ctx.check("still dropped under --strict-mcp-config", "claude-in-chrome" not in resolved)
    ctx.check(f"but a notice explains why, got {notices}",
              any("claude-in-chrome" in n and "strict" in n.lower() for n in notices))


@test
def test_resolve_extra_dynamic_dropped_under_managed_mcp_json_with_notice(ctx: Ctx):
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        managed_path = Path(td) / "managed-mcp.json"
        managed_path.write_text('{"mcpServers": {"managed-only": {"command": "m"}}}', encoding="utf-8")
        dynamic = {"playwright": M.McpServerConfig(name="playwright", type="stdio", command="npx", scope="dynamic")}
        resolved, notices = M.resolve_server_configs(cwd=cwd, claude_json={}, extra_dynamic=dynamic,
                                                        managed_mcp_path=managed_path)
        ctx.check("still dropped under managed-mcp.json exclusivity", "playwright" not in resolved)
        ctx.check(f"but a notice explains why, got {notices}",
                  any("playwright" in n and "managed-mcp.json" in n for n in notices))


# ---- managed allowedMcpServers/deniedMcpServers [finding 10] --------------
# 2.1.281's real schema (read from the binary) is a list of OBJECTS, each
# with exactly one of serverName/serverCommand/serverUrl -- NEVER bare
# strings (the old fixture shape here raised `TypeError: unhashable type:
# 'dict'` the instant a real managed-settings file used the real shape).

@test
def test_managed_denied_mcp_servers_removes_a_server(ctx: Ctx):
    from halo_harness.config.settings import Settings
    cwd = Path("/tmp/proj5")
    claude_json = _claude_json(user={"github": {"command": "gh"}, "other": {"command": "o"}})
    settings = Settings(raw={"deniedMcpServers": [{"serverName": "github"}]}, layers=[], errors=[])
    resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json=claude_json, settings=settings)
    ctx.check("denied server removed", "github" not in resolved)
    ctx.check("other server survives", "other" in resolved)


@test
def test_managed_allowed_mcp_servers_narrows_to_the_list(ctx: Ctx):
    from halo_harness.config.settings import Settings
    cwd = Path("/tmp/proj6")
    claude_json = _claude_json(user={"github": {"command": "gh"}, "other": {"command": "o"}})
    settings = Settings(raw={"allowedMcpServers": [{"serverName": "github"}]}, layers=[], errors=[])
    resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json=claude_json, settings=settings)
    ctx.check("only the allow-listed server survives", set(resolved) == {"github"})


@test
def test_allowed_mcp_servers_empty_list_is_lockdown(ctx: Ctx):
    """finding 10: `allowedMcpServers: []` -- PRESENT but with zero
    entries -- means "users can use no servers of their own", distinct
    from the key being entirely absent (no restriction at all, covered by
    every other resolve_server_configs test that never sets it)."""
    from halo_harness.config.settings import Settings
    cwd = Path("/tmp/proj6b")
    claude_json = _claude_json(user={"github": {"command": "gh"}})
    settings = Settings(raw={"allowedMcpServers": []}, layers=[], errors=[])
    resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json=claude_json, settings=settings)
    ctx.check(f"empty allow list locks everything out, got {list(resolved)}", resolved == {})


@test
def test_denied_mcp_servers_server_command_matches_the_exact_invocation(ctx: Ctx):
    from halo_harness.config.settings import Settings
    cwd = Path("/tmp/proj6c")
    claude_json = _claude_json(user={"a": {"command": "npx", "args": ["-y", "bad-server"]},
                                      "b": {"command": "npx", "args": ["-y", "good-server"]}})
    settings = Settings(raw={"deniedMcpServers": [{"serverCommand": ["npx", "-y", "bad-server"]}]},
                         layers=[], errors=[])
    resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json=claude_json, settings=settings)
    ctx.check(f"only the exact command+args match is denied, got {list(resolved)}",
              "a" not in resolved and "b" in resolved)


@test
def test_denied_mcp_servers_server_url_wildcard_matches(ctx: Ctx):
    from halo_harness.config.settings import Settings
    cwd = Path("/tmp/proj6d")
    claude_json = _claude_json(user={
        "remote": {"type": "http", "url": "https://evil.example.com/mcp"},
        "other": {"type": "http", "url": "https://good.example.com/mcp"},
    })
    settings = Settings(raw={"deniedMcpServers": [{"serverUrl": "https://evil.example.com/*"}]},
                         layers=[], errors=[])
    resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json=claude_json, settings=settings)
    ctx.check(f"wildcard url match denied, exact other url survives, got {list(resolved)}",
              "remote" not in resolved and "other" in resolved)


@test
def test_malformed_policy_entries_are_skipped_with_a_notice(ctx: Ctx):
    """A bare string (the OLD, wrong shape), an entry with zero or more
    than one of the three keys, or a non-dict entry must never crash
    resolve_server_configs (finding 10's exact repro: `TypeError:
    unhashable type: 'dict'`) and must never match anything -- skipped,
    with a notice explaining why, and every server survives untouched."""
    from halo_harness.config.settings import Settings
    cwd = Path("/tmp/proj6e")
    claude_json = _claude_json(user={"github": {"command": "gh"}})
    settings = Settings(raw={"deniedMcpServers": [
        "github",                                       # bare string -- the old, wrong shape
        {},                                              # zero keys
        {"serverName": "github", "serverUrl": "x"},       # more than one key
        {"serverName": 123},                              # wrong value type
    ]}, layers=[], errors=[])
    resolved, notices = M.resolve_server_configs(cwd=cwd, claude_json=claude_json, settings=settings)
    ctx.check(f"nothing crashed and the server survives, got {list(resolved)}", "github" in resolved)
    ctx.check(f"a notice explains the malformed entries, got {notices}",
              any("malformed entry" in n for n in notices))


# ---- disabledMcpjsonServers/enabledMcpjsonServers read from settings too --

@test
def test_disabled_mcpjson_servers_read_from_settings_never_removes_other_scopes(ctx: Ctx):
    """finding 11: (a) disabledMcpjsonServers is read from the resolved
    settings too, not just ~/.claude.json; (b) it removes ONLY the actual
    .mcp.json-sourced entry, never a same-named user/local/flag server."""
    from halo_harness.config.settings import Settings
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        (cwd / ".mcp.json").write_text('{"mcpServers": {"github": {"command": "project-gh"}}}', encoding="utf-8")
        claude_json = _claude_json(user={"github": {"command": "user-gh"}})
        settings = Settings(raw={"disabledMcpjsonServers": ["github"]}, layers=[], errors=[])
        resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json=claude_json, settings=settings, print_mode=True)
        ctx.check(f"the .mcp.json entry is dropped, the user-scope one survives, got {resolved.get('github')}",
                  resolved.get("github") is not None and resolved["github"].command == "user-gh")


@test
def test_enabled_mcpjson_servers_read_from_settings(ctx: Ctx):
    from halo_harness.config.settings import Settings
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        (cwd / ".mcp.json").write_text('{"mcpServers": {"a": {"command": "a"}}}', encoding="utf-8")
        settings = Settings(raw={"enabledMcpjsonServers": ["a"]}, layers=[], errors=[])
        resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json={}, settings=settings, print_mode=False)
        ctx.check("approved via the settings-sourced enabledMcpjsonServers", resolved["a"].pending_approval is False)


# ---- McpLoop ------------------------------------------------------------

@test
def test_mcp_loop_runs_a_coroutine_and_returns_its_result(ctx: Ctx):
    from halo_harness.mcp.client import McpLoop
    loop = McpLoop()
    try:
        async def _add(a, b):
            return a + b
        ctx.check("coroutine result comes back", loop.run(_add(2, 3)) == 5)
    finally:
        loop.close()


@test
def test_mcp_loop_timeout_raises(ctx: Ctx):
    import asyncio
    import concurrent.futures
    from halo_harness.mcp.client import McpLoop
    loop = McpLoop()
    try:
        async def _slow():
            await asyncio.sleep(5)
        raised = False
        try:
            loop.run(_slow(), timeout=0.05)
        except concurrent.futures.TimeoutError:
            raised = True
        ctx.check("a too-short timeout raises TimeoutError", raised)
    finally:
        loop.close()


@test
def test_mcp_loop_close_is_idempotent(ctx: Ctx):
    from halo_harness.mcp.client import McpLoop
    loop = McpLoop()
    loop.run(_noop_coro())
    loop.close()
    loop.close()  # must not raise
    ctx.check("closing twice never raises", True)


async def _noop_coro():
    return None


# ---- OpenCode-H9 MCP compatibility: progress resets the call timeout ------

@test
def test_progress_keepalive_extends_run_abortable_past_its_own_timeout(ctx: Ctx):
    """A coroutine that takes LONGER than `timeout` must still succeed as
    long as it "pings" the keepalive before each window elapses -- proof
    that `run_abortable`'s OWN outer deadline is pushed back out by a
    progress signal, not just whatever the SDK's internal read timeout
    does on its own."""
    import asyncio
    from halo_harness.mcp.client import McpLoop, ProgressKeepalive
    loop = McpLoop()
    keepalive = ProgressKeepalive()

    async def _slow_with_progress():
        for _ in range(3):
            await asyncio.sleep(0.15)
            await keepalive(1, 3, "still going")
        return "done"

    try:
        # 0.15s * 3 == 0.45s total, each leg pinging BEFORE a 0.25s
        # timeout would otherwise have expired -- without the keepalive
        # extending the deadline this would raise TimeoutError well
        # before the coroutine finishes.
        result = loop.run_abortable(_slow_with_progress(), timeout=0.25, keepalive=keepalive)
        ctx.check(f"survives past one timeout window via progress pings, got {result!r}", result == "done")
    finally:
        loop.close()


@test
def test_run_abortable_without_keepalive_still_times_out_normally(ctx: Ctx):
    """No regression: passing `keepalive=None` (every existing caller)
    behaves exactly like before -- a slow coroutine with no progress
    signal still times out on schedule."""
    import asyncio
    import concurrent.futures
    from halo_harness.mcp.client import McpLoop
    loop = McpLoop()
    try:
        async def _slow():
            await asyncio.sleep(2)
        raised = False
        try:
            loop.run_abortable(_slow(), timeout=0.1)
        except concurrent.futures.TimeoutError:
            raised = True
        ctx.check("still times out with no keepalive", raised)
    finally:
        loop.close()


@test
def test_wait_future_abortable_honours_abort(ctx: Ctx):
    """The generic helper `McpServerHandle.start()`/`close()` now use for
    finding 9's "reconnect honours Esc" -- an abort Event set mid-wait
    raises McpAborted promptly instead of waiting out the full timeout."""
    import concurrent.futures
    import threading
    import time as _time
    from halo_harness.mcp.client import McpAborted, McpLoop
    loop = McpLoop()
    try:
        fut = concurrent.futures.Future()  # never resolved -- simulates a hung connect/close
        abort = threading.Event()
        threading.Timer(0.1, abort.set).start()
        start = _time.monotonic()
        raised = None
        try:
            loop.wait_future_abortable(fut, timeout=5.0, abort=abort)
        except McpAborted as e:
            raised = e
        elapsed = _time.monotonic() - start
        ctx.check(f"raised McpAborted, got {raised!r}", raised is not None)
        ctx.check(f"returned promptly (~0.1s), not the full 5s timeout, got {elapsed:.2f}s", elapsed < 2.0)
    finally:
        loop.close()


# ---- OpenCode-H9 MCP compatibility: Streamable HTTP -> SSE fallback -------

@test
def test_http_transport_falls_back_to_sse_on_connect_failure(ctx: Ctx):
    """A server configured `type: "http"` that only actually speaks the
    deprecated `sse` transport (Streamable HTTP handshake fails outright)
    must still connect via the sse fallback instead of failing the whole
    handle."""
    import asyncio
    from halo_harness.mcp import http_sse

    calls = {"http": 0, "sse": 0}

    async def _fake_connect_http(*, url, headers, connect_timeout):
        calls["http"] += 1
        raise RuntimeError("405 Method Not Allowed (streamable-http not supported)")

    async def _fake_connect_sse(*, url, headers, connect_timeout):
        calls["sse"] += 1
        return "FAKE_STACK", "FAKE_SESSION"

    orig_http, orig_sse = http_sse.connect_http, http_sse.connect_sse
    http_sse.connect_http = _fake_connect_http
    http_sse.connect_sse = _fake_connect_sse
    try:
        cfg = M.McpServerConfig(name="fallback-test", type="http", url="https://example.com/mcp")
        handle = M.McpServerHandle(cfg, loop=None, tool_env={}, cwd=Path("."))
        stack, session = asyncio.run(handle._open_transport(connect_timeout=1.0))
        ctx.check("streamable-http was tried first", calls["http"] == 1)
        ctx.check("sse fallback was used", calls["sse"] == 1)
        ctx.check(f"the sse connection's own result is returned, got {(stack, session)}",
                  (stack, session) == ("FAKE_STACK", "FAKE_SESSION"))
    finally:
        http_sse.connect_http, http_sse.connect_sse = orig_http, orig_sse


@test
def test_http_transport_raises_the_original_error_when_sse_also_fails(ctx: Ctx):
    """When NEITHER transport works, the ORIGINAL (streamable-http)
    failure is what surfaces -- it's the more informative one for a
    server that's simply unreachable/misconfigured, not a transport
    mismatch."""
    import asyncio
    from halo_harness.mcp import http_sse

    async def _fake_connect_http(*, url, headers, connect_timeout):
        raise RuntimeError("original streamable-http failure")

    async def _fake_connect_sse(*, url, headers, connect_timeout):
        raise RuntimeError("sse also failed")

    orig_http, orig_sse = http_sse.connect_http, http_sse.connect_sse
    http_sse.connect_http, http_sse.connect_sse = _fake_connect_http, _fake_connect_sse
    try:
        cfg = M.McpServerConfig(name="fallback-test-2", type="http", url="https://example.com/mcp")
        handle = M.McpServerHandle(cfg, loop=None, tool_env={}, cwd=Path("."))
        raised = None
        try:
            asyncio.run(handle._open_transport(connect_timeout=1.0))
        except RuntimeError as e:
            raised = e
        ctx.check(f"the ORIGINAL http error surfaces, got {raised!r}",
                  raised is not None and "original streamable-http failure" in str(raised))
    finally:
        http_sse.connect_http, http_sse.connect_sse = orig_http, orig_sse


# ---- Halo 2.0.2 round C: connection-refused/DNS-failure fail fast ---------

@test
def test_preflight_tcp_reachability_raises_fast_on_connection_refused(ctx: Ctx):
    """Halo 2.0.2 round C: "connection refused ... fail fast ... instead
    of waiting out MCP_TIMEOUT" -- a FAKE `open_connection` that raises
    ConnectionRefusedError immediately (standing in for the real OS-level
    refusal, verified separately to behave this way for real) must come
    straight back as `ConnectionRefusedError`, never swallowed/retried."""
    import asyncio
    from halo_harness.mcp import http_sse

    async def _refused(host, port):
        raise ConnectionRefusedError(f"[Errno 111] Connection refused: {host}:{port}")

    raised = None
    try:
        asyncio.run(http_sse.preflight_tcp_reachability(
            "http://127.0.0.1:9/mcp", timeout=5.0, open_connection=_refused))
    except ConnectionRefusedError as e:
        raised = e
    ctx.check(f"raises ConnectionRefusedError naming host:port, got {raised!r}",
              raised is not None and "127.0.0.1:9" in str(raised))


@test
def test_preflight_tcp_reachability_raises_fast_on_dns_failure(ctx: Ctx):
    """Halo 2.0.2 round C: "DNS failure fail fast" -- `socket.gaierror`
    IS an `OSError` (same branch as connection-refused), covered by the
    same fast-fail path."""
    import asyncio
    import socket
    from halo_harness.mcp import http_sse

    async def _no_such_host(host, port):
        raise socket.gaierror("getaddrinfo failed")

    raised = None
    try:
        asyncio.run(http_sse.preflight_tcp_reachability(
            "http://does-not-resolve.invalid/mcp", timeout=5.0, open_connection=_no_such_host))
    except ConnectionRefusedError as e:
        raised = e
    ctx.check(f"a DNS failure ALSO raises ConnectionRefusedError (never the raw gaierror), got {raised!r}",
              raised is not None and "does-not-resolve.invalid" in str(raised))


@test
def test_preflight_tcp_reachability_is_inconclusive_for_a_merely_slow_server(ctx: Ctx):
    """A server that's simply SLOW to accept (never confirmed refused)
    must not be treated as dead -- this probe's own timeout elapsing
    returns normally (never raises), leaving the real, longer connect
    attempt to make the actual call."""
    import asyncio
    from halo_harness.mcp import http_sse

    async def _never_returns(host, port):
        await asyncio.sleep(60)
        raise AssertionError("should have been cancelled by the probe's own short timeout")

    import time
    t0 = time.monotonic()
    asyncio.run(http_sse.preflight_tcp_reachability(
        "http://slow.example/mcp", timeout=0.2, open_connection=_never_returns))
    ctx.check(f"returned promptly (bounded by its OWN short timeout), got {time.monotonic()-t0:.2f}s",
              time.monotonic() - t0 < 2.0)


@test
def test_preflight_tcp_reachability_succeeds_silently_on_a_real_connect(ctx: Ctx):
    """The ordinary, overwhelmingly common case -- a reachable server --
    must never raise or otherwise change anything downstream."""
    import asyncio
    from halo_harness.mcp import http_sse

    class _FakeWriter:
        def close(self):
            pass

    async def _ok(host, port):
        return object(), _FakeWriter()

    asyncio.run(http_sse.preflight_tcp_reachability(
        "http://example.com/mcp", timeout=5.0, open_connection=_ok))  # must not raise
    ctx.check("reaches here -- no exception for a successful connect", True)


@test
def test_connect_http_and_connect_sse_both_run_the_preflight_check_first(ctx: Ctx):
    """Halo 2.0.2 round C: both transports must run the SAME preflight
    check before any of the real (SDK-internal) connect work -- proven
    by a fake `preflight_tcp_reachability` that raises unconditionally;
    if either transport skipped it, it would instead fail later/
    differently (or hang, per the real bug this closes) rather than with
    this EXACT error."""
    import asyncio
    from halo_harness.mcp import http_sse

    async def _boom(url, *, timeout, open_connection=None):
        raise ConnectionRefusedError("preflight fired")

    orig = http_sse.preflight_tcp_reachability
    http_sse.preflight_tcp_reachability = _boom
    try:
        for coro_fn in (http_sse.connect_http, http_sse.connect_sse):
            raised = None
            try:
                asyncio.run(coro_fn(url="http://127.0.0.1:9/mcp", headers={}, connect_timeout=1.0))
            except ConnectionRefusedError as e:
                raised = e
            ctx.check(f"{coro_fn.__name__} ran the preflight check first, got {raised!r}",
                      raised is not None and "preflight fired" in str(raised))
    finally:
        http_sse.preflight_tcp_reachability = orig


# ---- Linux/H4 must-do: mcpLazy servers connect for discoverability --------

@test
def test_ensure_lazy_started_all_starts_only_pending_lazy_servers(ctx: Ctx):
    cfg_lazy = M.McpServerConfig(name="lazy1", type="stdio", command=sys.executable,
                                  args=["-m", "tests.helpers.fake_mcp_server"], cwd=str(REPO_DIR), lazy=True)
    cfg_eager = M.McpServerConfig(name="eager1", type="stdio", command=sys.executable,
                                   args=["-m", "tests.helpers.fake_mcp_server"], cwd=str(REPO_DIR))
    mgr = M.McpManager({"lazy1": cfg_lazy, "eager1": cfg_eager}, tool_env=dict(os.environ),
                        cwd=REPO_DIR, lazy_names={"lazy1"})
    try:
        mgr.start_all()
        ctx.check("the eager server started at start_all()", mgr.handles["eager1"].state == "connected")
        ctx.check("the lazy server did NOT start at start_all()", mgr.handles["lazy1"].state == "pending")
        ctx.check("its tools are therefore invisible to all_tools() so far",
                  not any(s == "lazy1" for s, _, _ in mgr.all_tools()))
        started = mgr.ensure_lazy_started_all()
        ctx.check(f"ensure_lazy_started_all reports it started lazy1, got {started}", started == ["lazy1"])
        ctx.check(f"the lazy server is now connected, got {mgr.handles['lazy1'].state}",
                  mgr.handles["lazy1"].state == "connected")
        ctx.check("its tools are now visible to all_tools()",
                  any(s == "lazy1" for s, _, _ in mgr.all_tools()))
        again = mgr.ensure_lazy_started_all()
        ctx.check(f"a second call is a cheap no-op, got {again}", again == [])
    finally:
        mgr.close_all()


@test
def test_h9_ensure_lazy_started_all_starts_targets_in_parallel(ctx: Ctx):
    """H9 must-do: the lazy-start path used to be a plain SERIAL `for name
    in self._lazy_names: h.start()` loop -- 3 lazy servers would take
    roughly 3x one server's connect time. It now shares `start_all()`'s
    concurrent-gather machinery, so 3 slow-starting lazy servers should
    finish in roughly ONE slow-start's worth of wall clock, not three."""
    cfgs = {name: M.McpServerConfig(name=name, type="stdio", command=sys.executable,
                                     args=["-m", "tests.helpers.fake_mcp_server"],
                                     cwd=str(REPO_DIR), lazy=True)
            for name in ("lazyA", "lazyB", "lazyC")}
    mgr = M.McpManager(cfgs, tool_env={**os.environ, "FAKE_MCP_MODE": "slow", "FAKE_MCP_SLEEP_S": "1.5"},
                        cwd=REPO_DIR, lazy_names=set(cfgs))
    try:
        mgr.start_all()  # no-op: all 3 are lazy, none eager
        t0 = time.monotonic()
        started = mgr.ensure_lazy_started_all()
        elapsed = time.monotonic() - t0
        ctx.check(f"all 3 lazy servers started, got {sorted(started)}", sorted(started) == ["lazyA", "lazyB", "lazyC"])
        states = [mgr.handles[n].state for n in cfgs]
        ctx.check(f"all 3 connect, got {states}", states == ["connected"] * 3)
        ctx.check(f"parallel (~1.5s), not serial (~4.5s) -- took {elapsed:.2f}s", elapsed < 3.0)
    finally:
        mgr.close_all()


@test
def test_h9_ensure_lazy_started_all_honours_abort(ctx: Ctx):
    """H9 must-do: `ensure_lazy_started_all` now accepts `abort` (threaded
    from `ToolSearchTool` via `SessionCatalog.ensure_lazy_discovered`) and
    waits the same ABORTABLE way `McpServerHandle.start()` already does --
    an Esc/Ctrl+C fired mid-wait must return promptly instead of blocking
    for the lazy servers' full (here, deliberately long) startup sleep."""
    import threading
    cfgs = {name: M.McpServerConfig(name=name, type="stdio", command=sys.executable,
                                     args=["-m", "tests.helpers.fake_mcp_server"],
                                     cwd=str(REPO_DIR), lazy=True)
            for name in ("slowlazyA", "slowlazyB")}
    mgr = M.McpManager(cfgs, tool_env={**os.environ, "FAKE_MCP_MODE": "slow", "FAKE_MCP_SLEEP_S": "6"},
                        cwd=REPO_DIR, lazy_names=set(cfgs))
    abort = threading.Event()
    threading.Timer(0.3, abort.set).start()
    try:
        t0 = time.monotonic()
        mgr.ensure_lazy_started_all(abort=abort)
        elapsed = time.monotonic() - t0
        ctx.check(f"returns promptly (~0.3s), not the full 6s slow-start sleep, got {elapsed:.2f}s", elapsed < 3.0)
    finally:
        mgr.close_all()


# ---- finding 9: Controller.reconnect_mcp threads abort through --------------

@test
def test_controller_reconnect_mcp_threads_abort_through_to_a_slow_reconnect(ctx: Ctx):
    """`Controller.reconnect_mcp(name, abort=...)` (the TUI's `/mcp`
    dialog worker thread's own call, tui/dialogs/mcp_status.py) must
    thread the abort Event all the way down to `McpManager.reconnect`'s
    own close/start waits -- against a server whose STARTUP itself is
    slow (5s), an abort fired 0.3s in must return well before the 5s
    sleep elapses, proving the whole chain (Controller -> the
    tui/bootstrap.py-style reconnect closure -> McpManager.reconnect ->
    McpServerHandle.start -> wait_future_abortable) is actually wired,
    not just the bottom primitive tested in isolation elsewhere."""
    import threading
    import time as _time
    from halo_harness.controller import Controller

    env = {"FAKE_MCP_MODE": "slow", "FAKE_MCP_SLEEP_S": "5"}
    cfg = M.McpServerConfig(name="slowreconnect", type="stdio", command=sys.executable,
                             args=["-m", "tests.helpers.fake_mcp_server"], env=env, cwd=str(REPO_DIR))
    mgr = M.McpManager({"slowreconnect": cfg}, tool_env=dict(os.environ), cwd=REPO_DIR)
    try:
        def _reconnect_fn(name, abort=None):
            ok = mgr.reconnect(name, abort=abort)
            return [f"{name}: {'connected' if ok else 'failed'}"]

        controller = Controller(session=None, cwd=REPO_DIR, reconnect_fn=_reconnect_fn)
        abort = threading.Event()
        threading.Timer(0.3, abort.set).start()
        start = _time.monotonic()
        result = controller.reconnect_mcp("slowreconnect", abort=abort)
        elapsed = _time.monotonic() - start
        ctx.check(f"reconnect_mcp returns SOMETHING (never raises), got {result}", isinstance(result, list))
        ctx.check(f"returns promptly (~0.3s), not the full 5s slow-start sleep, got {elapsed:.2f}s", elapsed < 3.0)
    finally:
        # abort only cuts reconnect_mcp's WAIT short -- the underlying
        # connect this test deliberately made slow (FAKE_MCP_SLEEP_S=5)
        # keeps running regardless ("abandoned, not stopped", same as
        # everywhere else abort-aware waiting is used in this codebase),
        # so close_all()'s own default 5.0s budget is not enough slack for
        # it to finish naturally and close cleanly -- verified on Windows:
        # with the default budget this reliably lost the race and left a
        # live child process + an unclosed transport for the interpreter
        # to crash on at shutdown. A generous explicit timeout here lets
        # close_all() wait out the real 5s sleep (plus process-start/
        # handshake overhead) and hit the fast, clean shutdown path
        # instead of leaning on McpLoop.stop()'s forced-cancel fallback.
        mgr.close_all(timeout=15.0)


# ---- live connections through the REAL fake stdio server -------------------

def _fake_cfg(name="fake", *, mode=None, tool_count=None, cwd=None):
    from halo_harness.mcp.manager import McpServerConfig
    env = {}
    if mode:
        env["FAKE_MCP_MODE"] = mode
    if tool_count:
        env["FAKE_MCP_TOOL_COUNT"] = str(tool_count)
    return McpServerConfig(name=name, type="stdio", command=sys.executable,
                            args=["-m", "tests.helpers.fake_mcp_server"], env=env,
                            cwd=str(cwd) if cwd else str(REPO_DIR))


@test
def test_handle_connects_lists_tools_and_closes(ctx: Ctx):
    from halo_harness.mcp.client import McpLoop
    from halo_harness.mcp.manager import McpServerHandle
    loop = McpLoop()
    h = McpServerHandle(_fake_cfg(), loop, tool_env=dict(os.environ), cwd=REPO_DIR)
    try:
        ctx.check("initial state is pending", h.state == "pending")
        h.start()
        ctx.check(f"connects, got state={h.state} error={h.error}", h.state == "connected")
        names = sorted(t.name for t in h.tools)
        ctx.check(f"discovers the 7 core fake tools, got {names}",
                  {"echo", "image", "error_tool", "always_load_tool", "read_only_tool"} <= set(names))
        ctx.check("instructions carried from initialize()", "fake MCP server" in (h.instructions or ""))
    finally:
        h.close()
        loop.close()
    ctx.check("state is closed after close()", h.state == "closed")


@test
def test_manager_start_all_is_bounded_by_mcp_timeout(ctx: Ctx):
    from halo_harness.mcp.manager import McpManager
    old = os.environ.pop("MCP_TIMEOUT", None)
    try:
        os.environ["MCP_TIMEOUT"] = "150"
        mgr = McpManager({"slow": _fake_cfg("slow", mode="slow")}, tool_env={**os.environ, "FAKE_MCP_SLEEP_S": "3"})
        t0 = time.monotonic()
        mgr.start_all()
        elapsed = time.monotonic() - t0
        try:
            ctx.check(f"failed within ~MCP_TIMEOUT (took {elapsed:.2f}s), never waited the full 3s sleep",
                      elapsed < 2.0)
            ctx.check(f"state is failed, got {mgr.status()[0]}", mgr.status()[0]["state"] == "failed")
        finally:
            mgr.close_all()
    finally:
        if old is None:
            os.environ.pop("MCP_TIMEOUT", None)
        else:
            os.environ["MCP_TIMEOUT"] = old


@test
def test_manager_crash_mode_fails_cleanly(ctx: Ctx):
    from halo_harness.mcp.manager import McpManager
    mgr = McpManager({"crash": _fake_cfg("crash", mode="crash")}, tool_env=dict(os.environ))
    try:
        mgr.start_all()
        status = mgr.status()[0]
        ctx.check(f"crash mode -> failed, never a raised exception, got {status}", status["state"] == "failed")
        ctx.check("an error message is recorded", bool(status["error"]))
    finally:
        mgr.close_all()


@test
def test_manager_starts_multiple_servers_in_parallel(ctx: Ctx):
    from halo_harness.mcp.manager import McpManager
    mgr = McpManager({"a": _fake_cfg("a"), "b": _fake_cfg("b"), "c": _fake_cfg("c")}, tool_env=dict(os.environ))
    try:
        t0 = time.monotonic()
        mgr.start_all()
        elapsed = time.monotonic() - t0
        states = [s["state"] for s in mgr.status()]
        ctx.check(f"all 3 connect, got {states}", states == ["connected", "connected", "connected"])
        ctx.check(f"parallel, not serial (took {elapsed:.2f}s for 3 servers)", elapsed < 5.0)
    finally:
        mgr.close_all()


@test
def test_manager_call_dispatches_a_real_tool(ctx: Ctx):
    from tests.helpers.fake_mcp_server import running_manager
    with running_manager({"fake": _fake_cfg()}, tool_env=dict(os.environ)) as mgr:
        result = mgr.call("fake", "echo", {"text": "round trip"})
        ctx.check("real call_tool round trip", result.content[0].text == "round trip")


@test
def test_manager_all_tools_name_sorted_and_sanitised(ctx: Ctx):
    from halo_harness.mcp.manager import McpManager
    mgr = McpManager({"fake": _fake_cfg()}, tool_env=dict(os.environ))
    try:
        mgr.start_all()
        triples = mgr.all_tools()
        names = [t[1] for t in triples]
        ctx.check("every name is mcp__fake__...", all(n.startswith("mcp__fake__") for n in names))
        ctx.check("name-sorted", names == sorted(names))
    finally:
        mgr.close_all()


@test
def test_manager_reconnect(ctx: Ctx):
    from halo_harness.mcp.manager import McpManager
    mgr = McpManager({"fake": _fake_cfg()}, tool_env=dict(os.environ))
    try:
        mgr.start_all()
        ctx.check("connected first", mgr.status()[0]["state"] == "connected")
        ok = mgr.reconnect("fake")
        ctx.check("reconnect() re-establishes the connection", ok is True)
        ctx.check("still connected after reconnect", mgr.status()[0]["state"] == "connected")
    finally:
        mgr.close_all()


@test
def test_manager_reconnect_skips_a_disabled_handle(ctx: Ctx):
    """2.0.2 review finding 18 (major) pin: `reconnect()` used to
    unconditionally set `h.state` to "pending"/"pending_approval"
    BEFORE calling `start()`, which skipped `start()`'s own "disabled"
    guard (by then `h.state` was no longer "disabled" at all) -- `R`
    (reconnect all) silently brought back up any server the user just
    disabled with `d`, and a single `r` on a disabled row did the
    same. `h.state = "disabled"` here mirrors exactly what `Controller.
    set_mcp_server_disabled(name, True)` does to a live handle."""
    from halo_harness.mcp.manager import McpManager
    mgr = McpManager({"fake": _fake_cfg()}, tool_env=dict(os.environ))
    try:
        mgr.start_all()
        ctx.check("connected first", mgr.status()[0]["state"] == "connected")
        h = mgr.handles["fake"]
        h.close(timeout=5.0)
        h.state = "disabled"
        h.error = "disabled by the user (`/mcp` d)"
        ok = mgr.reconnect("fake")
        ctx.check(f"reconnect() refuses a disabled handle, got {ok!r}", ok is False)
        ctx.check(f"the handle STAYS disabled, got {h.state!r}", h.state == "disabled")
    finally:
        mgr.close_all()


@test
def test_manager_resources_and_prompts(ctx: Ctx):
    from halo_harness.mcp.manager import McpManager
    mgr = McpManager({"fake": _fake_cfg()}, tool_env=dict(os.environ))
    try:
        mgr.start_all()
        resources = mgr.resources()
        ctx.check(f"lists the fake resource, got {resources}", any(r.uri == "fake://note" for _, r in resources))
        read = mgr.read_resource("fake", "fake://note")
        ctx.check("reads the resource's text", "fake MCP resource" in read.contents[0].text)
        prompts = mgr.prompts()
        ctx.check(f"lists the fake prompt, got {prompts}", any(p.name == "greet" for _, p in prompts))
        got_prompt = mgr.get_prompt("fake", "greet", {"name": "rolo"})
        ctx.check("get_prompt renders the argument", "rolo" in got_prompt.messages[0].content.text)
    finally:
        mgr.close_all()


@test
def test_manager_scales_to_300_tools(ctx: Ctx):
    from halo_harness.mcp.manager import McpManager
    mgr = McpManager({"big": _fake_cfg("big", tool_count=300)}, tool_env=dict(os.environ))
    try:
        mgr.start_all()
        ctx.check(f"connects, state={mgr.status()[0]}", mgr.status()[0]["state"] == "connected")
        ctx.check("discovers all 300 tools", len(mgr.all_tools()) == 300)
    finally:
        mgr.close_all()


# ---- finding 16 test 3: fake-server badbytes / die-mid-call modes ---------

@test
def test_badbytes_call_returns_within_limit_encoding_replace(ctx: Ctx):
    """finding 3: a non-UTF-8 byte on the server's stdout must never kill
    the reader permanently -- verified end to end (real subprocess, real
    bad bytes) rather than just unit-testing StdioServerParameters."""
    from tests.helpers.fake_mcp_server import running_manager
    cfg = _fake_cfg("fake")
    cfg.env["FAKE_MCP_EXTRA_TOOLS"] = "badbytes_tool"
    t0 = time.monotonic()
    with running_manager({"fake": cfg}, tool_env=dict(os.environ)) as mgr:
        result = mgr.call("fake", "badbytes_tool", {})
        elapsed = time.monotonic() - t0
        ctx.check(f"answered promptly despite the bad bytes, took {elapsed:.2f}s", elapsed < 10.0)
        ctx.check("the real answer came through", "answered after bad bytes" in result.content[0].text)


@test
def test_die_mid_call_marks_the_handle_failed_and_drops_session(ctx: Ctx):
    """finding 6: the server process dying mid-call must be detected --
    the call itself returns promptly (an error, not a hang), and the
    handle's status is no longer 'connected' afterward."""
    from halo_harness.mcp.manager import McpManager
    cfg = _fake_cfg("fake")
    cfg.env["FAKE_MCP_EXTRA_TOOLS"] = "die_mid_call"
    mgr = McpManager({"fake": cfg}, tool_env=dict(os.environ))
    try:
        mgr.start_all()
        t0 = time.monotonic()
        raised = False
        try:
            mgr.call("fake", "die_mid_call", {})
        except Exception:
            raised = True
        elapsed = time.monotonic() - t0
        ctx.check(f"the call fails promptly rather than hanging, took {elapsed:.2f}s", raised and elapsed < 10.0)
        # the (failed) call above already triggered ONE auto-reconnect
        # attempt (finding 6: "reconnect once on the next call") against a
        # dead process, which itself fails fast -- status must reflect
        # that, never a stale "connected".
        ctx.check(f"status is no longer connected, got {mgr.status()[0]}", mgr.status()[0]["state"] != "connected")
    finally:
        mgr.close_all()


# ---- finding 16 test 4: abort during an in-flight MCP call ----------------

@test
def test_abort_during_call_tool_returns_in_under_1s(ctx: Ctx):
    """finding 4: an in-flight MCP call must poll `abort` in <=0.2s
    slices, not block for the tool's own (here, 6s) sleep."""
    import threading
    from halo_harness.mcp.client import McpAborted
    with running_manager_ctx() as mgr:
        h = mgr.handles["fake"]
        abort = threading.Event()

        def _fire_abort():
            time.sleep(1.0)
            abort.set()
        threading.Thread(target=_fire_abort, daemon=True).start()

        t0 = time.monotonic()
        raised = False
        try:
            h.call_tool("slow_tool", {"seconds": 6.0}, timeout=30.0, abort=abort)
        except McpAborted:
            raised = True
        elapsed = time.monotonic() - t0
        ctx.check(f"aborted well under the 6s sleep, took {elapsed:.2f}s", raised and elapsed < 2.0)


def running_manager_ctx():
    from tests.helpers.fake_mcp_server import running_manager
    return running_manager({"fake": _fake_cfg("fake")}, tool_env=dict(os.environ))


# ---- finding 16 test 5: startup timeout + close_all leaves nothing behind -

def _win_pid_alive(pid: int) -> bool:
    """H4 fix: `os.kill(pid, 0)` on Windows is NOT a reliable liveness
    check -- CPython implements it via `OpenProcess`, which can succeed
    (raising nothing) for a process that has ALREADY exited but whose
    process object Windows hasn't fully torn down yet (a process object
    stays open-able until every handle referencing it closes, unlike
    POSIX's pid-table-based kill(pid,0) semantics) -- verified: `psutil`
    independently confirms the process is gone (`NoSuchProcess`) at the
    exact moment `os.kill(pid, 0)` still reports it alive, immediately
    after `McpManager.close_all()` returns. The correct Windows check
    (what `psutil` itself does, without adding a project dependency for
    one test helper) is `OpenProcess` + `GetExitCodeProcess`, comparing
    against `STILL_ACTIVE` (259) -- an exited process reports its real
    exit code, never 259, even while its object is still open-able."""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False  # no such process (or, vanishingly unlikely for our own child, access denied)
    try:
        exit_code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@test
def test_startup_timeout_then_close_all_leaves_no_child_and_no_future_exception(ctx: Ctx):
    """finding 16 test 5 (finding 15's own verification bar): after a
    connect that never finishes within MCP_TIMEOUT, followed by
    close_all(), (a) the child process is gone, and (b) the handle's
    `_lifecycle_future` holds NO exception (an unretrieved future
    exception is exactly what `PYTHONWARNINGS=error::RuntimeWarning`
    would have caught as a cancel-scope RuntimeWarning before finding 15's
    fix)."""
    from halo_harness.mcp.manager import McpManager
    with tempfile.TemporaryDirectory() as td:
        pid_file = Path(td) / "pid.txt"
        old = os.environ.pop("MCP_TIMEOUT", None)
        try:
            os.environ["MCP_TIMEOUT"] = "150"
            cfg = _fake_cfg("slow", mode="slow")
            cfg.env["FAKE_MCP_PID_FILE"] = str(pid_file)
            mgr = McpManager({"slow": cfg}, tool_env={**os.environ, "FAKE_MCP_SLEEP_S": "3"})
            mgr.start_all()
            ctx.check(f"failed within MCP_TIMEOUT, got {mgr.status()[0]}", mgr.status()[0]["state"] == "failed")
            h = mgr.handles["slow"]
            fut = h._lifecycle_future
            mgr.close_all()
            # give the OS a moment to actually reap the killed child on slow CI
            deadline = time.monotonic() + 5.0
            pid = None
            if pid_file.exists():
                try:
                    pid = int(pid_file.read_text(encoding="utf-8").strip())
                except ValueError:
                    pid = None
            while pid is not None and _pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if pid is not None:
                ctx.check(f"no child process survives (pid {pid})", not _pid_alive(pid))
            exc = None
            if fut is not None:
                try:
                    exc = fut.exception(timeout=0)
                except Exception as e:
                    exc = e
            ctx.check(f"the lifecycle future holds no unhandled exception, got {exc!r}", exc is None)
        finally:
            if old is None:
                os.environ.pop("MCP_TIMEOUT", None)
            else:
                os.environ["MCP_TIMEOUT"] = old


@test
def test_looks_like_auth_required_detection(ctx: Ctx):
    """The needs_auth STATE TRANSITION is exercised directly (a synthetic
    401-shaped exception) rather than via a real subprocess -- MCP auth/
    OAuth is an HTTP-transport concept with no stdio equivalent to
    reproduce it through (see fake_mcp_server.py's own docstring)."""
    from halo_harness.mcp.http_sse import looks_like_auth_required

    class _FakeResponse:
        status_code = 401

    class _FakeHttpError(Exception):
        response = _FakeResponse()

    ctx.check("a .response.status_code == 401 is detected", looks_like_auth_required(_FakeHttpError("boom")))
    ctx.check("a plain 'Unauthorized' message is detected", looks_like_auth_required(Exception("401 Unauthorized")))
    ctx.check("an ordinary connect failure is NOT flagged as auth",
              not looks_like_auth_required(ConnectionRefusedError("refused")))


@test
def test_needs_auth_state_via_injected_failure(ctx: Ctx):
    """A McpServerHandle whose transport raises a 401-shaped exception ends
    in state 'needs_auth', not 'failed' -- proven by monkeypatching
    `_open_transport` rather than requiring a live OAuth-gated server."""
    from halo_harness.mcp.client import McpLoop
    from halo_harness.mcp.manager import McpServerHandle

    loop = McpLoop()
    h = McpServerHandle(_fake_cfg(), loop, tool_env=dict(os.environ), cwd=REPO_DIR)

    async def _raise_401(_connect_timeout):
        raise PermissionError("401 Unauthorized: token expired")
    h._open_transport = _raise_401
    try:
        h.start()
        ctx.check(f"state is needs_auth, got {h.state}", h.state == "needs_auth")
    finally:
        h.close()
        loop.close()


# ---- headersHelper: off the event loop, trust-gated for project scope ----

@test
def test_headers_helper_does_not_block_other_loop_work(ctx: Ctx):
    """H4 must-do: headersHelper's blocking subprocess.run must run OFF
    the McpLoop's own event loop thread -- proven by running a slow (1s)
    helper CONCURRENTLY with an unrelated coroutine on the SAME loop; if
    the helper blocked the loop thread (the old sync `_resolved_headers`),
    the unrelated coroutine couldn't even START running until the helper
    finished."""
    from halo_harness.mcp.client import McpLoop
    from halo_harness.mcp.manager import McpServerConfig, McpServerHandle
    py = sys.executable
    helper_cmd = f'"{py}" -c "import time,json; time.sleep(1.0); print(json.dumps({{\'X\': \'y\'}}))"'
    cfg = McpServerConfig(name="s", type="http", url="https://example.invalid/mcp",
                           headers_helper=helper_cmd, scope="user")
    loop = McpLoop()
    h = McpServerHandle(cfg, loop, tool_env=dict(os.environ), cwd=REPO_DIR, trusted=True)
    try:
        headers_future = loop.spawn(h._resolved_headers())
        time.sleep(0.15)  # let the helper's subprocess actually start sleeping
        t0 = time.monotonic()
        loop.run(_noop_coro(), timeout=2.0)
        elapsed = time.monotonic() - t0
        ctx.check(f"an unrelated coroutine on the SAME loop runs promptly while the "
                  f"helper is still sleeping, took {elapsed:.2f}s", elapsed < 0.5)
        headers = headers_future.result(timeout=5)
        ctx.check(f"the helper's own result still comes through afterward, got {headers}", headers.get("X") == "y")
    finally:
        h.close()
        loop.close()


@test
def test_headers_helper_skipped_for_untrusted_project_scope(ctx: Ctx):
    """binary-facts sec.9: "repo-resident config needs persisted trust" --
    an untrusted PROJECT-scope (.mcp.json-sourced) server's headersHelper
    must never run; only the static headers are used."""
    from halo_harness.mcp.client import McpLoop
    from halo_harness.mcp.manager import McpServerConfig, McpServerHandle
    cfg = McpServerConfig(name="s", type="http", url="https://example.invalid/mcp",
                           headers={"Static": "yes"}, headers_helper="echo should-never-run",
                           scope="project")
    loop = McpLoop()
    h = McpServerHandle(cfg, loop, tool_env=dict(os.environ), cwd=REPO_DIR, trusted=False)
    try:
        headers = loop.run(h._resolved_headers(), timeout=5)
        ctx.check(f"untrusted project scope: helper skipped, only the static header survives, got {headers}",
                  headers == {"Static": "yes"})
    finally:
        h.close()
        loop.close()


@test
def test_headers_helper_runs_for_untrusted_non_project_scope(ctx: Ctx):
    """Only PROJECT-scope (repo-resident) config needs trust -- a
    user-scope server's headersHelper is the operator's OWN global
    config and must still run regardless of `trusted` (a fresh/untrusted
    project must not silently disable the user's own working setup)."""
    from halo_harness.mcp.client import McpLoop
    from halo_harness.mcp.manager import McpServerConfig, McpServerHandle
    py = sys.executable
    helper_cmd = f'"{py}" -c "import json; print(json.dumps({{\'X\': \'ran\'}}))"'
    cfg = McpServerConfig(name="s", type="http", url="https://example.invalid/mcp",
                           headers_helper=helper_cmd, scope="user")
    loop = McpLoop()
    h = McpServerHandle(cfg, loop, tool_env=dict(os.environ), cwd=REPO_DIR, trusted=False)
    try:
        headers = loop.run(h._resolved_headers(), timeout=10)
        ctx.check(f"user-scope helper still runs even when trusted=False, got {headers}", headers.get("X") == "ran")
    finally:
        h.close()
        loop.close()


@test
def test_f13_w6a_resolved_headers_adds_bearer_token_from_oauth_login(ctx: Ctx):
    """finding 13 (W6a): `halo mcp login <name>` saved tokens that
    NOTHING read -- `_resolved_headers` must add `Authorization: Bearer
    <access_token>` from `oauth.load_tokens(name)` when no static/helper
    header already set one."""
    from halo_harness.mcp import oauth
    from halo_harness.mcp.client import McpLoop
    from halo_harness.mcp.manager import McpServerConfig, McpServerHandle

    saved_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="w6a-f13-home-")))
    try:
        oauth.save_tokens("oauth_server", {"access_token": "tok-abc123", "refresh_token": "rtok-xyz"})
        cfg = McpServerConfig(name="oauth_server", type="http", url="https://example.invalid/mcp", scope="user")
        loop = McpLoop()
        h = McpServerHandle(cfg, loop, tool_env=dict(os.environ), cwd=REPO_DIR, trusted=True)
        try:
            headers = loop.run(h._resolved_headers(), timeout=5)
            ctx.check(f"Authorization header carries the saved access token, got {headers}",
                      headers.get("Authorization") == "Bearer tok-abc123")
        finally:
            h.close()
            loop.close()
    finally:
        if saved_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = saved_home


@test
def test_f13_w6a_resolved_headers_static_authorization_wins_over_oauth(ctx: Ctx):
    """An explicit static `Authorization` header (or one a headersHelper
    sets) must never be silently overwritten by a stored OAuth token."""
    from halo_harness.mcp import oauth
    from halo_harness.mcp.client import McpLoop
    from halo_harness.mcp.manager import McpServerConfig, McpServerHandle

    saved_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="w6a-f13-home2-")))
    try:
        oauth.save_tokens("oauth_server2", {"access_token": "should-not-be-used"})
        # finding 13 (W6a): kept under 16 chars after "Bearer " on purpose
        # -- tests/test_privacy_scan.py's own bearer-token-shaped-fragment
        # scan (halo_harness.redact._BEARER_RE) flags anything longer as
        # a possible real leaked credential.
        cfg = McpServerConfig(name="oauth_server2", type="http", url="https://example.invalid/mcp",
                               headers={"Authorization": "Bearer static-tok"}, scope="user")
        loop = McpLoop()
        h = McpServerHandle(cfg, loop, tool_env=dict(os.environ), cwd=REPO_DIR, trusted=True)
        try:
            headers = loop.run(h._resolved_headers(), timeout=5)
            ctx.check(f"the explicit static header wins, got {headers}",
                      headers.get("Authorization") == "Bearer static-tok")
        finally:
            h.close()
            loop.close()
    finally:
        if saved_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = saved_home


@test
def test_f13_w6a_connect_with_oauth_retry_refreshes_once_then_succeeds(ctx: Ctx):
    """finding 13 (W6a): a connect attempt that looks like a 401, with a
    refresh_token on file, is retried exactly once with a freshly
    refreshed access token -- isolated from the real MCP transport via a
    fake `connect_fn` that fails on the first (stale-token) header and
    succeeds on the second (refreshed) one."""
    from halo_harness.mcp import oauth
    from halo_harness.mcp.client import McpLoop
    from halo_harness.mcp.manager import McpServerConfig, McpServerHandle

    saved_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="w6a-f13-retry-")))
    try:
        oauth.save_tokens("retry_server", {"access_token": "stale-token", "refresh_token": "the-refresh-token"})

        def _fake_refresh(name, stored, *, oauth_cfg, server_url):
            ctx.check(f"the stored refresh_token reached refresh_tokens, got {stored}",
                      stored.get("refresh_token") == "the-refresh-token")
            new_tokens = {"access_token": "refreshed-token", "refresh_token": "the-refresh-token"}
            oauth.save_tokens(name, new_tokens)
            return new_tokens, None
        import halo_harness.mcp.oauth as oauth_mod
        original_refresh = oauth_mod.refresh_tokens
        oauth_mod.refresh_tokens = _fake_refresh

        calls = []

        async def _fake_connect(*, url, headers, connect_timeout):
            calls.append(dict(headers))
            if headers.get("Authorization") == "Bearer stale-token":
                raise RuntimeError("401 Unauthorized")
            return "connected-ok"

        cfg = McpServerConfig(name="retry_server", type="http", url="https://example.invalid/mcp", scope="user")
        loop = McpLoop()
        h = McpServerHandle(cfg, loop, tool_env=dict(os.environ), cwd=REPO_DIR, trusted=True)
        try:
            headers = loop.run(h._resolved_headers(), timeout=5)
            result = loop.run(h._connect_with_oauth_retry(_fake_connect, headers=headers, connect_timeout=5.0),
                               timeout=5)
            ctx.check(f"the connect eventually succeeded, got {result!r}", result == "connected-ok")
            ctx.check(f"exactly two attempts were made (one retry), got {len(calls)}", len(calls) == 2)
            ctx.check(f"the SECOND attempt used the refreshed token, got {calls}",
                      calls[1].get("Authorization") == "Bearer refreshed-token")
        finally:
            h.close()
            loop.close()
            oauth_mod.refresh_tokens = original_refresh
    finally:
        if saved_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = saved_home


@test
def test_run_all_scopes_the_state_dir_when_nothing_set_it_first(ctx: Ctx):
    """Test hygiene (round B fix pass, notes file): THIS file is exactly
    the one the notes file names -- a standalone `python tests/test_mcp_
    manager.py` run used to write fake-server logs into the REAL
    `~/.halo/mcp/`, because nothing in this file itself (unlike files
    that call `ensure_scoped_state_dir_once()`/`ensure_default_provider_
    credentials()` at module level) ever scoped `BRIDGE_TEST_HOME`
    before a test touched `bridge_home()`. `run_all` itself now does
    this, before running any test -- checked here by clearing both
    state-dir vars, running an EMPTY test list through it, and confirming
    `bridge_home()` resolves under a freshly-made temp dir afterward,
    never the real machine home."""
    import tests.helpers.runner as runner_mod
    from halo_harness.config.paths import bridge_home
    saved = {k: os.environ.get(k) for k in ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_TEST_NO_BACKGROUND_NET")}
    for k in saved:
        os.environ.pop(k, None)
    try:
        real_home = Path.home() / ".halo"
        runner_mod.run_all([], Ctx())
        ctx.check("BRIDGE_TEST_HOME was set by run_all with nothing scoping it before",
                  bool(os.environ.get("BRIDGE_TEST_HOME")))
        ctx.check(f"BRIDGE_TEST_NO_BACKGROUND_NET was set too, got {os.environ.get('BRIDGE_TEST_NO_BACKGROUND_NET')!r}",
                  os.environ.get("BRIDGE_TEST_NO_BACKGROUND_NET") == "1")
        resolved = bridge_home()
        ctx.check(f"bridge_home() resolves under the scoped temp dir, got {resolved}",
                  str(resolved) != str(real_home) and str(resolved).startswith(os.environ["BRIDGE_TEST_HOME"]))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
