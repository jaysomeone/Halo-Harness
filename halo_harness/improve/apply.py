"""halo_harness.improve.apply -- H10 Part B: turns one drafted `Candidate`
into a real file on disk, ONLY on explicit approval (an `ImproveCard`'s
`a`/`e` key, or headless `improve --apply`). Provenance is INFORMATION on
the card/in the file's own trailing comment -- never a block, filter or
classifier. Only NEW files are created unless the target already carries
the halo provenance comment (a prior /improve write); a collision
with a user-authored file picks a new name instead of ever touching it.
"""

from __future__ import annotations

import datetime
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROVENANCE_MARKER = "<!-- halo improve:"
# 2.0.0 fixpass finding 6: a file `/improve` wrote under 1.0.1 carries THIS
# marker, never the new one -- `has_provenance_marker` must still recognize
# it as "already provenanced" (never user-authored), or every such file
# looks user-authored to 2.0.0 and the next candidate writes `foo-2.md`
# right alongside it instead of updating it in place. Only ever CHECKED,
# never written -- a fresh apply_candidate() always writes PROVENANCE_
# MARKER (the new one).
PROVENANCE_MARKER_LEGACY = "<!-- rolo-claude improve:"


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def provenance_comment(*, sessions, evidence_count: int, model: str, from_tool_output: bool,
                        created: Optional[str] = None) -> str:
    sess = ",".join(sorted(set(sessions)))
    return (f"{PROVENANCE_MARKER} created={created or _iso_now()} sessions={sess} "
            f"evidence={evidence_count} model={model} from_tool_output={str(bool(from_tool_output)).lower()} -->")


def has_provenance_marker(text: str) -> bool:
    text = text or ""
    return PROVENANCE_MARKER in text or PROVENANCE_MARKER_LEGACY in text


def candidate_hash(candidate) -> str:
    """sha256 of kind+target+body -- the ONE formula backing both the `d`
    dismissed-forever store (`~/.halo/improve/dismissed.json`) and
    the `improve_applied` log node's own `sha256` field, so "was this exact
    candidate already dismissed" and "what did apply actually write" are
    the same content-addressed identity."""
    raw = f"{candidate.kind}|{candidate.scope}|{candidate.path}|{candidate.body}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _rule_dir(scope: str, cwd: Path) -> Path:
    from halo_harness.config.paths import claude_config_dir
    return (Path(cwd) / ".claude" / "rules") if scope == "project" else (claude_config_dir() / "rules")


def _skill_dir(scope: str, cwd: Path) -> Path:
    from halo_harness.config.paths import claude_config_dir
    return (Path(cwd) / ".claude" / "skills") if scope == "project" else (claude_config_dir() / "skills")


@dataclass
class ResolvedTarget:
    path: Path
    exists: bool
    is_update: bool  # an existing file that already carries the provenance marker
    existing_text: Optional[str]
    renamed_from: Optional[str] = None  # set when a user-authored collision forced a new name


