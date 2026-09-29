"""tests.test_dbx_work_matrix -- H14 scope H: `doctor --work --probe-all`.
One short pong per chat-shaped endpoint through the REAL stream_completion/
stream_anthropic_completion machinery against tests/helpers/mock_databricks.py
-- never a real Databricks call (the real workspace is behind an IP access
list from this box, per the brief)."""
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
                        "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS")}
        d = Path(tempfile.mkdtemp(prefix="dbx-work-matrix-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".rolo-claude")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        self.state_dir = d / ".rolo-claude"
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
def test_run_work_matrix_probes_mlflow_and_anthropic_endpoints(ctx: Ctx):
    from rolo_claude.providers.databricks import write_dbx_endpoints_json
    from rolo_claude.work_matrix import run_work_matrix

    mock = MockDatabricks().start()
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            write_dbx_endpoints_json(env.state_dir, [
                _parsed("databricks-glm-5-3-ok", "glm-5-3", ["mlflow/v1/chat/completions", "anthropic/v1/messages"]),
                _parsed("databricks-claude-opus-4-6-ok", "claude-opus-4-6",
                        ["mlflow/v1/chat/completions", "anthropic/v1/messages"]),
                _parsed("databricks-gte-large-en", "gte-large-en", ["mlflow/v1/embeddings"],
                        task="llm/v1/embeddings"),
            ])
            rows, report_path = run_work_matrix(state_dir=env.state_dir)
            names = [r.name for r in rows]
            ctx.check(f"glm and claude probed, embeddings excluded, got {names}",
                      "databricks-glm-5-3-ok" in names and "databricks-claude-opus-4-6-ok" in names and
                      not any("gte-large" in n for n in names))
            glm_row = next(r for r in rows if r.name == "databricks-glm-5-3-ok")
            ctx.check(f"glm defaults to mlflow, got {glm_row.path_type}", glm_row.path_type == "mlflow")
            ctx.check(f"glm status 200, got {glm_row.status}", glm_row.status == "200")
            ctx.check(f"glm got a real reply, got {glm_row.first_tokens!r}", glm_row.first_tokens == "ok")
            claude_row = next(r for r in rows if r.name == "databricks-claude-opus-4-6-ok")
            ctx.check(f"claude foundation uses the anthropic gateway, got {claude_row.path_type}",
                      claude_row.path_type == "anthropic")
            ctx.check(f"claude status 200, got {claude_row.status}", claude_row.status == "200")
            ctx.check(f"report written, got {report_path}", report_path.exists())
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            joined = json.dumps(payload)
            ctx.check("report has no host/token in it", mock.root not in joined and "tok" != joined)
            ctx.check(f"report lists both endpoints, got {[r['endpoint'] for r in payload['rows']]}",
                      len(payload["rows"]) == 2)
    finally:
        mock.stop()


@test
def test_run_work_matrix_both_flag_adds_anthropic_gateway_row_for_glm(ctx: Ctx):
    from rolo_claude.providers.databricks import write_dbx_endpoints_json
    from rolo_claude.work_matrix import run_work_matrix

    mock = MockDatabricks().start()
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            write_dbx_endpoints_json(env.state_dir, [
                _parsed("databricks-kimi-k3-ok", "kimi-k3", ["mlflow/v1/chat/completions", "anthropic/v1/messages"]),
            ])
            rows_default, _ = run_work_matrix(state_dir=env.state_dir)
            ctx.check(f"without --both, one row only, got {len(rows_default)}", len(rows_default) == 1)

            rows_both, _ = run_work_matrix(state_dir=env.state_dir, both=True)
            ctx.check(f"with --both, mlflow AND anthropic both probed, got {[r.path_type for r in rows_both]}",
                      sorted(r.path_type for r in rows_both) == ["anthropic", "mlflow"])
    finally:
        mock.stop()


@test
def test_run_work_matrix_only_glob_filters(ctx: Ctx):
    from rolo_claude.providers.databricks import write_dbx_endpoints_json
    from rolo_claude.work_matrix import run_work_matrix

    mock = MockDatabricks().start()
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            write_dbx_endpoints_json(env.state_dir, [
                _parsed("databricks-glm-5-3-ok", "glm-5-3", ["mlflow/v1/chat/completions"]),
                _parsed("databricks-kimi-k3-ok", "kimi-k3", ["mlflow/v1/chat/completions"]),
            ])
            rows, _ = run_work_matrix(state_dir=env.state_dir, only="*glm*")
            ctx.check(f"only glm matched, got {[r.name for r in rows]}",
                      [r.name for r in rows] == ["databricks-glm-5-3-ok"])
    finally:
        mock.stop()


@test
def test_run_work_matrix_tool_call_probe(ctx: Ctx):
    from rolo_claude.providers.databricks import write_dbx_endpoints_json
    from rolo_claude.work_matrix import run_work_matrix

    mock = MockDatabricks().start()
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            # The scenario name travels as a SUFFIX of the WIRE model value
            # (mock_databricks.py's own dispatch) -- foundation_model_name
            # is what actually goes on the wire for an mlflow candidate, so
            # it (not just the endpoint name) must carry the suffix.
            write_dbx_endpoints_json(env.state_dir, [
                _parsed("databricks-glm-5-3-tool-call-ok", "glm-5-3-tool-call-ok", ["mlflow/v1/chat/completions"]),
            ])
            rows, _ = run_work_matrix(state_dir=env.state_dir, tools=True)
            ctx.check(f"tool call detected, got {rows[0].tool_call_ok!r}", rows[0].tool_call_ok is True)
    finally:
        mock.stop()


@test
def test_run_work_matrix_error_status_reported_not_crashed(ctx: Ctx):
    from rolo_claude.providers.databricks import write_dbx_endpoints_json
    from rolo_claude.work_matrix import run_work_matrix

    mock = MockDatabricks().start()
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            write_dbx_endpoints_json(env.state_dir, [
                _parsed("databricks-glm-5-3-rate-limit-429", "glm-5-3-rate-limit-429",
                        ["mlflow/v1/chat/completions"]),
            ])
            rows, _ = run_work_matrix(state_dir=env.state_dir)
            ctx.check(f"429 surfaced as a status, not a crash, got {rows[0].status!r}", rows[0].status == "429")
    finally:
        mock.stop()


@test
def test_run_work_matrix_not_configured_writes_a_note_and_no_rows(ctx: Ctx):
    from rolo_claude.work_matrix import run_work_matrix
    with _Env() as env:
        for k in ("BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                  "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
            os.environ.pop(k, None)
        rows, report_path = run_work_matrix(state_dir=env.state_dir)
        ctx.check("no rows", rows == [])
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        ctx.check(f"note explains why, got {payload}", "not configured" in payload.get("note", ""))


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
