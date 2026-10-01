"""halo_harness.roles -- V2c (H15): the `roles` table (`orchestrator`,
`coder`, `reviewer`, `researcher`, `small`) a built-in/custom sub-agent's own
role (a frontmatter `role:` key, an `Agent(role=...)` call-time override, or
a built-in's own fixed default) resolves a MODEL from, `--role name=model`
CLI overrides, `team.json`'s own `roles` map seeded into `~/.halo/
config.json` (the exact idiom `team_config.apply_gateway_preference` already
uses for `gateway_preference`), and `/roles`'s own table rendering (model,
endpoint/path type, price per role). See `docs/ROLES.md`.

Precedence a role-aware agent's model actually resolves through (full chain,
`config/agents_md.py::resolve_agent_model`): invocation `model=` > a CLI
`--role` override for THIS agent's own role > the agent file's own `model:`
> this module's role table (persisted config.json/team.json, or the cost-
aware default below) for that role > `CLAUDE_CODE_SUBAGENT_MODEL`/
`settings.subagentModel` > the parent/session model. `orchestrator` needs no
entry at all to mean "the session model" -- that IS what an absent/empty
role-table lookup already falls through to.
"""

from __future__ import annotations

from typing import Optional

ROLE_NAMES = ("orchestrator", "coder", "reviewer", "researcher", "small")

# Cost-aware defaults (brief: "cheap model for exploration, strong model for
# planning and review"), DOCUMENTED here and in docs/ROLES.md -- applied
# ONLY by `resolve_role_table` below, and ONLY when BOTH (a) the persisted
# table (config.json's own "roles" key -- itself already seeded from a
# team.json at `init` time, see `apply_role_preference`) is completely
# empty, and (b) the session's own model is a Databricks one. Presets
# themselves are init-time-only and never persisted anywhere (see
# `init_cli.py`), so "the preset is work" is proxied the same way
# `model.py`/`providers.config` already treat "acting like work" elsewhere:
# by the session's OWN resolved provider. `orchestrator`/`coder`/`reviewer`
# are deliberately absent from this dict -- "the session model for the
# rest" is exactly what an absent role-table entry already falls through
# to, so nothing needs to be written for them. Never applied anywhere else
# in this module or the codebase -- "never automatic beyond that" (brief).
_COST_AWARE_DATABRICKS_MODEL = "dbx:databricks-deepseek-v4-1-flash"
COST_AWARE_DEFAULTS = {"researcher": _COST_AWARE_DATABRICKS_MODEL, "small": _COST_AWARE_DATABRICKS_MODEL}


def configured_role_table() -> dict:
    """`~/.halo/config.json`'s own `roles` map, filtered to known
    role names holding a non-empty string value. Never raises."""
    from halo_harness.theme import get_config_value
    raw = get_config_value("roles", default={})
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in ROLE_NAMES and isinstance(v, str) and v.strip()}


def resolve_role_table(*, provider: Optional[str] = None) -> dict:
    """The persisted role table a session resolves agents against --
    `configured_role_table()` verbatim whenever it holds ANYTHING at all;
    otherwise the cost-aware defaults, but only when `provider` (the
    session's own resolved model provider) is `"databricks"` -- see this
    module's own docstring/`COST_AWARE_DEFAULTS` comment. Any other
    provider with an empty configured table gets `{}` back."""
    configured = configured_role_table()
    if configured:
        return configured
    if provider == "databricks":
        return dict(COST_AWARE_DEFAULTS)
    return {}


def parse_role_flag(raw: str) -> "tuple[str, str]":
    """`--role name=model` -> `(name, model)`. Raises ValueError (cli.py
    turns this into a clean exit-2 usage error, never a traceback) on a bad
    shape or an unrecognized role name."""
    if not isinstance(raw, str) or "=" not in raw:
        raise ValueError(f"--role must be NAME=MODEL, got {raw!r}")
    name, _, model = raw.partition("=")
    name, model = name.strip(), model.strip()
    if name not in ROLE_NAMES:
        raise ValueError(f"--role: unknown role {name!r} (expected one of {', '.join(ROLE_NAMES)})")
    if not model:
        raise ValueError(f"--role {name}=... needs a model reference")
    return name, model


def parse_role_flags(values: Optional[list]) -> dict:
    """`--role` may be repeated; a later repeat of the SAME role name wins
    (plain last-one-wins). `{}` for `None`/empty -- never raises for that."""
    out: dict = {}
    for raw in (values or []):
        name, model = parse_role_flag(raw)
        out[name] = model
    return out


