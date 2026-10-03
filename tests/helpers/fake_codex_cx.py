"""tests.helpers.fake_codex_cx -- a fake `codex` executable for the `cx:`
route's tests (`HALO_CODEX_EXE` points a test at `"<python>" "<this file>"`).

  * `<fake> login status` -- prints `FAKE_CODEX_LOGIN` (default
    "Logged in using ChatGPT") to stderr, like codex 0.153.4 does.
  * `<fake> --version` -- `codex-cli 0.153.4`.
  * `<fake> app-server ...` -- newline JSON-RPC on stdio, the subset Halo
    uses: initialize, config/read, model/list, account/read,
    account/rateLimits/read, thread/start|resume|fork, turn/start,
    turn/steer, turn/interrupt. On thread start it spawns the "halo" MCP
    server from the thread config (the REAL `python -m
    halo_harness.ccbridge`) and talks real MCP to it.

Turn grammar (the last text input of turn/start):
  * `TOOL:<name>:<json-args>` -- elicitation request, then a real
    tools/call on the bridge; replies `done:<first line of the result>`.
  * `PATCH:<path>:<content>` -- a native fileChange item plus an approval
    request; writes the file only when accepted.
  * `SLEEP:<seconds>` -- waits (turn/interrupt ends it early); any
    turn/steer text received meanwhile is echoed as `steered:<text>`.
  * `FAIL:<message>` -- an error notification and a failed turn.
  * anything else -- replies `pong` if it contains "pong", else `noted`.

`FAKE_CODEX_LOG` (a path) gets one JSON line per thread/turn request.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid

_out_lock = threading.Lock()
_pending: dict = {}           # server-request id -> {"event", "result"}
_next_server_id = [1000]
_threads: dict = {}           # thread id -> {"mcp": McpClient|None}
_turn = {"id": None, "interrupt": threading.Event(), "steers": []}
_usage = {"inputTokens": 0, "outputTokens": 0, "cachedInputTokens": 0, "reasoningOutputTokens": 0}


def _send(obj: dict) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def _log(method: str, params) -> None:
    path = os.environ.get("FAKE_CODEX_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"method": method, "params": params}) + "\n")


def _notify(method: str, params: dict) -> None:
    _send({"method": method, "params": params})


def _server_request(method: str, params: dict, timeout: float = 60.0):
    _next_server_id[0] += 1
    rid = _next_server_id[0]
    slot = {"event": threading.Event(), "result": None}
    _pending[rid] = slot
    _send({"id": rid, "method": method, "params": params})
    slot["event"].wait(timeout)
    return slot["result"] or {}


class McpClient:
    """Minimal synchronous stdio MCP client for the bridge child."""

    def __init__(self, entry: dict):
        env = dict(os.environ)
        env.update(entry.get("env") or {})
        self.proc = subprocess.Popen([entry["command"], *entry.get("args", [])], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        self.next_id = 0
        self.lock = threading.Lock()
        self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "fake-codex", "version": "0"}})
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.tools = [t["name"] for t in self.call("tools/list", {}).get("tools", [])]

    def _write(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def call(self, method: str, params: dict) -> dict:
        with self.lock:
            self.next_id += 1
            mid = self.next_id
            self._write({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    return {}
                msg = json.loads(line)
                if msg.get("id") == mid and "method" not in msg:
                    return msg.get("result") or {}

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def _agent_message(thread_id: str, turn_id: str, text: str) -> None:
    item_id = f"msg_{uuid.uuid4().hex[:8]}"
    _notify("item/started", {"threadId": thread_id, "turnId": turn_id,
                             "item": {"type": "agentMessage", "id": item_id, "text": ""}})
    _notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": turn_id, "itemId": item_id, "delta": text})
    _notify("item/completed", {"threadId": thread_id, "turnId": turn_id,
                               "item": {"type": "agentMessage", "id": item_id, "text": text, "phase": "final_answer"}})


def _run_turn(thread_id: str, turn_id: str, text: str) -> None:
    status, error = "completed", None
    mcp = _threads.get(thread_id, {}).get("mcp")
    if text.startswith("TOOL:"):
        _, name, args = text.split(":", 2)
        args = json.loads(args)
        reply = _server_request("mcpServer/elicitation/request", {
            "threadId": thread_id, "turnId": turn_id, "serverName": "halo", "mode": "form",
            "_meta": {"codex_approval_kind": "mcp_tool_call"}, "message": f"Allow {name}?"})
        if reply.get("action") != "accept" or mcp is None:
            _agent_message(thread_id, turn_id, "tool call rejected")
        else:
            item_id = f"exec-{uuid.uuid4().hex[:8]}"
            item = {"type": "mcpToolCall", "id": item_id, "server": "halo", "tool": name, "arguments": args}
            _notify("item/started", {"threadId": thread_id, "turnId": turn_id, "item": item})
            res = mcp.call("tools/call", {"name": name, "arguments": args})
            first = ((res.get("content") or [{}])[0].get("text") or "").splitlines()[:1]
            _notify("item/completed", {"threadId": thread_id, "turnId": turn_id, "item": {**item, "result": res}})
            _agent_message(thread_id, turn_id, "done:" + (first[0] if first else ""))
    elif text.startswith("PATCH:"):
        _, path, content = text.split(":", 2)
        item_id = f"exec-{uuid.uuid4().hex[:8]}"
        item = {"type": "fileChange", "id": item_id,
                "changes": [{"path": path, "kind": {"type": "add"}, "diff": content}], "status": "inProgress"}
        _notify("item/started", {"threadId": thread_id, "turnId": turn_id, "item": item})
        reply = _server_request("item/fileChange/requestApproval",
                                {"threadId": thread_id, "turnId": turn_id, "itemId": item_id})
        accepted = reply.get("decision") == "accept"
        if accepted:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        _notify("item/completed", {"threadId": thread_id, "turnId": turn_id,
                                   "item": {**item, "status": "completed" if accepted else "declined"}})
        _agent_message(thread_id, turn_id, "patched" if accepted else "declined")
    elif text.startswith("SLEEP:"):
        deadline = time.monotonic() + float(text.split(":", 1)[1].split()[0])
        while time.monotonic() < deadline and not _turn["interrupt"].is_set():
            time.sleep(0.02)
        if _turn["interrupt"].is_set():
            status = "interrupted"
        else:
            _agent_message(thread_id, turn_id, "".join(f"steered:{s}" for s in _turn["steers"]) or "slept")
    elif text.startswith("FAIL:"):
        error = text.split(":", 1)[1]
        _notify("error", {"threadId": thread_id, "turnId": turn_id, "willRetry": False, "error": {"message": error}})
        status = "failed"
    else:
        _agent_message(thread_id, turn_id, "pong" if "pong" in text else "noted")
    last = {"inputTokens": 100, "outputTokens": 10, "cachedInputTokens": 0, "reasoningOutputTokens": 0}
    for k, v in last.items():
        _usage[k] += v
    _notify("thread/tokenUsage/updated", {"threadId": thread_id, "turnId": turn_id, "tokenUsage": {
        "total": dict(_usage), "last": last, "modelContextWindow": 200000}})
    _notify("account/rateLimits/updated", {"rateLimits": {"planType": "pro", "primary": {
        "usedPercent": 7, "windowDurationMins": 300, "resetsAt": int(time.time()) + 3600}}})
    _turn["id"] = None
    _notify("turn/completed", {"threadId": thread_id, "turn": {
        "id": turn_id, "status": status, "error": {"message": error} if error else None, "durationMs": 5}})


def _thread_open(method: str, params: dict) -> dict:
    _log(method, params)
    tid = params.get("threadId") if method == "thread/resume" else None
    if method == "thread/resume" and os.environ.get("FAKE_CODEX_RESUME_FAILS"):
        raise ValueError("no rollout found for thread id")
    tid = tid or str(uuid.uuid4())
    entry = ((params.get("config") or {}).get("mcp_servers") or {}).get("halo")
    _threads[tid] = {"mcp": McpClient(entry) if entry else None}
    return {"thread": {"id": tid, "model": params.get("model")}}


def _handle(msg: dict) -> None:
    method, params, mid = msg.get("method"), msg.get("params") or {}, msg.get("id")
    try:
        if method == "initialize":
            result = {"userAgent": "fake-codex/0.153.4", "codexHome": "/nonexistent"}
        elif method == "config/read":
            names = [n for n in os.environ.get("FAKE_CODEX_USER_MCP", "").split(",") if n]
            result = {"config": {"mcp_servers": {n: {"command": "x"} for n in names}}}
        elif method == "model/list":
            result = {"data": [
                {"id": "gpt-test", "model": "gpt-test", "displayName": "GPT-Test", "isDefault": True,
                 "defaultReasoningEffort": "medium", "hidden": False, "inputModalities": ["text", "image"],
                 "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high")]},
                {"id": "gpt-hidden", "model": "gpt-hidden", "hidden": True, "supportedReasoningEfforts": []}]}
        elif method == "account/read":
            result = {"account": {"type": "chatgpt", "email": "user@example.com", "planType": "pro"}}
        elif method == "account/rateLimits/read":
            result = {"rateLimits": {"planType": "pro", "primary": {"usedPercent": 3, "windowDurationMins": 300}}}
        elif method in ("thread/start", "thread/resume", "thread/fork"):
            result = _thread_open(method, params)
        elif method == "turn/start":
            _log(method, params)
            texts = [i.get("text", "") for i in params.get("input", []) if i.get("type") == "text"]
            turn_id = str(uuid.uuid4())
            _turn.update(id=turn_id, steers=[])
            _turn["interrupt"].clear()
            threading.Thread(target=_run_turn, args=(params["threadId"], turn_id, texts[-1] if texts else ""),
                             daemon=True).start()
            result = {"turn": {"id": turn_id, "status": "inProgress"}}
        elif method == "turn/steer":
            _log(method, params)
            if params.get("expectedTurnId") != _turn["id"]:
                raise ValueError("no active turn to steer")
            _turn["steers"].append(params["input"][0]["text"])
            result = {"turnId": _turn["id"]}
        elif method == "turn/interrupt":
            _log(method, params)
            _turn["interrupt"].set()
            result = {}
        else:
            result = {}
    except Exception as e:
        if mid is not None:
            _send({"id": mid, "error": {"code": -32600, "message": str(e)}})
        return
    if mid is not None:
        _send({"id": mid, "result": result})


def app_server() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if "method" not in msg and msg.get("id") in _pending:
            slot = _pending.pop(msg["id"])
            slot["result"] = msg.get("result")
            slot["event"].set()
            continue
        if msg.get("method") in ("turn/start", "turn/steer", "turn/interrupt") or "id" not in msg:
            _handle(msg)
        else:
            threading.Thread(target=_handle, args=(msg,), daemon=True).start()
    for t in _threads.values():
        if t.get("mcp"):
            t["mcp"].close()
    return 0


def main(argv: list) -> int:
    if argv[:2] == ["login", "status"]:
        text = os.environ.get("FAKE_CODEX_LOGIN", "Logged in using ChatGPT")
        print(text, file=sys.stderr)
        return 0 if text.lower().startswith("logged in") else 1
    if argv[:1] == ["--version"]:
        print("codex-cli 0.153.4")
        return 0
    if argv[:1] == ["app-server"]:
        return app_server()
    print(f"fake codex: unsupported argv {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
