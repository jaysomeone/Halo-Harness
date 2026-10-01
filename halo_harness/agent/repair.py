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
from dataclasses import dataclass
from typing import Optional

from rolo_claude.providers.hooks import leak_parser

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

# u2-h3b finding 13: the guard against fuzzy-renaming an MCP tool name (see
# `resolve_tool_name` below) originally checked only a RAW `mcp__` prefix,
# so a single mangled underscore/hyphen or wrong case -- `mcp_hardware__
# hw_clock_stop`, `MCP__hardware__hw_clock_stop`, `mcp-hardware__hw_clock_
# stop` -- fell all the way through to the generic alias-table/difflib path
# below and got auto-renamed into a DIFFERENT, unrelated MCP tool (the exact
# bug finding 2 was meant to close, just reached through a spelling variant
# instead of the exact "mcp__" prefix). `_MCP_PREFIX_RE` recognises "mcp"
# (any case) followed by one or more `_`/`-` separators as an INTENDED mcp
# reference; `canonicalize_mcp_name` normalises just that prefix to the
# canonical "mcp__", leaving the server/tool halves untouched (they stay
# case-sensitive/exact -- only the well-known "mcp__" marker itself is
# spelling-tolerant).
_MCP_PREFIX_RE = re.compile(r"^mcp[_-]+", re.IGNORECASE)


def canonicalize_mcp_name(name: str) -> Optional[str]:
    """`None` for a name that doesn't look like an mcp__ reference at all
    (never touches an ordinary built-in tool name like "Read"). For one
    that does, returns the canonical "mcp__<rest>" spelling -- unchanged
    when `name` already has the exact "mcp__" prefix, prefix-normalised
    otherwise."""
    if not name:
        return None
    if name.startswith("mcp__"):
        return name
    m = _MCP_PREFIX_RE.match(name)
    if not m:
        return None
    return "mcp__" + name[m.end():]


def looks_like_mcp_name(name: str) -> bool:
    return canonicalize_mcp_name(name) is not None


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


