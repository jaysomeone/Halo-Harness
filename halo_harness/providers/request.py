"""halo_harness.providers.request -- the profile-driven OpenAI-dialect
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
import re
from typing import Optional

from halo_harness.providers.hooks import host_allowlist, reasoning_echo, system_normalize
from halo_harness.providers.profiles import ProviderProfile, clamp_effort, clamp_temperature, map_effort
from halo_harness.providers.routing import anthropic_tool_to_openai, map_tool_choice
from halo_harness.providers.translate import _flatten_messages

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
        # H9 MCP-compatibility matrix bug (b): a WIDER anyOf/oneOf (3+
        # variants, or a 2-variant one that isn't the nullable pair above)
        # used to be deleted outright by the keyword strip below, leaving
        # the property with NO type at all -- unlike `$ref`, which falls
        # back to a permissive object. Keep the FIRST non-null typed
        # variant instead (the model at least learns the primary type), or
        # the same permissive object when no variant is a plain typed dict.
        for kw in ("anyOf", "oneOf"):
            variants = node.get(kw)
            if isinstance(variants, list) and kw in node:
                typed = [b for b in variants if isinstance(b, dict) and b.get("type") not in (None, "null")]
                fallback = dict(typed[0]) if typed else {"type": "object"}
                replaced = dict(node)
                replaced.pop(kw, None)
                for fk, fv in fallback.items():
                    replaced.setdefault(fk, fv)
                log.debug("databricks schema: replaced %s with its first typed variant %r", kw, fallback)
                node = replaced
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


_OPENROUTER_CACHE_CONTROL = {"type": "ephemeral"}


def _as_cache_controlled_content(content) -> list:
    """A `content` field as `_flatten_messages` left it (a plain string,
    normally) -> a one-part array with `cache_control` on that part --
    OpenRouter's wire shape for a cached breakpoint on a chat-completions
    message (mirrors `cache_control: {"type": "ephemeral"}` content
    blocks; a list already is copied and the marker added to its last
    part instead of wrapping it again)."""
    if isinstance(content, list):
        out = list(content)
        last = dict(out[-1]) if out and isinstance(out[-1], dict) else {"type": "text", "text": ""}
        last["cache_control"] = dict(_OPENROUTER_CACHE_CONTROL)
        if out:
            out[-1] = last
        else:
            out = [last]
        return out
    return [{"type": "text", "text": content if isinstance(content, str) else "", "cache_control": dict(_OPENROUTER_CACHE_CONTROL)}]


def apply_openrouter_claude_cache_control(oai_messages: list) -> None:
    """Mutates `oai_messages` IN PLACE (called right after they're built,
    before anything else reads them): cache_control on the system message
    (index 0, when present) and on the LAST `role: "tool"` message -- H5
    scope C's "OpenRouter anthropic/claude-* ... cache_control breakpoints
    (<=4, on the system node and the last tool result)". A no-op when
    there is no system message and/or no tool message."""
    if oai_messages and oai_messages[0].get("role") == "system":
        oai_messages[0]["content"] = _as_cache_controlled_content(oai_messages[0].get("content"))
    last_tool_idx = None
    for i, msg in enumerate(oai_messages):
        if msg.get("role") == "tool":
            last_tool_idx = i
    if last_tool_idx is not None:
        oai_messages[last_tool_idx]["content"] = _as_cache_controlled_content(oai_messages[last_tool_idx].get("content"))


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
    if profile.family == "claude" and profile.host_specific_fields:
        # H5 scope C: OpenRouter needs EXPLICIT cache_control breakpoints
        # for Anthropic-backed models (no automatic caching, unlike
        # OpenAI/DeepSeek/Gemini/Grok/Groq/Moonshot) -- system node + the
        # last tool result, mirroring the native-dialect placement in
        # apply_anthropic_cache_control, just in the openai-chat wire shape
        # (content as an array-of-parts with cache_control on the block,
        # not the plain string _flatten_messages produced).
        apply_openrouter_claude_cache_control(oai_messages)

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
            # GLM-brief.md item 5: "temperature is clamped to [0, 1] on GLM
            # routes" -- a no-op for every other family (clamp_temperature's
            # own docstring), and for every GLM row today too (all already
            # seed 1.0), but keeps a future out-of-range table/override
            # value off the wire.
            body["temperature"] = clamp_temperature(profile.temperature, profile)
        if profile.top_k is not None and profile.host_specific_fields:
            body["top_k"] = profile.top_k  # OpenRouter accepts top_k; Databricks does not (allowlist drops it below)
    # H5 scope F: top_p gated independently when a row says so (DeepSeek V4
    # Flash: omit temperature, still send top_p 0.95) -- `use_top_p is
    # None` (every pre-H5 row) falls back to `use_temperature` exactly as
    # before, so this is a no-op for every row that never opted in.
    send_top_p = profile.use_top_p if profile.use_top_p is not None else profile.use_temperature
    if send_top_p and profile.top_p is not None:
        body["top_p"] = profile.top_p

    # 1.0.1 hotfix 22: has_tools=bool(oai_tools) -- gpt-6's own
    # reasoning_effort_with_tools override only ever applies to a request
    # that actually carries tools; map_effort itself handles
    # reasoning_no_disable and the ordinary --effort/clamp_effort path.
    body.update(map_effort(effort, profile, has_tools=bool(oai_tools)))

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

    # H9 sampling-table audit: model_table.json rows for DeepSeek V4/Grok/
    # MiniMax/Kimi K2.6+ list fields their host 400s or silently ignores
    # (presence_penalty/frequency_penalty/logit_bias/n/logprobs/stop,
    # temperature/top_p/n themselves for Kimi K2.6+/K3, which are FIXED
    # server-side) -- nothing in this build ever sets those keys today, so
    # this is currently a no-op belt-and-suspenders, not a behavior change,
    # but it makes the table's own data authoritative instead of decorative
    # the moment anything upstream (a hook rewrite, a future settings knob)
    # starts populating one.
    for _unsupported_key in profile.sampling_unsupported_params:
        body.pop(_unsupported_key, None)

    return body


# ---------------------------------------------------------------------------
# H5 scope C: the native-Anthropic-dialect request builder (`ant:`, Databricks
# Claude passthrough `dbx:databricks-claude-*`/`dbx:system.ai.claude-*`, and
# OpenRouter Claude `or:anthropic/claude-*` -- the last of those ALSO uses
# `build_request_body` above for its actual wire body, since OpenRouter
# speaks the openai-chat dialect even for Claude; this function is used only
# when `route.dialect == "anthropic-passthrough"`). Unlike the OpenAI-dialect
# builder, the derived transcript is ALREADY Anthropic-shaped end to end --
# messages, tool defs (`input_schema`, no conversion), thinking blocks with
# their `signature` -- so this is mostly straight assembly, not translation.
# ---------------------------------------------------------------------------

_CACHE_CONTROL = {"type": "ephemeral"}
MAX_CACHE_BREAKPOINTS = 4


def _mark_cache_control(content: list) -> list:
    """Return a copy of `content` (a list of Anthropic content blocks) with
    `cache_control` added to its LAST block -- a no-op (returns the list
    unchanged) for an empty list."""
    if not content:
        return content
    out = list(content)
    last = dict(out[-1]) if isinstance(out[-1], dict) else {"type": "text", "text": str(out[-1])}
    last["cache_control"] = dict(_CACHE_CONTROL)
    out[-1] = last
    return out


def apply_anthropic_cache_control(system_text: str, messages: list) -> "tuple[list, list]":
    """Appendix F / scope C: cache_control breakpoints (<= 4) on the system
    node and the LAST tool result -- returns (system_blocks, messages) with
    `cache_control` markers placed; never mutates the input `messages`.
    Breakpoint budget used here: 1 (system) + 1 (last tool result) = 2,
    comfortably under the 4-breakpoint cap (OpenRouter prompt-caching docs;
    Appendix F's OpenRouter recipe)."""
    system_blocks = [{"type": "text", "text": system_text, "cache_control": dict(_CACHE_CONTROL)}] if system_text else []
    out = list(messages)
    last_tool_result_idx = None
    for i, msg in enumerate(out):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            last_tool_result_idx = i
    if last_tool_result_idx is not None:
        msg = dict(out[last_tool_result_idx])
        msg["content"] = _mark_cache_control(msg.get("content") or [])
        out[last_tool_result_idx] = msg
    return system_blocks, out


_ANTHROPIC_MIN_THINKING_BUDGET = 1024  # Anthropic's own documented floor for budget_tokens


_ADAPTIVE_ALWAYS_FAMILIES = ("fable", "mythos")  # always adaptive-capable, whatever the version
# opus joins sonnet in the version gate: Opus 4.1/4.5 (both served on Databricks) take
# `budget_tokens`, not `thinking: adaptive`; adaptive starts at 4.6 for both families.
_ADAPTIVE_VERSION_GATED_FAMILIES = ("opus", "sonnet")  # adaptive only from _ADAPTIVE_MIN_VERSION onward
_ADAPTIVE_MIN_VERSION = 4.6  # sonnet: adaptive from 4.6 onward (4.5 and older stay budget_tokens)


def _anthropic_model_supports_adaptive_thinking(model_id: str) -> bool:
    """H5c finding 16: `{type: "adaptive"}` (never `budget_tokens`, which
    Sonnet 5 rejects with a 400) is required on Sonnet 4.6 onward, in
    addition to every Opus/Fable/Mythos id (unconditionally adaptive-
    capable already, unchanged from before this fix) -- the OLD check
    (`"opus" in low or "fable" in low`) missed Sonnet 5 (and Sonnet 4.6)
    entirely, routing it into the `budget_tokens` branch below, which it
    does not accept. Haiku (any version) and Sonnet 4.5 or older are the
    opposite: no adaptive mode at all, always `budget_tokens`. Parses a
    trailing `<family>-<version>` (`claude-sonnet-4.6` -> ("sonnet", 4.6));
    an unparseable id (or an unrecognised family) falls back to the
    ORIGINAL substring check (a dated/legacy snapshot id that still names
    "opus"/"fable" somewhere) rather than guessing wrong either way."""
    low = (model_id or "").lower()
    # 1.0.1 fixpass finding 11: real model ids hyphenate the minor version
    # (`claude-sonnet-4-6`, `databricks-claude-sonnet-4-6`) -- the old
    # pattern's `(?:\.\d+)?` only ever recognised a LITERAL dot separator
    # (`sonnet-4.6`), so a hyphenated id parsed as bare major version "4"
    # (`float("4") == 4.0 < 4.6`), misclassifying 4.6+ Sonnet as pre-4.6 and
    # routing it into the `budget_tokens` branch it rejects. The minor
    # group is capped at 2 digits with a trailing `(?!\d)` so a snapshot-
    # dated id (`claude-sonnet-4-20250514`) never misreads the date's first
    # digits as a fake minor version -- that case correctly falls back to
    # "no minor" (bare major "4") instead.
    m = re.search(r"(opus|sonnet|haiku|fable|mythos)[-_](\d+)(?:[-_.](\d{1,2})(?!\d))?", low)
    # Bedrock/external ids put the version BEFORE the family
    # (`us-anthropic-claude-3-7-sonnet-20250219-v1-0`): read that order
    # first, or the snapshot date after the family would parse as a huge
    # "major" version and wrongly classify Claude 3.x as adaptive.
    rev = re.search(r"claude[-_](\d+)(?:[-_.](\d{1,2})(?!\d))?[-_](opus|sonnet|haiku)", low)
    if rev is not None:
        family, major, minor = rev.group(3), rev.group(1), rev.group(2)
    elif m is None:
        return "opus" in low or "fable" in low
    else:
        family, major, minor = m.group(1), m.group(2), m.group(3)
    version_str = f"{major}.{minor}" if minor is not None else major
    if family in _ADAPTIVE_ALWAYS_FAMILIES:
        return True
    if family not in _ADAPTIVE_VERSION_GATED_FAMILIES:
        return False  # haiku, or any other recognised-but-never-adaptive family
    try:
        version = float(version_str)
    except ValueError:
        return "opus" in low or "fable" in low
    return version >= _ADAPTIVE_MIN_VERSION


def map_effort_anthropic(effort: Optional[str], model_id: str, *, max_tokens: Optional[int] = None) -> dict:
    """`{"thinking": {"type": "adaptive"}, "output_config": {"effort":
    ...}}` (Fable/Mythos, Opus/Sonnet 4.6+ -- `budget_tokens` is REMOVED
    on these and rejected with a 400) or `{"thinking": {"type": "enabled",
    "budget_tokens": N}}` (Haiku 4.5 and any older Sonnet/Opus, which have
    no adaptive mode at all and REQUIRE a budget) from `--effort`. Returns
    {} when `effort` is falsy (omit -> provider default, never a disabling
    value sent blindly).

    H5c finding 16: the OLD model check (`"opus" in low or "fable" in
    low`) sent `budget_tokens` to every OTHER id, including Sonnet 5 --
    which rejects it outright (400 on the very first `--effort` call on
    that model). `_anthropic_model_supports_adaptive_thinking` is the
    correct, version-aware split (see its own docstring).

    finding 15 (major, h4-h5-h3c review): Anthropic REQUIRES
    `budget_tokens < max_tokens` -- the pre-fix version picked
    `budget_tokens` from `--effort` alone (24,000 for "high") with no
    regard for what `max_tokens` this particular call was actually
    sending (16,384 for an ordinary turn, 4,096 for the summariser),
    400ing on the very first `--effort high` call and on EVERY
    compaction. `max_tokens` is now clamped against here: the budget
    never reaches or exceeds it, floored at Anthropic's own documented
    1,024-token minimum -- if even THAT can't fit under `max_tokens`
    (the summariser's small budget, most notably), thinking is omitted
    entirely for this call rather than sent invalid."""
    if not effort:
        return {}
    if _anthropic_model_supports_adaptive_thinking(model_id):
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}
    budget_by_effort = {"low": 4096, "medium": 10000, "high": 24000, "xhigh": 32000, "max": 32000}
    budget = budget_by_effort.get(effort, 10000)
    if isinstance(max_tokens, int) and max_tokens > 0:
        if max_tokens <= _ANTHROPIC_MIN_THINKING_BUDGET:
            return {}  # no room for even the minimum viable budget -- omit thinking outright
        # 1.0.1 fixpass finding 11: capped at HALF of max_tokens, not
        # max_tokens - 1 -- a non-adaptive model (Haiku 4.5, Sonnet 4.5 or
        # older) getting "high" by default used to reserve nearly the WHOLE
        # max_tokens budget for thinking (16383 of 16384), leaving thinking
        # free to crowd out the actual answer where 1.0.0 sent no thinking
        # at all. Still floored at Anthropic's documented 1,024 minimum --
        # safe even when max_tokens // 2 undershoots it, since the early
        # return just above already guarantees max_tokens > 1,024 here, so
        # 1,024 is still strictly less than max_tokens.
        budget = max(_ANTHROPIC_MIN_THINKING_BUDGET, min(budget, max_tokens // 2))
    return {"thinking": {"type": "enabled", "budget_tokens": budget}}


def map_tool_choice_anthropic(tool_choice) -> Optional[dict]:
    """The harness's internal tool_choice vocabulary (None, "auto",
    "required" -- see providers.routing.map_tool_choice's OWN reverse
    mapping for the OpenAI-dialect side) -> Anthropic's `{"type": "auto"|
    "any"|"tool", "name": ...}` shape."""
    if tool_choice is None or tool_choice == "auto":
        return None  # omit -> Anthropic's own default (auto)
    if tool_choice == "required":
        return {"type": "any"}
    if tool_choice == "none":
        # finding 18: the MAX_STEPS wrap-up call keeps the full frozen tool
        # catalog in `tools` (Anthropic 400s on "tool_use ... must define
        # tools" if history has tool_use blocks but the request omits
        # `tools`) while still forbidding a NEW call this turn.
        return {"type": "none"}
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        name = (tool_choice.get("function") or {}).get("name")
        return {"type": "tool", "name": name} if name else {"type": "any"}
    return None


def _reasoning_text_for_downgrade(reasoning: dict) -> str:
    text = reasoning.get("text") if isinstance(reasoning, dict) else None
    if isinstance(text, str) and text:
        return text
    details = reasoning.get("details") if isinstance(reasoning, dict) else None
    if isinstance(details, list):
        parts = []
        for entry in details:
            if isinstance(entry, dict):
                t = entry.get("text") or entry.get("summary")
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts)
    return ""


def prepare_anthropic_messages(messages: list) -> list:
    """finding 15 (major, h4-h5-h3c review): the derived transcript
    (agent/derive.py) is dialect-agnostic -- an assistant message can
    carry shapes that are only ever valid INTERNALLY, never on
    Anthropic's real wire. Applied to a fresh copy; the derived list (and
    the log underneath it) is never mutated.

      * A logged `thinking` block stores its content under `text` (this
        harness's own storage convention, shared with plain `text`
        blocks) -- renamed to `thinking` here, the field Anthropic's wire
        actually needs (the pre-fix version replayed `text` verbatim,
        400ing on the very first thinking+tool-loop turn).
      * A message-level `reasoning` key (OpenAI-dialect's own
        reasoning_content/reasoning_details bookkeeping -- providers.
        hooks.reasoning_echo) is meaningless to Anthropic's schema and is
        DROPPED -- but never silently: if it carries real text and this
        message has no genuine native `thinking` block of its own (i.e.
        it was captured from a DIFFERENT, OpenAI-dialect model earlier in
        this same session -- a mid-session `/model` switch -- and so can
        never be replayed as a native thinking block, which needs a real
        signature that was never produced for it), that text survives as
        a plain, visibly-labelled TEXT block instead of vanishing.
      * A `thinking` block with an empty `signature` is DROPPED outright --
        a partial block a steer/abort/interrupt cut short BEFORE
        `signature_delta` ever arrived, which Anthropic rejects as
        malformed if replayed. H5b finding 16: a SIGNED block with EMPTY
        text is NOT the same thing -- it is the normal shape when
        `display` defaults to "omitted" (Opus 5/5.5, Fable, Sonnet 5, which
        think by default) and must be echoed back unchanged, never dropped
        just because `text` is empty.
      * A `text` block with empty text is DROPPED outright -- a partial
        block a steer/abort/interrupt cut short before it ever finished
        forming, which Anthropic rejects as malformed if replayed.
      * H5b finding 5: an ASSISTANT message that ends up with NO content
        blocks at all once the two rules above are applied (every block it
        had was an unsigned thinking/empty-text partial) is DROPPED
        ENTIRELY -- Anthropic rejects a non-final assistant message with
        empty content outright. Dropping it can leave two adjacent
        user-role messages (the turn's own user message, immediately
        followed by a steer's user message that used to have the now-gone
        assistant reply between them) -- merged into one afterwards, since
        Anthropic requires alternating roles."""
    out = []
    for msg in messages:
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            out.append(msg)
            continue
        content = msg["content"]
        new_msg = {k: v for k, v in msg.items() if k != "reasoning"}
        new_content: list = []
        reasoning = msg.get("reasoning")
        has_native_thinking = any(isinstance(b, dict) and b.get("type") == "thinking" for b in content)
        if isinstance(reasoning, dict) and not has_native_thinking:
            reasoning_text = _reasoning_text_for_downgrade(reasoning)
            if reasoning_text:
                new_content.append({"type": "text",
                                     "text": f"[Reasoning carried over from a prior model]\n{reasoning_text}"})
        for block in content:
            if not isinstance(block, dict):
                new_content.append(block)
                continue
            btype = block.get("type")
            if btype == "thinking":
                signature = block.get("signature") or ""
                if not signature:
                    continue  # unsigned -- cut short before signature_delta, never valid to replay
                new_block = {k: v for k, v in block.items() if k != "text"}
                new_block["thinking"] = block.get("text") or ""
                new_content.append(new_block)
            elif btype == "text":
                if not (block.get("text") or ""):
                    continue  # empty -- cut short before any content streamed
                new_content.append(block)
            else:
                new_content.append(block)
        if msg.get("role") == "assistant" and not new_content:
            continue  # finding 5: nothing replayable survived -- drop the whole message
        new_msg["content"] = new_content
        out.append(new_msg)

    # finding 5: dropping an empty assistant message above can leave two
    # adjacent user-role messages where it used to sit between them --
    # merge their content into one (Anthropic requires strictly alternating
    # user/assistant roles).
    merged: list = []
    for msg in out:
        if (merged and isinstance(msg, dict) and msg.get("role") == "user"
                and isinstance(merged[-1], dict) and merged[-1].get("role") == "user"
                and isinstance(merged[-1].get("content"), list) and isinstance(msg.get("content"), list)):
            merged[-1] = {**merged[-1], "content": merged[-1]["content"] + msg["content"]}
            continue
        merged.append(msg)
    return merged


def build_anthropic_request_body(
    *, system_text: str, messages: list, tools: Optional[list] = None, tool_choice=None,
    route, profile: ProviderProfile, effort: Optional[str] = None,
    requested_max_tokens: Optional[int] = None, apply_cache_control: bool = True,
) -> dict:
    """Build a native Anthropic Messages body. `messages`/`tools` are
    ALREADY Anthropic-shaped (agent/derive.py's own canonical form) --
    passed through `prepare_anthropic_messages` first (finding 15: fixes
    up the harness's own internal storage shapes into what Anthropic's
    wire actually needs), including any `thinking` block's `signature`
    (Anthropic requires it replayed byte-for-byte; nothing here strips or
    rewrites a genuine one). `apply_cache_control` is True for every
    caller except a Databricks Claude passthrough row that opts out (none
    do today; the flag exists for a future host that rejects the field)."""
    prepared_messages = prepare_anthropic_messages(messages)
    if apply_cache_control:
        system_blocks, out_messages = apply_anthropic_cache_control(system_text, prepared_messages)
    else:
        system_blocks = [{"type": "text", "text": system_text}] if system_text else []
        out_messages = prepared_messages

    max_tokens = requested_max_tokens or profile.max_tokens_default or 8192
    body: dict = {
        "model": route.upstream_model, "messages": out_messages, "stream": True,
        "max_tokens": max_tokens,
    }
    if system_blocks:
        body["system"] = system_blocks
    forced_tool_choice = False
    if tools:
        ordered = sorted(tools, key=lambda t: t.get("name", ""))
        body["tools"] = ordered
        tc = map_tool_choice_anthropic(tool_choice)
        if tc is not None:
            body["tool_choice"] = tc
            forced_tool_choice = True

    # 1.0.1 fixpass finding 11: Anthropic rejects extended thinking together
    # with any FORCED tool_choice (`{"type": "any"}`/`{"type": "tool", ...}`/
    # `{"type": "none"}` -- anything but the "auto" default `tc is None`
    # already omits) -- the repair retry that forces `tool_choice="required"`
    # (agent/loop.py's own leak_parser fallback, `{"type": "any"}` via
    # map_tool_choice_anthropic) must never also send thinking, or the
    # repair call itself 400s. An ordinary turn (`tool_choice` None/"auto")
    # is completely unaffected -- thinking still applies as usual.
    effort = None if forced_tool_choice else effort

    # 1.0.1 hotfix 19: clamp BEFORE map_effort_anthropic ever sees the value
    # -- verified live: a user's own `~/.claude/settings.json` `effortLevel:
    # "xhigh"` (a value real Claude Code's own routes accept) reached
    # `output_config.effort` on a Databricks Claude foundation endpoint
    # verbatim and 400'd ("Input should be 'low', 'medium', 'high' or
    # 'max'") on the very first prompt. `profile.effort_values_supported`
    # is `ANTHROPIC_EFFORT_LEVELS` (no `xhigh`) for every anthropic-
    # passthrough route (`resolve_profile`), so this reproduces on cc:/ant:/
    # any Databricks Claude foundation model alike.
    body.update(map_effort_anthropic(clamp_effort(effort, profile), route.upstream_model, max_tokens=max_tokens))
    return body
