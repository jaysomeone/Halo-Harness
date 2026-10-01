"""rolo_claude.tools.glob_tool -- the Glob tool (H2 scope A). Wording
matches Claude Code's own Glob tool exactly. Recursive glob (`**` included),
mtime-descending sort, a 500-result cap, and `.git`/`node_modules`/`.venv`/
`__pycache__` pruned out of every result regardless of how permissive the
pattern is (module named glob_tool.py, not glob.py, purely so nothing here
is ever mistaken for -- or shadows -- the stdlib `glob` module by a reader
skimming file names)."""

from __future__ import annotations

from pathlib import Path

from rolo_claude.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = (
    "- Fast file pattern matching tool that works with any codebase size\n"
    "- Supports glob patterns like \"**/*.js\" or \"src/**/*.ts\"\n"
    "- Returns matching file paths sorted by modification time\n"
    "- Use this tool when you need to find files by name patterns\n"
    "- When you are doing an open ended search that may require multiple rounds of globbing and "
    "grepping, use the Agent tool instead (if available)"
)

_PRUNE_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__"})
MAX_RESULTS = 500


class GlobTool(Tool):
    name = "Glob"
    description = DESCRIPTION
    is_read_only = True
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "The glob pattern to match files against"},
            "path": {"type": "string", "description": (
                "The directory to search in. If not specified, the current working directory will "
                "be used. IMPORTANT: Omit this field to use the default directory. DO NOT enter "
                "\"undefined\" or \"null\" - simply omit it for the default behavior. Must be a "
                "valid directory path if provided."
            )},
        },
        "required": ["pattern"],
    }

    def summary(self, input: dict) -> str:
        return f"Glob({input.get('pattern', '')})"

    def permission_content(self, input: dict) -> str:
        return input.get("path", "") if isinstance(input, dict) else ""

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        pattern = input.get("pattern") if isinstance(input, dict) else None
        if not pattern or not isinstance(pattern, str):
            return ToolResult("The pattern parameter is required", is_error=True)

        base_str = input.get("path") if isinstance(input, dict) else None
        base = Path(base_str) if base_str else ctx.cwd
        if not base.is_absolute():
            base = ctx.cwd / base
        if not base.exists():
            return ToolResult(f"Path does not exist: {base}", is_error=True)
        if not base.is_dir():
            return ToolResult(f"Path is not a directory: {base}", is_error=True)

        try:
            candidates = list(base.glob(pattern))
        except (ValueError, NotImplementedError, OSError) as e:
            return ToolResult(f"Invalid glob pattern {pattern!r}: {e}", is_error=True)

        matches = []
        for p in candidates:
            try:
                if not p.is_file():
                    continue
                if any(part in _PRUNE_DIRS for part in p.relative_to(base).parts[:-1]):
                    continue
                mtime = p.stat().st_mtime
            except OSError:
                continue
            matches.append((mtime, p))

        matches.sort(key=lambda t: t[0], reverse=True)
        total = len(matches)
        matches = matches[:MAX_RESULTS]
        if not matches:
            return ToolResult("No files found")
        lines = [str(p) for _, p in matches]
        if total > MAX_RESULTS:
            lines.append(f"... [showing {MAX_RESULTS} of {total} matches; narrow the pattern for the rest]")
        return ToolResult("\n".join(lines))
