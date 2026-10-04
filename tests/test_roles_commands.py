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
    """2.0.2 review finding 23: `/role` must land in `cli_role_overrides`
    (the rung `resolve_agent_model` treats as winning even over an agent
    file's own `model:`), never the plain `role_table` -- so this checks
    `agent_runtime.cli_role_overrides`, not `session.roles`."""
    from halo_harness.commands.builtins import _cmd_role
    facade, session = _fake_facade_with_session(roles={})
    out = _cmd_role("coder or:vendor/newcoder high", facade)
    ctx.check(f"confirms the set, got {out!r}", "coder" in out and "newcoder" in out)
    overrides = session.agent_runtime.cli_role_overrides
    ctx.check(f"landed in cli_role_overrides, got {overrides}",
              overrides.get("coder") == {"model": "or:vendor/newcoder", "effort": "high"})
    ctx.check(f"the plain role_table was left alone, got {session.roles}", session.roles == {})


@test
def test_role_command_no_effort_sets_a_bare_string(ctx: Ctx):
    from halo_harness.commands.builtins import _cmd_role
    facade, session = _fake_facade_with_session(roles={})
    _cmd_role("researcher or:vendor/newresearcher", facade)
    overrides = session.agent_runtime.cli_role_overrides
    ctx.check(f"bare model, no effort, got {overrides}", overrides.get("researcher") == "or:vendor/newresearcher")


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
def test_role_command_rejects_bad_model_and_bad_effort(ctx: Ctx):
    """2.0.2 review finding 23: the model/effort strings were never
    validated at all before this -- a typo silently became "this role's
    live override" with no error until the next sub-agent spawn failed."""
    from halo_harness.commands.builtins import _cmd_role
    facade, session = _fake_facade_with_session(roles={})
    out = _cmd_role("coder not-a-real-model-ref", facade)
    ctx.check(f"bad model is rejected, got {out!r}", "not-a-real-model-ref" in out)
    ctx.check("nothing was written", not session.agent_runtime.cli_role_overrides)

    out2 = _cmd_role("coder or:vendor/x ultra-high", facade)
    ctx.check(f"bad effort is rejected, got {out2!r}", "ultra-high" in out2 and "high" in out2)
    ctx.check("nothing was written", not session.agent_runtime.cli_role_overrides)


@test
def test_role_command_override_wins_over_frontmatter_model(ctx: Ctx):
    """2.0.2 review finding 23 pin: `/role` used to write into the plain
    `role_table` rung, which `resolve_agent_model` ranks BELOW an agent
    file's own `model:` -- so `/role coder X` reported success but a
    coder-role agent with its own `model:` set kept using ITS model, not
    X. `cli_role_overrides` is the rung that chain says wins regardless."""
    from halo_harness.commands.builtins import _cmd_role
    from halo_harness.config.agents_md import resolve_agent_model
    facade, session = _fake_facade_with_session(roles={})
    out = _cmd_role("coder or:vendor/overridden", facade)
    ctx.check(f"confirms the set, got {out!r}", "overridden" in out)
    runtime = session.agent_runtime
    ref, _profile = resolve_agent_model(
        frontmatter_model="or:vendor/agent-files-own-model", role_name="coder",
        role_table=runtime.role_table, cli_role_overrides=runtime.cli_role_overrides,
        parent_ref=session.model_ref, parent_profile=session.model_profile, state_dir=session.state_dir,
        routes=runtime.routes,
    )
    ctx.check(f"the live /role override wins over the agent file's own model:, got {ref.raw}",
              ref.raw == "or:vendor/overridden")


@test
def test_roles_set_is_the_same_as_role(ctx: Ctx):
    from halo_harness.commands.builtins import _cmd_role, _cmd_roles
    facade1, session1 = _fake_facade_with_session(roles={})
    facade2, session2 = _fake_facade_with_session(roles={})
    _cmd_roles("set coder or:vendor/x high", facade1)
    _cmd_role("coder or:vendor/x high", facade2)
    overrides1 = session1.agent_runtime.cli_role_overrides
    overrides2 = session2.agent_runtime.cli_role_overrides
    ctx.check(f"/roles set and /role agree, got {overrides1} vs {overrides2}",
              overrides1 == overrides2 and overrides1.get("coder") == {"model": "or:vendor/x", "effort": "high"})


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
