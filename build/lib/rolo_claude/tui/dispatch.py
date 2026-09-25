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


async def _show_permission_card(app, data: dict, *, agent_id: "str | None" = None) -> None:
    """H5c finding 8: `agent_id`, when given, means this ask came from a
    sub-agent, not the main session -- the SAME live, answerable card
    (never an informational note), just tagged so the user knows which
    sub-agent is asking. `app.resolve_permission_decision` -> `Controller.
    answer_permission` -> `Session.resolve_permission` on the TOP-level
    session works unchanged for a sub-agent's own ask: the child's waiter
    is parked in the SAME `_permission_waiters` dict (see `agent/
    subagent.py`'s `_build_child_session`)."""
    request_id = data.get("id")
    name = data.get("name") or "?"
    input_data = data.get("input") or {}
    suggested = data.get("suggested_rule")
    summary = _tool_header(app, name, input_data)[2:]
    if agent_id:
        summary = f"[sub-agent {agent_id}] {summary}"

    def on_decide(decision: dict) -> None:
        app.resolve_permission_decision(request_id, decision, suggested_rule=suggested)
        app.clear_pending_card()

    card = PermissionCard(request_id=request_id, summary=summary,
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
    agent_id = event.agent_id
    if kind == "user_message":
        await app.transcript.add_user(data.get("text", ""))
    elif kind == "message_start":
        app.transcript.begin_message(turn, agent_id=agent_id)
        # H9 whole-tree review finding 11: a child's own `message_start`
        # must never touch the MAIN status bar (verified: a child's status
        # set it to the child's model/cost, and to idle while the parent
        # was still running) -- only the main session's own events
        # (agent_id is None) ever do.
        if agent_id is None:
            app.status_bar.apply_status({"phase": "thinking"})
    elif kind == "text_delta":
        await app.transcript.append_text(turn, data.get("index", 0), data.get("text", ""), agent_id=agent_id)
    elif kind == "thinking_delta":
        await app.transcript.append_thinking(turn, data.get("index", 0), data.get("text", ""), agent_id=agent_id)
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
        # H5c finding 8: a FOREGROUND sub-agent's own "ask" (when its
        # parent session is interactive) is now a LIVE, answerable card
        # too, agent-tagged -- never an informational "already denied"
        # note. A BACKGROUND sub-agent's ask (or any ask in print mode)
        # never reaches here at all: `agent/loop.py`'s own
        # `_subagent_live_asks` gate resolves those immediately instead
        # of ever yielding this event.
        await _show_permission_card(app, data, agent_id=event.agent_id)
    elif kind == "question":
        await _show_question_card(app, data)
    elif kind == "plan_review":
        await _show_plan_card(app, data)
    elif kind == "todos":
        await app.transcript.add_note(_format_todos(data), kind="todos")
    elif kind == "status":
        # H9 whole-tree review finding 11: a sub-agent Session's OWN
        # `turn()` yields the exact same "status" events the main session
        # does (same `events.status(...)` call sites in agent/loop.py) --
        # tagged with its `agent_id` like every other child event, but
        # never meant for the MAIN status bar (verified: a child's status
        # briefly showed the child's own model and a cost that dropped
        # from the parent's real running total).
        if agent_id is None:
            app.status_bar.apply_status(data)
    elif kind == "message_end":
        if agent_id is None:
            app.status_bar.apply_status({"phase": "idle", "cost_usd": data.get("cost_usd")})
            if data.get("context_pct") is not None:
                app.status_bar.apply_context_pct(data["context_pct"])
        await app.transcript.finish_open_streams(agent_id=agent_id)
    elif kind == "error":
        message = data.get("message", "")
        prefix = f"[sub-agent {agent_id}] " if agent_id else ""
        await app.transcript.add_note(f"✗ Error: {prefix}{message}", kind="error")
        app.notify(f"{prefix}{message}", severity="error", title="Error")
    elif kind == "turn_done":
        # finding 11: a child's own turn_done must never drive the MAIN
        # session's own idle/auto-title bookkeeping (verified: a child's
        # turn_done called app.on_turn_done, which showed "Interrupted."
        # and fired the auto-title logic even while the parent's own turn
        # was still running) -- its OWN open streams still get closed
        # either way, via the agent_id-scoped finish_open_streams below.
        if agent_id is None:
            app.status_bar.apply_status({"phase": "idle"})
        await app.transcript.finish_open_streams(agent_id=agent_id)
        if agent_id is None:
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
        # yields right before `steer_applied`).
        #
        # H5c finding 19: this event genuinely fires TWICE for one logical
        # steer -- once immediately at submit time (`Controller.submit`,
        # for instant feedback the moment the turn reaches a safe point
        # to actually apply it -- which can be much later) and once again
        # from the session's OWN turn-event stream when it actually
        # applies (kept there too: a bare `Session`/print-mode caller with
        # no Controller at all has no OTHER path to ever see this event,
        # and print mode's own documented contract promises it). Rather
        # than dropping the event at its source (breaking that other
        # path), the note is deduplicated HERE, by text, against a steer
        # still awaiting its matching `steer_applied` -- a SECOND, GENUINELY
        # DIFFERENT steer queued before the first applies still gets its
        # own note.
        pending = app._pending_steer_note_texts if hasattr(app, "_pending_steer_note_texts") else None
        if pending is None:
            pending = app._pending_steer_note_texts = []
        text = data.get("text")
        if text in pending:
            pass  # the same steer's own second (apply-time) emission
        else:
            pending.append(text)
            await app.transcript.add_note("↳ steering…", kind="steer")
    elif kind == "steer_applied":
        pending = getattr(app, "_pending_steer_note_texts", None)
        text = data.get("text")
        if pending and text in pending:
            pending.remove(text)
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
        elif phase == "skipped":
            # H5b finding 3: an auto-compaction the loop deliberately chose
            # NOT to attempt (still over the trigger right after the
            # previous one already ran) is not a failure -- nothing was
            # tried and nothing went wrong; a plain note, not the error
            # styling "failed" gets.
            reason = data.get("reason") or "not enough new room since the last compaction"
            await app.transcript.add_note(f"⧗ Compaction skipped: {reason}", kind="compaction")
            app.status_bar.apply_status({"phase": "idle"})
