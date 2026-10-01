"""tests.test_run_all_hermeticity -- 2.0.0 fixpass finding 2: pinning tests
for tests/run_all.py's own hermeticity helpers (clearing stray HALO_* vars
and scoping a whole-run BRIDGE_TEST_HOME/BRIDGE_STATE_DIR before the FIRST
test module is even imported, plus the real-state-dir guard that fails the
run if the REAL ~/.rolo-claude or ~/.halo changes at all). Unit-tested
directly against the helper functions rather than through a real `python
tests/run_all.py` subprocess, which would be slow and -- for the stray-
HALO_*-in-the-parent-shell scenario specifically -- impossible to observe
from outside that subprocess's own (disposable) environment.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
import tests.run_all as run_all_mod

test, TESTS = new_registry()


@test
def test_clear_halo_env_vars_removes_only_halo_prefixed_names(ctx: Ctx):
    saved = {k: os.environ.get(k) for k in ("HALO_MODEL", "HALO_PING_INTERVAL", "BRIDGE_MODEL")}
    try:
        os.environ["HALO_MODEL"] = "should-be-cleared"
        os.environ["HALO_PING_INTERVAL"] = "should-also-be-cleared"
        os.environ["BRIDGE_MODEL"] = "left-alone"
        run_all_mod._clear_halo_env_vars()
        ctx.check("HALO_MODEL cleared", "HALO_MODEL" not in os.environ)
        ctx.check("HALO_PING_INTERVAL cleared", "HALO_PING_INTERVAL" not in os.environ)
        ctx.check("a non-HALO_ var is left alone", os.environ.get("BRIDGE_MODEL") == "left-alone")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_pop_stray_bridge_state_dir_removes_it_and_leaves_test_home_alone(ctx: Ctx):
    """2.0.0 fixpass item G: a stray BRIDGE_STATE_DIR left in the parent
    shell must be gone before _ensure_whole_run_state_dir runs -- otherwise
    that helper would treat it as deliberate external scoping and skip
    setting its own BRIDGE_TEST_HOME, leaving home()-based paths pointed at
    the real machine home. BRIDGE_TEST_HOME itself is a DIFFERENT var and
    must never be touched by this one."""
    saved = {k: os.environ.get(k) for k in ("BRIDGE_STATE_DIR", "BRIDGE_TEST_HOME")}
    try:
        os.environ["BRIDGE_STATE_DIR"] = "/tmp/stray-leftover-from-parent-shell/.halo"
        os.environ["BRIDGE_TEST_HOME"] = "/tmp/a-legitimately-scoped-outer-home"
        run_all_mod._pop_stray_bridge_state_dir()
        ctx.check("BRIDGE_STATE_DIR is gone", "BRIDGE_STATE_DIR" not in os.environ)
        ctx.check("BRIDGE_TEST_HOME is left completely alone",
                  os.environ.get("BRIDGE_TEST_HOME") == "/tmp/a-legitimately-scoped-outer-home")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_ensure_whole_run_state_dir_sets_once_never_clobbers(ctx: Ctx):
    saved = {k: os.environ.get(k) for k in ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR")}
    try:
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_STATE_DIR", None)
        run_all_mod._ensure_whole_run_state_dir()
        ctx.check("BRIDGE_TEST_HOME is now set", "BRIDGE_TEST_HOME" in os.environ)
        first = os.environ["BRIDGE_TEST_HOME"]

        run_all_mod._ensure_whole_run_state_dir()
        ctx.check("a second call never clobbers the first value", os.environ["BRIDGE_TEST_HOME"] == first)

        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ["BRIDGE_STATE_DIR"] = str(Path(tempfile.gettempdir()) / "h2-explicit-state-dir")
        run_all_mod._ensure_whole_run_state_dir()
        ctx.check("an externally-set BRIDGE_STATE_DIR alone is enough to skip the default",
                  "BRIDGE_TEST_HOME" not in os.environ)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_real_state_dir_diff_flags_a_history_size_change_and_a_new_directory(ctx: Ctx):
    before = {"rolo_claude_dir_exists": False, "rolo_claude_history_size": None,
              "halo_dir_exists": True, "halo_history_size": 100}
    same = dict(before)
    ctx.check("identical snapshots -> no problems", run_all_mod._real_state_dir_diff(before, same) == [])

    grew = dict(before)
    grew["halo_history_size"] = 108
    problems = run_all_mod._real_state_dir_diff(before, grew)
    ctx.check(f"a history.jsonl size change is flagged, got {problems}",
              any("history.jsonl size changed" in p and "~/.halo" in p for p in problems))

    appeared = dict(before)
    appeared["rolo_claude_dir_exists"] = True
    problems2 = run_all_mod._real_state_dir_diff(before, appeared)
    ctx.check(f"a newly-appeared real dir is flagged, got {problems2}",
              any("did not exist" in p and "~/.rolo-claude" in p for p in problems2))


@test
def test_real_state_dir_snapshot_matches_literal_home_never_a_scoped_one(ctx: Ctx):
    """H10b's own point, extended to this new snapshot (finding 2): it must
    read the REAL machine home unconditionally, never whatever
    BRIDGE_TEST_HOME a test currently has set -- otherwise a leaky test
    could fool the guard into comparing a fake home against itself."""
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    tmp = Path(tempfile.mkdtemp(prefix="h2-real-state-snapshot-fake-home-"))
    try:
        os.environ["BRIDGE_TEST_HOME"] = str(tmp)
        (tmp / ".halo").mkdir()
        (tmp / ".halo" / "history.jsonl").write_text("x" * 50, encoding="utf-8")
        snap = run_all_mod._real_state_dir_snapshot()
        ctx.check(f"halo_history_size is NOT the fake home's 50-byte file, got {snap['halo_history_size']}",
                  snap["halo_history_size"] != 50)
    finally:
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
