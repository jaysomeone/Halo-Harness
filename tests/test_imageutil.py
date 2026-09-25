"""tests.test_imageutil -- rolo_claude.tools.imageutil: pure-stdlib PNG/
JPEG/GIF/WEBP dimension sniffing and the OpenCode size/dimension gate
(H8 scope B), with no imaging library dependency.
"""
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.tools import imageutil as I

test, TESTS = new_registry()


def _make_png(width: int, height: int) -> bytes:
    """A minimal, syntactically valid 1x1-pixel-data PNG with the GIVEN
    width/height in its IHDR chunk (pixel data doesn't need to actually
    match -- only the header is ever inspected)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"\x00" + b"\xff\x00\x00" * width
    idat = zlib.compress(raw * height)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _make_gif(width: int, height: int) -> bytes:
    header = b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"
    return header


def _make_jpeg(width: int, height: int) -> bytes:
    soi = b"\xff\xd8"
    # A minimal SOF0 segment: length(2)=11, precision(1)=8, height(2), width(2), components(1)=1, component data(3)
    sof_payload = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"
    sof = b"\xff\xc0" + struct.pack(">H", 2 + len(sof_payload)) + sof_payload
    eoi = b"\xff\xd9"
    return soi + sof + eoi


def _make_webp_vp8x(width: int, height: int) -> bytes:
    w1, h1 = width - 1, height - 1
    vp8x_payload = b"\x10" + b"\x00" * 3 + struct.pack("<I", w1)[:3] + struct.pack("<I", h1)[:3]
    chunk = b"VP8X" + struct.pack("<I", len(vp8x_payload)) + vp8x_payload
    riff_body = b"WEBP" + chunk
    return b"RIFF" + struct.pack("<I", len(riff_body)) + riff_body


# ---- dimension sniffing -----------------------------------------------------

@test
def test_sniff_png_dimensions(ctx: Ctx):
    data = _make_png(800, 600)
    ctx.check("PNG dims read correctly", I.sniff_dimensions(data) == (800, 600))


@test
def test_sniff_gif_dimensions(ctx: Ctx):
    data = _make_gif(320, 240)
    ctx.check("GIF dims read correctly", I.sniff_dimensions(data) == (320, 240))


@test
def test_sniff_jpeg_dimensions(ctx: Ctx):
    data = _make_jpeg(1024, 768)
    ctx.check("JPEG dims read correctly", I.sniff_dimensions(data) == (1024, 768))


@test
def test_sniff_webp_vp8x_dimensions(ctx: Ctx):
    data = _make_webp_vp8x(500, 400)
    ctx.check("WEBP (VP8X) dims read correctly", I.sniff_dimensions(data) == (500, 400))


@test
def test_sniff_unrecognized_format_returns_none(ctx: Ctx):
    ctx.check("garbage bytes -> None, never raises", I.sniff_dimensions(b"not an image at all") is None)


@test
def test_sniff_truncated_png_returns_none_not_raise(ctx: Ctx):
    ctx.check("truncated header -> None", I.sniff_dimensions(b"\x89PNG\r\n\x1a\n\x00\x00") is None)


@test
def test_is_image_path_recognizes_extensions(ctx: Ctx):
    ctx.check("png", I.is_image_path(Path("/x/a.PNG")) == "image/png")
    ctx.check("jpg", I.is_image_path(Path("/x/a.jpg")) == "image/jpeg")
    ctx.check("jpeg", I.is_image_path(Path("/x/a.jpeg")) == "image/jpeg")
    ctx.check("gif", I.is_image_path(Path("/x/a.gif")) == "image/gif")
    ctx.check("webp", I.is_image_path(Path("/x/a.webp")) == "image/webp")
    ctx.check("not an image extension -> None", I.is_image_path(Path("/x/a.txt")) is None)


# ---- image_block_or_note (the OpenCode size/dimension gate) ---------------

@test
def test_small_image_becomes_a_real_block(ctx: Ctx):
    data = _make_png(100, 100)
    block, note = I.image_block_or_note(data, "image/png")
    ctx.check("no note", note is None)
    ctx.check("real image block", block is not None and block["type"] == "image")
    ctx.check("media type preserved", block["source"]["media_type"] == "image/png")


def _pillow_available() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


@test
def test_oversized_dimension_is_resized_or_omitted(ctx: Ctx):
    """A REAL, decodable over-MAX_IMAGE_DIM image is resized when Pillow is
    installed (this build never REQUIRES it -- an optional extra, see
    pyproject.toml), and omitted with OpenCode's own exact note text when
    it isn't -- environment-adaptive so this test is correct either way,
    never silently crashed on."""
    data = _make_png(I.MAX_IMAGE_DIM + 500, 100)
    block, note = I.image_block_or_note(data, "image/png")
    if _pillow_available():
        ctx.check("Pillow installed: resized to a real, now-compliant block", block is not None and note is None)
    else:
        ctx.check("no Pillow: omitted", block is None)
        ctx.check(f"OpenCode's own note text, got {note!r}", note == I.OMITTED_NOTE)


@test
def test_oversized_bytes_of_non_image_data_is_always_omitted(ctx: Ctx):
    """Plain non-image bytes (not a real, Pillow-decodable image at all)
    over the byte cap can never be resized by anything -- always omitted,
    regardless of whether Pillow is installed."""
    huge = b"x" * (I.MAX_IMAGE_BYTES + 1)
    block, note = I.image_block_or_note(huge, "image/octet-stream")
    ctx.check("no block", block is None)
    ctx.check("omitted note", note == I.OMITTED_NOTE)


@test
def test_exactly_at_byte_limit_is_not_omitted(ctx: Ctx):
    exact = b"x" * I.MAX_IMAGE_BYTES
    block, note = I.image_block_or_note(exact, "image/octet-stream")
    ctx.check("at (not over) the byte cap is kept, not omitted", block is not None and note is None)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
