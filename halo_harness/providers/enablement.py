"""halo_harness.providers.enablement -- H15 item 21, REPLACED by the H15
part 2 addendum (rolo, live use on his personal Mac, same day): detected
credentials/a real claude.ai login now AUTO-enable a provider -- the
"never run init, the harness already finds the available keys and
subscription and uses those" case. The `"providers"` block in config.json
stores OVERRIDES only: an explicit `"enabled"` key (true OR false) always
wins over auto-detection; a provider the block doesn't mention at all (or
no block yet) falls through to `credentials_present` (the exact same
detection `/model`'s own hint text and `/providers`/`halo providers`
already compute).

Exact per-provider detection rule (`credentials_present` below):
OpenRouter/Anthropic API (key)/TypeSafe are auto-enabled once their key is
found (env file, shell env, settings env chain); Databricks once a host
AND token are found (same sources, plus `~/.databrickscfg`); Claude Code
subscription (`cc:`) ONLY when `claude auth status` reports `loggedIn` with
`authMethod` exactly `"claude.ai"` -- a `claude` driven by an API token or
a custom base URL (a work box's own settings-driven login) never auto-
enables it, loggedIn or not.

Stored at `~/.halo/config.json`'s own `"providers"` key:
`{"<name>": {"enabled": bool, ...non-secret settings}}` -- secrets stay in
the env file, exactly like every other provider credential this harness
has ever stored; this module never reads/writes a key or token. Migration
(`ensure_providers_migrated`) is now a permanent no-op: auto-detection
already covers what it used to write once, live, every time, so there is
nothing left for a one-time migration to do.
"""

from __future__ import annotations

from typing import Optional

# Order matters for display only (the `providers` table/`/providers`).
# Halo 2.0.3 round 4: "huggingface" added -- unlike "ollama" (round 2/3,
# surfaced only via its own `/ollama` panel and doctor section, never this
# generic table), the round 4 brief explicitly asks for `/providers`/
# `halo providers`/doctor's enabled-count line to show Hugging Face, so it
# joins this table while "ollama" still deliberately does not.
PROVIDER_NAMES = ("databricks", "openrouter", "anthropic", "claude_subscription", "typesafe", "huggingface")

LABELS = {
    "databricks": "Databricks",
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic API (key)",
    "claude_subscription": "Claude Code subscription",
    "typesafe": "TypeSafe",
    "huggingface": "Hugging Face",
}

# item 21.6: the `dbx:`/`or:`/`ant:`/`cc:` prefix table -- also in
# docs/MODELS.md. TypeSafe has no routed models yet ("stores a key only,
# for a later feature"), so it has no prefix of its own.
PREFIXES = {
    "databricks": "dbx:", "openrouter": "or:", "anthropic": "ant:",
    "claude_subscription": "cc:", "typesafe": None, "huggingface": "hf:",
}

# A caller naturally has `ModelRef.provider` ("cc"), `init_providers.py`'s
# own picker/tab key ("claude"), or a bare prefix word -- every function
# below runs the name through this first so none of them have to agree on
# one spelling.
_ALIASES = {
    "cc": "claude_subscription", "claude": "claude_subscription",
    "dbx": "databricks", "or": "openrouter", "ant": "anthropic",
    "hf": "huggingface",
}


def canonical(name: str) -> str:
    return _ALIASES.get(name, name)


def label_for(name: str) -> str:
    return LABELS.get(canonical(name), name)


def _providers_block(state_dir=None) -> Optional[dict]:
    from halo_harness.theme import get_config_value
    block = get_config_value("providers", default=None)
    return block if isinstance(block, dict) else None


def providers_block_exists(*, state_dir=None) -> bool:
    return _providers_block(state_dir) is not None


