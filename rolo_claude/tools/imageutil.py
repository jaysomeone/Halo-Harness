"""rolo_claude.tools.imageutil -- pure-stdlib image sniffing + the OpenCode
size rule (H8 scope B): png/jpeg/gif/webp dimension detection with no
decoder dependency (a manylinux/Windows binary wheel for a real image
library is exactly the kind of thing `tools/vendor_wheels.py` would need to
carry for the work box, so this stays dependency-free), used to decide
whether an image reaches the model as a real `image` content block or gets
omitted with OpenCode's own note text.

Real resizing needs a decoder/encoder this module deliberately does not
depend on -- when Pillow happens to be installed (an OPTIONAL extra, see
pyproject.toml's `[project.optional-dependencies].vision`), `maybe_resize`
uses it to actually shrink an oversized image; otherwise (the common case
on a fresh install) an oversized image is omitted with a note, exactly the
fallback OpenCode's own rule describes ("resize ... or omit with a note").
"""

from __future__ import annotations

import base64
import io
import struct
from pathlib import Path
from typing import Optional

# Anthropic's own documented image limits (long edge auto-resized above
# this; OpenCode adopts the same numbers for its own attachment rule).
MAX_IMAGE_DIM = 1568
MAX_IMAGE_BYTES = 5 * 1024 * 1024

IMAGE_EXTENSIONS = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}

OMITTED_NOTE = "[Image omitted: could not be resized below the image size limit.]"


def is_image_path(path: Path) -> Optional[str]:
    """The media type for `path`'s extension, or None if it isn't one of
    the image extensions this tool speaks at all (case-insensitive)."""
    return IMAGE_EXTENSIONS.get(path.suffix.lower())


def _png_dimensions(data: bytes) -> Optional["tuple[int, int]"]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _gif_dimensions(data: bytes) -> Optional["tuple[int, int]"]:
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    width, height = struct.unpack("<HH", data[6:10])
    return width, height


def _jpeg_dimensions(data: bytes) -> Optional["tuple[int, int]"]:
    """Scan JPEG markers for the first SOFn (start-of-frame) segment, which
    carries the real pixel dimensions -- APP/EXIF/COM segments (which can
    precede it) are just skipped over by their own declared length."""
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # SOF0..SOF15 except the DHT/JPG/DAC markers (0xC4, 0xC8, 0xCC),
        # which share the numeric range but aren't frame headers.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 > n:
                return None
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2  # markers with no length field
            continue
        if i + 4 > n:
            return None
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seg_len
    return None


def _webp_dimensions(data: bytes) -> Optional["tuple[int, int]"]:
    """VP8X (extended format) only -- the common case for a screenshot tool
    or a modern export; plain lossy/lossless VP8/VP8L bitstreams need a
    real bit-level parse this module deliberately skips (the byte-size
    gate below still applies to those)."""
    if len(data) < 30 or data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    if data[12:16] != b"VP8X":
        return None
    width = (data[24] | (data[25] << 8) | (data[26] << 16)) + 1
    height = (data[27] | (data[28] << 8) | (data[29] << 16)) + 1
    return width, height


def sniff_dimensions(data: bytes) -> Optional["tuple[int, int]"]:
    """Best-effort `(width, height)` from raw image bytes, or None when the
    format isn't recognized/parseable (never raises) -- callers that get
    None still have the byte-size gate to fall back on."""
    for sniffer in (_png_dimensions, _jpeg_dimensions, _gif_dimensions, _webp_dimensions):
        try:
            dims = sniffer(data)
        except (struct.error, IndexError):
            dims = None
        if dims:
            return dims
    return None


def _try_pillow_resize(data: bytes, media_type: str) -> Optional[bytes]:
    """Best-effort real resize via Pillow, when installed (optional extra)
    -- returns re-encoded bytes fitting both MAX_IMAGE_DIM and
    MAX_IMAGE_BYTES, or None if Pillow isn't available or resizing still
    can't get under the byte cap (caller falls back to omitting)."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        width, height = img.size
        scale = min(1.0, MAX_IMAGE_DIM / max(width, height))
        if scale < 1.0:
            img = img.resize((max(1, int(width * scale)), max(1, int(height * scale))))
        fmt = {"image/png": "PNG", "image/jpeg": "JPEG", "image/gif": "GIF", "image/webp": "WEBP"}.get(
            media_type, "PNG")
        for quality in (85, 70, 50):
            buf = io.BytesIO()
            save_kwargs = {"quality": quality} if fmt == "JPEG" else {}
            img.convert("RGB" if fmt == "JPEG" else img.mode).save(buf, format=fmt, **save_kwargs)
            out = buf.getvalue()
            if len(out) <= MAX_IMAGE_BYTES:
                return out
        return None
    except Exception:
        return None


def image_block_or_note(data: bytes, media_type: str) -> "tuple[Optional[dict], Optional[str]]":
    """`(image_block, note)` -- exactly one is non-None. Applies OpenCode's
    own rule: resize/downscale to fit `MAX_IMAGE_DIM`/`MAX_IMAGE_BYTES`
    when possible (Pillow installed), else omit with its exact note text
    ("[N image(s) omitted: could not be resized below the image size
    limit.]" -- singular note text here, the caller pluralizes/counts when
    combining several)."""
    dims = sniff_dimensions(data)
    over_dim = bool(dims and max(dims) > MAX_IMAGE_DIM)
    over_bytes = len(data) > MAX_IMAGE_BYTES
    if over_dim or over_bytes:
        resized = _try_pillow_resize(data, media_type)
        if resized is not None:
            data = resized
        else:
            return None, OMITTED_NOTE
    encoded = base64.b64encode(data).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}}, None
