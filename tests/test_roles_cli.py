"""tests.test_roles_cli -- Halo 2.0.2 (W7 round 1, brief A.5): `halo roles
template list|show|save|new|load`. (`edit` needs a real $EDITOR subprocess
and is covered live per the brief's own verification section, not here.)
"""
from __future__ import annotations

import json
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
def test_roles_template_export_to_file_then_import_round_trip(ctx: Ctx):
    """Halo 2.0.2 round D (brief item 3): `halo roles template export
    <name> [file]` / `import <file>` -- plain JSON, round trips."""
    from halo_harness.roles import load_role_template, save_role_template
    _fresh_state_dir("roles-cli-export-import-")
    try:
        save_role_template("source", {"description": "exportable", "roles": {"coder": "or:vendor/x"}})
        out_path = Path(tempfile.mkdtemp(prefix="roles-cli-export-out-")) / "source.json"
        rc, _out, err = _run(["template", "export", "source", str(out_path)])
        ctx.check(f"export to file succeeds, got rc={rc} err={err!r}", rc == 0 and out_path.is_file())
        exported = json.loads(out_path.read_text(encoding="utf-8"))
        ctx.check(f"exported JSON has the role table, got {exported}", exported.get("roles", {}).get("coder"))

        rc2, out2, _err2 = _run(["template", "export", "source"])
        ctx.check(f"with no file, prints the JSON to stdout, got rc={rc2}",
                  rc2 == 0 and json.loads(out2).get("name") == "source")

        rc3, out3, _err3 = _run(["template", "import", str(out_path)])
        ctx.check(f"import under a DIFFERENT name (the file's own content) succeeds, got {out3!r}", rc3 == 0)
        reimported = load_role_template("source")
        ctx.check("re-imported template matches the original", reimported["roles"] == {"coder": "or:vendor/x"})
    finally:
        _clear_state_dir_env()


@test
def test_roles_template_import_rejects_invalid_shape_with_every_problem(ctx: Ctx):
    _fresh_state_dir("roles-cli-import-invalid-")
    try:
        bad_path = Path(tempfile.mkdtemp(prefix="roles-cli-import-bad-")) / "bad.json"
        bad_path.write_text(json.dumps({"roles": {"coder": 123, "Bad Name": "or:x"}}), encoding="utf-8")
        rc, _out, err = _run(["template", "import", str(bad_path)])
        ctx.check(f"non-zero exit, got {rc}", rc != 0)
        ctx.check(f"lists what is wrong, got {err!r}", "coder" in err and "Bad Name" in err)

        missing_path = Path(tempfile.mkdtemp(prefix="roles-cli-import-missing-")) / "nope.json"
        rc2, _out2, err2 = _run(["template", "import", str(missing_path)])
        ctx.check(f"a missing file is a clean error, not a traceback, got rc={rc2} err={err2!r}",
                  rc2 != 0 and "nope.json" in err2)
    finally:
        _clear_state_dir_env()


@test
def test_roles_template_import_falls_back_to_filename_when_name_missing(ctx: Ctx):
    from halo_harness.roles import load_role_template
    _fresh_state_dir("roles-cli-import-noname-")
    try:
        path = Path(tempfile.mkdtemp(prefix="roles-cli-import-noname-src-")) / "my-template.json"
        path.write_text(json.dumps({"description": "no name field", "roles": {}}), encoding="utf-8")
        rc, out, _err = _run(["template", "import", str(path)])
        ctx.check(f"import succeeds using the filename, got rc={rc} out={out!r}",
                  rc == 0 and "my-template" in out)
        ctx.check("loadable under the filename-derived name",
                  load_role_template("my-template") is not None)
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
def test_roles_template_edit_with_a_multi_word_editor_does_not_crash(ctx: Ctx):
    """2.0.2 review finding 36 pin: `EDITOR="code --wait"` used to be
    passed to `subprocess.call` as a SINGLE argv[0] (the literal string
    "code --wait", space included), raising an uncaught
    `FileNotFoundError`. `subprocess.call` is swapped out (via the
    module's own `subprocess` name) so this stays hermetic -- never
    launches a real editor -- while still proving the actual argv
    `roles_cli.py` builds."""
    import halo_harness.roles_cli as roles_cli_mod
    _fresh_state_dir("roles-cli-edit-")
    old_editor = os.environ.get("EDITOR")
    os.environ["EDITOR"] = "code --wait"
    captured = {}

    class _FakeSubprocess:
        @staticmethod
        def call(argv):
            captured["argv"] = argv
            return 0
    real_subprocess = roles_cli_mod.subprocess
    roles_cli_mod.subprocess = _FakeSubprocess
    try:
        rc, _out, err = _run(["template", "edit", "my-template"])
        ctx.check(f"no crash, exit 0, got rc={rc} err={err!r}", rc == 0)
        argv = captured.get("argv")
        ctx.check(f"'--wait' is its own argv element, got {argv}", argv is not None and "--wait" in argv)
        ctx.check(f"argv[0] is never the literal 'code --wait' string, got {argv}",
                  argv is not None and argv[0] != "code --wait")
    finally:
        roles_cli_mod.subprocess = real_subprocess
        if old_editor is None:
            os.environ.pop("EDITOR", None)
        else:
            os.environ["EDITOR"] = old_editor
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
