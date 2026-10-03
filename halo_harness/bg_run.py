"""halo_harness.bg_run -- W4a CLI flags: `--bg`/`--background` (a scoped-down
v1: spawn this same invocation detached, print an id + log path; no
`attach`/`logs`/`stop`/`rm` subcommands yet -- the log file and the OS's own
process tools cover the same ground for 2.0.1) and `--tmux` (re-exec inside a
new tmux window when tmux exists; no iTerm2-native-pane backend).

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
    Python process for every invocation shape it wants to check."""
    run_id = uuid.uuid4().hex[:8]
    run_dir = _bg_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "output.log"
    meta_path = run_dir / "meta.json"

    forced_argv = list(argv)
    if "-p" not in forced_argv and "--print" not in forced_argv:
        forced_argv = forced_argv + ["-p"]
    command = [sys.executable, "-m", "halo_harness", *forced_argv]

    log_fh = open(log_path, "ab")
    try:
        proc = popen(
            command, cwd=str(Path.cwd()), stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            start_new_session=(os.name != "nt"),
        )
    finally:
        log_fh.close()  # the CHILD inherited its own duplicate fd; this process's copy is done with it

    meta = {"id": run_id, "pid": proc.pid, "command": command, "cwd": str(Path.cwd()),
            "started": time.time(), "log_path": str(log_path)}
    try:
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError:
        pass
    return {"id": run_id, "log_path": str(log_path), "pid": proc.pid, "meta_path": str(meta_path)}


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
