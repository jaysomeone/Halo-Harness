"""tests.test_init_wizard_round7 -- Halo 2.0.2 round 7 (init wizard brief):
pure-logic pieces that don't need a Textual pilot -- `roles.py`/`orgs.py`'s
own mode switches (`roles_mode_enabled`/`orgs_mode_enabled`), the three
shipped role presets (`compute_builtin_role_presets`/`ensure_builtin_role_
presets`), the org default (`default_org_name`/`set_default_org`), `/org
run`'s "no name" argument split (`parse_run_args`), `validate_org`'s new
`budget_usd` checks, and `orgs.OrgBudgetTracker`'s own threshold math
(brief 3b). The Textual pilot tests (the wizard walking its steps, the
roles/org editors' template pickers, `/setup`) live in test_tui.py, per
house convention; the run_org_call/Agent-tool integration pieces (goal
task creation, the budget tracker actually wired into a real dispatch)
live in tests/test_init_wizard_round7_integration.py, split out to stay
under the per-file size this package writes in.
"""
from __future__ import annotations

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


# ---- roles_mode_enabled / resolve_role_table gating ------------------------

@test
def test_roles_mode_enabled_defaults_true_and_disables_the_table(ctx: Ctx):
    from halo_harness.roles import resolve_role_table, roles_mode_enabled
    from halo_harness.theme import set_config_value
    _fresh_state_dir("roles-mode-")
    try:
        ctx.check("defaults on", roles_mode_enabled() is True)
        set_config_value("roles.coder", "or:vendor/pinned")
        ctx.check("configured table resolves while enabled",
                  resolve_role_table().get("coder") == "or:vendor/pinned")
        set_config_value("roles.enabled", False)
        ctx.check("now reports disabled", roles_mode_enabled() is False)
        ctx.check(f"resolve_role_table is {{}} regardless of the configured table, "
                   f"got {resolve_role_table()}", resolve_role_table() == {})
        ctx.check("even the cost-aware databricks default is suppressed",
                  resolve_role_table(provider="databricks") == {})
    finally:
        _clear_state_dir_env()


# ---- orgs_mode_enabled ------------------------------------------------------

@test
def test_orgs_mode_enabled_defaults_false(ctx: Ctx):
    from halo_harness.orgs import orgs_mode_enabled
    from halo_harness.theme import set_config_value
    _fresh_state_dir("orgs-mode-")
    try:
        ctx.check("defaults off", orgs_mode_enabled() is False)
        set_config_value("orgs.enabled", True)
        ctx.check("flips on when configured", orgs_mode_enabled() is True)
    finally:
        _clear_state_dir_env()


# ---- the three shipped role presets ----------------------------------------

@test
def test_compute_builtin_role_presets_shapes(ctx: Ctx):
    from halo_harness.roles import BUILTIN_ROLE_PRESET_NAMES, compute_builtin_role_presets
    state_dir = Path(tempfile.mkdtemp(prefix="roles-presets-"))
    presets = compute_builtin_role_presets(default_model="or:vendor/strong", state_dir=state_dir)
    ctx.check(f"all three names present, got {sorted(presets)}",
              set(presets) == set(BUILTIN_ROLE_PRESET_NAMES))
    ctx.check(f"quality pins judge/reviewer to the given default model, got {presets['quality']['roles']}",
              presets["quality"]["roles"].get("judge") == "or:vendor/strong"
              and presets["quality"]["roles"].get("reviewer") == "or:vendor/strong")
    # No configured provider at all in this bare state_dir -- "balanced"/
    # "local-first" fall back to the given default model too (never crash
    # on an empty catalog).
    ctx.check(f"balanced falls back to the default model with no catalog, got {presets['balanced']['roles']}",
              presets["balanced"]["roles"].get("researcher") == "or:vendor/strong")
    ctx.check("every preset carries a human description",
              all(presets[n]["description"] for n in BUILTIN_ROLE_PRESET_NAMES))


