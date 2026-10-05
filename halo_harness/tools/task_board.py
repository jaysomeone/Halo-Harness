"""halo_harness.tools.task_board -- the shared task board (Halo 2.0.2
round 3, brief C item 3): `TaskCreate`/`TaskUpdate`/`TaskList`, a tiny
JSON-backed TODO list at `<top_session_dir>/tasks.json` that EVERY
sub-agent of the same session reads and writes through -- unlike
`TodoWriteTool` (one model's own private, whole-list-replacing todos),
this is shared: an org's root creates entries, its workers claim and
report on them. Fields per task: id, title, status
(open|claimed|done|blocked), owner (a free-text agent/position name the
caller supplies -- nothing here tracks "who am I" on its own), notes,
result (a free-text pointer, e.g. a file path or a one-line summary),
created/updated timestamps.

A child session's own `ctx.session_dir` is `<top>/subagents/agent-<id>
[/subagents/agent-<id>...]` (agent/subagent.py's `_child_log_paths`) --
`_top_session_dir` strips every trailing `subagents/agent-*` hop so the
WHOLE tree, at any depth, lands on the exact same file. A single
process-wide lock serializes every read-modify-write cycle (claims from
two sub-agents racing for the same task resolve to exactly one winner,
never a lost update) -- the file itself is small and these are not
hot-path calls, so one coarse lock costs nothing.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from halo_harness.tools.base import Tool, ToolContext, ToolResult

_STATUSES = ("open", "claimed", "done", "blocked")
# RLock, not Lock: TaskCreateTool/TaskUpdateTool.run() hold this across
# their own whole read-modify-write cycle and call the PUBLIC `read_board`
# (which also takes it) from inside that same section, on the SAME
# thread -- a plain Lock would deadlock on that self-reacquisition. Only a
# genuinely DIFFERENT thread (two sub-agents racing to claim the same
# task) ever actually blocks on this.
_lock = threading.RLock()


def _top_session_dir(session_dir: Path) -> Path:
    """The TOP-level session's own directory, given ANY descendant's
    `ctx.session_dir` -- everything from the first `subagents` path
    component onward is a nested sub-agent hop, stripped unconditionally
    (at any depth); a bare top-level `ctx.session_dir` (no `subagents`
    component at all) is returned unchanged."""
    parts = Path(session_dir).parts
    try:
        idx = parts.index("subagents")
    except ValueError:
        return Path(session_dir)
    return Path(*parts[:idx]) if idx > 0 else Path(session_dir)


def _board_path(session_dir: Path) -> Path:
    return _top_session_dir(session_dir) / "tasks.json"


def read_board(session_dir: Path) -> list:
    """`[]` on a missing/unreadable/malformed file -- a fresh session with
    no board yet is not an error."""
    path = _board_path(session_dir)
    with _lock:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
    tasks = data.get("tasks") if isinstance(data, dict) else None
    return tasks if isinstance(tasks, list) else []


def _write_board(session_dir: Path, tasks: list) -> None:
    """2.0.2 review finding 34: a plain `write_text` can leave a TORN
    (partially-written) file behind if this process is killed mid-write
    -- the next `read_board` would then see a JSON parse failure and
    silently degrade to `[]`, and the next `create_task` would happily
    "fix" that by writing a brand new single-task board over it, losing
    every task that was already there. Written to a tmp file and
    `os.replace`d instead (atomic on both POSIX and Windows -- the same
    pattern `providers/learned_rules.py`'s own writer and `update.py`'s
    `_save_cache` already use), so a reader only ever sees either the
    complete old file or the complete new one, never a partial write."""
    path = _board_path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp_path.write_text(json.dumps({"tasks": tasks}, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp_path, path)


def _read_board_for_write(session_dir: Path) -> "tuple[list, Optional[str]]":
    """Like `read_board`, but DISTINGUISHES "no board file yet at all"
    (`([], None)`, the ordinary case for a brand-new session) from "a
    board file exists but failed to parse" (`([], <error text>)` -- a
    torn write from before this finding's fix, or genuine corruption).
    Only a caller about to WRITE the board needs this distinction --
    `read_board`/`TaskListTool` stay "an unreadable board just shows
    empty," never an error, same as before. `create_task` (the one path
    that would otherwise silently replace a corrupt file with a single
    new task, discarding everything else that was in it) uses this
    instead, and backs up rather than overwrites when `error` is set."""
    path = _board_path(session_dir)
    with _lock:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return [], None  # genuinely nothing there yet -- not an error
        try:
            data = json.loads(text)
        except ValueError as e:
            return [], f"{type(e).__name__}: {e}"
    tasks = data.get("tasks") if isinstance(data, dict) else None
    return (tasks if isinstance(tasks, list) else []), None


def _format_task(t: dict) -> str:
    owner = f" owner={t['owner']}" if t.get("owner") else ""
    notes = f" notes={t['notes']!r}" if t.get("notes") else ""
    result = f" result={t['result']!r}" if t.get("result") else ""
    parent = f" parent={t['parent']}" if t.get("parent") else ""
    return f"[{t.get('id')}] ({t.get('status')}){owner}{parent} {t.get('title')}{notes}{result}"


def create_task(session_dir: Path, *, title: str, notes: Optional[str] = None,
                 parent: Optional[str] = None, kind: str = "task") -> str:
    """The real work behind `TaskCreateTool.run()`, also called directly
    by `agent/subagent.py::run_org_call` (Halo 2.0.2 round 7, brief 3b,
    "goals") to record an org run's own goal as the board's root task,
    before any position ever runs. `kind` ("task" or "goal") and `parent`
    (another task's id) are the same two additions that let the board
    show goal -> tasks -> results instead of a flat, unrelated list --
    a plain `TaskCreate` call with neither is unchanged from before this
    round. Returns the new task's id."""
    task = {"id": uuid.uuid4().hex[:8], "title": title, "status": "open", "owner": None,
            "notes": notes or "", "result": None, "parent": parent or None, "kind": kind or "task",
            "created": time.time(), "updated": time.time()}
    with _lock:
        tasks, error = _read_board_for_write(session_dir)
        if error is not None:
            # finding 34: refuse to silently treat a corrupt file as an
            # empty board and overwrite it -- the corrupt file is kept,
            # under its own name, for forensics/manual recovery, and this
            # call starts a fresh board with just the new task (never
            # raises: `create_task` has no error return of its own, and
            # `run_org_call`'s "never raises" contract calls this too).
            path = _board_path(session_dir)
            try:
                path.rename(path.with_name(f"{path.name}.corrupt-{int(time.time())}"))
            except OSError:
                pass
            tasks = []
        tasks.append(task)
        _write_board(session_dir, tasks)
    return task["id"]


class TaskCreateTool(Tool):
    name = "TaskCreate"
    description = (
        "Add a task to this session's SHARED task board (distinct from TodoWrite's own private list) -- "
        "visible to every sub-agent of this session, so an organization's workers can see what needs doing. "
        "Returns the new task's id. Use TaskList to see the board and TaskUpdate to claim/update a task."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "A short description of the work"},
            "notes": {"type": "string", "description": "Optional extra detail"},
            "parent": {"type": "string", "description": "Optional: another task's id (e.g. this run's own "
                                                          "goal task) this one works toward -- the board tab "
                                                          "shows goal -> tasks -> results when this is set"},
        },
        "required": ["title"],
    }

    def summary(self, input: dict) -> str:
        input = input if isinstance(input, dict) else {}
        return f"TaskCreate({(input.get('title') or '')[:60]})"

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        input = input if isinstance(input, dict) else {}
        title = input.get("title")
        if not title or not isinstance(title, str):
            return ToolResult("The title parameter is required", is_error=True)
        if ctx.session_dir is None:
            return ToolResult("No session directory is available in this context.", is_error=True)
        task_id = create_task(ctx.session_dir, title=title, notes=input.get("notes"), parent=input.get("parent"))
        return ToolResult(f"Created task {task_id!r}: {title}")


