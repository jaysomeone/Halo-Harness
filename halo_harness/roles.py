"""halo_harness.roles -- Halo 2.0.2 (W7 round 1, brief A "roles v2"),
carrying forward V2c (H15): the `roles` table a built-in/custom sub-agent's
own role (a frontmatter `role:` key, an `Agent(role=...)` call-time
override, or a built-in's own fixed default) resolves a MODEL (and now an
EFFORT) from, `--role NAME=MODEL[:EFFORT]` CLI overrides, `team.json`'s own
`roles` map seeded into `~/.halo/config.json` (the exact idiom `team_config.
apply_gateway_preference` already uses for `gateway_preference`), a loaded
`~/.halo/roles/<name>.json` TEMPLATE (new this round), and `/roles`'s own
table rendering (model, effort, endpoint/path type, price per role). See
`docs/ROLES.md`.

Precedence a role-aware agent's model actually resolves through (full chain,
`config/agents_md.py::resolve_agent_model`): invocation `model=` > a CLI
`--role` override for THIS agent's own role > the agent file's own `model:`
> this module's role table (persisted config.json -- itself already layered
CLI-template-over-team.json-over-local by whoever builds the session, or the
cost-aware default below) for that role > `CLAUDE_CODE_SUBAGENT_MODEL`/
`settings.subagentModel` > `role_table["subagent_default"]` (new this round,
ONLY for a sub-agent with no role name at all) > the parent/session model.
`orchestrator` needs no entry at all to mean "the session model" -- that IS
what an absent/empty role-table lookup already falls through to.

A role table VALUE (config.json/team.json/template/`--role`) is either a
bare model-reference string, or `{"model": "...", "effort": "..."}` --
`_role_value_parts` below is the one place every caller unpacks either shape
into a plain `(model, effort)` pair; nothing else in this module (or
anywhere that reads a role table) should pattern-match the raw value itself.

Role NAMES are no longer a closed set: `ROLE_NAMES` below are the built-ins
(every one wired to a built-in agent, a system subsystem, or both -- see
`docs/ROLES.md`), but any OTHER syntactically-valid name (`[a-z][a-z0-9_]*`)
that a team.json or a loaded roles template actually defines a value for is
an equally valid role for `Agent(role=...)`, `--role`, and frontmatter
`role:` -- `known_role_names()` is the live "what's valid right now" set
every validation point checks a name against.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

# Halo 2.0.2 brief A.1: the screenshot's three additions (`planner`,
# `tester`, `compaction`) plus `judge` and `subagent_default` --
# `config/agents_md.py`'s own `_builtin_specs()` wires `Plan` to `planner`
# and adds `Judge`/`Tester`; `compaction` and `subagent_default` are
# consulted directly by `agent/loop.py` (see each one's own docstring
# there) rather than through a built-in AgentSpec -- neither is ever a
# sub-agent's own `role:`, so neither appears in `config/agents_md.py`.
ROLE_NAMES = ("orchestrator", "planner", "coder", "reviewer", "judge", "researcher",
              "tester", "compaction", "small", "subagent_default")

_ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# 2.0.2 review finding 13 (major): `roles.enabled` (roles_mode_enabled,
# below) and `roles.editor` (tui/slash.py's own $EDITOR-override switch)
# are reserved SETTINGS, but both live in the exact same `roles` config
# map a real role->model table does -- "editor" is syntactically a valid
# role name and `"external"` normalizes as a perfectly good bare model
# string, so `configured_role_table()` used to read `roles.editor:
# "external"` as a custom role named "editor" pointed at the model
# "external". That made `/roles` (and `halo roles`/`stats --roles`/
# completion) try to resolve a model named "external" and fail outright
# with InvalidModelError, and turned off the Databricks cost-aware
# defaults (a non-empty configured table always wins, see `resolve_role_
# table`) for every session, just because this ONE setting happened to
# be configured.
_RESERVED_ROLES_SETTING_KEYS = frozenset({"enabled", "editor"})


def is_role_name_syntax(name: str) -> bool:
    """Brief A.3: "any `[a-z][a-z0-9_]*`" -- the syntax check alone, with
    no opinion on whether `name` is actually DEFINED anywhere; see
    `known_role_names` for that."""
    return bool(name) and bool(_ROLE_NAME_RE.match(name))

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


def _normalize_role_value(value) -> "Optional[object]":
    """A raw role-table VALUE, cleaned to either a non-empty model string
    or a `{"model": ..., "effort": ...}` dict with a non-empty `model`
    (brief A.2) -- `None` for anything else (an empty string, a dict with
    no usable `model`, a list, ...). The `effort` key is dropped (not
    just left as-is) when it isn't a non-empty string, so a caller never
    has to re-check its type."""
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, dict):
        model = value.get("model")
        if not isinstance(model, str) or not model.strip():
            return None
        effort = value.get("effort")
        if isinstance(effort, str) and effort.strip():
            return {"model": model, "effort": effort.strip()}
        return {"model": model}
    return None


def role_value_parts(value) -> "tuple[Optional[str], Optional[str]]":
    """Unpacks ANY role-table value shape into a plain `(model, effort)`
    pair -- the ONE place every caller (CLI override, config.json/team.json
    table, template, `resolve_role_ref`, `agents_md.resolve_agent_model`,
    `/roles`, `stats --roles`) reads a role's model/effort apart, so
    nothing else needs to know there are two shapes at all."""
    normalized = _normalize_role_value(value)
    if normalized is None:
        return None, None
    if isinstance(normalized, dict):
        return normalized.get("model"), normalized.get("effort")
    return normalized, None


def configured_role_table() -> dict:
    """`~/.halo/config.json`'s own `roles` map, filtered to SYNTACTICALLY
    valid role names (brief A.3: built-in OR custom, `[a-z][a-z0-9_]*` --
    no longer restricted to `ROLE_NAMES`) holding a usable value (a
    non-empty model string, or a `{"model", "effort"}` dict -- brief
    A.2), with the reserved settings keys that happen to live in this
    SAME map (`_RESERVED_ROLES_SETTING_KEYS` -- finding 13) excluded no
    matter what they're set to. Never raises."""
    from halo_harness.theme import get_config_value
    raw = get_config_value("roles", default={})
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k, v in raw.items():
        if not is_role_name_syntax(k) or k in _RESERVED_ROLES_SETTING_KEYS:
            continue
        normalized = _normalize_role_value(v)
        if normalized is not None:
            out[k] = normalized
    return out


