"""rolo_claude.tui.bootstrap -- builds a real `agent.loop.Session` +
`controller.Controller` for an interactive TUI launch.

u2-h3b finding 9: this used to be its own ~190-line hand-copy of
`headless.py.run_print_mode`'s setup (kept "in spirit" only, per this
module's old docstring) -- and it had already drifted: no server-level
`alwaysLoad`, no "keep ToolSearch when the deferred pool is non-empty"
rule, so `rolo-claude --tools Read,Bash` left every deferred MCP tool
unreachable in the TUI while the prompt still said "call ToolSearch".
Both entry points now build through `headless.build_session` (the ONE
shared builder) and can never drift like that again; this module's own
job shrinks to translating `args` (an argparse.Namespace) into that
function's keyword arguments, then wrapping the result in a `Controller`
plus the TUI's own `/mcp` status+reconnect closures.
"""

from __future__ import annotations

import sys
from pathlib import Path

from rolo_claude.controller import Controller
from rolo_claude.headless import attach_cli_files, build_session


def build_controller(args) -> "tuple[Controller, object, object]":
    """`args`: an argparse.Namespace with the same attributes cli.py's flag
    table produces (only the ones relevant to an interactive launch are
    read). Returns `(controller, registry, facade)`, NOT yet started
    (`controller.start()` is the caller's job, once the App is ready to
    receive events)."""
    cwd = Path(args.cwd).resolve() if getattr(args, "cwd", None) else Path.cwd()

    build = build_session(
        cwd=cwd, model_ref_raw=getattr(args, "model", None),
        small_model_ref_raw=getattr(args, "small_model", None),
        settings_flag=getattr(args, "settings", None), setting_sources=None,
        effort=getattr(args, "effort", None),
        allowed_tools=getattr(args, "allowed_tools", None),
        disallowed_tools=getattr(args, "disallowed_tools", None),
        permission_mode=getattr(args, "permission_mode", None),
        dangerously_skip_permissions=bool(getattr(args, "dangerously_skip_permissions", False)),
        tools=getattr(args, "tools", None), add_dir=getattr(args, "add_dir", None),
        bare=bool(getattr(args, "bare", False)), session_id=getattr(args, "session_id", None),
        max_turns=getattr(args, "max_turns", None) or 50,
        append_system_prompt=getattr(args, "append_system_prompt", None),
        chrome=bool(getattr(args, "chrome", False)), no_chrome=bool(getattr(args, "no_chrome", False)),
        playwright=bool(getattr(args, "playwright", False)),
        playwright_cdp=getattr(args, "playwright_cdp", None),
        playwright_headless=bool(getattr(args, "playwright_headless", False)),
        mcp_config=getattr(args, "mcp_config", None),
        strict_mcp_config=bool(getattr(args, "strict_mcp_config", False)),
        print_mode=False,
    )
    attach_cli_files(build.session, getattr(args, "file", None), cwd=cwd)

    def _mcp_status_fn() -> dict:
        if build.mcp_manager is None:
            return {"connected": 0, "total": 0}
        rows = build.mcp_manager.status()
        return {"connected": sum(1 for r in rows if r.get("state") == "connected"), "total": len(rows)}

    def _reconnect_fn(name: str, abort=None) -> list:
        """u2-h3b finding 9: `abort` (default None, so a plain
        `fn(name)` call -- e.g. from a unit test -- is unchanged) lets a
        caller running this on its OWN abort-aware worker thread (the
        `/mcp` dialog's reconnect action, `tui/dialogs/mcp_status.py`)
        cut short a reconnect against a hung/slow server instead of
        freezing for its full timeout; `Controller.reconnect_mcp` (which
        calls this) is what actually threads a real Event through, and
        (H9 bug fix, item 11) is also where the config-file resync now
        happens -- BEFORE this plain restart -- so this function itself
        stays the simple "just restart this one handle" primitive."""
        if build.mcp_manager is None:
            return [f"MCP support is not enabled this session ({name} unchanged)."]
        ok = build.mcp_manager.reconnect(name, abort=abort)
        row = next((r for r in build.mcp_manager.status() if r.get("name") == name), None)
        state = row.get("state") if row else "unknown"
        return [f"{name}: {'connected' if ok else 'failed'} (state={state})"]

    controller = Controller(
        session=build.session, cwd=cwd, state_dir=build.state_dir, routes=build.routes,
        registry=build.command_registry, facade=build.facade,
        mcp_status_fn=_mcp_status_fn, reconnect_fn=_reconnect_fn, settings=build.settings,
    )
    controller.mcp_manager = build.mcp_manager  # tui/app.py's clean shutdown hook
    for n in build.mcp_notices:
        print(f"[rolo-claude] mcp: {n}", file=sys.stderr)
    return controller, build.command_registry, build.facade
