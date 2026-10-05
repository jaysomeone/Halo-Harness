"""halo_harness.tui.completion -- `/` (slash command) and `@` (path) prefix
completion for PromptInput's popup. Pure functions, no textual import, so
they're unit-testable directly. `@path` completion is an `os.scandir` walk
(never `Path.rglob`, which has no early-exit/prune hook) capped at 20k
directory entries visited (D-TUI: "os.scandir walk with prunes, 20k cap").
"""

from __future__ import annotations

import os
import re

SCAN_CAP = 20_000
MAX_RESULTS = 50

# U5 scope A: `@file#L10-20` / `@file#L10` mentions -- the `#L...` suffix is
# OPTIONAL and, when present, is excluded from the path itself (it can't be
# part of a real filename, so no path is ever mis-split by it).
_AT_MENTION_LINE_RE = re.compile(r"(?<![\w`@])@((?:~/|\.{1,2}/|//?)?[^\s`'\"()<>#]+)(?:#[Ll](\d+)(?:-(\d+))?)?")


def parse_at_mentions(text: str) -> "list[tuple[str, object, object]]":
    """`[(raw_path, start_line_or_None, end_line_or_None), ...]` for every
    `@path` or `@path#L10-20`/`@path#L10` mention in `text` -- pure regex,
    no filesystem access (`Controller.ingest_at_mentions` resolves/reads
    each one, via the Read tool's own path resolution, matching this
    module's existing `complete_at_path`'s "no textual import" rule so
    it's directly unit-testable)."""
    out: "list[tuple[str, object, object]]" = []
    for m in _AT_MENTION_LINE_RE.finditer(text or ""):
        raw = m.group(1).rstrip(".,;:!?)")
        if not raw:
            continue
        start = int(m.group(2)) if m.group(2) else None
        end = int(m.group(3)) if (m.group(3) and start is not None) else start
        out.append((raw, start, end))
    return out

_PRUNE_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".idea",
    ".vscode", ".tox", "egg-info", ".egg-info",
})


def _is_pruned(name: str) -> bool:
    return name in _PRUNE_DIRS or name.endswith(".egg-info")


def complete_slash(prefix: str, registry) -> list:
    """`[(invocation, description), ...]` for every command whose name (or
    alias) starts with `prefix` (already stripped of its leading '/' by the
    caller, or not -- `Registry.complete` strips it itself). `registry` may
    be None (no commands discovered yet, or a minimal test double)."""
    if registry is None:
        return []
    return [(cmd.invocation(), cmd.description) for cmd in registry.complete(prefix)]


def complete_at_path(prefix: str, cwd: str) -> "list[str]":
    """Paths (relative to `cwd`, forward-slashed) whose text (basename, or
    the trailing path segment being typed) starts with the last path
    segment of `prefix`, case-insensitively. `prefix` may itself contain
    slashes (`src/too`) -- everything before the last `/` names the
    directory to list; only ONE level is scanned per call (the popup
    re-queries as the user types past a `/`), matching a normal shell-style
    completion feel rather than a full recursive search."""
    prefix = prefix or ""
    if "/" in prefix:
        rel_dir, partial = prefix.rsplit("/", 1)
    else:
        rel_dir, partial = "", prefix
    base = os.path.join(cwd, rel_dir) if rel_dir else cwd
    partial_low = partial.lower()

    results: list = []
    visited = 0
    try:
        with os.scandir(base) as it:
            for entry in it:
                visited += 1
                if visited > SCAN_CAP:
                    break
                name = entry.name
                if name.startswith(".") and not partial.startswith("."):
                    continue
                if not name.lower().startswith(partial_low):
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                if is_dir and _is_pruned(name):
                    continue
                rel = f"{rel_dir}/{name}" if rel_dir else name
                results.append(rel + "/" if is_dir else rel)
                if len(results) >= MAX_RESULTS:
                    break
    except OSError:
        return []
    results.sort(key=str.lower)
    return results


def current_token(text: str, cursor_pos: int) -> "tuple[str, int, str]":
    """`(kind, start_index, token_text)` for the `/`- or `@`-prefixed token
    the cursor is currently inside of, scanning back from `cursor_pos` in
    the flat string `text` to the nearest preceding whitespace or start of
    string. `kind` is "slash" (only valid when the token starts at index 0
    -- a real Claude Code slash command is only recognized as the first
    thing on the line), "at", "arg" (Halo 2.0.2 brief A.4: an ARGUMENT of
    `/role ...`/`/roles set ...` -- see `role_command_arg_index`), or ""
    (no completion applies here)."""
    start = cursor_pos
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    token = text[start:cursor_pos]
    if token.startswith("/") and start == 0:
        return "slash", start, token[1:]
    if token.startswith("@"):
        return "at", start, token[1:]
    if role_command_arg_index(text, cursor_pos) is not None:
        return "arg", start, token
    if org_command_arg_index(text, cursor_pos) is not None:
        return "orgarg", start, token
    return "", start, token


