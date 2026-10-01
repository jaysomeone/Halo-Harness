"""tests.test_invariants -- agent/invariants.py: unpaired tool_use
detection, synthetic result writing, well-formed-text repair.

H15 Part D2.1: `SessionLog.__init__` ALWAYS resolves its own storage root
via `bridge_home()` (`BRIDGE_STATE_DIR`, else `BRIDGE_TEST_HOME`-derived,
else the REAL `~/.halo`) -- completely independent of whatever
`cwd` is passed to it, and `.mkdir(parents=True, exist_ok=True)` runs
UNCONDITIONALLY at construction time, before a single node is ever
appended. Every `@test` here is therefore transparently wrapped in an
isolated, per-test `BRIDGE_STATE_DIR` (found leaking real empty
`invariants-test-*` slug directories into `~/.halo/sessions` during
the H15 fix pass -- invisible to the OLD file-only REAL SESSIONS GUARD,
closed by D2.2), same pattern `tests/test_log_derive.py` already uses.
"""
import functools
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.agent.log import SessionLog
from halo_harness.agent.invariants import (
    ABORTED_BEFORE_DISPATCH, INTERRUPTED_MESSAGE, find_unpaired_tool_use_ids,
    highest_kimi_functions_idx, repair_truncated_text, synthesize_missing_results, validate_tool_use,
)

_register, TESTS = new_registry()


def test(fn):
    @functools.wraps(fn)
    def wrapper(ctx):
        old = os.environ.get("BRIDGE_STATE_DIR")
        os.environ["BRIDGE_STATE_DIR"] = str(Path(tempfile.mkdtemp(prefix="invariants-test-state-")))
        try:
            return fn(ctx)
        finally:
            if old is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old
    return _register(wrapper)


def _fresh_log() -> SessionLog:
    d = Path(tempfile.mkdtemp(prefix="invariants-test-"))
    return SessionLog(d, session_id="test-session")


@test
def test_h9_highest_kimi_functions_idx_empty_log_is_minus_one(ctx: Ctx):
    log = _fresh_log()
    log.append_system("sys")
    ctx.check("no functions.*:N ids anywhere -> -1 (caller starts fresh at 0)",
              highest_kimi_functions_idx(log) == -1)


@test
def test_h9_highest_kimi_functions_idx_finds_the_max_across_every_assistant_node(ctx: Ctx):
    """H9 critical review finding 1 repro: 6 Reads across 6 SEPARATE
    assistant nodes (i.e. 6 separate model-call steps, exactly the
    real-world shape) must all be seen, not just the last node -- an id
    minted several steps ago is exactly the one a reset-to-0 counter would
    collide with next."""
    log = _fresh_log()
    log.append_system("sys")
    log.append_user([{"type": "text", "text": "go"}])
    for i in range(6):
        log.append_assistant(content=[{"type": "tool_use", "id": f"functions.Read:{i}", "name": "Read", "input": {}}],
                              stop_reason="tool_use")
        log.append_tool_result(tool_use_id=f"functions.Read:{i}", content=f"file {i} contents")
    ctx.check(f"highest idx across all 6 steps is 5, got {highest_kimi_functions_idx(log)}",
              highest_kimi_functions_idx(log) == 5)


@test
def test_h9_highest_kimi_functions_idx_ignores_non_kimi_shaped_ids(ctx: Ctx):
    log = _fresh_log()
    log.append_system("sys")
    log.append_user([{"type": "text", "text": "go"}])
    log.append_assistant(content=[{"type": "tool_use", "id": "call_abc123", "name": "Read", "input": {}}],
                          stop_reason="tool_use")
    log.append_tool_result(tool_use_id="call_abc123", content="x")
    log.append_assistant(content=[{"type": "tool_use", "id": "functions.Grep:2", "name": "Grep", "input": {}}],
                          stop_reason="tool_use")
    log.append_tool_result(tool_use_id="functions.Grep:2", content="y")
    ctx.check(f"only the native-shaped id counts, got {highest_kimi_functions_idx(log)}",
              highest_kimi_functions_idx(log) == 2)


