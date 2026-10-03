"""halo_harness.tui.clipboard -- clipboard fallback for Linux terminals (U5
scope E), plus (W2c item 3) the READ-direction counterpart Ctrl+V needs.
Textual's own `App.copy_to_clipboard` already emits an OSC 52 escape
sequence (works over SSH, tmux-passthrough permitting), which is
`tui/app.py`'s primary COPY mechanism (review finding 16). Some terminals/
multiplexer configs don't relay OSC 52 at all -- `copy_via_external_tool`
below is the belt-and-suspenders fallback: best-effort, also pipe the text
into `xclip`/`wl-copy` if either is on PATH, so at least ONE mechanism
lands the text on the system clipboard. `read_via_external_tool` is the
other direction entirely: Ctrl+V has no OSC 52 equivalent to rely on at all
(OSC 52's own "read" variant is disabled by default in most terminals, as a
security measure), so a real system-clipboard PASTE always goes through one
of these external tools, by PATH/platform detection -- `xclip -o`/`xsel -o`
on X11, `wl-paste` on Wayland, `pbpaste` on macOS, PowerShell's own
`Get-Clipboard` on win32. Pure subprocess plumbing, no textual import, so
it's unit-testable without a running App.

`doctor.py` is H8's module (docs/harness/H8-brief.md lists it under H8's
own files) -- rather than edit a contested file, `clipboard_doctor_line()`
below is the ready-to-call check; H8 (or a future pass) can add one line to
`doctor.run_checks()`: `lines.append(clipboard_doctor_line())`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from typing import Optional

# Checked in order; the first one found on PATH is used.
_LINUX_CLIPBOARD_TOOLS = (
    ("wl-copy", ["wl-copy"]),
    ("xclip", ["xclip", "-selection", "clipboard"]),
    ("xsel", ["xsel", "--clipboard", "--input"]),
)

_MACOS_COPY_ARGV = ["pbcopy"]


def find_clipboard_tool() -> "Optional[tuple[str, list]]":
    """`(name, argv)` for the first of `wl-copy`/`xclip`/`xsel` found on
    PATH, or `None` if none are installed (headless server, minimal
    container, ...). Linux-only (see `copy_via_external_tool`'s own
    platform dispatch for win32/darwin, each of which has exactly one
    well-known tool rather than a PATH search)."""
    for name, argv in _LINUX_CLIPBOARD_TOOLS:
        if shutil.which(argv[0]):
            return name, argv
    return None


def _copy_windows_clipboard(text: str, *, timeout_s: float) -> bool:
    """W4c item 3: the actual win32 write-direction fallback -- before this,
    `copy_via_external_tool` only ever tried the LINUX tools above (none of
    which exist on PATH on Windows), so `clipboard_doctor_line()`'s own
    long-standing win32 claim ("plus the native Windows clipboard via
    clip.exe") was never backed by a real call. `clip.exe` (bundled with
    every Windows install) reads raw bytes from stdin; fed UTF-16LE with a
    leading BOM (the same trick `Out-File -Encoding Unicode` uses) it
    copies as real Unicode instead of mangling anything outside the
    console's current codepage -- important here since model output
    routinely contains em dashes, curly quotes and non-Latin text."""
    try:
        # chr(0xFEFF) (never a literal escape/pasted glyph in this source --
        # ASCII-only, so no editor/encoding layer can silently mangle an
        # invisible character here) is the BOM; UTF-16LE encoding is what
        # actually makes clip.exe treat this as Unicode rather than the
        # console's current (possibly lossy) codepage.
        payload = (chr(0xFEFF) + text).encode("utf-16-le")
        proc = subprocess.run(["clip.exe"], input=payload, timeout=timeout_s)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def _copy_argv(argv: list, text: str, *, timeout_s: float) -> bool:
    try:
        proc = subprocess.run(argv, input=text, capture_output=True, text=True, timeout=timeout_s)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def copy_via_external_tool(text: str, *, timeout_s: float = 3.0,
                            platform: "Optional[str]" = None) -> bool:
    """Best-effort fallback copy through an external clipboard tool.
    Returns True only on a confirmed clean run; any failure (not
    installed, no display/wayland session, timeout, ...) returns False
    silently -- this is always a SECOND mechanism alongside OSC 52, never
    the only one, so a failure here must never surface as an error to the
    user. `platform` (default `sys.platform`) is a test seam -- W4c item 3:
    "test seam for both [Windows] paths" -- that also lets a test on ANY
    host exercise the win32/darwin/linux dispatch deterministically rather
    than only whichever platform happens to be running the suite."""
    plat = platform if platform is not None else sys.platform
    if plat == "win32":
        return _copy_windows_clipboard(text, timeout_s=timeout_s)
    if plat == "darwin":
        return _copy_argv(_MACOS_COPY_ARGV, text, timeout_s=timeout_s)
    found = find_clipboard_tool()
    if found is None:
        return False
    _name, argv = found
    return _copy_argv(argv, text, timeout_s=timeout_s)


def osc52_trusted(env: "Optional[dict]" = None, *, platform: "Optional[str]" = None) -> bool:
    """Whether OSC 52 itself (not the external-tool fallback) should be
    trusted to actually reach the system clipboard on THIS terminal --
    W4c item 3: only Windows genuinely varies here. Windows Terminal relays
    OSC 52 and sets `WT_SESSION`; a plain `conhost.exe` console host (a
    bare `cmd.exe`/PowerShell window, not inside Windows Terminal) does
    not relay it at all -- the write is a silent no-op there, so `clip.exe`
    (always available on Windows) is what actually has to do the work.
    Every other platform's terminals are assumed to relay it (xterm, kitty,
    iTerm, tmux/screen passthrough, gnome-terminal, ... -- the existing,
    unconditional assumption this app already made before this brief); the
    external-tool fallback always ALSO runs regardless of this result
    (belt-and-suspenders), so a wrong `True` here on some exotic terminal
    never loses the copy outright. `env`/`platform` default to the real
    `os.environ`/`sys.platform` -- both are test seams."""
    environ = env if env is not None else os.environ
    plat = platform if platform is not None else sys.platform
    if plat == "win32":
        return bool(environ.get("WT_SESSION"))
    return True


def active_clipboard_backend_name(env: "Optional[dict]" = None, *,
                                   platform: "Optional[str]" = None) -> str:
    """One short, truthful description of which mechanism(s) a copy right
    now would actually use -- shared by `clipboard_doctor_line()` (W4c item
    3: "a one-time doctor line names which backend is active") and
    anything else that wants the same answer without duplicating the
    platform logic above."""
    plat = platform if platform is not None else sys.platform
    trusted = osc52_trusted(env, platform=plat)
    if plat == "win32":
        if trusted:
            return "OSC 52 (Windows Terminal), plus the clip.exe fallback"
        return "clip.exe (OSC 52 is not relayed by this console host)"
    if plat == "darwin":
        return "OSC 52, plus the pbcopy fallback"
    found = find_clipboard_tool()
    if found is not None:
        return f"OSC 52, backed up by {found[0]} (found on PATH)"
    return "OSC 52 only -- no xclip/wl-copy/xsel on PATH for a fallback"


# The READ-direction argv for each name `find_clipboard_tool` can return --
# never a separate detection pass: wl-copy/wl-paste, xclip and xsel each
# ship as one package/pair, so "wl-copy is on PATH" is already a reliable
# proxy for "wl-paste is too" (same convention `find_clipboard_tool` already
# uses for the write direction).
_LINUX_CLIPBOARD_READ_ARGV = {
    "wl-copy": ["wl-paste", "--no-newline"],
    "xclip": ["xclip", "-selection", "clipboard", "-o"],
    "xsel": ["xsel", "--clipboard", "--output"],
}

_MACOS_PASTE_ARGV = ["pbpaste"]
_WINDOWS_PASTE_ARGV = ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard"]


def read_via_external_tool(*, timeout_s: float = 3.0) -> "Optional[str]":
    """Best-effort read of the REAL system clipboard -- `None` for "nothing
    usable" (no tool available, the run failed, or it timed out), never
    raises. `tui/app.py`'s own Ctrl+V handler shows the same "paste with
    your terminal" notice `doctor`'s clipboard line already points at when
    this comes back `None`. An empty clipboard also comes back as `None`
    (via the empty-string-is-falsy check its one caller already does) --
    pasting nothing is a no-op either way, so this never needs to
    distinguish "empty" from "no tool" any more precisely than that."""
    if sys.platform == "win32":
        argv = _WINDOWS_PASTE_ARGV
    elif sys.platform == "darwin":
        argv = _MACOS_PASTE_ARGV
    else:
        found = find_clipboard_tool()
        if found is None:
            return None
        name, _write_argv = found
        argv = _LINUX_CLIPBOARD_READ_ARGV.get(name)
        if argv is None:
            return None
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
        if proc.returncode != 0:
            return None
        return proc.stdout or None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def clipboard_doctor_line(env: "Optional[dict]" = None, *, platform: "Optional[str]" = None) -> str:
    """U5 leftover / H8 must-do (now wired into `doctor.run_checks()`):
    one `doctor`-style line naming the clipboard backend actually active
    right now (W4c item 3) -- `active_clipboard_backend_name()` does the
    real platform/trust work; a MISSING fallback tool on Linux (the one
    case where there's genuinely nothing backing OSC 52 up) is the only
    WARN, everything else is OK since every other case has a working
    mechanism. `env`/`platform` are the same test seams `active_clipboard_
    backend_name`/`osc52_trusted` take."""
    plat = platform if platform is not None else sys.platform
    name = active_clipboard_backend_name(env, platform=plat)
    if plat == "win32":
        return f"[OK] Clipboard backend: win32 -- {name}"
    if plat == "darwin":
        return f"[OK] Clipboard backend: darwin -- {name}"
    if find_clipboard_tool() is not None:
        return f"[OK] Clipboard backend: {name}"
    return (f"[WARN] Clipboard backend: {name} (Ctrl+C on a selection still works on terminals that "
            "relay OSC 52, but there's no backup if the terminal/multiplexer doesn't)")


# ============================================================================
# W4c item 1: "copy the SOURCE text, not screen cells". Every transcript/
# tool-card widget exposes its own `copy_text()` (plain strings, no textual
# import needed there either); everything below is the shared, pure cleanup
# `tui/app.py` runs that raw text through before it ever reaches a clipboard
# -- glyph/border stripping, trailing-space stripping, and concatenating a
# multi-widget selection in document order. "Soft wraps joined" is NOT a
# text transform here: a terminal only ever wraps a long line visually, at
# RENDER time, into screen cells -- it never inserts a real "\n" into the
# widget's own stored string. Reading that stored string directly (rather
# than `Screen.get_selected_text()`'s cell-by-cell walk, the old mechanism)
# means a long line simply never had a wrap-induced break to begin with.
# ============================================================================

# The UI-chrome glyphs/box-drawing characters item 1 names -- never
# characters that are part of actual message CONTENT (the ✓/✗/⚠/↩/⑂/⬇/✦
# decision-line/card glyphs elsewhere in this app all stay).
_CHROME_GLYPHS = "⏺❯✻│─╭╰"
_CHROME_GLYPH_RE = re.compile("[" + re.escape(_CHROME_GLYPHS) + "]")
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)(?:```|\Z)", re.DOTALL)


def strip_chrome_glyphs(text: str) -> str:
    """Remove the bullet (⏺), prompt arrow (❯), thinking spinner (✻) and
    `│─╭╰` panel-border characters from copied text. A glyph sitting at
    the very start of a line (every real call site's own convention --
    `"⏺ Bash(...)"`, `"❯ hello"`) takes the ONE space right after it with
    it, so the result reads as plain text rather than leaving a stray
    leading space; a glyph found anywhere else (rare -- these are UI
    chrome, not real content) is simply deleted in place, never touching
    indentation that has nothing to do with it (a code block's own
    leading spaces, for instance)."""
    out_lines = []
    for line in text.split("\n"):
        prefix_len = len(line) - len(line.lstrip())
        rest = line[prefix_len:]
        if rest[:1] in _CHROME_GLYPHS:
            rest = rest[1:]
            if rest.startswith(" "):
                rest = rest[1:]
            line = line[:prefix_len] + rest
        out_lines.append(_CHROME_GLYPH_RE.sub("", line))
    return "\n".join(out_lines)


def normalize_line_endings(text: str) -> str:
    """W4c item 1: "`\\n` on every platform" -- collapse any `\\r\\n`/bare
    `\\r` a pasted-through source might carry to plain `\\n` BEFORE the
    optional `clipboard.crlf` conversion (`apply_crlf_if_configured`) runs
    at the very end of the pipeline, so CRLF is only ever added back
    deliberately, once, never doubled up."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_trailing_spaces(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.split("\n"))


def clean_copy_text(text: str) -> str:
    """The general-purpose cleanup for everything EXCEPT an extracted code
    block (`clean_code_text` below, which deliberately skips glyph
    stripping -- "a code block's exact contents")."""
    text = normalize_line_endings(text)
    text = strip_chrome_glyphs(text)
    text = strip_trailing_spaces(text)
    return text.strip("\n")


def clean_code_text(text: str) -> str:
    """`/copy code`'s own, gentler cleanup -- W4c item 1: "a code block's
    EXACT contents" -- only line-ending normalization and a trailing-
    whitespace trim, never glyph stripping (code legitimately uses `─`/`│`
    for box-drawing output, ASCII art, table borders, ...) and no reflow:
    there is nothing to join here either, see the module note above."""
    text = normalize_line_endings(text)
    text = strip_trailing_spaces(text)
    return text.strip("\n")


def join_copied_texts(parts: "list") -> str:
    """A selection spanning several widgets (or `Y`'s whole current turn)
    concatenates their ALREADY-cleaned texts in order, one blank line
    between each -- the same separator the pre-existing exit-time
    scrollback (`Transcript.plain_log`) already joins its own entries
    with. Empty contributions (a widget with nothing to say, e.g. a
    ThinkingBlock that never got any reasoning) are dropped rather than
    leaving a stray blank paragraph."""
    return "\n\n".join(p for p in parts if p)


def extract_fenced_code_blocks(markdown_text: str) -> "list":
    """Every fenced (```) code block's content in a markdown source, in
    document order, exactly as written (never re-indented). An
    unterminated trailing fence (the reply was still streaming when
    `/copy code` was used) still returns whatever text came after the
    opening fence, up to the end, rather than being silently dropped."""
    return [block.rstrip("\n") for block in _FENCE_RE.findall(markdown_text)]


def apply_crlf_if_configured(text: str) -> str:
    """W4c item 1: `clipboard.crlf: true` in `~/.halo/config.json` converts
    every `\\n` to `\\r\\n` right before the text actually reaches a
    clipboard mechanism -- the LAST step, after every other cleanup above,
    so the character count a copy toast reports matches what's really on
    the clipboard. Default `false` (plain `\\n`, which Windows Terminal and
    every editor this app has been checked against already accepts)."""
    from halo_harness.theme import get_config_value
    if bool(get_config_value("clipboard.crlf", False)):
        return text.replace("\n", "\r\n")
    return text
