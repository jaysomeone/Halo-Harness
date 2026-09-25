"""rolo_claude.tools.edit -- the Edit tool (H2 scope A; H5 scope F item 1
replaces the old two-stage matcher with OpenCode's NINE-stage replacer
chain, in its own order, thresholds and error strings, verbatim where given
-- reports/OpenCode harness deep review.md Appendix C). Parameters and
description wording match Claude Code's own Edit tool (binary-facts
sec.14); everything below `_levenshtein` is the replacer chain itself, tried
in order until one stage yields exactly one match (or, with `replace_all`,
every exact match). A Read-since-mtime check identical to Write's still
gates every call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from rolo_claude.tools.base import Tool, ToolContext, ToolResult
from rolo_claude.tools.write import _detect_bom, _detect_newline

DESCRIPTION = (
    "Performs exact string replacements in files.\n\n"
    "Usage:\n"
    "- You must use your `Read` tool at least once in the conversation before editing. This tool "
    "will error if you attempt an edit without reading the file.\n"
    "- When editing text from Read tool output, ensure you preserve the exact indentation "
    "(tabs/spaces) as it appears AFTER the line number prefix. The line number prefix format is: "
    "line number + tab. Everything after that is the actual file content to match. Never include "
    "any part of the line number prefix in the old_string or new_string.\n"
    "- ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.\n"
    "- Only use emojis if the user explicitly requests it. Avoid adding emojis to files unless asked.\n"
    "- The edit will FAIL if `old_string` is not unique in the file. Either provide a larger string "
    "with more surrounding context to make it unique or use `replace_all` to change every instance "
    "of `old_string`.\n"
    "- Use `replace_all` for replacing and renaming strings across the file. This parameter is "
    "useful if you want to rename a variable for instance."
)

# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------

BLOCK_ANCHOR_SIMILARITY = 0.65      # stage 3 (Appendix C)
CONTEXT_AWARE_MATCH_FRACTION = 0.50  # stage 8 (Appendix C)


def _leading_ws(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" \t"))]


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[-1]


def _similarity(a: str, b: str) -> float:
    """Levenshtein similarity in [0, 1]; two equally-empty strings are
    trivially identical."""
    longest = max(len(a), len(b))
    return 1.0 if longest == 0 else 1.0 - (_levenshtein(a, b) / longest)


def _common_indent(lines: list) -> str:
    """The longest whitespace prefix every NON-BLANK line shares."""
    candidates = [_leading_ws(l) for l in lines if l.strip()]
    if not candidates:
        return ""
    shortest = min(candidates, key=len)
    for i in range(len(shortest)):
        if any(c[i:i + 1] != shortest[i] for c in candidates):
            return shortest[:i]
    return shortest


def _collapse_ws(s: str) -> str:
    return " ".join(s.split())


def _unescape(s: str) -> str:
    return (s.replace("\\r\\n", "\r\n").replace("\\n", "\n").replace("\\t", "\t")
             .replace('\\"', '"').replace("\\'", "'"))


def _line_offsets(content: str) -> "tuple[list, list]":
    lines = content.splitlines(keepends=True)
    offsets, pos = [], 0
    for l in lines:
        offsets.append(pos)
        pos += len(l)
    return lines, offsets


def _window_span(lines: list, offsets: list, i: int, m: int) -> "tuple[int, int, str]":
    """(start, end, first_line_indent) for lines[i:i+m] -- start sits right
    AFTER the first line's own indentation, end is the last line's last
    non-newline char, so a fuzzy replacement can never eat either."""
    first_indent = _leading_ws(lines[i])
    last_content = lines[i + m - 1].rstrip("\r\n")
    return offsets[i] + len(first_indent), offsets[i + m - 1] + len(last_content), first_indent


class Match:
    __slots__ = ("start", "end", "indent")

    def __init__(self, start: int, end: int, indent: str = ""):
        self.start, self.end, self.indent = start, end, indent


def _drop_overlapping_matches(matches: list) -> list:
    """H5c finding 17: a fuzzy stage's sliding window can yield OVERLAPPING
    candidates (e.g. `"a = 1\\na = 1\\na = 1\\n"` against a 2-line pattern
    matches at line-index 0 AND line-index 1 -- the second window starts
    inside the first). `EditTool.run`'s own splice loop walks matches in
    start order with `cursor = m.end` after each one; an overlapping
    SECOND match has `m.start < cursor`, so `content[cursor:m.start]`
    silently slices backward-to-empty (Python never raises on that) and
    `cursor` then jumps to the overlapping match's OWN end, silently
    dropping whatever real file content sat between the two matches --
    verified: `"a = 1\\na = 1\\na = 1\\n"` with old `"  a = 1\\n  a = 1"`
    (fuzzy `IndentationFlexible`) and `replace_all` corrupted the file to
    `"BB\\n"` instead of replacing two independent, non-overlapping lines.
    Keeps the FIRST (earliest-starting) match of any overlapping cluster,
    in file order, and drops every match that starts before the
    previously-kept one's own `end` -- "the first non-overlapping set"."""
    kept: list = []
    cursor = -1
    for m in sorted(matches, key=lambda mm: (mm.start, mm.end)):
        if m.start < cursor:
            continue  # overlaps the previously-kept match -- drop it
        kept.append(m)
        cursor = m.end
    return kept


