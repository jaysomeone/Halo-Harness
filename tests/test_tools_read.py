"""tests.test_tools_read -- tools/{base,registry,read}.py: cat -n
numbering, offset/limit, absolute-path requirement, registry dispatch and
name-sorted definitions.
"""
import base64
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials, ensure_scoped_state_dir_once

ensure_scoped_state_dir_once()
ensure_default_provider_credentials()
from rolo_claude.tools.base import ToolContext
from rolo_claude.tools.read import ReadTool
from rolo_claude.tools.registry import ToolRegistry

test, TESTS = new_registry()

# A real, minimal (1x1 pixel) PNG -- valid enough for imageutil's own
# dimension sniffer and small enough to embed directly in a test.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


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
def test_read_image_without_vision_gets_a_text_note(ctx: Ctx):
    """H8 scope B: `ctx.vision` defaults to False (every pre-H8 ToolContext)
    -- an image file must become a plain text note, never a raw error and
    never a real image block a non-vision model can't consume."""
    d = Path(tempfile.mkdtemp(prefix="read-tool-img-novision-"))
    f = d / "shot.png"
    f.write_bytes(_TINY_PNG)
    tool = ReadTool()
    result = tool.run({"file_path": str(f)}, ToolContext(cwd=d))
    ctx.check("not an error", result.is_error is False)
    ctx.check("content stays a plain string (no vision)", isinstance(result.content, str))
    ctx.check("notes that vision isn't available", "vision" in result.content.lower())


@test
def test_read_image_with_vision_returns_an_image_block(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="read-tool-img-vision-"))
    f = d / "shot.png"
    f.write_bytes(_TINY_PNG)
    tool = ReadTool()
    cache: dict = {}
    result = tool.run({"file_path": str(f)}, ToolContext(cwd=d, vision=True, read_cache=cache))
    ctx.check("not an error", result.is_error is False)
    ctx.check(f"content is a list of blocks, got {type(result.content)}", isinstance(result.content, list))
    block = result.content[0]
    ctx.check("a real image block", block.get("type") == "image")
    ctx.check("base64 source with the right media type", block.get("source", {}).get("media_type") == "image/png")
    ctx.check("read_cache updated for this path (Write's must-Read-first check)", str(f) in cache)


@test
def test_read_image_case_insensitive_extension_and_jpeg(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="read-tool-img-jpg-"))
    f = d / "photo.JPG"
    # Not a real JPEG -- imageutil's dimension sniffer will fail to parse
    # it and fall back to the byte-size gate only, which a few bytes easily
    # passes; this test only cares that the .JPG (uppercase) EXTENSION is
    # still recognised and routed as an image, not "appears to be binary".
    f.write_bytes(b"\xff\xd8\xff\xe0not a real jpeg but has the magic bytes")
    tool = ReadTool()
    result = tool.run({"file_path": str(f)}, ToolContext(cwd=d, vision=True))
    ctx.check("not flagged as a generic binary file", "binary file" not in (
        result.content if isinstance(result.content, str) else ""))


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
    from rolo_claude.tools.base import Tool

    class BoomTool(Tool):
        name = "Boom"
        description = "always raises"

        def run(self, input, ctx):
            raise RuntimeError("boom")

    reg = ToolRegistry(tools=[BoomTool()])
    result = reg.dispatch("Boom", {}, ToolContext(cwd=Path(".")))
    ctx.check("a tool raising an exception becomes an error result, never propagates", result.is_error is True)
    ctx.check("mentions the exception", "boom" in result.content.lower())


# ---- H8 scope B: image files -----------------------------------------------

def _write_png(path: Path, width: int = 100, height: int = 80) -> None:
    from tests.test_imageutil import _make_png
    path.write_bytes(_make_png(width, height))


@test
def test_read_image_without_vision_is_a_plain_note(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="read-img-"))
    png = d / "screenshot.png"
    _write_png(png)
    result = ReadTool().run({"file_path": str(png)}, ToolContext(cwd=d, vision=False))
    ctx.check("not an error", result.is_error is False)
    ctx.check("a plain text note, not an image block", isinstance(result.content, str))
    ctx.check(f"names it as an image, got {result.content!r}", "image" in result.content.lower())
    ctx.check("says why (no vision)", "vision" in result.content.lower())


