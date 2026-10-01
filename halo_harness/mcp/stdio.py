"""rolo_claude.mcp.stdio -- stdio transport connect, built on
`mcp.client.stdio.stdio_client` + `mcp.ClientSession`. Imports `mcp` lazily
(inside function bodies only) so this module stays importable without the
SDK; `manager.py` is the only caller and only calls in after
`rolo_claude.mcp.available()` is True.

Verified against the installed SDK (2.2.0) rather than assumed from the
plan's Claude-Code-binary-derived spec: `stdio_client` ALREADY does both
things the plan called out as harness responsibilities --
`_get_executable_command`/`get_windows_executable_command` resolves a bare
`npx` to `npx.cmd` via PATHEXT on win32, and `_create_platform_compatible_
process` spawns "in its own kill scope" (a Job Object on win32, `
start_new_session=True` -- a process group -- on POSIX), so closing the
connection's AsyncExitStack tears down the WHOLE child tree on both
platforms without this module reimplementing `CREATE_NEW_PROCESS_GROUP`/
`taskkill /T /F` by hand. This module's own job is just: build
StdioServerParameters correctly (env layering, cwd) and the rotating
per-server stderr log.
"""

from __future__ import annotations

import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Optional

from rolo_claude.config.paths import bridge_home

_ROTATE_MAX_BYTES = 5 * 1024 * 1024  # 5 MB, matches binary-facts sec.9's own server log rotation


def errlog_path(server_name: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in server_name) or "server"
    return bridge_home() / "mcp" / f"{safe}.log"


def _rotate_if_oversized(path: Path, max_bytes: int) -> None:
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            backup = path.with_name(path.name + ".1")
            if backup.exists():
                backup.unlink()
            path.rename(backup)
    except OSError:
        pass  # best-effort -- a rotation failure must never block a connect


def open_errlog(server_name: str, max_bytes: int = _ROTATE_MAX_BYTES):
    """A REAL file object at `~/.rolo-claude/mcp/<server>.log`, rotated to
    a single `.log.1` backup first if it's already over 5 MB.

    `mcp.client.stdio.stdio_client`'s `errlog=` is handed straight to the
    OS as the child process's stderr redirect target (it needs a real file
    descriptor -- `_create_platform_compatible_process` passes it to
    `anyio.open_process`/`CreateProcess`), so the subprocess writes
    directly to the underlying fd; nothing in this PROCESS ever sees those
    bytes to inspect them mid-write, which rules out true continuous
    rotation (the classic `RotatingFileHandler` pattern) without an extra
    pipe-plus-reader-thread the size of this feature doesn't justify.
    Rotating once per CONNECT (this function) instead gives the same
    steady-state behaviour (a long-lived server's log never grows
    unbounded across repeated `mcp list`/session runs) at a coarser, much
    simpler granularity."""
    path = errlog_path(server_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_oversized(path, max_bytes)
    return open(path, "a", encoding="utf-8", errors="replace")


def build_env(server_env: Optional[dict], effective_env: dict, cwd: Path) -> dict:
    """`effective_env` (settings-aware: shell < user < trusted project/local
    < flag < policy) + this server's OWN `env` block (wins collisions) +
    `CLAUDE_PROJECT_DIR` [D-CFG: "env = effective_env + server env +
    CLAUDE_PROJECT_DIR", "stdio servers get CLAUDE_PROJECT_DIR"]."""
    env = dict(effective_env)
    if server_env:
        env.update({str(k): str(v) for k, v in server_env.items()})
    env.setdefault("CLAUDE_PROJECT_DIR", str(cwd))
    return env


async def connect(*, command: str, args: list, env: dict, cwd: Optional[str],
                   errlog, connect_timeout: float):
    """Open a stdio MCP connection. Returns `(stack, session)` with the
    `AsyncExitStack` STILL OPEN -- the caller (manager.McpServerHandle)
    owns its lifetime from here and must `await stack.aclose()` exactly
    once. Raises on any failure (spawn error, connect timeout); the caller
    maps the exception to a connection state.

    finding 15: `connect_timeout` is enforced via `client.task_timeout`
    (same-task `Task.cancel()`), never `asyncio.wait_for` -- `stdio_client`
    stays open past this function's return (the AsyncExitStack pattern is
    the whole point), and `wait_for` ties its anyio cancel scope to the
    short-lived Task it creates to run the awaitable, not to whichever
    task calls `stack.aclose()` later -- verified: "Attempted to exit
    cancel scope in a different task than it was entered in"."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from rolo_claude.mcp.client import task_timeout

    params = StdioServerParameters(
        command=command, args=[str(a) for a in (args or [])],
        env=env, cwd=str(cwd) if cwd else None,
        # finding 3: the SDK default is "strict" -- one non-UTF-8 byte on
        # the server's stdout then kills the reader permanently (every
        # later response silently dropped, every call hangs its full
        # timeout). "replace" degrades that one line instead of the whole
        # connection.
        encoding_error_handler="replace",
    )
    stack = AsyncExitStack()
    try:
        async with task_timeout(connect_timeout):
            read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
        session = await stack.enter_async_context(ClientSession(read, write))
        return stack, session
    except BaseException:
        await stack.aclose()
        raise


def is_windows() -> bool:
    return sys.platform == "win32"
