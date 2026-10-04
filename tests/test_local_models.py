"""tests.test_local_models -- Halo 2.0.3 round 5 (brief item 4): the
shared `/local` merged view -- source order and group labels, the manual
Hugging Face entry's bare-vs-refresh behaviour, auto-detected-server
addressability, and `halo local`'s own CLI output. Every test here scopes
`OLLAMA_HOST`/`HF_HUB_CACHE`/`BRIDGE_STATE_DIR` explicitly -- this view
otherwise reads the REAL local Ollama daemon and the REAL `~/.cache/
huggingface/hub` (confirmed live against this repo's own build host while
writing this file: an unscoped call found a real Ollama catalog AND a
real Hugging Face Hub cache entry), which must never happen from a test.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.mock_ollama import MockUpstream as MockOllama
from tests.helpers.mock_openai import MockUpstream as MockOpenAI
from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_ENV_NAMES = ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "OLLAMA_HOST", "OLLAMA_API_KEY",
              "HF_HUB_CACHE", "HF_HOME", "HF_LOCAL_PROBE_PORTS", "BRIDGE_TEST_NO_BACKGROUND_NET")


class _Env:
    """Scopes every env var `providers.local_models.build_local_view`
    touches -- see this module's own docstring for why that matters here
    specifically. `no_background_net` (default True): most of these tests
    don't exercise auto-detection at all, so the gate stays ON by default
    (never a real, even if harmless, loopback probe sweep); the one test
    that DOES test auto-detection passes `False`."""

    def __init__(self, *, no_background_net: bool = True):
        self._no_background_net = no_background_net

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_NAMES}
        d = Path(tempfile.mkdtemp(prefix="local-view-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["OLLAMA_HOST"] = "http://127.0.0.1:1"  # unreachable unless a test overrides it
        os.environ.pop("OLLAMA_API_KEY", None)
        os.environ["HF_HUB_CACHE"] = str(d / "empty-hf-cache")  # does not exist -> scan returns []
        os.environ.pop("HF_HOME", None)
        os.environ.pop("HF_LOCAL_PROBE_PORTS", None)
        if self._no_background_net:
            os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = "1"
        else:
            os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        self.home = d
        self.state_dir = d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _set_ollama_host(name: str, url: str, *, default: bool = True) -> None:
    from halo_harness.theme import set_config_value
    set_config_value("ollama.hosts", [{"name": name, "url": url, "default": default}])


def _set_hf_local_server(name: str, url: str, *, api_key=None, default: bool = True) -> None:
    from halo_harness.theme import set_config_value
    entry = {"name": name, "url": url, "default": default}
    if api_key:
        entry["api_key"] = api_key
    set_config_value("huggingface.local_servers", [entry])


@test
def test_everything_empty_reports_nothing_found(ctx: Ctx):
    from halo_harness.providers.local_models import build_local_view, format_local_view
    with _Env() as e:
        rows = build_local_view(env=dict(os.environ), state_dir=e.state_dir)
        ctx.check(f"only the unreachable synthesized Ollama default host, got {rows}", len(rows) == 1)
        ctx.check("it's reported unreachable", rows[0].reachable is False)
        text = format_local_view(rows)
        ctx.check(f"mentions Ollama, got {text!r}", "Ollama" in text and "unreachable" in text)


@test
def test_source_order_and_group_labels(ctx: Ctx):
    from halo_harness.providers.local_models import build_local_view
    with _Env() as e:
        ollama = MockOllama().start()
        try:
            _set_ollama_host("bench", ollama.base_url)
            _set_hf_local_server("my-server", "http://127.0.0.1:9/v1")  # unreachable -- never probed (bare, no refresh)
            # `build_local_view` imports `scan_hub_cache` deferred, INSIDE
            # its own body (`from ...huggingface_hub_cache import
            # scan_hub_cache`) -- patching that SOURCE module's attribute
            # (never a `providers.local_models` one, which has no such
            # name bound at module scope at all) is what a deferred import
            # actually re-resolves on each call.
            import halo_harness.providers.huggingface_hub_cache as hub_cache_mod
            from halo_harness.providers.huggingface_hub_cache import HubCacheModel
            real_scan = hub_cache_mod.scan_hub_cache
            hub_cache_mod.scan_hub_cache = lambda env=None: [HubCacheModel(
                repo_id="org/cached-model", dirname="models--org--cached-model",
                size_bytes=123, formats=("safetensors",))]
            try:
                rows = build_local_view(env=dict(os.environ), state_dir=e.state_dir)
            finally:
                hub_cache_mod.scan_hub_cache = real_scan
            groups_in_order = list(dict.fromkeys(r.group for r in rows))
            ctx.check(f"Ollama group first, got {groups_in_order}", groups_in_order[0] == "Ollama (bench)")
            ctx.check(f"Hugging Face local-server group second, got {groups_in_order}",
                      groups_in_order[1] == "Hugging Face (local: my-server)")
            ctx.check(f"Hugging Face cache group last, got {groups_in_order}",
                      groups_in_order[2] == "Hugging Face (cache, not served)")
            ollama_rows = [r for r in rows if r.group == "Ollama (bench)"]
            ctx.check(f"both default-catalog models present, got {[r.name for r in ollama_rows]}",
                      {"qwen3:30b", "gpt-oss:20b"} <= {r.name for r in ollama_rows})
            manual_rows = [r for r in rows if r.group == "Hugging Face (local: my-server)"]
            ctx.check(f"a bare (non-refresh) manual entry is reported as not-yet-probed, got {manual_rows}",
                      manual_rows[0].reachable is None and "refresh" in manual_rows[0].name)
            cache_rows = [r for r in rows if r.group == "Hugging Face (cache, not served)"]
            ctx.check(f"the cache-only model is listed, got {cache_rows}", "org/cached-model" in cache_rows[0].name)
        finally:
            ollama.stop()


@test
def test_refresh_probes_the_manual_entry_for_real(ctx: Ctx):
    from halo_harness.providers.local_models import build_local_view
    with _Env() as e:
        server_mock = MockOpenAI(path_prefix="/v1", expected_bearer="probe-me-token").start()
        try:
            server_mock.models_response = {"data": [{"id": "my-model", "context_length": 4096}]}
            _set_hf_local_server("bench", server_mock.base_url, api_key="probe-me-token")
            bare_rows = build_local_view(env=dict(os.environ), state_dir=e.state_dir)
            manual_bare = [r for r in bare_rows if r.group == "Hugging Face (local: bench)"]
            ctx.check(f"bare: not probed, got {manual_bare}", manual_bare[0].reachable is None)

            fresh_rows = build_local_view(refresh=True, env=dict(os.environ), state_dir=e.state_dir)
            manual_fresh = [r for r in fresh_rows if r.group == "Hugging Face (local: bench)"]
            ctx.check(f"refresh: now reachable with the real model id, got {manual_fresh}",
                      manual_fresh[0].reachable is True and manual_fresh[0].name == "my-model")
            ctx.check(f"refresh: the ref is addressable by name, got {manual_fresh[0].ref!r}",
                      manual_fresh[0].ref == "hf:local/my-model@bench")
            ctx.check(f"refresh: context read back, got {manual_fresh[0].context}", manual_fresh[0].context == 4096)
        finally:
            server_mock.stop()


@test
def test_only_the_first_auto_detected_server_is_addressable(ctx: Ctx):
    from halo_harness.providers.local_models import build_local_view
    with _Env(no_background_net=False) as e:
        mocks = [MockOpenAI(path_prefix="/v1").start() for _ in range(2)]
        try:
            mocks[0].models_response = {"data": [{"id": "model-one"}]}
            mocks[1].models_response = {"data": [{"id": "model-two"}]}
            ports = ",".join(str(m.port) for m in mocks)
            env = dict(os.environ)
            env["HF_LOCAL_PROBE_PORTS"] = ports
            rows = build_local_view(env=env, state_dir=e.state_dir)
            row_one = next(r for r in rows if r.name == "model-one")
            row_two = next(r for r in rows if r.name == "model-two")
            ctx.check(f"the FIRST auto-detected server's model is addressable, got {row_one.ref!r}",
                      row_one.ref == "hf:local/model-one")
            ctx.check(f"a SECOND auto-detected server's model is never addressable, got {row_two.ref!r}",
                      row_two.ref is None)
        finally:
            for m in mocks:
                m.stop()


@test
def test_halo_local_cli_prints_the_formatted_view(ctx: Ctx):
    import io
    import contextlib
    from halo_harness.local_cli import cmd_local
    with _Env():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_local([])
        ctx.check(f"exit 0, got {rc}", rc == 0)
        ctx.check(f"prints the Ollama group, got {buf.getvalue()!r}", "Ollama" in buf.getvalue())


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
