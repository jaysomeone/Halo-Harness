"""rolo_claude.init_providers -- 1.0.1 hotfix 13: pure data/logic for
`rolo-claude init`'s provider-first flow ("select a provider to set up"
instead of a home/work/claude preset -- rolo: "I think work is actually
setting up databricks... the init should scroll thru all possible
providers and the user go thru that path of setup"). Kept separate from
`init_cli.py` (which owns the actual step sequencing/console output) so the
provider table/status/model-entry logic is independently testable and
reusable from the TUI's own picker module without a console-flow import.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# Order matters: this is the exact row order `init`'s provider picker shows.
PROVIDERS = ("databricks", "openrouter", "anthropic", "claude")

PROVIDER_LABEL = {
    "databricks": "Databricks -- workspace host + your personal token, discovers every served endpoint",
    "openrouter": "OpenRouter -- API key, full catalog",
    "anthropic": "Anthropic API -- API key, ant: models",
    "claude": "Claude subscription -- uses your claude.ai login through the installed claude, cc: models",
}

PROVIDER_DEFAULT_MODEL = {
    "databricks": "dbx:databricks-deepseek-v4-1-flash",
    "openrouter": "or:deepseek/deepseek-v4.1-flash",
    "anthropic": "ant:sonnet",
    "claude": "cc:sonnet",
}

# 1.0.1 hotfix 13 point 5: `--preset` stays accepted as a deprecated alias
# for `--provider` so documented commands like `init --preset work --yes`
# keep working verbatim.
PRESET_TO_PROVIDER = {"home": "openrouter", "work": "databricks", "claude": "claude"}


def claude_login_available() -> bool:
    from rolo_claude.providers.cc_models import SUBSCRIPTION_AUTH_METHODS, claude_auth_status
    status = claude_auth_status()
    return bool(status and status.logged_in and status.auth_method in SUBSCRIPTION_AUTH_METHODS)


def provider_status(name: str) -> str:
    """`"configured"` / `"logged in"` (claude only) / `"not set up"` -- a
    status TAG only, never a decision: the picker's cursor position comes
    from `detect_default_provider` below, completely separately."""
    from rolo_claude.providers.config import resolve_anthropic, resolve_databricks, resolve_openrouter
    if name == "databricks":
        return "configured" if resolve_databricks() is not None else "not set up"
    if name == "openrouter":
        return "configured" if resolve_openrouter() is not None else "not set up"
    if name == "anthropic":
        return "configured" if resolve_anthropic() is not None else "not set up"
    if name == "claude":
        return "logged in" if claude_login_available() else "not set up"
    return "not set up"


def detect_default_provider() -> str:
    """Where the picker's cursor STARTS -- same precedence the old preset
    auto-detection used (OpenRouter, then Databricks, then a claude.ai
    login), with `anthropic` slotted in right after Databricks; "detection
    only positions the cursor, it never decides" (hotfix 13 spec) -- the
    user can always move off it."""
    from rolo_claude.providers.config import resolve_anthropic, resolve_databricks, resolve_openrouter
    if resolve_openrouter() is not None:
        return "openrouter"
    if resolve_databricks() is not None:
        return "databricks"
    if resolve_anthropic() is not None:
        return "anthropic"
    if claude_login_available():
        return "claude"
    return "openrouter"


def _cc_ant_entries(prefix: str, target_table: dict) -> "list[dict]":
    from rolo_claude.providers.cc_models import profile_fields_for_cc_model
    out = []
    for alias, target in target_table.items():
        fields = profile_fields_for_cc_model(target) or {}
        price_in = fields.get("price_in")
        price_out = fields.get("price_out")
        out.append({
            "ref": f"{prefix}:{alias}", "context_tokens": fields.get("context_tokens"),
            "max_output_tokens": fields.get("max_output_tokens"),
            "price_in_per_m": price_in * 1_000_000 if isinstance(price_in, (int, float)) else None,
            "price_out_per_m": price_out * 1_000_000 if isinstance(price_out, (int, float)) else None,
        })
    return out


def _price_per_m(price_per_token) -> "float | None":
    """1.0.1 fixpass finding 7: models.json stores EVERY OpenRouter price as
    a STRING (e.g. "0.0000008") -- float(v) in a try/except, same as the
    pre-1.0.1 code, so a numeric string is never treated as unknown."""
    if isinstance(price_per_token, bool):
        return None
    try:
        return float(price_per_token) * 1_000_000
    except (TypeError, ValueError):
        return None


def _openrouter_entries(state_dir) -> "list[dict]":
    from rolo_claude.providers.databricks import load_models_json
    models = load_models_json(state_dir) or {}
    out = []
    for mid in sorted(models):
        entry = models[mid] or {}
        pricing = entry.get("pricing") or {}
        out.append({
            "ref": f"or:{mid}", "context_tokens": entry.get("context_length"),
            "max_output_tokens": entry.get("max_output_tokens"),
            "price_in_per_m": _price_per_m(pricing.get("prompt")),
            "price_out_per_m": _price_per_m(pricing.get("completion")),
        })
    return out


def model_entries_for_provider(provider: str, state_dir: Path) -> "list[dict]":
    """`Controller.list_models()`-shaped entries (see `model_display.
    format_model_row`) restricted to ONE provider -- what `init`'s own
    per-provider default-model pick (hotfix 5, extended by hotfix 13 to
    every provider, not just Databricks) shows. Chat-capable Databricks
    endpoints only (`dbx_routing.is_chat_task`); the full cached catalog
    for OpenRouter; the fixed nine subscription-model aliases for
    anthropic/claude."""
    if provider == "databricks":
        from rolo_claude.init_cli import _chat_capable_dbx_entries
        return _chat_capable_dbx_entries(state_dir)
    if provider == "openrouter":
        return _openrouter_entries(state_dir)
    if provider == "anthropic":
        from rolo_claude.providers.cc_models import ANT_ALIASES
        return _cc_ant_entries("ant", ANT_ALIASES)
    if provider == "claude":
        from rolo_claude.providers.cc_models import CC_ALIASES
        return _cc_ant_entries("cc", CC_ALIASES)
    return []


def configured_providers() -> "list[str]":
    """Every provider whose credentials/login ALREADY resolve, regardless
    of whether this specific `init` run was the one that set them up --
    used to decide whether the final cross-provider default-model pick is
    even needed (hotfix 13: "when more than one provider ends up
    configured")."""
    return [p for p in PROVIDERS if provider_status(p) in ("configured", "logged in")]
