"""tests.test_h15_path_check -- H15 addendum (owner report from the work
VM, 2026-09-30): a bare `halo` typed OUTSIDE the checkout directory
did not work, because the installed console script was never put on PATH
-- only the checkout's own `bin/halo` wrapper had ever been used,
from inside the checkout. Pins `doctor.check_command_on_path`/
`reinstall_command` (the shared check `doctor`'s own check list and
`init`'s own Summary step both render) and the `init` summary wiring.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


class _Env:
    """No test here builds a real Session/Controller, but every module in
    this suite scopes BRIDGE_TEST_HOME/BRIDGE_STATE_DIR regardless (house
    rule) -- cheap insurance against a future check in this same function
    starting to read config.json."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE", "BRIDGE_TEST_CC_AUTH_STATUS",
                        "OPENROUTER_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "TYPESAFE_API_KEY")}
        d = Path(tempfile.mkdtemp(prefix="h15-path-check-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        # Never spawn a real `claude auth status` subprocess from
        # `_check_entries`/`_check_providers_enabled` -- same seam
        # tests/test_init_cli.py documents.
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = '{"loggedIn": false}'
        for k in ("OPENROUTER_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                  "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "TYPESAFE_API_KEY"):
            os.environ.pop(k, None)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _console():
    from rich.console import Console
    # A wide, fixed width -- Rich wraps long lines at its own narrower
    # default when `file` isn't a real terminal, which would otherwise
    # break a plain substring check against a long WARN/fix line.
    return Console(file=io.StringIO(), width=200)


# ---------------------------------------------------------------------------
# doctor.reinstall_command / _detect_install_tool
# ---------------------------------------------------------------------------

@test
def test_reinstall_command_prefers_uv_when_present(ctx: Ctx):
    from halo_harness.doctor import reinstall_command
    cmd = reinstall_command(uv_found=True)
    ctx.check(f"uv command, got {cmd!r}", cmd == "uv tool install --reinstall .")


@test
def test_reinstall_command_falls_back_to_pip(ctx: Ctx):
    from halo_harness.doctor import reinstall_command
    cmd = reinstall_command(uv_found=False, externally_managed=False)
    ctx.check(f"pip command, got {cmd!r}", cmd == "pip install --user -e .")


# ---------------------------------------------------------------------------
# 1.0.1 part 2 fixpass finding 16: no uv, PEP 668's EXTERNALLY-MANAGED
# marker present (Kali/Debian 12+) -- pipx when it's on PATH, else a venv;
# never a bare `pip install --user`.
# ---------------------------------------------------------------------------

@test
def test_reinstall_command_prefers_pipx_when_externally_managed_and_pipx_present(ctx: Ctx):
    from halo_harness.doctor import reinstall_command
    cmd = reinstall_command(uv_found=False, externally_managed=True, pipx_found=True)
    ctx.check(f"pipx command, got {cmd!r}", cmd == "pipx install --force -e .")


@test
def test_reinstall_command_falls_back_to_venv_when_externally_managed_and_no_pipx(ctx: Ctx):
    from halo_harness.doctor import reinstall_command
    cmd = reinstall_command(uv_found=False, externally_managed=True, pipx_found=False)
    ctx.check(f"a venv command, got {cmd!r}", cmd.startswith("python3 -m venv"))
    ctx.check(f"never a bare pip install --user, got {cmd!r}", "pip install --user" not in cmd)


@test
def test_reinstall_command_venv_also_links_the_console_script_into_local_bin(ctx: Ctx):
    """M5 (1.0.1 final pass): a plain venv install puts the console script
    at .venv/bin/halo, which is never on PATH by itself (unlike `uv
    tool install`/`pipx install`, which both register one) -- the fix line
    must also link it into ~/.local/bin so check_command_on_path's own
    `shutil.which("halo")` actually resolves afterward (the PATH-for-
    a-non-interactive-shell half of that is already _check_local_bin_on_
    path's own, separate fix)."""
    from halo_harness.doctor import reinstall_command
    cmd = reinstall_command(uv_found=False, externally_managed=True, pipx_found=False)
    ctx.check(f"still a venv command, got {cmd!r}", cmd.startswith("python3 -m venv"))
    ctx.check(f"links the venv's own console script, got {cmd!r}", ".venv/bin/halo" in cmd)
    ctx.check(f"into ~/.local/bin specifically, got {cmd!r}", "~/.local/bin" in cmd)


@test
def test_reinstall_command_uv_wins_even_when_externally_managed(ctx: Ctx):
    from halo_harness.doctor import reinstall_command
    cmd = reinstall_command(uv_found=True, externally_managed=True, pipx_found=False)
    ctx.check(f"uv still wins, got {cmd!r}", cmd == "uv tool install --reinstall .")


@test
def test_detect_install_tool_real_marker_check_is_a_test_seam(ctx: Ctx):
    """`BRIDGE_TEST_EXTERNALLY_MANAGED` short-circuits the real filesystem
    check entirely -- without passing `externally_managed=` explicitly, a
    caller (doctor's own real checks) still gets a deterministic answer in
    a test, on every platform, regardless of whether THIS box's own system
    Python happens to carry the marker."""
    from halo_harness.doctor import _detect_install_tool
    saved = os.environ.get("BRIDGE_TEST_EXTERNALLY_MANAGED")
    try:
        os.environ["BRIDGE_TEST_EXTERNALLY_MANAGED"] = "1"
        tool = _detect_install_tool(uv_found=False, pipx_found=True)
        ctx.check(f"pipx via the test seam, got {tool!r}", tool == "pipx")
        os.environ["BRIDGE_TEST_EXTERNALLY_MANAGED"] = "0"
        tool2 = _detect_install_tool(uv_found=False, pipx_found=True)
        ctx.check(f"plain pip via the test seam, got {tool2!r}", tool2 == "pip")
    finally:
        if saved is None:
            os.environ.pop("BRIDGE_TEST_EXTERNALLY_MANAGED", None)
        else:
            os.environ["BRIDGE_TEST_EXTERNALLY_MANAGED"] = saved


# ---------------------------------------------------------------------------
# doctor.check_command_on_path
# ---------------------------------------------------------------------------

@test
def test_not_found_on_path_warns_with_the_exact_reinstall_fix(ctx: Ctx):
    from halo_harness.doctor import MISSING, OK, WARN, check_command_on_path
    with _Env():
        line = check_command_on_path(resolved=None, uv_found=False, externally_managed=False)
        ctx.check(f"a WARN, got {line!r}", line.startswith(WARN))
        ctx.check(f"names 'not found on PATH', got {line!r}", "not found on PATH" in line)
        ctx.check(f"fix names the pip reinstall command, got {line!r}",
                  "-> fix: pip install --user -e ." in line)
        ctx.check(f"fix reminds about git pull, got {line!r}", "git pull" in line)


@test
def test_not_found_prefers_uv_reinstall_command_when_uv_is_present(ctx: Ctx):
    from halo_harness.doctor import WARN, check_command_on_path
    with _Env():
        line = check_command_on_path(resolved="", uv_found=True)
        ctx.check(f"a WARN, got {line!r}", line.startswith(WARN))
        ctx.check(f"fix names the uv reinstall command, got {line!r}",
                  "-> fix: uv tool install --reinstall ." in line)


@test
def test_repo_bin_wrapper_warns_even_though_something_resolved(ctx: Ctx):
    """The exact bug: `shutil.which` finding the checkout's OWN bin/
    wrapper must still WARN (it only works from inside the checkout) --
    "something resolved" alone is not enough to call this OK."""
    from halo_harness.doctor import WARN, _repo_bin_dir, check_command_on_path
    with _Env():
        wrapper_path = str(_repo_bin_dir() / "halo")
        line = check_command_on_path(resolved=wrapper_path, uv_found=False, externally_managed=False)
        ctx.check(f"a WARN, got {line!r}", line.startswith(WARN))
        ctx.check(f"names the checkout's own bin/ wrapper, got {line!r}",
                  "this checkout's own bin/ wrapper" in line)
        ctx.check(f"still names the exact fix command, got {line!r}",
                  "-> fix: pip install --user -e ." in line)


@test
def test_a_real_installed_script_elsewhere_is_ok(ctx: Ctx):
    from halo_harness.doctor import OK, check_command_on_path
    with _Env():
        fake_installed = str(Path(tempfile.mkdtemp(prefix="h15-fake-install-")) / "halo")
        line = check_command_on_path(resolved=fake_installed)
        ctx.check(f"an OK line naming the resolved path, got {line!r}",
                  line.startswith(OK) and fake_installed in line)


# ---------------------------------------------------------------------------
# doctor's own check list carries it.
# ---------------------------------------------------------------------------

@test
def test_doctor_check_entries_include_command_on_path(ctx: Ctx):
    from halo_harness.doctor import _check_entries
    with _Env():
        ids = [cid for cid, _line in _check_entries(Path.cwd())]
        ctx.check(f"'command_on_path' is one of doctor's own checks, got {ids}", "command_on_path" in ids)
        ctx.check(f"'providers_enabled' is one of doctor's own checks, got {ids}", "providers_enabled" in ids)


# ---------------------------------------------------------------------------
# init's own Summary step ends with the same check + the extra sentence.
# ---------------------------------------------------------------------------

@test
def test_init_summary_ends_with_the_path_check_and_extra_sentence_when_not_found(ctx: Ctx):
    import halo_harness.doctor as doctor_mod
    from halo_harness.init_cli import _step_summary
    real_check = doctor_mod.check_command_on_path
    doctor_mod.check_command_on_path = lambda **kw: real_check(resolved=None, uv_found=False,
                                                                 externally_managed=False)
    try:
        with _Env():
            console = _console()
            _step_summary(console, written=[], pong_ok=True, doctor_lines=[], no_live=True,
                           configured_this_run=["openrouter"], final_model="or:deepseek/deepseek-v4.1-flash")
            out = console.file.getvalue()
            ctx.check(f"the summary carries the command-on-path WARN, got {out!r}",
                      "halo command: not found on PATH" in out)
            ctx.check(f"the summary carries the exact fix command, got {out!r}",
                      "pip install --user -e ." in out)
            ctx.check(f"the extra 'run from any directory' sentence is present, got {out!r}",
                      "Run it from any directory" in out)
    finally:
        doctor_mod.check_command_on_path = real_check


@test
def test_init_summary_omits_the_extra_sentence_once_the_command_resolves_for_real(ctx: Ctx):
    import halo_harness.doctor as doctor_mod
    from halo_harness.init_cli import _step_summary
    real_check = doctor_mod.check_command_on_path
    doctor_mod.check_command_on_path = lambda **kw: real_check(resolved="/usr/local/bin/halo")
    try:
        with _Env():
            console = _console()
            _step_summary(console, written=[], pong_ok=True, doctor_lines=[], no_live=True,
                           configured_this_run=["openrouter"], final_model="or:deepseek/deepseek-v4.1-flash")
            out = console.file.getvalue()
            ctx.check(f"the OK line is present, got {out!r}", "/usr/local/bin/halo" in out)
            ctx.check(f"no extra sentence once it resolves cleanly, got {out!r}",
                      "Run it from any directory" not in out)
    finally:
        doctor_mod.check_command_on_path = real_check


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
