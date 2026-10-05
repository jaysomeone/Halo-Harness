"""tests.test_ollama_throughput_ui -- Halo 2.0.3 round 5b item 7: the
status bar's `ol:`-only throughput chip (`model_display.format_ollama_
throughput`, `events.status`'s three new fields) and the end-to-end path
from a real `/api/chat` turn's timing fields to `~/.halo/ollama-fit.json`'s
persisted "last turn" record `halo ollama` reads back -- same real-
Session-via-subprocess pattern `tests/test_providers_ollama_session.py`
already uses (`MockUpstream` standing in for the daemon).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.provider_env_defaults import ensure_default_provider_credentials
ensure_default_provider_credentials()

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_ollama import MockUpstream, end_ndjson, partial_offload_ps_entry, start_ndjson, \
    write_ndjson_line

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


# ---- format_ollama_throughput (pure) --------------------------------------

@test
def test_format_ollama_throughput_both_known(ctx: Ctx):
    from halo_harness.model_display import format_ollama_throughput
    got = format_ollama_throughput(41.0, 1.2, False)
    ctx.check(f"compact 'N tok/s · prefill N.N s', got {got!r}", got == "41 tok/s · prefill 1.2 s")


@test
def test_format_ollama_throughput_offloaded_marker(ctx: Ctx):
    from halo_harness.model_display import format_ollama_throughput
    got = format_ollama_throughput(12.0, 3.4, True)
    ctx.check(f"'· offloaded' appended, got {got!r}", got == "12 tok/s · prefill 3.4 s · offloaded")


@test
def test_format_ollama_throughput_blank_when_nothing_known(ctx: Ctx):
    from halo_harness.model_display import format_ollama_throughput
    ctx.check("blank, not a placeholder, same convention as every other optional segment",
              format_ollama_throughput(None, None, None) == "")


@test
def test_format_ollama_throughput_one_figure_only(ctx: Ctx):
    from halo_harness.model_display import format_ollama_throughput
    ctx.check("tok/s alone", format_ollama_throughput(41.0, None, False) == "41 tok/s")
    ctx.check("prefill alone", format_ollama_throughput(None, 1.2, False) == "prefill 1.2 s")


# ---- events.status's three new fields --------------------------------

@test
def test_events_status_ollama_fields_default_to_none(ctx: Ctx):
    from halo_harness import events
    ev = events.status(phase="idle")
    ctx.check("ollama_tokens_per_second defaults to None", ev.data["ollama_tokens_per_second"] is None)
    ctx.check("ollama_prefill_seconds defaults to None", ev.data["ollama_prefill_seconds"] is None)
    ctx.check("ollama_offloaded defaults to None", ev.data["ollama_offloaded"] is None)


@test
def test_events_status_ollama_fields_pass_through(ctx: Ctx):
    from halo_harness import events
    ev = events.status(phase="idle", ollama_tokens_per_second=41.0, ollama_prefill_seconds=1.2,
                        ollama_offloaded=True)
    ctx.check("passed through unchanged", ev.data["ollama_tokens_per_second"] == 41.0
              and ev.data["ollama_prefill_seconds"] == 1.2 and ev.data["ollama_offloaded"] is True)


# ---- end-to-end: a real turn's timing -> ~/.halo/ollama-fit.json ----------

def _hermetic_child_env() -> dict:
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    env.pop("OLLAMA_API_KEY", None)
    return env


def _run_cli(fh, mock, prompt, *, model: str, timeout=30):
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_TEST_NO_BACKGROUND_NET": "1",
                "OLLAMA_HOST": mock.base_url, "PYTHONPATH": str(REPO_DIR)})
    args = [sys.executable, "-m", "halo_harness", "-p", prompt, "--model", model, "--cwd", str(fh["proj"])]
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


def _scn_timed(handler, body) -> None:
    """50 eval tokens over 1.0s (50 tok/s), 0.5s prefill -- round numbers
    chosen so the expected arithmetic is exact, never a floating rounding
    question."""
    start_ndjson(handler)
    write_ndjson_line(handler, {
        "message": {"role": "assistant", "content": "hi"}, "done": True, "done_reason": "stop",
        "prompt_eval_count": 100, "prompt_eval_duration": 500_000_000,
        "eval_count": 50, "eval_duration": 1_000_000_000,
    })
    end_ndjson(handler)


@test
def test_real_turn_persists_throughput_for_halo_ollama_to_read(ctx: Ctx):
    from halo_harness.providers.ollama_calibrate import get_last_turn_throughput
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        mock.scenarios["throughput-model"] = _scn_timed
        result = _run_cli(fh, mock, "hi", model="ol:throughput-model")
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-500:]!r}", result.returncode == 0)
        state_dir = fh["home"] / ".halo"
        got = get_last_turn_throughput(state_dir, host_url=mock.base_url, model="throughput-model")
        ctx.check(f"persisted a record, got {got!r}", got is not None)
        ctx.check(f"tokens_per_second == 50.0 (50 tokens / 1.0 s), got {got.get('tokens_per_second')}",
                  got.get("tokens_per_second") == 50.0)
        ctx.check(f"prefill_seconds == 0.5, got {got.get('prefill_seconds')}", got.get("prefill_seconds") == 0.5)
    finally:
        mock.stop()


@test
def test_real_turn_persists_offloaded_marker(ctx: Ctx):
    """`/api/ps` reports this model partially offloaded -- the SAME
    `/api/ps` read `estimate_fit_for_host` already makes for the fit
    estimate, never a second probe -- and that reading reaches the
    persisted record's own `offloaded` field. Needs "throughput-model"
    to actually be IN the mock's catalog (unlike the simpler throughput-
    only test above): `estimate_fit_for_host`/`_record_last_known_offload`
    both return before ever reading `/api/ps` when `catalog_row` finds
    nothing, by design (no fit computation is attempted for a model
    nobody has even pulled)."""
    from halo_harness.providers.ollama_calibrate import get_last_turn_throughput
    tags_response = {"models": [
        {"name": "throughput-model", "model": "throughput-model", "modified_at": "2026-01-01T00:00:00Z",
         "size": 123, "digest": "sha256:deadbeef9",
         "details": {"family": "qwen3", "families": ["qwen3"], "parameter_size": "1B",
                     "quantization_level": "Q4_0", "format": "gguf"}},
    ]}
    show_responses = {"throughput-model": {"modelfile": "", "parameters": "", "template": "",
                                            "capabilities": ["tools"],
                                            "details": {"family": "qwen3", "quantization_level": "Q4_0"},
                                            "model_info": {"qwen3.context_length": 8192}}}
    fh = build_fake_home()
    mock = MockUpstream(tags_response=tags_response, show_responses=show_responses, ps_response={"models": [
        partial_offload_ps_entry("throughput-model", size=20 * 1024**3, size_vram=12 * 1024**3,
                                  context_length=8192),
    ]}).start()
    try:
        mock.scenarios["throughput-model"] = _scn_timed
        result = _run_cli(fh, mock, "hi", model="ol:throughput-model")
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-500:]!r}", result.returncode == 0)
        state_dir = fh["home"] / ".halo"
        got = get_last_turn_throughput(state_dir, host_url=mock.base_url, model="throughput-model")
        ctx.check(f"offloaded marker is True, got {got!r}", got is not None and got.get("offloaded") is True)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
