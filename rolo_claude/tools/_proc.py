"""rolo_claude.tools._proc -- shared subprocess execution for Bash/
PowerShell (H2 scope A): merged stdout+stderr streamed to a progress
callback roughly every 0.5s, process-GROUP kill on timeout/abort (never
just the one visible child -- a shell that forked children of its own must
not survive its parent's kill), abort-aware, cross-platform.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

POLL_INTERVAL_S = 0.5


def _kill_process_group(proc: "subprocess.Popen") -> None:
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                            capture_output=True, timeout=5)
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
) -> "tuple[str, Optional[int], bool, bool]":
    """Run `argv`, return (merged_output, exit_code, timed_out, aborted).
    `exit_code` is None only when the process itself could never be
    launched (`merged_output` is then a plain error message, not real
    subprocess output). `abort` is a threading.Event polled alongside the
    timeout; either firing kills the whole process group, never just the
    immediate child."""
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

    chunks: list = []
    pending: list = []
    last_flush = time.monotonic()
    deadline = (time.monotonic() + timeout_s) if timeout_s and timeout_s > 0 else None
    timed_out = False
    aborted = False
    stream_closed = False

    while True:
        try:
            item = q.get(timeout=0.1)
            if item is None:
                stream_closed = True
            else:
                chunks.append(item)
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
        if deadline is not None and now >= deadline:
            timed_out = True
            break
        if abort is not None and abort.is_set():
            aborted = True
            break

    if timed_out or aborted:
        _kill_process_group(proc)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    else:
        proc.wait()

    return "".join(chunks), proc.returncode, timed_out, aborted