class TaskUpdateTool(Tool):
    name = "TaskUpdate"
    description = (
        "Update a task on this session's shared task board (TaskCreate/TaskList). Set status to 'claimed' "
        "(with owner=your own name/title) to take an open task -- this errors clearly if someone else "
        "already claimed it first, so two agents racing for the same task never both win. Set status to "
        "'done' or 'blocked' to report outcome; notes/result may be set at any time."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "The task id (from TaskCreate or TaskList)"},
            "status": {"type": "string", "enum": list(_STATUSES)},
            "owner": {"type": "string", "description": "Your own agent/position name -- required to claim"},
            "notes": {"type": "string"},
            "result": {"type": "string", "description": "A pointer to the outcome (a file path, a summary, ...)"},
        },
        "required": ["id"],
    }

    def summary(self, input: dict) -> str:
        input = input if isinstance(input, dict) else {}
        return f"TaskUpdate({input.get('id')}: {input.get('status') or '...'})"

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        input = input if isinstance(input, dict) else {}
        task_id = input.get("id")
        if not task_id or not isinstance(task_id, str):
            return ToolResult("The id parameter is required", is_error=True)
        if ctx.session_dir is None:
            return ToolResult("No session directory is available in this context.", is_error=True)
        new_status = input.get("status")
        if new_status is not None and new_status not in _STATUSES:
            return ToolResult(f"status must be one of {list(_STATUSES)}, got {new_status!r}", is_error=True)
        if new_status == "claimed" and not input.get("owner"):
            return ToolResult("owner is required to claim a task", is_error=True)

        with _lock:
            tasks = read_board(ctx.session_dir)
            task = next((t for t in tasks if t.get("id") == task_id), None)
            if task is None:
                return ToolResult(f"Unknown task id {task_id!r}.", is_error=True)
            if new_status == "claimed" and task.get("status") not in (None, "open"):
                return ToolResult(
                    f"Task {task_id!r} is already {task.get('status')} (owner={task.get('owner')!r}) -- "
                    f"not open to claim.", is_error=True)
            if new_status is not None:
                task["status"] = new_status
            if input.get("owner") is not None:
                task["owner"] = input["owner"]
            if input.get("notes") is not None:
                task["notes"] = input["notes"]
            if input.get("result") is not None:
                task["result"] = input["result"]
            task["updated"] = time.time()
            _write_board(ctx.session_dir, tasks)
        return ToolResult(_format_task(task))


class TaskListTool(Tool):
    name = "TaskList"
    is_read_only = True
    description = "List this session's shared task board (TaskCreate/TaskUpdate) -- every task, any status."
    input_schema = {"type": "object", "properties": {}}

    def summary(self, input: dict) -> str:
        return "TaskList()"

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        if ctx.session_dir is None:
            return ToolResult("No session directory is available in this context.", is_error=True)
        tasks = read_board(ctx.session_dir)
        if not tasks:
            return ToolResult("(the task board is empty)")
        return ToolResult("\n".join(_format_task(t) for t in tasks))
