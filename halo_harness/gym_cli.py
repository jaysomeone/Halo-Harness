"""halo_harness.gym_cli -- Halo 2.0.3 round 5d: `halo gym [--models ...]
[--roles ...] [--quick]`, `halo gym show [model]`, `halo gym propose
[--apply] [--roles ...] [--main REF] [--name NAME]`. Argparse + printing
only -- `gym_run.py`/`gym_propose.py`/`gym.py` do the actual work, same
module split as `roles_cli.py` over `roles.py`.
"""

from __future__ import annotations

import argparse
import sys

from halo_harness.gym import SUPPORTING_ROLES


def _parse_csv(raw) -> "list":
    return [v.strip() for v in raw.split(",") if v.strip()] if raw else []


def _parse_roles(raw) -> "list":
    names = _parse_csv(raw)
    unknown = [n for n in names if n not in SUPPORTING_ROLES]
    if unknown:
        raise ValueError(f"--roles: unknown role(s) {', '.join(unknown)} (expected a subset of "
                          f"{', '.join(SUPPORTING_ROLES)})")
    return names


def _cmd_run(argv: list) -> int:
    from halo_harness.config.paths import bridge_home
    from halo_harness.gym import format_card
    from halo_harness.gym_propose import composite_score
    from halo_harness.gym_run import run_gym

    parser = argparse.ArgumentParser(
        prog="halo gym", add_help=True,
        description="Run the fixed task battery against local models on THIS machine's hardware and score "
                    "each one -- tool-call accuracy, edit success, context recall, instruction adherence, "
                    "tokens/second, prefill seconds. ol:, hf:local/*, and hf:mlx/* refs are all local.")
    parser.add_argument("--models", default=None, metavar="REF,REF,...",
                         help="comma-separated ol:/hf:local/hf:mlx refs (default: every model in the default "
                             "Ollama host's own catalog); a cloud/router/endpoint ref is accepted for "
                             "comparison but never run by default")
    parser.add_argument("--roles", default=None, metavar="ROLE,ROLE,...",
                         help="also print each model's weighted composite for these roles (default: "
                             f"{', '.join(SUPPORTING_ROLES)})")
    parser.add_argument("--quick", action="store_true", help="halve the battery size N for a faster, "
                                                              "noisier read")
    parser.add_argument("--show-replies", action="store_true",
                         help="print each reply-only task's actual excerpt (first 200 chars) alongside its "
                             "score -- samples are always saved in the result JSON regardless of this flag")
    args = parser.parse_args(argv)
    try:
        models = _parse_csv(args.models) or None
        roles = _parse_roles(args.roles) or list(SUPPORTING_ROLES)
    except ValueError as e:
        print(f"halo gym: {e}", file=sys.stderr)
        return 2

    state_dir = bridge_home()
    target = ", ".join(models) if models else "every model in the default Ollama host's catalog"
    print(f"halo gym: running the battery against {target}{' (--quick)' if args.quick else ''}...")

    def _on_each(ref, result) -> None:
        print()
        print(format_card(result, show_replies=args.show_replies))
        tps_values = [result["tokens_per_second"]] if isinstance(result.get("tokens_per_second"), (int, float)) else []
        max_tps = max(tps_values) if tps_values else None
        for role in roles:
            score, _terms = composite_score(result, role, max_tps=max_tps)
            print(f"  as {role}: {score:.2f}" if score is not None else f"  as {role}: n/a")

    results = run_gym(models, quick=args.quick, state_dir=state_dir, on_each=_on_each)
    if not results:
        print("halo gym: no models to run -- pass --models, or configure an Ollama host "
              "(`ollama.hosts` in ~/.halo/config.json) with at least one model pulled", file=sys.stderr)
        return 1
    print(f"\nSaved under ~/.halo/gym/<host>/<digest>.json. `halo gym show` prints a card per model; "
          f"`halo gym propose` turns these scores into a role table.")
    return 0


