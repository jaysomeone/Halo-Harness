"""tests.test_work_matrix_cli -- V2b: `halo work-matrix show/apply`,
the matrix-driven fixes tooling that turns a `doctor --work --probe-all`
JSON report into a suggested action per failure and (`apply`) writes the
ONE class of failure that maps onto a real config.json knob
(`databricks.gateway.<endpoint>`). Two synthetic sample reports live under
tests/fixtures/work-matrix/ (all-green, and one of each failure class).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
FIXTURES = REPO_DIR / "tests" / "fixtures" / "work-matrix"
test, TESTS = new_registry()


def _run(argv, home: Path, *, stdin: str = "", timeout: int = 30):
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(home), "PYTHONPATH": str(REPO_DIR)})
    return subprocess.run([sys.executable, "-m", "halo_harness"] + argv, env=env, cwd=str(REPO_DIR),
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           input=stdin, timeout=timeout)


def _fresh_home() -> Path:
    return Path(tempfile.mkdtemp(prefix="halo-workmatrix-"))


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Pure functions: classify_row / render_show_table / writable_overrides.
# ---------------------------------------------------------------------------

@test
def test_render_show_table_all_green_says_nothing_to_fix(ctx: Ctx):
    from halo_harness.work_matrix import render_show_table
    text = render_show_table(_load("all-green.json"))
    ctx.check(f"reports nothing to fix, got {text!r}", "nothing to fix" in text)


@test
def test_render_show_table_all_failures_lists_every_class(ctx: Ctx):
    from halo_harness.work_matrix import render_show_table
    text = render_show_table(_load("all-failures.json"))
    ctx.check("403 IP access list flagged", "databricks-gemma-3-27b" in text and "IP access list" in text)
    ctx.check("VPN action suggested for the 403", "VPN" in text)
    ctx.check("wrong-path/anthropic-companion flagged",
              "databricks-glm-5-3" in text and "wrong path" in text)
    ctx.check("gateway override action suggested", "databricks.gateway.databricks-glm-5-3" in text)
    ctx.check("unrecovered 500 reported as unknown",
              "databricks-qwen-3-max" in text and "unknown" in text)
    ctx.check("tool-call failure flagged", "databricks-deepseek-v4-1-flash" in text
              and "no tool call produced" in text)
    ctx.check("reasoning replay failure flagged", "databricks-gpt-oss-120b" in text
              and "reasoning replay rejected" in text)
    ctx.check("the clean claude-opus row is never listed as a failure",
              text.count("databricks-claude-opus-4-6") == 0)


@test
def test_classify_row_wrong_path_uses_anthropic_companion(ctx: Ctx):
    from halo_harness.work_matrix import classify_row
    rows = _load("all-failures.json")["rows"]
    by_name = {r["endpoint"]: r for r in rows}
    row = by_name["databricks-glm-5-3"]
    result = classify_row(row, by_name)
    ctx.check(f"config_key targets this endpoint, got {result}",
              result["config_key"] == "databricks.gateway.databricks-glm-5-3")
    ctx.check("config_value is anthropic", result["config_value"] == "anthropic")


@test
def test_classify_row_clean_200_is_not_a_failure(ctx: Ctx):
    from halo_harness.work_matrix import classify_row
    rows = _load("all-green.json")["rows"]
    by_name = {r["endpoint"]: r for r in rows}
    for row in rows:
        ctx.check(f"{row['endpoint']} classified as clean", classify_row(row, by_name) is None)


@test
def test_writable_overrides_only_the_wrong_path_class(ctx: Ctx):
    from halo_harness.work_matrix import writable_overrides
    overrides = writable_overrides(_load("all-failures.json"))
    ctx.check(f"exactly one writable override, got {overrides}",
              overrides == [("databricks.gateway.databricks-glm-5-3", "anthropic")])


@test
def test_writable_overrides_empty_for_all_green(ctx: Ctx):
    from halo_harness.work_matrix import writable_overrides
    ctx.check("no overrides for a clean report", writable_overrides(_load("all-green.json")) == [])


# ---------------------------------------------------------------------------
# End to end: the real `halo work-matrix` CLI.
# ---------------------------------------------------------------------------

@test
def test_cli_show_end_to_end(ctx: Ctx):
    home = _fresh_home()
    result = _run(["work-matrix", "show", str(FIXTURES / "all-failures.json")], home)
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    ctx.check("output mentions the wrong-path endpoint", "databricks-glm-5-3" in result.stdout)
    ctx.check("output mentions the suggested gateway override",
              "databricks.gateway.databricks-glm-5-3" in result.stdout)


@test
def test_cli_show_bad_path_is_a_clean_error_not_a_traceback(ctx: Ctx):
    home = _fresh_home()
    missing = home / "does-not-exist.json"
    result = _run(["work-matrix", "show", str(missing)], home)
    ctx.check(f"exit 2, got {result.returncode}", result.returncode == 2)
    ctx.check("no Python traceback leaked", "Traceback" not in result.stderr)


@test
def test_cli_apply_yes_writes_only_the_one_gateway_override(ctx: Ctx):
    home = _fresh_home()
    result = _run(["work-matrix", "apply", str(FIXTURES / "all-failures.json"), "--yes"], home)
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    ctx.check("reports one override written", "Wrote 1 override" in result.stdout)
    cfg = json.loads((home / ".halo" / "config.json").read_text(encoding="utf-8"))
    ctx.check(f"gateway override actually written, got {cfg}",
              cfg.get("databricks", {}).get("gateway", {}).get("databricks-glm-5-3") == "anthropic")
    # Never anything for the other four failure classes.
    ctx.check("no gateway key written for the 403/unknown/tool-call/replay endpoints",
              set(cfg.get("databricks", {}).get("gateway", {}).keys()) == {"databricks-glm-5-3"})


@test
def test_cli_apply_confirmation_prompt_shows_overrides_before_asking(ctx: Ctx):
    home = _fresh_home()
    result = _run(["work-matrix", "apply", str(FIXTURES / "all-failures.json")], home, stdin="n\n")
    ctx.check(f"declined -> exit 1, got {result.returncode}", result.returncode == 1)
    ctx.check("the override was listed before the prompt",
              "databricks.gateway.databricks-glm-5-3" in result.stdout)
    ctx.check("aborted, nothing written", "Aborted" in result.stdout)
    ctx.check("no config.json written at all", not (home / ".halo" / "config.json").exists())


@test
def test_cli_apply_all_green_nothing_actionable(ctx: Ctx):
    home = _fresh_home()
    result = _run(["work-matrix", "apply", str(FIXTURES / "all-green.json"), "--yes"], home)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("says nothing actionable", "nothing actionable" in result.stdout)
    ctx.check("no config.json written", not (home / ".halo" / "config.json").exists())


@test
def test_cli_apply_never_touches_claude_json_or_settings(ctx: Ctx):
    home = _fresh_home()
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    settings_path = home / ".claude" / "settings.json"
    settings_path.write_text('{"marker": "untouched"}', encoding="utf-8")
    claude_json_path = home / ".claude.json"
    claude_json_path.write_text('{"marker": "untouched"}', encoding="utf-8")
    result = _run(["work-matrix", "apply", str(FIXTURES / "all-failures.json"), "--yes"], home)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("settings.json byte-for-byte unchanged", settings_path.read_text(encoding="utf-8") == '{"marker": "untouched"}')
    ctx.check("claude.json byte-for-byte unchanged", claude_json_path.read_text(encoding="utf-8") == '{"marker": "untouched"}')


def _hermetic_child_env() -> dict:
    """2.0.0 fixpass item G: never forward a stray BRIDGE_STATE_DIR
    (would let bridge_home() escape this test's own BRIDGE_TEST_HOME
    scoping) or HALO_* (would out-rank the legacy BRIDGE_* name a
    fixture deliberately sets, per env_compat's own precedence) from
    the parent process into a spawned child -- same hermeticity
    tests/test_init_cli.py::_run already has, applied at each of this
    file's own `env = dict(os.environ)` call sites."""
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    return env


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
