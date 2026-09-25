"""rolo_claude.agent.jobs -- background Bash job registry (H8 scope A).

Mirrors agent/subagent.py's background-sub-agent pattern (dsh: "background
jobs ... report completion as a user-role notice in the next step") for
Bash's own `run_in_background` and for a FOREGROUND command that hits its
own timeout ("a foreground command that hits its timeout is moved to the
background rather than killed" -- dsh's agent-loop README, quoted in
H8-brief.md scope A).

Tool naming (H8-brief.md: "mirror Claude Code 2.1.281's real tool names ...
grep the installed binary"): `claude.exe` 2.1.282's own minified source has
`var i={Task:"Agent",KillShell:"TaskStop",KillBash:"TaskStop",...}` -- an
explicit alias table mapping OLD per-kind tool names to their CURRENT
replacement. Killing any background task (a backgrounded Bash shell OR a
background Agent/Task) is unified under ONE tool, literally
`var Fm="TaskStop"` a few lines later in that same module, whose own Zod
schema takes `task_id` (current) with `shell_id` documented inline as
"Deprecated: use task_id instead" -- so `KillShell`/`KillBash` both now
alias to `TaskStop`. Reading a backgrounded Bash shell's own output stays a
SEPARATE tool: `BashOutput` appears live alongside `TaskOutput` in the same
build (a `Set` of current tool/class identifiers together, `"TaskOutput"`
paired with `"AgentOutputTool"`/`"BashOutputTool"`/`"AgentOutput"`/
`"BashOutput"`) and is never one of the keys in that alias-remap table --
i.e. only the KILL verb was unified, not the READ verb. This harness
therefore registers `BashOutput` (tools/bash_output.py) + `TaskStop`
(tools/task_stop.py), using `shell_id` as BashOutput's own id parameter --
the one concrete field name the binary evidences anywhere for identifying a
backgrounded shell -- for consistency with TaskStop's own deprecated alias.
"""

from __future__ import annotations

import dataclasses
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from rolo_claude.tools._proc import _CappedCollector, _kill_process_group

MAX_CONCURRENT_JOBS = 10
_QUEUE_POLL_EMPTY = object()  # sentinel: "queue.Empty fired this poll", distinct from a real `None` EOF


@dataclasses.dataclass
class JobRecord:
    job_id: str
    command: str
    description: str
    cwd: str
    started_at: float
    proc: "Optional[subprocess.Popen]" = None
    collector: "Optional[_CappedCollector]" = None
    lock: "threading.Lock" = dataclasses.field(default_factory=threading.Lock)
    status: str = "running"           # running | completed | killed | failed
    exit_code: Optional[int] = None
    read_offset: int = 0              # chars of collector.result() already delivered via BashOutput
    origin: str = "background"        # "background" (explicit run_in_background) | "timeout" (converted)
    # Set only for an "timeout"-origin record: the SAME per-call nonce
    # tools/bash.py generated for its marker-wrapped script (still running
    # when it was handed off) -- `_drain_and_finalize` uses it to strip the
    # trailing `__ROLO_CLAUDE_EXIT__`/`__ROLO_CLAUDE_CWD__` marker lines out
    # of what BashOutput ever shows and to recover the ORIGINAL command's
    # real exit status (the wrapper script's own last command is a
    # `printf`, so `proc.returncode` alone would just be the printf's exit
    # status, almost always 0). None (every "background"-origin record,
    # which never had a wrapper/markers at all) skips this entirely.
    nonce: Optional[str] = None
    _parsed_exit: Optional[int] = dataclasses.field(default=None, repr=False)


