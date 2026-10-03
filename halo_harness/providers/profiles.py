"""halo_harness.providers.profiles -- per-provider/model compatibility
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
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("bridge")

_LOCK = threading.Lock()
_MODEL_TABLE_CACHE: Optional[dict] = None

# The harness's own general `--effort`/`/effort` vocabulary (cli.py's own
# `choices=`, profiles.py's `map_effort` for chat-dialect routes below) --
# moved above `ProviderProfile` (1.0.1 hotfix 19) so the dataclass's own
# `effort_values_supported` field can default to it directly.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# 1.0.1 fixpass finding 13: every OpenAI-shaped chat-dialect route
# (Databricks openai-chat, OpenRouter) -- "max" is an ANTHROPIC-only level
# (`output_config.effort`'s own enum); a chat route that inherited the
# unrestricted `EFFORT_LEVELS` default let `/effort` offer (and send) "max"
# on every such route, 400ing every turn until the user picked a different
# level. `xhigh` stays -- it's the harness's own strongest OpenAI-dialect-
# style level, genuinely meaningful there (`clamp_effort`'s own docstring).
OPENAI_EFFORT_LEVELS = ("low", "medium", "high", "xhigh")

# 1.0.1 hotfix 19: the Anthropic MESSAGES API's `output_config.effort` field
# (adaptive-thinking Opus/Sonnet 4.6+/Fable/Mythos, `providers/request.py`'s
# `map_effort_anthropic`) rejects `xhigh` outright -- "Input should be
# 'low', 'medium', 'high' or 'max'", verified live on a Databricks Claude
# foundation endpoint (`dbx:databricks-claude-opus-4-6`, first prompt, no
# `--effort` given at all -- the value came from the user's OWN
# `~/.claude/settings.json` `effortLevel: "xhigh"`, a value real Claude
# Code's own settings schema allows for ITS routes but this harness's
# Anthropic-passthrough routes must not forward as-is). Every
# `thinking_format == "anthropic_thinking"` profile uses this narrower set
# instead of the general `EFFORT_LEVELS` above.
ANTHROPIC_EFFORT_LEVELS = ("low", "medium", "high", "max")


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


def decision_only_info(model_id: str, model_table: Optional[dict] = None) -> Optional[dict]:
    """Halo 2.0.2 round 5 (Qwen-at-work brief, item 1): `{"reason": str}`
    when `model_id` is a DECISION-ONLY endpoint -- one that answers with a
    verdict (yes/no, a choice, a score) and was never meant to receive
    `tools`/`tool_choice` at all. `None` for every ordinary chat/tool-
    calling model. The owner's work-VM "openjev qwen" bugreport is the
    motivating case: Databricks documents `databricks-openjev-qwen35-4b`
    as exactly this shape (docs/harness/QWEN-RESEARCH.md).

    Checked two ways, either one enough -- deliberately DATA, not code, so
    a wrong guess is a one-line model_table.json edit:
    1. A specific row's own `capabilities.decision_only` (the confirmed,
       tabled case) -- its `capabilities.description` is Databricks' own
       one-line wording when present.
    2. A case-insensitive substring match of the top-level
       `decision_only_name_patterns.patterns` list against the bare
       model id -- the untabled-endpoint net ("for endpoints matching
       openjev/jev-judge names"), so a differently-named judge-style
       endpoint Halo has never seen gets classified correctly from editing
       that list alone, never this function.

    Consulted by `resolve_profile` (`ProviderProfile.decision_only`/
    `tools_supported`), `controller.list_models()` (the picker's "judge /
    decision" group) and `Controller.set_model`/`headless.build_session`
    (never the session model -- routed to the `judge` role instead)."""
    model_table = model_table if model_table is not None else load_model_table()
    low = (model_id or "").lower()
    for host_key in ("databricks", "openrouter"):
        row = (model_table.get(host_key) or {}).get(model_id)
        caps = (row or {}).get("capabilities") if isinstance(row, dict) else None
        if isinstance(caps, dict) and caps.get("decision_only"):
            return {"reason": caps.get("description") or
                    "this endpoint only answers yes/no, choice and scoring questions -- it does not take tools"}
    patterns = (model_table.get("decision_only_name_patterns") or {}).get("patterns") or []
    for pat in patterns:
        if isinstance(pat, str) and pat and pat.lower() in low:
            return {"reason": f"this endpoint's name matches the known decision-only/judge naming pattern {pat!r} "
                               "-- it answers yes/no, choice and scoring questions, not tool calls"}
    return None


def decision_only_notice(model_id: str, model_table: Optional[dict] = None) -> Optional[str]:
    """One line for a human -- the picker's note, the "never the session
    model" refusal, and the clear tools-present request error all share
    this EXACT wording (single source) rather than each phrasing it
    separately. `None` when `model_id` isn't decision-only."""
    info = decision_only_info(model_id, model_table)
    if info is None:
        return None
    reason = info["reason"].rstrip(". ")
    return (f"{model_id} {reason}. Use the judge role instead of the session model "
            f"(`/roles set judge {model_id}`, or `Agent(role=\"judge\")`).")


def edit_hint_for(provider: str, model_id: str, model_table: Optional[dict] = None) -> Optional[str]:
    """H12 Part C (RECOMMENDATIONS.md P0 #3 / §4 telemetry: DeepSeek V4.1
    Flash's 8% Edit "Found multiple matches" failure rate is almost
    entirely too-little old_string context): the Edit tool's optional
    per-family context-line hint from `model_table.json`'s own top-level
    `"edit_hints"` map, keyed by the SAME coarse family `model_family()`
    returns -- a specific (provider, model_id) row's own `"edit_hint"` key
    (including an explicit `""` to silence the family default for just
    that one model) wins over the family default when present. Claude/GPT
    rows carry no family default at all (no `"claude"`/`"gpt"` key in the
    table) -- a native tool-calling loop doesn't need the extra nudge the
    DeepSeek/Kimi/GLM/Qwen/MiniMax families do. Returns None (never `""`)
    when there is nothing to append, so a caller can `if hint:` directly."""
    model_table = model_table if model_table is not None else load_model_table()
    family = model_family(model_id)
    host_key = provider if provider in ("databricks", "openrouter") else None
    row = ((model_table.get(host_key) or {}).get(model_id) or {}) if host_key else {}
    if "edit_hint" in row:
        return row.get("edit_hint") or None
    return (model_table.get("edit_hints") or {}).get(family) or None


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
    # 1.0.1 hotfix 22: when set, a REQUEST THAT CARRIES TOOLS on a chat-
    # completions route forces its effort to exactly this value instead of
    # whatever --effort/session effort chose -- the gpt-6 family 400s
    # otherwise ("Function tools with reasoning_effort are not supported
    # for gpt-6-sol in /v1/chat/completions... set reasoning_effort to
    # 'none'"). A tool-less request on the same profile is unaffected.
    reasoning_effort_with_tools: Optional[str] = None
    # 1.0.1 hotfix 19: the effort VALUES this route's wire format actually
    # accepts -- `request.py`'s effort-clamping helper (`clamp_effort`)
    # reads this before putting anything on the wire. Defaults to the full
    # harness vocabulary (today's behaviour, unchanged, for every chat-
    # dialect route); the anthropic-passthrough branch of `resolve_profile`
    # below overrides it to `ANTHROPIC_EFFORT_LEVELS` (no `xhigh`).
    effort_values_supported: tuple = EFFORT_LEVELS
    # Halo 2.0.1 (GLM-brief.md item 1): an EXPLICIT {requested: sent} table
    # for a route whose narrower-than-harness-vocabulary levels don't map
    # onto `reasoning_default_effort`/the generic xhigh<->max narrowing
    # `clamp_effort` already does -- e.g. Databricks GLM's own `medium ->
    # high, minimal -> low, xhigh -> max, none -> low`, where "minimal"/
    # "none" must land on the CHEAPEST accepted level, not this route's
    # default. None (every row before this) means "no explicit table --
    # keep using the generic fallback below", so this is a pure opt-in with
    # zero effect on any other row's clamping.
    effort_clamp_map: Optional[dict] = None
    # Halo 2.0.1 (GLM-brief.md item 1, "default high"): on a route whose
    # OWN silent default is its most expensive level (Databricks GLM:
    # reasoning always on, omitted `reasoning_effort` == `max`), a call
    # with no effort configured anywhere sends `reasoning_default_effort`
    # explicitly instead of omitting the field, and an effort inherited
    # from Claude Code's settings (`effortLevel`, a Claude-oriented knob,
    # typically `xhigh`) that the route does not accept lands on the same
    # default rather than on the clamp map's most expensive mapping. An
    # explicit `--effort`/`/effort` value still goes through the clamp map
    # exactly as written (so `xhigh` -> `max` stays a deliberate choice).
    default_effort_when_unset: bool = False
    # Halo 2.0.2 round 5 (Qwen-at-work brief, item 1): True for a
    # decision-only/judge endpoint (`decision_only_info` above) --
    # `decision_only_reason` is its human-readable "why" (Databricks' own
    # wording when tabled). `tools_supported` is the narrower, purely
    # mechanical flag `providers/request.py::build_request_body` actually
    # gates on before putting `tools` on the wire: False whenever
    # `decision_only` is True, OR (Databricks only) a prior live request
    # against this exact endpoint already proved it rejects tools
    # (`providers.learned_rules.learned_tools_rejected` -- a model with NO
    # row/pattern match at all can still end up here after one real 400).
    # A decision-only row is therefore always `tools_supported=False`, but
    # the reverse need not hold.
    decision_only: bool = False
    decision_only_reason: Optional[str] = None
    tools_supported: bool = True
    # The bare upstream model id this profile was resolved for
    # (`route.upstream_model`) -- None only for a profile built by hand in
    # a test, never for one `resolve_profile` returns. Exists so a clear
    # error (`providers.request.ToolsNotSupported`) can name the actual
    # model without `convert_tools`/`build_request_body` needing their own
    # separate `route`/model-id parameter just for a message string.
    model_id: Optional[str] = None


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


def resolve_profile(route, model_table: Optional[dict] = None, state_dir=None) -> ProviderProfile:
    """Resolve a `ProviderProfile` from a `providers.routing.Route` (duck-
    typed: anything with `.provider`, `.upstream_model`, `.dialect`) plus
    `model_table.json` per-model overrides. `route.provider` is
    "databricks" | "openrouter" | "anthropic"; `route.dialect` is
    "openai-chat" | "anthropic-passthrough".

    1.0.1 part 2 (item 22 remainder): `state_dir`, when given, lets a
    Databricks route with no `reasoning_effort_with_tools` of its own (no
    `model_table.json` row, not a `gpt-6`/`gpt-5-6` name) fall back to a
    LEARNED per-endpoint rule from a prior live 400 on this same endpoint
    (`providers.learned_rules`) -- optional and additive; every existing
    caller that omits it keeps today's behaviour (no learned-rule lookup)
    byte for byte."""
    model_table = model_table if model_table is not None else load_model_table()
    family = model_family(route.upstream_model)
    host_key = route.provider if route.provider in ("databricks", "openrouter") else None
    row = ((model_table.get(host_key) or {}).get(route.upstream_model) or {}) if host_key else {}
    # Halo 2.0.2 round 5 item 1: computed ONCE, shared by every branch below
    # (including the anthropic-passthrough early return -- a native Claude
    # route is never decision-only, but this keeps every ProviderProfile
    # this function can return carrying the same two fields regardless).
    _decision = decision_only_info(route.upstream_model, model_table)
    decision_only = _decision is not None
    decision_only_reason = _decision.get("reason") if _decision else None

    if route.dialect == "anthropic-passthrough":
        return ProviderProfile(
            system_vs_developer="system", thinking_format="anthropic_thinking",
            reasoning_replay="thinking", reasoning_effort_supported=True,
            family=family, edit_format=row.get("edit_format", "diff"),
            effort_values_supported=ANTHROPIC_EFFORT_LEVELS,
            model_id=route.upstream_model,
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
    # 1.0.1 fixpass finding 13: the narrowed chat-dialect default (no "max",
    # an Anthropic-only level) -- but a TABLED row's own `reasoning_default_
    # effort` (data-driven, from the ingested adapter-rules report) must
    # always be one of this profile's OWN accepted values, even when it
    # falls outside the generic default set (verified: GLM-5.3's row
    # documents "max" as its real default -- `reasoning_no_disable` forces
    # a disabling `--effort none` UP to it, so clamp_effort must never then
    # reject that same value as unsupported and silently fall back to
    # "medium" instead). An explicit `effort_values_supported` row key (none
    # today) still wins outright over both.
    effort_values_supported = OPENAI_EFFORT_LEVELS
    row_default_effort = row.get("reasoning_default_effort")
    if row_default_effort and row_default_effort not in effort_values_supported:
        effort_values_supported = effort_values_supported + (row_default_effort,)
    effort_values_supported = row.get("effort_values_supported", effort_values_supported)
    hook_fields = dict(
        tool_id_format=row.get("tool_id_format", "preserve"),
        reasoning_dual_field=bool(row.get("reasoning_dual_field", False)),
        tool_leak_patterns=tuple(row.get("tool_leak_patterns") or ()),
        system_placement=row.get("system_placement", "first"),
        context_tokens=row.get("context_tokens"),
        databricks_rate_limits=row.get("rate_limits"),
        unverified=tuple(row.get("unverified") or ()),
        sampling_unsupported_params=tuple(row.get("sampling_unsupported_params") or ()),
        decision_only=decision_only,
        decision_only_reason=decision_only_reason,
    )

    if route.provider == "databricks":
        # item 1/4: never sent to a decision-only endpoint; also learns
        # False for an UNTABLED endpoint once a live request already
        # proved it rejects tools (see agent/loop.py's `_step`, the
        # `is_tools_rejected_message` branch that calls
        # `learn_tools_rejected` -- the same shape `reasoning_effort_
        # with_tools`'s own learned-rule lookup below already uses).
        tools_supported = not decision_only
        if tools_supported and state_dir is not None:
            from halo_harness.providers.learned_rules import learned_tools_rejected
            if learned_tools_rejected(state_dir, "databricks", route.upstream_model):
                tools_supported = False
        default_use_temp = family not in ("deepseek", "kimi", "glm", "qwen", "qwen-coder")
        # 1.0.1 fixpass finding 12: this rule is Databricks-only (the
        # ORIGINAL hotfix 22 put it in the shared `hook_fields` dict above,
        # splatted into BOTH branches, so it also fired for OpenRouter's own
        # "openai/gpt-6" id -- silently disabling reasoning and making
        # `/effort` a no-op there, never verified/intended for that host).
        # Driven by an explicit model_table.json row now (added alongside
        # this fix for both real models.dev name shapes,
        # `databricks-gpt-6-*` and `databricks-gpt-5-6-*`, `sol`/`luna`/
        # `terra`); the substring check is only a LAST-RESORT net for a
        # variant not yet tabled -- corrected to actually match the real
        # catalog naming: `databricks-gpt-5-6-sol` does NOT contain the
        # bare substring "gpt-6" the old code checked for (there's a "-5-"
        # in between), so this also checks for "gpt-5-6" explicitly. A
        # tabled row's own `reasoning_effort_with_tools` (including an
        # explicit `null` to opt back OUT) always wins via `row.get`.
        reasoning_effort_with_tools = row.get(
            "reasoning_effort_with_tools",
            "none" if ("gpt-6" in route.upstream_model.lower() or "gpt-5-6" in route.upstream_model.lower())
            else None)
        if reasoning_effort_with_tools is None and state_dir is not None:
            # item 22 remainder: a model_table.json row (including an
            # explicit `null` there, which this `.get` default never even
            # reaches) always wins -- the learned cache only ever fills
            # the gap for an endpoint the table doesn't cover yet.
            from halo_harness.providers.learned_rules import learned_reasoning_effort_with_tools
            reasoning_effort_with_tools = learned_reasoning_effort_with_tools(
                state_dir, "databricks", route.upstream_model)
        # GLM-brief.md 2026-10-01 (Databricks Foundation Model APIs,
        # "supported-models"/"query reasoning models" pages): every
        # `databricks-glm-*` endpoint -- including one with NO
        # model_table.json row of its own yet (e.g. a future
        # "databricks-glm-5" release) -- accepts EXACTLY low/high/max for
        # `reasoning_effort`; "max" is the GATEWAY's own SILENT fallback for
        # anything else (medium/minimal/xhigh/none -- never a 400), which is
        # why a harness-level "medium" used to run as an undocumented "max"
        # with no error at all (a user report, "it pauses": a thinking phase
        # at the model's MOST expensive setting, not a harness hang). A
        # row's own explicit `reasoning_default_effort`/
        # `effort_values_supported`/`effort_clamp_map` still wins via
        # `row.get(key, <this default>)`, so a future row can override any
        # one of these three independently.
        databricks_effort_default = row.get("reasoning_default_effort")
        databricks_effort_values = effort_values_supported
        databricks_effort_clamp_map = row.get("effort_clamp_map")
        if family == "glm":
            databricks_effort_default = row.get("reasoning_default_effort", "high")
            databricks_effort_values = tuple(row.get("effort_values_supported") or ("low", "high", "max"))
            databricks_effort_clamp_map = row.get("effort_clamp_map") or {
                "medium": "high", "minimal": "low", "xhigh": "max", "none": "low",
            }
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
            reasoning_default_effort=databricks_effort_default,
            reasoning_no_disable=bool(row.get("reasoning_no_disable", False)),
            edit_format=row.get("edit_format", "diff"),
            tool_choice_required_supported=row.get("tool_choice_required_supported", tc_required_default),
            reasoning_effort_with_tools=reasoning_effort_with_tools,
            effort_values_supported=databricks_effort_values,
            effort_clamp_map=databricks_effort_clamp_map,
            default_effort_when_unset=bool(row.get("default_effort_when_unset", family == "glm")),
            tools_supported=tools_supported,
            model_id=route.upstream_model,
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
        # GLM-brief.md item 1: "Keep the Z.ai-direct set (seven values) on
        # OpenRouter z-ai/glm-* routes where the provider forwards them" --
        # a row's own `effort_values_supported` (added to the four GLM 5.x
        # rows) always wins; this generic passthrough is a no-op (None) for
        # every row that doesn't set one, same as before.
        effort_clamp_map=row.get("effort_clamp_map"),
        openrouter_pin=row.get("openrouter_pin"), edit_format=row.get("edit_format", "diff"),
        tool_choice_required_supported=row.get("tool_choice_required_supported", tc_required_default),
        # 1.0.1 fixpass finding 12: no substring guessing on this host at
        # all -- only an explicit model_table.json row (e.g. a future
        # "openrouter"."openai/gpt-6" row) ever sets this here.
        reasoning_effort_with_tools=row.get("reasoning_effort_with_tools"),
        effort_values_supported=effort_values_supported,
        tools_supported=not decision_only,
        model_id=route.upstream_model,
        **hook_fields,
    )


_DISABLING_EFFORTS = frozenset({"none", "disabled", "off", "minimal"})


def clamp_effort(effort: Optional[str], profile: ProviderProfile) -> Optional[str]:
    """1.0.1 hotfix 19: the LAST step before `effort` reaches any body-
    building function (`map_effort`/`map_effort_anthropic`) -- makes it
    impossible for a value outside `profile.effort_values_supported` to
    reach the wire, regardless of where it came from (`--effort`, `/effort`,
    settings `effortLevel`/`modelSettings.<id>.effortLevel`, or this
    session's own Anthropic-family "high" default). `None` (nothing
    configured at all) passes through unchanged -- clamping only ever
    narrows a REAL value, it never invents one.

    `xhigh` (the harness's own strongest OpenAI-dialect-style level) on a
    route that doesn't list it becomes `max` specifically -- not the
    generic "unknown -> route default" rule below -- since `max` is every
    such route's own equivalent strongest level, not an arbitrary fallback.
    1.0.1 part 2 (reviewer minor): the mirror case -- `max` (the harness's
    strongest ANTHROPIC-dialect-style level) on a chat-dialect route that
    has no `max` but DOES list `xhigh` becomes `xhigh`, that route's own
    equivalent strongest level, instead of falling all the way through to
    the bland "medium"/`reasoning_default_effort` default below (verified:
    a session carrying `effort="max"` -- e.g. switched from an Anthropic-
    family route, or anything else that set the harness's own ceiling --
    onto a plain OpenAI-dialect route used to silently downgrade to
    "medium", two full levels below what that route can actually do). Any
    OTHER value the route's own set doesn't recognize (a typo, a stale
    config from a level this harness has since renamed) becomes the
    route's OWN default (`reasoning_default_effort` when the profile has
    one, else the harness-wide default `"medium"`) rather than silently
    passing through and risking the exact 400 this function exists to
    prevent."""
    if effort is None:
        return None
    supported = profile.effort_values_supported or EFFORT_LEVELS
    if effort in supported:
        return effort
    if profile.effort_clamp_map and effort in profile.effort_clamp_map:
        # Halo 2.0.1: an explicit per-route table wins outright over the
        # generic xhigh/max/default narrowing below -- e.g. Databricks GLM
        # sends "minimal"/"none" as "low" (the cheapest accepted level),
        # which the generic rule (unknown value -> this route's own
        # default) would get wrong (it would send "high" instead).
        clamped = profile.effort_clamp_map[effort]
        log.debug("clamp_effort: %r mapped to %r by this route's explicit clamp table", effort, clamped)
        return clamped
    if effort == "xhigh" and "xhigh" not in supported:
        clamped = "max" if "max" in supported else (profile.reasoning_default_effort or "medium")
    elif effort == "max" and "max" not in supported and "xhigh" in supported:
        clamped = "xhigh"
    else:
        clamped = profile.reasoning_default_effort if profile.reasoning_default_effort in supported else "medium"
    log.debug("clamp_effort: %r not accepted by this route (accepts %r) -- using %r instead",
              effort, supported, clamped)
    return clamped


# GLM-brief.md 2026-10-01 (Z.ai GLM API, docs.z.ai chat completion
# reference): "temperature range [0, 1] default 1.0" -- every current
# model_table.json GLM row already seeds 1.0 (in range), so this is a
# defensive wire-safety net (a future row edit or override cannot put an
# out-of-range value on the wire), not a fix for any value seeded today.
GLM_TEMPERATURE_RANGE = (0.0, 1.0)


def clamp_temperature(value: Optional[float], profile: ProviderProfile) -> Optional[float]:
    """GLM-brief.md item 5: "temperature is clamped to [0, 1] on GLM
    routes" -- a no-op for `value is None` or any non-GLM family. Applied
    at the one place `providers/request.py::build_request_body` puts
    `profile.temperature` on the wire; also used by `providers/effort.py::
    requested_vs_sent` to report a clamp when the table value itself is
    ever edited outside this range."""
    if value is None or profile.family != "glm":
        return value
    lo, hi = GLM_TEMPERATURE_RANGE
    return max(lo, min(hi, value))


def effort_display_override(profile: Optional[ProviderProfile]) -> Optional[str]:
    """1.0.1 part 2 (item 22 remainder): the "<value> (tools)" display
    string for `/effort`, the EffortCard's own description line, and the
    status bar's effort tag -- when `profile`'s route forces an EXPLICIT
    `reasoning_effort_with_tools` override (the gpt-6 table rule, or a
    LEARNED per-endpoint rule -- see `providers.learned_rules`), that
    override is what's actually sent on essentially every real (tool-
    carrying) turn, regardless of whatever `--effort`/`/effort` configured
    -- showing it here beats a configured value that reads as a lie the
    moment the next turn goes out. None when this route has no such
    override (the Anthropic thinking-budget shape never uses this field at
    all) -- the caller shows its own configured/default value instead."""
    if profile is None or not profile.reasoning_effort_with_tools:
        return None
    if profile.thinking_format == "anthropic_thinking":
        return None
    return f"{profile.reasoning_effort_with_tools} (tools)"


def resolve_effective_effort(effort: Optional[str], profile: ProviderProfile) -> Optional[str]:
    """Halo 2.0.1: the `_DISABLING_EFFORTS`-vs-`effort_clamp_map`-vs-
    `clamp_effort` decision shared by `map_effort` below (the wire-body
    builder) and `providers/effort.py::sent_effort` (the DISPLAY helper
    `/status`/`/context`/the stream-json init line/W2b's card and status
    chip all read) -- extracted so the two can never drift apart. Excludes
    the `has_tools`+`reasoning_effort_with_tools` override, which each
    caller checks FIRST and returns early for on its own (that override
    also skips `reasoning_effort_supported`, which only `map_effort`'s own
    caller-side gate applies).

    `profile.reasoning_no_disable` (GLM-5.3/5.3-Flash: `thinking.type:
    disabled` -> HTTP 400 code 1210) forces an EXPLICITLY disabling
    `--effort` to the profile's own default instead of turning reasoning
    off, UNLESS `effort_clamp_map` already has a more specific answer for
    this exact value (Databricks GLM's own `none -> low`, the CHEAPEST
    accepted level, not `reasoning_default_effort` ("high")) -- checked
    first, so the explicit table always wins. `effort is None` (no
    `--effort` given at all) is left alone -- that means "omit the field",
    which safely takes the server's own default, not "send a disabling
    value"."""
    explicit_clamp_covers_it = bool(
        effort and profile.effort_clamp_map and effort.lower() in profile.effort_clamp_map)
    if (effort and effort.lower() in _DISABLING_EFFORTS and profile.reasoning_no_disable
            and profile.reasoning_default_effort and not explicit_clamp_covers_it):
        effort = profile.reasoning_default_effort
    return clamp_effort(effort, profile)


def map_effort(effort: Optional[str], profile: ProviderProfile, *, has_tools: bool = False) -> dict:
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
    default, not "send a disabling value".

    1.0.1 hotfix 22: `has_tools` + `profile.reasoning_effort_with_tools`
    (gpt-6: verified live 400 -- "Function tools with reasoning_effort are
    not supported for gpt-6-sol... set reasoning_effort to 'none'") forces
    the EXPLICIT override value regardless of what `effort` would otherwise
    have been, INCLUDING None -- omitting the field is not enough on this
    family, since the endpoint's own default is not `none` either. A
    tool-less request on the same profile is unaffected -- checked and
    returned before the ordinary `not effort` early-out below, and only for
    the two chat-dialect field shapes (`reasoning.effort`/`reasoning_effort`)
    this concept applies to, never the Anthropic thinking-budget shape."""
    if has_tools and profile.reasoning_effort_with_tools and profile.thinking_format != "anthropic_thinking":
        override = profile.reasoning_effort_with_tools
        return {"reasoning": {"effort": override}} if profile.host_specific_fields else {"reasoning_effort": override}
    effort = resolve_effective_effort(effort, profile)  # 1.0.1 hotfix 19: never forward a value this route rejects
    if not effort or not profile.reasoning_effort_supported:
        return {}
    if profile.thinking_format == "anthropic_thinking":
        budget_by_effort = {"low": 4096, "medium": 10000, "high": 24000, "xhigh": 32000, "max": 32000}
        return {"thinking": {"type": "enabled", "budget_tokens": budget_by_effort.get(effort, 10000)}}
    if profile.host_specific_fields:
        return {"reasoning": {"effort": effort}}
    return {"reasoning_effort": effort}
