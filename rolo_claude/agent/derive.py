"""rolo_claude.agent.derive -- derive_request(): rebuilds the Anthropic-
shaped (system_text, messages, tools) transcript from a SessionLog (H1
scope E). This is the "log is the single source of truth" half of dsh's
invariant: `providers.request.build_request_body` (and, for a native Claude
route, `providers.anthropic_sse.build_anthropic_body`) never sees anything
that isn't first reconstructable from here.

`content_hash` backs the runtime assertion ("model-visible means logged"):
the loop stores it on every assistant node it appends; a test re-derives
the log up to (excluding) that node and asserts the SAME hash comes back --
proving the request that produced that reply is byte-reconstructable from
the log alone, independent of run-to-run budget/sampling choices (which are
harness policy, not conversation content, and are legitimately allowed to
differ between an original run and a later replay).
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional


class LogAssemblyError(AssertionError):
    """Assembly fails LOUDLY (never silently) on a malformed log -- e.g.
    more than one `system` node, or none at all."""


def derive_request(log, tools: Optional[list] = None, upto: Optional[int] = None) -> "tuple[str, list, Optional[list]]":
    """Walk `log.nodes(upto=upto)` in order and rebuild:
      * `system_text` -- the ONE `system` node's text (raises LogAssemblyError
        if there isn't exactly one).
      * `messages` -- Anthropic-shaped user/assistant messages: `snapshot`
        and `user` nodes fold into the CURRENT pending user turn (in log
        order, snapshots and real user input interleave exactly as logged);
        `tool_result` nodes append a `tool_result` content block to the
        pending user turn (never their own message -- matches Anthropic's
        own wire shape, where tool results travel inside a user message);
        `assistant` nodes flush any pending user turn first, then become
        their own message (with `reasoning`, if the node carried any,
        attached at `message["reasoning"]` for `providers.request` to
        replay per-profile).
      * `tools` -- `tools` if given (the session's frozen catalog, owned by
        the tool registry / caller, never re-derived from the log every
        call); else the last `meta` node's own `tools` list, if any.
    `usage`/`error`/`interrupted`/`compacted` nodes never contribute a
    message directly (compaction is H5; an `interrupted` node's synthetic
    tool_results are themselves logged as ordinary `tool_result` nodes by
    `agent/invariants.py`, so they need no special handling here).
    """
    nodes = log.nodes(upto=upto) if hasattr(log, "nodes") else list(log)
    system_text: Optional[str] = None
    messages: list = []
    pending: list = []
    tools_out = tools

    def flush_user():
        nonlocal pending
        if pending:
            messages.append({"role": "user", "content": pending})
            pending = []

    for node in nodes:
        kind = node.get("type")
        if kind == "system":
            if system_text is not None:
                raise LogAssemblyError("session log has more than one 'system' node -- exactly one is required")
            system_text = node.get("text", "")
        elif kind == "meta":
            if tools is None and isinstance(node.get("tools"), list):
                tools_out = node["tools"]
        elif kind in ("snapshot", "user"):
            content = node.get("content")
            if isinstance(content, list):
                pending.extend(content)
        elif kind == "assistant":
            flush_user()
            msg: dict = {"role": "assistant", "content": node.get("content") or []}
            if node.get("reasoning") is not None:
                msg["reasoning"] = node["reasoning"]
            messages.append(msg)
        elif kind == "tool_result":
            block = {"type": "tool_result", "tool_use_id": node.get("tool_use_id"), "content": node.get("content")}
            if node.get("is_error"):
                block["is_error"] = True
            pending.append(block)
        # usage / error / interrupted / compacted: no direct message contribution
    flush_user()

    if system_text is None:
        raise LogAssemblyError("session log has no 'system' node -- assembly must fail loudly, never silently proceed without one")

    return system_text, messages, tools_out


def content_hash(system_text: str, messages: list, tools) -> str:
    """SHA-256 over the model-VISIBLE content (system + messages + tool
    defs), canonicalized (sorted keys, stable separators) so the same
    logical content always hashes the same regardless of dict key order."""
    canon = json.dumps(
        {"system": system_text, "messages": messages, "tools": tools},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
