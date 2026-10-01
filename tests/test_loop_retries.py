"""tests.test_loop_retries -- finding 16's required tests that need a real
Session driven end to end against a mock upstream: an upstream tool-call id
round-trips into the next request (finding 1), a length-truncated call
yields an error result with the log still fully paired (finding 3), a
`length` reply with <=1 output token re-routes as a provider failure
instead of being retried in place (finding 3), and the loop honours a
Databricks 429 body's retry_after (finding 7).
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, _finish
from tests.helpers.mock_databricks import MockDatabricks, SCENARIOS as DBX_SCENARIOS, _finish as _dbx_finish
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()

REPO_DIR = Path(__file__).resolve().parent.parent

test, TESTS = new_registry()


def _new_session(fh, mock, *, model="or:mock/model"):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
    model_ref = parse_model_ref(model)
    return Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(), creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="loop-retries-")), model_label=model, session_context=session_ctx,
        openrouter_base_url=mock.base_url, max_turns=6,
    )


@test
def test_finding_1_upstream_kimi_id_round_trips_into_the_next_request(ctx: Ctx):
    """finding 16 required test #1: an upstream `functions.Read:0` id must
    come back VERBATIM as the next request's tool_call_id -- the pre-H1
    bug minted a fresh toolu_ id and replayed THAT instead."""
    fh = build_fake_home()
    target = fh["proj"] / "kimi_id_target.txt"
    target.write_text("hi\n", encoding="utf-8")
    mock = MockUpstream().start()

    def _scn_kimi_native_id(h, body):
        messages = (body or {}).get("messages") or []
        if any(m.get("role") == "tool" for m in messages):
            _finish(h, [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"content": "done"}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])
        else:
            _finish(h, [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": "functions.Read:0", "type": "function",
                     "function": {"name": "Read", "arguments": json.dumps({"file_path": str(target)})}},
                ]}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ])

    SCENARIOS["kimi-native-id"] = _scn_kimi_native_id
    try:
        session = _new_session(fh, mock, model="or:mock/kimi-native-id")
        list(session.turn("read the kimi id target file"))
        ctx.check(f"two upstream calls happened, got {len(mock.requests)}", len(mock.requests) == 2)
        second_body = mock.requests[1]["body"] or {}
        tool_msgs = [m for m in second_body.get("messages", []) if m.get("role") == "tool"]
        ctx.check(f"a tool message was sent, got {second_body.get('messages')}", len(tool_msgs) == 1)
        ctx.check(f"tool_call_id is the upstream's OWN id, verbatim, got {tool_msgs[0].get('tool_call_id')!r}",
                  tool_msgs[0].get("tool_call_id") == "functions.Read:0")
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_overflow_classifier_category_reaches_the_error_event(ctx: Ctx):
    """must-do 3: hooks.overflow_classifier is wired into agent/loop.py's
    _step (not just defined/tested in isolation) -- an unfixable context
    overflow's error event carries the dsh-taxonomy `category` alongside
    its own `context_overflow` err_type."""
    from halo_harness.providers.errors import CONTEXT_WINDOW_EXCEEDED
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(fh, mock, model="or:mock/context-overflow-openrouter")
        events_seen = list(session.turn("hi"))
        errors = [e for e in events_seen if e.kind == "error"]
        ctx.check(f"an error event was emitted, got kinds={[e.kind for e in events_seen]}", len(errors) == 1)
        ctx.check(f"category is the dsh taxonomy bucket, got {errors[0].data}",
                   errors[0].data.get("category") == CONTEXT_WINDOW_EXCEEDED)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_finding_3_length_truncated_call_yields_error_result_paired_log(ctx: Ctx):
    """finding 16 required test #5: a `finish_reason: length` tool call
    (kept, tagged truncated_by_length, since Session always runs
    harness_mode=True/strict_tool_json=True) must get a named `is_error`
    tool_result telling the model to split the operation -- not an
    unpaired tool_use that silently ends the turn (the pre-H2 bug)."""
    from halo_harness.agent.invariants import find_unpaired_tool_use_ids
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(fh, mock, model="or:mock/length-cut")  # existing scenario, finish_reason=length + tool_calls
        events_seen = []
        for event in session.turn("read a file"):
            events_seen.append(event)
            if event.kind == "tool_result":
                break
        ctx.check("a tool_result event was emitted for the truncated call", any(e.kind == "tool_result" for e in events_seen))
        tool_result_events = [e for e in events_seen if e.kind == "tool_result"]
        ctx.check(f"it's an error, got {tool_result_events[-1].data}", tool_result_events[-1].data.get("ok") is False)
        summary = tool_result_events[-1].data.get("summary", "")
        ctx.check(f"names the truncation and tells the model to split the call, got {summary!r}",
                  "cut off at max_tokens" in summary and "split the operation" in summary)
        ctx.check("the log is fully paired (no unanswered tool_use)", find_unpaired_tool_use_ids(session.log) == [])
    finally:
        try:
            session.abort.set()
        except Exception:
            pass
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_finding_3_length_with_one_token_reroutes_as_provider_failure(ctx: Ctx):
    """finding 16 required test #6: `finish_reason: length` with <=1
    output token is indistinguishable from a genuine per-endpoint cap --
    treated as a provider failure to re-route, NEVER retried in place
    (report: 64-tool/~287k-token prompts got this 5/5 times on OpenRouter)."""
    fh = build_fake_home()
    mock = MockUpstream().start()

    def _scn_length_one_token(h, body):
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": "x"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
             "usage": {"prompt_tokens": 5000, "completion_tokens": 1}},
        ])

    SCENARIOS["length-one-token"] = _scn_length_one_token
    try:
        session = _new_session(fh, mock, model="or:mock/length-one-token")
        events_seen = list(session.turn("hi"))
        errors = [e for e in events_seen if e.kind == "error"]
        ctx.check(f"an error event was emitted, got kinds={[e.kind for e in events_seen]}", len(errors) == 1)
        ctx.check(f"tagged provider_failure, got {errors[0].data}", errors[0].data.get("err_type") == "provider_failure")
        ctx.check("NEVER retried in place -- exactly one upstream call", len(mock.requests) == 1)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_finding_7_loop_honours_databricks_429_retry_after(ctx: Ctx):
    """finding 16 required test #8: the LOOP (not just stream_completion in
    isolation) must actually WAIT the retry_after a Databricks 429 body
    names before its retry, then succeed."""
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds

    calls = {"n": 0}

    def _scn_fail_once_then_ok(h, body):
        calls["n"] += 1
        if calls["n"] == 1:
            from tests.helpers.mock_databricks import _send_json
            _send_json(h, 429, {"error": {
                "message": "Rate limit exceeded: output_tokens_per_minute", "type": "rate_limit_exceeded",
                "code": 429, "limit_type": "output_tokens_per_minute", "limit": 40000, "current": 40001,
                "retry_after": 1,
            }})
        else:
            _dbx_finish(h, [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"content": "ok after retry"}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])

    DBX_SCENARIOS["fail-once-429"] = _scn_fail_once_then_ok
    mock = MockDatabricks().start()
    try:
        fh = build_fake_home()
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        session_ctx = SessionContext(cwd=fh["proj"], model_label="dbx:databricks-kimi-k3-fail-once-429")
        model_ref = parse_model_ref("dbx:databricks-kimi-k3-fail-once-429")
        session = Session(
            cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
            creds=ProviderCreds(base_url=mock.root, api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="dbx-429-loop-")), model_label=model_ref.raw,
            session_context=session_ctx, max_turns=4,
        )
        t0 = time.monotonic()
        events_seen = list(session.turn("hi"))
        dt = time.monotonic() - t0
        ctx.check(f"two upstream calls (one 429, one ok), got {calls['n']}", calls["n"] == 2)
        ctx.check(f"the retry_after=1s from the BODY was actually observed, got dt={dt:.2f}s", dt >= 0.9)
        ctx.check(f"bounded (not a long ladder wait), got dt={dt:.2f}s", dt < 10.0)
        texts = [e.data.get("text", "") for e in events_seen if e.kind == "text_delta"]
        ctx.check(f"the retried call's real answer came through, got {texts}", "".join(texts) == "ok after retry")
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)


@test
def test_h8_retry_wait_cap_raised_to_300s_and_logged(ctx: Ctx):
    """H8 cheap must-do: `_step`'s per-wait cap is 300s (raised from 60s)
    -- a provider's own longer `Retry-After` (a real 429 body can
    legitimately ask for several minutes) is honoured up to 5 minutes
    instead of being silently clipped to one."""
    import logging
    from halo_harness.agent.loop import _MAX_RETRY_WAIT_S, _capped_retry_delay

    ctx.check(f"the cap constant itself is 300s, got {_MAX_RETRY_WAIT_S}", _MAX_RETRY_WAIT_S == 300.0)

    # A 90s Retry-After used to be clipped to 60s -- now passes through unchanged.
    ctx.check("a 90s Retry-After is honoured in full (was silently clipped to 60s before)",
              _capped_retry_delay(1, {"retry-after": "90"}) == 90.0)

    # A Retry-After longer than the new 300s cap is still clipped, and the clip is logged.
    class _CaptureHandler(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record.getMessage())

    handler = _CaptureHandler()
    logger = logging.getLogger("bridge")
    logger.addHandler(handler)
    try:
        delay = _capped_retry_delay(1, {"retry-after": "600"})
    finally:
        logger.removeHandler(handler)
    ctx.check(f"a 600s Retry-After is still clipped to the 300s cap, got {delay}", delay == 300.0)
    ctx.check(f"the clip is logged, got {handler.records}",
              any("capped" in r and "600" in r for r in handler.records))


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
