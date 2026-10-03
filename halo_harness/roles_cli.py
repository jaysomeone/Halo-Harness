"""halo_harness.roles_cli -- Halo 2.0.2 (W7 round 1, brief A.5): `halo
roles template list|save|load|new|edit|show` over `~/.halo/roles/
<name>.json` templates (`halo_harness.roles`'s own CRUD functions do the
actual file work; this module is just argparse + formatting, same split as
`bg_cli.py`/`mcp_cli.py`).

`save`/`new` both take the CURRENT config.json role table as their
starting point for `save` (brief: "save the current table"), and an empty
one for `new` (brief: "a TUI form... or $EDITOR when configured" -- the
CLI's own `edit` always uses `$EDITOR`, there being no form to fall back
to outside the TUI).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


def _cmd_list(rest: list) -> int:
    from halo_harness.roles import list_role_templates
    argparse.ArgumentParser(prog="halo roles template list", add_help=True).parse_args(rest)
    names = list_role_templates()
    if not names:
        print("No role templates saved yet.")
        return 0
    for n in names:
        print(n)
    return 0


def _cmd_show(rest: list) -> int:
    from halo_harness.roles import load_role_template, role_value_parts
    parser = argparse.ArgumentParser(prog="halo roles template show", add_help=True)
    parser.add_argument("name")
    args = parser.parse_args(rest)
    template = load_role_template(args.name)
    if template is None:
        print(f"halo roles template show: no such template {args.name!r} (or it failed validation)", file=sys.stderr)
        return 1
    print(f"{template['name']} -- {template['description'] or '(no description)'}")
    for role_name, value in sorted(template["roles"].items()):
        model, effort = role_value_parts(value)
        print(f"  {role_name}: {model}" + (f" ({effort})" if effort else ""))
    return 0


def _cmd_save(rest: list) -> int:
    from halo_harness.roles import configured_role_table, save_role_template
    parser = argparse.ArgumentParser(prog="halo roles template save", add_help=True)
    parser.add_argument("name")
    parser.add_argument("--description", default="")
    args = parser.parse_args(rest)
    ok, problems = save_role_template(args.name, {"description": args.description, "roles": configured_role_table()})
    if not ok:
        print(f"halo roles template save: {'; '.join(problems)}", file=sys.stderr)
        return 1
    print(f"Saved the current role table as template {args.name!r}.")
    return 0


def _cmd_new(rest: list) -> int:
    from halo_harness.roles import save_role_template
    parser = argparse.ArgumentParser(prog="halo roles template new", add_help=True)
    parser.add_argument("name")
    parser.add_argument("--description", default="")
    args = parser.parse_args(rest)
    ok, problems = save_role_template(args.name, {"description": args.description, "roles": {}})
    if not ok:
        print(f"halo roles template new: {'; '.join(problems)}", file=sys.stderr)
        return 1
    print(f"Created an empty role template {args.name!r}.")
    return 0


def _cmd_load(rest: list) -> int:
    from halo_harness.roles import apply_role_template
    parser = argparse.ArgumentParser(prog="halo roles template load", add_help=True)
    parser.add_argument("name")
    args = parser.parse_args(rest)
    ok, problems = apply_role_template(args.name)
    if not ok:
        print(f"halo roles template load: {'; '.join(problems)}", file=sys.stderr)
        return 1
    print(f"Loaded role template {args.name!r} into ~/.halo/config.json.")
    return 0


def _cmd_edit(rest: list) -> int:
    """No form here (that's the TUI's `/roles edit <name>`, `tui/dialogs/
    roles_editor.py`) -- `$EDITOR`/`$VISUAL` on the raw JSON file, creating
    an empty template first if `name` doesn't exist yet so there's always
    something to open."""
    from halo_harness.roles import load_role_template, role_templates_dir, save_role_template, validate_role_template
    parser = argparse.ArgumentParser(prog="halo roles template edit", add_help=True)
    parser.add_argument("name")
    args = parser.parse_args(rest)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        print("halo roles template edit: no $VISUAL/$EDITOR set", file=sys.stderr)
        return 1
    if load_role_template(args.name) is None:
        save_role_template(args.name, {"description": "", "roles": {}})
    path = role_templates_dir() / f"{args.name}.json"
    rc = subprocess.call([editor, str(path)])
    if rc != 0:
        return rc
    try:
        problems = validate_role_template(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        print(f"halo roles template edit: could not re-read {path}: {e}", file=sys.stderr)
        return 1
    if problems:
        print(f"halo roles template edit: saved file is invalid: {'; '.join(problems)}", file=sys.stderr)
        return 1
    print(f"Saved role template {args.name!r}.")
    return 0


_TEMPLATE_SUBCOMMANDS = {
    "list": _cmd_list, "show": _cmd_show, "save": _cmd_save,
    "new": _cmd_new, "load": _cmd_load, "edit": _cmd_edit,
}


def _cmd_table() -> int:
    """Bare `halo roles`: the configured role table (role, model, effort),
    the same rows `/roles` shows minus the live-session price column; a
    role with nothing configured says "(session model)" because that is
    what it resolves to at run time. Found live on the Kali VM: the bare
    command printed the template usage line and exited 0."""
    from halo_harness.roles import configured_role_table, known_role_names, role_value_parts
    table = configured_role_table()
    names = known_role_names(table)
    w = max(len(n) for n in names)
    print("Configured roles (model / effort; `halo roles template ...` manages saved tables):")
    for name in names:
        model, effort = role_value_parts(table.get(name))
        if model is None and effort is None:
            print(f"  {name:<{w}}  (session model)")
        else:
            print(f"  {name:<{w}}  {model or '(session model)'}  {effort or '-'}")
    return 0


def cmd_roles(argv: list) -> int:
    """`halo roles template <list|show|save|new|load|edit> [name] [...]`
    -- the only subcommand GROUP under `halo roles` today (bare `/roles`
    and `/role` live in the TUI/print-mode slash-command path instead,
    `commands/builtins.py`)."""
    if not argv:
        return _cmd_table()
    if argv[0] in ("-h", "--help"):
        print("usage: halo roles                      show the configured role table", file=sys.stderr)
        print("       halo roles template list|show <name>|save <name>|new <name>|load <name>|edit <name>",
              file=sys.stderr)
        return 0
    if argv[0] != "template":
        print(f"halo roles: unknown subcommand {argv[0]!r} (known: template)", file=sys.stderr)
        return 2
    rest = argv[1:]
    if not rest:
        print("usage: halo roles template list|show <name>|save <name>|new <name>|load <name>|edit <name>",
              file=sys.stderr)
        return 2
    sub, sub_rest = rest[0], rest[1:]
    fn = _TEMPLATE_SUBCOMMANDS.get(sub)
    if fn is None:
        print(f"halo roles template: unknown subcommand {sub!r} (known: {', '.join(sorted(_TEMPLATE_SUBCOMMANDS))})",
              file=sys.stderr)
        return 2
    return fn(sub_rest)