def is_enabled(name: str, *, state_dir=None, detected: Optional[bool] = None) -> bool:
    """An explicit `"enabled"` override in the `providers` block (true OR
    false) always wins; absent that -- no block, or a block that doesn't
    mention this name, or a row with no `"enabled"` key -- falls through to
    `credentials_present` (auto-detect from real credentials/login).

    `detected` (1.0.1 part 2 fixpass finding 1): a caller that already
    computed `credentials_present(name, ...)` itself (most importantly
    `/providers`'s own `provider_rows()`, which otherwise re-derived it up
    to 5-6 times per row -- for `claude_subscription` specifically, each an
    UNCACHED `claude auth status` spawn) passes the result straight through
    here instead of this function silently re-deriving it a second time."""
    name = canonical(name)
    block = _providers_block(state_dir)
    row = block.get(name) if isinstance(block, dict) else None
    if isinstance(row, dict) and "enabled" in row:
        return bool(row["enabled"])
    return credentials_present(name) if detected is None else detected


def enablement_status(name: str, *, state_dir=None, detected: Optional[bool] = None) -> str:
    """One of `"auto"` (detected, no override), `"enabled_by_user"` /
    `"disabled_by_user"` (an explicit override), or `"not_set_up"` (no
    override, no credentials either) -- `/providers`/`halo
    providers`'s own display tag, see `enablement_display` for the actual
    user-facing string. `detected`: see `is_enabled`'s own docstring."""
    name = canonical(name)
    block = _providers_block(state_dir)
    row = block.get(name) if isinstance(block, dict) else None
    if isinstance(row, dict) and "enabled" in row:
        return "enabled_by_user" if row["enabled"] else "disabled_by_user"
    is_detected = credentials_present(name) if detected is None else detected
    return "auto" if is_detected else "not_set_up"


def enablement_display(name: str, *, state_dir=None, detected: Optional[bool] = None) -> str:
    """`"auto (detected from <source>)"` / `"disabled by you"` / `"enabled
    by you"` / `"not set up"` -- the exact wording `/providers`/`halo
    providers` show per provider. `detected`: see `is_enabled`'s own
    docstring.

    2.0.1 gateway rule: a `claude_subscription` row that's `"not set up"`
    gets one extra clause, `"(claude is configured for a gateway)"`,
    whenever `cc_models.is_claude_gateway_driven()` says so -- the owner's
    own work-VM case, where `claude` is deliberately wired to Databricks
    and will NEVER report a real claude.ai login, so plain "not set up"
    (indistinguishable from "just never logged in") would be misleading."""
    name = canonical(name)
    status = enablement_status(name, state_dir=state_dir, detected=detected)
    if status == "auto":
        return f"auto (detected from {credentials_source(name, detected=detected) or '?'})"
    if status == "disabled_by_user":
        return "disabled by you"
    if status == "enabled_by_user":
        source = credentials_source(name, detected=detected)
        return f"enabled by you (detected from {source})" if source else "enabled by you"
    if name == "claude_subscription":
        from halo_harness.providers.cc_models import is_claude_gateway_driven
        if is_claude_gateway_driven():
            return "not set up (claude is configured for a gateway)"
    return "not set up"


def set_enabled(name: str, enabled: bool, *, extra: Optional[dict] = None) -> None:
    from halo_harness.theme import get_config_value, set_config_value
    name = canonical(name)
    block = get_config_value("providers", default=None)
    block = dict(block) if isinstance(block, dict) else {}
    row = dict(block.get(name) or {}) if isinstance(block.get(name), dict) else {}
    row["enabled"] = bool(enabled)
    if extra:
        row.update(extra)
    block[name] = row
    set_config_value("providers", block)


def enable(name: str, *, extra: Optional[dict] = None) -> None:
    set_enabled(name, True, extra=extra)


def disable(name: str) -> None:
    set_enabled(name, False)


def enable_if_was_explicitly_disabled(name: str) -> None:
    """1.0.1 part 2 fixpass finding 15: `init`'s own "a successful setup
    enables the provider" step (and the init tabs' matching "Save"/"Check
    login" step) must only ever WRITE a permanent `enabled: true` override
    when FLIPPING an existing EXPLICIT `enabled: false` -- auto-detection
    (`is_enabled` -> `credentials_present`) already covers the ordinary
    "just configured, no override yet" case live, every time (the exact
    reasoning `ensure_providers_migrated`'s own docstring already gives for
    never writing one proactively). Writing one here unconditionally meant
    a provider stayed "enabled by you" forever afterward, surviving even a
    LATER revocation (a claude.ai logout, a deleted key) that auto-
    detection alone would otherwise have reflected immediately. A no-op
    when there's no override at all, or an already-`true` one -- `enable()`
    itself (the explicit `halo providers enable <name>`/`/providers
    enable <name>` command) is UNCHANGED and still always writes one; this
    helper is only for the IMPLICIT "setup just succeeded" callers."""
    name = canonical(name)
    block = _providers_block()
    row = block.get(name) if isinstance(block, dict) else None
    if isinstance(row, dict) and row.get("enabled") is False:
        enable(name)


