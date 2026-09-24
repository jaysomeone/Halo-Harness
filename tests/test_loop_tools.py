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


def _scn_page_forever(h, body):
    """Read the SAME file with a DIFFERENT (ever-increasing) offset every
    call -- finding 9's own failure scenario ("a model that pages through a
    large file by offset ... runs and bills indefinitely in -p"). Each call
    is canonically DIFFERENT, so the identical-args loop breaker (rule 8)
    never trips; only --max-turns (model calls per turn) can stop this."""
    messages = (body or {}).get("messages") or []
    offset = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "tool") * 100
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": f"call_page_{offset}", "type": "function",
             "function": {"name": "Read", "arguments": json.dumps(
                 {"file_path": str(Path(tempfile.gettempdir()) / "loop-breaker-target.txt"), "offset": offset})}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ])


SCENARIOS["page-forever"] = _scn_page_forever


def _scn_page_until_no_tools(h, body):
    """Like `_scn_page_forever`, but a COMPLIANT model: once a request
    arrives with no `tools` field at all (H5's MAX_STEPS_PROMPT wrap-up
    call withholds it), reply with a plain text summary instead of another
    tool call -- proving the wrap-up call, when the model actually behaves,
    ends the turn with real text rather than another tool_use."""
    if not (body or {}).get("tools"):
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": "Reached the maximum number of steps; summary: paged through the file."}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])
        return
    messages = (body or {}).get("messages") or []
    offset = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "tool") * 100
    _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": f"call_page_{offset}", "type": "function",
             "function": {"name": "Read", "arguments": json.dumps(
                 {"file_path": str(Path(tempfile.gettempdir()) / "loop-breaker-target.txt"), "offset": offset})}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ])


SCENARIOS["page-until-no-tools"] = _scn_page_until_no_tools


def _run_cli(fh, mock, prompt, extra_args=None, timeout=30, model="or:mock/model", max_turns=20):
    # finding 9: --max-turns now counts MODEL CALLS made within the one
    # turn `-p` runs (Claude Code semantics), not `turn()` invocations --
    # the pre-H2 value here (3) relied on the OLD (buggy) meaning to avoid
    # cutting the loop-breaker test below short; the default is now well
    # above the loop breaker's own 8-call end threshold so THIS helper
    # keeps testing the loop breaker, not --max-turns (see
    # test_max_turns_caps_model_calls_per_turn for the flag itself).
    env = dict(os.environ)
    env.update({
        "BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
        "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR),
    })
    args = [sys.executable, "-m", "rolo_claude", "-p", prompt, "--model", model,
            "--cwd", str(fh["proj"]), "--max-turns", str(max_turns)] + (extra_args or [])
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
def test_max_turns_caps_model_calls_per_turn(ctx: Ctx):
    """finding 9: --max-turns counts MODEL CALLS made within the ONE turn
    `-p` runs, not `turn()` invocations (which `-p` only ever calls once,
    so the pre-H2 semantics never actually bounded anything inside a tool
    loop). A DIFFERENT tool call every time (paging by offset) never trips
    the identical-args loop breaker, so ONLY --max-turns can stop it here.

    H5 scope F item 9 (OpenCode Appendix H): once the cap is hit, the loop
    now makes ONE MORE call -- MAX_STEPS_PROMPT injected, `tools` withheld
    from the wire entirely -- so the model gets a real chance to summarise
    instead of just being cut off; --max-turns=4 therefore means 5 upstream
    calls (4 tool-calling + 1 wrap-up), and that final call's request must
    carry no `tools` field at all. This mock always scripts a tool_use
    reply regardless of what it's asked (it doesn't simulate a model that
    reads MAX_STEPS_PROMPT), so `stop_reason` is still "tool_use" here --
    test_max_turns_wrap_up_call_gets_a_text_reply below covers the case
    where the model DOES comply."""
    fh = build_fake_home()
    (Path(tempfile.gettempdir()) / "loop-breaker-target.txt").write_text("target file\n", encoding="utf-8")
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "page through the file", extra_args=["--output-format", "json", "--verbose"],
                           model="or:mock/page-forever", max_turns=4)
        ctx.check(f"process exits cleanly, got {result.returncode}", result.returncode in (0, 1))
        ctx.check(f"upstream called max_turns + 1 wrap-up call (5), got {len(mock.requests)}", len(mock.requests) == 5)
        ctx.check("the wrap-up call's own request body carries no tools field",
                  "tools" not in (mock.requests[-1].get("body") or {}))
        obj = json.loads(result.stdout)
        ctx.check(f"json result reports a stop_reason, got {obj.get('stop_reason')!r}",
                   obj.get("stop_reason") in ("tool_use", "end_turn"))
    finally:
        mock.stop()
        try:
            (Path(tempfile.gettempdir()) / "loop-breaker-target.txt").unlink()
        except OSError:
            pass


