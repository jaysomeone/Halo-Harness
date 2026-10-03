"""halo_harness.bg_run -- W4a/W5 CLI flags: `--bg`/`--background` (spawn
this same invocation detached, print an id + log path, state under
`<state>/bg/<id>/{output.log,meta.json}`) and `--tmux` (re-exec inside a
new tmux window when tmux exists; no iTerm2-native-pane backend). W5
(carried from W4a) adds the read/manage side over those SAME run
directories -- `list_runs`/`pid_alive`/`kill_pid` below, driven by
`bg_cli.cmd_bg`'s own `halo bg list|logs|stop|rm` (kept in a separate
module, same split as `mcp_setup.py`/`mcp_cli.py`: this file stays
argparse-free and directly unit-testable).

Kept as its own module (not inline in cli.py) so both are unit-testable
without going through argparse/subprocess-spawning-a-real-python at all --
`start_background_run`'s own argv-to-Popen-args translation and `run_in_tmux`'s
tmux-missing fallback are the parts worth pinning directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional


def _bg_root() -> Path:
    from halo_harness.config.paths import bridge_home
    return bridge_home() / "bg"


def start_background_run(argv: list, *, popen=subprocess.Popen) -> dict:
    """Spawns `[sys.executable, -m halo_harness] + argv` (forcing `-p` when
    neither `-p` nor `--print` is already present -- a detached child has no
    terminal a TUI could ever render into), stdout+stderr merged into
    `<state>/bg/<id>/output.log`, stdin closed. Returns `{"id", "log_path",
    "pid", "meta_path"}` immediately -- never waits for the child. `popen`
    is a seam: a test substitutes a fake to avoid actually launching a real
    Python process for every invocation shape it wants to check.

    finding 15 (W6a): the REAL command is launched through `_bg_wrapper`
    (this process's own pid, recorded in `meta.json`, is the WRAPPER's --
    killing its group takes the real command with it, unchanged from
    before) so it can write `status.json` once the real command actually
    exits; `meta.json` also records `start_time` (`process_start_time`),
    so a PID the OS later reuses for something unrelated is never
    mistaken for this run still being alive."""
    run_id = uuid.uuid4().hex[:8]
    run_dir = _bg_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "output.log"
    meta_path = run_dir / "meta.json"
    status_path = run_dir / "status.json"

    forced_argv = list(argv)
    if "-p" not in forced_argv and "--print" not in forced_argv:
        forced_argv = forced_argv + ["-p"]
    real_command = [sys.executable, "-m", "halo_harness", *forced_argv]
    command = [sys.executable, "-m", "halo_harness._bg_wrapper", str(status_path), *real_command]

    log_fh = open(log_path, "ab")
    try:
        proc = popen(
            command, cwd=str(Path.cwd()), stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            # review finding 37: CREATE_NEW_PROCESS_GROUP alone still
            # leaves this child attached to the launching console --
            # Windows sends CTRL_CLOSE_EVENT to every process attached to
            # a closing console, killing it the moment that terminal
            # window closes, unlike the POSIX path's `start_new_session`
            # (a real new session, survives the launching terminal
            # closing by construction). DETACHED_PROCESS is what actually
            # detaches it from the console, same intent as `start_new_
            # session` on POSIX.
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                           if os.name == "nt" else 0),
            start_new_session=(os.name != "nt"),
        )
    finally:
        log_fh.close()  # the CHILD inherited its own duplicate fd; this process's copy is done with it

    meta = {"id": run_id, "pid": proc.pid, "command": real_command, "cwd": str(Path.cwd()),
            "started": time.time(), "log_path": str(log_path), "status_path": str(status_path),
            "start_time": process_start_time(proc.pid)}
    try:
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError:
        pass
    return {"id": run_id, "log_path": str(log_path), "pid": proc.pid, "meta_path": str(meta_path)}


def process_start_time(pid: int) -> Optional[str]:
    """finding 15 (W6a): an opaque, re-readable "when did THIS pid start"
    fingerprint -- never a real timestamp a caller should interpret, just
    a value that stays the same for the SAME still-running process and
    differs once the OS reuses that pid for something else entirely.
    `/proc/<pid>/stat` field 22 (starttime) on Linux; the creation
    `FILETIME` from `GetProcessTimes` on Windows. `None` when it can't be
    determined at all (an unsupported platform, or the process is already
    gone) -- callers treat that as "can't tell, don't refuse on it
    alone", same as a pre-upgrade `meta.json` with no `start_time` key."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        return _windows_process_start_time(pid)
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        # `comm` (field 2, in parens) can itself contain spaces/parens --
        # same `rsplit(")", 1)` trick `pid_alive` above already uses for
        # the state field. Fields from "state" (3) onward are then
        # `after[0]`, `after[1]`, ...; starttime is field 22, so index 19.
        after = content.rsplit(")", 1)[-1].split()
        return after[19]
    except (OSError, IndexError, ValueError):
        return None


