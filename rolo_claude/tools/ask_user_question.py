"""rolo_claude.tools.ask_user_question -- the AskUserQuestion tool.

H6 scope F (OpenCode adopt items, U2/H3b review must-do): the wire schema
is Claude Code's OWN `questions: [{question, header, options: [{label,
description}], multiSelect}]` shape -- NOT OpenCode's single flat
`question`/`options: [str]` shape (explicitly rejected by the review).
`tui/widgets/cards.py`'s `QuestionCard` already renders both this list
shape and the OLD flat single-question shape this module used to advertise
before H6; `input_schema` below only documents the new shape, but `run()`
still accepts either at the object level so an older-format tool_use
(hand-built in a test, or a model that learned the old shape from earlier
context) never just errors out on shape alone.

This build only ever drives print mode (`-p`) directly through `run()` --
an interactive session never reaches this method at all (agent/loop.py's
`_resolve_tool_call` special-cases `name == "AskUserQuestion" and self.
interactive`, emitting a real `question` event and waiting for the UI's
reply instead) -- so per the brief's own contract ("print mode -> error
result") `run()`'s ONLY implemented behavior is that error path.
"""

from __future__ import annotations

from rolo_claude.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = (
    "Ask the user one or more clarifying questions, each with a short list of suggested options, when "
    "you are genuinely blocked on a decision only they can make. Set multiSelect on a question where "
    "more than one option may apply. Not available in print mode (`-p`) -- there is no one to answer "
    "there, so the call returns an error; an interactive session shows the question(s) and waits for a "
    "reply instead."
)

_OPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "description": "The short option text shown to the user"},
        "description": {"type": "string", "description": "Optional extra detail shown alongside the label"},
    },
    "required": ["label"],
}

_QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "The question to ask the user"},
        "header": {"type": "string", "description": "A short (<=12 char) label/category for this question"},
        "options": {"type": "array", "items": _OPTION_SCHEMA, "description": "Suggested answers"},
        "multiSelect": {"type": "boolean", "description": "Whether more than one option may be chosen"},
    },
    "required": ["question", "options"],
}

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array", "items": _QUESTION_SCHEMA,
            "description": "One or more questions to ask the user, each with its own options",
        },
    },
    "required": ["questions"],
}


def _normalize_questions(input: dict) -> "list":
    """Accepts the current `questions: [...]` shape AND the pre-H6 flat
    `{question, options: [str]}` shape (never breaks an older caller/test)
    -- returns a list of `{question, header, options, multiSelect}` dicts,
    `[]` when nothing usable was given."""
    questions = input.get("questions")
    if isinstance(questions, list) and questions:
        out = []
        for q in questions:
            if isinstance(q, dict) and q.get("question"):
                out.append(q)
        return out
    if input.get("question"):
        return [{"question": input["question"], "options": input.get("options"), "header": input.get("header")}]
    return []


class AskUserQuestionTool(Tool):
    name = "AskUserQuestion"
    description = DESCRIPTION
    input_schema = INPUT_SCHEMA

    def summary(self, input: dict) -> str:
        input = input if isinstance(input, dict) else {}
        questions = _normalize_questions(input)
        first = questions[0]["question"] if questions else ""
        more = f" (+{len(questions) - 1} more)" if len(questions) > 1 else ""
        return f"AskUserQuestion({first[:60]}{more})"

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        input = input if isinstance(input, dict) else {}
        if not _normalize_questions(input):
            return ToolResult("The questions parameter is required (at least one question)", is_error=True)
        return ToolResult(
            "AskUserQuestion is not available in this session (print mode has no user to answer "
            "interactively) -- proceed using your best judgment, and state any assumption clearly "
            "in your final answer instead of waiting for a reply.",
            is_error=True,
        )
