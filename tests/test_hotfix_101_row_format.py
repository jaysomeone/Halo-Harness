"""tests.test_hotfix_101_row_format -- 1.0.1 hotfix 12: one unified
ctx/output/price row format for every model-listing surface, normalized
token-count units, never a bare "?", and `fetch_models_dev`'s own
User-Agent header (models.dev's CDN 403s a header-less/default urllib
agent).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


# ---------------------------------------------------------------------------
# format_token_count / format_price_per_m
# ---------------------------------------------------------------------------

@test
def test_format_token_count_normalizes_k_and_m_units(ctx: Ctx):
    from rolo_claude.model_display import format_token_count
    cases = {200000: "200k", 128000: "128k", 1048576: "1M", 1050000: "1M", 500: "500", 16384: "16k"}
    for n, expected in cases.items():
        got = format_token_count(n)
        ctx.check(f"{n} -> {expected!r}, got {got!r}", got == expected)


@test
def test_format_token_count_blank_never_question_mark(ctx: Ctx):
    from rolo_claude.model_display import format_token_count
    for bad in (None, 0, -5, "not a number", True, False):
        got = format_token_count(bad)
        ctx.check(f"{bad!r} -> blank, got {got!r}", got == "")
        ctx.check(f"{bad!r} never renders as '?'", got != "?")


@test
def test_format_price_per_m_never_multiplies(ctx: Ctx):
    from rolo_claude.model_display import format_price_per_m
    ctx.check(f"already-per-million value formatted as-is, got {format_price_per_m(3.0)!r}",
              format_price_per_m(3.0) == "$3.00/M")
    ctx.check(f"blank (never '?') for None, got {format_price_per_m(None)!r}", format_price_per_m(None) == "")


# ---------------------------------------------------------------------------
# format_model_row: single line, no "?", <=110 columns, for all three
# source scenarios (models.dev-backed, model_table-only, unknown).
# ---------------------------------------------------------------------------

@test
def test_format_model_row_models_dev_backed_entry_full_columns(ctx: Ctx):
    from rolo_claude.model_display import format_model_row
    entry = {"ref": "dbx:databricks-glm-5-3", "context_tokens": 128000, "max_output_tokens": 64000,
             "price_in_per_m": 1.0, "price_out_per_m": 5.0, "detail": "glm · mlflow-chat"}
    row = format_model_row(entry)
    ctx.check(f"single line, got {row!r}", "\n" not in row)
    ctx.check(f"<=110 columns, got {len(row)}", len(row) <= 110)
    ctx.check(f"never a bare '?', got {row!r}", "?" not in row)
    ctx.check(f"shows normalized ctx, got {row!r}", "128k" in row)
    ctx.check(f"shows normalized output, got {row!r}", "64k" in row)
    ctx.check(f"shows both prices, got {row!r}", "$1.00/M" in row and "$5.00/M" in row)
    ctx.check(f"shows the detail tag, got {row!r}", "[glm · mlflow-chat]" in row)


@test
def test_format_model_row_model_table_only_ctx_present_price_blank(ctx: Ctx):
    from rolo_claude.model_display import format_model_row
    entry = {"ref": "dbx:databricks-deepseek-v4-1-flash", "context_tokens": 1048576}
    row = format_model_row(entry)
    ctx.check(f"single line, got {row!r}", "\n" not in row)
    ctx.check(f"<=110 columns, got {len(row)}", len(row) <= 110)
    ctx.check(f"never a bare '?', got {row!r}", "?" not in row)
    ctx.check(f"ctx present, got {row!r}", "1M" in row)
    ctx.check(f"no price shown (blank field), got {row!r}", "in=$" not in row and "out=$" not in row)


@test
def test_format_model_row_unknown_endpoint_all_blank(ctx: Ctx):
    from rolo_claude.model_display import format_model_row
    entry = {"ref": "dbx:some-brand-new-endpoint"}
    row = format_model_row(entry)
    ctx.check(f"single line, got {row!r}", "\n" not in row)
    ctx.check(f"<=110 columns, got {len(row)}", len(row) <= 110)
    ctx.check(f"never a bare '?', got {row!r}", "?" not in row)
    ctx.check(f"no dbu suffix when unknown, got {row!r}", "dbu=" not in row)


@test
def test_format_model_row_dbu_appended_only_when_known(ctx: Ctx):
    from rolo_claude.model_display import format_model_row
    with_dbu = format_model_row({"ref": "dbx:x", "dbu": "0.500 DBU"})
    without_dbu = format_model_row({"ref": "dbx:x"})
    ctx.check(f"dbu appended when known, got {with_dbu!r}", "dbu=0.500 DBU" in with_dbu)
    ctx.check(f"no dbu suffix at all when unknown, got {without_dbu!r}", "dbu=" not in without_dbu)


# ---------------------------------------------------------------------------
# databricks_row_fields: (a) models.dev live cache, (b) model_table.json
# fallback (ctx only), (c) neither -> {}.
# ---------------------------------------------------------------------------

@test
def test_databricks_row_fields_prefers_live_models_dev_cache(ctx: Ctx):
    from rolo_claude.model_display import databricks_row_fields
    from rolo_claude.providers.models_dev import write_models_dev_json
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp)
        write_models_dev_json(state_dir, {"databricks": {"models": {
            "databricks-glm-5-3": {"limit": {"context": 128000, "output": 64000},
                                    "cost": {"input": 1.0, "output": 5.0}},
        }}})
        fields = databricks_row_fields("databricks-glm-5-3", state_dir=state_dir)
        ctx.check(f"context from models.dev, got {fields}", fields.get("context_tokens") == 128000)
        ctx.check(f"output from models.dev, got {fields}", fields.get("max_output_tokens") == 64000)
        ctx.check(f"price_in NOT multiplied (already per-million), got {fields}", fields.get("price_in_per_m") == 1.0)
        ctx.check(f"price_out NOT multiplied, got {fields}", fields.get("price_out_per_m") == 5.0)


@test
def test_databricks_row_fields_falls_back_to_model_table_ctx_only(ctx: Ctx):
    from rolo_claude.model_display import databricks_row_fields
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp)  # no models-dev.json cache at all here
        fake_table = {"databricks": {"databricks-deepseek-v4-1-flash": {"context_tokens": 1048576}}}
        fields = databricks_row_fields("databricks-deepseek-v4-1-flash", state_dir=state_dir, model_table=fake_table)
        ctx.check(f"context from model_table.json, got {fields}", fields.get("context_tokens") == 1048576)
        ctx.check(f"no output (model_table has none), got {fields}", "max_output_tokens" not in fields)
        ctx.check(f"no prices at all, got {fields}",
                  "price_in_per_m" not in fields and "price_out_per_m" not in fields)


@test
def test_databricks_row_fields_unknown_endpoint_returns_empty(ctx: Ctx):
    from rolo_claude.model_display import databricks_row_fields
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp)
        fields = databricks_row_fields("totally-unknown-endpoint", state_dir=state_dir, model_table={})
        ctx.check(f"empty dict for a name in neither source, got {fields}", fields == {})


# ---------------------------------------------------------------------------
# fetch_models_dev: sends a real User-Agent (models.dev's CDN 403s the bare
# default urllib agent) -- a tiny local mock server captures the header.
# ---------------------------------------------------------------------------

class _ThreadingHTTPServer(HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _CapturingHandler(BaseHTTPRequestHandler):
    captured_headers: "list[dict]" = []

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        _CapturingHandler.captured_headers.append(dict(self.headers.items()))
        body = json.dumps({"databricks": {"models": {}}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@test
def test_fetch_models_dev_sends_a_real_user_agent(ctx: Ctx):
    from rolo_claude import __version__
    from rolo_claude.providers.models_dev import fetch_models_dev
    _CapturingHandler.captured_headers = []
    server = _ThreadingHTTPServer(("127.0.0.1", 0), _CapturingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        fetch_models_dev(base_url=base_url)
        ctx.check(f"exactly one request captured, got {len(_CapturingHandler.captured_headers)}",
                  len(_CapturingHandler.captured_headers) == 1)
        ua = _CapturingHandler.captured_headers[0].get("User-Agent", "")
        ctx.check(f"User-Agent names rolo-claude with its version, got {ua!r}",
                  ua == f"rolo-claude/{__version__}")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


# ---------------------------------------------------------------------------
# 1.0.1 fixpass finding 7: models.json stores EVERY OpenRouter price as a
# STRING (e.g. "0.0000008") -- every price reader must accept that, not
# only int/float, or the column renders blank everywhere.
# ---------------------------------------------------------------------------

@test
def test_format_price_per_m_accepts_numeric_strings(ctx: Ctx):
    from rolo_claude.model_display import format_price_per_m
    ctx.check(f"a numeric string formats exactly like the equivalent float, got {format_price_per_m('3.5')!r}",
              format_price_per_m("3.5") == "$3.50/M")
    ctx.check(f"a non-numeric string is still blank, got {format_price_per_m('n/a')!r}",
              format_price_per_m("n/a") == "")


@test
def test_openrouter_price_entries_string_prices_produce_real_numbers(ctx: Ctx):
    """init_providers._openrouter_entries: the models.json shape OpenRouter
    probes actually write (pricing.prompt/completion as strings)."""
    from rolo_claude.init_providers import _openrouter_entries
    from rolo_claude.providers.databricks import write_models_json
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp)
        write_models_json(state_dir, [{
            "id": "vendor/model-x", "context_length": 128000, "max_output_tokens": 8192,
            "pricing": {"prompt": "0.0000008", "completion": "0.0000024"},
        }])
        entries = {e["ref"]: e for e in _openrouter_entries(state_dir)}
        row = entries["or:vendor/model-x"]
        ctx.check(f"price_in_per_m is a real number, not None, got {row}", row["price_in_per_m"] is not None)
        ctx.check(f"price_in_per_m correctly converted (0.0000008 * 1e6 == 0.8), got {row['price_in_per_m']}",
                  abs(row["price_in_per_m"] - 0.8) < 1e-9)
        ctx.check(f"price_out_per_m correctly converted, got {row['price_out_per_m']}",
                  abs(row["price_out_per_m"] - 2.4) < 1e-9)


@test
def test_controller_list_models_openrouter_string_prices_not_blank(ctx: Ctx):
    """The SAME bug, exercised through Controller.list_models() -- the
    exact path the /model picker and init's own picker both call."""
    from rolo_claude.controller import Controller
    from rolo_claude.providers.databricks import write_models_json

    class _FakeModelRef:
        raw = "or:vendor/model-x"
        provider = "openrouter"

    class _FakeModelProfile:
        context_tokens = 128000
        max_output_tokens = 8192

    class _FakeSession:
        model_ref = _FakeModelRef()
        model_profile = _FakeModelProfile()

    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp)
        write_models_json(state_dir, [{
            "id": "vendor/model-x", "context_length": 128000, "max_output_tokens": 8192,
            "pricing": {"prompt": "0.0000008", "completion": "0.0000024"},
        }])
        ctrl = Controller(session=_FakeSession(), cwd=Path.cwd(), state_dir=state_dir, routes={})
        rows = {m["ref"]: m for m in ctrl.list_models()}
        row = rows["or:vendor/model-x"]
        ctx.check(f"price_in_per_m populated (not None) from a string price, got {row}",
                  row.get("price_in_per_m") is not None)
        ctx.check(f"price_out_per_m populated (not None) from a string price, got {row}",
                  row.get("price_out_per_m") is not None)


