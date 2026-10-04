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
from tests.helpers.mock_ollama import MockUpstream, partial_offload_ps_entry

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


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