# ---------------------------------------------------------------------------
# Halo 2.0.2 (W7 round 1, brief A.4): `/role <name> <model> [effort]` and
# `/roles set <name> <model> [effort]` argument completion. Pure, no
# textual/controller import -- `tui/app.py` supplies the live candidate
# lists (role names, model refs, effort levels) for whichever argument
# index these report; this module only ever decides "which argument" and
# "which of these candidates match."
# ---------------------------------------------------------------------------

_ROLE_ARG_COMMAND_RE = re.compile(r"^/role(?:s\s+set)?\s")


def role_command_arg_index(text: str, cursor_pos: int) -> "object":
    """`0`/`1`/`2` (role name / model / effort) for a cursor inside a
    `/role ...`/`/roles set ...` line's ARGUMENTS, or `None` when `text`
    isn't shaped like one at all -- `text`/`cursor_pos` are the WHOLE
    input and the live caret position, exactly like `current_token`'s own
    parameters (checked independently here since the two commands' shared
    `/role` prefix makes a plain startswith check ambiguous on its own).
    An index past 2 (a 4th+ argument) is still reported as-is -- the
    caller decides there's nothing left to complete."""
    m = _ROLE_ARG_COMMAND_RE.match(text)
    if not m:
        return None
    after_command = text[:cursor_pos][m.end():].lstrip(" \t")
    pieces = re.split(r"[ \t]+", after_command) if after_command else [""]
    return len(pieces) - 1


def role_command_args(text: str) -> "Optional[list]":
    """The FULL `/role ...`/`/roles set ...` line's own argument pieces
    (role name, model, effort, ...), with the command's own `/role`/
    `/roles set` prefix stripped off first -- `None` when `text` isn't
    shaped like either command at all. 2.0.2 review finding 24: shared
    with `role_command_arg_index` above (same prefix regex) so a caller
    completing one argument (the effort level) can read an EARLIER one
    (the model just typed) without re-splitting the whole line itself and
    getting the two commands' different prefix word counts wrong -- that
    was `tui/app.py::_complete_role_command_arg`'s own bug: it read
    `pieces[1]` of the UN-stripped line, which is the role name for
    `/role` and the literal word "set" for `/roles set`, never the model."""
    m = _ROLE_ARG_COMMAND_RE.match(text)
    if not m:
        return None
    after_command = text[m.end():].strip()
    return re.split(r"[ \t]+", after_command) if after_command else []


# ---------------------------------------------------------------------------
# 2.0.2 review ("No Tab completion for org/position names in /org ..."
# confirmed, round 2 notes; fix pass round B): `/org show|edit|run|load
# <name> ...` -- the FIRST argument of each of these four subcommands is
# an existing organization's own name (`run`'s own goal text after it is
# free-form prose, never completed). Deliberately narrower than the
# `/role` pair above: unlike `/role <name> <model> [effort]`, there is no
# slash-command surface that ever takes a bare POSITION name as its own
# argument (positions are only ever edited inside the `/org edit` form
# itself) -- "position names" in the org editor's own "Reports" field is
# a DIFFERENT, modal-dialog-local completion surface, not this one.
# ---------------------------------------------------------------------------

_ORG_ARG_COMMAND_RE = re.compile(r"^/org\s+(show|edit|run|load)\s")


def org_command_arg_index(text: str, cursor_pos: int) -> "object":
    """`0` for a cursor inside the org-NAME argument of `/org show|edit|
    run|load <name> ...`, `None` otherwise (including a cursor already
    past argument 0, e.g. `run`'s own goal text -- there is nothing to
    complete there)."""
    m = _ORG_ARG_COMMAND_RE.match(text)
    if not m:
        return None
    after_command = text[:cursor_pos][m.end():].lstrip(" \t")
    if not after_command:
        return 0
    pieces = re.split(r"[ \t]+", after_command)
    index = len(pieces) - 1
    return index if index == 0 else None


def filter_items(items: "list[str]", prefix: str) -> "list[str]":
    """Ranks `items` for a `prefix` the user is typing (brief A.4: "prefix
    first, then substring"): every item whose text starts with `prefix`
    (case-insensitive) first, in their original relative order, then
    every item that merely CONTAINS `prefix` elsewhere, same relative
    order -- never re-sorted alphabetically, so a caller's own meaningful
    ordering (role-table order, a model catalog's own ranking, ...)
    survives untouched within each tier. An empty `prefix` returns
    `items` unchanged (every item "starts with" "")."""
    if not prefix:
        return list(items)
    low = prefix.lower()
    starts, contains = [], []
    for it in items:
        it_low = it.lower()
        if it_low.startswith(low):
            starts.append(it)
        elif low in it_low:
            contains.append(it)
    return starts + contains
