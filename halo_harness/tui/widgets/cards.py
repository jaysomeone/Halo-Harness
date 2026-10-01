"""halo_harness.tui.widgets.cards -- inline prompt cards (D-TUI scope D):
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
import time
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
    never needs a registry reference itself).

    Halo 2.0.1 W2b (liveness-tips-brief Part A4): a RUNNING card's header
    also carries its own elapsed seconds (`⏺ Bash(pytest -q) · 12 s`),
    ticking every drain tick (`tick()`, called from `Transcript.
    tick_tool_cards`/`BridgeApp._drain` for every still-running card) the
    same way the transcript's own phase line does -- Grep/Glob/WebFetch/
    MCP/Agent calls get at least this counter even though only Bash
    streams `tool_progress` lines of its own."""

    BINDINGS = [Binding("o", "open_pager", "Pager", show=False)]

    def __init__(self, *, tool_use_id: str, header: str) -> None:
        super().__init__("", markup=False, classes="tool-card tool-running")
        self.tool_use_id = tool_use_id
        self.header = header
        self.body_text = ""
        self.status = "running"
        self.expanded = False
        self.started_at = time.monotonic()
        self._last_rendered: Optional[str] = None
        self._refresh()

    def tick(self) -> None:
        """A no-op render-wise unless the displayed elapsed seconds
        actually changed (`_refresh`'s own `_last_rendered` cache) --
        cheap to call unconditionally every drain tick."""
        if self.status == "running":
            self._refresh()

    def append_progress(self, chunk: str) -> None:
        self.body_text += chunk
        self._refresh()

    def set_result(self, *, ok: bool, summary: str, content: Optional[str] = None,
                    images: Optional[list] = None, render_mode: Optional[str] = None,
                    protocol: Optional[str] = None, write_fn: Optional[Callable] = None) -> None:
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
        # H13 Part B ("inline images in the terminal"): the caption text
        # above is ALWAYS set first and is never replaced by this -- an
        # inline render is a best-effort ADDITION on top of it (kitty/sixel
        # draw into the terminal's own graphics layer; the text cells this
        # widget owns are untouched), so a detection miss, an unsupported
        # terminal, or any failure here still leaves the exact same caption
        # a caller that never passes `images` at all would see.
        if images and render_mode is None:
            render_mode = getattr(getattr(self, "app", None), "images_render_mode", "caption")
        if images and protocol is None:
            protocol = getattr(getattr(self, "app", None), "image_protocol", "none")
        if images and render_mode == "inline" and protocol in ("kitty", "sixel"):
            self._try_render_inline(images[0], protocol, write_fn=write_fn)

    def _try_render_inline(self, image: dict, protocol: str, *, write_fn: Optional[Callable] = None) -> None:
        import base64
        from halo_harness.tui import images as image_mod

        data_b64, media_type = image.get("data"), image.get("media_type") or "image/png"
        if not data_b64:
            return
        try:
            raw = base64.b64decode(data_b64, validate=False)
        except Exception:
            return
        raw = image_mod.downscale_for_terminal(raw, media_type)
        seq = image_mod.encode_kitty_apc(raw, cell_cols=48) if protocol == "kitty" else image_mod.encode_sixel(raw)
        if not seq:
            return
        # test hook: a pilot asserts on this without needing a real
        # terminal or a stable Rich/Textual internal write API.
        self._last_inline_sequence = seq
        (write_fn or self._default_inline_writer)(seq)

    def _default_inline_writer(self, seq: str) -> None:
        app = getattr(self, "app", None)
        console = getattr(app, "console", None)
        if console is None or not getattr(console, "is_terminal", False):
            return  # a pilot/print-mode-adjacent run, or output piped -- never write raw escapes there
        self.call_after_refresh(lambda: self._write_positioned(seq))

    def _write_positioned(self, seq: str) -> None:
        # Written to the SAME underlying stream Textual's own driver
        # ultimately writes to (`Console.file`, a stable public Rich API),
        # bracketed in cursor save/move/restore so it lands at this card's
        # own on-screen region without disturbing wherever Textual's next
        # redraw expects the cursor to be. The image lives in the
        # terminal's own graphics layer from here on -- a later scroll/
        # resize can visually strand it (kitty/sixel have no "reflow"
        # concept), an accepted limitation the brief's own caption fallback
        # covers for every terminal that can't do this at all.
        try:
            region = self.region
            out = f"\x1b[s\x1b[{region.y + 1};{region.x + 1}H{seq}\x1b[u"
            self.app.console.file.write(out)
            self.app.console.file.flush()
        except Exception:
            pass

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
        from halo_harness.model_display import format_elapsed_seconds

        glyph = STATUS_GLYPHS[self.status]
        # A4: a running card's own elapsed seconds, right in the header --
        # replaces the old bare "⋯" spinner (the counter itself is now the
        # liveness signal); a resolved card (ok/error/skipped) shows no
        # elapsed suffix at all, same as before this brief.
        header = (f"{self.header} · {format_elapsed_seconds(time.monotonic() - self.started_at)}"
                  if self.status == "running" else self.header)
        if self.expanded:
            body, extra = self.body_text, 0
        else:
            body, extra = _truncate_lines(self.body_text, 3)
        hint = f"\n  … +{extra} lines (ctrl+o, or o for a pager)" if extra else ""
        indented = "\n".join(f"  {line}" for line in body.splitlines())
        text = f"{header}\n  {glyph}\n{indented}{hint}"
        if text == self._last_rendered:
            return
        self._last_rendered = text
        self.update(text)


