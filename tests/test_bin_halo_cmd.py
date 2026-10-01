"""tests.test_bin_halo_cmd -- 2.0.0 fixpass finding 9: bin/halo.cmd must
pass through the REAL exit code of whichever `halo` it finds on PATH (or
falls back to `python -m halo_harness`), never whatever %ERRORLEVEL%
happened to be before the parenthesised `for ... do ( )` block that used to
swallow it (cmd.exe expands %ERRORLEVEL% once, at PARSE time, before the
real call inside the block ever runs). Windows-only -- `.cmd` has no
meaning elsewhere; skipped everywhere else.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
HALO_CMD = REPO_DIR / "bin" / "halo.cmd"
test, TESTS = new_registry()


def _require_windows() -> None:
    if sys.platform != "win32" or not HALO_CMD.exists():
        raise SkipTest("bin/halo.cmd is a Windows-only wrapper")


def _minimal_system_path() -> str:
    """Just enough of the real PATH (System32 et al.) for `cmd`/`where`
    themselves to keep working, with nothing project- or user-installed
    on it -- so a fake executable prepended by a test is the ONLY thing
    named `halo`/`python` that `where`/the shell can ever find."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return os.pathsep.join([f"{system_root}\\System32", system_root, f"{system_root}\\System32\\Wbem"])


@test
def test_halo_cmd_passes_through_the_installed_targets_real_exit_code(ctx: Ctx):
    """finding 9: a fake `halo.cmd` standing in for an installed console
    script exits 42 -- the wrapper's OWN exit code must be 42 too, not the
    stale pre-loop ERRORLEVEL (0, the overwhelmingly common case) the old
    script always reported instead."""
    _require_windows()
    fake_bin = Path(tempfile.mkdtemp(prefix="halo-cmd-fake-bin-"))
    (fake_bin / "halo.cmd").write_text("@echo off\r\necho fake halo ran\r\nexit /b 42\r\n", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = str(fake_bin) + os.pathsep + _minimal_system_path()
    result = subprocess.run(["cmd", "/c", str(HALO_CMD)], env=env, cwd=str(REPO_DIR),
                             capture_output=True, text=True, timeout=20)
    ctx.check(f"stdout shows the fake halo actually ran, got {result.stdout!r}", "fake halo ran" in result.stdout)
    ctx.check(f"the wrapper's own exit code is the REAL 42, not swallowed to 0, got {result.returncode}",
              result.returncode == 42)


@test
def test_halo_cmd_passes_through_exit_0_too(ctx: Ctx):
    """Sanity check: a fake `halo` that exits 0 must still report 0 --
    proves the fix passes the REAL code through both ways, not just
    inverting the old always-0 bug into an always-nonzero one."""
    _require_windows()
    fake_bin = Path(tempfile.mkdtemp(prefix="halo-cmd-fake-bin-ok-"))
    (fake_bin / "halo.cmd").write_text("@echo off\r\necho fake halo ok\r\nexit /b 0\r\n", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = str(fake_bin) + os.pathsep + _minimal_system_path()
    result = subprocess.run(["cmd", "/c", str(HALO_CMD)], env=env, cwd=str(REPO_DIR),
                             capture_output=True, text=True, timeout=20)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)


@test
def test_halo_cmd_fallback_branch_passes_through_exit_code_too(ctx: Ctx):
    """When `where halo` finds nothing at all, the pre-existing fallback
    branch (`python -m halo_harness %*` + `exit /b %ERRORLEVEL%`, never
    inside a parenthesised block, so never bugged by this finding) must
    still pass a real nonzero exit code through -- a fake `python` stands
    in so this doesn't depend on halo_harness's own exit behavior at all."""
    _require_windows()
    empty_bin = Path(tempfile.mkdtemp(prefix="halo-cmd-fallback-bin-"))
    (empty_bin / "python.cmd").write_text(
        "@echo off\r\necho fake python ran: %*\r\nexit /b 7\r\n", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = str(empty_bin) + os.pathsep + _minimal_system_path()
    result = subprocess.run(["cmd", "/c", str(HALO_CMD), "--probe-arg"], env=env, cwd=str(REPO_DIR),
                             capture_output=True, text=True, timeout=20)
    ctx.check(f"the fake python fallback ran with our args, got {result.stdout!r}",
              "fake python ran: -m halo_harness --probe-arg" in result.stdout)
    ctx.check(f"exit code 7 passed through from the fallback branch too, got {result.returncode}",
              result.returncode == 7)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
