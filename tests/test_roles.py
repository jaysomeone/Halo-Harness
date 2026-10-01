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
    ctx.check("parses cleanly", parse_role_flag("coder=dbx:databricks-claude-opus-4-6")
              == ("coder", "dbx:databricks-claude-opus-4-6"))


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
    try:
        parse_role_flag("not-a-role=or:vendor/x")
        ctx.check("should have raised", False)
    except ValueError as e:
        ctx.check(f"names the bad role, got {e}", "not-a-role" in str(e))


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


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
