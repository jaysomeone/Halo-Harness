"""rolo_claude.tools.grep_tool -- the Grep tool (H2 scope A). Wording
matches Claude Code's own Grep tool exactly. Uses `rg` when it's on PATH,
else a pure-Python engine with the SAME output modes -- both backends funnel
their raw matches through the ONE shared `_format` function below, so the
two are byte-identical for the same query whenever both are available
(tested directly in tests/test_tools_grep.py; `rg` isn't installed on this
build host, so that suite mostly exercises the Python engine for real and
skips the live rg-vs-Python comparison with a stated reason).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from rolo_claude.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = (
    "A powerful search tool built on ripgrep\n\n"
    "  Usage:\n"
    "  - ALWAYS use Grep for search tasks. NEVER invoke `grep` or `rg` as a Bash command.\n"
    "  - Supports full regex syntax (e.g., \"log.*Error\", \"function\\s+\\w+\")\n"
    "  - Filter files with glob parameter (e.g., \"*.js\", \"**/*.tsx\") or type parameter (e.g., \"js\", \"py\", \"rust\")\n"
    "  - Output modes: \"content\" shows matching lines, \"files_with_matches\" shows only file paths "
    "(default), \"count\" shows match counts\n"
    "  - Pattern syntax: ripgrep-compatible regex - literal braces need escaping (use `interface\\{\\}` "
    "to find `interface{}` in Go code)\n"
    "  - Multiline matching: by default patterns match within single lines only. For cross-line "
    "patterns like `struct \\{[\\s\\S]*?field`, use `multiline: true`"
)

_PRUNE_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__"})
_BINARY_SNIFF_BYTES = 8192
_MAX_FILE_BYTES = 5_000_000

_TYPE_EXTENSIONS = {
    "py": ["*.py"], "js": ["*.js", "*.jsx", "*.mjs", "*.cjs"], "ts": ["*.ts", "*.tsx"],
    "rust": ["*.rs"], "go": ["*.go"], "java": ["*.java"], "c": ["*.c", "*.h"],
    "cpp": ["*.cpp", "*.cc", "*.cxx", "*.hpp", "*.hh"], "json": ["*.json"],
    "md": ["*.md", "*.markdown"], "html": ["*.html", "*.htm"], "css": ["*.css"],
    "sh": ["*.sh", "*.bash"], "yaml": ["*.yaml", "*.yml"], "txt": ["*.txt"],
    "toml": ["*.toml"], "rb": ["*.rb"], "php": ["*.php"],
}


def _looks_binary(raw: bytes) -> bool:
    return b"\x00" in raw[:_BINARY_SNIFF_BYTES]


def _iter_files(base: Path, glob_pattern, file_type):
    if base.is_file():
        yield base
        return
    import fnmatch
    exts = _TYPE_EXTENSIONS.get(file_type) if file_type else None
    for root, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        for fname in filenames:
            if exts is not None and not any(fnmatch.fnmatch(fname, e) for e in exts):
                continue
            p = Path(root) / fname
            if glob_pattern:
                rel = str(p.relative_to(base)).replace(os.sep, "/")
                if not (fnmatch.fnmatch(fname, glob_pattern) or fnmatch.fnmatch(rel, glob_pattern)):
                    continue
            yield p


def _search_file(path: Path, regex, multiline: bool):
    """Returns list[(line_no, line_text)] or None if the file is binary/
    unreadable -- the SAME shape a parsed `rg` result is normalized to."""
    try:
        with open(path, "rb") as f:
            raw = f.read(_MAX_FILE_BYTES)
    except OSError:
        return None
    if _looks_binary(raw):
        return None
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    matches = []
    if multiline:
        seen = set()
        for m in regex.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            if line_no in seen:
                continue
            seen.add(line_no)
            matches.append((line_no, lines[line_no - 1] if 0 <= line_no - 1 < len(lines) else ""))
    else:
        for i, line in enumerate(lines, start=1):
            if regex.search(line):
                matches.append((i, line))
    return matches


def _read_lines(path: Path) -> list:
    try:
        with open(path, "rb") as f:
            raw = f.read(_MAX_FILE_BYTES)
        return raw.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _format(ordered: list, *, output_mode: str, show_line_numbers: bool,
            before: int, after: int, head_limit) -> str:
    """`ordered` is `[(path, [(line_no, line_text), ...]), ...]` -- the ONE
    shared shape both the Python engine and the rg-output parser produce, so
    formatting (and therefore the final text a model sees) never depends on
    which backend found the matches."""
    if output_mode == "files_with_matches":
        lines = [str(p) for p, _ in ordered]
    elif output_mode == "count":
        lines = [f"{p}:{len(m)}" for p, m in ordered]
    else:
        lines = []
        for p, matches in ordered:
            matched_nos = sorted({no for no, _ in matches})
            if not matched_nos:
                continue
            file_lines = _read_lines(p)
            window = set()
            for no in matched_nos:
                for k in range(max(1, no - before), min(len(file_lines), no + after) + 1):
                    window.add(k)
            prev = None
            for no in sorted(window):
                if prev is not None and no != prev + 1:
                    lines.append("--")
                text = file_lines[no - 1] if 0 <= no - 1 < len(file_lines) else ""
                sep = ":" if no in matched_nos else "-"
                lines.append(f"{p}:{no}{sep}{text}" if show_line_numbers else f"{p}{sep}{text}")
                prev = no
    if isinstance(head_limit, int) and head_limit > 0:
        lines = lines[:head_limit]
    return "\n".join(lines) if lines else "No matches found"


def _run_python_backend(pattern, regex, base, *, glob_pattern, file_type, output_mode,
                         show_line_numbers, before, after, head_limit):
    ordered = []
    for f in sorted(_iter_files(base, glob_pattern, file_type), key=str):
        matches = _search_file(f, regex, bool(regex.flags & re.DOTALL))
        if matches:
            ordered.append((f, matches))
    return _format(ordered, output_mode=output_mode, show_line_numbers=show_line_numbers,
                    before=before, after=after, head_limit=head_limit)


def _parse_rg_line_output(text: str) -> dict:
    """Parse ripgrep's `--line-number --no-heading` content output
    (`path:lineno:text`, split on the first two colons only -- `text` may
    itself contain colons) into the shared `{path: [(line_no, text), ...]}`
    shape."""
    per_file: dict = {}
    for raw_line in text.splitlines():
        parts = raw_line.split(":", 2)
        if len(parts) != 3:
            continue
        path_str, line_no_str, content = parts
        try:
            line_no = int(line_no_str)
        except ValueError:
            continue
        per_file.setdefault(Path(path_str), []).append((line_no, content))
    return per_file


class _RipgrepUnavailable(Exception):
    pass


def _run_ripgrep_backend(pattern, base, *, ignore_case, glob_pattern, file_type,
                          output_mode, show_line_numbers, before, after, head_limit, multiline):
    rg = shutil.which("rg")
    if not rg:
        raise _RipgrepUnavailable()
    # Always ask rg for line-numbered, unheaded content output so its raw
    # text can be parsed into the SAME shape the Python engine produces,
    # then rendered through the one shared `_format` -- our own -n/before/
    # after/head_limit are applied ourselves, not delegated to rg's flags.
    args = [rg, "--line-number", "--no-heading", "--color=never"]
    if ignore_case:
        args.append("-i")
    if multiline:
        args.extend(["-U", "--multiline-dotall"])
    if glob_pattern:
        args.extend(["--glob", glob_pattern])
    if file_type:
        args.extend(["--type", file_type])
    args.extend(["-e", pattern, str(base)])
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        raise _RipgrepUnavailable()
    if proc.returncode not in (0, 1):  # 1 == "no matches", not a failure
        raise _RipgrepUnavailable()
    per_file = _parse_rg_line_output(proc.stdout)
    ordered = sorted(per_file.items(), key=lambda kv: str(kv[0]))
    return _format(ordered, output_mode=output_mode, show_line_numbers=show_line_numbers,
                    before=before, after=after, head_limit=head_limit)


class GrepTool(Tool):
    name = "Grep"
    description = DESCRIPTION
    is_read_only = True
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "The regular expression pattern to search for in file contents"},
            "path": {"type": "string", "description": "File or directory to search in (rg PATH). Defaults to current working directory."},
            "glob": {"type": "string", "description": "Glob pattern to filter files (e.g. \"*.js\", \"*.{ts,tsx}\")"},
            "type": {"type": "string", "description": "File type to search (rg --type). Common types: js, py, rust, go, java, etc."},
            "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"],
                             "description": "Defaults to \"files_with_matches\"."},
            "-i": {"type": "boolean", "description": "Case insensitive search"},
            "-n": {"type": "boolean", "description": "Show line numbers in output. Defaults to true."},
            "-A": {"type": "number", "description": "Lines to show after each match (content mode only)"},
            "-B": {"type": "number", "description": "Lines to show before each match (content mode only)"},
            "-C": {"type": "number", "description": "Lines to show before and after each match (content mode only)"},
            "multiline": {"type": "boolean", "description": "Enable multiline mode where . matches newlines"},
            "head_limit": {"type": "integer", "description": "Limit output to the first N lines/entries"},
        },
        "required": ["pattern"],
    }

    def summary(self, input: dict) -> str:
        return f"Grep({input.get('pattern', '')})"

    def permission_content(self, input: dict) -> str:
        return input.get("path", "") if isinstance(input, dict) else ""

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        if not isinstance(input, dict):
            input = {}
        pattern = input.get("pattern")
        if not pattern or not isinstance(pattern, str):
            return ToolResult("The pattern parameter is required", is_error=True)

        output_mode = input.get("output_mode") or "files_with_matches"
        if output_mode not in ("content", "files_with_matches", "count"):
            return ToolResult(f"Invalid output_mode: {output_mode!r}", is_error=True)

        ignore_case = bool(input.get("-i"))
        multiline = bool(input.get("multiline"))
        show_line_numbers = bool(input.get("-n", True))
        context = input.get("-C")
        before = input.get("-B", context if context is not None else 0) or 0
        after = input.get("-A", context if context is not None else 0) or 0
        try:
            before, after = int(before), int(after)
        except (TypeError, ValueError):
            before, after = 0, 0
        head_limit = input.get("head_limit")

        flags = re.IGNORECASE if ignore_case else 0
        if multiline:
            flags |= re.MULTILINE | re.DOTALL
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult(f"Invalid regular expression {pattern!r}: {e}", is_error=True)

        base_str = input.get("path")
        base = Path(base_str) if base_str else ctx.cwd
        if not base.is_absolute():
            base = ctx.cwd / base
        if not base.exists():
            return ToolResult(f"Path does not exist: {base}", is_error=True)

        glob_pattern = input.get("glob")
        file_type = input.get("type")

        if shutil.which("rg"):
            try:
                body = _run_ripgrep_backend(
                    pattern, base, ignore_case=ignore_case, glob_pattern=glob_pattern, file_type=file_type,
                    output_mode=output_mode, show_line_numbers=show_line_numbers, before=before, after=after,
                    head_limit=head_limit, multiline=multiline,
                )
                return ToolResult(body)
            except _RipgrepUnavailable:
                pass  # fall through to the Python engine, never surface an rg-specific failure

        body = _run_python_backend(
            pattern, regex, base, glob_pattern=glob_pattern, file_type=file_type, output_mode=output_mode,
            show_line_numbers=show_line_numbers, before=before, after=after, head_limit=head_limit,
        )
        return ToolResult(body)
