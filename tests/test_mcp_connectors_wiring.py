"""tests.test_mcp_connectors_wiring -- halo_harness/headless.py's own
connector-tool registration (Halo 2.0.1 gap-list brief, "W4 MCP: claude.ai
connectors bridge" item 3): a cached connector becomes a real
`connector__<slug>` session tool, deferred by default (ToolSearch) or
preloaded when `alwaysLoad` is configured, honouring --tools/deny the same
way a built-in would. Cache-only (never spawns `claude` just from building
a session -- see mcp/connectors_bridge.py's own eligibility docstring).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.mcp import connectors_bridge
from halo_harness.mcp.connectors import ConnectorInfo

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()

_INFO = ConnectorInfo(name="Claude Docs", slug="claude_docs", account_token="Claude_Docs",
                        url="https://api.anthropic.com/v1/pages/mcp", host="api.anthropic.com",
                        status="connected", tools=["batch", "create"])


def _scoped(fn):
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("BRIDGE_TEST_HOME")
        os.environ["BRIDGE_TEST_HOME"] = td
        try:
            return fn(Path(td))
        finally:
            if old is None:
                os.environ.pop("BRIDGE_TEST_HOME", None)
            else:
                os.environ["BRIDGE_TEST_HOME"] = old


@test
def test_connector_tool_deferred_by_default_not_on_the_wire(ctx: Ctx):
    def _run(_home: Path):
        connectors_bridge.save_cache([_INFO])
        from halo_harness import headless
        build = headless.build_session(cwd=REPO_DIR, model_ref_raw="or:deepseek/deepseek-v4.1-flash",
                                         bare=False, print_mode=True)
        names = build.tool_registry.names()
        ctx.check("not preloaded onto the wire catalog by default", "connector__claude_docs" not in names)
        catalog = build.session.session_catalog
        ctx.check("session has a catalog (mcp is available)", catalog is not None)
        ctx.check(f"but reachable via ToolSearch's deferred pool, got {sorted(catalog.deferred)}",
                  "connector__claude_docs" in catalog.deferred)
    _scoped(_run)


@test
def test_connector_tool_preloaded_when_always_load_configured(ctx: Ctx):
    def _run(_home: Path):
        connectors_bridge.save_cache([_INFO])
        from halo_harness.theme import set_config_value
        set_config_value("connectors.claude_docs.alwaysLoad", True)
        from halo_harness import headless
        build = headless.build_session(cwd=REPO_DIR, model_ref_raw="or:deepseek/deepseek-v4.1-flash",
                                         bare=False, print_mode=True)
        names = build.tool_registry.names()
        ctx.check(f"preloaded onto the wire catalog, got {'connector__claude_docs' in names}",
                  "connector__claude_docs" in names)
        tool = build.tool_registry.get("connector__claude_docs")
        ctx.check("it's the real ConnectorTool (not a stub)", tool is not None and tool.info.name == "Claude Docs")
    _scoped(_run)


@test
def test_connector_disabled_per_config_is_never_registered(ctx: Ctx):
    def _run(_home: Path):
        connectors_bridge.save_cache([_INFO])
        from halo_harness.theme import set_config_value
        set_config_value("connectors.claude_docs.enabled", False)
        from halo_harness import headless
        build = headless.build_session(cwd=REPO_DIR, model_ref_raw="or:deepseek/deepseek-v4.1-flash",
                                         bare=False, print_mode=True)
        ctx.check("never on the wire", "connector__claude_docs" not in build.tool_registry.names())
        catalog = build.session.session_catalog
        ctx.check("and never even in the deferred pool",
                  catalog is None or "connector__claude_docs" not in catalog.deferred)
    _scoped(_run)


@test
def test_tools_flag_excludes_connector_tools_like_any_other_name(ctx: Ctx):
    def _run(_home: Path):
        connectors_bridge.save_cache([_INFO])
        from halo_harness.theme import set_config_value
        set_config_value("connectors.claude_docs.alwaysLoad", True)
        from halo_harness import headless
        build = headless.build_session(cwd=REPO_DIR, model_ref_raw="or:deepseek/deepseek-v4.1-flash",
                                         bare=False, print_mode=True, tools="Read,Write")
        ctx.check("--tools Read,Write excludes it even though alwaysLoad is set",
                  "connector__claude_docs" not in build.tool_registry.names())
    _scoped(_run)


@test
def test_bare_session_never_builds_connector_tools(ctx: Ctx):
    def _run(_home: Path):
        connectors_bridge.save_cache([_INFO])
        from halo_harness import headless
        build = headless.build_session(cwd=REPO_DIR, model_ref_raw="or:deepseek/deepseek-v4.1-flash",
                                         bare=True, print_mode=True)
        ctx.check("--bare has no MCP/connectors wiring at all",
                  "connector__claude_docs" not in build.tool_registry.names())
    _scoped(_run)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
