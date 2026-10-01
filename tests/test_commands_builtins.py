"""tests.test_commands_builtins -- halo_harness/commands/builtins.py's
headless-facade behaviour for each of the ~22 built-in slash commands (U0
scope B), plus a couple of true end-to-end checks through the real CLI
(`-p "/cost"`, `-p "/help"`) proving the full wiring in headless.py works
without ever needing a model (core-kind commands short-circuit before any
network call).
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from halo_harness.commands.registry import Registry
from halo_harness.commands.builtins import HeadlessFacade

test, TESTS = new_registry()

REPO_DIR = Path(__file__).resolve().parent.parent


def _facade_for(fh, **overrides):
    registry = Registry.discover(fh["proj"], fh["home"])
    kwargs = dict(cwd=fh["proj"], registry=registry, model_ref="or:mock/model", permission_mode="default")
    kwargs.update(overrides)
    return HeadlessFacade(**kwargs), registry


@test
def test_cost_line_format(ctx: Ctx):
    fh = build_fake_home()
    facade, registry = _facade_for(fh, cost_usd=0.1234, num_turns=3)
    out = registry.resolve("cost").run("", facade)
    ctx.check(f"cost line mentions the dollar figure and turn count, got {out!r}",
              "$0.1234" in out and "3 turn" in out)


@test
def test_model_command_shows_current_model_and_effort(ctx: Ctx):
    fh = build_fake_home()
    facade, registry = _facade_for(fh, effort="high")
    out = registry.resolve("model").run("", facade)
    ctx.check("model shown", "or:mock/model" in out)
    ctx.check("effort shown", "high" in out)


@test
def test_help_lists_every_builtin(ctx: Ctx):
    fh = build_fake_home()
    facade, registry = _facade_for(fh)
    out = registry.resolve("help").run("", facade)
    for name in ("cost", "model", "mcp", "doctor", "theme", "permissions", "status"):
        ctx.check(f"/help mentions /{name}", f"/{name}" in out)


@test
def test_theme_show_and_set(ctx: Ctx):
    fh = build_fake_home()
    facade, registry = _facade_for(fh, theme="claude-dark")
    shown = registry.resolve("theme").run("", facade)
    ctx.check(f"shows current theme, got {shown!r}", "claude-dark" in shown)

    old = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    try:
        result = registry.resolve("theme").run("claude-light-ansi", facade)
        ctx.check(f"confirms the set, got {result!r}", "claude-light-ansi" in result)
        from halo_harness import theme as theme_mod
        ctx.check("persisted for real to ~/.halo/config.json",
                  theme_mod.load_persisted_theme() == "claude-light-ansi")
    finally:
        if old is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old


@test
def test_theme_rejects_invalid_name_without_raising(ctx: Ctx):
    fh = build_fake_home()
    facade, registry = _facade_for(fh)
    out = registry.resolve("theme").run("not-a-theme", facade)
    ctx.check(f"friendly error text, not a traceback, got {out!r}", "not a valid theme" in out)


@test
def test_add_dir_without_args_shows_usage(ctx: Ctx):
    fh = build_fake_home()
    facade, registry = _facade_for(fh)
    out = registry.resolve("add-dir").run("", facade)
    ctx.check("usage hint shown", "Usage:" in out)


@test
def test_doctor_slash_command_reuses_doctor_module(ctx: Ctx):
    fh = build_fake_home()
    facade, registry = _facade_for(fh)
    out = registry.resolve("doctor").run("", facade)
    ctx.check("mentions Python (a real doctor check ran)", "Python" in out)


@test
def test_init_is_prompt_kind_with_nonempty_body(ctx: Ctx):
    fh = build_fake_home()
    facade, registry = _facade_for(fh)
    cmd = registry.resolve("init")
    ctx.check("kind is prompt", cmd.kind == "prompt")
    out = cmd.run("", facade)
    ctx.check("mentions CLAUDE.md", "CLAUDE.md" in out)


@test
def test_ui_kind_commands_still_return_friendly_text(ctx: Ctx):
    fh = build_fake_home()
    facade, registry = _facade_for(fh)
    for name in ("clear", "plan", "resume", "export", "exit"):
        cmd = registry.resolve(name)
        ctx.check(f"/{name} is kind=ui", cmd.kind == "ui")
        out = cmd.run("", facade)
        ctx.check(f"/{name} (ui kind) returns non-empty text instead of erroring", bool(out.strip()))


@test
def test_compact_is_now_core_kind_and_handles_no_session(ctx: Ctx):
    """H5 scope B: /compact fully executes (kind="core") -- a real
    compaction with a live Session attached (see test_compact_command_runs_
    a_real_compaction in test_loop_h2_integration-adjacent coverage), or a
    clear "nothing to compact" message for a bare facade with none."""
    fh = build_fake_home()
    facade, registry = _facade_for(fh)
    cmd = registry.resolve("compact")
    ctx.check("/compact is kind=core (fully executes, not a UI stub)", cmd.kind == "core")
    out = cmd.run("", facade)
    ctx.check(f"no session attached -> the honest no-history message, got {out!r}",
              "no prior history" in out.lower())


# ---- e2e through the real CLI (core kind never reaches a model) -----------

def _run_cli(fh, prompt: str, extra_args=None, timeout=30):
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "PYTHONPATH": str(REPO_DIR)})
    args = [sys.executable, "-m", "halo_harness", "-p", prompt, "--cwd", str(fh["proj"])] + (extra_args or [])
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


@test
def test_e2e_cost_via_cli(ctx: Ctx):
    fh = build_fake_home()
    result = _run_cli(fh, "/cost")
    ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-300:]!r}", result.returncode == 0)
    ctx.check(f"stdout has a cost figure, got {result.stdout!r}", "$" in result.stdout)


@test
def test_e2e_help_via_cli(ctx: Ctx):
    fh = build_fake_home()
    result = _run_cli(fh, "/help")
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("stdout lists commands", "Available commands" in result.stdout)


@test
def test_e2e_unrecognized_slash_falls_through_without_crashing(ctx: Ctx):
    fh = build_fake_home()
    result = _run_cli(fh, "/this-is-not-a-real-command", extra_args=["--model", "or:mock/model"])
    ctx.check("no Python traceback leaked to stderr", "Traceback" not in result.stderr)
    ctx.check(f"exit code is a clean failure (no creds configured), got {result.returncode}", result.returncode != 0)


@test
def test_e2e_disable_slash_commands_sends_literal_text(ctx: Ctx):
    fh = build_fake_home()
    result = _run_cli(fh, "/cost", extra_args=["--disable-slash-commands", "--model", "or:mock/model"])
    ctx.check("no Python traceback leaked to stderr", "Traceback" not in result.stderr)
    ctx.check("did NOT print the /cost builtin's own line format",
              "Total cost: $0.0000 across 0 turn(s)" not in result.stdout)


def _hermetic_child_env() -> dict:
    """2.0.0 fixpass item G: never forward a stray BRIDGE_STATE_DIR
    (would let bridge_home() escape this test's own BRIDGE_TEST_HOME
    scoping) or HALO_* (would out-rank the legacy BRIDGE_* name a
    fixture deliberately sets, per env_compat's own precedence) from
    the parent process into a spawned child -- same hermeticity
    tests/test_init_cli.py::_run already has, applied at each of this
    file's own `env = dict(os.environ)` call sites."""
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    return env


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
