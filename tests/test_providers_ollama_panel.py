"""tests.test_providers_ollama_panel -- Halo 2.0.3 round 3: the offload
sentence wording, `providers.ollama_panel.analyze_host`/`format_host_
analysis` against a `mock_ollama.MockUpstream` (never a real daemon --
every test here scopes `ollama.hosts` to its OWN mock, and every GPU read
is injected via `hw_runner`), and `halo doctor`'s Ollama section.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.provider_env_defaults import ensure_default_provider_credentials
ensure_default_provider_credentials()

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_ollama import MockUpstream, multi_host_config, partial_offload_ps_entry

test, TESTS = new_registry()

_NO_GPU_RUNNER = lambda argv, timeout: None  # never a real OS tool in this file


def _fresh_state_dir(prefix: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    return d


def _clear_state_dir_env() -> None:
    os.environ.pop("BRIDGE_STATE_DIR", None)


# ---- offload sentence (pure) --------------------------------------------

@test
def test_offload_sentence_fully_loaded(ctx: Ctx):
    from halo_harness.providers.ollama_panel import offload_sentence
    got = offload_sentence("m", 1000, 1000)
    ctx.check(f"fully loaded wording, got {got!r}", got == "m is fully loaded in GPU memory.")


@test
def test_offload_sentence_partial_wording(ctx: Ctx):
    from halo_harness.providers.ollama_panel import offload_sentence
    got = offload_sentence("m", 10 * 1024**3, 6 * 1024**3)
    expected = "m is partially offloaded: 6.0 GB of 10.0 GB in GPU memory (60%), the rest in system RAM (slower)."
    ctx.check(f"partial offload wording, got {got!r}", got == expected)


@test
def test_offload_sentence_unknown(ctx: Ctx):
    from halo_harness.providers.ollama_panel import offload_sentence
    got = offload_sentence("m", None, None)
    ctx.check(f"unknown wording, got {got!r}", got == "m: offload unknown (host did not report size/size_vram).")


# ---- analyze_host / format_host_analysis against a mock -----------------

@test
def test_analyze_host_partial_offload_and_context(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_panel import analyze_host, format_host_analysis
    mock = MockUpstream(ps_response={"models": [
        partial_offload_ps_entry("qwen3:30b", size=20 * 1024**3, size_vram=12 * 1024**3, context_length=40960),
    ]})
    mock.start()
    try:
        host = OllamaHost(name="mock", url=mock.base_url)
        analysis = analyze_host(host, hw_runner=_NO_GPU_RUNNER)
        ctx.check("reachable", analysis.reachable is True)
        ctx.check("local host (loopback)", analysis.is_local is True)
        ctx.check("gpu is None (injected runner reports nothing)", analysis.gpu is None)
        ctx.check(f"exactly one loaded model, got {analysis.loaded}", len(analysis.loaded) == 1)
        m = analysis.loaded[0]
        ctx.check("offload sentence mentions partial", "partially offloaded" in m.offload_sentence)
        ctx.check(f"effective context is /api/ps's own context_length, got {m.effective_context}",
                  m.effective_context == 40960)
        ctx.check(f"trained context from the catalog's model_info, got {m.trained_context}",
                  m.trained_context == 40960)  # DEFAULT_SHOW's own qwen3:30b row
        text = format_host_analysis(analysis)
        ctx.check("formatted text carries the KV formula origin note",
                  "standard GGML/llama.cpp accounting" in text)
    finally:
        mock.stop()


@test
def test_analyze_host_unreachable_host(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_panel import analyze_host, format_host_analysis
    host = OllamaHost(name="dead", url="http://127.0.0.1:1")  # nothing listens on port 1
    analysis = analyze_host(host, hw_runner=_NO_GPU_RUNNER)
    ctx.check("not reachable", analysis.reachable is False)
    ctx.check("formatted text says unreachable", "unreachable" in format_host_analysis(analysis))


@test
def test_multi_host_config_resolves_both_by_name(ctx: Ctx):
    from halo_harness.providers.ollama import resolve_ollama_hosts
    from halo_harness.theme import set_config_value
    _fresh_state_dir("ol-panel-multihost-")
    mock_a, mock_b = MockUpstream().start(), MockUpstream().start()
    try:
        set_config_value("ollama.hosts", multi_host_config({"a": mock_a, "b": mock_b}, default="b"))
        hosts = resolve_ollama_hosts()
        by_name = {h.name: h for h in hosts}
        ctx.check(f"both configured hosts present, got {sorted(by_name)}", set(by_name) == {"a", "b"})
        ctx.check("a's url matches mock_a", by_name["a"].url == mock_a.base_url)
        ctx.check("b's url matches mock_b", by_name["b"].url == mock_b.base_url)
        ctx.check("b is the configured default", by_name["b"].default is True and by_name["a"].default is False)
    finally:
        mock_a.stop()
        mock_b.stop()
        _clear_state_dir_env()


# ---- halo doctor's Ollama section ----------------------------------------

@test
def test_doctor_ollama_section_one_line_per_host(ctx: Ctx):
    from halo_harness.doctor import _check_ollama_hosts
    from halo_harness.theme import set_config_value
    _fresh_state_dir("ol-panel-doctor-")
    mock = MockUpstream().start()
    try:
        set_config_value("ollama.hosts", multi_host_config({"mock": mock}))
        entries = _check_ollama_hosts()
        ctx.check(f"exactly one doctor entry for the one configured host, got {entries}", len(entries) == 1)
        cid, line = entries[0]
        ctx.check("id names the host", cid == "ollama_host_mock")
        ctx.check(f"line reports reachable + version, got {line!r}", "reachable" in line and "version" in line)
        ctx.check(f"line reports a loaded-model count, got {line!r}", "model(s) loaded" in line)
    finally:
        mock.stop()
        _clear_state_dir_env()


@test
def test_doctor_ollama_section_unreachable_host(ctx: Ctx):
    from halo_harness.doctor import _check_ollama_hosts
    from halo_harness.theme import set_config_value
    _fresh_state_dir("ol-panel-doctor-dead-")
    try:
        set_config_value("ollama.hosts", [{"name": "dead", "url": "http://127.0.0.1:1", "default": True}])
        entries = _check_ollama_hosts()
        ctx.check(f"one entry, got {entries}", len(entries) == 1)
        ctx.check(f"reports not reachable, got {entries[0][1]!r}", "not reachable" in entries[0][1])
    finally:
        _clear_state_dir_env()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