def _summarize_call(name: str, input_data: dict) -> str:
    try:
        body = json.dumps(input_data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        body = str(input_data)
    return f"{name}({body[:80]})" if len(body) <= 80 else f"{name}({body[:80]}...)"


class SubAgentCard(Static):
    """Halo 2.0.1 W2b (liveness-tips-brief Part A5): the parent's own live
    summary of ONE running sub-agent -- `"agent reviewer · thinking 9 s ·
    3 tools"` -- mounted (by `tui/dispatch.py`) on `subagent_start` in
    place of the old plain "-> sub-agent: X" note, updated from the
    CHILD's own `phase`/`tool_use_ready` events (every one of them already
    tagged with this card's own `agent_id`, the same tagging every other
    child event carries -- see `Transcript`'s own class docstring),
    finalized (frozen, stops ticking) on `subagent_end`.

    Never focusable -- purely informational, like a SystemNote, never
    stealing focus the way an answerable card correctly does. This is
    IN ADDITION TO the child's own inline text/thinking blocks (which
    already render directly in the transcript, CSS-tagged `*-child`) --
    this card is the at-a-glance summary a reader can check without
    reading the child's full streamed output. `started_at` is set ONCE,
    at construction, and never reset -- "9 s" is how long the WHOLE
    sub-agent invocation has been running, not any one internal call's
    own elapsed (unlike the main session's own per-call phase line)."""

    def __init__(self, *, agent_id: str, name: str) -> None:
        super().__init__("", markup=False, classes="system-note system-note-subagent")
        self.agent_id = agent_id
        # NEVER `self.name` -- Textual's own `Widget.name` is a read-only
        # property (the widget's DOM name), and assigning to it raises
        # "property 'name' ... has no setter" (caught and swallowed by
        # dispatch.py's own apply_event try/except, which is what let this
        # slip through as a silent per-event error note instead of a loud
        # crash the first time around).
        self.agent_name = name
        self.phase_word = "thinking"
        self.tool_count = 0
        self.done = False
        self.started_at = time.monotonic()
        self._last_rendered: Optional[str] = None
        self._refresh()

    def set_phase_word(self, word: str) -> None:
        if not self.done and word != self.phase_word:
            self.phase_word = word
            self._refresh()

    def note_tool_call(self) -> None:
        if not self.done:
            self.tool_count += 1
            self._refresh()

    def finish(self) -> None:
        self.done = True
        self._refresh()

    def tick(self) -> None:
        if not self.done:
            self._refresh()

    def _refresh(self) -> None:
        from halo_harness.model_display import format_elapsed_seconds

        elapsed = format_elapsed_seconds(time.monotonic() - self.started_at)
        word = "done" if self.done else self.phase_word
        text = f"agent {self.agent_name} · {word} {elapsed} · {self.tool_count} tools"
        if text == self._last_rendered:
            return
        self._last_rendered = text
        self.update(text)


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
                 suggested_rule: Optional[str], on_decide: Callable,
                 on_resolved_externally: Optional[Callable] = None) -> None:
        super().__init__("", markup=False, classes="permission-card")
        self.request_id = request_id
        self.summary = summary
        self.reason = reason
        self.suggested_rule = suggested_rule
        self._on_decide = on_decide
        # H15 Part D2.3: for a card resolved OUTSIDE the model loop (the
        # `!cmd` inline-shell ask has no worker thread of its own to wake
        # up when `reevaluate_pending_permission` resolves its slot) --
        # None (every live tool-call card, unchanged) means `resolve_
        # externally` below stays exactly as finding 4 left it: display-
        # only, never re-firing anything.
        self._on_resolved_externally = on_resolved_externally
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
        # 1.0.1 fixpass finding 4: a `done` guard -- without it, a STALE
        # `_borrowing_card` pointing at an already-finished card (the old
        # mode-switch-while-awaiting-feedback bug; see app.py's own
        # `_reevaluate_pending_permission_for_mode`) made the user's NEXT
        # Enter re-finish it, firing `on_decide` a second time (a "no
        # longer waiting" toast) and calling `clear_pending_card()` again
        # -- clearing whatever DIFFERENT card was actually pending by then.
        if self.done:
            return
        self._finish({"action": "deny", "scope": None, "rule": None, "message": message})

    def resolve_externally(self, action: str) -> None:
        """1.0.1 fixpass finding 4: the request has ALREADY been resolved
        directly against the permission engine (`Controller.
        reevaluate_pending_permission`, a mode change re-deciding a parked
        ask) -- this only updates the card's own display to match, WITHOUT
        calling `on_decide` again (that would re-resolve the SAME waiter a
        second time; see `Session.reevaluate_pending_permission`'s own
        docstring for why that's unsafe). `action` is "allow" or "deny"."""
        if self.done:
            return
        self.done = True
        self.awaiting_feedback = False
        outcome = "allowed" if action == "allow" else "denied"
        self.summary = f"{self.summary} -- {outcome} (mode changed)"
        self._refresh()
        # H15 Part D2.3: for an inline-shell card specifically (the ONLY
        # caller that ever passes this), the engine call above resolved
        # its slot but nothing else is listening for that -- `on_decide`'s
        # OWN closure (reused verbatim as `on_resolved_externally`) is
        # what actually starts the command on allow / does nothing on
        # deny, same as a normal digit-press would.
        if self._on_resolved_externally is not None:
            self._on_resolved_externally({"action": action, "scope": None, "rule": None, "message": ""})


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
    list of string options (`halo_harness/tools/ask_user_question.py`) --
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