@test
def test_ensure_builtin_role_presets_never_overwrites(ctx: Ctx):
    from halo_harness.roles import ensure_builtin_role_presets, load_role_template, save_role_template
    state_dir = Path(tempfile.mkdtemp(prefix="roles-presets-noclobber-"))
    ensure_builtin_role_presets(default_model="or:vendor/x", state_dir=state_dir)
    first = load_role_template("balanced", state_dir=state_dir)
    ctx.check("balanced was written on first use", first is not None)
    save_role_template("balanced", {"description": "edited by the user", "roles": {}}, state_dir=state_dir)
    ensure_builtin_role_presets(default_model="or:vendor/y", state_dir=state_dir)  # a second "first use"
    reread = load_role_template("balanced", state_dir=state_dir)
    ctx.check("the user's edit survived a second ensure call", reread["description"] == "edited by the user")


# ---- default_org_name / set_default_org ------------------------------------

@test
def test_default_org_round_trip_and_unknown_name_refused(ctx: Ctx):
    from halo_harness.orgs import default_org_name, ensure_builtin_orgs, set_default_org
    state_dir = Path(tempfile.mkdtemp(prefix="orgs-default-"))
    ensure_builtin_orgs(state_dir=state_dir)
    ctx.check("unset by default", default_org_name(state_dir=state_dir) is None)
    ok, problems = set_default_org("solo", state_dir=state_dir)
    ctx.check(f"solo is a real org, got {problems}", ok)
    ctx.check("default_org_name now reads it back", default_org_name(state_dir=state_dir) == "solo")
    ok2, problems2 = set_default_org("not-a-real-org", state_dir=state_dir)
    ctx.check(f"an unknown name is refused, got {problems2}", not ok2 and problems2)


@test
def test_default_org_name_ignores_a_stale_value(ctx: Ctx):
    """A default pointing at an org that no longer validates (deleted,
    or hand-edited into an invalid shape) must never be treated as real."""
    from halo_harness.orgs import default_org_name, orgs_dir, set_default_org
    state_dir = Path(tempfile.mkdtemp(prefix="orgs-default-stale-"))
    from halo_harness.orgs import save_org
    save_org("temp", {"name": "temp", "positions": [{"title": "X", "reports": []}]}, state_dir=state_dir)
    set_default_org("temp", state_dir=state_dir)
    (orgs_dir(state_dir) / "temp.json").unlink()
    ctx.check("a deleted default org resolves to None, not a dangling name",
              default_org_name(state_dir=state_dir) is None)


# ---- parse_run_args: "/org run [<name>] \"<goal>\"" ------------------------

@test
def test_parse_run_args_quoted_goal_means_no_name(ctx: Ctx):
    from halo_harness.orgs import parse_run_args
    ctx.check("quoted-only -> (None, goal)", parse_run_args('"fix the thing"') == (None, "fix the thing"))
    ctx.check("single-quoted too", parse_run_args("'fix the thing'") == (None, "fix the thing"))
    ctx.check("name then goal -> (name, goal)",
              parse_run_args('release-flow "fix the thing"') == ("release-flow", '"fix the thing"'))
    ctx.check("empty input -> (None, '')", parse_run_args("") == (None, ""))
    ctx.check("bare name no goal -> goal is empty", parse_run_args("release-flow") == ("release-flow", ""))


# ---- validate_org: budget_usd -----------------------------------------------

@test
def test_validate_org_rejects_negative_budgets(ctx: Ctx):
    from halo_harness.orgs import validate_org
    org = {"name": "b", "budget_usd": -1, "positions": [
        {"title": "A", "budget_usd": -5, "reports": []}]}
    problems = validate_org(org)
    ctx.check(f"org-level negative budget flagged, got {problems}",
              any('"budget_usd"' in p for p in problems))
    ctx.check(f"position-level negative budget flagged, got {problems}",
              any("A" in p and "budget_usd" in p for p in problems))
    ok_org = {"name": "ok", "budget_usd": 5.0, "positions": [{"title": "A", "budget_usd": 1, "reports": []}]}
    ctx.check(f"a valid non-negative budget passes clean, got {validate_org(ok_org)}", validate_org(ok_org) == [])


