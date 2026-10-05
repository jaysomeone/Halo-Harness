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


def probe_host_reachable(base_url: str, *, connect_timeout: Optional[int] = None) -> "tuple[bool, Optional[str]]":
    """`(True, None)` once a TCP/TLS connection to `base_url`'s host opens
    within the usual 8s connect cap; `(False, "<one-line reason>")`
    otherwise (DNS failure, refused, timeout -- `UpstreamConnectError`'s
    own message, already one line). Never raises.

    `connect_timeout` (W3b test-determinism seam): forwarded straight to
    `open_upstream`'s own `connect_timeout` -- `None` (every pre-existing
    caller) keeps the real 8s default; a test that wants a probe to fail
    fast (never actually waiting out a real connect attempt) passes a tiny
    value instead of monkeypatching `open_upstream` itself."""
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    if not host:
        return False, f"not a valid URL: {base_url!r}"
    port = parsed.port or (443 if parsed.scheme != "http" else 80)
    tls = parsed.scheme != "http"
    kwargs = {} if connect_timeout is None else {"connect_timeout": connect_timeout}
    try:
        conn = open_upstream(host, port, tls, **kwargs)
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
    if name == "huggingface":
        # Round 5 fix: this branch never existed before (a round 4 gap --
        # `reachability_tag("huggingface")` always fell through to "not
        # set up" below regardless of configuration, latent because
        # "huggingface" never appeared in `init_providers.TAB_PROVIDERS`
        # until round 5 wired it up; `/providers`/`halo providers` DID
        # already call this for every `PROVIDER_NAMES` row, including
        # huggingface, since round 4 -- so this also fixes that table's
        # own reachable column for an already-configured HF_TOKEN/
        # endpoint). Same priority order `providers.enablement.
        # credentials_present("huggingface")` already checks: the router
        # when HF_TOKEN is set, else the first configured dedicated
        # endpoint, else the first configured manual local server --
        # auto-detected servers are never probed from this generic,
        # config-only lookup (the `/local` view's own probe covers those).
        from halo_harness.providers.config import resolve_huggingface
        hf = resolve_huggingface()
        if hf is not None:
            return hf.base_url
        from halo_harness.providers.huggingface import resolve_huggingface_endpoints, resolve_huggingface_local_servers
        endpoints = resolve_huggingface_endpoints()
        if endpoints:
            return endpoints[0].url
        servers = resolve_huggingface_local_servers()
        return servers[0].url if servers else None
    if name == "openai":
        from halo_harness.providers.config import resolve_openai
        oai = resolve_openai()
        return oai.base_url if oai is not None else "https://api.openai.com/v1"
    if name == "ollama":
        # Round 5: the resolved DEFAULT host's url (never a specific named
        # LAN/cloud entry -- there is no single "the" host to pick among
        # several otherwise) -- `resolve_ollama_hosts` always synthesizes
        # at least one, so this never returns `None` once `ollama` reaches
        # this branch at all (only reached once `credentials_present
        # ("ollama")` already confirmed either an explicit `ollama.hosts`
        # entry or `OLLAMA_HOST` is set, per that function's own branch).
        from halo_harness.providers.ollama import resolve_ollama_host
        host = resolve_ollama_host(None)
        return host.url if host is not None else None
    return None


def reachability_tag(name: str, *, detected: Optional[bool] = None, connect_timeout: Optional[int] = None) -> str:
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
    uncached `claude auth status` spawn). `connect_timeout` (W3b): forwarded
    to `probe_host_reachable` -- see its own docstring."""
    from halo_harness.providers.enablement import canonical, credentials_present
    canon = canonical(name)
    is_detected = credentials_present(canon) if detected is None else detected
    if not is_detected:
        return "not set up"
    if canon == "claude_subscription":
        return "reachable"  # credentials_present() already confirmed a real claude.ai login
    if canon == "codex_subscription":
        return "reachable"  # credentials_present() already confirmed a real ChatGPT login
    if canon == "typesafe":
        return "not probed (stores a key only, for a later feature)"
    base_url = provider_base_url(canon)
    if not base_url:
        return "not set up"
    ok, reason = probe_host_reachable(base_url, connect_timeout=connect_timeout)
    return "reachable" if ok else f"unreachable: {reason}"
