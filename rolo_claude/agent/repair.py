"""rolo_claude.agent.repair -- the repair layer (H2 scope B). Sits between
a model's raw reply and real tool dispatch: normalizes an unknown tool name,
validates/coerces arguments against the tool's own schema, flags a
duplicate call within one message, and (via `providers.hooks.leak_parser`/
`args_repair`, reused rather than duplicated here) promotes a text-embedded
call to a real tool_use when no native one exists. Never raises and never
silently drops a call -- every outcome is either a usable (possibly
repaired) block or a clear, quoted error a model can act on.

Call order in agent/loop.py per the brief's scope D ("schema validate ->
repair -> permission decide -> ... -> run -> truncate -> result"):
  1. agent/invariants.validate_tool_use -- basic shape (non-empty id, dict
     input) -- unchanged, still the FIRST gate.
  2. repair_assistant_turn (THIS module) -- name resolution, schema
     validate+coerce, duplicate detection, text-embedded-call promotion.
  3. rolo_claude.permissions.decide.
  4. dispatch / truncate.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Optional

from rolo_claude.providers.hooks import args_repair, leak_parser

# ---------------------------------------------------------------------------
# 1. Unknown tool name normalization + difflib rename.
# ---------------------------------------------------------------------------

# A handful of common non-Claude-Code names a model might reach for out of
# habit (its own training data's tool-calling conventions) -- tried BEFORE
# difflib, so an exact conceptual match never depends on string distance.
_NAME_ALIASES = {
    "read_file": "Read", "readfile": "Read", "file_read": "Read", "cat": "Read",
    "write_file": "Write", "writefile": "Write", "file_write": "Write", "create_file": "Write",
    "edit_file": "Edit", "editfile": "Edit", "str_replace": "Edit", "str_replace_editor": "Edit",
    "run_command": "Bash", "run_shell_command": "Bash", "shell": "Bash", "execute_command": "Bash",
    "bash_tool": "Bash", "terminal": "Bash",
    "powershell_tool": "PowerShell",
    "search_files": "Grep", "grep_search": "Grep", "code_search": "Grep", "text_search": "Grep",
    "find_files": "Glob", "glob_search": "Glob", "file_search": "Glob", "list_files": "Glob",
    "web_fetch": "WebFetch", "fetch_url": "WebFetch", "fetch": "WebFetch", "http_get": "WebFetch",
    "todo_write": "TodoWrite", "update_todos": "TodoWrite", "manage_todos": "TodoWrite",
    "ask_user": "AskUserQuestion", "ask_user_question": "AskUserQuestion", "clarify": "AskUserQuestion",
    "tool_search": "ToolSearch", "search_tools": "ToolSearch",
    "invoke_skill": "Skill", "run_skill": "Skill",
}

_SEP_RE = re.compile(r"[-_\s]+")


def normalize_tool_name(name: str) -> str:
    """Alias-table lookup (separator/case-insensitive) for a common
    non-Claude-Code spelling; returns `name` unchanged if nothing matches."""
    if not name:
        return name
    key = _SEP_RE.sub("", name.strip().lower())
    for alias, canonical in _NAME_ALIASES.items():
        if _SEP_RE.sub("", alias) == key:
            return canonical
    return name


def resolve_tool_name(name: str, known_names: list) -> "tuple[Optional[str], list]":
    """Resolve `name` against `known_names` (the frozen catalog): exact
    match, then the alias table, then a case-insensitive exact match, then
    `difflib` at cutoff >= 0.85 (auto-renamed -- the single best match is
    used). Returns (resolved_name_or_None, closest_5_names) -- `closest_5`
    is populated (even on a clean resolution's empty list) only when a
    caller needs to quote alternatives in an error message."""
    if not name:
        return None, list(known_names)[:5]
    if name in known_names:
        return name, []
    aliased = normalize_tool_name(name)
    if aliased in known_names:
        return aliased, []
    lower_map = {n.lower(): n for n in known_names}
    if name.lower() in lower_map:
        return lower_map[name.lower()], []
    close = difflib.get_close_matches(name, known_names, n=5, cutoff=0.85)
    if close:
        return close[0], close
    return None, difflib.get_close_matches(name, known_names, n=5, cutoff=0.0)


# ---------------------------------------------------------------------------
# 2. Schema validator: required / types / enum / coercions.
# ---------------------------------------------------------------------------

_JSON_TYPES = {
    "string": str, "integer": int, "number": (int, float), "boolean": bool,
    "array": list, "object": dict,
}


def validate_and_coerce(input: dict, schema: dict) -> "tuple[dict, list]":
    """(coerced_input, errors). Never raises; an empty `errors` list means
    `input` (possibly with a few values coerced -- a stringified number, a
    numeric string for an integer field, "true"/"false" for a boolean) is
    schema-valid. Only `properties`/`required`/`enum` are consulted --
    deliberately not a full JSON-Schema implementation."""
    if not isinstance(schema, dict):
        return (input if isinstance(input, dict) else {}), []
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    errors: list = []
    out = dict(input) if isinstance(input, dict) else {}

    for key in required:
        if key not in out:
            errors.append(f"missing required parameter {key!r}")

    for key, value in list(out.items()):
        prop_schema = props.get(key)
        if not isinstance(prop_schema, dict):
            continue
        expected = prop_schema.get("type")
        py_types = _JSON_TYPES.get(expected)
        if py_types is None:
            continue
        if isinstance(value, bool) and py_types is not bool:
            errors.append(f"parameter {key!r} must be {expected}, got a boolean")
            continue
        if isinstance(value, py_types):
            pass
        elif expected == "string" and isinstance(value, (int, float)):
            out[key] = str(value)
        elif expected in ("number", "integer") and isinstance(value, str):
            try:
                out[key] = int(value) if expected == "integer" else float(value)
            except ValueError:
                errors.append(f"parameter {key!r} must be {expected}, got {value!r}")
        elif expected == "boolean" and isinstance(value, str) and value.strip().lower() in ("true", "false"):
            out[key] = value.strip().lower() == "true"
        else:
            errors.append(f"parameter {key!r} must be {expected}, got {type(value).__name__}")
        enum = prop_schema.get("enum")
        if isinstance(enum, list) and key in out and out[key] not in enum:
            errors.append(f"parameter {key!r} must be one of {enum}, got {out[key]!r}")

    return out, errors


# ---------------------------------------------------------------------------
# 3. Duplicate-call detection within one assistant message.
# ---------------------------------------------------------------------------

def _canonical_args(args) -> str:
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(args)


def find_duplicate_calls(tool_use_blocks: list) -> dict:
    """`{block_id: first_occurrence_id}` for every block AFTER the first
    with the same (name, canonically-equal arguments) in THIS ONE message
    (property order ignored, matching the loop breaker's own rule 8
    definition of "identical")."""
    seen: dict = {}
    dupes: dict = {}
    for b in tool_use_blocks:
        if not isinstance(b, dict):
            continue
        key = (b.get("name"), _canonical_args(b.get("input") or {}))
        block_id = b.get("id")
        if key in seen:
            dupes[block_id] = seen[key]
        else:
            seen[key] = block_id
    return dupes


# ---------------------------------------------------------------------------
# 4. Text-embedded call promotion -- thin wrapper around providers.hooks.
# ---------------------------------------------------------------------------

def extract_leaked_call(text: str, profile) -> Optional[dict]:
    """Thin passthrough to `providers.hooks.leak_parser` (never duplicated
    here) -- kept as its own named function so agent/loop.py's call site
    reads as repair-layer vocabulary ("was a call promoted from text?")
    rather than reaching into providers.hooks directly."""
    return leak_parser(text, profile)


# ---------------------------------------------------------------------------
# 5. Putting it together: one tool_use block.
# ---------------------------------------------------------------------------

@dataclass
class RepairOutcome:
    block: dict                     # the (possibly renamed/coerced) tool_use block
    ok: bool                        # False -> `error_text` names why; never dispatch
    error_text: Optional[str] = None
    repaired: bool = False          # name was renamed and/or args were coerced
    duplicate_of: Optional[str] = None


def repair_tool_use_block(block: dict, registry, known_names: Optional[list] = None) -> RepairOutcome:
    """Resolve `block["name"]` against the registry and validate/coerce its
    `input` against that tool's schema. `known_names` lets a caller pass a
    catalog snapshot explicitly (falls back to `registry.names()`)."""
    names = known_names if known_names is not None else registry.names()
    name = block.get("name")
    resolved, close = resolve_tool_name(name, names)
    if resolved is None:
        suggestion = f" Did you mean one of: {', '.join(close)}?" if close else ""
        return RepairOutcome(
            block=block, ok=False,
            error_text=f"Unknown tool {name!r}.{suggestion} Available tools: {', '.join(names)}",
        )

    repaired_name = resolved != name
    tool = registry.get(resolved)
    schema = tool.input_schema if tool is not None else None
    coerced, errors = validate_and_coerce(block.get("input") or {}, schema or {})
    if errors:
        return RepairOutcome(
            block={**block, "name": resolved}, ok=False,
            error_text=f"Invalid arguments for {resolved}: " + "; ".join(errors),
            repaired=repaired_name,
        )

    repaired_args = coerced != (block.get("input") or {})
    new_block = {**block, "name": resolved, "input": coerced}
    return RepairOutcome(block=new_block, ok=True, repaired=(repaired_name or repaired_args))


def repair_assistant_turn(tool_use_blocks: list, registry) -> "list[RepairOutcome]":
    """Run `repair_tool_use_block` over every block in one assistant
    message, PLUS duplicate detection across the whole set (a duplicate is
    reported without ever touching the registry/schema for that block --
    it never needed to be valid to be recognized as a repeat)."""
    dupes = find_duplicate_calls(tool_use_blocks)
    outcomes = []
    names = registry.names()
    for b in tool_use_blocks:
        block_id = b.get("id") if isinstance(b, dict) else None
        if block_id in dupes:
            outcomes.append(RepairOutcome(block=b, ok=False, duplicate_of=dupes[block_id],
                                           error_text=f"(duplicate of {dupes[block_id]})"))
            continue
        outcomes.append(repair_tool_use_block(b, registry, known_names=names))
    return outcomes