# ---- OrgBudgetTracker: warning at 80%, hard stop at 100% -------------------

@test
def test_budget_tracker_org_level_warns_then_stops(ctx: Ctx):
    from halo_harness.orgs import OrgBudgetTracker
    tracker = OrgBudgetTracker(org_name="demo", org_budget_usd=1.0, baseline_usd=0.0)
    ctx.check("no refusal before any spend", tracker.refusal_before_spawn("A", 0.0) is None)
    note = tracker.record_spend("A", 0.85, 0.85)  # 85% of $1.00
    ctx.check(f"warns at >=80%% but no stop yet, got {note!r}", note is not None and "80" not in note
              and "has used" in note and not tracker.org_stopped)
    refusal = tracker.refusal_before_spawn("B", 0.85)
    ctx.check("still allowed below 100%", refusal is None)
    note2 = tracker.record_spend("B", 0.20, 1.05)  # now over $1.00
    ctx.check(f"reaching the org budget is reported, got {note2!r}", note2 is not None and "reached" in note2)
    ctx.check("org_stopped flips True", tracker.org_stopped is True)
    refusal2 = tracker.refusal_before_spawn("C", 1.05)
    ctx.check(f"a further spawn is refused, got {refusal2!r}", refusal2 is not None and "budget" in refusal2)


@test
def test_budget_tracker_org_level_trips_despite_a_baseline_from_a_different_meter(ctx: Ctx):
    """2.0.2 review finding 5 (major) pin: every REAL caller passes the
    SPAWNING POSITION's own fresh CostMeter total (a brand new Session
    per position, starting at 0), never the root session's meter the
    baseline was captured from -- the review's own repro: budget $1.00,
    baseline $3.00 (the root session had already spent $3 before this
    org run started), then a position's own fresh total reaches $2.50.
    Before the fix, `max(0, 2.50 - 3.00)` stayed 0 forever and the org
    budget could never trip no matter how much was actually spent."""
    from halo_harness.orgs import OrgBudgetTracker
    tracker = OrgBudgetTracker(org_name="demo", org_budget_usd=1.0, baseline_usd=3.0)
    ctx.check("no refusal before any spend", tracker.refusal_before_spawn("A", 2.50) is None)
    note = tracker.record_spend("A", 2.50, 2.50)  # this position's OWN fresh total/delta: $2.50
    ctx.check(f"org budget ($1.00) already exceeded by $2.50 of real spend, got {note!r}",
              note is not None and "reached" in note and tracker.org_stopped is True)
    refusal = tracker.refusal_before_spawn("B", 0.0)
    ctx.check(f"a further spawn is refused, got {refusal!r}", refusal is not None and "budget" in refusal)


@test
def test_budget_tracker_record_spend_uses_the_callers_own_delta_not_a_shared_total(ctx: Ctx):
    """2.0.2 review finding 5: `record_spend`'s 3rd argument
    (`parent_total_usd`) is accepted but no longer trusted for the
    org-wide check -- only `spent_usd` (the caller's OWN delta,
    `child.cost_meter.total_usd` in real code) accumulates into `org_
    spent_usd`. Two concurrent children's own true spend ($0.40 + $0.40 =
    $0.80) must add up correctly even when the THIRD argument they each
    happen to pass is a stale/shared/bogus number -- proving the fix no
    longer double-counts (or under-counts) off of it."""
    from halo_harness.orgs import OrgBudgetTracker
    tracker = OrgBudgetTracker(org_name="demo", org_budget_usd=1.0, baseline_usd=0.0)
    tracker.record_spend("A", 0.40, 999.0)   # a deliberately-wrong/stale 3rd arg
    tracker.record_spend("B", 0.40, 999.0)   # same stale 3rd arg from a "concurrent" sibling
    ctx.check(f"org_spent_usd is the real sum of deltas, got {tracker.org_spent_usd}", tracker.org_spent_usd == 0.80)
    ctx.check("not yet stopped (under $1.00)", tracker.org_stopped is False)
    tracker.record_spend("C", 0.25, 999.0)
    ctx.check(f"now over budget, got org_spent_usd={tracker.org_spent_usd}", tracker.org_stopped is True)


