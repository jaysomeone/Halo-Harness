"""rolo_claude.tools.tool_search -- the ToolSearch tool (H2 scope A): a
deferred-tool loader over the FROZEN catalog. Real MCP tools (which can
genuinely be deferred/unloaded) arrive in H3, so for now this searches the
same built-in registry every session already has fully loaded -- the tool
exists and behaves correctly now so a model habituated to calling it (or a
session that grows deferred MCP tools later) already has a working
`ToolSearch`, without this milestone needing to invent MCP deferral first.
"""

from __future__ import annotations

import json

from rolo_claude.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = (
    "Fetches full schema definitions for deferred tools so they can be called. This build has no "
    "deferred MCP tools yet (every built-in tool is already fully loaded), so this simply looks "
    "tools up by exact name or keyword over the registry -- once MCP tools exist, unloaded ones "
    "will show up here too.\n\n"
    "Query forms:\n"
    "- \"select:Read,Edit,Grep\" -- fetch these exact tools by name\n"
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
        defs = registry.definitions()

        if query.startswith("select:"):
            names = [n.strip() for n in query[len("select:"):].split(",") if n.strip()]
            found = [d for d in defs if d.get("name") in names]
            found_names = {d.get("name") for d in found}
            missing = [n for n in names if n not in found_names]
            body = json.dumps(found, indent=2)
            if missing:
                body += f"\n\n(not found: {', '.join(missing)})"
            return ToolResult(body)

        terms = [t.lower() for t in query.split() if t]
        scored = []
        for d in defs:
            haystack = f"{d.get('name', '')} {d.get('description', '')}".lower()
            score = sum(haystack.count(t) for t in terms)
            if score > 0:
                scored.append((score, d.get("name", ""), d))
        scored.sort(key=lambda t: (-t[0], t[1]))
        results = [d for _, _, d in scored[:max_results]]
        if not results:
            return ToolResult(f"No tools matched {query!r}. Available tools: {', '.join(registry.names())}")
        return ToolResult(json.dumps(results, indent=2))
