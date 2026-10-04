"""tests.test_org_cli -- Halo 2.0.2 round D: `halo org install|export|
import|resume`, the standalone `halo org ...` CLI entry points
(`halo_harness.org_cli`) layered over `halo_harness.orgs`'s own CRUD --
`tests/test_orgs.py` already covers the backing functions directly; this
file pins the argparse/stdout/stderr plumbing on top, same split
`test_roles_cli.py` already uses for `halo roles template ...`.
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
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()

test, TESTS = new_registry()


def _fresh_state_dir(prefix: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    return d


def _clear_state_dir_env() -> None:
    os.environ.pop("BRIDGE_STATE_DIR", None)


def _run(argv: list) -> "tuple[int, str, str]":
    from halo_harness.org_cli import cmd_org
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = StringIO(), StringIO()
    try:
        rc = cmd_org(argv)
        return rc, sys.stdout.getvalue(), sys.stderr.getvalue()
    finally:
        sys.stdout, sys.stderr = old_out, old_err


# ---- list (now shows descriptions) -----------------------------------------

@test
def test_list_shows_the_readme_description(ctx: Ctx):
    _fresh_state_dir("org-cli-list-")
    try:
        rc, out, _err = _run(["list"])
        ctx.check(f"rc 0, got {rc}", rc == 0)
        ctx.check(f"solo shown with its one-line description, got {out!r}",
                  "solo -- A single orchestrator position" in out)
    finally:
        _clear_state_dir_env()


# ---- install ----------------------------------------------------------------

@test
def test_install_shipped_then_refuses_then_force(ctx: Ctx):
    _fresh_state_dir("org-cli-install-")
    try:
        rc, out, _err = _run(["install", "release-flow"])
        ctx.check(f"install succeeds, got rc={rc} out={out!r}", rc == 0 and "release-flow" in out)
        rc2, _out2, err2 = _run(["install", "release-flow"])
        ctx.check(f"refused the second time, got rc={rc2} err={err2!r}", rc2 == 1 and "--force" in err2)
        rc3, out3, _err3 = _run(["install", "release-flow", "--force"])
        ctx.check(f"--force succeeds, got rc={rc3} out={out3!r}", rc3 == 0)
        rc4, _out4, err4 = _run(["install", "not-a-template"])
        ctx.check(f"unknown template is a clean error, got rc={rc4} err={err4!r}",
                  rc4 == 1 and "not-a-template" in err4)
    finally:
        _clear_state_dir_env()


# ---- export / import --------------------------------------------------------

@test
def test_export_to_file_and_stdout_then_import_round_trip(ctx: Ctx):
    from halo_harness.orgs import load_org
    _fresh_state_dir("org-cli-export-import-")
    try:
        _run(["new", "pair", "--description", "two-person team"])
        out_path = Path(tempfile.mkdtemp(prefix="org-cli-export-out-")) / "pair.json"
        rc, _out, err = _run(["export", "pair", str(out_path)])
        ctx.check(f"export to file succeeds, got rc={rc} err={err!r}", rc == 0 and out_path.is_file())
        exported = json.loads(out_path.read_text(encoding="utf-8"))
        ctx.check(f"exported JSON round trips the description, got {exported}",
                  exported.get("description") == "two-person team")

        rc2, out2, _err2 = _run(["export", "pair"])
        ctx.check(f"with no file, prints JSON to stdout, got rc={rc2}",
                  rc2 == 0 and json.loads(out2)["name"] == "pair")

        rc3, out3, _err3 = _run(["import", str(out_path)])
        ctx.check(f"import succeeds, got rc={rc3} out={out3!r}", rc3 == 0 and "pair" in out3)
        reimported = load_org("pair")
        ctx.check("re-imported org matches the original", reimported["description"] == "two-person team")
    finally:
        _clear_state_dir_env()


@test
def test_import_rejects_invalid_shape_listing_every_problem(ctx: Ctx):
    _fresh_state_dir("org-cli-import-invalid-")
    try:
        bad_path = Path(tempfile.mkdtemp(prefix="org-cli-import-bad-")) / "bad.json"
        bad_path.write_text(json.dumps({"positions": [
            {"title": "A", "role": "not-a-real-role", "reports": ["Ghost"]},
        ]}), encoding="utf-8")
        rc, _out, err = _run(["import", str(bad_path)])
        ctx.check(f"non-zero exit, got {rc}", rc != 0)
        ctx.check(f"lists role AND reports problems, got {err!r}", "not-a-real-role" in err and "Ghost" in err)
    finally:
        _clear_state_dir_env()


@test
def test_import_falls_back_to_filename_when_name_missing(ctx: Ctx):
    from halo_harness.orgs import load_org
    _fresh_state_dir("org-cli-import-noname-")
    try:
        path = Path(tempfile.mkdtemp(prefix="org-cli-import-noname-src-")) / "my-org.json"
        path.write_text(json.dumps({"positions": [{"title": "Lead", "reports": []}]}), encoding="utf-8")
        rc, out, _err = _run(["import", str(path)])
        ctx.check(f"import succeeds using the filename, got rc={rc} out={out!r}", rc == 0 and "my-org" in out)
        ctx.check("loadable under the filename-derived name", load_org("my-org") is not None)
    finally:
        _clear_state_dir_env()


# ---- resume (brief item 4) --------------------------------------------------

@test
def test_resume_cli_resolves_the_session_and_calls_resume_org_run(ctx: Ctx):
    """`halo org resume <session-id>` -- `resume_org_run` itself is
    stubbed out (never a real model dispatch here; that path is covered
    end-to-end with a MockUpstream in tests/test_orgs.py) so this test
    stays hermetic and pins ONLY the session-id -> session_dir resolution
    step, the same split test_init_wizard_round7_integration.py's own
    `test_org_run_cli_with_no_name_uses_the_default` already uses for
    `halo org run`."""
    import halo_harness.agent.subagent as subagent_mod
    from halo_harness.agent.sessions import sessions_dir
    from halo_harness.org_cli import cmd_org
    from halo_harness.tools.base import ToolResult
    _fresh_state_dir("org-cli-resume-")
    cwd = Path(tempfile.mkdtemp(prefix="org-cli-resume-cwd-"))
    try:
        sdir = sessions_dir(cwd)
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "abc123.jsonl").write_text("", encoding="utf-8")

        captured = {}
        real_resume = subagent_mod.resume_org_run

        def _fake_resume_org_run(*, runtime, tool_id, tool_name, session_dir, on_event=None):
            captured["session_dir"] = session_dir
            captured["tool_id"] = tool_id
            return [], ToolResult("stubbed resume")

        subagent_mod.resume_org_run = _fake_resume_org_run
        try:
            rc = cmd_org(["resume", "abc123", "--cwd", str(cwd)])
        finally:
            subagent_mod.resume_org_run = real_resume
        ctx.check(f"exit 0, got {rc}", rc == 0)
        ctx.check(f"resolved the right session's own session_dir, got {captured}",
                  captured.get("session_dir") is not None and captured["session_dir"].name == "abc123")
    finally:
        _clear_state_dir_env()


@test
def test_resume_cli_unknown_session_id_refuses_cleanly(ctx: Ctx):
    from halo_harness.org_cli import cmd_org
    _fresh_state_dir("org-cli-resume-unknown-")
    cwd = Path(tempfile.mkdtemp(prefix="org-cli-resume-unknown-cwd-"))
    try:
        rc, _out, err = _run(["resume", "no-such-session", "--cwd", str(cwd)])
        ctx.check(f"non-zero exit, got {rc}", rc == 2)
        ctx.check(f"a clean message, not a traceback, got {err!r}", "no-such-session" in err)
    finally:
        _clear_state_dir_env()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
