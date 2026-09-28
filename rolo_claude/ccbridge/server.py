"""rolo_claude.ccbridge.server -- ToolBridgeServer, the PARENT side of the
tool bridge (H11 Part B). Newline-delimited JSON-RPC-shaped messages over
a local socket:

    request:  {"id": <int>, "method": "tools/list"|"tools/call", "params": {...}}
    response: {"id": <int>, "result": {...}}            (success)
           or {"id": <int>, "error": {"message": str}}  (failure)

`tools/list` params `{}` -> `{"tools": [{"name","description","input_schema"}, ...]}`
(the session's frozen catalog, same shape `ToolRegistry.definitions()`
already produces -- the child converts each straight into an `mcp.types.
Tool`). `tools/call` params `{"name": str, "arguments": dict}` -> `{"content":
[{"type":"text","text":...} | {"type":"image","data":...,"mime_type":...}],
"is_error": bool}`.

Transport: a Unix domain socket at `~/.rolo-claude/run/<sid>.sock`, mode
0600, on POSIX (no token needed -- filesystem permissions ARE the auth);
a TCP loopback socket (127.0.0.1, an OS-assigned port) plus a random
per-session token on Windows (no `AF_UNIX` there before 3.12/certain
builds, and even where present it is unreliable) -- the child's very
first line on a Windows connection must be `{"token": "..."}` or the
connection is closed without a response.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
from pathlib import Path
from typing import Callable, Optional

from rolo_claude.config.paths import bridge_home

_ACCEPT_POLL_S = 0.5


class ToolBridgeServer:
    def __init__(self, *, session_id: str, list_tools_fn: Callable[[], list],
                 call_tool_fn: Callable[[str, dict], dict]):
        self.session_id = session_id
        self._list_tools_fn = list_tools_fn
        self._call_tool_fn = call_tool_fn
        self._sock: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._conn_threads: "list[threading.Thread]" = []
        self._closed = threading.Event()
        self.token: Optional[str] = None
        self.socket_path: Optional[Path] = None
        self.host: Optional[str] = None
        self.port: Optional[int] = None

    def start(self) -> None:
        if self._sock is not None:
            return
        if os.name == "nt":
            self._start_tcp()
        else:
            self._start_unix()
        self._accept_thread = threading.Thread(target=self._accept_loop, name=f"ccbridge-accept-{self.session_id}",
                                                 daemon=True)
        self._accept_thread.start()

    def _start_unix(self) -> None:
        run_dir = bridge_home() / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        self.socket_path = run_dir / f"{self.session_id}.sock"
        try:
            self.socket_path.unlink()
        except OSError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        sock.listen(8)
        self._sock = sock

    def _start_tcp(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        self.host, self.port = sock.getsockname()
        self.token = secrets.token_hex(16)
        self._sock = sock

    def child_env(self) -> dict:
        """Environment additions the CHILD (`python -m rolo_claude.
        ccbridge`) needs to reach this server -- folded into the
        `--mcp-config` stdio server's own `env`."""
        if os.name == "nt":
            return {"ROLO_CCBRIDGE_HOST": self.host or "127.0.0.1", "ROLO_CCBRIDGE_PORT": str(self.port),
                     "ROLO_CCBRIDGE_TOKEN": self.token or ""}
        return {"ROLO_CCBRIDGE_SOCKET": str(self.socket_path)}

    def _accept_loop(self) -> None:
        while not self._closed.is_set():
            try:
                self._sock.settimeout(_ACCEPT_POLL_S)
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            t = threading.Thread(target=self._serve_conn, args=(conn,),
                                  name=f"ccbridge-conn-{self.session_id}", daemon=True)
            self._conn_threads.append(t)
            t.start()

    def _serve_conn(self, conn: socket.socket) -> None:
        rf = conn.makefile("rb")
        wf = conn.makefile("wb", buffering=0)
        try:
            if os.name == "nt":
                first = rf.readline()
                if not first:
                    return
                try:
                    hello = json.loads(first.decode("utf-8", "replace"))
                except ValueError:
                    hello = None
                if not isinstance(hello, dict) or hello.get("token") != self.token:
                    self._write(wf, {"id": None, "error": {"message": "bad or missing token"}})
                    return
            while not self._closed.is_set():
                line = rf.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                self._write(wf, self._handle(req))
        except OSError:
            pass
        finally:
            for f in (rf, wf):
                try:
                    f.close()
                except OSError:
                    pass
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _write(wf, obj: dict) -> None:
        try:
            wf.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass

    def _handle(self, req: dict) -> dict:
        rid = req.get("id") if isinstance(req, dict) else None
        method = req.get("method") if isinstance(req, dict) else None
        params = (req.get("params") if isinstance(req, dict) else None) or {}
        try:
            if method == "tools/list":
                return {"id": rid, "result": {"tools": self._list_tools_fn()}}
            if method == "tools/call":
                result = self._call_tool_fn(params.get("name"), params.get("arguments") or {})
                return {"id": rid, "result": result}
            return {"id": rid, "error": {"message": f"unknown ccbridge method {method!r}"}}
        except Exception as e:  # a bridge RPC handler must never take the accept loop down with it
            return {"id": rid, "error": {"message": f"{type(e).__name__}: {e}"}}

    def close(self) -> None:
        self._closed.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self.socket_path is not None:
            try:
                self.socket_path.unlink()
            except OSError:
                pass
