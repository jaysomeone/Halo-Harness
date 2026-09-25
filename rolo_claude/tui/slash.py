"""rolo_claude.tui.slash -- slash-command handling for the TUI. A handful of
"ui"-kind builtins (D-TUI: "still return a real string here -- they just
describe what needs the interactive TUI instead of performing it") get REAL
interactive behavior in here -- a picker/dialog, or a direct transcript/
theme mutation -- before ever reaching `Controller.run_slash`'s own
headless-text fallback (used for everything else: /help, /cost, /context,
/status, /skills, /agents, /effort, /doctor, custom commands, skills).
"""

from __future__ import annotations


async def handle_slash(app, name: str, args: str) -> None:
    name = name.lower()
    handler = {
        "model": _handle_model, "mcp": _handle_mcp, "clear": _handle_clear,
        "resume": _handle_resume, "permissions": _handle_permissions,
        "exit": _handle_exit, "quit": _handle_exit, "theme": _handle_theme,
        # U5 scope C: sessions UX.
        "rename": _handle_rename, "fork": _handle_fork, "export": _handle_export,
        "stats": _handle_stats,
        # U5 scope B: git-shadow rewind.
        "rewind": _handle_rewind, "undo": _handle_undo, "redo": _handle_redo,
        # U5 scope A: keymap.
        "keybindings": _handle_keybindings,
    }.get(name)
    if handler is not None:
        await handler(app, args)
        return
    result = app.controller.run_slash(name, args)
    if result:
        await app.transcript.add_note(result, kind="command")


async def _handle_model(app, args: str) -> None:
    from rolo_claude.tui.dialogs.model_picker import ModelPicker

    args = args.strip()
    if args:
        _apply_model(app, args)
        return
    models = app.controller.list_models()
    app.push_screen(ModelPicker(models, current=app.status_bar.model), lambda ref: _apply_model(app, ref))


def _apply_model(app, ref) -> None:
    if not ref:
        return
    err = app.controller.set_model(ref)
    if err:
        app.notify(err, severity="error", title="/model")
    else:
        app.notify(f"Model set to {ref}", title="/model")


async def _handle_mcp(app, _args: str) -> None:
    from rolo_claude.tui.dialogs.mcp_status import McpStatus

    list_fn = getattr(app.controller, "list_mcp_servers", None)
    servers = list_fn() if list_fn is not None else []
    app.push_screen(McpStatus(servers, reconnect=app.controller.reconnect_mcp,
                               approve=app.controller.approve_mcp_server))


async def _handle_clear(app, _args: str) -> None:
    # U5 must-do: /clear now actually starts a NEW session log
    # (SessionEnd(clear) + SessionStart(clear), off the UI thread via
    # Controller.clear_session) -- the old version only wiped the visible
    # transcript WIDGET, leaving the underlying context (and everything
    # the next request would derive from it) completely untouched.
    clear = getattr(app.controller, "clear_session", None)
    if not callable(clear):
        await app.transcript.clear_view()
        await app.transcript.add_note("Conversation view cleared.", kind="note")
        return
    error = clear()
    if error:
        await app.transcript.add_note(error, kind="error")
        return
    await app.transcript.clear_view()
    await app.transcript.add_note("Conversation cleared -- starting a new session context.", kind="note")


async def _handle_resume(app, _args: str) -> None:
    # U5/review must-do: `list_sessions()` is file I/O -- off the UI thread.
    app.run_worker(lambda: _resume_list_worker(app), thread=True, name="list-sessions")


def _resume_list_worker(app) -> None:
    sessions = app.controller.list_sessions()
    app.call_from_thread(_open_resume_picker, app, sessions)


def _open_resume_picker(app, sessions) -> None:
    from rolo_claude.tui.dialogs.session_picker import SessionPicker

    def _on_pick(session_id) -> None:
        if session_id:
            app.controller.resume(session_id)

    app.push_screen(SessionPicker(sessions), _on_pick)


async def _handle_permissions(app, _args: str) -> None:
    from rolo_claude.tui.dialogs.permissions import PermissionsDialog

    list_fn = getattr(app.controller, "list_permission_rules", None)
    rules = list_fn() if list_fn is not None else []
    app.push_screen(PermissionsDialog(rules, mode=app.status_bar.mode,
                                       add_rule=lambda text: app.controller.add_permission_rule(text, "session")))


async def _handle_exit(app, _args: str) -> None:
    await app.action_quit_now()


async def _handle_theme(app, args: str) -> None:
    from rolo_claude import theme as theme_mod

    name = args.strip()
    if not name:
        app.notify(f"Current theme: {app.theme_name}", title="/theme")
        return
    if not theme_mod.is_valid_theme(name):
        app.notify(f"Not a valid theme name: {name} (expected one of {sorted(theme_mod.VALID_THEMES)})",
                   severity="error", title="/theme")
        return
    theme_mod.persist_theme(name)
    app.apply_theme(name)
    app.notify(f"Theme set to {name}", title="/theme")


# ============================================================================
# U5 scope C: session titles/rename/fork/export/stats.
# ============================================================================

async def _handle_rename(app, args: str) -> None:
    title = args.strip()
    get_title = getattr(app.controller, "get_title", None)
    rename = getattr(app.controller, "rename_session", None)
    if not title:
        current = get_title() if callable(get_title) else ""
        app.notify(f"Current title: {current or '(untitled)'} -- usage: /rename <title>", title="/rename")
        return
    if not callable(rename):
        app.notify("Renaming needs a real session.", severity="warning", title="/rename")
        return
    rename(title)
    app.notify(f"Session renamed to {title!r}", title="/rename")


