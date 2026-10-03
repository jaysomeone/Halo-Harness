"""tests.test_mcp_catalog -- halo_harness/agent/catalog.py +
tools/registry.py's add_tool/remove_tool/definitions_for +
tools/tool_search.py's catalog-aware path (H3 scope C, plan revision 4):
frozen-catalog preload selection under the 32/128 host caps, ToolSearch
`select:`/keyword loading a deferred tool (append-only, tool_reference
blocks), and LRU eviction once the catalog is full.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.agent.catalog import DEFAULT_DATABRICKS_CAP, DEFAULT_OPENROUTER_CAP, SessionCatalog, host_cap, select_preload
from halo_harness.mcp.manager import McpManager, McpServerConfig
from halo_harness.tools.registry import ToolRegistry
from halo_harness.tools.tool_search import ToolSearchTool
from halo_harness.tools.base import ToolContext

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
    quietly fill the rest of the budget with unrequested tools (that's what
    put a real large catalog exactly at cap with zero headroom)."""
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
    from halo_harness.tools.mcp_tool import McpTool
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
    from halo_harness.agent.catalog import DEFERRED_LRU_MAX
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
    from halo_harness.agent.catalog import DEFAULT_DATABRICKS_CAP, DEFAULT_OPENROUTER_CAP
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
    from halo_harness.tools.read import ReadTool
    reg = ToolRegistry(tools=[])
    ctx.check("empty registry has no tools", reg.names() == [])
    reg.add_tool(ReadTool())
    ctx.check("add_tool makes it dispatchable", reg.get("Read") is not None)
    reg.remove_tool("Read")
    ctx.check("remove_tool removes it", reg.get("Read") is None)
    reg.remove_tool("NeverThere")  # must not raise
    ctx.check("removing a missing name is a silent no-op", True)


# ---- finding 12: golden ToolSearch ranking on a real-shaped catalog -------
# Names below are shaped like a real, large deferred-tool catalog from a
# home music-production MCP setup (anonymized -- not one real session's
# verbatim tool list, and every server/tool name is a generic stand-in,
# never a real product name), spanning 15 differently-themed MCP servers
# (daw, codriver, devices, expanded-models, gui, hardware, hear, jam, kb,
# max, mix, play, plugins, samples, drums -- excludes claude-in-chrome/
# claude_ai_* connectors, which aren't this kind of server). Descriptions
# are plausible reconstructions in this codebase's own terse MCP-tool
# style, sized to match the finding's own repro shape: most are one short
# sentence, `synth_set` and `hw_synth_cc` are deliberately long and
# mention "midi" many times INCIDENTALLY (hardware-synth CC parameter
# dumps), recreating the exact regression ("19 midi hits in 2,510
# characters" outscoring hw_ports' own short, on-topic one under the OLD
# raw-substring-count algorithm).

def _midi_heavy_description(subject: str) -> str:
    """~2,500 characters, ~19 incidental "midi" mentions -- same shape as
    the finding's own verified repro, built programmatically rather than
    typed out by hand."""
    sentence = (f"Configures {subject} MIDI CC assignments, MIDI clock source, MIDI sync mode, "
                f"MIDI velocity curve, MIDI aftertouch routing, MIDI pitch bend range, and MIDI "
                f"program change behaviour for this device. ")
    return (sentence * 8).strip()


