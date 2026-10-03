"""halo_harness._bg_wrapper -- finding 15 (W6a release fix pass): the
detached `--bg` child is launched through THIS script instead of
directly, so it can write its own exit-status file once the real command
finishes. `bg_cli.py`'s `list`/`stop`/`rm` otherwise have no way to tell
"exited cleanly" from "still running" except a bare PID -- which the OS
can and does reuse for something else entirely once it exits.

Usage (built by `bg_run.start_background_run`, never run by hand):
    <python> -m halo_harness._bg_wrapper <status_path> <python> -m halo_harness <args...>

argv[0] is the status file to write; the REST is the real command, run as
this script's own child (inheriting this process's already-redirected
stdio, so the launcher's merged-log-file redirection applies to it
unchanged). This process's own PID -- not the real command's -- is what
`start_background_run` records in `meta.json` and what `kill_pid`/
`pid_alive` act on; killing its process GROUP (the launcher already
spawns it with `start_new_session`/`CREATE_NEW_PROCESS_GROUP`) takes the
real command down with it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time


def main(argv: list) -> int:
    if len(argv) < 2:
        return 2
    status_path, real_argv = argv[0], argv[1:]
    try:
        proc = subprocess.Popen(real_argv)
    except OSError as e:
        try:
            with open(status_path, "w", encoding="utf-8") as f:
                json.dump({"exit_code": None, "ended": time.time(), "launch_error": str(e)}, f)
        except OSError:
            pass
        return 1
    code = proc.wait()
    try:
        with open(status_path, "w", encoding="utf-8") as f:
            json.dump({"exit_code": code, "ended": time.time()}, f)
    except OSError:
        pass
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
