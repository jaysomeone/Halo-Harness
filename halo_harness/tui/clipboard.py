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


def find_clipboard_tool() -> "Optional[tuple[str, list]]":
    """`(name, argv)` for the first of `wl-copy`/`xclip`/`xsel` found on
    PATH, or `None` if none are installed (headless server, minimal
    container, ...)."""
    for name, argv in _LINUX_CLIPBOARD_TOOLS:
        if shutil.which(argv[0]):
            return name, argv
    return None


def copy_via_external_tool(text: str, *, timeout_s: float = 3.0) -> bool:
    """Best-effort fallback copy through an external clipboard tool.
    Returns True only on a confirmed clean run; any failure (not
    installed, no display/wayland session, timeout, ...) returns False
    silently -- this is always a SECOND mechanism alongside OSC 52, never
    the only one, so a failure here must never surface as an error to the
    user."""
    found = find_clipboard_tool()
    if found is None:
        return False
    _name, argv = found
    try:
        proc = subprocess.run(argv, input=text, capture_output=True, text=True, timeout=timeout_s)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


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


def clipboard_doctor_line() -> str:
    """U5 leftover / H8 must-do (now wired into `doctor.run_checks()`):
    one `doctor`-style line reporting whether an external clipboard
    fallback tool is available (OSC 52 itself is always attempted first
    and needs no external tool, so this is purely informational -- never
    a MISSING/failure line). Reports the platform-native backend on
    win32/macOS (both always have a real system clipboard mechanism,
    `clip.exe`/`pbcopy`, so there's nothing to detect there -- only Linux
    genuinely varies by desktop/compositor and needs `find_clipboard_
    tool()`'s own PATH probe)."""
    if sys.platform == "win32":
        return "[OK] Clipboard backend: win32 (OSC 52, plus the native Windows clipboard via clip.exe)"
    if sys.platform == "darwin":
        return "[OK] Clipboard backend: pbcopy (plus OSC 52)"
    found = find_clipboard_tool()
    if found is not None:
        return f"[OK] Clipboard backend: OSC 52, backed up by {found[0]} (found on PATH)"
    return ("[WARN] Clipboard backend: OSC 52 only -- no xclip/wl-copy/xsel on PATH for a fallback "
            "(Ctrl+C on a selection still works on terminals that relay OSC 52, but there's no "
            "backup if the terminal/multiplexer doesn't)")
