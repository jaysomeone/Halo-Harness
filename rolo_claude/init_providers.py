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

# H15 Part A: the tabbed init view's own 5 tabs -- PROVIDERS plus TypeSafe,
# which has no model catalog/default-model concept of its own (it only
# ever stores TYPESAFE_API_KEY, "for a later feature") and so is kept OUT
# of PROVIDERS itself -- every model-related helper above/below (the
# cross-provider default-model pick, `configured_providers()`) must never
# see it as a provider with models to offer.
TAB_PROVIDERS = PROVIDERS + ("typesafe",)

TAB_LABEL = {
    "databricks": "Databricks", "openrouter": "OpenRouter", "anthropic": "Anthropic API (key)",
    "claude": "Claude Code subscription", "typesafe": "TypeSafe",
}

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
    from rolo_claude.providers.cc_models import alias_display_detail, profile_fields_for_cc_model
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
            # H15 addendum 2: "-> <resolved-id>" next to the alias, same
            # `detail` bracket a Databricks row's family/path already uses.
            "detail": alias_display_detail(alias),
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
    configured").

    H15 item 21: ALSO enabled -- a provider with credentials but not yet
    ENABLED (an old env file migrated as disabled, or a tab never
    finished) must not appear in the cross-provider default-model pick
    either, matching every other model-listing surface's own rule. A box
    with no `providers` block at all (pre-H15, or any test that never
    calls `ensure_providers_migrated`) is unaffected -- `is_enabled` fails
    open in that case, exactly like before this item existed."""
    from rolo_claude.providers.enablement import is_enabled
    return [p for p in PROVIDERS if provider_status(p) in ("configured", "logged in") and is_enabled(p)]


# ---------------------------------------------------------------------------
# H15 Part A: the tabbed init view's own per-tab state -- pure data, no
# Textual import, so it's independently testable (and reusable from the
# plain numbered-fallback path with no real terminal).
# ---------------------------------------------------------------------------

def tab_credential_state(provider: str, *, team_cfg: Optional[dict] = None) -> dict:
    """`{"configured", "source", "masked", "known_host", "fields"}` for one
    TAB_PROVIDERS entry:

    - `configured`: real credentials/a login already resolve.
    - `source`/`masked`: "picked up from <source>" + the masked value (A.2)
      -- both None when not configured.
    - `known_host`: Databricks only -- a host discovered (Claude Code's own
      settings env, `team_cfg`) with no token yet; pre-fills the host field
      instead of asking for it too.
    - `fields`: `[{"name", "label", "secret"}, ...]` -- the inline fields
      A.2 wants for whatever is still missing (empty once `configured`).

    `team_cfg` (1.0.1 part 2 fixpass finding 6): the tabs app's own loaded
    `team.json` (`team_config.load_team_config`'s first return value) --
    its `"host"` is the fallback once Claude Code's own settings env has
    nothing (same precedence `init_cli.py::_ensure_databricks_creds`
    already applies on the sequential path). `None` (the default, every
    pre-existing call site) means "no team config available here" -- never
    an error, just one fewer host-discovery source.

    Detection for `openrouter`/`anthropic`/`typesafe` now goes through
    `providers.config.listing_effective_env()` (finding 3) so a key living
    only in a settings.json `env` block shows as configured here too,
    matching what a real session would resolve."""
    from rolo_claude.providers.config import listing_effective_env, redact
    env = listing_effective_env()
    if provider == "databricks":
        from rolo_claude.providers.config import resolve_databricks, resolve_databricks_source
        dbx = resolve_databricks(env)
        if dbx is not None:
            return {"configured": True, "source": resolve_databricks_source(env) or "env file",
                    "masked": f"{dbx.host} / {redact(dbx.token)}", "known_host": None, "fields": []}
        from rolo_claude.init_cli import _known_databricks_host
        discovered_host = _known_databricks_host()
        team_host = (team_cfg or {}).get("host")
        host = discovered_host or team_host
        fields = []
        if not host:
            fields.append({"name": "host", "label": "DATABRICKS_HOST", "secret": False})
        fields.append({"name": "token", "label": "DATABRICKS_TOKEN", "secret": True})
        source = "Claude Code's settings" if discovered_host else ("the team config" if team_host else None)
        return {"configured": False, "source": source, "masked": host, "known_host": host, "fields": fields}
    if provider == "openrouter":
        from rolo_claude.providers.config import resolve_openrouter
        orc = resolve_openrouter(env)
        if orc is not None:
            return {"configured": True, "source": "env file", "masked": redact(orc.api_key),
                    "known_host": None, "fields": []}
        return {"configured": False, "source": None, "masked": None, "known_host": None,
                "fields": [{"name": "key", "label": "OPENROUTER_API_KEY", "secret": True}]}
    if provider == "anthropic":
        from rolo_claude.providers.config import resolve_anthropic
        ant = resolve_anthropic(env)
        if ant is not None:
            return {"configured": True, "source": "env file", "masked": redact(ant.api_key),
                    "known_host": None, "fields": []}
        return {"configured": False, "source": None, "masked": None, "known_host": None,
                "fields": [{"name": "key", "label": "ANTHROPIC_API_KEY", "secret": True}]}
    if provider == "claude":
        available = claude_login_available()
        return {"configured": available, "source": "claude.ai login" if available else None,
                "masked": "logged in" if available else None, "known_host": None, "fields": []}
    if provider == "typesafe":
        key = env.get("TYPESAFE_API_KEY")
        if key:
            return {"configured": True, "source": "env file", "masked": redact(key),
                    "known_host": None, "fields": []}
        return {"configured": False, "source": None, "masked": None, "known_host": None,
                "fields": [{"name": "key", "label": "TYPESAFE_API_KEY", "secret": True}]}
    return {"configured": False, "source": None, "masked": None, "known_host": None, "fields": []}


_TAB_KEY_ENV = {"openrouter": "OPENROUTER_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "typesafe": "TYPESAFE_API_KEY"}


def save_tab_credentials(provider: str, values: dict, *, team_cfg: Optional[dict] = None) -> "tuple[bool, str]":
    """Writes `values` (keyed by `tab_credential_state`'s own `fields`
    `name`s) to the SAME shared env file + this process's live env every
    other part of the harness reads, exactly like `init_cli.py`'s own
    `_ensure_*_creds` helpers -- kept Textual-free so the tabbed app, a
    future non-interactive path, and tests can all call it with no UI
    import needed. Returns `(ok, message)`: `ok=False` only when a
    required value is missing/blank; never writes partial credentials.

    1.0.1 part 2 fixpass finding 6: Databricks' own `host` falls back to
    `_known_databricks_host()` then `team_cfg["host"]` when `values` has no
    "host" key at all -- exactly the shape `tab_credential_state` renders
    once a host was ALREADY discovered (only a token field, see its own
    docstring): `_collect_values` then never had a "host" to collect in the
    first place, so demanding one here unconditionally refused EVERY
    token-only save outright ("both DATABRICKS_HOST and DATABRICKS_TOKEN
    are needed" even though the host was already known). A `team_cfg` with
    a `gateway_preference`/`roles` map is applied here too, same as the
    sequential `init` flow's own `_ensure_databricks_creds`."""
    import os
    from rolo_claude.init_cli import _env_file_path, _write_env_var
    if provider == "databricks":
        from rolo_claude.init_cli import _known_databricks_host
        host = (values.get("host") or "").strip() or _known_databricks_host() or (team_cfg or {}).get("host") or ""
        token = (values.get("token") or "").strip()
        if not host or not token:
            return False, "both DATABRICKS_HOST and DATABRICKS_TOKEN are needed"
        path = _env_file_path()
        _write_env_var(path, "DATABRICKS_HOST", host)
        _write_env_var(path, "DATABRICKS_TOKEN", token)
        os.environ["DATABRICKS_HOST"] = host
        os.environ["DATABRICKS_TOKEN"] = token
        if team_cfg and team_cfg.get("gateway_preference"):
            from rolo_claude.team_config import apply_gateway_preference
            apply_gateway_preference(team_cfg["gateway_preference"])
        if team_cfg and team_cfg.get("roles"):
            from rolo_claude.roles import apply_role_preference
            apply_role_preference(team_cfg["roles"])
        return True, f"wrote DATABRICKS_HOST/DATABRICKS_TOKEN to {path}"
    if provider in _TAB_KEY_ENV:
        key_env = _TAB_KEY_ENV[provider]
        value = (values.get("key") or "").strip()
        if not value:
            return False, f"{key_env} is needed"
        path = _env_file_path()
        _write_env_var(path, key_env, value)
        os.environ[key_env] = value
        return True, f"wrote {key_env} to {path}"
    if provider == "claude":
        if claude_login_available():
            return True, "claude.ai login confirmed"
        return False, "no claude.ai login found -- run `claude` once to log in first"
    return False, f"unknown provider: {provider}"


def refresh_tab_catalog(provider: str) -> "tuple[bool, str]":
    """H15 item A.4: "finishing a tab that has credentials and is
    reachable fetches and caches that provider's catalog right then" --
    best-effort (ok, note); a no-op (`True`, `""`) for a provider with no
    catalog concept of its own (claude, typesafe) or that isn't actually
    configured yet (nothing to fetch)."""
    from rolo_claude.config.paths import bridge_home
    state_dir = bridge_home()
    if provider == "openrouter":
        from rolo_claude.providers.config import resolve_openrouter
        from rolo_claude.providers.databricks import probe_openrouter_models, write_models_json
        orc = resolve_openrouter()
        if orc is None:
            return False, "OpenRouter is not configured"
        try:
            fetched = probe_openrouter_models(orc.base_url, orc.api_key)
            write_models_json(state_dir, fetched)
            return True, f"{len(fetched)} model(s) cached"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
    if provider == "databricks":
        from rolo_claude.providers.config import derive_workspace_root, resolve_databricks
        from rolo_claude.providers.databricks import refresh_dbx_catalog
        dbx = resolve_databricks()
        if dbx is None:
            return False, "Databricks is not configured"
        root = derive_workspace_root(dbx.host)
        ok, _diff, note = refresh_dbx_catalog(state_dir, root, dbx.token)
        return ok, (note if not ok else "catalog refreshed")
    return True, ""  # claude / anthropic / typesafe: no catalog of their own to cache here
