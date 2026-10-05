"""halo_harness.gym_propose -- Halo 2.0.3 round 5d: "from data to the
role table." Turns saved `gym.GymResult` rows into a role-table proposal
in the EXISTING roles v2 shape (`roles.py`'s own `{"model"[, "effort"]}`
value, a plain template `{"name", "description", "roles"}` document) --
nothing new. Each of `gym.SUPPORTING_ROLES` gets the LOCAL (`ol:`) model
with the best WEIGHTED composite of the four raw ratios/throughput for
that role's own priorities; `main` (`orchestrator`) is never touched here
(brief: "main left as configured unless `--main` is passed" -- even when
passed, `--main` only supplies the VRAM-fit reference point below, it
never adds an `orchestrator` entry to the proposal -- matching `roles.
compute_builtin_role_presets`' own "never automatic beyond what's
documented" rule).
"""

from __future__ import annotations

from typing import Optional

from halo_harness.gym import SUPPORTING_ROLES

# Weights are DOCUMENTED, not derived: how much each role's own job leans
# on each raw measurement. `tokens_per_second` is normalized against the
# fastest candidate IN THIS proposal batch (every other term is already a
# 0..1 ratio) before these weights apply. A metric with no measurement at
# all for a given candidate (`score is None`) drops OUT of that
# candidate's weighted average instead of counting as zero -- "never
# measured" must never score worse than "measured and failed."
ROLE_WEIGHTS = {
    "small": {"tokens_per_second": 0.4, "instruction_adherence": 0.3, "tool_call_accuracy": 0.3},
    "researcher": {"context_recall": 0.4, "tool_call_accuracy": 0.3, "edit_success": 0.3},
    "judge": {"instruction_adherence": 0.5, "context_recall": 0.3, "tool_call_accuracy": 0.2},
    "subagent_default": {"tool_call_accuracy": 0.4, "edit_success": 0.4, "instruction_adherence": 0.2},
}


def _raw_metric(result: dict, metric: str) -> Optional[float]:
    if metric == "tokens_per_second":
        v = result.get("tokens_per_second")
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    sub = result.get(metric) or {}
    v = sub.get("score")
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def composite_score(result: dict, role: str, *, max_tps: Optional[float]) -> "tuple[Optional[float], list]":
    """`(score, terms)` -- `terms` is `[(metric, raw_value, weight), ...]`
    for every metric that actually had data, for the sentence builder
    below. `score` is `None` when NOT ONE weighted metric had data."""
    weights = ROLE_WEIGHTS.get(role, {})
    terms = []
    weighted_sum = 0.0
    weight_total = 0.0
    for metric, weight in weights.items():
        raw = _raw_metric(result, metric)
        if raw is None:
            continue
        normalized = (raw / max_tps) if (metric == "tokens_per_second" and max_tps) else raw
        normalized = max(0.0, min(1.0, normalized))
        weighted_sum += normalized * weight
        weight_total += weight
        terms.append((metric, raw, weight))
    if weight_total <= 0:
        return None, terms
    return weighted_sum / weight_total, terms


def _sentence(role: str, result: dict, score: float, terms: list, vram_reason: Optional[str]) -> str:
    parts = ", ".join(f"{metric.replace('_', ' ')} {raw:.2f}" if metric != "tokens_per_second"
                       else f"{raw:.1f} tok/s" for metric, raw, _w in terms)
    base = f"{role} -> {result.get('model_ref')}: composite {score:.2f} from {parts or 'no measured terms'}."
    return f"{base} {vram_reason}." if vram_reason else base


_LOCAL_REF_PREFIXES = ("ol:", "hf:local/", "hf:mlx/")


def _is_local_ref(model_ref) -> bool:
    return isinstance(model_ref, str) and model_ref.startswith(_LOCAL_REF_PREFIXES)


