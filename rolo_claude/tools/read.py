"""rolo_claude.tools.read -- the Read tool (H1 scope G): cat -n numbering,
offset/limit (default 2000 lines), absolute path required. Wording matches
Claude Code's own exactly (binary-facts sec.14) so a weaker model gets
identical guidance to a real Claude Code session -- rule 3/D4 in the plan.
"""

from __future__ import annotations

import stat
from pathlib import Path

from rolo_claude.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = (
    "Reads a file from the local filesystem. You can access any file directly by using this tool.\n"
    "Assume this tool is able to read all files on the machine. If the User provides a path to a file "
    "assume that path is valid. It is okay to read a file that does not exist; an error will be returned.\n\n"
    "Usage:\n"
    "- The file_path parameter must be an absolute path, not a relative path\n"
    "- By default, it reads up to 2000 lines starting from the beginning of the file\n"
    "- When you already know which part of the file you need, only read that part. This can be important "
    "for larger files.\n"
    "- Results are returned using cat -n format, with line numbers starting at 1\n"
    "- Try to maintain your current working directory throughout the session by using absolute paths and "
    "avoiding usage of `cd`."
)

DEFAULT_LIMIT = 2000
_MAX_LINE_CHARS = 2000  # finding 10: a minified/binary-ish "line" must never become one multi-MB block
_MAX_RESULT_CHARS = 25_000 * 4  # ~25k tokens at this codebase's own len(text)/4 estimate (providers/config.py)
_BINARY_SNIFF_BYTES = 8192


def _looks_binary(path: Path) -> bool:
    """A NUL byte in the first 8 KiB is the standard cheap binary sniff
    (git/most editors use the same heuristic) -- read as raw bytes, never
    through the text-mode path that would otherwise have to guess an
    encoding for content that isn't text at all."""
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return False


class ReadTool(Tool):
    name = "Read"
    description = DESCRIPTION
    is_read_only = True
    result_cap = None  # manages its own line/char caps below -- see _MAX_RESULT_CHARS
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "The absolute path to the file to read"},
            "offset": {"type": "integer", "description": "The line number to start reading from (0-based)"},
            "limit": {"type": "integer", "description": "The number of lines to read"},
        },
        "required": ["file_path"],
    }

    def summary(self, input: dict) -> str:
        return f"Read({input.get('file_path', '')})"

    def permission_content(self, input: dict) -> str:
        return input.get("file_path", "") if isinstance(input, dict) else ""

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        file_path = input.get("file_path") if isinstance(input, dict) else None
        if not file_path or not isinstance(file_path, str):
            return ToolResult("The file_path parameter must be an absolute path, not a relative path", is_error=True)
        path = Path(file_path)
        if not path.is_absolute():
            return ToolResult(
                f"The file_path parameter must be an absolute path, not a relative path: {file_path!r}",
                is_error=True,
            )
        try:
            st = path.stat()
        except OSError:
            return ToolResult(f"File does not exist: {file_path}", is_error=True)
        if stat.S_ISDIR(st.st_mode):
            return ToolResult(f"Path is a directory, not a file: {file_path}", is_error=True)
        if not stat.S_ISREG(st.st_mode):
            # finding 10: a FIFO/char-device/socket -- Read /dev/zero,
            # /dev/urandom, or a named pipe would otherwise hang the
            # process reading an infinite/blocking stream, or exhaust
            # memory well before any line-based cap below ever applies.
            return ToolResult(f"Not a regular file (refusing to read): {file_path}", is_error=True)
        if _looks_binary(path):
            return ToolResult(
                f"{file_path} appears to be a binary file ({st.st_size} bytes) -- refusing to read it as text.",
                is_error=True,
            )

        offset = max(0, int(input.get("offset") or 0))
        limit = max(1, int(input.get("limit") or DEFAULT_LIMIT))

        # finding 10: stream the file line by line, reading only up to
        # offset+limit(+1 peek) lines regardless of the file's real size --
        # a multi-GB regular file never gets materialized whole in memory
        # the way `path.read_text()` used to.
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for _ in range(offset):
                    if f.readline() == "":
                        break
                selected = []
                for _ in range(limit):
                    line = f.readline()
                    if line == "":
                        break
                    selected.append(line.rstrip("\r\n"))
                has_more = f.readline() != ""
        except OSError as e:
            return ToolResult(f"Error reading file: {e}", is_error=True)

        # H2: record this Read in the session's shared cache (mtime AT READ
        # TIME) -- Write's must-Read-first check and Edit's stale-file check
        # both consult this same dict (agent/loop.py threads ONE ToolContext.
        # read_cache instance through every dispatch call in a session).
        if isinstance(getattr(ctx, "read_cache", None), dict):
            ctx.read_cache[str(path)] = st.st_mtime

        numbered = []
        for i, line in enumerate(selected):
            if len(line) > _MAX_LINE_CHARS:
                line = line[:_MAX_LINE_CHARS] + f"... [line truncated at {_MAX_LINE_CHARS} chars]"
            numbered.append(f"{i + offset + 1:6d}\t{line}")
        body = "\n".join(numbered) if numbered else "(file is empty)"

        size_truncated = len(body) > _MAX_RESULT_CHARS
        if size_truncated:
            body = body[:_MAX_RESULT_CHARS]

        hints = []
        if has_more:
            hints.append(f"pass offset={offset + len(selected)} to continue")
        if size_truncated:
            hints.append(f"result truncated at ~{_MAX_RESULT_CHARS // 4} tokens; use a smaller limit")
        if hints:
            body += "\n... [" + "; ".join(hints) + "]"
        return ToolResult(body)
