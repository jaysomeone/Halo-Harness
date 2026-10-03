"""tests.test_w4a_subagent_dock -- W4a item 4: a BACKGROUND sub-agent's own
"ask" now reaches the parent's live dock too (narrowed to permission/
question/plan asks only, never the rest of its output stream -- see
agent/subagent.py's own `_bg_run` comment), via `Session._event_sink`
(set by `run()`, simulated here directly). The FOREGROUND case was already
pinned before this round (test_subagent_e2e.py's own
`test_h5c_f08_child_permission_ask_is_live_answerable_and_tagged_with_agent_id`);
this file only covers what's NEW.
"""
import json
import os
import queue
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, ScriptedTurns
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()
test, TESTS = new_registry()


def _text_step(text: str) -> list:
    return [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": text}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]


def _tool_call_step(name: str, arguments: dict, call_id: str = "call_1") -> list:
    return [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": call_id, "type": "function",
                 "function": {"name": name, "arguments": json.dumps(arguments)}}]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}]


def _new_session(*, mock, model, agents, cwd):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine
    from halo_harness.providers.stream import ProviderCreds

    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="w4a-dock-home-")))
    session_ctx = SessionContext(cwd=cwd, model_label=model, bare=True)
    session = Session(
        cwd=cwd, model_ref=parse_model_ref(model), model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="w4a-dock-state-")), model_label=model,
        session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=10,
        permission_engine=PermissionEngine(mode="auto", cwd=cwd), agents=agents, routes={},
    )
    session.interactive = True  # a background child's live-ask gate is `bool(parent.interactive)`
    return session


@test
def test_background_subagent_permission_ask_reaches_the_parent_event_sink(ctx: Ctx):
    from halo_harness.permissions import Decision

    mock = MockUpstream().start()
    try:
        SCENARIOS["w4a-dock-parent"] = ScriptedTurns([
            _tool_call_step("Task", {"description": "write", "prompt": "write a file",
                                      "subagent_type": "writer", "model": "or:mock/w4a-dock-child",
                                      "run_in_background": True}),
            _text_step("started it"),
        ])
        cwd = Path(tempfile.mkdtemp(prefix="w4a-dock-cwd-"))
        target = cwd / "bg_written.txt"
        SCENARIOS["w4a-dock-child"] = ScriptedTurns([
            _tool_call_step("Write", {"file_path": str(target), "content": "hi"}),
            _text_step("done writing"),
        ])
        from halo_harness.config.agents_md import AgentSpec
        spec = AgentSpec(name="writer", description="writer sub-agent", tools=None,
                          disallowed_tools=["Agent", "Task"], body="write files.", permission_mode="default")
        session = _new_session(mock=mock, model="or:mock/w4a-dock-parent", agents={"writer": spec}, cwd=cwd)

        # W4a: simulates what `Session.run()` does -- a plain queue.Queue,
        # the SAME shape `Controller.events` is.
        q: "queue.Queue" = queue.Queue()
        session._event_sink = q.put

        list(session.turn("have the writer sub-agent write a file in the background"))

        deadline = time.monotonic() + 10
        card = None
        drained = []
        while time.monotonic() < deadline:
            try:
                ev = q.get(timeout=0.1)
            except queue.Empty:
                continue
            drained.append(ev)
            if ev.kind == "permission_request":
                card = ev
                break
        ctx.check(f"a permission_request from the BACKGROUND child reached the event sink, got kinds="
                  f"{[e.kind for e in drained]}", card is not None)
        ctx.check(f"tagged with the child's own agent_id, got {card.agent_id!r}",
                  card is not None and card.agent_id is not None)
        ctx.check(f"names the gated tool, got {card.data if card else None}",
                  card is not None and card.data.get("name") == "Write")

        resolved = session.resolve_permission(card.data.get("id"), Decision("allow", "test allow, bg sub-agent ask"))
        ctx.check("Session.resolve_permission found the background child's own waiter", resolved is True)

        deadline2 = time.monotonic() + 10
        while not target.exists() and time.monotonic() < deadline2:
            time.sleep(0.1)
        ctx.check(f"the Write actually ran once allowed, got exists={target.exists()}", target.exists())
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