@test
def test_fully_paired_turn_has_no_unpaired_ids(ctx: Ctx):
    log = _fresh_log()
    log.append_system("sys")
    log.append_user([{"type": "text", "text": "hi"}])
    log.append_assistant(content=[{"type": "tool_use", "id": "call_1", "name": "Read", "input": {}}], stop_reason="tool_use")
    log.append_tool_result(tool_use_id="call_1", content="file contents")
    ctx.check("no unpaired ids", find_unpaired_tool_use_ids(log) == [])
    ctx.check("synthesize is a no-op on an already-paired log", synthesize_missing_results(log) == [])


@test
def test_interrupted_turn_gets_synthetic_results(ctx: Ctx):
    log = _fresh_log()
    log.append_system("sys")
    log.append_user([{"type": "text", "text": "hi"}])
    log.append_assistant(
        content=[
            {"type": "tool_use", "id": "call_1", "name": "Read", "input": {}},
            {"type": "tool_use", "id": "call_2", "name": "Read", "input": {}},
        ],
        stop_reason="tool_use",
    )
    # crash/interrupt before either tool_result is written
    missing = find_unpaired_tool_use_ids(log)
    ctx.check(f"both ids detected as unpaired, got {missing}", set(missing) == {"call_1", "call_2"})

    synthesized = synthesize_missing_results(log, reason=INTERRUPTED_MESSAGE)
    ctx.check(f"synthesize_missing_results returns both ids, got {synthesized}", set(synthesized) == {"call_1", "call_2"})
    ctx.check("no unpaired ids remain", find_unpaired_tool_use_ids(log) == [])

    results = [n for n in log.nodes() if n.get("type") == "tool_result"]
    ctx.check("two synthetic tool_result nodes written", len(results) == 2)
    ctx.check("synthetic results are marked is_error", all(r["is_error"] for r in results))
    ctx.check("synthetic results carry the interrupted message", all(r["content"] == INTERRUPTED_MESSAGE for r in results))


@test
def test_partially_answered_turn_only_synthesizes_the_gap(ctx: Ctx):
    log = _fresh_log()
    log.append_system("sys")
    log.append_user([{"type": "text", "text": "hi"}])
    log.append_assistant(
        content=[
            {"type": "tool_use", "id": "call_a", "name": "Read", "input": {}},
            {"type": "tool_use", "id": "call_b", "name": "Read", "input": {}},
        ],
        stop_reason="tool_use",
    )
    log.append_tool_result(tool_use_id="call_a", content="ok")  # call_b never answered (crash mid-dispatch)
    synthesized = synthesize_missing_results(log, reason=ABORTED_BEFORE_DISPATCH)
    ctx.check(f"only call_b synthesized, got {synthesized}", synthesized == ["call_b"])
    total_results = [n for n in log.nodes() if n.get("type") == "tool_result"]
    ctx.check("exactly 2 tool_result nodes total (1 real + 1 synthetic)", len(total_results) == 2)


@test
def test_a_fully_paired_multi_turn_history_has_no_unpaired_ids(ctx: Ctx):
    """A normal, tool-free (or fully-paired) multi-turn history never
    false-positives, regardless of how many assistant nodes precede the
    current one."""
    log = _fresh_log()
    log.append_system("sys")
    log.append_user([{"type": "text", "text": "hi"}])
    log.append_assistant(content=[{"type": "text", "text": "no tools this turn"}], stop_reason="end_turn")
    log.append_user([{"type": "text", "text": "again"}])
    log.append_assistant(content=[{"type": "text", "text": "still no tools"}], stop_reason="end_turn")
    ctx.check("no unpaired ids across a tool-free history", find_unpaired_tool_use_ids(log) == [])