@test
def test_budget_tracker_position_level_independent_of_org_level(ctx: Ctx):
    from halo_harness.orgs import OrgBudgetTracker
    tracker = OrgBudgetTracker(org_name="demo", org_budget_usd=None,
                                position_budgets={"Researcher": 0.50}, baseline_usd=0.0)
    tracker.record_spend("Researcher", 0.50, 0.50)  # exactly at the position's own budget
    ctx.check("Researcher's own budget tripped", "Researcher" in tracker.stopped_positions)
    refusal = tracker.refusal_before_spawn("Researcher", 0.50)
    ctx.check(f"Researcher alone is refused, got {refusal!r}", refusal is not None)
    other_ok = tracker.refusal_before_spawn("OtherPosition", 0.50)
    ctx.check("a DIFFERENT position with no budget of its own is unaffected", other_ok is None)


@test
def test_build_budget_tracker_reads_org_and_position_budgets(ctx: Ctx):
    from halo_harness.orgs import build_budget_tracker
    org = {"name": "x", "budget_usd": 2.5, "positions": [
        {"title": "A", "budget_usd": 1.0, "reports": ["B"]}, {"title": "B", "reports": []}]}
    tracker = build_budget_tracker(org, baseline_usd=0.3)
    ctx.check(f"org budget carried over, got {tracker.org_budget_usd}", tracker.org_budget_usd == 2.5)
    ctx.check(f"baseline carried over, got {tracker.baseline_usd}", tracker.baseline_usd == 0.3)
    ctx.check(f"only A has its own position budget, got {tracker.position_budgets}",
              tracker.position_budgets == {"A": 1.0})


# ---- init_wizard.py: pure (Textual-free) pieces ----------------------------

@test
def test_resolve_start_index_accepts_ordinal_or_key(ctx: Ctx):
    from halo_harness.tui.dialogs.init_wizard import ALL_STEP_KEYS, _resolve_start_index
    ctx.check('start_step=5 (1-based ordinal) resolves to "roles"',
              ALL_STEP_KEYS[_resolve_start_index(5, ALL_STEP_KEYS)] == "roles")
    ctx.check('start_step="orgs" (the key itself) resolves to "orgs"',
              ALL_STEP_KEYS[_resolve_start_index("orgs", ALL_STEP_KEYS)] == "orgs")
    ctx.check("an out-of-range ordinal falls back to step 1", _resolve_start_index(99, ALL_STEP_KEYS) == 0)
    ctx.check("an unknown key falls back to step 1", _resolve_start_index("not-a-step", ALL_STEP_KEYS) == 0)
    ctx.check("None falls back to step 1", _resolve_start_index(None, ALL_STEP_KEYS) == 0)


@test
def test_linux_fixes_step_key_never_shifts_roles_or_orgs(ctx: Ctx):
    """Whether or not `linux_fixes` survives into `full_step_keys()`,
    "roles" and "orgs" must stay at the SAME 1-based ordinal (5 and 6) --
    `linux_fixes` sits AFTER both, never between them."""
    from halo_harness.tui.dialogs.init_wizard import ALL_STEP_KEYS
    ctx.check(f"roles is step 5, got {ALL_STEP_KEYS}", ALL_STEP_KEYS[4] == "roles")
    ctx.check(f"orgs is step 6, got {ALL_STEP_KEYS}", ALL_STEP_KEYS[5] == "orgs")
    without_linux_fixes = tuple(k for k in ALL_STEP_KEYS if k != "linux_fixes")
    ctx.check("still true with linux_fixes dropped", without_linux_fixes[4] == "roles"
              and without_linux_fixes[5] == "orgs")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
