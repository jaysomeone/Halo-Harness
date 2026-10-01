"""halo_harness.ccbridge.client -- ParentLink, the CHILD-side connection to
the PARENT's ToolBridgeServer (H11 Part B). Reads its endpoint from the
environment (`HALO_CCBRIDGE_SOCKET` on POSIX, `HALO_CCBRIDGE_HOST`/`_PORT`/
`_TOKEN` on Windows -- see server.py's own `child_env()`, folded into the
`--mcp-config` stdio server's `env` by `agent/cc_process.py`). One
connection, held for the whole child process lifetime; calls are
synchronous and serialized under a lock (the MCP SDK's own stdio server
calls `on_call_tool` sequentially per connection today, but a lock costs
nothing and removes any doubt).
"""

from __future__ import annotations

import json
import os
import socket
import threading
from typing import Optional


class ParentLinkError(Exception):
    pass


class ParentLink:
    def __init__(self, env: Optional[dict] = None):
        env = env if env is not None else os.environ
        self._lock = threading.Lock()
        self._next_id = 1
        sock_path = env.get("HALO_CCBRIDGE_SOCKET")
        if sock_path:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(sock_path)
        else:
            host = env.get("HALO_CCBRIDGE_HOST", "127.0.0.1")
            port_raw = env.get("HALO_CCBRIDGE_PORT")
            if not port_raw:
                raise ParentLinkError(
                    "no ccbridge endpoint in the environment (HALO_CCBRIDGE_SOCKET or "
                    "HALO_CCBRIDGE_HOST/_PORT must be set -- this process must be spawned by "
                    "halo's own cc: transport, never run standalone)"
                )
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((host, int(port_raw)))
            token = env.get("HALO_CCBRIDGE_TOKEN", "")
            sock.sendall((json.dumps({"token": token}) + "\n").encode("utf-8"))
        self._sock = sock
        self._rf = sock.makefile("rb")
        self._wf = sock.makefile("wb", buffering=0)

    def _call(self, method: str, params: dict) -> dict:
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            payload = json.dumps({"id": rid, "method": method, "params": params}, ensure_ascii=False)
            self._wf.write((payload + "\n").encode("utf-8"))
            line = self._rf.readline()
        if not line:
            raise ParentLinkError("ccbridge parent link closed (halo process gone?)")
        resp = json.loads(line.decode("utf-8", "replace"))
        err = resp.get("error")
        if err:
            raise ParentLinkError(err.get("message", "ccbridge error") if isinstance(err, dict) else str(err))
        return resp.get("result") or {}

    def list_tools(self) -> list:
        result = self._call("tools/list", {})
        tools = result.get("tools")
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._call("tools/call", {"name": name, "arguments": arguments or {}})

    def await_tools_change(self, known_generation: int) -> dict:
        """H11b finding 6: a bounded long-poll -- the parent's own
        `_await_tools_change` always answers within `_TOOLS_AWAIT_
        TIMEOUT_S`, generation unchanged if nothing grew the catalog in
        that window. Callers loop this on their OWN dedicated connection
        (see `ccbridge/__main__.py`'s `_watch_tool_changes`)."""
        return self._call("tools/await_change", {"known_generation": known_generation})

    def close(self) -> None:
        for f in (self._rf, self._wf):
            try:
                f.close()
            except OSError:
                pass
        try:
            self._sock.close()
        except OSError:
            pass
