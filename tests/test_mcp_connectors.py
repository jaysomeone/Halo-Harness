"""tests.test_mcp_connectors -- halo_harness/mcp/connectors.py +
connectors_bridge.py (Halo 2.0.1 gap-list brief, "W4 MCP: claude.ai
connectors bridge"): parsing/naming, discovery against the fake `claude`
(tests/helpers/fake_claude_cc.py), caching, eligibility (never spawns
during a test -- see connectors_bridge.discovery_eligible's own docstring),
status text, and permission-rule translation. Generic only -- no connector
name here is Google (the owner dropped that 2026-10-01); "Claude Docs" and two
made-up names stand in for the three claude.ai connector states.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.mcp import connectors, connectors_bridge
from halo_harness.permissions import parse_rule

REPO_DIR = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = f'"{sys.executable}" "{REPO_DIR / "tests" / "helpers" / "fake_claude_cc.py"}"'
test, TESTS = new_registry()


class _Env:
    """Snapshots/restores every var this test module touches (house rule)."""
    KEYS = ("BRIDGE_TEST_HOME", "HALO_CLAUDE_EXE", "BRIDGE_CLAUDE_EXE", "FAKE_CLAUDE_CC_CONNECTORS",
            "FAKE_CLAUDE_CC_CONNECTOR_TOOLS", "BRIDGE_TEST_CC_AUTH_STATUS", "BRIDGE_TEST_NO_BACKGROUND_NET",
            "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")

    def __enter__(self):
        self._snap = {k: os.environ.get(k) for k in self.KEYS}
        # W6b section E fallout: tests/run_all.py's own whole-run default
        # (closing a WSL hang in an unrelated module) now leaves this set
        # ambiently for every module -- this file's own discovery tests
        # need it genuinely ABSENT to exercise "a worker actually
        # started", so it is cleared here; a test that wants it set
        # (test_background_net_disabled_flag_suppresses_discovery) does
        # so itself, same as it always has.
        os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        return self

    def __exit__(self, *exc):
        for k, v in self._snap.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _scoped_home():
    td = tempfile.mkdtemp(prefix="mcp-connectors-home-")
    os.environ["BRIDGE_TEST_HOME"] = td
    return Path(td)


# ---- naming / parsing (pure functions) -------------------------------------

@test
def test_slugify_and_account_token_and_wire_prefix(ctx: Ctx):
    ctx.check("account_token spaces->underscore", connectors.account_token("Claude Docs") == "Claude_Docs")
    ctx.check("slug is lowercase", connectors.slugify("Claude Docs") == "claude_docs")
    ctx.check("wire_prefix matches binary-facts sec.9's shape",
              connectors.wire_prefix("Claude Docs") == "claude_ai_Claude_Docs")


@test
def test_connector_specific_sanitiser_collapses_runs_of_underscore(ctx: Ctx):
    # binary-facts sec.9: "claude.ai servers collapse `_+`" -- unlike the
    # generic mcp.manager.sanitize_name, which deliberately does NOT.
    ctx.check("double-special-char run collapses to one underscore",
              connectors.account_token("Weird  (Name)") == "Weird_Name_")


@test
def test_parse_claude_mcp_list_all_three_states(ctx: Ctx):
    output = (
        "claude.ai Claude Docs: https://api.anthropic.com/v1/pages/mcp - ✔ Connected\n"
        "claude.ai Widgets: https://example.invalid/mcp/v1 - ! Needs authentication\n"
        "claude.ai Broken Co: https://broken.example/mcp - ✗ Failed to connect\n"
        "my-local-server: node index.js - ✔ Connected\n"
        "\n"
    )
    rows = connectors.parse_claude_mcp_list(output)
    ctx.check(f"exactly the 3 claude.ai rows, got {[r.name for r in rows]}",
              [r.name for r in rows] == ["Claude Docs", "Widgets", "Broken Co"])
    by_name = {r.name: r for r in rows}
    ctx.check("connected classified", by_name["Claude Docs"].status == "connected")
    ctx.check("needs_auth classified", by_name["Widgets"].status == "needs_auth")
    ctx.check("failed classified", by_name["Broken Co"].status == "failed")
    ctx.check("host parsed from the URL", by_name["Claude Docs"].host == "api.anthropic.com")
    ctx.check("non-claude.ai line is never included", "my-local-server" not in by_name)


@test
def test_group_claude_ai_tool_names(ctx: Ctx):
    names = ["mcp__claude_ai_Claude_Docs__batch", "mcp__claude_ai_Claude_Docs__create",
              "mcp__claude_ai_Widgets__list", "Read", "mcp__other__thing"]
    buckets = connectors.group_claude_ai_tool_names(names)
    ctx.check(f"two connectors bucketed, got {sorted(buckets)}", sorted(buckets) == ["Claude_Docs", "Widgets"])
    ctx.check("tool names preserved", sorted(buckets["Claude_Docs"]) == ["batch", "create"])


# ---- discovery against the fake claude -------------------------------------

@test
def test_discover_connectors_now_against_fake_claude(ctx: Ctx):
    with _Env():
        _scoped_home()
        os.environ["HALO_CLAUDE_EXE"] = FAKE_CLAUDE
        os.environ["FAKE_CLAUDE_CC_CONNECTORS"] = json.dumps([
            {"name": "Claude Docs", "url": "https://api.anthropic.com/v1/pages/mcp", "status_text": "✔ Connected"},
            {"name": "Widgets", "url": "https://example.invalid/mcp", "status_text": "! Needs authentication"},
        ])
        os.environ["FAKE_CLAUDE_CC_CONNECTOR_TOOLS"] = "mcp__claude_ai_Claude_Docs__batch,mcp__claude_ai_Widgets__list"
        found = connectors.discover_connectors_now(timeout=15.0)
        by_name = {c.name: c for c in found}
        ctx.check(f"both connectors discovered, got {sorted(by_name)}", sorted(by_name) == ["Claude Docs", "Widgets"])
        ctx.check(f"Claude Docs got its own tool, got {by_name['Claude Docs'].tools}",
                  by_name["Claude Docs"].tools == ["batch"])
        ctx.check(f"Widgets got its own tool, got {by_name['Widgets'].tools}", by_name["Widgets"].tools == ["list"])


@test
def test_discover_connectors_now_zero_when_claude_reports_none(ctx: Ctx):
    with _Env():
        _scoped_home()
        os.environ["HALO_CLAUDE_EXE"] = FAKE_CLAUDE
        os.environ.pop("FAKE_CLAUDE_CC_CONNECTORS", None)
        found = connectors.discover_connectors_now(timeout=15.0)
        ctx.check(f"empty, got {found}", found == [])


# ---- cache ------------------------------------------------------------------

@test
def test_cache_round_trip(ctx: Ctx):
    with _Env():
        home = _scoped_home()
        info = connectors.ConnectorInfo(name="Claude Docs", slug="claude_docs", account_token="Claude_Docs",
                                          url="https://x/mcp", host="x", status="connected",
                                          status_text="✔ Connected", tools=["batch"])
        connectors_bridge.save_cache([info])
        path = connectors_bridge.cache_path()
        ctx.check(f"cache written under the scoped ~/.halo, got {path}",
                  str(path).replace("\\", "/").startswith(str(home).replace("\\", "/")))
        loaded, fetched_at = connectors_bridge.load_cache()
        ctx.check(f"round-trips, got {[c.name for c in loaded]}", [c.name for c in loaded] == ["Claude Docs"])
        ctx.check("fetched_at recorded", isinstance(fetched_at, (int, float)))


# ---- eligibility: never spawns in a test, explicit refresh still works -----

@test
def test_background_discovery_never_fires_without_a_confirmed_claude_ai_login(ctx: Ctx):
    with _Env():
        _scoped_home()
        os.environ["HALO_CLAUDE_EXE"] = FAKE_CLAUDE
        os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)
        from halo_harness.providers.cc_models import reset_cached_claude_auth_status
        reset_cached_claude_auth_status()
        connectors_bridge.reset_session_state()
        started = connectors_bridge.ensure_discovered_in_background()
        ctx.check("no worker started -- the auth-status cache was never primed", started is False)
        ctx.check("nothing cached either", connectors_bridge.load_cache() == ([], None))


@test
def test_background_discovery_fires_once_a_claude_ai_login_is_confirmed(ctx: Ctx):
    with _Env():
        _scoped_home()
        os.environ["HALO_CLAUDE_EXE"] = FAKE_CLAUDE
        os.environ["FAKE_CLAUDE_CC_CONNECTORS"] = json.dumps(
            [{"name": "Claude Docs", "url": "https://x/mcp", "status_text": "✔ Connected"}])
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        connectors_bridge.reset_session_state()
        started = connectors_bridge.ensure_discovered_in_background(timeout=15.0)
        ctx.check("a worker started", started is True)
        deadline = time.monotonic() + 10.0
        found = []
        while time.monotonic() < deadline:
            found, _ = connectors_bridge.load_cache()
            if found:
                break
            time.sleep(0.05)
        ctx.check(f"the background worker populated the cache, got {found}", [c.name for c in found] == ["Claude Docs"])
        ctx.check("a second call this session is a no-op (once per session)",
                  connectors_bridge.ensure_discovered_in_background() is False)


@test
def test_background_net_disabled_flag_suppresses_discovery(ctx: Ctx):
    with _Env():
        _scoped_home()
        os.environ["HALO_CLAUDE_EXE"] = FAKE_CLAUDE
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
        os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = "1"
        connectors_bridge.reset_session_state()
        ctx.check("suppressed by the net-disabled test seam",
                  connectors_bridge.ensure_discovered_in_background() is False)


@test
def test_refresh_now_is_explicit_and_ignores_eligibility(ctx: Ctx):
    with _Env():
        _scoped_home()
        os.environ["HALO_CLAUDE_EXE"] = FAKE_CLAUDE
        os.environ["FAKE_CLAUDE_CC_CONNECTORS"] = json.dumps(
            [{"name": "Claude Docs", "url": "https://x/mcp", "status_text": "✔ Connected"}])
        os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)  # NOT eligible -- refresh_now ignores that entirely
        found = connectors_bridge.refresh_now(timeout=15.0)
        ctx.check(f"refresh_now still ran, got {[c.name for c in found]}", [c.name for c in found] == ["Claude Docs"])
        cached, _ = connectors_bridge.load_cache()
        ctx.check("and saved the result", [c.name for c in cached] == ["Claude Docs"])


# ---- status text + rule translation ----------------------------------------

@test
def test_connector_status_entry_and_format_mcp_list_line(ctx: Ctx):
    from halo_harness.mcp_cli import format_mcp_list_line
    info = connectors.ConnectorInfo(name="Claude Docs", slug="claude_docs", account_token="Claude_Docs",
                                      url="https://api.anthropic.com/v1/pages/mcp", host="api.anthropic.com",
                                      status="connected", status_text="✔ Connected", tools=["batch"])
    entry = connectors_bridge.connector_status_entry(info)
    line = format_mcp_list_line(entry)
    ctx.check(f"labelled per the brief, got {line!r}",
              line == "claude.ai connector (via claude) Claude Docs: api.anthropic.com - ✔ Connected")


@test
def test_status_line_needs_auth_names_the_exact_next_step(ctx: Ctx):
    info = connectors.ConnectorInfo(name="Widgets", slug="widgets", account_token="Widgets",
                                      url="https://x/mcp", host="x", status="needs_auth",
                                      status_text="! Needs authentication")
    line = connectors_bridge.status_line(info)
    ctx.check(f"names claude.ai and /mcp reconnect, got {line!r}",
              "claude.ai" in line and "/mcp" in line and "reconnect" in line)
    ctx.check("never claims to read claude's credentials file", "credentials file" not in line or "never reads" in line)


@test
def test_translate_claude_ai_rule_shapes(ctx: Ctx):
    cases = {
        "mcp__claude_ai_Claude_Docs__batch": "connector__claude_docs(batch)",
        "mcp__claude_ai_Claude_Docs__*": "connector__claude_docs",
        "mcp__claude_ai_Claude_Docs": "connector__claude_docs",
        "mcp__claude_ai_Widgets__list__pages": "connector__widgets(list__pages)",
        "Bash(pytest -q)": None,
        "mcp__some_other_server__tool": None,
    }
    for raw, expected in cases.items():
        got = connectors_bridge.translate_claude_ai_rule(raw)
        ctx.check(f"{raw!r} -> {expected!r}, got {got!r}", got == expected)


@test
def test_translate_rules_appends_never_replaces(ctx: Ctx):
    original = [parse_rule("mcp__claude_ai_Claude_Docs__batch", source="settings", action="deny"),
                 parse_rule("Bash(rm -rf /)", source="settings", action="deny")]
    out = connectors_bridge.translate_rules(original, action="deny")
    ctx.check("both originals still present", all(r in out for r in original))
    ctx.check(f"exactly one translated rule appended, got {[r.raw for r in out]}", len(out) == 3)
    translated = [r for r in out if r not in original][0]
    ctx.check(f"translated to the connector rule, got tool={translated.tool!r} kind={translated.kind!r}",
              translated.tool == "connector__claude_docs" and translated.kind == "exact" and translated.value == "batch")


@test
def test_unavailable_reason_when_claude_missing(ctx: Ctx):
    import halo_harness.mcp_setup as mcp_setup
    old = mcp_setup.find_claude_exe
    mcp_setup.find_claude_exe = lambda: None
    try:
        with _Env():
            _scoped_home()
            reason = connectors_bridge.unavailable_reason()
            ctx.check(f"names claude as missing, got {reason!r}", reason is not None and "not installed" in reason)
    finally:
        mcp_setup.find_claude_exe = old


@test
def test_unavailable_reason_when_gateway_driven(ctx: Ctx):
    with _Env():
        _scoped_home()
        os.environ["HALO_CLAUDE_EXE"] = FAKE_CLAUDE
        os.environ["ANTHROPIC_BASE_URL"] = "https://gateway.example.invalid"
        reason = connectors_bridge.unavailable_reason()
        ctx.check(f"names the gateway case, got {reason!r}", reason is not None and "gateway" in reason)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
