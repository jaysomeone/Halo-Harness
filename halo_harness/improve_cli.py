"""rolo_claude.improve_cli -- `rolo-claude improve` (H10 Part B5): the
headless surface for the SAME human-gated loop the TUI's `/improve` runs
interactively. `-p` sessions never draft or write on their own; this is the
ONLY place a headless invocation may draft or apply, and only on an
explicit `--apply`. `--bare` disables everything (no scan, no draft).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _improve_dir() -> Path:
    from rolo_claude.config.paths import bridge_home
    return bridge_home() / "improve"


def _save_candidates(candidates: list) -> Path:
    _improve_dir().mkdir(parents=True, exist_ok=True)
    path = _improve_dir() / f"{int(time.time())}.json"
    path.write_text(json.dumps([c.to_dict() for c in candidates], ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
    return path


def _print_candidates_text(candidates: list) -> None:
    if not candidates:
        print("rolo-claude improve: no candidates.")
        return
    for c in candidates:
        print(f"[{c.id}] {c.kind} -> {c.scope}/{c.path}  (confidence={c.confidence})")
        print(f"    title: {c.title}")
        if c.rationale:
            print(f"    rationale: {c.rationale}")
        if c.evidence:
            print(f"    evidence: {', '.join(c.evidence)}")
        if c.from_tool_output:
            print("    (derived from tool output)")


def _draft(*, cwd: Path, since: str, all_projects: bool, max_candidates: int) -> "tuple[list, list, object]":
    """Returns (clusters, candidates, session) -- `session` is the
    throwaway (`--bare`) Session built just for the ONE drafting model
    call; callers that need to `--apply` reuse it as the `session=` kwarg
    to `apply_candidate` so the write still gets an `improve_applied` log
    node (a real, if short, session -- headless is still a real
    invocation, traceable like any other model call this harness makes)."""
    from rolo_claude.config.paths import project_slug
    from rolo_claude.improve import draft as draft_mod
    from rolo_claude.improve import evidence as evidence_mod
    from rolo_claude.improve.config import load_improve_config

    cfg = load_improve_config()
    slug = None if all_projects else project_slug(cwd)
    clusters = evidence_mod.build_clusters(since=since, slug=slug, all_projects=all_projects)
    if not clusters:
        return [], [], None

    from rolo_claude.headless import build_session
    build = build_session(cwd=cwd, bare=True, print_mode=True, max_turns=1)
    session = build.session
    candidates, error = draft_mod.draft_candidates(
        session, clusters, max_candidates=max_candidates or cfg.max_candidates, configured_model=cfg.model)
    if error and not candidates:
        print(f"rolo-claude improve: {error}", file=sys.stderr)
    return clusters, candidates, session


def cmd_improve(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="rolo-claude improve", add_help=True,
                                      description="Human-gated self-improvement: draft memory/rule/skill "
                                                   "candidates from recent session failures (never applied "
                                                   "without --apply).")
    parser.add_argument("--since", default="7d", choices=("7d", "30d", "all"))
    parser.add_argument("--all-projects", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default=None, metavar="FILE")
    parser.add_argument("--apply", action="append", default=None, metavar="FILE#ID",
                         help="Write exactly this candidate (repeatable). Skips drafting.")
    parser.add_argument("--cwd", default=None, metavar="DIR")
    parser.add_argument("--bare", action="store_true", help="Disable everything -- no scan, no draft, no apply")
    parser.add_argument("--max-candidates", type=int, default=None)
    args = parser.parse_args(argv)

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()

    if args.bare:
        print("rolo-claude improve: --bare disables /improve entirely.")
        return 0

    if args.apply:
        from rolo_claude.improve import apply as apply_mod
        from rolo_claude.improve.draft import Candidate

        applied = []
        for spec in args.apply:
            if "#" not in spec:
                print(f"rolo-claude improve: --apply expects FILE#ID, got {spec!r}", file=sys.stderr)
                return 2
            file_part, cand_id = spec.rsplit("#", 1)
            try:
                raw_list = json.loads(Path(file_part).read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                print(f"rolo-claude improve: could not read {file_part!r}: {e}", file=sys.stderr)
                return 2
            raw = next((r for r in raw_list if r.get("id") == cand_id), None)
            if raw is None:
                print(f"rolo-claude improve: no candidate {cand_id!r} in {file_part!r}", file=sys.stderr)
                return 2
            target = raw.get("target") or {}
            candidate = Candidate(
                id=raw["id"], kind=raw["kind"], title=raw.get("title", raw["id"]),
                scope=target.get("scope", "project"), path=target.get("path", raw["id"]),
                body=raw.get("body", ""), rationale=raw.get("rationale", ""),
                evidence=raw.get("evidence") or [], confidence=raw.get("confidence", "low"),
                from_tool_output=bool(raw.get("from_tool_output")),
            )
            result = apply_mod.apply_candidate(candidate, cwd=cwd, model_label="headless")
            applied.append(result)
            print(f"applied [{result.kind}] -> {result.path}")
        return 0

    clusters, candidates, _session = _draft(cwd=cwd, since=args.since, all_projects=args.all_projects,
                                             max_candidates=args.max_candidates)
    saved_path = _save_candidates(candidates) if candidates else None

    if args.json:
        print(json.dumps({
            "candidates": [c.to_dict() for c in candidates],
            "saved_to": str(saved_path) if saved_path else None,
        }, ensure_ascii=False))
    else:
        _print_candidates_text(candidates)
        if saved_path:
            print(f"\nSaved to {saved_path}")
            print(f"Apply one with: rolo-claude improve --apply {saved_path}#<id>")

    if args.out and candidates:
        Path(args.out).write_text(json.dumps([c.to_dict() for c in candidates], ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
    return 0