_LARGE_TOOL_CATALOG = [
    # server, tool, description
    ("hardware", "hw_ports", "List every MIDI input and output port name currently visible to the OS."),
    ("hardware", "hw_clock_start", "Start the hardware MIDI clock generator at the current tempo."),
    ("hardware", "hw_clock_stop", "Stop the hardware MIDI clock generator."),
    ("hardware", "hw_clock_status", "Report whether the hardware MIDI clock is currently running."),
    ("hardware", "hw_calibrate", "Measure and store round-trip audio driver latency."),
    ("hardware", "hw_buffer_find", "Find the smallest stable audio buffer size for this machine."),
    ("hardware", "hw_latency_measure", "Measure end-to-end MIDI-to-audio latency with a loopback cable."),
    ("hardware", "hw_gpu", "Report installed GPU(s) and available VRAM."),
    ("hardware", "hw_cpu_profile", "Profile CPU headroom under a synthetic DSP load."),
    ("hardware", "hw_driver_guide", "Recommend an audio driver (ASIO/WASAPI/CoreAudio) for this machine."),
    ("hardware", "hw_machine_profile", "Summarise this machine's CPU/RAM/GPU/audio interface."),
    ("hardware", "hw_playability", "Score a MIDI controller's playability from its velocity curve."),
    ("hardware", "hw_profiles", "List saved hardware profiles."),
    ("hardware", "hw_align_take", "Align a recorded take's timing against the hardware clock."),
    ("hardware", "hw_multicore_status", "Report multicore render farm node status."),
    ("hardware", "hw_multicore_build", "Build a multicore render job."),
    ("hardware", "hw_multicore_mirror", "Mirror project files to a multicore render node."),
    ("hardware", "hw_multicore_recommend", "Recommend multicore render settings for this project."),
    ("hardware", "hw_synth_detect", "Detect connected hardware synthesizers over MIDI."),
    ("hardware", "hw_synth_profile", "Read one hardware synth's stored parameter profile."),
    ("hardware", "hw_synth_profiles", "List stored hardware synth profiles."),
    ("hardware", "hw_synth_diagnose", "Diagnose a hardware synth connection problem."),
    ("hardware", "hw_synth_setup_plan", "Plan a new hardware synth's studio setup."),
    ("hardware", "hw_synth_program_change", "Send a MIDI program change to a hardware synth."),
    ("hardware", "hw_synth_sysex_dump", "Dump a hardware synth's patch memory via SysEx."),
    ("hardware", "hw_synth_sysex_restore", "Restore a hardware synth's patch memory via SysEx."),
    ("hardware", "hw_synth_clock_plan", "Plan hardware synth MIDI clock routing."),
    ("hardware", "hw_synth_local_control", "Toggle a hardware synth's local control on/off."),
    ("hardware", "hw_synth_latency_compensation", "Compute hardware synth latency compensation in samples."),
    ("hardware", "hw_synth_cc", _midi_heavy_description("the hardware synth")),
    ("hardware", "ekit_detect", "Detect a connected electronic drum kit module."),
    ("hardware", "ekit_profile", "Read one electronic kit's stored pad-mapping profile."),
    ("hardware", "ekit_profiles_list", "List stored electronic kit profiles."),
    ("hardware", "ekit_drum_rack_map", "Map an electronic kit's pads onto a DAW drum rack."),
    ("hardware", "ekit_jam_map", "Map an electronic kit for a jam session."),
    ("hardware", "ekit_sampler_setup", "Configure the drum sampler library for a detected electronic kit."),
    ("daw", "synth_set", _midi_heavy_description("the hardware synth")),
    ("daw", "transport", "Start, stop, or query the DAW's transport."),
    ("daw", "midi_send", "Send a raw MIDI message to a track's input."),
    ("daw", "capture_midi", "Capture the last few bars of incoming MIDI as a clip."),
    ("daw", "tuning_system", "Set or query the current microtuning system."),
    ("daw", "scale", "Set or query the current musical scale."),
    ("daw", "set_mixer", "Set a track's volume/pan/sends."),
    ("daw", "create_track", "Create a new audio or MIDI track."),
    ("daw", "fire", "Fire a clip or scene."),
    ("daw", "get_session", "Read the current session's track/clip layout."),
    ("codriver", "desktop_screenshot", "Take a screenshot of the desktop."),
    ("codriver", "desktop_click", "Click at a screen coordinate."),
    ("codriver", "desktop_find", "Find a UI element on screen by description."),
    ("devices", "device_list", "List installed M4L devices."),
    ("devices", "device_get", "Read one M4L device's current parameter state."),
    ("devices", "device_new", "Scaffold a new M4L device project."),
    ("expanded-models", "ask_local", "Ask a locally-hosted model a question."),
    ("expanded-models", "ask_openrouter", "Ask an OpenRouter-hosted model a question."),
    ("gui", "doctor", "Check the DAW install's health."),
    ("gui", "export_audio", "Export the current selection as audio."),
    ("hear", "analyze_file", "Analyze an audio file's spectral content."),
    ("hear", "ab_compare", "A/B compare two audio renders."),
    ("jam", "jam_status", "Report the current jam session's state."),
    ("jam", "jam_generate_next", "Generate the next section of a jam."),
    ("kb", "kb_midi_lookup", "Look up a plugin's MIDI CC map in the knowledge base."),
    ("kb", "kb_midi_map", "Return a device's full MIDI CC map."),
    ("kb", "kb_search", "Full-text search the knowledge base."),
    ("kb", "kb_how_to", "Look up a how-to recipe by topic."),
    ("max", "max_object", "Look up a Max object's inlets/outlets/attributes."),
    ("max", "max_search", "Search Max object documentation."),
    ("mix", "mix_diagnose", "Diagnose common mix problems in the current session."),
    ("mix", "mix_apply", "Apply a mix plan to the session."),
    ("play", "play_pattern", "Play a generated rhythmic pattern."),
    ("play", "play_design_sound", "Design a synth sound from a text description."),
    ("plugins", "plugins_list", "List installed plugins."),
    ("plugins", "preset_recall", "Recall a saved plugin preset."),
    ("samples", "samples_search", "Search the sample library by description."),
    ("samples", "samples_similar", "Find samples similar to a given one."),
    ("drums", "drums_search_instruments", "Search the drum sampler library's instrument library."),
    ("drums", "drums_search_grooves", "Search the drum sampler library's MIDI groove library."),
]


