"""rolo_claude.statusline -- runs the `statusLine` command from settings
(U5 scope D), matching Claude Code's own contract: a `{"type": "command",
"command": "...", "padding": N}` object (`Settings.statusline`,
`config/settings.py`) is spawned once per refresh with a JSON payload on
stdin describing the current session, and its stdout (first line, ANSI
allowed) becomes the status bar's rightmost segment.

Pure I/O + JSON shaping -- no textual import, so it's unit-testable without
a running App; `tui/app.py` is the only caller, and always runs it on a
worker thread (a status-line script is an arbitrary user command with no
timeout guarantee from us to rely on).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

DEFAULT_TIMEOUT_S = 10.0


def build_payload(
    *, session_id: str, cwd: str, model_id: str, model_display: Optional[str] = None,
    transcript_path: Optional[str] = None, version: str = "", output_style: str = "default",
    cost_usd: Optional[float] = None, duration_ms: Optional[int] = None,
    api_duration_ms: Optional[int] = None, lines_added: int = 0, lines_removed: int = 0,
    project_dir: Optional[str] = None,
) -> dict:
    """Claude Code's own `statusLine` stdin JSON shape: `hook_event_name`,
    `session_id`, `transcript_path`, `cwd`, `model: {id, display_name}`,
    `workspace: {current_dir, project_dir}`, `version`, `output_style:
    {name}`, `cost: {total_cost_usd, total_duration_ms,
    total_api_duration_ms, total_lines_added, total_lines_removed}` (per
    the docs' statusLine JSON input contract). `project_dir` defaults to
    `cwd` (no separate worktree/subdirectory tracked in this build)."""
    return {
        "hook_event_name": "Status",
        "session_id": session_id,
        "transcript_path": transcript_path or "",
        "cwd": cwd,
        "model": {"id": model_id, "display_name": model_display or model_id},
        "workspace": {"current_dir": cwd, "project_dir": project_dir or cwd},
        "version": version,
        "output_style": {"name": output_style},
        "cost": {
            "total_cost_usd": cost_usd if cost_usd is not None else 0.0,
            "total_duration_ms": duration_ms or 0,
            "total_api_duration_ms": api_duration_ms or 0,
            "total_lines_added": lines_added,
            "total_lines_removed": lines_removed,
        },
    }


def run_statusline_command(command: str, payload: dict, *, cwd: "Path | str",
                            timeout_s: float = DEFAULT_TIMEOUT_S) -> Optional[str]:
    """Spawn `command` through the shell, feed `payload` as JSON on stdin,
    return its stdout (ANSI/color codes preserved verbatim -- the status
    bar renders them via `rich.text.Text.from_ansi`), trimmed of a single
    trailing newline. `None` on any failure (missing command, non-zero
    exit, timeout, ...) -- a broken user script must never crash or block
    the status bar, it just leaves the segment blank for that refresh."""
    if not command or not command.strip():
        return None
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(cwd), input=json.dumps(payload), capture_output=True,
            text=True, timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout
    if out is None:
        return None
    return out.rstrip("\n") or None
