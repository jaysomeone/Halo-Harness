"""halo_harness.init_providers -- 1.0.1 hotfix 13: pure data/logic for
`halo init`'s provider-first flow ("select a provider to set up"
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

# H15 Part A: the tabbed init view's own tabs -- PROVIDERS plus Ollama,
# Hugging Face (round 5) and TypeSafe. Deliberately NOT added to PROVIDERS
# itself: that tuple also drives the OLD sequential `--provider`/`--preset`
# CLI picker (`init_cli.py`'s own `_step_credentials`/`PROVIDER_LABEL`/
# `PROVIDER_DEFAULT_MODEL` -- none of which have a branch/sensible
# hardcoded default model for either, unlike every provider already in
# PROVIDERS) and the cross-provider "Default model" step's own
# `configured_providers()` (a few-second live probe for either would be
# out of place blocking that step's synchronous `body()`) -- same reason
# TypeSafe (no model catalog/default-model concept of its own) was
# already kept out. `halo init`'s INTERACTIVE Providers tab (and its own
# no-TTY/Textual-failure fallback, which is the OLD sequential picker
# above and so never offers these two either -- a documented, deliberate
# gap, not an oversight) is the only surface TAB_PROVIDERS drives;
# `halo setup`/`/setup` never reach a providers step at all (`setup_cli.
# py`'s own step list is `roles`/`orgs`/`summary` only), so neither is
# affected by this tuple either way.
TAB_PROVIDERS = PROVIDERS + ("ollama", "huggingface", "typesafe")

TAB_LABEL = {
    "databricks": "Databricks", "openrouter": "OpenRouter", "anthropic": "Anthropic API (key)",
    "claude": "Claude Code subscription", "ollama": "Ollama (local or LAN)", "huggingface": "Hugging Face",
    "typesafe": "TypeSafe",
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
    """2.0.1 launch-hang fix: reads ONLY `cc_models.cached_claude_auth_
    status()` now -- NEVER spawns `claude auth status` itself. A cache miss
    (nothing has primed it yet in this process) means "not detected yet",
    same as every other cache-only reader of this exact cache
    (`Controller.list_models()`'s own `cc_available`). The one legitimate
    LIVE spawn this function used to do unconditionally is now the startup
    worker's job (`tui/app.py::_prime_auth_status_worker`, headless -p's own
    background thread) or an explicit, caller-requested `refresh_cached_
    claude_auth_status()` right before a flow that genuinely needs a fresh
    answer right now (`halo init`'s tabs -- `tui/dialogs/init_tabs.py`'s
    `_claude_state_worker`/`_save_claude_worker`, already off the UI thread
    -- and `init_cli.py::cmd_init`'s own sequential/non-interactive path)."""
    from halo_harness.providers.cc_models import SUBSCRIPTION_AUTH_METHODS, cached_claude_auth_status
    status = cached_claude_auth_status()
    return bool(status and status.logged_in and status.auth_method in SUBSCRIPTION_AUTH_METHODS)


def provider_status(name: str) -> str:
    """`"configured"` / `"logged in"` (claude only) / `"not set up"` -- a
    status TAG only, never a decision: the picker's cursor position comes
    from `detect_default_provider` below, completely separately."""
    from halo_harness.providers.config import resolve_anthropic, resolve_databricks, resolve_openrouter
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
    from halo_harness.providers.config import resolve_anthropic, resolve_databricks, resolve_openrouter
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
    from halo_harness.providers.cc_models import alias_display_detail, profile_fields_for_cc_model
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
    from halo_harness.providers.databricks import load_models_json
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
            # 2.0.2 review finding 28: carried through so `roles.py::_cheapest_
            # model_entry` can skip a tool-less endpoint when the catalog
            # says so -- `None` (most other providers' own row shapes never
            # set this at all) means "unknown", never "no tools".
            "supported_parameters": entry.get("supported_parameters"),
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
        from halo_harness.init_cli import _chat_capable_dbx_entries
        return _chat_capable_dbx_entries(state_dir)
    if provider == "openrouter":
        return _openrouter_entries(state_dir)
    if provider == "anthropic":
        from halo_harness.providers.cc_models import ANT_ALIASES
        return _cc_ant_entries("ant", ANT_ALIASES)
    if provider == "claude":
        from halo_harness.providers.cc_models import CC_ALIASES
        return _cc_ant_entries("cc", CC_ALIASES)
    if provider == "huggingface":
        # Round 5 fix: this branch never existed before (`huggingface` was
        # never a `model_entries_for_provider` caller until now, since it
        # was never in PROVIDERS/TAB_PROVIDERS) -- `halo providers`/
        # `/providers`'s own "models" column (`providers_cli.py::_model_
        # count`, keyed by `enablement.PROVIDER_NAMES`, which HAS included
        # "huggingface" since round 4) always showed "-" for it
        # regardless of configuration; this is the router's own cached
        # catalog only (mirrors `_openrouter_entries` above) -- a local
        # server/dedicated endpoint has no persisted catalog of its own to
        # list here, same reason `refresh_tab_catalog` skips them too.
        from halo_harness.providers.huggingface_catalog import load_hf_models_json
        models = load_hf_models_json(state_dir) or {}
        out = []
        for mid in sorted(models):
            entry = models[mid] or {}
            pricing = entry.get("pricing") or {}
            out.append({
                "ref": f"hf:{mid}", "context_tokens": entry.get("context_length"),
                "price_in_per_m": _price_per_m(pricing.get("prompt")),
                "price_out_per_m": _price_per_m(pricing.get("completion")),
            })
        return out
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
    from halo_harness.providers.enablement import is_enabled
    return [p for p in PROVIDERS if provider_status(p) in ("configured", "logged in") and is_enabled(p)]


# ---------------------------------------------------------------------------
# H15 Part A: the tabbed init view's own per-tab state -- pure data, no
# Textual import, so it's independently testable (and reusable from the
# plain numbered-fallback path with no real terminal).
# ---------------------------------------------------------------------------

def tab_credential_state(provider: str, *, team_cfg: Optional[dict] = None,
                          cwd: Optional[Path] = None, settings_flag: Optional[str] = None) -> dict:
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
    matching what a real session would resolve.

    `cwd`/`settings_flag` (findings 22/23, 2.0.1): threaded straight into
    `listing_effective_env` -- both default to `None` (today's unchanged
    behavior, bare process `Path.cwd()`, no settings-flag override) since
    `halo init` has no `--cwd`/`--settings` flags of its own yet; present so
    a caller that DOES have an explicit cwd/settings-flag (a future `halo
    init --cwd`, or a direct test) never has to re-derive this function's
    own env resolution a second way."""
    from halo_harness.providers.config import listing_effective_env, redact
    env = listing_effective_env(cwd, settings_flag)
    if provider == "databricks":
        from halo_harness.providers.config import resolve_databricks, resolve_databricks_source
        dbx = resolve_databricks(env)
        if dbx is not None:
            return {"configured": True, "source": resolve_databricks_source(env) or "env file",
                    "masked": f"{dbx.host} / {redact(dbx.token)}", "known_host": None, "fields": []}
        from halo_harness.init_cli import _known_databricks_host
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
        from halo_harness.providers.config import resolve_openrouter
        orc = resolve_openrouter(env)
        if orc is not None:
            return {"configured": True, "source": "env file", "masked": redact(orc.api_key),
                    "known_host": None, "fields": []}
        return {"configured": False, "source": None, "masked": None, "known_host": None,
                "fields": [{"name": "key", "label": "OPENROUTER_API_KEY", "secret": True}]}
    if provider == "anthropic":
        from halo_harness.providers.config import resolve_anthropic
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
    if provider == "ollama":
        # Round 5: config-only (`ollama.hosts`, never a live probe here --
        # see this function's own "never computed here" contract).
        # `fields` stay visible EVEN ONCE configured -- unlike every
        # provider above, this tab is ADDITIVE ("add a LAN or cloud host"
        # on top of whatever's already there), never a single secret that
        # should disappear once set.
        from halo_harness.theme import get_config_value
        hosts_cfg = get_config_value("ollama.hosts", default=None)
        hosts_cfg = [h for h in hosts_cfg if isinstance(h, dict)] if isinstance(hosts_cfg, list) else []
        names = [h.get("name") or h.get("url") or "?" for h in hosts_cfg]
        fields = [
            {"name": "name", "label": "Host name (blank = default)", "secret": False},
            {"name": "url", "label": "Ollama URL (blank = local daemon, 127.0.0.1:11434)", "secret": False},
            {"name": "api_key", "label": "API key (optional, e.g. Ollama Cloud)", "secret": True},
        ]
        return {"configured": bool(hosts_cfg), "source": "config.json" if hosts_cfg else None,
                "masked": (f"{len(hosts_cfg)} host(s): " + ", ".join(names)) if hosts_cfg else None,
                "known_host": None, "fields": fields}
    if provider == "huggingface":
        # Round 5: THREE independent, equally-optional sources -- the
        # router token, a dedicated endpoint, a manual local server --
        # `fields` cover all three at once and stay visible even once one
        # is configured, same "additive tab" reasoning as Ollama above
        # (a user who already pasted HF_TOKEN can still come back and add
        # a local server entry too).
        from halo_harness.providers.config import resolve_huggingface
        from halo_harness.providers.huggingface import resolve_huggingface_endpoints, resolve_huggingface_local_servers
        parts = []
        hf = resolve_huggingface(env)
        if hf is not None:
            parts.append(f"HF_TOKEN set ({redact(hf.api_key)})")
        endpoints = resolve_huggingface_endpoints()
        if endpoints:
            parts.append(f"{len(endpoints)} endpoint(s)")
        servers = resolve_huggingface_local_servers()
        if servers:
            parts.append(f"{len(servers)} local server(s)")
        fields = [
            {"name": "token", "label": "HF_TOKEN (router)", "secret": True},
            {"name": "endpoint_name", "label": "Dedicated endpoint name (optional)", "secret": False},
            {"name": "endpoint_url", "label": "Dedicated endpoint URL (optional)", "secret": False},
            {"name": "endpoint_token", "label": "Dedicated endpoint token (optional)", "secret": True},
            {"name": "local_url", "label": "Local server URL (optional -- blank relies on auto-detection)",
             "secret": False},
            {"name": "local_key", "label": "Local server API key (optional)", "secret": True},
        ]
        return {"configured": bool(parts), "source": "env file / config.json" if parts else None,
                "masked": "; ".join(parts) if parts else None, "known_host": None, "fields": fields}
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
    from halo_harness.init_cli import _env_file_path, _write_env_var
    if provider == "databricks":
        from halo_harness.init_cli import _known_databricks_host
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
            from halo_harness.team_config import apply_gateway_preference
            apply_gateway_preference(team_cfg["gateway_preference"])
        if team_cfg and team_cfg.get("roles"):
            from halo_harness.roles import apply_role_preference
            apply_role_preference(team_cfg["roles"])
        return True, f"wrote DATABRICKS_HOST/DATABRICKS_TOKEN to {path}"
    if provider == "ollama":
        url = (values.get("url") or "").strip()
        name = (values.get("name") or "").strip() or "default"
        api_key = (values.get("api_key") or "").strip()
        from halo_harness.providers.ollama import DEFAULT_OLLAMA_URL, _normalize_host_url
        from halo_harness.theme import get_config_value, set_config_value
        normalized = _normalize_host_url(url) if url else DEFAULT_OLLAMA_URL
        hosts = get_config_value("ollama.hosts", default=None)
        hosts = [h for h in hosts if isinstance(h, dict)] if isinstance(hosts, list) else []
        entry = {"name": name, "url": normalized}
        if api_key:
            entry["api_key"] = api_key
        if not hosts:
            entry["default"] = True
        hosts = [h for h in hosts if h.get("name") != name] + [entry]
        set_config_value("ollama.hosts", hosts)
        return True, f"added Ollama host {name!r} ({normalized}) to ollama.hosts"
    if provider == "huggingface":
        from halo_harness.theme import get_config_value, set_config_value
        wrote = []
        token = (values.get("token") or "").strip()
        if token:
            path = _env_file_path()
            _write_env_var(path, "HF_TOKEN", token)
            os.environ["HF_TOKEN"] = token
            wrote.append("HF_TOKEN")
        ep_name = (values.get("endpoint_name") or "").strip()
        ep_url = (values.get("endpoint_url") or "").strip()
        if ep_name and ep_url:
            entries = get_config_value("huggingface.endpoints", default=None)
            entries = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
            entry = {"name": ep_name, "url": ep_url}
            ep_token = (values.get("endpoint_token") or "").strip()
            if ep_token:
                entry["token"] = ep_token
            entries = [e for e in entries if e.get("name") != ep_name] + [entry]
            set_config_value("huggingface.endpoints", entries)
            wrote.append(f"huggingface.endpoints[{ep_name}]")
        local_url = (values.get("local_url") or "").strip()
        if local_url:
            servers = get_config_value("huggingface.local_servers", default=None)
            servers = [s for s in servers if isinstance(s, dict)] if isinstance(servers, list) else []
            local_key = (values.get("local_key") or "").strip()
            entry = {"name": "default", "url": local_url}
            if local_key:
                entry["api_key"] = local_key
            if not servers:
                entry["default"] = True
            servers = [s for s in servers if s.get("name") != "default"] + [entry]
            set_config_value("huggingface.local_servers", servers)
            wrote.append("huggingface.local_servers[default]")
        if not wrote:
            return False, ("nothing entered -- paste HF_TOKEN, add a dedicated endpoint (name + URL), add a "
                           "local server (URL), or Skip to rely on auto-detection")
        return True, "wrote " + ", ".join(wrote)
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
    from halo_harness.config.paths import bridge_home
    state_dir = bridge_home()
    if provider == "openrouter":
        from halo_harness.providers.config import resolve_openrouter
        from halo_harness.providers.databricks import probe_openrouter_models, write_models_json
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
        from halo_harness.providers.config import derive_workspace_root, resolve_databricks
        from halo_harness.providers.databricks import refresh_dbx_catalog
        dbx = resolve_databricks()
        if dbx is None:
            return False, "Databricks is not configured"
        root = derive_workspace_root(dbx.host)
        ok, _diff, note = refresh_dbx_catalog(state_dir, root, dbx.token)
        return ok, (note if not ok else "catalog refreshed")
    if provider == "huggingface":
        from halo_harness.providers.config import resolve_huggingface
        hf = resolve_huggingface()
        if hf is None:
            return True, ""  # endpoint/local-only setup -- no router catalog to fetch
        from halo_harness.providers.huggingface_catalog import probe_huggingface_models, write_hf_models_json
        try:
            fetched = probe_huggingface_models(hf.base_url, hf.api_key)
            write_hf_models_json(state_dir, fetched)
            return True, f"{len(fetched)} model(s) cached"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
    return True, ""  # claude / anthropic / ollama / typesafe: no catalog of their own to cache here
