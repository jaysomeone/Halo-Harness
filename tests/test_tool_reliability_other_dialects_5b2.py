"""tests.test_tool_reliability_other_dialects_5b2 -- Halo 2.0.3 round 5b
part 2 (brief items 1/2 pin: "no repair on other dialects"): a malformed/
invalid-args tool call on an `openrouter`-dialect session gets the exact
SAME plain error as before this round, with NO repair network call at
all -- `_attempt_tool_repair`'s own gate (`providers.tool_call_schema.
supports_constrained_tool_calls`) returns False for this route before any
HTTP request is built, so this needs no working mock handler for a
"repair" step at all; a stray extra request would 404 against the plain
`MockUpstream()` used here (no scenario registered for one), which would
surface as a very different, loud failure if the gate ever regressed.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()

test, TESTS = new_registry()


def _make_session(fh, *, mock: MockUpstream):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds
    session_ctx = SessionContext(cwd=fh["proj"], model_label="mock/plain")
    return Session(
        cwd=fh["proj"], model_ref=parse_model_ref("or:mock/plain"), model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="other-dialect-5b2-")), model_label="mock/plain",
        session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=50,
    )


@test
def test_openrouter_malformed_json_gets_plain_error_no_repair_call(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _make_session(fh, mock=mock)
        tu = {"type": "tool_use", "id": "toolu_x", "name": "Bash", "input": {}}
        tool_call_flags = {"toolu_x": {"malformed_json": True, "raw_input": '{"command": ', "json_error": "bad"}}
        before = len(mock.requests)
        list(session._dispatch_tools(1, [tu], tool_call_flags))
        ctx.check("no HTTP request at all (the gate returns before any network call)",
                  len(mock.requests) == before)
        # Assert on the SESSION LOG (stable across call sites) rather than
        # the raw event stream, whose exact shape varies by caller.
        nodes = session.log.nodes()
        tool_results = [n for n in nodes if n.get("type") == "tool_result"]
        ctx.check(f"a tool_result was logged, got {len(tool_results)}", len(tool_results) == 1)
        content = tool_results[0].get("content") if tool_results else None
        text = content if isinstance(content, str) else str(content)
        ctx.check(f"the SAME plain 'not valid JSON' error as before this round, got {text!r}",
                  "not valid JSON" in text)
    finally:
        mock.stop()


@test
def test_openrouter_invalid_args_gets_plain_error_no_repair_call(ctx: Ctx):
    from halo_harness.agent.repair import RepairOutcome
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _make_session(fh, mock=mock)
        tu = {"type": "tool_use", "id": "toolu_y", "name": "Read", "input": {}}
        outcome = RepairOutcome(block={**tu, "name": "Read"}, ok=False,
                                 error_text="Invalid arguments for Read: missing required parameter 'file_path'",
                                 error_kind="invalid_args")
        item = session._resolve_tool_call(tu, outcome, {})
        before = len(mock.requests)
        ctx.check("no HTTP request at all (the gate returns before any network call)", len(mock.requests) == before)
        ctx.check(f"the plain error text is carried through unchanged, got {item.get('text')!r}",
                  item.get("text") == outcome.error_text)
        ctx.check(f"error_class is schema_invalid, got {item.get('error_class')!r}",
                  item.get("error_class") == "schema_invalid")
        ctx.check("never marked repaired", item.get("repaired") is False)
    finally:
        mock.stop()


@test
def test_openrouter_identical_calls_never_trip_the_new_3x_guard(ctx: Ctx):
    """Fix pass pin: the NEW `ollama`/`huggingface`-only identical-call
    guard (three in a row stops the turn) must never fire for any other
    dialect -- the SAME three identical, otherwise-valid calls that would
    stop an ollama/huggingface turn must all reach `ready=True` here
    (the existing, unchanged, 5/8-threshold generic breaker is the only
    thing that could ever apply, and three repeats never reaches it)."""
    from halo_harness.agent.repair import RepairOutcome
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _make_session(fh, mock=mock)
        for i in range(3):
            tu = {"type": "tool_use", "id": f"toolu_{i}", "name": "Read", "input": {"file_path": "x"}}
            outcome = RepairOutcome(block={**tu, "name": "Read"}, ok=True)
            item = session._resolve_tool_call(tu, outcome, {})
            ctx.check(f"call {i + 1}/3 is NOT stopped by the new guard, got {item!r}",
                      item.get("error_class") != "loop_breaker" and item.get("ready") is True)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