def resolve_tool_name(name: str, known_names: list, *, catalog=None, registry=None) -> "tuple[Optional[str], list]":
    """Resolve `name` against `known_names` (the frozen catalog): exact
    match, then (for anything OTHER than an `mcp__` name) the alias
    table, a case-insensitive exact match, then `difflib` at cutoff
    >= 0.85 (auto-renamed -- the single best match is used).

    finding 2: an `mcp__`-prefixed name is NEVER fuzzy-renamed -- MCP
    sibling tools are often one edit apart (`mcp__hardware__hw_clock_stop`
    vs `hw_clock_start`, `mcp__kb__kb_plugin_params` vs `kb_plugin_card`),
    so difflib silently dispatches a DIFFERENT tool with the model's
    original arguments. When an `mcp__` name isn't already resolvable, two
    more sources are tried before giving up: `registry` (a name another
    tool_use block already auto-loaded EARLIER in this SAME turn --
    `known_names` is a snapshot taken once at the top of
    `repair_assistant_turn`, so it can't see that), then `catalog.
    deferred` (the session's SessionCatalog, when one exists) -- found
    there, it's LOADED right here, so a call to a not-yet-loaded MCP tool
    (or one a resumed `--session-id` replay logged before it was ever
    loaded into THIS process) just works instead of failing the turn.

    Returns (resolved_name_or_None, closest_5_names) -- `closest_5` is
    always empty for an `mcp__` name (never a fuzzy "did you mean"
    suggestion pointing at an unrelated MCP tool)."""
    if not name:
        return None, list(known_names)[:5]
    if name in known_names:
        return name, []
    canonical = canonicalize_mcp_name(name)
    if canonical is not None:
        # finding 13: normalise mcp_/MCP__/mcp- to mcp__ FIRST, then apply
        # the exact-same finding-2 rule to the canonical spelling -- an
        # exact/registry/deferred match only, never difflib, regardless of
        # which of the four prefix spellings the model actually sent.
        if canonical in known_names:
            return canonical, []
        if registry is not None and registry.get(canonical) is not None:
            return canonical, []
        if catalog is not None and canonical in catalog.deferred:
            loaded = catalog.load([canonical])
            if canonical in loaded:
                return canonical, []
        return None, []
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
    numeric string for an integer field, "true"/"false" for a boolean, an
    integral float for an integer field, or a JSON-encoded STRING for an
    array/object field -- finding 5's two missing coercions, e.g. a Qwen/
    GLM habit of sending TodoWrite's `todos` as `'[{"content":...}]'`
    rather than a real array) is schema-valid. Only
    `properties`/`required`/`enum` are consulted -- deliberately not a
    full JSON-Schema implementation."""
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
        elif expected == "integer" and isinstance(value, float) and value.is_integer():
            out[key] = int(value)
        elif expected in ("number", "integer") and isinstance(value, str):
            try:
                out[key] = int(value) if expected == "integer" else float(value)
            except ValueError:
                errors.append(f"parameter {key!r} must be {expected}, got {value!r}")
        elif expected == "boolean" and isinstance(value, str) and value.strip().lower() in ("true", "false"):
            out[key] = value.strip().lower() == "true"
        elif expected in ("array", "object") and isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, py_types):
                out[key] = parsed
            else:
                errors.append(f"parameter {key!r} must be {expected}, got {value!r}")
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
    # H4 must-do: "hook matchers must see the model's original tool name,
    # not a repair rename" -- the model's OWN spelling, set only when the
    # name was actually changed (alias-table/difflib; never set for an
    # mcp__ auto-load, which never renames anything). None otherwise (incl.
    # every existing caller/test that never looks at this field).
    original_name: Optional[str] = None
    # H10 Part A: a structured discriminant for agent/loop.py's own
    # tool_result `error_class` telemetry -- "unknown_tool" (name never
    # resolved) or "invalid_args" (schema validate_and_coerce rejected the
    # input); None for every ok=True outcome and for a duplicate (which
    # never reaches either branch below). Kept as an explicit field rather
    # than having the caller pattern-match `error_text`'s own wording, so a
    # future rewording of either message can never silently break the
    # classification.
    error_kind: Optional[str] = None


def repair_tool_use_block(block: dict, registry, known_names: Optional[list] = None, *, catalog=None) -> RepairOutcome:
    """Resolve `block["name"]` against the registry and validate/coerce its
    `input` against that tool's schema. `known_names` lets a caller pass a
    catalog snapshot explicitly (falls back to `registry.names()`).
    `catalog` (agent/catalog.py's SessionCatalog, when one exists) lets an
    unresolved `mcp__` name be auto-loaded from the deferred pool -- see
    `resolve_tool_name`."""
    names = known_names if known_names is not None else registry.names()
    name = block.get("name")
    resolved, close = resolve_tool_name(name, names, catalog=catalog, registry=registry)
    if resolved is None:
        if name and looks_like_mcp_name(name):
            # finding 13: hint at the CANONICAL name (never the model's
            # possibly-mangled prefix) so the "select:" query it's told to
            # retry with is one ToolSearch can actually match.
            hint_name = canonicalize_mcp_name(name) or name
            error_text = (f"Unknown tool {name!r}. If this is a real MCP tool that just isn't "
                          f"loaded in this conversation yet, call ToolSearch with query "
                          f"\"select:{hint_name}\" to load it, then call it again -- never assume it's "
                          f"one of the tools below. Available tools: {', '.join(names)}")
        else:
            suggestion = f" Did you mean one of: {', '.join(close)}?" if close else ""
            error_text = f"Unknown tool {name!r}.{suggestion} Available tools: {', '.join(names)}"
        return RepairOutcome(block=block, ok=False, error_text=error_text, error_kind="unknown_tool")

    repaired_name = resolved != name
    tool = registry.get(resolved)
    schema = tool.input_schema if tool is not None else None
    coerced, errors = validate_and_coerce(block.get("input") or {}, schema or {})
    original_name = name if repaired_name else None
    if errors:
        return RepairOutcome(
            block={**block, "name": resolved}, ok=False,
            error_text=f"Invalid arguments for {resolved}: " + "; ".join(errors),
            repaired=repaired_name, original_name=original_name, error_kind="invalid_args",
        )

    repaired_args = coerced != (block.get("input") or {})
    new_block = {**block, "name": resolved, "input": coerced}
    return RepairOutcome(block=new_block, ok=True, repaired=(repaired_name or repaired_args),
                          original_name=original_name)


# ---------------------------------------------------------------------------
# 6. H10 Part A: repair_kind telemetry for agent/log.py's own assistant-node
#    `tool_meta` -- combines this module's own RepairOutcome with the
#    stream-level `lenient_json_repaired` flag (providers/oai_stream.py) and
#    the leak-parser promotion marker (agent/loop.py's own `_turn_body`,
#    `_promoted_from_leak` on the block) into ONE taxonomy value per call.
# ---------------------------------------------------------------------------

def repair_kind_for(tu: dict, outcome: "RepairOutcome", tool_call_flags: dict) -> str:
    """`"leak_parser"|"lenient_json"|"rename"|"args_repair"|"none"` -- checked
    in that priority order (a leaked call promoted from prose is always
    "leak_parser" even if its args also needed lenient JSON repair; a
    renamed call is "rename" even if its args were ALSO coerced, since the
    name mismatch is the more informative signal). A length-truncated or
    unrecoverably-malformed call (never repairable JSON at all) is "none"
    here -- that failure is `tool_result.error_class`'s job, not this
    taxonomy's, which only ever names a SUCCESSFUL repair."""
    if isinstance(tu, dict) and tu.get("_promoted_from_leak"):
        return "leak_parser"
    tool_id = tu.get("id") if isinstance(tu, dict) else None
    flags = tool_call_flags.get(tool_id) or {} if tool_id else {}
    if flags.get("lenient_json_repaired"):
        return "lenient_json"
    if outcome.original_name is not None:
        return "rename"
    if outcome.repaired:
        return "args_repair"
    return "none"


def build_tool_meta(tool_use_blocks: list, repair_outcomes: list, tool_call_flags: dict) -> dict:
    """`{tool_use_id: {"repaired": bool, "repair_kind": str,
    "promoted_from_leak": bool}}` for every block in `tool_use_blocks` --
    `agent/loop.py`'s own `_turn_body` computes this ONCE (reusing the same
    `repair_outcomes` list `_dispatch_tools` goes on to dispatch from,
    never recomputed) and passes it to `SessionLog.append_assistant`."""
    tool_call_flags = tool_call_flags or {}
    meta: dict = {}
    for tu, outcome in zip(tool_use_blocks, repair_outcomes):
        tool_id = tu.get("id") if isinstance(tu, dict) else None
        if not tool_id:
            continue
        promoted = bool(isinstance(tu, dict) and tu.get("_promoted_from_leak"))
        repaired = bool(outcome.repaired) or promoted
        meta[tool_id] = {
            "repaired": repaired,
            "repair_kind": repair_kind_for(tu, outcome, tool_call_flags),
            "promoted_from_leak": promoted,
        }
    return meta


def repair_assistant_turn(tool_use_blocks: list, registry, *, catalog=None) -> "list[RepairOutcome]":
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
        outcomes.append(repair_tool_use_block(b, registry, known_names=names, catalog=catalog))
    return outcomes
