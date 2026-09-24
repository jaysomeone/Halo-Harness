"""tests.test_loop_headless -- e2e: `python -m rolo_claude -p "..."` against
the mock upstream via BRIDGE_TEST_HOME + BRIDGE_OPENROUTER_BASE_URL. Asserts
the system prompt is byte-stable across two constructions, MEMORY.md content
reaches the actual upstream request, and the `--output-format json` shape.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream

REPO_DIR = Path(__file__).resolve().parent.parent

test, TESTS = new_registry()


def _run_cli(fh, mock, prompt, extra_args=None, extra_env=None, timeout=30):
    env = dict(os.environ)
    env.update({
        "BRIDGE_TEST_HOME": str(fh["home"]),
        "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
        "OPENROUTER_API_KEY": "test-key",
        "PYTHONPATH": str(REPO_DIR),
    })
    if extra_env:
        env.update(extra_env)
    args = [sys.executable, "-m", "rolo_claude", "-p", prompt, "--model", "or:mock/model",
            "--cwd", str(fh["proj"])] + (extra_args or [])
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


@test
def test_print_mode_text_output_says_pong(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "reply with the single word pong")
        ctx.check(f"exit 0, got {result.returncode} (stderr: {result.stderr[-500:]!r})", result.returncode == 0)
        ctx.check(f"stdout mentions pong, got {result.stdout!r}", "pong" in result.stdout)
    finally:
        mock.stop()


@test
def test_memory_md_content_reaches_the_upstream_request(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "what does MEMORY.md say about the Plex server?")
        ctx.check(f"exit 0, got {result.returncode} (stderr: {result.stderr[-500:]!r})", result.returncode == 0)
        ctx.check("at least one request recorded", len(mock.requests) >= 1)
        last_body = mock.requests[-1]["body"] or {}
        system_text = last_body.get("messages", [{}])[0].get("content", "") if last_body.get("messages") else ""
        # The system prompt travels as an OpenAI "system" role message (or
        # folded into the first message) once translated -- check the whole
        # serialized body for the memory text rather than assuming exactly
        # which message index/role carries it.
        whole_body_text = json.dumps(last_body)
        ctx.check("MEMORY.md's roundBoy content reached the actual upstream request",
                  "roundBoy" in whole_body_text)
        ctx.check("the Plex IP reached the actual upstream request",
                  "lan-host.lan" in whole_body_text)
    finally:
        mock.stop()


@test
def test_json_output_shape(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "hi", extra_args=["--output-format", "json"])
        ctx.check(f"exit 0, got {result.returncode} (stderr: {result.stderr[-500:]!r})", result.returncode == 0)
        try:
            obj = json.loads(result.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as e:
            ctx.check(f"stdout is valid JSON, got {result.stdout!r} ({e})", False)
            return
        for key in ("type", "subtype", "is_error", "result", "session_id", "num_turns",
                    "stop_reason", "usage", "total_cost_usd", "model"):
            ctx.check(f"json result has key {key!r}", key in obj)
        ctx.check("type == result", obj.get("type") == "result")
        ctx.check("is_error is False on success", obj.get("is_error") is False)
        ctx.check("result text is 'pong'", obj.get("result") == "pong")
    finally:
        mock.stop()


@test
def test_system_prompt_byte_stable_across_two_constructions(ctx: Ctx):
    """Builds SessionContext (which computes the system prompt) twice with
    identical inputs and confirms byte-for-byte equality -- the actual
    "computed once per session, byte-identical every turn" guarantee is that
    agent.loop.Session never recomputes it after __init__, so this proves
    the INPUT side (build_system_prompt/collect_git_info/memory read) has
    no hidden non-determinism that would make even a fresh computation
    differ."""
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    from rolo_claude.agent.assemble import SessionContext

    ctx1 = SessionContext(cwd=fh["proj"], model_label="or:mock/model")
    ctx2 = SessionContext(cwd=fh["proj"], model_label="or:mock/model")
    ctx.check("system prompts are byte-identical across two constructions",
              ctx1.system_prompt == ctx2.system_prompt)
    ctx.check("system prompt is non-trivial", len(ctx1.system_prompt) > 100)


@test
def test_append_system_prompt_is_appended(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "hi", extra_args=["--append-system-prompt", "MARKER-TEXT-XYZ"])
        ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
        last_body = mock.requests[-1]["body"] or {}
        ctx.check("appended text reached the upstream request",
                  "MARKER-TEXT-XYZ" in json.dumps(last_body))
    finally:
        mock.stop()


@test
def test_verbose_flag_does_not_break_output(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "hi", extra_args=["--verbose"])
        ctx.check(f"exit 0 with --verbose, got {result.returncode}", result.returncode == 0)
        ctx.check("stdout still has the reply", "pong" in result.stdout)
    finally:
        mock.stop()


@test
def test_without_p_flag_prints_tui_notice_exit_2(ctx: Ctx):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_DIR)
    result = subprocess.run([sys.executable, "-m", "rolo_claude"], env=env, cwd=str(REPO_DIR),
                             capture_output=True, text=True, timeout=15)
    ctx.check(f"exit code 2, got {result.returncode}", result.returncode == 2)
    ctx.check("TUI-not-built-yet notice printed", "TUI not built yet" in result.stderr)


@test
def test_version_flag(ctx: Ctx):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_DIR)
    result = subprocess.run([sys.executable, "-m", "rolo_claude", "--version"], env=env, cwd=str(REPO_DIR),
                             capture_output=True, text=True, timeout=15)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check(f"prints rolo-claude 0.3.0, got {result.stdout!r}", result.stdout.strip() == "rolo-claude 0.3.0")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
