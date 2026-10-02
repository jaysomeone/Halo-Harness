"""tests.test_mcp_explain -- halo_harness/mcp/explain.py ("explain the
zero", Halo 2.0.1 gap-list brief W4b item 1): `/mcp`, `halo mcp list` and
doctor's MCP line must name every scope searched with its own count,
another directory's own `.mcp.json` when history shows one, and the
claude.ai connectors `claude` reports -- never a bare 0.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.mcp import connectors, connectors_bridge, explain

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _fresh_home() -> Path:
    return Path(tempfile.mkdtemp(prefix="halo-explain-"))


def _hermetic_child_env() -> dict:
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    return env


def _run(argv, home: Path, timeout=30):
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(home), "PYTHONPATH": str(REPO_DIR)})
    return subprocess.run([sys.executable, "-m", "halo_harness"] + argv, env=env, cwd=str(home),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


# ---- scope_rows / other_project_mcp_jsons (pure, in-process) ---------------

@test
def test_scope_rows_counts_each_scope_independently(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="explain-cwd-"))
    (cwd / ".mcp.json").write_text(json.dumps({"mcpServers": {"a": {"command": "a"}, "b": {"command": "b"}}}),
                                     encoding="utf-8")
    claude_json = {"mcpServers": {"user-one": {"command": "u"}},
                    "projects": {str(cwd): {"mcpServers": {"local-one": {"command": "l"}}}}}
    rows = explain.scope_rows(cwd=cwd, claude_json=claude_json)
    by_scope = {r["scope"]: r["count"] for r in rows}
    ctx.check(f"user scope counted, got {by_scope}", by_scope["user"] == 1)
    ctx.check(f"project-local scope counted, got {by_scope}", by_scope["project-local"] == 1)
    ctx.check(f"this directory's .mcp.json counted, got {by_scope}", by_scope["this directory's .mcp.json"] == 2)
    ctx.check("plugins/managed present even at 0", "plugins" in by_scope and "managed" in by_scope)


@test
def test_scope_summary_line_never_empty_even_with_nothing_configured(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="explain-cwd-empty-"))
    line = explain.scope_summary_line(cwd=cwd, claude_json={})
    ctx.check(f"names every scope with its own 0, got {line!r}",
              line.startswith("Searched --") and all(s in line for s in
                  ("user (", "project-local (", "this directory's .mcp.json (", "plugins (", "managed (")))


@test
def test_other_project_mcp_jsons_names_history_with_its_own_mcp_json(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="explain-cwd-a-"))
    other = Path(tempfile.mkdtemp(prefix="explain-other-serverMode-"))
    (other / ".mcp.json").write_text("{}", encoding="utf-8")
    no_config = Path(tempfile.mkdtemp(prefix="explain-other-noconfig-"))
    claude_json = {"projects": {str(cwd): {}, str(other): {}, str(no_config): {}}}
    found = explain.other_project_mcp_jsons(cwd=cwd, claude_json=claude_json)
    ctx.check(f"only the directory that actually has one, got {found}",
              len(found) == 1 and str(other) in found[0] and "loads only when halo runs there" in found[0])
    ctx.check("the CURRENT cwd itself is never listed as 'other'", not any(str(cwd) in f for f in found))


# ---- connector_lines (cache-only) -------------------------------------------

@test
def test_connector_lines_shows_cached_connectors(ctx: Ctx):
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(_fresh_home())
    try:
        info = connectors.ConnectorInfo(name="Claude Docs", slug="claude_docs", account_token="Claude_Docs",
                                          url="https://x/mcp", host="x", status="connected",
                                          status_text="✔ Connected")
        connectors_bridge.save_cache([info])
        lines = explain.connector_lines()
        ctx.check(f"one line naming the connector, got {lines}",
                  len(lines) == 1 and "Claude Docs" in lines[0] and "claude.ai connector (via claude)" in lines[0])
    finally:
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home


@test
def test_explain_lines_never_crashes_and_always_has_the_scope_line_first(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="explain-cwd-compose-"))
    lines = explain.explain_lines(cwd=cwd, claude_json={})
    ctx.check(f"at least the scope summary line, got {lines}", lines and lines[0].startswith("Searched --"))


# ---- CLI integration: halo mcp list / halo doctor --------------------------

@test
def test_cli_mcp_list_explains_the_zero(ctx: Ctx):
    home = _fresh_home()
    result = _run(["mcp", "list"], home)
    ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr!r}", result.returncode == 0)
    ctx.check("checking header still first", result.stdout.lstrip().startswith("Checking MCP server health..."))
    ctx.check("names what was searched", "Searched --" in result.stdout)
    ctx.check("names every scope", all(s in result.stdout for s in ("user (", "plugins (", "managed (")))


@test
def test_cli_doctor_mcp_lines_explain_the_zero_and_show_bridge_state(ctx: Ctx):
    home = _fresh_home()
    result = _run(["doctor"], home)
    ctx.check(f"exit 0 or 1, got {result.returncode}", result.returncode in (0, 1))
    ctx.check("MCP servers line explains scopes when empty",
              "MCP servers: none configured" in result.stdout and "Searched --" in result.stdout)
    ctx.check("a claude.ai connectors bridge line is present", "claude.ai connectors" in result.stdout)


# ---- headless "/mcp" (commands/builtins._cmd_mcp) --------------------------

@test
def test_headless_slash_mcp_explains_the_zero(ctx: Ctx):
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_mcp
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(_fresh_home())
    try:
        cwd = Path(tempfile.mkdtemp(prefix="explain-slash-mcp-"))
        facade = HeadlessFacade(cwd=cwd, claude_json={}, mcp_servers={})
        text = _cmd_mcp("", facade)
        ctx.check(f"explains the search, got {text!r}", "Searched --" in text)
        ctx.check("still reports none configured", "No MCP servers configured in this directory." in text)
    finally:
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
