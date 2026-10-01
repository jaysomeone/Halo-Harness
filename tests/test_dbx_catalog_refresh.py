"""tests.test_dbx_catalog_refresh -- H14 scope J: catalog diff, the shared
refresh-if-stale helper, and `halo models --refresh --urls [--json]`.
Every case runs against tests/helpers/mock_databricks.py -- never a real
Databricks call.
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_databricks import MockDatabricks

test, TESTS = new_registry()


class _Env:
    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE",
                        "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS")}
        d = Path(tempfile.mkdtemp(prefix="dbx-catalog-refresh-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        self.state_dir = d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _raw_endpoint(name: str, fm_name: str, api_types: list, task: str = "llm/v1/chat") -> dict:
    """The REAL GET /api/2.0/serving-endpoints shape -- api_types/name/
    model_class nest under "foundation_model" (what `probe_databricks_
    endpoints_full` actually parses) -- feeds `mock.set_endpoints_catalog`."""
    return {"name": name, "task": task, "foundation_model": {"name": fm_name, "api_types": api_types}}


def _parsed_endpoint(name: str, fm_name: str, api_types: list, task: str = "llm/v1/chat") -> dict:
    """The ALREADY-PARSED cache shape `probe_databricks_endpoints_full`
    itself produces from a `_raw_endpoint` (same "name" + fields list shape
    `write_dbx_endpoints_json` expects) -- for tests that write dbx-
    endpoints.json directly (no live probe involved), so a diff against a
    mock-served refresh compares apples to apples."""
    return {"name": name, "task": task, "ready": None, "permission_level": None, "endpoint_type": None,
            "ai_gateway_v2_supported": None, "api_types": api_types, "foundation_model_name": fm_name,
            "model_class": None}


_RAW_V1 = [
    _raw_endpoint("databricks-glm-5-3", "glm-5-3", ["mlflow/v1/chat/completions", "anthropic/v1/messages"]),
    _raw_endpoint("databricks-deepseek-v4-1-flash", "deepseek-v4-1-flash", ["mlflow/v1/chat/completions"]),
]
_RAW_V2 = [
    _raw_endpoint("databricks-glm-5-3", "glm-5-3", ["mlflow/v1/chat/completions"]),  # anthropic removed -> "changed"
    _raw_endpoint("databricks-kimi-k3", "kimi-k3", ["mlflow/v1/chat/completions"]),  # new -> "added"
    # deepseek dropped -> "removed"
]
_PARSED_V1 = [
    _parsed_endpoint("databricks-glm-5-3", "glm-5-3", ["mlflow/v1/chat/completions", "anthropic/v1/messages"]),
    _parsed_endpoint("databricks-deepseek-v4-1-flash", "deepseek-v4-1-flash", ["mlflow/v1/chat/completions"]),
]
_PARSED_V2 = [
    _parsed_endpoint("databricks-glm-5-3", "glm-5-3", ["mlflow/v1/chat/completions"]),
    _parsed_endpoint("databricks-kimi-k3", "kimi-k3", ["mlflow/v1/chat/completions"]),
]


@test
def test_diff_dbx_catalog_added_removed_changed(ctx: Ctx):
    from halo_harness.providers.databricks import diff_dbx_catalog, write_dbx_endpoints_json, load_dbx_endpoints_json
    with _Env() as env:
        write_dbx_endpoints_json(env.state_dir, _PARSED_V1)
        old = load_dbx_endpoints_json(env.state_dir)
        write_dbx_endpoints_json(env.state_dir, _PARSED_V2)
        new = load_dbx_endpoints_json(env.state_dir)
        diff = diff_dbx_catalog(old, new)
        ctx.check(f"kimi added, got {diff['added']}", diff["added"] == ["databricks-kimi-k3"])
        ctx.check(f"deepseek removed, got {diff['removed']}", diff["removed"] == ["databricks-deepseek-v4-1-flash"])
        ctx.check(f"glm changed (api_types), got {diff['changed']}",
                  diff["changed"] == [{"name": "databricks-glm-5-3", "fields": ["api_types"]}])


@test
def test_refresh_dbx_catalog_success_diffs_and_clears_route_cache(ctx: Ctx):
    from halo_harness.providers.databricks import (
        refresh_dbx_catalog, write_dbx_endpoints_json, dbx_cache_set_route,
        dbx_cache_get_route,
    )
    mock = MockDatabricks().start()
    try:
        with _Env() as env:
            write_dbx_endpoints_json(env.state_dir, _PARSED_V1)
            dbx_cache_set_route("databricks-glm-5-3", 0, env.state_dir)
            ctx.check("route cache primed", dbx_cache_get_route("databricks-glm-5-3", env.state_dir) == 0)
            mock.set_endpoints_catalog(_RAW_V2)
            ok, diff, note = refresh_dbx_catalog(env.state_dir, mock.root, "tok")
            ctx.check(f"ok, got {note!r}", ok is True)
            ctx.check(f"diff has the added endpoint, got {diff}", "databricks-kimi-k3" in diff["added"])
            ctx.check("route cache cleared by the refresh (scope D/J)",
                      dbx_cache_get_route("databricks-glm-5-3", env.state_dir) is None)
    finally:
        mock.stop()


@test
def test_refresh_dbx_catalog_failure_keeps_old_cache(ctx: Ctx):
    from halo_harness.providers.databricks import refresh_dbx_catalog, write_dbx_endpoints_json, load_dbx_endpoints_json
    mock = MockDatabricks().start()
    try:
        with _Env() as env:
            write_dbx_endpoints_json(env.state_dir, _PARSED_V1)
            before = load_dbx_endpoints_json(env.state_dir)
            mock.set_endpoints_error("403-ip")
            ok, diff, note = refresh_dbx_catalog(env.state_dir, mock.root, "tok")
            ctx.check(f"not ok, got {note!r}", ok is False)
            ctx.check("diff empty on failure", diff == {})
            ctx.check(f"note mentions keeping the cache, got {note!r}", "keeping the cached catalog" in note)
            after = load_dbx_endpoints_json(env.state_dir)
            ctx.check("cache byte-identical after a failed refresh", after == before)
    finally:
        mock.stop()


@test
def test_refresh_if_stale_skips_when_fresh_and_runs_when_missing(ctx: Ctx):
    from halo_harness.providers.databricks import (
        refresh_dbx_catalog_if_stale, write_dbx_endpoints_json,
    )
    mock = MockDatabricks().start()
    mock.set_endpoints_catalog(_RAW_V1)
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            # No cache yet -> always attempts a refresh regardless of max_age_hours.
            result = refresh_dbx_catalog_if_stale(env.state_dir, max_age_hours=24)
            ctx.check(f"missing cache triggers a refresh, got {result}", result is not None and result[0] is True)

            # Freshly written -> not stale -> no refresh attempted (None).
            write_dbx_endpoints_json(env.state_dir, _PARSED_V1)
            result2 = refresh_dbx_catalog_if_stale(env.state_dir, max_age_hours=24)
            ctx.check(f"fresh cache -> no refresh, got {result2}", result2 is None)

            # max_age_hours=0 -> always stale -> always refreshes.
            result3 = refresh_dbx_catalog_if_stale(env.state_dir, max_age_hours=0)
            ctx.check(f"max_age_hours=0 always refreshes, got {result3}", result3 is not None and result3[0] is True)
    finally:
        mock.stop()


@test
def test_refresh_if_stale_no_databricks_configured_is_none(ctx: Ctx):
    from halo_harness.providers.databricks import refresh_dbx_catalog_if_stale
    with _Env() as env:
        for k in ("BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                  "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
            os.environ.pop(k, None)
        result = refresh_dbx_catalog_if_stale(env.state_dir, max_age_hours=24)
        ctx.check("nothing configured -> None (never crashes)", result is None)


@test
def test_cmd_models_refresh_urls_json_shape(ctx: Ctx):
    from halo_harness.catalog_cli import cmd_models
    mock = MockDatabricks().start()
    mock.set_endpoints_catalog(_RAW_V1)
    try:
        with _Env():
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cmd_models(["--refresh", "--urls", "--json"])
            ctx.check(f"exit 0, got {rc}", rc == 0)
            payload = json.loads(buf.getvalue())
            dbx_rows = payload.get("databricks") or {}
            ctx.check(f"both endpoints present, got {sorted(dbx_rows)}",
                      sorted(dbx_rows) == ["databricks-deepseek-v4-1-flash", "databricks-glm-5-3"])
            glm = dbx_rows["databricks-glm-5-3"]
            ctx.check(f"glm family classified, got {glm.get('family')}", glm["family"] == "glm")
            ctx.check(f"glm default path is mlflow, got {glm.get('path_type')}", glm["path_type"] == "mlflow")
            ctx.check(f"exact URL printed, got {glm.get('url')}",
                      glm["url"] == f"{mock.root}/ai-gateway/mlflow/v1/chat/completions")
            ctx.check("diff present on a --refresh run", "databricks_diff" in payload)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
