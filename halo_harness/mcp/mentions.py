"""halo_harness.mcp.mentions -- `@server:resource` mention expansion (H8
scope E, deferred by H3): a prompt or a custom-command/skill body may
reference an MCP server's resource the same way `@path` references a
local file (`commands/registry.py`'s own convention, `tui/completion.py`'s
own `@path` parsing) -- resolved here via `McpManager.resources()`/
`read_resource(server, uri)` and appended as a log snapshot, never inlined
into the submitted prompt text itself (the same "always a separate context
block" rule every other `@mention` kind in this harness already follows).

Best-effort, like every other `@mention` resolver in this package: a
`@server:uri` that doesn't match a REAL, currently-listed resource on a
CONNECTED server is silently left as plain text (the model still sees the
literal `@mention` and can use ListMcpResourcesTool/ReadMcpResourceTool
itself) -- never an error, never a hard reference.
"""

from __future__ import annotations

import re
import threading
import time
import weakref
from typing import Optional

# `@server-name:some/uri` -- server names are the same charset MCP server
# config names use (letters/digits/-/_/.); the URI itself is whatever the
# server declared, taken verbatim up to the next whitespace.
#
# review finding 23: the leading `(?<![^\s])` requires start-of-text or a
# preceding whitespace character -- without it, this matched the `@` inside
# an ordinary `git@github.com:org/repo.git` clone URL or a bare `user@
# host:22` SSH target as if it were a real `@server:uri` mention (server=
# "github.com"/"host", uri="org/repo.git"/"22"), which `unresolved_server_
# mentions` below then warned about as an unconnected MCP server.
_AT_SERVER_RESOURCE_RE = re.compile(r"(?<![^\s])@([A-Za-z0-9_.\-]+):(\S+)")

# H9 whole-tree review finding 14: `McpManager.resources()` is one live
# `resources/list` RPC per CONNECTED server (MCP_TIMEOUT, 30s, each) -- a
# short cache so a session that mentions `@server:uri` more than once in a
# short span (the common case: someone actively using this feature) doesn't
# re-pay that cost on every single prompt/steer. Keyed by the manager
# object itself (`WeakKeyDictionary`, never a bare `id()` -- a GC'd
# manager's address being reused by an unrelated later object could
# otherwise serve stale data for it) so the cache entry's lifetime is tied
# to the manager's own, with no explicit invalidation needed on session end.
_CACHE_TTL_S = 15.0
_cache_lock = threading.Lock()
_resource_cache: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _known_resources(mcp_manager) -> "dict[str, set]":
    known: "dict[str, set]" = {}
    for server, resource in mcp_manager.resources():
        uri = getattr(resource, "uri", None)
        if uri is None:
            continue
        known.setdefault(server, set()).add(str(uri))
    return known


def _known_resources_cached(mcp_manager) -> "dict[str, set]":
    now = time.monotonic()
    with _cache_lock:
        cached = _resource_cache.get(mcp_manager)
        if cached is not None and now - cached[0] < _CACHE_TTL_S:
            return cached[1]
    known = _known_resources(mcp_manager)
    with _cache_lock:
        _resource_cache[mcp_manager] = (now, known)
    return known


def extract_server_resource_mentions(text: str, *, mcp_manager) -> "list[tuple[str, str]]":
    """`[(server, uri), ...]` for every `@server:resource` mention in
    `text` where `server` names a CONNECTED MCP server that currently
    lists a resource with that exact uri. `mcp_manager=None` (no MCP
    client this session, or `--bare`) always returns `[]`.

    H9 whole-tree review finding 14: the cheap regex runs FIRST now -- the
    live (if uncached) `resources()` RPC only ever happens when `text`
    actually contains at least one `@name:uri`-shaped candidate. Verified
    bug: ordinary prompts with no `@` mention at all ("fix the failing
    test", "why is the build red?", "thanks") used to block for 1.5s each
    against a stub manager, and a genuinely hung server blocked for its
    FULL 30s timeout on every single Enter, mention or not."""
    if mcp_manager is None or not text:
        return []
    candidates = list(_AT_SERVER_RESOURCE_RE.finditer(text))
    if not candidates:
        return []
    known = _known_resources_cached(mcp_manager)
    if not known:
        return []
    out = []
    for m in candidates:
        server, uri = m.group(1), m.group(2)
        if server in known and uri in known[server]:
            out.append((server, uri))
    return out


# H9 whole-tree review finding 14: same cap `commands/registry.py`'s own
# `@path` mention reading uses (`_AT_MENTION_MAX_BYTES`) -- resource content
# was previously appended with NO size cap at all, so one `@server:huge-log`
# mention could dump an unbounded amount of text straight into the log as a
# single snapshot.
_RESOURCE_MENTION_MAX_BYTES = 200_000


