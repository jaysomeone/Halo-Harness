"""tests.test_subagent_e2e -- H6 scope B: the Agent/Task tool end to end
through a REAL agent.loop.Session (parent AND child), against the mock
upstream: child answers/parent continues, two parallel agents, depth-1
refusal, maxTurns inside a child, background completion notice, a
permission ask inside a child (denied, tagged with the agent), subagent
log files + meta.json, task_id resume, and SubagentStart/SubagentStop
hook payloads.
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, ScriptedTurns
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

# H15 part 2 addendum 3.1: parse_model_ref now refuses an or:/dbx:/ant: ref
# whose provider isn't auto-detected as enabled -- a believable default
# credential (never a real one; this file already scopes its own state dir
# below) keeps every `or:mock/...` ref resolving exactly as it did before
# that addendum.
ensure_default_provider_credentials()

test, TESTS = new_registry()

# Test hygiene: BRIDGE_TEST_HOME is a process-wide env var (tests/run_all.py
# imports every test_*.py module into ONE interpreter) that `_new_session`
# below must set (SessionContext's settings resolution reads home() even
# with bare=True) for the WHOLE lifetime of a test -- including every
# child Session a Task/Agent call builds mid-turn -- so it can't be
# restored right after `_new_session` returns. Captured once here and
# restored by `test_zzz_restore_env`, registered LAST (tests run in file/
# registration order -- see tests/helpers/runner.py's run_all), so a LATER
# test module in the same run_all.py process never inherits our temp value.
_ORIGINAL_BRIDGE_TEST_HOME = os.environ.get("BRIDGE_TEST_HOME")


def _text_step(text: str) -> list:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]


def _tool_call_step(name: str, arguments: dict, call_id: str = "call_1") -> list:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments)}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def _multi_tool_call_step(calls: list) -> list:
    """`calls` = [(name, arguments, call_id), ...] -- several tool_use
    blocks in ONE assistant message (for the "two parallel agents" test)."""
    tool_calls = [
        {"index": i, "id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for i, (name, args, cid) in enumerate(calls)
    ]
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": tool_calls}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def _new_session(*, mock, model, agents=None, interactive=False, permission_mode="auto", max_turns=10, cwd=None):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine
    from halo_harness.providers.stream import ProviderCreds

    cwd = cwd or Path(tempfile.mkdtemp(prefix="rc-agent-e2e-"))
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="rc-agent-e2e-home-")))
    session_ctx = SessionContext(cwd=cwd, model_label=model, bare=True)
    model_ref = parse_model_ref(model)
    session = Session(
        cwd=cwd, model_ref=model_ref, model_profile=ModelProfile(), creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="rc-agent-e2e-state-")), model_label=model,
        session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=max_turns,
        permission_engine=PermissionEngine(mode=permission_mode, cwd=cwd),
        agents=(agents if agents is not None else {}), routes={},
    )
    session.interactive = interactive
    return session


def _switch_model(session, model: str) -> None:
    """Mid-test model swap that ALSO updates route/provider_profile (a
    plain `session.model_ref = ...` reassignment does not -- `set_model`
    is the real entry point `/model` itself uses)."""
    from halo_harness.model import ModelProfile, parse_model_ref
    session.set_model(parse_model_ref(model), ModelProfile())


def _general_purpose_spec(**overrides):
    from halo_harness.config.agents_md import AgentSpec
    kwargs = dict(name="general-purpose", description="general purpose sub-agent",
                  tools=None, disallowed_tools=["Agent", "Task"], body="You are a helpful sub-agent.")
    kwargs.update(overrides)
    return AgentSpec(**kwargs)


def _drain(session, prompt) -> list:
    return list(session.turn(prompt))


# ---- child answers, parent continues -----------------------------------------

@test
def test_agent_tool_child_answers_parent_continues(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-parent-basic"] = ScriptedTurns([
            _tool_call_step("Task", {"description": "count", "prompt": "count to 3",
                                      "subagent_type": "general-purpose", "model": "or:mock/h6-child-basic"}),
            _text_step("The sub-agent reported: one two three"),
        ])
        SCENARIOS["h6-child-basic"] = ScriptedTurns([_text_step("one two three")])

        session = _new_session(mock=mock, model="or:mock/h6-parent-basic",
                                agents={"general-purpose": _general_purpose_spec()})
        events = _drain(session, "use a sub-agent to count to 3")

        starts = [e for e in events if e.kind == "subagent_start"]
        ends = [e for e in events if e.kind == "subagent_end"]
        ctx.check("one subagent_start", len(starts) == 1)
        ctx.check("one subagent_end", len(ends) == 1)
        ctx.check("start/end share an agent_id", starts[0].agent_id == ends[0].agent_id)
        ctx.check("agent_id is on the event, not just data", starts[0].agent_id is not None)
        ctx.check("parent_tool_use_id carried in data", starts[0].data.get("parent_tool_use_id"))

        final_text = "".join(e.data.get("text", "") for e in events if e.kind == "text_delta")
        ctx.check("parent's final answer references the child's report", "one two three" in final_text)

        tool_results = [e.data for e in events if e.kind == "tool_result"]
        agent_result = next(r for r in tool_results if "task_id" in (r.get("content") or ""))
        ctx.check("result is <task_result>-wrapped", "<task_result" in agent_result["content"])
    finally:
        mock.stop()


@test
def test_parent_abort_reaches_a_running_childs_in_flight_stream(ctx: Ctx):
    """H6 scope B must-do ("sub-agents reuse steer/abort/... plumbing"):
    `_run_child_to_completion` is a bare drain loop with no abort check of
    its own -- before `Session.__init__` grew a shared `abort` param, a
    child always got its OWN private Event, so an Esc/kill during a
    running sub-agent (`Controller.interrupt()`/`quit()`, which just call
    `session.abort.set()` on the PARENT) had ZERO effect until the child's
    entire turn finished organically, no matter how long that took.
    `or:mock/long-abort` streams ~60 chunks 0.1s apart (~6s total) -- with
    the fix, setting the PARENT's abort mid-stream must cut the CHILD's
    in-flight call short well before that, because the child now observes
    the SAME Event object through its own existing `self.abort.is_set()`
    checks (no new logic needed inside `_run_child_to_completion` itself)."""
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-parent-long-child"] = ScriptedTurns([
            _tool_call_step("Task", {"description": "slow", "prompt": "do a slow thing",
                                      "subagent_type": "general-purpose", "model": "or:mock/long-abort"}),
        ])
        session = _new_session(mock=mock, model="or:mock/h6-parent-long-child",
                                agents={"general-purpose": _general_purpose_spec()})

        events_seen: list = []
        errors: list = []
        done = threading.Event()

        def _drive():
            try:
                for ev in session.turn("use a sub-agent to do a slow thing"):
                    events_seen.append(ev)
            except Exception as e:  # pragma: no cover -- would fail the check below
                errors.append(e)
            finally:
                done.set()

        t = threading.Thread(target=_drive, daemon=True)
        t0 = time.monotonic()
        t.start()

        # Wait until the CHILD's own request has actually reached the mock
        # (request 1 = parent's, request 2 = the child's `long-abort` call),
        # then let a few 0.1s-spaced chunks genuinely stream before
        # interrupting -- a real mid-flight cut, not "aborted before it
        # ever started".
        deadline = time.monotonic() + 10
        while len(mock.requests) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        ctx.check(f"the child's own request reached the mock, got {len(mock.requests)} requests",
                  len(mock.requests) >= 2)
        time.sleep(0.3)

        session.abort.set()
        done.wait(timeout=10)
        dt = time.monotonic() - t0
        t.join(timeout=1)

        ctx.check(f"no exception propagated out of turn(), got {errors}", errors == [])
        ctx.check(f"the whole parent+child turn finished promptly (well under the "
                  f"~6s the un-aborted stream would take), got dt={dt:.2f}s", dt < 3.0)
        # Two turn_done events are expected and correct here: the CHILD's
        # own (nested, tagged, forwarded by `_run_child_to_completion`)
        # plus the PARENT's own top-level one -- the point of this check
        # is that the pair fired at ALL (a clean finish, not a hang).
        kinds = [e.kind for e in events_seen]
        ctx.check(f"turn_done fired (a clean finish, not a hang), got kinds={kinds}",
                  kinds.count("turn_done") >= 1)
        text_deltas = kinds.count("text_delta")
        ctx.check(f"the child's stream was genuinely cut short, NOT run to the full ~60 "
                  f"chunks a completed long-abort scenario sends, got {text_deltas} text_delta events",
                  text_deltas < 30)
    finally:
        mock.stop()


@test
def test_h5c_f07_five_agents_esc_after_four_started_all_end_interrupted(ctx: Ctx):
    """H5c finding 7: every child `turn()` used to call `self.abort.clear()`
    UNCONDITIONALLY, even though a sub-agent shares the PARENT's own Event
    object -- verified with exactly this repro: 5 Agent calls, so the pool
    of 4 (`MAX_CONCURRENT_AGENTS`) queues one. Esc fired once 4 children's
    own requests are genuinely streaming interrupted children 0-3 (their
    `_step`/streaming already observe the SHARED Event), but the 5th
    started LATER, its own `turn()` cleared the shared Event, and it (plus
    the parent's own next model call) then ran to completion as if Esc had
    never happened. Fixed via `Session._owns_abort` (only the OWNER clears
    at turn start) and an explicit abort check in the agent-batch pool's
    `_run_one`, right before a QUEUED call would otherwise start. All 5
    children must end up interrupted (4 genuinely cut mid-stream, the 5th
    never even started) and the PARENT's own turn must end
    `reason="interrupted"`, not silently succeed."""
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-parent-five-agents"] = ScriptedTurns([
            _multi_tool_call_step([
                ("Task", {"description": f"slow-{i}", "prompt": f"do slow thing {i}",
                           "subagent_type": "general-purpose", "model": "or:mock/long-abort"}, f"call_{i}")
                for i in range(5)
            ]),
        ])
        session = _new_session(mock=mock, model="or:mock/h6-parent-five-agents",
                                agents={"general-purpose": _general_purpose_spec()})

        events_seen: list = []
        errors: list = []
        done = threading.Event()

        def _drive():
            try:
                for ev in session.turn("run five slow sub-agents"):
                    events_seen.append(ev)
            except Exception as e:  # pragma: no cover -- would fail the check below
                errors.append(e)
            finally:
                done.set()

        t = threading.Thread(target=_drive, daemon=True)
        t0 = time.monotonic()
        t.start()

        # Wait until 4 children's own requests have reached the mock (1
        # parent request + 4 concurrent children -- the pool's own cap) and
        # let a few 0.1s-spaced chunks genuinely stream, so the 5th is
        # provably still queued (never started) when we interrupt.
        deadline = time.monotonic() + 10
        while len(mock.requests) < 5 and time.monotonic() < deadline:
            time.sleep(0.05)
        ctx.check(f"exactly 4 children's requests reached the mock (the pool cap), got {len(mock.requests)}",
                  len(mock.requests) == 5)
        time.sleep(0.3)

        session.abort.set()
        done.wait(timeout=10)
        dt = time.monotonic() - t0
        t.join(timeout=1)

        ctx.check(f"no exception propagated out of turn(), got {errors}", errors == [])
        ctx.check(f"the whole turn finished promptly (well under the ~6s an un-aborted child "
                  f"takes), got dt={dt:.2f}s -- this is the bug: the OLD code let child 5 (and the "
                  f"parent's own continuation) run to completion", dt < 3.0)
        # The 5th child must NEVER have started its own request at all --
        # proves the abort check inside the pool's `_run_one` caught it
        # before it ever built a child Session.
        ctx.check(f"the 5th child's request never reached the mock, still exactly 5 total, got {len(mock.requests)}",
                  len(mock.requests) == 5)

        tool_results = {e.data.get("id"): e.data for e in events_seen if e.kind == "tool_result"}
        ctx.check(f"all 5 Task calls got a result (none left unpaired), got {sorted(tool_results)}",
                  len(tool_results) == 5)
        # Every one of the 5 shows the interrupt somewhere: the 4 that
        # genuinely started stream a normal (non-error) result carrying the
        # child's own "[Request interrupted by user]" marker (same shape as
        # `test_parent_abort_reaches_a_running_childs_in_flight_stream`);
        # the 5th, caught by the pool's own abort check before ever
        # building a child Session, is a real `is_error` result instead.
        never_started = [cid for cid, data in tool_results.items()
                          if data.get("ok") is False]
        ctx.check(f"exactly one call never started at all (the queued 5th), got {never_started}",
                  len(never_started) == 1)
        for call_id, data in tool_results.items():
            summary = data.get("summary") or data.get("content") or ""
            ctx.check(f"{call_id}: shows the interrupt one way or another, got {data}",
                      "interrupted" in summary.lower() or "not started" in summary.lower())

        ctx.check(f"the PARENT's OWN turn ends interrupted, got kinds={[e.kind for e in events_seen][-6:]}",
                  any(e.kind == "turn_done" and e.agent_id is None and e.data.get("reason") == "interrupted"
                      for e in events_seen))
    finally:
        mock.stop()


# ---- two parallel agents in one turn -----------------------------------------

@test
def test_two_parallel_agents_in_one_turn(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-parent-parallel"] = ScriptedTurns([
            _multi_tool_call_step([
                ("Task", {"description": "a", "prompt": "a", "subagent_type": "general-purpose",
                           "model": "or:mock/h6-child-parallel-a"}, "call_a"),
                ("Task", {"description": "b", "prompt": "b", "subagent_type": "general-purpose",
                           "model": "or:mock/h6-child-parallel-b"}, "call_b"),
            ]),
            _text_step("Both sub-agents finished."),
        ])
        SCENARIOS["h6-child-parallel-a"] = ScriptedTurns([_text_step("result-A")])
        SCENARIOS["h6-child-parallel-b"] = ScriptedTurns([_text_step("result-B")])

        session = _new_session(mock=mock, model="or:mock/h6-parent-parallel",
                                agents={"general-purpose": _general_purpose_spec()})
        events = _drain(session, "run two sub-agents")

        starts = [e for e in events if e.kind == "subagent_start"]
        ctx.check("two subagent_start events", len(starts) == 2)
        agent_ids = {e.agent_id for e in starts}
        ctx.check("distinct agent_ids", len(agent_ids) == 2)

        results_text = " ".join((e.data.get("content") or "") for e in events if e.kind == "tool_result")
        ctx.check("both children's results present", "result-A" in results_text and "result-B" in results_text)
    finally:
        mock.stop()


# ---- depth-1 refusal -----------------------------------------------------------

@test
def test_depth_1_refusal(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-parent-depth"] = ScriptedTurns([
            _tool_call_step("Task", {"description": "nested", "prompt": "go deeper",
                                      "subagent_type": "general-purpose"}),
            _text_step("acknowledged"),
        ])
        session = _new_session(mock=mock, model="or:mock/h6-parent-depth",
                                agents={"general-purpose": _general_purpose_spec()})
        session.agent_runtime.depth = 1  # simulate: this session IS ALREADY a sub-agent
        events = _drain(session, "try to spawn a nested sub-agent")

        denials = [e.data for e in events if e.kind == "tool_result" and not e.data.get("ok")]
        ctx.check("the Task call was refused", any("depth limit" in (d.get("content") or "").lower() for d in denials))
        ctx.check("no subagent_start at all", not any(e.kind == "subagent_start" for e in events))
    finally:
        mock.stop()


# ---- unknown subagent_type -----------------------------------------------------

@test
def test_unknown_subagent_type_is_an_error(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-parent-unknown"] = ScriptedTurns([
            _tool_call_step("Task", {"description": "x", "prompt": "x", "subagent_type": "does-not-exist"}),
            _text_step("ok"),
        ])
        session = _new_session(mock=mock, model="or:mock/h6-parent-unknown",
                                agents={"general-purpose": _general_purpose_spec()})
        events = _drain(session, "use a bogus sub-agent type")
        denials = [e.data for e in events if e.kind == "tool_result" and not e.data.get("ok")]
        ctx.check("unknown subagent_type reported", any("unknown subagent_type" in (d.get("content") or "").lower()
                                                          for d in denials))
    finally:
        mock.stop()


# ---- maxTurns applies inside a child -------------------------------------------

@test
def test_child_max_turns_wraps_up_instead_of_hanging(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-parent-maxturns"] = ScriptedTurns([
            _tool_call_step("Task", {"description": "loop", "prompt": "keep reading forever",
                                      "subagent_type": "loopy", "model": "or:mock/h6-child-maxturns"}),
            _text_step("child wrapped up"),
        ])
        SCENARIOS["h6-child-maxturns"] = ScriptedTurns([
            _tool_call_step("Read", {"file_path": "/nonexistent/one"}, call_id="r1"),
            _tool_call_step("Read", {"file_path": "/nonexistent/two"}, call_id="r2"),
            _text_step("giving up, here is my summary"),
        ])
        spec = _general_purpose_spec(name="loopy", max_turns=2)
        session = _new_session(mock=mock, model="or:mock/h6-parent-maxturns", agents={"loopy": spec})
        events = _drain(session, "run the loopy agent")
        ends = [e for e in events if e.kind == "subagent_end"]
        ctx.check("the child still finished (didn't hang)", len(ends) == 1)
        results_text = " ".join((e.data.get("content") or "") for e in events if e.kind == "tool_result")
        ctx.check("child's wrap-up text made it back", "giving up" in results_text or "summary" in results_text)
    finally:
        mock.stop()


# ---- background completion notice ----------------------------------------------

@test
def test_background_agent_completion_notice_on_next_turn(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-parent-bg"] = ScriptedTurns([
            _tool_call_step("Task", {"description": "bg", "prompt": "work in the background",
                                      "subagent_type": "general-purpose", "model": "or:mock/h6-child-bg",
                                      "run_in_background": True}),
            _text_step("started it in the background"),
        ])
        SCENARIOS["h6-child-bg"] = ScriptedTurns([_text_step("background result ready")])

        session = _new_session(mock=mock, model="or:mock/h6-parent-bg",
                                agents={"general-purpose": _general_purpose_spec()})
        _drain(session, "run something in the background")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not session._pending_agent_notices:
            time.sleep(0.05)
        ctx.check("a background notice queued", bool(session._pending_agent_notices))

        SCENARIOS["h6-parent-bg-turn2"] = ScriptedTurns([_text_step("noted the background result")])
        _switch_model(session, "or:mock/h6-parent-bg-turn2")
        second_events = _drain(session, "anything new?")
        notice_messages = [e.data.get("text", "") for e in second_events if e.kind == "user_message"]
        ctx.check("the background notice was injected as a user message",
                  any("Background sub-agent" in t for t in notice_messages))
    finally:
        mock.stop()


# ---- a permission ask inside a child is surfaced LIVE and tagged ---------------

@test
def test_h5c_f08_child_permission_ask_is_live_answerable_and_tagged_with_agent_id(ctx: Ctx):
    """H5c finding 8 (closes the H6/D10 v1 gap this test used to pin as
    correct): a FOREGROUND sub-agent's own "ask" decision, when the PARENT
    is interactive, is now a LIVE, answerable `permission_request` --
    agent-tagged -- not an immediate, non-interactive denial. Drives the
    turn on a background thread (same pattern as
    `test_parent_abort_reaches_a_running_childs_in_flight_stream`), waits
    for the child's own `permission_request` to arrive tagged with its
    `agent_id`, answers it via `session.resolve_permission` (the SAME
    top-level entry point the UI's `Controller.answer_permission` uses --
    proving the child's waiter really is parked on the PARENT's own
    `_permission_waiters`), and confirms the Write actually ran."""
    from halo_harness.permissions import Decision

    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-parent-askchild"] = ScriptedTurns([
            _tool_call_step("Task", {"description": "write", "prompt": "write a file",
                                      "subagent_type": "writer", "model": "or:mock/h6-child-askchild"}),
            _text_step("the sub-agent wrote the file"),
        ])
        cwd = Path(tempfile.mkdtemp(prefix="rc-agent-e2e-askchild-"))
        target = cwd / "new_file.txt"
        SCENARIOS["h6-child-askchild"] = ScriptedTurns([
            _tool_call_step("Write", {"file_path": str(target), "content": "hi"}, "call_write_1"),
            _text_step("done writing"),
        ])
        # permission_mode="default" on the SPEC forces a real "ask" decision
        # for the child even though the PARENT session itself is "auto".
        spec = _general_purpose_spec(name="writer", permission_mode="default")
        session = _new_session(mock=mock, model="or:mock/h6-parent-askchild", agents={"writer": spec},
                                permission_mode="auto", cwd=cwd,
                                interactive=True)  # H5c finding 8: a live card now reaches even a sub-agent's ask

        events_seen: list = []
        done = threading.Event()

        def _drive():
            try:
                for ev in session.turn("have the writer sub-agent write a file"):
                    events_seen.append(ev)
            finally:
                done.set()

        t = threading.Thread(target=_drive, daemon=True)
        t.start()

        deadline = time.monotonic() + 10
        while not any(e.kind == "permission_request" for e in events_seen) and time.monotonic() < deadline:
            time.sleep(0.05)
        cards = [e for e in events_seen if e.kind == "permission_request"]
        ctx.check(f"a permission_request arrived, got kinds={[e.kind for e in events_seen]}", len(cards) == 1)
        card = cards[0]
        ctx.check(f"it is tagged with the CHILD's own agent_id (not None -- the parent never asks here), "
                  f"got {card.agent_id!r}", card.agent_id is not None)
        ctx.check(f"it names the gated tool, got {card.data}", card.data.get("name") == "Write")

        # It must be genuinely LIVE -- not already resolved/denied before we
        # get a chance to answer it.
        ctx.check("no tool_result for this call yet (still waiting on a real answer)",
                  not any(e.kind == "tool_result" and e.data.get("id") == card.data.get("id") for e in events_seen))

        resolved = session.resolve_permission(card.data.get("id"), Decision("allow", "test allow, live sub-agent ask"))
        ctx.check("the top-level Session.resolve_permission -- same entry point the UI uses -- found the "
                  "child's waiter and resolved it", resolved is True)

        done.wait(timeout=10)
        ctx.check("the turn finished", done.is_set())

        write_results = [e for e in events_seen if e.kind == "tool_result" and e.data.get("id") == "call_write_1"]
        ctx.check(f"the Write call got a result, got {[e.data for e in write_results]}", len(write_results) == 1)
        ctx.check(f"it succeeded (the live 'allow' answer actually ran the tool), got {write_results[0].data}",
                  write_results[0].data.get("ok") is True)
        ctx.check(f"the successful result is STILL agent-tagged, got agent_id={write_results[0].agent_id!r}",
                  write_results[0].agent_id is not None)
        ctx.check(f"the file was genuinely written to disk, got exists={target.exists()}", target.exists())
    finally:
        mock.stop()


@test
def test_h5c_f08_child_permission_ask_denied_live_still_tagged_and_merged(ctx: Ctx):
    """H5c finding 8, the deny half: a live sub-agent ask that the user
    genuinely denies (not just auto-denied) must still behave like every
    other denial -- an agent-tagged `is_error` tool_result, and merged into
    the PARENT's own `permission_denials` (H6/D10: print mode's top-level
    JSON result only ever reads the PARENT's list)."""
    from halo_harness.permissions import Decision

    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-parent-askchild-deny"] = ScriptedTurns([
            _tool_call_step("Task", {"description": "write", "prompt": "write a file",
                                      "subagent_type": "writer", "model": "or:mock/h6-child-askchild-deny"}),
            _text_step("the sub-agent was blocked from writing"),
        ])
        cwd = Path(tempfile.mkdtemp(prefix="rc-agent-e2e-askchild-deny-"))
        target = cwd / "denied_file.txt"
        SCENARIOS["h6-child-askchild-deny"] = ScriptedTurns([
            _tool_call_step("Write", {"file_path": str(target), "content": "hi"}, "call_write_deny_1"),
            # A second scripted step for the child's OWN post-denial reply
            # -- without one, `ScriptedTurns` replays the SAME Write call
            # again (a second, never-answered ask that hangs the child's
            # thread forever, since `_await_permission_decision` has no
            # timeout of its own).
            _text_step("understood, I will not write that file"),
        ])
        spec = _general_purpose_spec(name="writer", permission_mode="default")
        session = _new_session(mock=mock, model="or:mock/h6-parent-askchild-deny", agents={"writer": spec},
                                permission_mode="auto", cwd=cwd, interactive=True)

        events_seen: list = []
        done = threading.Event()

        def _drive():
            try:
                for ev in session.turn("have the writer sub-agent write a file"):
                    events_seen.append(ev)
            finally:
                done.set()

        t = threading.Thread(target=_drive, daemon=True)
        t.start()

        deadline = time.monotonic() + 10
        while not any(e.kind == "permission_request" for e in events_seen) and time.monotonic() < deadline:
            time.sleep(0.05)
        cards = [e for e in events_seen if e.kind == "permission_request"]
        ctx.check(f"a permission_request arrived, tagged with the child's agent_id, got {cards}",
                  len(cards) == 1 and cards[0].agent_id is not None)

        resolved = session.resolve_permission(cards[0].data.get("id"), Decision("deny", "no, don't write that"))
        ctx.check("resolve_permission found the child's waiter", resolved is True)
        done.wait(timeout=10)
        ctx.check("the turn finished", done.is_set())

        write_results = [e for e in events_seen if e.kind == "tool_result" and e.data.get("id") == "call_write_deny_1"]
        ctx.check(f"the Write call got a result, got {[e.data for e in write_results]}", len(write_results) == 1)
        ctx.check(f"it was genuinely denied, got {write_results[0].data}", write_results[0].data.get("ok") is False)
        ctx.check("the denial mentions why", "don't write that" in (write_results[0].data.get("summary") or ""))
        ctx.check(f"the file was never written, got exists={target.exists()}", not target.exists())
        # H6 known v1 gap (D10) / B must-do: the child's own denial is
        # merged into the PARENT's `permission_denials` -- print mode's
        # top-level JSON result reads ONLY the parent's list, so a sub-
        # agent's denied tool call used to be invisible there entirely.
        ctx.check(f"the sub-agent's denial reached the PARENT's own permission_denials, got {session.permission_denials}",
                  any(d.get("tool_name") == "Write" for d in session.permission_denials))
    finally:
        mock.stop()