def is_provider_disabled_message(name: str, *, state_dir=None) -> Optional[str]:
    """1.0.1 part 2 fixpass criticals #2/#3: OVERRIDE-ONLY. `None` unless
    the `providers` block carries an EXPLICIT `"enabled": false` for `name`
    -- this is the parse-time gate `model._refuse_if_disabled` calls for
    every hand-typed `--model`/`/model` ref, and it must never spawn a
    subprocess or read a credential: a provider with no override at all
    (the overwhelming common case -- most boxes never run `providers
    disable`) ALWAYS resolves here, regardless of whether credentials are
    even present yet. This used to fall through to live auto-detection
    (`credentials_present`, an uncached `claude auth status` spawn for
    `claude_subscription`) and, for that one provider specifically, a
    `_preflight_cc()` call for an even more specific message -- both
    removed: "not configured"/"not logged in" are the TURN-time
    preflight's own job now (`agent.cc_runtime._preflight_cc`, the
    credential resolution in `headless.build_session`/`_resolve_creds`),
    which already produce the precise message once a turn actually runs,
    with no loss of detail -- just surfaced at the right time instead of
    speculatively, 1-3x per `/model` open, sub-agent spawn or `-p`
    startup, every one of them paying up to a 10s subprocess timeout for a
    question nobody asked yet.

    `/model`'s own dim hint (a DETECTED-but-disabled provider) is a
    SEPARATE, listing-time concern (`Controller.list_models()`'s own
    `_maybe_hint`, which still consults `credentials_present` -- that one
    runs off the UI thread and is exactly the kind of listing surface this
    fixpass's "effective env" fix targets, never the parse-time gate)."""
    name = canonical(name)
    block = _providers_block(state_dir)
    row = block.get(name) if isinstance(block, dict) else None
    if isinstance(row, dict) and "enabled" in row and not row["enabled"]:
        return (f"{label_for(name)} is not enabled -- run `halo providers enable {name}` "
                f"(or finish its tab in `halo init`) first")
    return None


def is_enabled_with_env(name: str, env: Optional[dict], *, state_dir=None) -> bool:
    """`is_enabled` with detection run against a caller-supplied env dict
    (a session's trust-filtered `Settings.effective_env`), so a key that
    lives only in a settings.json env block enables the provider for the
    background catalog and balance workers too. `env=None` keeps the
    default (bare-environment) detection."""
    detected = credentials_present(name, env=env) if env is not None else None
    return is_enabled(name, state_dir=state_dir, detected=detected)


