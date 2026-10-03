"""halo_harness.tui.bootstrap -- builds a real `agent.loop.Session` +
`controller.Controller` for an interactive TUI launch.

u2-h3b finding 9: this used to be its own ~190-line hand-copy of
`headless.py.run_print_mode`'s setup (kept "in spirit" only, per this
module's old docstring) -- and it had already drifted: no server-level
`alwaysLoad`, no "keep ToolSearch when the deferred pool is non-empty"
rule, so `halo --tools Read,Bash` left every deferred MCP tool
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

from halo_harness import events
from halo_harness.cli_flags import cli_flags_from_args
from halo_harness.controller import Controller
from halo_harness.headless import attach_cli_files, build_session, maybe_create_worktree


def build_controller(args) -> "tuple[Controller, object, object]":
    """`args`: an argparse.Namespace with the same attributes cli.py's flag
    table produces (only the ones relevant to an interactive launch are
    read). Returns `(controller, registry, facade)`, NOT yet started
    (`controller.start()` is the caller's job, once the App is ready to
    receive events)."""
    cwd = Path(args.cwd).resolve() if getattr(args, "cwd", None) else Path.cwd()

    # finding 2 (W6a): `cli_flags_from_args(args)` was never called here at
    # all, so EVERY 2.0.1 flag it carries (--restricted, --fallback-model,
    # --plugin-dir/--plugin-url, --betas, --brief, --environment,
    # --forward-subagent-text, ...) was silently accepted and ignored by
    # an interactive launch -- `build_session` below always got
    # `cli_flags=None` -> `{}`. `-w/--worktree` is resolved the same way
    # `run_print_mode` resolves it (the shared `maybe_create_worktree`
    # helper, called before `build_session` so settings/CLAUDE.md/tool
    # access all resolve against the worktree from the first line) --
    # `-w` used to exist only inside `run_print_mode`, so `halo -w` (no
    # `-p`) edited the real working tree.
    cli_flags = cli_flags_from_args(args)
    cwd = maybe_create_worktree(cwd, cli_flags)

    # H13 Part C ("--resume <text> picks the unique match or opens the
    # picker filtered"): resolved BEFORE build_session (which would
    # otherwise silently pick `resolve_resume`'s own "most recent match"
    # guess, or silently start a brand-new session on zero matches, with no
    # feedback either way -- the TUI had no picker wired to `--resume`'s own
    # text at all before this). A real *text* argument (not a bare `-r`/
    # `--resume` flag, and not paired with `--continue`) that resolves to
    # anything OTHER than exactly one session is deferred to the picker,
    # opened already filtered by that same text once the app mounts
    # (`tui/app.py::on_mount`) -- `build_session` then runs with NO resume
    # at all, so it starts an ordinary new session in the meantime.
    pending_resume_filter = None
    resume_arg = getattr(args, "resume", None)
    effective_resume = resume_arg
    if resume_arg and not getattr(args, "continue_", False):
        from halo_harness.agent.sessions import find_resume_matches
        if len(find_resume_matches(cwd, resume_arg)) != 1:
            pending_resume_filter = resume_arg
            effective_resume = None

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
        # H13 Part C bug fix: `--continue`/`--resume`/`--fork-session` were
        # never passed through here at all -- headless.py's own print-mode
        # call site (below, in run_print_mode) always has, but the TUI's
        # `build_session` call silently dropped every one of these three
        # flags, so `halo --resume`/`--continue`/`--fork-session`
        # (without `-p`) always just started a brand-new session with zero
        # feedback. `resume` uses `effective_resume` (None instead of an
        # AMBIGUOUS text -- see `pending_resume_filter` above), never the
        # raw flag value, so an ambiguous/no-match text never lets
        # `resolve_resume`'s own "most recent match" guess silently win.
        continue_=bool(getattr(args, "continue_", False)), resume=effective_resume,
        fork_session_flag=bool(getattr(args, "fork_session", False)),
        print_mode=False, roles_flag=getattr(args, "role", None),
        cli_flags=cli_flags,
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
        if name.startswith("connector__"):
            # W4b connectors bridge: a connector row has no McpManager
            # handle at all (it's a cached claude.ai account connector, not
            # a locally-configured server) -- `r` here forces a fresh
            # `claude mcp list` + tool-name discovery instead.
            from halo_harness.mcp import connectors_bridge
            slug = name[len("connector__"):]
            connectors = connectors_bridge.refresh_now()
            info = next((c for c in connectors if c.slug == slug), None)
            if info is None:
                return [f"{name}: no longer reported by `claude mcp list`."]
            return [connectors_bridge.status_line(info)]
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
    # H13 Part C: `tui/launch.py` reads this straight off the controller
    # (same pattern as `mcp_manager` just above) to tell `BridgeApp` to open
    # the resume picker, pre-filtered, right after mount -- None (the
    # overwhelming majority of launches: no --resume at all, or one that
    # already resolved to exactly one session) means "nothing to do".
    controller.pending_resume_filter = pending_resume_filter
    for n in build.mcp_notices:
        print(f"[halo] mcp: {n}", file=sys.stderr)

    # W5b ("connector cold start, properly"): the TUI's own half of the
    # redesign -- discovery stays entirely in the background (never
    # synchronous here, unlike print mode's opt-in path in `build_session`
    # itself), and `on_done` only runs once a real discovery round actually
    # finished. It adds whatever it found straight into the now-live
    # `SessionCatalog` (safe: `add_connector_tools` takes the catalog's own
    # lock) and pushes ONE transcript line onto `controller.events` --
    # thread-safe and drain-loop-unconditional (`tui/app.py::_drain` reads
    # it 30x/s regardless of whether a turn is active), so this can land
    # before, during or after the user's first turn.
    if build.session_catalog is not None:
        def _on_connectors_discovered(connectors) -> None:
            build.session_catalog.add_connector_tools(connectors)
            plural = "" if len(connectors) == 1 else "s"
            controller.events.put(events.system_note(
                f"{len(connectors)} claude.ai connector{plural} available."))

        # Live on the Kali VM (2.0.1 part 11): a FRESH TUI process has no
        # cached claude.ai auth status yet when this runs, so the kick
        # below is refused by `discovery_eligible()` (cache-only, by
        # design) and nothing ever discovered the connectors -- the
        # transcript note never came, MCP stayed 0/0. The callback is
        # therefore also parked on the controller for `tui/app.py::
        # _prime_auth_status_worker`, which re-kicks the SAME once-per-
        # process discovery the moment its own auth refresh lands and says
        # claude.ai login. The immediate attempt stays for the case where
        # the cache is already primed in this process.
        controller.connectors_discovery_on_done = _on_connectors_discovered
        from halo_harness.mcp import connectors_bridge
        connectors_bridge.ensure_discovered_in_background(on_done=_on_connectors_discovered)

    return controller, build.command_registry, build.facade
