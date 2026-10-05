"""halo_harness.worktree -- W4a `-w/--worktree [name]` and sub-agent
`isolation: worktree` (shared, per the gap list's own "shared with
--worktree" wording): "Create a new git worktree for this session" via a
real `git worktree add`, isolated under `<state>/worktrees/<repo-slug>/
<name>`, on a fresh branch so the real working tree is never touched.
`None` cwd (not inside a git repo, or `git worktree add` itself failing) is
the caller's cue to fall back to running in the ORIGINAL cwd with one
notice -- a session must never fail to start just because isolation wasn't
available.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text).strip("-") or "repo"


def _git(args: list, cwd: Path) -> "subprocess.CompletedProcess":
    """Review finding 27: `git` missing from PATH (`FileNotFoundError`, an
    `OSError` subclass) or a hung git process (`TimeoutExpired`, a
    `subprocess.SubprocessError` subclass) used to escape every caller
    here as a bare traceback instead of this module's own documented
    "(None, reason)" contract -- never raises now. Every caller already
    treats a nonzero returncode as "this git call failed" (reading
    `stderr`/`stdout` for the reason), so a synthetic result here is
    enough to route through that same, already-correct path."""
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(["git", *args], 1, "", f"git: {e}")


def repo_root(cwd: Path) -> Optional[Path]:
    result = _git(["rev-parse", "--show-toplevel"], cwd)
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def create_worktree(cwd: Path, name: Optional[str], *, state_dir: Path) -> "tuple[Optional[Path], Optional[str]]":
    """`(worktree_path, error)` -- exactly one is non-None. `name` (falsy ->
    a short random one) becomes both the directory's own leaf name and the
    new branch's name (`halo-worktree-<name>`); a collision gets a numeric
    suffix rather than failing outright."""
    # review finding 27: checked BEFORE ever calling `_git` (which now
    # never raises either way, but this gives the common "git isn't
    # installed at all" case its own precise message rather than the
    # generic "not inside a git repository" one below).
    if shutil.which("git") is None:
        return None, "git not found on PATH -- install git to use worktree isolation"
    root = repo_root(cwd)
    if root is None:
        return None, "the current directory is not inside a git repository"
    base_name = name or f"session-{uuid.uuid4().hex[:8]}"
    base_name = _slug(base_name)
    worktrees_dir = Path(state_dir) / "worktrees" / _slug(root.name)
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    dest = worktrees_dir / base_name
    suffix = 2
    while dest.exists():
        dest = worktrees_dir / f"{base_name}-{suffix}"
        suffix += 1
    branch = f"halo-worktree-{dest.name}-{int(time.time())}"
    result = _git(["worktree", "add", "-b", branch, str(dest)], root)
    if result.returncode != 0:
        return None, (result.stderr or result.stdout or "git worktree add failed").strip().splitlines()[-1]
    return dest, None


def remove_worktree(worktree_path: Path, *, repo_root_hint: Optional[Path] = None) -> "tuple[bool, Optional[str]]":
    """`(removed, reason)` -- `reason` is `None` on success, else "dirty"
    (real uncommitted changes -- kept, never removed) or "failed" (the
    `git worktree remove` call itself failed). Run from `repo_root_hint`
    when given, else the MAIN repo root discovered from the worktree
    itself. Never raises.

    Review finding 28: this used to fall back to `git worktree remove
    --force` once the plain form refused over uncommitted changes -- in a
    `-p -w` run, those uncommitted edits ARE the session's whole output,
    so silently discarding them on exit defeated the entire point of
    `-w`. A dirty worktree is left alone now, kept for the caller to
    report; a CLEAN one is removed and its own disposable
    `halo-worktree-*` branch (captured before removal, while it still
    exists to ask `git` for) is deleted right along with it -- it used to
    be left behind forever, every single session.

    W5 (carried from W4a, live bug found wiring in `halo worktree rm`'s
    first real caller): `git worktree remove` must run with `cwd` OUTSIDE
    the directory it's deleting -- a process cwd'd INTO that directory
    holds an OS-level lock on Windows, so the removal itself fails with
    "Permission denied" even though git accepts the command (verified
    live: `worktree_path.parent` -- the `<state>/worktrees/<repo-slug>/`
    layout this module creates -- is itself not a git working tree at
    all, so the OLD code's `git -C <that dir> worktree remove ...` was
    failing outright, every time, for every caller that didn't pass an
    explicit `repo_root_hint`). `git -C <worktree_path> rev-parse --git-
    common-dir` is READ-ONLY (safe to run with cwd INSIDE the worktree,
    unlike the destructive removal step) and resolves to the MAIN repo's
    shared `.git` directory even from a linked worktree -- its parent is
    the real repo root to run the actual removal from."""
    worktree_path = Path(worktree_path)
    repo_root = repo_root_hint
    if repo_root is None:
        common = _git(["rev-parse", "--git-common-dir"], worktree_path)
        if common.returncode == 0 and common.stdout.strip():
            common_dir = Path(common.stdout.strip())
            if not common_dir.is_absolute():
                common_dir = (worktree_path / common_dir).resolve()
            repo_root = common_dir.parent
    cwd = repo_root or worktree_path.parent

    # Captured while the worktree still exists (a branch checked out by a
    # linked worktree can't be queried from anywhere else) -- read-only,
    # safe to run with cwd INSIDE the worktree like the status check below.
    branch_result = _git(["rev-parse", "--abbrev-ref", "HEAD"], worktree_path)
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    if branch == "HEAD":  # detached HEAD -- not a real branch name to delete
        branch = None

    status = _git(["status", "--porcelain"], worktree_path)
    if status.returncode != 0 or status.stdout.strip():
        return False, "dirty"

    result = _git(["worktree", "remove", str(worktree_path)], cwd)
    if result.returncode != 0:
        return False, "failed"
    if branch:
        _git(["branch", "-D", branch], cwd)
    return True, None
