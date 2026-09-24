"""rolo_claude.tools.read -- the Read tool (H1 scope G): cat -n numbering,
offset/limit (default 2000 lines), absolute path required. Wording matches
Claude Code's own exactly (binary-facts sec.14) so a weaker model gets
identical guidance to a real Claude Code session -- rule 3/D4 in the plan.
"""

from __future__ import annotations

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


class ReadTool(Tool):
    name = "Read"
    description = DESCRIPTION
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
        if not path.exists():
            return ToolResult(f"File does not exist: {file_path}", is_error=True)
        if path.is_dir():
            return ToolResult(f"Path is a directory, not a file: {file_path}", is_error=True)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ToolResult(f"Error reading file: {e}", is_error=True)

        lines = text.splitlines()
        offset = max(0, int(input.get("offset") or 0))
        limit = max(1, int(input.get("limit") or DEFAULT_LIMIT))
        selected = lines[offset:offset + limit]
        numbered = [f"{i + offset + 1:6d}\t{line}" for i, line in enumerate(selected)]
        body = "\n".join(numbered) if numbered else "(file is empty)"
        remaining = len(lines) - (offset + len(selected))
        if remaining > 0:
            body += f"\n... [{remaining} more line(s) not shown; pass offset={offset + len(selected)} to continue]"
        return ToolResult(body)
