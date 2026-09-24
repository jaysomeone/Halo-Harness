"""tests.test_tools_powershell -- the PowerShell tool (H2 scope A):
win32-only, both at the registry level (never registered on POSIX) and at
the tool level (skips with a clear reason on POSIX rather than failing).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all
from rolo_claude.tools.base import ToolContext

test, TESTS = new_registry()


@test
def test_powershell_only_registered_in_default_tools_on_win32(ctx: Ctx):
    from rolo_claude.tools.registry import ToolRegistry
    names = ToolRegistry().names()
    if sys.platform == "win32":
        ctx.check("PowerShell present in the default registry on win32", "PowerShell" in names)
    else:
        ctx.check("PowerShell absent from the default registry off win32", "PowerShell" not in names)


@test
def test_powershell_runs_a_command(ctx: Ctx):
    if sys.platform != "win32":
        raise SkipTest("PowerShell tool is win32-only")
    from rolo_claude.tools.powershell import PowerShellTool
    d = Path(tempfile.mkdtemp(prefix="ps-run-"))
    result = PowerShellTool().run({"command": "Write-Output hello-from-powershell"}, ToolContext(cwd=d))
    ctx.check("no error", result.is_error is False)
    ctx.check(f"output captured, got {result.content!r}", "hello-from-powershell" in result.content)


@test
def test_powershell_nonzero_exit_reports_marker(ctx: Ctx):
    if sys.platform != "win32":
        raise SkipTest("PowerShell tool is win32-only")
    from rolo_claude.tools.powershell import PowerShellTool
    d = Path(tempfile.mkdtemp(prefix="ps-exit-"))
    result = PowerShellTool().run({"command": "exit 3"}, ToolContext(cwd=d))
    ctx.check(f"[exit code 3] marker present, got {result.content!r}", "[exit code 3]" in result.content)


@test
def test_powershell_missing_command_is_error(ctx: Ctx):
    if sys.platform != "win32":
        raise SkipTest("PowerShell tool is win32-only")
    from rolo_claude.tools.powershell import PowerShellTool
    result = PowerShellTool().run({}, ToolContext(cwd=Path(".")))
    ctx.check("missing command is an error", result.is_error is True)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
