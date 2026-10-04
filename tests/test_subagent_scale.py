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
def test_fanout_job_still_queued_after_abort_never_starts(ctx: Ctx):
    """2.0.2 review finding 11 (major) pin: after Esc, every job still
    sitting in the fan-out pool's own queue used to still build a child
    session (a worktree included, when isolated), fire SubagentStart/
    TaskCreated + SubagentStop/TaskCompleted, and write logs -- undoing
    H5c finding 7's own abort-batch contract. cap=1 so only ONE job can
    ever be "running" at a time; `parent.abort` is set the INSTANT the
    first job starts (well before it finishes), so every OTHER job is
    still queued when its own turn comes up."""
    from halo_harness.theme import set_config_value
    _tracked["active"] = 0
    _tracked["peak"] = 0
    _tracked["start_order"] = []
    mock = MockUpstream().start()
    session = None
    try:
        started = threading.Event()

        def _scn(index: int):
            def _run(h, body):
                if index == 0:
                    started.set()
                with _tracked_lock:
                    _tracked["active"] += 1
                    _tracked["start_order"].append(index)
                try:
                    start_sse(h)
                    time.sleep(0.3)
                    write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {"content": "x"}}]})
                    write_sse_chunk(h, None, {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                    write_sse_chunk(h, None, "[DONE]")
                    end_sse(h)
                finally:
                    with _tracked_lock:
                        _tracked["active"] -= 1
            return _run
        for i in range(4):
            SCENARIOS[f"scale-abort-child-{i}"] = _scn(i)
        session = _new_session(mock=mock, model="or:mock/scale-parent-abort", agents={"general-purpose": _gp_spec()})
        set_config_value("agents.max_concurrent", 1)
        batch = [{"prompt": f"do {i}", "model": f"or:mock/scale-abort-child-{i}"} for i in range(4)]

        done = []
        t = threading.Thread(target=lambda: done.append(_dispatch_one(
            session, {"description": "fan", "subagent_type": "general-purpose", "batch": batch})))
        t.start()
        ctx.check("the first job actually started", started.wait(timeout=5.0))
        session.abort.set()
        t.join(timeout=10.0)
        ctx.check("the dispatch finished (no hang)", not t.is_alive() and bool(done))
        events = done[0] if done else []
        results = [e for e in events if e.kind == "tool_result" and e.agent_id is None]
        ctx.check(f"one combined tool_result, got {len(results)}", len(results) == 1)
        content = results[0].data.get("content", "") if results else ""
        ctx.check(f"a queued job reports 'not started: interrupted' instead of running, got {content!r}",
                  "not started" in content.lower() and "interrupted" in content.lower())
        ctx.check(f"not all 4 children ever actually ran, got {_tracked['start_order']}",
                  len(_tracked["start_order"]) < 4)
    finally:
        if session is not None:
            session.abort.clear()
        mock.stop()


@test
def test_concurrency_cap_is_session_wide_across_two_fanout_calls_in_one_turn(ctx: Ctx):
    """2.0.2 review finding 10 (major) pin: cap 2, TWO separate `batch`
    calls in the SAME turn (two Agent tool_use blocks -- the outer
    `_dispatch_tools` pool runs both concurrently, each with its OWN
    inner fan-out pool for its own 3 children). Before the fix, each
    fan-out call's own `ThreadPoolExecutor` was independently sized at
    the cap, so the two together could run up to 2x the configured
    limit -- the review's own repro: two `count=6` calls, cap 4, peaked
    at 8 concurrent children."""
    from halo_harness.theme import set_config_value
    _tracked["active"] = 0
    _tracked["peak"] = 0
    _tracked["start_order"] = []
    mock = MockUpstream().start()
    try:
        for i in range(6):
            SCENARIOS[f"scale-sesswide-child-{i}"] = _scn_tracked_slow(i)
        # `_new_session` points BRIDGE_TEST_HOME at a fresh home, so the
        # config is written AFTER it; the gate re-reads the cap at every
        # spawn (`SessionConcurrencyGate.set_limit`), which is also what a
        # config change during a real session relies on.
        session = _new_session(mock=mock, model="or:mock/scale-parent-sesswide", agents={"general-purpose": _gp_spec()})
        set_config_value("agents.max_concurrent", 2)
        batch_a = [{"prompt": f"do {i}", "model": f"or:mock/scale-sesswide-child-{i}"} for i in range(3)]
        batch_b = [{"prompt": f"do {i}", "model": f"or:mock/scale-sesswide-child-{i}"} for i in range(3, 6)]
        tus = [
            {"type": "tool_use", "id": "call_a", "name": "Agent",
             "input": {"description": "fan-a", "subagent_type": "general-purpose", "batch": batch_a}},
            {"type": "tool_use", "id": "call_b", "name": "Agent",
             "input": {"description": "fan-b", "subagent_type": "general-purpose", "batch": batch_b}},
        ]
        done = []
        t = threading.Thread(target=lambda: done.append(list(session._dispatch_tools(1, tus))))
        t.start()
        t.join(timeout=30)
        ctx.check("the dispatch finished within 30s (no deadlock)", not t.is_alive() and done)
        ctx.check(f"peak concurrency across BOTH fan-out calls never exceeded the session-wide cap (2), "
                  f"got {_tracked['peak']}", _tracked["peak"] <= 2)
        ctx.check(f"all 6 children across both calls eventually ran, got {sorted(_tracked['start_order'])}",
                  sorted(_tracked["start_order"]) == [0, 1, 2, 3, 4, 5])
    finally:
        mock.stop()


@test
def test_concurrency_cap_of_1_never_deadlocks_a_nested_delegation_chain(ctx: Ctx):
    """2.0.2 review finding 10: the session-wide gate must be REENTRANT
    per thread -- a child delegating to a grandchild runs on the SAME
    thread that is already, right now, inside the gate for its own
    parent's call (`_run_child_to_completion` blocks the whole chain). A
    bare `threading.Semaphore(1)` would have that thread wait forever for
    a slot it already holds further up its own call stack. cap=1 is the
    smallest possible value, maximizing the chance of catching a
    regression back to a plain (non-reentrant) semaphore."""
    from halo_harness.theme import set_config_value
    mock = MockUpstream().start()
    try:
        SCENARIOS["scale-gate-grandchild"] = ScriptedTurns([_text_step("grandchild done")])
        SCENARIOS["scale-gate-child"] = ScriptedTurns([
            [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": 0, "id": "call_gc", "type": "function",
                    "function": {"name": "Agent", "arguments": json.dumps({
                        "description": "gc", "prompt": "go deeper", "subagent_type": "general-purpose",
                        "model": "or:mock/scale-gate-grandchild"})},
                }]}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ],
            _text_step("child wrapped up"),
        ])
        # `effective_max_concurrent` is read ONCE, at Session construction
        # (to size the gate), unlike `effective_max_depth` (read fresh on
        # every check) -- `_new_session` itself re-randomizes BRIDGE_TEST_
        # HOME every call, so config must be written into the SAME fixed
        # dir the Session construction below will actually read from,
        # with no `_new_session`-internal re-randomization in between.
        os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="scale-gate-home-")))
        set_config_value("agents.max_depth", 2)
        set_config_value("agents.max_concurrent", 1)
        from halo_harness.agent.assemble import SessionContext
        from halo_harness.agent.loop import Session
        from halo_harness.model import ModelProfile, parse_model_ref
        from halo_harness.permissions import PermissionEngine
        from halo_harness.providers.stream import ProviderCreds
        cwd = Path(tempfile.mkdtemp(prefix="scale-gate-cwd-"))
        # "general-purpose" (`_gp_spec()`) disallows Agent/Task outright --
        # fine for the GRANDCHILD (a true leaf; MAX_DEPTH stops it anyway)
        # but it would also silently block the CHILD's own nested call
        # (never reaching the pool/gate at all) if reused for that hop
        # too, so the child needs its own, delegation-capable spec.
        delegator_spec = _gp_spec(name="delegator", disallowed_tools=[])
        session = Session(
            cwd=cwd, model_ref=parse_model_ref("or:mock/scale-gate-parent"), model_profile=ModelProfile(),
            creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="scale-gate-state-")), model_label="or:mock/scale-gate-parent",
            session_context=SessionContext(cwd=cwd, model_label="or:mock/scale-gate-parent", bare=True),
            openrouter_base_url=mock.base_url, max_turns=10,
            permission_engine=PermissionEngine(mode="auto", cwd=cwd),
            agents={"general-purpose": _gp_spec(), "delegator": delegator_spec}, routes={},
        )
        done = []
        t = threading.Thread(target=lambda: done.append(_dispatch_one(
            session, {"description": "c", "prompt": "go", "subagent_type": "delegator",
                       "model": "or:mock/scale-gate-child"})))
        t.start()
        t.join(timeout=15)
        ctx.check("the nested chain finished within 15s instead of deadlocking", not t.is_alive() and done)
        if done:
            events_seen = done[0]
            results = [e for e in events_seen if e.kind == "tool_result" and e.agent_id is None]
            ctx.check(f"not an error, got {results[0].data if results else None}",
                      results and results[0].data.get("ok") is True)
            ctx.check("the child's own wrap-up text reached the top",
                      results and "child wrapped up" in results[0].data.get("content", ""))
            nested_results = [e for e in events_seen if e.kind == "tool_result" and e.agent_id is not None]
            ctx.check(f"the grandchild ACTUALLY ran (a nested tool_result exists), got "
                      f"{[e.data for e in nested_results]}",
                      nested_results and any("grandchild done" in e.data.get("content", "") for e in nested_results))
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


