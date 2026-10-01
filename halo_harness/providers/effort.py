"""halo_harness.providers.effort -- Halo 2.0.1 (W2a, GLM-brief.md item 1 /
HALO-2.0.1-liveness-tips-brief.md Part C): the one place `/effort`,
`/status`, `/context`, the stream-json `system/init` line and (W2b) the TUI
effort card/status chip all read "what will this route actually do with
what I asked for" from.

Nothing here re-derives effort rules of its own -- `effort_set()` and
`sent_effort()` are thin, display-oriented wrappers around
`providers/profiles.py`'s existing `resolve_profile`/`clamp_effort`/
`resolve_effective_effort` (the SAME functions `providers/request.py` uses
to build a real wire body), so a display surface can never drift from what
a request actually sends.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from halo_harness.providers.profiles import (
    EFFORT_LEVELS, ProviderProfile, clamp_temperature, resolve_effective_effort,
)


@dataclass(frozen=True)
class EffortSet:
    """`effort_set()`'s return shape. `allowed` is this route's own
    accepted `reasoning_effort`/`output_config.effort` values
    (`profile.effort_values_supported`); `default` is what an
    omitted/unrecognized value resolves to; `clamp_map` is the EXPLICIT
    `{requested: sent}` table a route with a narrower-than-harness-
    vocabulary set supplies (`profile.effort_clamp_map`) -- `{}` for a
    route that needs no translation (e.g. OpenRouter GLM's seven Z.ai
    values, which already ARE the harness's accepted set there)."""
    allowed: tuple
    default: Optional[str]
    clamp_map: dict


def effort_set_for_profile(profile: ProviderProfile) -> EffortSet:
    """`effort_set()`'s logic, for a caller that already resolved (or
    faked, in a test) a `ProviderProfile` -- avoids a second
    `resolve_profile` call when one is already in hand (e.g. `Session`,
    which keeps `self.provider_profile` live across the whole session)."""
    allowed = tuple(profile.effort_values_supported or EFFORT_LEVELS)
    default = profile.reasoning_default_effort if profile.reasoning_default_effort in allowed else (
        allowed[0] if allowed else None)
    return EffortSet(allowed=allowed, default=default, clamp_map=dict(profile.effort_clamp_map or {}))


def effort_set(route, *, model_table: Optional[dict] = None, state_dir=None) -> EffortSet:
    """Resolve `route`'s own effort vocabulary via `providers.profiles.
    resolve_profile` -- the SAME profile request building uses, so this
    (and therefore `/effort`, and W2b's picker built on it) never drifts
    from what a real request will do."""
    from halo_harness.providers.profiles import resolve_profile
    profile = resolve_profile(route, model_table=model_table, state_dir=state_dir)
    return effort_set_for_profile(profile)


def sent_effort(requested: Optional[str], profile: ProviderProfile, *, has_tools: bool = False) -> Optional[str]:
    """The value a request for `requested` on `profile` actually puts on
    the wire -- mirrors `providers.profiles.map_effort`'s own decision
    (the `reasoning_effort_with_tools` override first, else
    `resolve_effective_effort`) without building a request body itself;
    DISPLAY only, never used to build a real request (request.py's
    `map_effort` stays the one place that does that)."""
    if has_tools and profile.reasoning_effort_with_tools and profile.thinking_format != "anthropic_thinking":
        return profile.reasoning_effort_with_tools
    return resolve_effective_effort(requested, profile)


def requested_vs_sent(
    profile: ProviderProfile, *, effort_requested: Optional[str] = None, effort_sent: Optional[str] = None,
    has_tools: bool = False, context_tokens: Optional[int] = None, prompt_estimate: int = 0,
    requested_max_tokens: Optional[int] = None,
) -> "dict[str, dict]":
    """Every request parameter THIS route's profile changed from what was
    asked -- `{"effort": {"requested": X, "sent": Y}, "temperature": {...},
    "max_tokens": {...}}` -- a key is present ONLY when requested != sent,
    so an unaffected route reports `{}` (nothing to show). `/status`/
    `/context` (headless text, this worker's own job) and W2b's TUI card/
    status chip all read this same dict rather than recomputing the
    comparison themselves.

    `effort_sent`, when the caller already knows it (e.g. `Session.effort`,
    already clamped at session-construction time), is used as-is instead of
    recomputed via `sent_effort` -- the two are the identical value for
    every route `resolve_effective_effort`/`sent_effort` would also compute,
    but the caller's own live value is authoritative when given. When
    `effort_requested` is also None (nothing was ever explicitly asked
    for), it's reported equal to `effort_sent` so no spurious "change"
    appears for an engine-applied default (e.g. the Anthropic-family "high"
    default -- see `Session.__init__`)."""
    changes: dict = {}

    resolved_sent = effort_sent if effort_sent is not None else sent_effort(
        effort_requested, profile, has_tools=has_tools)
    resolved_requested = effort_requested if effort_requested is not None else resolved_sent
    if resolved_requested is not None and resolved_sent is not None and resolved_requested != resolved_sent:
        changes["effort"] = {"requested": resolved_requested, "sent": resolved_sent}

    if profile.use_temperature and profile.temperature is not None:
        clamped_temp = clamp_temperature(profile.temperature, profile)
        if clamped_temp != profile.temperature:
            changes["temperature"] = {"requested": profile.temperature, "sent": clamped_temp}

    if context_tokens is not None:
        from halo_harness.providers.request import budget_max_tokens
        default_requested = requested_max_tokens or profile.max_tokens_default or profile.max_tokens_cap or 16384
        sent_tokens = budget_max_tokens(
            profile=profile, context_tokens=context_tokens, prompt_estimate=prompt_estimate,
            requested=requested_max_tokens,
        )
        if sent_tokens != default_requested:
            changes["max_tokens"] = {"requested": default_requested, "sent": sent_tokens}

    return changes


def format_requested_vs_sent(changes: dict) -> "list[str]":
    """One `"<param>: requested X, sent Y"` line per changed parameter
    (Part C: "`requested X, sent Y`"), in a stable order -- `/status` and
    `/context`'s own shared formatting, so neither surface invents its own
    wording."""
    order = ("effort", "temperature", "max_tokens")
    lines = []
    for key in order:
        if key in changes:
            c = changes[key]
            lines.append(f"  {key}: requested {c['requested']}, sent {c['sent']}")
    return lines
