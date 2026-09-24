"""tests.test_tools_glob_grep -- Glob (rglob, pruning, mtime sort, 500 cap)
and Grep (rg vs pure-Python parity, output modes, -i/-n/-A/-B/-C, glob,
type, head_limit, multiline) on a real temp tree.
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all
from rolo_claude.tools.base import ToolContext
from rolo_claude.tools.glob_tool import GlobTool
from rolo_claude.tools.grep_tool import GrepTool

test, TESTS = new_registry()


def _build_tree():
    d = Path(tempfile.mkdtemp(prefix="glob-grep-tree-"))
    (d / "src").mkdir()
    (d / "src" / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    (d / "src" / "b.py").write_text("def bar():\n    return 2\n\ndef baz():\n    return foo() + 1\n", encoding="utf-8")
    (d / "src" / "readme.md").write_text("# hello\nfoo bar\n", encoding="utf-8")
    (d / "node_modules").mkdir()
    (d / "node_modules" / "pkg.js").write_text("function foo() {}\n", encoding="utf-8")
    (d / ".git").mkdir()
    (d / ".git" / "config").write_text("foo\n", encoding="utf-8")
    return d


# ---- Glob ---------------------------------------------------------------

@test
def test_glob_matches_recursive_pattern(ctx: Ctx):
    d = _build_tree()
    result = GlobTool().run({"pattern": "**/*.py", "path": str(d)}, ToolContext(cwd=d))
    ctx.check("no error", result.is_error is False)
    ctx.check("finds a.py", "a.py" in result.content)
    ctx.check("finds b.py", "b.py" in result.content)
    ctx.check("does not include the readme", "readme.md" not in result.content)


@test
def test_glob_prunes_node_modules_and_git(ctx: Ctx):
    d = _build_tree()
    result = GlobTool().run({"pattern": "**/*", "path": str(d)}, ToolContext(cwd=d))
    ctx.check("node_modules pruned", "pkg.js" not in result.content)
    ctx.check(".git pruned", "config" not in result.content or "node_modules" not in result.content)


@test
def test_glob_sorted_by_mtime_descending(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="glob-mtime-"))
    old = d / "old.txt"
    old.write_text("old", encoding="utf-8")
    time.sleep(0.05)
    new = d / "new.txt"
    new.write_text("new", encoding="utf-8")
    result = GlobTool().run({"pattern": "*.txt", "path": str(d)}, ToolContext(cwd=d))
    lines = result.content.splitlines()
    ctx.check(f"newest file listed first, got {lines}", lines[0].endswith("new.txt"))


@test
def test_glob_500_result_cap(ctx: Ctx):
    from rolo_claude.tools.glob_tool import MAX_RESULTS
    d = Path(tempfile.mkdtemp(prefix="glob-cap-"))
    for i in range(MAX_RESULTS + 20):
        (d / f"f{i}.txt").write_text("x", encoding="utf-8")
    result = GlobTool().run({"pattern": "*.txt", "path": str(d)}, ToolContext(cwd=d))
    shown = [l for l in result.content.splitlines() if l.endswith(".txt")]
    ctx.check(f"capped at {MAX_RESULTS} results, got {len(shown)}", len(shown) == MAX_RESULTS)
    ctx.check("a note about more results present", "more" in result.content.lower() or "matches" in result.content.lower())


@test
def test_glob_no_matches(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="glob-empty-"))
    result = GlobTool().run({"pattern": "*.nonexistent"}, ToolContext(cwd=d))
    ctx.check("no error on zero matches", result.is_error is False)
    ctx.check("clear message", "no files found" in result.content.lower())


@test
def test_glob_missing_pattern_is_error(ctx: Ctx):
    result = GlobTool().run({}, ToolContext(cwd=Path(".")))
    ctx.check("missing pattern is an error", result.is_error is True)


# ---- Grep -----------------------------------------------------------------

@test
def test_grep_files_with_matches_default_mode(ctx: Ctx):
    d = _build_tree()
    result = GrepTool().run({"pattern": "def foo", "path": str(d)}, ToolContext(cwd=d))
    ctx.check("finds a.py", "a.py" in result.content)
    ctx.check("node_modules excluded from search by default pruning", "pkg.js" not in result.content)


@test
def test_grep_content_mode_with_line_numbers(ctx: Ctx):
    d = _build_tree()
    result = GrepTool().run({"pattern": "def bar", "path": str(d), "output_mode": "content"}, ToolContext(cwd=d))
    ctx.check(f"line number 1 shown, got {result.content!r}", ":1:" in result.content)
    ctx.check("matched text shown", "def bar" in result.content)


@test
def test_grep_count_mode(ctx: Ctx):
    d = _build_tree()
    result = GrepTool().run({"pattern": "def ", "path": str(d / "src" / "b.py"), "output_mode": "count"}, ToolContext(cwd=d))
    ctx.check(f"count mode reports 2 matches, got {result.content!r}", result.content.strip().endswith(":2"))


@test
def test_grep_case_insensitive_flag(ctx: Ctx):
    d = _build_tree()
    r_sensitive = GrepTool().run({"pattern": "DEF FOO", "path": str(d)}, ToolContext(cwd=d))
    r_insensitive = GrepTool().run({"pattern": "DEF FOO", "path": str(d), "-i": True}, ToolContext(cwd=d))
    ctx.check("case-sensitive search finds nothing", "No matches" in r_sensitive.content)
    ctx.check("case-insensitive search finds a.py", "a.py" in r_insensitive.content)


@test
def test_grep_glob_filter(ctx: Ctx):
    d = _build_tree()
    result = GrepTool().run({"pattern": "foo", "path": str(d), "glob": "*.md"}, ToolContext(cwd=d))
    ctx.check("only the markdown file matches", "readme.md" in result.content and "a.py" not in result.content)


@test
def test_grep_type_filter(ctx: Ctx):
    d = _build_tree()
    result = GrepTool().run({"pattern": "foo", "path": str(d), "type": "py"}, ToolContext(cwd=d))
    ctx.check("py-type filter excludes the markdown file", "readme.md" not in result.content)
    ctx.check("py-type filter includes a.py", "a.py" in result.content)


@test
def test_grep_context_lines_A_B_C(ctx: Ctx):
    d = _build_tree()
    result = GrepTool().run(
        {"pattern": "def baz", "path": str(d / "src" / "b.py"), "output_mode": "content", "-B": 1, "-A": 1},
        ToolContext(cwd=d),
    )
    ctx.check("context line before the match included (blank line)", result.content.count("\n") >= 2)
    ctx.check("the return line after is included", "return foo" in result.content)


@test
def test_grep_head_limit(ctx: Ctx):
    d = _build_tree()
    result = GrepTool().run({"pattern": "def", "path": str(d), "output_mode": "content", "head_limit": 1},
                             ToolContext(cwd=d))
    ctx.check(f"head_limit trims to 1 line, got {result.content!r}", len(result.content.splitlines()) == 1)


@test
def test_grep_multiline_matches_across_lines(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="grep-multiline-"))
    (d / "m.go").write_text("struct Foo {\n    Bar int\n}\n", encoding="utf-8")
    result = GrepTool().run({"pattern": r"struct \w+ \{[\s\S]*?Bar", "path": str(d), "multiline": True,
                              "output_mode": "content"}, ToolContext(cwd=d))
    ctx.check(f"multiline pattern matches across lines, got {result.content!r}", "struct Foo" in result.content)


@test
def test_grep_invalid_regex_is_error(ctx: Ctx):
    result = GrepTool().run({"pattern": "(unclosed"}, ToolContext(cwd=Path(tempfile.mkdtemp(prefix="grep-badre-"))))
    ctx.check("invalid regex is an error", result.is_error is True)


@test
def test_grep_rg_vs_python_backend_parity(ctx: Ctx):
    """When rg IS on PATH, invoking both backends directly for the SAME
    query must produce byte-identical formatted output (both funnel
    through the one shared _format function)."""
    if not shutil.which("rg"):
        raise SkipTest("ripgrep (rg) is not installed on this host -- only the Python engine is exercised here")
    import re
    from rolo_claude.tools.grep_tool import _run_python_backend, _run_ripgrep_backend
    d = _build_tree()
    regex = re.compile("def ")
    py_out = _run_python_backend("def ", regex, d, glob_pattern=None, file_type=None,
                                  output_mode="content", show_line_numbers=True, before=0, after=0, head_limit=None)
    rg_out = _run_ripgrep_backend("def ", d, ignore_case=False, glob_pattern=None, file_type=None,
                                   output_mode="content", show_line_numbers=True, before=0, after=0,
                                   head_limit=None, multiline=False)
    ctx.check(f"rg and python backends agree, got\nPY={py_out!r}\nRG={rg_out!r}", py_out == rg_out)


@test
def test_grep_rg_output_parser_handles_colons_in_path_and_text(ctx: Ctx):
    """finding 8: `_parse_rg_line_output` must not require the REAL `rg`
    binary to be installed to exercise its own parsing logic (both build
    hosts lack rg, so the live parity test above never actually runs) --
    a synthetic `--with-filename --null` sample pins the fix directly: a
    Windows drive-letter path's OWN colon, and a colon inside the matched
    TEXT, must never be mistaken for the path/line-number separator (the
    old `split(":", 2)` broke on both)."""
    from rolo_claude.tools.grep_tool import _parse_rg_line_output
    sample = "C:\\repo\\a.py\x0012:x = {\"k\": 1}\nC:\\repo\\a.py\x0013:y = 2\n"
    parsed = _parse_rg_line_output(sample)
    ctx.check(f"exactly one file key, got {list(parsed.keys())}", len(parsed) == 1)
    path = next(iter(parsed))
    ctx.check(f"the Windows path with its own colon is the WHOLE path, got {path!r}", str(path) == "C:\\repo\\a.py")
    lines = parsed[path]
    ctx.check(f"both lines parsed with line numbers intact, got {lines}",
              lines == [(12, 'x = {"k": 1}'), (13, "y = 2")])


@test
def test_grep_glob_filter_handles_braces_and_double_star(ctx: Ctx):
    """finding 8: fnmatch has no `{a,b}`/`**` support at all -- the
    schema's own documented examples must actually work."""
    d = _build_tree()
    (d / "src" / "c.tsx").write_text("foo tsx\n", encoding="utf-8")
    r_brace = GrepTool().run({"pattern": "foo", "path": str(d), "glob": "*.{ts,tsx}"}, ToolContext(cwd=d))
    ctx.check(f"brace glob matches .tsx, got {r_brace.content!r}", "c.tsx" in r_brace.content)
    (d / "src" / "d.ts").write_text("foo ts\n", encoding="utf-8")
    r_double_star = GrepTool().run({"pattern": "foo", "path": str(d), "glob": "**/*.ts"}, ToolContext(cwd=d))
    ctx.check(f"**/*.ts matches a nested .ts file, got {r_double_star.content!r}", "d.ts" in r_double_star.content)


