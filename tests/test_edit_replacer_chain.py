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


# ---------------------------------------------------------------------------
# finding 2 (critical, h4-h5-h3c review): an ambiguous fuzzy stage must
# never silently narrow to candidates[0] (the FIRST occurrence in the
# file) -- verified regressions from the review, reproduced exactly.
# ---------------------------------------------------------------------------

@test
def test_h5b_f02_line_trimmed_two_near_duplicate_blocks_is_ambiguous(ctx: Ctx):
    """The review's own verified repro: two identical `x = 1` / `return x`
    blocks, an old_string indented by 2 instead of 4 spaces (so Simple/
    exact misses and LineTrimmed is the first stage that can match at
    all) -- LineTrimmed finds BOTH blocks and must refuse rather than
    silently edit the FIRST one and report success."""
    content = "def a():\n    x = 1\n    return x\n\ndef b():\n    x = 1\n    return x\n"
    old = "  x = 1\n  return x"  # 2-space indent, file uses 4
    matches = stage_line_trimmed(content, old)
    ctx.check(f"LineTrimmed finds both near-duplicate blocks, got {len(matches)}", len(matches) == 2)
    found, stage, err = find_replacement(content, old)
    ctx.check("ambiguous LineTrimmed hit is refused, not silently resolved", found is None)
    ctx.check(f"OpenCode-style multiple-matches wording, got {err!r}", "Found multiple matches" in err)


@test
def test_h5b_f02_edit_tool_refuses_ambiguous_near_duplicate_blocks(ctx: Ctx):
    """End-to-end: EditTool.run on the same two-block file must return an
    error (never silently edit the first block) -- this is the exact
    "wrong-location edit reported as success" failure mode the finding
    verified."""
    d = _tmpdir("edit-f02-ambiguous-")
    initial = "def a():\n    x = 1\n    return x\n\ndef b():\n    x = 1\n    return x\n"
    f, result = _read_then_edit(d, "dup.py", initial, "  x = 1\n  return x", "  x = 2\n  return x")
    ctx.check(f"the ambiguous fuzzy match is refused, got is_error={result.is_error!r} content={result.content!r}",
              result.is_error is True)
    ctx.check("wording names the ambiguity", "multiple matches" in result.content.lower())
    ctx.check("the file was NOT modified", f.read_text(encoding="utf-8") == initial)


@test
def test_h5b_f02_trimmed_boundary_needle_present_twice_is_ambiguous(ctx: Ctx):
    """The review's second verified repro: a TrimmedBoundary needle
    ("\\nfoo\\n"-shaped -- whitespace/newlines around the boundary trimmed
    before an exact substring search) that occurs at more than one
    position in the file must refuse rather than editing the first `foo`
    anywhere in the file."""
    content = "alpha\nfoo\nbeta\nfoo\ngamma\n"
    old = "  \nfoo\n  "  # extra whitespace/newlines around the boundary, per stage 7's own docstring
    matches = stage_trimmed_boundary(content, old)
    ctx.check(f"TrimmedBoundary finds both occurrences of the trimmed needle, got {len(matches)}", len(matches) == 2)
    found, stage, err = find_replacement(content, old)
    ctx.check("ambiguous TrimmedBoundary hit is refused, not silently resolved to the first occurrence",
              found is None)
    ctx.check(f"OpenCode-style multiple-matches wording, got {err!r}", "Found multiple matches" in err)


@test
def test_h5b_f02_ambiguous_stage_is_skipped_in_favor_of_a_later_unique_one(ctx: Ctx):
    """An earlier stage being ambiguous must not stop the chain outright --
    `find_replacement` moves PAST it and keeps trying later stages, so a
    case that is ambiguous under LineTrimmed but resolves uniquely under a
    later, stricter-anchored stage still succeeds."""
    # LineTrimmed/BlockAnchor/WhitespaceNormalized all see two candidates
    # here (both blocks' lines are identical once trimmed/collapsed), but
    # IndentationFlexible compares each window against its OWN common
    # indent without stripping anything from a zero-indent anchor line
    # (`if a:`), so only the block whose second line ALSO keeps old_string's
    # exact 4-space indent (never the 1-space block) matches -- unique.
    content = "if a:\n pass\nif a:\n    pass\n"
    old = "if a:\n    pass"
    ws_matches = stage_whitespace_normalized(content, old)
    ctx.check(f"WhitespaceNormalized alone is ambiguous here, got {len(ws_matches)}", len(ws_matches) == 2)
    found, stage, err = find_replacement(content, old)
    ctx.check(f"a later unique stage still resolves the edit, got stage={stage!r} err={err!r}",
              found is not None and err == "")


