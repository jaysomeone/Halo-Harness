"""tests.test_hotfix_101_models_cache -- 1.0.1 hotfixes 3 and 4:

  (3) `/models` (bare, TUI or headless) and `halo models` (bare CLI)
      render the CACHED Databricks table instantly and never touch the
      network; only an explicit `refresh` (or `/dbx`) does, and a failed
      refresh still shows the cached table plus one line naming the error.
  (4) The cache carries the gateway types (api_types/task/...); the `path`
      column is always the family default from dbx_routing (never gated
      behind --urls), "chat-capable" counts `task == "llm/v1/chat"` (not a
      name-based family guess or `bool(api_types)`), and an old-shape cache
      (no api_types recorded at all) is migrated on next real-network use,
      else shown as "unknown (refresh needed)" rather than silently wrong.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_databricks import MockDatabricks

test, TESTS = new_registry()

_UNRESOLVABLE = "https://totally-unresolvable-host.invalid"


class _Env:
    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE",
                        "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                        "OPENROUTER_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")}
        d = Path(tempfile.mkdtemp(prefix="hotfix101-models-cache-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        os.environ.pop("OPENROUTER_API_KEY", None)
        self.state_dir = d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _raw(name, fm_name, api_types, task="llm/v1/chat", model_class=None):
    fm = {"name": fm_name, "api_types": api_types}
    if model_class:
        fm["model_class"] = model_class
    return {"name": name, "task": task, "foundation_model": fm}


# One endpoint per family (hotfix 4's own "mock listing" requirement).
_ONE_PER_FAMILY = [
    _raw("databricks-claude-opus-4-6", "claude-opus-4-6", ["mlflow/v1/chat/completions", "anthropic/v1/messages"]),
    _raw("databricks-glm-5-3", "glm-5-3", ["mlflow/v1/chat/completions", "anthropic/v1/messages"]),
    _raw("databricks-kimi-k3", "kimi-k3", ["mlflow/v1/chat/completions", "anthropic/v1/messages"]),
    _raw("databricks-deepseek-v4-1-flash", "deepseek-v4-1-flash", ["mlflow/v1/chat/completions"]),
    _raw("databricks-qwen35-122b-a10b", "system.ai.qwen35-122b-a10b", ["mlflow/v1/chat/completions"]),
    _raw("databricks-llama-4-maverick", "system.ai.llama-4-maverick", ["mlflow/v1/chat/completions"]),
    _raw("databricks-gemma-3", "gemma-3", ["mlflow/v1/chat/completions"]),
    _raw("databricks-gpt-oss-120b", "gpt-oss-120b", ["mlflow/v1/chat/completions"]),
    _raw("databricks-gpt-5", "gpt-5", ["mlflow/v1/chat/completions", "cursor/v1/chat/completions"]),
    _raw("databricks-gpt-5-5-pro", "gpt-5-5-pro", ["cursor/v1/chat/completions"]),
    _raw("databricks-grok-4-6", "grok-4-6", ["mlflow/v1/chat/completions"]),
    _raw("databricks-gemini-3-1-pro", "gemini-3-1-pro", ["mlflow/v1/chat/completions", "cursor/v1/chat/completions"]),
    _raw("us-anthropic-claude-3-5-sonnet-v2", "us-anthropic-claude-3-5-sonnet-v2", [], task="llm/v1/external/chat"),
    _raw("databricks-gte-large-en", "gte-large-en", ["mlflow/v1/embeddings"], task="llm/v1/embeddings"),
]


def _parsed_from_raw(entries):
    """Mimics probe_databricks_endpoints_full's own parse (name + fields,
    NEW shape -- api_types key always present)."""
    out = []
    for e in entries:
        fm = e.get("foundation_model") or {}
        out.append({"name": e["name"], "task": e.get("task"), "ready": None, "permission_level": None,
                    "endpoint_type": None, "ai_gateway_v2_supported": None,
                    "api_types": fm.get("api_types") or [], "foundation_model_name": fm.get("name"),
                    "model_class": fm.get("model_class")})
    return out


# ---------------------------------------------------------------------------
# Fix 4: chat-capable / path-type correctness against the one-per-family mock.
# ---------------------------------------------------------------------------

@test
def test_one_per_family_mock_chat_capable_and_path_type_per_row(ctx: Ctx):
    from halo_harness.catalog_cli import _dbx_rows
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    with _Env() as env:
        write_dbx_endpoints_json(env.state_dir, _parsed_from_raw(_ONE_PER_FAMILY))
        from halo_harness.providers.databricks import load_dbx_endpoints_json
        endpoints = load_dbx_endpoints_json(env.state_dir)
        rows = _dbx_rows(endpoints, "https://example.cloud.databricks.com", env.state_dir, urls=False)

        ctx.check(f"claude foundation is chat-capable, got {rows['databricks-claude-opus-4-6']}",
                  rows["databricks-claude-opus-4-6"]["chat"] is True)
        ctx.check(f"claude foundation path is anthropic, got {rows['databricks-claude-opus-4-6']['path_type']}",
                  rows["databricks-claude-opus-4-6"]["path_type"] == "anthropic")

        for name in ("databricks-glm-5-3", "databricks-kimi-k3", "databricks-deepseek-v4-1-flash",
                     "databricks-qwen35-122b-a10b", "databricks-llama-4-maverick", "databricks-gemma-3",
                     "databricks-gpt-oss-120b"):
            ctx.check(f"{name} chat-capable, got {rows[name]}", rows[name]["chat"] is True)
            ctx.check(f"{name} path is mlflow (never invocations -- api_types lists mlflow), "
                      f"got {rows[name]['path_type']}", rows[name]["path_type"] == "mlflow")

        ctx.check(f"gpt-5-5-pro has no mlflow chat -- path is cursor, got "
                  f"{rows['databricks-gpt-5-5-pro']['path_type']}",
                  rows["databricks-gpt-5-5-pro"]["path_type"] == "cursor")

        ctx.check(f"bedrock external is NOT chat-capable by task, got {rows['us-anthropic-claude-3-5-sonnet-v2']}",
                  rows["us-anthropic-claude-3-5-sonnet-v2"]["chat"] is False)
        ctx.check(f"embeddings endpoint not chat-capable, got {rows['databricks-gte-large-en']}",
                  rows["databricks-gte-large-en"]["chat"] is False)


@test
def test_chat_capable_column_and_init_summary_count_never_disagree(ctx: Ctx):
    """The exact bug reported: the table said chat=yes while init's own
    summary said 0 chat-capable -- both now key off the SAME
    dbx_routing.is_chat_task check."""
    from halo_harness.catalog_cli import _dbx_rows
    from halo_harness.providers.databricks import load_dbx_endpoints_json, write_dbx_endpoints_json
    from halo_harness.providers.dbx_routing import is_chat_task
    with _Env() as env:
        write_dbx_endpoints_json(env.state_dir, _parsed_from_raw(_ONE_PER_FAMILY))
        endpoints = load_dbx_endpoints_json(env.state_dir)
        rows = _dbx_rows(endpoints, "https://example.cloud.databricks.com", env.state_dir, urls=False)
        table_chat_count = sum(1 for r in rows.values() if r["chat"])
        summary_chat_count = sum(1 for e in endpoints.values() if is_chat_task(e.get("task")))
        ctx.check(f"table and summary agree on chat-capable count, got table={table_chat_count} "
                  f"summary={summary_chat_count}", table_chat_count == summary_chat_count)
        ctx.check(f"12 of 14 are chat-capable (external+embeddings excluded), got {table_chat_count}",
                  table_chat_count == 12)


# ---------------------------------------------------------------------------
# Fix 4: old-shape cache migration.
# ---------------------------------------------------------------------------

def _old_shape_entry(name: str) -> dict:
    """The PRE-hotfix-4 cache shape: no "api_types" key at all."""
    return {"name": name, "task": "llm/v1/chat", "ready": True, "permission_level": "CAN_QUERY"}


@test
def test_old_shape_cache_is_detected(ctx: Ctx):
    from halo_harness.providers.databricks import dbx_endpoints_cache_is_old_shape
    old = {"databricks-kimi-k3": _old_shape_entry("databricks-kimi-k3")}
    new = {"databricks-kimi-k3": {"task": "llm/v1/chat", "api_types": ["mlflow/v1/chat/completions"]}}
    ctx.check("an old-shape cache (no api_types key at all) is detected", dbx_endpoints_cache_is_old_shape(old))
    ctx.check("a new-shape cache is not old-shape", not dbx_endpoints_cache_is_old_shape(new))
    ctx.check("an empty cache is not 'old-shape' (nothing to migrate)", not dbx_endpoints_cache_is_old_shape({}))
    ctx.check("a real endpoint with a genuinely empty api_types list is still new-shape (key present)",
              not dbx_endpoints_cache_is_old_shape({"x": {"task": "llm/v1/chat", "api_types": []}}))


@test
def test_old_shape_cache_shows_unknown_not_a_wrong_invocations_path(ctx: Ctx):
    from halo_harness.catalog_cli import _dbx_rows
    with _Env() as env:
        endpoints = {"databricks-kimi-k3": _old_shape_entry("databricks-kimi-k3"),
                     "databricks-glm-5-3": _old_shape_entry("databricks-glm-5-3")}
        from halo_harness.providers.databricks import write_dbx_endpoints_json
        write_dbx_endpoints_json(env.state_dir, list(endpoints.values()))
        from halo_harness.providers.databricks import load_dbx_endpoints_json
        loaded = load_dbx_endpoints_json(env.state_dir)
        rows = _dbx_rows(loaded, "https://example.cloud.databricks.com", env.state_dir, urls=False)
        for name, r in rows.items():
            ctx.check(f"{name} shows 'unknown', never a guessed 'invocations', got {r['path_type']!r}",
                      r["path_type"] == "unknown")


@test
def test_old_shape_cache_display_label_says_refresh_needed(ctx: Ctx):
    from halo_harness.catalog_cli import format_dbx_table_lines, _dbx_rows
    with _Env() as env:
        from halo_harness.providers.databricks import load_dbx_endpoints_json, write_dbx_endpoints_json
        write_dbx_endpoints_json(env.state_dir, [_old_shape_entry("databricks-kimi-k3")])
        endpoints = load_dbx_endpoints_json(env.state_dir)
        rows = _dbx_rows(endpoints, "https://example.cloud.databricks.com", env.state_dir, urls=False)
        lines = format_dbx_table_lines(rows)
        text = "\n".join(lines)
        ctx.check(f"text table says 'unknown (refresh needed)', got {text!r}", "unknown (refresh needed)" in text)


@test
def test_refresh_if_stale_always_refreshes_an_old_shape_cache_regardless_of_age(ctx: Ctx):
    from halo_harness.providers.databricks import refresh_dbx_catalog_if_stale, write_dbx_endpoints_json
    mock = MockDatabricks().start()
    mock.set_endpoints_catalog(_ONE_PER_FAMILY[:2])
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            write_dbx_endpoints_json(env.state_dir, [_old_shape_entry("databricks-kimi-k3")])
            # Freshly written (age ~0s) -- an ordinary cache would NOT be
            # stale yet at max_age_hours=24, but an old-shape one always is.
            result = refresh_dbx_catalog_if_stale(env.state_dir, max_age_hours=24)
            ctx.check(f"old-shape cache is refreshed even though it's fresh by age, got {result}",
                      result is not None and result[0] is True)
    finally:
        mock.stop()


@test
def test_init_chat_capable_summary_uses_task_not_api_types_truthiness(ctx: Ctx):
    from rich.console import Console
    from halo_harness.init_cli import _print_work_catalog_summary
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    with _Env() as env:
        # Old-shape cache: task says chat, but api_types was never recorded
        # at all -- the OLD `bool(api_types)` count would say 0; the fixed
        # task-based count must say 2.
        write_dbx_endpoints_json(env.state_dir, [
            _old_shape_entry("databricks-kimi-k3"), _old_shape_entry("databricks-glm-5-3"),
        ])
        buf = io.StringIO()
        console = Console(file=buf, width=200)
        _print_work_catalog_summary(console, "dbx:databricks-kimi-k3")
        out = buf.getvalue()
        ctx.check(f"2 chat-capable despite an old-shape (api_types-less) cache, got {out!r}",
                  "2 chat-capable" in out)


# ---------------------------------------------------------------------------
# Fix 3: bare /models never touches the network; only refresh does; a failed
# refresh still shows the cached table plus one line naming the error.
# ---------------------------------------------------------------------------

@test
def test_cli_bare_models_never_touches_network_even_with_unresolvable_host(ctx: Ctx):
    from halo_harness.catalog_cli import cmd_models
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    with _Env() as env:
        os.environ["BRIDGE_DBX_BASE_URL"] = _UNRESOLVABLE
        os.environ["BRIDGE_DBX_TOKEN"] = "tok"
        write_dbx_endpoints_json(env.state_dir, _parsed_from_raw(_ONE_PER_FAMILY[:2]))
        buf = io.StringIO()
        t0 = time.monotonic()
        from contextlib import redirect_stdout
        with redirect_stdout(buf):
            rc = cmd_models([])
        elapsed = time.monotonic() - t0
        ctx.check(f"exits 0, got {rc}", rc == 0)
        ctx.check(f"returns near-instantly (no network attempted), got {elapsed:.2f}s", elapsed < 2.0)
        out = buf.getvalue()
        ctx.check(f"shows the cached endpoints, got {out!r}", "databricks-glm-5-3" in out)


@test
def test_headless_bare_models_never_touches_network_even_with_unresolvable_host(ctx: Ctx):
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_models
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    with _Env() as env:
        os.environ["BRIDGE_DBX_BASE_URL"] = _UNRESOLVABLE
        os.environ["BRIDGE_DBX_TOKEN"] = "tok"
        write_dbx_endpoints_json(env.state_dir, _parsed_from_raw(_ONE_PER_FAMILY[:2]))
        t0 = time.monotonic()
        out = _cmd_models("", HeadlessFacade(cwd=Path.cwd()))
        elapsed = time.monotonic() - t0
        ctx.check(f"returns near-instantly (no network attempted), got {elapsed:.2f}s", elapsed < 2.0)
        ctx.check(f"shows the cached table AND the count/age summary, got {out!r}",
                  "databricks-glm-5-3" in out and "2 Databricks endpoint(s) cached" in out)


@test
def test_headless_models_refresh_failure_shows_cached_table_plus_one_line_error(ctx: Ctx):
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_models
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    mock = MockDatabricks().start()
    mock.set_endpoints_error("403-ip")
    try:
        with _Env() as env:
            os.environ["BRIDGE_DBX_BASE_URL"] = mock.root
            os.environ["BRIDGE_DBX_TOKEN"] = "tok"
            write_dbx_endpoints_json(env.state_dir, _parsed_from_raw(_ONE_PER_FAMILY[:2]))
            out = _cmd_models("refresh", HeadlessFacade(cwd=Path.cwd()))
            ctx.check(f"still shows the cached table, got {out!r}", "databricks-glm-5-3" in out)
            ctx.check(f"names the failure in one line, got {out!r}", "Refresh failed" in out)
    finally:
        mock.stop()


class _FakeTranscript:
    def __init__(self):
        self.notes: list = []

    def add_note(self, text, **_kw):
        self.notes.append(text)


class _FakeAppForModelsWorker:
    def __init__(self, state_dir):
        self.controller = type("C", (), {"state_dir": state_dir})()
        self.transcript = _FakeTranscript()
        self.notifications: list = []

    def call_from_thread(self, fn, *a, **kw):
        fn(*a, **kw)

    def notify(self, text, **kw):
        self.notifications.append(text)


@test
def test_tui_models_worker_bare_never_calls_refresh_dbx_catalog(ctx: Ctx):
    """Direct monkeypatch proof for the TUI surface: `refresh_dbx_catalog`
    itself must never even be CALLED for a bare (non-refresh) `/models`
    (`_models_refresh_worker` imports it locally at call time, so patching
    the source module's attribute is what a real, unmocked call would see)."""
    import halo_harness.providers.databricks as dbx_mod
    import halo_harness.tui.slash as slash_mod
    from halo_harness.providers.databricks import write_dbx_endpoints_json

    with _Env() as env:
        os.environ["BRIDGE_DBX_BASE_URL"] = _UNRESOLVABLE
        os.environ["BRIDGE_DBX_TOKEN"] = "tok"
        write_dbx_endpoints_json(env.state_dir, _parsed_from_raw(_ONE_PER_FAMILY[:1]))

        called = []
        real = dbx_mod.refresh_dbx_catalog

        def _tripwire(*a, **kw):
            called.append(True)
            return real(*a, **kw)

        dbx_mod.refresh_dbx_catalog = _tripwire
        try:
            app = _FakeAppForModelsWorker(env.state_dir)
            slash_mod._models_refresh_worker(app, False)
            ctx.check("refresh_dbx_catalog was never called for a bare /models", called == [])
            ctx.check(f"a note was still posted showing the cache, got {app.transcript.notes}",
                      any("cached" in n for n in app.transcript.notes))
            ctx.check(f"the note includes the cached table, got {app.transcript.notes}",
                      any("databricks-claude-opus-4-6" in n for n in app.transcript.notes))
        finally:
            dbx_mod.refresh_dbx_catalog = real


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
