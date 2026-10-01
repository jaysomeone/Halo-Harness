"""halo_harness.tui.images -- H13 Part B ("inline images in the terminal").

Pure, stdlib-first (Pillow optional, same "works with no imaging library at
all" contract as `tools/imageutil.py`) building blocks for rendering a tool
result's image inline instead of the plain type/size/dimensions caption:
terminal-protocol detection (kitty graphics protocol vs sixel vs neither),
the two wire encoders, and the config/flag/detection -> "inline"|"caption"
decision. `tui/widgets/cards.py` is the only caller that actually WRITES an
encoded sequence to the terminal; everything here is byte-shape-testable
without one.

`textual-image` (PyPI) was evaluated per the brief and rejected on BOTH
grounds it named: its own GitHub repo declares LGPL-3.0 (not permissive),
and it requires Python >=3.12 (this project supports >=3.10, tested down to
3.10 per pyproject.toml) -- so the encoders below are the brief's own
documented fallback, not a shortcut taken lightly.
"""

from __future__ import annotations

import base64
import io
import subprocess
from typing import Callable, Optional

KITTY_CHUNK_SIZE = 4096  # protocol max per-escape base64 payload size
MAX_INLINE_PX = 640      # long-edge cap for a rendered-inline image (downscaled when Pillow is present)

_KITTY_TERM_PROGRAMS = {"wezterm", "ghostty"}
_KITTY_TERM_EXACT = {"foot", "foot-extra"}


# ---- detection ---------------------------------------------------------

def kitty_capable_from_env(env: dict) -> bool:
    """kitty itself, WezTerm, Ghostty, foot -- all speak the kitty graphics
    protocol; detected from env vars alone (no terminal round-trip needed,
    unlike sixel below)."""
    if env.get("KITTY_WINDOW_ID"):
        return True
    if (env.get("TERM_PROGRAM") or "").lower() in _KITTY_TERM_PROGRAMS:
        return True
    term = (env.get("TERM") or "").lower()
    return "kitty" in term or term in _KITTY_TERM_EXACT


def tmux_passthrough_enabled(*, run: Optional[Callable] = None) -> bool:
    """`tmux show-options -g allow-passthrough` -- best-effort, `run`
    (a `subprocess.run`-shaped callable) is injectable for tests; no tmux
    binary, no server, an old tmux with no such option, or any other
    failure all read as False (never inline through an un-forwarded tmux),
    never raise."""
    run = run or subprocess.run
    try:
        result = run(["tmux", "show-options", "-g", "allow-passthrough"],
                      capture_output=True, text=True, timeout=1.0)
        return result.returncode == 0 and "on" in (result.stdout or "").lower()
    except Exception:
        return False


def sixel_capable_from_query(*, query: Optional[Callable] = None) -> bool:
    """A live DA1 (`ESC [ c`) capability query -- xterm/mlterm/others report
    sixel support as attribute "4" in the reply (e.g. `\\x1b[?64;1;4;6c`).
    `query` (a zero-arg callable returning the raw reply string) is
    injectable for tests since this needs a real terminal round-trip
    otherwise; any failure (no real tty, timeout, silence) reads as False."""
    try:
        reply = query() if query is not None else None
    except Exception:
        return False
    if not reply:
        return False
    body = reply.rsplit("?", 1)[-1].rstrip("c\x1b\\")
    return "4" in body.split(";")


def detect_image_protocol(env: dict, *, isatty: bool = True,
                           tmux_check: Optional[Callable] = None,
                           sixel_query: Optional[Callable] = None) -> str:
    """"kitty" | "sixel" | "none" -- the brief's own matrix. No real tty
    (piped output, a test harness) is always "none". Inside tmux, EITHER
    protocol needs `allow-passthrough on` first -- checked once, live
    (`tmux_check`, injectable) -- otherwise "none" outright, before even
    looking at $TERM. Kitty-family detection (env-only) beats a live sixel
    query, which only ever runs when nothing kitty-shaped was found."""
    if not isatty:
        return "none"
    if env.get("TMUX") and not tmux_passthrough_enabled(run=tmux_check):
        return "none"
    if kitty_capable_from_env(env):
        return "kitty"
    if sixel_capable_from_query(query=sixel_query):
        return "sixel"
    return "none"


# ---- config / flag -> effective render mode -----------------------------

def effective_render_mode(config_value: Optional[str], *, no_inline_flag: bool = False) -> str:
    """"inline" | "caption" -- `--no-inline-images` and `images: "caption"`/
    `"off"` in `~/.halo/config.json` all mean the same thing for
    RENDERING purposes (never attempt a terminal query or an escape
    sequence); `"inline"` (the default, `config_value` missing/unrecognised)
    means "try `detect_image_protocol`, fall back to the caption if it says
    none". `"off"` vs `"caption"` are kept as distinct config STRINGS (the
    brief documents both) even though they render identically -- "off"
    additionally means callers should skip probing the terminal at all,
    which is `effective_render_mode` itself being called or not, not a
    branch inside it."""
    if no_inline_flag or config_value in ("caption", "off"):
        return "caption"
    return "inline"