def known_role_names(*extra_tables: "Optional[dict]") -> "tuple[str, ...]":
    """`ROLE_NAMES` plus every syntactically-valid custom key found in
    `extra_tables` (role-table dicts -- CLI overrides, a session's own
    resolved table, a loaded template's `roles` map, ...) and in the
    persisted `configured_role_table()` -- the live "what's a valid role
    name right now" set (brief A.3) every validation point (CLI `--role`,
    `Agent(role=...)`, frontmatter `role:`) checks a name against, and
    every "unknown role" error message lists. Order: the built-ins first
    (their documented order), then every extra name first-seen, so the
    error message/completion list is stable rather than dict-order-
    dependent."""
    seen = list(ROLE_NAMES)
    seen_set = set(seen)
    for table in (list(extra_tables) + [configured_role_table()]):
        for name in (table or {}):
            if is_role_name_syntax(name) and name not in seen_set:
                seen.append(name)
                seen_set.add(name)
    return tuple(seen)


def roles_mode_enabled() -> bool:
    """Halo 2.0.2 round 7 (init wizard brief, "Modes"): `roles.enabled` in
    `~/.halo/config.json`, default True. False means "every role resolves
    to the session model" -- enforced by `resolve_role_table` returning
    `{}` unconditionally below, the SAME empty-table shape an agent with
    no role at all already falls through to, so turning this off needs no
    other code path to change at all."""
    from halo_harness.theme import get_config_value
    return bool(get_config_value("roles.enabled", True))


