"""tests.test_v2a_errors_and_usage -- V2a: error shapes exact across every
gateway type (400 unknown field, 401, 403-IP, 404 wrong-path-fallback, 413/
overflow, 429 with limit_type/retry_after, 5xx, finish_reason=length with
<=1 token), plus usage/cost per type (cached/reasoning tokens, Databricks
cost always n/a, the catalog's own DBU-rate-to-dollars conversion) -- against
the extended tests/helpers/mock_databricks.py.
"""
import json as _json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_databricks import MockDatabricks
from rolo_claude.providers.databricks import write_dbx_endpoints_json
from rolo_claude.providers.profiles import reset_model_table_cache, resolve_profile
from rolo_claude.providers.request import build_request_body
from rolo_claude.providers.routing import Route
from rolo_claude.providers.stream import CompletionRequest, ContextOverflow, ProviderCreds, UpstreamError, stream_completion

test, TESTS = new_registry()


def _req_for(mock, model, state_dir) -> CompletionRequest:
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model=model, dialect="openai-chat")
    profile = resolve_profile(route)
    body = build_request_body(system_text="s", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                               route=route, profile=profile)
    return CompletionRequest(
        body={"messages": []}, route=route, profile={"context_tokens": 128000, "max_output_tokens": 16384},
        creds=ProviderCreds(base_url=mock.root, api_key="tok"), state_dir=state_dir, extra_headers={},
        model_label=model, harness_mode=True, prebuilt_oai_body=body,
    )


def _harness_meta_from(events_list: list) -> dict:
    for ev in events_list:
        if ev.get("type") == "message_delta" and isinstance(ev.get("harness_meta"), dict):
            return ev["harness_meta"]
    return {}


@test
def test_v2a_400_unknown_field_strict_allowlist_every_gateway_path(ctx: Ctx):
    """The SAME strict allowlist guard applies identically to mlflow,
    cursor, and invocations -- no gateway type gets a looser body check."""
    mock = MockDatabricks().start()
    try:
        payload = _json.dumps({"messages": [{"role": "user", "content": "hi"}], "model": "ok",
                                "some_disallowed_field": True}).encode("utf-8")
        for path in ("/ai-gateway/mlflow/v1/chat/completions", "/ai-gateway/cursor/v1/chat/completions",
                     "/serving-endpoints/some-endpoint/invocations"):
            req = urllib.request.Request(f"{mock.root}{path}", data=payload,
                                          headers={"Content-Type": "application/json"}, method="POST")
            try:
                urllib.request.urlopen(req, timeout=5)
                ctx.check(f"{path}: a disallowed field must 400", False)
            except urllib.error.HTTPError as e:
                ctx.check(f"{path}: got 400, actual {e.code}", e.code == 400)
                ctx.check(f"{path}: names the field", "some_disallowed_field" in e.read().decode("utf-8", "replace"))
    finally:
        mock.stop()