def _windows_process_start_time(pid: int) -> Optional[str]:
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            creation, exit_t, kernel_t, user_t = (wintypes.FILETIME(), wintypes.FILETIME(),
                                                     wintypes.FILETIME(), wintypes.FILETIME())
            ok = kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_t),
                                            ctypes.byref(kernel_t), ctypes.byref(user_t))
            if not ok:
                return None
            return str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def is_our_process(meta: dict) -> bool:
    """finding 15 (W6a): `pid_alive(meta["pid"])` alone answers "is SOME
    process using this pid right now" -- true even once the OS has
    reassigned it to something this run never spawned. Refuses that case
    whenever `meta["start_time"]` (recorded at spawn) and a FRESH
    `process_start_time` for the SAME pid disagree; a run recorded before
    this field existed, or a platform/moment where it can't be read at
    all, falls back to the pid-only check rather than a false refusal."""
    pid = meta.get("pid")
    if not pid_alive(pid):
        return False
    expected = meta.get("start_time")
    if not expected:
        return True
    current = process_start_time(pid)
    return current is None or current == expected


def list_runs() -> "list[dict]":
    """Every `<state>/bg/<id>/meta.json`, oldest first, each with its own
    directory under `run_dir` -- a run directory with no readable
    `meta.json` (never written, or corrupted) is silently skipped, same
    degrade-gracefully contract every small-JSON-file loader in this
    codebase already has."""
    root = _bg_root()
    if not root.is_dir():
        return []
    out: "list[dict]" = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        meta = dict(meta)
        meta["run_dir"] = str(d)
        out.append(meta)
    return out


def load_run(run_id: str) -> Optional[dict]:
    """One run's own meta dict (plus `run_dir`), or `None` for an unknown
    id -- never raises."""
    try:
        meta = json.loads((_bg_root() / run_id / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    meta = dict(meta)
    meta["run_dir"] = str(_bg_root() / run_id)
    return meta


def pid_alive(pid: int) -> bool:
    """Best-effort liveness for a PID from a PAST process this one never
    spawned (no live `Popen` handle to `.poll()`) -- `tasklist` on Windows
    (no extra dependency; `psutil` isn't in this project's requirements),
    a signal-0 probe on POSIX."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                      capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else -- still alive
    except OSError:
        return False
    # A zombie (exited, not yet reaped) still answers kill(pid, 0); Linux
    # exposes its state in /proc, and a zombie is not alive for our purposes.
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as fh:
            state = fh.read().rsplit(")", 1)[-1].split()[0]
        return state != "Z"
    except (OSError, IndexError, ValueError):
        return True
    except OSError:
        return False


def kill_pid(pid: int) -> bool:
    """Best-effort kill of a background run's own process TREE by bare PID
    (`halo bg stop`/`rm --force`) -- reuses `tools/_proc.py`'s own Windows
    orphan-grandchild-aware tree kill (the SAME one `agent.jobs.JobRegistry.
    kill` already uses for a live Bash job, just driven by a bare pid here
    instead of a live `Popen` handle, since this process never spawned the
    one it's stopping). Never raises; returns whether the process looks
    gone afterward."""
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return False  # never ourselves
    if os.name == "nt":
        try:
            from halo_harness.tools._proc import _kill_process_tree_windows
            _kill_process_tree_windows(pid)
        except Exception:
            pass
    else:
        import signal
        # A real `--bg` run lives in its own session (start_new_session), so
        # killing its process group is the right tree kill. A pid that is NOT
        # in its own group -- one that shares THIS process's group -- must be
        # killed alone: killpg there would take down the caller too (seen on
        # Linux: a test's plain child in the suite runner's own group ended
        # the whole runner, its wrapper shell and the SSH session).
        try:
            target_pgid = os.getpgid(pid)
        except (ProcessLookupError, OSError):
            target_pgid = None
        own_pgid = os.getpgid(0)
        try:
            if target_pgid is not None and target_pgid != own_pgid and target_pgid == pid:
                os.killpg(target_pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        # Reap it when it is our own child so it does not linger as a zombie
        # (a pid we never spawned is not ours to wait for).
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, OSError):
            pass
    return not pid_alive(pid)


def run_in_tmux(argv: list, *, which=None, call=subprocess.call) -> Optional[int]:
    """`None` when `tmux` isn't on PATH (the caller falls back to an
    ordinary in-process launch and prints its own notice) -- otherwise
    blocks for the whole tmux session's life and returns its exit code,
    exactly like the in-process launch it replaces would have."""
    import shutil
    which = which or shutil.which
    tmux_path = which("tmux")
    if not tmux_path:
        return None
    command = [tmux_path, "new-session", sys.executable, "-m", "halo_harness", *argv]
    return call(command)