def _next_available(base: Path, *, is_dir: bool) -> "tuple[Path, bool, Optional[str]]":
    """Returns (path, is_update, renamed_from). `base` is tried first; on a
    collision with a file/dir that does NOT carry the provenance marker,
    `-2`, `-3`, ... is appended (before the suffix for a file, after the
    name for a directory) until an available or already-provenanced target
    is found."""

    def _marked(p: Path) -> bool:
        target = (p / "SKILL.md") if is_dir else p
        if not target.exists():
            return False
        try:
            return has_provenance_marker(target.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return False

    if not base.exists() or _marked(base):
        return base, base.exists(), None
    n = 2
    while True:
        candidate = (base.parent / f"{base.name}-{n}") if is_dir else base.with_name(f"{base.stem}-{n}{base.suffix}")
        if not candidate.exists() or _marked(candidate):
            return candidate, candidate.exists(), str(base)
        n += 1


def resolve_target_path(candidate, *, cwd: Path, settings=None) -> ResolvedTarget:
    if candidate.kind == "memory":
        from halo_harness.config.memory import MemoryStore
        store = MemoryStore(cwd, settings)
        filename = candidate.path if candidate.path.endswith(".md") else candidate.path + ".md"
        base = store.memory_dir_path / filename
        path, is_update, renamed_from = _next_available(base, is_dir=False)
    elif candidate.kind == "rule":
        filename = candidate.path if candidate.path.endswith(".md") else candidate.path + ".md"
        base = _rule_dir(candidate.scope, cwd) / filename
        path, is_update, renamed_from = _next_available(base, is_dir=False)
    elif candidate.kind == "skill":
        name = candidate.path.strip("/\\")
        base = _skill_dir(candidate.scope, cwd) / name
        path, is_update, renamed_from = _next_available(base, is_dir=True)
        path = path / "SKILL.md"
    else:
        raise ValueError(f"unknown candidate kind: {candidate.kind!r}")

    exists = path.exists()
    existing_text = None
    if exists:
        try:
            existing_text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing_text = ""
    return ResolvedTarget(path=path, exists=exists, is_update=(exists and has_provenance_marker(existing_text or "")),
                           existing_text=existing_text, renamed_from=renamed_from)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _yaml_safe(text: str) -> str:
    """H10 Part B: `config/frontmatter.py`'s own parser has no escape-
    sequence support (confirmed against `config/memory.py`'s own
    description-quoting), so a drafted title/description just gets its
    quotes/newlines neutralized rather than properly YAML-escaped --
    matches `MemoryStore.write`'s own sanitization exactly."""
    return (text or "").replace('"', "'").replace("\n", " ").strip()


def _rule_body(candidate, comment: str) -> str:
    body = candidate.body.strip("\n")
    fm_lines = ["---", f'name: "{_yaml_safe(candidate.title)}"']
    if getattr(candidate, "paths_glob", None):
        fm_lines.append(f"paths: {candidate.paths_glob}")
    fm_lines.append("---")
    return "\n".join(fm_lines) + "\n\n" + body + "\n\n" + comment + "\n"


def _skill_body(candidate, comment: str) -> str:
    body = candidate.body.strip("\n")
    name = Path(candidate.path.strip("/\\")).name
    fm = ["---", f'name: "{_yaml_safe(name)}"', f'description: "{_yaml_safe(candidate.title)}"',
          "disable-model-invocation: true"]
    if getattr(candidate, "argument_hint", None):
        fm.append(f'argument-hint: "{_yaml_safe(candidate.argument_hint)}"')
    fm.append("---")
    return "\n".join(fm) + "\n\n" + body + "\n\n" + comment + "\n"


def render_preview(candidate, comment: str) -> str:
    """The exact bytes `apply_candidate` would write, WITHOUT touching the
    filesystem -- an `ImproveCard`'s own diff/body preview (B3: "the card
    then shows a unified diff" for an update to an already-provenanced
    file). Shares the SAME renderers `apply_candidate` itself calls, so a
    preview can never drift from what actually gets written."""
    if candidate.kind == "memory":
        from halo_harness.config.memory import render_memory_content
        content, _ = render_memory_content(
            name=candidate.title, description=candidate.rationale or candidate.title, type="feedback",
            body=candidate.body, origin_session_id=(candidate.evidence[0].split("#")[0] if candidate.evidence else None),
            provenance_comment=comment,
        )
        return content
    if candidate.kind == "rule":
        return _rule_body(candidate, comment)
    if candidate.kind == "skill":
        return _skill_body(candidate, comment)
    raise ValueError(f"unknown candidate kind: {candidate.kind!r}")


def diff_preview(target: ResolvedTarget, candidate, comment: str) -> "list[str]":
    """Unified diff lines (existing file -> what apply would write); empty
    list when `target` isn't an update (nothing to diff against)."""
    import difflib

    if not target.is_update or target.existing_text is None:
        return []
    new_content = render_preview(candidate, comment)
    return list(difflib.unified_diff(
        target.existing_text.splitlines(keepends=True), new_content.splitlines(keepends=True),
        fromfile=str(target.path), tofile=f"{target.path} (proposed)",
    ))


@dataclass
class ApplyResult:
    path: Path
    kind: str
    candidate_id: str
    sha256: str
    renamed_from: Optional[str] = None


def apply_candidate(candidate, *, cwd: Path, settings=None, model_label: str = "?",
                     session=None) -> ApplyResult:
    """Writes `candidate` to disk (atomic tmp+`os.replace`), appends an
    `improve_applied` log node on `session` when one is given (headless
    `improve --apply` outside a live session passes `session=None` and
    just skips that -- and the snapshot refresh below), and refreshes the
    NEXT turn's claude_md/memory-index snapshot (same mechanism as
    post-compaction re-injection) for a memory/rule candidate so the
    running session sees the new file without a restart."""
    target = resolve_target_path(candidate, cwd=cwd, settings=settings)
    comment = provenance_comment(
        sessions=[e.split("#")[0] for e in candidate.evidence], evidence_count=len(candidate.evidence),
        model=model_label, from_tool_output=getattr(candidate, "from_tool_output", False),
    )

    if candidate.kind == "memory":
        from halo_harness.config.memory import MemoryStore
        store = MemoryStore(cwd, settings)
        if target.exists:
            # An UPDATE (target.is_update was already confirmed True by
            # resolve_target_path -- only a provenance-marked file reaches
            # here at all): remove the old file so MemoryStore.write's own
            # FileExistsError guard passes; the write immediately below
            # replaces it, so the window with no file on disk is as short
            # as a single Python statement, never observed by a concurrent
            # reader in this single-user, single-process tool.
            target.path.unlink(missing_ok=True)
        origin = candidate.evidence[0].split("#")[0] if candidate.evidence else None
        store.write(filename=target.path.name, name=candidate.title, description=candidate.rationale or candidate.title,
                    type=("feedback" if candidate.confidence != "high" else "project"),
                    body=candidate.body, origin_session_id=origin, provenance_comment=comment)
    elif candidate.kind in ("rule", "skill"):
        _atomic_write(target.path, render_preview(candidate, comment))
    else:
        raise ValueError(f"unknown candidate kind: {candidate.kind!r}")

    sha = candidate_hash(candidate)
    if session is not None:
        log = getattr(session, "log", None)
        if log is not None:
            log.append_improve_applied(kind=candidate.kind, path=str(target.path), candidate_id=candidate.id, sha256=sha)
        _refresh_snapshot(session, candidate.kind)
    return ApplyResult(path=target.path, kind=candidate.kind, candidate_id=candidate.id, sha256=sha,
                        renamed_from=target.renamed_from)


def _refresh_snapshot(session, kind: str) -> None:
    """Same mechanism `_run_compaction` uses to re-inject CLAUDE.md/memory
    after a compaction -- `session_context.refresh_*()` re-reads from disk
    (unlike compaction's own replay, which intentionally reuses the
    session-start cache), then a fresh `snapshot` node is appended so the
    NEXT turn's `derive_request` sees it."""
    ctx = getattr(session, "session_context", None)
    log = getattr(session, "log", None)
    if ctx is None or log is None:
        return
    if kind == "rule":
        refresh = getattr(ctx, "refresh_instructions", None)
        if callable(refresh):
            refresh()
        text = ctx.claude_md_text() if hasattr(ctx, "claude_md_text") else ""
        if text:
            log.append_snapshot([{"type": "text", "text": text}], kind="claude_md")
    elif kind == "memory":
        refresh = getattr(ctx, "refresh_memory", None)
        if callable(refresh):
            refresh()
        text = ctx.memory_snapshot_text() if hasattr(ctx, "memory_snapshot_text") else ""
        if text:
            log.append_snapshot([{"type": "text", "text": text}], kind="memory_index")
    # kind == "skill": no snapshot mechanism to refresh (ships with
    # disable-model-invocation: true -- see the H10 brief's own B3).
