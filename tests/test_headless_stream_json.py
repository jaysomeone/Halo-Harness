"""tests.test_headless_stream_json -- rolo_claude/headless.py + output.py
(U0 scope F): `--output-format stream-json` line order/shapes,
`--input-format stream-json` (one turn per stdin line, ONE init line for
the whole run), `--max-budget-usd` early exit, `--json-schema` ->
`structured_output`, `--include-partial-messages`, `--replay-user-messages`,
`--system-prompt[-file]`/`--append-system-prompt-file` full-replacement/
append reaching the real upstream request, and `--setting-sources`
restricting which settings layers load.
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
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, _finish

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _scn_cost_reply(h, body):
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": "expensive reply"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.05}},
    ])


def _scn_slow_then_reply(h, body):
    """A deliberately slow, multi-chunk stream (finding 10's own test
    need: a real wall-clock window during which a second stdin line can
    be written while the FIRST turn is still in flight) -- once the
    request's messages mention "steer-please", replies immediately and
    briefly instead, so the SECOND (steered-in) request resolves fast."""
    import time as _time
    whole = json.dumps(body)
    if "steer-please" in whole:
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": "steered"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])
        return
    from tests.helpers.mock_openai import start_sse, write_sse_chunk, end_sse
    start_sse(h)
    write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
    for piece in ("slow ", "streaming ", "reply "):
        _time.sleep(0.4)
        write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {"content": piece}}]})
    write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    end_sse(h)


SCENARIOS["slow-then-reply"] = _scn_slow_then_reply


def _scn_json_reply(h, body):
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": '{"name": "Ada", "age": 7}'}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])


def _run_cli(fh, mock, prompt=None, extra_args=None, stdin_text=None, timeout=30):
    env = dict(os.environ)
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
    args = [sys.executable, "-m", "rolo_claude", "-p"]
    if prompt is not None:
        args.append(prompt)
    args += ["--model", "or:mock/model", "--cwd", str(fh["proj"])] + (extra_args or [])
    return subprocess.run(args, input=stdin_text, env=env, cwd=str(REPO_DIR), capture_output=True,
                           text=True, timeout=timeout)


@test
def test_stream_json_line_order_and_shapes(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "reply with the single word pong", extra_args=["--output-format", "stream-json"])
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-300:]!r}", result.returncode == 0)
        lines = [json.loads(l) for l in result.stdout.strip().splitlines()]
        types = [l["type"] for l in lines]
        ctx.check(f"init line first, got {types!r}", lines[0]["type"] == "system" and lines[0]["subtype"] == "init")
        ctx.check("assistant appears", "assistant" in types)
        ctx.check(f"result line last, got {types!r}", types[-1] == "result")
        ctx.check("assistant before result", types.index("assistant") < types.index("result"))

        init_line = lines[0]
        for key in ("session_id", "cwd", "model", "permissionMode", "tools", "mcp_servers",
                    "slash_commands", "rolo_claude_version"):
            ctx.check(f"init line has {key!r}", key in init_line)

        assistant_line = lines[types.index("assistant")]
        content = assistant_line["message"]["content"]
        ctx.check(f"assistant text block has pong, got {content!r}",
                  any(b.get("type") == "text" and "pong" in b.get("text", "") for b in content))

        result_line = lines[-1]
        ctx.check("result subtype success", result_line.get("subtype") == "success")
        ctx.check("result is_error False", result_line.get("is_error") is False)
        ctx.check("result has structured_output key (None here, no --json-schema)",
                  "structured_output" in result_line and result_line["structured_output"] is None)
    finally:
        mock.stop()


@test
def test_input_format_stream_json_two_turns(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        stdin_payload = "\n".join([
            json.dumps({"type": "user", "message": {"role": "user", "content": "first turn"}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": "second turn"}}),
        ]) + "\n"
        result = _run_cli(fh, mock, prompt=None, stdin_text=stdin_payload,
                           extra_args=["--input-format", "stream-json", "--output-format", "stream-json"])
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-300:]!r}", result.returncode == 0)
        lines = [json.loads(l) for l in result.stdout.strip().splitlines()]
        inits = [l for l in lines if l["type"] == "system" and l.get("subtype") == "init"]
        results = [l for l in lines if l["type"] == "result"]
        ctx.check(f"exactly ONE init line for the whole run, got {len(inits)}", len(inits) == 1)
        ctx.check(f"two result lines (one per turn), got {len(results)}", len(results) == 2)
        ctx.check(f"mock upstream saw both turns, got {len(mock.requests)} requests", len(mock.requests) >= 2)
    finally:
        mock.stop()


@test
def test_h5b_f10_stdin_held_open_does_not_deadlock(ctx: Ctx):
    """finding 10 (major, h4-h5-h3c review): an SDK-style client that
    writes ONE stream-json line and waits for that turn's own `result`
    line before writing the next one must NOT deadlock -- the old code
    read stdin to EOF (i.e. until the pipe is CLOSED) before running
    anything at all, so a client holding stdin open like this would hang
    forever with the old code, never seeing turn 1's result."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    env = dict(os.environ)
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
    proc = subprocess.Popen(
        [sys.executable, "-m", "rolo_claude", "-p", "--model", "or:mock/model", "--cwd", str(fh["proj"]),
         "--input-format", "stream-json", "--output-format", "stream-json"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, cwd=str(REPO_DIR), text=True, bufsize=1,
    )
    try:
        proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": "first turn"}}) + "\n")
        proc.stdin.flush()
        # Read stdout lines until turn 1's own "result" line -- with a
        # hard deadline, so a genuine deadlock fails the test instead of
        # hanging the whole suite forever.
        import time as _time
        deadline = _time.monotonic() + 20
        got_result = False
        while _time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            obj = json.loads(line)
            if obj.get("type") == "result":
                got_result = True
                break
        ctx.check("turn 1's result arrived WITHOUT closing stdin first (no deadlock)", got_result is True)

        # Stdin is STILL open at this point -- prove a second line still
        # works (queued as turn 2, since turn 1 already finished by now).
        proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": "second turn"}}) + "\n")
        proc.stdin.flush()
        # NOTE: don't manually .close() proc.stdin here -- communicate()
        # (called with no `input`) already flushes+closes stdin itself so
        # the child still sees EOF, but CPython's own POSIX _communicate
        # calls stdin.flush() unguarded before closing, while its Windows
        # _communicate only calls the idempotent .close(); a pre-emptive
        # manual close here is fine on Windows (repeat-close is a no-op)
        # but raises ValueError: I/O operation on closed file on POSIX
        # (Linux/WSL) -- a stdlib platform difference, not a product bug.
        remaining_out, remaining_err = proc.communicate(timeout=20)
        ctx.check(f"process exits cleanly, got {proc.returncode} stderr={remaining_err[-500:]!r}", proc.returncode == 0)
        ctx.check(f"mock upstream saw both turns, got {len(mock.requests)} requests", len(mock.requests) >= 2)
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        mock.stop()


@test
def test_h5c_f10_line_written_while_a_turn_is_in_flight_is_a_real_steer(ctx: Ctx):
    """H5c finding 24 (test-quality): the OLD version of this test (named
    `test_h5b_f10_line_written_while_a_turn_is_in_flight_is_never_lost`)
    deliberately accepted EITHER "steer" or "queued as the next turn" as
    passing, papering over which one actually happened. The line is
    written well inside the slow scenario's ~1.2s of remaining streaming
    time (right after the FIRST delta, with 3 more 0.4s-spaced chunks
    still to come) -- with finding 10 correctly implemented, `session.
    busy` is unambiguously still True at that point, so this must
    deterministically be a STEER folded into turn 1, never a second,
    independent turn: exactly ONE `result` line for the whole run (a
    second turn would produce two), and exactly 2 upstream requests (the
    original slow one, cut short, plus the steered-in one) -- never 3."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    env = dict(os.environ)
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
    proc = subprocess.Popen(
        [sys.executable, "-m", "rolo_claude", "-p", "--model", "or:mock/slow-then-reply", "--cwd", str(fh["proj"]),
         "--input-format", "stream-json", "--output-format", "stream-json", "--include-partial-messages"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, cwd=str(REPO_DIR), text=True, bufsize=1,
    )
    try:
        proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": "do the slow thing"}}) + "\n")
        proc.stdin.flush()
        # Wait for the FIRST streamed text chunk (proof the slow turn has
        # genuinely started, i.e. session.busy is True), THEN write the
        # second line while it is still streaming.
        import time as _time
        deadline = _time.monotonic() + 20
        saw_first_delta = False
        while _time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            obj = json.loads(line)
            if obj.get("type") == "stream_event":
                saw_first_delta = True
                break
        ctx.check("saw the slow turn actually start streaming", saw_first_delta is True)

        proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": "steer-please"}}) + "\n")
        proc.stdin.flush()
        # NOTE: see the sibling test above -- communicate() already
        # closes stdin itself; a manual close first is a stdlib platform
        # trap (ValueError on POSIX, silent no-op on Windows), not a
        # product bug.
        remaining_out, remaining_err = proc.communicate(timeout=25)
        ctx.check(f"process exits cleanly, got {proc.returncode} stderr={remaining_err[-500:]!r}", proc.returncode == 0)
        whole_output = remaining_out
        ctx.check(f"the steer text reached the real upstream, got {len(mock.requests)} requests",
                  any("steer-please" in json.dumps(r["body"]) for r in mock.requests))
        ctx.check("the steered reply text made it into the output", "steered" in whole_output)
        ctx.check(f"exactly 2 upstream requests (the cut-short original + the steer), got {len(mock.requests)}",
                  len(mock.requests) == 2)
        all_lines = [json.loads(l) for l in whole_output.strip().splitlines()]
        result_lines = [l for l in all_lines if l.get("type") == "result"]
        ctx.check(f"exactly ONE result line for the whole run (a steer within turn 1, never a second turn), "
                  f"got {len(result_lines)}", len(result_lines) == 1)
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        mock.stop()


@test
def test_h5c_f13_line_written_during_a_stop_hook_then_stdin_closed_still_runs(ctx: Ctx):
    """H5c finding 13: the stream-json reader queues `None` at stdin EOF
    independently of whether the CURRENT turn has finished yet. A line
    written while that turn's own Stop hook is still sleeping becomes a
    LEFTOVER steer (the per-step steer check runs BEFORE the Stop hook,
    so a steer arriving DURING it is never applied within the turn --
    `Session.turn()`'s own `finally` moves it to `_leftover_steer_texts`
    once the hook finally returns and the turn ends) -- if stdin is
    closed in that same instant, the reader thread's `None` can land in
    the queue BEFORE the main loop re-queues that leftover, and a plain
    FIFO queue would then stop at `None` without ever reading it.
    Verified with the CLI: exit 0, a single result, and the line was
    dropped entirely. A real `.claude/settings.json` Stop hook (the
    `sleep` test script) makes this deterministic -- the turn cannot end
    until the hook itself returns, so the write is provably DURING it."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    (fh["proj"] / ".claude").mkdir(parents=True, exist_ok=True)
    (fh["proj"] / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {"Stop": [{"matcher": "", "hooks": [
            {"type": "command", "args": [sys.executable, "-m", "tests.helpers.hook_scripts", "sleep"]},
        ]}]},
    }), encoding="utf-8")
    SCENARIOS["hook-stop-sleep-reply"] = lambda h, body: _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": "first done"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])
    env = dict(os.environ)
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR), "HOOK_SLEEP_S": "3"})
    proc = subprocess.Popen(
        [sys.executable, "-m", "rolo_claude", "-p", "--model", "or:mock/hook-stop-sleep-reply",
         "--cwd", str(fh["proj"]), "--input-format", "stream-json", "--output-format", "stream-json"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, cwd=str(REPO_DIR), text=True, bufsize=1,
    )
    try:
        proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": "first turn"}}) + "\n")
        proc.stdin.flush()
        # The model's own reply is near-instant; the Stop hook is what
        # takes 3s. Give the reply time to land and the hook time to
        # actually start sleeping, then write the second line and close
        # stdin immediately -- reproducing "a line written during a Stop
        # hook, followed by closing stdin" as closely as a black-box CLI
        # test can.
        import time as _time
        _time.sleep(1.0)
        proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": "second line during stop hook"}}) + "\n")
        proc.stdin.flush()
        remaining_out, remaining_err = proc.communicate(timeout=25)
        ctx.check(f"process exits cleanly, got {proc.returncode} stderr={remaining_err[-500:]!r}", proc.returncode == 0)
        ctx.check(f"the second line was NEVER dropped -- it reached the real upstream, got "
                  f"{len(mock.requests)} requests", any(
                      "second line during stop hook" in json.dumps(r["body"]) for r in mock.requests))
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        mock.stop()


@test
def test_max_budget_usd_stops_and_reports_error(ctx: Ctx):
    fh = build_fake_home()
    SCENARIOS["cost-reply"] = _scn_cost_reply
    mock = MockUpstream().start()
    try:
        env = dict(os.environ)
        env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                    "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
        args = [sys.executable, "-m", "rolo_claude", "-p", "spend money", "--model", "or:mock/cost-reply",
                "--cwd", str(fh["proj"]), "--output-format", "json", "--max-budget-usd", "0.01"]
        result = subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=30)
        ctx.check(f"exit 1 (is_error), got {result.returncode}", result.returncode == 1)
        obj = json.loads(result.stdout.strip())
        ctx.check(f"subtype is error_max_budget_usd, got {obj.get('subtype')!r}", obj.get("subtype") == "error_max_budget_usd")
        ctx.check("is_error True", obj.get("is_error") is True)
    finally:
        mock.stop()
        SCENARIOS.pop("cost-reply", None)