class EffortCard(Static, can_focus=True):
    """1.0.1 hotfix 20.2: the inline `/effort` selector, Claude-Code-style
    -- one horizontal row of THIS route's own accepted effort levels
    (`levels`, ordered -- e.g. `["low","medium","high","max"]` for an
    Anthropic route, never the harness-wide vocabulary including `xhigh`
    that route would reject outright per hotfix 19), the cursor bracketed,
    a one-line description underneath. Left/Right/h/l move, Enter applies
    (`on_select(level)`), Esc cancels (`on_select(None)`, the caller keeps
    the old value) -- either way `on_select` fires exactly once. A model
    with no adjustable effort at all (`levels` empty) shows one line saying
    so and closes on any key (`on_select(None)`)."""

    BINDINGS = [
        Binding("left,h", "move_left", "Previous", show=False),
        Binding("right,l", "move_right", "Next", show=False),
        Binding("enter", "apply", "Apply", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    _DESCRIPTIONS = {
        "low": "fastest, least thorough reasoning",
        "medium": "balanced speed and thoroughness",
        "high": "slower, more thorough (recommended for hard problems)",
        "xhigh": "extra thorough, higher latency",
        "max": "maximum reasoning effort this model supports",
    }

    def __init__(self, *, levels: list, current: "str | None", model_id: str, on_select: Callable,
                 descriptions: "Optional[dict]" = None, override_note: "Optional[str]" = None,
                 requested: "Optional[str]" = None) -> None:
        super().__init__("", markup=False, classes="effort-card")
        self.levels = list(levels)
        self.model_id = model_id
        self._descriptions = descriptions or self._DESCRIPTIONS
        self.index = self.levels.index(current) if current in self.levels else 0
        self._on_select = on_select
        self.done = False
        # 1.0.1 part 2 (item 22 remainder): "the selector's description
        # line says why" -- set when this route forces an explicit
        # reasoning_effort override alongside tools (gpt-6's table rule, or
        # a learned per-endpoint rule), so picking a level here still shows
        # the user why their choice won't change what actually goes out on
        # a tool-carrying turn.
        self._override_note = override_note
        # Halo 2.0.1 W2b (liveness-tips-brief Part C): "the /effort card
        # marks a clamped choice inline, e.g. 'medium (sent as high on
        # this route)'" -- `current` is already the SENT value (`Session.
        # effort`); `requested` (`Session.effort_requested`) is the raw
        # value last explicitly asked for, before clamping. Computed ONCE,
        # describing the state the card opened with -- arrowing through
        # the row changes `self.index`, never this note (nothing has been
        # applied yet while the card is still open).
        self._clamp_note = (f"Current: {requested} (sent as {current} on this route)."
                             if requested is not None and current is not None and requested != current
                             else None)
        self._refresh()

    def _refresh(self) -> None:
        if not self.levels:
            self.update(f"Effort level: {self.model_id} has no adjustable effort level. (any key closes)")
            return
        row = "   ".join(f"[{lvl}]" if i == self.index else lvl for i, lvl in enumerate(self.levels))
        desc = self._descriptions.get(self.levels[self.index], "")
        lines = [f"Effort level for {self.model_id}:", f"  {row}"]
        if desc:
            lines.append(f"  {desc}")
        if self._clamp_note:
            lines.append(f"  {self._clamp_note}")
        if self._override_note:
            lines.append(f"  {self._override_note}")
        if not self.done:
            lines.append("  ←/→ (or h/l) move   Enter apply   Esc cancel")
        self.update("\n".join(lines))

    def _finish(self, level) -> None:
        self.done = True
        self._refresh()
        self._on_select(level)

    def action_move_left(self) -> None:
        if not self.done and self.levels:
            self.index = (self.index - 1) % len(self.levels)
            self._refresh()

    def action_move_right(self) -> None:
        if not self.done and self.levels:
            self.index = (self.index + 1) % len(self.levels)
            self._refresh()

    def action_apply(self) -> None:
        if self.done:
            return
        self._finish(self.levels[self.index] if self.levels else None)

    def action_cancel(self) -> None:
        if not self.done:
            self._finish(None)

    def on_key(self, event) -> None:
        # A model with no adjustable effort at all: "closes on any key",
        # not only the two bound keys above.
        if not self.levels and not self.done:
            self._finish(None)
            event.stop()


class RewindCard(Static, can_focus=True):
    """Confirmation card for `/rewind`/`/undo`/`/redo` (U5 scope B): shows
    the target shadow-repo step and asks to confirm before touching the
    real working tree. `on_decide(confirmed: bool)` fires exactly once."""

    BINDINGS = [
        Binding("1,y,enter", "confirm", "Restore", show=False),
        Binding("2,n,escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, *, step: dict, verb: str = "rewind", on_decide: Callable) -> None:
        super().__init__("", markup=False, classes="rewind-card")
        self.step = step
        self.verb = verb  # "rewind" | "undo" | "redo"
        self._on_decide = on_decide
        self.done = False
        self._refresh()

    def _refresh(self) -> None:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.step.get("ts", 0) or 0))
        files = ", ".join(self.step.get("files") or []) or "(no files recorded)"
        lines = [
            f"↩ {self.verb.capitalize()} to step {self.step.get('id', '?')} ({when}):",
            f"  {self.step.get('label', '')}",
            f"  files: {files}",
        ]
        if self.done:
            pass
        else:
            lines.append("  [1] Restore   [2] Cancel")
        self.update("\n".join(lines))

    def _finish(self, confirmed: bool) -> None:
        self.done = True
        self._refresh()
        self._on_decide(confirmed)

    def action_confirm(self) -> None:
        if not self.done:
            self._finish(True)

    def action_cancel(self) -> None:
        if not self.done:
            self._finish(False)


class ImproveCard(Static, can_focus=True):
    """One drafted `/improve` candidate (H10 Part B3, RewindCard's own
    confirm/cancel pattern extended to 5 actions): kind badge, target path,
    rendered body (or a unified diff when the target already carries the
    halo provenance comment -- an UPDATE), rationale, evidence list
    (`session#seq`; `o` opens the first excerpt in a pager), the
    provenance line itself. `on_action(action, data)` fires exactly once,
    `action` in `{"apply", "edit", "skip", "dismiss", "quit"}` (`data` is
    only ever used for "edit", carrying nothing extra today -- the App-side
    handler owns the actual `$VISUAL`/`$EDITOR` suspend-and-reapply, same
    as `app.py`'s own `action_open_editor`, since only the App has
    `self.suspend()`)."""

    BINDINGS = [
        Binding("a", "do_apply", "Apply", show=False),
        Binding("e", "do_edit", "Edit, then apply", show=False),
        Binding("s", "do_skip", "Skip", show=False),
        Binding("d", "do_dismiss", "Dismiss forever", show=False),
        Binding("q,escape", "do_quit", "Stop reviewing", show=False),
        Binding("o", "open_excerpt", "Open first excerpt", show=False),
    ]

    def __init__(self, *, candidate, provenance_line: str, index: int, total: int,
                 diff_lines: Optional[list] = None, excerpt_text: str = "", on_action: Callable) -> None:
        super().__init__("", markup=False, classes="improve-card")
        self.candidate = candidate
        self.provenance_line = provenance_line
        self.index = index
        self.total = total
        self.diff_lines = diff_lines or []
        self.excerpt_text = excerpt_text
        self._on_action = on_action
        self.done = False
        self._refresh()

    def _refresh(self) -> None:
        c = self.candidate
        lines = [f"✦ Improve candidate {self.index}/{self.total}: [{c.kind}] {c.title}"]
        target_note = " (would UPDATE an existing halo file)" if self.diff_lines else " (new file)"
        lines.append(f"  target: {c.scope}/{c.path}{target_note}")
        lines.append(f"  confidence: {c.confidence}")
        if c.rationale:
            lines.append(f"  rationale: {c.rationale}")
        if c.from_tool_output:
            lines.append("  (derived from tool output)")
        if c.evidence:
            lines.append("  evidence: " + ", ".join(c.evidence) + "  (o to open the first excerpt)")
        lines.append("")
        if self.diff_lines:
            lines.append("  --- diff ---")
            lines.extend(f"  {ln.rstrip(chr(10))}" for ln in self.diff_lines[:40])
        else:
            body = c.body if len(c.body) < 1600 else c.body[:1600] + "…"
            lines.extend(f"  {ln}" for ln in body.splitlines())
        lines.append("")
        lines.append(f"  {self.provenance_line}")
        if not self.done:
            lines.append("")
            lines.append("  [a] Apply   [e] Edit, then apply   [s] Skip   [d] Dismiss forever   [q] Stop reviewing")
        self.update("\n".join(lines))

    def _finish(self, action: str) -> None:
        self.done = True
        self._refresh()
        self._on_action(action, None)

    def action_do_apply(self) -> None:
        if not self.done:
            self._finish("apply")

    def action_do_edit(self) -> None:
        if not self.done:
            self._finish("edit")

    def action_do_skip(self) -> None:
        if not self.done:
            self._finish("skip")

    def action_do_dismiss(self) -> None:
        if not self.done:
            self._finish("dismiss")

    def action_do_quit(self) -> None:
        if not self.done:
            self._finish("quit")

    def action_open_excerpt(self) -> None:
        if self.candidate.evidence:
            self.app.push_screen(PagerScreen(f"Evidence: {self.candidate.evidence[0]}",
                                              self.excerpt_text or "(excerpt text unavailable)"))
