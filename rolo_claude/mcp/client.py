"""rolo_claude.mcp.client -- McpLoop: one daemon thread owning a SINGLE
asyncio event loop for the whole process's MCP traffic (every transport the
`mcp` SDK offers -- stdio/http/sse -- is async-only; the rest of this
harness, including the agent loop, is synchronous). `run(coro, timeout)` is
the ONLY way anything outside this module touches that loop: it schedules
the coroutine via `asyncio.run_coroutine_threadsafe` and blocks the CALLING
(sync) thread until it finishes or `timeout` elapses.

Deliberately has NO dependency on the `mcp` package itself -- this module
manages an asyncio loop and nothing else, so it stays importable even when
`mcp` isn't installed (rolo_claude.mcp.available() gates whether anything
ever calls into it).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, Coroutine, Optional, TypeVar

T = TypeVar("T")


class McpLoop:
    """Lazily starts its background thread + event loop on the FIRST
    `run()` call (never at construction, and never at all if MCP is never
    actually used in a session) -- a `-p` turn with zero MCP servers
    configured must not pay a thread-start cost it never needed."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            ready = threading.Event()
            holder: dict = {}

            def _run() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                holder["loop"] = loop
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            thread = threading.Thread(target=_run, name="rolo-claude-mcp-loop", daemon=True)
            thread.start()
            ready.wait(timeout=10)
            self._loop = holder.get("loop")
            self._thread = thread
            return self._loop

    def run(self, coro: "Coroutine[Any, Any, T]", timeout: Optional[float] = None) -> T:
        """Schedule `coro` onto the daemon loop; block the CALLING thread
        for its result. `timeout` (seconds, None = wait forever) raises
        `concurrent.futures.TimeoutError` on expiry -- the coroutine itself
        keeps running on the loop (asyncio has no safe cross-thread hard
        cancel of arbitrary awaits), so a caller that times out here must
        treat the underlying operation as ABANDONED, not actually stopped;
        `.cancel()` is still requested as a best-effort courtesy."""
        loop = self._ensure_started()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise

    def spawn(self, coro: "Coroutine[Any, Any, T]") -> "concurrent.futures.Future[T]":
        """Schedule `coro` onto the daemon loop WITHOUT blocking the
        calling thread for its result -- returns the `concurrent.futures.
        Future` immediately so the caller can wait on it LATER, or never.
        For a long-lived coroutine whose lifetime spans several separate
        synchronous calls (manager.McpServerHandle's open-then-wait-for-
        close lifecycle task -- anyio's cancel scopes require the SAME
        asyncio Task to both enter and exit a `stdio_client`/TaskGroup
        context, so open and close must run inside ONE task rather than
        each being its own `run()` call, which `asyncio.run_coroutine_
        threadsafe` would otherwise make true by creating a fresh Task
        per call [verified: "Attempted to exit cancel scope in a
        different task than it was entered in" on Linux/WSL])."""
        loop = self._ensure_started()
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def call_soon(self, callback) -> None:
        """Thread-safe: run `callback` (a plain, non-async function) on
        the daemon loop's own thread at its next iteration -- used to
        signal an `asyncio.Event` a spawned task is awaiting from the
        calling (sync) thread."""
        loop = self._ensure_started()
        loop.call_soon_threadsafe(callback)

    def close(self, timeout: float = 5.0) -> None:
        """Stop the loop and join its thread. Safe to call when never
        started, and safe to call more than once (a second call is a
        no-op) -- `McpManager.close_all()`'s own 5s deadline wraps this."""
        if self._loop is None:
            return
        loop = self._loop
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass  # loop already stopped/closed
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._loop = None
        self._thread = None
