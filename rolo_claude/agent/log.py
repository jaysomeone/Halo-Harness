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
from typing import Optional

from rolo_claude.config.paths import bridge_home, project_slug

NODE_TYPES = frozenset({
    "meta", "system", "user", "assistant", "tool_result",
    "snapshot", "usage", "error", "interrupted", "compacted", "rewind",
    "prune_commit", "improve_applied",
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
                # H11b finding 21: `errors="surrogateescape"` -- a tool
                # result built from `os.fsdecode`d bytes (e.g. Glob over a
                # directory with a non-UTF-8 filename) can carry a lone
                # surrogate codepoint; the default `errors="strict"` raised
                # UnicodeEncodeError HERE (verified on WSL), which reached
                # a cc: bridged call as an unhandled dispatch exception.
                # surrogateescape round-trips it back to the exact original
                # bytes instead, the same error handler `os.fsdecode`
                # itself used to produce it in the first place.
                with open(self.path, "a", encoding="utf-8", errors="surrogateescape") as f:
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

    def append_user(self, content: list, *, kind: Optional[str] = None) -> dict:
        """H9 whole-tree review finding 29: `kind`, when given, tags a
        "user"-type node as something OTHER than a genuine new user prompt
        -- a background sub-agent/job completion notice, a Stop-hook
        continuation, a steer's own text, or a compaction's own summary/
        re-appended tail (`compute_session_stats` -- controller.py, /stats'
        own data source -- used to count EVERY "user" node as a "turn",
        wildly over-counting for a session with any of these). Left `None`
        (the default, and every node from before this existed) for the
        ONE real case: `Session.turn()`'s own append of the actual new
        prompt -- `compute_session_stats` counts exactly that shape."""
        node = {"type": "user", "content": content}
        if kind is not None:
            node["kind"] = kind
        return self._append(node)

    def append_snapshot(self, content: list, *, kind: str) -> dict:
        """`kind` names the snapshot for humans/debugging (e.g.
        "claude_md", "memory_index", "permission_mode", "nested_claude_md")
        -- never read by `derive_request`, which treats every snapshot as
        an opaque user-role content block, in log order."""
        return self._append({"type": "snapshot", "kind": kind, "content": content})

    def append_assistant(self, *, content: list, reasoning: Optional[dict] = None,
                          stop_reason: Optional[str] = None, request_hash: Optional[str] = None,
                          tool_meta: Optional[dict] = None) -> dict:
        """H10 Part A: `tool_meta`, when given, is `{tool_use_id: {"repaired":
        bool, "repair_kind": "leak_parser"|"lenient_json"|"rename"|
        "args_repair"|"none", "promoted_from_leak": bool}}` -- repair-layer
        telemetry for the tool_use blocks in THIS content list, kept as a
        SIBLING key on the node rather than inside the content blocks
        themselves. `agent/derive.py`'s `derive_request` only ever reads
        `node["content"]`/`node["reasoning"]` off an assistant node, so a
        sibling key here is automatically invisible to every derived
        request (never a new model-visible field, never needs stripping) --
        unlike a key embedded IN a tool_use block, which would ride along
        verbatim through `providers.request.prepare_anthropic_messages`'s
        catch-all passthrough for a native Anthropic wire body."""
        node: dict = {"type": "assistant", "content": content, "stop_reason": stop_reason}
        if reasoning is not None:
            node["reasoning"] = reasoning
        if request_hash is not None:
            node["request_hash"] = request_hash
        if tool_meta:
            node["tool_meta"] = tool_meta
        return self._append(node)

    def append_tool_result(self, *, tool_use_id: str, content, is_error: bool = False,
                            tool: Optional[str] = None, error_class: Optional[str] = None,
                            ms: Optional[float] = None, num_bytes: Optional[int] = None,
                            spilled: Optional[bool] = None) -> dict:
        """H10 Part A: `tool`/`error_class`/`ms`/`num_bytes`/`spilled` are
        non-wire telemetry -- `derive_request` reconstructs a `tool_result`
        wire block from ONLY `tool_use_id`/`content`/`is_error` (see its own
        docstring: "non-wire keys stripped"), so any other key stored here
        is already invisible to every derived request; nothing to strip,
        nothing to prove byte-identical beyond the existing reconstruction.
        `num_bytes` (not `bytes`, a builtin) is stored under the JSON key
        `"bytes"` for telemetry.py's own reading convenience."""
        node: dict = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content, "is_error": is_error,
                      "ok": not is_error}
        if tool is not None:
            node["tool"] = tool
        if error_class is not None:
            node["error_class"] = error_class
        if ms is not None:
            node["ms"] = ms
        if num_bytes is not None:
            node["bytes"] = num_bytes
        if spilled is not None:
            node["spilled"] = spilled
        return self._append(node)

    def append_usage(self, usage: dict, cost_usd=None, *, agent_id: Optional[str] = None,
                      model: Optional[str] = None, route: Optional[str] = None,
                      provider: Optional[str] = None, finish_reason: Optional[str] = None,
                      latency_ms: Optional[float] = None, ttft_ms: Optional[float] = None,
                      retries: Optional[int] = None, status: Optional[str] = None,
                      estimate: Optional[bool] = None) -> dict:
        """H9 whole-tree review finding 13: `agent_id`, when given, tags
        this usage node as a SUB-AGENT's rolled-up total (agent/subagent.py
        calls this on the PARENT's own log once a child finishes) rather
        than the session's own direct model call -- `compute_session_stats`
        (controller.py) sums every "usage" node's `cost_usd`/tokens exactly
        the same either way (a child's total was, before this, invisible
        to `/stats` and `stats` entirely -- logged only in the child's own,
        separately-globbed `subagents/*.jsonl`), but keeps the tag so a
        reader of the raw log can tell a rolled-up child total apart from
        one of the parent's own real model calls.

        H10 Part A: `model`/`route`/`provider`/`finish_reason`/`latency_ms`/
        `ttft_ms`/`retries`/`status` are telemetry.py's own source of truth
        for per-model/per-provider aggregation -- never read by
        `derive_request` (a `usage` node contributes no message at all), so
        adding them is automatically wire-safe. `status` is one of
        "ok"|"429"|"5xx"|"overflow"|"aborted"|"connect_error".

        H11 Part B: `estimate=True` marks a `cc:`-route usage node whose
        `cost_usd` came from Claude Code's own `total_cost_usd` -- a
        subscription is not billed per token, so that figure is Claude
        Code's own estimate, not a real charge; `stats --models`/`/cost`
        surface this instead of presenting it as exact spend like every
        other route's real per-token pricing."""
        node = {"type": "usage", "usage": usage, "cost_usd": cost_usd}
        if estimate is not None:
            node["estimate"] = estimate
        if agent_id is not None:
            node["agent_id"] = agent_id
        if model is not None:
            node["model"] = model
        if route is not None:
            node["route"] = route
        if provider is not None:
            node["provider"] = provider
        if finish_reason is not None:
            node["finish_reason"] = finish_reason
        if latency_ms is not None:
            node["latency_ms"] = latency_ms
        if ttft_ms is not None:
            node["ttft_ms"] = ttft_ms
        if retries is not None:
            node["retries"] = retries
        if status is not None:
            node["status"] = status
        return self._append(node)

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

    def append_prune_commit(self, stub_ids) -> dict:
        """H9 whole-tree review finding 28: a pure marker node, never read
        by `derive_request` (same as `compacted`/`rewind` above) -- records
        exactly which tool_use_ids `Session._pruned_messages_for_wire`
        (agent/loop.py) just committed to `self._prune_committed_stub_ids`,
        so a resumed process can reconstruct that in-memory-only set from
        the log alone (plan rule 1: any request must be re-derivable from
        the log) instead of starting it empty and silently sending a
        DIFFERENT (larger -- every previously-committed id un-stubbed
        again) wire prefix than what the model actually saw before the
        restart. Only the NEWLY-committed batch is logged per call (not
        the whole running set) -- `Session.__init__`'s own resume branch
        unions every `prune_commit` node's `stub_ids` back together, so
        logging just the increment is sufficient and keeps these nodes
        small. Stored sorted so two processes committing the exact same
        set always log byte-identical JSON."""
        return self._append({"type": "prune_commit", "stub_ids": sorted(stub_ids)})

    def append_improve_applied(self, *, kind: str, path: str, candidate_id: str, sha256: str) -> dict:
        """H10 Part B: a pure marker node (never read by `derive_request`,
        same as `rewind`/`prune_commit` above) recording that `/improve`'s
        `a`/`e` key (or headless `improve --apply`) actually wrote
        `path` -- `kind` is "memory"|"rule"|"skill", `sha256` is the
        applied body's own hash (the same one `~/.rolo-claude/improve/
        dismissed.json` keys a `d` dismissal by)."""
        return self._append({"type": "improve_applied", "kind": kind, "path": path,
                              "candidate_id": candidate_id, "sha256": sha256})

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
