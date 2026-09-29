"""tests.test_stats_cli_models -- H10 Part A: `rolo-claude stats --models
--json`'s stable schema, `improve.*` config keys (dotted set/get), and the
doctor sessions/cache-age/improve lines.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
FIXTURES = REPO_DIR / "tests" / "fixtures" / "telemetry"
test, TESTS = new_registry()


def _run(argv, home: Path, cwd: Path, timeout=30):
    env = dict(os.environ)
    env.update({"BRIDGE_TEST_HOME": str(home), "PYTHONPATH": str(REPO_DIR)})
    return subprocess.run([sys.executable, "-m", "rolo_claude"] + argv, env=env, cwd=str(cwd),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _home_with_fixtures() -> Path:
    import shutil
    home = Path(tempfile.mkdtemp(prefix="stats-models-home-"))
    sessions_dir = home / ".rolo-claude" / "sessions"
    shutil.copytree(FIXTURES, sessions_dir)
    now = time.time()
    for p in sessions_dir.glob("*/*.jsonl"):
        os.utime(p, (now, now))
    return home


@test
def test_stats_models_json_stable_schema(ctx: Ctx):
    home = _home_with_fixtures()
    cwd = Path(tempfile.mkdtemp(prefix="stats-models-cwd-"))
    result = _run(["stats", "--models", "--tools", "--since", "all", "--all-projects", "--json"], home, cwd)
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    obj = json.loads(result.stdout)
    for key in ("sessions", "since", "models", "tools", "top_error_classes"):
        ctx.check(f"top-level key {key!r} present", key in obj)
    ctx.check(f"3 sessions counted, got {obj['sessions']}", obj["sessions"] == 3)
    ctx.check("models is a list", isinstance(obj["models"], list))


@test
def test_stats_since_1d_is_accepted_not_rejected_by_argparse(ctx: Ctx):
    """H13 Part D bug fix: `--since` used to be `choices=("7d","30d","all")`
    -- "1d" (the brief's own "use `stats --models --since 1d` for the
    family-baseline numbers" acceptance line) failed with argparse's
    "invalid choice" before any fix landed. Any positive "<N>d" now parses;
    a genuinely bad value is still a clean, honest usage error (exit 2),
    never silently reinterpreted."""
    home = _home_with_fixtures()
    cwd = Path(tempfile.mkdtemp(prefix="stats-models-cwd-"))
    result = _run(["stats", "--models", "--since", "1d", "--all-projects", "--json"], home, cwd)
    ctx.check(f"--since 1d is accepted, exit 0, got {result.returncode} stderr={result.stderr!r}",
              result.returncode == 0)
    obj = json.loads(result.stdout)
    ctx.check(f"since echoed back exactly, got {obj.get('since')!r}", obj.get("since") == "1d")
    ctx.check(f"the 3 fixture sessions (mtimes bumped to now) are still within a 1-day window, got {obj['sessions']}",
              obj["sessions"] == 3)

    result2 = _run(["stats", "--models", "--since", "2d", "--all-projects", "--json"], home, cwd)
    ctx.check(f"any positive <N>d works, not just 1d, got {result2.returncode}", result2.returncode == 0)

    bad = _run(["stats", "--models", "--since", "not-a-window", "--all-projects", "--json"], home, cwd)
    ctx.check(f"a genuinely invalid --since is still a clean usage error, got exit {bad.returncode}",
              bad.returncode == 2)
    ctx.check("tools is a list", isinstance(obj["tools"], list))
    if obj["models"]:
        row = obj["models"][0]
        for key in ("model", "provider", "route", "sessions", "calls", "tokens_in", "tokens_out",
                    "cost_usd", "avg_ttft_ms", "avg_latency_ms", "finish_length_pct", "retries",
                    "status_counts", "overflows", "tool_calls", "tool_error_pct", "repair_hit_pct",
                    "edit_failure_pct", "steers", "interrupts", "compactions", "loop_breaker_trips"):
            ctx.check(f"model row has key {key!r}", key in row)
    if obj["tools"]:
        trow = obj["tools"][0]
        for key in ("tool", "calls", "error_pct", "avg_ms", "avg_bytes", "spilled", "error_classes"):
            ctx.check(f"tool row has key {key!r}", key in trow)


@test
def test_stats_bare_default_unchanged_by_new_flags(ctx: Ctx):
    """Bare `rolo-claude stats` (no --models/--tools) keeps its OLD schema
    -- adding --models/--tools support must never change the default path."""
    home = _home_with_fixtures()
    cwd = Path(tempfile.mkdtemp(prefix="stats-bare-cwd-"))
    result = _run(["stats", "--json"], home, cwd)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    obj = json.loads(result.stdout)
    ctx.check("old shape: sessions/turns/total_cost_usd/per_model/tool_counts",
              set(obj) == {"sessions", "turns", "total_cost_usd", "per_model", "tool_counts"})


@test
def test_improve_config_dotted_keys_roundtrip(ctx: Ctx):
    home = Path(tempfile.mkdtemp(prefix="improve-cfg-home-"))
    cwd = Path(tempfile.mkdtemp(prefix="improve-cfg-cwd-"))
    r1 = _run(["config", "set", "improve.model", "or:deepseek/deepseek-v4-flash"], home, cwd)
    ctx.check(f"config set exit 0, got {r1.returncode}, stderr={r1.stderr!r}", r1.returncode == 0)
    r2 = _run(["config", "get", "improve.model"], home, cwd)
    ctx.check(f"config get exit 0, got {r2.returncode}", r2.returncode == 0)
    ctx.check(f"round-trips, got {r2.stdout!r}", json.loads(r2.stdout) == "or:deepseek/deepseek-v4-flash")
    # A sibling key set afterward must not disturb the first.
    r3 = _run(["config", "set", "improve.hint", "false"], home, cwd)
    ctx.check(f"exit 0, got {r3.returncode}", r3.returncode == 0)
    r4 = _run(["config", "get", "improve.model"], home, cwd)
    ctx.check("sibling set left improve.model untouched", json.loads(r4.stdout) == "or:deepseek/deepseek-v4-flash")
    data = json.loads((home / ".rolo-claude" / "config.json").read_text(encoding="utf-8"))
    ctx.check("stored as a real nested dict, not a flat 'improve.model' key",
              isinstance(data.get("improve"), dict) and data["improve"].get("model") == "or:deepseek/deepseek-v4-flash"
              and data["improve"].get("hint") is False)


@test
def test_doctor_shows_sessions_cache_age_and_improve_config(ctx: Ctx):
    home = _home_with_fixtures()
    cwd = Path(tempfile.mkdtemp(prefix="doctor-improve-cwd-"))
    result = _run(["doctor"], home, cwd)
    ctx.check(f"exit code is 0 or 1 (WARN lines are fine, no crash), got {result.returncode}",
              result.returncode in (0, 1))
    ctx.check("no traceback", "Traceback" not in result.stderr)
    ctx.check("mentions Sessions", "Sessions:" in result.stdout)
    ctx.check("mentions /improve config", "/improve:" in result.stdout)
    ctx.check("mentions enabled=", "enabled=" in result.stdout)


def _fake_model_row(model="or:test/model", **overrides) -> dict:
    """A complete `aggregate_by_model` row shape -- every `_MODEL_COLUMNS`
    formatter reads a specific key directly, so a partial fake would
    KeyError."""
    row = {
        "model": model, "provider": "TestProv", "route": "or", "sessions": 3, "turns": None,
        "calls": 10, "tokens_in": 1000, "tokens_out": 200, "tokens_cached": 50, "cost_usd": 0.1234,
        "avg_ttft_ms": 120.0, "avg_latency_ms": 800.0, "finish_length_pct": 5.0, "retries": 1,
        "status_counts": {}, "overflows": 0, "tool_calls": 8, "tool_error_pct": 12.5,
        "repair_hit_pct": {"leak_parser": 0.0, "lenient_json": 0.0, "rename": 10.0, "args_repair": 0.0, "none": 0.0},
        "edit_failure_pct": 0.0, "steers": 1, "interrupts": 0, "compactions": 1, "loop_breaker_trips": 0,
    }
    row.update(overrides)
    return row


@test
def test_terminal_width_env_overrides(ctx: Ctx):
    """H10b defect 2: `RC_TERM_WIDTH` wins over `COLUMNS`, and neither can
    push the target below the 80-col floor the brief asks for."""
    from rolo_claude import stats_cli
    saved = {k: os.environ.get(k) for k in ("RC_TERM_WIDTH", "COLUMNS")}
    try:
        os.environ["RC_TERM_WIDTH"] = "150"
        os.environ["COLUMNS"] = "40"
        ctx.check("RC_TERM_WIDTH wins over COLUMNS", stats_cli._terminal_width() == 150)
        os.environ.pop("RC_TERM_WIDTH", None)
        os.environ["COLUMNS"] = "40"
        ctx.check(f"COLUMNS used when RC_TERM_WIDTH unset, floored to 80, got {stats_cli._terminal_width()}",
                  stats_cli._terminal_width() == 80)
        os.environ["COLUMNS"] = "300"
        ctx.check(f"a wide COLUMNS passes through, got {stats_cli._terminal_width()}",
                  stats_cli._terminal_width() == 300)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_fit_columns_drops_more_at_narrower_width(ctx: Ctx):
    """H10b defect 2: `_fit_columns` drops columns right-to-left (least
    important first) until the estimate fits, and never drops below 1."""
    from rolo_claude import stats_cli
    rows = [_fake_model_row()]
    wide = stats_cli._fit_columns(stats_cli._MODEL_COLUMNS, rows, 2000)
    narrow = stats_cli._fit_columns(stats_cli._MODEL_COLUMNS, rows, 80)
    ctx.check(f"a generous width keeps every column, got {len(wide)} of {len(stats_cli._MODEL_COLUMNS)}",
              len(wide) == len(stats_cli._MODEL_COLUMNS))
    ctx.check(f"an 80-col width keeps strictly fewer, got {len(narrow)} vs {len(wide)}", len(narrow) < len(wide))
    ctx.check("an 80-col width still keeps at least one column", len(narrow) >= 1)
    ctx.check("the most important column (model) always survives", narrow[0][0] == "model")
    tiny = stats_cli._fit_columns(stats_cli._MODEL_COLUMNS, rows, 1)
    ctx.check("even an impossible width never drops the last column", len(tiny) == 1)


@test
def test_print_table_tty_path_shrinks_columns_to_width(ctx: Ctx):
    """H10b defect 2: the rich-table (TTY) render path actually narrows the
    visible column set as the target width shrinks -- `force_terminal=True`
    simulates a real terminal without needing a pty."""
    import io
    from rich.console import Console
    from rolo_claude import stats_cli

    rows = [_fake_model_row()]
    saved = os.environ.get("RC_TERM_WIDTH")
    try:
        wide_buf, narrow_buf = io.StringIO(), io.StringIO()
        wide_console = Console(file=wide_buf, force_terminal=True, width=400, no_color=True)
        os.environ["RC_TERM_WIDTH"] = "400"
        stats_cli._print_table(wide_console, "Per model / provider", stats_cli._MODEL_COLUMNS, rows)
        wide_out = wide_buf.getvalue()

        narrow_console = Console(file=narrow_buf, force_terminal=True, width=80, no_color=True)
        os.environ["RC_TERM_WIDTH"] = "80"
        stats_cli._print_table(narrow_console, "Per model / provider", stats_cli._MODEL_COLUMNS, rows)
        narrow_out = narrow_buf.getvalue()

        ctx.check("wide render shows the wide-only 'interrupt' column header", "interrupt" in wide_out)
        ctx.check("narrow (80-col) render drops the wide-only 'interrupt' column header",
                  "interrupt" not in narrow_out)
        ctx.check("narrow render still shows the model column/value", "test/model" in narrow_out)
    finally:
        if saved is None:
            os.environ.pop("RC_TERM_WIDTH", None)
        else:
            os.environ["RC_TERM_WIDTH"] = saved


@test
def test_stats_models_wide_shows_more_columns_than_compact(ctx: Ctx):
    """H10b defect 2: `--wide` engages the full column set (`--wide`-only
    headers like 'route'/'interrupt' show up) where the bare default
    (compact, 14 columns) never does -- checked over the real CLI
    subprocess (captured stdout is never a TTY, so this exercises the
    plain-text non-TTY path for both)."""
    home = _home_with_fixtures()
    cwd = Path(tempfile.mkdtemp(prefix="stats-wide-cwd-"))
    compact = _run(["stats", "--models", "--since", "all", "--all-projects"], home, cwd)
    wide = _run(["stats", "--models", "--wide", "--since", "all", "--all-projects"], home, cwd)
    ctx.check(f"compact exit 0, got {compact.returncode}", compact.returncode == 0)
    ctx.check(f"wide exit 0, got {wide.returncode}", wide.returncode == 0)
    ctx.check("compact default omits the wide-only 'interrupt' column", "interrupt" not in compact.stdout)
    ctx.check("compact default omits the wide-only 'route' column", "route" not in compact.stdout)
    ctx.check("--wide includes the 'interrupt' column", "interrupt" in wide.stdout)
    ctx.check("--wide includes the 'route' column", "route" in wide.stdout)
    # stdout: [0] scope line, [1] table title, [2] the column-header row itself.
    ctx.check("--wide's column-header row is longer than compact's",
              len(wide.stdout.splitlines()[2]) > len(compact.stdout.splitlines()[2]))


@test
def test_stats_models_non_tty_plain_table_no_box_chars(ctx: Ctx):
    """H10b defect 2: piping (captured subprocess stdout is never a TTY)
    must produce a plain aligned text table -- no rich box-drawing
    characters, no ellipsis truncation."""
    home = _home_with_fixtures()
    cwd = Path(tempfile.mkdtemp(prefix="stats-plain-cwd-"))
    result = _run(["stats", "--models", "--since", "all", "--all-projects"], home, cwd)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    box_chars = set("─│┃┏┓┗┛├┤┬┴┼═║╔╗╚╝╠╣╦╩╬")
    found_box = box_chars & set(result.stdout)
    ctx.check(f"no box-drawing characters in piped output, found {found_box}", not found_box)
    ctx.check("no ellipsis truncation marker in piped output", "…" not in result.stdout)
    ctx.check("still shows the model column header", "model" in result.stdout)


@test
def test_stats_tools_header_says_tools_not_models(ctx: Ctx):
    """H10b defect 2: a bare `stats --tools` (no `--models`) must say
    'stats --tools' in its own header line, never 'stats --models'."""
    home = _home_with_fixtures()
    cwd = Path(tempfile.mkdtemp(prefix="stats-tools-header-cwd-"))
    result = _run(["stats", "--tools", "--since", "all", "--all-projects"], home, cwd)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    header = result.stdout.splitlines()[0]
    ctx.check(f"header says '--tools', got {header!r}", "--tools" in header)
    ctx.check(f"header does NOT say '--models', got {header!r}", "--models" not in header)

    both = _run(["stats", "--models", "--tools", "--since", "all", "--all-projects"], home, cwd)
    both_header = both.stdout.splitlines()[0]
    ctx.check(f"both flags -> header names both, got {both_header!r}",
              "--models" in both_header and "--tools" in both_header)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
