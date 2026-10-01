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
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()

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
    """Like `_scn_page_forever`, but a COMPLIANT model: once a request's
    own `tool_choice` is `"none"` (finding 18: the MAX_STEPS_PROMPT
    wrap-up call keeps the full `tools` catalog on the wire -- an Anthropic
    route 400s on "tool_use ... must define tools" if history has tool_use
    blocks but the request's own `tools` is empty/absent -- and instead
    forbids a NEW call via `tool_choice: "none"`), reply with a plain text
    summary instead of another tool call -- proving the wrap-up call, when
    the model actually behaves, ends the turn with real text rather than
    another tool_use."""
    if (body or {}).get("tool_choice") == "none":
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
    env = _hermetic_child_env()
    env.update({
        "BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
        "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR),
    })
    args = [sys.executable, "-m", "halo_harness", "-p", prompt, "--model", model,
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
    now makes ONE MORE call -- MAX_STEPS_PROMPT injected -- so the model
    gets a real chance to summarise instead of just being cut off;
    --max-turns=4 therefore means 5 upstream calls (4 tool-calling + 1
    wrap-up). finding 18: the wrap-up call's request keeps the FULL
    `tools` catalog on the wire (an Anthropic route 400s on "tool_use ...
    must define tools" if history has tool_use blocks but the request's
    own `tools` is empty/absent) and instead forbids a new call via
    `tool_choice: "none"`. This mock always scripts a tool_use reply
    regardless of what it's asked (it doesn't simulate a model that reads
    MAX_STEPS_PROMPT), so `stop_reason` is still "tool_use" here --
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
        wrap_up_body = mock.requests[-1].get("body") or {}
        ctx.check("the wrap-up call's own request body still carries the full tools catalog",
                  bool(wrap_up_body.get("tools")))
        ctx.check(f"the wrap-up call forbids a new call via tool_choice: none, got {wrap_up_body.get('tool_choice')!r}",
                  wrap_up_body.get("tool_choice") == "none")
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
def test_h5c_f20_max_steps_wrapup_strips_a_disobedient_tool_use(ctx: Ctx):
    """H5c finding 20: `tool_choice: "none"` on the MAX_STEPS wrap-up call is
    only a REQUEST -- a model (or a provider silently downgrading it) can
    still reply with a tool_use block anyway. That wrap-up call never
    dispatches anything (the turn ends right after it), so if the tool_use
    were logged as-is it would sit UNPAIRED in the transcript forever,
    corrupting every later request on `ant:`/Databricks Claude routes (400:
    "tool_use ... must define tools") and papered over on OpenAI-dialect
    routes with a synthetic "(no result)". `mock/page-forever` always
    replies with a tool_call no matter what `tool_choice` says (see
    `_scn_page_forever` above, and test_max_turns_caps_model_calls_per_turn
    which uses the same scenario over the CLI) -- driven here as a real
    in-process Session so the LOG ITSELF can be inspected after the turn,
    not just the CLI's stdout summary."""
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.invariants import find_unpaired_tool_use_ids
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds

    fh = build_fake_home()
    # H10b: this test never set BRIDGE_TEST_HOME, so its real in-process
    # Session fell through to the REAL `~/.halo/sessions` -- exactly
    # how `or:mock/page-forever` sessions leaked into rolo's real session
    # history (H10b report; see the sibling tests just below, which DO set it).
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    (Path(tempfile.gettempdir()) / "loop-breaker-target.txt").write_text("target file\n", encoding="utf-8")
    mock = MockUpstream().start()
    try:
        session_ctx = SessionContext(cwd=fh["proj"], model_label="mock/page-forever")
        model_ref = parse_model_ref("or:mock/page-forever")
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
        session = Session(
            cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(), creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="f20-wrapup-test-")), model_label="mock/page-forever",
            session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=2,
        )
        events_seen = list(session.turn("page through the file"))
        ctx.check(f"upstream called max_turns + 1 wrap-up call (3), got {len(mock.requests)}", len(mock.requests) == 3)
        wrap_up_body = mock.requests[-1].get("body") or {}
        ctx.check(f"the wrap-up call forbids a new call via tool_choice: none, got {wrap_up_body.get('tool_choice')!r}",
                  wrap_up_body.get("tool_choice") == "none")
        ctx.check("no unanswered tool_use ids remain anywhere in the log after the disobedient wrap-up reply "
                  "(the wrap-up call dispatches nothing, so a logged tool_use here would be unpaired forever)",
                  find_unpaired_tool_use_ids(session.log) == [])
        turn_done_events = [e for e in events_seen if e.kind == "turn_done"]
        ctx.check(f"exactly one turn_done event, got {len(turn_done_events)}", len(turn_done_events) == 1)
        ctx.check(f"turn ends via the max_turns path, got reason={turn_done_events[0].data.get('reason')!r}",
                  turn_done_events[0].data.get("reason") == "max_turns")
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)
        try:
            (Path(tempfile.gettempdir()) / "loop-breaker-target.txt").unlink()
        except OSError:
            pass