@test
def test_v2a_401_maps_to_auth_not_retryable(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        state_dir = Path(tempfile.mkdtemp(prefix="v2a-err-401-"))
        try:
            list(stream_completion(_req_for(mock, "databricks-glm-5-3-401-error", state_dir)))
            ctx.check("401 must raise UpstreamError", False)
        except UpstreamError as e:
            ctx.check(f"status 401, got {e.status}", e.status == 401)
            ctx.check("not retryable", e.retryable is False)
    finally:
        mock.stop()


@test
def test_v2a_403_ip_access_list(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        state_dir = Path(tempfile.mkdtemp(prefix="v2a-err-403-"))
        try:
            list(stream_completion(_req_for(mock, "databricks-glm-5-3-403-ip-error", state_dir)))
            ctx.check("403 must raise UpstreamError", False)
        except UpstreamError as e:
            ctx.check(f"status 403, got {e.status}", e.status == 403)
    finally:
        mock.stop()


@test
def test_v2a_404_on_every_candidate_surfaces_a_real_error(ctx: Ctx):
    """Every candidate 404ing (a genuinely wrong path, not just a
    stale-cache miss) must surface as a real UpstreamError, never a silent
    hang or an infinite candidate loop."""
    mock = MockDatabricks().start()
    try:
        state_dir = Path(tempfile.mkdtemp(prefix="v2a-err-404-"))
        write_dbx_endpoints_json(state_dir, [
            {"name": "databricks-deepseek-404-not-found", "foundation_model_name": "deepseek-404-not-found",
             "task": "llm/v1/chat", "api_types": ["mlflow/v1/chat/completions"]},
        ])
        try:
            list(stream_completion(_req_for(mock, "databricks-deepseek-404-not-found", state_dir)))
            ctx.check("404 on every candidate must raise UpstreamError", False)
        except UpstreamError as e:
            ctx.check(f"status 404, got {e.status}", e.status == 404)
    finally:
        mock.stop()


@test
def test_v2a_413_overflow_non_retryable_and_context_window_category(ctx: Ctx):
    """V2a fix: a literal 413 is now a non-retryable client error (was
    previously defaulting to retryable=True and being retried up to
    MAX_RETRIES times against the identical, still-too-large body)."""
    from rolo_claude.providers.errors import CONTEXT_WINDOW_EXCEEDED, classify_error_category
    mock = MockDatabricks().start()
    try:
        state_dir = Path(tempfile.mkdtemp(prefix="v2a-err-413-"))
        try:
            list(stream_completion(_req_for(mock, "databricks-glm-5-3-413-overflow", state_dir)))
            ctx.check("413 must raise UpstreamError", False)
        except UpstreamError as e:
            ctx.check(f"status 413, got {e.status}", e.status == 413)
            ctx.check("not retryable", e.retryable is False)
            ctx.check(f"category is CONTEXT_WINDOW_EXCEEDED, got {classify_error_category(413, e.message)}",
                      classify_error_category(413, e.message) == CONTEXT_WINDOW_EXCEEDED)
    finally:
        mock.stop()


@test
def test_v2a_context_overflow_400_databricks_wording_parsed(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        state_dir = Path(tempfile.mkdtemp(prefix="v2a-err-overflow-"))
        try:
            list(stream_completion(_req_for(mock, "databricks-glm-5-3-context-overflow-400", state_dir)))
            ctx.check("dbx overflow wording must raise ContextOverflow", False)
        except ContextOverflow as e:
            ctx.check(f"limit parsed as L, got {e.limit}", e.limit == 131072)
            ctx.check(f"prompt_tokens parsed as A, got {e.prompt_tokens}", e.prompt_tokens == 120000)
    finally:
        mock.stop()


@test
def test_v2a_429_limit_type_and_retry_after_parsed(ctx: Ctx):
    from rolo_claude.providers.errors import parse_databricks_rate_limit
    mock = MockDatabricks().start()
    try:
        state_dir = Path(tempfile.mkdtemp(prefix="v2a-err-429-"))
        try:
            list(stream_completion(_req_for(mock, "databricks-glm-5-3-rate-limit-429", state_dir)))
            ctx.check("429 must raise UpstreamError", False)
        except UpstreamError as e:
            ctx.check(f"status 429, got {e.status}", e.status == 429)
            parsed = parse_databricks_rate_limit({"error": {"limit_type": "input_tokens_per_minute",
                                                             "retry_after": 2, "limit": 200000, "current": 200150}})
            ctx.check(f"limit_type extracted, got {parsed}", parsed.get("limit_type") == "input_tokens_per_minute")
            ctx.check(f"retry_after honoured on the real response too, got {e.retry_after!r}",
                      e.retry_after is not None and float(e.retry_after) == 2.0)
    finally:
        mock.stop()


@test
def test_v2a_5xx_retryable(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        state_dir = Path(tempfile.mkdtemp(prefix="v2a-err-500-"))
        try:
            list(stream_completion(_req_for(mock, "databricks-glm-5-3-500-error", state_dir)))
            ctx.check("500 must raise UpstreamError", False)
        except UpstreamError as e:
            ctx.check(f"status mapped, got {e.status}", e.status == 500)
            ctx.check("retryable", e.retryable is True)
    finally:
        mock.stop()


@test
def test_v2a_finish_reason_length_minimal_output_flagged(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        state_dir = Path(tempfile.mkdtemp(prefix="v2a-err-length-"))
        events = list(stream_completion(_req_for(mock, "databricks-glm-5-3-finish-length-minimal", state_dir)))
        meta = _harness_meta_from(events)
        ctx.check(f"length_with_minimal_output flagged, got {meta}", meta.get("length_with_minimal_output") is True)
    finally:
        mock.stop()


@test
def test_v2a_cached_and_reasoning_tokens_parsed(ctx: Ctx):
    mock = MockDatabricks().start()
    try:
        state_dir = Path(tempfile.mkdtemp(prefix="v2a-usage-cached-"))
        events = list(stream_completion(_req_for(mock, "databricks-glm-5-3-usage-cached-tokens", state_dir)))
        usage_events = [e for e in events if e.get("type") == "message_delta" and isinstance(e.get("usage"), dict)]
        usage = usage_events[-1]["usage"] if usage_events else {}
        ctx.check(f"cached tokens parsed, got {usage}", usage.get("cache_read_input_tokens") == 300)
        ctx.check(f"reasoning tokens parsed, got {usage}", usage.get("reasoning_tokens") == 8)
    finally:
        mock.stop()


@test
def test_v2a_databricks_cost_always_na_regardless_of_type(ctx: Ctx):
    """CostMeter is gateway-type-blind -- Databricks never reports cost on
    ANY of mlflow/cursor/invocations/anthropic, so this is one check that
    covers all four by construction (add_usage only branches on `provider`)."""
    from rolo_claude.model import CostMeter
    meter = CostMeter(price_in=0.000001, price_out=0.000002)  # even WITH pricing configured
    cost = meter.add_usage("databricks", {"input_tokens": 100, "output_tokens": 50})
    ctx.check(f"cost is None for databricks, got {cost!r}", cost is None)
    ctx.check("has_cost_data flips False (the real \"n/a\" signal /cost reads)", meter.has_cost_data is False)


@test
def test_v2a_dbu_catalog_rate_converts_to_dollars_when_configured(ctx: Ctx):
    """`dbu_price_usd`/`format_dbu_cost` (providers/dbx_routing.py) is what
    `Controller.list_models()` feeds a catalog endpoint's own
    `usage_policy.output_dbu_per_1k_tokens` through for the `/model` picker's
    informational DBU column (rolo_claude/controller.py, `tui/dialogs/
    model_picker.py`'s `dbu=...` line) -- Databricks itself never reports a
    per-turn DBU spend, so this conversion is catalog-rate display only,
    never a `/cost`/stats figure (see test_v2a_databricks_cost_always_na_
    regardless_of_type above)."""
    import os
    import tempfile as _tempfile
    from rolo_claude.providers.dbx_routing import dbu_price_usd, format_dbu_cost
    from rolo_claude.theme import set_config_value
    saved = {k: os.environ.get(k) for k in ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR")}
    home = Path(_tempfile.mkdtemp(prefix="v2a-dbu-"))
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    os.environ["BRIDGE_STATE_DIR"] = str(home / ".rolo-claude")
    try:
        ctx.check("unset -> raw DBU count", format_dbu_cost(1.5) == "1.500 DBU")
        ctx.check("unset -> unknown stays '?'", format_dbu_cost(None) == "?")
        set_config_value("databricks.dbu_price_usd", 0.07)
        ctx.check(f"price now configured, got {dbu_price_usd()}", dbu_price_usd() == 0.07)
        ctx.check(f"catalog DBU rate converts to dollars, got {format_dbu_cost(2.0)!r}",
                  format_dbu_cost(2.0) == "$0.1400")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
