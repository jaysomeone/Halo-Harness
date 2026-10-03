"""tests.test_w4a_cli_flags -- W4a item 2: real BEHAVIOR (not just
"accepted, doesn't crash" -- test_cli_flags.py's own job) for a sample of
the 21 flags this round moved out of `_NOT_YET_FLAGS`: `--restricted`
(strips Bash/PowerShell/WebFetch from the catalog), `--brief` (adds
SendUserMessage), `--environment KEY=VALUE` (reaches the tool child env),
`--autocompact` (sets CLAUDE_CODE_AUTO_COMPACT_WINDOW, the knob `agent.
compact.resolve_knobs` already reads). Not exhaustive over all 21 -- see
the worker report for which ones only have the `cli.py`-level acceptance
test and a code-level design note instead of a dedicated behavior test.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, ScriptedTurns

test, TESTS = new_registry()
REPO_DIR = Path(__file__).resolve().parent.parent


def _hermetic_child_env() -> dict:
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    return env


def _run_cli(fh, mock, prompt, extra_args=None, timeout=30, model="or:mock/model"):
    import subprocess
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
    args = [sys.executable, "-m", "halo_harness", "-p", prompt, "--model", model,
            "--cwd", str(fh["proj"])] + (extra_args or [])
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


def _init_tools(result) -> list:
    first_line = json.loads(result.stdout.splitlines()[0])
    return first_line.get("tools") or []


@test
def test_restricted_strips_code_exec_tools_and_webfetch(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "reply with the single word pong",
                           extra_args=["--output-format", "stream-json", "--restricted"])
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr!r}", result.returncode == 0)
        tools = _init_tools(result)
        ctx.check(f"Bash removed, got {tools}", "Bash" not in tools)
        ctx.check(f"WebFetch removed, got {tools}", "WebFetch" not in tools)
        ctx.check("Read still present (restricted only removes code-exec tools)", "Read" in tools)
    finally:
        mock.stop()


@test
def test_restricted_keeps_a_tool_explicitly_named_in_tools_flag(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "reply with the single word pong",
                           extra_args=["--output-format", "stream-json", "--restricted",
                                       "--tools", "Read,Bash"])
        ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
        tools = _init_tools(result)
        ctx.check(f"Bash kept (explicitly named in --tools), got {tools}", "Bash" in tools)
    finally:
        mock.stop()


@test
def test_brief_adds_senduser_message_tool(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "reply with the single word pong",
                           extra_args=["--output-format", "stream-json", "--brief"])
        ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
        tools = _init_tools(result)
        ctx.check(f"SendUserMessage present, got {tools}", "SendUserMessage" in tools)

        without = _run_cli(fh, mock, "reply with the single word pong", extra_args=["--output-format", "stream-json"])
        ctx.check("SendUserMessage absent without --brief", "SendUserMessage" not in _init_tools(without))
    finally:
        mock.stop()


@test
def test_f_parity_brief_message_is_printed_in_plain_text_output(ctx: Ctx):
    """parity gap (W6a): `--brief`'s whole point is running commentary
    BEFORE the final answer -- SendUserMessage's own text used to be only
    a tool result (TUI transcript/--verbose/stream-json), invisible in
    the single most common case: `-p`, plain text, non-verbose. Must now
    be printed as it happens, even without --verbose, ahead of the
    turn's real final answer."""
    import json as _json

    SCENARIOS["w4a-brief-text"] = ScriptedTurns([
        [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
         {"choices": [{"index": 0, "delta": {"tool_calls": [
             {"index": 0, "id": "call_sum", "type": "function",
              "function": {"name": "SendUserMessage",
                            "arguments": _json.dumps({"message": "working on it now"})}},
         ]}}]},
         {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}],
        [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
         {"choices": [{"index": 0, "delta": {"content": "final answer here"}}]},
         {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}],
    ])
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "do the thing and tell me how it's going",
                           extra_args=["--brief"], model="or:mock/w4a-brief-text")
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr!r}", result.returncode == 0)
        ctx.check(f"the SendUserMessage text was printed, got stdout={result.stdout!r}",
                  "working on it now" in result.stdout)
        ctx.check(f"the real final answer still came through too, got stdout={result.stdout!r}",
                  "final answer here" in result.stdout)
    finally:
        mock.stop()


@test
def test_environment_flag_reaches_the_bash_tool_env(ctx: Ctx):
    from tests.helpers.mock_openai import SCENARIOS, ScriptedTurns
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        SCENARIOS["w4a-env-probe"] = ScriptedTurns([
            [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
             {"choices": [{"index": 0, "delta": {"tool_calls": [
                 {"index": 0, "id": "c1", "type": "function",
                  "function": {"name": "Bash", "arguments": json.dumps({"command": "echo $W4A_PROBE_VAR"})}}]}}]},
             {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}],
            [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
             {"choices": [{"index": 0, "delta": {"content": "done"}}]},
             {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}],
        ])
        result = _run_cli(fh, mock, "echo the env var", model="or:mock/w4a-env-probe",
                           extra_args=["--environment", "W4A_PROBE_VAR=hello-w4a", "--verbose"])
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr!r}", result.returncode == 0)
    finally:
        mock.stop()


@test
def test_autocompact_reaches_cli_flags_and_never_touches_the_env_var(ctx: Ctx):
    """Release review finding 34: `--autocompact <tokens>` used to write
    straight into `os.environ["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]`, the
    LOWEST (shell) layer of `settings.effective_env` -- a settings.json
    `env` block setting the SAME name silently outranked this explicit
    flag. It now flows through `cli_flags["autocompact"]` only (see
    `agent.compact.resolve_knobs`'s own precedence test in
    test_compact.py); `cli.py` must never set the env var at all any
    more, whatever the value. In-process (no subprocess/network needed):
    handled entirely in `cli.py main()` before either run_print_mode or
    the TUI starts -- `--demo -p` drives argument parsing through to that
    point without a real model call."""
    from halo_harness.cli import _build_parser, main
    from halo_harness.cli_flags import cli_flags_from_args

    args = _build_parser().parse_args(["--autocompact", "123000", "--demo", "-p"])
    ctx.check(f"the raw value reaches cli_flags unchanged, got {cli_flags_from_args(args).get('autocompact')!r}",
              cli_flags_from_args(args).get("autocompact") == "123000")

    old = os.environ.pop("CLAUDE_CODE_AUTO_COMPACT_WINDOW", None)
    try:
        main(["--autocompact", "123000", "--demo", "-p"])
        ctx.check("cli.py never sets the env var any more, whatever the value",
                  "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in os.environ)
    finally:
        if old is not None:
            os.environ["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = old


@test
def test_autocompact_auto_is_a_noop(ctx: Ctx):
    from halo_harness.cli import main
    old = os.environ.pop("CLAUDE_CODE_AUTO_COMPACT_WINDOW", None)
    try:
        main(["--autocompact", "auto", "--demo", "-p"])
        ctx.check("auto never sets the env var", "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in os.environ)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CODE_AUTO_COMPACT_WINDOW", None)
        else:
            os.environ["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = old


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