def best_candidate_for_role(results: "list[dict]", role: str, *, main_ref=None) -> "tuple[Optional[dict], Optional[str]]":
    """`(value_for_role_table, one_sentence)` -- `value_for_role_table` is
    `None` when no LOCAL candidate has a usable score for `role` at all
    (brief's own "a role with no candidate" case), in which case the
    sentence says so in plain English rather than being omitted. "Local"
    (round 5i) is `ol:`/`hf:local/*`/`hf:mlx/*` -- never a router/
    endpoint/cloud ref. The round 5b VRAM-aware rule below is applied to
    EVERY winner regardless of provider, never a separate branch here --
    `roles.vram_aware_override` already no-ops on its own whenever either
    `main_ref` or the candidate isn't an `ollama` ref (its own existing
    guard clauses), which is exactly "skipped when no Ollama host is
    involved" for an hf: winner or an hf:-main `--main`."""
    local = [r for r in results if _is_local_ref(r.get("model_ref"))]
    tps_values = [r["tokens_per_second"] for r in local if isinstance(r.get("tokens_per_second"), (int, float))]
    max_tps = max(tps_values) if tps_values else None
    scored = []
    for r in local:
        score, terms = composite_score(r, role, max_tps=max_tps)
        if score is not None:
            scored.append((score, r, terms))
    if not scored:
        return None, f"{role} -> no local model has a usable score for this role yet -- run `halo gym` first."
    scored.sort(key=lambda t: (-t[0], t[1].get("model_ref", "")))
    score, result, terms = scored[0]
    value = result["model_ref"]
    vram_reason = None
    if role in SUPPORTING_ROLES and main_ref is not None:
        from halo_harness.roles import vram_aware_override
        try:
            redirected, vram_reason = vram_aware_override(role, value, main_ref=main_ref)
            value = redirected
        except Exception:
            pass
    return {"value": value, "sentence": _sentence(role, result, score, terms, vram_reason)}, None


def propose_role_table(results: "list[dict]", *, roles: "Optional[list]" = None,
                        main_ref_raw: "Optional[str]" = None, state_dir=None) -> "tuple[dict, list]":
    """`(roles_dict, sentences)` -- `roles_dict` is ready to drop straight
    into a role template's own `"roles"` key (brief: "the existing roles
    v2 shape, nothing new"). `roles` narrows which of `gym.SUPPORTING_
    ROLES` to propose (default: all four); a role with no candidate is
    left OUT of `roles_dict` entirely (never a null placeholder) but
    still gets its plain-English sentence."""
    from halo_harness.model import parse_model_ref
    from halo_harness.theme import get_config_value
    wanted = [r for r in (roles or SUPPORTING_ROLES) if r in SUPPORTING_ROLES]
    main_ref = None
    main_raw = main_ref_raw or get_config_value("model", default=None)
    if isinstance(main_raw, str) and main_raw.strip():
        try:
            main_ref = parse_model_ref(main_raw)
        except Exception:
            main_ref = None
    roles_dict: dict = {}
    sentences: "list[str]" = []
    for role in wanted:
        chosen, no_candidate_sentence = best_candidate_for_role(results, role, main_ref=main_ref)
        if chosen is None:
            sentences.append(no_candidate_sentence)
            continue
        roles_dict[role] = chosen["value"]
        sentences.append(chosen["sentence"])
    return roles_dict, sentences


def apply_proposal(roles_dict: dict, *, template_name: str = "gym-proposed",
                    description: str = "") -> "tuple[bool, list]":
    """Brief item 2: "`--apply` writes it through the existing `halo
    roles template import` path" -- builds the ordinary template document
    `roles_cli._cmd_import` already validates/saves, and calls that SAME
    CLI entry point (never `roles.save_role_template` directly) so an
    applied proposal goes through EXACTLY the path a user's own `halo
    roles template import <file>` would, including its env-based
    `bridge_home()` resolution -- there is no separate `state_dir`
    parameter here for the same reason `cmd_roles` itself has none; a
    hermetic test scopes `BRIDGE_TEST_HOME`/`BRIDGE_STATE_DIR` first, same
    as every other test of this exact CLI path. The result lands at
    `~/.halo/roles/<template_name>.json`; `halo roles template load
    <template_name>` (or the picker) is the separate, explicit step that
    makes it the live table -- consistent with every other saved
    template."""
    import json
    import tempfile
    from pathlib import Path

    from halo_harness.roles_cli import cmd_roles
    payload = {"name": template_name,
               "description": description or "Proposed by `halo gym propose` from this machine's own gym scores.",
               "roles": roles_dict}
    tmp_dir = Path(tempfile.mkdtemp(prefix="halo-gym-propose-"))
    tmp_path = tmp_dir / f"{template_name}.json"
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = cmd_roles(["template", "import", str(tmp_path)])
    if rc != 0:
        return False, [buf.getvalue().strip() or f"halo roles template import exited {rc}"]
    return True, []
