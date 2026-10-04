"""halo_harness.termtitle -- Halo 2.0.2 (W7 round 1, brief item F): the
REAL OS/terminal-emulator title (the console window/tab caption) -- a
different thing from Textual's own `BridgeApp.TITLE` (`tui/app.py`), which
is just the text its in-app Header widget renders, never anything sent to
the terminal itself.

Root cause, confirmed by reading this repo and Textual's own driver code:
Textual never emits an OSC 2 title sequence or calls `SetConsoleTitleW`
anywhere (grepped its installed package -- the only "title" hits are its
Header WIDGET, not a driver) -- so `BridgeApp.TITLE = "halo"` has never
touched the real terminal at all, on any platform. Every `claude`
subprocess this harness spawns (`agent/cc_process.ClaudeCodeProcess`,
`providers/cc_models.py`'s `auth status`/model-alias-refresh/`--version`
probes, `mcp/connectors.py`'s `mcp list`/discovery probe) pipes the
child's stdout/stderr for parsing, but never detaches it from the shared
console/tty (no `CREATE_NEW_CONSOLE`/`DETACHED_PROCESS`/`CREATE_NO_WINDOW`
on Windows, no new pty on POSIX -- only `CREATE_NEW_PROCESS_GROUP`/
`start_new_session`, which keep the child on the SAME console/tty and are
there for signal targeting, not isolation). A real `claude` binary sets
the console/terminal title itself at startup through a side channel
independent of the redirected stdio handles (Windows: the console title
is a property of the console object itself, settable by any attached
process regardless of which handle its own stdout points at; POSIX: an
OSC 2 write most terminals honour however it arrives) and never restores
it, so the tab/window is left reading "claude" after the child exits --
nothing in halo ever re-asserted its own title afterward, on any
platform, until this module.
"""

from __future__ import annotations

import sys
from typing import Optional

_OSC_TITLE_FMT = "\x1b]2;{}\x07"


_tui_driver = None  # Textual `App._driver` while the TUI runs; None otherwise.


def set_tui_driver(driver) -> None:
    """Called once the TUI mounts (alongside `activate_tui_mode`, same
    call site) with `App._driver`; `None` again on unmount. 2.0.2 review
    finding 31: while this is set, `emit_osc2`'s DEFAULT-stream path
    (never an explicitly-passed `stream`, which stays a deliberate direct
    write -- e.g. print mode's `PrintModeTitleGuard`, which has no driver
    at all) routes the OSC sequence through the driver's own `write()`
    instead of writing `sys.__stdout__` directly from whatever thread
    happens to call `set_terminal_title` -- Textual 8 writes every FRAME
    from its own `textual-output` WriterThread (verified: both
    `linux_driver.py` and `windows_driver.py` write through `self._writer_
    thread.write`), never the UI thread this module used to assume "also
    owns Textual's terminal writes." `driver.write()` queues onto that
    SAME thread, so the title OSC is ordered with frames instead of
    racing them."""
    global _tui_driver
    _tui_driver = driver


def emit_osc2(title: str, *, stream=None) -> None:
    """Best-effort OSC 2 title write -- every terminal emulator that
    tracks a tab/window title honours this (Windows Terminal, GNOME
    Terminal, tmux, iTerm2, ...); a plain pipe or a legacy `conhost`
    window that never learned OSC 2 just ignores the bytes. Never
    raises: a closed/non-writable `stream` is exactly "no terminal is
    watching," not an error worth surfacing."""
    if stream is None and _tui_driver is not None:
        try:
            _tui_driver.write(_OSC_TITLE_FMT.format(title))
        except Exception:
            pass
        return
    s = stream if stream is not None else _real_stdout()
    try:
        s.write(_OSC_TITLE_FMT.format(title))
        s.flush()
    except Exception:
        pass


def _real_stdout():
    """The process's ORIGINAL stdout (`sys.__stdout__`). Found live on the
    Kali VM: Textual replaces `sys.stdout` with its own print capture for
    the whole `App.run()`, so a title written there never reached the
    terminal (the tmux pane title stayed on the host name) and
    `sys.stdout.isatty()` read False, which skipped the write anyway.
    Textual's own drivers write to `sys.__stdout__`, so that is the
    stream the terminal is actually watching."""
    return sys.__stdout__ if sys.__stdout__ is not None else sys.stdout


