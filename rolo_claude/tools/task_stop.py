"""rolo_claude.tools.task_stop -- the TaskStop tool (H8 scope A): stops a
running background Bash shell. See agent/jobs.py's module docstring for the
binary evidence behind this name (Claude Code 2.1.282 unified the old
`KillShell`/`KillBash` tools into one current `TaskStop`, `task_id` primary,
`shell_id` kept only as a documented-deprecated input alias).
"""

from __future__ import annotations

from rolo_claude.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = (
    "Stops a running background task by its ID.\n"
    "- Takes a task_id parameter identifying the task to stop (a shell_id from Bash/BashOutput also "
    "works -- shell_id is kept only as a deprecated alias for task_id)\n"
    "- Returns a success or failure status\n"
    "- Use this tool when you need to terminate a long-running background shell before it finishes on "
    "its own"
)


class TaskStopTool(Tool):
    name = "TaskStop"
    description = DESCRIPTION
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The ID of the background task to stop"},
            "shell_id": {"type": "string", "description": "Deprecated: use task_id instead"},
        },
    }

    def summary(self, input: dict) -> str:
        input = input if isinstance(input, dict) else {}
        return f"TaskStop({input.get('task_id') or input.get('shell_id') or ''})"

    def permission_content(self, input: dict) -> str:
        input = input if isinstance(input, dict) else {}
        return input.get("task_id") or input.get("shell_id") or ""

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        input = input if isinstance(input, dict) else {}
        job_id = input.get("task_id") or input.get("shell_id")
        if not job_id or not isinstance(job_id, str):
            return ToolResult("Either task_id or shell_id is required", is_error=True)
        registry = getattr(ctx, "job_registry", None)
        if registry is None:
            return ToolResult("No background shells are available in this session.", is_error=True)
        ok, message = registry.kill(job_id)
        return ToolResult(message, is_error=not ok)