class _Env:
    """Scopes ONLY what test_cmd_models_cli_prints_openrouter_prices_not_
    blank below touches -- cmd_models(argv) (no --refresh) never reads any
    provider credential, only BRIDGE_TEST_HOME/BRIDGE_STATE_DIR/BRIDGE_ENV_
    FILE (bridge_home()'s own lookup)."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE")}
        d = Path(tempfile.mkdtemp(prefix="hotfix101-price-cli-"))
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


@test
def test_cmd_models_cli_prints_openrouter_prices_not_blank(ctx: Ctx):
    """`rolo-claude models` (bare, no --refresh -- must never touch the
    network) -- the plain-text table's price columns must show a real
    dollar figure, not blank, for a models.json entry with string prices."""
    import contextlib
    import io
    from rolo_claude.catalog_cli import cmd_models
    from rolo_claude.providers.databricks import write_models_json

    with _Env() as env:
        write_models_json(env.state_dir, [{
            "id": "vendor/model-x", "context_length": 128000, "max_output_tokens": 8192,
            "pricing": {"prompt": "0.0000008", "completion": "0.0000024"},
        }])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cmd_models([])
        ctx.check(f"exits 0, got {code}", code == 0)
        out = buf.getvalue()
        line = next((ln for ln in out.splitlines() if "vendor/model-x" in ln), "")
        ctx.check(f"model row printed at all, got output={out!r}", line != "")
        ctx.check(f"price column is a real dollar figure, not blank, got line={line!r}", "$0.80/M" in line)
        ctx.check(f"completion price column also real, got line={line!r}", "$2.40/M" in line)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
