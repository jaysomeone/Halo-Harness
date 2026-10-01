"""rolo_claude.mcp.tools_cache -- per-server tool cache (H13 Part A: "lazy
MCP start by default"). A lazy server's tool NAMES and DESCRIPTIONS must be
known before it is ever connected, so the frozen catalog/ToolSearch can find
and preload/defer them on session start without spawning a single process.

One JSON file per server, under `~/.rolo-claude/mcp/tools-cache/<server>.
json`, keyed by a hash of the server's own config entry (command/args/env/
url/headers -- the fields that actually determine what tools a `tools/list`
would return; `cwd`/`timeout`/`alwaysLoad`/`mcpLazy`/scope never do). A
missing file, a hash mismatch (config changed since the cache was written),
or unreadable/corrupt JSON all read back as "no cache" (`None`) -- the
caller's job (`mcp_setup.py`) is then to connect once, for real, and write
a fresh cache from what it gets back.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

from rolo_claude.config.paths import bridge_home
from rolo_claude.mcp.manager import sanitize_name


def cache_dir() -> Path:
    return bridge_home() / "mcp" / "tools-cache"


def cache_file_path(server_name: str) -> Path:
    return cache_dir() / f"{sanitize_name(server_name)}.json"


def config_cache_key(cfg) -> str:
    """sha256 of the identity-relevant subset of one server's resolved
    config (command/args/env/url/headers) -- same "hash the entry" spirit
    as `manager.mcp_approval_key`, but over the fields that actually change
    what `tools/list` would return, not the whole raw JSON entry (a
    `timeout`/`cwd`/scope edit must never invalidate a perfectly good
    cache)."""
    shape = {
        "command": cfg.command, "args": list(cfg.args or []),
        "env": dict(cfg.env or {}), "url": cfg.url, "headers": dict(cfg.headers or {}),
    }
    canonical = json.dumps(shape, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CachedTool:
    """A `mcp.types.Tool`-shaped stand-in built from a cache file, so every
    caller that only ever reads `.name`/`.description`/`.input_schema`/
    `.meta` off a real SDK Tool (agent/catalog.py, tools/mcp_tool.py) works
    identically whether a tool came from a live connection or the cache."""

    def __init__(self, name: str, description: str, input_schema: dict, meta: Optional[dict]) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.meta = meta


def tool_to_dict(tool) -> dict:
    return {
        "name": getattr(tool, "name", ""),
        "description": getattr(tool, "description", None) or "",
        "input_schema": getattr(tool, "input_schema", None) or {"type": "object", "properties": {}},
        "meta": getattr(tool, "meta", None) or {},
    }


def tool_from_dict(d: dict) -> CachedTool:
    return CachedTool(
        name=d.get("name", ""), description=d.get("description", "") or "",
        input_schema=d.get("input_schema") or {"type": "object", "properties": {}},
        meta=d.get("meta") or {},
    )


def read_cache(server_name: str) -> Optional[dict]:
    """`{"hash":, "tools": [tool dicts], "instructions":, "cached_at":}` or
    `None` -- missing file, unreadable, or not a JSON object all read back
    as "no cache" (never raises; a corrupt cache is exactly as informative
    as no cache at all, so the caller just re-bootstraps)."""
    path = cache_file_path(server_name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "hash" not in data:
        return None
    return data


def write_cache(server_name: str, *, key: str, tools: list, instructions: Optional[str]) -> None:
    """Best-effort atomic write (tmp + `os.replace`, same pattern as
    `mcp_setup.record_mcp_approval`/`mcp_cli._write_claude_json_raw`) --
    a cache write failing (full disk, read-only home) must never crash a
    session that otherwise just connected successfully."""
    path = cache_file_path(server_name)
    data = {
        "hash": key, "tools": [tool_to_dict(t) for t in (tools or [])],
        "instructions": instructions, "cached_at": time.time(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def cache_age_s(server_name: str) -> Optional[float]:
    """Seconds since this server's cache was written, or `None` when there
    is no (readable) cache -- `doctor`'s own "cache ages" line."""
    entry = read_cache(server_name)
    if entry is None:
        return None
    cached_at = entry.get("cached_at")
    if not isinstance(cached_at, (int, float)):
        return None
    return max(0.0, time.time() - cached_at)


def tool_signature_set(tools: list) -> frozenset:
    """A comparable, order-independent fingerprint of a tool list (name +
    description + canonical-JSON schema) -- used to tell "the cache still
    matches what the server just returned live" from "the server's tool
    list drifted even though its config didn't" (H13 Part A: "stale cache +
    changed tools on connect")."""
    out = []
    for t in tools or []:
        schema = getattr(t, "input_schema", None) or {}
        try:
            schema_json = json.dumps(schema, sort_keys=True, default=str)
        except (TypeError, ValueError):
            schema_json = str(schema)
        out.append((getattr(t, "name", ""), getattr(t, "description", None) or "", schema_json))
    return frozenset(out)
