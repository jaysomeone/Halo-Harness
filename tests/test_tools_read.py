"""tests.test_tools_read -- tools/{base,registry,read}.py: cat -n
numbering, offset/limit, absolute-path requirement, registry dispatch and
name-sorted definitions.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all
from rolo_claude.tools.base import ToolContext
from rolo_claude.tools.read import ReadTool
from rolo_claude.tools.registry import ToolRegistry

test, TESTS = new_registry()


@test
def test_read_cat_n_numbering(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="read-tool-"))
    f = d / "sample.txt"
    f.write_text("line one\nline two\nline three\n", encoding="utf-8")
    tool = ReadTool()
    result = tool.run({"file_path": str(f)}, ToolContext(cwd=d))
    ctx.check("not an error", result.is_error is False)
    ctx.check("cat -n style: line 1 numbered", "     1\tline one" in result.content)
    ctx.check("line 3 numbered", "     3\tline three" in result.content)


@test
def test_read_offset_and_limit(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="read-tool-offset-"))
    f = d / "many.txt"
    f.write_text("\n".join(f"L{i}" for i in range(1, 21)) + "\n", encoding="utf-8")
    tool = ReadTool()
    result = tool.run({"file_path": str(f), "offset": 5, "limit": 3}, ToolContext(cwd=d))
    ctx.check("offset+limit selects the right window", "L6" in result.content and "L7" in result.content and "L8" in result.content)
    ctx.check("line before the window absent", "L5\n" not in result.content and not result.content.strip().startswith("     5"))
    ctx.check("line after the window absent from the body proper", "L9" not in result.content.split("pass offset")[0])
    # finding 10: the hint is offset-based, deliberately WITHOUT a precise
    # remaining-line count -- computing one would mean reading the rest of
    # the file, exactly what streaming offset+limit is meant to avoid.
    ctx.check("truncation note hints the next offset to continue from", "pass offset=8 to continue" in result.content)


@test
def test_read_default_limit_is_2000(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="read-tool-default-"))
    f = d / "huge.txt"
    f.write_text("\n".join(f"L{i}" for i in range(1, 2101)) + "\n", encoding="utf-8")
    tool = ReadTool()
    result = tool.run({"file_path": str(f)}, ToolContext(cwd=d))
    ctx.check("L2000 included", "L2000" in result.content)
    ctx.check("L2001 excluded (default 2000-line cap)", "\tL2001" not in result.content)


@test
def test_read_relative_path_rejected(ctx: Ctx):
    tool = ReadTool()
    result = tool.run({"file_path": "relative/path.txt"}, ToolContext(cwd=Path(".")))
    ctx.check("relative path -> is_error", result.is_error is True)
    ctx.check("error message names the requirement", "absolute" in result.content.lower())


@test
def test_read_missing_file(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="read-tool-missing-"))
    tool = ReadTool()
    result = tool.run({"file_path": str(d / "does-not-exist.txt")}, ToolContext(cwd=d))
    ctx.check("missing file -> is_error", result.is_error is True)
    ctx.check("error names the file", "does-not-exist.txt" in result.content)


@test
def test_read_directory_rejected(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="read-tool-dir-"))
    tool = ReadTool()
    result = tool.run({"file_path": str(d)}, ToolContext(cwd=d))
    ctx.check("a directory path -> is_error", result.is_error is True)


@test
def test_read_empty_file(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="read-tool-empty-"))
    f = d / "empty.txt"
    f.write_text("", encoding="utf-8")
    tool = ReadTool()
    result = tool.run({"file_path": str(f)}, ToolContext(cwd=d))
    ctx.check("empty file is not an error", result.is_error is False)


@test
def test_read_refuses_non_regular_file(ctx: Ctx):
    """finding 10: a FIFO must be refused, never opened for a blocking
    read (Read /dev/zero, /dev/urandom, or a named pipe hangs the process
    or exhausts memory) -- POSIX only (os.mkfifo doesn't exist on
    Windows)."""
    import os
    if not hasattr(os, "mkfifo"):
        raise SkipTest("os.mkfifo not available on this platform (Windows)")
    d = Path(tempfile.mkdtemp(prefix="read-tool-fifo-"))
    fifo_path = d / "a.fifo"
    os.mkfifo(fifo_path)
    tool = ReadTool()
    result = tool.run({"file_path": str(fifo_path)}, ToolContext(cwd=d))
    ctx.check("a FIFO is refused, not opened", result.is_error is True)
    ctx.check("error names it as non-regular", "regular file" in result.content.lower())


@test
def test_read_flags_binary_content(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="read-tool-binary-"))
    f = d / "data.bin"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4)
    tool = ReadTool()
    result = tool.run({"file_path": str(f)}, ToolContext(cwd=d))
    ctx.check("a binary file is flagged as an error, not dumped as text", result.is_error is True)
    ctx.check("error names it as binary", "binary" in result.content.lower())


@test
def test_read_truncates_a_very_long_line(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="read-tool-longline-"))
    f = d / "minified.js"
    f.write_text("x=" + ("a" * 5000) + ";\nshort line\n", encoding="utf-8")
    tool = ReadTool()
    result = tool.run({"file_path": str(f)}, ToolContext(cwd=d))
    ctx.check("not an error", result.is_error is False)
    first_line = result.content.splitlines()[0]
    ctx.check(f"first line capped well under the raw 5000+ chars, got {len(first_line)}", len(first_line) < 2200)
    ctx.check("truncation is noted on the line itself", "truncated" in first_line)
    ctx.check("the short second line still comes through untouched", "short line" in result.content)


@test
def test_read_caps_total_result_size(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="read-tool-hugeresult-"))
    f = d / "huge.txt"
    # ~2000 lines * ~100 chars/line ~ 200k chars, comfortably over the
    # ~100k-char (~25k token) result cap even with a generous default limit.
    f.write_text("\n".join(("y" * 95) for _ in range(2000)) + "\n", encoding="utf-8")
    tool = ReadTool()
    result = tool.run({"file_path": str(f)}, ToolContext(cwd=d))
    ctx.check(f"result stays near the ~25k-token cap, got {len(result.content)} chars", len(result.content) < 105_000)
    ctx.check("size-cap hint present", "truncated at ~25000 tokens" in result.content)


@test
def test_registry_definitions_name_sorted(ctx: Ctx):
    reg = ToolRegistry()
    defs = reg.definitions()
    names = [d["name"] for d in defs]
    ctx.check(f"name-sorted, got {names}", names == sorted(names))
    ctx.check("Read is present", "Read" in names)


@test
def test_registry_dispatch_and_unknown_tool(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="registry-dispatch-"))
    f = d / "x.txt"
    f.write_text("hi\n", encoding="utf-8")
    reg = ToolRegistry()
    result = reg.dispatch("Read", {"file_path": str(f)}, ToolContext(cwd=d))
    ctx.check("dispatch to Read works", result.is_error is False and "hi" in result.content)

    unknown = reg.dispatch("NotARealTool", {}, ToolContext(cwd=d))
    ctx.check("unknown tool name -> error result, not an exception", unknown.is_error is True)
    ctx.check("names the unknown tool", "NotARealTool" in unknown.content)


@test
def test_registry_dispatch_never_raises_on_tool_exception(ctx: Ctx):
    from rolo_claude.tools.base import Tool, ToolResult

    class BoomTool(Tool):
        name = "Boom"
        description = "always raises"

        def run(self, input, ctx):
            raise RuntimeError("boom")

    reg = ToolRegistry(tools=[BoomTool()])
    result = reg.dispatch("Boom", {}, ToolContext(cwd=Path(".")))
    ctx.check("a tool raising an exception becomes an error result, never propagates", result.is_error is True)
    ctx.check("mentions the exception", "boom" in result.content.lower())


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
