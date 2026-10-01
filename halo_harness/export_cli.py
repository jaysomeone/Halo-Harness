"""rolo_claude.export_cli -- `rolo-claude export` subcommand (H8 scope D):
a headless version of the TUI's own `/export` (U5), for a script or a CI
job with no interactive file picker to drive it through. Reads a session's
JSONL log (the latest for the given/current directory, or an explicit
`--session <id-or-prefix>`), optionally sanitizing common credential shapes
out of it, and writes the raw JSONL to a file or stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Best-effort redaction only (a convenience for safely sharing a transcript,
# never a security boundary): well-known secret-shaped tokens, plus
# NAME=value for a short list of well-known secret env var names as they'd
# appear in a Bash `env`/`printenv` tool_result the model actually ran, OR
# `"NAME": "value"` as they'd appear in a Read of settings.json's own `env`
# block.
#
# H9 whole-tree review finding 4: this is now the ONE sanitizer both export
# paths (`rolo-claude export --sanitize` below AND the TUI's own `/export
# --sanitize`, controller.py's `sanitize_transcript`) and the deleted
# `session_cli.py`'s functionality all resolve to -- see this module's own
# docstring reference in `controller.py`. Fixed here (all verified missing
# before): OpenRouter's own `sk-or-v1-...` shape (the old bare `sk-
# [A-Za-z0-9]{16,}` pattern stops at the second `-`, 2 alnum chars short of
# its 16-char minimum); `sk-proj-...`; a QUOTED assignment, either literally
# quoted (`export KEY="value"`, read straight off disk/a live transcript) or
# JSON-escaped-quoted (`export KEY=\"value\"`, the shape that same literal
# text takes once `sanitize_node` below has round-tripped the whole node
# through `json.dumps` -- the old `[^\s"\\]+` value class excludes `\` and
# `"` outright, so it can't even START matching a value that begins with
# either); a JSON key form (`"OPENROUTER_API_KEY": "value"`, no `=` at all);
# and name-based redaction for ANY `*_API_KEY`/`*TOKEN*`/`*SECRET*`-shaped
# name, not just this fixed list (which is kept for names that don't fit
# that shape, e.g. AWS's own `_ACCESS_KEY_ID`/`_HOST`).
_SECRET_ENV_NAMES = (
    "OPENROUTER_API_KEY", "DATABRICKS_TOKEN", "DATABRICKS_HOST", "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID",
    "AWS_SESSION_TOKEN", "NPM_TOKEN", "OPENROUTER_MANAGEMENT_KEY", "TYPESAFE_API_KEY",
)
# Any identifier containing API_KEY/TOKEN/SECRET anywhere in its name (a
# generic catch-all for names this list doesn't happen to enumerate, e.g. a
# plugin's own `SLACK_BOT_TOKEN` or `MY_SERVICE_SECRET`) -- deliberately a
# substring match (a wildcard-shaped name check, not a strict identifier
# segmentation), since over-redacting a rare false positive is the safe
# failure mode for a "best-effort, share this transcript safely" tool.
_GENERIC_SECRET_NAME = r"[A-Z0-9_]*(?:API_KEY|APIKEY|TOKEN|SECRET)[A-Z0-9_]*"
_NAME_ALT = "|".join(re.escape(n) for n in _SECRET_ENV_NAMES) + "|" + _GENERIC_SECRET_NAME
# A literal `"` OR a JSON-escaped `\"` (one optional backslash then a
# quote) -- matches a value's opening/closing quote whether this text is
# raw (a live transcript, a Read of a real file) or already JSON-string-
# encoded (`sanitize_node`'s own `json.dumps` round trip below).
_Q = r'\\?"'
# Named groups so the replacement function can put back EXACTLY the quote
# characters (if any) it actually matched around the name and the value --
# `lead_q`/`mid_q` are the JSON-key form's own opening/closing quotes
# (`"OPENROUTER_API_KEY":`), `open_q`/`close_q` are the value's. Dropping a
# quote that was structural JSON/shell syntax (rather than part of the
# secret itself) would corrupt that syntax; preserving each one exactly as
# matched -- while replacing only what was BETWEEN them -- can't.
_ENV_ASSIGN_RE = re.compile(
    r'(?P<lead_q>")?\b(?P<name>' + _NAME_ALT + r')(?P<mid_q>")?\s*(?P<sep>[:=])\s*'
    r'(?P<open_q>' + _Q + r')?(?P<value>[^\s"\\]+)(?P<close_q>' + _Q + r')?',
    re.IGNORECASE,  # `"api_key": "..."`/`token=...` must redact regardless
                     # of case -- the shape (name immediately followed by
                     # `:`/`=` then a value) is what makes this safe from
                     # false positives on ordinary lowercase prose ("this
                     # token was issued yesterday" has no `:`/`=` right
                     # after "token", so it never matches at all).
)


def _redact_env_assign(m: "re.Match") -> str:
    return (f"{m.group('lead_q') or ''}{m.group('name')}{m.group('mid_q') or ''}{m.group('sep')}"
            f"{m.group('open_q') or ''}<redacted>{m.group('close_q') or ''}")
_TOKEN_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{16,}"),
    re.compile(r"sk-or-v1-[A-Za-z0-9]{16,}"),
    re.compile(r"sk-proj-[A-Za-z0-9\-_]{16,}"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"dapi[a-f0-9]{20,}"),  # Databricks personal access token shape
    re.compile(r"AKIA[A-Z0-9]{12,}"),  # AWS access key id shape
)
_BEARER_RE = re.compile(r"(Bearer\s+)([A-Za-z0-9\-_.]{16,})", re.IGNORECASE)


def sanitize_text(text: str) -> str:
    """Redact every recognized secret shape in `text` -- shell `export
    NAME=value` (quoted or not), a JSON `"NAME": "value"` field (settings.
    json's own `env` block shape), a Bearer header, and bare secret-shaped
    tokens (sk-or-v1-/sk-ant-/sk-proj-/ghp_/dapi/AKIA...). Order matters:
    the env-assignment and Bearer-header forms are more specific than the
    bare token patterns, so they run first -- and among the bare token
    patterns, hyphenated prefixes (`sk-ant-`/`sk-or-v1-`/`sk-proj-`) run
    before the generic `sk-...` pattern, which would otherwise partially
    consume just the `sk-` + first short alnum run and leave the rest of a
    hyphenated key (which `sk-[A-Za-z0-9]{16,}` alone can never match, since
    `-` isn't in that class) sitting right there, unredacted, next to a
    stray already-redacted fragment."""
    text = _ENV_ASSIGN_RE.sub(_redact_env_assign, text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)}<redacted>", text)
    for pat in _TOKEN_PATTERNS:
        text = pat.sub("<redacted>", text)
    return text


def sanitize_node(node: dict) -> dict:
    """Sanitize one JSONL log node by round-tripping it through JSON text --
    the simplest way to catch a secret wherever it's nested (a tool_result's
    content list, an assistant text block, an environment snapshot, ...)
    without hand-enumerating every possible node shape. If the sanitized
    text somehow isn't valid JSON any more (a redaction pattern landing
    somewhere it corrupted the structure), this returns a placeholder
    naming the node's own `type`/`role` rather than the ORIGINAL node --
    silently falling back to the unsanitized text would defeat the entire
    point of asking for `--sanitize` in the first place."""
    try:
        blob = json.dumps(node, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"type": node.get("type", "?"), "_sanitize_error": "could not serialize this node"}
    cleaned = sanitize_text(blob)
    try:
        return json.loads(cleaned)
    except ValueError:
        return {"type": node.get("type", "?"), "_sanitize_error": "redaction produced invalid JSON; node omitted"}


def cmd_export(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="rolo-claude export", add_help=True,
                                      description="Export a session's transcript as JSONL (headless /export).")
    parser.add_argument("--session", default=None, metavar="ID",
                         help="Session id or unique prefix (default: the latest session for this directory)")
    parser.add_argument("--sanitize", action="store_true",
                         help="Redact common credential shapes (API keys, tokens, secret env var values) before writing")
    parser.add_argument("-o", "--output", default=None, metavar="FILE", help="Write to FILE instead of stdout")
    parser.add_argument("--cwd", default=None, metavar="DIR",
                         help="Project directory whose sessions to look in (default: the current directory)")
    args = parser.parse_args(argv)

    from rolo_claude.agent import sessions as agent_sessions
    from rolo_claude.agent.log import SessionLog

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    if args.session:
        session_id, err = agent_sessions.resolve_resume(cwd, args.session)
        if session_id is None:
            print(f"rolo-claude export: {err}", file=sys.stderr)
            return 2
        log = SessionLog(cwd, session_id=session_id)
    else:
        log = SessionLog.latest_for_cwd(cwd)
        if log is None:
            print("rolo-claude export: no sessions found for this directory", file=sys.stderr)
            return 2

    nodes = log.read_all()
    if not nodes:
        print(f"rolo-claude export: session {log.session_id} has no recorded nodes", file=sys.stderr)
        return 2

    if args.sanitize:
        nodes = [sanitize_node(n) for n in nodes]

    output_text = "\n".join(json.dumps(n, ensure_ascii=False) for n in nodes) + "\n"

    if args.output:
        try:
            Path(args.output).write_text(output_text, encoding="utf-8")
        except OSError as e:
            print(f"rolo-claude export: could not write {args.output}: {e}", file=sys.stderr)
            return 1
        print(f"rolo-claude export: wrote {len(nodes)} node(s) from session {log.session_id} to {args.output}",
              file=sys.stderr)
    else:
        sys.stdout.write(output_text)
    return 0
