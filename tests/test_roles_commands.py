"""tests.test_roles_commands -- Halo 2.0.2 (W7 round 1, brief A.4/A.5):
`/role <name> <model> [effort]` and `/roles set|templates|save|load|new|
show` (`commands/builtins.py`). Split out of tests/test_roles_resolve.py to
keep each file under the house "<= 250 lines per Write" rule.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


def _fake_facade_with_session(*, roles=None, cli_roles=None, state_dir=None):
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.commands.builtins import HeadlessFacade

    roles = roles if roles is not None else {}
    runtime = SimpleNamespace(role_table=roles, cli_role_overrides=(cli_roles or {}), routes={})
    session = SimpleNamespace(model_ref=parse_model_ref("or:vendor/parent"), model_profile=ModelProfile(),
                               state_dir=state_dir or Path(tempfile.mkdtemp(prefix="roles-cmd-")),
                               agent_runtime=runtime, roles=roles)
    return HeadlessFacade(cwd=Path(tempfile.mkdtemp(prefix="roles-cmd-cwd-")), session=session), session


@test
def test_role_command_sets_a_known_role_live(ctx: Ctx):
    from halo_harness.commands.builtins import _cmd_role
    facade, session = _fake_facade_with_session(roles={})
    out = _cmd_role("coder or:vendor/newcoder high", facade)
    ctx.check(f"confirms the set, got {out!r}", "coder" in out and "newcoder" in out)
    ctx.check(f"the live session's own role table was mutated, got {session.roles}",
              session.roles.get("coder") == {"model": "or:vendor/newcoder", "effort": "high"})


@test
def test_role_command_no_effort_sets_a_bare_string(ctx: Ctx):
    from halo_harness.commands.builtins import _cmd_role
    facade, session = _fake_facade_with_session(roles={})
    _cmd_role("researcher or:vendor/newresearcher", facade)
    ctx.check(f"bare model, no effort, got {session.roles}", session.roles.get("researcher") == "or:vendor/newresearcher")


@test
def test_role_command_unknown_name_lists_known_names(ctx: Ctx):
    from halo_harness.commands.builtins import _cmd_role
    facade, _session = _fake_facade_with_session(roles={})
    out = _cmd_role("not_a_role or:vendor/x", facade)
    ctx.check(f"names the bad role and lists known ones, got {out!r}", "not_a_role" in out and "coder" in out)


@test
def test_role_command_no_session_is_a_clean_message(ctx: Ctx):
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_role
    facade = HeadlessFacade(cwd=Path(tempfile.mkdtemp()))
    out = _cmd_role("coder or:vendor/x", facade)
    ctx.check(f"no traceback, a plain message, got {out!r}", "session" in out.lower())


@test
def test_roles_set_is_the_same_as_role(ctx: Ctx):
    from halo_harness.commands.builtins import _cmd_role, _cmd_roles
    facade1, session1 = _fake_facade_with_session(roles={})
    facade2, session2 = _fake_facade_with_session(roles={})
    _cmd_roles("set coder or:vendor/x high", facade1)
    _cmd_role("coder or:vendor/x high", facade2)
    ctx.check(f"/roles set and /role agree, got {session1.roles} vs {session2.roles}",
              session1.roles == session2.roles)


@test
def test_roles_templates_save_show_load_round_trip(ctx: Ctx):
    from halo_harness.commands.builtins import _cmd_roles
    d = Path(tempfile.mkdtemp(prefix="roles-cmd-templates-"))
    facade, session = _fake_facade_with_session(roles={"coder": "or:vendor/coder-model"}, state_dir=d)

    out_empty = _cmd_roles("templates", facade)
    ctx.check(f"nothing saved yet, got {out_empty!r}", "No role templates" in out_empty)

    out_save = _cmd_roles("save my-template", facade)
    ctx.check(f"save confirms, got {out_save!r}", "my-template" in out_save)

    out_list = _cmd_roles("templates", facade)
    ctx.check(f"the saved template is now listed, got {out_list!r}", "my-template" in out_list)

    out_show = _cmd_roles("show my-template", facade)
    ctx.check(f"show renders the saved role, got {out_show!r}", "coder" in out_show and "coder-model" in out_show)

    # A fresh session with nothing configured, then /roles load.
    facade2, session2 = _fake_facade_with_session(roles={}, state_dir=d)
    out_load = _cmd_roles("load my-template", facade2)
    ctx.check(f"load confirms, got {out_load!r}", "my-template" in out_load)
    ctx.check(f"the live session picked it up immediately, got {session2.roles}",
              session2.roles.get("coder") == "or:vendor/coder-model")


@test
def test_roles_new_creates_an_empty_template(ctx: Ctx):
    from halo_harness.commands.builtins import _cmd_roles
    from halo_harness.roles import load_role_template
    d = Path(tempfile.mkdtemp(prefix="roles-cmd-new-"))
    facade, _session = _fake_facade_with_session(roles={}, state_dir=d)
    out = _cmd_roles("new blank-profile", facade)
    ctx.check(f"confirms creation, got {out!r}", "blank-profile" in out)
    ctx.check("an empty template now exists on disk",
              load_role_template("blank-profile", state_dir=d) == {"name": "blank-profile", "description": "", "roles": {}})


@test
def test_roles_edit_has_no_headless_form(ctx: Ctx):
    from halo_harness.commands.builtins import _cmd_roles
    facade, _session = _fake_facade_with_session(roles={})
    out = _cmd_roles("edit anything", facade)
    ctx.check(f"names the TUI as the real home for this, got {out!r}", "TUI" in out)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
