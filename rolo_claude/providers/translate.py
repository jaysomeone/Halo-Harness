"""rolo_claude.providers.translate -- Anthropic Messages -> OpenAI chat
request translation (anthropic_to_openai and the message-flattening
algorithm). Moved out of bridge.py unchanged in the H0 package split; see
wip/SIGNATURES.md part2 for the full message-flattening algorithm contract.
"""

from __future__ import annotations

import json

from rolo_claude.providers.config import estimate_tokens
from rolo_claude.providers.routing import (
    Route,
    anthropic_tool_to_openai,
    clamp_max_tokens,
    map_tool_choice,
    select_tools,
)


class WebSearchUnavailable(Exception):
    pass


def anthropic_to_openai(body: dict, route: Route, profile: dict | None = None) -> dict:
    """Convert Anthropic request to OpenAI format; raises WebSearchUnavailable on web_search."""
    # Check for web_search tools before any work
    tools = body.get("tools")
    if tools:
        for t in tools:
            if isinstance(t, dict) and t.get("type", "").startswith("web_search"):
                raise WebSearchUnavailable()

    # Build messages per algorithm
    messages = _flatten_messages(body.get("messages", []), body.get("system"))

    # Build tools list
    oai_tools = None
    if tools:
        selected = select_tools(tools, body.get("messages", []))
        if selected:
            oai_tools = [anthropic_tool_to_openai(t) for t in selected]

    # Up-front max_tokens clamp: min(requested, profile cap, context headroom).
    # `profile` defaults to the foundation's hardcoded 16384/128000 pair so
    # behavior is unchanged for callers that don't pass one.
    profile = profile or {"context_tokens": 128000, "max_output_tokens": 16384}
    prelim_estimate = estimate_tokens({"messages": messages, "tools": oai_tools or []})
    requested_max = body.get("max_tokens", profile["max_output_tokens"])
    clamped_max = clamp_max_tokens(requested_max, profile, prelim_estimate)

    # Build final body
    oai_body = {
        "model": route.upstream_model,
        "messages": messages,
        "stream": True,
        "max_tokens": clamped_max,
    }
    if oai_tools:
        oai_body["tools"] = oai_tools
        oai_body["provider"] = {"require_parameters": True}
        # tool_choice is only meaningful (and only accepted by some strict
        # backends) when tools are actually present in the forwarded body.
        tc = map_tool_choice(body.get("tool_choice"))
        if tc is not None:
            oai_body["tool_choice"] = tc
    for key in ["temperature", "top_p", "stop"]:
        if key in body:
            oai_body[key] = body[key]
    # Explicitly drop
    for key in ["top_k", "metadata", "thinking"]:
        oai_body.pop(key, None)
    return oai_body


