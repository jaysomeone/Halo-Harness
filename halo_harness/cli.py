"""halo_harness.cli -- command-line entry point (`halo` console
script / `python -m halo_harness`). U0 scope A: the single argparse surface
mirroring `docs/harness/claude-help-2.1.281.txt` flag-for-flag (the flag-
parity rule: every flag `claude` accepts is accepted here with the same
name/arity/meaning; a flag whose FEATURE isn't built yet is still parsed
and gets one `halo_harness.not_yet` stderr line, never an argparse error --
so an actually-unknown flag really is unknown and remains an argparse
error, exit 2). Subcommands `proxy`/`models`/`mcp`/`config`/`doctor` are
dispatched before the main parser ever sees the rest of argv (each has its
own, separate flag surface). Bare `halo` (no `-p`) opens the U2
full-screen TUI (`halo_harness/tui/launch.py`, imported lazily so `-p`/
`import halo_harness` never pull in textual); a positional PROMPT without
`-p` opens the TUI and submits it as the first turn.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from halo_harness import __version__
from halo_harness.cli_flags import cli_flags_from_args
from halo_harness.not_yet import print_not_applicable, print_not_yet
from halo_harness.providers.routing import InvalidModelError

_STDIN_CAP_BYTES = 10 * 1024 * 1024  # 10 MB, matches Claude Code's own -p stdin cap


def _cmd_proxy(rest: list) -> int:
    """`halo proxy ...` -- import bridge.py and hand it the
    remaining argv verbatim, exactly as `python bridge.py ...` would."""
    import bridge
    result = bridge.main(rest)
    return result if isinstance(result, int) else 0


# =============================================================================
# The flag table (flag-parity rule). Every entry: (flag strings, add_argument
# kwargs incl. an explicit dest=). `_REAL_FLAGS` behave identically to
# Claude Code; `_NOT_YET_FLAGS` additionally carry (canonical label,
# milestone) and get one `not_yet` stderr line the first time their value
# differs from "never touched" (see `_flag_was_set`).
# =============================================================================

_REAL_FLAGS = [
    (["-p", "--print"], dict(dest="print_mode", action="store_true")),
    (["--model"], dict(dest="model", default=None)),
    (["--small-model"], dict(dest="small_model", default=None)),
    (["--cwd"], dict(dest="cwd", default=None)),
    (["--output-format"], dict(dest="output_format", choices=["text", "json", "stream-json"], default="text")),
    (["--input-format"], dict(dest="input_format", choices=["text", "stream-json"], default="text")),
    (["--include-partial-messages"], dict(dest="include_partial_messages", action="store_true")),
    (["--max-turns"], dict(dest="max_turns", type=int, default=50)),
    (["--append-system-prompt"], dict(dest="append_system_prompt", default=None, metavar="TEXT")),
    (["--append-system-prompt-file"], dict(dest="append_system_prompt_file", default=None, metavar="FILE")),
    (["--system-prompt"], dict(dest="system_prompt", default=None, metavar="PROMPT")),
    (["--system-prompt-file"], dict(dest="system_prompt_file", default=None, metavar="FILE")),
    (["--settings"], dict(dest="settings", default=None, metavar="JSON_OR_PATH")),
    (["--setting-sources"], dict(dest="setting_sources", default=None, metavar="SOURCES")),
    (["--effort"], dict(dest="effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)),
    (["--verbose"], dict(dest="verbose", action="store_true")),
    (["--version", "-v"], dict(dest="version", action="store_true")),
    (["--allowedTools", "--allowed-tools"], dict(dest="allowed_tools", default=None, metavar="TOOLS")),
    (["--disallowedTools", "--disallowed-tools"], dict(dest="disallowed_tools", default=None, metavar="TOOLS")),
    (["--permission-mode"], dict(dest="permission_mode", default=None,
        choices=["default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions", "manual"])),
    # W4a: `--allow-dangerously-skip-permissions` is Claude Code's own alias
    # for this same flag ("as an option, without it being enabled by
    # default" -- this harness has always treated the two identically, so
    # the second flag string is the whole fix).
    (["--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"],
        dict(dest="dangerously_skip_permissions", action="store_true")),
    (["--tools"], dict(dest="tools", default=None, metavar="TOOLS")),
    (["--max-budget-usd"], dict(dest="max_budget_usd", type=float, default=None)),
    (["--json-schema"], dict(dest="json_schema", default=None, metavar="SCHEMA")),
    (["--add-dir"], dict(dest="add_dir", nargs="+", default=None, metavar="DIRECTORY")),
    (["--theme"], dict(dest="theme", default=None)),
    (["--demo"], dict(dest="demo", action="store_true")),
    (["--stress"], dict(dest="stress", type=int, default=None, metavar="N")),
    (["--bare"], dict(dest="bare", action="store_true")),
    (["--disable-slash-commands"], dict(dest="disable_slash_commands", action="store_true")),
    (["--session-id"], dict(dest="session_id", default=None, metavar="UUID")),
    (["--replay-user-messages"], dict(dest="replay_user_messages", action="store_true")),
    # H3: real now (were "not yet" -- MCP client + frozen catalog/lazy
    # load + the claude-in-chrome/playwright dynamic servers land this
    # milestone).
    (["--chrome"], dict(dest="chrome", action="store_true")),
    (["--no-chrome"], dict(dest="no_chrome", action="store_true")),
    (["--playwright"], dict(dest="playwright", action="store_true")),
    (["--playwright-cdp"], dict(dest="playwright_cdp", default=None, metavar="ENDPOINT")),
    (["--playwright-headless"], dict(dest="playwright_headless", action="store_true")),
    (["--mcp-config"], dict(dest="mcp_config", nargs="+", default=None, metavar="CONFIG")),
    (["--strict-mcp-config"], dict(dest="strict_mcp_config", action="store_true")),
    # H13 Part B: forces the plain type/size/dimensions caption for every
    # image tool result this run, same as `images: "caption"`/`"off"` in
    # ~/.halo/config.json but for just this one invocation -- the
    # TUI only (print mode has no inline-image concept to disable).
    (["--no-inline-images"], dict(dest="no_inline_images", action="store_true")),
    # 2.0.0 Launch intro: skips the one-time "I am just a copy, of a copy,
    # of a copy..." typewriter line a fresh interactive launch otherwise
    # shows -- same effect as `"intro": false` in ~/.halo/config.json, see
    # tui/launch.py's own show_intro resolution.
    (["--no-intro"], dict(dest="no_intro", action="store_true")),
    # H6: real now (agent definitions + the Agent tool + plan mode +
    # sessions land this milestone).
    (["--agent"], dict(dest="agent", default=None, metavar="AGENT")),
    (["--agents"], dict(dest="agents", default=None, metavar="JSON_OR_FILE")),
    # V2c (H15), widened Halo 2.0.2: repeatable NAME=MODEL[:EFFORT] override
    # for one of the ten built-in roles (or a currently-known custom one)
    # (orchestrator|coder|reviewer|researcher|small) -- validated in main()
    # right after parsing, so a bad NAME=MODEL is a clean exit-2 usage error
    # before either run_print_mode or the TUI ever starts building a Session.
    (["--role"], dict(dest="role", action="append", default=None, metavar="NAME=MODEL[:EFFORT]")),
    (["-c", "--continue"], dict(dest="continue_", action="store_true")),
    (["-r", "--resume"], dict(dest="resume", nargs="?", const="", default=None)),
    (["--fork-session"], dict(dest="fork_session", action="store_true")),
    (["-n", "--name"], dict(dest="name", default=None)),
    # H8 scope B: real now for a local path (attached as a log snapshot
    # before the first turn; image when the model has vision, text
    # otherwise). Claude Code's own file_id:relative_path cloud-resource
    # form gets a clear "not available here" notice instead -- see
    # headless.attach_cli_files.
    (["--file"], dict(dest="file", nargs="+", default=None, metavar="SPEC")),
    # Real now: `-d/--debug [FILTER]` turns on DEBUG file logging for the
    # whole run (TUI or print mode) at <state dir>/bridge.log, `--debug-file
    # PATH` picks the file. Found missing live: a blank TUI on a Linux box
    # had no log to look at because --debug still printed the "planned"
    # notice. The optional FILTER value is accepted for Claude Code parity
    # and ignored (everything is logged).
    (["-d", "--debug"], dict(dest="debug", nargs="?", const="", default=None)),
    (["--debug-file"], dict(dest="debug_file", default=None, metavar="PATH")),
    # W4a: real now -- the 21 flags the 2.0.1 gap list named as
    # implementable in a standalone harness (plans/2.0.1-w4-plan.md W4a
    # item 2). Each one's actual behaviour is wired in `_cli_flags_from_
    # args` below plus `headless.build_session`/`agent/loop.Session`; a few
    # reuse an existing knob outright (`--autocompact` is `CLAUDE_CODE_
    # AUTO_COMPACT_WINDOW`, already read by `agent/compact.resolve_knobs`).
    (["--autocompact"], dict(dest="autocompact", default=None, metavar="AUTO_OR_TOKENS")),
    (["--ax-screen-reader"], dict(dest="ax_screen_reader", action="store_true")),
    (["--bg", "--background"], dict(dest="background", action="store_true")),
    (["--betas"], dict(dest="betas", nargs="+", default=None, metavar="BETA")),
    (["--brief"], dict(dest="brief", action="store_true")),
    # W4a: repurposed for a standalone harness (no cloud sessions exist to
    # run "on a self-hosted environment") -- one or more KEY=VALUE pairs
    # merged into the tool child env, the one piece of claude's own
    # `--environment` that still means something here.
    (["--environment"], dict(dest="environment", action="append", default=None, metavar="KEY=VALUE")),
    (["--exclude-dynamic-system-prompt-sections"], dict(dest="exclude_dynamic_system_prompt_sections", action="store_true")),
    (["--fallback-model"], dict(dest="fallback_model", default=None, metavar="MODEL")),
    (["--forward-subagent-text"], dict(dest="forward_subagent_text", action="store_true")),
    (["--include-hook-events"], dict(dest="include_hook_events", action="store_true")),
    (["--no-session-persistence"], dict(dest="no_session_persistence", action="store_true")),
    (["--permission-prompt-tool"], dict(dest="permission_prompt_tool", default=None, metavar="TOOL")),
    (["--permission-prompts"], dict(dest="permission_prompts", choices=["host", "none"], default=None)),
    (["--plugin-dir"], dict(dest="plugin_dir", action="append", default=None, metavar="PATH")),
    (["--plugin-url"], dict(dest="plugin_url", action="append", default=None, metavar="URL")),
    (["--prompt-suggestions"], dict(dest="prompt_suggestions", nargs="?", const="true", default=None,
        choices=["true", "false", "1", "0", "yes", "no", "on", "off"])),
    (["--restricted"], dict(dest="restricted", action="store_true")),
    (["--system-prompt-snapshot"], dict(dest="system_prompt_snapshot", choices=["on", "off"], default=None)),
    (["--tmux"], dict(dest="tmux", nargs="?", const="default", default=None)),
    (["-w", "--worktree"], dict(dest="worktree", nargs="?", const="", default=None)),
]

# W4a: the 7 flags whose Claude Code feature is a cloud/IDE concept with no
# standalone-harness equivalent -- parsed and answered with one `not_
# applicable_line` (never "planned", since there is no future milestone
# that would change this), both here and in docs/COMMANDS.md.
_NOT_APPLICABLE_FLAGS = [
    (["--cloud"], dict(dest="cloud", nargs="?", const="", default=None),
        "--cloud", "halo has no cloud session service -- every session runs on this machine"),
    (["--teleport"], dict(dest="teleport", nargs="?", const="", default=None),
        "--teleport", "teleport sessions are a claude.ai cloud feature halo has no equivalent of"),
    (["--remote-control"], dict(dest="remote_control", nargs="?", const="", default=None),
        "--remote-control", "Remote Control pairs a session with the claude.ai mobile/web app, which halo does not integrate with"),
    (["--remote-control-session-name-prefix"], dict(dest="remote_control_session_name_prefix", default=None),
        "--remote-control-session-name-prefix", "only meaningful alongside --remote-control, which is not applicable here"),
    (["--from-pr"], dict(dest="from_pr", nargs="?", const="", default=None),
        "--from-pr", "resuming a session linked to a PR is a claude.ai cloud-session feature halo does not have"),
    (["--ide"], dict(dest="ide", action="store_true"),
        "--ide", "needs Anthropic's IDE extension protocol, which halo does not implement"),
    # D-CFG/no-cyber-blocks: Halo has no safety/refusal heuristics to
    # disable by design -- accepted for CLI compatibility, no effect
    # whatsoever (never gated, never even inspected past this point).
    (["--safe-mode"], dict(dest="safe_mode", action="store_true"),
        "--safe-mode", "halo has no safety heuristics to disable by design -- accepted for compatibility, no effect"),
]

# Kept as an empty list rather than removed outright: `_build_parser`/`main`
# below still iterate it uniformly alongside `_NOT_APPLICABLE_FLAGS`, so a
# FUTURE flag that is genuinely not-yet-built again (a new Claude Code
# release) has a ready home and a test (`test_cli_flags.py`) already
# asserting the invariant "every entry here really does print the not-yet
# line, never an argparse error" without needing to reinvent the mechanism.
_NOT_YET_FLAGS = []


def _enable_debug_logging(debug_file: Optional[str]) -> None:
    """`--debug` / `--debug-file PATH`: DEBUG file logging for the whole run,
    TUI or print mode. Default file = <state dir>/bridge.log (rotating,
    secrets redacted by RedactingFormatter); `--debug-file` picks the path.
    Never raises -- a logging problem must not stop the harness."""
    import logging
    try:
        from halo_harness.config.paths import bridge_home
        from halo_harness.providers.config import RedactingFormatter, setup_logging
        if debug_file:
            log_path = Path(debug_file).expanduser()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            logger = logging.getLogger("bridge")
            for hdlr in logger.handlers[:]:
                logger.removeHandler(hdlr)
            handler = logging.FileHandler(str(log_path), encoding="utf-8")
            handler.setFormatter(RedactingFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
            logger.addHandler(handler)
            logger.propagate = False
        else:
            logger = setup_logging(bridge_home())
            log_path = bridge_home() / "bridge.log"
        logger.setLevel(logging.DEBUG)
        for name in ("halo_harness", "halo_harness.tui", "halo_harness.agent", "halo_harness.mcp"):
            child = logging.getLogger(name)
            child.setLevel(logging.DEBUG)
            if not child.handlers:
                for hdlr in logger.handlers:
                    child.addHandler(hdlr)
        logger.debug("debug logging enabled (halo %s, argv=%s)", __version__, sys.argv[1:])
        print(f"halo: debug log -> {log_path}", file=sys.stderr)
        # 2.0.1 launch-hang investigation: one [timeline] line per startup
        # phase (settings, instructions, session build, MCP discovery,
        # first paint), each with milliseconds elapsed since THIS call --
        # see halo_harness/debug_timeline.py. A no-op cost everywhere else
        # (`mark()` is a bool check unless `enable()` ran).
        from halo_harness import debug_timeline
        debug_timeline.enable()
    except Exception as e:  # pragma: no cover - defensive
        print(f"halo: could not enable debug logging: {e}", file=sys.stderr)


def _flag_was_set(value) -> bool:
    """True iff argparse gave `value` something other than an untouched
    flag's own default (every not-yet flag above is defined with default
    None/False, so this one check works uniformly for store_true, nargs="?"
    with a const, nargs="+"/append lists, and plain single values alike)."""
    return value not in (None, False)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="halo", add_help=True,
        description="halo - starts an interactive session by default, use -p/--print for non-interactive output",
        epilog="Commands: init, proxy, mcp, models, config, doctor, update, stats, improve, export, "
               "roles, completion "
               "(run `halo <command> --help`; `halo init` sets up a fresh box in one go)",
    )
    try:
        parser._positionals.title = "Arguments"
        parser._optionals.title = "Options"
    except AttributeError:
        pass
    parser.add_argument("prompt", nargs="?", default=None, metavar="PROMPT",
                         help="The prompt text; reads stdin (UTF-8, 10 MB cap) if omitted")
    for flags, kwargs in _REAL_FLAGS:
        parser.add_argument(*flags, **kwargs)
    for flags, kwargs, _label, _milestone in _NOT_YET_FLAGS:
        parser.add_argument(*flags, **kwargs)
    for flags, kwargs, _label, _reason in _NOT_APPLICABLE_FLAGS:
        parser.add_argument(*flags, **kwargs)
    return parser


def _make_streams_utf8_safe() -> None:
    """A model's reply may contain any Unicode character; a Windows
    console/pipe commonly defaults stdout/stderr to a legacy codepage that
    can't represent most of them -- reconfigure to UTF-8 with lossy-safe
    replacement, guarded since reconfigure() doesn't exist on every stream
    (e.g. a test harness's io.StringIO)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _read_stdin_prompt() -> "tuple[Optional[str], Optional[str]]":
    """Read stdin as UTF-8 with a 10 MB cap. Returns (text, error) -- exactly
    one is non-None. Reads `.buffer` in bytes mode and decodes ourselves so
    a legacy-codepage terminal's default text-mode codec never mangles a
    piped UTF-8 prompt/stream-json line."""
    buf = getattr(sys.stdin, "buffer", None)
    if buf is not None:
        raw = buf.read(_STDIN_CAP_BYTES + 1)
        if len(raw) > _STDIN_CAP_BYTES:
            return None, f"stdin prompt exceeds the {_STDIN_CAP_BYTES // (1024 * 1024)} MB cap"
        return raw.decode("utf-8", "replace"), None
    return sys.stdin.read(), None  # fallback for a replaced (non-buffer) stdin, e.g. some test harnesses


def _parse_stream_json_lines(raw: str) -> list:
    """One JSON object per non-empty line; a malformed line is skipped
    (best-effort, matches history.py's own JSONL tolerance)."""
    out = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _apply_update_and_relaunch(argv: list) -> int:
    """Halo 2.0.2 round 6 `/update`: `run_tui(args)` just returned
    `update.RESTART_EXIT_CODE` -- the TUI's own Textual app has already
    exited (screen torn down, terminal restored) by the time `run_tui`
    returns at all, so only NOW is it safe to run the reinstall: the
    Windows order is always quit, then update, then relaunch, never
    update while any halo process (this one included) still has the
    install open. A failed update still relaunches the OLD halo with
    `--continue`, same as a successful one -- the session must resume
    either way, never strand the user at a dead prompt."""
    from halo_harness.update import relaunch_halo
    from halo_harness.update_cli import apply_update
    try:
        # Finding 2: this process's own Textual app has ALREADY exited
        # (see this function's own docstring above) -- the ONLY thing
        # `other_halo_pids`'s refusal could still be catching at this
        # exact point is this same invocation's own uv/pipx console-
        # script launcher parent (now excluded by `_ancestor_pids`, but
        # `force=True` here too so this specific, already-past-the-TUI
        # handoff never refuses on account of itself either way).
        apply_update(force=True)
    except Exception as e:
        print(f"halo: update failed: {e}", file=sys.stderr)
    relaunch_args = [a for a in argv if a not in ("--continue", "-c")]
    return relaunch_halo(["--continue", *relaunch_args])


def main(argv: Optional[list] = None) -> int:
    _make_streams_utf8_safe()
    try:  # best-effort: SIGTERM -> exit 143 (not available on every platform/thread)
        signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(143))
    except (ValueError, AttributeError, OSError):
        pass
    # H9 whole-tree review finding 3: SIGHUP -- what closing the terminal or
    # an SSH connection drop actually sends (there is no SIGTERM involved at
    # all) -- was never handled, so it fell through to Python's own default
    # disposition (process dies immediately, `sys.exit(143)` above never
    # runs, no `finally` block anywhere gets a chance to fire). Verified on
    # WSL: SIGHUP to a `-p` run exited -1 and left its background job
    # running. `SIGHUP` doesn't exist on Windows (`AttributeError` there,
    # caught same as SIGTERM's own guard above); raised as a real
    # `sys.exit()` (never the signal's own default disposition) so it
    # unwinds through every `finally: session.job_registry.kill_all()` /
    # `mcp_manager.close_all()` block the same way SIGTERM already does.
    try:
        signal.signal(signal.SIGHUP, lambda signum, frame: sys.exit(129))
    except (ValueError, AttributeError, OSError):
        pass
    argv = sys.argv[1:] if argv is None else list(argv)

    if argv and argv[0] == "proxy":
        return _cmd_proxy(argv[1:])
    if argv and argv[0] == "models":
        from halo_harness.catalog_cli import cmd_models
        return cmd_models(argv[1:])
    if argv and argv[0] == "mcp":
        from halo_harness.mcp_cli import cmd_mcp
        return cmd_mcp(argv[1:])
    if argv and argv[0] == "config":
        from halo_harness.config_cli import cmd_config
        return cmd_config(argv[1:])
    if argv and argv[0] == "doctor":
        from halo_harness.doctor import cmd_doctor
        return cmd_doctor(argv[1:])
    if argv and argv[0] == "update":
        from halo_harness.update_cli import cmd_update
        return cmd_update(argv[1:])
    if argv and argv[0] == "work-matrix":
        from halo_harness.work_matrix import cmd_work_matrix
        return cmd_work_matrix(argv[1:])
    if argv and argv[0] == "init":
        from halo_harness.init_cli import cmd_init
        return cmd_init(argv[1:])
    if argv and argv[0] == "providers":
        from halo_harness.providers_cli import cmd_providers
        return cmd_providers(argv[1:])
    if argv and argv[0] == "stats":
        from halo_harness.stats_cli import cmd_stats
        return cmd_stats(argv[1:])
    if argv and argv[0] == "improve":
        from halo_harness.improve_cli import cmd_improve
        return cmd_improve(argv[1:])
    if argv and argv[0] == "export":
        from halo_harness.export_cli import cmd_export
        return cmd_export(argv[1:])
    if argv and argv[0] == "bugreport":
        from halo_harness.bugreport import cmd_bugreport
        return cmd_bugreport(argv[1:])
    if argv and argv[0] == "timeline":
        from halo_harness.bugreport_timeline_cli import cmd_timeline
        return cmd_timeline(argv[1:])
    if argv and argv[0] == "worktree":
        from halo_harness.worktree_cli import cmd_worktree
        return cmd_worktree(argv[1:])
    if argv and argv[0] == "bg":
        from halo_harness.bg_cli import cmd_bg
        return cmd_bg(argv[1:])
    if argv and argv[0] == "roles":
        from halo_harness.roles_cli import cmd_roles
        return cmd_roles(argv[1:])
    if argv and argv[0] == "ollama":
        from halo_harness.ollama_cli import cmd_ollama
        return cmd_ollama(argv[1:])
    if argv and argv[0] == "gym":
        from halo_harness.gym_cli import cmd_gym
        return cmd_gym(argv[1:])
    if argv and argv[0] == "local":
        from halo_harness.local_cli import cmd_local
        return cmd_local(argv[1:])
    if argv and argv[0] == "org":
        from halo_harness.org_cli import cmd_org
        return cmd_org(argv[1:])
    if argv and argv[0] == "setup":
        from halo_harness.setup_cli import cmd_setup
        return cmd_setup(argv[1:])
    if argv and argv[0] == "completion":
        from halo_harness.completion_cli import cmd_completion
        return cmd_completion(argv[1:])

    parser = _build_parser()
    try:
        args = parser.parse_intermixed_args(argv)
    except TypeError:
        # documented limitation of parse_intermixed_args on some
        # optional/positional shapes; fall back rather than crash (only
        # changes behavior for argv shapes that would have been ambiguous
        # anyway -- an genuinely-unknown flag still raises SystemExit(2)
        # from WITHIN parse_intermixed_args itself, before this except ever runs).
        args, _ = parser.parse_known_args(argv)

    if args.version:
        # Halo 2.0.2 round 6: `(commit, branch)` once the install's own
        # commit is known (PEP 610 direct_url.json, or git in a live
        # checkout) -- see update.installed_build/format_version_line.
        try:
            from halo_harness.update import format_version_line, installed_build
            print(format_version_line(installed_build()))
        except Exception:
            print(f"halo {__version__}")
        return 0

    for _flags, kwargs, label, milestone in _NOT_YET_FLAGS:
        if _flag_was_set(getattr(args, kwargs["dest"])):
            print_not_yet(label, milestone)
    for _flags, kwargs, label, reason in _NOT_APPLICABLE_FLAGS:
        if _flag_was_set(getattr(args, kwargs["dest"])):
            print_not_applicable(label, reason)

    if _flag_was_set(getattr(args, "debug", None)) or getattr(args, "debug_file", None):
        _enable_debug_logging(getattr(args, "debug_file", None))

    # review finding 34: `--autocompact <auto|tokens>` used to write
    # straight into `os.environ["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]`, the
    # LOWEST (shell) layer of `settings.effective_env` -- a settings.json
    # `env` block setting the SAME name silently outranked this explicit
    # flag. The raw value already reaches `cli_flags["autocompact"]`
    # (`cli_flags_from_args`) unconditionally; `agent/compact.resolve_
    # knobs` now parses it itself and gives it top precedence, so nothing
    # needs doing here at all any more.

    if getattr(args, "role", None):
        # V2c (H15): validated ONCE, here, before either run_print_mode or
        # the TUI ever starts building a Session -- a bad NAME=MODEL is a
        # clean exit-2 usage error, same treatment as a bad --session-id.
        from halo_harness.roles import parse_role_flags
        try:
            parse_role_flags(args.role)
        except ValueError as e:
            print(f"halo: {e}", file=sys.stderr)
            return 2

    if args.demo and args.print_mode:
        from halo_harness.testing.fake_controller import run_demo
        demo_format = args.output_format if args.output_format in ("text", "json") else "text"
        return run_demo(output_format=demo_format, stress=args.stress)

    # W4a: `--bg`/`--background` -- spawn THIS SAME invocation (minus the
    # flag itself, and forced to -p since a detached child has no terminal
    # a TUI could render into) as a detached background process, print its
    # id + log path, and return immediately. A scoped-down v1 of Claude
    # Code's own `--bg` (no `attach`/`logs`/`stop`/`rm` subcommands yet --
    # the log file and the OS's own process tools cover the same ground
    # for this round; see the worker report for what's left).
    if _flag_was_set(getattr(args, "background", False)):
        from halo_harness.bg_run import start_background_run
        filtered = [a for a in argv if a not in ("--bg", "--background")]
        info = start_background_run(filtered)
        print(f"halo: started in the background, id={info['id']}", file=sys.stderr)
        print(f"halo: log -> {info['log_path']}", file=sys.stderr)
        return 0

    if not args.print_mode:
        # U2: bare `halo [PROMPT]` and `halo --demo` (without
        # -p) both open the full-screen TUI; textual/rich become real
        # imports only from this lazy import down. A full-screen session
        # needs a real terminal to render into and read keys from -- stdin
        # not being a tty (piped/redirected input, or a subprocess with no
        # console at all, e.g. a headless test or CI runner) means nothing
        # could ever drive it, so this is a deterministic one-line notice
        # + exit 2 rather than Textual hanging trying to set up a terminal
        # that doesn't exist.
        if not sys.stdin.isatty():
            print("halo: a full-screen session requires an interactive terminal "
                  "(stdin is not a tty) -- use -p/--print for a non-interactive run",
                  file=sys.stderr)
            return 2
        # W4a: `--tmux` -- a simpler, tmux-only reading of Claude Code's own
        # version (no iTerm2 native-pane backend here): re-exec this same
        # launch inside a new tmux window when tmux exists and we are not
        # ALREADY inside one (`$TMUX` set) -- never an error when tmux is
        # missing, just a notice and the ordinary in-process launch.
        if _flag_was_set(getattr(args, "tmux", None)) and "TMUX" not in os.environ:
            from halo_harness.bg_run import run_in_tmux
            tmux_argv = [a for a in argv if a != "--tmux" and not a.startswith("--tmux=")]
            code = run_in_tmux(tmux_argv)
            if code is not None:
                return code
            print("halo: --tmux requires the `tmux` binary on PATH -- continuing without it",
                  file=sys.stderr)
        # W4a: `--ax-screen-reader` -- plain, line-oriented output instead
        # of the full Textual UI (flat text, no borders/animations/spinners).
        if _flag_was_set(getattr(args, "ax_screen_reader", False)):
            from halo_harness.ax_mode import run_ax_screen_reader_mode
            return run_ax_screen_reader_mode(args)
        from halo_harness.tui.launch import run_tui
        code = run_tui(args)
        from halo_harness.update import RESTART_EXIT_CODE
        if code == RESTART_EXIT_CODE:
            return _apply_update_and_relaunch(argv)
        return code

    system_prompt_text = args.system_prompt
    if args.system_prompt_file and system_prompt_text is None:
        try:
            system_prompt_text = Path(args.system_prompt_file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"halo: could not read --system-prompt-file: {e}", file=sys.stderr)
            return 2
    append_system_prompt_text = args.append_system_prompt
    if args.append_system_prompt_file:
        try:
            file_text = Path(args.append_system_prompt_file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"halo: could not read --append-system-prompt-file: {e}", file=sys.stderr)
            return 2
        append_system_prompt_text = f"{append_system_prompt_text}\n\n{file_text}" if append_system_prompt_text else file_text

    setting_sources = [s.strip() for s in args.setting_sources.split(",") if s.strip()] if args.setting_sources else None

    stdin_lines = None
    prompt_text = args.prompt
    if args.input_format == "stream-json":
        # finding 10 (major, h4-h5-h3c review): stdin is read
        # INCREMENTALLY, on a background thread, by run_print_mode itself
        # -- reading it all here, to EOF, before the first turn even
        # starts, deadlocked an SDK-style client that writes one line and
        # waits for that turn's `result` before writing the next one.
        # `stdin_lines=None` for stream-json now just means "headless.py
        # owns stdin for this run", not "nothing was piped".
        pass
    elif prompt_text is None:
        if sys.stdin.isatty():
            print("halo: -p requires a prompt (inline or piped via stdin)", file=sys.stderr)
            return 2
        prompt_text, err = _read_stdin_prompt()
        if err is not None:
            print(f"halo: {err}", file=sys.stderr)
            return 2
        if not prompt_text:
            print("halo: -p requires a prompt (inline or piped via stdin)", file=sys.stderr)
            return 2

    from halo_harness.headless import run_print_mode
    try:
        return run_print_mode(
            prompt=prompt_text,
            model_ref_raw=args.model,
            small_model_ref_raw=args.small_model,
            cwd=Path(args.cwd) if args.cwd else None,
            output_format=args.output_format,
            input_format=args.input_format,
            max_turns=args.max_turns,
            append_system_prompt=append_system_prompt_text,
            system_prompt=system_prompt_text,
            settings_flag=args.settings,
            setting_sources=setting_sources,
            verbose=args.verbose,
            effort=getattr(args, "effort", None),
            allowed_tools=args.allowed_tools,
            disallowed_tools=args.disallowed_tools,
            permission_mode=args.permission_mode,
            dangerously_skip_permissions=args.dangerously_skip_permissions,
            tools=args.tools,
            add_dir=args.add_dir,
            bare=args.bare,
            disable_slash_commands=args.disable_slash_commands,
            session_id=args.session_id,
            include_partial_messages=args.include_partial_messages,
            max_budget_usd=args.max_budget_usd,
            json_schema=args.json_schema,
            replay_user_messages=args.replay_user_messages,
            stdin_lines=stdin_lines,
            chrome=args.chrome, no_chrome=args.no_chrome, playwright=args.playwright,
            playwright_cdp=args.playwright_cdp, playwright_headless=args.playwright_headless,
            mcp_config=args.mcp_config, strict_mcp_config=args.strict_mcp_config,
            continue_=bool(getattr(args, "continue_", False)), resume=getattr(args, "resume", None),
            fork_session_flag=bool(getattr(args, "fork_session", False)),
            agent=getattr(args, "agent", None), agents_flag=getattr(args, "agents", None),
            roles_flag=getattr(args, "role", None),
            name=getattr(args, "name", None), file_specs=getattr(args, "file", None),
            cli_flags=cli_flags_from_args(args),
        )
    except InvalidModelError as e:
        # a bad --model/alias must be a clean config error (exit 2), not an
        # uncaught traceback (exit 1 is reserved for a request that ran and
        # failed, not a bad invocation).
        print(f"halo: invalid --model: {e}", file=sys.stderr)
        if args.model:
            from halo_harness.catalog_cli import near_miss_slug
            from halo_harness.config.paths import bridge_home
            from halo_harness.providers.databricks import load_models_json
            try:
                known = load_models_json(bridge_home())
                matches = near_miss_slug(args.model, known)
                if matches:
                    print(f"halo: did you mean: {', '.join('or:' + m for m in matches)}", file=sys.stderr)
            except Exception:
                pass
        return 2
    except KeyboardInterrupt:
        # Ctrl+C during a print-mode turn: POSIX exit-code convention
        # (128 + SIGINT's 2 = 130), never an uncaught-traceback exit 1.
        print("\nhalo: interrupted", file=sys.stderr)
        return 130


def main_deprecated_alias(argv: Optional[list] = None) -> int:
    """`rolo-claude`'s deprecated-alias entry point: one notice line to
    stderr, then exactly `main(argv)` -- same process, same parser, same
    behavior, so every flag/subcommand (including `rolo-claude proxy`)
    keeps working under the old name. Never prints the notice more than
    once per process (there's only ever one entry into argv handling per
    invocation).

    2.0.1: no longer a `pyproject.toml` console-script entry point (an old,
    separately-installed `rolo-claude` tool owning that name made a fresh
    `uv tool install --editable .` of Halo fail outright -- see CHANGELOG
    [2.0.1]). `bin/rolo-claude` (the no-install-at-all POSIX fallback) does
    NOT call this -- it prints its own notice and execs `bin/halo` directly
    -- this function is kept only as a small, still-tested, directly
    callable building block for any future reuse."""
    print("halo: 'rolo-claude' is deprecated, use 'halo' instead", file=sys.stderr)
    return main(argv)


if __name__ == "__main__":
    sys.exit(main())