@test
def test_h9_two_parallel_children_with_colliding_tool_ids_get_distinct_live_asks(ctx: Ctx):
    """H9 critical review finding 2: TWO PARALLEL children whose own
    `tool_use` ids happen to be IDENTICAL (the concrete real-world trigger
    is Kimi's per-call id counter restarting fresh in each child's own,
    separately-empty log -- reproduced directly here with two children both
    scripted to call Bash with the literal id "call_ask_0") must get TWO
    DISTINCT, independently-answerable `permission_request`s, never one
    shared waiter slot in `_permission_waiters` (the SAME dict object both
    children share with the parent -- agent/subagent.py's
    `_build_child_session`). Before the fix: the second child's insert
    silently overwrote the first's live slot, so resolving one card could
    wake the WRONG child's thread (running ITS command instead) while the
    other waited forever with no way to ever be answered."""
    from halo_harness.permissions import Decision

    mock = MockUpstream().start()
    try:
        SCENARIOS["h9-parent-two-askchildren"] = ScriptedTurns([
            _multi_tool_call_step([
                ("Task", {"description": "run a", "prompt": "run a", "subagent_type": "writer",
                           "model": "or:mock/h9-child-askchild-a"}, "call_agent_a"),
                ("Task", {"description": "run b", "prompt": "run b", "subagent_type": "writer",
                           "model": "or:mock/h9-child-askchild-b"}, "call_agent_b"),
            ]),
            # A required second (final) step -- without it, ScriptedTurns
            # replays the SAME two-Task-call step once both children's
            # results come back, spawning a fresh pair of children forever
            # instead of ending the turn (same gotcha the "five agents"
            # test's own comment calls out for a child's post-denial reply).
            _text_step("both sub-agents finished"),
        ])
        cwd = Path(tempfile.mkdtemp(prefix="rc-agent-e2e-collide-"))
        # Both children scripted with the SAME literal tool_use id -- this
        # is the collision itself; which upstream mechanism would produce
        # matching ids in real use (Kimi's per-call counter) doesn't matter
        # to this fix, which is purely about the SHARED waiter dict. `printf`
        # (unlike `echo`) is NOT on permissions.py's built-in read-only
        # whitelist, so `default` mode genuinely asks for it instead of
        # auto-allowing it outright.
        SCENARIOS["h9-child-askchild-a"] = ScriptedTurns([
            _tool_call_step("Bash", {"command": "printf FROM_A"}, "call_ask_0"),
            _text_step("a done"),
        ])
        SCENARIOS["h9-child-askchild-b"] = ScriptedTurns([
            _tool_call_step("Bash", {"command": "printf FROM_B"}, "call_ask_0"),
            _text_step("b done"),
        ])
        spec = _general_purpose_spec(name="writer", permission_mode="default")
        session = _new_session(mock=mock, model="or:mock/h9-parent-two-askchildren", agents={"writer": spec},
                                permission_mode="auto", cwd=cwd, interactive=True)

        events_seen: list = []
        done = threading.Event()

        def _drive():
            try:
                for ev in session.turn("run two sub-agents in parallel"):
                    events_seen.append(ev)
            finally:
                done.set()

        t = threading.Thread(target=_drive, daemon=True)
        t.start()
        try:
            deadline = time.monotonic() + 10
            while len([e for e in events_seen if e.kind == "permission_request"]) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            cards = [e for e in events_seen if e.kind == "permission_request"]
            ctx.check(f"both children's asks arrived live, got {len(cards)}", len(cards) == 2)
            ctx.check(f"both cards name the same colliding tool_use id, got {[c.data.get('id') for c in cards]}",
                      all(c.data.get("name") == "Bash" for c in cards))
            ids = [c.data.get("id") for c in cards]
            ctx.check(f"THE FIX: the two waiter/request ids are DISTINCT despite the identical tool_use id, got {ids}",
                      len(set(ids)) == 2)
            agent_ids = {c.agent_id for c in cards}
            ctx.check(f"tagged with two distinct agent_ids, got {agent_ids}", len(agent_ids) == 2)
            card_by_agent = {c.agent_id: c for c in cards}

            # Resolve ONE card and confirm ONLY that child's own Bash call
            # produced a result so far -- the other must still be waiting
            # (proves no cross-wiring: answering A never touches B's slot).
            first_agent_id = next(iter(agent_ids))
            resolved = session.resolve_permission(card_by_agent[first_agent_id].data.get("id"),
                                                    Decision("allow", "allow the first one"))
            ctx.check("resolve_permission found the first child's own waiter", resolved is True)

            deadline = time.monotonic() + 10
            while not any(e.kind == "tool_result" and e.agent_id == first_agent_id for e in events_seen) \
                    and time.monotonic() < deadline:
                time.sleep(0.05)
            first_results = [e for e in events_seen if e.kind == "tool_result" and e.agent_id == first_agent_id]
            ctx.check(f"the FIRST child's own Bash call got a result, got {first_results}", len(first_results) == 1)
            ctx.check(f"it genuinely ran (not denied), got {first_results[0].data}", first_results[0].data.get("ok") is True)
            other_agent_id = next(a for a in agent_ids if a != first_agent_id)
            still_waiting = [e for e in events_seen if e.kind == "tool_result" and e.agent_id == other_agent_id]
            ctx.check(f"the OTHER child's Bash call has NO result yet -- still genuinely waiting, "
                      f"not silently resolved by the first answer, got {still_waiting}", still_waiting == [])

            # Now resolve the second card too, so the turn can finish cleanly.
            resolved2 = session.resolve_permission(card_by_agent[other_agent_id].data.get("id"),
                                                     Decision("allow", "allow the second one"))
            ctx.check("resolve_permission found the SECOND child's own waiter (not already consumed)", resolved2 is True)
            done.wait(timeout=10)
            ctx.check("the whole turn finished", done.is_set())
            second_results = [e for e in events_seen if e.kind == "tool_result" and e.agent_id == other_agent_id]
            ctx.check(f"the second child's own Bash call also got its OWN result, got {second_results}",
                      len(second_results) == 1 and second_results[0].data.get("ok") is True)
        finally:
            done.wait(timeout=5)
    finally:
        mock.stop()


