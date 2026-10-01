"""halo_harness.agent.prune -- deterministic, cache-prefix-preserving pruning
of the DERIVED request (H5 scope A). Applied AFTER agent/derive.py's
derive_request() and BEFORE providers/request.py's build_request_body(), so
the session LOG always keeps the full, unpruned text -- only what actually
goes out over the wire shrinks (`agent/log.py` is never touched by this
module).

ONE rule, from dsh's own tool-result pruner (reports/DeepSeek and OpenRouter
ori harnesses.md, "Compaction is the part of dsh most worth copying
wholesale" paragraph) plus OpenCode's protection-window constants (reports/
OpenCode harness deep review.md Appendix D):

  A tool_result whose distance from the END of the message list (summed, in
  estimated tokens, over tool_result content strictly newer than it) exceeds
  PRUNE_PROTECT_TOKENS (40,000, OpenCode's PRUNE_PROTECT) is replaced
  entirely with a short excerpt (2,000 chars) plus OpenCode's own marker
  string, "[Old tool result content cleared]". A result INSIDE the window is
  never touched here at all: it already went through its own tool's
  `result_cap` (tools/truncate.py's `spill_and_truncate`, or the MCP-specific
  equivalent) at the moment it was logged, which is the ONE place its size is
  this harness's business to bound.

H5b finding 1 (critical) removed a SECOND, blanket rule this module used to
also apply -- "any tool_result over 8,192 chars gets head/tail-truncated to
4,096+1,024 chars, regardless of position" -- because it fired on results
INSIDE the protection window too: a 28,690-char Read (well under any tool's
own cap, and the model's very last action) was silently cut to ~5,120 chars
on the very next request, with no spill pointer, while the TUI kept showing
the model's own untouched log copy -- "nothing looks wrong" is exactly how
it shipped. A fresh Read/Bash/Grep/WebFetch/MCP result must always reach the
model in full (bounded only by its own tool's cap) on the request immediately
after the tool ran.

H5b finding 2 (major) is why stub DECISIONS are no longer recomputed from
scratch, from raw token math, on every single request: the exact boundary
token count grows by a little on every step, so a NEW message crossing
40,000 (and therefore changing shape) on almost every step invalidated the
provider's cache prefix from that point on, at a real cost (finding 2:
~42,000 uncached tokens resent per step in the verified repro). Callers that
own persisted state across requests (`agent.loop.Session`) commit stub
decisions in BATCHES of at least `PRUNE_REBALANCE_CHUNK_TOKENS` (20,000, via
`compute_stub_candidates` below) and pass the resulting STABLE id set back in
as `force_stub_ids` -- unchanged for potentially many requests in a row, so
the wire prefix up to the oldest still-growing content stays byte-identical
across them. A caller with no such state (a one-off `/context` preview, a
unit test) may omit `force_stub_ids` and get the un-batched, freshly-computed
set instead -- correct for a single call, just not prefix-stable across many.

Determinism/cache-prefix stability: for a given `messages` and `force_stub_ids`,
this is a pure function of both (never wall-clock time, a call counter, or
RNG) -- the caller's OWN persisted id set is what makes repeated calls with
growing `messages` prefix-stable, not anything hidden in this module.

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

# H5b finding 2: a caller with persisted state (agent.loop.Session) commits
# newly-qualifying stub candidates in batches of at least this many
# estimated tokens (dsh/OpenCode-shaped "chunked rebalance", per the
# finding's own suggested fix) rather than the moment each one individually
# crosses PRUNE_PROTECT_TOKENS -- see `compute_stub_candidates` below.
PRUNE_REBALANCE_CHUNK_TOKENS = 20_000

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


def _is_real_user_prompt(message: dict) -> bool:
    """H9 whole-tree review finding 20: True for a genuine user turn --
    `role == "user"` AND at least one content block that ISN'T a
    tool_result (a bare-string/empty `content` counts as a real prompt
    too, never a tool_result shape). False for a wire message that is
    PURELY a tool round-trip's own result, even though it also carries
    `role: "user"` on the wire."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return True
    if not content:
        return True
    return any(not (isinstance(b, dict) and b.get("type") == "tool_result") for b in content)


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


