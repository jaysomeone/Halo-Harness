"""tests.test_databricks_mock -- providers/stream.py + oai_stream.py driven
end to end against tests/helpers/mock_databricks.py: the strict
unknown-field guard, both reasoning shapes decoded, and a 429 body with
retry_after honoured.
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_databricks import MockDatabricks
from rolo_claude.providers.profiles import reset_model_table_cache, resolve_profile
from rolo_claude.providers.request import build_request_body
from rolo_claude.providers.routing import Route
from rolo_claude.providers.stream import CompletionRequest, ProviderCreds, UpstreamError, stream_completion

test, TESTS = new_registry()


def _dbx_req(mock: MockDatabricks, model: str, *, harness_mode: bool = True) -> CompletionRequest:
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model=model, dialect="openai-chat")
    profile = resolve_profile(route)
    body = build_request_body(
        system_text="SYS", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        route=route, profile=profile, context_tokens=128000, prompt_estimate=10,
    )
    return CompletionRequest(
        body={"messages": []}, route=route, profile={"context_tokens": 128000, "max_output_tokens": 16384},
        creds=ProviderCreds(base_url=mock.root, api_key="test-token"),
        state_dir=Path(tempfile.mkdtemp(prefix="dbx-mock-state-")), extra_headers={},
        model_label=model, harness_mode=harness_mode, prebuilt_oai_body=body,
    )


def _harness_meta_from(events_list: list) -> dict:
    for ev in events_list:
        if ev.get("type") == "message_delta" and isinstance(ev.get("harness_meta"), dict):
            return ev["harness_meta"]
    return {}


@test
def test_compliant_request_never_trips_the_unknown_field_guard(ctx: Ctx):
    """A request built by providers.request.build_request_body for a real
    Databricks profile must never contain a key outside the allowlist --
    proven by actually sending it to a mock that 400s on any such key."""
    mock = MockDatabricks().start()
    try:
        req = _dbx_req(mock, "databricks-kimi-k3-reasoning-content-shape")
        events_list = list(stream_completion(req))
        kinds = [e.get("type") for e in events_list]
        ctx.check(f"no error event (the guard did not trip), got kinds={kinds}", "error" not in kinds)
        ctx.check("the mock actually received the request", len(mock.requests) >= 1)
    finally:
        mock.stop()


@test
def test_unknown_field_guard_trips_on_a_deliberately_bad_body(ctx: Ctx):
    """Direct proof the mock's own enforcement works, as a regression
    safety net for this fixture: `call_databricks_chat` ALWAYS allowlist-
    filters before sending (so a disallowed key can never reach the mock
    through the real client path -- that's the point of
    test_compliant_request_never_trips_the_unknown_field_guard above) --
    this test bypasses our client entirely with a raw HTTP POST."""
    import json as _json
    import urllib.request
    mock = MockDatabricks().start()
    try:
        payload = _json.dumps({"messages": [{"role": "user", "content": "hi"}], "model": "ok",
                                "some_disallowed_field": True}).encode("utf-8")
        req = urllib.request.Request(
            f"{mock.root}/ai-gateway/mlflow/v1/chat/completions", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            ctx.check("a disallowed field must get a 400 from the mock", False)
        except urllib.error.HTTPError as e:
            ctx.check(f"400 surfaced, got status={e.code}", e.code == 400)
            body_text = e.read().decode("utf-8", "replace")
            ctx.check(f'message names the field, got {body_text!r}', "some_disallowed_field" in body_text)
    finally:
        mock.stop()


@test
def test_reasoning_content_shape_decoded(ctx: Ctx):
    """Databricks reasoning SHAPE 1: top-level reasoning_content deltas."""
    mock = MockDatabricks().start()
    try:
        req = _dbx_req(mock, "databricks-kimi-k3-reasoning-content-shape")
        events_list = list(stream_completion(req))
        meta = _harness_meta_from(events_list)
        ctx.check(f"reasoning captured, got {meta.get('reasoning_text')!r}",
                  meta.get("reasoning_text") == "thinking step 1 step 2")
        texts = [e["delta"]["text"] for e in events_list if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta"]
        ctx.check(f"displayed text excludes the reasoning, got {texts}", "".join(texts) == "the answer")
    finally:
        mock.stop()


@test
def test_reasoning_blocks_shape_decoded(ctx: Ctx):
    """Databricks reasoning SHAPE 2: {"type":"reasoning","summary":[...]} content-list blocks."""
    mock = MockDatabricks().start()
    try:
        req = _dbx_req(mock, "databricks-claude-reasoning-blocks-shape")
        events_list = list(stream_completion(req))
        meta = _harness_meta_from(events_list)
        ctx.check(f"reasoning captured from the content-list shape, got {meta.get('reasoning_text')!r}",
                  meta.get("reasoning_text") == "block reasoning text")
        texts = [e["delta"]["text"] for e in events_list if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta"]
        ctx.check(f"displayed text is the real answer only, got {texts}", "".join(texts) == "final answer")
    finally:
        mock.stop()


@test
def test_429_body_with_retry_after_honoured(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        req = _dbx_req(mock, "databricks-glm-rate-limit-429")
        try:
            list(stream_completion(req))
            ctx.check("a 429 must surface as UpstreamError", False)
        except UpstreamError as e:
            ctx.check(f"status 429, got {e.status}", e.status == 429)
            # finding 16/7: the mock's 429 body carries retry_after=2 with
            # NO Retry-After HTTP header at all -- this was previously
            # tautological (`in (..., None) or is not None` is always
            # true); now it must be the value the BODY actually said.
            ctx.check(f"retry_after read from the 429 BODY (no header present), got {e.retry_after!r}",
                      e.retry_after is not None and float(e.retry_after) == 2.0)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
