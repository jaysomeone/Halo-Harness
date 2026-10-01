"""tests.test_hotfix_101_permission_mode -- 1.0.1 hotfix 18: the starting
permission mode is a first-class `init` choice (rolo runs in `auto` and
never wants Claude Code's own `default` on his own boxes), with a new
`~/.rolo-claude/config.json` `permission_mode` layer in the precedence
chain: `--dangerously-skip-permissions` > `--permission-mode` >
config.json's `permission_mode` (NEW) > settings.json's `permissions.
defaultMode` > the hardcoded `default`. Also covers `doctor`'s own
effective-mode-and-source line.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


class _Env:
    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE")}
        home = Path(tempfile.mkdtemp(prefix="hotfix101-permmode-"))
        os.environ["BRIDGE_TEST_HOME"] = str(home)
        os.environ["BRIDGE_STATE_DIR"] = str(home / ".rolo-claude")
        os.environ["BRIDGE_ENV_FILE"] = str(home / "no-env-file")
        self.home = home
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_config_json_permission_mode_wins_over_hardcoded_default(ctx: Ctx):
    from rolo_claude import headless
    from rolo_claude.theme import set_config_value
    with _Env() as env:
        set_config_value("permission_mode", "auto")
        build = headless.build_session(cwd=env.home, bare=True, print_mode=True)
        ctx.check(f"config.json's permission_mode wins over the hardcoded default, got "
                  f"{build.session.permission_engine.mode!r}", build.session.permission_engine.mode == "auto")


@test
def test_explicit_permission_mode_flag_wins_over_config_json(ctx: Ctx):
    from rolo_claude import headless
    from rolo_claude.theme import set_config_value
    with _Env() as env:
        set_config_value("permission_mode", "auto")
        build = headless.build_session(cwd=env.home, bare=True, print_mode=True, permission_mode="plan")
        ctx.check(f"--permission-mode beats config.json, got {build.session.permission_engine.mode!r}",
                  build.session.permission_engine.mode == "plan")


@test
def test_dangerously_skip_permissions_wins_over_everything(ctx: Ctx):
    from rolo_claude import headless
    from rolo_claude.theme import set_config_value
    with _Env() as env:
        set_config_value("permission_mode", "plan")
        build = headless.build_session(cwd=env.home, bare=True, print_mode=True, permission_mode="default",
                                        dangerously_skip_permissions=True)
        ctx.check(f"--dangerously-skip-permissions beats every other layer, got "
                  f"{build.session.permission_engine.mode!r}", build.session.permission_engine.mode == "bypassPermissions")


@test
def test_no_config_json_key_falls_through_to_hardcoded_default(ctx: Ctx):
    """Unchanged pre-1.0.1 behavior when the new layer simply isn't set --
    proves the new elif doesn't accidentally shadow the existing chain."""
    from rolo_claude import headless
    with _Env() as env:
        build = headless.build_session(cwd=env.home, bare=True, print_mode=True)
        ctx.check(f"falls through to default, got {build.session.permission_engine.mode!r}",
                  build.session.permission_engine.mode == "default")


@test
def test_doctor_reports_the_effective_mode_and_its_source(ctx: Ctx):
    from rolo_claude.doctor import run_checks
    from rolo_claude.theme import set_config_value
    old_cwd = Path.cwd()
    with _Env() as env:
        try:
            os.chdir(env.home)
            set_config_value("permission_mode", "auto")
            lines, _ok = run_checks(env.home)
        finally:
            os.chdir(old_cwd)
        matches = [l for l in lines if "Permission mode" in l]
        ctx.check(f"a permission-mode line is present, got {lines}", len(matches) == 1)
        ctx.check(f"names the effective mode, got {matches}", "auto" in matches[0])
        ctx.check(f"names the source, got {matches}", "config.json" in matches[0])


@test
def test_init_yes_writes_nothing_for_permission_mode_on_a_fresh_box(ctx: Ctx):
    """1.0.1 H14c fixpass finding 9 (supersedes this file's own hotfix
    18.1 test, which pinned the bug finding 9 reports): a non-interactive
    (`--yes`) run on a FRESH box -- nothing chosen, nothing already
    configured -- writes NOTHING for `permission_mode` any more, instead
    of forcing `auto` over whatever settings.json's own `permissions.
    defaultMode` might already say for this project."""
    import json
    import subprocess
    home = Path(tempfile.mkdtemp(prefix="hotfix101-permmode-init-"))
    repo_dir = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env.update({"BRIDGE_TEST_HOME": str(home), "PYTHONPATH": str(repo_dir),
                "BRIDGE_TEST_CC_AUTH_STATUS": json.dumps({"loggedIn": False})})
    result = subprocess.run(
        [sys.executable, "-m", "rolo_claude", "init", "--provider", "openrouter", "--yes", "--no-live"],
        env=env, cwd=str(repo_dir), capture_output=True, text=True, encoding="utf-8", errors="replace",
        input="sk-or-fake-key-999\n", timeout=40,
    )
    ctx.check(f"init exited 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    cfg_path = home / ".rolo-claude" / "config.json"
    ctx.check(f"config.json exists at {cfg_path}", cfg_path.exists())
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    ctx.check(f"permission_mode is NOT written on a fresh --yes run, got {cfg.get('permission_mode')!r}",
              "permission_mode" not in cfg)


@test
def test_init_yes_keeps_an_existing_permission_mode(ctx: Ctx):
    """The other half of finding 9: --yes on a box that ALREADY has a
    permission_mode configured (e.g. a re-run) must leave it exactly as
    it was, never downgrade/overwrite it to auto."""
    import json
    import subprocess
    home = Path(tempfile.mkdtemp(prefix="hotfix101-permmode-init-"))
    (home / ".rolo-claude").mkdir(parents=True, exist_ok=True)
    (home / ".rolo-claude" / "config.json").write_text(json.dumps({"permission_mode": "plan"}), encoding="utf-8")
    repo_dir = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env.update({"BRIDGE_TEST_HOME": str(home), "PYTHONPATH": str(repo_dir),
                "BRIDGE_TEST_CC_AUTH_STATUS": json.dumps({"loggedIn": False})})
    result = subprocess.run(
        [sys.executable, "-m", "rolo_claude", "init", "--provider", "openrouter", "--yes", "--no-live"],
        env=env, cwd=str(repo_dir), capture_output=True, text=True, encoding="utf-8", errors="replace",
        input="sk-or-fake-key-999\n", timeout=40,
    )
    ctx.check(f"init exited 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    cfg_path = home / ".rolo-claude" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    ctx.check(f"permission_mode still says 'plan', never forced to auto, got {cfg.get('permission_mode')!r}",
              cfg.get("permission_mode") == "plan")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
