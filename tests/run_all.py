"""tests/run_all.py -- discovers every tests/test_*.py module, runs each
one's own TESTS registry (see tests/helpers/runner.py) against a fresh Ctx,
and prints one combined RESULT line. Exit code 0 iff every test in every
module passed.

Run:
    python tests/run_all.py ; echo exit=$?
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
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
# 2.0.0 rename: `HALO_` added alongside the pre-existing `BRIDGE_` -- the
# new canonical prefix for every harness-owned knob (env_compat), guarded
# here the same way even though nothing sets one via real os.environ yet.
_GUARDED_ENV_PREFIXES = ("BRIDGE_", "HALO_", "OPENROUTER_", "DATABRICKS_", "ANTHROPIC_", "TYPESAFE_")


def _snapshot_guarded_env() -> dict:
    return {k: v for k, v in os.environ.items() if k.startswith(_GUARDED_ENV_PREFIXES)}


def _restore_guarded_env(snapshot: dict) -> None:
    """D2.1: taken/restored around EACH module's run below (not just once
    for the whole suite) -- the actual H15 fix-pass finding was that many
    test modules build a real `Session`/`Controller`/`build_session` from a
    `build_fake_home()` cwd without scoping `BRIDGE_STATE_DIR` themselves,
    and only ever avoided writing session FILES into the real
    `~/.halo` because an EARLIER module happened to leave
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


def _clear_halo_env_vars() -> None:
    """2.0.0 fixpass finding 2: a stray `HALO_*` left set in the developer's
    OWN shell (the new canonical prefix every harness knob now checks
    FIRST, per env_compat) would silently out-rank whatever legacy
    `BRIDGE_*` value an individual test deliberately injects to control a
    real Session/Controller it builds -- cleared ONCE, for the whole run,
    before any test module is even imported."""
    for key in [k for k in os.environ if k.startswith("HALO_")]:
        os.environ.pop(key, None)


def _pop_stray_bridge_state_dir() -> None:
    """2.0.0 fixpass item G: a stray `BRIDGE_STATE_DIR` left in the
    developer's OWN shell would make `_ensure_whole_run_state_dir` below
    silently no-op (it treats ANY pre-set `BRIDGE_TEST_HOME`/
    `BRIDGE_STATE_DIR` as deliberate external scoping) while never setting
    `BRIDGE_TEST_HOME` -- leaving every `home()`-based path (`~/.claude.
    json`, `~/.config/halo/env`, ...) pointed at the REAL machine home even
    though `bridge_home()` itself would still be safely scoped by the
    stray var. Popped here, before `_ensure_whole_run_state_dir` runs and
    before any test module is even imported, same timing as
    `_clear_halo_env_vars`'s own HALO_* sweep just above. `BRIDGE_TEST_
    HOME` is deliberately never touched here -- an outer caller that
    legitimately wants a specific scratch home used for this whole run
    sets THAT, and it must survive untouched."""
    os.environ.pop("BRIDGE_STATE_DIR", None)


def _ensure_whole_run_state_dir() -> None:
    """2.0.0 fixpass finding 2: a module that builds a real Session/
    Controller/BridgeApp without scoping BRIDGE_TEST_HOME/BRIDGE_STATE_DIR
    ITSELF used to only ever avoid the REAL `~/.halo` by accident of import
    order (whatever an EARLIER module happened to leave set) -- this sets
    both, once, before the FIRST module is imported, so "never touching
    the real state dir" no longer depends on which module happens to run
    first. A no-op when either is already set (an external caller's own
    explicit scoping always wins)."""
    if "BRIDGE_TEST_HOME" in os.environ or "BRIDGE_STATE_DIR" in os.environ:
        return
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="run-all-wholerun-home-")))


def _ensure_whole_run_test_seams() -> None:
    """W6b section E: a module that never sets `BRIDGE_TEST_NO_BACKGROUND_
    NET`/`BRIDGE_TEST_CC_AUTH_STATUS` itself -- most don't; only the ones
    that deliberately exercise the claude.ai/cc: auth path do, see
    `tests/helpers/provider_env_defaults.ensure_default_provider_
    credentials` -- used to leave `claude_auth_status()` and claude.ai
    connector discovery free to spawn a REAL `claude auth status`/`claude
    mcp list` against whatever `claude` happens to be on this box's PATH.
    Found on a WSL box with a logged-in native `claude`: `tests/
    test_doctor_mcp_config_cli.py::test_mcp_list_no_servers` (a module that
    never calls `ensure_default_provider_credentials`) took 22s instead of
    ~1s, because its own `_hermetic_child_env()` forwards a plain copy of
    `os.environ` to the `halo mcp list` child it spawns, and neither var
    was set anywhere in that process tree. Setting both here, once, before
    the FIRST module is even imported, closes this for every module
    (compliant or not) and every child any of them spawns through a plain
    `dict(os.environ)` copy -- `setdefault` so a module/test that wants the
    logged-in path by setting `BRIDGE_TEST_CC_AUTH_STATUS` itself (`tests/
    test_w5b_connector_cold_start.py::_make_eligible`) is never overridden,
    and the Kali real-`claude`-binary interop tests (`tests/
    test_mcp_compat_matrix.py` items 6-8) are unaffected -- the real
    `claude` binary never reads either var, and nothing those tests check
    depends on claude.ai connector auth."""
    os.environ.setdefault("BRIDGE_TEST_NO_BACKGROUND_NET", "1")
    os.environ.setdefault("BRIDGE_TEST_CC_AUTH_STATUS", json.dumps({"loggedIn": False}))


