"""halo_harness.agent.invariants -- serialize-time invariants (H1 scope F):
dsh's "session poisoning" class of bugs (discussions #2900/#4843) made
concrete as functions the loop calls at well-defined points, instead of
hoped for.

  * every `tool_use` id gets exactly one `tool_result` before the next
    assistant node -- `synthesize_missing_results` writes
    `ABORTED_BEFORE_DISPATCH` / "Tool call interrupted by user" results for
    any that don't, on cancel/crash/interrupt.
  * `validate_tool_use` -- non-empty id, valid JSON-shaped `input` (a dict).
  * `repair_truncated_text` -- a lone UTF-16 surrogate (from truncating a
    stream mid-character) is replaced rather than left to blow up whatever
    reads it next.
"""

from __future__ import annotations

import re
from typing import Optional

ABORTED_BEFORE_DISPATCH = "ABORTED_BEFORE_DISPATCH"
INTERRUPTED_MESSAGE = "Tool call interrupted by user"
_KIMI_FUNCTIONS_IDX_RE = re.compile(r"^functions\.[^:]+:(\d+)$")


def tool_use_blocks(content: list) -> list:
    """Every `{"type": "tool_use", ...}` block in an assistant message's
    content list, in order."""
    return [b for b in (content or []) if isinstance(b, dict) and b.get("type") == "tool_use"]


def validate_tool_use(block: dict) -> Optional[str]:
    """None if `block` is a well-formed tool_use block; else a short error
    string naming what's wrong (non-empty id, a name, and a dict `input`)."""
    if not isinstance(block, dict):
        return "tool_use block is not an object"
    tool_id = block.get("id")
    if not isinstance(tool_id, str) or not tool_id:
        return "tool_use block has an empty or missing id"
    if not block.get("name"):
        return "tool_use block has no tool name"
    if "input" in block and not isinstance(block["input"], dict):
        return "tool_use block's input is not a JSON object"
    return None


def repair_truncated_text(text: str) -> str:
    """Replace any lone UTF-16 surrogate left behind by truncating a stream
    mid-character (dsh #4843: "unexpected end of hex escape") with U+FFFD,
    rather than letting it reach `json.dumps`/a terminal encoder and raise.
    A no-op for ordinary well-formed text (the common case)."""
    if not text:
        return text
    try:
        text.encode("utf-8")
        return text  # already well-formed -- no lone surrogates present
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogatepass").decode("utf-8", "replace")


def find_unpaired_tool_use_ids(log) -> list:
    """Walk EVERY assistant node in the log (finding 3, h4-h5-h3c review)
    and return the ids of every `tool_use` block with no matching
    `tool_result` node anywhere in the log. The pre-finding-3 version
    checked only the LAST assistant node, reasoning that every earlier one
    is a completed, already-paired turn by construction -- true as long as
    the only way to leave a gap was a crash/cancel BETWEEN the last
    assistant node and now. A steer noticed mid-`_dispatch_tools` used to
    break out of that loop without synthesizing results for the remaining
    calls and then let the turn continue (a NEW user node, then further
    turns/assistant nodes), which is exactly the case this whole-log scan
    catches and the last-node-only version could never self-heal: once an
    EARLIER node was left unpaired, `synthesize_missing_results` (run on
    every later crash/interrupt) kept missing it forever, corrupting every
    subsequent request on `ant:`/Databricks Claude routes. Order is not
    re-checked (a `tool_use` id is unique and its `tool_result` is always
    appended immediately after it by construction, so "exists anywhere in
    the log" is equivalent to "exists after it" for a well-formed log)."""
    nodes = log.nodes()
    answered = {n.get("tool_use_id") for n in nodes if n.get("type") == "tool_result"}
    missing = []
    for node in nodes:
        if node.get("type") != "assistant":
            continue
        for b in tool_use_blocks(node.get("content")):
            tid = b.get("id")
            if tid and tid not in answered:
                missing.append(tid)
    return missing


def highest_kimi_functions_idx(log) -> int:
    """H9 critical review finding 1 (whole-tree review, 2026-09-25): Kimi's
    native tool-id shape `functions.{name}:{idx}` uses a CONVERSATION-GLOBAL
    counter on the wire, but `providers/oai_stream.py`'s
    `OpenAIStreamToAnthropic._kimi_counter_box` used to start at `[0]` on
    EVERY fresh stream (a new instance per model call) -- any non-native
    upstream id (empty, or an OpenAI-style `call_...`, which Kimi's own
    endpoints and Databricks both send at least sometimes) was renamed
    starting from 0 again on every single step, so `functions.Read:0` (etc)
    collides across the whole session: prune's stub set, spilled-result
    filenames, `find_unpaired_tool_use_ids` and the TUI's own card grouping
    are all keyed by the bare id, so a fresh `functions.Read:0` silently
    reuses (and can be confused with) an OLDER one from several steps ago.

    This scans EVERY assistant node in the log (not just the last one --
    same reasoning as `find_unpaired_tool_use_ids`: an id minted several
    turns ago is exactly the one a fresh per-stream counter would collide
    with) for a `tool_use` id already in the native `functions.{name}:{idx}`
    shape, and returns the highest `idx` found, or -1 if none is. The
    caller (`agent/loop.py`'s `_build_request`) seeds the NEXT stream's
    rename counter from `highest + 1` instead of 0, so a rename anywhere in
    the session is guaranteed to mint an id that has never been used before
    in this conversation -- "continue the counter across the session",
    exactly like Kimi's own idx semantics already assume."""
    highest = -1
    for node in log.nodes():
        if node.get("type") != "assistant":
            continue
        for b in tool_use_blocks(node.get("content")):
            tid = b.get("id")
            if not isinstance(tid, str):
                continue
            m = _KIMI_FUNCTIONS_IDX_RE.match(tid)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest


def synthesize_missing_results(log, *, reason: str = INTERRUPTED_MESSAGE) -> list:
    """Write a synthetic `is_error` tool_result for every unanswered
    `tool_use` id from the last assistant node (cancel/crash/interrupt) so
    the log NEVER contains an unanswered tool call -- the single most
    common cause of a provider's "assistant message with tool_calls must be
    followed by tool messages" 400 on the very next turn. Returns the list
    of ids it synthesized results for (empty if the log was already fully
    paired, the normal case)."""
    missing = find_unpaired_tool_use_ids(log)
    for tool_id in missing:
        log.append_tool_result(tool_use_id=tool_id, content=reason, is_error=True)
    return missing
