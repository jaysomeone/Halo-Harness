"""tests.test_mcp_serve -- `halo mcp serve` (Halo 2.0.1 gap-list brief,
"W4 MCP" item 2, "the ccbridge code is the base"): Halo's own built-in
tools exposed as a standalone stdio MCP server. Run as a real subprocess
and driven by a real MCP client (McpManager), exactly like
tests/helpers/fake_mcp_server.py's own servers.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_mcp_server import running_manager
from halo_harness.mcp.manager import McpServerConfig

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _serve_config(cwd: Path) -> dict:
    return {"halo-serve": McpServerConfig(name="halo-serve", type="stdio", command=sys.executable,
                                            args=["-m", "halo_harness.mcp.serve", str(cwd)])}


@test
def test_serve_lists_the_curated_tool_subset(ctx: Ctx):
    with running_manager(_serve_config(REPO_DIR)) as mgr:
        status = next(s for s in mgr.status() if s["name"] == "halo-serve")
        ctx.check(f"connected, got {status}", status["state"] == "connected")
        tool_names = {t[1] for t in mgr.all_tools()}
        for expected in ("mcp__halo-serve__Read", "mcp__halo-serve__Bash", "mcp__halo-serve__Write",
                          "mcp__halo-serve__Edit", "mcp__halo-serve__Glob", "mcp__halo-serve__Grep"):
            ctx.check(f"{expected} exposed, got {sorted(tool_names)}", expected in tool_names)
        for excluded in ("mcp__halo-serve__Agent", "mcp__halo-serve__AskUserQuestion",
                          "mcp__halo-serve__ToolSearch", "mcp__halo-serve__TaskStop"):
            ctx.check(f"{excluded} is never exposed (needs a live session/agent runtime)",
                      excluded not in tool_names)


@test
def test_serve_read_tool_actually_works(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="halo-serve-cwd-"))
    sample = cwd / "hello.txt"
    sample.write_text("hello from halo mcp serve\n", encoding="utf-8")
    with running_manager(_serve_config(cwd)) as mgr:
        result = mgr.call("halo-serve", "Read", {"file_path": str(sample)})
        text = "".join(getattr(c, "text", "") or "" for c in (result.content or []))
        ctx.check(f"real file content came back, got {text!r}", "hello from halo mcp serve" in text)
        ctx.check("not an error", not bool(getattr(result, "is_error", False)))


@test
def test_serve_write_tool_actually_writes(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="halo-serve-cwd-write-"))
    target = cwd / "out.txt"
    with running_manager(_serve_config(cwd)) as mgr:
        result = mgr.call("halo-serve", "Write", {"file_path": str(target), "content": "written via mcp serve\n"})
        ctx.check("not an error", not bool(getattr(result, "is_error", False)))
        ctx.check(f"the file really exists with the right content, got {target.exists()}", target.exists())
        ctx.check("content matches", target.read_text(encoding="utf-8") == "written via mcp serve\n")


@test
def test_serve_tool_crash_is_a_clean_mcp_error_not_a_dead_server(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="halo-serve-cwd-crash-"))
    with running_manager(_serve_config(cwd)) as mgr:
        result = mgr.call("halo-serve", "Read", {"file_path": str(cwd / "does-not-exist.txt")})
        ctx.check("a missing file is reported as an error result", bool(getattr(result, "is_error", False)))
        # the server process must still be alive for a SECOND call right after
        again = mgr.call("halo-serve", "Read", {"file_path": str(cwd)})
        ctx.check(f"server survives a tool-level error, got state={mgr.status()}",
                  next(s for s in mgr.status() if s["name"] == "halo-serve")["state"] == "connected")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