def resolve_role_table(*, provider: Optional[str] = None) -> dict:
    """The persisted role table a session resolves agents against --
    `configured_role_table()` verbatim whenever it holds ANYTHING at all;
    otherwise the cost-aware defaults, but only when `provider` (the
    session's own resolved model provider) is `"databricks"` -- see this
    module's own docstring/`COST_AWARE_DEFAULTS` comment. Any other
    provider with an empty configured table gets `{}` back.

    Round 7: `{}` outright when `roles.enabled` is False -- checked FIRST,
    ahead of even the cost-aware default, since "every role resolves to
    the session model" (the mode's own documented meaning) must win over
    everything else this function would otherwise apply."""
    if not roles_mode_enabled():
        return {}
    configured = configured_role_table()
    if configured:
        return configured
    if provider == "databricks":
        return dict(COST_AWARE_DEFAULTS)
    return {}


def _split_model_and_effort(rest: str) -> "tuple[str, Optional[str]]":
    """`MODEL[:EFFORT]` -> `(model, effort_or_None)` -- splits on the
    LAST `:` only when that tail is one of the harness's own accepted
    effort words (`providers.profiles.EFFORT_LEVELS`, the exact set
    `--effort` itself accepts). Safe against a model ref that legitimately
    contains colons (`or:vendor/model`, `dbx:endpoint`, `cc:fable[1m]`,
    ...) -- none of those ever end in a bare "low"/"medium"/"high"/
    "xhigh"/"max" segment, so there is no real ambiguity in practice."""
    from halo_harness.providers.profiles import EFFORT_LEVELS
    if ":" in rest:
        head, _, tail = rest.rpartition(":")
        if head and tail in EFFORT_LEVELS:
            return head, tail
    return rest, None


def parse_role_flag(raw: str) -> "tuple[str, object]":
    """`--role NAME=MODEL[:EFFORT]` -> `(name, value)`, `value` a bare
    model string or `{"model", "effort"}` (brief A.2) -- whichever shape
    `role_value_parts`/every other reader already expects. Raises
    ValueError (cli.py turns this into a clean exit-2 usage error, never a
    traceback) on a bad shape or a name `known_role_names()` doesn't
    recognize (brief A.3: "an unknown name errors with the list of known
    names")."""
    if not isinstance(raw, str) or "=" not in raw:
        raise ValueError(f"--role must be NAME=MODEL[:EFFORT], got {raw!r}")
    name, _, rest = raw.partition("=")
    name, rest = name.strip(), rest.strip()
    known = known_role_names()
    if name not in known:
        raise ValueError(f"--role: unknown role {name!r} (expected one of {', '.join(known)})")
    if not rest:
        raise ValueError(f"--role {name}=... needs a model reference")
    model, effort = _split_model_and_effort(rest)
    if not model:
        raise ValueError(f"--role {name}=... needs a model reference")
    return name, ({"model": model, "effort": effort} if effort else model)


def parse_role_flags(values: Optional[list]) -> dict:
    """`--role` may be repeated; a later repeat of the SAME role name wins
    (plain last-one-wins). `{}` for `None`/empty -- never raises for that."""
    out: dict = {}
    for raw in (values or []):
        name, value = parse_role_flag(raw)
        out[name] = value
    return out


def apply_role_preference(roles: dict) -> None:
    """`team.json`'s own `roles` map (or a loaded template's) seeded into
    `~/.halo/config.json` -- the SAME idiom `team_config.apply_gateway_
    preference` uses for `gateway_preference`: idempotent, never overwrites
    a role the user already configured locally (a personal config.json
    value always wins over the shared team/template default). brief A.3:
    any syntactically-valid name is accepted, not only `ROLE_NAMES` -- an
    actually-malformed name/value is silently skipped (team.json is
    shared/committed; a typo there should not clutter config.json with a
    key nothing reads, and must never crash `init`/`roles load`)."""
    from halo_harness.theme import get_config_value, set_config_value
    for name, value in (roles or {}).items():
        if not is_role_name_syntax(name):
            continue
        normalized = _normalize_role_value(value)
        if normalized is None:
            continue
        key = f"roles.{name}"
        if get_config_value(key, default=None) is None:
            set_config_value(key, normalized)