@test
def test_json_schema_produces_structured_output(ctx: Ctx):
    fh = build_fake_home()
    SCENARIOS["json-reply"] = _scn_json_reply
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "give me a person as JSON", extra_args=[
            "--model", "or:mock/json-reply", "--output-format", "json", "--json-schema",
            '{"type":"object","properties":{"name":{"type":"string"}}}',
        ])
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-300:]!r}", result.returncode == 0)
        obj = json.loads(result.stdout.strip())
        ctx.check(f"structured_output parsed from the reply, got {obj.get('structured_output')!r}",
                  obj.get("structured_output") == {"name": "Ada", "age": 7})
    finally:
        mock.stop()
        SCENARIOS.pop("json-reply", None)


@test
def test_json_schema_instruction_reaches_upstream_system_prompt(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "hi", extra_args=["--json-schema", '{"type":"object"}'])
        ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
        whole_body = json.dumps(mock.requests[-1]["body"])
        ctx.check("schema instruction reached the real request", "Respond with JSON matching this schema" in whole_body)
    finally:
        mock.stop()


@test
def test_include_partial_messages_emits_stream_event_lines(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "reply with the single word pong",
                           extra_args=["--output-format", "stream-json", "--include-partial-messages"])
        ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
        lines = [json.loads(l) for l in result.stdout.strip().splitlines()]
        stream_events = [l for l in lines if l["type"] == "stream_event"]
        ctx.check(f"at least one stream_event line, got {len(stream_events)}", len(stream_events) >= 1)
    finally:
        mock.stop()


