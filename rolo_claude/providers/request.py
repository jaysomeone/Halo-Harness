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

from rolo_claude.providers.hooks import host_allowlist, reasoning_echo, system_normalize
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


# H2 finding 8: `_flatten_messages` silently DROPS an assistant message
# whose content is empty/whitespace-only text with no tool_use (a reply
# that spent its whole budget thinking, or an empty completion) instead of
# emitting a proto for it -- every LATER assistant message's reasoning then
# gets attached to the WRONG proto by positional drift in
# `hooks.reasoning_echo` below. Rather than forking `_flatten_messages`
# (this module's whole design avoids that), a message meeting this
# predicate gets a placeholder content block that's guaranteed to survive
# flattening, swapped back to "" once `_flatten_messages` returns -- so the
# assistant-proto count coming out of it always equals the assistant-
# message count going in, making the positional zip in `reasoning_echo`
# provably correct rather than merely usually-correct.
_EMPTY_ASSISTANT_PLACEHOLDER = "​"  # zero-width space: non-whitespace per str.strip()


def _is_empty_assistant_message(msg: dict) -> bool:
    content = msg.get("content")
    if not isinstance(content, list):
        return not content
    has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
    has_text = any(isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip() for b in content)
    return not (has_tool_use or has_text)


def _protect_empty_assistant_messages(messages: list) -> list:
    """Shallow copy of `messages` for use ONLY as `_flatten_messages` input
    -- every assistant message `_is_empty_assistant_message` flags gets a
    placeholder text block; every other message (including every non-
    assistant one) is returned unchanged, by reference."""
    out = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "assistant" and _is_empty_assistant_message(msg):
            out.append({**msg, "content": [{"type": "text", "text": _EMPTY_ASSISTANT_PLACEHOLDER}]})
        else:
            out.append(msg)
    return out


def _restore_empty_assistant_protos(oai_messages: list) -> None:
    """Mutates in place: swap the placeholder back to the TRUE empty string
    finding 8 asks for ("emitting content: '' for empty nodes instead of
    dropping them"), now that `_flatten_messages` is done and can no longer
    drop the proto out from under it."""
    for proto in oai_messages:
        if proto.get("role") == "assistant" and proto.get("content") == _EMPTY_ASSISTANT_PLACEHOLDER:
            proto["content"] = ""


_DATABRICKS_STRIP_KEYWORDS = frozenset({"$schema", "$defs", "definitions", "anyOf", "oneOf", "allOf", "pattern"})


def simplify_schema_for_databricks(schema: dict, *, max_keys: int = 16) -> dict:
    """Databricks structured-output/tool schemas: at most `max_keys`
    top-level `properties` entries and no `$ref`/`$defs`/`anyOf`/`pattern`
    AS SCHEMA KEYWORDS (coordinator research, glm_qwen_minimax_adapters.md).

    Finding 13: the pre-H2 version stripped these keyword NAMES at every
    dict level, including inside `properties`/`$defs` -- where the keys
    are PROPERTY NAMES, not schema keywords (a Grep/Glob-shaped tool with a
    `pattern` STRING parameter lost that property while `required:
    ["pattern"]` stayed, breaking every such tool on Databricks). Only a
    schema-shaped node's own keys are ever checked against the keyword
    list; `properties`/`$defs`/`definitions` values are recursed into
    without checking their KEYS. `$ref` nodes are replaced by a permissive
    `{"type": "object"}` rather than resolved; `anyOf: [T, {"type":
    "null"}]` (the common "optional nullable" shape) collapses to T instead
    of losing its type entirely. Logs what was cut, never raises."""
    def strip(node):
        if isinstance(node, list):
            return [strip(v) for v in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            log.debug("databricks schema: replaced $ref node %r with a permissive object", node.get("$ref"))
            return {"type": "object"}
        any_of = node.get("anyOf")
        if isinstance(any_of, list) and len(any_of) == 2:
            null_variant = any(isinstance(b, dict) and b.get("type") == "null" for b in any_of)
            non_null = [b for b in any_of if not (isinstance(b, dict) and b.get("type") == "null")]
            if null_variant and len(non_null) == 1 and isinstance(non_null[0], dict):
                collapsed = dict(node)
                collapsed.pop("anyOf", None)
                collapsed.update(non_null[0])
                node = collapsed
        out = {}
        for k, v in node.items():
            if k in _DATABRICKS_STRIP_KEYWORDS:
                continue
            if k in ("properties", "$defs", "definitions") and isinstance(v, dict):
                out[k] = {pk: strip(pv) for pk, pv in v.items()}  # pk is a PROPERTY NAME, never a keyword
            else:
                out[k] = strip(v)
        return out

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
    # hooks.system_normalize (fold_into_first_user rows -- Gemma 3, DeepSeek
    # R1) folds `system_text` into the first user turn instead of leaving it
    # a separate leading system message; the empty-assistant placeholder
    # sandwich (finding 8) keeps `_flatten_messages` from ever dropping an
    # assistant proto out from under `reasoning_echo`'s positional zip. Both
    # wrap the SAME unmodified `_flatten_messages`, never fork it.
    folded_system_text, normalized_messages = system_normalize(system_text, messages, profile)
    protected_messages = _protect_empty_assistant_messages(normalized_messages)
    oai_messages = _flatten_messages(protected_messages, folded_system_text)
    _restore_empty_assistant_protos(oai_messages)
    oai_tools = convert_tools(tools, profile)
    reasoning_echo(oai_messages, messages, profile, tools_present=bool(oai_tools))

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
    elif profile.body_allowlist is not None:
        # finding 6: Databricks' OTPM rolling-window budgeting
        # (hooks.max_tokens_budget) needs each response's real output-token
        # spend, which only arrives on the final SSE chunk when the
        # request explicitly asks for it (OpenAI streaming convention,
        # allowlisted in DATABRICKS_BODY_ALLOWLIST but never sent before).
        body["stream_options"] = {"include_usage": True}

    # finding 5 + hooks.host_allowlist: merge the row's OpenRouter pin into
    # `provider` on EVERY OpenRouter request (a no-op for Databricks --
    # host_allowlist returns immediately when profile.host_specific_fields
    # is False), and force require_parameters whenever tools are sent --
    # every seeded openrouter_pin was previously dead data.
    host_allowlist(body, profile, tools_present=bool(oai_tools))

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
