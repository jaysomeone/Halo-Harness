"""tests.test_edit_replacer_chain -- rolo_claude/tools/edit.py's 9-stage
replacer chain (H5 scope F item 1, reports/OpenCode harness deep review.md
Appendix C), stage-by-stage: each stage function directly, the span guard,
and end-to-end EditTool runs that exercise the stages naturally reachable
through the full chain (Simple, LineTrimmed, BlockAnchor, ContextAware,
plus the "not found"/"multiple matches" error strings)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.tools.base import ToolContext
from rolo_claude.tools.edit import (
    BLOCK_ANCHOR_SIMILARITY, CONTEXT_AWARE_MATCH_FRACTION, EditTool, _check_span_guard, _common_indent,
    _levenshtein, _similarity, _unescape, find_replacement, stage_block_anchor, stage_context_aware,
    stage_escape_normalized, stage_indentation_flexible, stage_line_trimmed, stage_trimmed_boundary,
    stage_whitespace_normalized,
)
from rolo_claude.tools.read import ReadTool

test, TESTS = new_registry()


def _tmpdir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def _read_then_edit(d: Path, name: str, initial: str, old: str, new: str, **kw):
    f = d / name
    f.write_text(initial, encoding="utf-8")
    ctx_obj = ToolContext(cwd=d)
    ReadTool().run({"file_path": str(f)}, ctx_obj)
    result = EditTool().run({"file_path": str(f), "old_string": old, "new_string": new, **kw}, ctx_obj)
    return f, result


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

@test
def test_levenshtein_basic(ctx: Ctx):
    ctx.check("identical strings -> 0", _levenshtein("abc", "abc") == 0)
    ctx.check("one substitution -> 1", _levenshtein("abc", "abd") == 1)
    ctx.check("empty vs N chars -> N", _levenshtein("", "abcd") == 4)


@test
def test_similarity_bounds(ctx: Ctx):
    ctx.check("identical -> 1.0", _similarity("hello", "hello") == 1.0)
    ctx.check("two empty strings -> 1.0 (trivially identical)", _similarity("", "") == 1.0)
    ctx.check("wildly different -> low similarity",
              _similarity("do the thing carefully please", "do a different thing entirely") < 0.3)


@test
def test_common_indent(ctx: Ctx):
    ctx.check("shared 4-space indent", _common_indent(["    a", "    b"]) == "    ")
    ctx.check("blank lines ignored when computing the common prefix",
              _common_indent(["    a", "", "    b"]) == "    ")
    ctx.check("no shared indent -> empty", _common_indent(["a", "  b"]) == "")


@test
def test_unescape(ctx: Ctx):
    ctx.check(r"\n -> real newline", _unescape("line1\\nline2") == "line1\nline2")
    ctx.check(r"\t -> real tab", _unescape("a\\tb") == "a\tb")


# ---------------------------------------------------------------------------
# Stage 2: LineTrimmed
# ---------------------------------------------------------------------------

@test
def test_stage_line_trimmed_direct(ctx: Ctx):
    content = "def foo():\n    return 1\n"
    old = "  def foo():\n      return 1"  # same content, different per-line indent
    matches = stage_line_trimmed(content, old)
    ctx.check(f"LineTrimmed finds the window despite differing indentation, got {len(matches)}", len(matches) == 1)


@test
def test_edit_via_line_trimmed(ctx: Ctx):
    d = _tmpdir("edit-linetrimmed-")
    f, result = _read_then_edit(d, "a.py", "def foo():\n    return 1\n",
                                 "  def foo():\n      return 1", "def foo():\n    return 2")
    ctx.check(f"edit succeeds, got {result.content}", result.is_error is False)
    ctx.check("used LineTrimmed", "LineTrimmed" in result.content)
    ctx.check("file updated", "return 2" in f.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Stage 3: BlockAnchor
# ---------------------------------------------------------------------------

@test
def test_stage_block_anchor_accepts_above_threshold(ctx: Ctx):
    content = 'function start() {\n    console.log("processing item now");\n    return true;\n}\n'
    old = 'function start() {\n    console.log("processing the item");\n    return true;\n}'
    matches = stage_block_anchor(content, old)
    ctx.check(f"BlockAnchor accepts a >= {BLOCK_ANCHOR_SIMILARITY} similarity middle line, got {len(matches)}",
              len(matches) == 1)


@test
def test_stage_block_anchor_rejects_below_threshold(ctx: Ctx):
    content = "START\ndo the thing carefully please\nEND\n"
    old = "START\ndo a different thing entirely\nEND"
    matches = stage_block_anchor(content, old)
    ctx.check("BlockAnchor rejects a middle line far below the similarity threshold", matches == [])


@test
def test_stage_block_anchor_requires_at_least_two_lines(ctx: Ctx):
    ctx.check("a single-line oldString has no anchors -> no candidates",
              stage_block_anchor("one line of content\n", "one line") == [])


@test
def test_edit_via_block_anchor(ctx: Ctx):
    d = _tmpdir("edit-blockanchor-")
    initial = 'function start() {\n    console.log("processing item now");\n    return true;\n}\n'
    old = 'function start() {\n    console.log("processing the item");\n    return true;\n}'
    new = 'function start() {\n    console.log("done");\n    return true;\n}'
    f, result = _read_then_edit(d, "b.js", initial, old, new)
    ctx.check(f"edit succeeds via BlockAnchor, got {result.content}", result.is_error is False and "BlockAnchor" in result.content)
    ctx.check("file updated", "done" in f.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Stage 4: WhitespaceNormalized
# ---------------------------------------------------------------------------

@test
def test_stage_whitespace_normalized_direct(ctx: Ctx):
    content = "value = compute(a,    b,     c)\n"
    old = "value = compute(a, b, c)"  # single spaces where the file has runs of spaces
    matches = stage_whitespace_normalized(content, old)
    ctx.check(f"WhitespaceNormalized collapses whitespace runs before comparing, got {len(matches)}", len(matches) == 1)


# ---------------------------------------------------------------------------
# Stage 5: IndentationFlexible
# ---------------------------------------------------------------------------

@test
def test_stage_indentation_flexible_direct(ctx: Ctx):
    content = "if outer:\n    if inner:\n        do_work()\n"
    old = "if outer:\n    if inner:\n        do_work()"
    matches = stage_indentation_flexible(content, old)
    ctx.check(f"IndentationFlexible matches identical relative structure, got {len(matches)}", len(matches) == 1)


@test
def test_stage_indentation_flexible_preserves_relative_structure_requirement(ctx: Ctx):
    # Same TEXT per line but the relative indentation between line 2 and 3
    # is reversed -- common-indent stripping must NOT treat this as equal.
    content = "a:\n    b:\n        c\n"
    old = "a:\n        b:\n    c"
    matches = stage_indentation_flexible(content, old)
    ctx.check("a genuinely different relative-indentation structure is rejected", matches == [])


# ---------------------------------------------------------------------------
# Stage 6: EscapeNormalized
# ---------------------------------------------------------------------------

@test
def test_stage_escape_normalized_direct(ctx: Ctx):
    content = "line one\nline two\nline three\n"
    old = "line one\\nline two"  # literal backslash-n instead of a real newline
    matches = stage_escape_normalized(content, old)
    ctx.check(f"EscapeNormalized unescapes \\n before searching, got {len(matches)}", len(matches) == 1)


# ---------------------------------------------------------------------------
# Stage 7: TrimmedBoundary
# ---------------------------------------------------------------------------

@test
def test_stage_trimmed_boundary_direct(ctx: Ctx):
    content = "exact content here\n"
    old = "  \nexact content here\n  "  # extra whitespace/newlines around the boundary
    matches = stage_trimmed_boundary(content, old)
    ctx.check(f"TrimmedBoundary trims the whole oldString before searching, got {len(matches)}", len(matches) == 1)


# ---------------------------------------------------------------------------
# Stage 8: ContextAware
# ---------------------------------------------------------------------------

@test
def test_stage_context_aware_accepts_half_matching_middle(ctx: Ctx):
    old_middle = ["same_line_1", "same_line_2", "COMPLETELY_DIFFERENT_ALPHA_XX", "COMPLETELY_DIFFERENT_BETA_WW"]
    file_middle = ["same_line_1", "same_line_2", "totally different alpha content zz", "totally different beta content yy"]
    content = "BEGIN_MARKER\n" + "\n".join(file_middle) + "\nEND_MARKER\n"
    old = "BEGIN_MARKER\n" + "\n".join(old_middle) + "\nEND_MARKER"
    # This scenario is specifically calibrated (see the module's sibling
    # research) so BlockAnchor's average-similarity bar (0.65) rejects it
    # (avg lands at 0.5) while ContextAware's >= 50% exact-middle-line bar
    # accepts it -- proving ContextAware is a genuinely LOOSER stage 8, not
    # a duplicate of stage 3.
    ctx.check("BlockAnchor rejects this exact scenario (calibration check)", stage_block_anchor(content, old) == [])
    matches = stage_context_aware(content, old)
    ctx.check(f"ContextAware accepts >= {CONTEXT_AWARE_MATCH_FRACTION} matching middle lines, got {len(matches)}",
              len(matches) == 1)


@test
def test_stage_context_aware_rejects_below_half(ctx: Ctx):
    old_middle = ["A_DIFFERENT", "B_DIFFERENT", "C_DIFFERENT", "same_line"]
    file_middle = ["totally-other-1", "totally-other-2", "totally-other-3", "same_line"]
    content = "BEGIN\n" + "\n".join(file_middle) + "\nEND\n"
    old = "BEGIN\n" + "\n".join(old_middle) + "\nEND"
    ctx.check("only 1/4 middle lines match -> below the 50% bar -> rejected", stage_context_aware(content, old) == [])


# ---------------------------------------------------------------------------
# Span guard
# ---------------------------------------------------------------------------

@test
def test_span_guard_rejects_a_much_larger_match(ctx: Ctx):
    old = "short"  # char_limit = max(5+500, 5*4) = 505
    huge = "x" * 600  # comfortably past the +500 floor for a short oldString
    err = _check_span_guard(old, huge)
    ctx.check(f"span guard fires on an oversized char match, got {err!r}", err is not None)
    ctx.check("uses OpenCode's own wording", err is not None and "Refusing replacement because the matched span" in err)


@test
def test_span_guard_allows_a_reasonably_sized_match(ctx: Ctx):
    old = "some text here"
    same_size = "some other text"
    ctx.check("a same-order-of-magnitude match passes the guard", _check_span_guard(old, same_size) is None)


@test
def test_span_guard_line_count_threshold(ctx: Ctx):
    old = "a\nb"  # 2 lines -> line_limit = max(2+3, 2*2) = 5
    four_lines = "w\nx\ny\nz"  # 4 lines, under the limit
    six_lines = "w\nx\ny\nz\nq\nr"  # 6 lines, at/over the limit
    ctx.check("4 lines (under max(5,4)=5) passes", _check_span_guard(old, four_lines) is None)
    ctx.check("6 lines (over the 5-line limit) fails", _check_span_guard(old, six_lines) is not None)


# ---------------------------------------------------------------------------
# End-to-end error strings (Appendix C, verbatim/near-verbatim where given)
# ---------------------------------------------------------------------------

@test
def test_edit_not_found_error_wording(ctx: Ctx):
    d = _tmpdir("edit-notfound2-")
    _, result = _read_then_edit(d, "x.txt", "alpha beta gamma\n", "zzz not present anywhere zzz", "y")
    ctx.check("is an error", result.is_error is True)
    ctx.check(f"OpenCode wording, got {result.content!r}", "Could not find oldString in the file" in result.content)


@test
def test_edit_multiple_matches_error_wording(ctx: Ctx):
    d = _tmpdir("edit-multi2-")
    _, result = _read_then_edit(d, "y.txt", "dup\ndup\ndup\n", "dup", "single")
    ctx.check("is an error", result.is_error is True)
    ctx.check(f"OpenCode wording, got {result.content!r}", "Found multiple matches for oldString" in result.content)


@test
def test_edit_identical_strings_error_wording(ctx: Ctx):
    d = _tmpdir("edit-identical2-")
    f = d / "z.txt"
    f.write_text("same\n", encoding="utf-8")
    ctx_obj = ToolContext(cwd=d)
    ReadTool().run({"file_path": str(f)}, ctx_obj)
    result = EditTool().run({"file_path": str(f), "old_string": "same text", "new_string": "same text"}, ctx_obj)
    ctx.check(f"exact OpenCode wording, got {result.content!r}",
              result.content == "No changes to apply: oldString and newString are identical.")


@test
def test_find_replacement_returns_none_on_total_miss(ctx: Ctx):
    matches, stage, err = find_replacement("nothing relevant here\n", "totally absent string")
    ctx.check("no stage matches -> None", matches is None)
    ctx.check("no stage name reported", stage is None)
    ctx.check("no guard error either (there was nothing to guard)", err == "")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
