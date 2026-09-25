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
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.permissions import PermissionEngine
    from rolo_claude.providers.stream import ProviderCreds

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
    from rolo_claude.model import ModelProfile, parse_model_ref
    session.set_model(parse_model_ref(model), ModelProfile())


def _general_purpose_spec(**overrides):
    from rolo_claude.config.agents_md import AgentSpec
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


# ---- a permission ask inside a child is denied and tagged ----------------------

@test
def test_child_permission_ask_is_denied_and_tagged_with_agent_id(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-parent-askchild"] = ScriptedTurns([
            _tool_call_step("Task", {"description": "write", "prompt": "write a file",
                                      "subagent_type": "writer", "model": "or:mock/h6-child-askchild"}),
            _text_step("the sub-agent was blocked from writing"),
        ])
        SCENARIOS["h6-child-askchild"] = ScriptedTurns([
            _tool_call_step("Write", {"file_path": "new_file.txt", "content": "hi"}),
        ])
        # permission_mode="default" on the SPEC forces a real "ask" decision
        # for the child even though the PARENT session itself is "auto".
        spec = _general_purpose_spec(name="writer", permission_mode="default")
        session = _new_session(mock=mock, model="or:mock/h6-parent-askchild", agents={"writer": spec},
                                permission_mode="auto",
                                interactive=True)  # even an INTERACTIVE parent -- v1: children never block on a human
        events = _drain(session, "have the writer sub-agent write a file")

        tagged_denials = [e for e in events if e.kind == "tool_result" and e.agent_id and not e.data.get("ok")]
        ctx.check("a denial event exists, tagged with the child's agent_id", len(tagged_denials) >= 1)
        denial_text = tagged_denials[0].data.get("content") or tagged_denials[0].data.get("summary") or ""
        ctx.check("denial mentions permission", "permission" in denial_text.lower())
        # The ask is still surfaced (informational, "surfaced ... with the
        # agent tag" per the brief) -- it just resolves to an immediate
        # deny rather than genuinely blocking, since v1 children always run
        # non-interactively regardless of the PARENT's own interactive flag.
        tagged_asks = [e for e in events if e.kind == "permission_request" and e.agent_id]
        ctx.check("the ask itself was surfaced tagged with the agent_id too", len(tagged_asks) >= 1)
        ctx.check("the turn completed (never actually blocked waiting on a human)",
                  any(e.kind == "turn_done" for e in events))
        # H6 known v1 gap (D10) / B must-do: the child's own denial is
        # merged into the PARENT's `permission_denials` -- print mode's
        # top-level JSON result reads ONLY the parent's list, so a sub-
        # agent's denied tool call used to be invisible there entirely.
        ctx.check(f"the sub-agent's denial reached the PARENT's own permission_denials, got {session.permission_denials}",
                  len(session.permission_denials) >= 1)
        ctx.check("the merged denial names the actually-denied tool",
                  any(d.get("tool_name") == "Write" for d in session.permission_denials))
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


# ---- task_id resume --------------------------------------------------------------

@test
def test_task_id_resume_continues_the_same_child_session(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["h6-child-resume"] = ScriptedTurns([_text_step("first answer: 7 widgets")])
        session = _new_session(mock=mock, model="or:mock/h6-parent-resume-unused",
                                agents={"general-purpose": _general_purpose_spec()})

        from rolo_claude.agent.subagent import run_agent_call
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


# ---- SubagentStart/SubagentStop hook payloads (unit-level) ---------------------

@test
def test_hook_runner_payload_carries_agent_id_and_type(ctx: Ctx):
    from rolo_claude.hooks import HookRunner
    runner = HookRunner({}, cwd=Path(tempfile.mkdtemp()), session_id="agent-abc123",
                         transcript_path="/tmp/x.jsonl", agent_id="abc123", agent_type="general-purpose")
    payload = runner.payload("SubagentStart")
    ctx.check("agent_id in payload", payload.get("agent_id") == "abc123")
    ctx.check("agent_type in payload", payload.get("agent_type") == "general-purpose")
    ctx.check("hook_event_name set", payload.get("hook_event_name") == "SubagentStart")


@test
def test_hook_runner_without_agent_id_omits_it(ctx: Ctx):
    from rolo_claude.hooks import HookRunner
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
