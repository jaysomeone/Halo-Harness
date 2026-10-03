"""halo_harness.tui.dispatch -- translates one `halo_harness.events.Event`
into transcript/status-bar/card mutations on a `BridgeApp`. Split out of
`app.py` to keep the App class itself focused on composition/keys/the drain
loop; every function here takes `app` explicitly rather than being a method,
so it stays easy to unit-test by constructing a tiny fake `app` stand-in if
ever needed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from halo_harness.tui.widgets.cards import PermissionCard, PlanCard, QuestionCard, _summarize_call
from halo_harness.tui.widgets.diffview import DiffView

log = logging.getLogger("bridge")
_SEVERITY = {"error": "error", "warning": "warning"}
# U5 scope B / W4a: git-shadow snapshots are recorded directly (a single
# `{path: content}` read, no diffing needed) for these tools -- NotebookEdit
# added in W4a (same single-known-path shape as Write/Edit, just a
# different input field name, see `_SHADOW_PATH_FIELD`). Bash is handled
# separately (`_maybe_record_bash_shadow_step`) via a git-status diff,
# since a shell command's own target file(s) are not knowable up front.
_SHADOW_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
_SHADOW_PATH_FIELD = {"Write": "file_path", "Edit": "file_path", "NotebookEdit": "notebook_path"}


def _phase_word_for(state: "str | None", kind: "str | None") -> "str | None":
    """Halo 2.0.1 W2b (liveness-tips-brief Part A3/A5): a `phase` event's
    (state, kind) -> the short display word the main status bar AND a
    sub-agent's own `SubAgentCard` both show -- ONE mapping, read by both,
    so a main-session call and a sub-agent's own call are always described
    the same way. None for a `phase` state that never changes the word on
    its own (`headers` alone stays "thinking" -- the richer "no tokens
    yet"/token-count text is the transcript's own phase line's job, not
    this compact word)."""
    if state in ("request_sent", "headers"):
        return "thinking"
    if state == "first_token":
        if kind == "reasoning":
            return "thinking"
        if kind == "tool":
            return "tool"
        return "writing"
    if state == "waiting_for_model":
        return "waiting"
    return None


def _tool_header(app, name: str, input_data: dict) -> str:
    tool = app.tool_registry.get(name) if app.tool_registry is not None else None
    body = tool.summary(input_data) if tool is not None else _summarize_call(name, input_data)
    return f"⏺ {body}"


async def _mount_tool_card(app, data: dict) -> None:
    from halo_harness.tui.widgets.cards import ToolCard

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
    # W4a ("steps record files CREATED so undo deletes them"): pre-existence
    # is only knowable NOW, before the call actually runs -- by the time
    # `_maybe_record_shadow_step` sees the `tool_result`, Write/NotebookEdit
    # have already (possibly) created the file.
    pre_exists = None
    field = _SHADOW_PATH_FIELD.get(name)
    if field and isinstance(input_data, dict):
        path_val = input_data.get(field)
        if isinstance(path_val, str) and path_val:
            try:
                pre_exists = Path(path_val).is_file()
            except OSError:
                pre_exists = None
    app._pending_tool_inputs[tool_id] = (name, input_data, pre_exists)
    if name == "Bash" and isinstance(input_data, dict):
        _start_bash_shadow_before(app, tool_id)
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
    """U5 scope B / W4a: every successful Write/Edit/NotebookEdit records a
    git-shadow snapshot of the file's RESULTING (post-edit) content, keyed
    to this step, so `/rewind`/`/undo`/`/redo` has something to restore to
    -- `created=[path]` when `_mount_tool_card` saw it did NOT exist before
    this call (so undo past this step deletes it, W4a). Bash is handled
    separately by `_maybe_record_bash_shadow_step` (a git-status diff
    around the call, off the UI thread). Best-effort throughout: no real
    session attached (FakeController), an unreadable file, ... all silently
    skip -- a shadow snapshot is a convenience, never part of the
    conversation itself, so it must never turn into an error note."""
    pending = getattr(app, "_pending_tool_inputs", None)
    info = pending.pop(tool_id, None) if pending else None
    if info is None:
        return
    name, input_data, pre_exists = info
    if name == "Bash":
        _maybe_record_bash_shadow_step(app, tool_id, name, input_data, ok)
        return
    if not ok:
        return
    if name not in _SHADOW_TOOLS or not isinstance(input_data, dict):
        return
    field = _SHADOW_PATH_FIELD.get(name, "file_path")
    file_path = input_data.get(field)
    if not isinstance(file_path, str) or not file_path:
        return
    try:
        from halo_harness.shadow import store_for_controller

        store = store_for_controller(app.controller)
        if store is None:
            return
        content = Path(file_path).read_text(encoding="utf-8", errors="replace")
        created = [file_path] if pre_exists is False else []
        store.record_step({file_path: content}, label=f"{name}({Path(file_path).name})", trigger="tool",
                           created=created)
    except Exception:
        pass


def _start_bash_shadow_before(app, tool_id) -> None:
    """W4a: Bash's own shadow-copy half -- the BEFORE snapshot, taken off
    the UI thread (same `run_worker(..., thread=True)` convention as
    `_git_branch`; this function itself runs inside `apply_event`, the
    drain loop's own call, which must never block on a subprocess). A
    repo-less cwd (not a git repo) or a timeout leaves nothing stashed,
    which `_maybe_record_bash_shadow_step` reads as "no diff available"
    and silently skips -- the documented limit for non-git directories."""
    if not hasattr(app, "_pending_bash_shadow_before"):
        app._pending_bash_shadow_before = {}
    app.run_worker(lambda: _bash_shadow_before_worker(app, tool_id), thread=True, name="shadow-bash-before")


def _bash_shadow_before_worker(app, tool_id) -> None:
    from halo_harness.shadow import git_status_untracked_paths
    before = git_status_untracked_paths(app.cwd)
    store = getattr(app, "_pending_bash_shadow_before", None)
    if store is not None and before is not None:
        store[tool_id] = before


def _maybe_record_bash_shadow_step(app, tool_id, name, input_data, ok: bool) -> None:
    before = (getattr(app, "_pending_bash_shadow_before", None) or {}).pop(tool_id, None)
    if not ok or before is None:
        return
    app.run_worker(lambda: _bash_shadow_after_worker(app, before), thread=True, name="shadow-bash-after")


def _bash_shadow_after_worker(app, before: set) -> None:
    from halo_harness.shadow import git_status_untracked_paths, store_for_controller
    after = git_status_untracked_paths(app.cwd)
    if after is None:
        return
    new_paths = sorted(after - before)
    if not new_paths:
        return
    try:
        store = store_for_controller(app.controller)
        if store is None:
            return
        files = {}
        for p in new_paths:
            try:
                files[p] = Path(p).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        if files:
            store.record_step(files, label="Bash(new file(s))", trigger="tool", created=list(files))
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
    # Halo 2.0.1 W3a (finding 16 / PendingDock): queued, never mounted
    # straight into the transcript any more -- a second concurrent ask no
    # longer silently overwrites this one. The transcript keeps a one-line
    # marker at this exact point in the conversation either way.
    await app.enqueue_pending_card(card, marker_text=f"⏸ permission needed for {summary}, see below")


def _tag_question_input(input_data: dict, agent_id: "str | None") -> dict:
    """W4a: a sub-agent's own AskUserQuestion, tagged with its agent name --
    same spirit as `_show_permission_card`'s own `[sub-agent {id}] {summary}`
    prefix, applied to EACH question's own text (single- or multi-question
    shape alike, see tools/ask_user_question.py's own `_questions_from`)."""
    if not agent_id or not isinstance(input_data, dict):
        return input_data
    tagged = dict(input_data)
    prefix = f"[sub-agent {agent_id}] "
    if isinstance(tagged.get("questions"), list):
        tagged["questions"] = [
            {**q, "question": prefix + q.get("question", "")} if isinstance(q, dict) else q
            for q in tagged["questions"]
        ]
    elif tagged.get("question"):
        tagged["question"] = prefix + tagged["question"]
    return tagged


async def _show_question_card(app, data: dict, *, agent_id: "str | None" = None) -> None:
    request_id = data.get("id")
    input_data = _tag_question_input(data.get("input") or {}, agent_id)

    def on_answer(answer) -> None:
        ok = app.controller.answer_question(request_id, answer)
        if not ok:
            app.notify("That question is no longer waiting for an answer (already answered or the turn "
                       "was interrupted).", severity="warning", title="Question")
        app.clear_pending_card()

    card = QuestionCard(request_id=request_id, input_data=input_data, on_answer=on_answer)
    await app.enqueue_pending_card(card, marker_text="⏸ question asked, see below")


async def _show_plan_card(app, data: dict, *, agent_id: "str | None" = None) -> None:
    request_id = data.get("id", "")
    card_data = data
    if agent_id:
        # W4a: same tagging spirit as the question card above -- a plan
        # REVIEW only ever comes from ExitPlanMode, one per child at a time
        # (mirrors the main session's own one-at-a-time plan-mode rule).
        card_data = {**data, "plan": f"[sub-agent {agent_id}]\n{data.get('plan', '')}"}

    def on_reply(decision: dict) -> None:
        app.controller.answer_plan(decision["approved"], feedback=decision.get("feedback", ""),
                                    mode_after=decision.get("mode_after"))
        app.clear_pending_card()

    card = PlanCard(request_id=request_id, input_data=card_data, on_reply=on_reply)
    await app.enqueue_pending_card(card, marker_text="⏸ plan ready for review, see below")


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
        text = data.get("text", "")
        await app.transcript.append_text(turn, data.get("index", 0), text, agent_id=agent_id)
        # A3: the status bar's own received-token counter -- never for a
        # child's delta (finding 11's own "never touch the MAIN status bar"
        # rule, unchanged by this brief).
        #
        # W2c (live-capture polish): `set_phase_word` BEFORE `add_received_
        # chars`, never after -- a word CHANGE resets the char counter, so
        # reversing this order would add this delta's own chars and then
        # immediately zero them back out. This mirrors `append_text` just
        # above's own direct `enter_writing()` call on the transcript's
        # phase line, for the SAME reason: `agent/loop.py`'s own
        # `chunk_started` latch fires the "phase"/first_token event ONCE
        # per call, for whichever kind (reasoning/tool/text) streams first
        # -- a model that reasons before writing never gets a SECOND phase
        # event for the reasoning -> text transition, so the cluster used to
        # stay stuck on "thinking" while the phase line (driven straight off
        # content arrival, not off that one-shot event) had already moved on
        # to "Writing". Reacting to the SAME delta here closes that gap:
        # same event, same drain tick, one source for both widgets' words.
        if agent_id is None:
            app.status_bar.set_phase_word("writing")
            app.status_bar.add_received_chars(len(text))
        else:
            # W3b (item 10): the SAME one-shot chunk_started latch gap
            # this file's own W2c comment above describes for the main
            # status bar applies identically to a child's own SubAgentCard
            # -- its phase word used to be driven ONLY by the `phase`/
            # first_token event (below), so a child that reasons then
            # writes within one call never got a second phase event for
            # the transition and stayed stuck on "thinking" while its card
            # kept streaming real text. Reacting to the same text_delta
            # here closes that gap for children exactly as it already does
            # for the main session.
            card = app.transcript.subagent_cards.get(agent_id)
            if card is not None:
                card.set_phase_word("writing")
    elif kind == "thinking_delta":
        text = data.get("text", "")
        await app.transcript.append_thinking(turn, data.get("index", 0), text, agent_id=agent_id)
        if agent_id is None:
            app.status_bar.set_phase_word("thinking")
            app.status_bar.add_received_chars(len(text))
        else:
            card = app.transcript.subagent_cards.get(agent_id)
            if card is not None:
                card.set_phase_word("thinking")
    elif kind == "tool_use_ready":
        await _mount_tool_card(app, data)
        # A3/A5: "tool <Name> <Ns>" on the main status bar, or the running
        # count on the sub-agent's own card -- never both for one event.
        if agent_id is None:
            app.status_bar.set_phase_word("tool")
            app.status_bar.set_tool_name(data.get("name"))
        else:
            card = app.transcript.subagent_cards.get(agent_id)
            if card is not None:
                card.note_tool_call()
    elif kind == "tool_progress":
        card = app.transcript.tool_cards.get(data.get("id"))
        if card is not None:
            card.append_progress(data.get("text", ""))
    elif kind == "tool_result":
        card = app.transcript.tool_cards.get(data.get("id"))
        if card is not None:
            # H13 Part B: `images` (present only for a real image tool
            # result -- Read of an image, a Playwright/Chrome screenshot,
            # an MCP image block) lets the card attempt an inline render
            # instead of just the plain caption text; a card that never
            # gets this kwarg (every non-image result) behaves exactly as
            # before.
            card.set_result(ok=bool(data.get("ok")), summary=data.get("summary", ""), content=data.get("content"),
                             images=data.get("images"))
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
        # W4a: a sub-agent's own AskUserQuestion is now live/answerable too
        # (see agent/loop.py's own widened gate) -- tagged with its agent
        # name exactly like a sub-agent's permission card already is.
        await _show_question_card(app, data, agent_id=agent_id)
    elif kind == "plan_review":
        # ExitPlanMode is NOT widened the same way (W4a scope note): `Session.
        # resolve_plan`/`_pending_plan_id` are single-slot by design (one
        # plan review at a time, no request_id) -- safely routing a CHILD's
        # own review through it needs a bigger change than this round's
        # "wire the existing queue" scope; `agent_id` is always None here
        # today (a child never reaches this event while non-interactive).
        await _show_plan_card(app, data, agent_id=agent_id)
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
            status_data = data
            # Halo 2.0.1 W2b (liveness-tips-brief Part C): "the status-bar
            # effort chip shows the SENT value" for EVERY status event, not
            # only the ones `/effort` itself pushes -- re-derives the same
            # "requested (sent as X on this route)" text `tui/slash.py`'s
            # own `_effort_status_text` already computes for `/effort`'s
            # card/confirmation, from the live session, so a route
            # configured via `--effort medium` at launch (never touching
            # `/effort` interactively at all) shows the clamp too.
            if data.get("effort") is not None:
                from halo_harness.tui.slash import _effort_status_text
                enriched = _effort_status_text(getattr(app.controller, "session", None))
                if enriched is not None:
                    status_data = {**data, "effort": enriched}
            app.status_bar.apply_status(status_data)
    elif kind == "message_end":
        if agent_id is None:
            # 1.0.1 hotfix 14: pass the raw context_tokens/context_limit and
            # token totals straight through (message_end already carries
            # them -- see events.message_end) instead of only cost_usd plus
            # the derived context_pct -- apply_status needs the raw numbers
            # itself now, to show "ctx 12k/1M 1%" or fall back to "in 12k
            # out 3k" when cost_usd is None.
            #
            # Halo 2.0.1 W2b: no "phase": "idle" here any more -- a message_
            # end mid-turn (a tool-calling step, stop_reason="tool_use") is
            # NOT the turn ending, and the old unconditional idle flash was
            # exactly the kind of misleading non-liveness signal A1/A3 are
            # about (the status bar briefly going blank while the turn was
            # still very much running, between steps). `phase(state=
            # "waiting_for_model")` (below) now fills that gap with a real
            # "waiting" word instead; `turn_done`'s own `go_idle()` is the
            # ONLY place idle is ever set from here on.
            app.status_bar.apply_status({
                "cost_usd": data.get("cost_usd"),
                "context_tokens": data.get("context_tokens"), "context_limit": data.get("context_limit"),
                "total_input_tokens": data.get("total_input_tokens"),
                "total_output_tokens": data.get("total_output_tokens"),
            })
        # A1: "collapses to a Thought-for summary ... or is removed when
        # there was none" -- resolved BEFORE finish_open_streams below
        # (which only pops bookkeeping/harvests plain-text, never touches
        # the DOM) so the two never race over the same widget.
        await app.transcript.finish_phase_line(turn, agent_id=agent_id)
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
            # A3: "After turn_done the cluster returns to idle" -- also
            # resets the received-token counter/running tool name, so
            # neither can ever carry a stale reading into the next turn.
            app.status_bar.go_idle()
        await app.transcript.finish_open_streams(agent_id=agent_id)
        if agent_id is None:
            app.on_turn_done(data.get("reason", "end_turn"))
    elif kind == "phase":
        # Halo 2.0.1 W2b (liveness-tips-brief Part A1/A3/A5): drives the
        # transcript's own live phase line (every state, main session or a
        # sub-agent's own call alike -- `agent_id` threaded straight
        # through) and, separately, whichever COMPACT live summary this
        # call belongs to -- the main status bar for `agent_id is None`,
        # that sub-agent's own `SubAgentCard` otherwise. See `_phase_word_
        # for`'s own docstring for the one (state, kind) -> word mapping
        # both summaries share.
        state = data.get("state")
        word = _phase_word_for(state, data.get("kind"))
        card = None if agent_id is None else app.transcript.subagent_cards.get(agent_id)
        if state == "request_sent":
            if agent_id is None:
                app.status_bar.start_phase_clock(word or "thinking")
            elif card is not None and word:
                card.set_phase_word(word)
            await app.transcript.begin_phase_line(turn, agent_id=agent_id, model_label=data.get("model"))
        elif state == "headers":
            app.transcript.phase_headers(turn, agent_id=agent_id, ttfb_ms=data.get("ttfb_ms"))
        elif state == "first_token":
            if word:
                if agent_id is None:
                    app.status_bar.set_phase_word(word)
                elif card is not None:
                    card.set_phase_word(word)
            app.transcript.phase_first_token(turn, agent_id=agent_id, kind=data.get("kind"))
        elif state == "waiting_for_model":
            if agent_id is None:
                app.status_bar.start_phase_clock("waiting")
            elif card is not None:
                card.set_phase_word("waiting")
            await app.transcript.begin_waiting_line(turn, agent_id=agent_id)
    elif kind == "steer_restart":
        # GLM-brief.md item 3 / W2-plan item 2: the in-flight call was
        # silently aborted (no chunk had arrived yet) and is being resent
        # with the steer appended -- nothing was lost. Mirrors print mode's
        # own `--verbose` one-liner (output.py); shown unconditionally here
        # (never gated on verbose) since the TUI already shows every other
        # steer as a transcript note (steer_queued's own "↳ steering…").
        await app.transcript.add_note("↳ steering (restarting the model call)", kind="steer")
    elif kind == "subagent_start":
        # U5 scope C / H6: `agent/subagent.py` tags THIS event's own
        # top-level `agent_id` with the CHILD's id (`start_ev.agent_id =
        # agent_id`, the same field every other event of that child's own
        # run carries) -- `agent_id` (extracted at the top of this
        # function) is therefore already the right routing key; `data`'s
        # OWN "agent_id" is read only as a defensive fallback (an older/
        # incomplete event shape that set the data field but not the
        # top-level one). Tracked in `subagent_marks` for child-session
        # navigation either way.
        #
        # Halo 2.0.1 W2b (Part A5): a LIVE `SubAgentCard` in place of the
        # old plain note when a child id IS known -- "agent <name> ·
        # <phase> <Ns> · <N> tools", updated from that child's own `phase`/
        # `tool_use_ready` events (the `elif kind == "phase"`/`tool_use_
        # ready` branches above, matched by this SAME agent_id) and frozen
        # on `subagent_end` below.
        name = data.get("name", "?")
        child_agent_id = agent_id or data.get("agent_id")
        if child_agent_id:
            from halo_harness.tui.widgets.cards import SubAgentCard
            card = SubAgentCard(agent_id=child_agent_id, name=name)
            await app.transcript.mount_subagent_card(card)
        else:
            widget = await app.transcript.add_note(f"→ sub-agent: {name}", kind="subagent")
            app.transcript.subagent_marks.append(widget)
    elif kind == "subagent_end":
        child_agent_id = agent_id or data.get("agent_id")
        card = app.transcript.subagent_cards.pop(child_agent_id, None) if child_agent_id else None
        if card is not None:
            card.finish()
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
