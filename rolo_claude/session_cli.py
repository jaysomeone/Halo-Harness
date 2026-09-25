"""rolo_claude.session_cli -- `rolo-claude stats` / `rolo-claude export`
(H8 scope D): headless, offline versions of the U5 `/stats`/`/export` slash
commands. Unlike the in-session slash commands (which need a LIVE
`agent.loop.Session` to read `session.log.nodes()` from), these read a
session's JSONL log straight off disk -- no model connection, no running
turn -- so they work as a standalone `rolo-claude <subcommand>` the same way
`models`/`mcp`/`config`/`doctor` already do. Both default to the most
recently touched session for `--cwd` (or the current directory) when
`--session` is omitted, and accept the same id/name/transcript-path forms
`-r`/`--resume` does.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional


def _resolve_session_nodes(cwd: Path, session_ref: Optional[str]) -> "tuple[Optional[str], Optional[list]]":
    """`(session_id, nodes)`, or `(None, None)` with an error already
    printed to stderr."""
    from rolo_claude.agent.log import SessionLog
    from rolo_claude.agent import sessions as agent_sessions

    if session_ref:
        resolved, err = agent_sessions.resolve_resume(cwd, session_ref)
        if resolved is None:
            print(f"rolo-claude: --session: {err or f'no session matching {session_ref!r}'}", file=sys.stderr)
            return None, None
        log = SessionLog(cwd, session_id=resolved)
        return resolved, log.read_all()

    log = SessionLog.latest_for_cwd(cwd)
    if log is None:
        print(f"rolo-claude: no sessions found for {cwd}", file=sys.stderr)
        return None, None
    return log.session_id, log.nodes()


def cmd_stats(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="rolo-claude stats", add_help=True,
        description="Show tokens/cost per model and tool-call counts for a session (offline, no live model call).",
    )
    parser.add_argument("--session", default=None, metavar="ID_OR_NAME",
                         help="Session id, name, or transcript path (default: the most recent session for --cwd)")
    parser.add_argument("--cwd", default=None, metavar="DIR", help="Project directory (default: the current one)")
    args = parser.parse_args(argv)
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()

    session_id, nodes = _resolve_session_nodes(cwd, args.session)
    if nodes is None:
        return 1

    from rolo_claude.controller import compute_session_stats
    stats = compute_session_stats(nodes)
    print(f"Session {session_id}")
    print(f"Turns: {stats['turns']}")
    print(f"Total cost: ${stats['total_cost_usd']:.4f}")
    for model, bucket in sorted(stats["per_model"].items()):
        print(f"  {model}: {bucket['calls']} call(s), "
              f"{bucket['input_tokens']}in/{bucket['output_tokens']}out tok, ${bucket['cost_usd']:.4f}")
    for name, n in sorted(stats["tool_counts"].items()):
        print(f"  tool {name}: {n} call(s)")
    return 0


# ---- export ---------------------------------------------------------------

# Field NAMES that are always secret-shaped regardless of their value's own
# shape (an exact, case-insensitive match on the JSON key).
_SECRET_KEYS = re.compile(
    r"^(api[_-]?key|token|authorization|bearer|secret|password|passwd|"
    r"access[_-]?token|refresh[_-]?token|x-api-key|dbx[_-]?token|"
    r"anthropic[_-]?api[_-]?key|openrouter[_-]?api[_-]?key|databricks[_-]?token)$",
    re.IGNORECASE,
)
# Credential SHAPES that can appear inside ordinary text (a Bash result
# echoing an env var, a hook's stdout, a pasted curl command) even outside a
# field the key-name check above would catch.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{10,}"),
    re.compile(r"sk-or-v1-[A-Za-z0-9]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9\-_.=]{10,}", re.IGNORECASE),
    re.compile(r"\bdapi[a-f0-9]{32,}\b", re.IGNORECASE),  # a Databricks personal access token's own shape
]
REDACTED = "[REDACTED]"


def _sanitize_text(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _sanitize_json(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out[k] = REDACTED if isinstance(k, str) and _SECRET_KEYS.match(k.strip()) else _sanitize_json(v)
        return out
    if isinstance(obj, list):
        return [_sanitize_json(v) for v in obj]
    if isinstance(obj, str):
        return _sanitize_text(obj)
    return obj


def cmd_export(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="rolo-claude export", add_help=True,
        description="Export a session's transcript as JSONL (offline, reads the on-disk session log directly).",
    )
    parser.add_argument("--session", default=None, metavar="ID_OR_NAME",
                         help="Session id, name, or transcript path (default: the most recent session for --cwd)")
    parser.add_argument("--cwd", default=None, metavar="DIR", help="Project directory (default: the current one)")
    parser.add_argument("--sanitize", action="store_true",
                         help="Redact API keys/tokens/bearer credentials from the exported transcript")
    parser.add_argument("-o", "--output", default=None, metavar="FILE", help="Write to FILE instead of stdout")
    args = parser.parse_args(argv)
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()

    session_id, nodes = _resolve_session_nodes(cwd, args.session)
    if nodes is None:
        return 1

    if args.sanitize:
        nodes = [_sanitize_json(n) for n in nodes]

    lines = [json.dumps(n, ensure_ascii=False) for n in nodes]
    text = "\n".join(lines) + ("\n" if lines else "")
    if args.output:
        try:
            Path(args.output).write_text(text, encoding="utf-8")
        except OSError as e:
            print(f"rolo-claude: could not write {args.output}: {e}", file=sys.stderr)
            return 1
        print(f"Exported session {session_id} ({len(nodes)} node(s)) to {args.output}"
              + (" (sanitized)" if args.sanitize else ""))
    else:
        sys.stdout.write(text)
    return 0