@test
def test_read_image_with_vision_returns_a_real_image_block(ctx: Ctx):
    """H8 scope B: this is the "image block reaches the TUI card path"
    check -- Read's own ToolResult content becomes a list with a real
    `image` block, which is exactly the shape agent/loop.py's
    `_finalize_tool_result` -> `_summary_text_for_blocks`/`_image_caption`
    turns into the `tool_result` event's "[image: image/png, WxH, N KB]"
    caption (tui/dispatch.py's ToolCard then renders that as its body
    text) -- see test_loop_tools_image_result_reaches_the_tool_result_event
    below for the full event-level proof."""
    d = Path(tempfile.mkdtemp(prefix="read-img-vision-"))
    png = d / "screenshot.png"
    _write_png(png)
    result = ReadTool().run({"file_path": str(png)}, ToolContext(cwd=d, vision=True))
    ctx.check("not an error", result.is_error is False)
    ctx.check("content is a list of blocks", isinstance(result.content, list))
    ctx.check("exactly one image block", len(result.content) == 1 and result.content[0]["type"] == "image")
    ctx.check("base64 source with the right media type",
              result.content[0]["source"]["type"] == "base64" and result.content[0]["source"]["media_type"] == "image/png")


@test
def test_read_image_updates_the_shared_read_cache(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="read-img-cache-"))
    png = d / "screenshot.png"
    _write_png(png)
    cache: dict = {}
    ReadTool().run({"file_path": str(png)}, ToolContext(cwd=d, vision=True, read_cache=cache))
    ctx.check("the image Read is recorded in the shared read_cache", str(png) in cache)


@test
def test_read_oversized_image_is_resized_or_omitted(ctx: Ctx):
    """Environment-adaptive (see test_imageutil.py's own note): a REAL,
    decodable oversized PNG is resized when Pillow is installed, omitted
    with OpenCode's exact note text when it isn't -- either way Read must
    never error out or crash on it.

    NEW from H9 (verified WSL failure): must exceed MAX_IMAGE_HARD_DIM
    (8000), NOT the older MAX_IMAGE_DIM (1568) this test used before H9
    whole-tree review finding 19 -- that finding moved the omit-without-
    Pillow gate from the soft (1568px, Anthropic's own auto-resize
    threshold) to the hard (8000px) dimension limit on purpose (a plain
    1920x1080 screenshot was being omitted for no real reason). Sizing
    this at only MAX_IMAGE_DIM + 400 now passes through BOTH with and
    without Pillow -- a real image block either way -- so the "no Pillow"
    branch below was never actually reachable on a Pillow-less box after
    that fix landed (masked on Windows dev boxes that happen to have
    Pillow installed; caught for real on a fresh WSL venv without it)."""
    from rolo_claude.tools.imageutil import MAX_IMAGE_HARD_DIM
    from tests.test_imageutil import _pillow_available
    d = Path(tempfile.mkdtemp(prefix="read-img-huge-"))
    png = d / "huge.png"
    _write_png(png, width=MAX_IMAGE_HARD_DIM + 400, height=10)
    result = ReadTool().run({"file_path": str(png)}, ToolContext(cwd=d, vision=True))
    ctx.check("not an error either way", result.is_error is False)
    if _pillow_available():
        ctx.check("Pillow installed: resized to a real image block", isinstance(result.content, list))
        return
    ctx.check("no Pillow: content is plain text (omitted), not an image block", isinstance(result.content, str))
    ctx.check(f"OpenCode's own omitted wording, got {result.content!r}",
              "could not be resized below the image size limit" in result.content)


@test
def test_read_image_missing_file_is_error(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="read-img-missing-"))
    result = ReadTool().run({"file_path": str(d / "nope.png")}, ToolContext(cwd=d, vision=True))
    ctx.check("missing image file is an error", result.is_error is True)


