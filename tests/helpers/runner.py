"""tests.helpers.runner -- the Ctx/@test/run_all/exit-code pattern, copied
in SPIRIT (not byte-for-byte -- test_bridge.py's own Ctx is proxy-specific:
BridgeProc/MockUpstream/dbx_bridge fixtures this suite has no use for) from
test_bridge.py's house style: stdlib only, a single global TESTS registry
per test MODULE (each test_*.py file gets its own registry+run_all via
`new_registry()`, so tests/run_all.py can import every test_*.py module and
run each one's tests without them colliding in one shared list), nonzero
exit on any real failure, gate on the exit code -- never on printed text.

Usage in a test_*.py file:

    from tests.helpers.runner import new_registry

    test, TESTS = new_registry()

    @test
    def test_something(ctx):
        ctx.check("1 == 1", 1 == 1)

    if __name__ == "__main__":
        import sys
        from tests.helpers.runner import Ctx, run_all, print_results
        ctx = Ctx()
        results, passed, failed, skipped = run_all(TESTS, ctx)
        sys.exit(print_results(results, passed, failed, skipped))
"""

from __future__ import annotations

import time
import traceback
from typing import Callable


class SkipTest(Exception):
    """Raise from inside a test to mark it SKIP (not FAIL) -- e.g. a
    fixture that needs something not present on this machine."""


class Ctx:
    """Minimal test context: a check() counter/assertion helper. Individual
    test_*.py files attach whatever fixtures they need as plain attributes
    (e.g. `ctx.home = ...`, `ctx.mock = ...`) rather than this class trying
    to anticipate every suite's fixture shape up front."""

    def __init__(self):
        self.checks = 0

    def check(self, description: str, condition: bool) -> None:
        self.checks += 1
        if not condition:
            raise AssertionError(description)


def new_registry():
    """Return a fresh (test_decorator, TESTS_list) pair, private to whatever
    module calls this -- each test_*.py file gets its own registry so
    importing several of them into tests/run_all.py never merges their
    test lists together."""
    registry: list = []

    def test(fn: Callable) -> Callable:
        registry.append((fn.__name__, fn))
        return fn

    return test, registry


def run_all(tests: list, ctx: Ctx):
    """Run every (name, fn) in `tests` against `ctx`, catching SkipTest ->
    SKIP, AssertionError -> FAIL (message only), anything else -> FAIL
    (with a short traceback). Returns (results, passed, failed, skipped)
    exactly like test_bridge.py's own run_all."""
    results = []
    passed = failed = skipped = 0
    for name, fn in tests:
        t0 = time.monotonic()
        try:
            fn(ctx)
            results.append((name, "PASS", "", time.monotonic() - t0))
            passed += 1
        except SkipTest as e:
            results.append((name, "SKIP", str(e), time.monotonic() - t0))
            skipped += 1
        except AssertionError as e:
            results.append((name, "FAIL", str(e), time.monotonic() - t0))
            failed += 1
        except Exception as e:
            detail = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=6)}"
            results.append((name, "FAIL", detail, time.monotonic() - t0))
            failed += 1
    return results, passed, failed, skipped


def print_results(results, passed: int, failed: int, skipped: int, *, label: str = "tests") -> int:
    """Print one line per test plus a RESULT summary line; returns the
    process exit code (0 iff failed == 0)."""
    for name, status, detail, dt in results:
        line = f"[{status}] {name} ({dt:.2f}s)"
        if detail and status != "PASS":
            line += f": {detail}"
        print(line)
    total = passed + failed + skipped
    print("-" * 74)
    print(f"RESULT: {total} {label}, {passed} passed, {failed} failed, {skipped} skipped")
    return 0 if failed == 0 else 1
