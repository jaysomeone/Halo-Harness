"""tests.test_shadow -- halo_harness/shadow.py: ShadowStore's own git
plumbing setup. The actual snapshot/rewind mechanics (mangle/unmangle,
step commits, restoring a working tree) are exercised indirectly through
test_tui.py's `/rewind` pilots; this file is for shadow.py's standalone
setup behavior that needs no TUI session to observe.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all
from halo_harness.shadow import ShadowStore

test, TESTS = new_registry()


@test
def test_fresh_shadow_repo_sets_core_longpaths_on_windows(ctx: Ctx):
    """The shadow repo sits under a deeply-nested state-dir path
    (~/.halo/sessions/<slug>/<session_id>/shadow/...), and a mangled
    absolute source path is appended underneath that (_mangle) -- together
    these can exceed Windows' 260-char limit under a long home or cwd.
    `_ensure_repo` now sets `core.longpaths true` right after `git init` so
    git itself never refuses a long object path there. POSIX has no such
    limit/setting, so this is win32-only."""
    if sys.platform != "win32":
        raise SkipTest("core.longpaths is a Windows-only git setting")
    session_dir = Path(tempfile.mkdtemp(prefix="shadow-longpaths-"))
    store = ShadowStore(session_dir)
    result = subprocess.run(
        ["git", "-C", str(store.dir), "config", "--get", "core.longpaths"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    ctx.check(f"core.longpaths is set to true, got stdout={result.stdout!r} stderr={result.stderr!r}",
              result.stdout.strip() == "true")


@test
def test_ensure_repo_is_idempotent_and_never_resets_an_existing_repo(ctx: Ctx):
    """A second ShadowStore over the SAME session_dir must not re-run `git
    init` (which would discard nothing here, but _ensure_repo's own
    early-return on an existing `.git` is the contract this whole module
    relies on for not fighting itself across multiple steps)."""
    session_dir = Path(tempfile.mkdtemp(prefix="shadow-idempotent-"))
    store1 = ShadowStore(session_dir)
    marker = store1.dir / ".git" / "this-run-marker.txt"
    marker.write_text("still here", encoding="utf-8")

    store2 = ShadowStore(session_dir)
    ctx.check("second construction reuses the same shadow dir", store2.dir == store1.dir)
    ctx.check("the existing .git was never reinitialized", marker.exists())


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
