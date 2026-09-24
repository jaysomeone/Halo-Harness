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
import atexit
import concurrent.futures
import threading
import weakref
from typing import Any, Coroutine, Optional, TypeVar

T = TypeVar("T")

# Every McpLoop still alive is tracked here (weakly -- being in this set
# must never be the reason a McpLoop outlives its owner) so
# `_stop_all_at_exit` can hard-stop any that are still running when the
# interpreter starts shutting down. This closes the gap that produced a
# Windows-only segfault (exit 139) after a full `test_mcp_manager.py` run:
# a daemon thread's asyncio loop (and any live MCP stdio subprocess
# transport still registered under it) left running into interpreter
# teardown, where CPython tearing down modules/C-extension state out from
# under that thread corrupts the loop's Windows IOCP machinery instead of
# just leaking memory.
_live_loops: "weakref.WeakSet[McpLoop]" = weakref.WeakSet()
_live_loops_lock = threading.Lock()
_atexit_registered = False


def _stop_all_at_exit() -> None:
    for loop in list(_live_loops):
        try:
            loop.stop()
        except Exception:
            pass


class McpLoop:
    """Lazily starts its background thread + event loop on the FIRST
    `run()` call (never at construction, and never at all if MCP is never
    actually used in a session) -- a `-p` turn with zero MCP servers
    configured must not pay a thread-start cost it never needed."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()
        self._stopped = False
        global _atexit_registered
        with _live_loops_lock:
            _live_loops.add(self)
            if not _atexit_registered:
                atexit.register(_stop_all_at_exit)
                _atexit_registered = True

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._stopped:
                raise RuntimeError("McpLoop has been stopped and cannot be restarted")
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
        started, and safe to call more than once, or after `stop()` (a
        repeat call is a no-op) -- `McpManager.close_all()`'s own 5s
        deadline wraps this."""
        if self._stopped:
            return
        self._stopped = True
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

    def stop(self, timeout: float = 5.0) -> None:
        """Hard stop -- used by `McpManager.close_all()` and by the
        process-exit safety net (`_stop_all_at_exit` above). First cancels
        every task still running on the loop (e.g. a `McpServerHandle.
        _lifecycle_task` still parked in `await self._close_event.wait()`
        because nobody called `.close()` on its handle), giving them up to
        half of `timeout` to actually unwind -- a cancelled
        `_lifecycle_task` still runs its own `finally: await self._stack.
        aclose()`, so this is what lets an MCP connection nobody
        explicitly closed tear down its subprocess/transport BEFORE the
        loop itself stops, rather than leaving it dangling for interpreter
        shutdown to trip over. Then does exactly what `close()` does.
        Idempotent, like `close()` (a second call, or a call after
        `close()`, is a no-op)."""
        if self._stopped:
            return
        half = max(0.1, timeout / 2.0)
        self._cancel_pending_tasks(half)
        self.close(timeout=max(0.1, timeout - half))

    def _cancel_pending_tasks(self, timeout: float) -> None:
        """Best-effort only: give every non-done task on this loop up to
        `timeout` to finish NATURALLY first, and only `.cancel()` whatever
        is still running once that's spent -- never the other way around.
        A straggler here can be a manager.py `McpServerHandle.
        _lifecycle_task` still parked in `await self._close_event.wait()`
        (cancelling it is exactly what lets its `finally: await self.
        _stack.aclose()` run), but it can just as easily be a task already
        MID-cleanup, e.g. manager.py's own fire-and-forget `stack.aclose()`
        task for a connect attempt that timed out -- the installed mcp
        SDK's `stdio_client` shutdown there is deliberately shielded from
        cancellation (so a caller's cancellation can't abort it), but that
        shield only protects against cancellation propagating from an
        ENCLOSING scope; a raw `Task.cancel()` called directly on it from
        here, as this method used to do first, still throws CancelledError
        into it and interrupts the shutdown sequence before it reaches the
        step that marks the subprocess transport closed -- verified on
        Windows to reproduce the exact leaked/crashing transport this
        whole mechanism exists to prevent. Waiting first avoids that.
        Never raises -- `stop()` proceeds into `close()` regardless of
        whether every task finishes in time."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return

        async def _wait_then_cancel() -> None:
            mine = asyncio.current_task()
            tasks = [t for t in asyncio.all_tasks() if t is not mine and not t.done()]
            if not tasks:
                return
            _done, pending = await asyncio.wait(tasks, timeout=timeout)
            for t in pending:
                t.cancel()  # best-effort courtesy only -- not awaited further

        try:
            fut = asyncio.run_coroutine_threadsafe(_wait_then_cancel(), loop)
            fut.result(timeout=timeout + 1.0)
        except Exception:
            pass
