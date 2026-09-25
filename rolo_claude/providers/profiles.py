"""rolo_claude.providers.profiles -- per-provider/model compatibility
profiles (H1 scope A). The report's central finding is that a gateway's
behaviour cannot be guessed from its base URL alone ("for an endpoint it
does not recognize the detection answers as though it were OpenAI itself,
which is wrong for most OpenAI-compatible gateways") -- so every request
this harness builds is driven by an explicit `ProviderProfile`, resolved
from (host, model family) with per-model overrides from `model_table.json`
(an Aider-shaped data file, not code).

H2 must-do 2: `model_table.json` is now the COMPILED form of the canonical
`reports/Open weight model adapter rules.md` block (`tools/
ingest_model_table.py` -- 64 model rows, effective row = family (+) model
(+) host, `unverified` flags carried through per row) -- every field a
tabled row can supply (sampling, reasoning replay/effort, tool_choice
modes, tool id format, leak patterns, Databricks rate limits, ...) is read
from THAT row; `_fallback_family_defaults` below (the old
`_thinking_defaults_for_family`) now only fires for the handful of
proprietary families the open-weight report doesn't cover (claude/gpt/
gemini/generic), exactly the narrow role its name now says.

This module is purely additive: nothing in `bridge.py`'s proxy path imports
it, so the 97 proxy tests are unaffected regardless of what's seeded here.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_LOCK = threading.Lock()
_MODEL_TABLE_CACHE: Optional[dict] = None


def _model_table_path() -> Path:
    return Path(__file__).resolve().parent / "model_table.json"


def load_model_table() -> dict:
    """Load providers/model_table.json once per process; {} if missing or
    unparseable (never raises -- a bad/missing data file degrades to profile
    defaults, it must never crash request building)."""
    global _MODEL_TABLE_CACHE
    with _LOCK:
        if _MODEL_TABLE_CACHE is None:
            try:
                _MODEL_TABLE_CACHE = json.loads(_model_table_path().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _MODEL_TABLE_CACHE = {}
        return _MODEL_TABLE_CACHE


def reset_model_table_cache() -> None:
    """Test seam: force the next load_model_table() to re-read from disk."""
    global _MODEL_TABLE_CACHE
    with _LOCK:
        _MODEL_TABLE_CACHE = None


# Strict Databricks body allowlist (scope B/D): an unlisted field is a 400
# "json: unknown field" on that gateway.
DATABRICKS_BODY_ALLOWLIST = frozenset({
    "messages", "max_tokens", "temperature", "top_p", "stop", "stream",
    "tools", "tool_choice", "reasoning_effort", "stream_options",
})
# OpenRouter-only fields, emitted ONLY when the host is openrouter.ai.
OPENROUTER_EXTRA_FIELDS = frozenset({"provider", "reasoning", "models", "plugins", "usage"})


def model_family(model_id: str) -> str:
    """Coarse model-family classifier from a bare upstream model id/name --
    drives thinking_format/reasoning_replay/sampling defaults whenever
    model_table.json has no exact-match row for this id."""
    low = (model_id or "").lower()
    if "claude" in low:
        return "claude"
    if "deepseek" in low:
        return "deepseek"
    if "kimi" in low or "moonshot" in low:
        return "kimi"
    if "glm" in low or "z-ai" in low or "zhipu" in low:
        return "glm"
    if "qwen" in low:
        return "qwen"
    if "gemini" in low:
        return "gemini"
    if "grok" in low:
        return "grok"
    if "minimax" in low:
        return "minimax"
    if "gpt" in low or low.startswith("openai") or "o1" in low or "o3" in low:
        return "gpt"
    return "generic"


@dataclass(frozen=True)
class ProviderProfile:
    system_vs_developer: str = "system"           # "system" | "developer" | "none"
    max_tokens_field: str = "max_tokens"
    reasoning_effort_supported: bool = False
    # none | deepseek_reasoning_content | openrouter_details | anthropic_thinking | fmapi_blocks
    thinking_format: str = "none"
    reasoning_replay: str = "empty"                # text | empty | details | thinking
    stream_usage: bool = True
    store: bool = False
    strict: bool = False
    tool_result_name: bool = False
    body_allowlist: Optional[frozenset] = None      # None == no restriction
    tools_max: Optional[int] = None
    supports_temperature_in_thinking: bool = False
    host_specific_fields: bool = False              # OpenRouter-only fields allowed
    family: str = "generic"
    use_temperature: bool = True
    # H5 scope F (OpenCode Appendix G transform table): `top_p` needs to be
    # gated INDEPENDENTLY of `temperature` for a row like DeepSeek V4 Flash
    # (0731) -- "omit temperature, but DO send top_p: 0.95". None (every
    # row before H5) means "follow use_temperature", so this is a pure
    # opt-in override with zero effect on any other row's wire body.
    use_top_p: Optional[bool] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens_default: Optional[int] = None
    max_tokens_cap: Optional[int] = None
    reasoning_default_effort: Optional[str] = None
    openrouter_pin: Optional[dict] = None
    edit_format: str = "diff"
    # DeepSeek V4 thinking mode 400s on tool_choice required/named; Kimi
    # K2.x/K2.5/K2.6/K2.7 and the WHOLE Qwen/GLM families accept only
    # auto|none (Kimi K3 adds required back) -- the request builder's
    # content-side "retry with tool_choice: required" fallback must be
    # skipped whenever this is False (coordinator research,
    # research_notes/Open weight model adapter rules/*.md).
    tool_choice_required_supported: bool = True
    top_k: Optional[int] = None
    # GLM-5.3/5.3-Flash (Z.ai + Databricks): thinking.type=disabled -> HTTP
    # 400 code 1210 -- a resolved effort of None/"none"/"disabled" must be
    # forced to `reasoning_default_effort` instead of omitted/disabled.
    reasoning_no_disable: bool = False
    # H2 code-branch hook fields (must-do 3), all row-driven:
    # preserve | kimi_functions_idx | alnum9 | minimax_preserve (hooks.tool_id_normalize).
    tool_id_format: str = "preserve"
    # DeepSeek V4 rows only: on OpenRouter, ALSO send reasoning_content
    # (accumulated text, "" when absent) beside the verbatim reasoning_details
    # array (finding 2's dual-field fix; hooks.reasoning_echo).
    reasoning_dual_field: bool = False
    tool_leak_patterns: tuple = ()  # hooks.leak_parser pattern names for this row
    system_placement: str = "first"  # "first" | "fold_into_first_user" (hooks.system_normalize)
    context_tokens: Optional[int] = None  # row-reported context window, informational
    databricks_rate_limits: Optional[dict] = None  # {itpm, otpm, qph} (hooks.max_tokens_budget)
    unverified: tuple = ()  # field names this row's data lacks a primary source for
    # H9 sampling-table audit: every model_table.json row already carries
    # this (DeepSeek V4/Grok/MiniMax: presence_penalty/frequency_penalty;
    # Kimi K2.6+/K3: temperature/top_p/n/presence_penalty/frequency_penalty
    # fixed server-side; Grok: +stop/logprobs/top_logprobs) but nothing
    # read it back out of the row into a ProviderProfile field, so it was
    # pure decoration -- request.build_request_body now pops any of these
    # keys from the final body right before sending (see there). Currently
    # a no-op in practice (nothing in this codebase sets presence_penalty/
    # frequency_penalty/logit_bias/n/logprobs/stop today), but it's the
    # documented safety net the table's own data implies, and it becomes
    # load-bearing the moment any of those surfaces (a hook rewrite, a
    # future settings knob) starts populating one.
    sampling_unsupported_params: tuple = ()


def _fallback_family_defaults(family: str, dialect: str) -> "tuple[str, str, bool]":
    """(thinking_format, reasoning_replay, reasoning_effort_supported) for a
    model with NO row in the ingested table -- i.e. a family the open-weight
    adapter report doesn't cover at all (claude/gpt/plain-gemini/generic).
    Every row that DOES exist in model_table.json (H2 must-do 2: the
    compiled `reports/Open weight model adapter rules.md` block) overrides
    every one of these three via `row.get(...)` below; this function is a
    narrow fallback now, not the primary source it was pre-H2."""
    if dialect == "anthropic-passthrough":
        return "anthropic_thinking", "thinking", True
    if family in ("deepseek", "kimi", "glm", "grok", "minimax"):
        # coordinator research (reports/Open weight model adapter rules.md):
        # GLM/MiniMax/Mistral join the mandatory reasoning-echo list --
        # MiniMax's <think>/reasoning_details must be replayed unchanged.
        return "deepseek_reasoning_content", "text", True
    if family in ("claude", "gpt", "gemini"):
        return "fmapi_blocks", "text", True
    return "none", "empty", False


def resolve_profile(route, model_table: Optional[dict] = None) -> ProviderProfile:
    """Resolve a `ProviderProfile` from a `providers.routing.Route` (duck-
    typed: anything with `.provider`, `.upstream_model`, `.dialect`) plus
    `model_table.json` per-model overrides. `route.provider` is
    "databricks" | "openrouter" | "anthropic"; `route.dialect` is
    "openai-chat" | "anthropic-passthrough"."""
    model_table = model_table if model_table is not None else load_model_table()
    family = model_family(route.upstream_model)
    host_key = route.provider if route.provider in ("databricks", "openrouter") else None
    row = ((model_table.get(host_key) or {}).get(route.upstream_model) or {}) if host_key else {}

    if route.dialect == "anthropic-passthrough":
        return ProviderProfile(
            system_vs_developer="system", thinking_format="anthropic_thinking",
            reasoning_replay="thinking", reasoning_effort_supported=True,
            family=family, edit_format=row.get("edit_format", "diff"),
        )

    thinking_format, replay, effort_supported = _fallback_family_defaults(family, route.dialect)
    replay = row.get("reasoning_replay", replay)
    effort_supported = row.get("reasoning_effort_supported", effort_supported)
    # Fallback ONLY (untabled families -- claude/gpt/generic): qwen/glm
    # reject tool_choice "required" as a coarse family rule; every tabled
    # row (the common case from H2 on) sets tool_choice_required_supported
    # explicitly from its own tool_choice_modes, so this default is never
    # consulted for a DeepSeek/Kimi/GLM/Qwen/MiniMax model.
    tc_required_default = family not in ("qwen", "qwen-coder", "qwen-qwq", "glm")
    hook_fields = dict(
        tool_id_format=row.get("tool_id_format", "preserve"),
        reasoning_dual_field=bool(row.get("reasoning_dual_field", False)),
        tool_leak_patterns=tuple(row.get("tool_leak_patterns") or ()),
        system_placement=row.get("system_placement", "first"),
        context_tokens=row.get("context_tokens"),
        databricks_rate_limits=row.get("rate_limits"),
        unverified=tuple(row.get("unverified") or ()),
        sampling_unsupported_params=tuple(row.get("sampling_unsupported_params") or ()),
    )

    if route.provider == "databricks":
        default_use_temp = family not in ("deepseek", "kimi", "glm", "qwen", "qwen-coder")
        return ProviderProfile(
            system_vs_developer="system", max_tokens_field="max_tokens",
            reasoning_effort_supported=effort_supported, thinking_format=thinking_format,
            reasoning_replay=replay, stream_usage=True, store=False, strict=True,
            tool_result_name=False, body_allowlist=DATABRICKS_BODY_ALLOWLIST, tools_max=32,
            supports_temperature_in_thinking=False, host_specific_fields=False, family=family,
            use_temperature=row.get("use_temperature", default_use_temp), use_top_p=row.get("use_top_p"),
            temperature=row.get("temperature"), top_p=row.get("top_p"), top_k=row.get("top_k"),
            max_tokens_default=row.get("max_tokens_default", 16384),
            max_tokens_cap=row.get("max_tokens_cap", 16384),
            reasoning_default_effort=row.get("reasoning_default_effort"),
            reasoning_no_disable=bool(row.get("reasoning_no_disable", False)),
            edit_format=row.get("edit_format", "diff"),
            tool_choice_required_supported=row.get("tool_choice_required_supported", tc_required_default),
            **hook_fields,
        )

    # openrouter (also the fallback for any other openai-chat-dialect host)
    return ProviderProfile(
        system_vs_developer="system", max_tokens_field="max_tokens",
        reasoning_effort_supported=effort_supported, thinking_format="openrouter_details",
        reasoning_replay=replay if replay != "text" else "details",
        stream_usage=True, store=False, strict=False,
        # H3 must-do (frozen-catalog host cap): 128, matching the plan's
        # "the host cap (128 OpenRouter, 32 Databricks)" -- the frozen-
        # catalog SELECTION step (agent/catalog.py) pre-selects under this
        # cap so ToolCatalogTooLarge should never actually fire in normal
        # operation; this is the backstop that makes a selection bug a
        # clear error instead of a silent truncation/wire 400.
        tool_result_name=False, body_allowlist=None, tools_max=128,
        supports_temperature_in_thinking=False, host_specific_fields=(route.provider == "openrouter"),
        family=family, use_temperature=row.get("use_temperature", True), use_top_p=row.get("use_top_p"),
        temperature=row.get("temperature"), top_p=row.get("top_p"), top_k=row.get("top_k"),
        max_tokens_default=row.get("max_tokens_default"), max_tokens_cap=row.get("max_tokens_cap"),
        reasoning_default_effort=row.get("reasoning_default_effort"),
        reasoning_no_disable=bool(row.get("reasoning_no_disable", False)),
        openrouter_pin=row.get("openrouter_pin"), edit_format=row.get("edit_format", "diff"),
        tool_choice_required_supported=row.get("tool_choice_required_supported", tc_required_default),
        **hook_fields,
    )


EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


_DISABLING_EFFORTS = frozenset({"none", "disabled", "off", "minimal"})


def map_effort(effort: Optional[str], profile: ProviderProfile) -> dict:
    """--effort -> the field(s) a request body actually needs for this
    profile (scope J): `reasoning.effort` on OpenRouter, `reasoning_effort`
    on Databricks/DeepSeek-shaped chat completions, a thinking budget on
    Messages routes. Returns a dict of EXTRA body keys to merge in (empty
    if `effort` is None or the profile doesn't support it).

    `profile.reasoning_no_disable` (GLM-5.3/5.3-Flash: `thinking.type:
    disabled` -> HTTP 400 code 1210) forces an EXPLICITLY disabling
    `--effort` to the profile's own default instead of turning reasoning
    off; `effort is None` (no `--effort` given at all) is left alone --
    that means "omit the field", which safely takes the server's own
    default, not "send a disabling value"."""
    if effort and effort.lower() in _DISABLING_EFFORTS and profile.reasoning_no_disable and profile.reasoning_default_effort:
        effort = profile.reasoning_default_effort
    if not effort or not profile.reasoning_effort_supported:
        return {}
    if profile.thinking_format == "anthropic_thinking":
        budget_by_effort = {"low": 4096, "medium": 10000, "high": 24000, "xhigh": 32000, "max": 32000}
        return {"thinking": {"type": "enabled", "budget_tokens": budget_by_effort.get(effort, 10000)}}
    if profile.host_specific_fields:
        return {"reasoning": {"effort": effort}}
    return {"reasoning_effort": effort}