# ---- subagent log files + meta.json --------------------------------------------

@test
def test_subagent_log_files_and_meta_json(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-parent-logfiles"] = ScriptedTurns([
            _tool_call_step("Task", {"description": "note", "prompt": "say hi",
                                      "subagent_type": "general-purpose", "model": "or:mock/h6-child-logfiles"}),
            _text_step("done"),
        ])
        SCENARIOS["h6-child-logfiles"] = ScriptedTurns([_text_step("hi there")])
        session = _new_session(mock=mock, model="or:mock/h6-parent-logfiles",
                                agents={"general-purpose": _general_purpose_spec()})
        events = _drain(session, "greet me via a sub-agent")
        agent_id = next(e.agent_id for e in events if e.kind == "subagent_start")

        subdir = session.log.dir / session.log.session_id / "subagents"
        log_path = subdir / f"agent-{agent_id}.jsonl"
        meta_path = subdir / f"agent-{agent_id}.meta.json"
        ctx.check("child log file exists", log_path.is_file())
        ctx.check("child meta.json exists", meta_path.is_file())

        nodes = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        types = [n["type"] for n in nodes]
        ctx.check("child log has its own system node", "system" in types)
        ctx.check("child log has its own user node", "user" in types)
        ctx.check("child log has its own assistant node", "assistant" in types)

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        ctx.check("meta status completed", meta.get("status") == "completed")
        ctx.check("meta type is the subagent_type", meta.get("type") == "general-purpose")
        ctx.check("meta agent_id matches", meta.get("agent_id") == agent_id)
    finally:
        mock.stop()


