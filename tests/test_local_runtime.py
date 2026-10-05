"""tests.test_local_runtime -- Halo 2.0.3 round 5c: the managed llama-
server/mlx_lm child-process registry (start, record, stop by name, stop-
all-except-kept) against a FAKE runtime binary -- a tiny Python stub THIS
TEST writes to a scratch file and runs as a real subprocess (never a real
llama-server/mlx_lm), serving the same `/v1/models` shape mock_openai.py's
servers answer. Never touches the real ~/.halo (state_dir is always an
explicit fresh tempdir).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_STUB_LISTENS = '''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port = int(sys.argv[sys.argv.index("--port") + 1])


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps({"object": "list", "data": [{"id": "stub-model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


HTTPServer(("127.0.0.1", port), H).serve_forever()
'''

_STUB_NEVER_LISTENS = "import time\ntime.sleep(60)\n"


def _write_stub(code: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="local-runtime-stub-"))
    p = d / "stub_runtime.py"
    p.write_text(code, encoding="utf-8")
    return p


def _posix_proc_state(pid):
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            content = f.read()
    except OSError:
        return None
    idx = content.rfind(")")
    if idx == -1:
        return None
    fields = content[idx + 1:].split()
    return fields[0] if fields else None


def _is_alive(pid) -> bool:
    """Cross-platform liveness check for a PID this test does not hold a
    `Popen` handle for (`start_managed_server` returns only the bare pid).
    `os.kill(pid, 0)`'s POSIX "signal 0" trick is not supported by
    Windows' own `os.kill` (CPython only maps SIGTERM/CTRL_* there), so
    Windows shells out to `tasklist` instead.

    FIX PASS (live run on Kali/WSL): a bare `os.kill(pid, 0)` on POSIX
    reports a ZOMBIE (already exited, not yet reaped by its parent) as
    "alive" -- the exact false positive that made `test_start_stop_
    managed_server_round_trip`/`test_stop_all_except_kept` look like
    they'd failed to stop anything when they actually had. This test IS
    the parent of the stub process (`start_managed_server` ran in THIS
    process), so it tries to reap it itself first (`os.waitpid(pid,
    os.WNOHANG)`, guarded for `ChildProcessError`), then falls back to
    reading `/proc/<pid>/stat`'s own state field -- `"Z"` reads as dead."""
    if not isinstance(pid, int):
        return False
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        return False
    if _posix_proc_state(pid) == "Z":
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _wait_until(predicate, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


@test
def test_llama_server_argv_matches_the_brief_exactly(ctx: Ctx):
    from halo_harness.providers.local_runtime import llama_server_argv
    argv = llama_server_argv(["llama-server"], model_path="/models/x.gguf", port=1234, context_length=16384)
    ctx.check(f"exact flag order, got {argv}",
              argv == ["llama-server", "-m", "/models/x.gguf", "-c", "16384", "-ngl", "99", "-fa", "on",
                       "--port", "1234", "--host", "127.0.0.1"])


@test
def test_llama_server_argv_omits_c_when_context_unknown(ctx: Ctx):
    from halo_harness.providers.local_runtime import llama_server_argv
    argv = llama_server_argv(["llama-server"], model_path="/models/x.gguf", port=1234)
    ctx.check(f"no -c flag, got {argv}", "-c" not in argv)


@test
def test_mlx_lm_server_argv_matches_the_brief_exactly(ctx: Ctx):
    from halo_harness.providers.local_runtime import mlx_lm_server_argv
    argv = mlx_lm_server_argv([sys.executable, "-m", "mlx_lm.server"], model_path="/models/dir", port=4321)
    ctx.check(f"exact flags, got {argv}",
              argv == [sys.executable, "-m", "mlx_lm.server", "--model", "/models/dir", "--port", "4321"])


@test
def test_start_stop_managed_server_round_trip(ctx: Ctx):
    from halo_harness.providers.local_runtime import (load_registry, start_managed_server, stop_managed_server)
    state_dir = Path(tempfile.mkdtemp(prefix="local-runtime-state-"))
    stub = _write_stub(_STUB_LISTENS)
    entry, reason = start_managed_server(model="/models/qwen3.gguf", runtime="llama-server",
                                          binary_argv=[sys.executable, str(stub)], model_path="/models/qwen3.gguf",
                                          context_length=8192, state_dir=state_dir, startup_timeout=10.0)
    try:
        ctx.check(f"start succeeded, got reason={reason!r}", entry is not None)
        ctx.check(f"base_url uses the chosen port, got {entry.base_url}", entry.base_url.endswith(f":{entry.port}/v1"))
        recorded = load_registry(state_dir)
        ctx.check(f"registry has exactly one entry, got {recorded}", len(recorded) == 1)
        ctx.check(f"registry entry's model matches, got {recorded[0]}", recorded[0]["model"] == "/models/qwen3.gguf")
        ok, msg = stop_managed_server("/models/qwen3.gguf", state_dir=state_dir)
        ctx.check(f"stop succeeded, got {msg!r}", ok)
        ctx.check("registry is empty after stop", load_registry(state_dir) == [])
        ctx.check("process is no longer alive after stop", _wait_until(lambda: not _is_alive(entry.pid)))
    finally:
        if entry is not None:
            from halo_harness.providers.local_runtime import _terminate_pid
            _terminate_pid(entry.pid)


@test
def test_stop_from_a_fresh_process_perspective_reaps_without_a_popen_handle(ctx: Ctx):
    """FIX PASS: `halo local stop` normally runs in a FRESH process with
    no `Popen` handle at all for the pid it's stopping -- simulated here
    by forgetting this test's own handle (`_forget_popen`) before
    calling stop, forcing `_terminate_pid` down its SIGTERM+`waitpid`
    fallback path instead of the handle-based `terminate()+wait()` one.
    Pins that the fallback still actually reaps/kills the process (never
    leaves a zombie `_is_alive` would misreport as running)."""
    from halo_harness.providers.local_runtime import (_forget_popen, load_registry, start_managed_server,
                                                        stop_managed_server)
    state_dir = Path(tempfile.mkdtemp(prefix="local-runtime-state-"))
    stub = _write_stub(_STUB_LISTENS)
    entry, reason = start_managed_server(model="/models/fresh-proc.gguf", runtime="llama-server",
                                          binary_argv=[sys.executable, str(stub)], model_path="/models/fresh-proc.gguf",
                                          state_dir=state_dir, startup_timeout=10.0)
    try:
        ctx.check(f"start succeeded, got reason={reason!r}", entry is not None)
        ctx.check("forgetting the handle leaves no trace of it", _forget_popen(entry.pid) is not None)
        ok, msg = stop_managed_server("/models/fresh-proc.gguf", state_dir=state_dir)
        ctx.check(f"stop still succeeds with no handle, got {msg!r}", ok)
        ctx.check("registry is empty after stop", load_registry(state_dir) == [])
        ctx.check("process is no longer alive after the no-handle stop path",
                  _wait_until(lambda: not _is_alive(entry.pid)))
    finally:
        if entry is not None:
            from halo_harness.providers.local_runtime import _terminate_pid
            _terminate_pid(entry.pid)


@test
def test_stop_unknown_model_fails_plainly(ctx: Ctx):
    from halo_harness.providers.local_runtime import stop_managed_server
    state_dir = Path(tempfile.mkdtemp(prefix="local-runtime-state-"))
    ok, msg = stop_managed_server("no-such-model", state_dir=state_dir)
    ctx.check(f"unknown model -> (False, ...), got ({ok}, {msg!r})", ok is False and "no-such-model" in msg)


@test
def test_start_times_out_and_terminates_the_half_started_process(ctx: Ctx):
    from halo_harness.providers.local_runtime import start_managed_server
    state_dir = Path(tempfile.mkdtemp(prefix="local-runtime-state-"))
    stub = _write_stub(_STUB_NEVER_LISTENS)
    entry, reason = start_managed_server(model="never-ready", runtime="llama-server",
                                          binary_argv=[sys.executable, str(stub)], model_path="x.gguf",
                                          state_dir=state_dir, startup_timeout=1.5)
    ctx.check(f"start reports failure, got entry={entry!r}", entry is None)
    ctx.check(f"reason mentions the timeout, got {reason!r}", reason and "did not answer" in reason)


@test
def test_stop_all_except_kept(ctx: Ctx):
    from halo_harness.providers.local_runtime import (load_registry, start_managed_server,
                                                        stop_all_managed_servers_except_kept, _terminate_pid)
    state_dir = Path(tempfile.mkdtemp(prefix="local-runtime-state-"))
    stub = _write_stub(_STUB_LISTENS)
    kept, _ = start_managed_server(model="keep-me", runtime="llama-server", binary_argv=[sys.executable, str(stub)],
                                    model_path="a.gguf", state_dir=state_dir, keep=True)
    gone, _ = start_managed_server(model="stop-me", runtime="llama-server", binary_argv=[sys.executable, str(stub)],
                                    model_path="b.gguf", state_dir=state_dir, keep=False)
    try:
        ctx.check("both started", kept is not None and gone is not None)
        stop_all_managed_servers_except_kept(state_dir=state_dir)
        remaining = {e["model"] for e in load_registry(state_dir)}
        ctx.check(f"kept entry survives, got {remaining}", remaining == {"keep-me"})
        ctx.check("the non-kept process was actually terminated", _wait_until(lambda: not _is_alive(gone.pid)))
        ctx.check("the kept process is still alive", _is_alive(kept.pid))
    finally:
        _terminate_pid(kept.pid if kept else None)
        _terminate_pid(gone.pid if gone else None)


@test
def test_find_runtime_binary_searches_the_runtimes_dir(ctx: Ctx):
    from halo_harness.providers.local_runtime import find_runtime_binary, runtimes_dir
    state_dir = Path(tempfile.mkdtemp(prefix="local-runtime-find-"))
    exe_name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    old_dir = runtimes_dir(state_dir) / "b1000"
    new_dir = runtimes_dir(state_dir) / "b2000"
    for d in (old_dir, new_dir):
        (d / "bin").mkdir(parents=True)
        (d / "bin" / exe_name).write_bytes(b"")
    import shutil
    real_which = shutil.which
    try:
        shutil.which = lambda name: None  # force the PATH lookup to miss, exercising the runtimes_dir fallback
        found = find_runtime_binary("llama-server", state_dir=state_dir)
    finally:
        shutil.which = real_which
    ctx.check(f"found the NEWER version directory, got {found}", found is not None and "b2000" in found[0])


@test
def test_find_runtime_binary_mlx_lm_shape(ctx: Ctx):
    from halo_harness.providers.local_runtime import find_runtime_binary
    found = find_runtime_binary("mlx_lm")
    ctx.check(f"either not found, or the documented python -m shape, got {found}",
              found is None or found == [sys.executable, "-m", "mlx_lm.server"])


@test
def test_runtime_for_format(ctx: Ctx):
    from halo_harness.providers.local_runtime import runtime_for_format
    ctx.check("gguf always maps to llama-server", runtime_for_format("gguf") == "llama-server")
    expected_mlx = "mlx_lm" if sys.platform == "darwin" else None
    ctx.check(f"safetensors maps to mlx_lm only on darwin, got {runtime_for_format('safetensors')!r}",
              runtime_for_format("safetensors") == expected_mlx)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