def credentials_present(name: str, env: Optional[dict] = None) -> bool:
    """Detected credentials/login only -- never enablement itself.

    `env` (1.0.1 part 2 fixpass finding 3): the merged env to resolve
    against -- `None` (every pre-existing call site, unchanged) means bare
    `os.environ` (Databricks still additionally re-derives the settings
    chain internally in that case, same as `resolve_databricks` always
    has). A LISTING surface (`/model` groups, `/providers`, the init tabs,
    doctor) that already has a real `Settings.effective_env` (or this
    module's own `providers.config.listing_effective_env` approximation)
    passes it here so a credential living only in a settings.json `env`
    block is seen too, exactly like a real session would resolve it."""
    name = canonical(name)
    if name == "databricks":
        from halo_harness.providers.config import resolve_databricks
        return resolve_databricks(env) is not None
    if name == "openrouter":
        from halo_harness.providers.config import resolve_openrouter
        return resolve_openrouter(env) is not None
    if name == "anthropic":
        from halo_harness.providers.config import resolve_anthropic
        return resolve_anthropic(env) is not None
    if name == "claude_subscription":
        from halo_harness.init_providers import claude_login_available
        return claude_login_available()
    if name == "huggingface":
        # 2.0.3 round 4 brief item 3: "HF_TOKEN present OR at least one
        # endpoint configured" -- either source alone is enough (a user who
        # only ever uses a dedicated endpoint never needs HF_TOKEN at all).
        # Round 5: a manually-configured `huggingface.local_servers` entry
        # is a THIRD, equally-sufficient source -- auto-DETECTED servers are
        # deliberately NOT checked here (that needs a live network probe;
        # this function's whole contract, shared with every other provider
        # branch on this page, is config/env reads only, never network).
        from halo_harness.providers.config import resolve_huggingface
        if resolve_huggingface(env) is not None:
            return True
        from halo_harness.providers.huggingface import resolve_huggingface_endpoints, resolve_huggingface_local_servers
        if resolve_huggingface_endpoints():
            return True
        return bool(resolve_huggingface_local_servers())
    if name == "ollama":
        # Round 5: config-only, never a network probe (same contract as
        # every branch on this page) -- `ollama.hosts` carries at least one
        # EXPLICIT entry, or `OLLAMA_HOST` names a specific daemon.
        # Deliberately NOT added to PROVIDER_NAMES/LABELS/PREFIXES/
        # _ALIASES above -- keeping "ollama" out of the generic
        # `/providers`/`halo providers`/doctor's enabled-count table is a
        # decision rounds 3/4 already made on purpose (see PROVIDER_NAMES's
        # own comment) and round 5 does not revisit it. This branch exists
        # ONLY so `halo init`'s new Ollama tab (`init_providers.py`) can ask
        # `reachability.reachability_tag("ollama")` for a real probe
        # without that tab first joining the generic table.
        import os
        e = env if env is not None else os.environ
        if e.get("OLLAMA_HOST"):
            return True
        from halo_harness.theme import get_config_value
        return bool(get_config_value("ollama.hosts", default=None))
    if name == "typesafe":
        import os
        e = env if env is not None else os.environ
        value = e.get("TYPESAFE_API_KEY")
        if not value and e is os.environ:
            # Finding 20 (2.0.1): same settings-env-chain fallback
            # resolve_openrouter/resolve_anthropic now have -- a bare call
            # (no env=) also checks a trusted settings.json env block.
            from pathlib import Path

            from halo_harness.providers.config import load_settings_env_chain
            value = load_settings_env_chain(Path.cwd()).get("TYPESAFE_API_KEY")
        return bool(value)
    return False


def credentials_source(name: str, *, detected: Optional[bool] = None) -> Optional[str]:
    """A short "where this came from" string for the `providers` table --
    None when nothing is configured. Databricks reuses its own richer
    `resolve_databricks_source` (env file/shell env/settings chain/
    ~/.databrickscfg); every other provider is a single env var, so the
    source is just naming the mechanism. `detected`: see `is_enabled`'s own
    docstring (finding 1 -- avoids a second `credentials_present` call when
    the caller already has the answer)."""
    name = canonical(name)
    is_detected = credentials_present(name) if detected is None else detected
    if not is_detected:
        return None
    if name == "databricks":
        from halo_harness.providers.config import resolve_databricks_source
        return resolve_databricks_source() or "env"
    if name == "claude_subscription":
        return "claude.ai login"
    if name == "typesafe":
        return "env"
    return "env file / shell env"


def ensure_providers_migrated() -> Optional[str]:
    """H15 part 2 addendum: permanent no-op, kept only so every existing
    call site (`doctor`, `init`, the `providers`/`models` CLI) stays valid
    without a sweep to remove them. Auto-detection (`is_enabled` ->
    `credentials_present`) now computes live, every time, exactly what a
    one-time migration used to have to write once -- there is nothing left
    to migrate, and writing an `"enabled": true` row for a detected
    provider would wrongly turn it into a permanent override that then
    ignores a LATER change in detection (a revoked key, a logged-out
    `claude`). Always returns None (nothing to print)."""
    return None