@test
def test_fanout_and_resume_both_call_the_org_budget_tracker(ctx: Ctx):
    """2.0.2 review finding 5 (major) pin -- the review's own "Tests to
    add" list: "Org budget trips ... for fan-out jobs and on resume".
    Neither path called `record_spend`/`refusal_before_spawn` AT ALL
    before this fix -- an org position's own budget could never trip no
    matter how many `count`/`batch` jobs or resumes it ran through.
    Spies on the TRACKER's own methods (never real dollar amounts --
    `or:mock/...` models report no real cost either way) so this proves
    the WIRING, independent of `OrgBudgetTracker`'s own math (pinned
    separately in tests/test_init_wizard_round7.py)."""
    from halo_harness.agent.subagent import run_agent_call
    from halo_harness.orgs import OrgBudgetTracker
    calls = []
    orig_record, orig_refusal = OrgBudgetTracker.record_spend, OrgBudgetTracker.refusal_before_spawn

    def _spy_record(self, title, spent_usd, parent_total_usd):
        calls.append(("record_spend", title))
        return orig_record(self, title, spent_usd, parent_total_usd)

    def _spy_refusal(self, title, parent_total_usd):
        calls.append(("refusal_before_spawn", title))
        return orig_refusal(self, title, parent_total_usd)
    OrgBudgetTracker.record_spend = _spy_record
    OrgBudgetTracker.refusal_before_spawn = _spy_refusal
    mock = MockUpstream().start()
    try:
        SCENARIOS["scale-budget-child"] = ScriptedTurns([_text_step("done")] * 3)
        session = _new_session(mock=mock, model="or:mock/scale-parent-budget", agents={"general-purpose": _gp_spec()})
        session.agent_runtime.org_budget = OrgBudgetTracker(org_name="t", org_budget_usd=100.0)
        events = _dispatch_one(session, {"description": "fan", "prompt": "go", "subagent_type": "general-purpose",
                                          "count": 2, "model": "or:mock/scale-budget-child"})
        results = [e for e in events if e.kind == "tool_result" and e.agent_id is None]
        ctx.check(f"fan-out completed ok, got {[e.data for e in results]}",
                  results and results[0].data.get("ok") is True)
        # 1 top-level check (run_agent_call's own, before EVER dispatching
        # to the fan-out pool at all) + 1 PER fan-out job (this fix) = 3.
        ctx.check(f"refusal_before_spawn checked at the top AND once per fan-out job, got {calls}",
                  len([c for c in calls if c == ("refusal_before_spawn", "general-purpose")]) == 3)
        ctx.check(f"record_spend called once PER fan-out job, got {calls}",
                  len([c for c in calls if c == ("record_spend", "general-purpose")]) == 2)

        task_id = next(iter(session.agent_runtime.tasks))
        calls.clear()
        _e2, r2 = run_agent_call(runtime=session.agent_runtime, tool_id="call_resume", tool_name="Agent",
                                  tool_input={"task_id": task_id, "prompt": "continue",
                                              "model": "or:mock/scale-budget-child"})
        ctx.check(f"resume ok, got {r2.content!r}", r2.is_error is False)
        ctx.check(f"the resume ALSO checks the budget before spawning, got {calls}",
                  ("refusal_before_spawn", "general-purpose") in calls)
        ctx.check(f"the resume ALSO records its own spend, got {calls}",
                  ("record_spend", "general-purpose") in calls)
    finally:
        OrgBudgetTracker.record_spend = orig_record
        OrgBudgetTracker.refusal_before_spawn = orig_refusal
        mock.stop()