@test
def test_loop_breaker_unit_level_thresholds(ctx: Ctx):
    """Exercise Session._dispatch_tools directly against a fresh in-process
    Session so the exact remind/deny/end wording and thresholds (3/5/8) are
    asserted precisely, independent of subprocess/CLI plumbing."""
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session, _LOOP_BREAKER_DENY_AT, _LOOP_BREAKER_END_AT, _LOOP_BREAKER_REMIND_AT
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds

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
def test_h9_loop_breaker_exempts_bash_output_polling(ctx: Ctx):
    """H9 whole-tree review finding 8: BashOutput(shell_id=...) polling a
    long-running background job calls with the SAME arguments every time
    by design (the shell_id never changes) -- Moonshot's own documented
    "wait for a job to finish" pattern. The identical-args loop breaker
    used to deny at 5 polls and end the turn at 8, well before a
    legitimately slow job (a dev server starting up, a long test run)
    could ever finish. 12 identical BashOutput calls (well past the old
    5/8 thresholds) must never trip the breaker."""
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds

    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    session_ctx = SessionContext(cwd=fh["proj"], model_label="mock/model")
    model_ref = parse_model_ref("or:mock/model")
    session = Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(), creds=ProviderCreds(base_url="http://x", api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="loop-breaker-bashoutput-")), model_label="mock/model", session_context=session_ctx,
    )
    tool_use = {"type": "tool_use", "id": "call_x", "name": "BashOutput", "input": {"shell_id": "bash_same_job"}}
    outcomes = []
    for i in range(12):
        events_seen = list(session._dispatch_tools(1, [dict(tool_use, id=f"call_{i}")]))
        result_events = [e for e in events_seen if e.kind == "tool_result"]
        outcomes.append((result_events[0].data["summary"] if result_events else "") or "")
    ctx.check(f"none of the 12 identical polls ever shows breaker text, got {outcomes}",
              all("Loop breaker" not in o and "reminder" not in o for o in outcomes))

    # A DIFFERENT tool called with identical args right alongside it must
    # still be caught normally -- this exemption is BashOutput-specific,
    # not a blanket "any tool repeated a lot is fine".
    other = {"type": "tool_use", "id": "call_y", "name": "Read", "input": {"file_path": "/same/other.txt"}}
    other_outcomes = []
    for i in range(8):
        events_seen = list(session._dispatch_tools(1, [dict(other, id=f"other_{i}")]))
        result_events = [e for e in events_seen if e.kind == "tool_result"]
        other_outcomes.append((result_events[0].data["summary"] if result_events else "") or "")
    ctx.check(f"a DIFFERENT repeated tool (Read) still ends the turn at its 8th call, got {other_outcomes[-1]!r}",
              "ending the turn" in other_outcomes[-1].lower())


