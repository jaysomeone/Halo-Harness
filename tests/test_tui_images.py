"""tests.test_tui_images -- H13 Part B ("inline images in the terminal"):
`halo_harness/tui/images.py`'s encoders (kitty APC framing, sixel DECSIXEL
header), the detection matrix (env-based kitty family, tmux passthrough
gate, an injectable live sixel query), and the config/flag ->
"inline"|"caption" resolution. Pure functions throughout -- no Textual, no
real terminal; `test_tui.py` covers the one pilot-level check (a fake image
tool result reaching a real `ToolCard`).
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all
from halo_harness.tui import images as I

test, TESTS = new_registry()

# The same tiny (1x1, transparent) real PNG `tests/helpers/fake_mcp_server.py`
# uses for its own `image` tool -- small enough to exercise the encoders
# without needing a real screenshot fixture.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


# ---- kitty APC framing -------------------------------------------------

@test
def test_encode_kitty_apc_single_chunk_shape(ctx: Ctx):
    seq = I.encode_kitty_apc(PNG_1X1)
    ctx.check(f"starts with the APC-open + transmit-and-display + PNG-format control data, got {seq[:20]!r}",
              seq.startswith("\x1b_Ga=T,f=100"))
    ctx.check(f"ends with the APC terminator, got {seq[-5:]!r}", seq.endswith("\x1b\\"))
    ctx.check("a single-chunk transfer still carries m=0 (\"no more chunks\")", ",m=0;" in seq)
    payload_b64 = base64.b64encode(PNG_1X1).decode("ascii")
    ctx.check("the base64 payload is present verbatim", payload_b64 in seq)


@test
def test_encode_kitty_apc_chunks_large_payloads(ctx: Ctx):
    seq = I.encode_kitty_apc(b"x" * 20000)
    chunks = [c for c in seq.split("\x1b\\") if c]
    ctx.check(f"more than one chunk for a >4096-base64-char payload, got {len(chunks)}", len(chunks) > 1)
    ctx.check(f"the FIRST chunk carries a=T,f=100 and m=1, got {chunks[0][:40]!r}",
              chunks[0].startswith("\x1b_Ga=T,f=100,m=1;"))
    ctx.check(f"every MIDDLE chunk carries only m=1, got {chunks[1]!r}", chunks[1].startswith("\x1b_Gm=1;"))
    ctx.check(f"the LAST chunk carries m=0 (no more chunks), got {chunks[-1][:20]!r}",
              chunks[-1].startswith("\x1b_Gm=0;"))


@test
def test_encode_kitty_apc_cell_cols_rows(ctx: Ctx):
    seq = I.encode_kitty_apc(PNG_1X1, cell_cols=40, cell_rows=20)
    ctx.check("c=/r= cell-size control data present", "c=40" in seq and "r=20" in seq)


# ---- sixel DECSIXEL header ------------------------------------------------

@test
def test_encode_sixel_header_and_trailer_shape(ctx: Ctx):
    seq = I.encode_sixel(PNG_1X1)
    if seq is None:
        raise SkipTest("Pillow not installed -- encode_sixel needs it for real pixel/palette work")
    ctx.check(f"starts with the DECSIXEL introducer, got {seq[:8]!r}", seq.startswith("\x1bPq"))
    ctx.check(f"ends with the ST terminator, got {seq[-4:]!r}", seq.endswith("\x1b\\"))
    ctx.check("at least one palette definition (#<idx>;2;R;G;B) present", "#0;2;" in seq)


@test
def test_encode_sixel_returns_none_without_pillow(ctx: Ctx):
    import builtins
    real_import = builtins.__import__

    def _no_pillow(name, *a, **k):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no Pillow for this test")
        return real_import(name, *a, **k)

    builtins.__import__ = _no_pillow
    try:
        ctx.check("no Pillow -> None (never raises)", I.encode_sixel(PNG_1X1) is None)
    finally:
        builtins.__import__ = real_import


# ---- detection matrix (env-based) -----------------------------------------

@test
def test_kitty_capable_from_env_matrix(ctx: Ctx):
    cases = [
        ({"KITTY_WINDOW_ID": "1"}, True, "kitty itself"),
        ({"TERM_PROGRAM": "WezTerm"}, True, "WezTerm (case-insensitive)"),
        ({"TERM_PROGRAM": "ghostty"}, True, "Ghostty"),
        ({"TERM": "foot"}, True, "foot"),
        ({"TERM": "xterm-kitty"}, True, "TERM containing kitty"),
        ({"TERM": "xterm-256color"}, False, "plain xterm"),
        ({}, False, "no env signal at all"),
    ]
    for env, expected, label in cases:
        got = I.kitty_capable_from_env(env)
        ctx.check(f"{label}: expected {expected}, got {got}", got == expected)


@test
def test_detect_image_protocol_no_tty_is_always_none(ctx: Ctx):
    ctx.check("no real tty -> none even with a perfect kitty env",
              I.detect_image_protocol({"KITTY_WINDOW_ID": "1"}, isatty=False) == "none")


@test
def test_detect_image_protocol_tmux_gate(ctx: Ctx):
    class _R:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    no_passthrough = I.detect_image_protocol(
        {"TMUX": "1", "KITTY_WINDOW_ID": "1"}, tmux_check=lambda *a, **k: _R(1, ""))
    ctx.check(f"tmux without allow-passthrough -> none even with a kitty env, got {no_passthrough}",
              no_passthrough == "none")
    with_passthrough = I.detect_image_protocol(
        {"TMUX": "1", "KITTY_WINDOW_ID": "1"}, tmux_check=lambda *a, **k: _R(0, "allow-passthrough on"))
    ctx.check(f"tmux WITH allow-passthrough -> the underlying protocol applies, got {with_passthrough}",
              with_passthrough == "kitty")


@test
def test_detect_image_protocol_sixel_via_live_query(ctx: Ctx):
    got = I.detect_image_protocol({"TERM": "xterm"}, sixel_query=lambda: "\x1b[?64;1;4;6c")
    ctx.check(f"a DA1 reply naming attribute 4 -> sixel, got {got}", got == "sixel")
    no_sixel = I.detect_image_protocol({"TERM": "xterm"}, sixel_query=lambda: "\x1b[?64;1;6c")
    ctx.check(f"a DA1 reply WITHOUT attribute 4 -> none, got {no_sixel}", no_sixel == "none")
    silent = I.detect_image_protocol({"TERM": "dumb"}, sixel_query=lambda: "")
    ctx.check(f"no reply at all -> none, got {silent}", silent == "none")
    raising = I.detect_image_protocol({"TERM": "dumb"}, sixel_query=lambda: (_ for _ in ()).throw(OSError()))
    ctx.check(f"a query that raises -> none, never propagates, got {raising}", raising == "none")


@test
def test_detect_image_protocol_kitty_beats_a_live_sixel_query(ctx: Ctx):
    got = I.detect_image_protocol({"KITTY_WINDOW_ID": "1"}, sixel_query=lambda: "\x1b[?64;1;4;6c")
    ctx.check(f"kitty-family env detection wins outright, no live query even attempted, got {got}", got == "kitty")


# ---- config / --no-inline-images -> effective render mode ----------------

@test
def test_effective_render_mode_matrix(ctx: Ctx):
    ctx.check("default (no config, no flag) -> inline", I.effective_render_mode(None) == "inline")
    ctx.check("config \"inline\" -> inline", I.effective_render_mode("inline") == "inline")
    ctx.check("config \"caption\" -> caption", I.effective_render_mode("caption") == "caption")
    ctx.check("config \"off\" -> caption (same rendering, distinct config string)",
              I.effective_render_mode("off") == "caption")
    ctx.check("--no-inline-images always wins, even over config \"inline\"",
              I.effective_render_mode("inline", no_inline_flag=True) == "caption")


# ---- bounded rendered size (downscale) ------------------------------------

@test
def test_downscale_for_terminal_shrinks_when_pillow_present_else_passthrough(ctx: Ctx):
    try:
        from PIL import Image
    except ImportError:
        ctx.check("no Pillow -> bytes pass through UNCHANGED",
                  I.downscale_for_terminal(PNG_1X1, "image/png") == PNG_1X1)
        return
    import io
    big = Image.new("RGB", (I.MAX_INLINE_PX * 2, 100), color=(10, 20, 30))
    buf = io.BytesIO()
    big.save(buf, format="PNG")
    shrunk = I.downscale_for_terminal(buf.getvalue(), "image/png")
    out = Image.open(io.BytesIO(shrunk))
    ctx.check(f"downscaled to at most MAX_INLINE_PX on the long edge, got {out.size}",
              max(out.size) <= I.MAX_INLINE_PX)
    ctx.check("an already-small image is left byte-for-byte unchanged",
              I.downscale_for_terminal(PNG_1X1, "image/png") == PNG_1X1)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
