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
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text).strip("-") or "repo"


def _git(args: list, cwd: Path) -> "subprocess.CompletedProcess":
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)


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


def remove_worktree(worktree_path: Path, *, repo_root_hint: Optional[Path] = None) -> bool:
    """Best-effort `git worktree remove` (falling back to `--force` once if
    the plain form refuses over uncommitted changes -- a session's own
    scratch worktree is disposable by design) run from `repo_root_hint`
    when given, else the MAIN repo root discovered from the worktree
    itself. Never raises; returns whether it succeeded.

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
    result = _git(["worktree", "remove", str(worktree_path)], cwd)
    if result.returncode != 0:
        result = _git(["worktree", "remove", "--force", str(worktree_path)], cwd)
    return result.returncode == 0
