"""rolo_claude.tools.mcp_tool -- McpTool (H3 scope B): wraps one MCP
server's tool as an ordinary `Tool` (`rolo_claude.tools.base`), named
`mcp__<server>__<tool>` (sanitised per binary-facts sec.9 via
`rolo_claude.mcp.manager.mcp_tool_name`). Content conversion (text/image/
embedded-resource/structuredContent/isError) and the output cap + Claude
Code's exact truncation string + spill-to-`tool-results/` are pure,
independently-testable functions below `McpTool` itself -- `McpTool.run`
just composes them around one `manager.call(...)`.

Does NOT import the `mcp` SDK itself -- everything here is duck-typed
against whatever `mcp.types.CallToolResult`/`Tool`/content objects the
manager hands back (`getattr(..., default)` throughout), so this module
stays importable without the SDK installed even though it's only ever
actually USED once a real McpManager exists.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from rolo_claude.mcp.manager import mcp_tool_name
from rolo_claude.tools.base import Tool, ToolContext, ToolResult

IMAGE_TOKEN_COST = 1_600  # binary-facts sec.9: "images count 1 600 tokens"


def default_output_token_limit() -> int:
    raw = os.environ.get("MAX_MCP_OUTPUT_TOKENS")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return 25_000  # binary-facts sec.9 default


def tool_always_load(meta: Optional[dict]) -> bool:
    """binary-facts sec.9: "`anthropic/alwaysLoad` (must be `=== true`)"
    -- a truthy-but-not-`True` value (e.g. the string "true", or 1) does
    NOT count, matching the strict-equality wording."""
    return isinstance(meta, dict) and meta.get("anthropic/alwaysLoad") is True


def tool_search_hint(meta: Optional[dict]) -> Optional[str]:
    """binary-facts sec.9: `_meta[anthropic/searchHint]` -- extra keyword
    text a server supplies specifically to improve ToolSearch discovery of
    a still-deferred tool (finding 13 must-do: "score searchHint"), given
    MORE weight than the tool's own name/description in
    `agent/catalog.py::SessionCatalog.search`'s ranking. A non-string (or
    blank) value is ignored rather than crashing the search."""
    if not isinstance(meta, dict):
        return None
    hint = meta.get("anthropic/searchHint")
    return hint if isinstance(hint, str) and hint.strip() else None


def tool_meta_max_result_size_chars(meta: Optional[dict]) -> Optional[int]:
    """binary-facts sec.9: "`anthropic/maxResultSizeChars` (positive
    finite)" -- anything else (missing, non-numeric, <=0, inf/nan) is
    ignored (the caller falls back to `MAX_MCP_OUTPUT_TOKENS*4`)."""
    if not isinstance(meta, dict):
        return None
    v = meta.get("anthropic/maxResultSizeChars")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v or v in (float("inf"), float("-inf")) or v <= 0:  # v != v catches NaN
        return None
    return int(v)


# ---- OpenCode-H9 MCP compatibility: per-family tool-schema sanitising -----
# Claude Code's own built-in tool schemas are flat (name/type/properties,
# no $ref/anyOf/tuple-items), so this never mattered for THEM -- but MCP
# servers ship arbitrary JSON Schema, and a non-Anthropic model's own
# function-calling schema validator can be considerably stricter than
# Claude's. Applied ONCE, at McpTool construction time (same point/same
# limitation as `vision`: re-sanitising after a `/model` family switch is
# H5's `set_model` territory, not this module's), keyed off
# `providers.profiles.model_family()`'s coarse family string.

def _strip_ref_siblings(node):
    """Moonshot/Kimi expands `$ref` before validation and rejects sibling
    keywords (e.g. `description`) on the SAME node -- reduce any dict that
    has both `$ref` and other keys down to `{"$ref": ...}` alone."""
    if isinstance(node, dict):
        if "$ref" in node and len(node) > 1:
            return {"$ref": node["$ref"]}
        return {k: _strip_ref_siblings(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_strip_ref_siblings(v) for v in node]
    return node


def _flatten_tuple_items(node):
    """Moonshot/Kimi's schema validator doesn't understand TUPLE-style
    `items` (a JSON array, one sub-schema per positional slot) -- flatten
    it to its first element (`items[0]`), same as OpenCode's own rule."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "items" and isinstance(v, list):
                out[k] = _flatten_tuple_items(v[0]) if v else {}
            else:
                out[k] = _flatten_tuple_items(v)
        return out
    if isinstance(node, list):
        return [_flatten_tuple_items(v) for v in node]
    return node


