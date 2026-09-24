"""rolo_claude.tui.widgets.cards -- inline prompt cards (D-TUI scope D):
`ToolCard`, `PermissionCard`, `QuestionCard`, `PlanCard`, plus the small
`PagerScreen` modal `o` opens from a focused `ToolCard`. Every card is
`can_focus=True` and owns its own `BINDINGS` -- `app.py` gives it focus the
moment it's mounted, which is what makes its number-key/letter shortcuts
win over PromptInput (a focused `TextArea` would otherwise swallow every
printable key itself). A card that needs free-text follow-up ("tell Claude
what to do differently", "Other...", plan feedback) never grows its own
text widget -- it asks the App to temporarily re-focus PromptInput and
deliver the next Enter-submit back to it (`app.borrow_input(card)`).
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static

STATUS_GLYPHS = {"running": "●", "ok": "✓", "error": "✗", "skipped": "⊘"}


class PagerScreen(ModalScreen):
    """A full-screen scrollable view of one tool call's complete,
    untruncated output (`o` on a focused ToolCard)."""

    BINDINGS = [Binding("escape,q,o", "dismiss_pager", "Close", show=False)]
    DEFAULT_CSS = """
    PagerScreen { align: center middle; }
    PagerScreen > Static { width: 90%; height: 90%; border: round $primary; padding: 1 2;
        background: $surface; overflow-y: auto; }
    """

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self):
        yield Static(f"{self._title}\n\n{self._body}", markup=False)

    def action_dismiss_pager(self) -> None:
        self.dismiss()


def _truncate_lines(text: str, n: int) -> "tuple[str, int]":
    lines = text.splitlines() or [""]
    if len(lines) <= n:
        return text, 0
    return "\n".join(lines[:n]), len(lines) - n


class ToolCard(Static, can_focus=True):
    """Keyed by `tool_use_id`. `header` is already the fully-formed one-line
    summary (`"⏺ Bash(git status -sb)"` -- app.py builds it via the
    tool registry's own `summary()`/`permission_content()` so this widget
    never needs a registry reference itself)."""

    BINDINGS = [Binding("o", "open_pager", "Pager", show=False)]

    def __init__(self, *, tool_use_id: str, header: str) -> None:
        super().__init__("", markup=False, classes="tool-card tool-running")
        self.tool_use_id = tool_use_id
        self.header = header
        self.body_text = ""
        self.status = "running"
        self.expanded = False
        self._refresh()

    def append_progress(self, chunk: str) -> None:
        self.body_text += chunk
        self._refresh()

    def set_result(self, *, ok: bool, summary: str, content: Optional[str] = None) -> None:
        # review finding 15: prefer the fuller `content` (still capped
        # upstream, never the raw uncapped tool output) over the 200-char
        # `summary` for the card's OWN body/pager text -- the old
        # unconditional `self.body_text = summary` is what made Ctrl+O
        # ("complete, untruncated output") never show more than 200 chars.
        self.status = "ok" if ok else "error"
        self.body_text = content if content is not None else summary
        self.remove_class("tool-running")
        self.add_class("tool-ok" if ok else "tool-error")
        self._refresh()

    def set_skipped(self, reason: str) -> None:
        self.status = "skipped"
        self.body_text = reason
        self.remove_class("tool-running")
        self._refresh()

    def set_verbose(self, expanded: bool) -> None:
        self.expanded = expanded
        self._refresh()

    def action_open_pager(self) -> None:
        self.app.push_screen(PagerScreen(self.header, self.body_text or "(no output yet)"))

    def _refresh(self) -> None:
        glyph = STATUS_GLYPHS[self.status]
        spinner = " ⋯" if self.status == "running" else ""
        if self.expanded:
            body, extra = self.body_text, 0
        else:
            body, extra = _truncate_lines(self.body_text, 3)
        hint = f"\n  … +{extra} lines (ctrl+o, or o for a pager)" if extra else ""
        indented = "\n".join(f"  {line}" for line in body.splitlines())
        self.update(f"{self.header}{spinner}\n  {glyph}\n{indented}{hint}")


def _summarize_call(name: str, input_data: dict) -> str:
    try:
        body = json.dumps(input_data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        body = str(input_data)
    return f"{name}({body[:80]})" if len(body) <= 80 else f"{name}({body[:80]}...)"


class PermissionCard(Static, can_focus=True):
    """`on_decide(decision: dict)` fires exactly once with
    `{"action": "allow"|"deny", "scope": "once"|"session"|"always"|None,
    "rule": str|None, "message": str}`. `4`/`n`/`escape` doesn't resolve
    immediately -- it asks the App to borrow PromptInput for one line of
    optional feedback first (`resolve_deny_with_message`)."""

    BINDINGS = [
        Binding("1,y", "choose_once", "Yes", show=False),
        Binding("2,a", "choose_session", "Yes, session", show=False),
        Binding("3", "choose_always", "Yes, always", show=False),
        Binding("4,n,escape", "choose_deny", "No", show=False),
    ]

    def __init__(self, *, request_id: str, summary: str, reason: str,
                 suggested_rule: Optional[str], on_decide: Callable) -> None:
        super().__init__("", markup=False, classes="permission-card")
        self.request_id = request_id
        self.summary = summary
        self.reason = reason
        self.suggested_rule = suggested_rule
        self._on_decide = on_decide
        self.awaiting_feedback = False
        self.done = False
        self._refresh()

    def _refresh(self) -> None:
        lines = [f"⚠ Permission needed: {self.summary}"]
        if self.reason:
            lines.append(f"  {self.reason}")
        if self.suggested_rule:
            lines.append(f"  suggested rule: {self.suggested_rule}")
        if self.done:
            pass
        elif self.awaiting_feedback:
            lines.append("  Tell Claude what to do differently, or press Enter to skip.")
        else:
            lines.append("  [1] Yes   [2] Yes, session   [3] Yes, always   [4] No")
        self.update("\n".join(lines))

    def _finish(self, decision: dict) -> None:
        self.done = True
        self.awaiting_feedback = False
        outcome = {"allow": "allowed", "deny": "denied"}.get(decision["action"], decision["action"])
        self.summary = f"{self.summary} -- {outcome}"
        self._refresh()
        self._on_decide(decision)

    def action_choose_once(self) -> None:
        if not self.done:
            self._finish({"action": "allow", "scope": "once", "rule": None, "message": ""})

    def action_choose_session(self) -> None:
        if not self.done:
            self._finish({"action": "allow", "scope": "session", "rule": self.suggested_rule, "message": ""})

    def action_choose_always(self) -> None:
        if not self.done:
            self._finish({"action": "allow", "scope": "always", "rule": self.suggested_rule, "message": ""})

    def action_choose_deny(self) -> None:
        if self.done:
            return
        self.awaiting_feedback = True
        self._refresh()
        self.app.borrow_input(self, placeholder="Tell Claude what to do differently (Enter to skip)")

    def resolve_with_message(self, message: str) -> None:
        self._finish({"action": "deny", "scope": None, "rule": None, "message": message})


def _option_texts(options) -> list:
    """review finding 5: `options` may be Claude Code's OWN richer shape
    (`[{"label":.., "description":..}, ...]`, the schema a Claude-trained
    model reaches for on its own even though this build's tool schema
    still advertises `options: [str]` -- full adoption is H6 scope) --
    coercing to plain strings here, once, means `OptionList(*opts)`
    downstream never receives a raw dict (which Textual can't render and
    raises `VisualError` for, killing the whole app)."""
    out: list = []
    for opt in (options or []):
        if isinstance(opt, dict):
            label = opt.get("label") or opt.get("value") or opt.get("name") or ""
            desc = opt.get("description")
            out.append(f"{label} -- {desc}" if (label and desc) else (str(label) or str(opt)))
        else:
            out.append(str(opt))
    return out


class QuestionCard(Static, can_focus=True):
    """`AskUserQuestion`'s actual schema today is one question + a flat
    list of string options (`rolo_claude/tools/ask_user_question.py`) --
    rendered as one `OptionList` plus an "Other..." entry. A forward-
    compatible `input["questions"]` (a list of `{question, options}`) is
    also accepted, one OptionList per question, `tab` moving between them;
    the final answer is a single string for the simple shape, or a
    `{question: answer}` dict (JSON-encoded by the loop) for the multi
    shape."""

    BINDINGS = [
        Binding("tab", "next_question", "Next question", show=False),
        Binding("escape", "dismiss", "Dismiss", show=False),
    ]
    OTHER = "… Other (type your own answer)"

    def __init__(self, *, request_id: str, input_data: dict, on_answer: Callable) -> None:
        super().__init__(classes="question-card")
        self.request_id = request_id
        self._on_answer = on_answer
        questions = input_data.get("questions") if isinstance(input_data.get("questions"), list) else None
        if questions:
            self.questions = [(q.get("question", "?"), _option_texts(q.get("options"))) for q in questions
                               if isinstance(q, dict)] or [("?", [])]
        else:
            self.questions = [(input_data.get("question", "?"), _option_texts(input_data.get("options")))]
        self.answers: dict = {}
        self.active_index = 0
        self.done = False
        self._lists: list = []

    def compose(self):
        for i, (question, options) in enumerate(self.questions):
            yield Static(f"? {question}", markup=False, classes="question-heading")
            opts = [*options, self.OTHER]
            ol = OptionList(*opts, id=f"q-opt-{i}")
            self._lists.append(ol)
            yield ol

    def on_mount(self) -> None:
        if self._lists:
            self.app.call_after_refresh(self._lists[0].focus)

    def action_dismiss(self) -> None:
        if self.done:
            return
        self.done = True
        for ol in self._lists:
            ol.disabled = True
        self._on_answer(None)

    def action_next_question(self) -> None:
        if len(self._lists) <= 1 or self.done:
            return
        self.active_index = (self.active_index + 1) % len(self._lists)
        self._lists[self.active_index].focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if self.done:
            return
        try:
            index = self._lists.index(event.option_list)
        except ValueError:
            return
        question_text, options = self.questions[index]
        chosen = event.option.prompt
        chosen_text = str(chosen)
        if chosen_text == self.OTHER:
            self.active_index = index
            self.app.borrow_input(self, placeholder=f"Your answer to: {question_text}")
            return
        self.answers[question_text] = chosen_text
        self._advance_or_finish(index)

    def resolve_with_message(self, message: str) -> None:
        question_text, _ = self.questions[self.active_index]
        self.answers[question_text] = message
        self._advance_or_finish(self.active_index)

    def _advance_or_finish(self, just_answered_index: int) -> None:
        remaining = [i for i in range(len(self.questions)) if self.questions[i][0] not in self.answers]
        if not remaining:
            self.done = True
            for ol in self._lists:
                ol.disabled = True
            if len(self.questions) == 1:
                self._on_answer(next(iter(self.answers.values())))
            else:
                self._on_answer(self.answers)
            return
        self.active_index = remaining[0]
        self._lists[self.active_index].focus()


class PlanCard(Static, can_focus=True):
    """Built against the (not-yet-emitted, H6) `plan_review` event --
    `input_data` is whatever payload it eventually carries; `"plan"` (a
    string) is read defensively so this card degrades gracefully rather
    than crashing if that shape changes before H6 lands."""

    BINDINGS = [
        Binding("1,a", "approve_auto", "Approve, auto-accept edits", show=False),
        Binding("2,m", "approve_manual", "Approve, manual", show=False),
        Binding("3,k,escape", "keep_planning", "Keep planning", show=False),
    ]

    def __init__(self, *, request_id: str, input_data: dict, on_reply: Callable) -> None:
        plan_text = input_data.get("plan") if isinstance(input_data, dict) else None
        self.plan_text = plan_text if isinstance(plan_text, str) else str(input_data)
        super().__init__("", markup=False, classes="plan-card")
        self.request_id = request_id
        self._on_reply = on_reply
        self.done = False
        self._refresh()

    def _refresh(self) -> None:
        lines = ["\U0001f4cb Plan ready for review:", "", self.plan_text.strip(), ""]
        if not self.done:
            lines.append("  [1] Approve, auto-accept edits   [2] Approve, manual   [3] Keep planning")
        self.update("\n".join(lines))

    def _finish(self, decision: dict) -> None:
        self.done = True
        self._refresh()
        self._on_reply(decision)

    def action_approve_auto(self) -> None:
        if not self.done:
            self._finish({"approved": True, "mode_after": "acceptEdits", "feedback": ""})

    def action_approve_manual(self) -> None:
        if not self.done:
            self._finish({"approved": True, "mode_after": "default", "feedback": ""})

    def action_keep_planning(self) -> None:
        if self.done:
            return
        self.app.borrow_input(self, placeholder="What should change about the plan? (Enter to skip)")

    def resolve_with_message(self, message: str) -> None:
        self._finish({"approved": False, "mode_after": None, "feedback": message})