@test
def test_max_turns_wrap_up_call_gets_a_text_reply(ctx: Ctx):
    """H5 scope F item 9: with a model that actually honours "no tools
    offered -> reply with text", the wrap-up call ends the turn with a
    real text summary (stop_reason "end_turn"), not another tool_use --
    the MAX_STEPS_PROMPT snapshot did its job."""
    fh = build_fake_home()
    (Path(tempfile.gettempdir()) / "loop-breaker-target.txt").write_text("target file\n", encoding="utf-8")
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "page through the file", extra_args=["--output-format", "json", "--verbose"],
                           model="or:mock/page-until-no-tools", max_turns=3)
        ctx.check(f"process exits cleanly, got {result.returncode}", result.returncode in (0, 1))
        ctx.check(f"3 tool-calling rounds + 1 wrap-up = 4 requests, got {len(mock.requests)}", len(mock.requests) == 4)
        obj = json.loads(result.stdout)
        ctx.check(f"the turn ends cleanly once the model complies, got stop_reason={obj.get('stop_reason')!r}",
                   obj.get("stop_reason") == "end_turn")
        ctx.check(f"the wrap-up text made it into the result, got {obj.get('result')!r}",
                   "summary" in (obj.get("result") or "").lower())
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
    """finding 16 (test #4 from the review's required list): an interrupt
    AFTER `tool_use_ready` (the assistant node WITH the tool_use is
    already logged -- unlike closing at `message_start`, before any
    tool_use exists at all, which is vacuously true regardless of whether
    synthesis actually works) must leave a REAL synthetic error result
    behind, not just an absence of unpaired ids that was never at risk."""
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.invariants import INTERRUPTED_MESSAGE, find_unpaired_tool_use_ids
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
        seen = []
        tool_use_id = None
        for event in gen:
            seen.append(event)
            if event.kind == "tool_use_ready":
                tool_use_id = event.data.get("id")
                break
        ctx.check("reached tool_use_ready before closing (the assistant node WITH the tool_use is now logged)",
                  tool_use_id is not None)
        gen.close()  # simulated interrupt, mid-dispatch -- BEFORE the real tool ran or a result was written
        ctx.check("no unanswered tool_use ids remain after an interrupt", find_unpaired_tool_use_ids(session.log) == [])
        result_nodes = [n for n in session.log.nodes() if n.get("type") == "tool_result" and n.get("tool_use_id") == tool_use_id]
        ctx.check(f"a REAL synthetic tool_result was written for {tool_use_id!r}, got {result_nodes}", len(result_nodes) == 1)
        ctx.check("it's tagged as an error", result_nodes[0].get("is_error") is True)
        ctx.check(f"content names the interruption, got {result_nodes[0].get('content')!r}",
                   INTERRUPTED_MESSAGE in str(result_nodes[0].get("content")))
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


@test
def test_finding_15_narrating_model_prints_only_the_final_answer(ctx: Ctx):
    """finding 15: a model that narrates ("Let me read the file.") before
    its tool_calls, then answers cleanly on the second call, must print
    ONLY the final answer in non-verbose text mode -- not
    "Let me read the file.5" (every intermediate message concatenated)."""
    fh = build_fake_home()
    target = fh["proj"] / "narrate_target.txt"
    target.write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8")
    mock = MockUpstream().start()

    def _scn_narrate_then_read(h, body):
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
                {"choices": [{"index": 0, "delta": {"content": "Let me read the file."}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": "call_narrate", "type": "function",
                     "function": {"name": "Read", "arguments": json.dumps({"file_path": str(target)})}},
                ]}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ])

    SCENARIOS["narrate-then-read"] = _scn_narrate_then_read
    try:
        result = _run_cli(fh, mock, f"read {target} and reply with only the number of lines",
                           model="or:mock/narrate-then-read")
        ctx.check(f"exit 0, got {result.returncode} (stderr: {result.stderr[-500:]!r})", result.returncode == 0)
        ctx.check(f"stdout is ONLY the final answer, got {result.stdout!r}", result.stdout.strip() == "5")
        ctx.check('the narration text never reached stdout', "Let me read" not in result.stdout)
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


