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
        if registry is not None and job_id in getattr(registry, "jobs", {}):
            ok, message = registry.kill(job_id)
            return ToolResult(message, is_error=not ok)
        # H9 whole-tree review finding 32: the real Claude Code TaskStop
        # also accepts a BACKGROUND SUB-AGENT's own task_id, not just a
        # Bash shell_id -- `agent/subagent.py`'s `run_agent_call` stashes
        # that child's own (background-only) abort Event on
        # `agent_runtime.tasks[task_id]["abort_event"]` for exactly this.
        # Setting it is the SAME interrupt signal the child's own turn loop
        # already polls everywhere `self.abort.is_set()` is checked -- no
        # new interrupt machinery needed, just reaching the right Event.
        runtime = getattr(ctx, "agent_runtime", None)
        if runtime is not None:
            # H9 whole-tree review finding 27 (task-map persistence half):
            # a task_id from BEFORE a `-c` resume is only in `runtime.tasks`
            # once this runs -- see its own docstring in agent/subagent.py.
            from rolo_claude.agent.subagent import _hydrate_tasks_from_disk
            _hydrate_tasks_from_disk(runtime)
            with runtime.lock:
                task = runtime.tasks.get(job_id)
            if task is not None:
                abort_event = task.get("abort_event")
                if abort_event is None:
                    if task.get("reconstructed") and task.get("was_background"):
                        # H9 finding 27: this WAS a background task, but its
                        # `_bg_run` thread lived in a process that has since
                        # exited (a `-c` resume reconstructed this entry from
                        # disk) -- unlike a live foreground task, there is no
                        # process left for the user's Esc/Ctrl+C to reach
                        # either, so "nothing to stop" is the honest answer.
                        return ToolResult(
                            f"Task {job_id!r} was a background sub-agent from a previous run -- the process "
                            f"that owned it has already exited, so it is not running and there is nothing to "
                            f"stop.", is_error=True)
                    return ToolResult(
                        f"Task {job_id!r} is a FOREGROUND sub-agent -- it shares the main session's own "
                        f"abort and is stopped by the user's own Esc/Ctrl+C, not TaskStop.", is_error=True)
                already_done = abort_event.is_set()
                abort_event.set()
                return ToolResult(f"Task {job_id!r} {'was already stopped' if already_done else 'stop requested'}.",
                                   is_error=False)
        known = ", ".join(sorted(getattr(registry, "jobs", {}) or {})) or "(none)"
        return ToolResult(f"Unknown shell_id/task_id {job_id!r}. Known background shells: {known}", is_error=True)
