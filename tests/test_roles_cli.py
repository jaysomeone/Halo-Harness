"""tests.test_roles_cli -- Halo 2.0.2 (W7 round 1, brief A.5): `halo roles
template list|show|save|new|load`. (`edit` needs a real $EDITOR subprocess
and is covered live per the brief's own verification section, not here.)
"""
from __future__ import annotations

import os
import sys
import tempfile
from io import StringIO
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


def _run(argv: list) -> "tuple[int, str, str]":
    from halo_harness.roles_cli import cmd_roles
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = StringIO(), StringIO()
    try:
        rc = cmd_roles(argv)
        return rc, sys.stdout.getvalue(), sys.stderr.getvalue()
    finally:
        sys.stdout, sys.stderr = old_out, old_err


@test
def test_roles_template_list_empty(ctx: Ctx):
    _fresh_state_dir("roles-cli-list-")
    try:
        rc, out, _err = _run(["template", "list"])
        ctx.check(f"rc 0, got {rc}", rc == 0)
        ctx.check(f"says nothing saved, got {out!r}", "No role templates" in out)
    finally:
        _clear_state_dir_env()


@test
def test_roles_template_new_then_list_then_show(ctx: Ctx):
    _fresh_state_dir("roles-cli-new-")
    try:
        rc, out, _err = _run(["template", "new", "blank", "--description", "a blank one"])
        ctx.check(f"new succeeds, got {out!r}", rc == 0 and "blank" in out)
        rc, out, _err = _run(["template", "list"])
        ctx.check(f"shows up in listing, got {out!r}", rc == 0 and "blank" in out)
        rc, out, _err = _run(["template", "show", "blank"])
        ctx.check(f"show renders the description, got {out!r}", rc == 0 and "a blank one" in out)
    finally:
        _clear_state_dir_env()


@test
def test_roles_template_save_captures_the_current_config(ctx: Ctx):
    from halo_harness.theme import set_config_value
    _fresh_state_dir("roles-cli-save-")
    try:
        set_config_value("roles.coder", "or:vendor/my-coder")
        rc, out, _err = _run(["template", "save", "current"])
        ctx.check(f"save succeeds, got {out!r}", rc == 0)
        rc, out, _err = _run(["template", "show", "current"])
        ctx.check(f"the configured role shows up, got {out!r}", "coder" in out and "my-coder" in out)
    finally:
        _clear_state_dir_env()


@test
def test_roles_template_load_applies_to_config_json(ctx: Ctx):
    from halo_harness.roles import configured_role_table
    _fresh_state_dir("roles-cli-load-")
    try:
        _run(["template", "new", "empty-first"])
        from halo_harness.roles import save_role_template
        save_role_template("to-load", {"roles": {"coder": "or:vendor/loaded"}})
        rc, out, _err = _run(["template", "load", "to-load"])
        ctx.check(f"load succeeds, got {out!r}", rc == 0)
        ctx.check(f"config.json now has it, got {configured_role_table()}",
                  configured_role_table().get("coder") == "or:vendor/loaded")
    finally:
        _clear_state_dir_env()


@test
def test_roles_template_show_unknown_name_is_a_clean_error(ctx: Ctx):
    _fresh_state_dir("roles-cli-unknown-")
    try:
        rc, _out, err = _run(["template", "show", "does-not-exist"])
        ctx.check(f"non-zero exit, got {rc}", rc != 0)
        ctx.check(f"a clean message, not a traceback, got {err!r}", "does-not-exist" in err)
    finally:
        _clear_state_dir_env()


@test
def test_roles_unknown_top_level_subcommand(ctx: Ctx):
    rc, _out, err = _run(["notasubcommand"])
    ctx.check(f"non-zero exit, got {rc}", rc == 2)
    ctx.check(f"names 'template' as the only group, got {err!r}", "template" in err)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
