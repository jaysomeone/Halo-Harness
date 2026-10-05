"""halo_harness.providers.responses_request -- Halo 2.0.3 round 5i part 1:
the OpenAI Responses API (`POST /v1/responses`) dialect's own body
builder and dialect-selection table, the `oai:` route's equivalent of
`providers.ollama_request.build_ollama_request_body`. Message flattening
reuses `providers.translate._flatten_messages` UNMODIFIED, then adapts
the result to the Responses `input` item shapes -- same reuse pattern
`ollama_request.py`'s own module docstring describes, never a second
flattening algorithm.

Dialect selection (`docs/harness/OPENAI-RESEARCH.md` "Dialect-selection
table"): exactly the two bare ids the Responses API reference names as
REQUIRING this dialect for function calling -- `gpt-6-astra`, `gpt-6.1-
sol` -- plus `openai.dialect_overrides` (`~/.halo/config.json`, `{"<bare
id>": "chat"|"responses"}`), which always wins over the table, in either
direction.
"""

from __future__ import annotations

import logging
from typing import Optional

from halo_harness.providers.request import convert_tools
from halo_harness.providers.routing import map_tool_choice
from halo_harness.providers.translate import _flatten_messages

log = logging.getLogger("bridge")

RESPONSES_REQUIRED_MODEL_IDS = frozenset({"gpt-6-astra", "gpt-6.1-sol"})

_DIALECT_ALIASES = {
    "chat": "openai-chat", "openai-chat": "openai-chat",
    "responses": "openai-responses", "openai-responses": "openai-responses",
}


def resolve_openai_dialect(model_id: str, *, overrides: "Optional[dict]" = None) -> str:
    """"openai-chat" or "openai-responses" for a bare `oai:<model_id>`.
    `overrides` (test seam; `None` reads `openai.dialect_overrides` from
    `~/.halo/config.json` via `halo_harness.theme.get_config_value`)
    always wins over the table below, in EITHER direction -- the table is
    deliberately narrow (exactly the two ids the research confirmed), and
    a future model may need the opposite of either answer with no code
    change. An unrecognized override value (a typo, an old dialect name)
    falls back to the table instead of raising -- a bad config value must
    never break model resolution."""
    if overrides is None:
        from halo_harness.theme import get_config_value
        overrides = get_config_value("openai.dialect_overrides", default=None)
    if isinstance(overrides, dict) and model_id in overrides:
        raw = overrides.get(model_id)
        mapped = _DIALECT_ALIASES.get(str(raw).strip().lower()) if raw is not None else None
        if mapped is not None:
            return mapped
        log.debug("openai: ignoring unrecognized openai.dialect_overrides[%r] = %r", model_id, raw)
    return "openai-responses" if model_id in RESPONSES_REQUIRED_MODEL_IDS else "openai-chat"


def _text_only(content) -> str:
    """Flatten an OAI-chat-shaped proto's `content` (a plain string, or --
    an image present -- a list of text/image_url parts) down to plain
    text for a Responses `input` message item. Same "describe behavior,
    never hide it" placeholder choice `providers.ollama_request._text_
    only` makes for Ollama's own native shape: the Responses API's real
    `input_image` part shape is confirmed to exist but is out of this
    round's scope."""
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
                parts.append("[image omitted -- oai: Responses dialect does not send images this release]")
        return "".join(parts)
    return str(content)


