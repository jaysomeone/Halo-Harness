"""tests/run_all.py -- discovers every tests/test_*.py module, runs each
one's own TESTS registry (see tests/helpers/runner.py) against a fresh Ctx,
and prints one combined RESULT line. Exit code 0 iff every test in every
module passed.

Run:
    python tests/run_all.py ; echo exit=$?
"""
from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent

if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from tests.helpers.runner import Ctx, cleanup_tracked_temp_dirs, install_temp_dir_tracking, run_all


def discover_test_modules() -> list:
    names = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        names.append(f"tests.{path.stem}")
    return names


def main() -> int:
    # NEW (post-H9 acceptance): see runner.py's own docstring on this pair
    # -- every tempfile.mkdtemp() call for the rest of this process (every
    # test module this run imports/executes) is tracked and swept up once,
    # at the very end, instead of leaking a directory per call forever.
    install_temp_dir_tracking()
    module_names = discover_test_modules()
    total_passed = total_failed = total_skipped = 0
    any_module_import_failed = False

    for mod_name in module_names:
        print(f"=== {mod_name} ===")
        try:
            module = importlib.import_module(mod_name)
        except Exception:
            print(f"[IMPORT ERROR] {mod_name} could not be imported:")
            traceback.print_exc()
            any_module_import_failed = True
            total_failed += 1
            continue

        tests = getattr(module, "TESTS", None)
        if tests is None:
            print(f"[SKIP MODULE] {mod_name} has no TESTS registry (not a runner-based test file)")
            continue

        ctx = Ctx()
        results, passed, failed, skipped = run_all(tests, ctx)
        for name, status, detail, dt in results:
            line = f"  [{status}] {name} ({dt:.2f}s)"
            if detail and status != "PASS":
                line += f": {detail}"
            print(line)
        total_passed += passed
        total_failed += failed
        total_skipped += skipped

    total = total_passed + total_failed + total_skipped
    print("-" * 74)
    print(f"RESULT: {total} tests across {len(module_names)} modules -- "
          f"{total_passed} passed, {total_failed} failed, {total_skipped} skipped")

    removed = cleanup_tracked_temp_dirs()
    print(f"[cleanup] removed {removed} tracked temp dir(s)")

    return 0 if (total_failed == 0 and not any_module_import_failed) else 1


if __name__ == "__main__":
    sys.exit(main())
