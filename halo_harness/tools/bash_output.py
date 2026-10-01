"""halo_harness.tools.bash_output -- the BashOutput tool (H8 scope A): polls
a backgrounded Bash shell's incremental output. See agent/jobs.py's module
docstring for why this harness registers `BashOutput` (rather than the
newer, agent-team-flavoured `TaskOutput` name also live in Claude Code
2.1.282's own binary) for this specific job.
"""

from __future__ import annotations

from halo_harness.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = (
    "Retrieves output from a running or completed background bash shell (one started with Bash's "
    "run_in_background:true, or a foreground command that was moved to the background after its own "
    "timeout).\n"
    "- Takes a shell_id parameter identifying the shell (shown when the shell was started, or via "
    "/tasks)\n"
    "- Always returns only new output since the last time you checked that shell\n"
    "- Returns stdout and stderr output along with shell status (running/completed/killed) and, once "
    "it is no longer running, its exit code\n"
    "- Supports optional regex filtering to show only lines matching a pattern -- this does NOT limit "
    "what the shell itself produces or what is captured, only what this call hands back to you\n"
    "- Use this tool when you need to monitor or check on a long-running background shell"
)


class BashOutputTool(Tool):
    name = "BashOutput"
    description = DESCRIPTION
    is_read_only = True
    result_cap = 30_000
    input_schema = {
        "type": "object",
        "properties": {
            "shell_id": {"type": "string", "description": "The ID of the background shell to retrieve output from"},
            "filter": {"type": "string", "description": "Optional regular expression to filter the output lines "
                                                          "you receive (only matching lines are included)"},
        },
        "required": ["shell_id"],
    }

    def summary(self, input: dict) -> str:
        input = input if isinstance(input, dict) else {}
        return f"BashOutput({input.get('shell_id', '')})"

    def permission_content(self, input: dict) -> str:
        return input.get("shell_id", "") if isinstance(input, dict) else ""

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        input = input if isinstance(input, dict) else {}
        shell_id = input.get("shell_id")
        if not shell_id or not isinstance(shell_id, str):
            return ToolResult("The shell_id parameter is required", is_error=True)
        registry = getattr(ctx, "job_registry", None)
        if registry is None:
            return ToolResult("No background shells are available in this session.", is_error=True)
        filt = input.get("filter")
        text, err = registry.poll(shell_id, filter_regex=filt if isinstance(filt, str) and filt else None)
        if err is not None:
            return ToolResult(err, is_error=True)
        return ToolResult(text)