# ---- 2.0.2 review finding 17 (major): tasks-panel log-read cache ----------------

@test
def test_agent_log_nodes_caches_by_size_and_mtime(ctx: Ctx):
    """2.0.2 review finding 17 (major) pin: the tasks panel's own 1Hz
    poll used to re-read and re-parse EVERY sub-agent's whole jsonl log
    on every tick just to count tools, even one that hadn't grown since
    the last tick. A cache hit (unchanged size/mtime) must skip the
    read+parse entirely -- proven here by silently rewriting the file's
    CONTENT (same size, mtime explicitly reset) and confirming the
    cached (stale) result is what comes back; a genuine append (new
    size/mtime) IS picked up on the next call."""
    from halo_harness.agent.subagent import _agent_log_nodes
    log_path = Path(tempfile.mkdtemp(prefix="agent-log-cache-")) / "agent-x.jsonl"
    line_a = json.dumps({"type": "assistant", "content": [{"type": "text", "text": "a"}]})
    log_path.write_text(line_a + "\n", encoding="utf-8")
    st = log_path.stat()
    first = _agent_log_nodes(log_path)
    ctx.check(f"one node read, got {first}", len(first) == 1 and first[0]["content"][0]["text"] == "a")

    # Same size, mtime explicitly reset back -- a cache HIT must return
    # the OLD (cached) content, never re-read this at all.
    line_z = json.dumps({"type": "assistant", "content": [{"type": "text", "text": "Z"}]})
    ctx.check("the replacement line is byte-identical in length (test premise)", len(line_z) == len(line_a))
    log_path.write_text(line_z + "\n", encoding="utf-8")
    os.utime(log_path, (st.st_atime, st.st_mtime))
    second = _agent_log_nodes(log_path)
    ctx.check(f"cache hit: still the OLD content, got {second}", second == first)

    # A genuine append (new size/mtime) -- a cache MISS, re-read for real.
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "assistant", "content": [{"type": "text", "text": "b"}]}) + "\n")
    third = _agent_log_nodes(log_path)
    ctx.check(f"cache miss on growth: picks up both lines fresh, got {third}",
              len(third) == 2 and third[1]["content"][0]["text"] == "b")