def apply_role_preference(roles: dict) -> None:
    """`team.json`'s own `roles` map seeded into `~/.halo/config.json`
    -- the SAME idiom `team_config.apply_gateway_preference` uses for
    `gateway_preference`: idempotent, never overwrites a role the user
    already configured locally (a personal config.json value always wins
    over the shared team default). An unrecognized role name is silently
    skipped (team.json is shared/committed; a typo there should not clutter
    config.json with a key nothing ever reads)."""
    from halo_harness.theme import get_config_value, set_config_value
    for name, model in (roles or {}).items():
        if name not in ROLE_NAMES or not isinstance(model, str) or not model.strip():
            continue
        key = f"roles.{name}"
        if get_config_value(key, default=None) is None:
            set_config_value(key, model)


def resolve_role_ref(name: str, *, role_table: Optional[dict] = None, cli_overrides: Optional[dict] = None,
                      parent_ref, parent_profile, state_dir, routes: Optional[dict] = None):
    """`(ModelRef, ModelProfile, source)` for role `name` RIGHT NOW --
    `source` is one of "CLI --role" / "role table" / "session model", for
    `/roles`'s own display. A CLI override wins over the (already cost-
    aware-defaulted where applicable) persisted table; neither present ->
    the session's own model/profile OBJECTS, unchanged (matches `resolve_
    agent_model`'s own "nothing resolved -> reuse parent objects" contract)."""
    from halo_harness.model import parse_model_ref, resolve_model_profile
    cli_overrides = cli_overrides or {}
    role_table = role_table or {}
    cli_raw = cli_overrides.get(name)
    table_raw = role_table.get(name)
    raw = cli_raw or table_raw
    if not raw:
        return parent_ref, parent_profile, "session model"
    ref = parse_model_ref(raw, routes)
    profile = resolve_model_profile(ref, state_dir, routes)
    return ref, profile, ("CLI --role" if cli_raw else "role table")


def _price_str(profile) -> str:
    price_in, price_out = getattr(profile, "price_in", None), getattr(profile, "price_out", None)
    if price_in is None or price_out is None:
        return "n/a"
    return f"${price_in * 1_000_000:.2f}/1M in, ${price_out * 1_000_000:.2f}/1M out"


def describe_role_ref(ref, profile, state_dir) -> dict:
    """`{"endpoint", "path_type", "price"}` for one resolved role ref --
    mirrors `Controller.list_models()`'s own per-Databricks-endpoint path-
    type/DBU lookup, and a plain per-million-token price for every other
    provider, so `/roles` never disagrees with `/model`."""
    if ref.provider == "databricks":
        from halo_harness.providers.databricks import load_dbx_endpoints_json
        from halo_harness.providers.dbx_routing import chat_route_candidates, format_dbu_cost, resolve_databricks_dialect
        try:
            _clean, dialect = resolve_databricks_dialect(ref.model, state_dir)
            if dialect == "anthropic-passthrough":
                path_type = "anthropic"
            else:
                cands = chat_route_candidates(ref.model, state_dir)
                path_type = cands[0].key if cands else "?"
        except Exception:
            path_type = "?"
        try:
            entries = load_dbx_endpoints_json(state_dir)
            e = entries.get(ref.model) if isinstance(entries.get(ref.model), dict) else {}
            usage_policy = e.get("usage_policy") if isinstance(e.get("usage_policy"), dict) else {}
            price = format_dbu_cost(usage_policy.get("output_dbu_per_1k_tokens"))
        except Exception:
            price = "?"
        return {"endpoint": ref.model, "path_type": path_type, "price": price}
    return {"endpoint": ref.model, "path_type": ref.provider, "price": _price_str(profile)}


def resolve_all_roles(*, role_table: Optional[dict] = None, cli_overrides: Optional[dict] = None,
                       parent_ref, parent_profile, state_dir, routes: Optional[dict] = None) -> "list[dict]":
    """One row per `ROLE_NAMES` entry -- `/roles`'s own data source."""
    rows = []
    for name in ROLE_NAMES:
        ref, profile, source = resolve_role_ref(
            name, role_table=role_table, cli_overrides=cli_overrides, parent_ref=parent_ref,
            parent_profile=parent_profile, state_dir=state_dir, routes=routes,
        )
        info = describe_role_ref(ref, profile, state_dir)
        rows.append({"role": name, "model": ref.raw, "source": source, **info})
    return rows


def format_roles_table(rows: "list[dict]") -> str:
    if not rows:
        return "No roles resolved."
    w_role = max(len(r["role"]) for r in rows)
    w_model = max(len(r["model"]) for r in rows)
    w_path = max(len(r["path_type"]) for r in rows)
    w_price = max(len(r["price"]) for r in rows)
    lines = ["Role table (endpoint/path type/price per role):"]
    for r in rows:
        lines.append(f"  {r['role'].ljust(w_role)}  {r['model'].ljust(w_model)}  "
                      f"{r['path_type'].ljust(w_path)}  {r['price'].ljust(w_price)}  ({r['source']})")
    return "\n".join(lines)