@test
def test_replay_user_messages_echoes_input(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        stdin_payload = json.dumps({"type": "user", "message": {"role": "user", "content": "echo-me-please"}}) + "\n"
        result = _run_cli(fh, mock, prompt=None, stdin_text=stdin_payload, extra_args=[
            "--input-format", "stream-json", "--output-format", "stream-json", "--replay-user-messages",
        ])
        ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
        lines = [json.loads(l) for l in result.stdout.strip().splitlines()]
        user_echoes = [l for l in lines if l["type"] == "user" and l.get("message", {}).get("content") == "echo-me-please"]
        ctx.check("the user message was echoed back on stdout", len(user_echoes) == 1)
    finally:
        mock.stop()


@test
def test_system_prompt_full_replacement_reaches_upstream(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "hi", extra_args=["--system-prompt", "You only ever say BANANA-MARKER."])
        ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
        whole_body = json.dumps(mock.requests[-1]["body"])
        ctx.check("the FULL replacement text reached upstream", "BANANA-MARKER" in whole_body)
    finally:
        mock.stop()


@test
def test_append_system_prompt_file_reaches_upstream(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    tmp = Path(tempfile.mkdtemp(prefix="rolo-claude-append-"))
    marker_file = tmp / "extra.txt"
    marker_file.write_text("PINEAPPLE-APPEND-MARKER", encoding="utf-8")
    try:
        result = _run_cli(fh, mock, "hi", extra_args=["--append-system-prompt-file", str(marker_file)])
        ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
        whole_body = json.dumps(mock.requests[-1]["body"])
        ctx.check("the appended file's text reached upstream", "PINEAPPLE-APPEND-MARKER" in whole_body)
    finally:
        mock.stop()


@test
def test_setting_sources_restricts_project_layer(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    project_settings = fh["proj"] / ".claude" / "settings.json"
    project_settings.parent.mkdir(parents=True, exist_ok=True)
    project_settings.write_text(json.dumps({"permissions": {"defaultMode": "acceptEdits"}}), encoding="utf-8")
    try:
        with_project = _run_cli(fh, mock, "hi", extra_args=["--output-format", "stream-json"])
        without_project = _run_cli(fh, mock, "hi", extra_args=["--output-format", "stream-json",
                                                                  "--setting-sources", "user"])
        mode_with = json.loads(with_project.stdout.splitlines()[0])["permissionMode"]
        mode_without = json.loads(without_project.stdout.splitlines()[0])["permissionMode"]
        ctx.check(f"project settings honored by default, got {mode_with!r}", mode_with == "acceptEdits")
        # fake_home's OWN userSettings sets permissions.defaultMode: "auto" --
        # with the project layer excluded, that's what's left standing.
        ctx.check(f"--setting-sources user excludes project, falls back to user's own auto, got {mode_without!r}",
                  mode_without == "auto")
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