# ---- task_id resume --------------------------------------------------------------

@test
def test_task_id_resume_continues_the_same_child_session(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-child-resume"] = ScriptedTurns([_text_step("first answer: 7 widgets")])
        session = _new_session(mock=mock, model="or:mock/h6-parent-resume-unused",
                                agents={"general-purpose": _general_purpose_spec()})

        from halo_harness.agent.subagent import run_agent_call
        _events1, result1 = run_agent_call(
            runtime=session.agent_runtime, tool_id="toolu_1",
            tool_input={"description": "count", "prompt": "how many widgets are there? (unique marker Q1)",
                        "subagent_type": "general-purpose", "model": "or:mock/h6-child-resume"},
            tool_name="Task",
        )
        m = re.search(r'task_id="([^"]+)"', result1.content)
        ctx.check("a task_id was returned", m is not None)
        task_id = m.group(1)

        SCENARIOS["h6-child-resume-2"] = ScriptedTurns([_text_step("second answer, remembering the first")])
        seen_original_context = {"ok": False}

        def _router(h, body):
            messages = body.get("messages") or []
            if "Q1" in json.dumps(messages):
                seen_original_context["ok"] = True
            SCENARIOS["h6-child-resume-2"](h, body)

        SCENARIOS["h6-child-resume-router"] = _router

        _events2, result2 = run_agent_call(
            runtime=session.agent_runtime, tool_id="toolu_2",
            tool_input={"task_id": task_id, "prompt": "and how many of them are red?",
                        "model": "or:mock/h6-child-resume-router"},
            tool_name="Task",
        )
        ctx.check("resume did not error", not result2.is_error)
        ctx.check("same task_id echoed back", f'task_id="{task_id}"' in result2.content)
        ctx.check("the RESUMED request still carried the original conversation", seen_original_context["ok"])
    finally:
        mock.stop()


@test
def test_h9_taskstop_targets_a_background_agents_own_abort_event(ctx: Ctx):
    """H9 whole-tree review finding 32: the real Claude Code TaskStop
    accepts a background sub-agent's own task_id, not just a Bash
    shell_id (`tools/task_stop.py` used to answer "Unknown shell_id/
    task_id" for every agent task, with no way to stop one at all).
    `agent/subagent.py`'s `run_agent_call` now stashes a BACKGROUND
    child's own (private, per finding 10) abort Event on `agent_runtime.
    tasks[task_id]["abort_event"]`; TaskStop must find and set it."""
    from halo_harness.agent.subagent import run_agent_call
    from halo_harness.tools.base import ToolContext
    from halo_harness.tools.task_stop import TaskStopTool

    mock = MockUpstream().start()
    try:
        SCENARIOS["h9-child-taskstop-bg"] = ScriptedTurns([_text_step("background result")])
        session = _new_session(mock=mock, model="or:mock/h9-parent-taskstop-unused",
                                agents={"general-purpose": _general_purpose_spec()})
        _events, result = run_agent_call(
            runtime=session.agent_runtime, tool_id="toolu_bg1",
            tool_input={"description": "bg", "prompt": "work in the background",
                        "subagent_type": "general-purpose", "model": "or:mock/h9-child-taskstop-bg",
                        "run_in_background": True},
            tool_name="Task",
        )
        m = re.search(r'task_id="?([a-f0-9]+)"?', result.content)
        ctx.check(f"a task_id was returned, got {result.content!r}", m is not None)
        task_id = m.group(1)
        task = session.agent_runtime.tasks.get(task_id)
        ctx.check("the task record carries a real abort_event for this BACKGROUND child",
                  task is not None and task.get("abort_event") is not None)

        stop_ctx = ToolContext(cwd=session.cwd, agent_runtime=session.agent_runtime)
        stop_result = TaskStopTool().run({"task_id": task_id}, stop_ctx)
        ctx.check(f"TaskStop succeeds on an agent task_id, got {stop_result.content!r}", not stop_result.is_error)
        ctx.check("the child's own abort Event is now set", task["abort_event"].is_set())

        # A SECOND stop on the same (already-stopped) task_id is still a
        # clean, non-error response (idempotent), never a crash/KeyError.
        stop_result2 = TaskStopTool().run({"task_id": task_id}, stop_ctx)
        ctx.check(f"stopping an already-stopped task is a clean no-op, got {stop_result2.content!r}",
                  not stop_result2.is_error and "already" in stop_result2.content.lower())
    finally:
        mock.stop()


@test
def test_h9_taskstop_on_a_foreground_agent_task_is_a_clear_error_not_unknown(ctx: Ctx):
    """A FOREGROUND sub-agent's task record deliberately carries NO abort_
    event of its own (finding 10: it shares the parent's -- stopping it is
    the user's own Esc/Ctrl+C, not TaskStop) -- must say so plainly, never
    the generic "Unknown shell_id/task_id" (which would wrongly suggest
    the task_id itself was wrong)."""
    from halo_harness.agent.subagent import run_agent_call
    from halo_harness.tools.base import ToolContext
    from halo_harness.tools.task_stop import TaskStopTool

    mock = MockUpstream().start()
    try:
        SCENARIOS["h9-child-taskstop-fg"] = ScriptedTurns([_text_step("foreground result")])
        session = _new_session(mock=mock, model="or:mock/h9-parent-taskstop-fg-unused",
                                agents={"general-purpose": _general_purpose_spec()})
        _events, result = run_agent_call(
            runtime=session.agent_runtime, tool_id="toolu_fg1",
            tool_input={"description": "fg", "prompt": "work in the foreground",
                        "subagent_type": "general-purpose", "model": "or:mock/h9-child-taskstop-fg"},
            tool_name="Task",
        )
        m = re.search(r'task_id="?([a-f0-9]+)"?', result.content)
        ctx.check(f"a task_id was returned, got {result.content!r}", m is not None)
        task_id = m.group(1)

        stop_ctx = ToolContext(cwd=session.cwd, agent_runtime=session.agent_runtime)
        stop_result = TaskStopTool().run({"task_id": task_id}, stop_ctx)
        ctx.check(f"a clear FOREGROUND-specific error, not 'Unknown', got {stop_result.content!r}",
                  stop_result.is_error and "FOREGROUND" in stop_result.content and "Unknown" not in stop_result.content)
    finally:
        mock.stop()


# ---- SubagentStart/SubagentStop hook payloads (unit-level) ---------------------

@test
def test_hook_runner_payload_carries_agent_id_and_type(ctx: Ctx):
    from halo_harness.hooks import HookRunner
    runner = HookRunner({}, cwd=Path(tempfile.mkdtemp()), session_id="agent-abc123",
                         transcript_path="/tmp/x.jsonl", agent_id="abc123", agent_type="general-purpose")
    payload = runner.payload("SubagentStart")
    ctx.check("agent_id in payload", payload.get("agent_id") == "abc123")
    ctx.check("agent_type in payload", payload.get("agent_type") == "general-purpose")
    ctx.check("hook_event_name set", payload.get("hook_event_name") == "SubagentStart")


@test
def test_hook_runner_without_agent_id_omits_it(ctx: Ctx):
    from halo_harness.hooks import HookRunner
    runner = HookRunner({}, cwd=Path(tempfile.mkdtemp()), session_id="s1", transcript_path="/tmp/x.jsonl")
    payload = runner.payload("SessionStart")
    ctx.check("no agent_id key for a top-level session", "agent_id" not in payload)


@test
def test_zzz_restore_env(ctx: Ctx):
    """Not a real test -- see the module-level comment by
    `_ORIGINAL_BRIDGE_TEST_HOME`. Must stay the LAST `@test` in this file."""
    if _ORIGINAL_BRIDGE_TEST_HOME is None:
        os.environ.pop("BRIDGE_TEST_HOME", None)
    else:
        os.environ["BRIDGE_TEST_HOME"] = _ORIGINAL_BRIDGE_TEST_HOME
    ctx.check("restored", True)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
