"""tests.test_w5b_connector_cold_start -- W5b (2026-10-03 ~03:20): "connector
cold start, properly". W5a added `ensure_discovered_synchronously_if_cold`
and wired it unconditionally into `headless.build_session` (both print mode
and the TUI) -- live on the Kali VM that meant EVERY session build paid for
(or at least attempted) a synchronous discovery on a cold cache whether or
not anything actually wanted a connector, exactly the kind of default-path
blocking "never block a -p run on a `claude` spawn by default" forbids.
This module pins the real design:

* print mode only ever runs the synchronous path when a `connector__*` tool
  is actually REQUESTED: `--tools` naming one (`build_session`'s own
  `connector_requested` gate), a `ToolSearch` call that mentions "connector"
  (`SessionCatalog.ensure_connectors_discovered_for_query`, called from
  `ToolSearchTool.run()`), or `connectors.discover_on_start: true` in config
  (`connectors_bridge.discover_on_start_configured`) -- never by default.
* the TUI never runs it synchronously at all -- discovery stays entirely in
  the background (`connectors_bridge.ensure_discovered_in_background`); its
  `on_done` (`tui/bootstrap.build_controller`) is exercised here through its
  two building blocks (`SessionCatalog.add_connector_tools`, which it calls,
  and the `events.system_note` it posts) -- `test_tui.py` pins the
  transcript-rendering half of that same event.
* `halo mcp list` (W5a, unchanged) stays green: a one-shot listing command
  IS always "asking" for the connector list, so it keeps the unconditional
  prime-then-discover path (see test_w5_connector_cold_start.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_openai import MockUpstream
from halo_harness.mcp import connectors_bridge

REPO_DIR = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = f'"{sys.executable}" "{REPO_DIR / "tests" / "helpers" / "fake_claude_cc.py"}"'
test, TESTS = new_registry()


class _Env:
    KEYS = ("BRIDGE_TEST_HOME", "HALO_CLAUDE_EXE", "BRIDGE_CLAUDE_EXE", "FAKE_CLAUDE_CC_CONNECTORS",
            "FAKE_CLAUDE_CC_CONNECTOR_TOOLS", "BRIDGE_TEST_CC_AUTH_STATUS", "BRIDGE_TEST_NO_BACKGROUND_NET")

    def __enter__(self):
        self._snap = {k: os.environ.get(k) for k in self.KEYS}
        # W6b section E fallout: tests/run_all.py's own whole-run default
        # (closing a WSL hang in an unrelated module) now leaves this set
        # ambiently for every module -- this file's own cold-start tests
        # need it genuinely ABSENT to exercise "discovery actually ran",
        # so it is cleared here; a test that wants it set does so itself,
        # same as it always has.
        os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        return self

    def __exit__(self, *exc):
        for k, v in self._snap.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _scoped_home() -> Path:
    td = tempfile.mkdtemp(prefix="w5b-coldstart-home-")
    os.environ["BRIDGE_TEST_HOME"] = td
    return Path(td)


def _make_eligible() -> None:
    os.environ["HALO_CLAUDE_EXE"] = FAKE_CLAUDE
    os.environ["FAKE_CLAUDE_CC_CONNECTORS"] = json.dumps(
        [{"name": "Claude Docs", "url": "https://api.anthropic.com/v1/pages/mcp", "status_text": "✔ Connected"}])
    os.environ["FAKE_CLAUDE_CC_CONNECTOR_TOOLS"] = "mcp__claude_ai_Claude_Docs__batch"
    os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})


def _tiny_catalog(**extra):
    from halo_harness.agent.catalog import SessionCatalog
    from halo_harness.tools.registry import ToolRegistry

    class _StubManager:
        def all_tools(self):
            return []

        def ensure_lazy_started_all(self, abort=None):
            return []

    return SessionCatalog(registry=ToolRegistry(tools=[]), deferred={}, manager=_StubManager(), cap=32, **extra)


# ---- SessionCatalog.add_connector_tools ------------------------------------

@test
def test_add_connector_tools_adds_enabled_skips_disabled_and_dupes(ctx: Ctx):
    from halo_harness.mcp.connectors import ConnectorInfo
    from halo_harness.theme import set_config_value
    with _Env():
        _scoped_home()
        a = ConnectorInfo(name="Claude Docs", slug="claude_docs", account_token="Claude_Docs",
                           url="https://x/mcp", host="x", status="connected", status_text="OK")
        b = ConnectorInfo(name="Gmail", slug="gmail", account_token="Gmail",
                           url="https://y/mcp", host="y", status="connected", status_text="OK")
        set_config_value("connectors.gmail.enabled", False)
        catalog = _tiny_catalog()
        added = catalog.add_connector_tools([a, b])
        ctx.check(f"only the enabled one was added, got {added}", added == ["connector__claude_docs"])
        ctx.check("it's really in deferred", "connector__claude_docs" in catalog.deferred)
        ctx.check("the disabled one never entered deferred", "connector__gmail" not in catalog.deferred)

        again = catalog.add_connector_tools([a])
        ctx.check(f"a repeat call is a no-op (already deferred), got {again}", again == [])


@test
def test_add_connector_tools_skips_one_already_loaded(ctx: Ctx):
    from halo_harness.mcp.connectors import ConnectorInfo
    from halo_harness.tools.connector_tool import ConnectorTool
    with _Env():
        _scoped_home()
        info = ConnectorInfo(name="Claude Docs", slug="claude_docs", account_token="Claude_Docs",
                              url="https://x/mcp", host="x", status="connected", status_text="OK")
        catalog = _tiny_catalog()
        catalog.registry.add_tool(ConnectorTool(info))
        catalog.names.append("connector__claude_docs")
        added = catalog.add_connector_tools([info])
        ctx.check(f"never re-added over an already-loaded tool, got {added}", added == [])
        ctx.check("deferred stays empty", catalog.deferred == {})


@test
def test_add_connector_tools_respects_tools_subset_and_bare_denied_names(ctx: Ctx):
    """Release review finding 30: `add_connector_tools` skipped the
    --tools/bare-deny filters `headless.build_session`'s own, otherwise
    identical, warm-cache loop already applies -- a connector excluded by
    `--tools` or a bare deny rule at session start became loadable again
    the moment it was (re)discovered mid-session."""
    from halo_harness.mcp.connectors import ConnectorInfo
    with _Env():
        _scoped_home()
        a = ConnectorInfo(name="Claude Docs", slug="claude_docs", account_token="Claude_Docs",
                           url="https://x/mcp", host="x", status="connected", status_text="OK")
        b = ConnectorInfo(name="Gmail", slug="gmail", account_token="Gmail",
                           url="https://y/mcp", host="y", status="connected", status_text="OK")

        subset_catalog = _tiny_catalog(tools_subset={"connector__claude_docs"})
        added = subset_catalog.add_connector_tools([a, b])
        ctx.check(f"--tools subset excludes the one not named, got {added}", added == ["connector__claude_docs"])

        denied_catalog = _tiny_catalog(bare_denied_names={"connector__claude_docs"})
        added2 = denied_catalog.add_connector_tools([a, b])
        ctx.check(f"a bare deny rule excludes it even with no --tools subset, got {added2}",
                  added2 == ["connector__gmail"])


# ---- SessionCatalog.ensure_connectors_discovered_for_query -----------------

@test
def test_ensure_connectors_discovered_for_query_ignores_an_unrelated_query(ctx: Ctx):
    with _Env():
        _scoped_home()
        _make_eligible()
        connectors_bridge.reset_session_state()
        catalog = _tiny_catalog()
        catalog.ensure_connectors_discovered_for_query("read files")
        ctx.check("an unrelated query never triggers discovery", connectors_bridge.load_cache() == ([], None))
        ctx.check("deferred stays empty", catalog.deferred == {})


@test
def test_ensure_connectors_discovered_for_query_select_connector_name_discovers(ctx: Ctx):
    with _Env():
        _scoped_home()
        _make_eligible()
        connectors_bridge.reset_session_state()
        catalog = _tiny_catalog()
        catalog.ensure_connectors_discovered_for_query("select:connector__claude_docs", timeout=15.0)
        found, _fetched_at = connectors_bridge.load_cache()
        ctx.check(f"a select: query naming a connector triggers real discovery, got {found}",
                  [c.name for c in found] == ["Claude Docs"])
        ctx.check("the discovered connector landed in deferred", "connector__claude_docs" in catalog.deferred)


@test
def test_ensure_connectors_discovered_for_query_keyword_connector_discovers(ctx: Ctx):
    with _Env():
        _scoped_home()
        _make_eligible()
        connectors_bridge.reset_session_state()
        catalog = _tiny_catalog()
        catalog.ensure_connectors_discovered_for_query("find a connector tool", timeout=15.0)
        found, _fetched_at = connectors_bridge.load_cache()
        ctx.check(f"the free-text word 'connector' also triggers it, got {found}",
                  [c.name for c in found] == ["Claude Docs"])


@test
def test_ensure_connectors_discovered_for_query_skips_auth_priming_when_cache_already_warm(ctx: Ctx):
    """Release review finding 30: `prime_auth_cache_if_stale()` (a real
    `claude auth status` spawn, up to 10s, once the 30s auth TTL has
    lapsed) used to run unconditionally, BEFORE the cheap "is the cache
    already warm" check that `ensure_discovered_synchronously_if_cold`
    only ran internally, AFTER it. A warm cache must skip straight past
    it now."""
    from halo_harness.mcp.connectors import ConnectorInfo
    with _Env():
        _scoped_home()
        _make_eligible()  # eligible for discovery, but the point is this never has to matter
        connectors_bridge.reset_session_state()
        connectors_bridge.save_cache([ConnectorInfo(name="Already Warm", slug="already_warm",
                                                       account_token="Already_Warm", url="https://x/mcp",
                                                       host="x", status="connected", status_text="OK")])
        calls = []
        original = connectors_bridge.prime_auth_cache_if_stale
        connectors_bridge.prime_auth_cache_if_stale = lambda: calls.append(1)
        try:
            catalog = _tiny_catalog()
            catalog.ensure_connectors_discovered_for_query("find a connector tool", timeout=15.0)
            ctx.check(f"prime_auth_cache_if_stale was never called, got {len(calls)} call(s)", calls == [])
        finally:
            connectors_bridge.prime_auth_cache_if_stale = original


# ---- ToolSearchTool.run() end to end ---------------------------------------

@test
def test_toolsearch_select_connector_discovers_and_loads_it_when_cold(ctx: Ctx):
    from halo_harness.tools.base import ToolContext
    from halo_harness.tools.tool_search import ToolSearchTool
    with _Env():
        _scoped_home()
        _make_eligible()
        connectors_bridge.reset_session_state()
        catalog = _tiny_catalog()
        ctx_obj = ToolContext(cwd=REPO_DIR, registry=catalog.registry, catalog=catalog)
        result = ToolSearchTool().run({"query": "select:connector__claude_docs"}, ctx_obj)
        ctx.check(f"no error, got {result.content!r}", not result.is_error)
        ctx.check("connector__claude_docs is now loaded (callable) from a COLD cache",
                  "connector__claude_docs" in catalog.names)


@test
def test_toolsearch_ordinary_keyword_query_never_discovers(ctx: Ctx):
    from halo_harness.tools.base import ToolContext
    from halo_harness.tools.tool_search import ToolSearchTool
    with _Env():
        _scoped_home()
        _make_eligible()
        connectors_bridge.reset_session_state()
        catalog = _tiny_catalog()
        ctx_obj = ToolContext(cwd=REPO_DIR, registry=catalog.registry, catalog=catalog)
        ToolSearchTool().run({"query": "read files"}, ctx_obj)
        ctx.check("an unrelated ToolSearch call never spawns connector discovery",
                  connectors_bridge.load_cache() == ([], None))


# ---- print mode: the three "requested" gates, end to end -------------------

def _cli_env(home: Path, *, extra: Optional[dict] = None) -> dict:
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    # W6b section E fallout: tests/run_all.py's own whole-run default
    # (closing a WSL hang in an unrelated module) now leaves this set
    # ambiently in the PARENT process -- these child CLI runs need it
    # genuinely ABSENT to actually run the synchronous cold-start
    # discovery several of this file's own tests check for.
    env.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
    env.update({
        "BRIDGE_TEST_HOME": str(home), "PYTHONPATH": str(REPO_DIR), "HALO_CLAUDE_EXE": FAKE_CLAUDE,
        "FAKE_CLAUDE_CC_CONNECTORS": json.dumps(
            [{"name": "Claude Docs", "url": "https://api.anthropic.com/v1/pages/mcp",
              "status_text": "✔ Connected"}]),
        "FAKE_CLAUDE_CC_CONNECTOR_TOOLS": "mcp__claude_ai_Claude_Docs__batch",
        "BRIDGE_TEST_CC_AUTH_STATUS": json.dumps({"loggedIn": True, "authMethod": "claude.ai"}),
    })
    env.update(extra or {})
    return env


def _cache_file(home: Path) -> Path:
    return home / ".halo" / "mcp" / "connectors.json"


def _run_print_mode(env: dict, extra_args: Optional[list] = None):
    mock = MockUpstream().start()
    try:
        full_env = dict(env)
        full_env.update({"BRIDGE_OPENROUTER_BASE_URL": mock.base_url, "OPENROUTER_API_KEY": "test-key"})
        args = [sys.executable, "-m", "halo_harness", "-p", "reply with the single word pong",
                "--model", "or:mock/model"] + (extra_args or [])
        return subprocess.run(args, env=full_env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=30)
    finally:
        mock.stop()


@test
def test_print_mode_default_never_discovers_a_cold_cache(ctx: Ctx):
    home = Path(tempfile.mkdtemp(prefix="w5b-cli-default-"))
    result = _run_print_mode(_cli_env(home))
    ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-400:]!r}", result.returncode == 0)
    ctx.check("prompt still ran (pong in stdout)", "pong" in result.stdout)
    ctx.check("never blocked on a connector discovery by default (cache still cold)",
              not _cache_file(home).exists())


@test
def test_print_mode_tools_flag_naming_a_connector_discovers_it(ctx: Ctx):
    home = Path(tempfile.mkdtemp(prefix="w5b-cli-tools-"))
    result = _run_print_mode(_cli_env(home), extra_args=["--tools", "connector__claude_docs"])
    ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-400:]!r}", result.returncode == 0)
    ctx.check(f"--tools naming a connector__* tool runs the synchronous cold start, "
              f"exists={_cache_file(home).exists()}", _cache_file(home).exists())
    cached = json.loads(_cache_file(home).read_text(encoding="utf-8"))
    ctx.check(f"the real connector is now cached, got {cached}",
              any(c.get("name") == "Claude Docs" for c in cached.get("connectors", [])))


@test
def test_print_mode_discover_on_start_config_discovers_without_tools_flag(ctx: Ctx):
    home = Path(tempfile.mkdtemp(prefix="w5b-cli-config-"))
    (home / ".halo").mkdir(parents=True, exist_ok=True)
    (home / ".halo" / "config.json").write_text(
        json.dumps({"connectors": {"discover_on_start": True}}), encoding="utf-8")
    result = _run_print_mode(_cli_env(home))
    ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-400:]!r}", result.returncode == 0)
    ctx.check("connectors.discover_on_start=true runs the synchronous cold start too",
              _cache_file(home).exists())


# ---- the TUI startup worker re-kicks discovery once the auth cache is primed

def _startup_worker_stub(parked, notes):
    """A stand-in for the mounted BridgeApp: `_prime_auth_status_worker`
    only touches `self.controller`, `self.call_from_thread` and
    `self.notify`, so it can run unbound against this, with no Textual
    pilot at all."""
    from types import SimpleNamespace
    return SimpleNamespace(
        controller=SimpleNamespace(connectors_discovery_on_done=parked),
        call_from_thread=lambda fn, *a, **kw: fn(*a, **kw),
        notify=lambda *a, **kw: notes.append(a),
    )


@test
def test_tui_startup_worker_rekicks_discovery_once_the_auth_refresh_says_claude_ai(ctx: Ctx):
    """2.0.1 part 11, found live on the Kali VM: `tui/bootstrap.py`'s own
    background kick runs before any auth status is cached, so
    `discovery_eligible()` refuses it and a cold-cache TUI never learned
    its connectors (no transcript note, MCP 0/0 all session). The startup
    worker that primes the auth cache must re-kick the same once-per-
    process discovery, with the `on_done` bootstrap parked on the
    controller, as soon as its refresh says claude.ai login."""
    from halo_harness.tui.app import BridgeApp
    gateway_keys = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")
    snap = {k: os.environ.get(k) for k in gateway_keys}
    with _Env():
        _scoped_home()
        _make_eligible()
        for k in gateway_keys:
            os.environ.pop(k, None)
        calls: list = []
        notes: list = []
        real = connectors_bridge.ensure_discovered_in_background
        connectors_bridge.ensure_discovered_in_background = lambda **kw: calls.append(kw) or True
        try:
            parked = lambda connectors: None
            BridgeApp._prime_auth_status_worker(_startup_worker_stub(parked, notes))
            ctx.check(f"discovery re-kicked exactly once, got {calls}", len(calls) == 1)
            ctx.check("with the on_done that bootstrap parked on the controller",
                      bool(calls) and calls[0].get("on_done") is parked)
            ctx.check(f"the subscription toast still fires once, got {notes}", len(notes) == 1)
        finally:
            connectors_bridge.ensure_discovered_in_background = real
            for k, v in snap.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


@test
def test_tui_startup_worker_leaves_discovery_alone_without_a_claude_ai_login(ctx: Ctx):
    from halo_harness.tui.app import BridgeApp
    with _Env():
        _scoped_home()
        _make_eligible()
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
        calls: list = []
        notes: list = []
        real = connectors_bridge.ensure_discovered_in_background
        connectors_bridge.ensure_discovered_in_background = lambda **kw: calls.append(kw) or True
        try:
            BridgeApp._prime_auth_status_worker(_startup_worker_stub(lambda c: None, notes))
            ctx.check(f"no discovery kick without a login, got {calls}", calls == [])
            ctx.check(f"no toast either, got {notes}", notes == [])
        finally:
            connectors_bridge.ensure_discovered_in_background = real


@test
def test_build_controller_parks_the_discovery_callback_and_it_lands_tools_and_a_note(ctx: Ctx):
    """The other half of the re-kick: `tui/bootstrap.build_controller` must
    leave its `on_done` on the controller, and that callback, run later by
    the startup worker, adds the connector tool to the live catalog's
    deferred pool and posts exactly one transcript note. Not `--bare`: bare
    mode builds no session catalog at all (no MCP, no deferred pool), so
    there is nothing for discovery to land in; `strict_mcp_config` with no
    `--mcp-config` keeps the catalog real but empty of servers."""
    import argparse
    import queue
    from tests.helpers.fake_home import build_fake_home
    from halo_harness.mcp.connectors import ConnectorInfo
    extra_keys = ("OPENROUTER_API_KEY", "BRIDGE_OPENROUTER_BASE_URL")
    snap = {k: os.environ.get(k) for k in extra_keys}
    controller = None
    mock = MockUpstream().start()
    with _Env():
        fh = build_fake_home()
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test-default"
        os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = "1"
        try:
            from halo_harness.tui.bootstrap import build_controller
            args = argparse.Namespace(
                cwd=str(fh["proj"]), settings=None, allowed_tools=None, disallowed_tools=None,
                permission_mode="bypassPermissions", dangerously_skip_permissions=False, bare=False,
                tools=None, add_dir=None, model="or:mock/tui-e2e-read", small_model=None, session_id=None,
                max_turns=10, effort=None, append_system_prompt=None, chrome=False, no_chrome=False,
                playwright=False, playwright_cdp=None, playwright_headless=False, mcp_config=None,
                strict_mcp_config=True,
            )
            controller, _registry, _facade = build_controller(args)
            cb = getattr(controller, "connectors_discovery_on_done", None)
            ctx.check("bootstrap parked the discovery callback on the controller", callable(cb))
            catalog = getattr(controller.session, "session_catalog", None)
            ctx.check("the session has a live catalog", catalog is not None)
            info = ConnectorInfo(name="Docs Fake", slug="docs_fake", account_token="Docs_Fake",
                                 url="https://example.invalid/mcp", host="example.invalid",
                                 status="connected", status_text="connected",
                                 tools=["mcp__claude_ai_Docs_Fake__read"])
            if callable(cb):
                cb([info])
            ctx.check("the connector tool joined the deferred pool",
                      catalog is not None and "connector__docs_fake" in getattr(catalog, "deferred", {}))
            texts = []
            while True:
                try:
                    ev = controller.events.get_nowait()
                except queue.Empty:
                    break
                texts.append(str(getattr(ev, "data", "")))
            ctx.check(f"exactly one transcript note about the connector, got {texts}",
                      sum("1 claude.ai connector available." in t for t in texts) == 1)
        finally:
            if controller is not None:
                try:
                    controller.quit()
                except Exception:
                    pass
            mock.stop()
            for k, v in snap.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
