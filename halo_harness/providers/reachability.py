"""halo_harness.providers.reachability -- the bounded background probe item
21.5/A.3 want per provider (table row / init tab): "reachable",
"unreachable (one-line reason)", or "not set up". Reuses the SAME 8s
connect-phase cap and "cannot resolve/reach" wording item 2's fail-fast
fix already established (`providers.http.open_upstream`/`_bounded_connect`)
-- a probe here is connect-phase ONLY (DNS + TCP + TLS, no bytes sent or
received), the cheapest real signal of "is this host reachable at all"
without spending a request/token against it. No home/work concept
anywhere -- every provider is probed the same way.
"""

from __future__ import annotations

import urllib.parse
from typing import Optional

from halo_harness.providers.http import UpstreamConnectError, open_upstream


def probe_host_reachable(base_url: str) -> "tuple[bool, Optional[str]]":
    """`(True, None)` once a TCP/TLS connection to `base_url`'s host opens
    within the usual 8s connect cap; `(False, "<one-line reason>")`
    otherwise (DNS failure, refused, timeout -- `UpstreamConnectError`'s
    own message, already one line). Never raises."""
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    if not host:
        return False, f"not a valid URL: {base_url!r}"
    port = parsed.port or (443 if parsed.scheme != "http" else 80)
    tls = parsed.scheme != "http"
    try:
        conn = open_upstream(host, port, tls)
    except UpstreamConnectError as e:
        return False, str(e)
    except Exception as e:  # never let a reachability probe crash a caller
        return False, f"{type(e).__name__}: {e}"
    try:
        conn.close()
    except Exception:
        pass
    return True, None


def provider_base_url(name: str) -> Optional[str]:
    """The host this provider's reachability probe should test -- None for
    a provider with no connect-probe concept of its own (not configured,
    TypeSafe, or the Claude subscription, which checks `claude auth
    status` instead -- see `reachability_tag`)."""
    from halo_harness.providers.enablement import canonical
    name = canonical(name)
    if name == "databricks":
        from halo_harness.providers.config import resolve_databricks
        dbx = resolve_databricks()
        return dbx.host if dbx is not None else None
    if name == "openrouter":
        from halo_harness.providers.config import resolve_openrouter
        orc = resolve_openrouter()
        return orc.base_url if orc is not None else "https://openrouter.ai/api/v1"
    if name == "anthropic":
        from halo_harness.providers.config import resolve_anthropic
        ant = resolve_anthropic()
        return ant.base_url if ant is not None else "https://api.anthropic.com"
    return None


def reachability_tag(name: str, *, detected: Optional[bool] = None) -> str:
    """item 21.5/A.3's three-state tag, as plain text: "reachable",
    "unreachable: <reason>", or "not set up" -- credentials are checked
    FIRST (never probes a host with nothing configured for it). The Claude
    subscription "probes" via `claude auth status` (already its own
    bounded subprocess check -- see `providers.cc_models`) rather than a
    bare TCP connect, since it has no separate network host of its own (it
    rides the installed `claude` binary); TypeSafe stores a key only, for
    a later feature, so it is never probed at all.

    `detected` (1.0.1 part 2 fixpass finding 1): a caller that already
    computed `credentials_present(canon, ...)` itself (`/providers`'s own
    `provider_rows()`) passes it straight through instead of this function
    re-deriving it a second time (for `claude_subscription`, an extra
    uncached `claude auth status` spawn)."""
    from halo_harness.providers.enablement import canonical, credentials_present
    canon = canonical(name)
    is_detected = credentials_present(canon) if detected is None else detected
    if not is_detected:
        return "not set up"
    if canon == "claude_subscription":
        return "reachable"  # credentials_present() already confirmed a real claude.ai login
    if canon == "typesafe":
        return "not probed (stores a key only, for a later feature)"
    base_url = provider_base_url(canon)
    if not base_url:
        return "not set up"
    ok, reason = probe_host_reachable(base_url)
    return "reachable" if ok else f"unreachable: {reason}"