# ---------------------------------------------------------------------------
# Stage 1: Simple -- exact substring (handled directly in EditTool.run, not
# here, since it needs the fast/common `str.count`/`str.replace` path).
# ---------------------------------------------------------------------------

def _sliding_window(lines: list, offsets: list, old_lines: list, transform: "Callable[[str], str]") -> list:
    """Every window of len(old_lines) lines whose per-line
    `transform(...)`-ed text equals old_lines' own transformed text --
    shared by stages 2 (LineTrimmed, transform=str.strip) and 4
    (WhitespaceNormalized, transform=_collapse_ws)."""
    n, m = len(lines), len(old_lines)
    if m == 0 or n < m:
        return []
    target = [transform(l) for l in old_lines]
    out = []
    for i in range(0, n - m + 1):
        window = [w.rstrip("\r\n") for w in lines[i:i + m]]
        if [transform(w) for w in window] == target:
            out.append(Match(*_window_span(lines, offsets, i, m)))
    return out


def stage_line_trimmed(content: str, old: str) -> list:
    lines, offsets = _line_offsets(content)
    return _sliding_window(lines, offsets, old.splitlines(), str.strip)


def stage_whitespace_normalized(content: str, old: str) -> list:
    lines, offsets = _line_offsets(content)
    return _sliding_window(lines, offsets, old.splitlines(), _collapse_ws)


def stage_block_anchor(content: str, old: str) -> list:
    """First/last lines (trimmed) anchor the window; middle lines scored by
    average Levenshtein SIMILARITY, threshold >= 0.65; the single BEST
    scoring candidate wins (not just the first).

    finding 2 (h4-h5-h3c review): "single best candidate" only means
    something when there IS a single best -- a short old_string (2 lines,
    no middle content at all: `old_middle` is empty and `score` is
    trivially 1.0 for EVERY window whose first/last line matches, the
    review's own repro case) can produce a genuine TIE at the top score.
    The old `score > best_score` strict comparison silently kept whichever
    tied window was found FIRST, which is the exact same "wrong-location
    edit reported as success" bug finding 2 targets, just hidden one level
    deeper than the stages it names explicitly. When multiple windows tie
    for the best score, ALL of them are returned instead of an arbitrary
    one, so `find_replacement`'s own ambiguity handling (skip this stage,
    or accept every one of them under `replace_all`) applies here too."""
    old_lines = old.splitlines()
    if len(old_lines) < 2:
        return []
    lines, offsets = _line_offsets(content)
    n, m = len(lines), len(old_lines)
    if n < m:
        return []
    first_anchor, last_anchor = old_lines[0].strip(), old_lines[-1].strip()
    old_middle = [l.strip() for l in old_lines[1:-1]]
    best_score = -1.0
    tied: list = []
    for i in range(0, n - m + 1):
        window = [w.rstrip("\r\n") for w in lines[i:i + m]]
        if window[0].strip() != first_anchor or window[-1].strip() != last_anchor:
            continue
        mid = [w.strip() for w in window[1:-1]]
        score = 1.0 if not old_middle else sum(_similarity(a, b) for a, b in zip(old_middle, mid)) / len(old_middle)
        if score > best_score:
            best_score, tied = score, [i]
        elif score == best_score:
            tied.append(i)
    if not tied or best_score < BLOCK_ANCHOR_SIMILARITY:
        return []
    return [Match(*_window_span(lines, offsets, i, m)) for i in tied]