def _resource_result_text(result) -> "Optional[str]":
    """`None` when the content is over the cap -- matches `@path`'s own
    "silently skipped, the mention text stays in place for the model to
    Read/fetch directly" behaviour, rather than a jagged mid-character
    truncation."""
    parts = []
    for content in getattr(result, "contents", None) or []:
        text_val = getattr(content, "text", None)
        if isinstance(text_val, str):
            parts.append(text_val)
            continue
        blob = getattr(content, "blob", None)
        if blob:
            mime = getattr(content, "mimeType", None) or "application/octet-stream"
            parts.append(f"[binary resource content ({mime}), not shown as text]")
    joined = "\n".join(parts) if parts else "(empty resource)"
    if len(joined.encode("utf-8", "replace")) > _RESOURCE_MENTION_MAX_BYTES:
        return None
    return joined


def _connected_server_names(mcp_manager) -> "set[str]":
    """review finding 23: `unresolved_server_mentions` used to check
    against `_known_resources_cached`'s own keys, which only ever contains
    a server that currently lists at least one resource -- a perfectly
    real, connected server with no resources at all (most MCP servers:
    tools only) was wrongly reported as "not connected". The real
    membership test is connection state, read straight off each handle,
    exactly like `McpManager.status()` does -- `.handles` is a real
    `McpManager`-only attribute, so this is empty (never raises) for a
    minimal, duck-typed test double that implements only `.resources()`/
    `.read_resource()` (this package's own documented minimal mcp_manager
    shape); `unresolved_server_mentions` below additionally falls back to
    `_known_resources_cached`'s own keys for exactly that case, so such a
    double's own resource-bearing servers are still recognized."""
    handles = getattr(mcp_manager, "handles", None) or {}
    return {name for name, h in handles.items() if getattr(h, "state", None) == "connected"}


def unresolved_server_mentions(text: str, *, mcp_manager) -> "list[str]":
    """W4a misc: "unresolved `@server:uri` mentions warn visibly naming the
    server" -- every `@name:uri`-SHAPED candidate in `text` whose `name`
    does NOT name a currently-connected MCP server at all (a URI that looks
    right but doesn't match any of that server's listed resources is left
    alone here -- that's `extract_server_resource_mentions`'s own silent-
    by-design case, a server typo is the one worth a visible word since the
    user almost certainly meant a real server and mistyped or forgot to
    connect it). Returns the distinct, first-seen-order server names named
    this way; `[]` when there are none or `mcp_manager` is None (no MCP
    client this session).

    review finding 23: "known" is `_connected_server_names` (handle state
    -- catches a real, connected, resource-less server) OR a key of
    `_known_resources_cached` (catches a duck-typed test double with no
    `.handles` at all, this package's own documented minimal mcp_manager
    shape, whose resource-bearing servers must still be recognized)."""
    if mcp_manager is None or not text:
        return []
    candidates = list(_AT_SERVER_RESOURCE_RE.finditer(text))
    if not candidates:
        return []
    try:
        resource_known = set(_known_resources_cached(mcp_manager))
    except Exception:
        # Best-effort, same spirit as every other mcp_manager call in this
        # module: a minimal/incompatible test double (no `.resources()`
        # at all, or not weakly-referenceable -- `types.SimpleNamespace`
        # instances aren't) must never crash this, only skip the duck-
        # typed fallback for it.
        resource_known = set()
    connected = _connected_server_names(mcp_manager) | resource_known
    seen: list = []
    for m in candidates:
        server = m.group(1)
        if server not in connected and server not in seen:
            seen.append(server)
    return seen


def read_server_resource_snapshots(text: str, *, mcp_manager) -> "list[tuple[str, str]]":
    """`[(label, content_text), ...]` -- one per matched `@server:resource`
    mention in `text`, actually read via `McpManager.read_resource`. Never
    raises: a read failure (the resource vanished, the server died between
    listing and reading) becomes a short note in place of the content,
    exactly like `commands.registry.read_at_mention_snapshots`'s own
    resilience for a file that stops existing between mention and read. An
    oversized resource (finding 14) is skipped entirely -- same convention
    as `@path`."""
    out = []
    for server, uri in extract_server_resource_mentions(text, mcp_manager=mcp_manager):
        label = f"{server}:{uri}"
        try:
            result = mcp_manager.read_resource(server, uri)
        except Exception as e:
            out.append((label, f"[could not read this MCP resource: {type(e).__name__}: {e}]"))
            continue
        text_out = _resource_result_text(result)
        if text_out is None:
            continue
        out.append((label, text_out))
    return out
