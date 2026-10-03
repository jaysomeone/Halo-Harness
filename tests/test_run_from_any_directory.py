"""tests.test_run_from_any_directory -- Halo Harness 2.0.1, the "run from
any directory" release. Pins the acceptance criteria directly: `halo
--version`/`halo doctor`/`halo -p` all run as a real subprocess with its
OWN process cwd set to a directory outside this checkout (literally under
the OS temp dir -- `/tmp` on Linux/Kali, matching the brief's own
acceptance wording -- never a `--cwd` flag standing in for actually being
started elsewhere); a scratch project's `CLAUDE.md` with one unique
sentence reaches the assembled request body through a mock upstream; and
the package's own vendored data files (the model table, the Databricks/
OpenRouter catalog fallbacks, the TUI's `styles.tcss`) load the same way
with the process's own cwd pointed somewhere else entirely.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream
from halo_harness import __version__

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _hermetic_env(extra: dict) -> dict:
    """Same hermeticity idiom every other subprocess-driving test file in
    this suite uses (e.g. test_h0_14_cases.py's own `_hermetic_child_env`):
    never forward a stray `BRIDGE_STATE_DIR`/`HALO_*` from the parent
    process into the spawned child, then layer this test's own env on
    top."""
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    env.update(extra)
    return env


def _scratch_dir_outside_the_repo(prefix: str) -> Path:
    """A directory under the OS temp dir -- literally `/tmp/<prefix>...`
    on Linux/Kali, an `AppData\\Local\\Temp` equivalent on Windows --
    guaranteed to be outside REPO_DIR, so a subprocess run with this as
    its OWN cwd proves the command does not depend on the checkout
    directory at all."""
    d = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    assert REPO_DIR not in d.parents and d != REPO_DIR, f"scratch dir {d} must be outside the checkout"
    return d


# ---------------------------------------------------------------------------
# halo --version / halo doctor / halo -p, process cwd outside the checkout.
# ---------------------------------------------------------------------------

@test
def test_version_from_a_scratch_directory_outside_the_repo(ctx: Ctx):
    scratch = _scratch_dir_outside_the_repo("halo-run-anywhere-version-")
    env = _hermetic_env({"PYTHONPATH": str(REPO_DIR)})
    result = subprocess.run([sys.executable, "-m", "halo_harness", "--version"],
                             cwd=str(scratch), env=env, capture_output=True, text=True, timeout=30)
    ctx.check(f"exit 0, got {result.returncode} (stderr: {result.stderr[-400:]!r})", result.returncode == 0)
    # Halo 2.0.2 round 6: `(commit, branch)` is appended once known --
    # this subprocess's cwd is the scratch dir, but PYTHONPATH still
    # points at this real checkout, so update.installed_build's own
    # PYTHONPATH-checkout fallback (keyed off __file__, never cwd) still
    # finds a real commit/branch -- proof this stays cwd-independent too.
    ctx.check(f"prints 'halo {__version__}', optionally with (commit, branch), got {result.stdout!r}",
              re.match(rf"^halo {re.escape(__version__)}( \([0-9a-f]{{7}}(, \S+)?\))?\s*$",
                        result.stdout) is not None)


@test
def test_doctor_from_a_scratch_directory_outside_the_repo(ctx: Ctx):
    fh = build_fake_home()
    scratch = _scratch_dir_outside_the_repo("halo-run-anywhere-doctor-")
    env = _hermetic_env({
        "PYTHONPATH": str(REPO_DIR), "BRIDGE_TEST_HOME": str(fh["home"]),
        # Never spawn a real `claude auth status` subprocess from doctor's
        # own cc: checks -- same seam tests/test_h15_path_check.py uses.
        "BRIDGE_TEST_CC_AUTH_STATUS": '{"loggedIn": false}',
    })
    for k in ("OPENROUTER_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
              "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    result = subprocess.run([sys.executable, "-m", "halo_harness", "doctor"],
                             cwd=str(scratch), env=env, capture_output=True, text=True, timeout=60)
    ctx.check(f"runs to completion with no traceback, got stderr={result.stderr[-800:]!r}",
              "Traceback" not in result.stderr)
    ctx.check(f"prints the doctor header, got {result.stdout[:200]!r}", "halo doctor" in result.stdout)
    ctx.check(f"carries the command-on-path check, got {result.stdout[:4000]!r}",
              "halo command:" in result.stdout)


@test
def test_p_from_a_scratch_directory_outside_the_repo(ctx: Ctx):
    fh = build_fake_home()  # fh["proj"] is tempfile-backed -- already outside REPO_DIR
    mock = MockUpstream().start()
    try:
        env = _hermetic_env({
            "PYTHONPATH": str(REPO_DIR), "BRIDGE_TEST_HOME": str(fh["home"]),
            "BRIDGE_OPENROUTER_BASE_URL": mock.base_url, "OPENROUTER_API_KEY": "test-key",
        })
        result = subprocess.run(
            [sys.executable, "-m", "halo_harness", "-p", "reply pong", "--model", "or:mock/model"],
            cwd=str(fh["proj"]), env=env, capture_output=True, text=True, timeout=30)
        ctx.check(f"exit 0, got {result.returncode} (stderr: {result.stderr[-400:]!r})", result.returncode == 0)
        ctx.check(f"answered pong, got {result.stdout!r}", "pong" in result.stdout)
    finally:
        mock.stop()


# ---------------------------------------------------------------------------
# halo -p from a scratch project whose CLAUDE.md holds one unique sentence
# -- the sentence must reach the assembled request body (brief deliverable
# 4: "a headless test with a mock upstream that asserts the sentence
# reached the request body").
# ---------------------------------------------------------------------------

@test
def test_p_shows_scratch_project_claude_md_sentence_in_the_request_body(ctx: Ctx):
    fh = build_fake_home()
    unique_sentence = "The halo-run-anywhere acceptance marker is kumquat-77214-lighthouse."
    (fh["proj"] / "CLAUDE.md").write_text(
        f"# Scratch project CLAUDE.md\n\n{unique_sentence}\n", encoding="utf-8")
    mock = MockUpstream().start()
    try:
        env = _hermetic_env({
            "PYTHONPATH": str(REPO_DIR), "BRIDGE_TEST_HOME": str(fh["home"]),
            "BRIDGE_OPENROUTER_BASE_URL": mock.base_url, "OPENROUTER_API_KEY": "test-key",
        })
        result = subprocess.run(
            [sys.executable, "-m", "halo_harness", "-p", "reply pong", "--model", "or:mock/model"],
            cwd=str(fh["proj"]), env=env, capture_output=True, text=True, timeout=30)
        ctx.check(f"exit 0, got {result.returncode} (stderr: {result.stderr[-400:]!r})", result.returncode == 0)
        ctx.check("the mock upstream actually received at least one request", len(mock.requests) >= 1)
        bodies = [json.dumps(r["body"], ensure_ascii=False) for r in mock.requests if r["body"] is not None]
        joined = "\n".join(bodies)
        ctx.check(f"the scratch project's CLAUDE.md unique sentence reached the request body, "
                  f"got bodies={joined[:2000]!r}", unique_sentence in joined)
    finally:
        mock.stop()


# ---------------------------------------------------------------------------
# Vendored data files load relative to the PACKAGE, proven with the
# process's own cwd pointed somewhere outside the checkout entirely.
# ---------------------------------------------------------------------------

@test
def test_model_table_and_vendored_catalogs_load_with_cwd_outside_the_repo(ctx: Ctx):
    from halo_harness.providers.profiles import load_model_table, reset_model_table_cache
    from halo_harness.providers.models_dev import (
        load_vendored_databricks_fallback, load_vendored_openrouter_fallback,
    )
    scratch = _scratch_dir_outside_the_repo("halo-run-anywhere-pkgdata-")
    old_cwd = os.getcwd()
    reset_model_table_cache()  # force a real re-read from disk below, never a cache hit from an earlier test
    os.chdir(scratch)
    try:
        table = load_model_table()
        ctx.check(f"model_table.json still loads with cwd={scratch}, got {len(table)} row(s)", len(table) > 0)
        dbx_fallback = load_vendored_databricks_fallback()
        ctx.check(f"the Databricks vendored fallback still loads, got {len(dbx_fallback)} entry/ies",
                  len(dbx_fallback) > 0)
        or_fallback = load_vendored_openrouter_fallback()
        ctx.check(f"the OpenRouter vendored fallback still loads, got {len(or_fallback)} entry/ies",
                  len(or_fallback) > 0)
    finally:
        os.chdir(old_cwd)
        reset_model_table_cache()  # leave the cache clean for whatever test runs after this one


@test
def test_tui_stylesheet_ships_next_to_the_package_module_not_the_cwd(ctx: Ctx):
    """U2's own package-data contract (pyproject.toml: "App's CSS_PATH
    resolves relative to tui/app.py at runtime") -- proven here by
    resolving the file with the process's own cwd pointed outside the
    checkout, the same way the data-file test above does, rather than
    just re-asserting the pyproject.toml comment."""
    import halo_harness.tui.app as app_module
    scratch = _scratch_dir_outside_the_repo("halo-run-anywhere-tcss-")
    old_cwd = os.getcwd()
    os.chdir(scratch)
    try:
        css_path = Path(app_module.__file__).resolve().parent / "styles.tcss"
        ctx.check(f"tui/styles.tcss sits next to tui/app.py regardless of cwd, got {css_path}",
                  css_path.is_file())
        ctx.check("it's a real stylesheet, not an empty placeholder", css_path.stat().st_size > 100)
    finally:
        os.chdir(old_cwd)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
