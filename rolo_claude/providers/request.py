"""rolo_claude.providers.request -- the profile-driven OpenAI-dialect
request builder (H1 scope B), replacing the ad-hoc parts of
`anthropic_to_openai` usage in the agent loop. `bridge.py`'s own proxy path
still calls `translate.anthropic_to_openai` directly and is UNCHANGED by
this module; this is purely additive, used only by `agent/loop.py`.

Message flattening reuses `providers.translate._flatten_messages` (the
proxy's own proven algorithm: single leading system, tool_result pairing,
image hoisting) unmodified, then splices reasoning replay on top per
profile -- rather than forking that algorithm, which would risk the two
copies drifting apart.
"""

from __future__ import annotations

import logging
from typing import Optional

from rolo_claude.providers.profiles import ProviderProfile, map_effort
from rolo_claude.providers.routing import anthropic_tool_to_openai, map_tool_choice, sanitize_tool_schema
from rolo_claude.providers.translate import _flatten_messages

log = logging.getLogger("bridge")


class ToolCatalogTooLarge(Exception):
    """Raised when the caller hands more tools than `profile.tools_max`
    allows (Databricks: 32) -- a clear error rather than a silent, wire-
    validated-later truncation. Catalog freezing/selection (WHICH tools to
    keep) is H3's job; H1 only asserts the invariant."""

    def __init__(self, count: int, limit: int):
        self.count = count
        self.limit = limit
        super().__init__(f"{count} tools exceeds this provider's {limit}-tool cap")


def _assistant_reasoning_list(messages: list) -> list:
    """One entry per assistant message IN ORDER: whatever `agent/log.py`
    attached at `message["reasoning"]` (see agent/derive.py), or None. Used
    to re-attach reasoning to the corresponding OpenAI-shaped assistant
    proto AFTER `_flatten_messages` has already built it (positional
    correspondence holds because a normal turn never emits two consecutive
    assistant messages without an intervening user/tool-result turn)."""
    return [m.get("reasoning") if isinstance(m, dict) else None
            for m in messages if isinstance(m, dict) and m.get("role") == "assistant"]


def _apply_reasoning_replay(oai_messages: list, messages: list, profile: ProviderProfile, tools_present: bool) -> None:
    """Mutates `oai_messages` in place: splice `reasoning_content` (text
    replay, `""` injected when absent and `tools_present`) or
    `reasoning_details` (OpenRouter, verbatim array) onto each assistant
    proto per `profile.reasoning_replay`. A no-op for "empty" (some models,
    e.g. Qwen3-thinking-2507, explicitly must NOT see prior reasoning) and
    for "thinking" (the Anthropic-Messages passthrough route never reaches
    this OpenAI-dialect builder at all)."""
    if profile.reasoning_replay not in ("text", "details"):
        return
    reasoning_list = _assistant_reasoning_list(messages)
    idx = 0
    for proto in oai_messages:
        if proto.get("role") != "assistant":
            continue
        raw = reasoning_list[idx] if idx < len(reasoning_list) else None
        idx += 1
        if profile.reasoning_replay == "text":
            if not tools_present:
                continue  # rule: only forced onto every message when `tools` is in the request
            value = raw.get("value") if isinstance(raw, dict) else raw
            proto["reasoning_content"] = value if isinstance(value, str) else ""
        elif profile.reasoning_replay == "details":
            value = raw.get("value") if isinstance(raw, dict) else raw
            if isinstance(value, list):
                proto["reasoning_details"] = value  # verbatim, in order -- never trimmed/reordered


def simplify_schema_for_databricks(schema: dict, *, max_keys: int = 16) -> dict:
    """Databricks structured-output/tool schemas: at most `max_keys`
    top-level `properties` entries and no `$ref`/`$defs`/`anyOf`/`pattern`
    (coordinator research, glm_qwen_minimax_adapters.md). `$ref` nodes are
    replaced by a permissive `{"type": "object"}` rather than resolved
    (H1's own tools have no $ref; this is forward cover for H2+ tool
    schemas) -- logs what was cut, never raises."""
    def strip(node):
        if isinstance(node, dict):
            if "$ref" in node:
                log.debug("databricks schema: replaced $ref node %r with a permissive object", node.get("$ref"))
                return {"type": "object"}
            out = {}
            for k, v in node.items():
                if k in ("$schema", "$defs", "definitions", "anyOf", "oneOf", "allOf", "pattern"):
                    continue
                out[k] = strip(v)
            return out
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    cleaned = strip(dict(schema) if isinstance(schema, dict) else {"type": "object", "properties": {}})
    props = cleaned.get("properties")
    if isinstance(props, dict) and len(props) > max_keys:
        kept = dict(list(props.items())[:max_keys])
        dropped = [k for k in props if k not in kept]
        log.debug("databricks schema: dropped %d properties over the %d-key cap: %s", len(dropped), max_keys, dropped)
        cleaned["properties"] = kept
        req = cleaned.get("required")
        if isinstance(req, list):
            cleaned["required"] = [k for k in req if k in kept]
    return cleaned