def stage_indentation_flexible(content: str, old: str) -> list:
    """Common leading indentation stripped from BOTH old_string and each
    candidate window before comparing line-by-line."""
    old_lines = old.splitlines()
    if not old_lines:
        return []
    old_indent = _common_indent(old_lines)
    old_target = [l[len(old_indent):] if l.startswith(old_indent) else l.strip() for l in old_lines]
    lines, offsets = _line_offsets(content)
    n, m = len(lines), len(old_lines)
    out = []
    for i in range(0, n - m + 1):
        window = [w.rstrip("\r\n") for w in lines[i:i + m]]
        win_indent = _common_indent(window)
        win_stripped = [w[len(win_indent):] if w.startswith(win_indent) else w.strip() for w in window]
        if win_stripped == old_target:
            out.append(Match(*_window_span(lines, offsets, i, m)))
    return out


def stage_escape_normalized(content: str, old: str) -> list:
    """`\\n`/`\\t`/etc. unescaped in old_string, then an exact substring
    search (a model that double-escaped a string literal it copied from
    Read's own display)."""
    unescaped = _unescape(old)
    if unescaped == old or not unescaped:
        return []
    out, start = [], 0
    while True:
        idx = content.find(unescaped, start)
        if idx < 0:
            break
        out.append(Match(idx, idx + len(unescaped)))
        start = idx + max(1, len(unescaped))
    return out


def stage_trimmed_boundary(content: str, old: str) -> list:
    """Leading/trailing whitespace of the WHOLE old_string trimmed, then an
    exact substring search."""
    trimmed = old.strip()
    if trimmed == old or not trimmed:
        return []
    out, start = [], 0
    while True:
        idx = content.find(trimmed, start)
        if idx < 0:
            break
        out.append(Match(idx, idx + len(trimmed)))
        start = idx + max(1, len(trimmed))
    return out


def stage_context_aware(content: str, old: str) -> list:
    """First/last lines (trimmed) as anchors; >= 50% of MIDDLE lines must
    match (trimmed, exact) -- looser than BlockAnchor's Levenshtein
    scoring, so it catches a window with a few genuinely different middle
    lines that BlockAnchor's average similarity rejected."""
    old_lines = old.splitlines()
    if len(old_lines) < 2:
        return []
    lines, offsets = _line_offsets(content)
    n, m = len(lines), len(old_lines)
    if n < m:
        return []
    first_anchor, last_anchor = old_lines[0].strip(), old_lines[-1].strip()
    old_middle = [l.strip() for l in old_lines[1:-1]]
    out = []
    for i in range(0, n - m + 1):
        window = [w.rstrip("\r\n") for w in lines[i:i + m]]
        if window[0].strip() != first_anchor or window[-1].strip() != last_anchor:
            continue
        mid = [w.strip() for w in window[1:-1]]
        matched = sum(1 for a, b in zip(old_middle, mid) if a == b)
        fraction = 1.0 if not old_middle else matched / len(old_middle)
        if fraction >= CONTEXT_AWARE_MATCH_FRACTION:
            out.append(Match(*_window_span(lines, offsets, i, m)))
    return out


