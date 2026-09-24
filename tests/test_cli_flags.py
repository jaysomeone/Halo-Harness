"""tests.test_cli_flags -- rolo_claude/cli.py (U0 scope A): the flag-parity
rule. Parses `docs/harness/claude-help-2.1.281.txt` (the top-level `claude
--help` section only -- `mcp`/`mcp add` have their own, separate flag
surface, not this one) and asserts every long option there is accepted by
`rolo-claude`'s parser; not-yet flags print the one stderr line and the
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
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream
from rolo_claude.cli import _build_parser, _NOT_YET_FLAGS

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
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_DIR)
    result = subprocess.run([sys.executable, "-m", "rolo_claude", "--totally-not-a-real-flag", "-p", "hi"],
                             env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=15)
    ctx.check(f"exit code 2, got {result.returncode}", result.returncode == 2)
    ctx.check("argparse error mentions the bad flag", "totally-not-a-real-flag" in result.stderr)


def _run_cli(fh, mock, prompt, extra_args=None, timeout=30, extra_env=None):
    env = dict(os.environ)
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
    env.update(extra_env or {})
    args = [sys.executable, "-m", "rolo_claude", "-p", prompt, "--model", "or:mock/model",
            "--cwd", str(fh["proj"])] + (extra_args or [])
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


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
            ctx.check(f"{label}: not-yet line printed", f"rolo-claude: {label} is not supported yet" in result.stderr)
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
        env = dict(os.environ)
        env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                    "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
        base = [sys.executable, "-m", "rolo_claude", "--model", "or:mock/model", "--cwd", str(fh["proj"])]
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
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_DIR)
    for flag in ("-v", "--version"):
        result = subprocess.run([sys.executable, "-m", "rolo_claude", flag], env=env, cwd=str(REPO_DIR),
                                 capture_output=True, text=True, timeout=15)
        ctx.check(f"{flag}: exit 0, got {result.returncode}", result.returncode == 0)
        ctx.check(f"{flag}: prints rolo-claude version, got {result.stdout!r}", "rolo-claude" in result.stdout)


@test
def test_proxy_version_delegates(ctx: Ctx):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_DIR)
    result = subprocess.run([sys.executable, "-m", "rolo_claude", "proxy", "--version"], env=env,
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
    import rolo_claude.cli as cli_mod
    import rolo_claude.headless as headless_mod

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
        env = dict(os.environ)
        env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                    "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
        args = [sys.executable, "-m", "rolo_claude", "-p", "take a while", "--model", "or:mock/slow",
                "--cwd", str(fh["proj"])]
        proc = subprocess.Popen(args, env=env, cwd=str(REPO_DIR), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        time.sleep(0.5)  # let it get into the (2s) slow scenario's request before interrupting
        proc.send_signal(signal.SIGINT)
        try:
            _out, _err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("process did not exit after SIGINT within 15s")
        ctx.check(f"exit code 130, got {proc.returncode}", proc.returncode == 130)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