def _system_text_parts(value) -> list:
    """Extract system-message text parts from a `system` field or an
    in-message `role: system` content value: a plain string is used as-is;
    a list yields the text of each text-type block, dropping any block whose
    text starts with "x-anthropic-billing-header" and ignoring non-text
    blocks. Shared by both call sites in `_flatten_messages` below so the
    top-level `system` field and any in-message `role: system` entry are
    filtered identically."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text.startswith("x-anthropic-billing-header"):
                    continue
                parts.append(text)
        return parts
    return []


def _flatten_messages(messages: list, system) -> list:
    """Implement the message flattening algorithm."""
    # Flatten system: start from the top-level `system` field, then fold in
    # the text of every in-message `role: system` entry encountered below (in
    # encounter order -- see the `role == "system"` branch inside the loop).
    # Claude Code 2.1.280 appends a trailing "# Environment ..." system
    # message AFTER the user turn; forwarding that in place as a trailing
    # `role: system` OpenAI message makes some upstreams treat it as the last
    # word and continue writing environment text instead of answering, so
    # every system-role message is hoisted into one leading system message
    # instead of being forwarded where it appeared.
    system_parts = _system_text_parts(system)

    pending_ids = []  # tool_use ids from preceding assistant turn
    protos = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", [])
        if role == "assistant":
            # Split into text and tool_use blocks
            text_parts = []
            tool_use_blocks = []
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_use_blocks.append(block)
            text = "\n".join(text_parts).strip() if text_parts else None
            tool_calls = None
            if tool_use_blocks:
                tool_calls = []
                for tu in tool_use_blocks:
                    tool_calls.append({
                        "id": tu["id"],
                        "type": "function",
                        "function": {
                            "name": tu["name"],
                            "arguments": json.dumps(tu.get("input") or {})
                        }
                    })
            # Emit assistant proto; content is explicitly present (None for a
            # tool-only turn) to match the canonical {"content": null, ...} shape.
            proto = {"role": "assistant", "content": text}
            if tool_calls:
                proto["tool_calls"] = tool_calls
            # Only emit if there was at least one original content block
            if content or (isinstance(content, list) and content):
                protos.append(proto)
            # Set pending_ids for next user turn
            pending_ids = [tc["id"] for tc in tool_calls] if tool_calls else []
        elif role == "user":
            # Gather tool_results
            result_by_id = {}
            other_blocks = []
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tid = block.get("tool_use_id")
                    if tid:
                        result_by_id[tid] = block
                else:
                    other_blocks.append(block)
            # Emit tool messages for pending_ids in order. Keep a `consumed`
            # record (tid -> popped result, incl. None for a missing one) so
            # the image-hoisting pass below can still see what was matched
            # here -- result_by_id itself is emptied by the .pop() calls.
            consumed = {}
            for tid in pending_ids:
                result = result_by_id.pop(tid, None)
                consumed[tid] = result
                content_text = "(no result)"
                if result:
                    cnt = result.get("content")
                    text = extract_text(cnt)
                    if result.get("is_error"):
                        text = "[tool error] " + text
                    # Check for image
                    if isinstance(cnt, list):
                        for part in cnt:
                            if isinstance(part, dict) and part.get("type") == "image":
                                text = "(image in next message)"
                                break
                    content_text = text
                protos.append({"role": "tool", "tool_call_id": tid, "content": content_text})
            # Build one trailing user proto
            user_parts = []
            # (a) images from consumed ids
            for tid in pending_ids:
                result = consumed.get(tid)
                if result and isinstance(result.get("content"), list):
                    for part in result["content"]:
                        if isinstance(part, dict) and part.get("type") == "image":
                            source = part.get("source", {})
                            if source.get("type") == "base64":
                                user_parts.append({"type": "text", "text": f"Image from {tid}:"})
                                user_parts.append({
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{source.get('media_type','image/jpeg')};base64,{source.get('data','')}"
                                    }
                                })
            # (b) orphaned results
            for tid, result in result_by_id.items():
                cnt = result.get("content")
                text = extract_text(cnt)
                if text:
                    user_parts.append({"type": "text", "text": text})
            # (c) other blocks from this user message
            for block in other_blocks:
                if isinstance(block, dict):
                    typ = block.get("type")
                    if typ == "text":
                        txt = block.get("text", "").strip()
                        if txt:
                            user_parts.append({"type": "text", "text": txt})
                    elif typ == "image":
                        source = block.get("source", {})
                        if source.get("type") == "base64":
                            user_parts.append({
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{source.get('media_type','image/jpeg')};base64,{source.get('data','')}"
                                }
                            })
            if user_parts:
                protos.append({"role": "user", "content": user_parts})
            pending_ids = []  # reset after user turn
        elif role == "system":
            # Hoisted into the leading system message (below) instead of
            # being forwarded in place; must NOT touch pending_ids, so a
            # preceding assistant tool_use turn still pairs correctly with
            # the tool_result in the next real user turn even when a system
            # message sits between them.
            system_parts.extend(_system_text_parts(content))
        else:
            # Unknown role, pass through as-is
            protos.append({"role": role, "content": content})

    system_text = "\n\n".join(system_parts).strip()
    if system_text:
        protos.insert(0, {"role": "system", "content": system_text})

    # Final pass: drop empty, merge consecutive same-role
    filtered = []
    for proto in protos:
        content = proto.get("content")
        # Drop empty protos
        if content is None:
            if "tool_calls" not in proto:
                continue
        elif isinstance(content, str):
            if not content.strip():
                if "tool_calls" not in proto:
                    continue
        elif isinstance(content, list):
            if not content:
                if "tool_calls" not in proto:
                    continue
            # Check if all text parts are whitespace-only
            all_ws = True
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    if part.get("text", "").strip():
                        all_ws = False
                        break
                else:
                    all_ws = False
                    break
            if all_ws and "tool_calls" not in proto:
                continue
        filtered.append(proto)

    # Merge consecutive same-role (user+user, assistant+assistant)
    merged = []
    for proto in filtered:
        if not merged:
            merged.append(proto)
            continue
        prev = merged[-1]
        if prev["role"] == proto["role"] and prev["role"] not in ("tool", "system"):
            # Merge content
            prev_content = prev.get("content")
            curr_content = proto.get("content")
            # Normalize to list of parts
            def to_parts(c):
                if c is None:
                    return []
                if isinstance(c, str):
                    return [{"type": "text", "text": c}]
                if isinstance(c, list):
                    return c
                return []
            prev_parts = to_parts(prev_content)
            curr_parts = to_parts(curr_content)
            merged_parts = prev_parts + curr_parts
            # Convert back if single text part
            if len(merged_parts) == 1 and merged_parts[0].get("type") == "text":
                prev["content"] = merged_parts[0]["text"]
            else:
                prev["content"] = merged_parts
            # Merge tool_calls
            if "tool_calls" in proto:
                if "tool_calls" not in prev:
                    prev["tool_calls"] = []
                prev["tool_calls"].extend(proto.get("tool_calls", []))
        else:
            merged.append(proto)

    # Final conversion: an all-text content list on a user/assistant message
    # becomes one string joined by "\n\n" (a list containing any non-text --
    # e.g. image_url -- part is left as a list).
    for proto in merged:
        if proto["role"] not in ("user", "assistant"):
            continue
        content = proto.get("content")
        if isinstance(content, list) and content and all(
            isinstance(part, dict) and part.get("type") == "text" for part in content
        ):
            proto["content"] = "\n\n".join(part.get("text", "") for part in content)
    return merged


def extract_text(content) -> str:
    """Extract text from content block."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(part.get("text", ""))
        return "\n".join(texts)
    return ""


