"""tests.test_providers_openai_catalog -- Halo 2.0.3 round 5i part 1:
`GET /v1/models` probe (against the parameterized tests/helpers/
mock_openai.py fake), the write/load round trip, the TTL/staleness-gated
refresh (mirrors tests/test_providers_huggingface_catalog.py's own
shape), and `oai_picker_fields`'s models.dev cross-check.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.mock_openai import MockUpstream
from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_ENV_VARS = ("OPENAI_API_KEY", "BRIDGE_OPENAI_BASE_URL", "HALO_OPENAI_BASE_URL")


class _Env:
    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in (("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR") + _ENV_VARS)}
        d = Path(tempfile.mkdtemp(prefix="oai-catalog-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        for k in _ENV_VARS:
            os.environ.pop(k, None)
        self.state_dir = d / ".halo"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_MODELS_RESPONSE = {
    "object": "list",
    "data": [
        {"id": "gpt-6-astra", "object": "model", "created": 1, "owned_by": "openai"},
        {"id": "gpt-5", "object": "model", "created": 2, "owned_by": "openai"},
        {"id": "", "object": "model", "created": 3, "owned_by": "openai"},  # malformed -- must be skipped
    ],
}


@test
def test_probe_openai_models_parses_bare_id_list(ctx: Ctx):
    from halo_harness.providers.openai_catalog import probe_openai_models
    mock = MockUpstream(path_prefix="/v1", expected_bearer="probe-token").start()
    try:
        mock.models_response = _MODELS_RESPONSE
        ids = probe_openai_models(mock.base_url, "probe-token")
        ctx.check(f"two valid ids parsed (the blank one skipped), got {ids!r}",
                  ids == ["gpt-6-astra", "gpt-5"])
    finally:
        mock.stop()


@test
def test_probe_openai_models_wrong_bearer_401s(ctx: Ctx):
    from halo_harness.providers.openai_catalog import probe_openai_models
    mock = MockUpstream(path_prefix="/v1", expected_bearer="right-token").start()
    try:
        mock.models_response = _MODELS_RESPONSE
        try:
            probe_openai_models(mock.base_url, "wrong-token")
            ctx.check("a 401 must raise", False)
        except RuntimeError as e:
            ctx.check(f"names the 401 status, got {e!r}", "401" in str(e))
    finally:
        mock.stop()


@test
def test_write_then_load_round_trip(ctx: Ctx):
    from halo_harness.providers.openai_catalog import load_oai_models_json, write_oai_models_json
    with _Env() as env:
        write_oai_models_json(env.state_dir, ["gpt-6-astra", "gpt-5"])
        loaded = load_oai_models_json(env.state_dir)
        ctx.check(f"both ids present as keys, got {sorted(loaded.keys())!r}",
                  sorted(loaded.keys()) == ["gpt-5", "gpt-6-astra"])


@test
def test_load_missing_file_is_empty_dict(ctx: Ctx):
    from halo_harness.providers.openai_catalog import load_oai_models_json
    with _Env() as env:
        ctx.check("missing file -> {}", load_oai_models_json(env.state_dir) == {})


@test
def test_refresh_skips_when_not_configured(ctx: Ctx):
    from halo_harness.providers.openai_catalog import refresh_openai_catalog_if_stale
    with _Env() as env:
        result = refresh_openai_catalog_if_stale(env.state_dir, env={})
        ctx.check(f"no key -> None (nothing to refresh), got {result!r}", result is None)


@test
def test_refresh_fetches_when_stale_and_configured(ctx: Ctx):
    from halo_harness.providers.openai_catalog import load_oai_models_json, refresh_openai_catalog_if_stale
    mock = MockUpstream(path_prefix="/v1").start()
    try:
        mock.models_response = _MODELS_RESPONSE
        with _Env() as env:
            result = refresh_openai_catalog_if_stale(
                env.state_dir, env={"OPENAI_API_KEY": "k", "HALO_OPENAI_BASE_URL": mock.base_url}, force=True)
            ctx.check(f"a forced refresh with a key fetches, got {result!r}", result is True)
            loaded = load_oai_models_json(env.state_dir)
            ctx.check(f"cache now has both ids, got {sorted(loaded.keys())!r}",
                      sorted(loaded.keys()) == ["gpt-5", "gpt-6-astra"])
    finally:
        mock.stop()


@test
def test_refresh_skips_when_fresh(ctx: Ctx):
    from halo_harness.providers.openai_catalog import write_oai_models_json, refresh_openai_catalog_if_stale
    with _Env() as env:
        write_oai_models_json(env.state_dir, ["gpt-5"])
        result = refresh_openai_catalog_if_stale(
            env.state_dir, env={"OPENAI_API_KEY": "k"}, max_age_hours=24, force=False)
        ctx.check(f"a just-written cache is fresh -> None (no refetch), got {result!r}", result is None)


@test
def test_refresh_backs_off_after_a_failure(ctx: Ctx):
    from halo_harness.providers.openai_catalog import refresh_openai_catalog_if_stale
    with _Env() as env:
        # No mock server at all -- this one genuinely fails (connect error).
        env_dict = {"OPENAI_API_KEY": "k", "HALO_OPENAI_BASE_URL": "http://127.0.0.1:1/v1"}
        first = refresh_openai_catalog_if_stale(env.state_dir, env=env_dict, force=True)
        ctx.check(f"the failing attempt returns False, got {first!r}", first is False)
        second = refresh_openai_catalog_if_stale(env.state_dir, env=env_dict, force=True)
        ctx.check(f"a second FORCED attempt within the backoff window still returns False "
                  f"(never silently skipped as None under force=True), got {second!r}", second is False)


# ---- picker fields (models.dev cross-check) ----------------------------------

@test
def test_oai_picker_fields_from_vendored_fallback(ctx: Ctx):
    from halo_harness.providers.openai_catalog import oai_picker_fields
    with _Env() as env:
        fields = oai_picker_fields("gpt-6.1-sol", env.state_dir)
        ctx.check(f"context carried through, got {fields.get('context_tokens')!r}",
                  fields.get("context_tokens") == 1_050_000)
        ctx.check(f"price already per-million (no /1e6), got {fields.get('price_in_per_m')!r}",
                  fields.get("price_in_per_m") == 2)


@test
def test_oai_picker_fields_unknown_id_is_empty(ctx: Ctx):
    from halo_harness.providers.openai_catalog import oai_picker_fields
    with _Env() as env:
        ctx.check("an id models.dev doesn't carry -> {}", oai_picker_fields("not-a-real-id", env.state_dir) == {})


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
