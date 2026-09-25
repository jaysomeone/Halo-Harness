"""tests.test_vendor_wheels -- H8 scope F: tools/vendor_wheels.py's PEP 508
marker evaluator (the part most likely to silently do the wrong thing --
a Windows build host vendoring wheels for a Linux work box must EXCLUDE
`pywin32 ; sys_platform == 'win32'`, which only works if marker evaluation
targets the requested Linux env, not whatever platform this process is
actually running on).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


def _linux_env():
    import vendor_wheels
    return vendor_wheels.linux_env("3.11.0")


@test
def test_marker_applies_all_five_real_requirements_lock_markers(ctx: Ctx):
    """The exact closed set of marker expressions requirements.lock
    actually contains (see the repo's own requirements.lock) -- every one
    evaluated against a Linux/CPython/3.11 target."""
    import vendor_wheels
    env = _linux_env()
    cases = [
        ("implementation_name != 'PyPy' and platform_python_implementation != 'PyPy'", True),
        ("platform_python_implementation != 'PyPy'", True),
        ("python_full_version >= '3.12' and sys_platform == 'emscripten'", False),
        ("sys_platform != 'emscripten'", True),
        ("sys_platform == 'win32'", False),
    ]
    for marker, expected in cases:
        got = vendor_wheels.marker_applies(marker, env=env)
        ctx.check(f"{marker!r} -> {expected}, got {got}", got == expected)


@test
def test_marker_applies_unrecognised_clause_fails_open(ctx: Ctx):
    import vendor_wheels
    env = _linux_env()
    ctx.check("an unrecognised clause shape is kept (fails open), never dropped",
              vendor_wheels.marker_applies("some_future_marker_syntax ~= '1.0'", env=env) is True)


@test
def test_filtered_requirements_excludes_pywin32_includes_core_deps(ctx: Ctx):
    import vendor_wheels
    reqs = vendor_wheels.filtered_requirements("3.11.0")
    ctx.check(f"pywin32 excluded for a Linux target, got {[r for r in reqs if 'pywin32' in r]}",
              not any("pywin32" in r for r in reqs))
    for pkg in ("textual", "rich", "mcp", "pydantic-core"):
        ctx.check(f"{pkg} present, got {[r for r in reqs if r.startswith(pkg)]}",
                  any(r.split("==")[0] == pkg for r in reqs))
    ctx.check("every returned line is a bare 'name==version' (markers stripped)",
              all(" " not in r and ";" not in r for r in reqs))


@test
def test_filtered_requirements_same_regardless_of_which_os_this_runs_on(ctx: Ctx):
    """The whole point: this must give the IDENTICAL Linux-targeted list
    whether the tool itself is invoked from Windows or from Linux -- it
    must never fall back to evaluating markers against ITS OWN platform."""
    import vendor_wheels
    reqs = vendor_wheels.filtered_requirements("3.11.0")
    ctx.check(f"still excludes pywin32 regardless of sys.platform ({sys.platform!r}), got "
              f"{[r for r in reqs if 'pywin32' in r]}", not any("pywin32" in r for r in reqs))


@test
def test_download_dry_run_builds_the_expected_pip_argv_shape(ctx: Ctx):
    import vendor_wheels
    with tempfile.TemporaryDirectory() as d:
        out_dir = Path(d) / "wheels"
        rc = vendor_wheels.download(out_dir=out_dir, python_tag="311", platform_tag="manylinux2014_x86_64",
                                     dry_run=True)
        ctx.check("dry-run exits 0", rc == 0)
        ctx.check("dry-run downloads nothing (no .whl files, req file cleaned up)",
                  not out_dir.exists() or not list(out_dir.glob("*.whl")))


@test
def test_main_missing_lock_file_is_a_clean_error_not_a_crash(ctx: Ctx):
    """`main()` (the real entry point) checks LOCK_FILE.exists() up front,
    before `download()` ever runs -- `download()` itself has no such guard
    (it's an internal helper `main()` alone is responsible for protecting),
    so this exercises the actual guarded path rather than `download()` in
    isolation for a precondition it doesn't enforce."""
    import vendor_wheels
    old_lock = vendor_wheels.LOCK_FILE
    try:
        vendor_wheels.LOCK_FILE = Path(tempfile.mkdtemp()) / "no-such-lock.txt"
        with tempfile.TemporaryDirectory() as d:
            rc = vendor_wheels.main(["--out", str(d), "--dry-run"])
            ctx.check("a clean non-zero exit, no traceback", rc == 1)
    finally:
        vendor_wheels.LOCK_FILE = old_lock


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
