"""tests.test_providers_huggingface_catalog -- Halo 2.0.3 round 4: the
router's `GET /v1/models` catalog (`probe_huggingface_models` parsing,
`write_hf_models_json`/`load_hf_models_json` round trip,
`refresh_huggingface_catalog_if_stale`'s TTL/force/not-configured
contract) and error translation (401/402/429/404, through the existing
`providers.stream`/`providers.errors` path every other provider already
goes through) -- against `tests/helpers/mock_openai.py`'s parameterized
`MockUpstream` (round 4's own `path_prefix`/`expected_bearer`/
`models_response` additions), never a second fake server.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.mock_openai import MockUpstream
from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


def _req(mock: MockUpstream, scenario: str, *, api_key: str = "test-key"):
    from halo_harness.providers.routing import Route
    from halo_harness.providers.stream import CompletionRequest, ProviderCreds
    model = f"mock/{scenario}"
    return CompletionRequest(
        body={"model": model, "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
        route=Route(provider="huggingface", upstream_model=model, dialect="openai-chat"),
        profile={"context_tokens": 128000, "max_output_tokens": 16384},
        creds=ProviderCreds(base_url=mock.base_url, api_key=api_key),
        state_dir=Path(tempfile.mkdtemp(prefix="hf-catalog-stream-")),
        extra_headers={}, model_label=model,
    )


# ---- probe_huggingface_models -----------------------------------------------

@test
def test_probe_huggingface_models_parses_router_response(ctx: Ctx):
    from halo_harness.providers.huggingface_catalog import probe_huggingface_models
    mock = MockUpstream(path_prefix="/v1").start()
    mock.models_response = {"data": [
        {"id": "Qwen/Qwen3-32B", "context_length": 40960, "pricing": {"prompt": "0.0000002", "completion": "0.0000006"}},
        {"id": "no-numbers/model"},
        {"not-an-id": "skipped"},
    ]}
    try:
        rows = probe_huggingface_models(mock.base_url, "probe-token")
        by_id = {r["id"]: r for r in rows}
        ctx.check(f"two valid rows parsed (the id-less one skipped), got {sorted(by_id)}",
                  sorted(by_id) == ["Qwen/Qwen3-32B", "no-numbers/model"])
        ctx.check(f"context_length carried through, got {by_id['Qwen/Qwen3-32B'].get('context_length')}",
                  by_id["Qwen/Qwen3-32B"]["context_length"] == 40960)
        ctx.check("a row with neither field omits both rather than inventing None",
                  "context_length" not in by_id["no-numbers/model"] and "pricing" not in by_id["no-numbers/model"])
        gets = [r for r in mock.requests if r["method"] == "GET"]
        ctx.check(f"exactly one GET recorded, got {len(gets)}", len(gets) == 1)
        ctx.check(f"bearer carries the probe's own token, got {gets[0]['headers'].get('authorization')!r}",
                  gets[0]["headers"].get("authorization") == "Bearer probe-token")
    finally:
        mock.stop()


@test
def test_probe_huggingface_models_nested_provider_fallback(ctx: Ctx):
    """When the top-level entry has neither field, a nested `providers`
    list's first entry carrying either is used instead (module docstring:
    a plausible alternate shape, never confirmed against a real response
    this round)."""
    from halo_harness.providers.huggingface_catalog import probe_huggingface_models
    mock = MockUpstream(path_prefix="/v1").start()
    mock.models_response = {"data": [
        {"id": "nested/model", "providers": [{"context_length": 8192, "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]},
    ]}
    try:
        rows = probe_huggingface_models(mock.base_url, "tok")
        ctx.check(f"context_length recovered from the nested providers list, got {rows[0]}",
                  rows[0].get("context_length") == 8192 and rows[0].get("pricing", {}).get("prompt") == "0.000001")
    finally:
        mock.stop()


# ---- write/load round trip + refresh TTL ------------------------------------

@test
def test_write_and_load_hf_models_json_round_trip(ctx: Ctx):
    from halo_harness.providers.huggingface_catalog import load_hf_models_json, write_hf_models_json
    state_dir = Path(tempfile.mkdtemp(prefix="hf-catalog-roundtrip-"))
    write_hf_models_json(state_dir, [{"id": "a/b", "context_length": 1234}, {"no_id_here": "skipped"}])
    loaded = load_hf_models_json(state_dir)
    ctx.check(f"keyed by id, got {sorted(loaded)}", sorted(loaded) == ["a/b"])
    ctx.check(f"context_length round-trips, got {loaded['a/b']}", loaded["a/b"]["context_length"] == 1234)
    ctx.check("a missing file loads as {}", load_hf_models_json(Path(tempfile.mkdtemp(prefix="hf-empty-"))) == {})


@test
def test_refresh_huggingface_catalog_if_stale_none_when_not_configured(ctx: Ctx):
    from halo_harness.providers.huggingface_catalog import refresh_huggingface_catalog_if_stale
    state_dir = Path(tempfile.mkdtemp(prefix="hf-refresh-noconfig-"))
    ctx.check("None (nothing configured) rather than False -- not a failure, just nothing to do",
              refresh_huggingface_catalog_if_stale(state_dir, env={}) is None)


@test
def test_refresh_huggingface_catalog_if_stale_fetches_then_respects_ttl_then_force(ctx: Ctx):
    from halo_harness.providers.huggingface_catalog import hf_models_json_age_seconds, load_hf_models_json, \
        refresh_huggingface_catalog_if_stale
    state_dir = Path(tempfile.mkdtemp(prefix="hf-refresh-ttl-"))
    env = {"HF_TOKEN": "tok"}
    mock = MockUpstream(path_prefix="/v1").start()
    mock.models_response = {"data": [{"id": "a/b", "context_length": 999}]}
    try:
        env["HALO_HF_ROUTER_BASE_URL"] = mock.base_url
        ok1 = refresh_huggingface_catalog_if_stale(state_dir, env=env, max_age_hours=24)
        ctx.check(f"first call (no cache yet) fetches, got {ok1!r}", ok1 is True)
        ctx.check("the catalog file now has the fetched model", "a/b" in load_hf_models_json(state_dir))
        age_after_first = hf_models_json_age_seconds(state_dir)

        mock.clear()
        ok2 = refresh_huggingface_catalog_if_stale(state_dir, env=env, max_age_hours=24)
        ctx.check(f"a fresh cache is not re-fetched, got {ok2!r}", ok2 is None)
        ctx.check("no GET sent for the skipped refresh", not any(r["method"] == "GET" for r in mock.requests))

        ok3 = refresh_huggingface_catalog_if_stale(state_dir, env=env, max_age_hours=24, force=True)
        ctx.check(f"force=True re-fetches regardless of freshness, got {ok3!r}", ok3 is True)
        # -1.0s tolerance: a full suite run under heavy parallel disk I/O can
        # see the filesystem's own mtime lag slightly behind time.time() by a
        # few hundred ms (observed in a 3000+-test run) -- this still catches
        # a real bug (None, a wildly wrong value) without being flaky on a
        # busy box.
        ctx.check(f"age_after_first is a real, roughly-zero-or-positive number, got {age_after_first!r}",
                  isinstance(age_after_first, float) and age_after_first >= -1.0)
    finally:
        mock.stop()


# ---- error translation: through the SAME path every other provider gets --

@test
def test_error_401_maps_to_authentication_error(ctx: Ctx):
    from halo_harness.providers.stream import UpstreamError, stream_completion
    mock = MockUpstream().start()
    try:
        try:
            next(stream_completion(_req(mock, "error-401")))
            ctx.check("expected UpstreamError", False)
        except UpstreamError as e:
            ctx.check(f"status 401, got {e.status}", e.status == 401)
            ctx.check(f"err_type authentication_error, got {e.err_type!r}", e.err_type == "authentication_error")
            ctx.check(f"not retryable, got {e.retryable!r}", e.retryable is False)
    finally:
        mock.stop()


@test
def test_error_402_maps_to_permission_error(ctx: Ctx):
    from halo_harness.providers.stream import UpstreamError, stream_completion
    mock = MockUpstream().start()
    try:
        try:
            next(stream_completion(_req(mock, "error-402")))
            ctx.check("expected UpstreamError", False)
        except UpstreamError as e:
            ctx.check(f"status 402, got {e.status}", e.status == 402)
            ctx.check(f"err_type permission_error, got {e.err_type!r}", e.err_type == "permission_error")
    finally:
        mock.stop()


@test
def test_error_429_maps_to_rate_limit_error_and_is_retryable(ctx: Ctx):
    from halo_harness.providers.stream import UpstreamError, stream_completion
    mock = MockUpstream().start()
    try:
        try:
            next(stream_completion(_req(mock, "error-429")))
            ctx.check("expected UpstreamError", False)
        except UpstreamError as e:
            ctx.check(f"status 429, got {e.status}", e.status == 429)
            ctx.check(f"err_type rate_limit_error, got {e.err_type!r}", e.err_type == "rate_limit_error")
            ctx.check(f"retryable, got {e.retryable!r}", e.retryable is True)
            ctx.check(f"Retry-After carried through, got {e.retry_after!r}", e.retry_after == "2")
    finally:
        mock.stop()


@test
def test_error_404_maps_to_not_found_error(ctx: Ctx):
    """The round 4 brief's own framing: "404 unknown model or provider
    suffix" -- the mock's generic error-<code> scenario stands in for
    either real cause (this layer only cares about the status code)."""
    from halo_harness.providers.stream import UpstreamError, stream_completion
    mock = MockUpstream().start()
    try:
        try:
            next(stream_completion(_req(mock, "error-404")))
            ctx.check("expected UpstreamError", False)
        except UpstreamError as e:
            ctx.check(f"status 404, got {e.status}", e.status == 404)
            ctx.check(f"err_type not_found_error, got {e.err_type!r}", e.err_type == "not_found_error")
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
