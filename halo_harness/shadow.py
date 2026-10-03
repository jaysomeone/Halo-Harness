"""halo_harness.shadow -- git-shadow snapshots for `/rewind` (U5 scope B).

Every Write/Edit/Bash-that-changed-files step records the RESULTING content
of every file it touched into a real, isolated git repository under
`~/.halo/sessions/<slug>/<session_id>/shadow/` (mirroring OpenCode's
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
import sys
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
        self._git("config", "user.email", "shadow@halo.local")
        self._git("config", "user.name", "halo shadow")
        if sys.platform == "win32":
            # The shadow repo sits under <state>/sessions/<cwd slug>/<id>/shadow,
            # deep enough that git objects can exceed Windows' 260-char path
            # limit on a long home or cwd; git supports long paths when told to.
            self._git("config", "core.longpaths", "true")

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

    def record_step(self, files: "dict[str, str]", *, label: str, trigger: str = "tool",
                     created: "Optional[list]" = None) -> dict:
        """`files`: `{absolute_path_str: resulting_content}` -- the content
        of every file this step touched, AFTER the change. Writes each into
        the shadow repo, commits, and appends one step to the index.
        `{}`/no real change -> returns `{}` and records nothing (a no-op
        step is never worth a rewind target).

        `created` (U5 scope B / W4a: "steps record files CREATED so undo
        deletes them"): the subset of `files` that did NOT exist on disk
        before this step ran (the caller knows this -- Write creates a file
        that didn't exist, Edit/NotebookEdit-on-an-existing-notebook never
        do). Recorded on the step so `rewind_to` can delete them when the
        cursor moves back past this step -- restoring an EARLIER commit's
        tree, which never had these paths, must also remove them from the
        REAL working tree, not just leave the content of files it DOES
        track stale."""
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
        created_set = set(created or ()) & set(files)
        step = {
            "id": commit_hash[:12], "hash": commit_hash, "ts": time.time(),
            "label": label or "snapshot", "trigger": trigger, "files": sorted(files),
            "created": sorted(created_set),
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
        `hash`) is `step_id`. Returns `{"step": step, "files": [...],
        "deleted": [...]}`, or `None` if no step matches.

        W4a ("steps record files CREATED so undo deletes them"): every
        step AFTER the target (`steps[target_index + 1:]`) that CREATED a
        path -- one neither this nor any earlier step ever tracked before --
        has that real file deleted, since the target commit's own tree
        (just restored above) never had it either; redoing forward past
        that step naturally re-creates it again (`_restore_commit` already
        writes back everything the LATER target's own tree lists). A path
        also written by an earlier-or-equal step is never deleted even if
        it's (wrongly) listed as `created` somewhere -- belt-and-suspenders,
        though `record_step` itself only ever marks true first-appearances."""
        step = self._find(step_id)
        if step is None:
            return None
        target_index = self.steps.index(step)
        restored = self._restore_commit(step["hash"])
        restored_set = {str(Path(p)) for p in restored}
        deleted: "list[str]" = []
        for later_step in self.steps[target_index + 1:]:
            for real_path in later_step.get("created") or []:
                if real_path in restored_set:
                    continue  # also written at/before the target -- never delete
                try:
                    p = Path(real_path)
                    if p.is_file():
                        p.unlink()
                        deleted.append(real_path)
                except OSError:
                    continue
        self.cursor = target_index
        return {"step": step, "files": restored, "deleted": deleted}

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


#  Porcelain v1 status codes meaning "this path has real, uncommitted
# CONTENT on disk right now" for a previously-TRACKED file -- used by
# `git_status_dirty_paths` below. Deliberately excludes a bare delete
# ("D", no resulting content to shadow-copy at all) and a rename/copy
# ("R"/"C" -- the NUL-delimited porcelain format emits the OLD path as a
# second token with no inline marker, which `git_status_dirty_paths`'s
# single-token-per-entry parse below does not attempt to disambiguate; the
# same "documented limit" the untracked-only v1 parse already carried for
# anything past its own narrow `??` filter).
_MODIFIED_TRACKED_CODES = frozenset({"M ", " M", "MM", "A ", "AM"})


def git_status_dirty_paths(cwd) -> "Optional[dict]":
    """W5 (carried from W4a): the FULL picture `git_status_untracked_paths`
    (below) only ever gave half of -- `{absolute_path_str: status_code}`
    for every `git status --porcelain=v1` entry that means "this path has
    real, uncommitted content on disk right now": untracked (`??`) AND a
    pre-existing TRACKED file with real worktree/index changes
    (`_MODIFIED_TRACKED_CODES`). `None` under the exact same conditions
    `git_status_untracked_paths` already documents (not a git repo, `git`
    itself unreachable, or the call times out).

    `tui/dispatch.py`'s own before/after Bash-shadow workers call this
    once on each side of a command and diff the two dicts BY KEY (a path
    present after but not before) -- this is what finally captures "a
    command modified an already-tracked file" (W4a's own documented scope
    cut): a path that was ALREADY dirty before the command ran is excluded
    either way (same key in both dicts), the one residual limitation this
    bounded before/after diff still carries (it cannot tell a file that was
    modified, then modified AGAIN by this command, from one left alone --
    both show the same status code on both sides)."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=str(cwd), capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    out: dict = {}
    for entry in (result.stdout or "").split("\x00"):
        if len(entry) < 4:
            continue
        code, rel = entry[:2], entry[3:]
        if not rel or (code != "??" and code not in _MODIFIED_TRACKED_CODES):
            continue
        try:
            out[str((Path(cwd) / rel).resolve())] = code
        except OSError:
            continue
    return out


def git_status_untracked_paths(cwd) -> "Optional[set]":
    """W4a: the Bash half of "Bash and NotebookEdit changes are shadow-
    copied via a git status diff before and after the command when the cwd
    is a git repo". Pure and synchronous -- the CALLER (`tui/dispatch.py`,
    via a `run_worker(..., thread=True)`) is responsible for keeping this
    off the UI thread, exactly like `_git_branch`'s own subprocess call.

    Returns the set of absolute paths `git status --porcelain=v1` reports
    as untracked ('??') right now, or `None` when `cwd` isn't a git repo,
    `git` itself isn't reachable, or the call times out -- the documented
    limit for a non-git directory (no shadow-copy for Bash there at all).

    W5: implemented via `git_status_dirty_paths` now (one subprocess call/
    parse shared with the tracked-modified case below) -- the public
    contract here (a bare set of untracked-only paths, `None` outside a
    repo) is UNCHANGED, so every existing caller/test keeps working byte-
    for-byte; `tui/dispatch.py`'s own Bash-shadow workers call
    `git_status_dirty_paths` directly now for the fuller picture."""
    dirty = git_status_dirty_paths(cwd)
    if dirty is None:
        return None
    return {p for p, code in dirty.items() if code == "??"}


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
