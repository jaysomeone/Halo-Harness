"""tests.test_v2a_work_matrix_probes -- V2a: the two open-question probes
`doctor --work --probe-all --tools` now runs, end to end, through
`halo_harness.work_matrix.run_work_matrix` against the extended
tests/helpers/mock_databricks.py:

  1. "reasoning replay after a tool call per family" -- `ProbeRow.
     reasoning_replay_ok` (None when not applicable, else whether a REAL
     second turn -- built through the exact same providers.request builder
     a live session uses -- replaying the captured reasoning/thinking plus
     the tool result was accepted).
  2. "route split per endpoint from the cache" -- `ProbeRow.cached_path_type`
     (what the on-disk route cache said BEFORE this run) vs. `path_type`
     (what this run actually used/re-cached); a mismatch is a visible split.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_databricks import MockDatabricks

test, TESTS = new_registry()


class _Env:
    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE",
                        "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN")}
        d = Path(tempfile.mkdtemp(prefix="v2a-work-matrix-"))
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


def _parsed(name, fm_name, api_types, task="llm/v1/chat"):
    return {"name": name, "task": task, "ready": None, "permission_level": None, "endpoint_type": None,
            "ai_gateway_v2_supported": None, "api_types": api_types, "foundation_model_name": fm_name,
            "model_class": None}


@test
def test_v2a_cached_path_type_none_on_first_discovery(ctx: Ctx):
    """finding (V2a): `providers.databricks._DBX_ROUTE_CACHE` is an
    in-memory dict keyed by MODEL NAME ONLY, shared process-wide across
    every test module `tests/run_all.py` imports into one process -- NOT
    scoped per state_dir. A model name any OTHER test file also uses (e.g.
    test_dbx_work_matrix.py's own "databricks-glm-5-3-ok") would already be
    cached from that earlier test by the time this one runs, even though
    THIS test's own on-disk state_dir has never seen it -- a false "already
    cached" result that has nothing to do with what this test actually
    wrote to disk. A name unique to this test file sidesteps that shared
    global entirely, matching how every other test in this file already
    avoids collisions."""
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    from halo_harness.work_matrix import run_work_matrix
    mock = MockDatabricks().start()
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            write_dbx_endpoints_json(env.state_dir, [
                _parsed("databricks-glm-5-3-v2a-first-discovery", "glm-5-3-v2a-first-discovery-ok",
                        ["mlflow/v1/chat/completions"]),
            ])
            rows, _ = run_work_matrix(state_dir=env.state_dir)
            row = rows[0]
            ctx.check(f"nothing cached yet, got {row.cached_path_type!r}", row.cached_path_type is None)
            ctx.check(f"this run discovered+cached mlflow, got {row.path_type!r}", row.path_type == "mlflow")
    finally:
        mock.stop()


@test
def test_v2a_cached_path_type_shows_a_route_split(ctx: Ctx):
    """A stale cache (seeded to "cursor", which no longer actually answers
    this run) fails over to mlflow -- `cached_path_type` (what the on-disk
    cache said going in) now visibly differs from `path_type` (what this
    run actually used and re-cached), exactly the "route split" open
    question a human reviewing the live report needs to see."""
    from halo_harness.providers.databricks import dbx_cache_set_route, write_dbx_endpoints_json
    from halo_harness.providers.dbx_routing import chat_route_candidates
    from halo_harness.work_matrix import run_work_matrix
    mock = MockDatabricks().start()
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            write_dbx_endpoints_json(env.state_dir, [
                _parsed("databricks-gpt-5-ok", "gpt-5-cursor-404-then-ok",
                        ["mlflow/v1/chat/completions", "cursor/v1/chat/completions"]),
            ])
            cands = chat_route_candidates("databricks-gpt-5-ok", env.state_dir)
            cursor_idx = next(i for i, c in enumerate(cands) if c.key == "cursor")
            dbx_cache_set_route("databricks-gpt-5-ok", cursor_idx, env.state_dir)
            rows, _ = run_work_matrix(state_dir=env.state_dir)
            row = rows[0]
            ctx.check(f"stale cache said cursor, got {row.cached_path_type!r}", row.cached_path_type == "cursor")
            ctx.check(f"cursor no longer answers -> falls over to mlflow, got {row.path_type!r}",
                      row.path_type == "mlflow")
            ctx.check("a real split is visible (cached != actual)", row.cached_path_type != row.path_type)
    finally:
        mock.stop()


@test
def test_v2a_reasoning_replay_ok_true_openai_dialect(ctx: Ctx):
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    from halo_harness.work_matrix import run_work_matrix
    mock = MockDatabricks().start()
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            write_dbx_endpoints_json(env.state_dir, [
                _parsed("databricks-deepseek-replay-ok", "deepseek-reasoning-replay-loop", ["mlflow/v1/chat/completions"]),
            ])
            rows, _ = run_work_matrix(state_dir=env.state_dir, tools=True)
            row = rows[0]
            ctx.check(f"tool call detected on turn 1, got {row.tool_call_ok!r}", row.tool_call_ok is True)
            ctx.check(f"reasoning replay accepted on turn 2, got {row.reasoning_replay_ok!r}",
                      row.reasoning_replay_ok is True)
    finally:
        mock.stop()


@test
def test_v2a_reasoning_replay_ok_true_anthropic_dialect(ctx: Ctx):
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    from halo_harness.work_matrix import run_work_matrix
    mock = MockDatabricks().start()
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            write_dbx_endpoints_json(env.state_dir, [
                _parsed("databricks-claude-opus-4-6-thinking-then-tool-then-reply", "claude-opus-4-6",
                        ["anthropic/v1/messages"]),
            ])
            rows, _ = run_work_matrix(state_dir=env.state_dir, tools=True)
            row = rows[0]
            ctx.check(f"claude foundation uses the anthropic gateway, got {row.path_type!r}", row.path_type == "anthropic")
            ctx.check(f"tool call detected, got {row.tool_call_ok!r}", row.tool_call_ok is True)
            ctx.check(f"a real signed thinking block replayed and accepted, got {row.reasoning_replay_ok!r}",
                      row.reasoning_replay_ok is True)
    finally:
        mock.stop()


@test
def test_v2a_reasoning_replay_ok_false_when_upstream_rejects_it(ctx: Ctx):
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    from halo_harness.work_matrix import run_work_matrix
    mock = MockDatabricks().start()
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            write_dbx_endpoints_json(env.state_dir, [
                _parsed("databricks-deepseek-replay-bad", "deepseek-reasoning-replay-rejected",
                        ["mlflow/v1/chat/completions"]),
            ])
            rows, _ = run_work_matrix(state_dir=env.state_dir, tools=True)
            row = rows[0]
            ctx.check(f"turn 1 tool call still detected, got {row.tool_call_ok!r}", row.tool_call_ok is True)
            ctx.check(f"turn 2 rejection correctly reported as a failed replay, got {row.reasoning_replay_ok!r}",
                      row.reasoning_replay_ok is False)
    finally:
        mock.stop()


@test
def test_v2a_reasoning_replay_ok_none_without_tools(ctx: Ctx):
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    from halo_harness.work_matrix import run_work_matrix
    mock = MockDatabricks().start()
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            # A name unique to this test (not "databricks-glm-5-3-ok", which
            # tests/test_dbx_work_matrix.py also uses) -- see the previous
            # test's own docstring on why a shared name risks reading a
            # stale process-wide _DBX_ROUTE_CACHE entry from an unrelated
            # test module's earlier run.
            write_dbx_endpoints_json(env.state_dir, [
                _parsed("databricks-glm-5-3-v2a-no-tools", "glm-5-3-v2a-no-tools-ok",
                        ["mlflow/v1/chat/completions"]),
            ])
            rows, _ = run_work_matrix(state_dir=env.state_dir)  # tools=False (default)
            ctx.check(f"not applicable without --tools, got {rows[0].reasoning_replay_ok!r}",
                      rows[0].reasoning_replay_ok is None)
    finally:
        mock.stop()


@test
def test_v2a_report_json_carries_both_open_question_fields_no_secrets(ctx: Ctx):
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    from halo_harness.work_matrix import run_work_matrix
    mock = MockDatabricks().start()
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            write_dbx_endpoints_json(env.state_dir, [
                _parsed("databricks-deepseek-replay-ok", "deepseek-reasoning-replay-loop", ["mlflow/v1/chat/completions"]),
            ])
            _rows, report_path = run_work_matrix(state_dir=env.state_dir, tools=True)
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            row = payload["rows"][0]
            ctx.check(f"cached_path_type key present, got {row}", "cached_path_type" in row)
            ctx.check(f"reasoning_replay_ok key present, got {row}", row.get("reasoning_replay_ok") is True)
            joined = json.dumps(payload)
            ctx.check(f"still no real host in the report, got {joined!r}", mock.root not in joined)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
