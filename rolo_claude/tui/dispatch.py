"""rolo_claude.tui.dispatch -- translates one `rolo_claude.events.Event`
into transcript/status-bar/card mutations on a `BridgeApp`. Split out of
`app.py` to keep the App class itself focused on composition/keys/the drain
loop; every function here takes `app` explicitly rather than being a method,
so it stays easy to unit-test by constructing a tiny fake `app` stand-in if
ever needed.
"""

from __future__ import annotations

from rolo_claude.tui.widgets.cards import PermissionCard, PlanCard, QuestionCard, _summarize_call
from rolo_claude.tui.widgets.diffview import DiffView

_SEVERITY = {"error": "error", "warning": "warning"}


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
    if name == "Edit" and isinstance(input_data, dict):
        diff = DiffView(input_data.get("old_string", ""), input_data.get("new_string", ""),
                         path=input_data.get("file_path", ""))
        await app.transcript.mount_widget(diff)
    elif name == "Write" and isinstance(input_data, dict):
        diff = DiffView("", input_data.get("content", ""), path=input_data.get("file_path", ""))
        await app.transcript.mount_widget(diff)


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
        app.controller.answer_question(request_id, answer)
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
            card.set_result(ok=bool(data.get("ok")), summary=data.get("summary", ""))
    elif kind == "permission_request":
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
        await app.transcript.add_note(f"→ sub-agent: {data.get('name', '?')}", kind="subagent")
    elif kind == "subagent_end":
        await app.transcript.add_note(f"← sub-agent finished: {data.get('name', '?')}", kind="subagent")
    elif kind == "replay":
        await _apply_replay(app, data.get("messages") or [])
    elif kind == "notification":
        app.notify(data.get("text", ""), severity=_SEVERITY.get(data.get("level"), "information"))