def convert_tools(tools: Optional[list], profile: ProviderProfile) -> Optional[list]:
    """Anthropic tool defs -> OpenAI `tools` array: name-sorted (stable
    prefix for provider prompt caching -- scope B/E), Databricks-simplified
    schemas when `profile.body_allowlist` is the Databricks allowlist, and
    a clear `ToolCatalogTooLarge` instead of a silent truncation/wire 400
    when the caller violates `profile.tools_max`."""
    if not tools:
        return None
    if profile.tools_max is not None and len(tools) > profile.tools_max:
        raise ToolCatalogTooLarge(len(tools), profile.tools_max)
    ordered = sorted(tools, key=lambda t: t.get("name", ""))
    is_databricks = profile.body_allowlist is not None and "reasoning_effort" in profile.body_allowlist
    out = []
    for t in ordered:
        oai_tool = anthropic_tool_to_openai(t)
        if is_databricks:
            oai_tool["function"]["parameters"] = simplify_schema_for_databricks(oai_tool["function"]["parameters"])
        out.append(oai_tool)
    return out


def budget_max_tokens(*, profile: ProviderProfile, context_tokens: int, prompt_estimate: int,
                       requested: Optional[int] = None) -> int:
    """`min(profile cap, context - estimate - buffer)` (scope B/D-7): the
    profile's own `max_tokens_cap`/`max_tokens_default` already encode a
    provider's pay-per-token OTPM ceiling (e.g. Databricks V4 Pro's 4,000)
    where relevant, so this is the SAME formula for every profile, not a
    Databricks-only special case."""
    cap = profile.max_tokens_cap or profile.max_tokens_default or 16384
    default = profile.max_tokens_default or cap
    want = requested if isinstance(requested, int) and requested > 0 else default
    headroom = context_tokens - prompt_estimate - 512
    if headroom <= 0:
        return max(1, min(want, cap))
    return max(1, min(want, cap, headroom))


def build_request_body(
    *, system_text: str, messages: list, tools: Optional[list] = None, tool_choice=None,
    route, profile: ProviderProfile, effort: Optional[str] = None,
    context_tokens: int = 128000, prompt_estimate: int = 0, requested_max_tokens: Optional[int] = None,
) -> dict:
    """Build the OpenAI-dialect body for one request. `messages` is the
    Anthropic-shaped derived transcript (agent/derive.py); `system_text` is
    the byte-stable system prompt PLUS any dynamic user-role snapshots
    already folded in by the caller (this function only ever emits ONE
    leading `system` message, per rule 5/D-CFG)."""
    oai_messages = _flatten_messages(messages, system_text)
    oai_tools = convert_tools(tools, profile)
    _apply_reasoning_replay(oai_messages, messages, profile, tools_present=bool(oai_tools))

    max_tokens = budget_max_tokens(
        profile=profile, context_tokens=context_tokens, prompt_estimate=prompt_estimate,
        requested=requested_max_tokens,
    )

    body: dict = {
        "model": route.upstream_model, "messages": oai_messages, "stream": True,
        profile.max_tokens_field: max_tokens,
    }
    if oai_tools:
        body["tools"] = oai_tools
        tc = map_tool_choice(tool_choice)
        if tc is not None:
            if tc == "required" and not profile.tool_choice_required_supported:
                tc = "auto"  # DeepSeek-V4-thinking 400s on required/named; Kimi K2.x/Qwen/GLM: auto|none only
            body["tool_choice"] = tc
        if profile.host_specific_fields:
            body["provider"] = {"require_parameters": True}  # avoid a 404 "No endpoints found that support tool use"

    if profile.use_temperature:
        if profile.temperature is not None:
            body["temperature"] = profile.temperature
        if profile.top_p is not None:
            body["top_p"] = profile.top_p
        if profile.top_k is not None and profile.host_specific_fields:
            body["top_k"] = profile.top_k  # OpenRouter accepts top_k; Databricks does not (allowlist drops it below)

    body.update(map_effort(effort, profile))  # map_effort itself handles reasoning_no_disable

    if profile.host_specific_fields:
        body["usage"] = {"include": True}  # always-on cost accounting (scope I "CostMeter")

    if profile.body_allowlist is not None:
        body = {k: v for k, v in body.items() if k in profile.body_allowlist or k == "model"}
        if "model" in body and "model" not in profile.body_allowlist:
            # Databricks: the invocations route takes NO "model" field at
            # all; the mlflow/v1 route needs it. The caller (stream
            # orchestration) already knows which route it's using and
            # re-adds "model" itself when needed (providers.databricks.
            # build_databricks_body) -- drop it here so the allowlist is
            # the single source of truth for what's actually sent.
            body.pop("model", None)
    elif not profile.host_specific_fields:
        body.pop("provider", None)
        body.pop("usage", None)

    return body
