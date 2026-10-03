"""halo_harness.agent.catalog -- SessionCatalog (H3 scope C, plan revision
4): the frozen tool catalog + lazy load over deferred MCP tools.

Freezing happens ONCE, before a session's first model call: built-ins +
MCP tools (already filtered by server-scoped permission deny rules) split
into "preload" (an MCP tool's own `_meta[anthropic/alwaysLoad]`, or a wire
name listed in `~/.halo/config.json`'s `mcpPreload`) vs "deferred"
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

import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from halo_harness.tools.registry import ToolRegistry

DEFAULT_DATABRICKS_CAP = 32
DEFAULT_OPENROUTER_CAP = 128
DEFERRED_LRU_MAX = 100  # D5: "LRU of 100 loaded deferred tools"

# ---- finding 12: ToolSearch keyword-search ranking -------------------------
# Old behaviour: `haystack.count(t)` over "name description" -- a raw
# substring COUNT, so a long, wordy description that happens to repeat a
# query term many times could outrank a short, precisely-named tool that
# mentions it once. Verified repro: on 117 real tool definitions, "list
# MIDI ports" ranked mcp__hardware__hw_ports 14th (score 3) behind
# daw__synth_set at 20 (19 "midi" hits in a 2,510-character
# description), so the default 5-result cutoff never contained it.
_TOKEN_RE = re.compile(r"[a-z0-9]+")

_NAME_TOKEN_WEIGHT = 10.0   # an exact whole-token hit in the tool's own name
_NAME_SUBSTR_WEIGHT = 4.0   # term appears inside the name string, not as a whole token
_HINT_WEIGHT = 6.0          # term present in the server's own _meta[anthropic/searchHint]
_DESC_BM25_K = 25.0         # BM25-ish k1 analogue, tuned to this catalog's short 1-2 sentence descriptions


def _tokens(text: Optional[str]) -> list:
    return _TOKEN_RE.findall((text or "").lower())


def _term_score(term: str, *, name_tokens: set, name_lower: str,
                 desc_tokens_set: set, desc_len_words: int, hint_tokens: set) -> float:
    """One query TERM's contribution to one tool's ranking score -- PER-
    TERM PRESENCE only (the caller iterates each unique query term exactly
    once: see `search()` below), name-token hits weighted far above a
    plain description hit, a `searchHint` hit weighted between the two
    (finding 13 must-do: "given MORE weight than the tool's own name/
    description" -- kept just below a NAME hit, since an exact name-token
    match naming the tool outright is still the single strongest possible
    signal), and a BM25-style `k / (k + len)` length normalisation on the
    description contribution alone -- a term buried in a huge block of
    prose counts for less than the SAME term appearing in a short, precise
    one, closing the exact regression above (19 incidental "midi" hits in
    a long description can no longer outrank hw_ports' own short, on-topic
    one)."""
    score = 0.0
    if term in name_tokens:
        score += _NAME_TOKEN_WEIGHT
    elif term in name_lower:
        score += _NAME_SUBSTR_WEIGHT
    if term in hint_tokens:
        score += _HINT_WEIGHT
    if term in desc_tokens_set:
        score += _DESC_BM25_K / (_DESC_BM25_K + desc_len_words)
    return score


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
    the owner's real ~213-tool scale put the frozen catalog EXACTLY at cap with
    zero headroom: the instant ToolSearch loaded one more deferred tool,
    `SessionCatalog._evict_if_needed` had nothing to evict but the tool
    just loaded (the only entry ever added to `_loaded_order`), so it
    evicted that -- the tool ToolSearch just told the model was loaded.
    `cap_budget <= 0` preloads nothing (valid when the built-in tools
    alone already fill the provider's cap); an always/requested tool that
    doesn't fit `cap_budget` is deferred too (never silently dropped)."""
    from halo_harness.tools.mcp_tool import tool_always_load

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
    # W4 unbuilt surfaces (MCP): same freeze-once plumbing as `vision` --
    # threaded into every McpTool `load()` constructs so a deferred-loaded
    # tool's audio content gets passed through exactly like a preloaded
    # one would (headless.py's own McpTool construction already does).
    audio: bool = False
    # OpenCode-H9 MCP-compatibility item: `providers.profiles.model_family()`'s
    # coarse family string, threaded into every McpTool `load()` constructs
    # so its input_schema gets the same per-family sanitising a PRELOADED
    # McpTool gets at session-build time (headless.py/tui/bootstrap.py) --
    # same freeze-once/no-re-sanitise-on-`/model`-switch limitation as
    # `vision` above (H5's `set_model` territory).
    family: Optional[str] = None
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
    # review finding 30: the same --tools subset / bare-deny-rule names
    # `headless.build_session`'s own warm-cache connector loop filters by
    # at session start (see that function's own `tools_subset`/`bare_
    # denied_names` locals) -- `add_connector_tools` reads these so a
    # connector discovered/re-discovered LATER (the TUI's background
    # on_done, or a mid-session ToolSearch hit) is filtered identically,
    # never becoming loadable again just because it was excluded before
    # discovery had even finished. `None`/empty (the default) filters
    # nothing, matching every caller that never passes these through.
    tools_subset: "Optional[set]" = None
    bare_denied_names: set = field(default_factory=set)

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

        from halo_harness.tools.mcp_tool import tool_search_hint
        # finding 13 must-do: `_meta[anthropic/searchHint]` -- only the
        # DEFERRED pool carries raw sdk_tool meta at all (an already-loaded
        # McpTool's `.definition()` is just name/description/input_schema,
        # same as any built-in).
        hint_by_name = {n: tool_search_hint(getattr(sdk_tool, "meta", None))
                         for n, (_server, sdk_tool) in self.deferred.items()}
        # finding 12: PER-TERM presence -- the unique SET of query terms,
        # never a raw substring count over the whole query string (a
        # repeated query word must not double-count).
        terms = set(t.lower() for t in query.split() if t)
        if not terms:
            return [], []
        scored = []
        for d in all_defs:
            name = d.get("name", "")
            description = d.get("description", "") or ""
            hint = hint_by_name.get(name)
            name_tokens = set(_tokens(name))
            name_lower = name.lower()
            desc_word_list = _tokens(description)
            desc_tokens_set = set(desc_word_list)
            desc_len_words = max(1, len(desc_word_list))
            hint_tokens = set(_tokens(hint)) if hint else set()
            score = sum(_term_score(t, name_tokens=name_tokens, name_lower=name_lower,
                                     desc_tokens_set=desc_tokens_set, desc_len_words=desc_len_words,
                                     hint_tokens=hint_tokens)
                        for t in terms)
            if score > 0:
                scored.append((score, name, d))
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
        from halo_harness.tools.mcp_tool import McpTool

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
                # W4b connectors bridge: a `server is None` entry is already
                # a built Tool instance (ConnectorTool -- see tools/
                # connector_tool.py), not an MCP sdk_tool to wrap; used
                # as-is so this one deferred/LRU/ToolSearch mechanism serves
                # both kinds of tool without a second abstraction layer.
                tool = sdk_tool if server is None else \
                    McpTool(server, sdk_tool, self.manager, vision=self.vision, audio=self.audio, family=self.family)
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

    def refresh_if_stale(self, server_name: str) -> Optional[str]:
        """H13 Part A ("stale cache + changed tools on connect -> refresh
        the catalog entries and emit a notification"): called right after a
        tool call against `server_name` completes (`tools/mcp_tool.py`'s
        `McpTool.run()`) -- a no-op returning `None` unless `self.manager`
        just detected (via `McpManager.ensure_started`/`reconnect`) that a
        `cached` handle's real, post-connect `tools/list` differs from what
        the cache had promised when this session's catalog was frozen. When
        it did: reuses the EXISTING reconnect-time refresh mechanism
        (`refresh_deferred_for_server`) so any tool this server added/
        removed/changed enters/leaves `self.deferred` correctly, clears the
        manager's own one-shot stale flag (so the same drift is never
        reported twice), and returns a short human-readable note for the
        caller to surface to the model -- never raises."""
        was_stale = getattr(self.manager, "was_cache_stale", lambda _n: False)(server_name)
        if not was_stale:
            return None
        self.refresh_deferred_for_server(server_name)
        clear = getattr(self.manager, "clear_cache_stale", None)
        if callable(clear):
            clear(server_name)
        return (f"(mcp: {server_name!r}'s tool list changed since it was last cached -- "
                f"the catalog has been refreshed.)")

    def ensure_lazy_discovered(self, abort=None) -> None:
        """Linux/H4 must-do: "`mcpLazy` servers must connect on first
        ToolSearch hit so their tools are discoverable" -- a lazy server
        is never started at `start_all()` time, so it never appears in
        `McpManager.all_tools()` (connected-only), so its tools never
        entered `self.deferred` in the first place (built once, at
        session start) -- permanently undiscoverable through ToolSearch,
        by keyword OR by name, no matter how long the session runs.
        `ToolSearchTool.run()` calls this at the top of EVERY call (cheap:
        `McpManager.ensure_lazy_started_all()` is a no-op once every lazy
        server has already been started) so a lazy server's tools become
        searchable/loadable starting with the model's very next
        ToolSearch call -- still never PRELOADED, only discoverable.
        H9 must-do: `abort` (pass `ctx.abort` from the tool call) threads
        through to `ensure_lazy_started_all`, which now starts every
        pending lazy server IN PARALLEL (one shared MCP_TIMEOUT window,
        not one per server) and honours the abort instead of blocking the
        whole wait."""
        started = self.manager.ensure_lazy_started_all(abort=abort)
        if not started:
            return
        with self._lock:
            for server, wire_name, sdk_tool in self.manager.all_tools():
                if server in started and wire_name not in self.names and wire_name not in self.deferred:
                    self.deferred[wire_name] = (server, sdk_tool)

    def add_connector_tools(self, infos: "list") -> "list[str]":
        """W5b ("connector cold start, properly"): the claude.ai-connectors
        sibling of `ensure_lazy_discovered` above -- called from whatever
        just finished a REAL discovery round (the TUI's background
        `on_done`, `tui/bootstrap.py`; a mid-session `ToolSearch` hit, see
        `ensure_connectors_discovered_for_query` below) to add each newly-
        found, enabled connector straight into the deferred pool, same
        `(None, ConnectorTool(info))` shape `headless.build_session` uses
        for a warm cache at session start (`agent.catalog.SessionCatalog.
        load()` already special-cases `server is None` to mean "already a
        built Tool instance"). Skips one already loaded/deferred or
        disabled via `connectors.<slug>.enabled`. Returns the wire names
        actually added -- the TUI's `on_done` uses the length for its own
        "N claude.ai connectors available" transcript line."""
        from halo_harness.mcp import connectors_bridge
        from halo_harness.tools.connector_tool import ConnectorTool
        added: list = []
        with self._lock:
            for info in infos:
                if not connectors_bridge.connector_enabled(info.slug):
                    continue
                wire_name = f"connector__{info.slug}"
                if wire_name in self.names or wire_name in self.deferred:
                    continue
                # review finding 30: this skipped the --tools/bare-deny
                # filters `headless.build_session`'s own, otherwise
                # identical, warm-cache loop already applies -- a
                # connector excluded by `--tools`/a bare deny rule at
                # session start became loadable again the moment it was
                # (re)discovered mid-session (the TUI's background
                # on_done, or a mid-session ToolSearch hit).
                if self.tools_subset is not None and wire_name not in self.tools_subset:
                    continue
                if wire_name in self.bare_denied_names:
                    continue
                self.deferred[wire_name] = (None, ConnectorTool(info))
                added.append(wire_name)
        return added

    def ensure_connectors_discovered_for_query(self, query: str, *, timeout: float = 20.0) -> None:
        """W5b: print mode's third way a connector can be "requested" --
        "a ToolSearch select of such a name" (the brief's own wording,
        alongside `--tools` and `connectors.discover_on_start`). A
        `connector__*` tool can only ever enter `self.deferred` from an
        already-warm connectors cache (`headless.build_session` only ever
        READS the cache at session start, never blocks a plain run on
        discovery by default), so a model that calls `ToolSearch` asking
        for one by name -- when the cache is still cold -- would otherwise
        get "not found" forever, even mid-session. Called from
        `ToolSearchTool.run()` ONLY when the query itself actually mentions
        a connector (never on an ordinary keyword search), so a plain
        ToolSearch call never pays for this; `connectors_bridge.
        ensure_discovered_synchronously_if_cold` is already a cheap,
        self-gating no-op the rest of the time (cache already warm, not
        eligible, or already run once this session) -- same shape as
        `ensure_lazy_discovered`'s own "cheap: a no-op once already
        started" call every ToolSearch hit makes unconditionally."""
        if "connector" not in query.lower():
            return
        from halo_harness.mcp import connectors_bridge
        # review finding 30: `already_discovered_or_warm()` is the SAME
        # cheap, no-subprocess check `ensure_discovered_synchronously_if_
        # cold` already runs internally -- checked here FIRST now so a
        # warm cache (or a discovery already run/running this session)
        # skips straight past `prime_auth_cache_if_stale()` (a real
        # `claude auth status` spawn, up to 10s, once the 30s auth TTL has
        # lapsed) instead of paying for it on every ToolSearch whose query
        # merely contains "connector".
        if connectors_bridge.already_discovered_or_warm():
            return
        connectors_bridge.prime_auth_cache_if_stale()
        if not connectors_bridge.ensure_discovered_synchronously_if_cold(timeout=timeout):
            return
        self.add_connector_tools(connectors_bridge.get_connectors())