def _cmd_show(argv: list) -> int:
    from halo_harness.config.paths import bridge_home
    from halo_harness.gym import find_results_for_model, format_card, iter_results

    parser = argparse.ArgumentParser(prog="halo gym show", add_help=True,
                                      description="Print the saved per-model gym card(s).")
    parser.add_argument("model", nargs="?", default=None, help="only this model's card (default: every "
                                                                "saved result, newest first)")
    parser.add_argument("--show-replies", action="store_true",
                         help="also print each reply-only task's saved reply excerpts")
    args = parser.parse_args(argv)
    state_dir = bridge_home()
    rows = find_results_for_model(state_dir, args.model) if args.model else iter_results(state_dir)
    if not rows:
        which = f" for {args.model!r}" if args.model else ""
        print(f"halo gym show: no saved gym results{which} -- run `halo gym` first.", file=sys.stderr)
        return 1
    for i, row in enumerate(rows):
        if i:
            print()
        print(format_card(row, show_replies=args.show_replies))
    return 0


def _cmd_propose(argv: list) -> int:
    from halo_harness.config.paths import bridge_home
    from halo_harness.gym import iter_results
    from halo_harness.gym_propose import apply_proposal, propose_role_table

    parser = argparse.ArgumentParser(
        prog="halo gym propose", add_help=True,
        description="Turn saved `halo gym` scores into a role-table proposal (the best LOCAL model per "
                    "supporting role) -- one sentence per choice naming the score behind it.")
    parser.add_argument("--apply", action="store_true",
                         help="save the proposal as a role template via the existing `halo roles template "
                             "import` path (`halo roles template load NAME` then applies it)")
    parser.add_argument("--name", default="gym-proposed", metavar="NAME",
                         help="template name for --apply (default: gym-proposed)")
    parser.add_argument("--roles", default=None, metavar="ROLE,ROLE,...",
                         help=f"propose only these roles (default: {', '.join(SUPPORTING_ROLES)})")
    parser.add_argument("--main", default=None, metavar="REF",
                         help="use REF, not the configured default model, as the VRAM-fit reference point "
                             "(main itself is never proposed either way)")
    args = parser.parse_args(argv)
    try:
        roles = _parse_roles(args.roles) or None
    except ValueError as e:
        print(f"halo gym propose: {e}", file=sys.stderr)
        return 2

    state_dir = bridge_home()
    results = iter_results(state_dir)
    if not results:
        print("halo gym propose: no saved gym results yet -- run `halo gym` first.", file=sys.stderr)
        return 1
    roles_dict, sentences = propose_role_table(results, roles=roles, main_ref_raw=args.main, state_dir=state_dir)
    for s in sentences:
        print(s)
    if not roles_dict:
        print("halo gym propose: nothing to propose yet -- every candidate role came back with no usable "
              "score (see the sentences above).", file=sys.stderr)
        return 1
    if args.apply:
        ok, problems = apply_proposal(roles_dict, template_name=args.name)
        if not ok:
            print(f"halo gym propose --apply: {'; '.join(problems)}", file=sys.stderr)
            return 1
        print(f"\nSaved as role template {args.name!r} -- `halo roles template load {args.name}` (or the "
              f"picker) applies it to your session.")
    return 0


def cmd_gym(argv: list) -> int:
    if argv and argv[0] == "show":
        return _cmd_show(argv[1:])
    if argv and argv[0] == "propose":
        return _cmd_propose(argv[1:])
    if argv and argv[0] in ("-h", "--help"):
        print("usage: halo gym [--models REF,REF,...] [--roles ROLE,...] [--quick]", file=sys.stderr)
        print("       halo gym show [model]", file=sys.stderr)
        print("       halo gym propose [--apply] [--name NAME] [--roles ROLE,...] [--main REF]", file=sys.stderr)
        return 0
    return _cmd_run(argv)
