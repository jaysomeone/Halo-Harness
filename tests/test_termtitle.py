"""tests.test_termtitle -- Halo 2.0.2 W7 round 1 (brief F): the real OS/
terminal title re-assertion helpers in halo_harness.termtitle. Hermetic:
every test here monkeypatches the module's own OS-facing functions
(never calls a real ctypes SetConsoleTitleW, and never relies on which
real platform runs this suite) rather than mutating the actual terminal
window this process happens to be running in.
"""

from __future__ import annotations

import io
import sys

from tests.helpers.runner import new_registry

test, TESTS = new_registry()


@test
def test_emit_osc2_writes_the_expected_escape_sequence(ctx):
    """"a unit test for the print-mode save/restore sequence with a
    captured stdout" (brief F) starts here: the literal bytes OSC 2
    writes, captured via a plain StringIO stand-in for stdout."""
    from halo_harness import termtitle

    buf = io.StringIO()
    termtitle.emit_osc2("halo", stream=buf)
    ctx.check("OSC 2 wraps the title in ESC ]2;<title>BEL", buf.getvalue() == "\x1b]2;halo\x07")


@test
def test_emit_osc2_never_raises_on_a_broken_stream(ctx):
    from halo_harness import termtitle

    class _Broken:
        def write(self, _s):
            raise OSError("closed")

        def flush(self):
            raise OSError("closed")

    termtitle.emit_osc2("halo", stream=_Broken())  # must not raise
    ctx.check("a broken stream is swallowed, not raised", True)


@test
def test_set_terminal_title_calls_both_osc2_and_windows_fallback(ctx):
    """`stdout_is_interactive` is forced True here -- this test is about
    `set_terminal_title`'s own fan-out to both mechanisms, not about the
    isatty gate (covered separately below)."""
    from halo_harness import termtitle

    calls = {"osc2": None, "win": None}
    orig_osc2 = termtitle.emit_osc2
    orig_win = termtitle.set_windows_console_title
    orig_interactive = termtitle.stdout_is_interactive
    termtitle.emit_osc2 = lambda title, stream=None: calls.__setitem__("osc2", title)
    termtitle.set_windows_console_title = lambda title: calls.__setitem__("win", title)
    termtitle.stdout_is_interactive = lambda: True
    try:
        termtitle.set_terminal_title("halo")
    finally:
        termtitle.emit_osc2 = orig_osc2
        termtitle.set_windows_console_title = orig_win
        termtitle.stdout_is_interactive = orig_interactive
    ctx.check(f"emit_osc2 got 'halo', got {calls['osc2']!r}", calls["osc2"] == "halo")
    ctx.check(f"set_windows_console_title got 'halo', got {calls['win']!r}", calls["win"] == "halo")


@test
def test_set_terminal_title_skips_osc2_on_a_non_interactive_default_stdout(ctx):
    """The corruption-avoidance guard itself: `-p --output-format json`
    pipes stdout to a consumer parsing it as structured data -- a raw
    OSC 2 escape sequence spliced in would corrupt that parse, so the
    DEFAULT stream is never written to when it isn't a real terminal.
    The Windows ctypes fallback is a separate channel and still runs
    (it can never corrupt piped output)."""
    from halo_harness import termtitle

    calls = {"osc2": None, "win": None}
    orig_osc2 = termtitle.emit_osc2
    orig_win = termtitle.set_windows_console_title
    orig_interactive = termtitle.stdout_is_interactive
    termtitle.emit_osc2 = lambda title, stream=None: calls.__setitem__("osc2", title)
    termtitle.set_windows_console_title = lambda title: calls.__setitem__("win", title)
    termtitle.stdout_is_interactive = lambda: False
    try:
        termtitle.set_terminal_title("halo")
    finally:
        termtitle.emit_osc2 = orig_osc2
        termtitle.set_windows_console_title = orig_win
        termtitle.stdout_is_interactive = orig_interactive
    ctx.check(f"emit_osc2 was skipped on a non-interactive default stdout, got {calls['osc2']!r}",
              calls["osc2"] is None)
    ctx.check(f"set_windows_console_title still ran (separate channel), got {calls['win']!r}",
              calls["win"] == "halo")


@test
def test_set_terminal_title_always_writes_to_an_explicit_stream(ctx):
    """An explicitly-passed `stream` (a test's own buffer, today) is
    always honoured regardless of the real process's own stdout -- the
    isatty gate only ever applies to the implicit default."""
    from halo_harness import termtitle

    orig_interactive = termtitle.stdout_is_interactive
    termtitle.stdout_is_interactive = lambda: False
    try:
        buf = io.StringIO()
        termtitle.set_terminal_title("halo", stream=buf)
        ctx.check("an explicit stream is written to even when stdout is not interactive",
                  buf.getvalue() == "\x1b]2;halo\x07")
    finally:
        termtitle.stdout_is_interactive = orig_interactive


