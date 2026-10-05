"""tests.test_gym_propose_5d -- Halo 2.0.3 round 5d: `halo gym propose`
against fixture score files (never a real model or a real gym run) --
ties, a model with no measured tool-calling ability, a role with no
candidate at all, the round 5b VRAM-aware rule, and `--apply` through the
existing `halo roles template import` path.
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


def _row(ref: str, *, tca=0.5, edit=0.5, recall=0.5, instr=0.5, tps=20.0) -> dict:
    return {"model_ref": ref, "model": ref.split(":", 1)[-1],
            "tool_call_accuracy": {"score": tca}, "edit_success": {"score": edit},
            "context_recall": {"score": recall}, "instruction_adherence": {"score": instr},
            "tokens_per_second": tps}


class _Env:
    _KEYS = ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE")

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self._KEYS}
        d = Path(tempfile.mkdtemp(prefix="gym-propose-5d-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        self.state_dir = d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_ties_break_deterministically_by_ref(ctx: Ctx):
    from halo_harness.gym_propose import best_candidate_for_role
    results = [_row("ol:z-model", tca=0.8, edit=0.8, instr=0.8), _row("ol:a-model", tca=0.8, edit=0.8, instr=0.8)]
    chosen, no_candidate = best_candidate_for_role(results, "subagent_default")
    ctx.check(f"no 'no candidate' message on a real tie, got {no_candidate}", no_candidate is None)
    ctx.check(f"the alphabetically-first ref wins a true tie, got {chosen['value']}", chosen["value"] == "ol:a-model")
    # Run it again (fresh list order reversed) -- must stay deterministic.
    chosen2, _ = best_candidate_for_role(list(reversed(results)), "subagent_default")
    ctx.check("tie-break is order-independent", chosen2["value"] == "ol:a-model")


@test
def test_model_with_no_tool_calling_ability_loses_tool_heavy_roles(ctx: Ctx):
    from halo_harness.gym_propose import propose_role_table
    no_tools = _row("ol:no-tools", tca=0.0, edit=0.0, recall=0.3, instr=0.3, tps=50.0)
    rounded = _row("ol:rounded", tca=0.9, edit=0.9, recall=0.6, instr=0.7, tps=15.0)
    roles_dict, sentences = propose_role_table([no_tools, rounded], roles=["subagent_default"])
    ctx.check(f"the rounded model wins subagent_default over the tool-incapable one, got {roles_dict}",
              roles_dict.get("subagent_default") == "ol:rounded")
    ctx.check("the sentence names the composite score", any("subagent_default ->" in s for s in sentences))


@test
def test_role_with_no_local_candidate(ctx: Ctx):
    from halo_harness.gym_propose import propose_role_table
    # Only a CLOUD row on file -- propose never proposes a non-ol: ref.
    cloud_only = [_row("or:vendor/cloud-model", tca=0.9, edit=0.9, recall=0.9, instr=0.9, tps=100.0)]
    roles_dict, sentences = propose_role_table(cloud_only, roles=["judge"])
    ctx.check(f"nothing proposed for judge, got {roles_dict}", roles_dict == {})
    ctx.check(f"the sentence says so in plain English, got {sentences}",
              len(sentences) == 1 and "no local model has a usable score" in sentences[0])
    # Empty store entirely.
    roles_dict2, sentences2 = propose_role_table([])
    ctx.check("every one of the four roles reports no-candidate when nothing is on file",
              roles_dict2 == {} and len(sentences2) == 4)


@test
def test_vram_aware_rule_is_respected_via_the_existing_function(ctx: Ctx):
    """Never re-derives `fits_beside_main`'s own GPU arithmetic (round 5b
    part 2's own suite already pins that) -- this only proves `gym_
    propose` calls `roles.vram_aware_override` and applies ITS answer,
    the "nothing new" the brief asks for."""
    import halo_harness.roles as roles_mod
    from halo_harness.gym_propose import best_candidate_for_role

    calls = []

    def fake_override(role_name, raw_value, *, main_ref, state_dir=None, hw_runner=None):
        calls.append((role_name, raw_value))
        return main_ref.raw, "(same as main: fits beside it: no)"

    original = roles_mod.vram_aware_override
    roles_mod.vram_aware_override = fake_override
    try:
        from halo_harness.model import parse_model_ref
        main_ref = parse_model_ref("ol:main-model")
        results = [_row("ol:small-candidate", tca=0.9, edit=0.9, instr=0.9, tps=30.0)]
        chosen, _no_candidate = best_candidate_for_role(results, "small", main_ref=main_ref)
        ctx.check(f"vram_aware_override was called for the winning candidate, got {calls}",
                  calls and calls[0] == ("small", "ol:small-candidate"))
        ctx.check(f"the redirected value won, got {chosen['value']}", chosen["value"] == "ol:main-model")
        ctx.check(f"the reason reached the sentence, got {chosen['sentence']!r}",
                  "same as main" in chosen["sentence"])
    finally:
        roles_mod.vram_aware_override = original


@test
def test_apply_writes_through_the_roles_template_import_path(ctx: Ctx):
    from halo_harness.gym_propose import apply_proposal
    from halo_harness.roles import load_role_template
    with _Env() as env:
        ok, problems = apply_proposal({"small": "ol:fast-model", "judge": "ol:smart-model"},
                                       template_name="gym-proposed-test")
        ctx.check(f"apply succeeds, got {problems}", ok and problems == [])
        saved_path = env.state_dir / "roles" / "gym-proposed-test.json"
        ctx.check(f"landed under ~/.halo/roles via the real import path, got {saved_path}", saved_path.exists())
        data = json.loads(saved_path.read_text(encoding="utf-8"))
        ctx.check(f"roles payload round-tripped, got {data}",
                  data.get("roles") == {"small": "ol:fast-model", "judge": "ol:smart-model"})
        # Also readable through roles.py's own loader (never a second shape).
        template = load_role_template("gym-proposed-test", state_dir=env.state_dir)
        ctx.check("roles.load_role_template reads it back cleanly", template is not None
                  and template["roles"].get("small") == "ol:fast-model")


@test
def test_apply_rejects_an_invalid_role_name_without_writing_anything(ctx: Ctx):
    from halo_harness.gym_propose import apply_proposal
    with _Env() as env:
        ok, problems = apply_proposal({"Not Valid!": "ol:x"}, template_name="gym-bad-test")
        ctx.check(f"apply fails with a plain reason, got {problems}", not ok and problems)
        ctx.check("nothing was written", not (env.state_dir / "roles" / "gym-bad-test.json").exists())


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
