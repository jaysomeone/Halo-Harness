"""halo_harness.providers.ollama_request -- Halo 2.0.3 round 2: the native
`/api/chat` request builder for the `ol:` dialect. Message flattening
reuses `providers.translate._flatten_messages` (the proxy's own proven
algorithm -- single leading system, tool_result pairing, image hoisting)
UNMODIFIED, exactly like `providers.request.build_request_body` already
does for the openai-chat dialect, then adapts the result to Ollama's own
message shape instead of forking the flattening algorithm a second time.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from halo_harness.providers.ollama import OllamaHost, compute_num_ctx, think_value_for_effort
from halo_harness.providers.request import convert_tools
from halo_harness.providers.translate import _flatten_messages

log = logging.getLogger("bridge")



def _text_only(content) -> str:
    """`_flatten_messages` content is a plain string, or (images present) a
    list of `{"type":"text"|"image_url", ...}` parts -- Ollama's native
    message shape wants a plain string; round 2 does not send Ollama's own
    `images` field yet (not in this round's brief), so an image part
    becomes a short, visible placeholder instead of silently vanishing
    (house policy: describe behavior, never hide it)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif block.get("type") == "image_url":
                parts.append("[image omitted -- ol: does not send images to Ollama in this release]")
        return "".join(parts)
    return str(content)


def _ollama_messages_from_oai(oai_messages: list) -> list:
    """Adapt `_flatten_messages`'s OpenAI-chat-shaped protos into Ollama's
    native shape: a tool-result message keys off `tool_name` instead of
    `tool_call_id` (research doc Q1 -- Ollama's replay shape carries no id
    concept at all: `{"role": "tool", "tool_name": "<name>", "content":
    "<result>"}`), and an assistant's `tool_calls[].function.arguments`
    goes back from the JSON STRING `_flatten_messages` always produces to a
    parsed OBJECT (Ollama's own wire shape, Q1: "arguments is a parsed JSON
    object, not a string"). The upstream tool-call id itself is dropped --
    Ollama never sends or expects one back (see `providers.ollama_stream`
    for where Halo's OWN synthesized id comes from on the way IN)."""
    id_to_name: dict = {}
    out = []
    for msg in oai_messages:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            calls = []
            for tc in msg["tool_calls"]:
                func = tc.get("function") or {}
                name = func.get("name") or "unknown"
                id_to_name[tc.get("id")] = name
                raw_args = func.get("arguments")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else {}
                except (json.JSONDecodeError, ValueError):
                    args = {}
                calls.append({"function": {"name": name, "arguments": args}})
            out.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        elif role == "tool":
            name = id_to_name.get(msg.get("tool_call_id"), "unknown")
            out.append({"role": "tool", "tool_name": name, "content": _text_only(msg.get("content"))})
        else:
            out.append({"role": role, "content": _text_only(msg.get("content"))})
    return out


def build_ollama_request_body(
    *, system_text: str, messages: list, tools: Optional[list] = None, tool_choice=None,
    route, profile, effort: Optional[str] = None, host: OllamaHost,
    trained_context: Optional[int] = None, fit_estimate: Optional[int] = None,
    requested_max_tokens: Optional[int] = None,
    learned_cap: Optional[int] = None, remote: bool = False,
    force_format: "Optional[dict]" = None,
) -> dict:
    """Build the native `/api/chat` body. `messages`/`system_text` are the
    SAME Anthropic-shaped derived-transcript inputs `providers.request.
    build_request_body` takes. `tool_choice` is accepted only for call-site
    symmetry with the other two dialect builders -- Ollama's native API
    documents no `tool_choice`-equivalent field, so it is never put on the
    wire. `options.num_ctx` is computed fresh (round 2's context-ownership
    rule, `providers.ollama.compute_num_ctx`) and sent on EVERY request --
    never relies on a Modelfile default (research doc Q2's unconfirmed
    override order is moot once Halo always sends the field itself).
    `keep_alive` is sent ONLY when the host entry configures one: a request
    that carries `keep_alive` overrides the server's own `OLLAMA_KEEP_ALIVE`
    for that model, so an unconfigured host must leave the field out and let
    the server's setting (the operator's choice, which may be longer than
    Ollama's 5-minute default) stand. Checked live on the round 2 run: with
    the field left out, the server applied its own expiry.

    Halo 2.0.3 round 3 (brief item 2/3): `num_ctx` is computed BEFORE the
    tool list, not after -- the context-class `tools_max` it drives has to
    be known before `convert_tools` checks the catalog against it, not the
    other way around. `agent/loop.py`'s `_sync_ollama_tools_cap` is what
    actually shrinks the SessionCatalog (so the catalog handed in here is
    ALREADY within bounds in the ordinary case); the `dataclasses.replace`
    below is the backstop assertion `ToolCatalogTooLarge`/`request.py`'s
    own docstring describes -- never a second capping path, just the one
    `profile.tools_max` check finally given a real, context-aware number
    for this dialect instead of round 2's permanently-unbounded `None`.
    That number is never pushed BELOW `len(tools)` though: when every
    tool already on the list is frozen/preloaded (nothing left for
    `_sync_ollama_tools_cap`'s own eviction to remove), failing the turn
    over a catalog that is physically unable to shrink any further would
    be exactly the kind of gating house policy forbids -- the backstop's
    job is catching round 2's unbounded-`None` bug, not blocking a turn
    the catalog layer already did everything it could about.

    Round 5b: `learned_cap`/`remote` are pre-resolved by the SAME
    `providers.ollama_hw.resolve_context_decision` call the caller
    (`agent/loop.py`'s `_build_ollama_body_for_ref`) already made to get
    `trained_context`/`fit_estimate` -- passed straight through to
    `compute_num_ctx` rather than re-derived here, so there is exactly
    ONE lookup of the learned-cap store per request, not two.

    Round 5b part 2 (brief item 1, "reliable tool calls"), FIX PASS
    (2026-10-04 live-run finding): constrained decoding is used ONLY to
    REPAIR a malformed call (`force_format`, below -- `agent/loop.py`'s
    `_attempt_tool_repair`), never to FORCE one on an ordinary turn. The
    first version of this round set `format` on every turn `providers.
    tool_call_schema.expected_to_call_tool` judged "expected to call a
    tool" (right after a tool result) -- that FORCED the model to answer
    in the tool-call shape on every such turn, with no way to just
    answer in prose; on a real local box the model had nothing useful
    left to call, emitted a meaningless call (`TaskStop` on a task that
    didn't exist) every time, Halo dispatched it, the NEXT turn was
    STILL constrained post-tool-result, and the session looped for 25
    minutes (175 requests) until killed by hand. A normal turn -- any
    turn that isn't an explicit repair -- is always decoded FREE now: the
    model must always be able to answer in prose. The generic bare-JSON/
    fenced-JSON leak-parser patterns (`profiles.py`'s `tool_leak_
    patterns`) stay enabled regardless -- a schema-shaped reply that
    still arrives as plain text gets promoted to a real tool_use, which
    is harmless without the constraint forcing it. A STRICTER identical-
    call loop guard (`agent/loop.py`'s `_identical_call_guard_*`, three
    in a row on this dialect) is the new backstop against a model stuck
    repeating one call regardless of why."""
    del tool_choice  # no native-API equivalent (see docstring)
    import dataclasses
    from halo_harness.providers.ollama_fit import resolve_ollama_tools_max
    oai_messages = _flatten_messages(messages, system_text)
    ollama_messages = _ollama_messages_from_oai(oai_messages)
    num_ctx = compute_num_ctx(trained_context, host.max_ctx, fit_estimate, learned_cap=learned_cap, remote=remote)
    tools_max = max(resolve_ollama_tools_max(num_ctx), len(tools or []))
    tools_profile = profile if tools_max == profile.tools_max else dataclasses.replace(profile, tools_max=tools_max)
    oai_tools = convert_tools(tools, tools_profile)
    body: dict = {
        "model": route.upstream_model,
        "messages": ollama_messages,
        "stream": True,
        "options": {"num_ctx": num_ctx},
    }
    if host.keep_alive is not None:
        body["keep_alive"] = host.keep_alive
    if oai_tools:
        body["tools"] = oai_tools
    if force_format is not None:
        # Round 5b part 2 (brief item 2, "repair loop"): `agent/loop.py`'s
        # `_attempt_tool_repair` wants the tool's OWN exact `input_schema`
        # as the constraint on an ISOLATED, tools-less completion -- the
        # ONLY place this module ever sets `format` now (fix pass: an
        # ordinary turn is never constrained, see this function's own
        # docstring).
        body["format"] = force_format
    think = think_value_for_effort(effort, route.upstream_model)
    if think is not None:
        body["think"] = think
    if isinstance(requested_max_tokens, int) and requested_max_tokens > 0:
        body["options"]["num_predict"] = requested_max_tokens
    return body