class JobRegistry:
    """Held as `Session.job_registry` and threaded through every
    `ToolContext` as `job_registry=` (tools/base.py) -- untyped there for
    the same reverse-import reason `agent_runtime`/`catalog` already are.
    One instance per session; `parent` is the owning `agent.loop.Session`,
    used only to push a completion notice onto its
    `_pending_job_notices`/`_job_notices_lock` (mirrors
    `AgentRuntime`/`_pending_agent_notices` exactly)."""

    def __init__(self, parent: object) -> None:
        self.parent = parent
        self.jobs: "dict[str, JobRecord]" = {}
        self.lock = threading.Lock()

    def _live_count(self) -> int:
        return sum(1 for j in self.jobs.values() if j.status == "running")

    def can_start(self) -> bool:
        return self._live_count() < MAX_CONCURRENT_JOBS

    @staticmethod
    def _new_id() -> str:
        return "bash_" + uuid.uuid4().hex[:10]

    # ---- lifecycle ---------------------------------------------------

    def start_background(
        self, command: str, *, description: str, cwd, env: dict, shell_path: str,
    ) -> "tuple[Optional[JobRecord], Optional[str]]":
        """Explicit `run_in_background: true` -- spawns immediately, never
        waits at all. `(record, None)` on success, `(None, error_text)`
        otherwise (never raises)."""
        if not self.can_start():
            return None, (f"Too many background jobs already running (max {MAX_CONCURRENT_JOBS}) -- "
                           f"wait for one to finish or stop one with TaskStop first.")
        try:
            proc = subprocess.Popen(
                [shell_path, "-lc", command], cwd=str(cwd), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
                start_new_session=(os.name != "nt"),
            )
        except OSError as e:
            return None, f"Error launching background process: {e}"

        job_id = self._new_id()
        record = JobRecord(job_id=job_id, command=command, description=description, cwd=str(cwd),
                            started_at=time.time(), proc=proc, collector=_CappedCollector(), origin="background")
        with self.lock:
            self.jobs[job_id] = record

        q: "queue.Queue" = queue.Queue()

        def _reader() -> None:
            try:
                for line in iter(proc.stdout.readline, ""):
                    q.put(line)
            except Exception:
                pass
            finally:
                q.put(None)

        threading.Thread(target=_reader, daemon=True, name=f"bashjob-reader-{job_id}").start()
        threading.Thread(target=self._drain_and_finalize, args=(record, q),
                          daemon=True, name=f"bashjob-{job_id}").start()
        return record, None

    def adopt_from_timeout(
        self, *, proc: "subprocess.Popen", q: "queue.Queue", collector: _CappedCollector,
        command: str, description: str, cwd, nonce: Optional[str] = None,
    ) -> JobRecord:
        """`tools/_proc.py`'s `on_timeout_handoff` callback lands here: a
        foreground Bash call's own subprocess/reader-queue/collector are
        handed off STILL LIVE (never killed) -- this takes over draining
        `q` exactly like `start_background`'s own watcher thread does, the
        one difference being who spawned `proc` and who was already
        feeding `q` before this call. Never gated by `can_start()` -- the
        process is already running either way; the only real choice was
        already made (background it, don't kill it)."""
        job_id = self._new_id()
        record = JobRecord(job_id=job_id, command=command, description=description, cwd=str(cwd),
                            started_at=time.time(), proc=proc, collector=collector, origin="timeout", nonce=nonce)
        with self.lock:
            self.jobs[job_id] = record
        threading.Thread(target=self._drain_and_finalize, args=(record, q),
                          daemon=True, name=f"bashjob-{job_id}").start()
        return record

    def _append_line(self, record: JobRecord, line: str) -> None:
        """Route one line of output into `record.collector`, stripping a
        marker line and capturing its exit code first when `record.nonce`
        is set (an adopted-from-timeout record only -- see its own field
        docstring); every "background"-origin record has no nonce and
        takes the plain fast path unchanged."""
        if record.nonce:
            from rolo_claude.tools.bash import _CWD_MARK, _EXIT_MARK
            exit_prefix = f"{_EXIT_MARK}_{record.nonce}:"
            cwd_prefix = f"{_CWD_MARK}_{record.nonce}:"
            stripped = line.rstrip("\r\n")
            if stripped.startswith(exit_prefix):
                try:
                    record._parsed_exit = int(stripped[len(exit_prefix):].strip())
                except ValueError:
                    pass
                return
            if stripped.startswith(cwd_prefix):
                return
        with record.lock:
            record.collector.append(line)

    def _drain_and_finalize(self, record: JobRecord, q: "queue.Queue") -> None:
        # H8 scope A finding (Windows): `kill()`'s own `_kill_process_group`
        # can leave an orphaned GRANDCHILD alive for a few hundred ms after
        # a kill requested only moments after the job started -- Windows
        # taskkill's own error reporting for that race is unreliable enough
        # (a retry sometimes reports "process not found" while the orphan
        # is, per direct observation, still very much alive and still
        # holding the pipe's write end open) that trying to detect "really
        # dead now" from taskkill's text output alone is a losing game.
        # Poll the queue at a short, FIXED interval always (never a single
        # unbounded blocking `q.get()`, whose call could itself start a
        # moment before `kill()` flips `record.status` on another thread
        # and then have nothing left to wake it early) -- indistinguishable
        # in effect from blocking for an ordinary job (a background job
        # producing no output for a while is the common case; this is a
        # 200ms-latency poll on that idle path, not a busy loop), but it
        # lets every iteration re-check whether a kill just landed. Once it
        # has, give the OS a bounded grace period to actually close the
        # pipe before giving up and finalizing anyway -- the reader thread
        # feeding `q` is left running as an abandoned daemon in that rare
        # case, harmless, draining itself once the orphan eventually exits.
        kill_deadline: Optional[float] = None
        try:
            while True:
                try:
                    item = q.get(timeout=0.2)
                except queue.Empty:
                    item = _QUEUE_POLL_EMPTY
                if item is None:
                    break
                if item is not _QUEUE_POLL_EMPTY:
                    self._append_line(record, item)
                with record.lock:
                    killed_now = record.status == "killed"
                if killed_now:
                    if kill_deadline is None:
                        kill_deadline = time.monotonic() + 3.0
                    elif time.monotonic() >= kill_deadline:
                        break
        except Exception:
            pass
        try:
            record.proc.wait(timeout=2.0 if kill_deadline is not None else 10.0)
        except Exception:
            pass
        with record.lock:
            if record.status == "running":
                record.status = "completed"
            record.exit_code = record._parsed_exit if record._parsed_exit is not None else record.proc.returncode
        self._push_notice(record)

    # H9 whole-tree review finding 7: a completion notice used to embed the
    # job's ENTIRE captured output (up to the collector's own ~300k-char
    # cap) verbatim, and unlike a real tool_result this text is a plain
    # user-role log entry that prune.py never stubs -- it stays at full
    # size in the log FOREVER, re-sent on every later request for the rest
    # of the session (verified: a 594k-char job produced a 300,292-char
    # notice, about 75k tokens, resent every step). BashOutput already
    # exists specifically to fetch a job's output on demand, so the notice
    # only needs a short tail to show what happened at a glance, not the
    # whole thing.
    _NOTICE_OUTPUT_TAIL_CHARS = 2000

    def _push_notice(self, record: JobRecord) -> None:
        """dsh: "background jobs ... report completion as a user-role
        notice in the next step" -- mirrors agent/subagent.py's own
        `_bg_run` -> `parent._pending_agent_notices` append exactly, via
        the session's OWN (separate) `_pending_job_notices` list so a bash
        job's completion is never confused with a sub-agent's in the log."""
        with record.lock:
            status, exit_code = record.status, record.exit_code
            output = record.collector.result().strip() if record.collector else ""
        label = record.description or record.command[:60]
        total_chars = len(output)
        if total_chars > self._NOTICE_OUTPUT_TAIL_CHARS:
            hint = (f"\n\n... [{total_chars - self._NOTICE_OUTPUT_TAIL_CHARS} earlier characters omitted -- "
                    f"use BashOutput({record.job_id!r}) to fetch the rest, or the full capture is at "
                    f"{record.collector.spill_path}]" if record.collector and record.collector.spill_path
                    else f"\n\n... [{total_chars - self._NOTICE_OUTPUT_TAIL_CHARS} earlier characters omitted -- "
                         f"use BashOutput({record.job_id!r}) to fetch the rest]")
            output = output[-self._NOTICE_OUTPUT_TAIL_CHARS:] + hint
        if status == "killed":
            text = f"[Background job {record.job_id} ({label}) was stopped before it finished]"
            if output:
                text += f"\nOutput so far:\n{output}"
        else:
            text = f"[Background job {record.job_id} ({label}) finished, exit code {exit_code}]"
            text += f"\n{output}" if output else "\n(no output)"
        notices_lock = getattr(self.parent, "_job_notices_lock", None)
        notices = getattr(self.parent, "_pending_job_notices", None)
        if notices_lock is None or notices is None:
            return  # a bare/unit-test parent with no notice plumbing -- nothing to append to
        with notices_lock:
            notices.append(text)

    # ---- tool-facing operations ---------------------------------------

    def poll(self, job_id: str, *, filter_regex: Optional[str] = None) -> "tuple[Optional[str], Optional[str]]":
        """`(text, error)` -- exactly one is non-None. `text` carries only
        NEW output since the last `poll()` on this same job (Claude Code's
        own BashOutput contract), a `<status>`/`<exit_code>` header, and
        (when `filter_regex` is given) only the matching LINES of that new
        slice -- the filter never limits what the shell itself produced or
        what was captured, only what this call hands back."""
        record = self.jobs.get(job_id)
        if record is None:
            known = ", ".join(sorted(self.jobs)) or "(none)"
            return None, f"Unknown shell_id {job_id!r}. Known background shells: {known}"
        with record.lock:
            full = record.collector.result() if record.collector else ""
            collector = record.collector
            prev_offset = record.read_offset
            total_len = collector.total_len if collector is not None else len(full)
            if collector is not None and collector.spill_path is not None and prev_offset < collector.tail_start:
                # H9 whole-tree review finding 6: `record.read_offset` used
                # to be compared against `len(full)`, but once output
                # exceeds the in-memory cap `result()` returns head+tail
                # only (roughly constant length from here on) -- diffing
                # against that silently DROPPED every char that had
                # already rolled out of both windows before this poll ever
                # ran. Verified: three 198k-char writes returned 198,136
                # new chars, then 102,088, then "(no new output)" forever,
                # even though the shell kept producing real output. Never
                # silently lose it: deliver any HEAD bytes not yet
                # delivered, account for the truly-unreachable middle with
                # an explicit skipped-count and the spill file's path (a
                # real Read call on it recovers everything), then the
                # current tail in full.
                head_len = collector.head_len
                undelivered_head = full[prev_offset:head_len] if prev_offset < head_len else ""
                skipped = collector.tail_start - max(prev_offset, head_len)
                parts = [undelivered_head]
                if skipped > 0:
                    parts.append(f"\n\n... [{skipped} characters skipped -- already rolled out of the "
                                 f"in-memory window; read {collector.spill_path} directly for the full output]\n\n")
                parts.append(collector.tail_text)
                new_text = "".join(parts)
            else:
                new_text = full[prev_offset:] if prev_offset <= len(full) else ""
            record.read_offset = total_len
            status, exit_code = record.status, record.exit_code
        if filter_regex:
            try:
                rx = re.compile(filter_regex)
            except re.error as e:
                return None, f"Invalid filter regex: {e}"
            new_text = "\n".join(line for line in new_text.splitlines() if rx.search(line))
        header = f"<status>{status}</status>"
        if status != "running":
            header += f"\n<exit_code>{exit_code}</exit_code>"
        body = new_text if new_text.strip() else "(no new output)"
        return f"{header}\n{body}", None

    def kill(self, job_id: str) -> "tuple[bool, str]":
        record = self.jobs.get(job_id)
        if record is None:
            return False, f"Unknown shell_id/task_id {job_id!r}."
        with record.lock:
            if record.status != "running":
                return True, f"Background job {job_id} already {record.status}."
            record.status = "killed"
            proc = record.proc
        if proc is not None:
            # tools/_proc.py's `_kill_process_group` (Windows:
            # `_kill_process_tree_windows`) already retries against a
            # just-forked grandchild that raced its own OS registration --
            # see that function's docstring for the exact failure mode this
            # closes (a kill requested moments after the job started).
            _kill_process_group(proc)
        return True, f"Stopped background job {job_id}."

    def kill_all(self) -> None:
        """Brief A: "jobs killed on quit" -- called from Controller.quit()
        and headless.py's run_print_mode `finally`, never from mid-session
        interrupt/steer handling (Esc only cuts the CURRENT foreground
        step; a background job surviving that is the whole point of it)."""
        for job_id in list(self.jobs):
            try:
                self.kill(job_id)
            except Exception:
                pass

    def list_jobs(self) -> list:
        """`[{id, description, command, status, started_at}, ...]`,
        id-sorted -- the `/tasks` slash command's own data source."""
        out = []
        for job_id in sorted(self.jobs):
            record = self.jobs[job_id]
            with record.lock:
                out.append({"id": job_id, "description": record.description, "command": record.command,
                            "status": record.status, "started_at": record.started_at, "origin": record.origin})
        return out
