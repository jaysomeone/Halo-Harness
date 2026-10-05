"""halo_harness.gym_tool_tasks -- Halo 2.0.3 round 5d: the two battery
tasks that dispatch a REAL tool (Read/Edit) against a scratch fixture --
tool-call accuracy and edit success. See `gym.py`'s module docstring for
what each score means; see `gym_send.py` for the shared real-turn helper.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from halo_harness.gym import RatioScore, ToolCallAccuracy, sample_excerpt
from halo_harness.gym_send import send_repair, send_turn


def _call_excerpt(turn) -> str:
    """Fix pass: one `sample_excerpt`-ready line describing what a turn
    actually produced -- the error, the plain text reply (no tool call at
    all), or a short repr of the first tool call -- so a tool task's own
    `samples` are just as diagnosable as a plain-text task's."""
    if turn.error:
        return f"[error] {turn.error}"
    if not turn.tool_blocks:
        return f"[no tool call] {turn.text!r}" if turn.text else "[no tool call, empty reply]"
    call = turn.tool_blocks[0]
    return f"{call.get('name')}({call.get('input')!r})"

_READ_PROMPT = "Call the Read tool on exactly this file, then stop: {path}"
_EDIT_PROMPT = (
    "Call the Edit tool on exactly this file: {path}\n"
    "Replace the exact text \"world\" with the exact text \"halo\" (old_string=\"world\", "
    "new_string=\"halo\", replace_all=false). Then stop -- no other tool calls, no explanation."
)
_EDIT_ORIGINAL = "Hello, world!\n"
_EDIT_EXPECTED = "Hello, halo!\n"


def _tool_def(name: str) -> dict:
    from halo_harness.tools.registry import ToolRegistry
    return ToolRegistry().get(name).definition()


def run_tool_call_accuracy_task(*, host, route, profile, decision, scratch_dir: Path, n: int,
                                 state_dir, timing: Optional[list] = None) -> ToolCallAccuracy:
    """N independent trials: ask the model to call `Read` on a known
    scratch file, with `Read` the only tool offered. A reply with no tool
    call at all, or one whose arguments fail `agent.repair.validate_and_
    coerce` against Read's own schema, gets exactly ONE local repair round
    (brief: "counting repair rounds separately") before being counted
    `failed`. Never actually dispatches Read (that would always succeed
    for ANY plausible file_path guess, telling the accuracy score
    nothing) -- only the CALL's own shape is judged here; `run_edit_
    success_task` is where a tool call is actually applied and diffed."""
    from halo_harness.agent.repair import validate_and_coerce
    fixture = scratch_dir / "gym-read-fixture.txt"
    fixture.write_text("the gym's own scratch fixture for a Read tool-call task\n", encoding="utf-8")
    read_def = _tool_def("Read")
    result = ToolCallAccuracy(attempted=n)
    for _ in range(n):
        turn = send_turn(
            host=host, route=route, profile=profile, decision=decision,
            system_text="You are a careful tool-using assistant. Use the offered tool exactly once per request.",
            messages=[{"role": "user", "content": [{"type": "text", "text": _READ_PROMPT.format(path=fixture)}]}],
            tools=[read_def], requested_max_tokens=128, state_dir=state_dir,
        )
        if timing is not None and not turn.error and turn.timing_ns:
            timing.append(turn.timing_ns)
        result.samples.append(sample_excerpt(_call_excerpt(turn)))
        if turn.error or not turn.tool_blocks:
            result.failed += 1
            continue
        call = turn.tool_blocks[0]
        _coerced, errors = validate_and_coerce(call.get("input") or {}, read_def["input_schema"])
        if call.get("name") == "Read" and not errors:
            result.valid_first_try += 1
            continue
        result.repair_rounds += 1
        # the repair's own plain failure reason is informational only --
        # not accumulated per-trial in this round (`format_card`'s own
        # `errors` list is for RUN-level failures, not per-attempt ones).
        _repaired, _reason = send_repair(
            host=host, route=route, profile=profile, decision=decision, tool_name=call.get("name") or "Read",
            schema=read_def["input_schema"] if call.get("name") == "Read" else None,
            error_message="; ".join(errors) if errors else "no tool call was made",
            raw_input=call.get("input"), state_dir=state_dir,
        )
        if _repaired is not None:
            result.valid_after_repair += 1
        else:
            result.failed += 1
    return result


def run_edit_success_task(*, host, route, profile, decision, scratch_dir: Path, n: int, state_dir,
                           timing: Optional[list] = None) -> RatioScore:
    """N independent trials: reset a fixture file to `_EDIT_ORIGINAL`, ask
    the model to call `Edit` with the exact replacement, and -- only when
    the call is schema-valid -- actually APPLY it (`EditTool.run`, brief:
    "apply a known edit to a fixture file in a scratch folder") and diff
    the result against `_EDIT_EXPECTED`. The Read-before-Edit guard
    (`tools/edit.py`) is pre-satisfied by seeding `ctx.read_cache`
    directly: this task measures edit-shape correctness, not whether the
    model remembers to Read first (a separate concern from any of the
    battery's four scores)."""
    from halo_harness.agent.repair import validate_and_coerce
    from halo_harness.tools.base import ToolContext
    from halo_harness.tools.registry import ToolRegistry
    fixture = scratch_dir / "gym-edit-fixture.txt"
    edit_def = _tool_def("Edit")
    edit_tool = ToolRegistry().get("Edit")
    result = RatioScore(attempted=n)
    for _ in range(n):
        fixture.write_text(_EDIT_ORIGINAL, encoding="utf-8")
        turn = send_turn(
            host=host, route=route, profile=profile, decision=decision,
            system_text="You are a careful tool-using assistant. Use the offered tool exactly once per request.",
            messages=[{"role": "user", "content": [{"type": "text", "text": _EDIT_PROMPT.format(path=fixture)}]}],
            tools=[edit_def], requested_max_tokens=192, state_dir=state_dir,
        )
        if timing is not None and not turn.error and turn.timing_ns:
            timing.append(turn.timing_ns)
        result.samples.append(sample_excerpt(_call_excerpt(turn)))
        if turn.error or not turn.tool_blocks:
            continue
        call = turn.tool_blocks[0]
        if call.get("name") != "Edit":
            continue
        coerced, errors = validate_and_coerce(call.get("input") or {}, edit_def["input_schema"])
        if errors:
            continue
        coerced["file_path"] = str(fixture)  # the model may not echo an absolute path verbatim; pin it
        ctx = ToolContext(cwd=scratch_dir, read_cache={str(fixture): fixture.stat().st_mtime})
        tool_result = edit_tool.run(coerced, ctx)
        if tool_result.is_error:
            continue
        try:
            after = fixture.read_text(encoding="utf-8")
        except OSError:
            continue
        if after == _EDIT_EXPECTED:
            result.succeeded += 1
    return result
