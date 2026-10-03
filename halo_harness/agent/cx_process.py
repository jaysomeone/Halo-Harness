"""halo_harness.agent.cx_process -- the `codex app-server` subprocess
behind the `cx:` route, and a small JSON-RPC client for it.

`codex app-server` speaks newline-delimited JSON-RPC 2.0 on stdio (the
protocol the Codex IDE extension uses). Three message kinds arrive on
stdout: responses to our requests (`id` + `result`/`error`), notifications
(`method`, no `id`: streaming deltas, item lifecycle, token usage, rate
limits, `turn/completed`), and server requests (`method` + `id`: approval
prompts and MCP elicitations, which we must answer). Verified live against
codex-cli 0.153.4.

One process per Halo session, kept alive across turns, its own process
group so Esc and quit reach every descendant (the MCP bridge child
included), the same lifecycle `cc_process.ClaudeCodeProcess` has.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from halo_harness import __version__ as _HALO_VERSION


class CodexRpcError(Exception):
    def __init__(self, message: str, *, code: Optional[int] = None, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


NotificationFn = Callable[[str, dict], None]
ServerRequestFn = Callable[[object, str, dict], None]


class CodexAppServer:
    def __init__(self, argv: list, *, cwd: "Path | str", env: Optional[dict] = None):
        self.argv = list(argv)
        kwargs = dict(cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      text=True, encoding="utf-8", errors="replace", bufsize=1)
        if env is not None:
            kwargs["env"] = dict(env)
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self._proc = subprocess.Popen(self.argv, **kwargs)
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict = {}
        self._closed = threading.Event()
        self._stderr_buf: "deque[str]" = deque(maxlen=200)
        self.on_notification: Optional[NotificationFn] = None
        self.on_server_request: Optional[ServerRequestFn] = None
        self.on_exit: Optional[Callable[[], None]] = None
        threading.Thread(target=self._drain_stderr, daemon=True, name=f"cx-stderr-{self.pid}").start()
        threading.Thread(target=self._read_loop, daemon=True, name=f"cx-reader-{self.pid}").start()

    @classmethod
    def start(cls, *, cwd: "Path | str", env: Optional[dict] = None, extra_args: "list | tuple" = (),
              timeout: float = 30.0) -> "CodexAppServer":
        """Spawn + `initialize` handshake. Raises CodexNotFoundError when
        there is no codex binary, CodexRpcError when the handshake fails."""
        from halo_harness.providers.cx_models import resolve_codex_launch_argv
        argv = resolve_codex_launch_argv() + ["app-server", *extra_args]
        server = cls(argv, cwd=cwd, env=env)
        try:
            server.request("initialize", {"clientInfo": {"name": "halo", "title": "Halo Harness",
                                                          "version": _HALO_VERSION}}, timeout=timeout)
            server.notify("initialized")
        except CodexRpcError:
            server.close()
            raise
        return server

    # ---- wire ----------------------------------------------------------

    def _write(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False)
        with self._write_lock:
            stdin = self._proc.stdin
            if stdin is None or stdin.closed:
                raise BrokenPipeError("codex app-server stdin is closed")
            stdin.write(line + "\n")
            stdin.flush()

    def request(self, method: str, params: Optional[dict] = None, *, timeout: Optional[float] = 60.0):
        """Send a request and wait for its response; returns `result`.
        Raises CodexRpcError on an error response, a timeout or EOF."""
        waiter = {"event": threading.Event(), "result": None, "error": None}
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            self._pending[rid] = waiter
        msg = {"id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._write(msg)
        except (BrokenPipeError, OSError) as e:
            with self._lock:
                self._pending.pop(rid, None)
            raise CodexRpcError(f"codex app-server is not running ({e})") from e
        if not waiter["event"].wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise CodexRpcError(f"codex app-server did not answer {method} within {timeout:.0f}s")
        if waiter["error"] is not None:
            err = waiter["error"]
            raise CodexRpcError(str(err.get("message") or err), code=err.get("code"), data=err.get("data"))
        return waiter["result"]

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        msg = {"method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._write(msg)
        except (BrokenPipeError, OSError):
            pass

    def respond(self, request_id, result: dict) -> None:
        try:
            self._write({"id": request_id, "result": result})
        except (BrokenPipeError, OSError):
            pass

    def respond_error(self, request_id, message: str, *, code: int = -32603) -> None:
        try:
            self._write({"id": request_id, "error": {"code": code, "message": message}})
        except (BrokenPipeError, OSError):
            pass

    def _read_loop(self) -> None:
        stdout = self._proc.stdout
        try:
            for line in stdout if stdout is not None else ():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    self._dispatch(obj)
        except (OSError, ValueError):
            pass
        finally:
            self._closed.set()
            with self._lock:
                pending, self._pending = self._pending, {}
            for waiter in pending.values():
                waiter["error"] = {"message": "codex app-server exited" + self._stderr_suffix()}
                waiter["event"].set()
            cb = self.on_exit
            if cb is not None:
                try:
                    cb()
                except Exception:
                    pass

    def _dispatch(self, obj: dict) -> None:
        method = obj.get("method")
        if method is None and "id" in obj:
            with self._lock:
                waiter = self._pending.pop(obj.get("id"), None)
            if waiter is not None:
                waiter["result"] = obj.get("result")
                waiter["error"] = obj.get("error")
                waiter["event"].set()
            return
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
        if "id" in obj:
            handler = self.on_server_request
            if handler is None:
                self.respond_error(obj["id"], f"halo has no handler for {method}", code=-32601)
                return
            # Approval handlers may wait on the user; never block the reader.
            threading.Thread(target=self._run_server_request, args=(handler, obj["id"], method, params),
                             daemon=True, name=f"cx-req-{method}").start()
            return
        cb = self.on_notification
        if cb is not None:
            try:
                cb(method, params)
            except Exception:
                pass

    def _run_server_request(self, handler: ServerRequestFn, rid, method: str, params: dict) -> None:
        try:
            handler(rid, method, params)
        except Exception as e:
            self.respond_error(rid, f"halo failed to handle {method}: {type(e).__name__}: {e}")

    def _drain_stderr(self) -> None:
        try:
            stream = self._proc.stderr
            for line in stream if stream is not None else ():
                self._stderr_buf.append(line.rstrip("\n"))
        except (OSError, ValueError):
            pass

    def _stderr_suffix(self) -> str:
        tail = [ln for ln in self._stderr_buf if ln.strip()]
        return f" -- {tail[-1][:300]}" if tail else ""

    # ---- lifecycle -----------------------------------------------------

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None and not self._closed.is_set()

    def stderr_tail(self, max_chars: int = 2000) -> str:
        return "\n".join(self._stderr_buf)[-max_chars:]

    def kill(self) -> None:
        try:
            if os.name == "nt":
                self._proc.kill()
            else:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def close(self, *, grace: float = 3.0) -> None:
        """Close stdin (app-server exits on EOF), then terminate the group."""
        try:
            with self._write_lock:
                if self._proc.stdin is not None and not self._proc.stdin.closed:
                    self._proc.stdin.close()
        except OSError:
            pass
        if self.wait(timeout=min(grace, 1.0)) is not None:
            return
        try:
            if os.name == "nt":
                self._proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        if self.wait(timeout=grace) is None:
            self.kill()
            self.wait(timeout=2.0)
