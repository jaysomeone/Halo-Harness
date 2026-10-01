"""tests.test_cli_flags -- halo_harness/cli.py (U0 scope A): the flag-parity
rule. Parses `docs/harness/claude-help-2.1.281.txt` (the top-level `claude
--help` section only -- `mcp`/`mcp add` have their own, separate flag
surface, not this one) and asserts every long option there is accepted by
`halo`'s parser; not-yet flags print the one stderr line and the
prompt still runs; `--permission-mode manual` behaves exactly like
`default`; the positional prompt works with `-p` before OR after it;
`proxy --version` delegates; an actually-unknown flag is still an argparse
error, exit 2.
"""
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream
from halo_harness.cli import _build_parser, _NOT_YET_FLAGS

REPO_DIR = Path(__file__).resolve().parent.parent
HELP_CAPTURE = REPO_DIR / "docs" / "harness" / "claude-help-2.1.281.txt"
_FLAG_LINE_RE = re.compile(r"^  ((?:-{1,2}[A-Za-z][\w-]*(?:, )?)+)")

test, TESTS = new_registry()


def _extract_top_level_long_flags() -> list:
    """Every `--long-flag` token that appears in the TOP-LEVEL `claude
    --help` section of the capture (before the first `=== ... ===` marker,
    which starts the separate `mcp`/`mcp add` sub-help sections)."""
    text = HELP_CAPTURE.read_text(encoding="utf-8")
    top = text.split("\n=== ")[0]
    flags = set()
    for line in top.splitlines():
        m = _FLAG_LINE_RE.match(line)
        if not m:
            continue
        for tok in m.group(1).split(", "):
            tok = tok.strip()
            if tok.startswith("--"):
                flags.add(tok)
    return sorted(flags)


@test
def test_every_long_option_in_the_help_capture_is_accepted(ctx: Ctx):
    flags = _extract_top_level_long_flags()
    ctx.check(f"the capture actually yielded a healthy number of flags, got {len(flags)}", len(flags) > 50)
    parser = _build_parser()
    option_actions = {opt: a for a in parser._actions for opt in a.option_strings}
    unaccepted = []
    for flag in flags:
        action = option_actions.get(flag)
        if action is None:
            unaccepted.append(flag)
            continue
        if flag in ("--help",):
            # -h/--help is BY DESIGN a SystemExit(0) action -- that IS
            # "accepted", not a parse failure.
            try:
                parser.parse_args([flag])
            except SystemExit as e:
                if e.code != 0:
                    unaccepted.append(flag)
            continue
        if getattr(action, "choices", None):
            dummy = action.choices[0]
        elif action.type in (int, float):
            dummy = "1"
        else:
            dummy = "x"
        argv = [flag] if action.const is not None or action.nargs == 0 else [flag, dummy]
        try:
            parser.parse_args(argv)
        except SystemExit:
            unaccepted.append(flag)
    ctx.check(f"every long option accepted, unaccepted: {unaccepted!r}", not unaccepted)


@test
def test_hidden_flags_also_accepted(ctx: Ctx):
    """binary facts sec.1: `--permission-prompt-tool`, `--max-turns`,
    `--system-prompt-file`, `--append-system-prompt-file` -- not in
    claude's own --help, but still real accepted flags."""
    parser = _build_parser()
    for flag, dummy in (("--permission-prompt-tool", "x"), ("--max-turns", "5"),
                         ("--system-prompt-file", "x"), ("--append-system-prompt-file", "x")):
        try:
            parser.parse_args([flag, dummy])
            ok = True
        except SystemExit:
            ok = False
        ctx.check(f"hidden flag {flag} accepted", ok)


@test
def test_unknown_flag_is_an_argparse_error_exit_2(ctx: Ctx):
    env = _hermetic_child_env()
    env["PYTHONPATH"] = str(REPO_DIR)
    result = subprocess.run([sys.executable, "-m", "halo_harness", "--totally-not-a-real-flag", "-p", "hi"],
                             env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=15)
    ctx.check(f"exit code 2, got {result.returncode}", result.returncode == 2)
    ctx.check("argparse error mentions the bad flag", "totally-not-a-real-flag" in result.stderr)


def _run_cli(fh, mock, prompt, extra_args=None, timeout=30, extra_env=None):
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
    env.update(extra_env or {})
    args = [sys.executable, "-m", "halo_harness", "-p", prompt, "--model", "or:mock/model",
            "--cwd", str(fh["proj"])] + (extra_args or [])
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


