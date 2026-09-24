"""rolo_claude.tools.edit -- the Edit tool (H2 scope A). Wording matches
Claude Code's own Edit tool exactly (binary-facts sec.14). Exact
`old_string` match once (or every occurrence with `replace_all`); a
whitespace-tolerant fallback when the exact text isn't found (a model that
reproduces the right lines with slightly different indentation); a
structured "not found" / "N near-matches" error otherwise; a Read-since-mtime
check identical to Write's.
"""

from __future__ import annotations

from pathlib import Path

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


def _normalize_line(line: str) -> str:
    return line.strip()


def _leading_ws(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" \t"))]


def _tolerant_matches(content: str, old: str) -> list:
    """Start/end char offsets of every window of `content`'s lines whose
    per-line STRIPPED text matches `old`'s per-line stripped text (a rescue
    for an old_string that reproduces the right content with different
    indentation/trailing whitespace than the file actually has). finding 3:
    the span is the CONTENT only -- from the first matched line's first
    non-blank char to the last matched line's last non-blank char -- NEVER
    the first line's own leading indentation (kept untouched, before
    `start`) or the last line's trailing newline (kept untouched, after
    `end`), so a tolerant replacement can never eat either. Each match is
    `(start, end, first_line_indent)`; the indent is handed back so the
    caller can re-indent `new_string` by the file-vs-old_string delta."""
    old_lines = old.splitlines()
    if not old_lines:
        return []
    content_lines = content.splitlines(keepends=True)
    normalized_old = [_normalize_line(l) for l in old_lines]
    n, m = len(content_lines), len(old_lines)
    offsets = []
    pos = 0
    for l in content_lines:
        offsets.append(pos)
        pos += len(l)
    matches = []
    for i in range(0, n - m + 1):
        window = content_lines[i:i + m]
        if [_normalize_line(w) for w in window] == normalized_old:
            first_line, last_line = window[0], window[-1]
            first_indent = _leading_ws(first_line)
            last_content = last_line.rstrip("\r\n")
            start = offsets[i] + len(first_indent)
            end = offsets[i + m - 1] + len(last_content)
            matches.append((start, end, first_indent))
    return matches


def _reindent_new_string(new_string: str, *, old_first_indent: str, file_first_indent: str) -> str:
    """finding 3: shift every line of `new_string` by the delta between
    old_string's OWN first-line indentation and the file's actual first-
    matched-line indentation. The first line gets NO added prefix -- the
    replacement span already starts right after the file's real
    indentation (see `_tolerant_matches`), so re-adding it here would
    double it. Every OTHER line that starts with old_string's first-line
    indent has that shared prefix swapped for the file's (preserving any
    EXTRA relative indentation, e.g. a nested block); a line that doesn't
    share that prefix is re-indented from scratch."""
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
            return ToolResult("old_string and new_string must be different", is_error=True)
        if old_string == "":
            # finding 4: `"abc".replace("", "X")` inserts X between EVERY
            # character (and at both ends) -- never a meaningful edit.
            return ToolResult("old_string must not be empty", is_error=True)
        path = Path(file_path)
        if not path.is_absolute():
            return ToolResult(
                f"The file_path parameter must be an absolute path, not a relative path: {file_path!r}",
                is_error=True,
            )
        if not path.exists():
            return ToolResult(f"File does not exist: {file_path}", is_error=True)
        if path.is_dir():
            return ToolResult(f"Path is a directory, not a file: {file_path}", is_error=True)

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
        # finding 4: surrogateescape (not "replace") -- a byte in the
        # EXISTING file that isn't valid UTF-8 round-trips losslessly back
        # to its own original byte on write, instead of being permanently
        # replaced with U+FFFD (which re-encodes as a DIFFERENT byte
        # sequence, corrupting untouched parts of the file).
        content = raw[len(bom):].decode("utf-8", "surrogateescape")
        newline = _detect_newline(content)
        normalized_content = content.replace("\r\n", "\n")
        normalized_old = old_string.replace("\r\n", "\n")
        normalized_new = new_string.replace("\r\n", "\n")

        count = normalized_content.count(normalized_old)
        used_tolerant = False
        if count == 0:
            candidates = _tolerant_matches(normalized_content, normalized_old)
            if len(candidates) == 1:
                start, end, file_first_indent = candidates[0]
                old_first_indent = _leading_ws(normalized_old.splitlines()[0])
                reindented = _reindent_new_string(
                    normalized_new, old_first_indent=old_first_indent, file_first_indent=file_first_indent,
                )
                new_content = normalized_content[:start] + reindented + normalized_content[end:]
                used_tolerant = True
            elif len(candidates) == 0:
                return ToolResult(
                    f"String not found in file (0 near-matches): old_string did not match any text in {file_path}. "
                    f"Make sure it is an exact substring, or matches the file's lines once whitespace is ignored.",
                    is_error=True,
                )
            else:
                return ToolResult(
                    f"String not found exactly, and {len(candidates)} near-matches (ambiguous whitespace-only "
                    f"matches) were found in {file_path}. Provide more surrounding context to make old_string unique.",
                    is_error=True,
                )
        elif count > 1 and not replace_all:
            return ToolResult(
                f"old_string is not unique in the file: found {count} matches in {file_path}. "
                f"Either provide a larger string with more surrounding context to make it unique, "
                f"or use replace_all to change every instance.",
                is_error=True,
            )
        else:
            if replace_all:
                new_content = normalized_content.replace(normalized_old, normalized_new)
            else:
                new_content = normalized_content.replace(normalized_old, normalized_new, 1)

        if newline == "\r\n":
            new_content = new_content.replace("\n", "\r\n")

        # finding 4: encode BEFORE opening the file -- an encode failure
        # (e.g. new_string introduces a lone surrogate) must never
        # truncate the file first.
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

        occurrences = count if count > 0 else 1
        replaced = occurrences if replace_all else 1
        note = " (tolerant whitespace match)" if used_tolerant else ""
        return ToolResult(f"The file {file_path} has been updated ({replaced} replacement(s)){note}.")