@test
def test_h9_loop_breaker_period2_ping_pong_detected(ctx: Ctx):
    """H9 OpenCode item 20: a strict A,B,A,B,... alternation between two
    DIFFERENT calls (never identical back to back) must ALSO trip the
    3/5/8 remind/deny/end breaker, via a dedicated pair counter layered on
    top of the plain per-key one -- and must reach deny/end FASTER (in
    total calls) than waiting for either A's or B's own count to get
    there alone (which would need the 5th/8th occurrence of ONE of them,
    i.e. absolute call 9/15 in a clean alternation, not call 7/10)."""
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds

    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    session_ctx = SessionContext(cwd=fh["proj"], model_label="mock/model")
    model_ref = parse_model_ref("or:mock/model")
    session = Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(), creds=ProviderCreds(base_url="http://x", api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="loop-breaker-period2-")), model_label="mock/model", session_context=session_ctx,
    )
    tool_a = {"type": "tool_use", "id": "call_a", "name": "Read", "input": {"file_path": "/h9-period2-a.txt"}}
    tool_b = {"type": "tool_use", "id": "call_b", "name": "Read", "input": {"file_path": "/h9-period2-b.txt"}}
    outcomes = []
    for i in range(10):
        tu = dict(tool_a if i % 2 == 0 else tool_b, id=f"call_{i}")
        events_seen = list(session._dispatch_tools(1, [tu]))
        result_events = [e for e in events_seen if e.kind == "tool_result"]
        outcomes.append((result_events[0].data["summary"] if result_events else "") or "")

    ctx.check("calls 1-4: no breaker text yet (per-key counts only reach 2 each)",
              all("Loop breaker" not in outcomes[i] for i in range(4)))
    ctx.check("call 5: a reminder has appeared (pair count reaches 3 before either key alone would)",
              "reminder" in outcomes[4])
    ctx.check("call 7: DENIED via the alternating/pair path (pair=5, ahead of either key's own count=4)",
              "denied" in outcomes[6].lower() and "alternat" in outcomes[6].lower())
    ctx.check("call 9: not yet ended (pair=7, still under the end threshold of 8)",
              "ending the turn" not in outcomes[8].lower())
    ctx.check("call 10: turn ENDS via the alternating/pair path (pair=8) -- 5 calls sooner than the "
              "15 a pure per-key count would need for a clean alternation",
              "ending the turn" in outcomes[9].lower() and "alternat" in outcomes[9].lower())


@test
def test_h9_kimi_tool_id_counter_continues_across_the_session_not_reset_per_stream(ctx: Ctx):
    """H9 critical review finding 1: `_build_request` must seed the
    Kimi-rename counter from the highest `functions.{name}:{idx}` already
    logged this session, so a fresh per-call stream never restarts at 0 and
    collides with an id minted several steps ago (the exact repro: 6 Reads
    of 6 different files, all with empty upstream ids, used to ALL become
    `functions.Read:0`)."""
    import dataclasses
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds

    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    session_ctx = SessionContext(cwd=fh["proj"], model_label="mock/model")
    model_ref = parse_model_ref("or:mock/model")
    session = Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(), creds=ProviderCreds(base_url="http://x", api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="kimi-tool-id-")), model_label="mock/model", session_context=session_ctx,
    )
    session.provider_profile = dataclasses.replace(session.provider_profile, tool_id_format="kimi_functions_idx")

    # No prior Kimi-shaped ids yet -> starts at 0, same as before this fix.
    req0 = session._build_request({"messages": []})
    ctx.check(f"first call of the session starts the counter at 0, got {req0.kimi_tool_id_start}",
              req0.kimi_tool_id_start == 0)

    # Simulate the review's own repro: 6 prior steps, each renaming an
    # empty upstream id to functions.Read:0..5 (what the OLD, per-stream-
    # reset counter actually produced -- every one of them "0" before this
    # fix, but logged here as the CORRECT post-fix shape to test that the
    # NEXT call continues from the true high-water mark of 5, not 0).
    for i in range(6):
        session.log.append_assistant(content=[{"type": "tool_use", "id": f"functions.Read:{i}", "name": "Read", "input": {}}],
                                      stop_reason="tool_use")
        session.log.append_tool_result(tool_use_id=f"functions.Read:{i}", content=f"file {i}")

    req1 = session._build_request({"messages": []})
    ctx.check(f"7th call continues from the highest logged idx (5) + 1, got {req1.kimi_tool_id_start}",
              req1.kimi_tool_id_start == 6)

    # A non-Kimi profile must never compute this at all (stays 0, the
    # proxy's own permanent default, regardless of what's in the log).
    session.provider_profile = dataclasses.replace(session.provider_profile, tool_id_format="preserve")
    req2 = session._build_request({"messages": []})
    ctx.check(f"non-kimi profile: always 0 regardless of log contents, got {req2.kimi_tool_id_start}",
              req2.kimi_tool_id_start == 0)


