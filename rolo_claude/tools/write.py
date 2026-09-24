"""rolo_claude.tools.write -- the Write tool (H2 scope A). Wording matches
Claude Code's own Write tool exactly (binary-facts sec.14 style). Must-Read-
first (unless the file is new), parent directories created on demand, and an
existing file's line ending style (CRLF vs LF) and UTF-8 BOM are preserved on
overwrite rather than silently normalized to LF/no-BOM.
"""

from __future__ import annotations

from pathlib import Path

from rolo_claude.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = (
    "Writes a file to the local filesystem.\n\n"
    "Usage:\n"
    "- This tool will overwrite the existing file if there is one at the provided path.\n"
    "- If this is an existing file, you MUST use the Read tool first to read the file's contents. "
    "This tool will fail if you did not read the file first.\n"
    "- Prefer the Edit tool for modifying existing files -- it only sends the diff. Only use this "
    "tool to create new files or for complete rewrites.\n"
    "- NEVER create documentation files (*.md) or README files unless explicitly requested by the User.\n"
    "- Only use emojis if the user explicitly requests it. Avoid writing emojis to files unless asked."
)


def _detect_bom(raw: bytes) -> bytes:
    return b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""


def _detect_newline(text: str) -> str:
    """"\r\n" if the file's FIRST line ending found is CRLF, else "\n" --
    matches the file's own dominant convention rather than mixing styles."""
    idx = text.find("\n")
    if idx > 0 and text[idx - 1] == "\r":
        return "\r\n"
    return "\n"


class WriteTool(Tool):
    name = "Write"
    description = DESCRIPTION
    is_destructive = True
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "The absolute path to the file to write (must be absolute, not relative)"},
            "content": {"type": "string", "description": "The content to write to the file"},
        },
        "required": ["file_path", "content"],
    }

    def summary(self, input: dict) -> str:
        return f"Write({input.get('file_path', '')})"

    def permission_content(self, input: dict) -> str:
        return input.get("file_path", "") if isinstance(input, dict) else ""

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        file_path = input.get("file_path") if isinstance(input, dict) else None
        content = input.get("content") if isinstance(input, dict) else None
        if not file_path or not isinstance(file_path, str):
            return ToolResult("The file_path parameter must be an absolute path, not a relative path", is_error=True)
        if not isinstance(content, str):
            return ToolResult("The content parameter is required and must be a string", is_error=True)
        path = Path(file_path)
        if not path.is_absolute():
            return ToolResult(
                f"The file_path parameter must be an absolute path, not a relative path: {file_path!r}",
                is_error=True,
            )

        read_cache = getattr(ctx, "read_cache", None) if isinstance(getattr(ctx, "read_cache", None), dict) else {}
        bom = b""
        newline = "\n"
        if path.exists():
            if path.is_dir():
                return ToolResult(f"Path is a directory, not a file: {file_path}", is_error=True)
            try:
                current_mtime = path.stat().st_mtime
            except OSError as e:
                return ToolResult(f"Error checking existing file: {e}", is_error=True)
            read_mtime = read_cache.get(str(path))
            if read_mtime is None:
                return ToolResult(
                    f"File has not been read yet. Read it first before writing to it: {file_path}",
                    is_error=True,
                )
            if read_mtime < current_mtime:
                return ToolResult(
                    f"File has been modified since it was last read (mtime mismatch). "
                    f"Read it again before writing: {file_path}",
                    is_error=True,
                )
            try:
                raw = path.read_bytes()
            except OSError as e:
                return ToolResult(f"Error reading existing file to preserve its formatting: {e}", is_error=True)
            bom = _detect_bom(raw)
            existing_text = raw[len(bom):].decode("utf-8", "replace")
            newline = _detect_newline(existing_text)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return ToolResult(f"Error creating parent directories: {e}", is_error=True)

        # Preserve the file's own line-ending convention: normalize the
        # incoming content to \n first (so a model that emits \n
        # consistently -- the overwhelmingly common case -- round-trips
        # exactly against a \n file), then re-expand to \r\n only if the
        # EXISTING file (or nothing, for a new file) was CRLF.
        body = content.replace("\r\n", "\n")
        if newline == "\r\n":
            body = body.replace("\n", "\r\n")

        try:
            with open(path, "wb") as f:
                f.write(bom)
                f.write(body.encode("utf-8"))
        except OSError as e:
            return ToolResult(f"Error writing file: {e}", is_error=True)

        try:
            read_cache[str(path)] = path.stat().st_mtime  # a write also counts as "read" for a later edit in the same turn
        except OSError:
            pass

        line_count = body.count("\n") + (1 if body and not body.endswith("\n") else 0)
        return ToolResult(f"File written successfully: {file_path} ({line_count} lines)")