async def _handle_fork(app, _args: str) -> None:
    fork = getattr(app.controller, "fork_session", None)
    if not callable(fork):
        app.notify("Forking needs a real session.", severity="warning", title="/fork")
        return
    new_id = fork()
    await app.transcript.add_note(
        f"⑂ Forked into a new session: {new_id} (this session now continues independently).",
        kind="command")
    app.notify(f"Forked to {new_id}", title="/fork")


async def _handle_export(app, args: str) -> None:
    export_fn = getattr(app.controller, "export_session", None)
    if not callable(export_fn):
        app.notify("Export needs a real session.", severity="warning", title="/export")
        return
    tokens = args.split()
    sanitize = "--sanitize" in tokens
    files = [t for t in tokens if t != "--sanitize"]
    out_path = export_fn(sanitize=sanitize, path=(files[0] if files else None))
    await app.transcript.add_note(f"⬇ Exported to {out_path}{' (sanitized)' if sanitize else ''}.",
                                   kind="command")


async def _handle_stats(app, _args: str) -> None:
    stats_fn = getattr(app.controller, "session_stats", None)
    if not callable(stats_fn):
        app.notify("Stats need a real session.", severity="warning", title="/stats")
        return
    stats = stats_fn()
    lines = [f"Turns: {stats.get('turns', 0)}", f"Total cost: ${stats.get('total_cost_usd', 0.0):.4f}"]
    for model, bucket in sorted((stats.get("per_model") or {}).items()):
        lines.append(f"  {model}: {bucket['calls']} call(s), "
                     f"{bucket['input_tokens']}in/{bucket['output_tokens']}out tok, ${bucket['cost_usd']:.4f}")
    for name, n in sorted((stats.get("tool_counts") or {}).items()):
        lines.append(f"  tool {name}: {n} call(s)")
    await app.transcript.add_note("\n".join(lines), kind="command")


# ============================================================================
# U5 scope B: git-shadow rewind (/rewind [step-id], /undo, /redo) -- a
# RewindCard always confirms before touching the real working tree.
# ============================================================================

async def _handle_rewind(app, args: str) -> None:
    step_id = args.strip()
    steps_fn = getattr(app.controller, "shadow_steps", None)
    if not callable(steps_fn):
        app.notify("Rewind needs a real session.", severity="warning", title="/rewind")
        return
    if step_id:
        step = next((s for s in steps_fn() if step_id in (s.get("id"), s.get("hash"))), None)
        if step is None:
            app.notify(f"No recorded step matches {step_id!r}.", severity="error", title="/rewind")
            return
        await _show_rewind_confirmation(app, step, "rewind")
        return
    app.run_worker(lambda: _rewind_picker_worker(app), thread=True, name="rewind-list")


def _rewind_picker_worker(app) -> None:
    steps = app.controller.shadow_steps()
    app.call_from_thread(_open_rewind_picker, app, steps)


def _open_rewind_picker(app, steps: list) -> None:
    from rolo_claude.tui.dialogs.rewind_picker import RewindPicker

    def _on_pick(step_id) -> None:
        if not step_id:
            return
        step = next((s for s in steps if s.get("id") == step_id), None)
        if step is not None:
            app.call_next(_show_rewind_confirmation, app, step, "rewind")

    app.push_screen(RewindPicker(steps), _on_pick)


async def _show_rewind_confirmation(app, step: dict, verb: str) -> None:
    from rolo_claude.tui.widgets.cards import RewindCard

    def on_decide(confirmed: bool) -> None:
        app.clear_pending_card()
        if confirmed:
            app.run_worker(lambda: _apply_rewind_worker(app, step["id"], verb), thread=True, name="rewind-apply")

    card = RewindCard(step=step, verb=verb, on_decide=on_decide)
    await app.transcript.mount_widget(card)
    app.set_pending_card(card)


def _apply_rewind_worker(app, step_id: str, verb: str) -> None:
    result = app.controller.rewind_apply(step_id, verb=verb)
    if result is None:
        app.call_from_thread(app.notify, "Rewind failed (step vanished?).", severity="error", title=f"/{verb}")
        return
    files = result.get("files") or []
    text = f"↩ {verb.capitalize()} complete -- restored {len(files)} file(s) to step {step_id}."
    app.call_from_thread(app.transcript.add_note, text, kind="command")


async def _handle_undo(app, _args: str) -> None:
    await _handle_undo_redo(app, "undo")


async def _handle_redo(app, _args: str) -> None:
    await _handle_undo_redo(app, "redo")


async def _handle_undo_redo(app, verb: str) -> None:
    preview_fn = getattr(app.controller, f"rewind_preview_{verb}", None)
    if not callable(preview_fn):
        app.notify(f"{verb.capitalize()} needs a real session.", severity="warning", title=f"/{verb}")
        return
    step = preview_fn()
    if step is None:
        app.notify(f"Nothing to {verb}.", title=f"/{verb}")
        return
    await _show_rewind_confirmation(app, step, verb)


# ============================================================================
# U5 scope A: /keybindings.
# ============================================================================

async def _handle_keybindings(app, _args: str) -> None:
    keymap = getattr(app, "_keymap", None)
    if keymap is None:
        from rolo_claude.tui.keys import load_keymap
        keymap = load_keymap()
    lines = ["Keybindings (~/.claude/keybindings.json merges onto these):"]
    for ctx in sorted(keymap):
        lines.append(f"  [{ctx}]")
        for chord in sorted(keymap[ctx]):
            lines.append(f"    {chord:<20} {keymap[ctx][chord]}")
    await app.transcript.add_note("\n".join(lines), kind="command")
