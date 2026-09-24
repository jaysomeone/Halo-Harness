"""tests.test_mcp_manager -- rolo_claude/mcp/{client,stdio,http_sse,
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
from rolo_claude.mcp import manager as M

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
        # our own approvals store also grants approval
        resolved2, _ = M.resolve_server_configs(cwd=cwd, claude_json=claude_json, print_mode=False,
                                                  approvals={"c": True})
        ctx.check("our own approvals.json entry approves it too", resolved2["c"].pending_approval is False)


@test
def test_resolve_print_mode_approves_dot_mcp_json_without_asking(ctx: Ctx):
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        (cwd / ".mcp.json").write_text('{"mcpServers": {"a": {"command": "a"}}}', encoding="utf-8")
        resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json={}, print_mode=True)
        ctx.check("-p loads .mcp.json servers without asking [D-CFG]", resolved["a"].pending_approval is False)


@test
def test_resolve_extra_dynamic_lowest_precedence(ctx: Ctx):
    cwd = Path("/tmp/proj3")
    claude_json = _claude_json(user={"srv": {"command": "real-cmd"}})
    dynamic = {"srv": M.McpServerConfig(name="srv", type="stdio", command="dynamic-cmd", scope="dynamic"),
               "claude-in-chrome": M.McpServerConfig(name="claude-in-chrome", type="stdio", command="claude", scope="dynamic")}
    resolved, _ = M.resolve_server_configs(cwd=cwd, claude_json=claude_json, extra_dynamic=dynamic)
    ctx.check("a real config entry wins over a same-named dynamic one", resolved["srv"].command == "real-cmd")
    ctx.check("a dynamic server with no collision is still added", resolved["claude-in-chrome"].command == "claude")


# ---- McpLoop ------------------------------------------------------------

@test
def test_mcp_loop_runs_a_coroutine_and_returns_its_result(ctx: Ctx):
    from rolo_claude.mcp.client import McpLoop
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
    from rolo_claude.mcp.client import McpLoop
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
    from rolo_claude.mcp.client import McpLoop
    loop = McpLoop()
    loop.run(_noop_coro())
    loop.close()
    loop.close()  # must not raise
    ctx.check("closing twice never raises", True)


async def _noop_coro():
    return None


# ---- live connections through the REAL fake stdio server -------------------

def _fake_cfg(name="fake", *, mode=None, tool_count=None, cwd=None):
    from rolo_claude.mcp.manager import McpServerConfig
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
    from rolo_claude.mcp.client import McpLoop
    from rolo_claude.mcp.manager import McpServerHandle
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
    from rolo_claude.mcp.manager import McpManager
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
    from rolo_claude.mcp.manager import McpManager
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
    from rolo_claude.mcp.manager import McpManager
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
    from rolo_claude.mcp.manager import McpManager
    mgr = McpManager({"fake": _fake_cfg()}, tool_env=dict(os.environ))
    try:
        mgr.start_all()
        result = mgr.call("fake", "echo", {"text": "round trip"})
        ctx.check("real call_tool round trip", result.content[0].text == "round trip")
    finally:
        mgr.close_all()


@test
def test_manager_all_tools_name_sorted_and_sanitised(ctx: Ctx):
    from rolo_claude.mcp.manager import McpManager
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
    from rolo_claude.mcp.manager import McpManager
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
def test_manager_resources_and_prompts(ctx: Ctx):
    from rolo_claude.mcp.manager import McpManager
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
    from rolo_claude.mcp.manager import McpManager
    mgr = McpManager({"big": _fake_cfg("big", tool_count=300)}, tool_env=dict(os.environ))
    try:
        mgr.start_all()
        ctx.check(f"connects, state={mgr.status()[0]}", mgr.status()[0]["state"] == "connected")
        ctx.check("discovers all 300 tools", len(mgr.all_tools()) == 300)
    finally:
        mgr.close_all()


@test
def test_looks_like_auth_required_detection(ctx: Ctx):
    """The needs_auth STATE TRANSITION is exercised directly (a synthetic
    401-shaped exception) rather than via a real subprocess -- MCP auth/
    OAuth is an HTTP-transport concept with no stdio equivalent to
    reproduce it through (see fake_mcp_server.py's own docstring)."""
    from rolo_claude.mcp.http_sse import looks_like_auth_required

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
    from rolo_claude.mcp.client import McpLoop
    from rolo_claude.mcp.manager import McpServerHandle

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


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
