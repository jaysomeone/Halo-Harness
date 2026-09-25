"""tests.test_session_cli -- `rolo-claude stats`/`rolo-claude export` (H8
scope D): headless, offline session stats/export CLI subcommands."""
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.agent.log import SessionLog
from rolo_claude import session_cli

test, TESTS = new_registry()


def _build_session_log(cwd: Path, session_id: str = "cli-test-session") -> SessionLog:
    log = SessionLog(cwd, session_id=session_id)
    log.append_meta(model="or:mock/stats-model", cwd=str(cwd), system_prompt_bytes=10, tools=[])
    log.append_system("You are a test assistant. Secret: sk-ant-abcd1234efgh5678ijkl")
    log.append_user([{"type": "text", "text": "hello"}])
    log.append_assistant(content=[{"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "ls"}},
                                    {"type": "text", "text": "done"}], stop_reason="tool_use")
    log.append_tool_result(tool_use_id="call_1", content="file1\nfile2", is_error=False)
    log.append_usage({"input_tokens": 100, "output_tokens": 40}, cost_usd=0.0025)
    return log


@test
def test_stats_summarises_a_real_session_log(ctx: Ctx):
    root = Path(tempfile.mkdtemp(prefix="session-cli-stats-"))
    cwd = root / "proj"
    cwd.mkdir()
    _build_session_log(cwd)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = session_cli.cmd_stats(["--cwd", str(cwd)])
    out = buf.getvalue()
    ctx.check("exit 0", rc == 0)
    ctx.check("names the session id", "cli-test-session" in out)
    ctx.check("shows the model bucket", "or:mock/stats-model" in out)
    ctx.check("shows the tool call count", "Bash: 1 call" in out)
    ctx.check("shows the total cost", "$0.0025" in out)


@test
def test_stats_no_sessions_is_a_clean_error(ctx: Ctx):
    root = Path(tempfile.mkdtemp(prefix="session-cli-stats-empty-"))
    cwd = root / "empty-proj"
    cwd.mkdir()
    buf = io.StringIO()
    with redirect_stdout(io.StringIO()):
        import contextlib
        with contextlib.redirect_stderr(buf):
            rc = session_cli.cmd_stats(["--cwd", str(cwd)])
    ctx.check("non-zero exit, no sessions", rc != 0)
    ctx.check("stderr names the problem", "no sessions found" in buf.getvalue())


@test
def test_export_round_trips_every_node_as_jsonl(ctx: Ctx):
    root = Path(tempfile.mkdtemp(prefix="session-cli-export-"))
    cwd = root / "proj"
    cwd.mkdir()
    log = _build_session_log(cwd)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = session_cli.cmd_export(["--cwd", str(cwd)])
    ctx.check("exit 0", rc == 0)
    lines = [l for l in buf.getvalue().splitlines() if l.strip()]
    ctx.check(f"one JSONL line per logged node, got {len(lines)}", len(lines) == len(log.nodes()))
    parsed = [json.loads(l) for l in lines]
    ctx.check("node types preserved in order",
              [n["type"] for n in parsed] == [n["type"] for n in log.nodes()])
    full_text = buf.getvalue()
    ctx.check("the raw secret IS present without --sanitize", "sk-ant-abcd1234efgh5678ijkl" in full_text)


@test
def test_export_sanitize_redacts_secrets(ctx: Ctx):
    root = Path(tempfile.mkdtemp(prefix="session-cli-export-sanitize-"))
    cwd = root / "proj"
    cwd.mkdir()
    _build_session_log(cwd)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = session_cli.cmd_export(["--cwd", str(cwd), "--sanitize"])
    ctx.check("exit 0", rc == 0)
    text = buf.getvalue()
    ctx.check("the sk-ant- secret is gone", "sk-ant-abcd1234efgh5678ijkl" not in text)
    ctx.check("a redaction marker took its place", "[REDACTED]" in text)
    ctx.check("ordinary content survives sanitisation", "hello" in text and "file1" in text)


@test
def test_export_sanitize_redacts_secret_shaped_field_values(ctx: Ctx):
    """A field literally named `api_key`/`token`/... is redacted by NAME,
    regardless of what its value looks like."""
    root = Path(tempfile.mkdtemp(prefix="session-cli-export-fields-"))
    cwd = root / "proj"
    cwd.mkdir()
    log = SessionLog(cwd, session_id="field-test")
    log.append_meta(model="or:mock/x", cwd=str(cwd), system_prompt_bytes=1, tools=[],
                     api_key="not-secret-shaped-but-named-like-one")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = session_cli.cmd_export(["--cwd", str(cwd), "--sanitize"])
    ctx.check("exit 0", rc == 0)
    text = buf.getvalue()
    ctx.check("the literal field value never appears", "not-secret-shaped-but-named-like-one" not in text)
    ctx.check("redacted by field name", "[REDACTED]" in text)


@test
def test_export_writes_to_a_file_with_output_flag(ctx: Ctx):
    root = Path(tempfile.mkdtemp(prefix="session-cli-export-file-"))
    cwd = root / "proj"
    cwd.mkdir()
    _build_session_log(cwd)
    out_path = root / "exported.jsonl"

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = session_cli.cmd_export(["--cwd", str(cwd), "-o", str(out_path)])
    ctx.check("exit 0", rc == 0)
    ctx.check("file actually written", out_path.exists())
    ctx.check("confirmation message names the file", str(out_path) in buf.getvalue())
    lines = [l for l in out_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    ctx.check(f"file has real JSONL content, got {len(lines)} lines", len(lines) >= 5)


@test
def test_cli_dispatches_stats_and_export_subcommands(ctx: Ctx):
    """`rolo-claude stats`/`rolo-claude export` route through cli.main
    exactly like the existing `models`/`mcp`/`config`/`doctor` subcommands."""
    from rolo_claude import cli
    root = Path(tempfile.mkdtemp(prefix="session-cli-main-"))
    cwd = root / "proj"
    cwd.mkdir()
    _build_session_log(cwd)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["stats", "--cwd", str(cwd)])
    ctx.check("cli.main dispatches to stats", rc == 0 and "Turns:" in buf.getvalue())

    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        rc2 = cli.main(["export", "--cwd", str(cwd)])
    ctx.check("cli.main dispatches to export", rc2 == 0 and '"type": "meta"' in buf2.getvalue())


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
