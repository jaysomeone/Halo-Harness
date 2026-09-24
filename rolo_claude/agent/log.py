"""rolo_claude.agent.log -- the append-only session log (H1 scope E): the
SINGLE SOURCE OF TRUTH every model request is derived from. Replaces
agent/session_store.py's role in the loop (that module is left in the tree,
unreferenced, rather than deleted, since nothing outside it imports it after
this milestone and deleting working code isn't this brief's job).

dsh's own architecture doc states the invariant this module exists to make
checkable: "Model-visible means logged. Anything that reaches a model
request must be reconstructable from the log, and a runtime invariant
asserts it." `agent/derive.py` is the reconstruction half; this module is
the storage half.

Node types: meta, system, user, assistant (content blocks incl. raw
thinking/reasoning), tool_result, snapshot (dynamic context delivered as a
user-role block: permission mode, CLAUDE.md chain, memory index, skills,
notices), usage, error, interrupted, compacted (H5: a pure shadow-range
marker -- see `append_compacted` and `agent/derive.py`).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from rolo_claude.config.paths import bridge_home, project_slug

NODE_TYPES = frozenset({
    "meta", "system", "user", "assistant", "tool_result",
    "snapshot", "usage", "error", "interrupted", "compacted", "rewind",
})


class SessionLog:
    """One session's append-only JSONL file at
    ~/.rolo-claude/sessions/<slug>/<session_id>.jsonl, PLUS an in-memory
    mirror (`self._nodes`) kept in lockstep so `derive_request` never has to
    re-read the file mid-session. A write failure is best-effort (matches
    session_store.py's own contract) -- the in-memory mirror is still
    updated even if the disk write fails, so a session stays usable for the
    rest of its own process even on a full disk."""

    def __init__(self, cwd, session_id: Optional[str] = None):
        self.cwd = cwd
        self.slug = project_slug(cwd)
        self.session_id = session_id or uuid.uuid4().hex
        self.dir = bridge_home() / "sessions" / self.slug
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{self.session_id}.jsonl"
        self._nodes: list = []
        self._lock = threading.Lock()

    def _append(self, node: dict) -> dict:
        node = dict(node)
        assert node.get("type") in NODE_TYPES, f"unknown session log node type: {node.get('type')!r}"
        with self._lock:
            node.setdefault("ts", time.time())
            node["seq"] = len(self._nodes)
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(node, ensure_ascii=False) + "\n")
            except OSError:
                pass
            self._nodes.append(node)
        return node

    # ---- typed append helpers -----------------------------------------

    def append_meta(self, **fields) -> dict:
        return self._append({"type": "meta", **fields})

    def append_system(self, text: str) -> dict:
        return self._append({"type": "system", "text": text})

    def append_user(self, content: list) -> dict:
        return self._append({"type": "user", "content": content})

    def append_snapshot(self, content: list, *, kind: str) -> dict:
        """`kind` names the snapshot for humans/debugging (e.g.
        "claude_md", "memory_index", "permission_mode", "nested_claude_md")
        -- never read by `derive_request`, which treats every snapshot as
        an opaque user-role content block, in log order."""
        return self._append({"type": "snapshot", "kind": kind, "content": content})

    def append_assistant(self, *, content: list, reasoning: Optional[dict] = None,
                          stop_reason: Optional[str] = None, request_hash: Optional[str] = None) -> dict:
        node: dict = {"type": "assistant", "content": content, "stop_reason": stop_reason}
        if reasoning is not None:
            node["reasoning"] = reasoning
        if request_hash is not None:
            node["request_hash"] = request_hash
        return self._append(node)

    def append_tool_result(self, *, tool_use_id: str, content, is_error: bool = False) -> dict:
        return self._append({"type": "tool_result", "tool_use_id": tool_use_id, "content": content, "is_error": is_error})

    def append_usage(self, usage: dict, cost_usd=None) -> dict:
        return self._append({"type": "usage", "usage": usage, "cost_usd": cost_usd})

    def append_error(self, message: str, *, err_type: str = "error") -> dict:
        return self._append({"type": "error", "message": message, "err_type": err_type})

    def append_interrupted(self, **fields) -> dict:
        return self._append({"type": "interrupted", **fields})

    def append_compacted(self, *, trigger: str, custom_instructions: Optional[str] = None) -> dict:
        """H5 scope B: a pure MARKER node -- carries no transcript content
        of its own. `agent/derive.py` uses THIS node's own `seq` (assigned
        by `_append` below, BEFORE the caller appends anything else) as the
        exclusive upper bound of the "shadowed" range: every later call to
        `derive_request` skips every non-system/meta node whose `seq` is
        less than this one. The caller (agent/compact.py, via
        `Session._run_compaction`) always appends this node FIRST, then
        immediately appends the replacement content (a `user` node carrying
        the `<compacted-summary>`, `snapshot` nodes for the re-injected
        CLAUDE.md/rules/memory/plan, and copies of the retained verbatim
        tail) -- all of which land at LATER seqs than this marker and are
        therefore never shadowed by it. `trigger` is "manual" (`/compact`)
        or "auto" (the 80%-of-context gate) or "overflow"
        (`ContextOverflow` -> compact -> retry); mirrors the PreCompact hook
        payload's own `trigger` field (Claude Code: "manual"|"auto")."""
        return self._append({"type": "compacted", "surface_op": "replace",
                              "trigger": trigger, "custom_instructions": custom_instructions})

    def append_rewind(self, *, verb: str, step_id: str, files: Optional[list] = None) -> dict:
        """U5 scope B: a pure marker node -- `/rewind`/`/undo`/`/redo`
        touched the real working tree OUTSIDE the model conversation (a
        git-shadow restore, `rolo_claude.shadow.ShadowStore`), so this is
        purely an audit trail entry (never read by `derive_request`, same
        as a `snapshot` node's `kind`) recording WHAT happened for
        `/export`/`/stats` and a human skimming the raw log. `verb` is
        "rewind"|"undo"|"redo"; `files` is the list of real paths
        restored."""
        return self._append({"type": "rewind", "verb": verb, "step_id": step_id, "files": files or []})

    # ---- reading ---------------------------------------------------------

    def nodes(self, upto: Optional[int] = None) -> list:
        """All nodes appended so far, in order; `upto` (exclusive) limits it
        to the first N nodes -- used by tests to assert a derived request
        at an EARLIER point in the session matches what was actually sent
        then (the "model-visible means logged" runtime assertion)."""
        with self._lock:
            return list(self._nodes) if upto is None else list(self._nodes[:upto])

    def read_all(self) -> list:
        """Reload from disk (a fresh process resuming a session would use
        this; the live in-process mirror in `self._nodes` is authoritative
        for the CURRENT process and is what `derive_request` actually
        reads)."""
        if not self.path.exists():
            return []
        records = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return records

    @classmethod
    def latest_for_cwd(cls, cwd) -> "Optional[SessionLog]":
        slug = project_slug(cwd)
        session_dir = bridge_home() / "sessions" / slug
        if not session_dir.is_dir():
            return None
        candidates = sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            return None
        log = cls(cwd, session_id=candidates[0].stem)
        log._nodes = log.read_all()
        return log
