"""halo_harness.doctor_local -- Halo 2.0.3 round 5d: `halo doctor --local
[--model ol:x]`, the 60-second local acceptance check -- "works out of the
box" proof per machine, and the first thing the docs point a new local-
model user at. Four steps, always run in this fixed order regardless of an
earlier FAIL (brief: "printing PASS or FAIL per step with the plain reason
and the elapsed time"): load the model, one real tool call, one structured
output (the SAME constrained-decoding path `agent/loop.py`'s repair round
uses), one compaction-style summary against a fixture transcript. Every
step is a real request/decode against the live host, through `gym_send.
send_turn` -- the identical primitive the gym's own battery uses, so this
check and `halo gym` can never quietly disagree about what "works" means.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from halo_harness.gym_send import send_turn

_SCRATCH_CONTENT = "halo doctor --local wrote this scratch line for the Read step.\n"
_STRUCTURED_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
_FIXTURE_TRANSCRIPT = (
    "User: Can you add a logging statement to utils.py?\n"
    "Assistant: Added `log.debug(\"starting cleanup\")` at the top of cleanup().\n"
    "User: Now add a matching one at the end of the function.\n"
    "Assistant: Added `log.debug(\"cleanup done\")` right before the return statement.\n"
    "User: Great, run the tests.\n"
    "Assistant: All 42 tests passed.\n"
)


def _resolve_default_ref(state_dir) -> Optional[str]:
    from halo_harness.gym_run import default_model_refs
    from halo_harness.theme import get_config_value
    configured = get_config_value("model", default=None)
    if isinstance(configured, str) and configured.startswith("ol:"):
        return configured
    refs = default_model_refs(state_dir)
    return refs[0] if refs else None


def _resolved(model_ref_raw, state_dir):
    """`(host, route, profile, decision, model_label, error)` -- `error`
    is the one FAIL reason for every step at once when nothing can even
    be resolved (no ref given/found, or no configured host)."""
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.ollama import resolve_ollama_host
    from halo_harness.providers.ollama_hw import resolve_context_decision
    from halo_harness.providers.profiles import resolve_profile
    from halo_harness.providers.routing import Route
    ref_raw = model_ref_raw or _resolve_default_ref(state_dir)
    if not ref_raw:
        return None, None, None, None, None, ("no ol: model given and no default local model found -- pass "
                                               "--model ol:x, or configure `ollama.hosts`/pull a model")
    ref = parse_model_ref(ref_raw)
    if ref.provider != "ollama":
        return None, None, None, None, None, f"{ref_raw!r} is not a local ol: model"
    host = resolve_ollama_host(ref.host)
    if host is None:
        return None, None, None, None, None, f"no configured Ollama host for {ref_raw!r}"
    route = Route(provider="ollama", upstream_model=ref.model, dialect="ollama")
    profile = resolve_profile(route, state_dir=state_dir)
    decision = resolve_context_decision(ref)
    return host, route, profile, decision, ref_raw, None


def _step(name: str, fn, *a, **kw) -> dict:
    t0 = time.monotonic()
    try:
        ok, reason = fn(*a, **kw)
    except Exception as e:
        ok, reason = False, f"{type(e).__name__}: {e}"
    return {"step": name, "ok": bool(ok), "reason": reason, "elapsed_s": round(time.monotonic() - t0, 2)}


def _load(host, route, profile, decision, state_dir, model_label) -> "tuple[bool, str]":
    turn = send_turn(host=host, route=route, profile=profile, decision=decision,
                      system_text="Reply with the single word ready.", messages=[
                          {"role": "user", "content": [{"type": "text", "text": "Reply with the single word ready."}]}],
                      tools=None, requested_max_tokens=8, state_dir=state_dir)
    if turn.error:
        return False, turn.error
    return True, f"{model_label} answered on '{host.name}' ({host.url})"


def _tool_call(host, route, profile, decision, scratch_dir: Path, state_dir) -> "tuple[bool, str]":
    from halo_harness.agent.repair import validate_and_coerce
    from halo_harness.tools.registry import ToolRegistry
    fixture = scratch_dir / "doctor-local-fixture.txt"
    fixture.write_text(_SCRATCH_CONTENT, encoding="utf-8")
    read_tool = ToolRegistry().get("Read")
    read_def = read_tool.definition()
    turn = send_turn(host=host, route=route, profile=profile, decision=decision,
                      system_text="You are a careful tool-using assistant.", messages=[
                          {"role": "user", "content": [{"type": "text",
                           "text": f"Call the Read tool on exactly this file, then stop: {fixture}"}]}],
                      tools=[read_def], requested_max_tokens=128, state_dir=state_dir)
    if turn.error:
        return False, turn.error
    if not turn.tool_blocks:
        return False, "the model replied without calling any tool"
    call = turn.tool_blocks[0]
    if call.get("name") != "Read":
        return False, f"the model called {call.get('name')!r} instead of Read"
    coerced, errors = validate_and_coerce(call.get("input") or {}, read_def["input_schema"])
    if errors:
        return False, f"Read call arguments were not schema-valid: {'; '.join(errors)}"
    from halo_harness.tools.base import ToolContext
    coerced["file_path"] = str(fixture)
    result = read_tool.run(coerced, ToolContext(cwd=scratch_dir))
    if result.is_error:
        return False, f"Read tool call failed to run: {result.content}"
    if _SCRATCH_CONTENT.strip() not in str(result.content):
        return False, "Read ran but did not return the expected scratch-file content"
    return True, f"Read({fixture.name}) called and dispatched, content matched"


def _structured_output(host, route, profile, decision, state_dir) -> "tuple[bool, str]":
    from halo_harness.agent.repair import validate_and_coerce
    from halo_harness.providers.ollama_hw import is_local_host
    from halo_harness.providers.tool_call_schema import supports_constrained_tool_calls
    constrained = supports_constrained_tool_calls(provider="ollama", dialect="ollama", local=is_local_host(host))
    turn = send_turn(
        host=host, route=route, profile=profile, decision=decision,
        system_text="Reply with ONLY a JSON object matching the given schema -- no prose, no markdown fence.",
        messages=[{"role": "user", "content": [{"type": "text",
                   "text": f'Reply with exactly this JSON object: {{"answer": "halo"}}'}]}],
        tools=[], requested_max_tokens=64, force_format=_STRUCTURED_SCHEMA if constrained else None,
        state_dir=state_dir,
    )
    if turn.error:
        return False, turn.error
    import json
    try:
        parsed = json.loads(turn.text.strip())
    except (ValueError, TypeError) as e:
        return False, f"reply was not valid JSON: {e}"
    coerced, errors = validate_and_coerce(parsed if isinstance(parsed, dict) else {}, _STRUCTURED_SCHEMA)
    if errors:
        return False, f"reply JSON failed schema validation: {'; '.join(errors)}"
    mode = "constrained decoding" if constrained else "free-form JSON (host has no constrained-decoding support)"
    return True, f"structured reply validated against the schema ({mode}): {coerced.get('answer')!r}"


def _compaction_summary(host, route, profile, decision, state_dir) -> "tuple[bool, str]":
    turn = send_turn(
        host=host, route=route, profile=profile, decision=decision,
        system_text="Summarize the following conversation in two or three sentences.",
        messages=[{"role": "user", "content": [{"type": "text", "text": _FIXTURE_TRANSCRIPT}]}],
        tools=None, requested_max_tokens=160, state_dir=state_dir,
    )
    if turn.error:
        return False, turn.error
    if not turn.text.strip():
        return False, "the model returned an empty summary"
    return True, f"produced a {len(turn.text.strip())}-character summary of the fixture transcript"


def run_local_acceptance_check(model_ref_raw: Optional[str] = None, *, state_dir,
                                scratch_dir=None) -> "tuple[list, bool]":
    """`(steps, ok)` -- `steps` is always exactly 4 dicts (`{step, ok,
    reason, elapsed_s}`), in order: load, tool call, structured output,
    compaction summary -- even when resolving the model/host fails
    outright (every step then shares that one FAIL reason, never a
    crash). `ok` is True iff every step passed."""
    import tempfile
    host, route, profile, decision, model_label, resolve_error = _resolved(model_ref_raw, state_dir)
    names = ("load", "tool call", "structured output", "compaction summary")
    if resolve_error:
        steps = [{"step": n, "ok": False, "reason": resolve_error, "elapsed_s": 0.0} for n in names]
        return steps, False
    scratch = Path(scratch_dir) if scratch_dir else Path(tempfile.mkdtemp(prefix="halo-doctor-local-"))
    scratch.mkdir(parents=True, exist_ok=True)
    steps = [
        _step("load", _load, host, route, profile, decision, state_dir, model_label),
        _step("tool call", _tool_call, host, route, profile, decision, scratch, state_dir),
        _step("structured output", _structured_output, host, route, profile, decision, state_dir),
        _step("compaction summary", _compaction_summary, host, route, profile, decision, state_dir),
    ]
    return steps, all(s["ok"] for s in steps)


def format_acceptance_lines(steps: "list") -> "list[str]":
    lines = []
    for s in steps:
        tag = "PASS" if s["ok"] else "FAIL"
        lines.append(f"[{tag}] {s['step']} ({s['elapsed_s']}s): {s['reason']}")
    return lines