# ---- error shapes -----------------------------------------------------------

@test
def test_agent_call_with_an_unresolvable_model_is_a_clean_error_never_a_crash(ctx: Ctx):
    """2.0.2 review finding 4 (major) pin: `run_agent_call`'s own
    docstring says "Never raises" -- an unresolvable model (a bad org
    position `model:`, or a `/role` pointed at a bogus ref) raised
    `InvalidModelError` straight out of `_build_child_session` instead.
    Called DIRECTLY here (never through `session._dispatch_tools`,
    which already has its OWN defense-in-depth try/except around a
    batched Agent call) -- the same way the TUI's own `_run_org_worker`
    (`tui/slash.py`) and `tools/skill.py`'s `context: fork`/`agent`
    path both call straight into this function, with no outer net."""
    from halo_harness.agent.subagent import run_agent_call
    mock = MockUpstream().start()
    try:
        bad_spec = _gp_spec(model="not-a-real-model-ref")
        session = _new_session(mock=mock, model="or:mock/scale-parent-badmodel",
                                agents={"general-purpose": bad_spec})
        _events, result = run_agent_call(
            runtime=session.agent_runtime, tool_id="call_1", tool_name="Agent",
            tool_input={"description": "x", "prompt": "go", "subagent_type": "general-purpose"})
        ctx.check(f"a clean ToolResult, never a raised exception, got {result!r}", result is not None)
        ctx.check(f"it's an error, got {result.content!r}", result.is_error is True)
        ctx.check(f"names the problem, got {result.content!r}", "not-a-real-model-ref" in result.content)
    finally:
        mock.stop()


