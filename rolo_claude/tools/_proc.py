"""rolo_claude.tools._proc -- shared subprocess execution for Bash/
PowerShell (H2 scope A): merged stdout+stderr streamed to a progress
callback roughly every 0.5s, process-GROUP kill on timeout/abort (never
just the one visible child -- a shell that forked children of its own must
not survive its parent's kill), abort-aware, cross-platform.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

POLL_INTERVAL_S = 0.5
# finding 7: `cat huge.log` / `yes` must never grow memory unbounded --
# collection is capped at this many characters TOTAL; once exceeded, only
# the head and tail (half each) are kept in RAM and the full raw output is
# spilled, incrementally, to a temp file instead (never held twice).
_BUFFER_CAP_CHARS = 300_000
_DRAIN_AFTER_EXIT_S = 0.2  # finding 7: bounded drain once the process itself has exited
_WAIT_BOUND_S = 5.0


class _CappedCollector:
    """Accumulates streamed text with a hard cap on what stays in memory:
    the full text up to `_BUFFER_CAP_CHARS`, then head+tail only (spilling
    every chunk to a temp file as it arrives, so nothing is ever lost --
    just not double-held in RAM)."""

    def __init__(self):
        self.total_len = 0
        self._head: list = []
        self._head_len = 0
        self._tail = ""
        self._spill_file = None
        self._spill_path: Optional[Path] = None

    def append(self, text: str) -> None:
        if not text:
            return
        self.total_len += len(text)
        half = _BUFFER_CAP_CHARS // 2
        if self._head_len < half:
            self._head.append(text)
            self._head_len += len(text)
        else:
            if self._spill_file is None:
                fd, name = tempfile.mkstemp(prefix="rolo-claude-proc-", suffix=".log")
                self._spill_file = os.fdopen(fd, "w", encoding="utf-8", errors="replace")
                self._spill_path = Path(name)
                self._spill_file.write("".join(self._head))
            elif self._spill_file.closed:
                # H8 scope A: a backgrounded job's collector keeps being
                # appended to (agent.jobs.JobRegistry's drain thread) well
                # after `result()` -- called once for run_streamed's own
                # return value at the moment of a timeout handoff -- has
                # already closed the file below. Re-open in append mode
                # rather than silently losing every byte written after
                # that first `result()` call (`except Exception: pass`
                # below would otherwise swallow every write to a closed
                # file with no error and no data).
                try:
                    self._spill_file = open(self._spill_path, "a", encoding="utf-8", errors="replace")
                except OSError:
                    pass
            try:
                self._spill_file.write(text)
            except Exception:
                pass
            self._tail = (self._tail + text)[-half:]

    # H9 whole-tree review finding 6: small public accessors so a caller
    # (agent/jobs.py's JobRegistry.poll) can compute an INCREMENTAL
    # "what's new since my last read" slice correctly even once spilling
    # has started, instead of naively diffing against `len(result())`
    # (which stays roughly constant once spilling begins, since `result()`
    # is head+tail only -- see that finding's fix in jobs.py for why this
    # matters).
    @property
    def tail_start(self) -> int:
        """Absolute character position where the CURRENT tail begins (i.e.
        `total_len` if nothing has ever spilled -- there IS no separate
        tail concept then, `result()` already carries everything)."""
        return self.total_len - len(self._tail)

    @property
    def spill_path(self) -> "Optional[Path]":
        return self._spill_path

    @property
    def head_len(self) -> int:
        return self._head_len

    @property
    def tail_text(self) -> str:
        return self._tail

    def result(self) -> str:
        """Safe to call more than once (H8 scope A: a background job's
        collector is polled repeatedly) -- each call flushes/closes the
        spill file as a consistent snapshot; `append` re-opens it (above)
        if more text arrives afterward."""
        if self._spill_file is not None:
            try:
                self._spill_file.flush()
                if not self._spill_file.closed:
                    self._spill_file.close()
            except Exception:
                pass
            omitted = self.total_len - self._head_len - len(self._tail)
            pointer = (f"\n\n... [{omitted} characters omitted -- output exceeded the "
                       f"{_BUFFER_CAP_CHARS}-char in-memory cap; full output saved to {self._spill_path}]\n\n")
            return "".join(self._head) + pointer + self._tail
        return "".join(self._head)


_TASKKILL_UNTERMINATED_RE = re.compile(r"process with PID (\d+).*?could not be terminated", re.IGNORECASE)


def _kill_process_tree_windows(pid: int) -> None:
    """H8 scope A finding: `taskkill /T /F /PID <pid>` can report "ERROR:
    The process with PID <n> (child process of PID <pid>) could not be
    terminated. Reason: The operation attempted is not supported." for a
    GRANDCHILD spawned only milliseconds earlier (observed: a background
    job killed essentially the instant it started -- e.g. TaskStop called
    right after Bash's own run_in_background reply). The named process
    (bash.exe itself, per `proc.poll()`) genuinely dies, but that orphaned
    grandchild survives and keeps the child's stdout PIPE open for its own
    full natural lifetime -- Windows only signals EOF once every write-end
    handle, across every process, is closed, so a reader thread blocks
    until the orphan exits on its own instead of seeing this as a kill.
    Retried a few times, targeting whatever taskkill's own stderr still
    names as un-terminated each round: it reliably succeeds once that PID
    has finished registering with the OS, typically within tens of ms."""
    targets = {pid}
    for _attempt in range(5):
        still_stuck = set()
        for target in targets:
            try:
                result = subprocess.run(["taskkill", "/T", "/F", "/PID", str(target)],
                                         capture_output=True, text=True, timeout=5)
            except Exception:
                continue
            for m in _TASKKILL_UNTERMINATED_RE.finditer(result.stderr or ""):
                still_stuck.add(int(m.group(1)))
        if not still_stuck:
            return
        targets = still_stuck
        time.sleep(0.2)


def _kill_process_group(proc: "subprocess.Popen") -> None:
    if os.name == "nt":
        try:
            _kill_process_tree_windows(proc.pid)
            return
        except Exception:
            pass
    else:
        try:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def run_streamed(
    argv: list, *, cwd, env: dict, timeout_s: float, abort=None,
    progress_cb: Optional[Callable[[str], None]] = None, poll_interval: float = POLL_INTERVAL_S,
    on_timeout_handoff: Optional[Callable[["subprocess.Popen", "queue.Queue", "_CappedCollector"], None]] = None,
) -> "tuple[str, Optional[int], bool, bool]":
    """Run `argv`, return (merged_output, exit_code, timed_out, aborted).
    `exit_code` is None only when the process itself could never be
    launched (`merged_output` is then a plain error message, not real
    subprocess output), OR when a timeout was handed off (see below) --
    the process is still running, ownership just moved elsewhere. `abort`
    is a threading.Event polled alongside the timeout; either firing kills
    the whole process group, never just the immediate child.

    finding 7: completion is keyed on the PROCESS exiting (`proc.poll()`),
    never on stdout EOF -- a backgrounded child (`sleep 100 &`) keeps the
    pipe's write end open long after the wrapper shell itself has exited,
    so waiting for EOF blocks for the full timeout even though the
    foreground command finished immediately. Once the process has exited,
    only a short bounded drain (`_DRAIN_AFTER_EXIT_S`) collects whatever
    was already flushed to the pipe before returning -- the process group
    is never killed just because it happened to leave a background job
    running.

    H8 scope A (dsh's agent-loop README, quoted in H8-brief.md): "a
    foreground command that hits its timeout is moved to the background
    rather than killed" -- when `on_timeout_handoff` is given AND a real
    TIMEOUT (never an abort) is what ends the wait, it is called with the
    still-live `(proc, q, collector)` instead of killing anything, and this
    function returns immediately WITHOUT touching any of the three again
    (a clean single-writer handoff: the caller, agent.jobs.JobRegistry,
    takes over draining `q` into `collector` and owns `proc` from that
    point on). `timed_out` is still True in the returned tuple so a caller
    that passed no callback keeps seeing today's exact behaviour."""
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            start_new_session=(os.name != "nt"),
        )
    except OSError as e:
        return f"Error launching process: {e}", None, False, False

    q: "queue.Queue" = queue.Queue()

    def _reader() -> None:
        try:
            for line in iter(proc.stdout.readline, ""):
                q.put(line)
        except Exception:
            pass
        finally:
            q.put(None)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    collector = _CappedCollector()
    pending: list = []
    last_flush = time.monotonic()
    deadline = (time.monotonic() + timeout_s) if timeout_s and timeout_s > 0 else None
    timed_out = False
    aborted = False
    stream_closed = False
    drain_deadline: Optional[float] = None

    while True:
        try:
            item = q.get(timeout=0.1)
            if item is None:
                stream_closed = True
            else:
                collector.append(item)
                pending.append(item)
        except queue.Empty:
            pass

        now = time.monotonic()
        if pending and (stream_closed or now - last_flush >= poll_interval):
            if progress_cb is not None:
                try:
                    progress_cb("".join(pending))
                except Exception:
                    pass
            pending = []
            last_flush = now

        if stream_closed:
            break
        if drain_deadline is None and proc.poll() is not None:
            drain_deadline = now + _DRAIN_AFTER_EXIT_S  # finding 7: bounded post-exit drain, not a hang on EOF
        if drain_deadline is not None and now >= drain_deadline:
            break
        if deadline is not None and now >= deadline:
            timed_out = True
            break
        if abort is not None and abort.is_set():
            aborted = True
            break

    if pending and progress_cb is not None:
        try:
            progress_cb("".join(pending))
        except Exception:
            pass

    if timed_out and not aborted and on_timeout_handoff is not None:
        try:
            on_timeout_handoff(proc, q, collector)
        except Exception:
            pass
        return collector.result(), None, timed_out, aborted

    if timed_out or aborted:
        _kill_process_group(proc)
        try:
            proc.wait(timeout=_WAIT_BOUND_S)
        except Exception:
            pass
    else:
        # finding 7: bounded even on the clean-exit/drained path -- never
        # an uncapped blocking wait().
        try:
            proc.wait(timeout=_WAIT_BOUND_S)
        except Exception:
            pass

    return collector.result(), proc.returncode, timed_out, aborted