@test
def test_h5b_f03_an_earlier_non_last_assistant_node_left_unpaired_is_still_detected(ctx: Ctx):
    """finding 3 (h4-h5-h3c review): the pre-fix version only ever checked
    the LAST assistant node, reasoning every earlier one is a completed,
    already-paired turn by construction. That reasoning broke once a steer
    noticed mid-`_dispatch_tools` could leave an EARLIER assistant node's
    tool_use unanswered while the turn kept going and appended FURTHER
    turns afterward (finding 3's own steering fix) -- this is the case
    that broke silently before: an unpaired id sitting behind later,
    unrelated assistant/user turns, never self-healed by
    `synthesize_missing_results` (which itself calls this function),
    corrupting every subsequent request on `ant:`/Databricks Claude
    routes forever after."""
    log = _fresh_log()
    log.append_system("sys")
    log.append_user([{"type": "text", "text": "first"}])
    log.append_assistant(
        content=[
            {"type": "tool_use", "id": "call_early_1", "name": "Write", "input": {}},
            {"type": "tool_use", "id": "call_early_2", "name": "Write", "input": {}},
        ],
        stop_reason="tool_use",
    )
    # Only the FIRST call got a result -- the second was never dispatched
    # (the bug finding 3 fixes) -- then the turn kept going regardless.
    log.append_tool_result(tool_use_id="call_early_1", content="wrote a")
    log.append_user([{"type": "text", "text": "a later, unrelated turn"}])
    log.append_assistant(content=[{"type": "text", "text": "a normal reply, no tools"}], stop_reason="end_turn")

    missing = find_unpaired_tool_use_ids(log)
    ctx.check(f"the EARLIER node's dangling tool_use is still found, got {missing}", missing == ["call_early_2"])

    synthesized = synthesize_missing_results(log, reason=ABORTED_BEFORE_DISPATCH)
    ctx.check(f"synthesize_missing_results fixes it even though it isn't in the last assistant node, got {synthesized}",
              synthesized == ["call_early_2"])
    ctx.check("nothing left unpaired afterward", find_unpaired_tool_use_ids(log) == [])


@test
def test_validate_tool_use(ctx: Ctx):
    ctx.check("well-formed block passes", validate_tool_use({"id": "x", "name": "Read", "input": {"a": 1}}) is None)
    ctx.check("empty id rejected", validate_tool_use({"id": "", "name": "Read"}) is not None)
    ctx.check("missing id rejected", validate_tool_use({"name": "Read"}) is not None)
    ctx.check("missing name rejected", validate_tool_use({"id": "x"}) is not None)
    ctx.check("non-dict input rejected", validate_tool_use({"id": "x", "name": "Read", "input": "not-a-dict"}) is not None)
    ctx.check("non-dict block rejected", validate_tool_use("not-a-dict") is not None)


@test
def test_repair_truncated_text_well_formed_passthrough(ctx: Ctx):
    ctx.check("ordinary text unchanged", repair_truncated_text("hello world") == "hello world")
    ctx.check("empty string unchanged", repair_truncated_text("") == "")
    ctx.check("unicode text unchanged", repair_truncated_text("café \U0001F600") == "café \U0001F600")


@test
def test_repair_truncated_text_lone_surrogate(ctx: Ctx):
    lone_high_surrogate = "before" + chr(0xD83D) + "after"  # half of an emoji surrogate pair, truncated
    try:
        lone_high_surrogate.encode("utf-8")
        ctx.check("test setup: a lone surrogate must be unencodable in plain utf-8", False)
    except UnicodeEncodeError:
        pass
    repaired = repair_truncated_text(lone_high_surrogate)
    try:
        repaired.encode("utf-8")
        ctx.check("repaired text is now well-formed UTF-8", True)
    except UnicodeEncodeError:
        ctx.check("repaired text must be encodable as UTF-8", False)
    ctx.check("surrounding text survives", "before" in repaired and "after" in repaired)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