# Stage 9 (MultiOccurrence) needs no function of its own: it is stage 1
# (Simple/exact) applied with `replace_all=True`, which `EditTool.run`
# already handles via `str.count`/`str.replace` before this chain even
# starts -- see Appendix C: "used for replaceAll".

STAGES: "list[tuple[str, Callable[[str, str], list]]]" = [
    ("LineTrimmed", stage_line_trimmed),
    ("BlockAnchor", stage_block_anchor),
    ("WhitespaceNormalized", stage_whitespace_normalized),
    ("IndentationFlexible", stage_indentation_flexible),
    ("EscapeNormalized", stage_escape_normalized),
    ("TrimmedBoundary", stage_trimmed_boundary),
    ("ContextAware", stage_context_aware),
]


def _check_span_guard(old: str, matched_text: str) -> Optional[str]:
    """Appendix C span guard, verbatim thresholds: reject a fuzzy hit whose
    matched span is either >= `max(oldLines+3, oldLines*2)` LINES or whose
    length is > `max(len(oldString)+500, len(oldString)*4)` CHARS. Returns
    the (OpenCode-worded) error string on a violation, else None."""
    old_lines = max(1, len(old.splitlines()) or 1)
    span_lines = matched_text.count("\n") + 1
    line_limit = max(old_lines + 3, old_lines * 2)
    char_limit = max(len(old) + 500, len(old) * 4)
    if span_lines >= line_limit or len(matched_text) > char_limit:
        return ("Refusing replacement because the matched span is much larger than oldString "
                f"({span_lines} lines / {len(matched_text)} chars matched, oldString is {old_lines} "
                f"lines / {len(old)} chars) -- provide more surrounding context to narrow the match.")
    return None


def find_replacement(content: str, old: str, *, replace_all: bool = False) -> "tuple[Optional[list], Optional[str], str]":
    """Try each fuzzy stage in Appendix C order; the FIRST stage that
    yields either exactly one candidate, or (with `replace_all=True`) any
    number of candidates, wins. Returns (matches_or_None, stage_name,
    error).

    finding 2 (critical, h4-h5-h3c review): a stage that yields MULTIPLE
    candidates while `replace_all` is False is never silently narrowed to
    `candidates[0]` (the first occurrence in the file, a wrong-location
    edit that reports success) -- it is skipped ("move past ambiguous
    stages"), and the search continues to the next stage in the chain.
    BlockAnchor/ContextAware's own "single best/first-window" internals
    already keep them naturally unique in the common case, but
    LineTrimmed/WhitespaceNormalized/IndentationFlexible/EscapeNormalized/
    TrimmedBoundary all collect EVERY matching window/position, so any of
    them can legitimately be ambiguous (two near-identical blocks, or a
    short boilerplate line like `return x` appearing twice).

    If every stage that produced candidates was ambiguous and none ever
    resolved uniquely (nor, with `replace_all`, was accepted as a whole
    set), `error` carries an OpenCode-style "Found multiple matches..."
    message instead of the plain empty-string "not found" case -- an
    ambiguous fuzzy hit is a REFUSAL, never a silent guess at the first
    occurrence, and never conflated with "no candidate existed at all"."""
    saw_ambiguous = False
    ambiguous_stage: Optional[str] = None
    for name, stage_fn in STAGES:
        try:
            candidates = stage_fn(content, old)
        except Exception:
            continue
        if not candidates:
            continue
        if len(candidates) > 1 and not replace_all:
            saw_ambiguous = True
            if ambiguous_stage is None:
                ambiguous_stage = name
            continue
        if replace_all and len(candidates) > 1:
            # H5c finding 17: never accept overlapping candidates from a
            # fuzzy stage -- see `_drop_overlapping_matches`'s own
            # docstring for the exact corruption this prevents.
            chosen = _drop_overlapping_matches(candidates)
        else:
            chosen = [candidates[0]]
        guard_error = None
        for m in chosen:
            guard_error = _check_span_guard(old, content[m.start:m.end])
            if guard_error:
                break
        if guard_error:
            return None, name, guard_error
        return chosen, name, ""
    if saw_ambiguous:
        return None, ambiguous_stage, (
            f"Found multiple matches for oldString (fuzzy match via {ambiguous_stage}). Provide more "
            f"surrounding context to make oldString unique, or set replace_all to true to change every "
            f"occurrence."
        )
    return None, None, ""


