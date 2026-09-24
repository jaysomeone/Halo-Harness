"""rolo_claude.agent.catalog -- SessionCatalog (H3 scope C, plan revision
4): the frozen tool catalog + lazy load over deferred MCP tools.

Freezing happens ONCE, before a session's first model call: built-ins +
MCP tools (already filtered by server-scoped permission deny rules) split
into "preload" (an MCP tool's own `_meta[anthropic/alwaysLoad]`, or a wire
name listed in `~/.rolo-claude/config.json`'s `mcpPreload`) vs "deferred"
(everything else, reachable only through ToolSearch), respecting the
provider's tools_max (32 Databricks / 128 OpenRouter --
providers/profiles.py). Growth from a ToolSearch load is APPEND-ONLY to an
explicit ordered name list independent of `ToolRegistry.definitions()`'s
own (always-alphabetical) ordering -- that list stays sorted for the
INITIAL freeze and for "what's loadable" listings, but is never consulted
again for WIRE-catalog order once a session exists; `SessionCatalog.names`
is what `agent/loop.py`'s `Session` logs as each new `meta` node's `tools`,
so `derive_request` reconstructs the SAME growing-but-never-reordered list
every replay (finding 4: "adding a tool is an accepted one-time cache
miss, never reorder").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from rolo_claude.tools.registry import ToolRegistry

DEFAULT_DATABRICKS_CAP = 32
DEFAULT_OPENROUTER_CAP = 128
DEFERRED_LRU_MAX = 100  # D5: "LRU of 100 loaded deferred tools"


def host_cap(provider: str) -> int:
    return DEFAULT_DATABRICKS_CAP if provider == "databricks" else DEFAULT_OPENROUTER_CAP


def select_preload(mcp_triples, *, preload_names=None, cap_budget: int) -> "tuple[list, dict]":
    """`mcp_triples` = `McpManager.all_tools()`'s own `[(server, wire_name,
    sdk_tool), ...]` (ALREADY filtered so a denied server's tools never
    appear at all). Returns `(preload, deferred)`: `preload` is
    `[(server, wire_name, sdk_tool), ...]`, at most `cap_budget` long,
    ready to become McpTool instances in the frozen registry; `deferred` is
    `{wire_name: (server, sdk_tool)}` for the rest. Every `_meta[anthropic/
    alwaysLoad]` tool preloads first, then every `mcpPreload`-listed name,
    THEN as many more (candidate order) as still fit -- `cap_budget <= 0`
    preloads nothing (everything becomes deferred, valid when the built-in
    tools alone already fill the provider's cap)."""
    from rolo_claude.tools.mcp_tool import tool_always_load

    preload_names = set(preload_names or ())
    always, requested, rest = [], [], []
    for server, wire_name, sdk_tool in mcp_triples:
        if tool_always_load(getattr(sdk_tool, "meta", None)):
            always.append((server, wire_name, sdk_tool))
        elif wire_name in preload_names:
            requested.append((server, wire_name, sdk_tool))
        else:
            rest.append((server, wire_name, sdk_tool))

    ordered = sorted(always, key=lambda t: t[1]) + sorted(requested, key=lambda t: t[1]) + rest
    budget = max(0, cap_budget)
    preload = ordered[:budget]
    deferred = {wire_name: (server, sdk_tool) for server, wire_name, sdk_tool in ordered[budget:]}
    return preload, deferred


@dataclass
class SessionCatalog:
    """Owns the session's growing, append-only wire-catalog NAME LIST plus
    the deferred MCP tool pool ToolSearch draws from. `registry` is
    mutated in place as tools load/evict (`ToolRegistry.add_tool`/
    `remove_tool`); `on_grow(names)` -- when set -- is called with the
    FULL updated ordered name list every time `load()` actually changes
    it. `agent/loop.py`'s `Session` wires `on_grow` to append a new `meta`
    log node so the growth is logged (model-visible means logged)."""

    registry: ToolRegistry
    deferred: dict                                      # wire_name -> (server, sdk_tool)
    manager: object                                      # McpManager, or a test double with the same .call()
    cap: int
    vision: bool = False
    names: list = field(default_factory=list)             # the ordered, append-only wire catalog
    on_grow: Optional[Callable[[list], None]] = None
    _loaded_order: list = field(default_factory=list)      # deferred-loaded names, oldest-first (LRU)

    def _deferred_definition(self, wire_name: str) -> dict:
        server, sdk_tool = self.deferred[wire_name]
        return {
            "name": wire_name,
            "description": getattr(sdk_tool, "description", None) or "",
            "input_schema": getattr(sdk_tool, "input_schema", None) or {"type": "object", "properties": {}},
        }

    def search(self, query: str, max_results: int = 5) -> "tuple[list, list]":
        """`(defs, deferred_names_among_them)` -- `defs` covers BOTH
        already-loaded tools (from `registry`, which also still contains
        every built-in) and not-yet-loaded deferred ones (a definition
        built fresh from the raw SDK tool, without loading it);
        `deferred_names_among_them` is the subset still in `self.deferred`
        -- what a `select:` caller should hand to `load()`."""
        registry_defs = self.registry.definitions()
        deferred_defs = [self._deferred_definition(n) for n in sorted(self.deferred)]
        all_defs = registry_defs + deferred_defs

        if query.startswith("select:"):
            wanted = [n.strip() for n in query[len("select:"):].split(",") if n.strip()]
            found = [d for d in all_defs if d.get("name") in wanted]
            matched_deferred = [n for n in wanted if n in self.deferred]
            return found, matched_deferred

        terms = [t.lower() for t in query.split() if t]
        scored = []
        for d in all_defs:
            haystack = f"{d.get('name', '')} {d.get('description', '')}".lower()
            score = sum(haystack.count(t) for t in terms)
            if score > 0:
                scored.append((score, d.get("name", ""), d))
        scored.sort(key=lambda t: (-t[0], t[1]))
        results = [d for _, _, d in scored[:max(1, max_results)]]
        matched_deferred = [d.get("name") for d in results if d.get("name") in self.deferred]
        return results, matched_deferred

    def load(self, names: "list[str]") -> "list[str]":
        """Load `names` (any subset of `self.deferred`, plus already-loaded
        names -- a no-op re-select just refreshes their LRU position) into
        the live catalog. Returns the names actually recognised (loaded
        now or already present); an unknown name is silently skipped (the
        caller -- ToolSearchTool -- already reports "not found" for those
        from the plain registry/deferred search results)."""
        from rolo_claude.tools.mcp_tool import McpTool

        touched: list = []
        newly_loaded = False
        for name in names:
            if name in self.names:
                if name in self._loaded_order:
                    self._loaded_order.remove(name)
                    self._loaded_order.append(name)
                touched.append(name)
                continue
            entry = self.deferred.pop(name, None)
            if entry is None:
                continue
            server, sdk_tool = entry
            tool = McpTool(server, sdk_tool, self.manager, vision=self.vision)
            self.registry.add_tool(tool)
            self.names.append(name)
            self._loaded_order.append(name)
            touched.append(name)
            newly_loaded = True

        if newly_loaded:
            self._evict_if_needed()
        if touched and newly_loaded and self.on_grow is not None:
            self.on_grow(list(self.names))
        return touched

    def _evict_if_needed(self) -> None:
        """LRU-evict the OLDEST loaded-deferred tool (never a tool present
        since the initial freeze -- those are never in `_loaded_order`)
        while the deferred-loaded count exceeds `DEFERRED_LRU_MAX` or the
        WHOLE catalog exceeds `self.cap` (D5 / the Databricks 32-cap
        must-do). An evicted tool goes back into `self.deferred` (via its
        own retained `sdk_tool`) so ToolSearch can reload it later --
        eviction means "not currently loaded", never "gone forever"."""
        while self._loaded_order and (len(self._loaded_order) > DEFERRED_LRU_MAX or len(self.names) > self.cap):
            victim_name = self._loaded_order.pop(0)
            tool = self.registry.get(victim_name)
            self.registry.remove_tool(victim_name)
            if victim_name in self.names:
                self.names.remove(victim_name)
            if tool is not None and hasattr(tool, "sdk_tool"):
                self.deferred[victim_name] = (tool.server_name, tool.sdk_tool)