@test
def test_resume_task_with_an_unresolvable_model_override_is_a_clean_error(ctx: Ctx):
    """2.0.2 review finding 4: `_resume_task`'s own `_build_child_
    session` call needed the SAME guard -- `Agent(task_id=..., model=
    <bad>)` on a resume used to crash the same way a fresh spawn did.
    Called DIRECTLY (see the sibling test's own comment on why)."""
    from halo_harness.agent.subagent import run_agent_call
    mock = MockUpstream().start()
    try:
        SCENARIOS["scale-resume-badmodel-child"] = ScriptedTurns([_text_step("first answer")])
        session = _new_session(mock=mock, model="or:mock/scale-parent-resumebad",
                                agents={"general-purpose": _gp_spec()})
        _events, result = run_agent_call(
            runtime=session.agent_runtime, tool_id="call_1", tool_name="Agent",
            tool_input={"description": "x", "prompt": "go", "subagent_type": "general-purpose",
                        "model": "or:mock/scale-resume-badmodel-child"})
        ctx.check(f"first spawn ok, got {result.content!r}", result.is_error is False)
        task_id = next(iter(session.agent_runtime.tasks))
        _events2, result2 = run_agent_call(
            runtime=session.agent_runtime, tool_id="call_resume", tool_name="Agent",
            tool_input={"task_id": task_id, "prompt": "continue", "model": "not-a-real-model-ref"})
        ctx.check(f"a clean ToolResult, never a raised exception, got {result2!r}", result2 is not None)
        ctx.check(f"it's an error, got {result2.content!r}", result2.is_error is True)
    finally:
        mock.stop()


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
