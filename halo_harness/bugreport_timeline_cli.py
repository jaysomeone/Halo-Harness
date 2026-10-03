"""halo_harness.bugreport_timeline_cli -- `halo timeline --last N` (2.0.1
W3a): reads the per-turn timeline records `agent.loop.Session.turn`'s own
wrapper writes to the session log (one `meta` node per turn, key
`timeline` -- `debug_timeline.py`'s own in-memory deque is per-PROCESS, so
a separate `halo timeline` invocation started after the fact has nothing
else to read). `/timeline` in the TUI/`-p` is a simpler, DIFFERENT path
(same process as the live session -- `commands/builtins.py::_cmd_timeline`)
that reads `debug_timeline.last_n_turns()` directly, no log file needed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def read_timeline_records(cwd: Path, session_arg: "str | None" = None) -> list:
    from halo_harness.agent import sessions as agent_sessions
    from halo_harness.agent.log import SessionLog
    if session_arg:
        session_id, err = agent_sessions.resolve_resume(cwd, session_arg)
        if session_id is None:
            return []
        log = SessionLog(cwd, session_id=session_id)
    else:
        log = SessionLog.latest_for_cwd(cwd)
        if log is None:
            return []
    try:
        nodes = log.read_all()
    except Exception:
        return []
    return [n["timeline"] for n in nodes if n.get("type") == "meta" and isinstance(n.get("timeline"), dict)]


def format_timeline_record(record: dict) -> str:
    """Parity gap: the hooks/permission-waits/compactions W3b item 11
    actually recorded (`debug_timeline.TurnTimeline.start_turn`'s own
    `_current` dict) only ever showed up in `--json`/the bugreport's own
    `json.dumps(record, ...)` dump -- this text form (`/timeline` and
    `halo timeline` without `--json`) never rendered any of the three."""
    lines = [f"Turn {record.get('turn')}:"]
    for key in ("request_sent_ms", "headers_ms", "first_reasoning_ms", "first_text_ms",
                "first_tool_call_ms", "message_end_ms"):
        value = record.get(key)
        if value is not None:
            lines.append(f"  {key}: +{value}ms")
    for tool in record.get("tools") or []:
        lines.append(f"  tool {tool.get('name')}: {tool.get('status')} "
                     f"(start +{tool.get('start_ms')}ms, end +{tool.get('end_ms')}ms)")
    for steer in record.get("steers") or []:
        lines.append(f"  steer: {steer!r}")
    for retry in record.get("retries") or []:
        lines.append(f"  error: {retry.get('status')} {retry.get('error')} (+{retry.get('ms')}ms)")
    for hook in record.get("hooks") or []:
        lines.append(f"  hook {hook.get('event')}: +{hook.get('ms')}ms ({hook.get('duration_ms')}ms)")
    for wait in record.get("permission_waits") or []:
        lines.append(f"  permission_wait: {wait.get('decision')} "
                     f"(+{wait.get('start_ms')}ms -> +{wait.get('end_ms')}ms)")
    for compaction in record.get("compactions") or []:
        bits = [f"compaction {compaction.get('phase')}: +{compaction.get('ms')}ms"]
        if compaction.get("trigger"):
            bits.append(f"trigger={compaction.get('trigger')}")
        if compaction.get("phase") == "done":
            bits.append(f"{compaction.get('tokens_before')}->{compaction.get('tokens_after')} tokens")
        elif compaction.get("phase") == "failed":
            bits.append(f"reason={compaction.get('reason')}")
        lines.append(f"  {' '.join(bits)}")
    return "\n".join(lines)


def cmd_timeline(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="halo timeline", add_help=True,
                                      description="Show the per-turn request/tool timing timeline for a session.")
    parser.add_argument("--last", type=int, default=1, metavar="N", help="Turns to show (default 1, the most recent)")
    parser.add_argument("--session", default=None, metavar="ID")
    parser.add_argument("--cwd", default=None, metavar="DIR")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of formatted text")
    args = parser.parse_args(argv)

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    records = read_timeline_records(cwd, args.session)
    if not records:
        print("halo timeline: no recorded turns for this session", file=sys.stderr)
        return 1
    wanted = records[-max(args.last, 1):]
    if args.json:
        print(json.dumps(wanted, indent=2, sort_keys=True, default=str))
    else:
        print("\n\n".join(format_timeline_record(r) for r in wanted))
    return 0
