"""rolo_claude.tui.clipboard -- clipboard fallback for Linux terminals (U5
scope E). Textual's own `App.copy_to_clipboard` already emits an OSC 52
escape sequence (works over SSH, tmux-passthrough permitting), which is
`tui/app.py`'s primary mechanism (review finding 16). Some terminals/multi-
plexer configs don't relay OSC 52 at all -- this module is the belt-and-
suspenders fallback: best-effort, also pipe the text into `xclip`/`wl-copy`
if either is on PATH, so at least ONE mechanism lands the text on the
system clipboard. Pure subprocess plumbing, no textual import, so it's
unit-testable without a running App.

`doctor.py` is H8's module (docs/harness/H8-brief.md lists it under H8's
own files) -- rather than edit a contested file, `clipboard_doctor_line()`
below is the ready-to-call check; H8 (or a future pass) can add one line to
`doctor.run_checks()`: `lines.append(clipboard_doctor_line())`.
"""

from __future__ import annotations

import shutil
import subprocess
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


def clipboard_doctor_line() -> str:
    """One `doctor`-style line reporting whether an external clipboard
    fallback tool is available (OSC 52 itself is always attempted first
    and needs no external tool, so this is purely informational -- never
    a MISSING/failure line)."""
    found = find_clipboard_tool()
    if found is not None:
        return f"[OK] Clipboard fallback: {found[0]} found (OSC 52 is tried first; this backs it up)"
    return ("[WARN] Clipboard fallback: no xclip/wl-copy/xsel on PATH -- OSC 52 (Ctrl+C on a "
            "selection) still works on terminals that support it, but there's no fallback if not")
