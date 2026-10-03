"""tests.test_clipboard -- halo_harness/tui/clipboard.py (2.0.1 W4c:
"clipboard quality and Ctrl+C reassurance"). Pure, stdlib-only, no Textual
import needed (the module's own docstring promise) -- the text-cleanup
pipeline (glyph/border stripping, trailing-space stripping, soft-wrap
non-mangling, fenced-code extraction, multi-part joining) and the
platform/trust dispatch (`osc52_trusted`, `copy_via_external_tool`,
`active_clipboard_backend_name`, `clipboard_doctor_line`), each exercised
through the `env=`/`platform=` test seams so every branch is reachable
regardless of which real OS runs this suite. The pilot-level checks (per-
widget `copy_text()`, multi-widget selection, `/copy`, `y`/`Y`, the toast
wording, the config switch end-to-end) live in test_tui.py instead, since
those need a running BridgeApp.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


class _Env:
    """Scopes `~/.halo` to a fresh scratch dir for the one test here that
    reads/writes `config.json` (`clipboard.crlf`) -- `bridge_home()` reads
    `BRIDGE_STATE_DIR` directly when set, never the real machine home."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR")}
        d = Path(tempfile.mkdtemp(prefix="halo-clipboard-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# Glyph/border stripping, trailing-space stripping, "soft wrap" non-mangling.
# ---------------------------------------------------------------------------

@test
def test_strip_chrome_glyphs_removes_the_exact_set_and_the_leading_space_after_it(ctx: Ctx):
    from halo_harness.tui.clipboard import strip_chrome_glyphs

    bullet, arrow, spinner = "⏺", "❯", "✻"
    vbar, hbar, tl, bl = "│", "─", "╭", "╰"
    ctx.check("bullet prefix stripped with its space", strip_chrome_glyphs(f"{bullet} Bash(ls)") == "Bash(ls)")
    ctx.check("prompt arrow prefix stripped with its space", strip_chrome_glyphs(f"{arrow} hello") == "hello")
    ctx.check("spinner prefix stripped with its space", strip_chrome_glyphs(f"{spinner} Thinking...") == "Thinking...")
    box = f"{tl}-----\n{vbar} hi\n{bl}-----"
    ctx.check(f"box-drawing border characters removed, got {strip_chrome_glyphs(box)!r}",
              strip_chrome_glyphs(box) == "-----\nhi\n-----")
    ctx.check(f"a mid-line stray glyph is deleted in place, got {strip_chrome_glyphs('a'+hbar+'b')!r}",
              strip_chrome_glyphs("a" + hbar + "b") == "ab")


@test
def test_strip_chrome_glyphs_never_touches_unrelated_characters_or_indentation(ctx: Ctx):
    from halo_harness.tui.clipboard import strip_chrome_glyphs

    ctx.check("ordinary content is untouched", strip_chrome_glyphs("def f():\n    return 1") == "def f():\n    return 1")
    decision = "✓ allowed once Bash(pytest -q)"  # a ✓ decision glyph -- real content, never stripped
    ctx.check(f"a ✓/✗/⚠-style decision glyph survives, got {strip_chrome_glyphs(decision)!r}",
              strip_chrome_glyphs(decision) == decision)


@test
def test_clean_copy_text_strips_trailing_spaces_normalizes_newlines_and_outer_blank_lines(ctx: Ctx):
    from halo_harness.tui.clipboard import clean_copy_text

    ctx.check("trailing spaces stripped per line", clean_copy_text("a   \nb\t\n") == "a\nb")
    ctx.check("CRLF normalized to LF", clean_copy_text("a\r\nb\r\n") == "a\nb")
    ctx.check("outer blank lines trimmed", clean_copy_text("\n\nhello\n\n\n") == "hello")


@test
def test_clean_copy_text_never_splits_a_long_line_soft_wrap_joining(ctx: Ctx):
    """W4c item 1 ("soft wraps joined"): a terminal only ever wraps a long
    line VISUALLY, at render time -- it never inserts a real "\\n" into the
    widget's own stored string. Reading that stored string (what every
    widget's `copy_text()` does) means a long line was never split to begin
    with; this is the regression that would catch a future change that
    somehow reintroduced a screen-cell read."""
    from halo_harness.tui.clipboard import clean_copy_text

    long_line = "word " * 60  # ~300 chars -- wraps across several rows at any normal terminal width
    ctx.check("a long single line stays one line", clean_copy_text(long_line).count("\n") == 0)
    ctx.check("its content is unchanged (just trailing space trimmed)",
              clean_copy_text(long_line) == long_line.rstrip())


@test
def test_clean_code_text_keeps_box_drawing_characters_exact_contents(ctx: Ctx):
    """W4c item 1: "a code block's EXACT contents" -- `clean_code_text`
    (unlike `clean_copy_text`) must never strip `─`/`│`/etc., since real
    code/tool output legitimately draws with them (ASCII art, `tree`-style
    listings, box tables)."""
    from halo_harness.tui.clipboard import clean_code_text

    code = "┌──┐\ndef f():\n    return 1   \n"
    cleaned = clean_code_text(code)
    ctx.check(f"box-drawing characters survive in code, got {cleaned!r}", "┌" in cleaned and "─" in cleaned)
    ctx.check("trailing whitespace on a code line is still trimmed", "return 1   " not in cleaned)
    ctx.check("line count/content otherwise exact", cleaned.splitlines()[1] == "def f():")


# ---------------------------------------------------------------------------
# /copy code: fenced-block extraction, multi-part joining.
# ---------------------------------------------------------------------------

@test
def test_extract_fenced_code_blocks_returns_each_blocks_content_in_order(ctx: Ctx):
    from halo_harness.tui.clipboard import extract_fenced_code_blocks

    md = "intro\n```python\nprint(1)\n```\nmiddle\n```\nbare fence\nline2\n```\nend"
    blocks = extract_fenced_code_blocks(md)
    ctx.check(f"two blocks found in order, got {blocks}", blocks == ["print(1)", "bare fence\nline2"])


@test
def test_extract_fenced_code_blocks_handles_an_unterminated_trailing_fence(ctx: Ctx):
    """A reply still streaming when `/copy code` is used -- the opening
    fence has no closing ``` yet; the content up to the end of the text is
    still returned rather than silently dropped."""
    from halo_harness.tui.clipboard import extract_fenced_code_blocks

    md = "before\n```python\nstill streaming"
    blocks = extract_fenced_code_blocks(md)
    ctx.check(f"the unterminated block's content is still returned, got {blocks}", blocks == ["still streaming"])


@test
def test_join_copied_texts_joins_with_a_blank_line_and_drops_empty_parts(ctx: Ctx):
    from halo_harness.tui.clipboard import join_copied_texts

    ctx.check("non-empty parts joined with one blank line between",
              join_copied_texts(["first", "second"]) == "first\n\nsecond")
    ctx.check("empty contributions are dropped, not left as a stray blank paragraph",
              join_copied_texts(["a", "", "b"]) == "a\n\nb")
    ctx.check("a single part is returned unchanged", join_copied_texts(["only"]) == "only")
    ctx.check("all-empty returns an empty string", join_copied_texts(["", ""]) == "")


# ---------------------------------------------------------------------------
# clipboard.crlf config switch.
# ---------------------------------------------------------------------------

@test
def test_apply_crlf_if_configured_default_false_and_true_switch(ctx: Ctx):
    from halo_harness.tui.clipboard import apply_crlf_if_configured
    from halo_harness import theme as theme_mod

    with _Env():
        ctx.check("default (unset) keeps plain LF", apply_crlf_if_configured("a\nb") == "a\nb")
        theme_mod.set_config_value("clipboard.crlf", True)
        ctx.check("clipboard.crlf: true converts to CRLF", apply_crlf_if_configured("a\nb") == "a\r\nb")
        theme_mod.set_config_value("clipboard.crlf", False)
        ctx.check("clipboard.crlf: false restores plain LF", apply_crlf_if_configured("a\nb") == "a\nb")


# ---------------------------------------------------------------------------
# W4c item 3: OSC 52 trust + the external-tool fallback, both via the
# `env=`/`platform=` test seams so every branch runs on any host OS.
# ---------------------------------------------------------------------------

@test
def test_osc52_trusted_windows_terminal_vs_conhost_via_the_seam(ctx: Ctx):
    from halo_harness.tui.clipboard import osc52_trusted

    ctx.check("Windows Terminal (WT_SESSION set) is trusted",
              osc52_trusted({"WT_SESSION": "abc"}, platform="win32") is True)
    ctx.check("plain conhost (no WT_SESSION) is NOT trusted",
              osc52_trusted({}, platform="win32") is False)


@test
def test_osc52_trusted_always_true_off_windows(ctx: Ctx):
    from halo_harness.tui.clipboard import osc52_trusted

    ctx.check("linux is always trusted", osc52_trusted({}, platform="linux") is True)
    ctx.check("darwin is always trusted", osc52_trusted({}, platform="darwin") is True)


@test
def test_copy_via_external_tool_windows_seam_calls_clip_exe_with_utf16(ctx: Ctx):
    import halo_harness.tui.clipboard as clipboard_mod

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        class _Result:
            returncode = 0
        return _Result()

    old_run = clipboard_mod.subprocess.run
    clipboard_mod.subprocess.run = _fake_run
    try:
        ok = clipboard_mod.copy_via_external_tool("hello", platform="win32")
        ctx.check("reports success", ok is True)
        ctx.check(f"clip.exe is the argv, got {calls}", calls and calls[0][0] == ["clip.exe"])
        payload = calls[0][1].get("input")
        ctx.check("the payload is UTF-16LE with a leading BOM (never plain text=True)",
                  isinstance(payload, bytes) and payload == ("﻿" + "hello").encode("utf-16-le"))
    finally:
        clipboard_mod.subprocess.run = old_run


@test
def test_copy_via_external_tool_darwin_seam_calls_pbcopy(ctx: Ctx):
    import halo_harness.tui.clipboard as clipboard_mod

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        class _Result:
            returncode = 0
        return _Result()

    old_run = clipboard_mod.subprocess.run
    clipboard_mod.subprocess.run = _fake_run
    try:
        ok = clipboard_mod.copy_via_external_tool("hello", platform="darwin")
        ctx.check("reports success", ok is True)
        ctx.check(f"pbcopy is the argv, got {calls}", calls == [["pbcopy"]])
    finally:
        clipboard_mod.subprocess.run = old_run


@test
def test_copy_via_external_tool_linux_seam_uses_find_clipboard_tool(ctx: Ctx):
    import halo_harness.tui.clipboard as clipboard_mod

    old_find = clipboard_mod.find_clipboard_tool
    clipboard_mod.find_clipboard_tool = lambda: ("xclip", ["xclip", "-selection", "clipboard"])
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        class _Result:
            returncode = 0
        return _Result()

    old_run = clipboard_mod.subprocess.run
    clipboard_mod.subprocess.run = _fake_run
    try:
        ok = clipboard_mod.copy_via_external_tool("hello", platform="linux")
        ctx.check("reports success", ok is True)
        ctx.check(f"the found tool's argv is used, got {calls}", calls == [["xclip", "-selection", "clipboard"]])
    finally:
        clipboard_mod.subprocess.run = old_run
        clipboard_mod.find_clipboard_tool = old_find


@test
def test_copy_via_external_tool_linux_seam_no_tool_found_returns_false(ctx: Ctx):
    import halo_harness.tui.clipboard as clipboard_mod

    old_find = clipboard_mod.find_clipboard_tool
    clipboard_mod.find_clipboard_tool = lambda: None
    try:
        ok = clipboard_mod.copy_via_external_tool("hello", platform="linux")
        ctx.check("no tool on PATH -> False, never raises", ok is False)
    finally:
        clipboard_mod.find_clipboard_tool = old_find


# ---------------------------------------------------------------------------
# The doctor line: names the active backend truthfully (W4c item 3).
# ---------------------------------------------------------------------------

@test
def test_active_clipboard_backend_name_windows_trusted_vs_untrusted_wording(ctx: Ctx):
    from halo_harness.tui.clipboard import active_clipboard_backend_name

    trusted = active_clipboard_backend_name({"WT_SESSION": "1"}, platform="win32")
    untrusted = active_clipboard_backend_name({}, platform="win32")
    ctx.check(f"trusted names OSC 52, got {trusted!r}", "OSC 52" in trusted)
    ctx.check(f"untrusted names clip.exe as the primary mechanism, got {untrusted!r}",
              untrusted.lower().startswith("clip.exe"))
    ctx.check(f"untrusted still explains why (mentions OSC 52), got {untrusted!r}", "OSC 52" in untrusted)


@test
def test_clipboard_doctor_line_names_win32_and_the_active_mechanism(ctx: Ctx):
    from halo_harness.tui.clipboard import clipboard_doctor_line

    trusted_line = clipboard_doctor_line({"WT_SESSION": "1"}, platform="win32")
    untrusted_line = clipboard_doctor_line({}, platform="win32")
    for line in (trusted_line, untrusted_line):
        ctx.check(f"[OK], names win32, got {line!r}", line.startswith("[OK]") and "win32" in line)
    darwin_line = clipboard_doctor_line({}, platform="darwin")
    ctx.check(f"darwin names pbcopy, got {darwin_line!r}", "pbcopy" in darwin_line)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
