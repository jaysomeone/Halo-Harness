"""tests.test_mcp_catalog -- rolo_claude/agent/catalog.py +
tools/registry.py's add_tool/remove_tool/definitions_for +
tools/tool_search.py's catalog-aware path (H3 scope C, plan revision 4):
frozen-catalog preload selection under the 32/128 host caps, ToolSearch
`select:`/keyword loading a deferred tool (append-only, tool_reference
blocks), and LRU eviction once the catalog is full.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.agent.catalog import DEFAULT_DATABRICKS_CAP, DEFAULT_OPENROUTER_CAP, SessionCatalog, host_cap, select_preload
from rolo_claude.mcp.manager import McpManager, McpServerConfig
from rolo_claude.tools.registry import ToolRegistry
from rolo_claude.tools.tool_search import ToolSearchTool
from rolo_claude.tools.base import ToolContext

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _manager(tool_count=None):
    env = {}
    if tool_count:
        env["FAKE_MCP_TOOL_COUNT"] = str(tool_count)
    cfg = McpServerConfig(name="fake", type="stdio", command=sys.executable,
                           args=["-m", "tests.helpers.fake_mcp_server"], env=env, cwd=str(REPO_DIR))
    mgr = McpManager({"fake": cfg}, tool_env=dict(os.environ), cwd=REPO_DIR)
    mgr.start_all()
    return mgr


# ---- host_cap / select_preload ---------------------------------------------

@test
def test_host_cap_by_provider(ctx: Ctx):
    ctx.check("databricks -> 32", host_cap("databricks") == DEFAULT_DATABRICKS_CAP == 32)
    ctx.check("openrouter -> 128", host_cap("openrouter") == DEFAULT_OPENROUTER_CAP == 128)
    ctx.check("anything else -> 128 (the openrouter/generic default)", host_cap("anthropic") == 128)


@test
def test_select_preload_always_load_wins_first(ctx: Ctx):
    mgr = _manager()
    try:
        triples = mgr.all_tools()
        preload, deferred = select_preload(triples, cap_budget=1)
        names = [t[1] for t in preload]
        ctx.check(f"the ONE preload slot goes to the alwaysLoad tool, got {names}",
                  names == ["mcp__fake__always_load_tool"])
        ctx.check("everything else deferred", len(deferred) == len(triples) - 1)
    finally:
        mgr.close_all()


@test
def test_select_preload_requested_names_after_always_load(ctx: Ctx):
    mgr = _manager()
    try:
        triples = mgr.all_tools()
        preload, deferred = select_preload(triples, preload_names={"mcp__fake__echo"}, cap_budget=2)
        names = [t[1] for t in preload]
        ctx.check(f"alwaysLoad + the requested name, got {names}",
                  set(names) == {"mcp__fake__always_load_tool", "mcp__fake__echo"})
    finally:
        mgr.close_all()


@test
def test_select_preload_zero_budget_defers_everything(ctx: Ctx):
    mgr = _manager()
    try:
        triples = mgr.all_tools()
        preload, deferred = select_preload(triples, cap_budget=0)
        ctx.check("nothing preloaded with a zero budget", preload == [])
        ctx.check("every tool deferred", len(deferred) == len(triples))
    finally:
        mgr.close_all()


@test
def test_select_preload_never_exceeds_cap_budget(ctx: Ctx):
    """finding 1: preload is ONLY alwaysLoad + mcpPreload, so an
    over-budget REQUEST (more mcpPreload names than cap_budget allows) is
    what must be truncated -- "rest" (unrequested) tools never fill
    leftover budget regardless of how much headroom cap_budget leaves."""
    mgr = _manager(tool_count=20)
    try:
        triples = mgr.all_tools()
        requested_names = {t[1] for t in triples if t[1] != "mcp__fake__always_load_tool"}
        ctx.check(f"at least 6 requestable names to work with, got {len(requested_names)}",
                  len(requested_names) >= 6)
        preload, deferred = select_preload(triples, preload_names=requested_names, cap_budget=5)
        ctx.check(f"exactly cap_budget preloaded (never more), got {len(preload)}", len(preload) == 5)
        ctx.check(f"the rest deferred, got preload={len(preload)} deferred={len(deferred)} total={len(triples)}",
                  len(preload) + len(deferred) == len(triples))
    finally:
        mgr.close_all()


@test
def test_select_preload_rest_never_fills_leftover_budget(ctx: Ctx):
    """The bug finding 1 fixes, directly: with NO requested names at all,
    a huge cap_budget must still preload ONLY the alwaysLoad tool -- never
    quietly fill the rest of the budget with unrequested tools (that's
    what put rolo's real catalog exactly at cap with zero headroom)."""
    mgr = _manager(tool_count=20)
    try:
        triples = mgr.all_tools()
        preload, deferred = select_preload(triples, cap_budget=1000)
        names = [t[1] for t in preload]
        ctx.check(f"only the alwaysLoad tool preloads, got {names}", names == ["mcp__fake__always_load_tool"])
        ctx.check("every other tool -- however much budget is left -- is deferred",
                  len(deferred) == len(triples) - 1)
    finally:
        mgr.close_all()


# ---- SessionCatalog.search / load / LRU eviction --------------------------

def _catalog(mgr, *, cap, preload_names=None, cap_budget=None):
    triples = mgr.all_tools()
    core = ToolRegistry()
    budget = cap_budget if cap_budget is not None else max(0, cap - len(core.names()))
    preload, deferred = select_preload(triples, preload_names=preload_names, cap_budget=budget)
    from rolo_claude.tools.mcp_tool import McpTool
    for server, wire_name, sdk_tool in preload:
        core.add_tool(McpTool(server, sdk_tool, mgr, vision=False))
    return SessionCatalog(registry=core, deferred=deferred, manager=mgr, cap=cap, names=core.names())


@test
def test_catalog_search_select_finds_loaded_and_deferred(ctx: Ctx):
    mgr = _manager()
    try:
        # finding 1: "echo" must be explicitly REQUESTED (mcpPreload-style)
        # to be preloaded now -- select_preload no longer auto-fills
        # leftover cap_budget with unrequested ("rest") tools.
        cat = _catalog(mgr, cap=30, cap_budget=2, preload_names={"mcp__fake__echo"})
        defs, deferred_matched = cat.search("select:mcp__fake__echo,mcp__fake__error_tool")
        found_names = {d["name"] for d in defs}
        ctx.check(f"finds both (one loaded, one deferred), got {found_names}",
                  found_names == {"mcp__fake__echo", "mcp__fake__error_tool"})
        ctx.check("only the still-deferred one is reported as loadable", deferred_matched == ["mcp__fake__error_tool"])
    finally:
        mgr.close_all()


@test
def test_catalog_search_keyword(ctx: Ctx):
    mgr = _manager()
    try:
        cat = _catalog(mgr, cap=30)
        defs, _ = cat.search("echo", max_results=5)
        ctx.check("keyword search finds the echo tool", any(d["name"] == "mcp__fake__echo" for d in defs))
    finally:
        mgr.close_all()


@test
def test_catalog_load_appends_names_never_reorders(ctx: Ctx):
    mgr = _manager()
    try:
        cat = _catalog(mgr, cap=30, cap_budget=2)
        before = list(cat.names)
        loaded = cat.load(["mcp__fake__error_tool"])
        ctx.check("load returns the loaded name", loaded == ["mcp__fake__error_tool"])
        ctx.check("appended to the END, existing prefix untouched",
                  cat.names[:len(before)] == before and cat.names[-1] == "mcp__fake__error_tool")
        ctx.check("the tool is now in the registry for real dispatch", cat.registry.get("mcp__fake__error_tool") is not None)
    finally:
        mgr.close_all()


@test
def test_catalog_on_grow_callback_fires_with_full_list(ctx: Ctx):
    mgr = _manager()
    try:
        cat = _catalog(mgr, cap=30, cap_budget=2)
        grown = []
        cat.on_grow = lambda names: grown.append(list(names))
        cat.load(["mcp__fake__huge"])
        ctx.check("on_grow called once", len(grown) == 1)
        ctx.check("carries the FULL updated name list", grown[0] == cat.names)
    finally:
        mgr.close_all()


@test
def test_catalog_reselecting_a_loaded_name_is_a_noop_touch(ctx: Ctx):
    mgr = _manager()
    try:
        cat = _catalog(mgr, cap=30)
        cat.load(["mcp__fake__huge"])
        names_after_first_load = list(cat.names)
        grown = []
        cat.on_grow = lambda names: grown.append(names)
        cat.load(["mcp__fake__huge"])  # already loaded
        ctx.check("re-selecting an already-loaded name doesn't change the catalog", cat.names == names_after_first_load)
        ctx.check("on_grow is NOT called for a pure re-touch (nothing NEW loaded)", grown == [])
    finally:
        mgr.close_all()


@test
def test_catalog_lru_eviction_respects_cap(ctx: Ctx):
    mgr = _manager()
    try:
        # cap leaves exactly 2 slots of headroom above the frozen set.
        cat = _catalog(mgr, cap=len(ToolRegistry().names()) + 1 + 2, preload_names=set(), cap_budget=1)
        deferred_names = sorted(cat.deferred)
        ctx.check(f"at least 3 deferred tools to work with, got {deferred_names}", len(deferred_names) >= 3)
        cat.load(deferred_names[:3])  # load 3 into only 2 slots of headroom
        ctx.check(f"catalog never exceeds cap, len={len(cat.names)} cap={cat.cap}", len(cat.names) <= cat.cap)
        ctx.check("the MOST RECENTLY loaded 2 survive (LRU evicts oldest first)",
                  deferred_names[1] in cat.names and deferred_names[2] in cat.names)
        ctx.check("the oldest of the 3 was evicted back to the deferred pool",
                  deferred_names[0] not in cat.names and deferred_names[0] in cat.deferred)
    finally:
        mgr.close_all()


@test
def test_catalog_evicted_tool_is_reloadable(ctx: Ctx):
    mgr = _manager()
    try:
        cat = _catalog(mgr, cap=len(ToolRegistry().names()) + 1 + 1, preload_names=set(), cap_budget=1)
        deferred_names = sorted(cat.deferred)
        first, second = deferred_names[0], deferred_names[1]
        cat.load([first])
        cat.load([second])  # evicts `first` (only 1 slot of headroom)
        ctx.check(f"{first} evicted", first not in cat.names)
        cat.load([first])  # reload it
        ctx.check(f"{first} loadable again after eviction", first in cat.names)
        ctx.check(f"{second} evicted in turn (cap still 1 over frozen)", second not in cat.names)
    finally:
        mgr.close_all()


@test
def test_catalog_deferred_lru_max_100(ctx: Ctx):
    from rolo_claude.agent.catalog import DEFERRED_LRU_MAX
    ctx.check("D5's own '100 loaded deferred tools' constant", DEFERRED_LRU_MAX == 100)


# ---- ToolSearchTool with ctx.catalog ---------------------------------------

@test
def test_toolsearch_loads_a_deferred_tool_and_returns_tool_reference(ctx: Ctx):
    mgr = _manager()
    try:
        cat = _catalog(mgr, cap=30, cap_budget=2)
        tctx = ToolContext(cwd=REPO_DIR, registry=cat.registry, catalog=cat)
        result = ToolSearchTool().run({"query": "select:mcp__fake__error_tool"}, tctx)
        ctx.check("no error", result.is_error is False)
        ctx.check("content becomes a list once something loaded", isinstance(result.content, list))
        ref_names = [b["tool_name"] for b in result.content if b.get("type") == "tool_reference"]
        ctx.check(f"a tool_reference block for the loaded tool, got {ref_names}", ref_names == ["mcp__fake__error_tool"])
        ctx.check("now really dispatchable", cat.registry.get("mcp__fake__error_tool") is not None)
    finally:
        mgr.close_all()


@test
def test_toolsearch_next_request_carries_the_loaded_tool(ctx: Ctx):
    """The point of the whole mechanism: after a ToolSearch load, a fresh
    `ToolRegistry.definitions_for(catalog.names)` call -- what
    agent/loop.py's `_on_catalog_grow` logs -- includes the newly-loaded
    tool's REAL schema."""
    mgr = _manager()
    try:
        cat = _catalog(mgr, cap=30)
        tctx = ToolContext(cwd=REPO_DIR, registry=cat.registry, catalog=cat)
        ToolSearchTool().run({"query": "select:mcp__fake__slow_tool"}, tctx)
        next_request_defs = cat.registry.definitions_for(cat.names)
        names = [d["name"] for d in next_request_defs]
        ctx.check("the loaded tool is now in the wire catalog", "mcp__fake__slow_tool" in names)
    finally:
        mgr.close_all()


@test
def test_headless_preload_formula_toolsearch_load_at_databricks_and_openrouter_caps(ctx: Ctx):
    """finding 16 test 1: the REAL headless.py formula (cap - len(built-in
    registry)), at BOTH real host caps -- a ToolSearch load must reach the
    wire catalog. This is exactly the path finding 1 broke: at cap 32/128
    with the OLD select_preload, headroom differed wildly by provider and
    a load could evict itself; slow_tool used to already be preloaded at
    cap 30 (7 fake tools fit easily), silently defeating this whole test."""
    from rolo_claude.agent.catalog import DEFAULT_DATABRICKS_CAP, DEFAULT_OPENROUTER_CAP
    for cap in (DEFAULT_DATABRICKS_CAP, DEFAULT_OPENROUTER_CAP):
        mgr = _manager()
        try:
            cat = _catalog(mgr, cap=cap)  # default cap_budget = headless.py's own "cap - len(registry)"
            ctx.check(f"cap={cap}: only the alwaysLoad tool preloads",
                      cat.names.count("mcp__fake__always_load_tool") == 1)
            ctx.check(f"cap={cap}: slow_tool starts DEFERRED, not auto-filled into the wire catalog",
                      "mcp__fake__slow_tool" in cat.deferred and "mcp__fake__slow_tool" not in cat.names)
            tctx = ToolContext(cwd=REPO_DIR, registry=cat.registry, catalog=cat)
            result = ToolSearchTool().run({"query": "select:mcp__fake__slow_tool"}, tctx)
            ctx.check(f"cap={cap}: no error", result.is_error is False)
            ref_names = ([b["tool_name"] for b in result.content if b.get("type") == "tool_reference"]
                         if isinstance(result.content, list) else [])
            ctx.check(f"cap={cap}: a real tool_reference for the newly loaded tool, got {ref_names}",
                      ref_names == ["mcp__fake__slow_tool"])
            next_names = [d["name"] for d in cat.registry.definitions_for(cat.names)]
            ctx.check(f"cap={cap}: the NEXT request's wire catalog carries it", "mcp__fake__slow_tool" in next_names)
        finally:
            mgr.close_all()


@test
def test_catalog_load_never_evicts_the_tool_being_loaded(ctx: Ctx):
    """finding 1's exact repro: cap sits with ZERO headroom above the
    frozen set (nothing loaded-deferred yet) -- loading ONE more deferred
    tool must be REFUSED, never evict itself (the only entry
    `_loaded_order` would otherwise contain right after being added)."""
    mgr = _manager()
    try:
        cat = _catalog(mgr, cap=len(ToolRegistry().names()) + 1, preload_names=set(), cap_budget=1)
        ctx.check(f"zero headroom at start, len(names)={len(cat.names)} cap={cat.cap}", len(cat.names) == cat.cap)
        deferred_name = sorted(cat.deferred)[0]
        loaded = cat.load([deferred_name])
        ctx.check(f"nothing could be evicted -> the load is REFUSED, not self-evicting, got loaded={loaded}",
                  loaded == [] and deferred_name not in cat.names)
        ctx.check(f"reported as refused, got {cat.last_refused}", cat.last_refused == [deferred_name])
        ctx.check("still findable/re-triable later (never dropped forever)", deferred_name in cat.deferred)
    finally:
        mgr.close_all()


@test
def test_toolsearch_reports_refused_load_with_no_tool_reference(ctx: Ctx):
    """finding 1: ToolSearch must never claim a tool it COULDN'T fit is
    now loaded -- no tool_reference block for a refused name, but its
    schema is still shown (the model sees it this turn even if it can't
    be called until something frees up room)."""
    mgr = _manager()
    try:
        cat = _catalog(mgr, cap=len(ToolRegistry().names()) + 1, preload_names=set(), cap_budget=1)
        deferred_name = sorted(cat.deferred)[0]
        tctx = ToolContext(cwd=REPO_DIR, registry=cat.registry, catalog=cat)
        result = ToolSearchTool().run({"query": f"select:{deferred_name}"}, tctx)
        has_ref = isinstance(result.content, list) and any(b.get("type") == "tool_reference" for b in result.content)
        ctx.check(f"no tool_reference block for a refused load, got {result.content!r}", not has_ref)
        body = result.content if isinstance(result.content, str) else next(
            (b["text"] for b in result.content if b.get("type") == "text"), "")
        ctx.check(f"the schema is still shown this turn, got body[:300]={body[:300]!r}", deferred_name in body)
        ctx.check(f"a clear explanation that it could not be loaded, got body={body!r}",
                  "could not load" in body.lower())
    finally:
        mgr.close_all()


@test
def test_refresh_deferred_for_server_adds_new_tools_leaves_loaded_alone(ctx: Ctx):
    mgr = _manager()
    try:
        cat = _catalog(mgr, cap=30, cap_budget=2, preload_names={"mcp__fake__echo"})
        loaded_names_before = list(cat.names)
        cat.deferred.pop("mcp__fake__huge", None)  # simulate it having gone missing pre-refresh
        cat.refresh_deferred_for_server("fake")
        ctx.check("re-adds a tool the pool had lost", "mcp__fake__huge" in cat.deferred)
        ctx.check("already-loaded tools untouched", cat.names == loaded_names_before)
        ctx.check("an already-loaded tool is never re-added to the deferred pool",
                  "mcp__fake__echo" not in cat.deferred)
    finally:
        mgr.close_all()


@test
def test_toolsearch_without_catalog_still_works_like_h2(ctx: Ctx):
    reg = ToolRegistry()
    tctx = ToolContext(cwd=REPO_DIR, registry=reg)  # no catalog=
    result = ToolSearchTool().run({"query": "select:Read"}, tctx)
    ctx.check("plain string content (unchanged H2 shape) when no catalog", isinstance(result.content, str))


@test
def test_registry_definitions_for_preserves_given_order(ctx: Ctx):
    reg = ToolRegistry()
    names = reg.names()
    reversed_order = list(reversed(names))
    defs = reg.definitions_for(reversed_order)
    ctx.check("definitions_for NEVER re-sorts", [d["name"] for d in defs] == reversed_order)


@test
def test_registry_add_and_remove_tool(ctx: Ctx):
    from rolo_claude.tools.read import ReadTool
    reg = ToolRegistry(tools=[])
    ctx.check("empty registry has no tools", reg.names() == [])
    reg.add_tool(ReadTool())
    ctx.check("add_tool makes it dispatchable", reg.get("Read") is not None)
    reg.remove_tool("Read")
    ctx.check("remove_tool removes it", reg.get("Read") is None)
    reg.remove_tool("NeverThere")  # must not raise
    ctx.check("removing a missing name is a silent no-op", True)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
