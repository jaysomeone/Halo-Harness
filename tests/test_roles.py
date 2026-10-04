"""tests.test_roles -- V2c (H15): `roles.py`'s own table resolution
(config.json/team.json + the cost-aware default) and `--role NAME=MODEL`
parsing. `resolve_role_ref`/`describe_role_ref`/`resolve_all_roles`/
`format_roles_table`/`/roles` rendering live in tests/test_roles_resolve.py,
and `stats --roles`'s per-role telemetry aggregation in
tests/test_stats_roles.py (both split out to keep each file under the
brief's own "<= 250 lines per Write" rule). Role resolution PRECEDENCE (CLI
> agent file > role table > session model) and the three new built-in
agents are covered in tests/test_agents_md.py; `Agent(role=...)` through a
real Session with the mock upstream is covered in tests/test_agent_tool.py;
team.json's own `roles` key roundtrip + `apply_role_preference` are covered
in tests/test_dbx_team_onboarding.py.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


def _fresh_state_dir(prefix: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    return d


def _clear_state_dir_env() -> None:
    os.environ.pop("BRIDGE_STATE_DIR", None)


# ---- configured_role_table / resolve_role_table ----------------------------

@test
def test_configured_role_table_empty_by_default(ctx: Ctx):
    from halo_harness.roles import configured_role_table
    _fresh_state_dir("roles-empty-")
    try:
        ctx.check("nothing configured -> {}", configured_role_table() == {})
    finally:
        _clear_state_dir_env()


@test
def test_configured_role_table_reads_config_json(ctx: Ctx):
    from halo_harness.roles import configured_role_table
    from halo_harness.theme import set_config_value
    _fresh_state_dir("roles-read-")
    try:
        set_config_value("roles.coder", "dbx:databricks-claude-opus-4-6")
        ctx.check("role read back", configured_role_table() == {"coder": "dbx:databricks-claude-opus-4-6"})
    finally:
        _clear_state_dir_env()


@test
def test_configured_role_table_filters_unknown_and_non_string(ctx: Ctx):
    from halo_harness.roles import configured_role_table
    from halo_harness.config.paths import bridge_home
    _fresh_state_dir("roles-filter-")
    try:
        cfg_path = bridge_home() / "config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps({"roles": {"coder": "or:vendor/x", "not-a-role": "y", "small": 5}}),
                             encoding="utf-8")
        table = configured_role_table()
        ctx.check(f"only the known, string-valued role survives, got {table}", table == {"coder": "or:vendor/x"})
    finally:
        _clear_state_dir_env()


@test
def test_configured_role_table_excludes_reserved_setting_keys(ctx: Ctx):
    """2.0.2 review finding 13 (major) pin: `roles.enabled`/`roles.editor`
    are reserved settings that live in the SAME `roles` config map a real
    role table does -- "editor" is syntactically a valid role name and
    `"external"` normalizes as a perfectly good bare model string, so
    this used to read `roles.editor: "external"` as a custom role
    "editor" pointed at the model "external", which then failed
    `/roles`/`known_role_names` outright with InvalidModelError the
    moment anything tried to actually resolve it."""
    from halo_harness.roles import configured_role_table, known_role_names
    from halo_harness.theme import set_config_value
    _fresh_state_dir("roles-reserved-")
    try:
        set_config_value("roles.editor", "external")
        set_config_value("roles.enabled", True)
        set_config_value("roles.coder", "or:vendor/x")
        table = configured_role_table()
        ctx.check(f"editor/enabled excluded, the real role survives, got {table}", table == {"coder": "or:vendor/x"})
        ctx.check(f"'editor' never becomes a known role name, got {known_role_names()}",
                  "editor" not in known_role_names())
    finally:
        _clear_state_dir_env()


@test
def test_resolve_role_table_empty_and_databricks_applies_cost_aware_default(ctx: Ctx):
    from halo_harness.roles import COST_AWARE_DEFAULTS, resolve_role_table
    _fresh_state_dir("roles-costaware-")
    try:
        table = resolve_role_table(provider="databricks")
        ctx.check(f"cost-aware default applied, got {table}", table == COST_AWARE_DEFAULTS)
    finally:
        _clear_state_dir_env()


@test
def test_resolve_role_table_empty_and_non_databricks_stays_empty(ctx: Ctx):
    from halo_harness.roles import resolve_role_table
    _fresh_state_dir("roles-nondbx-")
    try:
        ctx.check("openrouter provider -> {} (never automatic beyond the documented case)",
                  resolve_role_table(provider="openrouter") == {})
        ctx.check("no provider given -> {}", resolve_role_table(provider=None) == {})
    finally:
        _clear_state_dir_env()


@test
def test_resolve_role_table_any_configured_entry_skips_the_cost_aware_default_entirely(ctx: Ctx):
    from halo_harness.roles import resolve_role_table
    from halo_harness.theme import set_config_value
    _fresh_state_dir("roles-partial-")
    try:
        set_config_value("roles.coder", "dbx:databricks-claude-opus-4-6")
        table = resolve_role_table(provider="databricks")
        ctx.check(f"only what's configured -- no auto-fill for researcher/small, got {table}",
                  table == {"coder": "dbx:databricks-claude-opus-4-6"})
    finally:
        _clear_state_dir_env()


# ---- --role NAME=MODEL parsing ---------------------------------------------

@test
def test_parse_role_flag_valid(ctx: Ctx):
    from halo_harness.roles import parse_role_flag
    _fresh_state_dir("roles-parse-valid-")
    try:
        ctx.check("parses cleanly (no effort suffix -> bare model string)",
                  parse_role_flag("coder=dbx:databricks-claude-opus-4-6")
                  == ("coder", "dbx:databricks-claude-opus-4-6"))
    finally:
        _clear_state_dir_env()


@test
def test_parse_role_flag_with_effort_suffix(ctx: Ctx):
    """Halo 2.0.2 brief A.2: `--role NAME=MODEL[:EFFORT]`."""
    from halo_harness.roles import parse_role_flag
    _fresh_state_dir("roles-parse-effort-")
    try:
        name, value = parse_role_flag("coder=or:vendor/deepseek-v4.1-flash:high")
        ctx.check(f"name parsed, got {name!r}", name == "coder")
        ctx.check(f"model+effort dict, got {value!r}",
                  value == {"model": "or:vendor/deepseek-v4.1-flash", "effort": "high"})
        # A model ref that legitimately ends in a provider:model pair but
        # no recognized effort word must NOT be mis-split.
        name2, value2 = parse_role_flag("small=dbx:databricks-deepseek-v4-1-flash")
        ctx.check(f"a colon-bearing model with no real effort suffix stays a bare string, got {value2!r}",
                  value2 == "dbx:databricks-deepseek-v4-1-flash")
    finally:
        _clear_state_dir_env()


@test
def test_parse_role_flag_bad_shape_raises(ctx: Ctx):
    from halo_harness.roles import parse_role_flag
    try:
        parse_role_flag("no-equals-sign")
        ctx.check("should have raised", False)
    except ValueError as e:
        ctx.check(f"clear message, got {e}", "NAME=MODEL" in str(e))


@test
def test_parse_role_flag_unknown_role_raises(ctx: Ctx):
    from halo_harness.roles import parse_role_flag
    _fresh_state_dir("roles-parse-unknown-")
    try:
        parse_role_flag("not-a-role=or:vendor/x")
        ctx.check("should have raised", False)
    except ValueError as e:
        ctx.check(f"names the bad role, got {e}", "not-a-role" in str(e))
    finally:
        _clear_state_dir_env()


@test
def test_parse_role_flag_accepts_a_custom_name_once_configured(ctx: Ctx):
    """Halo 2.0.2 brief A.3: "any [a-z][a-z0-9_]* defined in a template or
    team.json is a valid role" -- proxied here via a plain config.json
    entry (what `apply_role_preference`/`apply_role_template` both
    ultimately write to), since that's the one place every source
    (team.json, a loaded template, a hand `halo config set`) converges."""
    from halo_harness.roles import parse_role_flag
    from halo_harness.theme import set_config_value
    _fresh_state_dir("roles-parse-custom-")
    try:
        set_config_value("roles.release_captain", "or:vendor/x")
        name, value = parse_role_flag("release_captain=or:vendor/y")
        ctx.check(f"a defined custom name is accepted, got {(name, value)!r}",
                  (name, value) == ("release_captain", "or:vendor/y"))
        try:
            parse_role_flag("still_unknown=or:vendor/z")
            ctx.check("an UNDEFINED custom name still raises", False)
        except ValueError as e:
            ctx.check(f"the error lists the known names including the custom one, got {e}",
                      "still_unknown" in str(e) and "release_captain" in str(e) and "coder" in str(e))
    finally:
        _clear_state_dir_env()


@test
def test_parse_role_flag_empty_model_raises(ctx: Ctx):
    from halo_harness.roles import parse_role_flag
    try:
        parse_role_flag("coder=")
        ctx.check("should have raised", False)
    except ValueError:
        pass


@test
def test_parse_role_flags_last_one_wins_and_none_is_empty(ctx: Ctx):
    from halo_harness.roles import parse_role_flags
    ctx.check("None -> {}", parse_role_flags(None) == {})
    ctx.check("[] -> {}", parse_role_flags([]) == {})
    out = parse_role_flags(["coder=or:vendor/first", "coder=or:vendor/second", "reviewer=or:vendor/r"])
    ctx.check(f"later repeat wins, got {out}", out == {"coder": "or:vendor/second", "reviewer": "or:vendor/r"})


# ---- per-role effort (brief A.2) -------------------------------------------

@test
def test_role_effort_for_cli_beats_table_same_as_model(ctx: Ctx):
    from halo_harness.roles import role_effort_for
    ctx.check("no role name -> None", role_effort_for(None, role_table={"coder": {"model": "m", "effort": "x"}})
              is None)
    ctx.check("a bare-string role value -> no effort",
              role_effort_for("coder", role_table={"coder": "or:vendor/m"}) is None)
    ctx.check("role table's own effort", role_effort_for(
        "coder", role_table={"coder": {"model": "or:vendor/m", "effort": "high"}}) == "high")
    ctx.check("CLI override's effort wins over the table's", role_effort_for(
        "coder", role_table={"coder": {"model": "or:vendor/m", "effort": "high"}},
        cli_overrides={"coder": {"model": "or:vendor/m2", "effort": "low"}}) == "low")
    ctx.check("a CLI override with no effort of its own suppresses the table's (same precedence slot as model)",
              role_effort_for("coder", role_table={"coder": {"model": "or:vendor/m", "effort": "high"}},
                               cli_overrides={"coder": "or:vendor/m2"}) is None)


# ---- role templates (~/.halo/roles/<name>.json) ----------------------------

@test
def test_role_template_save_load_round_trip(ctx: Ctx):
    from halo_harness.roles import list_role_templates, load_role_template, save_role_template
    d = _fresh_state_dir("roles-template-roundtrip-")
    try:
        ok, problems = save_role_template(
            "release-flow", {"description": "release flow roles",
                              "roles": {"coder": "or:vendor/coder", "judge": {"model": "or:vendor/j", "effort": "high"}}},
            state_dir=d)
        ctx.check(f"save succeeds, got {problems}", ok is True and problems == [])
        ctx.check("the saved file shows up in the listing", list_role_templates(state_dir=d) == ["release-flow"])
        loaded = load_role_template("release-flow", state_dir=d)
        ctx.check(f"round-tripped roles, got {loaded}", loaded == {
            "name": "release-flow", "description": "release flow roles",
            "roles": {"coder": "or:vendor/coder", "judge": {"model": "or:vendor/j", "effort": "high"}},
        })
    finally:
        _clear_state_dir_env()


@test
def test_role_template_rejects_a_bad_shape(ctx: Ctx):
    from halo_harness.roles import save_role_template
    d = _fresh_state_dir("roles-template-bad-")
    try:
        ok, problems = save_role_template("bad", {"roles": {"Not-Valid": "or:x"}}, state_dir=d)
        ctx.check(f"a syntactically-bad role name is rejected, got {problems}", ok is False and problems)
    finally:
        _clear_state_dir_env()


@test
def test_apply_role_template_overwrites_a_stale_local_value(ctx: Ctx):
    """brief A.5's own precedence note: "loaded template > ... > config
    roles" -- unlike the idempotent team.json seed, loading a template is
    a deliberate action and wins over whatever was already configured."""
    from halo_harness.roles import apply_role_template, configured_role_table, save_role_template
    from halo_harness.theme import set_config_value
    d = _fresh_state_dir("roles-template-apply-")
    try:
        set_config_value("roles.coder", "or:vendor/stale")
        save_role_template("fresh", {"roles": {"coder": "or:vendor/fresh"}}, state_dir=d)
        ok, problems = apply_role_template("fresh", state_dir=d)
        ctx.check(f"apply succeeds, got {problems}", ok is True)
        ctx.check(f"the stale local value was overwritten, got {configured_role_table()}",
                  configured_role_table()["coder"] == "or:vendor/fresh")
    finally:
        _clear_state_dir_env()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
