"""tests.test_install_halo_ps1 -- Halo 2.0.2 round 6: scripts/install-halo.ps1
(the Windows counterpart to install-halo.sh) stays syntactically valid
PowerShell, and its `-DryRun` mode genuinely never installs/uninstalls/clones
anything. Runs real PowerShell (`pwsh` preferred, `powershell` as a
fallback) -- skipped outright where neither exists (any non-Windows CI box
with no PowerShell Core installed).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
SCRIPT = REPO_DIR / "scripts" / "install-halo.ps1"


def _require_powershell() -> str:
    for exe in ("pwsh", "powershell"):
        found = shutil.which(exe)
        if found:
            return exe
    raise SkipTest("no pwsh/powershell on PATH")


test, TESTS = new_registry()


@test
def test_install_halo_ps1_exists(ctx: Ctx):
    ctx.check(f"scripts/install-halo.ps1 exists, got {SCRIPT}", SCRIPT.is_file())


@test
def test_install_halo_ps1_parses_as_a_scriptblock(ctx: Ctx):
    """The brief's own check: `[scriptblock]::Create((Get-Content -Raw
    ...))` -- parses the file without running a single line of it."""
    exe = _require_powershell()
    # A single-quoted PowerShell string literal -- '' escapes a literal '
    # inside one, so a path containing a quote is still one argument.
    ps_literal = "'" + str(SCRIPT).replace("'", "''") + "'"
    command = f"[scriptblock]::Create((Get-Content -Raw -Path {ps_literal})) | Out-Null; Write-Host 'PARSE OK'"
    result = subprocess.run([exe, "-NoProfile", "-Command", command],
                             capture_output=True, text=True, timeout=30)
    ctx.check(f"{exe} parses the script with exit 0, got {result.returncode} "
              f"stdout={result.stdout!r} stderr={result.stderr!r}",
              result.returncode == 0 and "PARSE OK" in result.stdout)


@test
def test_install_halo_ps1_dry_run_no_clone_prints_commands_and_exits_0(ctx: Ctx):
    exe = _require_powershell()
    result = subprocess.run([exe, "-NoProfile", "-File", str(SCRIPT), "-NoClone", "-DryRun"],
                             capture_output=True, text=True, timeout=60)
    out = result.stdout
    ctx.check(f"exits 0, got {result.returncode} stdout={out!r} stderr={result.stderr!r}", result.returncode == 0)
    ctx.check(f"prints the install command as a dry run, got {out!r}",
              "DRY RUN:" in out and "uv tool install --reinstall git+" in out)
    ctx.check(f"never claims to have actually verified an install, got {out!r}",
              "DRY RUN: would check halo is on PATH" in out)


@test
def test_install_halo_ps1_dry_run_clone_path_never_touches_the_filesystem(ctx: Ctx):
    exe = _require_powershell()
    scratch = Path(tempfile.mkdtemp(prefix="install-halo-ps1-dry-run-"))
    clone_dir = scratch / "Halo-Harness-clone-target"
    result = subprocess.run([exe, "-NoProfile", "-File", str(SCRIPT), "-DryRun", "-CloneDir", str(clone_dir)],
                             capture_output=True, text=True, timeout=60)
    out = result.stdout
    ctx.check(f"exits 0, got {result.returncode} stdout={out!r} stderr={result.stderr!r}", result.returncode == 0)
    ctx.check(f"prints the git clone command as a dry run, got {out!r}",
              "DRY RUN:" in out and "git clone" in out and str(clone_dir) in out)
    ctx.check(f"the clone directory was never actually created, got exists={clone_dir.exists()}",
              not clone_dir.exists())


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
