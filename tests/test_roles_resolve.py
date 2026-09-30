"""tests.test_roles_resolve -- V2c (H15): `roles.py`'s `resolve_role_ref`/
`describe_role_ref`/`resolve_all_roles`/`format_roles_table`, and `/roles`'s
own rendering (`commands/builtins.py::_cmd_roles`). Split out of
tests/test_roles.py (which covers `configured_role_table`/`resolve_role_
table`/`--role` parsing instead) to keep each file under the brief's own
"<= 250 lines per Write" rule.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


# ---- resolve_role_ref / describe_role_ref / resolve_all_roles / rendering --

@test
def test_resolve_role_ref_session_model_when_nothing_set(ctx: Ctx):
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.roles import resolve_role_ref
    parent_ref = parse_model_ref("or:vendor/parent")
    parent_profile = ModelProfile()
    ref, profile, source = resolve_role_ref("orchestrator", role_table={}, cli_overrides={},
                                             parent_ref=parent_ref, parent_profile=parent_profile,
                                             state_dir=Path(tempfile.mkdtemp()))
    ctx.check("reuses the parent ref object", ref is parent_ref)
    ctx.check("reuses the parent profile object", profile is parent_profile)
    ctx.check(f"source is 'session model', got {source!r}", source == "session model")


@test
def test_resolve_role_ref_role_table_and_cli_precedence(ctx: Ctx):
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.roles import resolve_role_ref
    parent_ref, parent_profile = parse_model_ref("or:vendor/parent"), ModelProfile()
    state_dir = Path(tempfile.mkdtemp())

    ref, _p, source = resolve_role_ref("researcher", role_table={"researcher": "or:vendor/fromtable"},
                                        cli_overrides={}, parent_ref=parent_ref, parent_profile=parent_profile,
                                        state_dir=state_dir)
    ctx.check("role table used", ref.model == "vendor/fromtable")
    ctx.check(f"source is 'role table', got {source!r}", source == "role table")

    ref2, _p2, source2 = resolve_role_ref("researcher", role_table={"researcher": "or:vendor/fromtable"},
                                           cli_overrides={"researcher": "or:vendor/fromcli"},
                                           parent_ref=parent_ref, parent_profile=parent_profile, state_dir=state_dir)
    ctx.check("CLI override wins", ref2.model == "vendor/fromcli")
    ctx.check(f"source is 'CLI --role', got {source2!r}", source2 == "CLI --role")


@test
def test_describe_role_ref_openrouter_price_format(ctx: Ctx):
    from rolo_claude.model import ModelProfile
    from rolo_claude.roles import describe_role_ref

    class _Ref:
        provider = "openrouter"
        model = "vendor/x"

    profile = ModelProfile(price_in=0.000002, price_out=0.000008)
    info = describe_role_ref(_Ref(), profile, Path(tempfile.mkdtemp()))
    ctx.check(f"endpoint is the bare model id, got {info}", info["endpoint"] == "vendor/x")
    ctx.check("path_type is the provider name", info["path_type"] == "openrouter")
    ctx.check(f"price formatted per-million, got {info['price']!r}", "1M in" in info["price"] and "1M out" in info["price"])


@test
def test_describe_role_ref_unknown_price_is_na(ctx: Ctx):
    from rolo_claude.model import ModelProfile
    from rolo_claude.roles import describe_role_ref

    class _Ref:
        provider = "cc"
        model = "claude-opus"

    info = describe_role_ref(_Ref(), ModelProfile(price_in=None, price_out=None), Path(tempfile.mkdtemp()))
    ctx.check(f"n/a when pricing unknown, got {info}", info["price"] == "n/a")


@test
def test_resolve_all_roles_covers_every_role_name(ctx: Ctx):
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.roles import ROLE_NAMES, resolve_all_roles
    parent_ref, parent_profile = parse_model_ref("or:vendor/parent"), ModelProfile()
    rows = resolve_all_roles(role_table={"coder": "or:vendor/coder-model"}, cli_overrides={},
                              parent_ref=parent_ref, parent_profile=parent_profile,
                              state_dir=Path(tempfile.mkdtemp()))
    ctx.check(f"one row per role, got {[r['role'] for r in rows]}", [r["role"] for r in rows] == list(ROLE_NAMES))
    coder_row = next(r for r in rows if r["role"] == "coder")
    ctx.check("coder resolved from the role table", coder_row["model"] == "or:vendor/coder-model")
    orchestrator_row = next(r for r in rows if r["role"] == "orchestrator")
    ctx.check("orchestrator (unset) falls back to the session model",
              orchestrator_row["model"] == parent_ref.raw)


@test
def test_format_roles_table_renders_every_role(ctx: Ctx):
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.roles import ROLE_NAMES, format_roles_table, resolve_all_roles
    parent_ref, parent_profile = parse_model_ref("or:vendor/parent"), ModelProfile()
    rows = resolve_all_roles(role_table={}, cli_overrides={}, parent_ref=parent_ref,
                              parent_profile=parent_profile, state_dir=Path(tempfile.mkdtemp()))
    text = format_roles_table(rows)
    for name in ROLE_NAMES:
        ctx.check(f"{name!r} appears in the rendered table", name in text)


# ---- /roles slash command ---------------------------------------------------

@test
def test_cmd_roles_no_session_is_a_clean_message(ctx: Ctx):
    from rolo_claude.commands.builtins import HeadlessFacade, _cmd_roles
    facade = HeadlessFacade(cwd=Path(tempfile.mkdtemp()))
    out = _cmd_roles("", facade)
    ctx.check(f"no traceback, a plain message, got {out!r}", "session" in out.lower())


@test
def test_cmd_roles_with_live_session_renders_table(ctx: Ctx):
    from types import SimpleNamespace
    from rolo_claude.commands.builtins import HeadlessFacade, _cmd_roles
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.roles import ROLE_NAMES

    fake_runtime = SimpleNamespace(role_table={"researcher": "or:vendor/researcher-model"},
                                    cli_role_overrides={}, routes={})
    fake_session = SimpleNamespace(model_ref=parse_model_ref("or:vendor/parent"), model_profile=ModelProfile(),
                                    state_dir=Path(tempfile.mkdtemp()), agent_runtime=fake_runtime)
    facade = HeadlessFacade(cwd=Path(tempfile.mkdtemp()), session=fake_session)
    out = _cmd_roles("", facade)
    for name in ROLE_NAMES:
        ctx.check(f"{name!r} appears in /roles output", name in out)
    ctx.check("the researcher role's configured model appears", "researcher-model" in out)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
