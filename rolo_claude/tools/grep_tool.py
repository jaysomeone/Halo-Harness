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

import fnmatch
import os
import re
import shutil
import stat
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


def _is_regular_file(path: Path) -> bool:
    """finding 8: checked before EVERY open -- a FIFO/char-device/socket
    walked into the result set would otherwise hang `open(path, "rb")`
    reading an infinite/blocking stream (verified: a dir containing a FIFO
    blocks the read-only pool forever, no timeout, no abort), exactly the
    same reasoning as tools/read.py's own S_ISREG check."""
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except OSError:
        return False


def _translate_glob_pattern(pattern: str) -> str:
    """gitignore-ish glob -> an ANCHORED regex (finding 8: fnmatch treats
    `?`/`[...]` as wildcards too and has no `**`/`{a,b}` support at all,
    silently missing `glob: "*.{ts,tsx}"` and `glob: "**/*.ts"`): `**` ->
    any depth (crosses `/`), `*` -> one path segment, `?` -> one char
    (never `/`), `{a,b,...}` -> alternation; everything else literal."""
    i, n = 0, len(pattern)
    out = []
    while i < n:
        c = pattern[i]
        if pattern[i:i + 2] == "**":
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        if c == "{":
            j = pattern.find("}", i)
            if j == -1:
                out.append(re.escape(c))
                i += 1
                continue
            options = pattern[i + 1:j].split(",")
            out.append("(?:" + "|".join(re.escape(o) for o in options) + ")")
            i = j + 1
            continue
        out.append(re.escape(c))
        i += 1
    return "^" + "".join(out) + "$"


_POSIX_CLASSES = {
    "alpha": "a-zA-Z", "digit": "0-9", "alnum": "a-zA-Z0-9", "upper": "A-Z", "lower": "a-z",
    "space": r"\s", "blank": r" \t", "punct": re.escape("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"),
    "cntrl": r"\x00-\x1f\x7f", "print": r"\x20-\x7e", "graph": r"\x21-\x7e", "xdigit": "0-9A-Fa-f",
}
_POSIX_CLASS_RE = re.compile(r"\[:(\w+):\]")


def _translate_posix_classes(pattern: str) -> str:
    """ripgrep/PCRE POSIX bracket classes (`[[:space:]]`, `[[:alpha:]]`,
    ...) aren't recognized by Python's `re` at all -- rewritten to their
    Python-`re`-legal equivalents (valid INSIDE a `[...]` bracket
    expression, which is the only place a POSIX class is legal) before
    compiling (finding 8: `foo[[:space:]]+bar` reported no matches)."""
    return _POSIX_CLASS_RE.sub(lambda m: _POSIX_CLASSES.get(m.group(1), m.group(0)), pattern)


def _iter_files(base: Path, glob_pattern, file_type):
    if base.is_file():
        if _is_regular_file(base):
            yield base
        return
    exts = _TYPE_EXTENSIONS.get(file_type) if file_type else None
    glob_re = re.compile(_translate_glob_pattern(glob_pattern)) if glob_pattern else None
    for root, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        for fname in filenames:
            if exts is not None and not any(fnmatch.fnmatch(fname, e) for e in exts):
                continue
            p = Path(root) / fname
            if glob_re is not None:
                rel = str(p.relative_to(base)).replace(os.sep, "/")
                if not (glob_re.match(fname) or glob_re.match(rel)):
                    continue
            if not _is_regular_file(p):
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
    """Parse ripgrep's `--line-number --no-heading --with-filename --null`
    content output into the shared `{path: [(line_no, text), ...]}` shape.
    finding 8: `--null` makes rg emit a NUL byte (never `:`) right after
    the path, so the path is split off UNAMBIGUOUSLY first -- neither a
    Windows drive letter's own `:` (`C:\\foo\\bar.py`) nor a colon inside
    the matched TEXT can be mistaken for the path/line-number separator
    (the old `split(":", 2)` broke on both); `--with-filename` (`-H`)
    forces the path prefix even when `base` is a single file, which rg
    otherwise omits on its own."""
    per_file: dict = {}
    for raw_line in text.split("\n"):
        if not raw_line or "\x00" not in raw_line:
            continue
        path_str, rest = raw_line.split("\x00", 1)
        line_no_str, sep, content = rest.partition(":")
        if not sep:
            continue
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
    # Always ask rg for line-numbered, unheaded, NUL-delimited content
    # output so its raw text can be parsed into the SAME shape the Python
    # engine produces, then rendered through the one shared `_format` --
    # our own -n/before/after/head_limit are applied ourselves, not
    # delegated to rg's flags. finding 8: `--with-filename` (`-H`, else
    # omitted for a single-file `base`) + `--null` (unambiguous path/text
    # split -- see _parse_rg_line_output).
    args = [rg, "--line-number", "--no-heading", "--with-filename", "--null", "--color=never"]
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
            # finding 8: ripgrep/PCRE POSIX bracket classes (`[[:space:]]`)
            # aren't valid Python `re` syntax on their own -- translated
            # first so the SAME pattern text works on both backends.
            regex = re.compile(_translate_posix_classes(pattern), flags)
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
        if file_type and file_type not in _TYPE_EXTENSIONS:
            # finding 8: an unrecognized `type` used to silently match
            # EVERY file (no filter applied at all) instead of telling the
            # model its filter never took effect.
            return ToolResult(
                f"Unknown type: {file_type!r}. Supported types: {', '.join(sorted(_TYPE_EXTENSIONS))}",
                is_error=True,
            )

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