# ---- Pillow-optional downscale (bounded rendered size) ------------------

def downscale_for_terminal(data: bytes, media_type: str, *, max_px: int = MAX_INLINE_PX) -> bytes:
    """Best-effort shrink to `max_px` on the long edge via Pillow, same
    "works with no imaging library at all" contract as `tools/imageutil.
    py`'s own `_try_pillow_resize` -- returns `data` UNCHANGED when Pillow
    isn't installed or the image is already small enough (kitty's own
    `c=`/`r=` cell-size control data still bounds the ON-SCREEN size either
    way; this bounds the bytes actually sent/decoded)."""
    try:
        from PIL import Image
    except ImportError:
        return data
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        width, height = img.size
        scale = min(1.0, max_px / max(width, height))
        if scale >= 1.0:
            return data
        img = img.resize((max(1, int(width * scale)), max(1, int(height * scale))))
        fmt = {"image/png": "PNG", "image/jpeg": "JPEG", "image/gif": "GIF", "image/webp": "WEBP"}.get(
            media_type, "PNG")
        buf = io.BytesIO()
        img.convert("RGB" if fmt == "JPEG" else img.mode).save(buf, format=fmt)
        return buf.getvalue()
    except Exception:
        return data


# ---- kitty graphics protocol (APC) --------------------------------------

def encode_kitty_apc(png_or_jpeg_bytes: bytes, *, cell_cols: Optional[int] = None,
                      cell_rows: Optional[int] = None) -> str:
    """`a=T,f=100,t=d` -- "transmit and display", format 100 (PNG/JPEG: let
    the TERMINAL decode it, no pixel-level work needed here at all), data
    given directly (base64) in the command. Payloads over `KITTY_CHUNK_SIZE`
    base64 chars are split across multiple escapes per the protocol's own
    chunking rule (`m=1` on every chunk but the last, which carries `m=0`);
    a single-chunk transfer's own escape still carries `m=0` for the same
    "no more chunks coming" meaning. `cell_cols`/`cell_rows` (kitty's own
    `c=`/`r=` keys) bound the ON-SCREEN size in terminal cells even when
    the source bytes weren't pixel-downscaled first."""
    payload = base64.b64encode(png_or_jpeg_bytes).decode("ascii")
    chunks = [payload[i:i + KITTY_CHUNK_SIZE] for i in range(0, len(payload), KITTY_CHUNK_SIZE)] or [""]
    out = []
    for i, chunk in enumerate(chunks):
        control = []
        if i == 0:
            control.append("a=T")
            control.append("f=100")
            if cell_cols:
                control.append(f"c={cell_cols}")
            if cell_rows:
                control.append(f"r={cell_rows}")
        control.append("m=1" if i < len(chunks) - 1 else "m=0")
        out.append(f"\x1b_G{','.join(control)};{chunk}\x1b\\")
    return "".join(out)


# ---- sixel (DECSIXEL) ----------------------------------------------------

def encode_sixel(data: bytes, *, max_colors: int = 256) -> Optional[str]:
    """A real (if simple: whole-image adaptive palette, RLE per row) DECSIXEL
    encoder -- needs Pillow for pixel access (sixel is a raster protocol; a
    per-format struct-level dimension sniff, `tools/imageutil.py`'s whole
    approach, has nothing to decode actual pixels FROM). Returns None when
    Pillow isn't installed or decoding fails -- the caller's own "no
    protocol" caption fallback covers this exactly like a failed detection
    would."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        pal_img = img.convert("P", palette=Image.ADAPTIVE, colors=max_colors)
        palette = pal_img.getpalette() or []
        w, h = pal_img.size
        pixels = pal_img.load()

        out = ["\x1bPq"]
        for idx in range(min(max_colors, len(palette) // 3)):
            r, g, b = palette[idx * 3:idx * 3 + 3]
            out.append(f"#{idx};2;{round(r * 100 / 255)};{round(g * 100 / 255)};{round(b * 100 / 255)}")
        for band_start in range(0, h, 6):
            band_h = min(6, h - band_start)
            col_bits: dict = {}
            for dy in range(band_h):
                y = band_start + dy
                for x in range(w):
                    c = pixels[x, y]
                    bits = col_bits.get(c)
                    if bits is None:
                        bits = col_bits[c] = [0] * w
                    bits[x] |= (1 << dy)
            first = True
            for color in sorted(col_bits):
                out.append("$" if not first else "")
                first = False
                out.append(f"#{color}")
                bits, row, i = col_bits[color], [], 0
                while i < w:
                    j = i
                    while j < w and bits[j] == bits[i]:
                        j += 1
                    run, ch = j - i, chr(bits[i] + 63)
                    row.append(f"!{run}{ch}" if run > 3 else ch * run)
                    i = j
                out.append("".join(row))
            out.append("-")
        out.append("\x1b\\")
        return "".join(out)
    except Exception:
        return None
