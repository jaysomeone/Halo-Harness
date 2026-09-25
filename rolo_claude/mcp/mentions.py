"""rolo_claude.mcp.mentions -- `@server:resource` mention expansion (H8
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
from typing import Optional

# `@server-name:some/uri` -- server names are the same charset MCP server
# config names use (letters/digits/-/_/.); the URI itself is whatever the
# server declared, taken verbatim up to the next whitespace.
_AT_SERVER_RESOURCE_RE = re.compile(r"@([A-Za-z0-9_.\-]+):(\S+)")


def _known_resources(mcp_manager) -> "dict[str, set]":
    known: "dict[str, set]" = {}
    for server, resource in mcp_manager.resources():
        uri = getattr(resource, "uri", None)
        if uri is None:
            continue
        known.setdefault(server, set()).add(str(uri))
    return known


def extract_server_resource_mentions(text: str, *, mcp_manager) -> "list[tuple[str, str]]":
    """`[(server, uri), ...]` for every `@server:resource` mention in
    `text` where `server` names a CONNECTED MCP server that currently
    lists a resource with that exact uri. `mcp_manager=None` (no MCP
    client this session, or `--bare`) always returns `[]`."""
    if mcp_manager is None or not text:
        return []
    known = _known_resources(mcp_manager)
    if not known:
        return []
    out = []
    for m in _AT_SERVER_RESOURCE_RE.finditer(text):
        server, uri = m.group(1), m.group(2)
        if server in known and uri in known[server]:
            out.append((server, uri))
    return out


def _resource_result_text(result) -> str:
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
    return "\n".join(parts) if parts else "(empty resource)"


def read_server_resource_snapshots(text: str, *, mcp_manager) -> "list[tuple[str, str]]":
    """`[(label, content_text), ...]` -- one per matched `@server:resource`
    mention in `text`, actually read via `McpManager.read_resource`. Never
    raises: a read failure (the resource vanished, the server died between
    listing and reading) becomes a short note in place of the content,
    exactly like `commands.registry.read_at_mention_snapshots`'s own
    resilience for a file that stops existing between mention and read."""
    out = []
    for server, uri in extract_server_resource_mentions(text, mcp_manager=mcp_manager):
        label = f"{server}:{uri}"
        try:
            result = mcp_manager.read_resource(server, uri)
        except Exception as e:
            out.append((label, f"[could not read this MCP resource: {type(e).__name__}: {e}]"))
            continue
        out.append((label, _resource_result_text(result)))
    return out
