"""tests.test_mcp_lazy_cache -- H13 Part A ("lazy MCP start by default" +
the per-server tools cache): `halo_harness/mcp/tools_cache.py`, the tri-state
`mcpLazy` resolution in `halo_harness/mcp/manager.py`
(`_apply_lazy_defaults`), `mcp_setup.bootstrap_lazy_from_cache`, the new
`"cached"` handle state (`McpManager.mark_cached`/`all_tools`/
`ensure_started`/`reconnect`), and the stale-cache-notice path
(`SessionCatalog.refresh_if_stale`, `McpTool.run`). Uses the same real
stdio fake server (`tests.helpers.fake_mcp_server`) as `test_mcp_manager.py`
-- these are live connections, not mocks, for the same reason that file's
own docstring gives.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.mcp import manager as M
from halo_harness.mcp import tools_cache as TC

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _fake_cfg(name="fake", *, mode=None, lazy=None, always_load=False, extra_env=None):
    env = dict(extra_env or {})
    if mode:
        env["FAKE_MCP_MODE"] = mode
    return M.McpServerConfig(name=name, type="stdio", command=sys.executable,
                              args=["-m", "tests.helpers.fake_mcp_server"], env=env,
                              cwd=str(REPO_DIR), lazy=lazy, always_load=always_load)


class _isolated_state_dir:
    """Points `bridge_home()` (and so `tools_cache`'s own cache dir) at a
    fresh temp directory for the duration of the `with` block -- restores
    whatever `BRIDGE_STATE_DIR` was set to (or unset) before."""

    def __enter__(self):
        self._old = os.environ.get("BRIDGE_STATE_DIR")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BRIDGE_STATE_DIR"] = str(Path(self._tmp.name) / ".halo")
        return Path(os.environ["BRIDGE_STATE_DIR"])

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = self._old
        self._tmp.cleanup()


# ---- tools_cache.py itself --------------------------------------------------

@test
def test_tools_cache_round_trips_tools_and_instructions(ctx: Ctx):
    with _isolated_state_dir():
        ctx.check("no cache yet", TC.read_cache("srv") is None)
        ctx.check("no age yet", TC.cache_age_s("srv") is None)
        cfg = _fake_cfg("srv")
        key = TC.config_cache_key(cfg)

        class _T:
            def __init__(self, n, d):
                self.name, self.description = n, d
                self.input_schema = {"type": "object", "properties": {}}
                self.meta = {"anthropic/alwaysLoad": True} if n == "always" else None

        TC.write_cache("srv", key=key, tools=[_T("echo", "echoes"), _T("always", "preloaded")],
                        instructions="hello from srv")
        entry = TC.read_cache("srv")
        ctx.check("hash recorded", entry["hash"] == key)
        ctx.check("instructions recorded", entry["instructions"] == "hello from srv")
        names = sorted(t["name"] for t in entry["tools"])
        ctx.check(f"both tools serialized, got {names}", names == ["always", "echo"])
        age = TC.cache_age_s("srv")
        ctx.check(f"age is a small non-negative number, got {age}", age is not None and 0 <= age < 5)

        rebuilt = [TC.tool_from_dict(d) for d in entry["tools"]]
        by_name = {t.name: t for t in rebuilt}
        ctx.check("round-tripped tool keeps its description", by_name["echo"].description == "echoes")
        ctx.check("round-tripped tool keeps its meta", by_name["always"].meta.get("anthropic/alwaysLoad") is True)


@test
def test_config_cache_key_ignores_cwd_timeout_and_scope_but_not_command_args_env_url_headers(ctx: Ctx):
    base = _fake_cfg("srv")
    same_but_cwd = M.dataclass_replace_config(base, cwd="/somewhere/else", timeout_ms=9999, scope="project")
    ctx.check("cwd/timeout/scope never affect the key",
              TC.config_cache_key(base) == TC.config_cache_key(same_but_cwd))
    changed_args = M.dataclass_replace_config(base, args=list(base.args) + ["--extra"])
    ctx.check("a real config change (args) changes the key",
              TC.config_cache_key(base) != TC.config_cache_key(changed_args))
    changed_env = M.dataclass_replace_config(base, env={**base.env, "X": "1"})
    ctx.check("env changes the key too", TC.config_cache_key(base) != TC.config_cache_key(changed_env))


# ---- tri-state mcpLazy resolution (_apply_lazy_defaults) --------------------

@test
def test_parse_server_lazy_is_tristate_none_when_unset(ctx: Ctx):
    unset = M.parse_server("s", {"command": "x"}, scope="user")
    ctx.check("no mcpLazy key -> tri-state None, not False", unset.lazy is None)
    explicit_true = M.parse_server("s", {"command": "x", "mcpLazy": True}, scope="user")
    ctx.check("explicit true -> True", explicit_true.lazy is True)
    explicit_false = M.parse_server("s", {"command": "x", "mcpLazy": False}, scope="user")
    ctx.check("explicit false -> False", explicit_false.lazy is False)


def _claude_json_with(servers: dict) -> dict:
    return {"mcpServers": servers}


@test
def test_lazy_is_the_default_with_no_settings_and_no_per_server_key(ctx: Ctx):
    cwd = Path("/tmp/h13-lazy-default")
    resolved, _ = M.resolve_server_configs(
        cwd=cwd, claude_json=_claude_json_with({"plain": {"type": "stdio", "command": "x"}}))
    ctx.check(f"a plain server with no mcpLazy key is lazy by default, got {resolved['plain'].lazy!r}",
              resolved["plain"].lazy is True)


@test
def test_explicit_per_server_mcplazy_false_wins_over_the_lazy_default(ctx: Ctx):
    cwd = Path("/tmp/h13-lazy-default")
    resolved, _ = M.resolve_server_configs(
        cwd=cwd, claude_json=_claude_json_with({"eager": {"type": "stdio", "command": "x", "mcpLazy": False}}))
    ctx.check("explicit mcpLazy:false stays eager even though lazy is now the default",
              resolved["eager"].lazy is False)


@test
def test_always_load_server_is_eager_even_with_no_mcplazy_key(ctx: Ctx):
    cwd = Path("/tmp/h13-lazy-default")
    resolved, _ = M.resolve_server_configs(
        cwd=cwd, claude_json=_claude_json_with({"al": {"type": "stdio", "command": "x", "alwaysLoad": True}}))
    ctx.check("alwaysLoad forces eager regardless of the lazy default", resolved["al"].lazy is False)


@test
def test_always_load_beats_an_explicit_mcplazy_true_too(ctx: Ctx):
    cwd = Path("/tmp/h13-lazy-default")
    resolved, _ = M.resolve_server_configs(
        cwd=cwd, claude_json=_claude_json_with(
            {"al": {"type": "stdio", "command": "x", "alwaysLoad": True, "mcpLazy": True}}))
    ctx.check("alwaysLoad wins even over an explicit mcpLazy:true", resolved["al"].lazy is False)


class _FakeSettings:
    def __init__(self, raw):
        self.raw = raw


@test
def test_global_settings_mcplazy_false_flips_the_default_but_not_an_explicit_per_server_true(ctx: Ctx):
    cwd = Path("/tmp/h13-lazy-default")
    settings = _FakeSettings({"mcpLazy": False})
    resolved, _ = M.resolve_server_configs(
        cwd=cwd, settings=settings, claude_json=_claude_json_with({
            "plain": {"type": "stdio", "command": "x"},
            "forced-lazy": {"type": "stdio", "command": "x", "mcpLazy": True},
        }))
    ctx.check("global mcpLazy:false makes an unset-per-server entry eager",
              resolved["plain"].lazy is False)
    ctx.check("an explicit per-server mcpLazy:true still wins over the global false",
              resolved["forced-lazy"].lazy is True)


# ---- mcp_setup.bootstrap_lazy_from_cache ------------------------------------

@test
def test_bootstrap_with_no_cache_connects_once_and_writes_a_fresh_cache(ctx: Ctx):
    from halo_harness.mcp_setup import bootstrap_lazy_from_cache
    with _isolated_state_dir():
        cfg = _fake_cfg("boot1", lazy=True)
        mgr = M.McpManager({"boot1": cfg}, tool_env=dict(os.environ), cwd=REPO_DIR, lazy_names={"boot1"})
        try:
            mgr.start_all()  # lazy -- no-op
            ctx.check("still pending before bootstrap", mgr.handles["boot1"].state == "pending")
            bootstrap_lazy_from_cache(mgr, {"boot1": cfg}, {"boot1"})
            ctx.check(f"bootstrap connects once (no cache existed), got {mgr.handles['boot1'].state}",
                      mgr.handles["boot1"].state == "connected")
            entry = TC.read_cache("boot1")
            ctx.check("a fresh cache was written after the bootstrap connect", entry is not None)
            ctx.check("cache hash matches this config", entry["hash"] == TC.config_cache_key(cfg))
            ctx.check("cache has real tool names", "echo" in {t["name"] for t in entry["tools"]})
        finally:
            mgr.close_all()


@test
def test_bootstrap_with_6_servers_and_a_full_matching_cache_makes_zero_connections(ctx: Ctx):
    """H13 Part A acceptance line, verbatim: "startup with 6 configured
    servers and a full cache makes zero connections until a tool is used"."""
    from halo_harness.mcp_setup import bootstrap_lazy_from_cache
    with _isolated_state_dir():
        names = [f"srv{i}" for i in range(6)]
        configs = {n: _fake_cfg(n, lazy=True) for n in names}
        # Pre-populate a VALID cache for all 6 by hand (no connection at all).
        for n in names:
            TC.write_cache(n, key=TC.config_cache_key(configs[n]),
                            tools=[TC.CachedTool("echo", "echoes text back", {"type": "object"}, None)],
                            instructions=f"fake {n}")
        mgr = M.McpManager(configs, tool_env=dict(os.environ), cwd=REPO_DIR, lazy_names=set(names))
        try:
            mgr.start_all()
            bootstrap_lazy_from_cache(mgr, configs, set(names))
            states = {n: mgr.handles[n].state for n in names}
            ctx.check(f"all 6 seeded from cache, none connected, got {states}",
                      all(s == "cached" for s in states.values()))
            ctx.check("zero real connections -- no handle ever has a live session",
                      all(mgr.handles[n]._session is None for n in names))
            ctx.check("zero real connections -- no handle ever has a live transport stack",
                      all(mgr.handles[n]._stack is None for n in names))
            tools = mgr.all_tools()
            ctx.check(f"all 6 servers' tools are still discoverable via all_tools(), got {len(tools)}",
                      len(tools) == 6)
        finally:
            mgr.close_all()


@test
def test_bootstrap_connects_multiple_uncached_lazy_servers_in_parallel_not_serially(ctx: Ctx):
    """Bug found live (H13 Part D dogfooding): `bootstrap_lazy_from_cache`
    used to `h.start()` each uncached lazy server ONE AT A TIME in a plain
    loop -- 3 slow-starting uncached servers took roughly 3x one server's
    own connect time. It now shares `start_all()`'s concurrent-gather
    machinery (`McpManager.start_many`), so 3 slow ones should finish in
    roughly ONE slow-start's worth of wall clock, not three (same shape as
    `test_mcp_manager.py::test_h9_ensure_lazy_started_all_starts_targets_in_parallel`)."""
    import time
    from halo_harness.mcp_setup import bootstrap_lazy_from_cache
    with _isolated_state_dir():
        names = ["slowA", "slowB", "slowC"]
        configs = {n: _fake_cfg(n, lazy=True, extra_env={"FAKE_MCP_MODE": "slow", "FAKE_MCP_SLEEP_S": "1.5"})
                   for n in names}
        mgr = M.McpManager(configs, tool_env=dict(os.environ), cwd=REPO_DIR, lazy_names=set(names))
        try:
            mgr.start_all()  # no-op: all 3 are lazy
            t0 = time.monotonic()
            bootstrap_lazy_from_cache(mgr, configs, set(names))
            elapsed = time.monotonic() - t0
            states = [mgr.handles[n].state for n in names]
            ctx.check(f"all 3 connected, got {states}", states == ["connected"] * 3)
            ctx.check(f"parallel (~1.5s), not serial (~4.5s) -- took {elapsed:.2f}s", elapsed < 3.0)
        finally:
            mgr.close_all()


@test
def test_cache_invalidated_by_a_config_change_reconnects_and_recaches(ctx: Ctx):
    from halo_harness.mcp_setup import bootstrap_lazy_from_cache
    with _isolated_state_dir():
        old_cfg = _fake_cfg("changed", lazy=True)
        TC.write_cache("changed", key=TC.config_cache_key(old_cfg),
                        tools=[TC.CachedTool("stale_tool", "no longer real", {"type": "object"}, None)],
                        instructions="old")
        # Simulate a real edit to the server's own config (a new env var
        # changes its identity hash even though the command/args are the same).
        new_cfg = _fake_cfg("changed", lazy=True, extra_env={"FAKE_MCP_TOOL_COUNT": "5"})
        ctx.check("the edit actually changes the cache key",
                  TC.config_cache_key(old_cfg) != TC.config_cache_key(new_cfg))
        mgr = M.McpManager({"changed": new_cfg}, tool_env=dict(os.environ), cwd=REPO_DIR, lazy_names={"changed"})
        try:
            mgr.start_all()
            bootstrap_lazy_from_cache(mgr, {"changed": new_cfg}, {"changed"})
            ctx.check(f"a hash mismatch is treated as no-cache -- connects for real, got {mgr.handles['changed'].state}",
                      mgr.handles["changed"].state == "connected")
            names = {t.name for t in mgr.handles["changed"].tools}
            ctx.check(f"the REAL tool list replaces the stale one, got {names}", "stale_tool" not in names and "echo" in names)
            fresh = TC.read_cache("changed")
            ctx.check("the cache on disk now matches the NEW config's hash", fresh["hash"] == TC.config_cache_key(new_cfg))
        finally:
            mgr.close_all()


# ---- a tool call on a cached/lazy server connects it and runs --------------

@test
def test_tool_call_on_a_cached_server_connects_it_for_real_and_runs(ctx: Ctx):
    with _isolated_state_dir():
        cfg = _fake_cfg("call1", lazy=True)
        mgr = M.McpManager({"call1": cfg}, tool_env=dict(os.environ), cwd=REPO_DIR, lazy_names={"call1"})
        try:
            # Seed as "cached" by hand -- exactly what bootstrap_lazy_from_cache
            # does when a valid cache exists, without needing one on disk here.
            mgr.mark_cached("call1", [TC.CachedTool("echo", "echoes", {"type": "object", "properties": {}}, None)], "hi")
            ctx.check("seeded as cached, not connected", mgr.handles["call1"].state == "cached")
            result = mgr.call("call1", "echo", {"text": "hello"})
            ctx.check(f"the call succeeded through a real connect, got {result!r}",
                      any("hello" in str(getattr(c, "text", "")) for c in (getattr(result, "content", None) or [])))
            ctx.check("the handle is now really connected", mgr.handles["call1"].state == "connected")
        finally:
            mgr.close_all()


@test
def test_lazy_server_that_fails_on_first_use_gives_an_is_error_result_naming_the_server(ctx: Ctx):
    from halo_harness.tools.mcp_tool import McpTool
    from halo_harness.tools.base import ToolContext

    with _isolated_state_dir():
        cfg = _fake_cfg("boom", mode="crash", lazy=True)
        mgr = M.McpManager({"boom": cfg}, tool_env=dict(os.environ), cwd=REPO_DIR, lazy_names={"boom"})
        try:
            mgr.mark_cached("boom", [TC.CachedTool("echo", "echoes", {"type": "object", "properties": {}}, None)], None)
            sdk_tool = TC.CachedTool("echo", "echoes", {"type": "object", "properties": {}}, None)
            tool = McpTool("boom", sdk_tool, mgr)
            ctx.check("wire name names the server", tool.name == "mcp__boom__echo")
            result = tool.run({"text": "x"}, ToolContext(cwd=REPO_DIR))
            ctx.check(f"a connect failure on first use is an is_error result, got {result.is_error!r}", result.is_error)
            content = result.content if isinstance(result.content, str) else str(result.content)
            ctx.check(f"the error names the server, got {content!r}", "boom" in content)
        finally:
            mgr.close_all()


# ---- ToolSearch finds cached tools of an unconnected server -----------------

@test
def test_toolsearch_finds_a_cached_tools_definition_without_connecting(ctx: Ctx):
    from halo_harness.agent.catalog import SessionCatalog
    from halo_harness.tools.registry import ToolRegistry
    from halo_harness.tools.tool_search import ToolSearchTool
    from halo_harness.tools.base import ToolContext

    with _isolated_state_dir():
        cfg = _fake_cfg("srchsrv", lazy=True)
        mgr = M.McpManager({"srchsrv": cfg}, tool_env=dict(os.environ), cwd=REPO_DIR, lazy_names={"srchsrv"})
        try:
            mgr.mark_cached("srchsrv", [TC.CachedTool(
                "greet_person", "Send a friendly greeting to a named person", {"type": "object"}, None)], None)
            registry = ToolRegistry()
            catalog = SessionCatalog(registry=registry, deferred={}, manager=mgr, cap=128, names=[])
            for server, wire_name, sdk_tool in mgr.all_tools():
                catalog.deferred[wire_name] = (server, sdk_tool)

            result = ToolSearchTool().run({"query": "greeting"}, ToolContext(cwd=REPO_DIR, registry=registry, catalog=catalog))
            ctx.check(f"found the cached tool by keyword, got {result.content!r}",
                      "mcp__srchsrv__greet_person" in str(result.content))
            ctx.check("searching alone never connects the server",
                      mgr.handles["srchsrv"].state == "cached")
        finally:
            mgr.close_all()


# ---- stale cache detected on real connect -> catalog refreshed + notice ---

@test
def test_stale_cache_is_detected_on_real_connect_and_catalog_refreshed(ctx: Ctx):
    from halo_harness.agent.catalog import SessionCatalog
    from halo_harness.tools.registry import ToolRegistry

    with _isolated_state_dir():
        cfg = _fake_cfg("stale1", lazy=True)
        mgr = M.McpManager({"stale1": cfg}, tool_env=dict(os.environ), cwd=REPO_DIR, lazy_names={"stale1"})
        try:
            # A cache that promises a tool the real server does NOT have --
            # simulates the server's own tool list drifting server-side
            # while the config (and so the cache hash) stayed identical.
            mgr.mark_cached("stale1", [TC.CachedTool("no_such_tool", "not real", {"type": "object"}, None)], None)
            registry = ToolRegistry()
            catalog = SessionCatalog(registry=registry, deferred={}, manager=mgr, cap=128, names=[])
            ctx.check("not stale before any real connect happens", not mgr.was_cache_stale("stale1"))

            mgr.ensure_started("stale1")
            ctx.check("really connected now", mgr.handles["stale1"].state == "connected")
            ctx.check("the manager noticed the live tools differ from the cache", mgr.was_cache_stale("stale1"))

            note = catalog.refresh_if_stale("stale1")
            ctx.check(f"a human-readable note is returned, got {note!r}", note and "stale1" in note)
            ctx.check("the stale flag is cleared after refreshing once", not mgr.was_cache_stale("stale1"))
            note_again = catalog.refresh_if_stale("stale1")
            ctx.check("a second call is a clean no-op", note_again is None)
        finally:
            mgr.close_all()


@test
def test_mcp_tool_run_surfaces_the_stale_cache_note_alongside_the_result(ctx: Ctx):
    from halo_harness.agent.catalog import SessionCatalog
    from halo_harness.tools.registry import ToolRegistry
    from halo_harness.tools.mcp_tool import McpTool
    from halo_harness.tools.base import ToolContext

    with _isolated_state_dir():
        cfg = _fake_cfg("stale2", lazy=True)
        mgr = M.McpManager({"stale2": cfg}, tool_env=dict(os.environ), cwd=REPO_DIR, lazy_names={"stale2"})
        try:
            mgr.mark_cached("stale2", [TC.CachedTool("echo", "wrong schema", {"type": "object", "extra": True}, None)], None)
            registry = ToolRegistry()
            catalog = SessionCatalog(registry=registry, deferred={}, manager=mgr, cap=128, names=[])
            sdk_tool = TC.CachedTool("echo", "wrong schema", {"type": "object", "extra": True}, None)
            tool = McpTool("stale2", sdk_tool, mgr)
            result = tool.run({"text": "hi"}, ToolContext(cwd=REPO_DIR, registry=registry, catalog=catalog))
            text = str(result.content)
            ctx.check(f"the stale note is surfaced on the very call that discovered it, got {text!r}",
                      "stale2" in text and "changed since it was last cached" in text)
        finally:
            mgr.close_all()


# ---- status counts -----------------------------------------------------

@test
def test_status_counts_only_count_real_connections_not_cached(ctx: Ctx):
    with _isolated_state_dir():
        eager_cfg = _fake_cfg("eager1", lazy=False)
        lazy_cfg = _fake_cfg("lazy1", lazy=True)
        mgr = M.McpManager({"eager1": eager_cfg, "lazy1": lazy_cfg}, tool_env=dict(os.environ),
                            cwd=REPO_DIR, lazy_names={"lazy1"})
        try:
            mgr.start_all()  # only eager1 connects
            mgr.mark_cached("lazy1", [TC.CachedTool("echo", "e", {"type": "object"}, None)], None)
            rows = mgr.status()
            connected = sum(1 for r in rows if r.get("state") == "connected")
            total = len(rows)
            ctx.check(f"MCP 1/2 (cached does not count as connected), got {connected}/{total}",
                      connected == 1 and total == 2)
            cached_row = next(r for r in rows if r["name"] == "lazy1")
            ctx.check("a cached row still reports its known tool count", cached_row["tool_count"] == 1)

            mgr.call("lazy1", "echo", {"text": "x"})
            rows2 = mgr.status()
            connected2 = sum(1 for r in rows2 if r.get("state") == "connected")
            ctx.check(f"MCP 2/2 once the lazy server is actually used, got {connected2}/2", connected2 == 2)
        finally:
            mgr.close_all()


# ---- end to end through mcp_setup.build_manager (what a real session uses) -

@test
def test_build_manager_end_to_end_zero_connect_with_cache_then_one_on_tool_use(ctx: Ctx):
    """The brief's own acceptance line, through the SAME `build_manager`
    every real session (headless + TUI) and `mcp list`/`mcp get` call --
    not just the lower-level helpers the tests above exercise directly."""
    from halo_harness.mcp_setup import build_manager

    with _isolated_state_dir():
        claude_json = {"mcpServers": {"e2e": {"type": "stdio", "command": sys.executable,
                                               "args": ["-m", "tests.helpers.fake_mcp_server"]}}}
        # First build: no cache yet -> bootstraps (connects once) and caches.
        mgr1, notices1 = build_manager(cwd=REPO_DIR, claude_json=claude_json, print_mode=True, start=True)
        try:
            ctx.check(f"no notices on a clean bootstrap, got {notices1}", not notices1)
            ctx.check("the only configured server is lazy by default (no mcpLazy key)",
                      mgr1.configs["e2e"].lazy is True)
            ctx.check(f"first-ever session bootstraps it (connects once to build the cache), got "
                       f"{mgr1.handles['e2e'].state!r}", mgr1.handles["e2e"].state == "connected")
        finally:
            mgr1.close_all()

        # Second build (a later session, real cache now on disk): zero
        # connections until a tool is actually used.
        mgr2, _ = build_manager(cwd=REPO_DIR, claude_json=claude_json, print_mode=True, start=True)
        try:
            rows = mgr2.status()
            connected = sum(1 for r in rows if r.get("state") == "connected")
            ctx.check(f"MCP 0/{len(rows)} at startup -- the cache satisfied the catalog with zero connections, "
                       f"got {connected}/{len(rows)} (state={mgr2.handles['e2e'].state!r})",
                      connected == 0 and mgr2.handles["e2e"].state == "cached")
            all_tools = mgr2.all_tools()
            ctx.check(f"its tools are still discoverable from the cache, got {[t[1] for t in all_tools]}",
                      any(t[1] == "mcp__e2e__echo" for t in all_tools))

            mgr2.call("e2e", "echo", {"text": "ping"})
            rows_after = mgr2.status()
            connected_after = sum(1 for r in rows_after if r.get("state") == "connected")
            ctx.check(f"MCP 1/{len(rows_after)} once a tool is actually used, got {connected_after}",
                      connected_after == 1)
        finally:
            mgr2.close_all()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
