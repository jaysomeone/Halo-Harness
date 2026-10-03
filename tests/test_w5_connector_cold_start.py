"""tests.test_w5_connector_cold_start -- W5 (carried from W4b): "connector
cold start". Live on the Kali VM, a fresh print-mode call with an empty
connectors cache had NO `connector__*` tools at all (discovery only ran in
the background, racing the session catalog freeze). Pins
`connectors_bridge.ensure_discovered_synchronously_if_cold`, its two call
sites (`headless.build_session`, `mcp_cli._cmd_list`), `mcp.explain.
connector_lines`'s new "none discovered yet" fallback, and `ConnectorTool`'s
"tool names are learned on first use" description wording. Generic
connector names only (no Google -- rolo dropped that 2026-10-01).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.mcp import connectors_bridge

REPO_DIR = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = f'"{sys.executable}" "{REPO_DIR / "tests" / "helpers" / "fake_claude_cc.py"}"'
test, TESTS = new_registry()


class _Env:
    KEYS = ("BRIDGE_TEST_HOME", "HALO_CLAUDE_EXE", "BRIDGE_CLAUDE_EXE", "FAKE_CLAUDE_CC_CONNECTORS",
            "FAKE_CLAUDE_CC_CONNECTOR_TOOLS", "BRIDGE_TEST_CC_AUTH_STATUS", "BRIDGE_TEST_NO_BACKGROUND_NET")

    def __enter__(self):
        self._snap = {k: os.environ.get(k) for k in self.KEYS}
        return self

    def __exit__(self, *exc):
        for k, v in self._snap.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _scoped_home():
    td = tempfile.mkdtemp(prefix="w5-coldstart-home-")
    os.environ["BRIDGE_TEST_HOME"] = td
    return Path(td)


# ---- ensure_discovered_synchronously_if_cold (in-process) -----------------

@test
def test_cold_start_synchronously_populates_an_empty_cache_when_eligible(ctx: Ctx):
    with _Env():
        _scoped_home()
        os.environ["HALO_CLAUDE_EXE"] = FAKE_CLAUDE
        os.environ["FAKE_CLAUDE_CC_CONNECTORS"] = json.dumps(
            [{"name": "Claude Docs", "url": "https://x/mcp", "status_text": "✔ Connected"}])
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        connectors_bridge.reset_session_state()
        ctx.check("nothing cached yet", connectors_bridge.load_cache() == ([], None))
        ran = connectors_bridge.ensure_discovered_synchronously_if_cold(timeout=15.0)
        ctx.check("a synchronous discovery actually ran", ran is True)
        found, _fetched_at = connectors_bridge.load_cache()
        ctx.check(f"the cache is populated IMMEDIATELY, no polling needed, got {found}",
                  [c.name for c in found] == ["Claude Docs"])


@test
def test_cold_start_is_a_noop_when_not_eligible(ctx: Ctx):
    with _Env():
        _scoped_home()
        os.environ["HALO_CLAUDE_EXE"] = FAKE_CLAUDE
        os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)  # no confirmed claude.ai login
        connectors_bridge.reset_session_state()
        ran = connectors_bridge.ensure_discovered_synchronously_if_cold(timeout=15.0)
        ctx.check("no-op -- not eligible", ran is False)
        ctx.check("still nothing cached", connectors_bridge.load_cache() == ([], None))


@test
def test_cold_start_is_a_noop_when_the_cache_already_has_something(ctx: Ctx):
    from halo_harness.mcp import connectors as connectors_mod
    with _Env():
        _scoped_home()
        os.environ["HALO_CLAUDE_EXE"] = FAKE_CLAUDE
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        existing = connectors_mod.ConnectorInfo(name="Already Here", slug="already_here",
                                                  account_token="Already_Here", url="https://x/mcp", host="x",
                                                  status="connected", status_text="✔ Connected")
        connectors_bridge.save_cache([existing])
        connectors_bridge.reset_session_state()
        ran = connectors_bridge.ensure_discovered_synchronously_if_cold(timeout=15.0)
        ctx.check("no-op -- a warm cache is never blocked on again", ran is False)
        found, _ = connectors_bridge.load_cache()
        ctx.check("the existing cache entry is untouched", [c.name for c in found] == ["Already Here"])


@test
def test_cold_start_and_background_discovery_share_the_same_once_per_session_gate(ctx: Ctx):
    with _Env():
        _scoped_home()
        os.environ["HALO_CLAUDE_EXE"] = FAKE_CLAUDE
        os.environ["FAKE_CLAUDE_CC_CONNECTORS"] = json.dumps(
            [{"name": "Claude Docs", "url": "https://x/mcp", "status_text": "✔ Connected"}])
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        connectors_bridge.reset_session_state()
        ran = connectors_bridge.ensure_discovered_synchronously_if_cold(timeout=15.0)
        ctx.check("the synchronous cold-start ran first", ran is True)
        started = connectors_bridge.ensure_discovered_in_background()
        ctx.check("the background worker is then a no-op this same session (shared gate)", started is False)


# ---- connector_lines: "none discovered yet" fallback -----------------------

@test
def test_connector_lines_names_none_discovered_yet_when_eligible_but_cache_empty(ctx: Ctx):
    """The exact state `unavailable_reason()` returns None for (bridge
    enabled, `claude` installed, not gateway-driven, no confirmed non-
    claude.ai login) but the cache is still genuinely empty -- `halo mcp
    list`/`/mcp` must say SOMETHING, matching doctor's own wording, never
    silence."""
    from halo_harness.mcp import explain
    with _Env():
        _scoped_home()
        os.environ["HALO_CLAUDE_EXE"] = FAKE_CLAUDE
        # Eligible-LOOKING (claude installed, no definitive "not a claude.ai
        # login" signal) but no BRIDGE_TEST_CC_AUTH_STATUS at all -- the one
        # state where discovery_eligible() itself is False (so the cold-
        # start helper above is a no-op) yet unavailable_reason() is ALSO
        # None, since nothing confirms the login is absent either.
        os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)
        from halo_harness.providers.cc_models import reset_cached_claude_auth_status
        reset_cached_claude_auth_status()
        ctx.check("cache is empty", connectors_bridge.load_cache() == ([], None))
        ctx.check("unavailable_reason is None in this exact state",
                  connectors_bridge.unavailable_reason() is None)
        lines = explain.connector_lines()
        ctx.check(f"never silent -- names 'none discovered yet', got {lines}",
                  len(lines) == 1 and "none discovered yet" in lines[0])


@test
def test_halo_mcp_list_without_refresh_shows_a_cold_started_connector(ctx: Ctx):
    """The end-to-end CLI surface: a FRESH scoped home (empty cache), a
    cached auth status already saying claude.ai login (as if a prior
    launch's startup worker had primed it) -- `halo mcp list` with NO
    `--refresh` still shows the real connector, via the synchronous
    cold-start this round added, not just a placeholder line."""
    home = Path(tempfile.mkdtemp(prefix="w5-coldstart-cli-"))
    env = dict(os.environ)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    env.pop("BRIDGE_STATE_DIR", None)
    env.update({
        "BRIDGE_TEST_HOME": str(home), "PYTHONPATH": str(REPO_DIR),
        "HALO_CLAUDE_EXE": FAKE_CLAUDE,
        "FAKE_CLAUDE_CC_CONNECTORS": json.dumps(
            [{"name": "Claude Docs", "url": "https://api.anthropic.com/v1/pages/mcp", "status_text": "✔ Connected"}]),
        "FAKE_CLAUDE_CC_CONNECTOR_TOOLS": "mcp__claude_ai_Claude_Docs__batch",
        "BRIDGE_TEST_CC_AUTH_STATUS": json.dumps({"loggedIn": True, "authMethod": "claude.ai"}),
    })
    result = subprocess.run([sys.executable, "-m", "halo_harness", "mcp", "list"], env=env, cwd=str(home),
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr!r}", result.returncode == 0)
    ctx.check(f"the connector shows up WITHOUT --refresh, got stdout={result.stdout!r}",
              "Claude Docs" in result.stdout)


# ---- ConnectorTool: "tool names are learned on first use" ------------------

@test
def test_connector_tool_description_says_tool_names_learned_on_first_use(ctx: Ctx):
    from halo_harness.mcp.connectors import ConnectorInfo
    from halo_harness.tools.connector_tool import ConnectorTool
    info = ConnectorInfo(name="Claude Docs", slug="claude_docs", account_token="Claude_Docs",
                           url="https://x/mcp", host="x", status="connected", status_text="✔ Connected", tools=[])
    tool = ConnectorTool(info)
    ctx.check(f"says tool names are learned on first use, got {tool.description!r}",
              "tool names are learned on first use" in tool.description)

    info_with_tools = ConnectorInfo(name="Claude Docs", slug="claude_docs", account_token="Claude_Docs",
                                      url="https://x/mcp", host="x", status="connected", status_text="✔ Connected",
                                      tools=["batch", "create"])
    tool_with_tools = ConnectorTool(info_with_tools)
    ctx.check(f"real tool names are listed once known, got {tool_with_tools.description!r}",
              "batch" in tool_with_tools.description and "create" in tool_with_tools.description)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
