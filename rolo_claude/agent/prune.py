"""rolo_claude.agent.prune -- deterministic, cache-prefix-preserving pruning
of the DERIVED request (H5 scope A). Applied AFTER agent/derive.py's
derive_request() and BEFORE providers/request.py's build_request_body(), so
the session LOG always keeps the full, unpruned text -- only what actually
goes out over the wire shrinks (`agent/log.py` is never touched by this
module).

Two independent rules, both from dsh's own tool-result pruner (reports/
DeepSeek and OpenRouter ori harnesses.md, "Compaction is the part of dsh
most worth copying wholesale" paragraph) plus OpenCode's protection-window
constants (reports/OpenCode harness deep review.md Appendix D):

  1. ANY tool_result content longer than PRUNE_CHAR_THRESHOLD (8,192 chars)
     is truncated to its own head PRUNE_HEAD_CHARS (4,096) / tail
     PRUNE_TAIL_CHARS (1,024) chars, regardless of position -- a single huge
     tool result never blows the budget even on the very next turn.
  2. A tool_result whose distance from the END of the message list (summed,
     in estimated tokens, over tool_result content strictly newer than it)
     exceeds PRUNE_PROTECT_TOKENS (40,000, OpenCode's PRUNE_PROTECT) is
     replaced entirely with a short excerpt (2,000 chars) plus OpenCode's
     own marker string, "[Old tool result content cleared]" -- overriding
     rule 1's head/tail result for anything that far back.

Determinism/cache-prefix stability: both rules are pure functions of the
CURRENT message list's content (size + position), never of wall-clock time,
a call counter, or RNG, so the same input always prunes the same way; once a
tool result crosses PRUNE_PROTECT_TOKENS and gets stubbed, it stays stubbed
identically on every later call (nothing about an already-stubbed result can
un-stub or re-stub it differently) -- so appending new turns only ever
invalidates the provider's cache prefix at the single boundary result that
just crossed the line on THIS call, never retroactively across the whole
prefix.

Images older than IMAGE_OFFLOAD_TURNS user turns are replaced with a text
placeholder ("images offloaded after N turns" -- dsh's own phrase, exact
wording is this module's, no upstream string is quoted for it).
"""

from __future__ import annotations

import copy
from typing import Optional

# dsh: "keeps a head 4,096 / tail 1,024 code points of any result above
# 8,192" (reports/DeepSeek and OpenRouter ori harnesses.md).
PRUNE_CHAR_THRESHOLD = 8192
PRUNE_HEAD_CHARS = 4096
PRUNE_TAIL_CHARS = 1024

# OpenCode Appendix D: PRUNE_PROTECT = 40_000 (newest completed tool-output
# tokens never pruned), TOOL_OUTPUT_MAX_CHARS-shaped excerpt length (2,000
# chars) and its own marker string, both adopted verbatim.
PRUNE_PROTECT_TOKENS = 40_000
PRUNE_STUB_CHARS = 2000
OLD_TOOL_RESULT_CLEARED = "[Old tool result content cleared]"
INTERRUPTED_MARKER = "[Tool execution was interrupted]"

# OpenCode Appendix D: CHARS_PER_TOKEN = 4, "used only where no usage
# exists" -- the same rough estimator this module uses for pruning
# decisions (a real usage.prompt_tokens count is not available mid-pruning,
# only after the provider replies).
CHARS_PER_TOKEN = 4

IMAGE_OFFLOAD_TURNS = 3
IMAGE_OFFLOAD_PLACEHOLDER = "[Image omitted: offloaded after {n} turns to save context]"


