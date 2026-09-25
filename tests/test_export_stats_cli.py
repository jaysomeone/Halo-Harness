"""tests.test_export_stats_cli -- H8 scope D: `rolo-claude export`
(--sanitize) and `rolo-claude stats`, headless versions of U5's own
`/export`/`/stats` slash commands, over real session JSONL logs.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _fresh_home() -> Path:
    return Path(tempfile.mkdtemp(prefix="rolo-claude-exportstats-"))


def _run(argv, home: Path, cwd: Path, timeout=30):
    env = dict(os.environ)
    env.update({"BRIDGE_TEST_HOME": str(home), "PYTHONPATH": str(REPO_DIR)})
    return subprocess.run([sys.executable, "-m", "rolo_claude"] + argv, env=env, cwd=str(cwd),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _make_session_with_secret(home: Path, cwd: Path) -> str:
    """Writes one real session log (via a real SessionLog + Session
    construction) containing a Bash tool_result with a fake secret in it,
    and returns its session_id."""
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    try:
        from rolo_claude.agent.assemble import SessionContext
        from rolo_claude.agent.loop import Session
        from rolo_claude.model import ModelProfile, parse_model_ref
        session_ctx = SessionContext(cwd=cwd, model_label="or:mock/model")
        model_ref = parse_model_ref("or:mock/model")
        session = Session(cwd=cwd, model_ref=model_ref, model_profile=ModelProfile(), creds=None,
                           state_dir=Path(tempfile.mkdtemp(prefix="exportstats-state-")), model_label="or:mock/model",
                           session_context=session_ctx)
        session.log.append_user([{"type": "text", "text": "run env"}])
        session.log.append_assistant(
            content=[{"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "env"}}],
            stop_reason="tool_use",
        )
        session.log.append_tool_result(
            tool_use_id="call_1",
            content="OPENROUTER_API_KEY=sk-or-v1-totallyrealsecretvalue1234567890\nPATH=/usr/bin",
            is_error=False,
        )
        session.log.append_usage({"input_tokens": 100, "output_tokens": 20}, 0.001)
        return session.log.session_id
    finally:
        os.environ.pop("BRIDGE_TEST_HOME", None)


@test
def test_export_no_sessions_is_a_clean_error(ctx: Ctx):
    home = _fresh_home()
    cwd = Path(tempfile.mkdtemp(prefix="exportstats-cwd-"))
    result = _run(["export"], home, cwd)
    ctx.check(f"exit 2, got {result.returncode}", result.returncode == 2)
    ctx.check("no traceback", "Traceback" not in result.stderr)
    ctx.check("names the problem", "no sessions found" in result.stderr.lower())


@test
def test_export_latest_session_round_trips_as_valid_jsonl(ctx: Ctx):
    home = _fresh_home()
    cwd = Path(tempfile.mkdtemp(prefix="exportstats-cwd-"))
    _make_session_with_secret(home, cwd)
    result = _run(["export"], home, cwd)
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    ctx.check(f"at least one JSONL line, got {len(lines)}", len(lines) > 0)
    parsed = [json.loads(l) for l in lines]
    ctx.check("every line parses as JSON", len(parsed) == len(lines))
    ctx.check("the real session's own model appears", any(n.get("model") == "or:mock/model" for n in parsed))
    blob = result.stdout
    ctx.check("without --sanitize, the raw secret IS present (baseline)", "sk-or-v1-totallyrealsecretvalue" in blob)


@test
def test_export_sanitize_redacts_the_api_key(ctx: Ctx):
    home = _fresh_home()
    cwd = Path(tempfile.mkdtemp(prefix="exportstats-cwd-"))
    _make_session_with_secret(home, cwd)
    result = _run(["export", "--sanitize"], home, cwd)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("the secret value is gone", "sk-or-v1-totallyrealsecretvalue" not in result.stdout)
    ctx.check("the env var name is still visible (only the value is redacted)", "OPENROUTER_API_KEY" in result.stdout)
    ctx.check("still valid JSONL after sanitizing", all(json.loads(l) for l in result.stdout.splitlines() if l.strip()))


@test
def test_export_output_file(ctx: Ctx):
    home = _fresh_home()
    cwd = Path(tempfile.mkdtemp(prefix="exportstats-cwd-"))
    _make_session_with_secret(home, cwd)
    out_path = Path(tempfile.mkdtemp(prefix="exportstats-out-")) / "session.jsonl"
    result = _run(["export", "-o", str(out_path)], home, cwd)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("the file was actually written", out_path.exists() and out_path.stat().st_size > 0)


@test
def test_sanitize_text_redacts_known_secret_shapes(ctx: Ctx):
    from rolo_claude.export_cli import sanitize_text
    cases = [
        "OPENROUTER_API_KEY=sk-or-v1-abcdef1234567890",
        "Authorization: Bearer abcdefghijklmnopqrstuvwx",
        "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "dbx token dapi0123456789abcdef0123456789abcdef",
    ]
    for text in cases:
        cleaned = sanitize_text(text)
        ctx.check(f"redacted something in {text!r}, got {cleaned!r}", cleaned != text and "<redacted>" in cleaned)


@test
def test_stats_no_sessions_reports_zero_not_a_crash(ctx: Ctx):
    home = _fresh_home()
    cwd = Path(tempfile.mkdtemp(prefix="exportstats-cwd-"))
    result = _run(["stats"], home, cwd)
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    ctx.check("no traceback", "Traceback" not in result.stderr)
    ctx.check("0 sessions reported", "0 session" in result.stdout)


@test
def test_stats_aggregates_real_session_usage_and_tool_calls(ctx: Ctx):
    home = _fresh_home()
    cwd = Path(tempfile.mkdtemp(prefix="exportstats-cwd-"))
    _make_session_with_secret(home, cwd)
    result = _run(["stats"], home, cwd)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("mentions the model", "or:mock/model" in result.stdout)
    ctx.check("mentions the Bash tool call", "Bash: 1" in result.stdout)
    ctx.check("mentions real cost", "$0.0010" in result.stdout or "0.0010" in result.stdout)


@test
def test_stats_json_output_is_parseable(ctx: Ctx):
    home = _fresh_home()
    cwd = Path(tempfile.mkdtemp(prefix="exportstats-cwd-"))
    _make_session_with_secret(home, cwd)
    result = _run(["stats", "--json"], home, cwd)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    obj = json.loads(result.stdout)
    ctx.check(f"real shape, got {obj!r}", obj.get("sessions") == 1 and "per_model" in obj and "tool_counts" in obj)
    ctx.check("Bash tool call counted", obj["tool_counts"].get("Bash") == 1)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
