"""rolo_claude.config_cli -- `rolo-claude config ...` subcommand (U0 scope
A, harness-only: not one of Claude Code's own top-level commands). Reads
and writes THIS harness's own small local store, `~/.rolo-claude/config.json`
(theme, and whatever else accumulates there later, e.g. a model preload
list) -- never Claude Code's `settings.json`, which this project only ever
reads. `rolo-claude config` / `config list` prints every key; `config get
<key>` prints one value; `config set <key> <value>` persists one (the value
is parsed as JSON when possible, e.g. `true`/`123`/`"a string"`, so a caller
can set non-string values too, else stored as the raw string given).
"""

from __future__ import annotations

import argparse
import json
import sys

from rolo_claude import theme as theme_mod


def _print_all() -> int:
    data = theme_mod.load_config()
    if not data:
        print("(no config set -- ~/.rolo-claude/config.json is empty or missing)")
        return 0
    for key in sorted(data):
        print(f"{key}={json.dumps(data[key])}")
    return 0


def _cmd_get(rest: list) -> int:
    parser = argparse.ArgumentParser(prog="rolo-claude config get", add_help=True)
    parser.add_argument("key")
    args = parser.parse_args(rest)
    data = theme_mod.load_config()
    if args.key not in data:
        print(f"rolo-claude config: {args.key!r} is not set", file=sys.stderr)
        return 1
    print(json.dumps(data[args.key]))
    return 0


def _cmd_set(rest: list) -> int:
    parser = argparse.ArgumentParser(prog="rolo-claude config set", add_help=True)
    parser.add_argument("key")
    parser.add_argument("value")
    args = parser.parse_args(rest)
    try:
        value = json.loads(args.value)
    except (ValueError, TypeError):
        value = args.value
    if args.key == "theme" and not theme_mod.is_valid_theme(value):
        print(f"rolo-claude config: not a valid theme name: {value!r} "
              f"(expected one of {sorted(theme_mod.VALID_THEMES)})", file=sys.stderr)
        return 2
    theme_mod.set_config_value(args.key, value)
    print(f"{args.key}={json.dumps(value)}")
    return 0


def cmd_config(argv: list) -> int:
    if not argv or argv[0] == "list":
        return _print_all()
    if argv[0] in ("-h", "--help"):
        print("Usage: rolo-claude config [list|get <key>|set <key> <value>]")
        return 0
    if argv[0] == "get":
        return _cmd_get(argv[1:])
    if argv[0] == "set":
        return _cmd_set(argv[1:])
    # `config key=value` (Claude Code's own slash-command shorthand,
    # accepted here too since it reads naturally as a single argv token).
    if "=" in argv[0] and len(argv) == 1:
        key, _, value = argv[0].partition("=")
        return _cmd_set([key, value])
    print(f"rolo-claude config: unrecognized arguments: {' '.join(argv)}", file=sys.stderr)
    return 2