@test
def test_interrupt_leaves_no_unanswered_tool_use(ctx: Ctx):
    """finding 16 (test #4 from the review's required list): an interrupt
    AFTER `tool_use_ready` (the assistant node WITH the tool_use is
    already logged -- unlike closing at `message_start`, before any
    tool_use exists at all, which is vacuously true regardless of whether
    synthesis actually works) must leave a REAL synthetic error result
    behind, not just an absence of unpaired ids that was never at risk."""
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.invariants import INTERRUPTED_MESSAGE, find_unpaired_tool_use_ids
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds

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
        env = _hermetic_child_env()
        env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                    "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
        args = [sys.executable, "-m", "halo_harness", "-p", f"read {target} and reply with only the number of lines",
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
    from halo_harness.agent.loop import _mcp_blocks_for_log
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
    from halo_harness.agent.loop import _mcp_blocks_for_log
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
    from halo_harness.agent.loop import _mcp_blocks_for_log
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
    from halo_harness.agent.loop import _summary_text_for_blocks
    ctx.check("first real text block wins", _summary_text_for_blocks(
        [{"type": "image", "source": {}}, {"type": "text", "text": "hello"}]) == "hello")
    ctx.check("no text -> an honest placeholder naming the content type",
              _summary_text_for_blocks([{"type": "image", "source": {}}]) == "[image]")


# A real, tiny, valid 1x1 RGBA PNG (base64) -- same fixture shape a
# Playwright/Chrome screenshot or a vision Read/MCP image result carries.
_PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@test
def test_summary_text_for_blocks_image_caption_names_media_type_dims_and_size(ctx: Ctx):
    """H8 scope B: a real image block (the shape a Playwright/Chrome
    screenshot or a vision Read/MCP result actually carries) gets a
    concrete caption -- media type, sniffed dimensions, human byte size --
    shown as the tool card's body, instead of the old bare "[image]"."""
    from halo_harness.agent.loop import _summary_text_for_blocks
    block = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _PNG_1X1_B64}}
    caption = _summary_text_for_blocks([block])
    ctx.check(f"names the media type, got {caption!r}", "image/png" in caption)
    ctx.check(f"names the real sniffed dimensions (1x1), got {caption!r}", "1x1" in caption)
    ctx.check(f"names a human byte size, got {caption!r}",
              any(unit in caption for unit in (" B]", " KB]", " MB]")))


@test
def test_summary_text_for_blocks_image_caption_degrades_gracefully_with_partial_info(ctx: Ctx):
    from halo_harness.agent.loop import _summary_text_for_blocks
    # media_type but undecodable/missing data -- still names what it knows,
    # never raises.
    only_media_type = _summary_text_for_blocks([{"type": "image", "source": {"media_type": "image/png"}}])
    ctx.check(f"names just the media type, got {only_media_type!r}", only_media_type == "[image: image/png]")
    not_real_base64 = _summary_text_for_blocks([{"type": "image", "source": {"data": "not-valid-base64!!"}}])
    ctx.check(f"bad base64 never raises, falls back to bare placeholder, got {not_real_base64!r}",
              not_real_base64 == "[image]")


@test
def test_summary_text_for_blocks_multiple_images_get_one_caption_line_each(ctx: Ctx):
    from halo_harness.agent.loop import _summary_text_for_blocks
    blocks = [{"type": "image", "source": {"media_type": "image/png"}},
              {"type": "image", "source": {"media_type": "image/jpeg"}}]
    caption = _summary_text_for_blocks(blocks)
    lines = caption.splitlines()
    ctx.check(f"one line per image, got {caption!r}",
              len(lines) == 2 and "image/png" in lines[0] and "image/jpeg" in lines[1])


@test
def test_summary_text_for_blocks_mixed_image_and_other_non_text_falls_back_to_kind_list(ctx: Ctx):
    """A MIX of an image with some other non-text kind is rarer/unusual
    enough that the old, simple "[kind1, kind2]" placeholder is kept --
    only an ALL-image result gets the richer per-image caption treatment."""
    from halo_harness.agent.loop import _summary_text_for_blocks
    caption = _summary_text_for_blocks([{"type": "image", "source": {}}, {"type": "tool_reference"}])
    ctx.check(f"falls back to the plain kind-list form, got {caption!r}", caption == "[image, tool_reference]")


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