@test
def test_loop_tools_image_result_reaches_the_tool_result_event(ctx: Ctx):
    """H8 scope B acceptance ("a Playwright screenshot rendered as an image
    card in the TUI"): verified headlessly (a live TUI screenshot pilot is
    impractical to gate this suite on) by asserting the image block
    actually reaches the `tool_result` EVENT's own data, carrying a real
    "[image: image/png, 100x80, N KB]" caption (not just a bare "[image]")
    -- the exact input tui/dispatch.py's handler uses to call
    `ToolCard.set_result(content=...)`, which renders ANY multi-line
    string as the card's body through its existing plain-text pipeline --
    through a REAL Session dispatch, not just the Read tool in isolation."""
    import os

    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import ModelProfile, parse_model_ref

    # H10b: never set before -- the real in-process Session below fell
    # through to the REAL `~/.rolo-claude/sessions`, leaking
    # `or:mock/vision-model` sessions into rolo's real session history
    # (H10b report).
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="read-img-e2e-home-")))
    d = Path(tempfile.mkdtemp(prefix="read-img-e2e-"))
    png = d / "screenshot.png"
    _write_png(png)
    session_ctx = SessionContext(cwd=d, model_label="mock/vision-model")
    model_ref = parse_model_ref("or:mock/vision-model")
    session = Session(cwd=d, model_ref=model_ref, model_profile=ModelProfile(vision=True), creds=None,
                       state_dir=Path(tempfile.mkdtemp(prefix="read-img-e2e-state-")), model_label="mock/vision-model",
                       session_context=session_ctx)
    tu = {"type": "tool_use", "id": "call_shot", "name": "Read", "input": {"file_path": str(png)}}
    events_seen = list(session._dispatch_tools(1, [tu]))
    results = [e for e in events_seen if e.kind == "tool_result"]
    ctx.check("one tool_result event", len(results) == 1)
    summary, content = results[0].data.get("summary"), results[0].data.get("content")
    # H8 scope B: the ToolCard-ready caption -- media type, the real
    # sniffed dimensions (100x80, this fixture's own size), a human byte
    # size -- not just the bare "[image]" this replaced (see
    # agent.loop._image_caption).
    ctx.check(f"summary and content agree, got summary={summary!r} content={content!r}", summary == content)
    ctx.check(f"names the media type, got {summary!r}", "image/png" in summary)
    ctx.check(f"names the real sniffed dimensions, got {summary!r}", "100x80" in summary)
    ctx.check("the call succeeded", results[0].data.get("ok") is True)
    # And the LOG (what a resumed session or /export would see) keeps the
    # real image block, not just the display placeholder.
    tool_result_node = next(n for n in session.log.nodes() if n.get("type") == "tool_result")
    logged = tool_result_node.get("content")
    ctx.check("the log keeps the REAL image block, not the placeholder",
              isinstance(logged, list) and any(b.get("type") == "image" for b in logged))


@test
def test_h9b_f19_media_type_comes_from_sniffed_bytes_not_the_extension(ctx: Ctx):
    """Verified bug: `shot.jpg` holding real PNG bytes was sent to the API
    labeled `image/jpeg` (from the extension), which the Anthropic API
    rejects since the declared media_type must match the real content."""
    d = Path(tempfile.mkdtemp(prefix="read-tool-img-mismatch-"))
    f = d / "shot.jpg"  # extension says JPEG
    f.write_bytes(_TINY_PNG)  # content is really PNG
    tool = ReadTool()
    result = tool.run({"file_path": str(f)}, ToolContext(cwd=d, vision=True))
    ctx.check(f"no error, got {result.content!r}", not result.is_error)
    block = result.content[0] if isinstance(result.content, list) else None
    ctx.check(f"a real image block, got {result.content!r}", block is not None and block.get("type") == "image")
    ctx.check(f"media_type reflects the REAL sniffed content (png), not the extension (jpg), "
              f"got {block['source']['media_type']!r}", block["source"]["media_type"] == "image/png")


@test
def test_h9b_f18_continue_offset_hint_matches_what_was_actually_shown_and_cuts_on_a_line_boundary(ctx: Ctx):
    """Verified bug: a 2,500-line CSV showed about line 1,218 (the char cap
    cut mid-line-count), and the hint said "pass offset=2000" (computed
    from how many lines were READ before the cap, not how many were
    actually SHOWN) -- skipping 782 real lines the model never saw."""
    d = Path(tempfile.mkdtemp(prefix="read-tool-offset-hint-"))
    f = d / "wide.csv"
    # Each line is long enough that the ~100k-char result cap cuts well
    # before all 2000 requested lines fit -- forces the size-truncation
    # path this fix targets.
    f.write_text("\n".join(f"{i},{'x' * 90}" for i in range(2500)) + "\n", encoding="utf-8")
    tool = ReadTool()
    result = tool.run({"file_path": str(f), "limit": 2000}, ToolContext(cwd=d))
    ctx.check("size-truncated (the scenario this bug needs)", "truncated at ~25000 tokens" in result.content)
    import re
    m = re.search(r"pass offset=(\d+) to continue", result.content)
    ctx.check(f"a continue hint is present, got {result.content[-200:]!r}", m is not None)
    hinted_offset = int(m.group(1))
    # The body (minus the trailing hint line) must have EXACTLY hinted_offset
    # numbered lines in it -- proving the hint matches what was truly shown,
    # and that the cut landed on a real line boundary (a mid-line cut would
    # make the last "line" not start with a clean "NNNNNN\t" numbering).
    body_lines = [ln for ln in result.content.splitlines() if ln[:1].isdigit() or (ln[:6].strip().isdigit())]
    ctx.check(f"the hint's offset ({hinted_offset}) matches the real number of shown lines "
              f"({len(body_lines)})", hinted_offset == len(body_lines))
    last_line = body_lines[-1]
    ctx.check(f"the last shown line is a clean, complete numbered line (never cut mid-line), "
              f"got {last_line!r}", "\t" in last_line and last_line.split("\t", 1)[1].count(",") == 1)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
