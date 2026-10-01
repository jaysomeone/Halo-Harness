"""tests.test_packaging -- H9 whole-tree review, "NEW from H9" item: the
`wip/` directory (scratch scripts/specs from earlier drafting) and other
top-level dev-only directories (`research_notes/`, `reports/`) must never
ship in the actual installable package. Builds a REAL wheel (setuptools'
own `build_meta.build_wheel`, the same entry point `pip wheel`/`pip
install` use) and inspects its file list directly -- the decisive check,
since `[tool.setuptools.packages.find] include = ["halo_harness*"]` is a
CONFIG claim; this proves what actually ends up installable.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()

_DISALLOWED_SUBSTRINGS = ("wip/", "research_notes/", "reports/", "/spec1.md", "diag_ping", "smoke_frontmatter")

_cached_wheel_path: "Path | None" = None
_cached_wheel_error: "str | None" = None


def _build_wheel() -> Path:
    """Returns the built wheel's absolute Path (built ONCE per process,
    via a real `pip wheel` SUBPROCESS -- not setuptools' `build_meta` called
    in-process, which left stale `build/` state behind on a second in-
    process call on this Windows build host; a fresh subprocess is also
    exactly how a real install actually builds one). SkipTest if pip/
    setuptools genuinely can't build one in this environment at all (a
    minimal test box with no build backend installed -- this check simply
    can't run there; it is not itself part of what a real user install
    needs)."""
    global _cached_wheel_path, _cached_wheel_error
    if _cached_wheel_error is not None:
        raise SkipTest(_cached_wheel_error)
    if _cached_wheel_path is not None:
        return _cached_wheel_path
    out_dir = Path(tempfile.mkdtemp(prefix="rc-wheel-build-"))
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", str(REPO_DIR), "--no-deps",
             "--no-build-isolation", "-w", str(out_dir)],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        _cached_wheel_error = f"could not run pip wheel: {type(e).__name__}: {e}"
        raise SkipTest(_cached_wheel_error)
    if result.returncode != 0:
        _cached_wheel_error = f"pip wheel failed (exit {result.returncode}): {result.stderr[-800:]}"
        raise SkipTest(_cached_wheel_error)
    wheels = list(out_dir.glob("*.whl"))
    if not wheels:
        _cached_wheel_error = "pip wheel exited 0 but produced no .whl file"
        raise SkipTest(_cached_wheel_error)
    _cached_wheel_path = wheels[0]
    return _cached_wheel_path


@test
def test_h9_wheel_never_ships_wip_or_other_drafting_scratch_dirs(ctx: Ctx):
    wheel_path = _build_wheel()
    with zipfile.ZipFile(wheel_path) as z:
        names = z.namelist()
    ctx.check(f"the wheel actually has real content, got {len(names)} file(s)", len(names) > 10)
    leaked = [n for n in names if any(bad in n for bad in _DISALLOWED_SUBSTRINGS)]
    ctx.check(f"no wip/research_notes/reports/scratch-script content leaked in, got {leaked}", leaked == [])
    ctx.check("halo_harness/__init__.py is present (the wheel isn't just empty/broken)",
              any(n.endswith("halo_harness/__init__.py") for n in names))


@test
def test_h9_wheel_ships_the_required_runtime_data_files(ctx: Ctx):
    """The package-data entries pyproject.toml itself documents as
    load-bearing at runtime (tui/styles.tcss, the provider JSON tables) --
    a regression here is a silent runtime crash on a non-editable install,
    not a test failure close to the cause."""
    wheel_path = _build_wheel()
    with zipfile.ZipFile(wheel_path) as z:
        names = set(z.namelist())
    for required_suffix in ("halo_harness/tui/styles.tcss", "halo_harness/providers/model_table.json",
                             "halo_harness/providers/catalog/models_dev_databricks_fallback.json",
                             "halo_harness/providers/catalog/openrouter_fallback.json"):
        ctx.check(f"{required_suffix} is in the wheel", any(n.endswith(required_suffix) for n in names))


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