@test
def test_print_mode_never_imports_textual(ctx: Ctx):
    """U2 scope A: `import halo_harness` and `-p` must stay dependency-free
    of textual/rich -- only `tui/launch.py`'s lazy import (reached solely
    from the bare, no-`-p` branch) may ever touch them. Proven with a
    PYTHONPATH shim: a poisoned `textual.py` stub that raises ImportError
    the instant anything imports it, placed EARLIER on sys.path than the
    real installed textual package (PYTHONPATH entries are searched before
    site-packages) -- so a `-p` run completing normally is only possible if
    nothing on that path ever did `import textual`."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    with tempfile.TemporaryDirectory() as shim_dir:
        stub = Path(shim_dir) / "textual.py"
        stub.write_text(
            "raise ImportError('textual must not be imported by -p/print mode')\n", encoding="utf-8",
        )
        try:
            result = _run_cli(fh, mock, "reply with the single word pong",
                               extra_env={"PYTHONPATH": f"{shim_dir}{os.pathsep}{REPO_DIR}"})
            ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-400:]!r}", result.returncode == 0)
            ctx.check("prompt still ran (pong in stdout)", "pong" in result.stdout)
            ctx.check("the poisoned stub was never triggered", "must not be imported by -p" not in result.stderr)
        finally:
            mock.stop()


@test
def test_debug_flag_writes_a_debug_log_and_is_not_a_not_yet_notice(ctx: Ctx):
    """Found live: a blank TUI on a Linux box had no log to look at because
    `--debug` still printed the "not supported yet (planned: H8)" notice.
    `--debug` now enables DEBUG file logging at <state dir>/bridge.log and
    says where the log is; `--debug-file PATH` picks the file. Print mode
    must keep working exactly as before."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "reply with the single word pong", extra_args=["--debug"])
        ctx.check(f"exit 0 with --debug, got {result.returncode} stderr={result.stderr[-300:]!r}", result.returncode == 0)
        ctx.check("pong still answered", "pong" in result.stdout)
        ctx.check("no not-yet notice for --debug", "not supported yet" not in result.stderr)
        ctx.check("stderr names the debug log", "debug log ->" in result.stderr)
        log = Path(fh["home"]) / ".halo" / "bridge.log"
        ctx.check(f"debug log written under the state dir ({log})", log.exists() and log.stat().st_size > 0)
        ctx.check("log records the debug-enabled line", "debug logging enabled" in log.read_text(encoding="utf-8", errors="replace"))
        with tempfile.TemporaryDirectory() as d:
            custom = Path(d) / "sub" / "my.log"
            result2 = _run_cli(fh, mock, "reply with the single word pong", extra_args=["--debug-file", str(custom)])
            ctx.check(f"--debug-file: exit 0, got {result2.returncode}", result2.returncode == 0)
            ctx.check("--debug-file: the chosen file exists", custom.exists() and custom.stat().st_size > 0)
    finally:
        mock.stop()


@test
def test_chrome_flag_real_and_prompt_still_runs(ctx: Ctx):
    """H3: `--chrome` is real now (was not-yet) -- it tries to spawn the
    claude-in-chrome MCP server; a short MCP_TIMEOUT keeps this test fast
    and deterministic regardless of whether a live `claude.exe`/browser
    happens to be reachable on the machine running it. Either way the
    turn itself must complete normally: a failed/slow MCP server is
    never fatal to the session (D-CFG)."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "reply with the single word pong", extra_args=["--chrome"],
                           extra_env={"MCP_TIMEOUT": "3000"})
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-400:]!r}", result.returncode == 0)
        ctx.check("no not-yet line for --chrome any more", "--chrome is not supported yet" not in result.stderr)
        ctx.check("no crash", "Traceback" not in result.stderr)
        ctx.check("prompt still ran (pong in stdout)", "pong" in result.stdout)
    finally:
        mock.stop()


@test
def test_playwright_flag_real_and_prompt_still_runs(ctx: Ctx):
    """H3: `--playwright` is real now -- same shape as the chrome test
    above (short MCP_TIMEOUT, never fatal to the turn)."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "reply with the single word pong", extra_args=["--playwright"],
                           extra_env={"MCP_TIMEOUT": "3000"})
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-400:]!r}", result.returncode == 0)
        ctx.check("no not-yet line for --playwright any more", "--playwright is not supported yet" not in result.stderr)
        ctx.check("no crash", "Traceback" not in result.stderr)
        ctx.check("prompt still ran (pong in stdout)", "pong" in result.stdout)
    finally:
        mock.stop()