def estimate_text_tokens(text: str) -> int:
    return max(0, (len(text or "") + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def _content_text(content) -> str:
    """Best-effort plain-text length proxy for a tool_result's `content`,
    which may be a bare string or an Anthropic-shaped list of blocks
    (`{"type": "text", "text": ...}`, `{"type": "image", ...}`, ...)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return ""


def _truncate_head_tail(text: str) -> str:
    if len(text) <= PRUNE_CHAR_THRESHOLD:
        return text
    head = text[:PRUNE_HEAD_CHARS]
    tail = text[-PRUNE_TAIL_CHARS:] if PRUNE_TAIL_CHARS else ""
    cut = len(text) - PRUNE_HEAD_CHARS - PRUNE_TAIL_CHARS
    return f"{head}\n... [{cut} chars pruned] ...\n{tail}"


def _stub_excerpt(text: str) -> str:
    excerpt = text[:PRUNE_STUB_CHARS]
    return f"{excerpt}\n{OLD_TOOL_RESULT_CLEARED}"


def _rewrite_tool_result_content(content, new_text: str):
    """Preserve the original shape (str stays str; a list of blocks gets its
    text block(s) replaced, non-text blocks -- e.g. an image the size rule
    doesn't apply to -- left alone) while swapping in `new_text`."""
    if isinstance(content, str):
        return new_text
    if isinstance(content, list):
        out = []
        replaced = False
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                if not replaced:
                    out.append({**block, "text": new_text})
                    replaced = True
                # further text blocks in the same result collapse away --
                # the excerpt already speaks for the whole result.
                continue
            out.append(block)
        if not replaced:
            out = [{"type": "text", "text": new_text}] + out
        return out
    return new_text


def _each_tool_result_block(message: dict):
    """Yield (block_index, block) for every tool_result content block in a
    user-role message -- the only place derive_request ever puts one (see
    agent/derive.py: tool_result nodes fold into the pending USER turn)."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for i, block in enumerate(content):
        if isinstance(block, dict) and block.get("type") == "tool_result":
            yield i, block


def prune_messages(
    messages: list,
    *,
    char_threshold: int = PRUNE_CHAR_THRESHOLD,
    head_chars: int = PRUNE_HEAD_CHARS,
    tail_chars: int = PRUNE_TAIL_CHARS,
    protect_tokens: int = PRUNE_PROTECT_TOKENS,
    image_offload_turns: int = IMAGE_OFFLOAD_TURNS,
) -> list:
    """Return a NEW messages list (the input is never mutated) with tool
    results pruned per the two rules above and old images offloaded. Purely
    a function of `messages` -- no hidden state, no clock."""
    n = len(messages)
    out = [copy.deepcopy(m) if isinstance(m, dict) else m for m in messages]

    # ---- rule 2 (protection window) needs the running token total of
    # tool_result content STRICTLY NEWER than each block, so walk newest
    # (end of list) to oldest first and accumulate.
    running_newer_tokens = 0
    for idx in range(n - 1, -1, -1):
        msg = out[idx]
        for _, block in _each_tool_result_block(msg):
            text = _content_text(block.get("content"))
            if not text:
                continue
            this_tokens = estimate_text_tokens(text)
            if running_newer_tokens > protect_tokens:
                block["content"] = _rewrite_tool_result_content(block.get("content"), _stub_excerpt(text))
            elif len(text) > char_threshold:
                # rule 1: still within the protection window, but this one
                # result alone is huge -- head/tail it (uses the caller's
                # own head_chars/tail_chars, not the module defaults, so a
                # test can exercise non-default thresholds).
                head = text[:head_chars]
                tail = text[-tail_chars:] if tail_chars else ""
                cut = len(text) - head_chars - tail_chars
                truncated = f"{head}\n... [{cut} chars pruned] ...\n{tail}" if cut > 0 else text
                block["content"] = _rewrite_tool_result_content(block.get("content"), truncated)
            running_newer_tokens += this_tokens

    # ---- image offload: walk oldest -> newest counting USER-turn
    # boundaries; once `image_offload_turns` boundaries have been crossed
    # (i.e. this message is older than that many turns), replace any image
    # content block with a text placeholder.
    turns_from_start = 0
    total_user_turns = sum(1 for m in out if isinstance(m, dict) and m.get("role") == "user")
    seen_user_turns = 0
    for msg in out:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            seen_user_turns += 1
        turns_from_end = total_user_turns - seen_user_turns
        if turns_from_end < image_offload_turns:
            continue  # recent enough to keep verbatim
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        new_content = []
        changed = False
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image":
                new_content.append({"type": "text", "text": IMAGE_OFFLOAD_PLACEHOLDER.format(n=image_offload_turns)})
                changed = True
            else:
                new_content.append(block)
        if changed:
            msg["content"] = new_content

    return out


def context_breakdown(system_text: str, messages: list, tools: Optional[list], pruned_messages: list) -> dict:
    """Token estimate per bucket for `/context` (scope D): system, tools,
    messages (the pruned/wire version -- what will actually be sent), and
    snapshots (a subset of `messages`' user-turn content already folded in
    by derive_request, estimated separately for display only -- see
    agent/derive.py, snapshot nodes fold into the SAME pending user message
    as ordinary user content, so this is an approximation from block shape,
    not a separately-tagged wire field). `pruned` is how many tokens rule
    1/2 above actually removed relative to the unpruned messages."""
    system_tokens = estimate_text_tokens(system_text)
    tools_tokens = estimate_text_tokens(_json_len(tools)) if tools else 0
    messages_tokens = sum(estimate_text_tokens(_json_len(m)) for m in pruned_messages)
    unpruned_tokens = sum(estimate_text_tokens(_json_len(m)) for m in messages)
    pruned_tokens = max(0, unpruned_tokens - messages_tokens)
    return {
        "system": system_tokens,
        "tools": tools_tokens,
        "messages": messages_tokens,
        "pruned": pruned_tokens,
        "total": system_tokens + tools_tokens + messages_tokens,
    }


def _json_len(obj) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(obj)
