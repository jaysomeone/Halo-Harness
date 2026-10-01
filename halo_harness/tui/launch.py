"""halo_harness.tui.launch -- the ONE entry point `cli.py` imports (lazily,
inside the branch that actually needs a full-screen session) to start the
Textual app -- textual/rich are real imports from this point down, never
above it, so `import halo_harness` and `-p` stay dependency-free.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _show_intro_for(args) -> bool:
    """2.0.0 Launch intro gating -- real auto-detection lives HERE, never
    inside `BridgeApp` itself (which defaults `show_intro=False` and only
    ever shows what it's explicitly told to, so every test that builds one
    directly is unaffected). Off for `--demo` (a deterministic scripted
    snapshot -- the brief: "never in --demo snapshots unless asked"), off
    for `--no-intro`, off when `"intro": false` sits in `~/.halo/
    config.json`, and off whenever stdout isn't a real terminal (the
    print-mode/non-interactive case this function is never even reached
    for in practice, plus any other redirected-stdout interactive launch)."""
    if getattr(args, "demo", False):
        return False
    if getattr(args, "no_intro", False):
        return False
    if not sys.stdout.isatty():
        return False
    from halo_harness.theme import get_config_value
    return bool(get_config_value("intro", True))


def run_tui(args) -> int:
    """`args`: the argparse.Namespace `cli.py._build_parser()` produces.
    Bare `halo [PROMPT]` and `halo --demo` (without `-p`, which
    stays the scripted print-mode replay in `testing.fake_controller.
    run_demo`) both land here."""
    from halo_harness.theme import load_persisted_theme, resolve_theme
    from halo_harness.tui.app import BridgeApp

    cwd = Path(args.cwd).resolve() if getattr(args, "cwd", None) else Path.cwd()

    if args.demo:
        controller, registry, facade, tui_setting = _build_demo_controller(args)
    else:
        from halo_harness.tui.bootstrap import build_controller
        controller, registry, facade = build_controller(args)
        cwd = controller.cwd
        tui_setting = facade.settings.tui if facade is not None and facade.settings is not None else None

    settings_theme = facade.settings.theme if facade is not None and facade.settings is not None else None
    theme_name = resolve_theme(cli_theme=getattr(args, "theme", None), settings_theme=settings_theme,
                                persisted_theme=load_persisted_theme())

    app = BridgeApp(
        controller, registry=registry, facade=facade, tool_registry=getattr(facade, "tool_registry", None),
        cwd=cwd, theme_name=theme_name, tui_setting=tui_setting,
        initial_prompt=(args.prompt if not args.demo else None),
        # H13 Part C: set only when `--resume <text>` didn't resolve to
        # exactly one session (`tui/bootstrap.py::build_controller`) -- opens
        # the resume picker, pre-filtered by that text, right after mount.
        initial_resume_filter=getattr(controller, "pending_resume_filter", None),
        no_inline_images=bool(getattr(args, "no_inline_images", False)),
        show_intro=_show_intro_for(args),
    )
    try:
        app.run()
    finally:
        # review finding 5: `app.run()` returning (or raising) via any
        # path OTHER than the app's own `_quit_worker` (an uncaught
        # exception escaping Textual's event loop, e.g.) must still stop
        # the worker thread and close every MCP subprocess -- `quit()` is
        # already idempotent (`quit_called`), so a normal exit that
        # already called it here is a harmless no-op.
        quit_fn = getattr(controller, "quit", None)
        if callable(quit_fn):
            try:
                quit_fn()
            except Exception:
                pass
    return app.return_code if app.return_code is not None else 0


def _build_demo_controller(args):
    """`--demo [--stress N]` without `-p`: the same scripted transcript
    `testing.fake_controller.run_demo` prints, driven through the real
    App/FakeController pair instead -- the manual "stays responsive" check
    (D-TUI/acceptance: `--demo --stress 500`)."""
    from halo_harness.testing.fake_controller import FakeController, stress_turns

    turns = stress_turns(args.stress) if args.stress else None
    controller = FakeController(turns=turns)
    return controller, None, None, None