@test
def test_h5b_f02_replace_all_accepts_every_fuzzy_candidate(ctx: Ctx):
    """`replace_all=True` is the one case where a multi-candidate fuzzy
    stage is accepted outright (every candidate replaced), per the
    finding's own fix text ("or all of them with replace_all")."""
    content = "def a():\n    x = 1\n    return x\n\ndef b():\n    x = 1\n    return x\n"
    old = "  x = 1\n  return x"
    matches, stage, err = find_replacement(content, old, replace_all=True)
    ctx.check(f"replace_all accepts both LineTrimmed candidates, got {matches!r} err={err!r}",
              matches is not None and len(matches) == 2 and err == "")


@test
def test_h5b_f02_edit_tool_replace_all_rewrites_every_fuzzy_occurrence(ctx: Ctx):
    d = _tmpdir("edit-f02-replaceall-")
    initial = "def a():\n    x = 1\n    return x\n\ndef b():\n    x = 1\n    return x\n"
    f, result = _read_then_edit(d, "dup2.py", initial, "  x = 1\n  return x", "  x = 2\n  return x",
                                 replace_all=True)
    ctx.check(f"replace_all succeeds across both ambiguous blocks, got {result.content!r}", result.is_error is False)
    updated = f.read_text(encoding="utf-8")
    ctx.check("both occurrences updated", updated.count("x = 2") == 2)
    ctx.check("reports 2 replacements", "2 replacement(s)" in result.content)


@test
def test_h5c_f17_overlapping_fuzzy_candidates_are_never_all_accepted(ctx: Ctx):
    """H5c finding 17: `IndentationFlexible` against a repeated single line
    produces OVERLAPPING sliding-window candidates (a 2-line pattern over
    3 identical lines matches at line-index 0 AND line-index 1 -- the
    second window starts INSIDE the first). Accepting both under
    `replace_all` corrupted the file (`EditTool.run`'s own splice loop
    silently drops the real content between two overlapping matches) --
    verified: `find_replacement` returned 2 "matches" whose 2nd start was
    BEFORE the 1st's end."""
    content = "a = 1\na = 1\na = 1\n"
    old = "  a = 1\n  a = 1"  # fake leading indent -- forces the IndentationFlexible stage
    matches, stage, err = find_replacement(content, old, replace_all=True)
    ctx.check(f"find_replacement returned some matches, got {matches!r} err={err!r}", matches)
    ctx.check(f"the overlapping candidate was dropped -- exactly ONE non-overlapping match survives, "
              f"got {[(m.start, m.end) for m in matches]}", len(matches) == 1)


@test
def test_h5c_f17_edit_tool_replace_all_never_corrupts_the_file_on_overlap(ctx: Ctx):
    """H5c finding 17, end to end through the real tool: the review's own
    verified repro -- `"a = 1\\na = 1\\na = 1\\n"` with old `"  a = 1\\n  a
    = 1"` and `replace_all` used to become `"BB\\n"` (silently dropping
    the third line and mis-reporting "2 replacement(s)"). Must now either
    make exactly ONE (non-overlapping) replacement -- with NO file content
    lost -- or refuse outright; corruption is the only unacceptable
    outcome."""
    d = _tmpdir("edit-f17-overlap-")
    initial = "a = 1\na = 1\na = 1\n"
    f, result = _read_then_edit(d, "overlap.py", initial, "  a = 1\n  a = 1", "B", replace_all=True)
    if result.is_error:
        # Refusing outright is an acceptable outcome too -- the file must
        # be untouched in that case.
        ctx.check("refused -> file untouched", f.read_text(encoding="utf-8") == initial)
        return
    updated = f.read_text(encoding="utf-8")
    ctx.check(f"exactly one replacement was reported, got {result.content!r}", "1 replacement(s)" in result.content)
    # The old, corrupted behaviour dropped the trailing "a = 1" line
    # entirely (result was "BB\n", 3 bytes, from an 18-byte file) -- the
    # untouched third line (or whatever legitimately remains outside the
    # ONE replaced window) must still be present.
    ctx.check(f"no content was silently dropped -- the untouched trailing line survives, got {updated!r}",
              "a = 1" in updated)
    ctx.check(f"the replacement text is present, got {updated!r}", "B" in updated)
    ctx.check(f"nothing resembling the old corrupted 'BB' double-collapse happened, got {updated!r}",
              updated != "BB\n")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
