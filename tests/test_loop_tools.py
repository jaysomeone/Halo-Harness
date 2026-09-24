"""tests.test_loop_tools -- agent/loop.py's real tool dispatch end to end:
the loop breaker (remind at 3, deny at 5, end the turn at 8) with a
scripted repeating tool call, pairing invariants on interrupt (an
unconsumed generator leaves synthetic tool_results, never an unanswered
tool_use), and the Read tool through a full two-model-call turn.
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


def _scn_repeat_read_forever(h, body):
    """Always returns the SAME Read tool call, regardless of how many
    tool_results have already come back -- a model stuck in a loop."""
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_repeat", "type": "function",
             "function": {"name": "Read", "arguments": json.dumps({"file_path": str(Path(tempfile.gettempdir()) / "loop-breaker-target.txt")})}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ])


SCENARIOS["repeat-read-forever"] = _scn_repeat_read_forever


def _run_cli(fh, mock, prompt, extra_args=None, timeout=30, model="or:mock/model"):
    env = dict(os.environ)
    env.update({
        "BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
        "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR),
    })
    args = [sys.executable, "-m", "rolo_claude", "-p", prompt, "--model", model,
            "--cwd", str(fh["proj"]), "--max-turns", "3"] + (extra_args or [])
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


@test
def test_loop_breaker_ends_the_turn_by_8_repeats(ctx: Ctx):
    """The model calls Read with IDENTICAL arguments over and over; the
    loop breaker must remind at 3, deny at 5, and end the turn at 8 --
    never let this run unbounded (--max-turns is a DIFFERENT, higher-level
    guard; this is a per-turn safety net inside a single tool loop)."""
    fh = build_fake_home()
    (Path(tempfile.gettempdir()) / "loop-breaker-target.txt").write_text("target file\n", encoding="utf-8")
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "read the loop breaker target file", extra_args=["--output-format", "json"],
                          model="or:mock/repeat-read-forever")
        ctx.check(f"process exits cleanly (not hung/killed), got {result.returncode}", result.returncode in (0, 1))
        # The loop breaker must have stopped it well short of --max-turns *
        # some-large-number of upstream calls -- 8 calls (the "end" threshold)
        # plus a small margin, not dozens/hundreds.
        ctx.check(f"upstream was called a bounded number of times (<=10), got {len(mock.requests)}", len(mock.requests) <= 10)
        ctx.check(f"upstream was called AT LEAST 8 times (reached the end-turn threshold), got {len(mock.requests)}", len(mock.requests) >= 8)
    finally:
        mock.stop()
        try:
            (Path(tempfile.gettempdir()) / "loop-breaker-target.txt").unlink()
        except OSError:
            pass


@test
def test_loop_breaker_unit_level_thresholds(ctx: Ctx):
    """Exercise Session._dispatch_tools directly against a fresh in-process
    Session so the exact remind/deny/end wording and thresholds (3/5/8) are
    asserted precisely, independent of subprocess/CLI plumbing."""
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session, _LOOP_BREAKER_DENY_AT, _LOOP_BREAKER_END_AT, _LOOP_BREAKER_REMIND_AT
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.providers.stream import ProviderCreds

    ctx.check("thresholds are 3/5/8", (_LOOP_BREAKER_REMIND_AT, _LOOP_BREAKER_DENY_AT, _LOOP_BREAKER_END_AT) == (3, 5, 8))

    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    session_ctx = SessionContext(cwd=fh["proj"], model_label="mock/model")
    model_ref = parse_model_ref("or:mock/model")
    session = Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(), creds=ProviderCreds(base_url="http://x", api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="loop-breaker-unit-")), model_label="mock/model", session_context=session_ctx,
    )
    tool_use = {"type": "tool_use", "id": "call_x", "name": "Read", "input": {"file_path": "/same/path.txt"}}
    outcomes = []
    for i in range(9):
        events_seen = list(session._dispatch_tools(1, [dict(tool_use, id=f"call_{i}")]))
        result_events = [e for e in events_seen if e.kind == "tool_result"]
        outcomes.append(result_events[0].data["summary"] if result_events else None)
    ctx.check("call 1 (count=1): no breaker text", "reminder" not in (outcomes[0] or "") and "Loop breaker" not in (outcomes[0] or ""))
    ctx.check("call 3 (count=3): reminder text present", "reminder" in (outcomes[2] or ""))
    ctx.check("call 5 (count=5): denied", "denied" in (outcomes[4] or "").lower())
    ctx.check("call 8 (count=8): turn-ending breaker text", "ending the turn" in (outcomes[7] or "").lower())


@test
def test_interrupt_leaves_no_unanswered_tool_use(ctx: Ctx):
    """gen.close() mid-turn (an external interrupt) must leave the log
    fully paired -- proving agent/invariants.py's synthesize on GeneratorExit."""
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.invariants import find_unpaired_tool_use_ids
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.providers.stream import ProviderCreds

    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    try:
        session_ctx = SessionContext(cwd=fh["proj"], model_label="mock/repeat-read-forever")
        model_ref = parse_model_ref("or:mock/repeat-read-forever")
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
        session = Session(
            cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(), creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="interrupt-test-")), model_label="mock/repeat-read-forever",
            session_context=session_ctx, openrouter_base_url=mock.base_url,
        )
        gen = session.turn("read the loop target repeatedly")
        next(gen)  # user_message
        next(gen)  # status
        next(gen)  # message_start (first model call under way)
        gen.close()  # simulated interrupt, mid-turn
        ctx.check("no unanswered tool_use ids remain after an interrupt", find_unpaired_tool_use_ids(session.log) == [])
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_read_tool_end_to_end_via_cli(ctx: Ctx):
    """`-p "read <file> and reply with the number of lines"` -> Read tool
    dispatched for real, second model call answers from the tool_result."""
    fh = build_fake_home()
    target = fh["proj"] / "line_count_target.txt"
    target.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
    mock = MockUpstream().start()
    try:
        SCENARIOS["read-then-answer"] = _scn_read_then_answer(str(target))
        env = dict(os.environ)
        env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                    "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
        args = [sys.executable, "-m", "rolo_claude", "-p", f"read {target} and reply with only the number of lines",
                "--model", "or:mock/read-then-answer", "--cwd", str(fh["proj"])]
        result = subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=30)
        ctx.check(f"exit 0, got {result.returncode} (stderr: {result.stderr[-500:]!r})", result.returncode == 0)
        ctx.check(f"answers with the real line count (5), got {result.stdout!r}", "5" in result.stdout)
        ctx.check("two upstream calls (tool call, then the answer)", len(mock.requests) == 2)
    finally:
        mock.stop()


def _scn_read_then_answer(target_path: str):
    def fn(h, body):
        messages = (body or {}).get("messages") or []
        if any(m.get("role") == "tool" for m in messages):
            _finish(h, [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"content": "5"}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])
        else:
            _finish(h, [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": "call_read", "type": "function",
                     "function": {"name": "Read", "arguments": json.dumps({"file_path": target_path})}},
                ]}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ])
    return fn


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
