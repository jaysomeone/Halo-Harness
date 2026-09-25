"""rolo_claude.tui.dispatch -- translates one `rolo_claude.events.Event`
into transcript/status-bar/card mutations on a `BridgeApp`. Split out of
`app.py` to keep the App class itself focused on composition/keys/the drain
loop; every function here takes `app` explicitly rather than being a method,
so it stays easy to unit-test by constructing a tiny fake `app` stand-in if
ever needed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from rolo_claude.tui.widgets.cards import PermissionCard, PlanCard, QuestionCard, _summarize_call
from rolo_claude.tui.widgets.diffview import DiffView

log = logging.getLogger("bridge")
_SEVERITY = {"error": "error", "warning": "warning"}
# U5 scope B: git-shadow snapshots are recorded for these tools only -- see
# `_maybe_record_shadow_step`'s own docstring for why Bash is excluded.
_SHADOW_TOOLS = frozenset({"Write", "Edit"})


def _tool_header(app, name: str, input_data: dict) -> str:
    tool = app.tool_registry.get(name) if app.tool_registry is not None else None
    body = tool.summary(input_data) if tool is not None else _summarize_call(name, input_data)
    return f"⏺ {body}"


async def _mount_tool_card(app, data: dict) -> None:
    from rolo_claude.tui.widgets.cards import ToolCard

    tool_id = data.get("id")
    name = data.get("name") or "?"
    input_data = data.get("input") or {}
    card = ToolCard(tool_use_id=tool_id, header=_tool_header(app, name, input_data))
    card.set_verbose(app.verbose)
    await app.transcript.mount_tool_card(card)
    # U5 scope B: stashed so the eventual `tool_result` (this event only
    # carries {id, ok, summary, content} -- never the tool name/input) can
    # decide whether/what to shadow-snapshot; see `_maybe_record_shadow_step`.
    if not hasattr(app, "_pending_tool_inputs"):
        app._pending_tool_inputs = {}
    app._pending_tool_inputs[tool_id] = (name, input_data)
    # review finding 5: repair can hand a REJECTED tool_use through to
    # `tool_use_ready` with its raw, un-coerced input (e.g. `old_string:
    # null`) -- DiffView's `.splitlines()` on a non-str kills the whole
    # app. `_as_text` never raises for anything JSON-shaped.
    if name == "Edit" and isinstance(input_data, dict):
        diff = DiffView(_as_text(input_data.get("old_string", "")), _as_text(input_data.get("new_string", "")),
                         path=_as_text(input_data.get("file_path", "")))
        await app.transcript.mount_widget(diff)
    elif name == "Write" and isinstance(input_data, dict):
        diff = DiffView("", _as_text(input_data.get("content", "")), path=_as_text(input_data.get("file_path", "")))
        await app.transcript.mount_widget(diff)


def _as_text(value) -> str:
    """review finding 5: coerce any tool_use input value to a safe str --
    a model/repair-rejected call can hand this ANY JSON-shaped value
    (None, a number, a list, ...), never just the string a card/DiffView
    assumes."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _maybe_record_shadow_step(app, tool_id, ok: bool) -> None:
    """U5 scope B: every successful Write/Edit records a git-shadow
    snapshot of the file's RESULTING (post-edit) content, keyed to this
    step, so `/rewind`/`/undo`/`/redo` has something to restore to.
    Deliberately NOT done for Bash: detecting which files a shell command
    touched would need a `git status` subprocess call right here, on the
    UI thread (this function runs inside `apply_event`, the drain loop's
    own call) -- exactly the kind of blocking call this same U5 pass is
    elsewhere REMOVING (`list_sessions`/`_git_branch` off-thread). A
    documented scope cut, not an oversight. Best-effort throughout: no
    real session attached (FakeController), an unreadable file, ... all
    silently skip -- a shadow snapshot is a convenience, never part of
    the conversation itself, so it must never turn into an error note."""
    pending = getattr(app, "_pending_tool_inputs", None)
    info = pending.pop(tool_id, None) if pending else None
    if not ok or info is None:
        return
    name, input_data = info
    if name not in _SHADOW_TOOLS or not isinstance(input_data, dict):
        return
    file_path = input_data.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return
    try:
        from rolo_claude.shadow import store_for_controller

        store = store_for_controller(app.controller)
        if store is None:
            return
        content = Path(file_path).read_text(encoding="utf-8", errors="replace")
        store.record_step({file_path: content}, label=f"{name}({Path(file_path).name})", trigger="tool")
    except Exception:
        pass