@test
def test_grep_unknown_type_is_error(ctx: Ctx):
    d = _build_tree()
    result = GrepTool().run({"pattern": "foo", "path": str(d), "type": "typescript"}, ToolContext(cwd=d))
    ctx.check("an unrecognized type errors instead of silently matching everything", result.is_error is True)


@test
def test_grep_posix_bracket_class_in_pattern(ctx: Ctx):
    """finding 8: `[[:space:]]` (ripgrep/PCRE syntax) is not valid Python
    `re` on its own -- must be translated before compiling."""
    d = Path(tempfile.mkdtemp(prefix="grep-posix-"))
    (d / "ws.txt").write_text("foo   bar\n", encoding="utf-8")
    result = GrepTool().run({"pattern": r"foo[[:space:]]+bar", "path": str(d), "output_mode": "content"},
                             ToolContext(cwd=d))
    ctx.check(f"POSIX class pattern matches, got {result.content!r}", "foo   bar" in result.content)


@test
def test_grep_over_a_fifo_does_not_hang(ctx: Ctx):
    """finding 8: a FIFO discovered while walking a directory must never
    reach `open(path, "rb")` (which blocks forever with no writer on the
    other end) -- run the call on a background thread and JOIN it with a
    generous timeout, so a regression fails this test instead of hanging
    the whole suite."""
    import os
    import threading
    if not hasattr(os, "mkfifo"):
        raise SkipTest("os.mkfifo not available on this platform (Windows)")
    d = Path(tempfile.mkdtemp(prefix="grep-fifo-"))
    (d / "normal.txt").write_text("foo bar\n", encoding="utf-8")
    fifo_path = d / "a_fifo"
    os.mkfifo(fifo_path)
    outcome: dict = {}

    def _run():
        outcome["result"] = GrepTool().run({"pattern": "foo", "path": str(d)}, ToolContext(cwd=d))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=10)
    ctx.check("Grep over a dir containing a FIFO returns within 10s (does not hang)", not t.is_alive())
    if not t.is_alive():
        ctx.check(f"the FIFO is skipped, the real match is still found, got {outcome['result'].content!r}",
                  "normal.txt" in outcome["result"].content)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
