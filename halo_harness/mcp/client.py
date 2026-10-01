"""halo_harness.mcp.client -- McpLoop: one daemon thread owning a SINGLE
asyncio event loop for the whole process's MCP traffic (every transport the
`mcp` SDK offers -- stdio/http/sse -- is async-only; the rest of this
harness, including the agent loop, is synchronous). `run(coro, timeout)` is
the ONLY way anything outside this module touches that loop: it schedules
the coroutine via `asyncio.run_coroutine_threadsafe` and blocks the CALLING
(sync) thread until it finishes or `timeout` elapses.

Deliberately has NO dependency on the `mcp` package itself -- this module
manages an asyncio loop and nothing else, so it stays importable even when
`mcp` isn't installed (halo_harness.mcp.available() gates whether anything
ever calls into it).
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import threading
import time
import weakref
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Coroutine, Optional, TypeVar

T = TypeVar("T")


@asynccontextmanager
async def task_timeout(seconds: Optional[float]) -> "AsyncIterator[None]":
    """finding 15: a `with`-style timeout for code that opens a resource
    which must OUTLIVE this block (an `AsyncExitStack`-held async context
    manager, e.g. `stdio_client`) -- cancels the CURRENT task directly
    (`asyncio.Task.cancel()`) rather than wrapping the guarded code in a
    NEW task (`asyncio.wait_for`'s own approach) or a NEW nested anyio
    cancel scope (`anyio.fail_after`). Both of those break once the
    guarded code opens something meant to survive past this block:
    `wait_for` ties the resource's anyio cancel scope to a task that's
    gone by the time the resource's `aclose()` runs later from the
    DIFFERENT (real owning) task -- verified: "Attempted to exit cancel
    scope in a different task than it was entered in". `anyio.fail_after`
    nests its OWN cancel scope around the still-open resource's, which
    then can't legally close before `fail_after`'s scope does -- verified
    (empirically, in a minimal repro, NOT just cross-task -- this happens
    even fully within ONE task): "Attempted to exit a cancel scope that
    isn't the current task's current cancel scope". A raw `Task.cancel()`
    creates neither a new task nor a new scope -- it just injects
    `CancelledError` at whatever await point is current, in THIS task, so
    whatever the guarded code opens stays correctly tied to this one task
    for its whole life, however long that turns out to be past this
    block. `seconds=None` disables the timeout (plain passthrough)."""
    if seconds is None:
        yield
        return
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    timed_out = False

    def _fire() -> None:
        nonlocal timed_out
        timed_out = True
        if task is not None:
            task.cancel()

    handle = loop.call_later(seconds, _fire)
    try:
        yield
    except asyncio.CancelledError:
        if timed_out:
            raise TimeoutError(f"timed out after {seconds:.1f}s") from None
        raise  # a genuinely different cancellation reason -- never mask it as our own timeout
    finally:
        handle.cancel()


# H3c-regression fix: the independent grace period `McpLoop._cancel_
# pending_tasks` gives a cancelled task's OWN follow-up cleanup (see that
# method's docstring) once it decides to force a task. Sized off the
# installed mcp SDK's own documented worst-case stdio shutdown budget
# (mcp.client.stdio: PROCESS_TERMINATION_TIMEOUT=2.0s close-stdin grace +
# _KILL_REAP_TIMEOUT=2.0s post-kill reap wait, run inside a cancellation-
# shielded block the SDK itself won't cut short), plus headroom -- NEVER
# derived from however little of `stop()`'s own (possibly near-exhausted)
# `timeout` happens to be left, which is exactly what let a still-
# connecting server's cleanup get abandoned mid-flight (verified on
# Windows: a leaked BaseSubprocessTransport crashing the interpreter at
# shutdown).
_CANCEL_CLEANUP_GRACE_S = 5.0


class McpAborted(Exception):
    """Raised by `McpLoop.run_abortable` when the caller-supplied `abort`
    Event fires before the coroutine finished (finding 4). Same "abandoned,
    not stopped" caveat as a plain timeout applies -- a best-effort
    `.cancel()` is requested on the underlying future, but the coroutine
    may keep running on the loop."""


class ProgressKeepalive:
    """OpenCode-H9 MCP-compatibility item: "progress notifications reset
    the call timeout" -- a thread-safe 'ping' flag, usable directly as an
    `mcp.ClientSession.call_tool(..., progress_callback=...)` (an ASYNC
    callable, per the SDK's `ProgressFnT` protocol: `async def
    __call__(self, progress, total, message) -> None`), that
    `McpLoop.run_abortable`'s `keepalive=` polling loop below consumes to
    push its OWN outer deadline back out. Without this, a legitimately
    long-running MCP tool that dutifully sends progress notifications
    (Claude Code's own MCP contract explicitly allows this) still gets cut
    off by `run_abortable`'s fixed `overall` bound in manager.py's
    `call_tool`, even though the SDK's own inner `read_timeout_seconds`
    would have kept waiting -- our wrapper has no visibility into THAT
    timer resetting on its own, so it needs its own copy of the same
    signal. `ping`/`consume` cross a real thread boundary (the SDK calls
    `__call__` on the McpLoop daemon thread; `consume` is read from
    whichever thread is blocked in `run_abortable`), so both are guarded
    by a plain `threading.Lock` -- the critical section is a couple of
    attribute writes, never worth an asyncio-side primitive."""

    def __init__(self) -> None:
        self._pinged = False
        self._lock = threading.Lock()

    async def __call__(self, progress=None, total=None, message=None) -> None:
        with self._lock:
            self._pinged = True

    def consume(self) -> bool:
        """True (and clears the flag) iff `__call__` fired at least once
        since the last `consume()` -- edge-triggered, so a burst of
        several progress notifications between two polls still counts as
        exactly one deadline reset, not several."""
        with self._lock:
            was = self._pinged
            self._pinged = False
            return was

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

            thread = threading.Thread(target=_run, name="halo-mcp-loop", daemon=True)
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

    def run_abortable(self, coro: "Coroutine[Any, Any, T]", *, timeout: Optional[float] = None,
                       abort: "Optional[Any]" = None, slice_s: float = 0.2,
                       keepalive: "Optional[ProgressKeepalive]" = None) -> T:
        """Like `run()`, but polls in `slice_s`-second steps instead of one
        blocking `.result(timeout=)` call, so an externally-set `abort`
        Event (agent/loop.py's `Session.abort`, threaded through a tool's
        `ctx.abort`) cuts an in-flight MCP call short instead of holding
        the calling thread for the full timeout (finding 4: "in-flight MCP
        calls ignore ctx.abort"). Raises `McpAborted` on an abort,
        `concurrent.futures.TimeoutError` on an ordinary timeout (same as
        `run()`) -- either way the coroutine itself is only `.cancel()`-ed
        as a best-effort courtesy (see `run()`'s own docstring: asyncio has
        no safe cross-thread hard cancel of an arbitrary await), so the
        caller must treat the operation as ABANDONED, not actually stopped.
        `keepalive` (a `ProgressKeepalive`, OpenCode H9 MCP-compatibility
        item): when given, each poll ALSO checks it and, if it fired since
        the last poll, pushes `deadline` back out by the full `timeout`
        again -- a still-progressing call is never cut off by this
        wrapper's own fixed bound just because it legitimately takes
        longer than one `timeout` window."""
        loop = self._ensure_started()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        deadline = None if timeout is None else (time.monotonic() + timeout)
        try:
            while True:
                if keepalive is not None and timeout is not None and keepalive.consume():
                    deadline = time.monotonic() + timeout
                remaining = None if deadline is None else (deadline - time.monotonic())
                if remaining is not None and remaining <= 0:
                    raise concurrent.futures.TimeoutError()
                wait_s = slice_s if remaining is None else min(slice_s, remaining)
                try:
                    return fut.result(timeout=wait_s)
                except concurrent.futures.TimeoutError:
                    if abort is not None and abort.is_set():
                        raise McpAborted("mcp call aborted") from None
                    continue
        except (McpAborted, concurrent.futures.TimeoutError):
            fut.cancel()
            raise

    def wait_future_abortable(self, fut: "concurrent.futures.Future[T]", *, timeout: Optional[float] = None,
                               abort: "Optional[Any]" = None, slice_s: float = 0.2) -> T:
        """Like `run_abortable`, but for a future ALREADY scheduled onto
        this loop (typically via `spawn()`) instead of a coroutine to
        schedule now -- `McpServerHandle.start()`/`close()` (manager.py)
        use this so the reconnect-on-next-call path (and the TUI's `/mcp`
        `r` reconnect) can honour Esc/`ctx.abort` too (u2-h3b finding 9),
        not just an ordinary tool call. Same semantics/exceptions as
        `run_abortable`: `McpAborted` on abort, `concurrent.futures.
        TimeoutError` on an ordinary timeout, and the future itself is
        left running either way (this only stops WAITING on it -- see
        `run()`'s own "abandoned, not stopped" note)."""
        deadline = None if timeout is None else (time.monotonic() + timeout)
        while True:
            remaining = None if deadline is None else (deadline - time.monotonic())
            if remaining is not None and remaining <= 0:
                raise concurrent.futures.TimeoutError()
            wait_s = slice_s if remaining is None else min(slice_s, remaining)
            try:
                return fut.result(timeout=wait_s)
            except concurrent.futures.TimeoutError:
                if abort is not None and abort.is_set():
                    raise McpAborted("mcp wait aborted") from None
                continue

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
        because nobody called `.close()` on its handle, or one still
        mid-connect that `McpManager.close_all()`'s own per-handle wait
        already gave up on), giving them up to half of `timeout` to finish
        NATURALLY first -- a cancelled `_lifecycle_task`/connect-attempt
        still runs its own local-`stack`-owning cleanup (see
        `_cancel_pending_tasks`'s docstring), so this is what lets an MCP
        connection nobody explicitly closed (or couldn't finish closing in
        time) tear down its subprocess/transport BEFORE the loop itself
        stops, rather than leaving it dangling for interpreter shutdown to
        trip over. Then does exactly what `close()` does. Idempotent, like
        `close()` (a second call, or a call after `close()`, is a no-op).
        NOTE: `_cancel_pending_tasks` below can, when it actually has
        something to force-cancel, run past `timeout`'s own half -- by
        design (see its docstring); `timeout` is honoured exactly whenever
        there was nothing to clean up, which is the overwhelmingly common
        case."""
        if self._stopped:
            return
        half = max(0.1, timeout / 2.0)
        self._cancel_pending_tasks(half)
        self.close(timeout=max(0.1, timeout - half))

    def _cancel_pending_tasks(self, timeout: float) -> None:
        """Give every non-done task on this loop up to `timeout` to finish
        NATURALLY first, and only `.cancel()` whatever is still running
        once that's spent -- never the other way around. A straggler here
        can be a manager.py `McpServerHandle._lifecycle_task` still parked
        in `await self._close_event.wait()` (cancelling it is exactly what
        lets its own `finally: await stack.aclose()` run), but it can just
        as easily be a task already MID-cleanup, e.g. manager.py's own
        fire-and-forget `stack.aclose()` task for a connect attempt that
        timed out -- the installed mcp SDK's `stdio_client` shutdown there
        is deliberately shielded from cancellation (so a caller's
        cancellation can't abort it), but that shield only protects
        against cancellation propagating from an ENCLOSING scope; a raw
        `Task.cancel()` called directly on it from here, as this method
        used to do first, still throws CancelledError into it and
        interrupts the shutdown sequence before it reaches the step that
        marks the subprocess transport closed -- verified on Windows to
        reproduce the exact leaked/crashing transport this whole mechanism
        exists to prevent. Waiting first avoids that.

        H3c-regression fix (verified on Windows): cancelling a task that's
        mid-connect (inside `manager._connect_once`'s `session.
        initialize()`) doesn't close anything itself -- it makes `_do()`'s
        own `except BaseException:` handler schedule a NEW, separate
        detached cleanup task (`self._loop.spawn(_cleanup())`) and then
        let the CancelledError propagate on, ending THIS task. The old
        version called `.cancel()` and returned immediately afterward
        ("best-effort courtesy only -- not awaited further"), so that
        freshly-spawned follow-up task got no guaranteed chance to run
        before `stop()` went on to `close()` (`loop.call_soon_threadsafe
        (loop.stop)` then joining the thread) -- a real race, lost often
        enough on Windows to leave a live child process + an unclosed
        BaseSubprocessTransport for the interpreter to crash on at
        shutdown. This now RE-SCANS `asyncio.all_tasks()` after cancelling
        and keeps waiting/cancelling whatever is newly pending -- covering
        a cancel-spawns-another-task hand-off of any depth, not just one
        level -- bounded by its own `_CANCEL_CLEANUP_GRACE_S` floor (NEVER
        by however little of `timeout` happened to be left; see that
        constant's own docstring for why) so a genuinely stuck task can't
        hang this past a bounded, small grace window either. Never raises
        -- `stop()` proceeds into `close()` regardless of whether every
        task finishes in time."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return

        async def _wait_then_cancel() -> None:
            mine = asyncio.current_task()
            pending = [t for t in asyncio.all_tasks() if t is not mine and not t.done()]
            if pending:
                _done, pending = await asyncio.wait(pending, timeout=timeout)
            if not pending:
                return
            grace_deadline = time.monotonic() + _CANCEL_CLEANUP_GRACE_S
            while pending:
                for t in pending:
                    t.cancel()
                remaining = grace_deadline - time.monotonic()
                if remaining <= 0:
                    return
                # re-scan (not just re-await `pending`): a cancelled task's
                # own cleanup handler can spawn a BRAND NEW task (manager.
                # py's `_do()` does exactly this) that all_tasks() only
                # reveals on the NEXT scan.
                still = [t for t in asyncio.all_tasks() if t is not mine and not t.done()]
                if not still:
                    return
                _done, pending = await asyncio.wait(still, timeout=remaining)

        try:
            fut = asyncio.run_coroutine_threadsafe(_wait_then_cancel(), loop)
            fut.result(timeout=timeout + _CANCEL_CLEANUP_GRACE_S + 1.0)
        except Exception:
            pass
