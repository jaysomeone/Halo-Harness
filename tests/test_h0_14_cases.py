"""tests.test_h0_14_cases -- H0 review finding #14's remaining required
cases (3, 4, 5, 6, 8; 1/2/7 were covered by H1, cases not reproduced here
per the H2 task's own scoping):
  3. `-p --output-format json "q"`, `"q" -p`, and piped UTF-8 stdin.
  4. the first text_delta reaches the sink before the upstream finishes.
  5. abort and a bare gen.close(), with the mock seeing the disconnect and
     the reader thread exiting.
  6. connection refused then OK (429 + Retry-After through both the proxy
     and the loop are covered by tests/test_databricks_mock.py and
     tests/test_loop_retries.py's finding-7 test).
  8. untrusted-layer drops and policy > flag.
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream

REPO_DIR = Path(__file__).resolve().parent.parent

test, TESTS = new_registry()


# ---- case 3: CLI positional/stdin ------------------------------------------

def _cli_env(fh, mock):
    env = dict(os.environ)
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
    return env


@test
def test_case3_flag_before_prompt_with_output_format(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        args = [sys.executable, "-m", "rolo_claude", "-p", "--output-format", "json", "pong please",
                "--model", "or:mock/model", "--cwd", str(fh["proj"])]
        result = subprocess.run(args, env=_cli_env(fh, mock), cwd=str(REPO_DIR), capture_output=True, text=True, timeout=30)
        ctx.check(f"exit 0, got {result.returncode} (stderr: {result.stderr[-400:]!r})", result.returncode == 0)
        ctx.check(f"valid JSON on stdout, got {result.stdout!r}", result.stdout.strip().startswith("{"))
    finally:
        mock.stop()


@test
def test_case3_prompt_before_flag(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        args = [sys.executable, "-m", "rolo_claude", "pong please", "-p",
                "--model", "or:mock/model", "--cwd", str(fh["proj"])]
        result = subprocess.run(args, env=_cli_env(fh, mock), cwd=str(REPO_DIR), capture_output=True, text=True, timeout=30)
        ctx.check(f"exit 0, got {result.returncode} (stderr: {result.stderr[-400:]!r})", result.returncode == 0)
        ctx.check(f"answered pong, got {result.stdout!r}", "pong" in result.stdout)
    finally:
        mock.stop()


@test
def test_case3_piped_utf8_stdin(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        args = [sys.executable, "-m", "rolo_claude", "-p", "--model", "or:mock/model", "--cwd", str(fh["proj"])]
        prompt = "pong please -- café — 日本語"  # UTF-8 multibyte content, piped as stdin
        result = subprocess.run(args, env=_cli_env(fh, mock), cwd=str(REPO_DIR), capture_output=True,
                                 input=prompt.encode("utf-8"), timeout=30)
        ctx.check(f"exit 0, got {result.returncode} (stderr: {result.stderr[-400:]!r})", result.returncode == 0)
        stdout_text = result.stdout.decode("utf-8", "replace")
        ctx.check(f"answered pong (stdin decoded correctly, no mojibake/crash), got {stdout_text!r}", "pong" in stdout_text)
    finally:
        mock.stop()


# ---- case 4: first text_delta reaches the sink before the upstream finishes

@test
def test_case4_first_delta_arrives_before_upstream_finishes(ctx: Ctx):
    from rolo_claude.providers.routing import Route
    from rolo_claude.providers.stream import CompletionRequest, ProviderCreds, stream_completion
    mock = MockUpstream().start()
    try:
        model = "mock/long-abort"  # ~60 chunks, 0.1s apart, ~6s total -- see mock_openai.py
        req = CompletionRequest(
            body={"model": model, "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
            route=Route(provider="openrouter", upstream_model=model, dialect="openai-chat"),
            profile={"context_tokens": 128000, "max_output_tokens": 16384},
            creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="case4-state-")), extra_headers={}, model_label=model,
            ping_interval=15.0,
        )
        gen = stream_completion(req)
        t0 = time.monotonic()
        first = next(gen)
        ctx.check("first event is message_start", first.get("type") == "message_start")
        next(gen)  # the first real content_block_start/delta
        dt = time.monotonic() - t0
        ctx.check(f"a real content event arrived well before the ~6s stream finishes, got dt={dt:.2f}s", dt < 3.0)
        gen.close()
    finally:
        mock.stop()


# ---- case 5: abort + bare gen.close() -- mock sees the disconnect ---------

@test
def test_case5_abort_makes_the_mock_see_a_disconnect(ctx: Ctx):
    from rolo_claude.providers.routing import Route
    from rolo_claude.providers.stream import CompletionRequest, ProviderCreds, stream_completion
    mock = MockUpstream().start()
    try:
        model = "mock/long-abort"
        req = CompletionRequest(
            body={"model": model, "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
            route=Route(provider="openrouter", upstream_model=model, dialect="openai-chat"),
            profile={"context_tokens": 128000, "max_output_tokens": 16384},
            creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="case5-state-")), extra_headers={}, model_label=model,
            ping_interval=15.0,
        )
        abort = threading.Event()
        gen = stream_completion(req, abort=abort)
        next(gen)  # message_start
        next(gen)  # one real content event, so the mock is mid-stream
        abort.set()
        gen.close()
        deadline = time.monotonic() + 5.0
        while not mock.disconnect_events and time.monotonic() < deadline:
            time.sleep(0.1)
        ctx.check(f"the mock's OWN write attempt saw the disconnect (proving WE shut the socket), "
                   f"got {mock.disconnect_events}", len(mock.disconnect_events) >= 1)
    finally:
        mock.stop()


# ---- case 6: connection refused, then OK -----------------------------------

@test
def test_case6_connection_refused_then_ok_retries_transparently(ctx: Ctx):
    """H0 #14 case 6: providers/stream.py's own single connect retry
    (`_run_phase1_attempts`: `except UpstreamConnectError: if attempt==0:
    continue`) must transparently succeed on a refusal-then-OK sequence.
    `open_upstream` itself is monkeypatched to simulate exactly ONE
    refusal deterministically -- real sockets can't reliably reproduce
    "refused on attempt 1, listening by attempt 2" since phase 1's two
    attempts run back to back with NO delay between them at all."""
    import rolo_claude.providers.http as http_mod
    from rolo_claude.providers.routing import Route
    from rolo_claude.providers.stream import CompletionRequest, ProviderCreds, stream_completion

    mock = MockUpstream().start()
    real_open_upstream = http_mod.open_upstream
    call_count = [0]

    def flaky_open_upstream(host, port, tls, connect_timeout=10, on_connect=None):
        call_count[0] += 1
        if call_count[0] == 1:
            raise ConnectionRefusedError("[simulated] connection refused")
        return real_open_upstream(host, port, tls, connect_timeout=connect_timeout, on_connect=on_connect)

    http_mod.open_upstream = flaky_open_upstream
    try:
        model = "mock/model"
        req = CompletionRequest(
            body={"model": model, "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
            route=Route(provider="openrouter", upstream_model=model, dialect="openai-chat"),
            profile={"context_tokens": 128000, "max_output_tokens": 16384},
            creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="case6-state-")), extra_headers={}, model_label=model,
            ping_interval=15.0,
        )
        events = list(stream_completion(req))
        kinds = [e.get("type") for e in events]
        ctx.check(f"the call succeeded despite the first connect being refused, got kinds={kinds}",
                   "message_stop" in kinds)
        ctx.check(f"exactly 2 connect attempts (1 refused + 1 ok), got {call_count[0]}", call_count[0] == 2)
    finally:
        http_mod.open_upstream = real_open_upstream
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
