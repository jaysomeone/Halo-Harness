"""halo_harness.org_cli -- Halo 2.0.2 round 2 (brief B), extended round D
(brief item 1/3/4): `halo org list|show|new|install|edit|run <name>
"<goal>"|export|import|resume` over `~/.halo/orgs/<name>.json`
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
import shlex
import shutil
import subprocess
import sys


def _cmd_list(rest: list) -> int:
    """2.0.2 round D: shows each org's own one-line README (its
    `description` field, docs/ORGS.md's schema table) -- used to print
    bare names only; `commands/builtins.py::_cmd_org`'s own "list" branch
    (the `/org list` TUI/print-mode form) already did this, this CLI
    entry point just catches up to match it."""
    from halo_harness.orgs import load_org, list_orgs
    argparse.ArgumentParser(prog="halo org list", add_help=True).parse_args(rest)
    names = list_orgs()
    if not names:
        print("No organizations saved yet.")
        return 0
    for n in names:
        org = load_org(n)
        desc = org.get("description") if org else None
        print(f"{n}" + (f" -- {desc}" if desc else ""))
    return 0


def _cmd_install(rest: list) -> int:
    """Halo 2.0.2 round D (brief item 1): `halo org install <name>
    [--force]` -- copies a shipped or saved template into `~/.halo/orgs/`
    (`orgs.install_org_template`), refusing to overwrite an existing file
    there without `--force`."""
    from halo_harness.orgs import install_org_template
    parser = argparse.ArgumentParser(prog="halo org install", add_help=True)
    parser.add_argument("name")
    parser.add_argument("--force", action="store_true", help="overwrite an existing organization of the same name")
    args = parser.parse_args(rest)
    ok, problems = install_org_template(args.name, force=args.force)
    if not ok:
        print(f"halo org install: {'; '.join(problems)}", file=sys.stderr)
        return 1
    print(f"Installed organization template {args.name!r} to ~/.halo/orgs/{args.name}.json.")
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


def _run_editor(editor: str, path, *, label: str) -> int:
    """2.0.2 review finding 36: `subprocess.call([editor, str(path)])`
    passed the WHOLE `$EDITOR` string as a single argv[0] -- `EDITOR=
    "code --wait"` tried to exec a literal file named "code --wait"
    (space included), and even a bare `EDITOR=code` on Windows needs
    `code.cmd`, which `subprocess.call` never resolves itself without a
    shell. Both used to crash with an uncaught `FileNotFoundError`
    traceback instead of a clean message. `shlex.split` separates the
    command from its own flags; `shutil.which` resolves the real
    executable (PATHEXT-aware on Windows, so `code` -> `code.cmd` needs
    no `shell=True` at all); any remaining `OSError` (a genuinely
    unresolvable editor) is caught and reported instead of raising."""
    argv = shlex.split(editor) or [editor]
    argv[0] = shutil.which(argv[0]) or argv[0]
    try:
        return subprocess.call([*argv, str(path)])
    except OSError as e:
        print(f"{label}: could not launch {editor!r}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


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
    rc = _run_editor(editor, path, label="halo org edit")
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


def _cmd_export(rest: list) -> int:
    """Halo 2.0.2 round D (brief item 3): `halo org export <name> [file]`
    -- plain JSON (`orgs.load_org`'s own validated shape, so it always
    carries a "name"), written to `file` when given, else stdout (so it
    can be piped/redirected -- the same default `halo export` already
    uses)."""
    from halo_harness.orgs import load_org
    parser = argparse.ArgumentParser(prog="halo org export", add_help=True)
    parser.add_argument("name")
    parser.add_argument("file", nargs="?", default=None, help="write here instead of stdout")
    args = parser.parse_args(rest)
    org = load_org(args.name)
    if org is None:
        print(f"halo org export: no such organization {args.name!r} (or it failed validation)", file=sys.stderr)
        return 1
    text = json.dumps(org, indent=2, sort_keys=True) + "\n"
    if args.file:
        from pathlib import Path
        try:
            Path(args.file).write_text(text, encoding="utf-8")
        except OSError as e:
            print(f"halo org export: could not write {args.file}: {e}", file=sys.stderr)
            return 1
        print(f"halo org export: wrote {args.name!r} to {args.file}.", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def _cmd_import(rest: list) -> int:
    """Halo 2.0.2 round D (brief item 3): `halo org import <file>` --
    plain JSON, validated the same way any other org write is
    (`orgs.save_org` -> `validate_org`: role names, reports, budgets),
    with every problem listed, not just the first. Falls back to the
    file's own basename when the JSON has no usable "name" field (a
    hand-written template need not repeat the name a positional CLI
    argument already gives it)."""
    from pathlib import Path
    from halo_harness.orgs import save_org
    parser = argparse.ArgumentParser(prog="halo org import", add_help=True)
    parser.add_argument("file")
    args = parser.parse_args(rest)
    path = Path(args.file)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"halo org import: could not read {args.file}: {e}", file=sys.stderr)
        return 1
    try:
        data = json.loads(raw)
    except ValueError as e:
        print(f"halo org import: {args.file} is not valid JSON: {e}", file=sys.stderr)
        return 1
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name.strip():
        name = path.stem
    ok, problems = save_org(name, data)
    if not ok:
        print(f"halo org import: {args.file} is not a valid organization:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"Imported organization {name!r} from {args.file}.")
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
    parser.add_argument("name_or_goal")
    parser.add_argument("goal", nargs="?", default=None)
    parser.add_argument("--model", default=None)
    # Halo 2.0.2 round D (brief item 2): "--yes/dontAsk accept
    # automatically" -- a run with no TTY to print an approval-gate
    # prompt to (or one that just wants every gate out of its way) sets
    # this directly on the session object, the SAME post-construction-
    # attribute convention `_isolation_worktree_path` etc. already use;
    # `agent/subagent.py::_apply_approval_gate` reads it generically.
    parser.add_argument("--yes", action="store_true",
                         help="accept any approval-gated position's result automatically, never prompting")
    args = parser.parse_args(rest)
    # Halo 2.0.2 round 7 (init wizard brief item 3): "orgs.default, used
    # by /org run with no name" -- `halo org run "<goal>"` (one
    # positional) uses it; `halo org run <name> "<goal>"` (two) names one
    # explicitly, unchanged from before this existed.
    if args.goal is None:
        from halo_harness.orgs import default_org_name
        name, goal = default_org_name(), args.name_or_goal
        if name is None:
            print('halo org run: no organization name given, and no default is set -- '
                  '`halo org run <name> "<goal>"`, or set one with `halo setup orgs`.', file=sys.stderr)
            return 2
    else:
        name, goal = args.name_or_goal, args.goal
    build = headless.build_session(cwd=Path.cwd(), bare=True, print_mode=True, model_ref_raw=args.model)
    session = build.session
    session.auto_accept_approvals = args.yes
    try:
        _events, result = run_org_call(
            runtime=session.agent_runtime, tool_id="cli-org-run", tool_name="Agent",
            tool_input={"org": name, "prompt": goal, "description": f"Run org {name}"},
        )
        print(result.content)
        return 1 if result.is_error else 0
    finally:
        try:
            session.job_registry.kill_all()
        except Exception:
            pass


def _cmd_resume(rest: list) -> int:
    """Halo 2.0.2 round D (brief item 4): `halo org resume <session-id>`
    -- continues an interrupted org run from that (past, possibly from a
    whole different process) session's own saved run record and shared
    task board (`agent.subagent.resume_org_run`). `<session-id>` is
    whatever `agent.sessions.resolve_resume` already accepts for `-r`/
    `--resume` (an exact id, a `.jsonl` path, or a title/first-message
    substring match)."""
    from pathlib import Path
    from halo_harness import headless
    from halo_harness.agent import sessions as agent_sessions
    from halo_harness.agent.log import SessionLog
    from halo_harness.agent.subagent import resume_org_run
    parser = argparse.ArgumentParser(prog="halo org resume", add_help=True)
    parser.add_argument("session_id")
    parser.add_argument("--cwd", default=None, help="project directory the session belongs to (default: cwd)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--yes", action="store_true",
                         help="accept any approval-gated position's result automatically, never prompting")
    args = parser.parse_args(rest)
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    resolved_id, err = agent_sessions.resolve_resume(cwd, args.session_id)
    if resolved_id is None:
        print(f"halo org resume: {err}", file=sys.stderr)
        return 2
    log = SessionLog(cwd, session_id=resolved_id)
    session_dir = log.dir / log.session_id
    build = headless.build_session(cwd=cwd, bare=True, print_mode=True, model_ref_raw=args.model)
    session = build.session
    session.auto_accept_approvals = args.yes
    try:
        _events, result = resume_org_run(
            runtime=session.agent_runtime, tool_id="cli-org-resume", tool_name="Agent", session_dir=session_dir,
        )
        print(result.content)
        return 1 if result.is_error else 0
    finally:
        try:
            session.job_registry.kill_all()
        except Exception:
            pass


_SUBCOMMANDS = {"list": _cmd_list, "show": _cmd_show, "new": _cmd_new, "edit": _cmd_edit, "run": _cmd_run,
                 "install": _cmd_install, "export": _cmd_export, "import": _cmd_import, "resume": _cmd_resume}

_USAGE = ('usage: halo org list|show <name>|new <name>|edit <name>|install <name> [--force]|\n'
          '                   run [<name>] "<goal>"|export <name> [file]|import <file>|resume <session-id>')


def cmd_org(argv: list) -> int:
    """`halo org list|show <name>|new <name>|edit <name>|install <name>
    [--force]|run [<name>] "<goal>"|export <name> [file]|import <file>|
    resume <session-id>` (`run` with no name uses `orgs.default`, see
    `halo setup orgs`)."""
    if not argv:
        print(_USAGE, file=sys.stderr)
        return 2
    if argv[0] in ("-h", "--help"):
        print(_USAGE)
        return 0
    sub, rest = argv[0], argv[1:]
    fn = _SUBCOMMANDS.get(sub)
    if fn is None:
        print(f"halo org: unknown subcommand {sub!r} (known: {', '.join(sorted(_SUBCOMMANDS))})", file=sys.stderr)
        return 2
    return fn(rest)