def _reindent_new_string(new_string: str, *, old_first_indent: str, file_first_indent: str) -> str:
    """Shift every line of `new_string` by the delta between old_string's
    OWN first-line indentation and the file's actual matched-line
    indentation. The first line gets no added prefix (the replacement span
    already starts right after the file's real indentation); a later line
    sharing old_string's first-line indent has that shared prefix swapped
    for the file's (preserving extra relative indentation); anything else
    is re-indented from scratch."""
    if old_first_indent == file_first_indent:
        return new_string
    lines = new_string.split("\n")
    out = [lines[0].lstrip(" \t")]
    for line in lines[1:]:
        if line.startswith(old_first_indent):
            out.append(file_first_indent + line[len(old_first_indent):])
        elif line.strip():
            out.append(file_first_indent + line.lstrip(" \t"))
        else:
            out.append(line)
    return "\n".join(out)


class EditTool(Tool):
    name = "Edit"
    description = DESCRIPTION
    is_destructive = True
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "The absolute path to the file to modify"},
            "old_string": {"type": "string", "description": "The text to replace"},
            "new_string": {"type": "string", "description": "The text to replace it with (must be different from old_string)"},
            "replace_all": {"type": "boolean", "description": "Replace all occurrences of old_string (default false)", "default": False},
        },
        "required": ["file_path", "old_string", "new_string"],
    }

    def summary(self, input: dict) -> str:
        return f"Edit({input.get('file_path', '')})"

    def permission_content(self, input: dict) -> str:
        return input.get("file_path", "") if isinstance(input, dict) else ""

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        file_path = input.get("file_path") if isinstance(input, dict) else None
        old_string = input.get("old_string") if isinstance(input, dict) else None
        new_string = input.get("new_string") if isinstance(input, dict) else None
        replace_all = bool(input.get("replace_all")) if isinstance(input, dict) else False

        if not file_path or not isinstance(file_path, str):
            return ToolResult("The file_path parameter must be an absolute path, not a relative path", is_error=True)
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return ToolResult("old_string and new_string are required string parameters", is_error=True)
        if old_string == new_string:
            return ToolResult("No changes to apply: oldString and newString are identical.", is_error=True)
        path = Path(file_path)
        if not path.is_absolute():
            return ToolResult(
                f"The file_path parameter must be an absolute path, not a relative path: {file_path!r}",
                is_error=True,
            )
        if not path.exists():
            return ToolResult(f"File {file_path} not found", is_error=True)
        if path.is_dir():
            return ToolResult(f"Path is a directory, not a file: {file_path}", is_error=True)
        if old_string == "":
            # Appendix C: empty oldString is only ever valid for creating a
            # NEW file (Write's job) -- on an EXISTING file it can never be
            # a meaningful edit (`"abc".replace("", "X")` inserts X between
            # every character).
            return ToolResult(
                "oldString cannot be empty when editing an existing file. Use the Write tool instead "
                "to create a new file, or provide the exact text to replace.",
                is_error=True,
            )

        read_cache = getattr(ctx, "read_cache", None) if isinstance(getattr(ctx, "read_cache", None), dict) else {}
        try:
            current_mtime = path.stat().st_mtime
        except OSError as e:
            return ToolResult(f"Error checking file: {e}", is_error=True)
        read_mtime = read_cache.get(str(path))
        if read_mtime is None:
            return ToolResult(
                f"File has not been read yet. Use the Read tool at least once before editing: {file_path}",
                is_error=True,
            )
        if read_mtime < current_mtime:
            return ToolResult(
                f"File has been modified since it was last read (mtime mismatch). "
                f"Read it again before editing: {file_path}",
                is_error=True,
            )

        try:
            raw = path.read_bytes()
        except OSError as e:
            return ToolResult(f"Error reading file: {e}", is_error=True)
        bom = _detect_bom(raw)
        # surrogateescape (not "replace"): a byte in the EXISTING file that
        # isn't valid UTF-8 round-trips losslessly back to its own original
        # byte on write, instead of being permanently replaced with U+FFFD.
        content = raw[len(bom):].decode("utf-8", "surrogateescape")
        newline = _detect_newline(content)
        normalized_content = content.replace("\r\n", "\n")
        normalized_old = old_string.replace("\r\n", "\n")
        normalized_new = new_string.replace("\r\n", "\n")

        count = normalized_content.count(normalized_old)
        stage_used = "Simple"
        replaced_count = 1
        if count == 0:
            # finding 2: `replace_all` is threaded into the fuzzy chain too
            # -- a stage that finds several candidates is accepted (ALL of
            # them replaced) only when the caller asked for that; otherwise
            # it is skipped as ambiguous (find_replacement's own doc).
            matches, stage_name, guard_error = find_replacement(normalized_content, normalized_old,
                                                                  replace_all=replace_all)
            if guard_error:
                return ToolResult(guard_error, is_error=True)
            if not matches:
                return ToolResult(
                    "Could not find oldString in the file. It must match exactly, including whitespace "
                    f"and indentation: {file_path}",
                    is_error=True,
                )
            old_first_indent = _leading_ws(normalized_old.splitlines()[0]) if normalized_old.splitlines() else ""
            ordered = sorted(matches, key=lambda mm: mm.start)
            pieces: list = []
            cursor = 0
            for m in ordered:
                pieces.append(normalized_content[cursor:m.start])
                pieces.append(_reindent_new_string(normalized_new, old_first_indent=old_first_indent,
                                                     file_first_indent=m.indent))
                cursor = m.end
            pieces.append(normalized_content[cursor:])
            new_content = "".join(pieces)
            replaced_count = len(ordered)
            stage_used = stage_name
        elif count > 1 and not replace_all:
            return ToolResult(
                f"Found multiple matches for oldString: {count} matches in {file_path}. Provide more "
                f"surrounding context to make oldString unique, or set replace_all to true to change "
                f"every occurrence.",
                is_error=True,
            )
        else:
            if replace_all:
                new_content = normalized_content.replace(normalized_old, normalized_new)
                stage_used = "MultiOccurrence" if count > 1 else "Simple"
                replaced_count = count
            else:
                new_content = normalized_content.replace(normalized_old, normalized_new, 1)
                replaced_count = 1

        if newline == "\r\n":
            new_content = new_content.replace("\n", "\r\n")

        try:
            encoded = new_content.encode("utf-8", "surrogateescape")
        except UnicodeEncodeError as e:
            return ToolResult(f"Error encoding content as UTF-8: {e}", is_error=True)

        try:
            with open(path, "wb") as f:
                f.write(bom)
                f.write(encoded)
        except OSError as e:
            return ToolResult(f"Error writing file: {e}", is_error=True)

        try:
            read_cache[str(path)] = path.stat().st_mtime
        except OSError:
            pass

        note = "" if stage_used == "Simple" else f" (matched via {stage_used})"
        return ToolResult(f"The file {file_path} has been updated ({replaced_count} replacement(s)){note}.")