async def _show_permission_card(app, data: dict) -> None:
    request_id = data.get("id")
    name = data.get("name") or "?"
    input_data = data.get("input") or {}
    suggested = data.get("suggested_rule")

    def on_decide(decision: dict) -> None:
        app.resolve_permission_decision(request_id, decision, suggested_rule=suggested)
        app.clear_pending_card()

    card = PermissionCard(request_id=request_id, summary=_tool_header(app, name, input_data)[2:],
                           reason=data.get("reason", ""), suggested_rule=suggested, on_decide=on_decide)
    await app.transcript.mount_widget(card)
    app.set_pending_card(card)


async def _show_question_card(app, data: dict) -> None:
    request_id = data.get("id")
    input_data = data.get("input") or {}

    def on_answer(answer) -> None:
        ok = app.controller.answer_question(request_id, answer)
        if not ok:
            app.notify("That question is no longer waiting for an answer (already answered or the turn "
                       "was interrupted).", severity="warning", title="Question")
        app.clear_pending_card()

    card = QuestionCard(request_id=request_id, input_data=input_data, on_answer=on_answer)
    await app.transcript.mount_widget(card)
    app.set_pending_card(card)


async def _show_plan_card(app, data: dict) -> None:
    request_id = data.get("id", "")

    def on_reply(decision: dict) -> None:
        app.controller.answer_plan(decision["approved"], feedback=decision.get("feedback", ""),
                                    mode_after=decision.get("mode_after"))
        app.clear_pending_card()

    card = PlanCard(request_id=request_id, input_data=data, on_reply=on_reply)
    await app.transcript.mount_widget(card)
    app.set_pending_card(card)


def _format_todos(data: dict) -> str:
    todos = data.get("todos") if isinstance(data.get("todos"), list) else []
    glyphs = {"completed": "✓", "in_progress": "◐", "pending": "○"}
    lines = ["Todos:"]
    for t in todos:
        status = t.get("status", "pending") if isinstance(t, dict) else "pending"
        text = t.get("content", str(t)) if isinstance(t, dict) else str(t)
        lines.append(f"  {glyphs.get(status, '?')} {text}")
    return "\n".join(lines)


async def _apply_replay(app, messages: list) -> None:
    for m in messages:
        role, text = m.get("role"), m.get("text", "")
        if not text:
            continue
        if role == "user":
            await app.transcript.add_user(text)
        else:
            await app.transcript.add_note(text, kind="replay")


async def apply_event(app, event) -> None:
    """review finding 5: NOTHING isolated exceptions here although a
    handler can raise on perfectly reachable input (a repair-rejected
    tool_use's raw None/int/list args reaching DiffView, a Claude-Code-
    shaped question, ...) -- one bad event used to kill the whole TUI,
    typically leaving a running Bash child orphaned. A failure here
    becomes an error note + a notify, never a crash."""
    try:
        await _apply_event_inner(app, event)
    except Exception as e:
        log.exception("apply_event failed for kind=%r", getattr(event, "kind", None))
        try:
            await app.transcript.add_note(f"✗ UI error handling {event.kind!r}: {type(e).__name__}: {e}", kind="error")
        except Exception:
            pass
        try:
            app.notify(f"UI error: {type(e).__name__}: {e}", severity="error", title="Internal error")
        except Exception:
            pass


