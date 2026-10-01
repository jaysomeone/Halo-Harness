"""tests.test_agent_tool -- the Agent/Task tool end-to-end (H6 scope B):
a real `agent.loop.Session` dispatching a real `Agent` tool_use against a
mocked upstream, via `Session._dispatch_tools` directly (same pattern
test_loop_tools.py's own loop-breaker test uses) -- child answers, parent
sees the wrapped <task_result>; two parallel agents; depth-1 refusal;
unknown subagent_type; subagent log files + .meta.json; background
completion notice; task_id resume.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

# H15 part 2 addendum 3.1: a believable default credential (never a real
# one) keeps every `or:mock/...` ref below resolving exactly as it did
# before parse_model_ref started refusing an auto-detected-disabled
# provider; each test here already scopes its OWN BRIDGE_TEST_HOME.
ensure_default_provider_credentials()

test, TESTS = new_registry()


def _make_session(fh, *, scenario: str, mock: MockUpstream, agent_depth: int = 0, max_turns: int = 50,
                   roles: "dict|None" = None, cli_roles: "dict|None" = None):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.config.agents_md import discover_agents
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds

    session_ctx = SessionContext(cwd=fh["proj"], model_label=f"mock/{scenario}")
    model_ref = parse_model_ref(f"or:mock/{scenario}")
    agents = discover_agents(fh["proj"], settings=None)
    return Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="agent-tool-test-")), model_label=f"mock/{scenario}",
        session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=max_turns,
        agents=agents, agent_depth=agent_depth, roles=roles, cli_roles=cli_roles,
    )


def _text_chunk(text: str, *, finish: str = "stop"):
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
    ]


def _agent_tool_call_chunks(tool_id: str, subagent_type: str, prompt: str, *, description: str = "task",
                             extra_input: "dict|None" = None, name: str = "Agent"):
    input_obj = {"description": description, "prompt": prompt, "subagent_type": subagent_type}
    if extra_input:
        input_obj.update(extra_input)
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": tool_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(input_obj)}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def _run_agent_dispatch(session, tool_id: str, subagent_type: str, prompt: str, *, extra_input=None, name="Agent"):
    tu = {"type": "tool_use", "id": tool_id, "name": name,
          "input": {"description": "task", "prompt": prompt, "subagent_type": subagent_type, **(extra_input or {})}}
    return list(session._dispatch_tools(1, [tu]))


@test
def test_agent_call_child_answers_and_parent_gets_wrapped_result(ctx: Ctx):
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    try:
        mock_scn = "agent-e2e-child"
        from tests.helpers.mock_openai import SCENARIOS
        SCENARIOS[mock_scn] = lambda h, b: __import__("tests.helpers.mock_openai", fromlist=["_finish"])._finish(
            h, _text_chunk("The child's final answer is 42."))
        session = _make_session(fh, scenario=mock_scn, mock=mock)
        events_seen = _run_agent_dispatch(session, "call_1", "general-purpose", "What is the answer?")
        results = [e for e in events_seen if e.kind == "tool_result"]
        ctx.check("exactly one tool_result for the Agent call", len(results) == 1)
        content = results[0].data.get("content", "")
        ctx.check("result is ok (not an error)", results[0].data.get("ok") is True)
        ctx.check("<task_result> wrapper present", content.startswith("<task_result task_id="))
        ctx.check("child's own answer text is inside the wrapper", "42" in content)

        subagent_starts = [e for e in events_seen if e.kind == "subagent_start"]
        subagent_ends = [e for e in events_seen if e.kind == "subagent_end"]
        ctx.check("a subagent_start event was forwarded", len(subagent_starts) == 1)
        ctx.check("a subagent_end event was forwarded", len(subagent_ends) == 1)
        ctx.check("subagent_start carries an agent_id", bool(subagent_starts[0].agent_id))
        ctx.check("child text_delta events are tagged with the SAME agent_id",
                  any(e.kind == "text_delta" and e.agent_id == subagent_starts[0].agent_id for e in events_seen))
    finally:
        mock.stop()


@test
def test_task_alias_behaves_identically_to_agent(ctx: Ctx):
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    try:
        mock_scn = "agent-e2e-task-alias"
        from tests.helpers.mock_openai import SCENARIOS, _finish
        SCENARIOS[mock_scn] = lambda h, b: _finish(h, _text_chunk("alias works"))
        session = _make_session(fh, scenario=mock_scn, mock=mock)
        events_seen = _run_agent_dispatch(session, "call_1", "general-purpose", "hi", name="Task")
        results = [e for e in events_seen if e.kind == "tool_result"]
        ctx.check("Task tool_use dispatches successfully", results and results[0].data.get("ok") is True)
        ctx.check("child's answer reached the wrapper", "alias works" in results[0].data.get("content", ""))
    finally:
        mock.stop()


@test
def test_unknown_subagent_type_is_a_clean_error(ctx: Ctx):
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    try:
        session = _make_session(fh, scenario="agent-unused", mock=mock)
        events_seen = _run_agent_dispatch(session, "call_1", "totally-not-a-real-agent-type", "do a thing")
        results = [e for e in events_seen if e.kind == "tool_result"]
        ctx.check("one tool_result", len(results) == 1)
        ctx.check("marked as an error", results[0].data.get("ok") is False)
        ctx.check("mentions the unknown type", "totally-not-a-real-agent-type" in results[0].data.get("summary", ""))
    finally:
        mock.stop()


@test
def test_depth_1_session_refuses_to_spawn_a_sub_agent(ctx: Ctx):
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    try:
        # A Session built with agent_depth=1 simulates being INSIDE a
        # sub-agent already (agent/subagent.py sets this for every real
        # child) -- its own Agent tool call must refuse outright, never
        # even reaching the model-call machinery for a grandchild.
        session = _make_session(fh, scenario="agent-unused", mock=mock, agent_depth=1)
        events_seen = _run_agent_dispatch(session, "call_1", "general-purpose", "go deeper")
        results = [e for e in events_seen if e.kind == "tool_result"]
        ctx.check("one tool_result", len(results) == 1)
        ctx.check("refused as an error", results[0].data.get("ok") is False)
        ctx.check("mentions the depth limit", "depth" in results[0].data.get("summary", "").lower())
    finally:
        mock.stop()


@test
def test_two_parallel_agent_calls_in_one_message_both_complete(ctx: Ctx):
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    try:
        scn_a, scn_b = "agent-parallel-a", "agent-parallel-b"
        from tests.helpers.mock_openai import SCENARIOS, _finish
        SCENARIOS[scn_a] = lambda h, b: _finish(h, _text_chunk("result from A"))
        SCENARIOS[scn_b] = lambda h, b: _finish(h, _text_chunk("result from B"))
        session = _make_session(fh, scenario="agent-parallel-unused", mock=mock)
        tu_a = {"type": "tool_use", "id": "call_a", "name": "Agent",
                "input": {"description": "task a", "prompt": "do a", "subagent_type": "general-purpose",
                          "model": f"or:mock/{scn_a}"}}
        tu_b = {"type": "tool_use", "id": "call_b", "name": "Agent",
                "input": {"description": "task b", "prompt": "do b", "subagent_type": "general-purpose",
                          "model": f"or:mock/{scn_b}"}}
        events_seen = list(session._dispatch_tools(1, [tu_a, tu_b]))
        results = {e.data["id"]: e for e in events_seen if e.kind == "tool_result"}
        ctx.check("both Agent calls produced a tool_result", set(results) == {"call_a", "call_b"})
        ctx.check("call_a got A's own answer", "result from A" in results["call_a"].data.get("content", ""))
        ctx.check("call_b got B's own answer", "result from B" in results["call_b"].data.get("content", ""))
        starts = [e for e in events_seen if e.kind == "subagent_start"]
        ctx.check("two distinct sub-agent ids were used", len({e.agent_id for e in starts}) == 2)
    finally:
        mock.stop()


@test
def test_subagent_log_files_and_meta_json_written(ctx: Ctx):
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    try:
        scn = "agent-log-files"
        from tests.helpers.mock_openai import SCENARIOS, _finish
        SCENARIOS[scn] = lambda h, b: _finish(h, _text_chunk("logged answer"))
        session = _make_session(fh, scenario=scn, mock=mock)
        _run_agent_dispatch(session, "call_1", "general-purpose", "log me")
        subagents_dir = session.log.dir / session.log.session_id / "subagents"
        ctx.check("subagents/ directory exists under the parent session dir", subagents_dir.is_dir())
        jsonl_files = list(subagents_dir.glob("agent-*.jsonl"))
        meta_files = list(subagents_dir.glob("agent-*.meta.json"))
        ctx.check("exactly one child .jsonl log", len(jsonl_files) == 1)
        ctx.check("exactly one child .meta.json", len(meta_files) == 1)
        meta = json.loads(meta_files[0].read_text(encoding="utf-8"))
        ctx.check("meta records the agent type", meta.get("type") == "general-purpose")
        ctx.check("meta records completed status", meta.get("status") == "completed")
        ctx.check("meta records the parent_tool_use_id", meta.get("parent_tool_use_id") == "call_1")
        child_nodes = [json.loads(line) for line in jsonl_files[0].read_text(encoding="utf-8").splitlines() if line]
        ctx.check("child log has its own system node", any(n.get("type") == "system" for n in child_nodes))
    finally:
        mock.stop()


@test
def test_background_agent_returns_immediately_and_notices_next_turn(ctx: Ctx):
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    try:
        scn = "agent-background"
        from tests.helpers.mock_openai import SCENARIOS, _finish
        def _slow_then_answer(h, b):
            time.sleep(0.3)
            _finish(h, _text_chunk("background result ready"))
        SCENARIOS[scn] = _slow_then_answer
        session = _make_session(fh, scenario="agent-background-unused", mock=mock)
        tu = {"type": "tool_use", "id": "call_bg", "name": "Agent",
              "input": {"description": "bg task", "prompt": "work in the background",
                        "subagent_type": "general-purpose", "run_in_background": True,
                        "model": f"or:mock/{scn}"}}
        t0 = time.monotonic()
        events_seen = list(session._dispatch_tools(1, [tu]))
        elapsed = time.monotonic() - t0
        results = [e for e in events_seen if e.kind == "tool_result"]
        ctx.check("returns promptly (doesn't block on the 0.3s child)", elapsed < 0.25)
        ctx.check("immediate result mentions running in the background", "background" in results[0].data.get("content", "").lower())

        # Give the background thread time to finish, then start a NEW turn
        # and confirm the notice is applied (dsh: "report completion as a
        # user-role notice in the next step").
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not session._pending_agent_notices:
            time.sleep(0.05)
        ctx.check("a background completion notice was queued", len(session._pending_agent_notices) == 1)
        turn_events = list(session._apply_pending_agent_notices(2))
        user_msgs = [e for e in turn_events if e.kind == "user_message"]
        ctx.check("the notice was applied as a user_message event", len(user_msgs) == 1)
        ctx.check("the notice text carries the child's result", "background result ready" in user_msgs[0].data.get("text", ""))
        ctx.check("notices are cleared after being applied once", session._pending_agent_notices == [])
    finally:
        mock.stop()


@test
def test_task_id_resumes_the_same_child_session(ctx: Ctx):
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    try:
        scn = "agent-resume"
        from tests.helpers.mock_openai import SCENARIOS, ScriptedTurns
        SCENARIOS[scn] = ScriptedTurns([_text_chunk("first answer"), _text_chunk("second answer, remembers context")])
        session = _make_session(fh, scenario=scn, mock=mock)
        first_events = _run_agent_dispatch(session, "call_1", "general-purpose", "start the task")
        first_result = [e for e in first_events if e.kind == "tool_result"][0]
        # Pull the task_id back out of the wrapper the same way the model would read it.
        content = first_result.data["content"]
        task_id = content.split('task_id="', 1)[1].split('"', 1)[0]
        ctx.check("a task_id was produced", bool(task_id))

        tu_resume = {"type": "tool_use", "id": "call_2", "name": "Agent",
                     "input": {"description": "continue", "prompt": "keep going", "task_id": task_id}}
        second_events = list(session._dispatch_tools(2, [tu_resume]))
        second_result = [e for e in second_events if e.kind == "tool_result"][0]
        ctx.check("resuming succeeds (not an error)", second_result.data.get("ok") is True)
        ctx.check("resumed with the SAME task_id", f'task_id="{task_id}"' in second_result.data["content"])

        subagents_dir = session.log.dir / session.log.session_id / "subagents"
        jsonl_files = list(subagents_dir.glob("agent-*.jsonl"))
        ctx.check("still only ONE child log file (resumed, not a new one)", len(jsonl_files) == 1)
        child_nodes = [json.loads(line) for line in jsonl_files[0].read_text(encoding="utf-8").splitlines() if line]
        user_texts = [b.get("text", "") for n in child_nodes if n.get("type") == "user"
                      for b in (n.get("content") or []) if isinstance(b, dict)]
        ctx.check("both prompts landed in the SAME child log",
                  any("start the task" in t for t in user_texts) and any("keep going" in t for t in user_texts))
    finally:
        mock.stop()


@test
def test_agent_role_override_resolves_model_from_role_table(ctx: Ctx):
    """V2c (H15): `Agent(role="researcher")` overrides general-purpose's own
    default role (orchestrator) for just this call -- the child must
    actually run against the model the ROLE TABLE names for "researcher",
    not the parent's own model, proven end to end by which mock scenario
    answers (MockUpstream dispatches on the wire `model` field)."""
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    try:
        parent_scn, role_scn = "agent-role-parent", "agent-role-researcher"
        from tests.helpers.mock_openai import SCENARIOS, _finish
        SCENARIOS[role_scn] = lambda h, b: _finish(h, _text_chunk("answer from the researcher-role model"))
        session = _make_session(fh, scenario=parent_scn, mock=mock,
                                 roles={"researcher": f"or:mock/{role_scn}"})
        events_seen = _run_agent_dispatch(session, "call_1", "general-purpose", "research this",
                                           extra_input={"role": "researcher"})
        results = [e for e in events_seen if e.kind == "tool_result"]
        ctx.check("Agent(role=) call succeeds", results and results[0].data.get("ok") is True)
        ctx.check("child ran against the ROLE TABLE's model, not the parent's own",
                  "answer from the researcher-role model" in results[0].data.get("content", ""))
    finally:
        mock.stop()


@test
def test_agent_cli_role_override_beats_role_table(ctx: Ctx):
    """V2c (H15) precedence: a CLI `--role` override (`cli_roles`, threaded
    the same way a top-level `--role name=model` flag would be) wins over
    the persisted role table for the SAME role name."""
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    try:
        parent_scn, table_scn, cli_scn = "agent-role-parent2", "agent-role-table-loses", "agent-role-cli-wins"
        from tests.helpers.mock_openai import SCENARIOS, _finish
        SCENARIOS[cli_scn] = lambda h, b: _finish(h, _text_chunk("answer from the CLI override"))
        session = _make_session(fh, scenario=parent_scn, mock=mock,
                                 roles={"researcher": f"or:mock/{table_scn}"},
                                 cli_roles={"researcher": f"or:mock/{cli_scn}"})
        events_seen = _run_agent_dispatch(session, "call_1", "Researcher", "research this")
        results = [e for e in events_seen if e.kind == "tool_result"]
        ctx.check("Researcher (built-in, role=researcher) call succeeds",
                  results and results[0].data.get("ok") is True)
        ctx.check("CLI --role override wins over the persisted role table",
                  "answer from the CLI override" in results[0].data.get("content", ""))
    finally:
        mock.stop()


@test
def test_custom_agent_file_role_frontmatter_resolves_from_role_table(ctx: Ctx):
    """V2c (H15): a CUSTOM `.claude/agents/*.md` file's own `role:`
    frontmatter (no `model:` of its own) works exactly like a built-in's
    fixed default role -- discovered fresh (not one of the 6 built-ins),
    invoked with NO `role=` call-time override, and still resolves its
    model from the role table for the role its OWN file names."""
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    try:
        agents_dir = fh["proj"] / ".claude" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "my-reviewer.md").write_text(
            "---\n"
            "name: my-reviewer\n"
            "description: A custom, project-defined reviewer agent\n"
            "role: reviewer\n"
            "---\n"
            "You are a custom reviewer agent.\n",
            encoding="utf-8",
        )
        parent_scn, role_scn = "agent-role-custom-parent", "agent-role-custom-reviewer"
        from tests.helpers.mock_openai import SCENARIOS, _finish
        SCENARIOS[role_scn] = lambda h, b: _finish(h, _text_chunk("answer from the custom agent's own role"))
        session = _make_session(fh, scenario=parent_scn, mock=mock, roles={"reviewer": f"or:mock/{role_scn}"})
        events_seen = _run_agent_dispatch(session, "call_1", "my-reviewer", "review this")
        results = [e for e in events_seen if e.kind == "tool_result"]
        ctx.check("the custom agent's own call succeeds", results and results[0].data.get("ok") is True)
        ctx.check("resolved from the role table via the FILE's own role: frontmatter",
                  "answer from the custom agent's own role" in results[0].data.get("content", ""))
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