def _real_state_dir_snapshot() -> dict:
    """2.0.0 fixpass finding 2 / the fixpass brief's own safety rule: the
    REAL (literal `Path.home()`, never a test-scoped one) `~/.rolo-claude`
    and `~/.halo` directories, and the exact byte size of each one's
    `history.jsonl` if present -- recorded before this run and compared
    again after, so ANY leftover test that still manages to touch the real
    machine's own state is caught even if it never writes a session file
    under `sessions/` (what `_real_sessions_snapshot` alone would catch)."""
    snap = {}
    for label, d in (("rolo_claude", Path.home() / ".rolo-claude"), ("halo", Path.home() / ".halo")):
        snap[f"{label}_dir_exists"] = d.is_dir()
        history = d / "history.jsonl"
        try:
            snap[f"{label}_history_size"] = history.stat().st_size if history.exists() else None
        except OSError:
            snap[f"{label}_history_size"] = None
    return snap


def _real_state_dir_diff(before: dict, after: dict) -> "list[str]":
    problems = []
    for label, pretty in (("rolo_claude", "~/.rolo-claude"), ("halo", "~/.halo")):
        if not before[f"{label}_dir_exists"] and after[f"{label}_dir_exists"]:
            problems.append(f"{pretty} did not exist before this run and does now")
        b_size, a_size = before[f"{label}_history_size"], after[f"{label}_history_size"]
        if b_size != a_size:
            problems.append(f"{pretty}/history.jsonl size changed: {b_size!r} -> {a_size!r}")
    return problems


def _real_sessions_snapshot() -> "set[str]":
    """H10b: the REAL sessions dir -- literal `Path.home()`, deliberately
    NOT `halo_harness.config.paths.bridge_home()` (which would honor
    whatever BRIDGE_TEST_HOME/BRIDGE_STATE_DIR a test left set in THIS
    process and so could be fooled into snapshotting a fake home instead).
    This matches `bridge_home()`'s own fallback branch exactly: a
    well-behaved test scopes its session log away from here entirely, so
    the set below should never gain an entry across a whole suite run. It
    did (H10b report: two fuzz-harness engines and three test files built a
    real in-process Session without ever setting the seam, leaking
    `or:mock/page-forever`/`ant:claude-h9fuzz-ant-*`/etc. sessions into
    the owner's actual session history) -- this is the suite-level guard against
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
    glob (which only ever looked two levels down for a `.jsonl` suffix).

    2.0.0 rename: BOTH the new default (`~/.halo/sessions`) and the legacy
    one (`~/.rolo-claude/sessions`) are unioned into one snapshot -- a box
    that hasn't launched the new build even once yet still keeps its real
    history under the old name (`bridge_home()`'s own one-time migration
    only fires the first time something actually resolves it), so a test
    leaking a session into either real directory must still be caught."""
    paths: "set[str]" = set()
    for d in (Path.home() / ".halo" / "sessions", Path.home() / ".rolo-claude" / "sessions"):
        try:
            if not d.is_dir():
                continue
            paths |= {str(p) for p in d.glob("*/*.jsonl")}
            paths |= {str(p) for p in d.iterdir() if p.is_dir()}
        except OSError:
            continue
    return paths


def main() -> int:
    # Line-buffered output even when redirected to a file: a run that is
    # killed mid-way must leave its last real line on disk, not a stale
    # module header from a block buffer (that misled a Kali investigation).
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    # NEW (post-H9 acceptance): see runner.py's own docstring on this pair
    # -- every tempfile.mkdtemp() call for the rest of this process (every
    # test module this run imports/executes) is tracked and swept up once,
    # at the very end, instead of leaking a directory per call forever.
    install_temp_dir_tracking()
    # 2.0.0 fixpass finding 2 / item G: hermetic from the very first import
    # onward -- see each helper's own docstring. The BRIDGE_STATE_DIR pop
    # runs BEFORE _ensure_whole_run_state_dir, which would otherwise treat
    # a stray leftover value as deliberate external scoping and skip
    # setting its own BRIDGE_TEST_HOME.
    _clear_halo_env_vars()
    _pop_stray_bridge_state_dir()
    _ensure_whole_run_state_dir()
    _ensure_whole_run_test_seams()
    real_state_before = _real_state_dir_snapshot()
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
              f"the REAL ~/.halo/sessions or ~/.rolo-claude/sessions during this run -- some test "
              f"built a Session without scoping BRIDGE_TEST_HOME/BRIDGE_STATE_DIR away from the "
              f"real machine state:")
        for p in real_sessions_leaked:
            print(f"    {p}")
    else:
        print("[REAL SESSIONS GUARD] ok -- no new files under the real ~/.halo/sessions or ~/.rolo-claude/sessions")

    # 2.0.0 fixpass finding 2: catches what the sessions-only guard above
    # wouldn't -- a real ~/.rolo-claude/~/.halo springing into existence
    # with no sessions/ underneath yet, or (the exact box this brief names)
    # a real history.jsonl growing from a TUI test that never scoped its
    # own state dir.
    real_state_problems = _real_state_dir_diff(real_state_before, _real_state_dir_snapshot())
    if real_state_problems:
        print("-" * 74)
        print(f"[REAL STATE DIR GUARD] FAIL: the real machine's own state changed during this run:")
        for p in real_state_problems:
            print(f"    {p}")
    else:
        print("[REAL STATE DIR GUARD] ok -- ~/.rolo-claude and ~/.halo (existence + history.jsonl size) unchanged")

    return 0 if (total_failed == 0 and not any_module_import_failed
                 and not real_sessions_leaked and not real_state_problems) else 1


if __name__ == "__main__":
    sys.exit(main())
