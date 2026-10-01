"""tests.test_debug_timeline -- 2.0.1 launch-hang investigation:
halo_harness/debug_timeline.py's opt-in `--debug` startup timeline
(settings, instructions, session build, MCP discovery, first paint), one
line per phase with milliseconds elapsed, to stderr and the "halo_harness"
DEBUG logger. No env vars/home dirs touched here -- just the module's own
tiny bit of global state, reset after every test.
"""
from __future__ import annotations

import contextlib
import io
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


@test
def test_disabled_by_default_mark_is_a_silent_no_op(ctx: Ctx):
    from halo_harness import debug_timeline
    debug_timeline.reset()
    try:
        ctx.check("disabled by default", debug_timeline.is_enabled() is False)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            debug_timeline.mark("settings")  # must be a true no-op: no clock read, no print
        ctx.check(f"nothing printed to stderr, got {buf.getvalue()!r}", buf.getvalue() == "")
    finally:
        debug_timeline.reset()


@test
def test_enable_then_mark_prints_a_timeline_line_with_milliseconds(ctx: Ctx):
    from halo_harness import debug_timeline
    debug_timeline.reset()
    try:
        debug_timeline.enable()
        ctx.check("enabled", debug_timeline.is_enabled() is True)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            debug_timeline.mark("settings")
        out = buf.getvalue()
        ctx.check(f"names the phase, got {out!r}", "[timeline] settings:" in out)
        ctx.check(f"carries a +<N>ms figure, got {out!r}", "+" in out and "ms" in out)
    finally:
        debug_timeline.reset()


@test
def test_mark_also_logs_to_the_halo_harness_debug_logger(ctx: Ctx):
    from halo_harness import debug_timeline
    debug_timeline.reset()
    logger = logging.getLogger("halo_harness")
    records: list = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = _Capture(level=logging.DEBUG)
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        debug_timeline.enable()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            debug_timeline.mark("mcp discovery")
        ctx.check(f"one DEBUG record logged, got {records}",
                  any("mcp discovery" in r for r in records))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        debug_timeline.reset()


@test
def test_reset_disables_and_a_second_enable_restarts_the_clock(ctx: Ctx):
    from halo_harness import debug_timeline
    debug_timeline.reset()
    try:
        debug_timeline.enable()
        ctx.check("enabled", debug_timeline.is_enabled() is True)
        debug_timeline.reset()
        ctx.check("disabled again", debug_timeline.is_enabled() is False)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            debug_timeline.mark("first paint")
        ctx.check("still a no-op after reset (enable() was never called again)", buf.getvalue() == "")
        debug_timeline.enable()
        buf2 = io.StringIO()
        with contextlib.redirect_stderr(buf2):
            debug_timeline.mark("first paint")
        ctx.check(f"works again after a fresh enable(), got {buf2.getvalue()!r}",
                  "[timeline] first paint:" in buf2.getvalue())
    finally:
        debug_timeline.reset()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