def _responses_input_from_oai(oai_messages: list) -> "tuple[Optional[str], list]":
    """Adapt `_flatten_messages`'s OpenAI-chat-shaped protos into the
    Responses API's `instructions` + `input` items (`docs/harness/OPENAI-
    RESEARCH.md` section 1): the one leading `role: system` proto (at
    most one, by `_flatten_messages`'s own contract) becomes
    `instructions` instead of an `input` item; an assistant's `tool_
    calls[]` becomes `function_call` items (`arguments` carried through
    VERBATIM -- already a JSON string on both sides, unlike `providers.
    ollama_request`'s own adapter, which has to parse it back to an
    object for Ollama's native shape); a `role: tool` proto becomes a
    `function_call_output` item keyed by the same id, now named
    `call_id`."""
    instructions = None
    out: list = []
    for i, msg in enumerate(oai_messages):
        role = msg.get("role")
        if i == 0 and role == "system":
            instructions = msg.get("content") or None
            continue
        if role == "assistant":
            text = msg.get("content")
            if isinstance(text, str) and text:
                out.append({"type": "message", "role": "assistant", "content": text})
            for tc in msg.get("tool_calls") or []:
                func = tc.get("function") or {}
                args = func.get("arguments")
                out.append({
                    "type": "function_call", "call_id": tc.get("id") or "",
                    "name": func.get("name") or "unknown",
                    "arguments": args if isinstance(args, str) else "{}",
                })
        elif role == "tool":
            out.append({
                "type": "function_call_output", "call_id": msg.get("tool_call_id") or "",
                "output": _text_only(msg.get("content")),
            })
        else:
            out.append({"type": "message", "role": role if role in ("user", "developer") else "user",
                         "content": _text_only(msg.get("content"))})
    return instructions, out


def _responses_tools(tools, profile) -> "Optional[list]":
    """`providers.request.convert_tools`'s chat-completions-shaped
    `{"type":"function","function":{"name",...}}` items, flattened one
    level into the Responses API's own flat function-tool item shape
    (confirmed shape, `docs/harness/OPENAI-RESEARCH.md` section 1) --
    reuses that function's sorting/cap-check/`ToolsNotSupported`/
    `ToolCatalogTooLarge` behavior unchanged rather than re-implementing
    any of it."""
    oai_tools = convert_tools(tools, profile)
    if not oai_tools:
        return None
    out = []
    for t in oai_tools:
        func = t.get("function") or {}
        out.append({
            "type": "function", "name": func.get("name"), "description": func.get("description", ""),
            "parameters": func.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _responses_tool_choice(tool_choice):
    """`providers.routing.map_tool_choice`'s chat-completions-shaped
    answer (`"auto"`/`"required"`/`"none"`/`{"type":"function","function":
    {"name":...}}`), flattened one level for Responses the same way
    `_responses_tools` flattens a tool definition."""
    mapped = map_tool_choice(tool_choice)
    if isinstance(mapped, dict) and mapped.get("type") == "function":
        name = (mapped.get("function") or {}).get("name")
        return {"type": "function", "name": name} if name else "auto"
    return mapped


def build_openai_responses_body(
    *, system_text: str, messages: list, tools: "Optional[list]" = None, tool_choice=None,
    route, profile, effort: "Optional[str]" = None, requested_max_tokens: "Optional[int]" = None,
) -> dict:
    """Build the `/v1/responses` body. `messages`/`system_text` are the
    SAME Anthropic-shaped derived-transcript inputs `providers.request.
    build_request_body`/`providers.ollama_request.build_ollama_request_
    body` take. `store: false` and NO `previous_response_id` are ALWAYS
    sent (brief: "Halo keeps owning the transcript") -- this dialect never
    relies on OpenAI's own server-side conversation state, same as every
    other dialect here replaying its own transcript on every request.
    `effort` is sent as-is (never re-clamped here): `Session.__init__`
    already clamped it against this route's `ProviderProfile.effort_
    values_supported` before any body builder ever sees it, the same
    precondition `build_ollama_request_body` relies on."""
    oai_messages = _flatten_messages(messages, system_text)
    instructions, input_items = _responses_input_from_oai(oai_messages)
    body: dict = {"model": route.upstream_model, "input": input_items, "store": False, "stream": True}
    if instructions:
        body["instructions"] = instructions
    responses_tools = _responses_tools(tools, profile)
    if responses_tools:
        body["tools"] = responses_tools
        mapped_choice = _responses_tool_choice(tool_choice)
        if mapped_choice is not None and mapped_choice != "auto":
            body["tool_choice"] = mapped_choice
    if effort:
        body["reasoning"] = {"effort": effort}
    if isinstance(requested_max_tokens, int) and requested_max_tokens > 0:
        body["max_output_tokens"] = requested_max_tokens
    return body
