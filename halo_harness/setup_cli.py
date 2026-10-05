"""halo_harness.setup_cli -- `halo setup [roles|orgs]` (Halo 2.0.2 round 7,
init wizard brief item 3): the SAME Roles/Organizations setup screens
`halo init`'s own wizard shows, reachable again later without re-running
the whole provider flow -- "a setup screen should pop up to set those
features up in addition to doing it within halo" (owner 2026-10-03).

On a real terminal, opens the wizard starting at the matching step (bare
`halo setup` chains roles -> orgs -> a short summary, "the set up roles
and orgs later" path); with no TTY (or if the Textual app itself fails to
run -- the same "interactive picker failed -> fallback" shape every other
picker in this package already uses), prints the current roles table /
orgs list instead of blocking, and exits 0. `/setup` (the TUI slash
command, `tui/slash.py::_handle_setup`) is the other way to reach this,
pushed onto a LIVE session instead of a standalone app.
"""

from __future__ import annotations

import sys


def _print_roles_table() -> int:
    from halo_harness.roles_cli import _cmd_table
    return _cmd_table()


def _print_orgs_list() -> int:
    from halo_harness.org_cli import _cmd_list
    return _cmd_list([])


def _print_both() -> int:
    rc1 = _print_roles_table()
    print()
    rc2 = _print_orgs_list()
    return rc1 or rc2


_STEP_KEYS = {"roles": ("roles",), "orgs": ("orgs",), "": ("roles", "orgs", "summary")}
_NO_TTY_FALLBACK = {"roles": _print_roles_table, "orgs": _print_orgs_list, "": _print_both}


def cmd_setup(argv: list) -> int:
    sub = argv[0] if argv else ""
    if sub in ("-h", "--help"):
        print("usage: halo setup [roles|orgs]")
        return 0
    if sub not in _STEP_KEYS:
        print(f"halo setup: unknown subcommand {sub!r} (known: roles, orgs)", file=sys.stderr)
        return 2
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _NO_TTY_FALLBACK[sub]()
    try:
        from pathlib import Path
        from halo_harness.tui.dialogs.init_wizard import InitWizardApp, WizardState
        state = WizardState(cwd=Path.cwd(), step_keys=_STEP_KEYS[sub])
        InitWizardApp(state).run()
        return 0
    except Exception as e:
        print(f"[WARN] the setup screen failed to run ({type(e).__name__}: {e}) -- "
              f"falling back to a plain listing.", file=sys.stderr)
        return _NO_TTY_FALLBACK[sub]()
