"""tests.test_ccbridge_server -- H11 Part B/C: ToolBridgeServer (parent
side) and the real `python -m rolo_claude.ccbridge` child, end to end
over BOTH transports it actually uses (Unix socket on POSIX, TCP loopback
+ token on Windows) -- via a real MCP client/server round trip (the exact
stack Claude Code itself drives), not a mock of the protocol.
"""
import json
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all

test, TESTS = new_registry()

REPO_DIR = Path(__file__).resolve().parent.parent


def _make_server(*, list_tools_fn=None, call_tool_fn=None, session_id="ccbridge-test"):
    from rolo_claude.ccbridge.server import ToolBridgeServer

    def _default_list():
        return [{"name": "Ping", "description": "pong tool",
                  "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}}}]

    def _default_call(name, arguments):
        if name == "Ping":
            return {"content": [{"type": "text", "text": f"PONG:{arguments.get('text')}"}], "is_error": False}
        return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "is_error": True}

    server = ToolBridgeServer(session_id=session_id, list_tools_fn=list_tools_fn or _default_list,
                                call_tool_fn=call_tool_fn or _default_call)
    server.start()
    return server


@test
def test_child_env_shape_matches_platform(ctx: Ctx):
    server = _make_server(session_id="env-shape")
    try:
        env = server.child_env()
        if os.name == "nt":
            ctx.check("has host", "ROLO_CCBRIDGE_HOST" in env)
            ctx.check("has port", "ROLO_CCBRIDGE_PORT" in env)
            ctx.check("has token", env.get("ROLO_CCBRIDGE_TOKEN"))
        else:
            ctx.check("has socket path", "ROLO_CCBRIDGE_SOCKET" in env)
            ctx.check("socket file exists", Path(env["ROLO_CCBRIDGE_SOCKET"]).exists())
    finally:
        server.close()


@test
def test_posix_socket_mode_0600(ctx: Ctx):
    if os.name == "nt":
        raise SkipTest("POSIX-only (Unix domain socket)")
    server = _make_server(session_id="mode-check")
    try:
        mode = server.socket_path.stat().st_mode & 0o777
        ctx.check(f"socket mode 0600, got {oct(mode)}", mode == 0o600)
    finally:
        server.close()


@test
def test_windows_loopback_connection_without_token_is_refused(ctx: Ctx):
    if os.name != "nt":
        raise SkipTest("Windows-only (TCP loopback + token transport)")
    server = _make_server(session_id="token-check")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((server.host, server.port))
        sock.sendall((json.dumps({"token": "wrong-token"}) + "\n").encode("utf-8"))
        sock.sendall((json.dumps({"id": 1, "method": "tools/list", "params": {}}) + "\n").encode("utf-8"))
        reply_line = sock.makefile("rb").readline()
        sock.close()
        if reply_line:
            reply = json.loads(reply_line.decode("utf-8"))
            ctx.check(f"bad token refused with an error, got {reply!r}", "error" in reply and reply.get("result") is None)
        else:
            ctx.check("connection closed outright on bad token (also an acceptable refusal)", True)
    finally:
        server.close()


@test
def test_raw_parent_link_list_and_call_tools(ctx: Ctx):
    """ParentLink (the child's OWN connection object) against a real
    ToolBridgeServer, without going through the MCP SDK layer -- isolates
    the bridge's own newline-JSON-RPC wire shape."""
    from rolo_claude.ccbridge.client import ParentLink

    calls = []
    server = _make_server(session_id="raw-link", call_tool_fn=lambda name, args: (
        calls.append((name, args)),
        {"content": [{"type": "text", "text": f"PONG:{args.get('text')}"}], "is_error": False},
    )[1])
    try:
        link = ParentLink(server.child_env())
        tools = link.list_tools()
        ctx.check(f"one tool listed, got {tools!r}", len(tools) == 1 and tools[0]["name"] == "Ping")
        result = link.call_tool("Ping", {"text": "hi"})
        ctx.check(f"call result shape, got {result!r}", result["content"][0]["text"] == "PONG:hi")
        ctx.check("is_error False", result["is_error"] is False)
        ctx.check("parent side recorded the call", calls == [("Ping", {"text": "hi"})])
        link.close()
    finally:
        server.close()


@test
def test_raw_parent_link_unknown_method_is_an_error(ctx: Ctx):
    from rolo_claude.ccbridge.client import ParentLink, ParentLinkError
    server = _make_server(session_id="raw-link-unknown")
    try:
        link = ParentLink(server.child_env())
        try:
            link._call("bogus/method", {})
            ctx.check("must raise on an unknown method", False)
        except ParentLinkError:
            ctx.check("ParentLinkError raised for an unknown method", True)
        link.close()
    finally:
        server.close()


@test
def test_call_tool_fn_exception_becomes_an_error_response_not_a_crash(ctx: Ctx):
    from rolo_claude.ccbridge.client import ParentLink

    def _boom(name, args):
        raise RuntimeError("dispatch exploded")

    server = _make_server(session_id="raw-link-boom", call_tool_fn=_boom)
    try:
        link = ParentLink(server.child_env())
        try:
            link.call_tool("Ping", {})
            ctx.check("must raise (server reports it as an error)", False)
        except Exception as e:
            ctx.check(f"error mentions the real exception, got {e}", "dispatch exploded" in str(e))
        # the connection/server must still be usable afterwards -- one bad
        # call must never take the whole bridge down.
        tools = link.list_tools()
        ctx.check("server still responds after a call_tool_fn crash", isinstance(tools, list))
        link.close()
    finally:
        server.close()


@test
def test_real_mcp_stdio_child_round_trip(ctx: Ctx):
    """The FULL child stack: `python -m rolo_claude.ccbridge` as a real
    MCP stdio server, driven by a real MCP client (mcp.ClientSession) --
    the exact shape Claude Code itself uses."""
    import asyncio

    async def _run():
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        calls = []
        server = _make_server(session_id="mcp-child-roundtrip", call_tool_fn=lambda name, args: (
            calls.append((name, args)),
            {"content": [{"type": "text", "text": f"PONG:{args.get('text')}"}], "is_error": False},
        )[1])
        try:
            env = dict(os.environ)
            env.update({k: str(v) for k, v in server.child_env().items()})
            env["PYTHONPATH"] = str(REPO_DIR)
            params = StdioServerParameters(command=sys.executable, args=["-m", "rolo_claude.ccbridge"], env=env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as mcp_session:
                    await mcp_session.initialize()
                    listed = await mcp_session.list_tools()
                    result = await mcp_session.call_tool("Ping", {"text": "hi"})
                    return [t.name for t in listed.tools], result.is_error, [c.text for c in result.content], calls
        finally:
            server.close()

    names, is_error, texts, calls = asyncio.run(_run())
    ctx.check(f"child listed our tool, got {names!r}", names == ["Ping"])
    ctx.check("is_error False", is_error is False)
    ctx.check(f"content round-tripped, got {texts!r}", texts == ["PONG:hi"])
    ctx.check("parent side saw the real call", calls == [("Ping", {"text": "hi"})])


@test
def test_real_mcp_stdio_child_error_result_is_error_flag(ctx: Ctx):
    import asyncio

    async def _run():
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        server = _make_server(session_id="mcp-child-error", call_tool_fn=lambda name, args: {
            "content": [{"type": "text", "text": "boom"}], "is_error": True,
        })
        try:
            env = dict(os.environ)
            env.update({k: str(v) for k, v in server.child_env().items()})
            env["PYTHONPATH"] = str(REPO_DIR)
            params = StdioServerParameters(command=sys.executable, args=["-m", "rolo_claude.ccbridge"], env=env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as mcp_session:
                    await mcp_session.initialize()
                    result = await mcp_session.call_tool("Ping", {"text": "hi"})
                    return result.is_error, [c.text for c in result.content]
        finally:
            server.close()

    is_error, texts = asyncio.run(_run())
    ctx.check("is_error True", is_error is True)
    ctx.check(f"error text carried through, got {texts!r}", texts == ["boom"])


@test
def test_multiple_sequential_connections_all_served(ctx: Ctx):
    """Belt-and-suspenders: the accept loop supports more than one
    connection over the server's lifetime (never assumes exactly one)."""
    from rolo_claude.ccbridge.client import ParentLink
    server = _make_server(session_id="multi-conn")
    try:
        for i in range(3):
            link = ParentLink(server.child_env())
            result = link.call_tool("Ping", {"text": str(i)})
            ctx.check(f"connection {i} got a real reply", result["content"][0]["text"] == f"PONG:{i}")
            link.close()
    finally:
        server.close()


@test
def test_server_close_is_idempotent_and_cleans_up_socket_file(ctx: Ctx):
    server = _make_server(session_id="close-idempotent")
    path = server.socket_path
    server.close()
    server.close()  # must not raise
    if path is not None:
        ctx.check("socket file removed on close", not path.exists())
    ctx.check("no exception raised by double close", True)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