# ---- finding 5: _mcp_blocks_for_log / _summary_text_for_blocks -----------
# Pure-function unit tests (no Session/subprocess needed) for
# agent/loop.py's own block-content finalize helpers.

@test
def test_mcp_blocks_for_log_preserves_a_real_image_block(ctx: Ctx):
    """finding 5: a vision image must stay a REAL image block through
    logging -- the old code flattened it to `[image: mime]` text before
    it ever reached the log/wire, so a vision-capable model never saw it."""
    from rolo_claude.agent.loop import _mcp_blocks_for_log
    blocks = [{"type": "text", "text": "here's a screenshot"},
              {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QQ=="}}]
    logged = _mcp_blocks_for_log(blocks, meta=None, session_dir=None, tool_use_id=None)
    images = [b for b in logged if b.get("type") == "image"]
    ctx.check(f"the image block survives untouched, got {logged}",
              len(images) == 1 and images[0]["source"]["data"] == "QQ==")


@test
def test_mcp_blocks_for_log_strips_tool_reference(ctx: Ctx):
    """finding 5: a `tool_reference` block (ToolSearch's own load-
    confirmation marker) must never appear in a TOOL RESULT's logged
    content -- it's not a real answer, and the old code rendered it as
    the literal string "[tool_reference block]"."""
    from rolo_claude.agent.loop import _mcp_blocks_for_log
    blocks = [{"type": "text", "text": "real answer"}, {"type": "tool_reference", "tool_name": "mcp__x__y"}]
    logged = _mcp_blocks_for_log(blocks, meta=None, session_dir=None, tool_use_id=None)
    ctx.check(f"tool_reference dropped entirely, got {logged}",
              not any(b.get("type") == "tool_reference" for b in logged))
    ctx.check("the real text block survives", any(b.get("text") == "real answer" for b in logged))


@test
def test_mcp_blocks_for_log_caps_once_with_spill_note(ctx: Ctx):
    """finding 5: capping happens ONCE here (via cap_and_spill), and the
    truncation line names the spill file -- no second, independent,
    hard-coded cut on top."""
    import os
    from rolo_claude.agent.loop import _mcp_blocks_for_log
    old = os.environ.get("MAX_MCP_OUTPUT_TOKENS")
    os.environ["MAX_MCP_OUTPUT_TOKENS"] = "50"
    try:
        with tempfile.TemporaryDirectory() as td:
            blocks = [{"type": "text", "text": "Z" * 5000}]
            logged = _mcp_blocks_for_log(blocks, meta=None, session_dir=Path(td), tool_use_id="toolu_x")
            joined = " ".join(b.get("text", "") for b in logged)
            ctx.check(f"Claude Code's exact truncation string, intact, got tail={joined[-120:]!r}",
                      "[OUTPUT TRUNCATED - exceeded 50 token limit]" in joined)
            ctx.check(f"names the spill file, got tail={joined[-160:]!r}", "Full output saved to" in joined
                      and str(Path(td) / "tool-results" / "toolu_x.txt") in joined)
            spill_path = Path(td) / "tool-results" / "toolu_x.txt"
            ctx.check("the spill file holds the FULL untruncated text",
                      spill_path.exists() and len(spill_path.read_text(encoding="utf-8")) == 5000)
    finally:
        if old is None:
            os.environ.pop("MAX_MCP_OUTPUT_TOKENS", None)
        else:
            os.environ["MAX_MCP_OUTPUT_TOKENS"] = old


@test
def test_summary_text_for_blocks_prefers_real_text(ctx: Ctx):
    from rolo_claude.agent.loop import _summary_text_for_blocks
    ctx.check("first real text block wins", _summary_text_for_blocks(
        [{"type": "image", "source": {}}, {"type": "text", "text": "hello"}]) == "hello")
    ctx.check("no text -> an honest placeholder naming the content type",
              _summary_text_for_blocks([{"type": "image", "source": {}}]) == "[image]")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
