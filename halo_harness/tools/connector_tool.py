"""halo_harness.tools.connector_tool -- ConnectorTool (Halo 2.0.1 "W4 MCP:
claude.ai connectors bridge", item 3): one Halo tool per claude.ai
connector (`connector__<slug>`), proxying a request through a headless
`claude -p` scoped to that one connector's own tools via `--allowedTools`.
Halo's permission engine gates every call exactly like any other tool --
this bridge itself never refuses a connector call, it runs it.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from halo_harness.mcp import connectors as mcp_connectors
from halo_harness.mcp import connectors_bridge
from halo_harness.tools.base import Tool, ToolContext, ToolResult
from halo_harness.tools.mcp_tool import cap_and_spill

DEFAULT_WALL_CLOCK_TIMEOUT_S = 120.0

_SYSTEM_PROMPT_TEMPLATE = (
    "You are a tool proxy. Using only the {prefix}__* connector tools, perform exactly the "
    "request below and return the results verbatim as JSON, with no commentary."
)


def _claude_subprocess_env() -> dict:
    from halo_harness.providers.config import cc_child_env
    return cc_child_env(dict(os.environ))


def _extract_result_text(stdout: str, returncode: int) -> "tuple[str, bool]":
    """Mirrors `agent.cc_runtime.one_shot_cc_call`'s own `--output-format
    json` parsing (a `{"result": ..., "is_error": ...}` object) -- kept as
    its own small function here since the bridge's error/timeout/missing-
    claude paths need to build a ToolResult directly, never raise."""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        text = stdout.strip() or f"connector call exited {returncode} with no parseable output"
        return text, True
    if not isinstance(data, dict):
        return str(data), bool(returncode)
    if data.get("is_error"):
        return str(data.get("result") or "connector call failed"), True
    result = data.get("result")
    text = result if isinstance(result, str) else json.dumps(data, ensure_ascii=False, default=str)
    cost = data.get("total_cost_usd")
    duration_ms = data.get("duration_ms")
    if isinstance(cost, (int, float)) or isinstance(duration_ms, (int, float)):
        text += f"\n\n(connector call: ${cost or 0:.4f}, {duration_ms or 0:.0f} ms, via the claude.ai subscription)"
    return text, False


class ConnectorTool(Tool):
    """`info` is a `mcp.connectors.ConnectorInfo` snapshot (tools/status as
    of the last discovery). `sdk_tool`/`server_name` let an instance ALSO
    sit in `agent.catalog.SessionCatalog.deferred` as a `(None,
    tool-instance)` entry -- see that module's own `server is None` branch
    -- reusing its preload/defer/LRU/ToolSearch machinery unchanged."""

    is_read_only = False  # unknown in general -- a connector tool may write (send mail, create a doc, ...)
    result_cap = None     # manages its own cap via cap_and_spill, like McpTool

    def __init__(self, info: "mcp_connectors.ConnectorInfo", *, max_turns: Optional[int] = None) -> None:
        self.info = info
        self.slug = info.slug
        self.name = f"connector__{info.slug}"
        # W5 (carried from W4b): seen live on the Kali VM -- the cached
        # `tools` list was empty because the one-shot `claude -p
        # --output-format stream-json` init-line discovery never captured
        # any `mcp__claude_ai_<name>__<tool>` names for this connector (a
        # slow/odd claude.ai response, or discovery simply hasn't run yet).
        # Says WHEN they'll show up (the next real call, not some separate
        # background step the user has to wait on or trigger) rather than
        # leaving "not yet discovered" sounding like a stuck state.
        tool_list = ", ".join(sorted(info.tools)) if info.tools else "(tool names are learned on first use)"
        self.description = (
            f'Proxy a request to the claude.ai "{info.name}" connector through Claude Code. '
            f"Describe what to do in plain words; `tool` optionally names one of this connector's "
            f"own tools to call directly, with `args` for it. Connector tools: {tool_list}."
        )
        self.input_schema = {
            "type": "object",
            "properties": {
                "request": {"type": "string", "description": "What to do, in plain words."},
                "tool": {"type": "string", "description": "Optional: one of this connector's own tool names."},
                "args": {"type": "object", "description": "Optional: arguments for that tool."},
            },
            "required": ["request"],
        }
        self.max_turns = max_turns if max_turns else connectors_bridge.connector_max_turns(info.slug)
        self.meta = {}
        self.sdk_tool = self      # catalog.py eviction/search sentinel (server=None means "already a Tool")
        self.server_name = None

    def always_load(self) -> bool:
        return connectors_bridge.connector_always_load(self.slug)

    def summary(self, input: dict) -> str:
        data = input if isinstance(input, dict) else {}
        subject = data.get("tool") or data.get("request") or ""
        return f"{self.name}({str(subject)[:80]})"

    def permission_content(self, input: dict) -> str:
        """A parenthesised `connector__<slug>(<tool>)` rule targets one
        specific underlying connector tool -- the bare tool name is what
        `mcp.connectors_bridge.translate_claude_ai_rule` produces from a
        Claude Code rule, and the most useful thing a hand-written one
        could match on too.

        finding 12 (W6a): a call that OMITS `tool` (the model describing
        the request in plain words, which `request` alone never updates
        this to match) has NOTHING here to match a per-tool rule against
        at all -- `run` below additionally pushes every per-tool deny/ask
        rule straight into the inner `claude -p`'s own `--disallowedTools`,
        so that call still cannot reach the denied tool even when Halo's
        own (necessarily coarse) decision here allowed the call through."""
        data = input if isinstance(input, dict) else {}
        return data.get("tool") or ""

    def _per_tool_rules_as_mcp_names(self, ctx: ToolContext, prefix: str) -> "list[str]":
        """finding 12 (W6a): every deny/ask rule targeting THIS connector
        tool by a SPECIFIC sub-tool name (`connector__<slug>(<tool>)`,
        never a bare whole-tool rule -- that already denies the call
        before `run` is ever reached) translated into the inner claude's
        own `mcp__claude_ai_<Name>__<tool>` naming, for `--disallowedTools`.
        Best-effort: no permission_engine attached (a bare ToolContext in
        a test) just means nothing is added."""
        engine = getattr(ctx, "permission_engine", None)
        if engine is None:
            return []
        names: "list[str]" = []
        for rule in list(getattr(engine, "deny_rules", None) or []) + list(getattr(engine, "ask_rules", None) or []):
            if getattr(rule, "tool", None) != self.name or getattr(rule, "kind", None) == "bare":
                continue
            value = getattr(rule, "value", None)
            if isinstance(value, str) and value:
                name = f"{prefix}__{value}"
                if name not in names:
                    names.append(name)
        return names

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        data = input if isinstance(input, dict) else {}
        request = (data.get("request") or "").strip()
        if not request:
            return ToolResult(f"{self.name}: 'request' is required.", is_error=True)
        tool = data.get("tool")
        args = data.get("args") if isinstance(data.get("args"), dict) else None

        from halo_harness.providers.cc_models import ClaudeCodeNotFoundError, resolve_claude_launch_argv
        try:
            argv = resolve_claude_launch_argv()
        except ClaudeCodeNotFoundError as e:
            return ToolResult(f"{self.name}: {e}", is_error=True)

        prefix = f"mcp__{mcp_connectors.wire_prefix(self.info.name)}"
        prompt_parts = [f"Request: {request}"]
        if tool:
            prompt_parts.append(f"Use the connector tool named (or ending in) {tool!r}.")
        if args:
            prompt_parts.append(f"Arguments: {json.dumps(args, ensure_ascii=False)}")
        # finding 12 (W6a): a per-tool deny/ask rule (`connector__<slug>
        # (<tool>)`) only ever matches when the MODEL happens to pass
        # `tool` -- a call with just `request` is decided against empty
        # permission_content and allowed through, after which the inner
        # claude could reach ANY of this connector's tools via
        # `--allowedTools {prefix}__*`. Every such rule is now ALSO
        # pushed into the inner claude's own `--disallowedTools`, and
        # `--allowedTools` itself narrows to the one named tool whenever
        # the model DOES give one.
        disallowed = self._per_tool_rules_as_mcp_names(ctx, prefix)
        allowed_tools_value = f"{prefix}__{tool}" if tool else f"{prefix}__*"
        full_argv = argv + [
            # review finding 29: without this, every connector__* call adds
            # a "Request: ..." entry to Claude Code's own /resume list --
            # this inner claude is a one-shot tool call, never a
            # conversation worth resuming.
            "-p", "--output-format", "json", "--max-turns", str(self.max_turns), "--no-session-persistence",
            "--allowedTools", allowed_tools_value,
        ] + (["--disallowedTools", ",".join(disallowed)] if disallowed else []) + [
            "--append-system-prompt", _SYSTEM_PROMPT_TEMPLATE.format(prefix=prefix),
            "\n".join(prompt_parts),
        ]
        # finding 11 (W6a): a plain blocking `subprocess.run(..., timeout=)`
        # never read `ctx.abort` at all (Esc did nothing for up to 120s on
        # any connector__* call) and, on timeout, killed only the direct
        # child -- on Windows that's commonly the npm `claude.CMD` shim,
        # orphaning the real `claude.exe` underneath it. `tools._proc.
        # run_streamed` is the SAME runner the Bash tool uses: its own
        # process group (Job object/CREATE_NEW_PROCESS_GROUP on Windows,
        # start_new_session on POSIX), polled against `ctx.abort`, with
        # the WHOLE group killed on abort or timeout.
        from halo_harness.tools._proc import run_streamed
        # review finding 29: `os.getcwd()` is the HALO PROCESS's own cwd,
        # not this session's -- under `--cwd <other-project>` the inner
        # claude read `<other-project>`'s own settings/.mcp.json instead of
        # the session's.
        output, exit_code, timed_out, aborted = run_streamed(
            full_argv, cwd=ctx.cwd, env=_claude_subprocess_env(),
            timeout_s=DEFAULT_WALL_CLOCK_TIMEOUT_S, abort=ctx.abort,
        )
        if aborted:
            return ToolResult(f"{self.name}: interrupted.", is_error=True)
        if timed_out:
            return ToolResult(f"{self.name}: timed out after {DEFAULT_WALL_CLOCK_TIMEOUT_S:.0f}s.", is_error=True)
        if exit_code is None:
            return ToolResult(f"{self.name}: failed to start claude: {output}", is_error=True)

        text, is_error = _extract_result_text(output, exit_code)
        blocks = cap_and_spill([{"type": "text", "text": text}], session_dir=getattr(ctx, "session_dir", None),
                                 tool_use_id=getattr(ctx, "tool_use_id", None))
        return ToolResult("".join(b.get("text", "") for b in blocks), is_error=is_error)
