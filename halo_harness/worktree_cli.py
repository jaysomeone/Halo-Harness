"""halo_harness.worktree_cli -- `halo worktree rm <path>` (W5, carried from
W4a): the explicit counterpart to `-w/--worktree`'s own `create_worktree` --
removes a worktree via `halo_harness.worktree.remove_worktree` and fires
WorktreeRemoved (removed from `hooks.NOT_EMITTED_V1`), the same event
`headless.run_print_mode`'s own session-end `worktree.remove_on_exit`
auto-removal fires. No live Session exists for a bare CLI invocation like
this one, so a throwaway `HookRunner` is built directly from this cwd's own
settings (+ installed plugin hooks) via `headless.build_hook_runner` --
reused as-is rather than inventing a second hook-loading path.
"""

from __future__ import annotations

import sys
from pathlib import Path

_USAGE = "usage: halo worktree rm <path>"


def _fire_worktree_removed(cwd: Path, path: Path) -> None:
    """Best-effort, never fatal -- a hook failing to fire must never turn
    an otherwise-successful `rm` into a reported error."""
    try:
        from halo_harness.config.settings import resolve_settings
        from halo_harness.headless import build_hook_runner

        settings = resolve_settings(cwd)
        runner = build_hook_runner(
            settings=settings, cwd=cwd, session_id="worktree-rm", transcript_path="",
            effort=None, permission_mode="auto", mcp_manager=None, bare=False,
        )
        if runner.has_hooks("WorktreeRemoved"):
            runner.run("WorktreeRemoved", runner.payload("WorktreeRemoved", extra={"path": str(path)}))
    except Exception:
        pass


def cmd_worktree(argv: list) -> int:
    """`halo worktree rm <path>` only, v1 -- no `list`/`add` subcommand yet
    (`-w/--worktree` itself is how one gets ADDED; this is just the
    explicit removal half WorktreeRemoved needed a real trigger for)."""
    if not argv:
        print(_USAGE, file=sys.stderr)
        return 2
    if argv[0] in ("-h", "--help"):
        print(_USAGE, file=sys.stderr)
        return 0
    sub, rest = argv[0], argv[1:]
    if sub != "rm":
        print(f"halo worktree: unknown subcommand {sub!r} (known: rm)", file=sys.stderr)
        return 2
    if not rest:
        print(_USAGE, file=sys.stderr)
        return 2
    path = Path(rest[0]).expanduser().resolve()
    if not path.is_dir():
        print(f"halo worktree rm: {path} is not a directory", file=sys.stderr)
        return 1
    from halo_harness.worktree import remove_worktree

    removed, reason = remove_worktree(path)
    if not removed:
        if reason == "dirty":
            # review finding 28: never force-removed -- those uncommitted
            # changes are real, and discarding them is not this command's
            # call to make silently.
            print(f"halo worktree rm: {path} has uncommitted changes -- kept (inspect or commit/discard "
                  f"them yourself, then run this again)", file=sys.stderr)
        else:
            print(f"halo worktree rm: failed to remove {path} -- run `git worktree list` to inspect it",
                  file=sys.stderr)
        return 1
    _fire_worktree_removed(Path.cwd(), path)
    print(f"halo: removed worktree {path}", file=sys.stderr)
    return 0
