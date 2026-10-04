"""tests.test_ollama_cli -- Halo 2.0.3 round 3: `halo ollama [--host NAME]
[--refresh]` end to end through the real `python -m halo_harness ollama`
entry point (runner style, same subprocess-per-test shape tests/
test_providers_ollama_session.py uses) -- `OLLAMA_HOST` always points at
either a `mock_ollama.MockUpstream` or a dead port, NEVER the real local
daemon that may be running on this box's default 127.0.0.1:11434.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_ollama import SCENARIOS, MockUpstream, partial_offload_ps_entry

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _hermetic_child_env() -> dict:
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    env.pop("OLLAMA_API_KEY", None)
    return env


def _run_ollama_cli(fh, ollama_host: str, extra_args=None, timeout=30):
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_TEST_NO_BACKGROUND_NET": "1",
                "OLLAMA_HOST": ollama_host, "PYTHONPATH": str(REPO_DIR)})
    args = [sys.executable, "-m", "halo_harness", "ollama"] + (extra_args or [])
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


@test
def test_halo_ollama_cli_reports_reachable_version_and_offload(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream(ps_response={"models": [
        partial_offload_ps_entry("qwen3:30b", size=20 * 1024**3, size_vram=12 * 1024**3, context_length=40960),
    ]}, version_response={"version": "9.9.9-mock"}).start()
    try:
        result = _run_ollama_cli(fh, mock.base_url)
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr!r}", result.returncode == 0)
        ctx.check("version reached stdout", "9.9.9-mock" in result.stdout)
        ctx.check("the offload sentence reached stdout", "partially offloaded" in result.stdout)
        ctx.check("the KV formula origin note reached stdout", "GGML/llama.cpp" in result.stdout)
    finally:
        mock.stop()


@test
def test_halo_ollama_cli_unreachable_host(ctx: Ctx):
    fh = build_fake_home()
    result = _run_ollama_cli(fh, "http://127.0.0.1:1")  # nothing listens on port 1
    ctx.check(f"exit 0 even when unreachable (a status report, not a failure), got {result.returncode}",
              result.returncode == 0)
    ctx.check("reports unreachable", "unreachable" in result.stdout)


@test
def test_halo_ollama_cli_unknown_host_flag_is_a_clean_error(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_ollama_cli(fh, mock.base_url, extra_args=["--host", "nope"])
        ctx.check(f"exit 1, got {result.returncode}", result.returncode == 1)
        ctx.check("names the unknown host", "nope" in result.stderr)
    finally:
        mock.stop()


@test
def test_halo_ollama_cli_refresh_flag_parses(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_ollama_cli(fh, mock.base_url, extra_args=["--refresh"])
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr!r}", result.returncode == 0)
        ctx.check("still reports the host", "Ollama host" in result.stdout)
    finally:
        mock.stop()


# ---- round 5b: `halo ollama calibrate` --------------------------------

def _make_fits_scenario(mock: MockUpstream, model: str):
    def scn(h, body):
        mock.ps_response = {"models": [{"model": model, "name": model, "size": 1024, "size_vram": 1024}]}
        SCENARIOS["done-reason-stop"](h, body)
    return scn


@test
def test_halo_ollama_calibrate_fits_prints_the_cap_and_records_it(ctx: Ctx):
    from halo_harness.providers.ollama_calibrate import lookup_learned_cap
    fh = build_fake_home()
    mock = MockUpstream().start()  # qwen3:30b is in DEFAULT_TAGS/DEFAULT_SHOW
    try:
        mock.scenarios["qwen3:30b"] = _make_fits_scenario(mock, "qwen3:30b")
        # Round 5b part 2: this scenario reports "fully resident" at EVERY
        # candidate, so the default step-UP phase (now on by default for
        # this CLI command, see tests/test_ollama_calibrate_step_up_5b2.py
        # for its own dedicated coverage) would keep climbing; --no-up
        # keeps this test's own scope narrow (find-and-record one fit).
        result = _run_ollama_cli(fh, mock.base_url,
                                  extra_args=["calibrate", "qwen3:30b", "--start", "8192", "--no-up"])
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr!r}", result.returncode == 0)
        ctx.check(f"reports fits + the num_ctx, got {result.stdout!r}",
                  "fits fully in GPU memory at num_ctx=8192" in result.stdout)
        state_dir = fh["home"] / ".halo"
        got = lookup_learned_cap(state_dir, host_url=mock.base_url, model="qwen3:30b")
        ctx.check(f"recorded to the store, got {got}", got == 8192)
    finally:
        mock.stop()


@test
def test_halo_ollama_calibrate_unknown_model_is_a_clean_error(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_ollama_cli(fh, mock.base_url, extra_args=["calibrate", "not-in-the-catalog:1b"])
        ctx.check(f"exit 1, got {result.returncode}", result.returncode == 1)
        ctx.check("names the model", "not-in-the-catalog:1b" in result.stderr)
    finally:
        mock.stop()


@test
def test_halo_ollama_calibrate_unknown_host_is_a_clean_error(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_ollama_cli(fh, mock.base_url, extra_args=["calibrate", "qwen3:30b", "--host", "nope"])
        ctx.check(f"exit 1, got {result.returncode}", result.returncode == 1)
        ctx.check("names the unknown host", "nope" in result.stderr)
    finally:
        mock.stop()


@test
def test_halo_ollama_calibrate_bare_subcommand_never_confused_with_flags(ctx: Ctx):
    """"calibrate" as argv[0] always dispatches to the subcommand, never
    mistaken for a --host/--refresh flag combination on the bare `halo
    ollama` form (and vice versa -- the bare form still works)."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        bare = _run_ollama_cli(fh, mock.base_url)
        ctx.check(f"bare form still works, got {bare.returncode}", bare.returncode == 0)
        ctx.check("bare form prints host analysis, not calibrate usage", "Ollama host" in bare.stdout)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
