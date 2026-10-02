"""halo_harness.export_cli -- `halo export` subcommand (H8 scope D):
a headless version of the TUI's own `/export` (U5), for a script or a CI
job with no interactive file picker to drive it through. Reads a session's
JSONL log (the latest for the given/current directory, or an explicit
`--session <id-or-prefix>`), optionally sanitizing common credential shapes
out of it, and writes the raw JSONL to a file or stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# H9 whole-tree review finding 4: this is the ONE sanitizer both export
# paths (`halo export --sanitize` below AND the TUI's own `/export
# --sanitize`, controller.py's `sanitize_transcript`) and the deleted
# `session_cli.py`'s functionality all resolve to -- see this module's own
# docstring reference in `controller.py`. 2.0.1 W3a: the actual patterns/
# functions moved to `halo_harness.redact` (re-imported here, so every
# existing `from halo_harness.export_cli import sanitize_text/sanitize_
# node/_SECRET_ENV_NAMES/_TOKEN_PATTERNS/_BEARER_RE` keeps working exactly
# as before) so `halo_harness.bugreport`'s own stronger pass can share the
# SAME base patterns without importing a CLI entry-point module as if it
# were a library.
from halo_harness.redact import (  # noqa: F401 -- re-exported for every existing importer of this module
    _BEARER_RE, _ENV_ASSIGN_RE, _GENERIC_SECRET_NAME, _NAME_ALT, _SECRET_ENV_NAMES, _TOKEN_PATTERNS,
    _redact_env_assign, sanitize_node, sanitize_text,
)


def cmd_export(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="halo export", add_help=True,
                                      description="Export a session's transcript as JSONL (headless /export).")
    parser.add_argument("--session", default=None, metavar="ID",
                         help="Session id or unique prefix (default: the latest session for this directory)")
    parser.add_argument("--sanitize", action="store_true",
                         help="Redact common credential shapes (API keys, tokens, secret env var values) before writing")
    parser.add_argument("-o", "--output", default=None, metavar="FILE", help="Write to FILE instead of stdout")
    parser.add_argument("--cwd", default=None, metavar="DIR",
                         help="Project directory whose sessions to look in (default: the current directory)")
    args = parser.parse_args(argv)

    from halo_harness.agent import sessions as agent_sessions
    from halo_harness.agent.log import SessionLog

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    if args.session:
        session_id, err = agent_sessions.resolve_resume(cwd, args.session)
        if session_id is None:
            print(f"halo export: {err}", file=sys.stderr)
            return 2
        log = SessionLog(cwd, session_id=session_id)
    else:
        log = SessionLog.latest_for_cwd(cwd)
        if log is None:
            print("halo export: no sessions found for this directory", file=sys.stderr)
            return 2

    nodes = log.read_all()
    if not nodes:
        print(f"halo export: session {log.session_id} has no recorded nodes", file=sys.stderr)
        return 2

    if args.sanitize:
        nodes = [sanitize_node(n) for n in nodes]

    output_text = "\n".join(json.dumps(n, ensure_ascii=False) for n in nodes) + "\n"

    if args.output:
        try:
            Path(args.output).write_text(output_text, encoding="utf-8")
        except OSError as e:
            print(f"halo export: could not write {args.output}: {e}", file=sys.stderr)
            return 1
        print(f"halo export: wrote {len(nodes)} node(s) from session {log.session_id} to {args.output}",
              file=sys.stderr)
    else:
        sys.stdout.write(output_text)
    return 0
