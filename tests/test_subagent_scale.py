"""tests.test_subagent_scale -- Halo 2.0.2 round 3 (brief C item 2):
`agents.max_concurrent`/`agents.max_depth` config knobs (`effective_max_
concurrent`/`effective_max_depth`, agent/subagent.py), the Agent tool's
`count`/`batch` fan-out (combined-result shape, the concurrency queue
capping how many run at once, `subagent_queued` events fired up front for
every job) through a REAL `agent.loop.Session` against the mock upstream
-- same house pattern as test_subagent_e2e.py/test_orgs.py.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, ScriptedTurns, end_sse, start_sse, write_sse_chunk
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()

test, TESTS = new_registry()


def _text_step(text: str) -> list:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]


def _new_session(*, mock, model, agents, max_turns=10):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine
    from halo_harness.providers.stream import ProviderCreds

    cwd = Path(tempfile.mkdtemp(prefix="scale-cwd-"))
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="scale-home-")))
    session_ctx = SessionContext(cwd=cwd, model_label=model, bare=True)
    return Session(
        cwd=cwd, model_ref=parse_model_ref(model), model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="scale-state-")), model_label=model,
        session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=max_turns,
        permission_engine=PermissionEngine(mode="auto", cwd=cwd), agents=agents, routes={},
    )


def _gp_spec(**overrides):
    from halo_harness.config.agents_md import AgentSpec
    kwargs = dict(name="general-purpose", description="general purpose sub-agent",
                  tools=None, disallowed_tools=["Agent", "Task"], body="You are a helpful sub-agent.")
    kwargs.update(overrides)
    return AgentSpec(**kwargs)


def _dispatch_one(session, tool_input: dict, *, name: str = "Agent", tool_id: str = "call_1") -> list:
    tu = {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}
    return list(session._dispatch_tools(1, [tu]))


# ---- effective_max_concurrent / effective_max_depth: pure unit --------------

@test
def test_effective_max_concurrent_reads_config_and_falls_back_on_bad_values(ctx: Ctx):
    from halo_harness.agent.subagent import MAX_CONCURRENT_AGENTS, AgentRuntime, effective_max_concurrent
    from halo_harness.theme import set_config_value
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="scale-cfg-home-")))
    runtime = AgentRuntime(parent=object())
    set_config_value("agents.max_concurrent", 7)
    ctx.check("reads the config value", effective_max_concurrent(runtime) == 7)
    for bad in (0, -3, "not-a-number"):
        set_config_value("agents.max_concurrent", bad)
        ctx.check(f"bad value {bad!r} falls back to the default",
                  effective_max_concurrent(runtime) == MAX_CONCURRENT_AGENTS)
    org_runtime = AgentRuntime(parent=object(), max_concurrent=11)
    ctx.check("an org's own max_concurrent wins outright regardless of config",
              effective_max_concurrent(org_runtime) == 11)


@test
def test_effective_max_depth_default_clamp_and_org_override_unclamped(ctx: Ctx):
    from halo_harness.agent.subagent import MAX_DEPTH, AgentRuntime, effective_max_depth
    from halo_harness.theme import set_config_value
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="scale-cfg-home2-")))
    runtime = AgentRuntime(parent=object())
    ctx.check(f"default is MAX_DEPTH, got {effective_max_depth(runtime)}", effective_max_depth(runtime) == MAX_DEPTH)
    set_config_value("agents.max_depth", 3)
    ctx.check("reads a valid config value (3, the top of the range)", effective_max_depth(runtime) == 3)
    set_config_value("agents.max_depth", 2)
    ctx.check("reads a valid config value (2)", effective_max_depth(runtime) == 2)
    for bad in (0, -1, 4, 99, "deep"):
        set_config_value("agents.max_depth", bad)
        ctx.check(f"out-of-[1,3]/bad value {bad!r} falls back to MAX_DEPTH",
                  effective_max_depth(runtime) == MAX_DEPTH)
    # brief: "NOT clamped to 3" for an org's own tree-derived override --
    # the `company` built-in alone needs depth 4.
    set_config_value("agents.max_depth", 1)  # config says 1; the org below must still win with 4
    org_runtime = AgentRuntime(parent=object(), max_depth=4)
    ctx.check("an org's own tree-derived max_depth is never clamped to the config range",
              effective_max_depth(org_runtime) == 4)


@test
def test_config_max_depth_lifts_a_depth_2_chain_the_default_would_refuse(ctx: Ctx):
    """Depth-1 (the default) refusing a grandchild is already pinned by
    test_subagent_e2e.py's own `test_depth_1_refusal` -- this proves the
    NEW knob is what actually lifts it, not some unrelated change."""
    from halo_harness.theme import set_config_value
    mock = MockUpstream().start()
    try:
        SCENARIOS["scale-grandchild"] = ScriptedTurns([_text_step("grandchild done")])
        SCENARIOS["scale-child"] = ScriptedTurns([
            [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": 0, "id": "call_gc", "type": "function",
                    "function": {"name": "Agent", "arguments": json.dumps({
                        "description": "gc", "prompt": "go deeper", "subagent_type": "general-purpose",
                        "model": "or:mock/scale-grandchild"})},
                }]}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ],
            _text_step("child wrapped up"),
        ])
        session = _new_session(mock=mock, model="or:mock/scale-parent-depth",
                                agents={"general-purpose": _gp_spec()})
        set_config_value("agents.max_depth", 2)
        events = _dispatch_one(session, {"description": "c", "prompt": "go",
                                          "subagent_type": "general-purpose", "model": "or:mock/scale-child"})
        # `events` also carries the GRANDCHILD's own nested tool_result
        # (agent_id = the child's own id) -- only the TOP-level one
        # (agent_id is None) answers THIS call's own `call_1`.
        results = [e for e in events if e.kind == "tool_result" and e.agent_id is None]
        ctx.check(f"one top-level tool_result, got {len(results)}", len(results) == 1)
        ctx.check(f"not an error (depth 2 allowed by config), got {results[0].data}",
                  results[0].data.get("ok") is True)
        ctx.check("the grandchild's own text reached the top", "child wrapped up" in results[0].data.get("content", ""))
    finally:
        mock.stop()


# ---- count / batch: combined-result shape, in spawn order -----------------

@test
def test_count_spawns_n_identical_jobs_combined_in_spawn_order(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        SCENARIOS["scale-count-child"] = ScriptedTurns([_text_step("count-child-done")])
        session = _new_session(mock=mock, model="or:mock/scale-parent-count", agents={"general-purpose": _gp_spec()})
        events = _dispatch_one(session, {"description": "fan", "prompt": "go", "subagent_type": "general-purpose",
                                          "count": 4, "model": "or:mock/scale-count-child"})
        results = [e for e in events if e.kind == "tool_result" and e.agent_id is None]
        ctx.check(f"one combined tool_result, got {len(results)}", len(results) == 1)
        content = results[0].data.get("content", "")
        ctx.check(f"all 4 children's text present, got {content!r}", content.count("count-child-done") == 4)
        ctx.check("one labelled section per child, in spawn order",
                  content.index("child 1/4") < content.index("child 2/4") < content.index("child 3/4")
                  < content.index("child 4/4"))
        starts = [e for e in events if e.kind == "subagent_start"]
        ends = [e for e in events if e.kind == "subagent_end"]
        ctx.check(f"4 starts and 4 ends, distinct agent_ids, got {len(starts)}/{len(ends)}",
                  len(starts) == 4 and len(ends) == 4 and len({e.agent_id for e in starts}) == 4)
    finally:
        mock.stop()


@test
def test_batch_spawns_per_item_overrides_combined_in_spawn_order(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        for i in range(3):
            SCENARIOS[f"scale-batch-child-{i}"] = ScriptedTurns([_text_step(f"batch-result-{i}")])
        session = _new_session(mock=mock, model="or:mock/scale-parent-batch", agents={"general-purpose": _gp_spec()})
        batch = [{"prompt": f"do {i}", "model": f"or:mock/scale-batch-child-{i}"} for i in range(3)]
        events = _dispatch_one(session, {"description": "fan", "subagent_type": "general-purpose", "batch": batch})
        results = [e for e in events if e.kind == "tool_result" and e.agent_id is None]
        ctx.check(f"one combined tool_result, got {len(results)}", len(results) == 1)
        content = results[0].data.get("content", "")
        for i in range(3):
            ctx.check(f"batch-result-{i} present", f"batch-result-{i}" in content)
        ctx.check("spawn order preserved in the combined text",
                  content.index("batch-result-0") < content.index("batch-result-1") < content.index("batch-result-2"))
    finally:
        mock.stop()


# ---- the concurrency queue: cap 2, 5 spawns --------------------------------

_tracked_lock = threading.Lock()
_tracked = {"active": 0, "peak": 0, "start_order": []}


def _scn_tracked_slow(index: int):
    def _run(h, body):
        with _tracked_lock:
            _tracked["active"] += 1
            _tracked["peak"] = max(_tracked["peak"], _tracked["active"])
            _tracked["start_order"].append(index)
        try:
            start_sse(h)
            for _ in range(3):
                write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {"content": "x"}}]})
                time.sleep(0.15)
            write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            write_sse_chunk(h, None, "[DONE]")
            end_sse(h)
        finally:
            with _tracked_lock:
                _tracked["active"] -= 1
    return _run


@test
def test_concurrency_queue_cap_2_with_5_spawns(ctx: Ctx):
    """cap 2, 5 spawns: never more than 2 running at once, all 5 finish,
    and (since a ThreadPoolExecutor's own work queue is FIFO and all 5
    jobs are submitted well before any could possibly finish) the first
    2 to actually start are exactly jobs 0 and 1, in spawn order."""
    from halo_harness.theme import set_config_value
    _tracked["active"] = 0
    _tracked["peak"] = 0
    _tracked["start_order"] = []
    mock = MockUpstream().start()
    try:
        for i in range(5):
            SCENARIOS[f"scale-cap-child-{i}"] = _scn_tracked_slow(i)
        session = _new_session(mock=mock, model="or:mock/scale-parent-cap", agents={"general-purpose": _gp_spec()})
        set_config_value("agents.max_concurrent", 2)
        batch = [{"prompt": f"do {i}", "model": f"or:mock/scale-cap-child-{i}"} for i in range(5)]
        events = _dispatch_one(session, {"description": "fan", "subagent_type": "general-purpose", "batch": batch})
        results = [e for e in events if e.kind == "tool_result" and e.agent_id is None]
        ctx.check(f"one combined tool_result, got {len(results)}", len(results) == 1)
        content = results[0].data.get("content", "")
        ctx.check(f"all 5 children finished, got {content!r}",
                  all(f"child {i + 1}/5" in content for i in range(5)))
        ctx.check(f"peak concurrency never exceeded the cap (2), got {_tracked['peak']}", _tracked["peak"] <= 2)
        ctx.check(f"the first 2 to start are jobs 0 and 1 (FIFO spawn order), got {_tracked['start_order']}",
                  sorted(_tracked["start_order"][:2]) == [0, 1])
        ctx.check(f"every job eventually started exactly once, got {_tracked['start_order']}",
                  sorted(_tracked["start_order"]) == [0, 1, 2, 3, 4])
    finally:
        mock.stop()


@test
def test_subagent_queued_events_fire_for_every_job_up_front(ctx: Ctx):
    """Every job's own `subagent_queued` must fire BEFORE the pool even
    starts picking any of them up -- capped at 1 concurrent so job 2+'s
    own `subagent_start` cannot possibly have happened yet when all 3
    `subagent_queued` events are checked."""
    from halo_harness.theme import set_config_value
    mock = MockUpstream().start()
    try:
        SCENARIOS["scale-queued-child"] = ScriptedTurns([_text_step("done")])
        session = _new_session(mock=mock, model="or:mock/scale-parent-queued", agents={"general-purpose": _gp_spec()})
        set_config_value("agents.max_concurrent", 1)
        events = _dispatch_one(session, {"description": "fan", "prompt": "go", "subagent_type": "general-purpose",
                                          "count": 3, "model": "or:mock/scale-queued-child"})
        queued = [e for e in events if e.kind == "subagent_queued"]
        ctx.check(f"3 subagent_queued events, distinct agent_ids, got {len(queued)}",
                  len(queued) == 3 and len({e.agent_id for e in queued}) == 3)
        first_start_index = next(i for i, e in enumerate(events) if e.kind == "subagent_start")
        ctx.check(f"every queued event precedes the first start event, got kinds={[e.kind for e in events]}",
                  all(events.index(q) < first_start_index for q in queued))
    finally:
        mock.stop()


# ---- error shapes -----------------------------------------------------------

@test
def test_bad_count_and_bad_batch_shapes_are_clear_errors(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        session = _new_session(mock=mock, model="or:mock/scale-parent-errs", agents={"general-purpose": _gp_spec()})
        for bad_input, label in (
            ({"description": "x", "prompt": "p", "subagent_type": "general-purpose", "count": 0}, "count=0"),
            ({"description": "x", "prompt": "p", "subagent_type": "general-purpose", "count": -1}, "count=-1"),
            ({"description": "x", "prompt": "p", "subagent_type": "general-purpose", "count": 51}, "count=51"),
            ({"description": "x", "subagent_type": "general-purpose", "batch": []}, "batch=[]"),
            ({"description": "x", "subagent_type": "general-purpose", "batch": [{"role": "coder"}]},
             "batch item missing prompt"),
        ):
            events = _dispatch_one(session, bad_input, tool_id=f"call-{label}")
            results = [e for e in events if e.kind == "tool_result" and e.agent_id is None]
            ctx.check(f"{label}: exactly one tool_result, got {len(results)}", len(results) == 1)
            ctx.check(f"{label}: is an error, got {results[0].data}", results[0].data.get("ok") is False)
    finally:
        mock.stop()


@test
def test_count_or_batch_combined_with_background_is_refused(ctx: Ctx):
    mock = MockUpstream().start()
    try:
        session = _new_session(mock=mock, model="or:mock/scale-parent-bg", agents={"general-purpose": _gp_spec()})
        events = _dispatch_one(session, {"description": "x", "prompt": "p", "subagent_type": "general-purpose",
                                          "count": 2, "run_in_background": True})
        results = [e for e in events if e.kind == "tool_result" and e.agent_id is None]
        ctx.check(f"exactly one tool_result, got {len(results)}", len(results) == 1)
        ctx.check(f"refused as an error, got {results[0].data}",
                  results[0].data.get("ok") is False and "background" in results[0].data.get("content", "").lower())
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
