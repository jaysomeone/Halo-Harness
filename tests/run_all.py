"""tests/run_all.py -- discovers every tests/test_*.py module, runs each
one's own TESTS registry (see tests/helpers/runner.py) against a fresh Ctx,
and prints one combined RESULT line. Exit code 0 iff every test in every
module passed.

Run:
    python tests/run_all.py ; echo exit=$?
"""
from __future__ import annotations

import importlib
import os
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


# H15 Part D2.1: every env var name a test module's own `_Env` helper is
# supposed to scope away (house rule: "every _Env helper must snapshot and
# restore EVERY provider variable it or its tests touch"). Prefix-matched,
# not a fixed list, so a future provider var is covered automatically.
_GUARDED_ENV_PREFIXES = ("BRIDGE_", "OPENROUTER_", "DATABRICKS_", "ANTHROPIC_", "TYPESAFE_")


def _snapshot_guarded_env() -> dict:
    return {k: v for k, v in os.environ.items() if k.startswith(_GUARDED_ENV_PREFIXES)}


def _restore_guarded_env(snapshot: dict) -> None:
    """D2.1: taken/restored around EACH module's run below (not just once
    for the whole suite) -- the actual H15 fix-pass finding was that many
    test modules build a real `Session`/`Controller`/`build_session` from a
    `build_fake_home()` cwd without scoping `BRIDGE_STATE_DIR` themselves,
    and only ever avoided writing session FILES into the real
    `~/.rolo-claude` because an EARLIER module happened to leave
    `BRIDGE_TEST_HOME` (or `BRIDGE_STATE_DIR`) set in `os.environ` --
    "only avoid it by accident of import order" is exactly the bug D2.1
    asks to close. Restoring this snapshot after every module -- success,
    failure, or import error alike -- makes "never relying on module
    order" true at the HARNESS level: whatever a module leaves behind
    (an incomplete `_Env.__exit__`, an exception that skipped cleanup, no
    `_Env` at all) can never reach the NEXT module's own `home()`/
    `bridge_home()` resolution, regardless of that module's own hygiene."""
    for k in list(os.environ):
        if k.startswith(_GUARDED_ENV_PREFIXES) and k not in snapshot:
            os.environ.pop(k, None)
    for k, v in snapshot.items():
        os.environ[k] = v


def _real_sessions_snapshot() -> "set[str]":
    """H10b: the REAL sessions dir -- literal `Path.home()`, deliberately
    NOT `rolo_claude.config.paths.bridge_home()` (which would honor
    whatever BRIDGE_TEST_HOME/BRIDGE_STATE_DIR a test left set in THIS
    process and so could be fooled into snapshotting a fake home instead).
    This matches `bridge_home()`'s own fallback branch exactly: a
    well-behaved test scopes its session log away from here entirely, so
    the set below should never gain an entry across a whole suite run. It
    did (H10b report: two fuzz-harness engines and three test files built a
    real in-process Session without ever setting the seam, leaking
    `or:mock/page-forever`/`ant:claude-h9fuzz-ant-*`/etc. sessions into
    rolo's actual session history) -- this is the suite-level guard against
    that ever happening again unnoticed, independent of any one test's own
    hygiene.

    H15 Part D2.2: ALSO every slug DIRECTORY directly under `sessions/`,
    not only `*.jsonl` FILES -- a test that builds a real `Session`/
    `Controller`/`build_session` from a `BRIDGE_TEST_HOME`-derived cwd
    without ALSO scoping `BRIDGE_STATE_DIR` away from here never writes a
    session file here (an earlier module happening to leave
    `BRIDGE_TEST_HOME` set is what prevented that), but `SessionLog`'s own
    `mkdir(parents=True)` still creates the empty slug directory the
    moment the Session exists -- 1972 such directories were found on the
    Windows build host during the 1.0.1 fix pass, all invisible to the old
    glob (which only ever looked two levels down for a `.jsonl` suffix)."""
    d = Path.home() / ".rolo-claude" / "sessions"
    try:
        if not d.is_dir():
            return set()
        paths = {str(p) for p in d.glob("*/*.jsonl")}
        paths |= {str(p) for p in d.iterdir() if p.is_dir()}
        return paths
    except OSError:
        return set()


def main() -> int:
    # NEW (post-H9 acceptance): see runner.py's own docstring on this pair
    # -- every tempfile.mkdtemp() call for the rest of this process (every
    # test module this run imports/executes) is tracked and swept up once,
    # at the very end, instead of leaking a directory per call forever.
    install_temp_dir_tracking()
    real_sessions_before = _real_sessions_snapshot()
    module_names = discover_test_modules()
    total_passed = total_failed = total_skipped = 0
    any_module_import_failed = False

    for mod_name in module_names:
        print(f"=== {mod_name} ===")
        # D2.1: snapshotted BEFORE this module touches anything, restored
        # in EVERY exit path below (import error, no TESTS registry, or a
        # normal run) -- see _restore_guarded_env's own docstring.
        env_before_module = _snapshot_guarded_env()
        try:
            module = importlib.import_module(mod_name)
        except Exception:
            print(f"[IMPORT ERROR] {mod_name} could not be imported:")
            traceback.print_exc()
            any_module_import_failed = True
            total_failed += 1
            _restore_guarded_env(env_before_module)
            continue

        tests = getattr(module, "TESTS", None)
        if tests is None:
            print(f"[SKIP MODULE] {mod_name} has no TESTS registry (not a runner-based test file)")
            _restore_guarded_env(env_before_module)
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
        _restore_guarded_env(env_before_module)

    total = total_passed + total_failed + total_skipped
    print("-" * 74)
    print(f"RESULT: {total} tests across {len(module_names)} modules -- "
          f"{total_passed} passed, {total_failed} failed, {total_skipped} skipped")

    removed = cleanup_tracked_temp_dirs()
    print(f"[cleanup] removed {removed} tracked temp dir(s)")

    real_sessions_leaked = sorted(_real_sessions_snapshot() - real_sessions_before)
    if real_sessions_leaked:
        print("-" * 74)
        print(f"[REAL SESSIONS GUARD] FAIL: {len(real_sessions_leaked)} new file(s) appeared under "
              f"the REAL ~/.rolo-claude/sessions during this run -- some test built a Session "
              f"without scoping BRIDGE_TEST_HOME/BRIDGE_STATE_DIR away from the real machine state:")
        for p in real_sessions_leaked:
            print(f"    {p}")
    else:
        print("[REAL SESSIONS GUARD] ok -- no new files under the real ~/.rolo-claude/sessions")

    return 0 if (total_failed == 0 and not any_module_import_failed and not real_sessions_leaked) else 1


if __name__ == "__main__":
    sys.exit(main())
