"""halo_harness.tools.todowrite -- the TodoWrite tool (H2 scope A): a
structured, session-visible task list. Each call REPLACES the whole list
(never merges); the tool validates shape (every item has content/status/
activeForm, status is one of pending/in_progress/completed, at most one
in_progress at a time) and echoes the rendered list back so both the model
and a transcript reader can see current state.
"""

from __future__ import annotations

from halo_harness.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = (
    "Use this tool to create and manage a structured task list for the current session. This helps "
    "track progress, organize complex multi-step work, and give the user visibility into what you "
    "are doing.\n\n"
    "Use it proactively for any non-trivial multi-step task; skip it for a single, trivial action. "
    "Mark a task completed IMMEDIATELY after finishing it -- never batch completions at the end. "
    "Keep at most ONE task `in_progress` at a time. Each call REPLACES the entire list with the one "
    "you send, so always send the full current list, not just what changed."
)

_VALID_STATUS = ("pending", "in_progress", "completed")
_BOX = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


class TodoWriteTool(Tool):
    name = "TodoWrite"
    description = DESCRIPTION
    input_schema = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "The full, up to date todo list",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": list(_VALID_STATUS)},
                        "activeForm": {"type": "string", "description": "Present-tense form shown while this item is in_progress"},
                    },
                    "required": ["content", "status", "activeForm"],
                },
            },
        },
        "required": ["todos"],
    }

    def summary(self, input: dict) -> str:
        todos = input.get("todos") if isinstance(input, dict) else None
        return f"TodoWrite({len(todos) if isinstance(todos, list) else 0} items)"

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        todos = input.get("todos") if isinstance(input, dict) else None
        if not isinstance(todos, list):
            return ToolResult("The todos parameter must be a list", is_error=True)

        in_progress = 0
        lines = []
        for i, item in enumerate(todos):
            if not isinstance(item, dict):
                return ToolResult(f"todos[{i}] must be an object", is_error=True)
            content = item.get("content")
            status = item.get("status")
            active_form = item.get("activeForm")
            if not content or not isinstance(content, str):
                return ToolResult(f"todos[{i}].content is required and must be a string", is_error=True)
            if status not in _VALID_STATUS:
                return ToolResult(f"todos[{i}].status must be one of {list(_VALID_STATUS)}, got {status!r}", is_error=True)
            if not active_form or not isinstance(active_form, str):
                return ToolResult(f"todos[{i}].activeForm is required and must be a string", is_error=True)
            if status == "in_progress":
                in_progress += 1
            lines.append(f"{_BOX[status]} {content}")

        if in_progress > 1:
            return ToolResult(f"At most one todo may be in_progress at a time, got {in_progress}", is_error=True)

        if isinstance(getattr(ctx, "bash_state", None), dict):
            ctx.bash_state["todos"] = todos
        return ToolResult("Todos updated:\n" + ("\n".join(lines) if lines else "(empty list)"))
