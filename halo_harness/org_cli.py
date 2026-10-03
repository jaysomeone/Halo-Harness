"""halo_harness.org_cli -- Halo 2.0.2 round 2 (brief B): `halo org
list|show|new|edit|run <name> "<goal>"` over `~/.halo/orgs/<name>.json`
organizations (`halo_harness.orgs`'s own CRUD/validation functions do the
actual work; this module is just argparse + formatting, the same split
`roles_cli.py` already uses for role templates). `/org load <name>`
(re-install a built-in, overwriting a local copy) is TUI-only, per the
brief's own command list -- not exposed here.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


def _cmd_list(rest: list) -> int:
    from halo_harness.orgs import list_orgs
    argparse.ArgumentParser(prog="halo org list", add_help=True).parse_args(rest)
    names = list_orgs()
    if not names:
        print("No organizations saved yet.")
        return 0
    for n in names:
        print(n)
    return 0


def _cmd_show(rest: list) -> int:
    from halo_harness.orgs import describe, load_org
    parser = argparse.ArgumentParser(prog="halo org show", add_help=True)
    parser.add_argument("name")
    args = parser.parse_args(rest)
    org = load_org(args.name)
    if org is None:
        print(f"halo org show: no such organization {args.name!r} (or it failed validation)", file=sys.stderr)
        return 1
    print(describe(org))
    return 0


def _cmd_new(rest: list) -> int:
    from halo_harness.orgs import save_org
    parser = argparse.ArgumentParser(prog="halo org new", add_help=True)
    parser.add_argument("name")
    parser.add_argument("--description", default="")
    args = parser.parse_args(rest)
    data = {"name": args.name, "description": args.description,
            "positions": [{"title": "Orchestrator", "role": "orchestrator", "reports": [], "instructions": ""}]}
    ok, problems = save_org(args.name, data)
    if not ok:
        print(f"halo org new: {'; '.join(problems)}", file=sys.stderr)
        return 1
    print(f"Created organization {args.name!r} with a single 'Orchestrator' root position.")
    return 0


def _cmd_edit(rest: list) -> int:
    """No form here (that's the TUI's `/org edit`, `tui/dialogs/
    org_editor.py`) -- `$EDITOR`/`$VISUAL` on the raw JSON file, creating
    a one-position starter org first if `name` doesn't exist yet."""
    from halo_harness.orgs import load_org, orgs_dir, save_org, validate_org
    parser = argparse.ArgumentParser(prog="halo org edit", add_help=True)
    parser.add_argument("name")
    args = parser.parse_args(rest)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        print("halo org edit: no $VISUAL/$EDITOR set", file=sys.stderr)
        return 1
    if load_org(args.name) is None:
        ok, problems = save_org(args.name, {"name": args.name, "positions": [
            {"title": "Orchestrator", "role": "orchestrator", "reports": [], "instructions": ""}]})
        if not ok:
            print(f"halo org edit: {'; '.join(problems)}", file=sys.stderr)
            return 1
    path = orgs_dir() / f"{args.name}.json"
    rc = subprocess.call([editor, str(path)])
    if rc != 0:
        return rc
    try:
        problems = validate_org(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        print(f"halo org edit: could not re-read {path}: {e}", file=sys.stderr)
        return 1
    if problems:
        print(f"halo org edit: saved file is invalid: {'; '.join(problems)}", file=sys.stderr)
        return 1
    print(f"Saved organization {args.name!r}.")
    return 0


def _cmd_run(rest: list) -> int:
    """Builds a bare, non-interactive print-mode session (`headless.
    build_session`, the SAME construction `-p` itself uses) to act as the
    org's own caller, then runs it through `agent.subagent.run_org_call`
    directly -- no model turn is wasted deciding to call the Agent tool,
    since the goal is unconditional here. `--model` is an optional
    override for whatever position (if any) has neither its own `role`
    nor `model` set (every built-in's own positions always set one, so
    this normally never matters) -- also what makes this hermetically
    testable against a mock upstream."""
    from pathlib import Path
    from halo_harness import headless
    from halo_harness.agent.subagent import run_org_call
    parser = argparse.ArgumentParser(prog="halo org run", add_help=True)
    parser.add_argument("name")
    parser.add_argument("goal")
    parser.add_argument("--model", default=None)
    args = parser.parse_args(rest)
    build = headless.build_session(cwd=Path.cwd(), bare=True, print_mode=True, model_ref_raw=args.model)
    session = build.session
    try:
        _events, result = run_org_call(
            runtime=session.agent_runtime, tool_id="cli-org-run", tool_name="Agent",
            tool_input={"org": args.name, "prompt": args.goal, "description": f"Run org {args.name}"},
        )
        print(result.content)
        return 1 if result.is_error else 0
    finally:
        try:
            session.job_registry.kill_all()
        except Exception:
            pass


_SUBCOMMANDS = {"list": _cmd_list, "show": _cmd_show, "new": _cmd_new, "edit": _cmd_edit, "run": _cmd_run}


def cmd_org(argv: list) -> int:
    """`halo org list|show <name>|new <name>|edit <name>|run <name>
    "<goal>"`."""
    if not argv:
        print('usage: halo org list|show <name>|new <name>|edit <name>|run <name> "<goal>"', file=sys.stderr)
        return 2
    if argv[0] in ("-h", "--help"):
        print('usage: halo org list|show <name>|new <name>|edit <name>|run <name> "<goal>"')
        return 0
    sub, rest = argv[0], argv[1:]
    fn = _SUBCOMMANDS.get(sub)
    if fn is None:
        print(f"halo org: unknown subcommand {sub!r} (known: {', '.join(sorted(_SUBCOMMANDS))})", file=sys.stderr)
        return 2
    return fn(rest)