def compute_stub_candidates(
    messages: list, *, protect_tokens: int = PRUNE_PROTECT_TOKENS,
) -> "dict[str, int]":
    """`{tool_use_id: estimated_tokens}` for every tool_result that
    CURRENTLY sits outside the protection window, using raw (uncommitted)
    token math over `messages` exactly as they stand right now. H5b finding
    2: a caller with persisted state (agent.loop.Session) uses this to
    decide whether enough NEW candidates have accumulated since the last
    commit to justify rebalancing `force_stub_ids` by a full
    PRUNE_REBALANCE_CHUNK_TOKENS-sized batch -- never call this to decide
    what to actually prune on a given request; that is `prune_messages`'s
    `force_stub_ids` param, which must stay STABLE across many requests in
    a row for cache-prefix stability. A tool_result with no `tool_use_id`
    (malformed input) is simply never a stub candidate."""
    n = len(messages)
    out: "dict[str, int]" = {}
    running_newer_tokens = 0
    for idx in range(n - 1, -1, -1):
        msg = messages[idx]
        for _, block in _each_tool_result_block(msg):
            text = _content_text(block.get("content"))
            if not text:
                continue
            this_tokens = estimate_text_tokens(text)
            if running_newer_tokens > protect_tokens:
                tool_use_id = block.get("tool_use_id")
                if tool_use_id:
                    out[tool_use_id] = this_tokens
            running_newer_tokens += this_tokens
    return out


def prune_messages(
    messages: list,
    *,
    protect_tokens: int = PRUNE_PROTECT_TOKENS,
    force_stub_ids: "Optional[frozenset]" = None,
    image_offload_turns: int = IMAGE_OFFLOAD_TURNS,
) -> list:
    """Return a NEW messages list (the input is never mutated) with tool
    results outside the protection window stubbed and old images offloaded.

    `force_stub_ids` (H5b finding 2): when given, EXACTLY this set of
    tool_use_ids is stubbed, full stop -- no token math is re-run to decide
    it, so a caller that keeps this set stable across many requests (see
    `compute_stub_candidates`) gets a byte-identical wire prefix across all
    of them. `None` (a one-off caller with no persisted state -- `/context`'s
    own preview, a unit test) falls back to computing the raw candidate set
    fresh from `messages` via `compute_stub_candidates` -- correct for a
    single call, just not prefix-stable across a series of growing ones.

    H5b finding 1 (critical): a result INSIDE the window (not in the stub
    set) is never touched here at all -- see this module's own docstring
    for why the old blanket "any result over 8,192 chars" rule is gone."""
    out = [copy.deepcopy(m) if isinstance(m, dict) else m for m in messages]
    stub_ids = force_stub_ids if force_stub_ids is not None else set(
        compute_stub_candidates(out, protect_tokens=protect_tokens))

    if stub_ids:
        for msg in out:
            for _, block in _each_tool_result_block(msg):
                if block.get("tool_use_id") not in stub_ids:
                    continue
                text = _content_text(block.get("content"))
                if not text:
                    continue
                block["content"] = _rewrite_tool_result_content(block.get("content"), _stub_excerpt(text))

    # ---- image offload: walk oldest -> newest counting USER-turn
    # boundaries; once `image_offload_turns` boundaries have been crossed
    # (i.e. this message is older than that many turns), replace any image
    # content block with a text placeholder.
    #
    # H9 whole-tree review finding 20: `msg.get("role") == "user"` counted
    # EVERY wire message in that role as its own "turn" -- but a tool_result
    # is ALSO sent back as a `role: "user"` message (agent/derive.py folds
    # it into the pending user turn; see `_each_tool_result_block`'s own
    # docstring), so a turn with several tool round-trips inflated
    # `total_user_turns` by one per round-trip. Verified: a screenshot in
    # "fix the bug in this screenshot" was offloaded after just 3 tool
    # steps of the SAME turn, not `image_offload_turns` real turns later.
    # `_is_real_user_prompt` counts a message only when it carries at least
    # one NON-tool_result block (a genuine prompt), never a message made
    # ENTIRELY of tool_result blocks. The other half of this finding: an
    # image nested INSIDE a tool_result block's own `content` (e.g. a Read
    # or MCP tool result carrying one) was never reached by the old
    # top-level-only block loop -- walked and offloaded the same way now.
    total_user_turns = sum(1 for m in out if isinstance(m, dict) and _is_real_user_prompt(m))
    seen_user_turns = 0
    for msg in out:
        if not isinstance(msg, dict):
            continue
        if _is_real_user_prompt(msg):
            seen_user_turns += 1
        turns_from_end = total_user_turns - seen_user_turns
        if turns_from_end < image_offload_turns:
            continue  # recent enough to keep verbatim
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        placeholder = {"type": "text", "text": IMAGE_OFFLOAD_PLACEHOLDER.format(n=image_offload_turns)}
        new_content = []
        changed = False
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image":
                new_content.append(dict(placeholder))
                changed = True
            elif isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                inner_out = []
                inner_changed = False
                for inner in block["content"]:
                    if isinstance(inner, dict) and inner.get("type") == "image":
                        inner_out.append(dict(placeholder))
                        inner_changed = True
                    else:
                        inner_out.append(inner)
                if inner_changed:
                    block = {**block, "content": inner_out}
                    changed = True
                new_content.append(block)
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
