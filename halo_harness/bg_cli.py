"""halo_harness.bg_cli -- `halo bg list|logs|stop|rm` (W5, carried from
W4a): the read/manage half of `--bg`/`--background` over the detached
runs' own `<state>/bg/<id>/{output.log,meta.json}` files
(`halo_harness.bg_run` does the actual file/process work; this module is
just argparse + formatting, same split as `mcp_setup.py`/`mcp_cli.py`).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def _format_age(started) -> str:
    try:
        secs = max(0, int(time.time() - float(started)))
    except (TypeError, ValueError):
        return "?"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _cmd_list(rest: list) -> int:
    from halo_harness.bg_run import list_runs, pid_alive

    parser = argparse.ArgumentParser(prog="halo bg list", add_help=True)
    parser.parse_args(rest)
    runs = list_runs()
    if not runs:
        print("No background runs.")
        return 0
    for meta in runs:
        pid = meta.get("pid")
        status = "running" if pid_alive(pid) else "exited"
        cmd_str = " ".join(str(a) for a in (meta.get("command") or []))
        print(f"{meta.get('id', '?'):10} {status:8} pid={pid} age={_format_age(meta.get('started')):4} {cmd_str}")
    return 0


def _cmd_logs(rest: list) -> int:
    from halo_harness.bg_run import load_run

    parser = argparse.ArgumentParser(prog="halo bg logs", add_help=True)
    parser.add_argument("id")
    parser.add_argument("-n", "--lines", type=int, default=None,
                         help="show only the last N lines (default: the whole log)")
    args = parser.parse_args(rest)
    meta = load_run(args.id)
    if meta is None:
        print(f"halo bg logs: unknown id {args.id!r}", file=sys.stderr)
        return 1
    log_path = Path(meta.get("log_path") or (Path(meta["run_dir"]) / "output.log"))
    if not log_path.is_file():
        print(f"halo bg logs: no log file at {log_path}", file=sys.stderr)
        return 1
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"halo bg logs: could not read {log_path} ({e})", file=sys.stderr)
        return 1
    if args.lines is not None:
        text = "\n".join(text.splitlines()[-args.lines:])
    print(text)
    return 0


def _cmd_stop(rest: list) -> int:
    from halo_harness.bg_run import kill_pid, load_run, pid_alive

    parser = argparse.ArgumentParser(prog="halo bg stop", add_help=True)
    parser.add_argument("id")
    args = parser.parse_args(rest)
    meta = load_run(args.id)
    if meta is None:
        print(f"halo bg stop: unknown id {args.id!r}", file=sys.stderr)
        return 1
    pid = meta.get("pid")
    if not pid_alive(pid):
        print(f"halo bg stop: {args.id} is not running (already exited)")
        return 0
    if kill_pid(pid):
        print(f"halo: stopped background run {args.id} (pid {pid})")
        return 0
    print(f"halo bg stop: could not confirm {args.id} (pid {pid}) actually stopped", file=sys.stderr)
    return 1


def _cmd_rm(rest: list) -> int:
    from halo_harness.bg_run import kill_pid, load_run, pid_alive

    parser = argparse.ArgumentParser(prog="halo bg rm", add_help=True)
    parser.add_argument("id")
    parser.add_argument("--force", action="store_true",
                         help="stop it first if still running, instead of refusing")
    args = parser.parse_args(rest)
    meta = load_run(args.id)
    if meta is None:
        print(f"halo bg rm: unknown id {args.id!r}", file=sys.stderr)
        return 1
    pid = meta.get("pid")
    if pid_alive(pid):
        if not args.force:
            print(f"halo bg rm: {args.id} is still running (pid {pid}) -- pass --force to stop and remove it",
                  file=sys.stderr)
            return 1
        kill_pid(pid)
    import shutil
    shutil.rmtree(meta["run_dir"], ignore_errors=True)
    print(f"halo: removed background run {args.id}")
    return 0


_SUBCOMMANDS = {"list": _cmd_list, "logs": _cmd_logs, "stop": _cmd_stop, "rm": _cmd_rm}


def cmd_bg(argv: list) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: halo bg list|logs <id>|stop <id>|rm <id> [--force]", file=sys.stderr)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    sub, rest = argv[0], argv[1:]
    fn = _SUBCOMMANDS.get(sub)
    if fn is None:
        print(f"halo bg: unknown subcommand {sub!r} (known: {', '.join(sorted(_SUBCOMMANDS))})", file=sys.stderr)
        return 2
    return fn(rest)