def resolve_role_ref(name: str, *, role_table: Optional[dict] = None, cli_overrides: Optional[dict] = None,
                      parent_ref, parent_profile, state_dir, routes: Optional[dict] = None):
    """`(ModelRef, ModelProfile, effort_requested, source)` for role
    `name` RIGHT NOW -- `source` is one of "CLI --role" / "role table" /
    "session model", for `/roles`'s own display. A CLI override wins over
    the (already cost-aware-defaulted where applicable) persisted table;
    neither present -> the session's own model/profile OBJECTS, unchanged
    (matches `resolve_agent_model`'s own "nothing resolved -> reuse parent
    objects" contract). `effort_requested` (brief A.2, new this round) is
    whichever of the two role values actually won carries as its own
    `effort` -- `None` when it didn't ask for one (the route's own
    default effort applies, same as an agent with no `effort:` override)."""
    from halo_harness.model import parse_model_ref, resolve_model_profile
    cli_overrides = cli_overrides or {}
    role_table = role_table or {}
    cli_raw = cli_overrides.get(name)
    table_raw = role_table.get(name)
    raw = cli_raw if cli_raw is not None else table_raw
    model, effort = role_value_parts(raw)
    if not model:
        return parent_ref, parent_profile, None, "session model"
    ref = parse_model_ref(model, routes)
    profile = resolve_model_profile(ref, state_dir, routes)
    return ref, profile, effort, ("CLI --role" if cli_raw is not None else "role table")


def role_effort_for(role_name: Optional[str], *, role_table: Optional[dict] = None,
                     cli_overrides: Optional[dict] = None) -> Optional[str]:
    """Just the EFFORT half of a role's resolved value (brief A.2),
    CLI-override-beats-role-table (same precedence as the model side) --
    used ALONGSIDE `config/agents_md.py::resolve_agent_model` (which
    stays model-only, a stable 2-tuple, so this round never ripples a
    return-shape change through every existing caller/test of that
    function) wherever a sub-agent's own effort needs to pick up a
    role's `{"model","effort"}` entry -- see `agent/subagent.py`'s own
    call site. `None` for no role name, or a role with no effort of its
    own (the route's/parent's own default effort then applies, exactly
    as before this existed)."""
    if not role_name:
        return None
    cli_overrides = cli_overrides or {}
    role_table = role_table or {}
    raw = cli_overrides.get(role_name)
    if raw is None:
        raw = role_table.get(role_name)
    _model, effort = role_value_parts(raw)
    return effort


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
    """One row per known role name -- `ROLE_NAMES` plus any custom name
    actually defined in `role_table`/`cli_overrides` (brief A.3) -- in
    `known_role_names`'s stable order. `/roles`'s own data source. Each
    row's `"effort"` is formatted like `providers/effort.py::format_
    requested_vs_sent`: the plain sent value, or `"X (sent as Y)"` when a
    role asked for a level its own route maps to a different one (brief
    A.2's "`requested (sent as X)` when they differ")."""
    from halo_harness.providers.effort import sent_effort
    from halo_harness.providers.profiles import resolve_profile
    from halo_harness.providers.routing import Route
    names = known_role_names(role_table, cli_overrides)
    rows = []
    for name in names:
        ref, profile, effort_requested, source = resolve_role_ref(
            name, role_table=role_table, cli_overrides=cli_overrides, parent_ref=parent_ref,
            parent_profile=parent_profile, state_dir=state_dir, routes=routes,
        )
        info = describe_role_ref(ref, profile, state_dir)
        # `profile` above is a `model.ModelProfile` (pricing/display) --
        # `sent_effort` needs the SEPARATE `providers.profiles.
        # ProviderProfile` (request-building) `effort_set`/`request.py`
        # use, resolved the same way `agent/loop.py`'s own compaction-
        # model-override does. Best-effort: an unresolvable route (a
        # malformed custom role, a provider this box can't reach right
        # now) just shows the requested effort as-is rather than raising
        # out of `/roles`.
        try:
            route = Route(provider=ref.provider, upstream_model=ref.model, dialect=ref.dialect)
            provider_profile = resolve_profile(route, state_dir=state_dir)
            effort_sent = sent_effort(effort_requested, provider_profile)
        except Exception:
            effort_sent = effort_requested
        if effort_requested and effort_requested != effort_sent:
            effort_display = f"{effort_requested} (sent as {effort_sent})"
        else:
            effort_display = effort_sent or "-"
        rows.append({"role": name, "model": ref.raw, "effort": effort_display, "source": source, **info})
    return rows