@test
def test_every_not_yet_flag_prints_its_line_and_never_crashes(ctx: Ctx):
    """A cheap, boolean-only-flag sample across the whole not-yet table
    (skipping ones that need a specific value shape) -- proves the generic
    detection loop actually fires for a WIDE variety of flags, not just the
    two the acceptance list names."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        sample = [label for flags, kwargs, label, _m in _NOT_YET_FLAGS if kwargs.get("action") == "store_true"]
        ctx.check("a healthy sample of boolean not-yet flags exists", len(sample) >= 10)
        for label in sample[:10]:
            result = _run_cli(fh, mock, "reply with the single word pong", extra_args=[label])
            ctx.check(f"{label}: exit 0, got {result.returncode}", result.returncode == 0)
            ctx.check(f"{label}: not-yet line printed", f"halo: {label} is not supported yet" in result.stderr)
            ctx.check(f"{label}: prompt still ran", "pong" in result.stdout)
    finally:
        mock.stop()


@test
def test_permission_mode_manual_equals_default(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "reply with the single word pong",
                           extra_args=["--permission-mode", "manual", "--output-format", "stream-json"])
        ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
        first_line = json.loads(result.stdout.splitlines()[0])
        ctx.check(f"init line's permissionMode normalizes manual -> default, got {first_line.get('permissionMode')!r}",
                  first_line.get("permissionMode") == "default")
    finally:
        mock.stop()


@test
def test_positional_prompt_works_before_and_after_print_flag(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        env = _hermetic_child_env()
        env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                    "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
        base = [sys.executable, "-m", "halo_harness", "--model", "or:mock/model", "--cwd", str(fh["proj"])]
        before = subprocess.run(base + ["-p", "reply with the single word pong"],
                                 env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=30)
        after = subprocess.run(base + ["reply with the single word pong", "-p"],
                                env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=30)
        ctx.check(f"'-p prompt' exits 0, got {before.returncode}", before.returncode == 0)
        ctx.check(f"'prompt -p' exits 0, got {after.returncode}", after.returncode == 0)
        ctx.check("'-p prompt' -> pong", "pong" in before.stdout)
        ctx.check("'prompt -p' -> pong", "pong" in after.stdout)
    finally:
        mock.stop()


@test
def test_version_flag_short_and_long(ctx: Ctx):
    env = _hermetic_child_env()
    env["PYTHONPATH"] = str(REPO_DIR)
    for flag in ("-v", "--version"):
        result = subprocess.run([sys.executable, "-m", "halo_harness", flag], env=env, cwd=str(REPO_DIR),
                                 capture_output=True, text=True, timeout=15)
        ctx.check(f"{flag}: exit 0, got {result.returncode}", result.returncode == 0)
        ctx.check(f"{flag}: prints halo version, got {result.stdout!r}", "halo" in result.stdout)


@test
def test_proxy_version_delegates(ctx: Ctx):
    env = _hermetic_child_env()
    env["PYTHONPATH"] = str(REPO_DIR)
    result = subprocess.run([sys.executable, "-m", "halo_harness", "proxy", "--version"], env=env,
                             cwd=str(REPO_DIR), capture_output=True, text=True, timeout=15)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check(f"delegated to bridge.py's own --version output ('claude-bridge ...'), got {result.stdout!r}",
              result.stdout.strip().startswith("claude-bridge "))


@test
def test_keyboard_interrupt_maps_to_exit_130(ctx: Ctx):
    """Unit-level: main() must catch a KeyboardInterrupt raised from
    inside run_print_mode and return 130 (POSIX Ctrl+C convention), never
    let it escape as an uncaught traceback (which Python would otherwise
    exit 1 for)."""
    import halo_harness.cli as cli_mod
    import halo_harness.headless as headless_mod

    def _raise_keyboard_interrupt(**kwargs):
        raise KeyboardInterrupt()

    original = headless_mod.run_print_mode
    headless_mod.run_print_mode = _raise_keyboard_interrupt
    try:
        rc = cli_mod.main(["-p", "anything"])
        ctx.check(f"exit code 130, got {rc}", rc == 130)
    finally:
        headless_mod.run_print_mode = original


@test
def test_real_sigint_delivers_exit_130_posix(ctx: Ctx):
    """End-to-end on a REAL process (POSIX only -- Windows Ctrl+C delivery
    to a child process needs a separate process-group dance argparse/
    subprocess don't give us portably; skipped there, matching the
    existing win32-skip pattern). Kali Linux is the primary target
    platform, so this exercises the real thing where it matters most."""
    if sys.platform == "win32":
        raise SkipTest("real SIGINT delivery to a child process isn't portable on win32")
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        env = _hermetic_child_env()
        env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                    "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
        args = [sys.executable, "-m", "halo_harness", "-p", "take a while", "--model", "or:mock/slow",
                "--cwd", str(fh["proj"])]
        proc = subprocess.Popen(args, env=env, cwd=str(REPO_DIR), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        # NEW from H9b (verified flake): a fixed `time.sleep(0.5)` here
        # raced a cold/loaded box -- if the child hadn't finished its own
        # (heavy: textual, the mcp SDK, ...) imports and reached the mock
        # server's slow scenario by then, SIGINT arrived before the
        # harness's own signal.signal(SIGINT, ...) installed, so Python's
        # default disposition killed it outright (returncode -2, not a
        # deliberate exit(130)) -- confirmed empirically on a freshly
        # tar-synced WSL tree. Poll `mock.requests` instead: the mock
        # server records a request the INSTANT it's received, before the
        # "slow" scenario's own artificial 2s delay even starts, so this
        # is both faster and race-free.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not mock.requests:
            if proc.poll() is not None:
                raise AssertionError(f"process exited before ever reaching the mock server, "
                                      f"returncode={proc.returncode}")
            time.sleep(0.02)
        ctx.check("the child process reached the mock server before SIGINT", bool(mock.requests))
        proc.send_signal(signal.SIGINT)
        try:
            _out, _err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("process did not exit after SIGINT within 15s")
        ctx.check(f"exit code 130, got {proc.returncode}", proc.returncode == 130)
    finally:
        mock.stop()


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