def _sdk_tool(name, description):
    return SimpleNamespace(name=name, description=description,
                            input_schema={"type": "object", "properties": {}}, meta=None)


@test
def test_finding_12_golden_ranking_list_midi_ports_ranks_hw_ports_first(ctx: Ctx):
    """The exact regression repro from the finding: on a real ~100-tool
    catalog shape spanning 15 differently-themed MCP servers,
    "list MIDI ports" must rank `mcp__hardware__hw_ports` FIRST -- not
    14th behind a long, MIDI-word-stuffed description (synth_set/
    hw_synth_cc, both deliberately built to recreate that exact
    regression here)."""
    from halo_harness.mcp.manager import mcp_tool_name

    deferred = {}
    for server, name, desc in _LARGE_TOOL_CATALOG:
        wire_name = mcp_tool_name(server, name)
        deferred[wire_name] = (server, _sdk_tool(name, desc))

    catalog = SessionCatalog(registry=ToolRegistry(), deferred=deferred, manager=None, cap=999,
                              names=ToolRegistry().names())
    results, _ = catalog.search("list MIDI ports", max_results=5)
    ranked_names = [d["name"] for d in results]
    ctx.check(f"mcp__hardware__hw_ports ranks FIRST, got top 5={ranked_names}",
              ranked_names and ranked_names[0] == "mcp__hardware__hw_ports")
    ctx.check("the long MIDI-word-stuffed synth_set does NOT outrank it",
              "mcp__daw__synth_set" not in ranked_names or ranked_names[0] != "mcp__daw__synth_set")


@test
def test_catalog_deferred_pool_accepts_a_prebuilt_tool_not_only_mcp_sdk_tools(ctx: Ctx):
    """W4b connectors bridge: a `(None, tool_instance)` deferred entry is
    an ALREADY-BUILT Tool (e.g. ConnectorTool), not an MCP sdk_tool to
    wrap -- reuses the exact same preload/defer/LRU/ToolSearch/eviction
    machinery with no second abstraction layer (catalog.py's own
    `server is None` branch in `load()`; `_evict_one()` needed no change
    at all, since ConnectorTool sets `self.sdk_tool = self` /
    `self.server_name = None` for exactly this)."""
    from halo_harness.mcp.connectors import ConnectorInfo
    from halo_harness.tools.connector_tool import ConnectorTool

    registry = ToolRegistry([])
    info = ConnectorInfo(name="Claude Docs", slug="claude_docs", account_token="Claude_Docs",
                           url="https://x/mcp", host="x", status="connected", tools=["batch"])
    tool = ConnectorTool(info)
    catalog = SessionCatalog(registry=registry, deferred={"connector__claude_docs": (None, tool)},
                              manager=None, cap=5, names=[])

    defs, deferred_names = catalog.search("claude docs")
    ctx.check(f"found via keyword search, got {[d['name'] for d in defs]}",
              "connector__claude_docs" in [d["name"] for d in defs])
    ctx.check("reported as still deferred", "connector__claude_docs" in deferred_names)

    loaded = catalog.load(["connector__claude_docs"])
    ctx.check(f"load() recognises it, got {loaded}", loaded == ["connector__claude_docs"])
    ctx.check("the REAL ConnectorTool instance is now in the registry, unchanged",
              registry.get("connector__claude_docs") is tool)
    ctx.check("names grew", catalog.names == ["connector__claude_docs"])

    evicted = catalog._evict_one()
    ctx.check("evicted successfully", evicted is True)
    ctx.check("back in the deferred pool as (None, tool)",
              catalog.deferred["connector__claude_docs"] == (None, tool))
    ctx.check("removed from the live registry", registry.get("connector__claude_docs") is None)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
