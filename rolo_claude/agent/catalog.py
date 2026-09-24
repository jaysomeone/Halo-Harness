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

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from rolo_claude.tools.registry import ToolRegistry

DEFAULT_DATABRICKS_CAP = 32
DEFAULT_OPENROUTER_CAP = 128
DEFERRED_LRU_MAX = 100  # D5: "LRU of 100 loaded deferred tools"


def host_cap(provider: str) -> int:
    return DEFAULT_DATABRICKS_CAP if provider == "databricks" else DEFAULT_OPENROUTER_CAP


def select_preload(mcp_triples, *, preload_names=None, always_load_servers=None, cap_budget: int) -> "tuple[list, dict]":
    """`mcp_triples` = `McpManager.all_tools()`'s own `[(server, wire_name,
    sdk_tool), ...]` (ALREADY filtered so a denied server's tools never
    appear at all). Returns `(preload, deferred)`: `preload` is
    `[(server, wire_name, sdk_tool), ...]`, at most `cap_budget` long,
    ready to become McpTool instances in the frozen registry; `deferred` is
    `{wire_name: (server, sdk_tool)}` for EVERYTHING ELSE.

    finding 1: preload is ONLY `_meta[anthropic/alwaysLoad]` tools, THEN
    `mcpPreload`-listed names -- never anything beyond those two groups,
    even when `cap_budget` has room left over. The OLD behaviour filled
    remaining budget with alphabetically-next tools ("rest"), which at
    rolo's real ~213-tool scale put the frozen catalog EXACTLY at cap with
    zero headroom: the instant ToolSearch loaded one more deferred tool,
    `SessionCatalog._evict_if_needed` had nothing to evict but the tool
    just loaded (the only entry ever added to `_loaded_order`), so it
    evicted that -- the tool ToolSearch just told the model was loaded.
    `cap_budget <= 0` preloads nothing (valid when the built-in tools
    alone already fill the provider's cap); an always/requested tool that
    doesn't fit `cap_budget` is deferred too (never silently dropped)."""
    from rolo_claude.tools.mcp_tool import tool_always_load

    preload_names = set(preload_names or ())
    always_load_servers = set(always_load_servers or ())
    always, requested, rest = [], [], []
    for server, wire_name, sdk_tool in mcp_triples:
        # finding 13 must-do: a SERVER-level `alwaysLoad` (McpServerConfig.
        # always_load, parsed from a config entry's own "alwaysLoad" key --
        # distinct from a TOOL's `_meta[anthropic/alwaysLoad]`) preloads
        # every one of that server's tools, same as if each had the
        # per-tool meta set individually.
        if tool_always_load(getattr(sdk_tool, "meta", None)) or server in always_load_servers:
            always.append((server, wire_name, sdk_tool))
        elif wire_name in preload_names:
            requested.append((server, wire_name, sdk_tool))
        else:
            rest.append((server, wire_name, sdk_tool))

    candidates = sorted(always, key=lambda t: t[1]) + sorted(requested, key=lambda t: t[1])
    budget = max(0, cap_budget)
    preload = candidates[:budget]
    deferred = {wire_name: (server, sdk_tool)
                for server, wire_name, sdk_tool in (candidates[budget:] + rest)}
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
    # finding 1: ToolSearch is a read-only tool, so two calls in one turn
    # can run CONCURRENTLY on tools/registry.py's read-only pool -- both
    # mutating `.names`/`.deferred`/`_loaded_order` without this lock would
    # race (a lost update, or a name touched twice by two overlapping
    # evictions).
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    # names `load()` COULD NOT fit last call (recognised, in `.deferred`,
    # but every catalog slot is a frozen/preloaded tool with nothing
    # loaded-deferred left to evict) -- ToolSearchTool reads this right
    # after calling `load()` to report a clear error instead of a false
    # tool_reference promise.
    last_refused: list = field(default_factory=list)

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

        from rolo_claude.tools.mcp_tool import tool_search_hint
        # finding 13 must-do: `_meta[anthropic/searchHint]` -- only the
        # DEFERRED pool carries raw sdk_tool meta at all (an already-loaded
        # McpTool's `.definition()` is just name/description/input_schema,
        # same as any built-in); weighted 3x a plain description word so a
        # server's own curated search terms actually move the ranking.
        hint_by_name = {n: tool_search_hint(getattr(sdk_tool, "meta", None))
                         for n, (_server, sdk_tool) in self.deferred.items()}
        terms = [t.lower() for t in query.split() if t]
        scored = []
        for d in all_defs:
            name = d.get("name", "")
            haystack = f"{name} {d.get('description', '')}".lower()
            hint = hint_by_name.get(name)
            if hint:
                haystack += f" {((hint + ' ') * 3).lower()}"
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
        the live catalog. Returns the names actually recognised AND
        SUCCESSFULLY placed (loaded now, or already present); an unknown
        name is silently skipped (the caller -- ToolSearchTool -- already
        reports "not found" for those from the plain registry/deferred
        search results). finding 1: locked (two ToolSearch calls in one
        turn run concurrently on the read-only pool) and evicts BEFORE
        appending -- never the tool just being loaded (see `_evict_one`).
        A name that's recognised but can't be fit (cap already full of
        frozen/preloaded tools, nothing loaded-deferred left to evict) is
        left OUT of the returned list and recorded in `self.last_refused`
        instead, rather than silently exceeding `self.cap` or evicting the
        very tool this call just promised the model."""
        from rolo_claude.tools.mcp_tool import McpTool

        with self._lock:
            touched: list = []
            refused: list = []
            newly_loaded = False
            for name in names:
                if name in self.names:
                    if name in self._loaded_order:
                        self._loaded_order.remove(name)
                        self._loaded_order.append(name)
                    touched.append(name)
                    continue
                entry = self.deferred.get(name)
                if entry is None:
                    continue
                if len(self.names) >= self.cap and not self._evict_one():
                    refused.append(name)
                    continue
                self.deferred.pop(name, None)
                server, sdk_tool = entry
                tool = McpTool(server, sdk_tool, self.manager, vision=self.vision)
                self.registry.add_tool(tool)
                self.names.append(name)
                self._loaded_order.append(name)
                touched.append(name)
                newly_loaded = True

            while len(self._loaded_order) > DEFERRED_LRU_MAX:
                self._evict_one()

            self.last_refused = refused
            if touched and newly_loaded and self.on_grow is not None:
                self.on_grow(list(self.names))
            return touched

    def _evict_one(self) -> bool:
        """LRU-evict the OLDEST loaded-deferred tool (never a tool present
        since the initial freeze -- those are never in `_loaded_order` --
        and, called from `load()` BEFORE the new tool is appended to
        `_loaded_order`, never the tool currently being loaded either).
        Returns False (nothing evictable) when `_loaded_order` is empty --
        the caller must then refuse the new load rather than exceed
        `self.cap`. An evicted tool goes back into `self.deferred` (via
        its own retained `sdk_tool`) so ToolSearch can reload it later --
        eviction means "not currently loaded", never "gone forever"."""
        if not self._loaded_order:
            return False
        victim_name = self._loaded_order.pop(0)
        tool = self.registry.get(victim_name)
        self.registry.remove_tool(victim_name)
        if victim_name in self.names:
            self.names.remove(victim_name)
        if tool is not None and hasattr(tool, "sdk_tool"):
            self.deferred[victim_name] = (tool.server_name, tool.sdk_tool)
        return True

    def refresh_deferred_for_server(self, server_name: str) -> None:
        """U2 must-do: a reconnected server's (possibly changed) tool list
        never enters the deferred pool otherwise, since the pool is only
        ever built once, from `McpManager.all_tools()`, at session start.
        Call this right after `McpManager.reconnect(server_name)`
        succeeds (the `/mcp reconnect` command's own job -- this is just
        the mechanism). Already-LOADED tools for this server are left
        alone (still real, still dispatchable, still in `self.names`)
        even if the fresh tool list no longer contains that exact tool --
        only the DEFERRED (not-yet-loaded) entries for this server are
        replaced."""
        with self._lock:
            for name in [n for n, (srv, _) in self.deferred.items() if srv == server_name]:
                del self.deferred[name]
            for server, wire_name, sdk_tool in self.manager.all_tools():
                if server != server_name or wire_name in self.names:
                    continue
                self.deferred[wire_name] = (server, sdk_tool)
