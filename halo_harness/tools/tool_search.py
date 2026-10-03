"""halo_harness.tools.tool_search -- the ToolSearch tool (H2 scope A, H3
scope C). Two modes, picked per-call from `ctx.catalog`:

- No `ctx.catalog` (every H2-era call site, and any session with zero MCP
  servers): searches `ctx.registry` alone, unchanged from H2 -- every tool
  is already fully loaded, so this is just a lookup.
- `ctx.catalog` set (`agent.catalog.SessionCatalog`, H3): searches BOTH the
  live registry and the still-deferred MCP pool; a match found in the
  deferred pool is LOADED (appended to the session's growing wire catalog,
  never reordered -- see agent/catalog.py) as a side effect of this very
  call, and the result carries a `tool_reference` block per newly-loaded
  name alongside the full JSON schema, so the model sees the schema THIS
  turn even though the wire catalog only grows for the NEXT request.
"""

from __future__ import annotations

import json

from halo_harness.tools.base import Tool, ToolContext, ToolResult

# finding 13 must-do: Claude Code's own deferred-tool wording (this
# build's actual, current help text -- the old copy's "This build has no
# deferred MCP tools yet" placeholder was already false the moment ANY
# MCP server was configured, and a model that took it literally never
# tried ToolSearch for a real deferred name).
DESCRIPTION = (
    "Fetches full schema definitions for deferred tools so they can be called. Deferred tools are "
    "real, usable tools (mostly from MCP servers) that are not loaded into this conversation yet to "
    "save context -- their names may already appear in this tool's own listings without a schema. "
    "Call this before using one of those names directly, or to search by keyword when the exact name "
    "isn't known.\n\n"
    "Query forms:\n"
    "- \"select:<name>[,<name>...]\" -- load these exact tools by name\n"
    "- \"read files\" -- keyword search, ranked by relevance, up to max_results best matches"
)


class ToolSearchTool(Tool):
    name = "ToolSearch"
    description = DESCRIPTION
    is_read_only = True
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "\"select:<name>[,<name>...]\" for exact names, or free-text keywords"},
            "max_results": {"type": "integer", "description": "Maximum number of results to return (default 5)"},
        },
        "required": ["query"],
    }

    def summary(self, input: dict) -> str:
        return f"ToolSearch({input.get('query', '')})"

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        query = input.get("query") if isinstance(input, dict) else None
        if not query or not isinstance(query, str):
            return ToolResult("The query parameter is required", is_error=True)
        registry = getattr(ctx, "registry", None)
        if registry is None:
            return ToolResult("No tool registry is available to search in this context.", is_error=True)

        max_results = input.get("max_results") if isinstance(input, dict) else None
        max_results = max_results if isinstance(max_results, int) and max_results > 0 else 5

        catalog = getattr(ctx, "catalog", None)
        if catalog is not None:
            # Linux/H4 must-do: connect any still-pending `mcpLazy` server
            # NOW, on ToolSearch's own first (and every subsequent, cheaply
            # no-op) call -- otherwise a lazy server's tools can never
            # enter `catalog.deferred` at all (see
            # `SessionCatalog.ensure_lazy_discovered`'s own docstring).
            ensure_lazy = getattr(catalog, "ensure_lazy_discovered", None)
            if ensure_lazy is not None:
                # H9 must-do: thread ctx.abort through so Esc/Ctrl+C cuts
                # the (now-parallel, still up-to-MCP_TIMEOUT) lazy-start
                # wait short instead of blocking ToolSearch's first call.
                ensure_lazy(abort=getattr(ctx, "abort", None))
            # W5b ("connector cold start, properly"): the claude.ai
            # connectors bridge's own on-first-hit discovery -- gated
            # INSIDE the method itself on the query actually mentioning a
            # connector, so an ordinary keyword/select call never pays for
            # a discovery round a cold cache would otherwise never run
            # (print mode's own "never block by default" rule).
            ensure_connectors = getattr(catalog, "ensure_connectors_discovered_for_query", None)
            if ensure_connectors is not None:
                ensure_connectors(query)
            results, deferred_matched = catalog.search(query, max_results)
        else:
            results, deferred_matched = self._search_registry_only(registry, query, max_results)

        if query.startswith("select:"):
            names = [n.strip() for n in query[len("select:"):].split(",") if n.strip()]
            found_names = {d.get("name") for d in results}
            missing = [n for n in names if n not in found_names]
        else:
            missing = []
            if not results:
                return ToolResult(f"No tools matched {query!r}. Available tools: {', '.join(registry.names())}")

        loaded = catalog.load(deferred_matched) if (catalog is not None and deferred_matched) else []
        # finding 1 must-do: a recognised, deferred name `load()` could NOT
        # fit (catalog at cap, nothing loaded-deferred left to evict) --
        # its schema is still shown above (found via `catalog.search`), but
        # it must never get a tool_reference block promising it's usable
        # NEXT turn when it isn't.
        refused = list(getattr(catalog, "last_refused", []) or []) if catalog is not None else []
        body = json.dumps(results, indent=2)
        if missing:
            body += f"\n\n(not found: {', '.join(missing)})"
        if refused:
            body += (f"\n\n(could not load right now -- the tool catalog is full and nothing could be "
                      f"freed to make room: {', '.join(refused)}. Try again after this turn, or call a "
                      f"tool that's already loaded instead.)")
        if not loaded:
            return ToolResult(body)

        # H3 scope C: a deferred tool actually got loaded this call -- the
        # NEXT request already carries its full definition (agent/loop.py's
        # Session logs a new `meta` node via `catalog.on_grow`); this
        # call's OWN result also gets a `tool_reference` block per loaded
        # name (plan D5: "returns defs + tool_reference blocks"), on top of
        # the plain JSON schema text every caller already gets.
        blocks = [{"type": "text", "text": body}]
        blocks.extend({"type": "tool_reference", "tool_name": name} for name in loaded)
        return ToolResult(blocks)

    @staticmethod
    def _search_registry_only(registry, query: str, max_results: int) -> "tuple[list, list]":
        """H2's original behaviour, unchanged: every tool is already fully
        loaded, so this is a pure lookup with nothing ever deferred."""
        defs = registry.definitions()
        if query.startswith("select:"):
            names = [n.strip() for n in query[len("select:"):].split(",") if n.strip()]
            return [d for d in defs if d.get("name") in names], []

        terms = [t.lower() for t in query.split() if t]
        scored = []
        for d in defs:
            haystack = f"{d.get('name', '')} {d.get('description', '')}".lower()
            score = sum(haystack.count(t) for t in terms)
            if score > 0:
                scored.append((score, d.get("name", ""), d))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [d for _, _, d in scored[:max_results]], []