@test
def test_windows_console_functions_are_inert_off_windows(ctx):
    """Forces the off-Windows branch deliberately (restored right after)
    so this is deterministic on every box this suite runs on, including
    this one."""
    from halo_harness import termtitle

    orig_platform = termtitle.sys.platform
    termtitle.sys.platform = "linux"
    try:
        ctx.check("get_windows_console_title() is None off win32",
                  termtitle.get_windows_console_title() is None)
        termtitle.set_windows_console_title("should be a no-op")  # must not raise
        ctx.check("set_windows_console_title() is a no-op off win32 (no exception)", True)
    finally:
        termtitle.sys.platform = orig_platform


@test
def test_print_mode_guard_restores_the_captured_title_at_exit(ctx):
    """brief F: "restore the previous title at exit (OSC 2 with the
    saved title when the terminal reported one...)" -- the OS layer is
    faked out (a captured call list, a canned previous-title return), so
    this proves the GUARD's own save/set/restore SEQUENCING."""
    from halo_harness import termtitle

    sent = []
    orig_get, orig_set = termtitle.get_terminal_title, termtitle.set_terminal_title
    termtitle.get_terminal_title = lambda: "previous-window-title"
    termtitle.set_terminal_title = lambda title, stream=None: sent.append(title)
    try:
        guard = termtitle.PrintModeTitleGuard("halo")
        guard.start()
        ctx.check(f"start() set 'halo' first, got {sent!r}", sent == ["halo"])
        ctx.check("start() captured the previous title", guard.previous == "previous-window-title")
        guard.restore()
        ctx.check(f"restore() set the previous title back, got {sent!r}",
                  sent == ["halo", "previous-window-title"])
    finally:
        termtitle.get_terminal_title, termtitle.set_terminal_title = orig_get, orig_set


@test
def test_print_mode_guard_leaves_the_title_alone_when_nothing_was_captured(ctx):
    """brief F: "...otherwise leave it" -- a platform/terminal
    `get_terminal_title()` can't read from (every POSIX box today)
    returns None at start, and `restore()` must then do nothing."""
    from halo_harness import termtitle

    sent = []
    orig_get, orig_set = termtitle.get_terminal_title, termtitle.set_terminal_title
    termtitle.get_terminal_title = lambda: None
    termtitle.set_terminal_title = lambda title, stream=None: sent.append(title)
    try:
        guard = termtitle.PrintModeTitleGuard("halo")
        guard.start()
        guard.restore()
        ctx.check(f"only the start-time 'halo' set happened, got {sent!r}", sent == ["halo"])
    finally:
        termtitle.get_terminal_title, termtitle.set_terminal_title = orig_get, orig_set


@test
def test_print_mode_guard_as_a_context_manager(ctx):
    from halo_harness import termtitle

    sent = []
    orig_get, orig_set = termtitle.get_terminal_title, termtitle.set_terminal_title
    termtitle.get_terminal_title = lambda: "prev"
    termtitle.set_terminal_title = lambda title, stream=None: sent.append(title)
    try:
        with termtitle.PrintModeTitleGuard("halo"):
            ctx.check(f"entering set 'halo', got {sent!r}", sent == ["halo"])
        ctx.check(f"exiting restored 'prev', got {sent!r}", sent == ["halo", "prev"])
    finally:
        termtitle.get_terminal_title, termtitle.set_terminal_title = orig_get, orig_set


@test
def test_consume_claude_child_exit_fires_once_per_mark(ctx):
    from halo_harness import termtitle

    before = (termtitle._claude_child_exit_seen, termtitle._claude_child_exit_handled)
    try:
        termtitle._claude_child_exit_seen = termtitle._claude_child_exit_handled = 0
        ctx.check("nothing to consume yet", termtitle.consume_claude_child_exit() is False)
        termtitle.mark_claude_child_exited()
        ctx.check("one mark -> True exactly once", termtitle.consume_claude_child_exit() is True)
        ctx.check("a second call with no new mark -> False", termtitle.consume_claude_child_exit() is False)
        termtitle.mark_claude_child_exited()
        termtitle.mark_claude_child_exited()
        ctx.check("two marks before a consume still only -> True once",
                  termtitle.consume_claude_child_exit() is True)
        ctx.check("...and then False again", termtitle.consume_claude_child_exit() is False)
    finally:
        termtitle._claude_child_exit_seen, termtitle._claude_child_exit_handled = before


@test
def test_reassert_after_claude_child_marks_and_sets(ctx):
    from halo_harness import termtitle

    sent = []
    orig_set = termtitle.set_terminal_title
    termtitle.set_terminal_title = lambda title, stream=None: sent.append(title)
    before = (termtitle._claude_child_exit_seen, termtitle._claude_child_exit_handled)
    try:
        termtitle._claude_child_exit_seen = termtitle._claude_child_exit_handled = 0
        termtitle.reassert_after_claude_child()
        ctx.check(f"set_terminal_title('halo') ran synchronously, got {sent!r}", sent == ["halo"])
        ctx.check("and the exit was marked for the drain-tick consumer too",
                  termtitle.consume_claude_child_exit() is True)
    finally:
        termtitle.set_terminal_title = orig_set
        termtitle._claude_child_exit_seen, termtitle._claude_child_exit_handled = before


if __name__ == "__main__":
    from tests.helpers.runner import Ctx, print_results, run_all
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
