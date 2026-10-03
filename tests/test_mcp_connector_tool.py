"""tests.test_mcp_connector_tool -- halo_harness/tools/connector_tool.py
(Halo 2.0.1 gap-list brief, "W4 MCP: claude.ai connectors bridge" item 3,
"the bridge tool"): schema/description, permission_content (parenthesised
rules target one underlying connector tool), and the real spawned command
line (--allowedTools, --max-turns, the system prompt, the request/tool/args
text) against the fake `claude` (tests/helpers/fake_claude_cc.py).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.mcp.connectors import ConnectorInfo
from halo_harness.tools.base import ToolContext
from halo_harness.tools.connector_tool import ConnectorTool

REPO_DIR = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = f'"{sys.executable}" "{REPO_DIR / "tests" / "helpers" / "fake_claude_cc.py"}"'
test, TESTS = new_registry()

_CLAUDE_DOCS = ConnectorInfo(name="Claude Docs", slug="claude_docs", account_token="Claude_Docs",
                              url="https://api.anthropic.com/v1/pages/mcp", host="api.anthropic.com",
                              status="connected", status_text="✔ Connected", tools=["batch", "create", "read"])


class _Env:
    KEYS = ("BRIDGE_TEST_HOME", "HALO_CLAUDE_EXE", "FAKE_CLAUDE_CC_ARGV_LOG", "FAKE_CLAUDE_CC_JSON_MODE")

    def __enter__(self):
        self._snap = {k: os.environ.get(k) for k in self.KEYS}
        return self

    def __exit__(self, *exc):
        for k, v in self._snap.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_name_description_and_schema(ctx: Ctx):
    tool = ConnectorTool(_CLAUDE_DOCS)
    ctx.check(f"name is connector__<slug>, got {tool.name!r}", tool.name == "connector__claude_docs")
    ctx.check("description lists the connector's own tools",
              all(n in tool.description for n in ("batch", "create", "read")))
    ctx.check("schema requires 'request'", tool.input_schema.get("required") == ["request"])
    ctx.check("schema has request/tool/args",
              set(tool.input_schema["properties"]) == {"request", "tool", "args"})


@test
def test_summary_and_permission_content(ctx: Ctx):
    tool = ConnectorTool(_CLAUDE_DOCS)
    ctx.check("summary shows the tool when given",
              tool.summary({"request": "x", "tool": "batch"}) == "connector__claude_docs(batch)")
    ctx.check("summary falls back to the request", tool.summary({"request": "list my docs"}).startswith(
        "connector__claude_docs(list my docs"))
    ctx.check("permission_content is the sub-tool name", tool.permission_content({"tool": "batch"}) == "batch")
    ctx.check("permission_content is '' with no sub-tool named", tool.permission_content({"request": "x"}) == "")


@test
def test_parenthesised_rule_targets_one_sub_tool(ctx: Ctx):
    """W4 unbuilt-surfaces MCP item: "permission rules with parentheses in
    patterns accepted" -- `connector__<slug>` is an ordinary (non-`mcp__`)
    tool name, so the generic `Tool(content)` grammar already accepts a
    parenthesised pattern; this pins that a translated/hand-written
    `connector__claude_docs(batch)` rule actually PARSES (never
    "invalid", unlike an `mcp__...(...)` rule) and matches via
    `permission_content`."""
    from halo_harness.permissions import parse_rule
    rule = parse_rule("connector__claude_docs(batch)", action="ask")
    ctx.check(f"parses as an ordinary exact-content rule, got kind={rule.kind!r} error={rule.error!r}",
              rule.kind == "exact" and rule.error is None)
    ctx.check("value is the sub-tool name", rule.value == "batch")


@test
def test_requires_request(ctx: Ctx):
    tool = ConnectorTool(_CLAUDE_DOCS)
    result = tool.run({}, ToolContext(cwd=REPO_DIR))
    ctx.check("is_error", result.is_error is True)
    ctx.check("names the missing field", "request" in result.content)


@test
def test_run_spawns_the_documented_command_line_and_passes_through_the_result(ctx: Ctx):
    with _Env():
        home = Path(tempfile.mkdtemp(prefix="connector-tool-home-"))
        os.environ["BRIDGE_TEST_HOME"] = str(home)
        os.environ["HALO_CLAUDE_EXE"] = FAKE_CLAUDE
        argv_log = home / "argv.log"
        os.environ["FAKE_CLAUDE_CC_ARGV_LOG"] = str(argv_log)

        tool = ConnectorTool(_CLAUDE_DOCS, max_turns=7)
        result = tool.run({"request": "list my 3 most recent docs", "tool": "batch", "args": {"n": 3}},
                           ToolContext(cwd=REPO_DIR))

        ctx.check(f"not an error, got {result.content!r}", result.is_error is False)
        ctx.check(f"the fake's echo is passed through, got {result.content!r}",
                  "ECHO:" in result.content and "list my 3 most recent docs" in result.content)
        ctx.check("the request/tool/args text reached the prompt",
                  "batch" in result.content and '"n": 3' in result.content)

        lines = [json.loads(l) for l in argv_log.read_text(encoding="utf-8").splitlines() if l.strip()]
        call = next(l for l in lines if "--output-format" in l and "json" in l)
        ctx.check(f"-p present, got {call}", "-p" in call)
        ctx.check("--max-turns 7", call[call.index("--max-turns") + 1] == "7")
        # finding 12 (W6a): `--allowedTools` now NARROWS to the one named
        # sub-tool whenever the call gives `tool` (this call does: "batch")
        # -- it only stays the wildcard prefix when `tool` is omitted.
        ctx.check("--allowedTools names this connector's own wire prefix AND tool",
                  call[call.index("--allowedTools") + 1] == "mcp__claude_ai_Claude_Docs__batch")
        system_prompt = call[call.index("--append-system-prompt") + 1]
        ctx.check(f"system prompt names the same prefix, got {system_prompt!r}",
                  "mcp__claude_ai_Claude_Docs" in system_prompt and "tool proxy" in system_prompt.lower())


@test
def test_f12_w6a_per_tool_deny_rule_reaches_disallowed_tools_when_tool_is_omitted(ctx: Ctx):
    """finding 12 (W6a): `connector__claude_docs(batch)` only ever matched
    Halo's own permission engine when the call gave `tool` -- a call with
    just `request` was decided against EMPTY `permission_content` and
    allowed straight through, after which the inner claude could reach
    ANY of this connector's tools via `--allowedTools {prefix}__*`. Every
    per-tool deny/ask rule targeting this connector must now ALSO reach
    the inner claude's own `--disallowedTools`, regardless of whether
    `tool` was given."""
    from halo_harness.permissions import PermissionEngine, parse_rule

    with _Env():
        home = Path(tempfile.mkdtemp(prefix="connector-tool-home-f12-"))
        os.environ["BRIDGE_TEST_HOME"] = str(home)
        os.environ["HALO_CLAUDE_EXE"] = FAKE_CLAUDE
        argv_log = home / "argv.log"
        os.environ["FAKE_CLAUDE_CC_ARGV_LOG"] = str(argv_log)

        deny_rule = parse_rule("connector__claude_docs(batch)", action="deny")
        engine = PermissionEngine(deny_rules=[deny_rule], mode="auto", cwd=REPO_DIR)
        # Halo's OWN top-level decision: a call with no `tool` is allowed
        # through (empty permission_content never matches "batch") --
        # exactly the gap this finding closes downstream of here.
        decision = engine.decide("connector__claude_docs", {"request": "list my docs"}, tool=ConnectorTool(_CLAUDE_DOCS))
        ctx.check(f"Halo's own coarse decision allows it through (the documented gap), got {decision.action!r}",
                  decision.action == "allow")

        tool = ConnectorTool(_CLAUDE_DOCS)
        ctx_obj = ToolContext(cwd=REPO_DIR, permission_engine=engine)
        result = tool.run({"request": "list my docs"}, ctx_obj)
        ctx.check(f"not an error (the inner claude call itself still ran), got {result.content!r}",
                  result.is_error is False)

        lines = [json.loads(l) for l in argv_log.read_text(encoding="utf-8").splitlines() if l.strip()]
        call = next(l for l in lines if "--output-format" in l and "json" in l)
        ctx.check(f"--allowedTools stays the wildcard prefix (no tool was named), got {call}",
                  call[call.index("--allowedTools") + 1] == "mcp__claude_ai_Claude_Docs__*")
        ctx.check(f"--disallowedTools carries the per-tool deny rule, got {call}",
                  "--disallowedTools" in call
                  and call[call.index("--disallowedTools") + 1] == "mcp__claude_ai_Claude_Docs__batch")


@test
def test_run_result_is_capped_and_spilled_like_any_other_mcp_tool(ctx: Ctx):
    """"the usual MCP result caps and spill apply" (gap-list brief) --
    `cap_and_spill` itself is already pinned generically in
    tests/test_mcp_tool.py; this proves ConnectorTool.run() actually wires
    session_dir/tool_use_id through to it."""
    with _Env():
        home = Path(tempfile.mkdtemp(prefix="connector-tool-home-cap-"))
        os.environ["BRIDGE_TEST_HOME"] = str(home)
        os.environ["HALO_CLAUDE_EXE"] = FAKE_CLAUDE
        old_cap = os.environ.get("MAX_MCP_OUTPUT_TOKENS")
        os.environ["MAX_MCP_OUTPUT_TOKENS"] = "10"  # a tiny 40-char cap
        try:
            tool = ConnectorTool(_CLAUDE_DOCS)
            ctx_obj = ToolContext(cwd=REPO_DIR, session_dir=home / "session", tool_use_id="toolu_cap_test")
            result = tool.run({"request": "x" * 500}, ctx_obj)
            ctx.check(f"truncated, got len={len(result.content)}", len(result.content) < 500)
            ctx.check("Claude Code's own truncation string is present", "OUTPUT TRUNCATED" in result.content)
            spill_path = home / "session" / "tool-results" / "toolu_cap_test.txt"
            ctx.check(f"full output spilled to disk, got exists={spill_path.exists()}", spill_path.exists())
            ctx.check("the spilled file has the FULL untruncated text",
                      "x" * 500 in spill_path.read_text(encoding="utf-8"))
        finally:
            if old_cap is None:
                os.environ.pop("MAX_MCP_OUTPUT_TOKENS", None)
            else:
                os.environ["MAX_MCP_OUTPUT_TOKENS"] = old_cap


@test
def test_run_reports_a_claude_side_error_cleanly(ctx: Ctx):
    with _Env():
        home = Path(tempfile.mkdtemp(prefix="connector-tool-home-"))
        os.environ["BRIDGE_TEST_HOME"] = str(home)
        os.environ["HALO_CLAUDE_EXE"] = FAKE_CLAUDE
        os.environ["FAKE_CLAUDE_CC_JSON_MODE"] = "error"
        tool = ConnectorTool(_CLAUDE_DOCS)
        result = tool.run({"request": "do something"}, ToolContext(cwd=REPO_DIR))
        ctx.check("is_error", result.is_error is True)
        ctx.check(f"the fake's own error text is passed through, got {result.content!r}",
                  "fake connector error" in result.content)


@test
def test_missing_claude_is_a_clean_error_not_a_crash(ctx: Ctx):
    with _Env():
        os.environ["BRIDGE_TEST_HOME"] = tempfile.mkdtemp(prefix="connector-tool-home-")
        os.environ["HALO_CLAUDE_EXE"] = '"/does/not/exist/claude"'
        import halo_harness.providers.cc_models as cc_models

        def _raise():
            from halo_harness.providers.cc_models import ClaudeCodeNotFoundError
            raise ClaudeCodeNotFoundError("claude executable not found")
        old = cc_models.resolve_claude_launch_argv
        cc_models.resolve_claude_launch_argv = _raise
        try:
            tool = ConnectorTool(_CLAUDE_DOCS)
            result = tool.run({"request": "x"}, ToolContext(cwd=REPO_DIR))
            ctx.check("is_error, never a crash", result.is_error is True)
            ctx.check("names the real reason", "claude executable not found" in result.content)
        finally:
            cc_models.resolve_claude_launch_argv = old


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