def format_roles_table(rows: "list[dict]") -> str:
    if not rows:
        return "No roles resolved."
    w_role = max(len(r["role"]) for r in rows)
    w_model = max(len(r["model"]) for r in rows)
    w_effort = max(len(r.get("effort", "-")) for r in rows)
    w_path = max(len(r["path_type"]) for r in rows)
    w_price = max(len(r["price"]) for r in rows)
    lines = ["Role table (model/effort/endpoint/path type/price per role):"]
    for r in rows:
        lines.append(f"  {r['role'].ljust(w_role)}  {r['model'].ljust(w_model)}  "
                      f"{r.get('effort', '-').ljust(w_effort)}  {r['path_type'].ljust(w_path)}  "
                      f"{r['price'].ljust(w_price)}  ({r['source']})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Brief A.5: role TEMPLATES -- `~/.halo/roles/<name>.json`
# `{"name", "description", "roles": {role: {"model", "effort"}}}`.
# ---------------------------------------------------------------------------

def role_templates_dir(state_dir=None):
    """`~/.halo/roles/` -- created on first write, never required to
    already exist for a read (`list_role_templates` on a fresh box just
    returns `[]`)."""
    from halo_harness.config.paths import bridge_home
    base = state_dir if state_dir is not None else bridge_home()
    from pathlib import Path
    return Path(base) / "roles"


_TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def is_valid_template_name(name: str) -> bool:
    """Deliberately more permissive than a ROLE name (brief A.5 gives no
    syntax of its own for a template's file-stem) -- just "a safe, single
    path segment," so `role_templates_dir() / f"{name}.json"` can never
    escape that directory or collide with a dotfile."""
    return bool(name) and bool(_TEMPLATE_NAME_RE.match(name)) and ".." not in name


def list_role_templates(state_dir=None) -> "list[str]":
    """Every `<name>.json` in `role_templates_dir()`, name only, sorted
    -- `[]` on a fresh box with no directory yet. Never raises."""
    try:
        return sorted(p.stem for p in role_templates_dir(state_dir).glob("*.json") if p.is_file())
    except OSError:
        return []


def validate_role_template(data) -> "list[str]":
    """Every problem with a template's SHAPE (brief A.5: `{"name",
    "description", "roles": {role: model_or_{"model","effort"}}}`) --
    `[]` means valid. Never raises; a caller (CLI/TUI) reports these as
    plain lines rather than a traceback."""
    if not isinstance(data, dict):
        return ["template must be a JSON object"]
    problems = []
    if "name" in data and not isinstance(data.get("name"), str):
        problems.append('"name" must be a string')
    if "description" in data and not isinstance(data.get("description"), str):
        problems.append('"description" must be a string')
    roles = data.get("roles", {})
    if not isinstance(roles, dict):
        return problems + ['"roles" must be an object of {role_name: model_or_{"model","effort"}}']
    for name, value in roles.items():
        if not is_role_name_syntax(name):
            problems.append(f"invalid role name {name!r} (expected [a-z][a-z0-9_]*)")
        elif _normalize_role_value(value) is None:
            problems.append(f'role {name!r}: value must be a model string or {{"model", "effort"}}')
    return problems


def load_role_template(name: str, state_dir=None) -> "Optional[dict]":
    """The parsed, VALIDATED contents of template `name` -- bad JSON, a
    missing file, or a shape `validate_role_template` rejects all return
    `None` (never raises). `{"name", "description", "roles"}`, `roles`
    values already normalized (`role_value_parts`-ready)."""
    import json
    path = role_templates_dir(state_dir) / f"{name}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if validate_role_template(raw):
        return None
    roles = {k: _normalize_role_value(v) for k, v in (raw.get("roles") or {}).items() if is_role_name_syntax(k)}
    return {"name": raw.get("name") or name, "description": raw.get("description") or "",
            "roles": {k: v for k, v in roles.items() if v is not None}}


def save_role_template(name: str, data: dict, state_dir=None) -> "tuple[bool, list[str]]":
    """Writes `~/.halo/roles/<name>.json` (brief A.5: `/roles save
    <name>`, `halo roles template save <name>`). `(True, [])` on success,
    `(False, problems)` on a bad `name` or a `data` shape `validate_role_
    template` rejects -- never raises, never writes a partial file."""
    import json
    if not is_valid_template_name(name):
        return False, [f"invalid template name {name!r}"]
    problems = validate_role_template(data)
    if problems:
        return False, problems
    d = role_templates_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    payload = {"name": data.get("name") or name, "description": data.get("description") or "",
               "roles": data.get("roles") or {}}
    (d / f"{name}.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return True, []


def delete_role_template(name: str, state_dir=None) -> bool:
    """Best-effort remove; True iff a file actually existed and was
    removed. Never raises."""
    try:
        (role_templates_dir(state_dir) / f"{name}.json").unlink()
        return True
    except OSError:
        return False


def apply_role_template(name: str, state_dir=None) -> "tuple[bool, list[str]]":
    """`/roles load <name>` / `halo roles template load <name>` (brief
    A.5): persists EVERY role the template defines straight into `~/.halo/
    config.json` -- unlike `apply_role_preference`'s idempotent team.json
    seed (which only ever fills a gap), loading a template is a
    deliberate, explicit user action and OVERWRITES whatever was there
    (brief's own precedence list: "loaded template > team.json > config
    roles" -- a template the user just asked to load outranks a stale
    local value exactly the way a fresh `--role`/`/role` override would).
    `(True, [])` on success; `(False, [reason])` for an unknown/invalid
    template name -- never raises."""
    from halo_harness.theme import set_config_value
    template = load_role_template(name, state_dir=state_dir)
    if template is None:
        return False, [f"no such role template: {name!r} (or it failed validation)"]
    for role_name, value in template["roles"].items():
        set_config_value(f"roles.{role_name}", value)
    return True, []


# ---------------------------------------------------------------------------
# Halo 2.0.2 round 7 (init wizard brief, item 2): three shipped PRESETS,
# written as ordinary templates (above) into `role_templates_dir()` the
# first time the roles setup screen runs, and never overwritten after
# that -- the exact "copy on first use, never overwrite" idiom `orgs.py::
# ensure_builtin_orgs` already uses for its own three built-ins. Unlike
# `_BUILTIN_ORGS` (a fixed literal), these three are COMPUTED from
# whatever is actually configured right now (there is no single "the
# strongest/cheapest model" without asking the live catalogs), so they
# are written once, at first-use time, not kept as a module constant.
# ---------------------------------------------------------------------------

BUILTIN_ROLE_PRESET_NAMES = ("balanced", "quality", "local-first")


def _configured_model_entries(state_dir=None) -> "list[dict]":
    """Every model entry across every CONFIGURED+enabled provider, in
    `init_providers.model_entries_for_provider`'s own row shape -- the one
    place `_cheapest_model_entry`/`_local_ol_model_entry` below both read
    from. Best-effort: any provider whose own catalog lookup raises (an
    unrefreshed cache, a provider with no catalog concept) just
    contributes nothing, never crashes the whole preset computation."""
    from halo_harness.init_providers import configured_providers, model_entries_for_provider
    out: "list[dict]" = []
    for provider in configured_providers():
        try:
            out.extend(model_entries_for_provider(provider, state_dir))
        except Exception:
            pass
    return out


def _cheapest_model_entry(state_dir=None) -> Optional[str]:
    """The lowest (strictly positive) `price_in_per_m` ref across every
    configured provider's catalog that also looks tool-capable -- `None`
    when nothing qualifies (a fresh/unrefreshed catalog, or every
    configured provider priced `None`), so the caller can fall back to
    the session's own default model instead.

    2.0.2 review finding 28: no price/tool-support floor at all used to
    mean a $0 `:free` model or a negative-priced router entry (OpenRouter's
    own `openrouter/auto` reported "-1") could win outright and become
    `researcher`/`small` (and `judge` in local-first) -- which then fails
    every call, since those roles need a real tool-calling endpoint.
    `price <= 0` is excluded (a real price is always > 0 per-million;
    `0`/negative means "not a real priced completion model" here, same as
    a router/free alias). `supported_parameters`, when the catalog
    reports it at all, must include "tools" -- `None` (most providers'
    own row shape never sets this) stays fail-open, never excluded, so
    this never gets stricter than before for a catalog with no such
    metadata."""
    best_ref, best_price = None, None
    for e in _configured_model_entries(state_dir):
        price = e.get("price_in_per_m")
        if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0:
            continue
        supported = e.get("supported_parameters")
        if isinstance(supported, list) and "tools" not in supported:
            continue
        if best_price is None or price < best_price:
            best_price, best_ref = price, e.get("ref")
    return best_ref


def _local_ol_model_entry(state_dir=None) -> Optional[str]:
    """A local Ollama (`ol:`) model, when one is configured -- forward-
    compatible with the 2.0.3 Ollama release (roadmap): `init` has no
    `ol:` provider to configure yet in 2.0.2, so this always returns
    `None` today and `compute_builtin_role_presets` below falls through to
    the cheapest configured model instead, exactly as the brief's own
    "else the cheapest configured model" names."""
    for e in _configured_model_entries(state_dir):
        ref = e.get("ref") or ""
        if ref.startswith("ol:"):
            return ref
    return None


def compute_builtin_role_presets(*, default_model: Optional[str] = None, state_dir=None) -> dict:
    """`{"balanced": {"description", "roles"}, "quality": {...}, "local-
    first": {...}}` (brief item 2's own three descriptions) -- `default_
    model` is "the strongest configured model" (brief's own wording for
    `quality`): this harness has no model-strength ranking of its own, so
    the session's own already-chosen default (whatever the provider/
    default-model wizard steps just resolved, or `config.json`'s current
    one) is the best available proxy, documented here rather than
    invented silently. A role only appears in a preset's own `roles` dict
    when a real value was actually found for it (no model configured at
    all yet -> an empty-but-valid template, never a crash)."""
    from halo_harness.theme import get_config_value
    if not default_model:
        current = get_config_value("model", default=None)
        default_model = current if isinstance(current, str) and current else None
    cheapest = _cheapest_model_entry(state_dir) or default_model
    local = _local_ol_model_entry(state_dir) or cheapest
    return {
        "balanced": {
            "description": "Cost-aware defaults: the session model for most roles; researcher and small "
                           "drop to the cheapest configured model.",
            "roles": ({"researcher": cheapest, "small": cheapest} if cheapest else {}),
        },
        "quality": {
            "description": "The session model everywhere; judge and reviewer pinned to the strongest "
                           "configured model.",
            "roles": ({"judge": default_model, "reviewer": default_model} if default_model else {}),
        },
        "local-first": {
            "description": "small, researcher and judge on a local ol: model when one is configured, "
                           "else the cheapest configured model.",
            "roles": ({"small": local, "researcher": local, "judge": local} if local else {}),
        },
    }


def ensure_builtin_role_presets(*, default_model: Optional[str] = None, state_dir=None) -> None:
    """Writes each of `BUILTIN_ROLE_PRESET_NAMES` as an ordinary template
    the first time it's missing -- never overwrites one that already
    exists (even an empty/stale one from an earlier run with a different
    provider configured), matching `orgs.ensure_builtin_orgs`'s own
    "never overwritten once present" rule."""
    presets = compute_builtin_role_presets(default_model=default_model, state_dir=state_dir)
    d = role_templates_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    for name, data in presets.items():
        if not (d / f"{name}.json").exists():
            save_role_template(name, data, state_dir=state_dir)
