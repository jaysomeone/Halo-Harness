"""tests.test_hotfix_101_fix_webfetch_tls -- 1.0.1 fixpass finding 8:
WebFetch's own `urllib.request.build_opener(handler)` used urllib's DEFAULT
TLS context (not `default_tls_context()`, so not caught by the "no plain
urlopen(" grep the original TLS hotfix relied on) -- on Python 3.13 behind
a TLS-inspecting proxy WebFetch failed the same "basic constraints of CA
cert not marked critical" error every OTHER HTTPS call in this harness was
already fixed for. MCP http/sse (mcp/http_sse.py, entirely separate httpx-
based connect path) was outside the fix for the same underlying reason.
"""
from __future__ import annotations

import ssl
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_UNRESOLVABLE_URL = "https://totally-unresolvable-host.invalid/page"


@test
def test_webfetch_opener_uses_the_shared_tls_context(ctx: Ctx):
    """`_fetch_once` must build its opener with an HTTPSHandler carrying
    `default_tls_context()` (VERIFY_X509_STRICT cleared when present, the
    same custom-CA-bundle env loading) -- not urllib's own bare default."""
    from halo_harness.providers.http import default_tls_context
    from halo_harness.tools.webfetch import WebFetchTool

    real_build_opener = urllib.request.build_opener
    captured: list = []

    def spying_build_opener(*handlers):
        captured.append(handlers)
        return real_build_opener(*handlers)

    urllib.request.build_opener = spying_build_opener
    try:
        tool = WebFetchTool()
        result = tool._fetch_once(_UNRESOLVABLE_URL)
        # The host is deliberately unresolvable -- this always fails, fast,
        # with no real network dependency; what matters is HOW the opener
        # that failure went through was built.
        from halo_harness.tools.base import ToolResult
        ctx.check(f"an unresolvable host is still reported as a normal tool error, got {result}",
                  isinstance(result, ToolResult) and result.is_error)
        ctx.check(f"build_opener was called exactly once, got {len(captured)}", len(captured) == 1)
        handlers = captured[0]
        https_handlers = [h for h in handlers if isinstance(h, urllib.request.HTTPSHandler)]
        ctx.check(f"an HTTPSHandler was passed to build_opener, got handlers={handlers}",
                  len(https_handlers) == 1)
        got_ctx = https_handlers[0]._context
        expected = default_tls_context()
        ctx.check(f"its SSLContext is built via default_tls_context() (same verify_flags), "
                  f"got {got_ctx.verify_flags!r} expected {expected.verify_flags!r}",
                  got_ctx.verify_flags == expected.verify_flags)
        strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict_flag:
            ctx.check("VERIFY_X509_STRICT is cleared, not urllib's own stricter 3.13+ default",
                      not (got_ctx.verify_flags & strict_flag))
    finally:
        urllib.request.build_opener = real_build_opener


@test
def test_webfetch_redirect_handler_still_present(ctx: Ctx):
    """The TLS fix must be ADDITIVE -- the existing same-host redirect
    handler is still passed to build_opener alongside the new HTTPSHandler,
    never replaced by it."""
    from halo_harness.tools.webfetch import WebFetchTool, _SameHostRedirectHandler

    real_build_opener = urllib.request.build_opener
    captured: list = []

    def spying_build_opener(*handlers):
        captured.append(handlers)
        return real_build_opener(*handlers)

    urllib.request.build_opener = spying_build_opener
    try:
        WebFetchTool()._fetch_once(_UNRESOLVABLE_URL)
        handlers = captured[0]
        ctx.check(f"the redirect handler is still one of the opener's handlers, got {handlers}",
                  any(isinstance(h, _SameHostRedirectHandler) for h in handlers))
    finally:
        urllib.request.build_opener = real_build_opener


# ---------------------------------------------------------------------------
# MCP http/sse: the same TLS policy, via httpx's own `verify=`.
# ---------------------------------------------------------------------------

@test
def test_mcp_http_client_factory_uses_the_shared_tls_context(ctx: Ctx):
    import asyncio
    from halo_harness.mcp.http_sse import _mcp_http_client_factory
    from halo_harness.providers.http import default_tls_context

    async def _build_and_inspect():
        client = _mcp_http_client_factory(headers={"X-Test": "1"})
        try:
            ssl_ctx = client._transport._pool._ssl_context
            expected = default_tls_context()
            ctx.check(f"httpx client's SSLContext matches default_tls_context() (same verify_flags), "
                      f"got {ssl_ctx.verify_flags!r} expected {expected.verify_flags!r}",
                      ssl_ctx.verify_flags == expected.verify_flags)
            ctx.check(f"caller headers still reach the client, got {dict(client.headers)}",
                      client.headers.get("x-test") == "1")
        finally:
            await client.aclose()

    try:
        asyncio.run(_build_and_inspect())
    except ImportError:
        # No real httpx build importable in this environment at all --
        # the factory's own fallback (create_mcp_http_client, unmodified)
        # is exactly what finding 8's fix says to do in that case.
        pass


@test
def test_mcp_http_client_factory_falls_back_when_tls_context_fails(ctx: Ctx):
    """Best-effort, per the fix's own contract: a context-build failure
    must still hand back a USABLE client (the SDK's own unmodified
    create_mcp_http_client), never raise and break the connection."""
    import asyncio
    import halo_harness.mcp.http_sse as http_sse_mod

    async def _build():
        client = http_sse_mod._mcp_http_client_factory(headers={})
        try:
            ctx.check(f"a client object was still returned, got {client!r}", client is not None)
        finally:
            await client.aclose()

    import halo_harness.providers.http as http_mod
    real_default_tls_context = http_mod.default_tls_context
    http_mod.default_tls_context = lambda: (_ for _ in ()).throw(RuntimeError("simulated"))
    try:
        asyncio.run(_build())
    finally:
        http_mod.default_tls_context = real_default_tls_context


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
