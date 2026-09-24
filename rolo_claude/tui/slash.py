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
    await app.transcript.clear_view()
    await app.transcript.add_note(
        "Conversation view cleared (this session's context is unchanged -- /clear is view-only "
        "in this build).", kind="note",
    )


async def _handle_resume(app, _args: str) -> None:
    from rolo_claude.tui.dialogs.session_picker import SessionPicker

    sessions = app.controller.list_sessions()

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
