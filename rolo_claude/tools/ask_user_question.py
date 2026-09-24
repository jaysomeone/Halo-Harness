"""rolo_claude.tools.ask_user_question -- the AskUserQuestion tool (H2
scope A). This build only ever drives print mode (`-p`) -- there is no
interactive UI (U2) yet for a real `question` event/reply round trip -- so
per the brief's own contract ("print mode -> error result") this tool's
ONLY implemented behavior right now IS that error path; a future
interactive loop can special-case the tool_use BEFORE dispatch (emit
`events.Event("question", ...)` and wait for a `question_reply` command)
rather than ever reaching this run() at all.
"""

from __future__ import annotations

from rolo_claude.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = (
    "Ask the user a clarifying question, with a small set of suggested options, when you are "
    "genuinely blocked on a decision only they can make. Not available in print mode (`-p`) -- "
    "there is no one to answer there, so the call returns an error; a future interactive session "
    "will show the question and wait for a reply instead."
)


class AskUserQuestionTool(Tool):
    name = "AskUserQuestion"
    description = DESCRIPTION
    input_schema = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question to ask the user"},
            "options": {"type": "array", "items": {"type": "string"}, "description": "Suggested answers, if any"},
        },
        "required": ["question"],
    }

    def summary(self, input: dict) -> str:
        q = input.get("question", "") if isinstance(input, dict) else ""
        return f"AskUserQuestion({q[:60]})"

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        question = input.get("question") if isinstance(input, dict) else None
        if not question or not isinstance(question, str):
            return ToolResult("The question parameter is required", is_error=True)
        return ToolResult(
            "AskUserQuestion is not available in this session (print mode has no user to answer "
            "interactively) -- proceed using your best judgment, and state any assumption clearly "
            "in your final answer instead of waiting for a reply.",
            is_error=True,
        )
