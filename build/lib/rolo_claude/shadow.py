"""rolo_claude.shadow -- git-shadow snapshots for `/rewind` (U5 scope B).

Every Write/Edit/Bash-that-changed-files step records the RESULTING content
of every file it touched into a real, isolated git repository under
`~/.rolo-claude/sessions/<slug>/<session_id>/shadow/` (mirroring OpenCode's
own snapshot mechanism and Claude Code's `file-history`), keyed as one git
commit per step. `/rewind <step>` (aliases `/undo` steps back, `/redo`
steps forward) restores the REAL working tree to that step: every file ever
tracked is checked out from that step's commit and written back to its real
location -- a full "restore the working tree to a step", not a per-file
diff/patch.

A real absolute path (Windows drive letters included) is "mangled" into a
safe, collision-free relative path inside the shadow repo (`_mangle`/
`_unmangle`) so the SAME shadow repo can track files from anywhere the
session touched, not just one subtree.

Deliberate scope cut (documented, not a bug): rewinding restores the
CONTENT of every file tracked as of the target step; it does not delete a
file that was created by a LATER step (that file simply isn't in the
target commit's tree, so it's left alone). A full create/delete-aware
rewind is future work -- OpenCode's own `revert` has the same "flags then
cleans up on the next prompt" asymmetry rather than a true transactional
undo.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

_DRIVE_RE = re.compile(r"^([A-Za-z]):/(.*)$")


def _mangle(path: Path) -> str:
    """Absolute real path -> a safe, unique relative path inside the shadow
    repo. A Windows drive letter becomes its own top segment
    (`C:\\Users\\x` -> `_drive_C/Users/x`); a POSIX absolute path just
    drops its leading slash. Two different real files never collide."""
    s = str(path).replace("\\", "/")
    m = _DRIVE_RE.match(s)
    if m:
        drive, rest = m.groups()
        return f"_drive_{drive.upper()}/{rest}"
    return s.lstrip("/")


def _unmangle(rel: str) -> Path:
    if rel.startswith("_drive_"):
        rest = rel[len("_drive_"):]
        drive, _, tail = rest.partition("/")
        return Path(f"{drive}:/{tail}")
    return Path("/" + rel)


class ShadowStore:
    """One session's shadow repo + step index. `session_dir` is the
    directory a step's snapshot lives under -- callers pass
    `bridge_home()/"sessions"/<slug>/<session_id>` (a NEW directory,
    alongside that session's existing `<session_id>.jsonl` log file, which
    `agent/log.py`'s `SessionLog` already owns -- this module never touches
    that file)."""

    def __init__(self, session_dir: Path):
        self.session_dir = Path(session_dir)
        self.dir = self.session_dir / "shadow"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.session_dir / "shadow-index.jsonl"
        self.steps: "list[dict]" = self._load_index()
        self.cursor = len(self.steps) - 1  # -1: no steps yet
        self._ensure_repo()

    # ---- git plumbing ---------------------------------------------------

    def _git(self, *args: str) -> "subprocess.CompletedProcess":
        return subprocess.run(["git", "-C", str(self.dir), *args], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")

    def _ensure_repo(self) -> None:
        if (self.dir / ".git").exists():
            return
        self._git("init", "-q")
        self._git("config", "user.email", "shadow@rolo-claude.local")
        self._git("config", "user.name", "rolo-claude shadow")

    # ---- index persistence -----------------------------------------------

    def _load_index(self) -> "list[dict]":
        if not self._index_path.exists():
            return []
        out = []
        try:
            for line in self._index_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            pass
        return out

    def _append_index(self, step: dict) -> None:
        try:
            with open(self._index_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(step, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ---- recording ---------------------------------------------------

    def record_step(self, files: "dict[str, str]", *, label: str, trigger: str = "tool") -> dict:
        """`files`: `{absolute_path_str: resulting_content}` -- the content
        of every file this step touched, AFTER the change. Writes each into
        the shadow repo, commits, and appends one step to the index.
        `{}`/no real change -> returns `{}` and records nothing (a no-op
        step is never worth a rewind target)."""
        if not files:
            return {}
        mangled: "list[str]" = []
        for real_path, content in files.items():
            rel = _mangle(Path(real_path))
            dest = self.dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest.write_text(content, encoding="utf-8", errors="replace")
            except OSError:
                continue
            mangled.append(rel)
        if not mangled:
            return {}
        self._git("add", "-A", "--", *mangled)
        self._git("commit", "-q", "--allow-empty", "-m", label or "snapshot")
        commit_hash = self._git("rev-parse", "HEAD").stdout.strip()
        if not commit_hash:
            return {}
        step = {
            "id": commit_hash[:12], "hash": commit_hash, "ts": time.time(),
            "label": label or "snapshot", "trigger": trigger, "files": sorted(files),
        }
        self.steps.append(step)
        self.cursor = len(self.steps) - 1
        self._append_index(step)
        return step

    # ---- restoring -----------------------------------------------------

    def _restore_commit(self, commit_hash: str) -> "list[str]":
        """Checks out EVERY file tracked as of `commit_hash` back to its
        real location. Returns the list of real paths written."""
        listing = self._git("ls-tree", "-r", "--name-only", commit_hash)
        restored: "list[str]" = []
        for rel in listing.stdout.splitlines():
            rel = rel.strip()
            if not rel:
                continue
            shown = self._git("show", f"{commit_hash}:{rel}")
            if shown.returncode != 0:
                continue
            real_path = _unmangle(rel)
            try:
                real_path.parent.mkdir(parents=True, exist_ok=True)
                real_path.write_text(shown.stdout, encoding="utf-8", errors="replace")
                restored.append(str(real_path))
            except OSError:
                continue
        return restored

    def rewind_to(self, step_id: str) -> "Optional[dict]":
        """Restore the working tree to the step whose `id` (or full
        `hash`) is `step_id`. Returns `{"step": step, "files": [...]}`, or
        `None` if no step matches."""
        step = self._find(step_id)
        if step is None:
            return None
        restored = self._restore_commit(step["hash"])
        self.cursor = self.steps.index(step)
        return {"step": step, "files": restored}

    def undo(self) -> "Optional[dict]":
        """Move the cursor one step back and restore to it. `None` if
        already at (or before) the first step -- nothing left to undo."""
        if self.cursor <= 0:
            return None
        return self.rewind_to(self.steps[self.cursor - 1]["id"])

    def redo(self) -> "Optional[dict]":
        """Move the cursor one step forward and restore to it. `None` if
        already at (or past) the last step -- nothing left to redo."""
        if self.cursor < 0 or self.cursor >= len(self.steps) - 1:
            return None
        return self.rewind_to(self.steps[self.cursor + 1]["id"])

    def _find(self, step_id: str) -> "Optional[dict]":
        for step in self.steps:
            if step_id in (step.get("id"), step.get("hash")):
                return step
        return None

    def list_steps(self) -> "list[dict]":
        return list(self.steps)

    def preview_undo(self) -> "Optional[dict]":
        """The step `undo()` would restore to, without side effects (the
        confirmation card reads this to show what it's about to do)."""
        if self.cursor <= 0:
            return None
        return self.steps[self.cursor - 1]

    def preview_redo(self) -> "Optional[dict]":
        if self.cursor < 0 or self.cursor >= len(self.steps) - 1:
            return None
        return self.steps[self.cursor + 1]


def store_for_controller(controller) -> "Optional[ShadowStore]":
    """A `ShadowStore` rooted at this controller's CURRENT session's shadow
    directory (`<session_log_dir>/<session_id>/shadow/`), or `None` when
    there's no real session to key it off (a bare `FakeController` in a
    test that hasn't opted in). `controller.shadow_dir`, when present, is
    an explicit test seam that skips the `.session.log` lookup entirely --
    a scripted test can point it straight at a temp directory. Stateless on
    purpose: every call re-reads the on-disk step index, so two callers
    (the tool-result recorder in `tui/dispatch.py`, the `/rewind` family in
    `tui/slash.py`) never need to share one cached instance to see each
    other's writes."""
    explicit = getattr(controller, "shadow_dir", None)
    if explicit is not None:
        return ShadowStore(Path(explicit))
    log = getattr(getattr(controller, "session", None), "log", None)
    if log is None:
        return None
    return ShadowStore(Path(log.dir) / log.session_id)
