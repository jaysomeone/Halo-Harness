"""tests.test_tools_write_edit -- the Write and Edit tools (H2 scope A):
must-Read-first, parent dirs created, BOM/CRLF preserved, exact-match-once
vs replace_all, the whitespace-tolerant fallback match, structured
not-found/near-match errors, and the mtime-since-Read staleness check.
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.tools.base import ToolContext
from rolo_claude.tools.edit import EditTool
from rolo_claude.tools.read import ReadTool
from rolo_claude.tools.write import WriteTool

test, TESTS = new_registry()


def _tmpdir(prefix):
    return Path(tempfile.mkdtemp(prefix=prefix))


# ---- Write ------------------------------------------------------------

@test
def test_write_new_file_no_read_required(ctx: Ctx):
    d = _tmpdir("write-new-")
    f = d / "new.txt"
    ctx_obj = ToolContext(cwd=d)
    result = WriteTool().run({"file_path": str(f), "content": "hello\n"}, ctx_obj)
    ctx.check("no error creating a brand new file", result.is_error is False)
    ctx.check("content written", f.read_text(encoding="utf-8") == "hello\n")


@test
def test_write_existing_file_requires_read_first(ctx: Ctx):
    d = _tmpdir("write-mustread-")
    f = d / "existing.txt"
    f.write_text("original\n", encoding="utf-8")
    ctx_obj = ToolContext(cwd=d)
    result = WriteTool().run({"file_path": str(f), "content": "new\n"}, ctx_obj)
    ctx.check("write without a prior Read is an error", result.is_error is True)
    ctx.check("error explains the requirement", "read" in result.content.lower())
    ctx.check("file unchanged", f.read_text(encoding="utf-8") == "original\n")


@test
def test_write_after_read_succeeds(ctx: Ctx):
    d = _tmpdir("write-afterread-")
    f = d / "existing.txt"
    f.write_text("original\n", encoding="utf-8")
    ctx_obj = ToolContext(cwd=d)
    ReadTool().run({"file_path": str(f)}, ctx_obj)
    result = WriteTool().run({"file_path": str(f), "content": "replaced\n"}, ctx_obj)
    ctx.check("write after Read succeeds", result.is_error is False)
    ctx.check("content replaced", f.read_text(encoding="utf-8") == "replaced\n")


@test
def test_write_detects_stale_mtime_since_read(ctx: Ctx):
    d = _tmpdir("write-stale-")
    f = d / "existing.txt"
    f.write_text("original\n", encoding="utf-8")
    ctx_obj = ToolContext(cwd=d)
    ReadTool().run({"file_path": str(f)}, ctx_obj)
    time.sleep(0.05)
    f.write_text("changed by someone else\n", encoding="utf-8")
    os_stat_time = f.stat().st_mtime
    ctx_obj.read_cache[str(f)] = os_stat_time - 5  # force staleness deterministically
    result = WriteTool().run({"file_path": str(f), "content": "clobber\n"}, ctx_obj)
    ctx.check("stale write is rejected", result.is_error is True)
    ctx.check("error mentions modification", "modified" in result.content.lower())


@test
def test_write_preserves_crlf_line_endings(ctx: Ctx):
    d = _tmpdir("write-crlf-")
    f = d / "crlf.txt"
    f.write_bytes(b"line one\r\nline two\r\n")
    ctx_obj = ToolContext(cwd=d)
    ReadTool().run({"file_path": str(f)}, ctx_obj)
    WriteTool().run({"file_path": str(f), "content": "a\nb\nc\n"}, ctx_obj)
    raw = f.read_bytes()
    ctx.check(f"CRLF preserved on overwrite, got {raw!r}", raw == b"a\r\nb\r\nc\r\n")


@test
def test_write_preserves_utf8_bom(ctx: Ctx):
    d = _tmpdir("write-bom-")
    f = d / "bom.txt"
    f.write_bytes(b"\xef\xbb\xbf" + "hello\n".encode("utf-8"))
    ctx_obj = ToolContext(cwd=d)
    ReadTool().run({"file_path": str(f)}, ctx_obj)
    WriteTool().run({"file_path": str(f), "content": "goodbye\n"}, ctx_obj)
    raw = f.read_bytes()
    ctx.check("BOM preserved on overwrite", raw.startswith(b"\xef\xbb\xbf"))
    ctx.check("new content present after the BOM", raw[3:] == b"goodbye\n")


@test
def test_write_preserves_unicode_content(ctx: Ctx):
    d = _tmpdir("write-unicode-")
    f = d / "unicode.txt"
    content = "café 日本語 \U0001F600\n"
    ctx_obj = ToolContext(cwd=d)
    WriteTool().run({"file_path": str(f), "content": content}, ctx_obj)
    ctx.check("unicode content round-trips exactly", f.read_text(encoding="utf-8") == content)


@test
def test_write_creates_parent_directories(ctx: Ctx):
    d = _tmpdir("write-parents-")
    f = d / "a" / "b" / "c" / "deep.txt"
    ctx_obj = ToolContext(cwd=d)
    result = WriteTool().run({"file_path": str(f), "content": "x\n"}, ctx_obj)
    ctx.check("no error creating nested parents", result.is_error is False)
    ctx.check("file exists at the deep path", f.exists())


@test
def test_write_rejects_relative_path(ctx: Ctx):
    result = WriteTool().run({"file_path": "relative.txt", "content": "x"}, ToolContext(cwd=Path(".")))
    ctx.check("relative path rejected", result.is_error is True)


# ---- Edit ---------------------------------------------------------------

@test
def test_edit_requires_read_first(ctx: Ctx):
    d = _tmpdir("edit-mustread-")
    f = d / "e.txt"
    f.write_text("alpha beta gamma\n", encoding="utf-8")
    ctx_obj = ToolContext(cwd=d)
    result = EditTool().run({"file_path": str(f), "old_string": "beta", "new_string": "delta"}, ctx_obj)
    ctx.check("edit without a prior Read is an error", result.is_error is True)


@test
def test_edit_exact_match_once(ctx: Ctx):
    d = _tmpdir("edit-exact-")
    f = d / "e.txt"
    f.write_text("alpha beta gamma\n", encoding="utf-8")
    ctx_obj = ToolContext(cwd=d)
    ReadTool().run({"file_path": str(f)}, ctx_obj)
    result = EditTool().run({"file_path": str(f), "old_string": "beta", "new_string": "delta"}, ctx_obj)
    ctx.check("edit succeeds", result.is_error is False)
    ctx.check("file updated", f.read_text(encoding="utf-8") == "alpha delta gamma\n")


@test
def test_edit_ambiguous_match_without_replace_all_errors(ctx: Ctx):
    d = _tmpdir("edit-ambiguous-")
    f = d / "e.txt"
    f.write_text("x = 1\nx = 1\nx = 1\n", encoding="utf-8")
    ctx_obj = ToolContext(cwd=d)
    ReadTool().run({"file_path": str(f)}, ctx_obj)
    result = EditTool().run({"file_path": str(f), "old_string": "x = 1", "new_string": "x = 2"}, ctx_obj)
    ctx.check("ambiguous (3 matches) without replace_all is an error", result.is_error is True)
    ctx.check("error names the match count", "3" in result.content)
    ctx.check("file unchanged", f.read_text(encoding="utf-8") == "x = 1\nx = 1\nx = 1\n")


@test
def test_edit_replace_all(ctx: Ctx):
    d = _tmpdir("edit-replaceall-")
    f = d / "e.txt"
    f.write_text("foo foo foo\n", encoding="utf-8")
    ctx_obj = ToolContext(cwd=d)
    ReadTool().run({"file_path": str(f)}, ctx_obj)
    result = EditTool().run({"file_path": str(f), "old_string": "foo", "new_string": "bar", "replace_all": True}, ctx_obj)
    ctx.check("replace_all succeeds", result.is_error is False)
    ctx.check("every occurrence replaced", f.read_text(encoding="utf-8") == "bar bar bar\n")


@test
def test_edit_not_found_reports_zero_near_matches(ctx: Ctx):
    d = _tmpdir("edit-notfound-")
    f = d / "e.txt"
    f.write_text("completely different content\n", encoding="utf-8")
    ctx_obj = ToolContext(cwd=d)
    ReadTool().run({"file_path": str(f)}, ctx_obj)
    result = EditTool().run({"file_path": str(f), "old_string": "nonexistent text", "new_string": "x"}, ctx_obj)
    ctx.check("not-found is an error", result.is_error is True)
    ctx.check("structured 0-near-matches wording", "0 near-matches" in result.content)


@test
def test_edit_whitespace_tolerant_fallback_match(ctx: Ctx):
    """A model that reproduces the right LINES with different indentation
    than the file actually has must still succeed via the tolerant
    fallback (never a hard failure for a whitespace-only mismatch when the
    match is otherwise unique)."""
    d = _tmpdir("edit-tolerant-")
    f = d / "e.py"
    f.write_text("def f():\n    x = 1\n    return x\n", encoding="utf-8")
    ctx_obj = ToolContext(cwd=d)
    ReadTool().run({"file_path": str(f)}, ctx_obj)
    # old_string has NO indentation on "x = 1" (file has 4 spaces) -- exact
    # match fails, tolerant (line-stripped) match must still find it.
    result = EditTool().run(
        {"file_path": str(f), "old_string": "def f():\nx = 1\n    return x", "new_string": "def f():\n    x = 2\n    return x"},
        ctx_obj,
    )
    ctx.check(f"tolerant match succeeds, got: {result.content!r}", result.is_error is False)
    ctx.check("tolerant match noted in the result", "tolerant" in result.content.lower())
    ctx.check("file actually updated", "x = 2" in f.read_text(encoding="utf-8"))


@test
def test_edit_preserves_crlf_and_bom(ctx: Ctx):
    d = _tmpdir("edit-crlf-bom-")
    f = d / "e.txt"
    f.write_bytes(b"\xef\xbb\xbf" + b"alpha\r\nbeta\r\ngamma\r\n")
    ctx_obj = ToolContext(cwd=d)
    ReadTool().run({"file_path": str(f)}, ctx_obj)
    EditTool().run({"file_path": str(f), "old_string": "beta", "new_string": "delta"}, ctx_obj)
    raw = f.read_bytes()
    ctx.check("BOM preserved", raw.startswith(b"\xef\xbb\xbf"))
    ctx.check(f"CRLF preserved and content updated, got {raw!r}", raw == b"\xef\xbb\xbfalpha\r\ndelta\r\ngamma\r\n")


@test
def test_edit_missing_file_is_error(ctx: Ctx):
    d = _tmpdir("edit-missing-")
    result = EditTool().run({"file_path": str(d / "nope.txt"), "old_string": "a", "new_string": "b"}, ToolContext(cwd=d))
    ctx.check("missing file is an error", result.is_error is True)


@test
def test_edit_old_equals_new_is_error(ctx: Ctx):
    d = _tmpdir("edit-sameold-")
    f = d / "e.txt"
    f.write_text("a\n", encoding="utf-8")
    ctx_obj = ToolContext(cwd=d)
    ReadTool().run({"file_path": str(f)}, ctx_obj)
    result = EditTool().run({"file_path": str(f), "old_string": "a", "new_string": "a"}, ctx_obj)
    ctx.check("identical old/new is rejected", result.is_error is True)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