def set_windows_console_title(title: str) -> None:
    """`ctypes.windll.kernel32.SetConsoleTitleW` -- the fallback for a
    legacy `conhost` window that never honours OSC 2 (brief F: "the
    fallback when OSC is not honoured"). Runs unconditionally alongside
    `emit_osc2` rather than trying to detect which mechanism a given
    console actually needs -- both are idempotent and harmless to call
    together. A no-op off Windows, or when no real console is attached
    (e.g. fully redirected output with no console at all)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(str(title))
    except Exception:
        pass


def get_windows_console_title() -> Optional[str]:
    """The console's CURRENT title via `GetConsoleTitleW`, for print
    mode's own save/restore -- `None` off Windows, or when no real
    console is attached (a caller treats `None` as "nothing to restore,
    leave it alone")."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetConsoleTitleW(buf, 1024)
        return buf.value if n else None
    except Exception:
        return None


def stdout_is_interactive() -> bool:
    """True only when THIS process's real `sys.stdout` is a live
    terminal -- the guard `set_terminal_title`'s own DEFAULT stream
    (never an explicitly-passed one, which is always a deliberate
    choice -- e.g. a test's own buffer) relies on before writing an OSC
    2 sequence, so a `-p --output-format json/stream-json` run (or any
    other piped/redirected consumer parsing stdout as structured data)
    never gets a raw escape sequence spliced into it -- no well-behaved
    CLI tool emits terminal control codes onto a non-tty stdout, for
    exactly this reason. `set_windows_console_title`'s ctypes call is a
    SEPARATE channel (it talks to the console object directly, never
    through the stdout file descriptor) and is unaffected by this --
    it can never corrupt piped/redirected output, so `set_terminal_
    title` below still calls it unconditionally even when this is
    False (the console's own chrome still gets re-titled correctly even
    while `-p ... > out.json` keeps the file itself byte-clean)."""
    try:
        return bool(_real_stdout().isatty())
    except Exception:
        return False


def set_terminal_title(title: str = "halo", *, stream=None) -> None:
    """The one call every spawn-site hook and TUI lifecycle point makes:
    OSC 2 (every modern terminal, POSIX and Windows Terminal alike) AND,
    on Windows, the legacy-conhost ctypes fallback. The OSC 2 write is
    unconditional for an explicitly-passed `stream`; for the default
    (this process's own stdout) it only happens when `stdout_is_
    interactive()` -- see that function's own docstring for why. Both
    writes are best-effort and neither ever raises."""
    if stream is not None or stdout_is_interactive():
        emit_osc2(title, stream=stream)
    set_windows_console_title(title)


def get_terminal_title() -> Optional[str]:
    """The best CURRENT-title read this process can manage. Windows:
    `GetConsoleTitleW`. POSIX has no portable, stdlib-only way to query a
    terminal's title without a risky OSC-2-query-and-read-stdin dance (it
    would need raw tty mode and a timeout, and could desync a real
    interactive session) -- `None` there, same as "no real console
    attached," so print mode's save/restore just leaves the title alone
    at exit on that platform (brief F: "otherwise leave it")."""
    return get_windows_console_title()


_claude_child_exit_seen = 0
_claude_child_exit_handled = 0


def mark_claude_child_exited() -> None:
    """Called right after EVERY `claude` subprocess spawn site observes
    its child has exited -- a monotonically increasing counter a drain
    loop (or a synchronous one-shot caller) can notice and react to
    exactly once per exit, independent of `reassert_after_claude_child`
    below (which ALSO calls this -- the two are separate hooks only so a
    caller that wants just the bookkeeping, e.g. a future drain-loop-only
    integration, can use one without the other)."""
    global _claude_child_exit_seen
    _claude_child_exit_seen += 1


def consume_claude_child_exit() -> bool:
    """True the first time this is called after `mark_claude_child_
    exited()` ran at least once since the PREVIOUS call -- `tui/app.py`'s
    `_drain()` tick calls this every 1/DRAIN_HZ seconds and re-asserts
    `halo` only when it comes back True, so the re-assert happens ON the
    drain tick that follows a child exit (brief F), not continuously."""
    global _claude_child_exit_handled
    if _claude_child_exit_handled == _claude_child_exit_seen:
        return False
    _claude_child_exit_handled = _claude_child_exit_seen
    return True


def reassert_after_claude_child(title: str = "halo") -> None:
    """The one-line hook every `claude`-spawning call site adds right
    after it observes its child has exited -- marks the exit AND
    immediately re-asserts `title` synchronously (print mode, and a
    one-shot probe like `claude auth status`, have no drain loop to catch
    up on the next tick, so this must not wait for one). The TUI's own
    drain-tick re-assert (`consume_claude_child_exit`, wired in
    `tui/app.py::_drain`) is additional insurance for a title write that
    lands slightly after this function already returned."""
    mark_claude_child_exited()
    if _tui_active:
        # A running TUI re-asserts from its own drain tick (`tui/app.py::
        # _drain` -> `consume_claude_child_exit`), on the UI thread that
        # also owns Textual's terminal writes. Spawn-site hooks run on
        # worker threads, and a title write from there could land inside
        # one of Textual's own frames.
        return
    set_terminal_title(title)


_tui_active = False


def activate_tui_mode() -> None:
    """Called by the TUI once mounted: from here on, spawn-site hooks only
    record a child exit and the drain tick does the re-assert."""
    global _tui_active
    _tui_active = True


def deactivate_tui_mode() -> None:
    global _tui_active
    _tui_active = False


class PrintModeTitleGuard:
    """Print mode's own "set `halo` at start, restore whatever was there
    at exit" (brief F) as one small object `headless.run_print_mode`'s
    several early-return paths and its existing outer `finally` can all
    share without needing to re-indent the whole function into a `with`
    block. `restore()` is a no-op when nothing was captured at start
    (POSIX, or no real console attached) -- brief: "otherwise leave it"."""

    def __init__(self, title: str = "halo"):
        self.title = title
        self.previous: Optional[str] = None

    def start(self) -> "PrintModeTitleGuard":
        self.previous = get_terminal_title()
        set_terminal_title(self.title)
        return self

    def restore(self) -> None:
        if self.previous is not None:
            set_terminal_title(self.previous)

    # Also usable as a real context manager (e.g. from a test).
    def __enter__(self) -> "PrintModeTitleGuard":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.restore()