def _coerce_gemini_enums(node):
    """Gemini's function-calling schema only accepts STRING enum members
    -- coerce any non-string enum value rather than have Gemini reject
    the whole tool definition."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "enum" and isinstance(v, list):
                out[k] = [v2 if isinstance(v2, str) else json.dumps(v2, default=str) for v2 in v]
            else:
                out[k] = _coerce_gemini_enums(v)
        return out
    if isinstance(node, list):
        return [_coerce_gemini_enums(v) for v in node]
    return node


def sanitize_tool_schema(schema: dict, *, family: Optional[str] = None) -> dict:
    """`family` is `providers.profiles.model_family()`'s own string
    ("kimi", "gemini", ...); `None`/an unrecognised family (in particular
    "claude", which never reaches an OpenAI/Gemini/Moonshot-shaped
    endpoint) is a no-op -- returns `schema` unchanged."""
    if not isinstance(schema, dict):
        return schema
    out = schema
    if family == "kimi":
        out = _strip_ref_siblings(out)
        out = _flatten_tuple_items(out)
        # Kimi K2.5 additionally rejects a `{}` (no properties) object
        # schema without an explicit "required": [] -- Claude Code itself
        # never sends a bare {} for a real parameter schema, but a no-arg
        # MCP tool's own input_schema often is exactly that.
        if out.get("type") == "object" and not out.get("properties"):
            out = {**out, "properties": {}, "required": list(out.get("required") or [])}
    if family == "gemini":
        out = _coerce_gemini_enums(out)
    return out


def convert_content_blocks(content, *, vision: bool) -> list:
    """One `CallToolResult.content` list -> Anthropic-shaped blocks (scope
    B): text passthrough; `image` -> a real `image` block when `vision`,
    else a short text note; embedded resource -> its own text when
    present, an image block for a binary/image resource (same `vision`
    gate), else a short note. Never raises -- an unrecognised block type
    becomes an honest placeholder rather than being silently dropped."""
    blocks: list = []
    for item in (content or []):
        itype = getattr(item, "type", None)
        if itype == "text":
            blocks.append({"type": "text", "text": getattr(item, "text", "") or ""})
        elif itype == "image":
            mime = getattr(item, "mime_type", None) or "image/png"
            if vision:
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": mime, "data": getattr(item, "data", "") or "",
                }})
            else:
                blocks.append({"type": "text",
                                "text": f"[image content ({mime}) omitted -- this model has no vision support]"})
        elif itype == "resource":
            resource = getattr(item, "resource", None)
            uri = getattr(resource, "uri", None) or "?"
            text = getattr(resource, "text", None)
            blob = getattr(resource, "blob", None)
            if isinstance(text, str):
                blocks.append({"type": "text", "text": f"[resource {uri}]\n{text}"})
            elif blob:
                mime = getattr(resource, "mime_type", None) or "application/octet-stream"
                if vision and mime.startswith("image/"):
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": blob}})
                else:
                    blocks.append({"type": "text", "text": f"[embedded resource {uri} ({mime}) omitted]"})
            else:
                blocks.append({"type": "text", "text": f"[resource: {uri}]"})
        elif itype == "audio":
            blocks.append({"type": "text", "text": "[audio content omitted -- not supported in this build]"})
        else:
            blocks.append({"type": "text", "text": f"[unsupported MCP content block: {itype or 'unknown'}]"})
    return blocks


def content_with_structured_fallback(blocks: list, structured_content) -> list:
    """binary-facts/scope-B: "`structuredContent` JSON when no text" -- a
    result with zero real text blocks but a non-empty `structuredContent`
    gets ONE extra text block carrying it as pretty-printed JSON, so a
    structured-only tool result is never silently empty."""
    has_text = any(b.get("type") == "text" and b.get("text") for b in blocks)
    if has_text or not structured_content:
        return blocks
    try:
        text = json.dumps(structured_content, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(structured_content)
    return list(blocks) + [{"type": "text", "text": text}]


def cap_and_spill(blocks: list, *, meta: Optional[dict] = None,
                   session_dir=None, tool_use_id: Optional[str] = None) -> list:
    """Output cap = min(`_meta[anthropic/maxResultSizeChars]`,
    `MAX_MCP_OUTPUT_TOKENS`x4 chars) [scope B]; under the cap, `blocks` is
    returned unchanged. Over it: the FULL text is spilled to
    `<session_dir>/tool-results/<tool_use_id>.txt` (matching
    `tools/truncate.py`'s own spill location/naming, best-effort -- a
    missing `session_dir`/`tool_use_id`, e.g. inside the read-only
    concurrent-batch pool, just skips the spill, never raises), text
    blocks are cut (in order, simple head-cut -- Claude Code's own
    algorithm, not this codebase's generic head+tail sandwich) to fit
    the remaining budget after reserving `IMAGE_TOKEN_COST` tokens' worth
    of chars per image block (an image is never itself truncated), and
    Claude Code's EXACT truncation string (binary-facts sec.9), "[OUTPUT
    TRUNCATED - exceeded N token limit]", is appended -- with (finding 5
    must-do) a spill-file pointer of this codebase's own after it when a
    spill actually happened, so a truncated MCP result names where the
    full text went, same as `tools/truncate.py::spill_and_truncate`
    already does for every other tool's own cap."""
    token_limit = default_output_token_limit()
    cap_chars = token_limit * 4
    meta_cap = tool_meta_max_result_size_chars(meta)
    if meta_cap is not None:
        cap_chars = min(cap_chars, meta_cap)
    effective_token_limit = max(1, cap_chars // 4)

    num_images = sum(1 for b in blocks if b.get("type") == "image")
    image_chars_equiv = num_images * IMAGE_TOKEN_COST * 4
    text_chars = sum(len(b.get("text", "") or "") for b in blocks if b.get("type") == "text")
    if text_chars + image_chars_equiv <= cap_chars:
        return blocks

    spill_note = ""
    if session_dir is not None and tool_use_id:
        full_text = "\n".join(b.get("text", "") or "" for b in blocks if b.get("type") == "text")
        try:
            results_dir = Path(session_dir) / "tool-results"
            results_dir.mkdir(parents=True, exist_ok=True)
            spill_path = results_dir / f"{tool_use_id}.txt"
            spill_path.write_text(full_text, encoding="utf-8")
            spill_note = f" Full output saved to {spill_path}."
        except OSError:
            pass

    remaining = max(0, cap_chars - image_chars_equiv)
    new_blocks: list = []
    for b in blocks:
        if b.get("type") != "text":
            new_blocks.append(b)
            continue
        text = b.get("text", "") or ""
        if remaining <= 0:
            continue  # budget already spent -- drop this text block entirely
        if len(text) <= remaining:
            remaining -= len(text)
            new_blocks.append(b)
        else:
            new_blocks.append({**b, "text": text[:remaining]})
            remaining = 0
    new_blocks.append({"type": "text",
                        "text": f"[OUTPUT TRUNCATED - exceeded {effective_token_limit} token limit]{spill_note}"})
    return new_blocks


class McpTool(Tool):
    """One `mcp__<server>__<tool>` tool. `manager` is a duck-typed object
    exposing `.call(server, tool, arguments, timeout=None)` (McpManager's
    own signature) -- a test can hand in any object with that one method."""

    result_cap = None  # manages its own truncation+spill (cap_and_spill), like tools/read.py

    def __init__(self, server_name: str, sdk_tool, manager, *, vision: bool = False,
                 family: Optional[str] = None) -> None:
        self.server_name = server_name
        self.sdk_tool = sdk_tool  # kept for agent/catalog.py's LRU eviction (rebuild the deferred-pool entry)
        self.tool_name = getattr(sdk_tool, "name", "")
        self.name = mcp_tool_name(server_name, self.tool_name)
        self.description = (getattr(sdk_tool, "description", None) or
                             f"{self.tool_name} (from MCP server {server_name!r})")
        raw_schema = getattr(sdk_tool, "input_schema", None) or {"type": "object", "properties": {}}
        self.input_schema = sanitize_tool_schema(raw_schema, family=family)
        annotations = getattr(sdk_tool, "annotations", None)
        self.is_read_only = bool(getattr(annotations, "read_only_hint", False))
        self.is_destructive = bool(getattr(annotations, "destructive_hint", False))
        self.meta = getattr(sdk_tool, "meta", None) or {}
        self.vision = vision
        self.manager = manager

    def always_load(self) -> bool:
        return tool_always_load(self.meta)

    def summary(self, input: dict) -> str:
        try:
            body = json.dumps(input, ensure_ascii=False, default=str) if input else ""
        except (TypeError, ValueError):
            body = str(input)
        return f"{self.name}({body[:80]})"

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        """H4/finding 4/5 must-do: the call itself is abort-aware (polls
        `ctx.abort` in <=0.2s slices via McpManager.call -> McpServerHandle.
        call_tool -> McpLoop.run_abortable, never blocking the whole turn
        on a hung server), and the result is returned UNCAPPED -- capping
        (`cap_and_spill`) moves to agent/loop.py's `_finalize_tool_result`,
        which runs AFTER the (future H4) PostToolUse hook point, so that
        hook sees the model's real, full output rather than an
        already-truncated one."""
        from rolo_claude.mcp.client import McpAborted
        arguments = input if isinstance(input, dict) else {}
        try:
            result = self.manager.call(self.server_name, self.tool_name, arguments, abort=getattr(ctx, "abort", None))
        except McpAborted:
            return ToolResult(f"MCP tool {self.name!r} call interrupted.", is_error=True)
        except Exception as e:  # a hung/failed/disconnected server must never crash the loop
            return ToolResult(f"MCP tool {self.name!r} call failed: {type(e).__name__}: {e}", is_error=True)

        blocks = convert_content_blocks(getattr(result, "content", None) or [], vision=self.vision)
        blocks = content_with_structured_fallback(blocks, getattr(result, "structured_content", None))
        if not blocks:
            blocks = [{"type": "text", "text": "(no content returned)"}]
        return ToolResult(content=blocks, is_error=bool(getattr(result, "is_error", False)))


# ---- ListMcpResourcesTool / ReadMcpResourceTool (scope B built-ins) --------
# Added to the frozen registry only when a session actually has an
# McpManager (headless.py) -- offering them with nothing to list/read would
# be a false capability promise (finding 14's own rule, applied here too).
# Both read `ctx.mcp_manager` (set alongside `ctx.catalog` in agent/loop.py).

class ListMcpResourcesTool(Tool):
    name = "ListMcpResourcesTool"
    description = ("List resources available from this session's connected MCP servers. "
                    "Optionally filter to one server by name.")
    is_read_only = True
    input_schema = {
        "type": "object",
        "properties": {"server": {"type": "string", "description": "Limit the listing to one server name"}},
    }

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        manager = getattr(ctx, "mcp_manager", None)
        if manager is None:
            return ToolResult("No MCP servers are connected in this session.", is_error=True)
        server_filter = (input or {}).get("server") if isinstance(input, dict) else None
        try:
            pairs = manager.resources()
        except Exception as e:
            return ToolResult(f"Failed to list resources: {type(e).__name__}: {e}", is_error=True)
        items = []
        for server, resource in pairs:
            if server_filter and server != server_filter:
                continue
            items.append({
                "server": server, "uri": getattr(resource, "uri", None),
                "name": getattr(resource, "name", None), "description": getattr(resource, "description", None),
            })
        if not items:
            suffix = f" from server {server_filter!r}" if server_filter else ""
            return ToolResult(f"No resources available{suffix}.")
        return ToolResult(json.dumps(items, indent=2, default=str))


class ReadMcpResourceTool(Tool):
    name = "ReadMcpResourceTool"
    description = "Read one MCP resource, given its server name and URI (see ListMcpResourcesTool)."
    is_read_only = True
    result_cap = None  # spills through the same cap_and_spill path as an ordinary MCP tool result
    input_schema = {
        "type": "object",
        "properties": {"server": {"type": "string"}, "uri": {"type": "string"}},
        "required": ["server", "uri"],
    }

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        manager = getattr(ctx, "mcp_manager", None)
        if manager is None:
            return ToolResult("No MCP servers are connected in this session.", is_error=True)
        data = input if isinstance(input, dict) else {}
        server, uri = data.get("server"), data.get("uri")
        if not server or not uri:
            return ToolResult("Both 'server' and 'uri' are required.", is_error=True)
        try:
            result = manager.read_resource(server, uri)
        except Exception as e:
            return ToolResult(f"Failed to read resource {uri!r} from {server!r}: {type(e).__name__}: {e}", is_error=True)
        parts = []
        for c in getattr(result, "contents", None) or []:
            text = getattr(c, "text", None)
            if isinstance(text, str):
                parts.append(text)
            else:
                blob = getattr(c, "blob", None)
                parts.append(f"[binary resource content, {len(blob) if blob else 0} base64 chars]")
        text = "\n".join(parts) if parts else "(empty resource)"
        capped_blocks = cap_and_spill(
            [{"type": "text", "text": text}], session_dir=getattr(ctx, "session_dir", None),
            tool_use_id=getattr(ctx, "tool_use_id", None),
        )
        return ToolResult("".join(b.get("text", "") for b in capped_blocks))