async def _apply_event_inner(app, event) -> None:
    kind, data, turn = event.kind, event.data, event.turn
    if kind == "user_message":
        await app.transcript.add_user(data.get("text", ""))
    elif kind == "message_start":
        app.transcript.begin_message(turn)
        app.status_bar.apply_status({"phase": "thinking"})
    elif kind == "text_delta":
        await app.transcript.append_text(turn, data.get("index", 0), data.get("text", ""))
    elif kind == "thinking_delta":
        await app.transcript.append_thinking(turn, data.get("index", 0), data.get("text", ""))
    elif kind == "tool_use_ready":
        await _mount_tool_card(app, data)
    elif kind == "tool_progress":
        card = app.transcript.tool_cards.get(data.get("id"))
        if card is not None:
            card.append_progress(data.get("text", ""))
    elif kind == "tool_result":
        card = app.transcript.tool_cards.get(data.get("id"))
        if card is not None:
            card.set_result(ok=bool(data.get("ok")), summary=data.get("summary", ""), content=data.get("content"))
        _maybe_record_shadow_step(app, data.get("id"), bool(data.get("ok")))
    elif kind == "permission_request":
        if event.agent_id:
            # H6 known v1 gap (D10) / B must-do: a SUB-AGENT's own "ask"
            # decision (agent/loop.py's `_resolve_tool_call`, the non-
            # interactive fallback -- sub-agents run with
            # `interactive=False`: their whole turn is drained to a list
            # before any of their events reach this live stream at all,
            # so there is no channel left to pause on for a real answer
            # by the time this arrives) is ALREADY final -- rendered as an
            # informational note, agent-tagged, never a live actionable
            # card that would misleadingly imply the user can still change
            # the outcome.
            await app.transcript.add_note(
                f"⚠ sub-agent (agent_id={event.agent_id}) needed permission for "
                f"{data.get('name', '?')} and was denied (no live approval for sub-agents "
                f"in this build): {data.get('reason', '')}",
                kind="error",
            )
        else:
            await _show_permission_card(app, data)
    elif kind == "question":
        await _show_question_card(app, data)
    elif kind == "plan_review":
        await _show_plan_card(app, data)
    elif kind == "todos":
        await app.transcript.add_note(_format_todos(data), kind="todos")
    elif kind == "status":
        app.status_bar.apply_status(data)
    elif kind == "message_end":
        app.status_bar.apply_status({"phase": "idle", "cost_usd": data.get("cost_usd")})
        if data.get("context_pct") is not None:
            app.status_bar.apply_context_pct(data["context_pct"])
        await app.transcript.finish_open_streams()
    elif kind == "error":
        message = data.get("message", "")
        await app.transcript.add_note(f"✗ Error: {message}", kind="error")
        app.notify(message, severity="error", title="Error")
    elif kind == "turn_done":
        app.status_bar.apply_status({"phase": "idle"})
        await app.transcript.finish_open_streams()
        app.on_turn_done(data.get("reason", "end_turn"))
    elif kind == "subagent_start":
        # U5 scope C: `agent_id`/`parent_tool_use_id` (D-Contract) are read
        # defensively -- H6's sub-agent event payload is still landing --
        # so this degrades to the plain note it always was if either is
        # absent. Tracked in `subagent_marks` for child-session navigation.
        suffix = f" (agent_id={data['agent_id']})" if data.get("agent_id") else ""
        widget = await app.transcript.add_note(f"→ sub-agent: {data.get('name', '?')}{suffix}", kind="subagent")
        app.transcript.subagent_marks.append(widget)
    elif kind == "subagent_end":
        widget = await app.transcript.add_note(f"← sub-agent finished: {data.get('name', '?')}", kind="subagent")
        app.transcript.subagent_marks.append(widget)
    elif kind == "replay":
        await _apply_replay(app, data.get("messages") or [])
    elif kind == "notification":
        app.notify(data.get("text", ""), severity=_SEVERITY.get(data.get("level"), "information"))
    elif kind == "steer_queued":
        # U5 must-do: the steer's own TEXT is never embedded in this note
        # -- it shows up exactly once, moments later, as an ordinary user
        # bubble (the `user_message` event `_apply_pending_steers_events`
        # yields right before `steer_applied`). The old note repeated the
        # full text here too, so a single steer showed up TWICE (a note,
        # then a user bubble with identical text). This event can fire
        # twice for one logical steer -- once immediately at submit time
        # (Controller.submit, for instant feedback) and once again when
        # the turn actually applies it -- both are just this same generic,
        # text-free indicator, never a second copy of the text.
        await app.transcript.add_note("↳ steering…", kind="steer")
    elif kind == "steer_applied":
        app.status_bar.apply_status({"phase": "thinking"})
    elif kind == "compaction":
        # U5 must-do: no handler existed at all before -- a "Compacting…"
        # indicator never appeared and a failure was invisible.
        phase = data.get("phase")
        if phase == "start":
            app.status_bar.apply_status({"phase": "compacting"})
            await app.transcript.add_note("⧗ Compacting the conversation…", kind="compaction")
        elif phase == "retry":
            missing = ", ".join(data.get("headings_missing") or [])
            await app.transcript.add_note(f"⧗ Compaction retrying (incomplete: {missing})", kind="compaction")
        elif phase == "done":
            before, after = data.get("tokens_before"), data.get("tokens_after")
            saved = f", freed ~{before - after} tokens" if isinstance(before, int) and isinstance(after, int) else ""
            await app.transcript.add_note(f"✓ Compacted the conversation (~{before} -> ~{after} tokens{saved})",
                                           kind="compaction")
            app.status_bar.apply_status({"phase": "idle"})
        elif phase == "failed":
            reason = data.get("reason") or "see logs"
            await app.transcript.add_note(f"✗ Compaction failed: {reason} (the conversation is unchanged)",
                                           kind="error")
            app.status_bar.apply_status({"phase": "idle"})
