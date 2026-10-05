"""halo_harness.mcp.http_sse -- http (`streamable_http_client`) and sse
(`sse_client`, deprecated transport) connect, plus the `headersHelper`
contract (binary-facts sec.9: shell:true, 10s timeout, maxBuffer 1e6, exit 0
+ stdout that parses as a JSON object of strings, else one of
`exec_failed|parse_failed|non_object|non_string_value`). Imports `mcp`
lazily; `manager.py` is the only caller. None of the owner's real 15 configured
servers use http/sse (plan finding B: "15 stdio servers... no http/sse") so
this path is exercised by the fake-server unit tests, not the live
acceptance run.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from contextlib import AsyncExitStack
from typing import Optional


class HeadersHelperError(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        self.kind = kind  # exec_failed | parse_failed | non_object | non_string_value
        self.detail = detail
        super().__init__(f"{kind}: {detail}" if detail else kind)


def run_headers_helper(command: str, *, env: dict, server_name: str, url: str,
                        timeout: float = 10.0) -> dict:
    """Run `command` via the shell with `CLAUDE_CODE_MCP_SERVER_NAME`/
    `CLAUDE_CODE_MCP_SERVER_URL` in its environment; its stdout must be a
    JSON object of strings within `timeout` seconds (binary-facts sec.9).
    Raises `HeadersHelperError` on any deviation -- the caller decides
    whether that's fatal or just "use the static headers unchanged"."""
    helper_env = dict(env)
    helper_env["CLAUDE_CODE_MCP_SERVER_NAME"] = server_name
    helper_env["CLAUDE_CODE_MCP_SERVER_URL"] = url
    try:
        result = subprocess.run(
            command, shell=True, env=helper_env, timeout=timeout,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise HeadersHelperError("exec_failed", str(e)) from e
    if result.returncode != 0 or not (result.stdout or "").strip():
        raise HeadersHelperError("exec_failed", f"exit {result.returncode}")
    try:
        data = json.loads(result.stdout)
    except ValueError as e:
        raise HeadersHelperError("parse_failed", str(e)) from e
    if not isinstance(data, dict):
        raise HeadersHelperError("non_object", type(data).__name__)
    for k, v in data.items():
        if not isinstance(v, str):
            raise HeadersHelperError("non_string_value", f"{k!r}: {type(v).__name__}")
    return {str(k): v for k, v in data.items()}


def merged_headers(static_headers: Optional[dict], helper_headers: Optional[dict]) -> dict:
    """Helper headers OVERRIDE static ones (binary-facts sec.9: "helper
    headers override static headers")."""
    out = dict(static_headers or {})
    out.update(helper_headers or {})
    return out


def looks_like_auth_required(exc: BaseException) -> bool:
    """Best-effort 401/oauth-required detection on a connect failure, from
    whatever shape httpx/the SDK raised -- no flow is implemented in v1
    (D-CFG: "oauth/401 -> needs_auth (no flow in v1)"), this just decides
    whether the resulting state is `needs_auth` instead of `failed`."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 401:
        return True
    text = str(exc).lower()
    return "401" in text or "unauthorized" in text or "oauth" in text


def _mcp_http_client_factory(headers: Optional[dict] = None, timeout=None, auth=None):
    """`create_mcp_http_client`-compatible factory (same call shape
    `sse_client`'s own `httpx_client_factory=` parameter expects) -- but
    with `default_tls_context()` as `verify=` (1.0.1 fixpass finding 8):
    MCP http/sse connections go through httpx entirely OUTSIDE http.py's
    own open_upstream/urlopen_tls, so they never picked up this hotfix's
    VERIFY_X509_STRICT-clearing/custom-CA-bundle TLS policy -- the same
    TLS-inspecting-proxy failure WebFetch had (see that tool's own fix)
    could equally hit an http/sse MCP server. Best-effort ("when httpx is
    present"): falls straight back to the SDK's own unmodified
    `create_mcp_http_client` (whatever this installed httpx build's default
    verification policy is) if no importable httpx build is found, or if
    building the context / passing `verify=` fails for any reason -- this
    must never turn a connection that would otherwise have worked into a
    failure."""
    from mcp.shared._httpx_utils import create_mcp_http_client
    try:
        from halo_harness.providers.http import default_tls_context
        ctx = default_tls_context()
    except Exception:
        return create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
    try:
        import httpx2 as _httpx  # the MCP SDK and requirements.lock are built on httpx2
    except ImportError:
        try:
            import httpx as _httpx  # classic httpx, only when httpx2 is absent
        except ImportError:
            return create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
    try:
        from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT
        if timeout is None:
            timeout = _httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)
        kwargs: dict = {"timeout": timeout, "verify": ctx}
        if headers is not None:
            kwargs["headers"] = headers
        if auth is not None:
            kwargs["auth"] = auth
        return _httpx.AsyncClient(**kwargs)
    except Exception:
        return create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)


async def preflight_tcp_reachability(url: str, *, timeout: float, open_connection=None) -> None:
    """Halo 2.0.2 round C: "connection refused and DNS failure fail fast
    (well under a second) instead of waiting out MCP_TIMEOUT". Verified
    directly against the installed MCP SDK: `streamable_http_client`'s
    own context-manager entry (what `connect_timeout`/`task_timeout`
    below actually wrap) is LAZY -- it returns in well under a second
    regardless of whether anything is even listening -- so neither
    `connect_http` nor `connect_sse`'s own timeout ever caught a dead
    server at all; the real first network round trip only happens later
    inside `ClientSession.initialize()` (called from `mcp/manager.py`,
    outside this module entirely), which then blocks on a low-level
    anyio memory-stream wait that a raw `asyncio.Task.cancel()` (this
    same codebase's own `mcp.client.task_timeout`) could NOT reliably
    interrupt within its configured window either (verified: it still
    hadn't returned after the window had long since passed). Rather than
    fight that SDK-internal cancellation gap, this runs a plain stdlib-
    shaped TCP connect attempt BEFORE any of that -- verified to raise
    promptly and cleanly on both failure shapes (a closed port: a real
    `ConnectionRefusedError` in a couple of seconds on Windows loopback,
    almost certainly faster on Linux/the project's primary platform; a
    bad hostname: `socket.gaierror` in well under a second).

    Raises `ConnectionRefusedError` ONLY for a CONFIRMED refusal/DNS
    failure (any `OSError`, which covers both). Returns normally (never
    raises) for anything else this can't confirm either way: no host in
    the URL, this probe's OWN `timeout` elapsing (inconclusive -- a
    genuinely slow-but-reachable server must still get its usual, longer
    chance via the real connect attempt that follows, unchanged), or any
    other surprise -- a false positive here would wrongly fail a server
    this was never meant to touch at all."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    open_connection = open_connection or asyncio.open_connection
    try:
        reader, writer = await asyncio.wait_for(open_connection(host, port), timeout=timeout)
    except asyncio.TimeoutError:
        return  # inconclusive (merely slow) -- let the real connect attempt decide
    except OSError as e:
        raise ConnectionRefusedError(f"could not reach {host}:{port} ({type(e).__name__}: {e})") from e
    else:
        writer.close()


async def connect_http(*, url: str, headers: dict, connect_timeout: float):
    """Streamable-HTTP connect. Returns `(stack, session)`, stack OPEN --
    same contract as `stdio.connect`. Headers are carried on a pre-built
    httpx.AsyncClient (this SDK version's `streamable_http_client` no
    longer takes `headers=` directly -- verified against the installed
    2.2.0 API, not assumed). finding 15: `connect_timeout` uses
    `client.task_timeout` (same-task `Task.cancel()`), not
    `asyncio.wait_for` -- see `stdio.connect`'s own docstring for why.

    Round C: `preflight_tcp_reachability` runs FIRST -- see its own
    docstring for why a connection-refused/DNS-failure server would
    otherwise sail straight past every timeout below."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from halo_harness.mcp.client import task_timeout

    await preflight_tcp_reachability(url, timeout=connect_timeout)
    http_client = _mcp_http_client_factory(headers=headers or {})
    stack = AsyncExitStack()
    try:
        stack.push_async_callback(http_client.aclose)
        async with task_timeout(connect_timeout):
            streams = await stack.enter_async_context(
                streamable_http_client(url, http_client=http_client))
        # H9 bug fix: the installed 2.2.0 SDK's `streamable_http_client`
        # yields a 2-tuple `(read_stream, write_stream)` -- ITS OWN
        # docstring says so ("Yields: Tuple containing: read_stream,
        # write_stream"), no `get_session_id` callable at all -- but this
        # function's comment claimed a 3-tuple "verified against the
        # installed 2.2.0 API", which a real live connection attempt
        # (H9 MCP-compatibility matrix) proved false: `ValueError: not
        # enough values to unpack (expected 3, got 2)` on EVERY `type:
        # "http"` server, 100% of the time. Take only what's actually
        # there and tolerate either shape so a future SDK upgrade that
        # reintroduces a third element doesn't break this again.
        read, write = streams[0], streams[1]
        session = await stack.enter_async_context(ClientSession(read, write))
        return stack, session
    except BaseException:
        await stack.aclose()
        raise


async def connect_sse(*, url: str, headers: dict, connect_timeout: float):
    """Deprecated `sse` transport connect (kept for existing configs that
    still name it -- `manager.py` logs a deprecation notice when it does).
    Returns `(stack, session)`, stack OPEN. finding 15: same
    `client.task_timeout` swap as `connect_http`/`stdio.connect`. Round
    C: `preflight_tcp_reachability` runs first, same as `connect_http`
    -- see its own docstring."""
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from halo_harness.mcp.client import task_timeout

    await preflight_tcp_reachability(url, timeout=connect_timeout)
    stack = AsyncExitStack()
    try:
        async with task_timeout(connect_timeout + 1):
            read, write = await stack.enter_async_context(
                sse_client(url, headers=headers or {}, timeout=connect_timeout,
                           httpx_client_factory=_mcp_http_client_factory))
        session = await stack.enter_async_context(ClientSession(read, write))
        return stack, session
    except BaseException:
        await stack.aclose()
        raise
