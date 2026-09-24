"""rolo_claude.cli -- command-line entry point (`rolo-claude` console
script / `python -m rolo_claude`). U0 scope A: the single argparse surface
mirroring `docs/harness/claude-help-2.1.281.txt` flag-for-flag (the flag-
parity rule: every flag `claude` accepts is accepted here with the same
name/arity/meaning; a flag whose FEATURE isn't built yet is still parsed
and gets one `rolo_claude.not_yet` stderr line, never an argparse error --
so an actually-unknown flag really is unknown and remains an argparse
error, exit 2). Subcommands `proxy`/`models`/`mcp`/`config`/`doctor` are
dispatched before the main parser ever sees the rest of argv (each has its
own, separate flag surface). Bare `rolo-claude` (no `-p`) prints
"TUI arrives in U2" and exits 2 -- the TUI itself is U2's job.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Optional

from rolo_claude import __version__
from rolo_claude.not_yet import print_not_yet
from rolo_claude.providers.routing import InvalidModelError

_STDIN_CAP_BYTES = 10 * 1024 * 1024  # 10 MB, matches Claude Code's own -p stdin cap


def _cmd_proxy(rest: list) -> int:
    """`rolo-claude proxy ...` -- import bridge.py and hand it the
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
    (["--dangerously-skip-permissions"], dict(dest="dangerously_skip_permissions", action="store_true")),
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
]

_NOT_YET_FLAGS = [
    (["--agent"], dict(dest="agent", default=None, metavar="AGENT"), "--agent", "H6"),
    (["--agents"], dict(dest="agents", default=None, metavar="JSON_OR_FILE"), "--agents", "H6"),
    (["--allow-dangerously-skip-permissions"], dict(dest="allow_dangerously_skip_permissions", action="store_true"),
        "--allow-dangerously-skip-permissions", "H4"),
    (["--autocompact"], dict(dest="autocompact", default=None, metavar="AUTO_OR_TOKENS"), "--autocompact", "H5"),
    (["--ax-screen-reader"], dict(dest="ax_screen_reader", action="store_true"), "--ax-screen-reader", "U2"),
    (["--bg", "--background"], dict(dest="background", action="store_true"), "--bg", "H8"),
    (["--betas"], dict(dest="betas", nargs="+", default=None, metavar="BETA"), "--betas", "H8"),
    (["--brief"], dict(dest="brief", action="store_true"), "--brief", "H4"),
    (["--chrome"], dict(dest="chrome", action="store_true"), "--chrome", "H3"),
    (["--cloud"], dict(dest="cloud", nargs="?", const="", default=None), "--cloud", "H8"),
    (["-c", "--continue"], dict(dest="continue_", action="store_true"), "--continue", "H6"),
    (["-d", "--debug"], dict(dest="debug", nargs="?", const="", default=None), "--debug", "H8"),
    (["--debug-file"], dict(dest="debug_file", default=None, metavar="PATH"), "--debug-file", "H8"),
    (["--environment"], dict(dest="environment_id", default=None, metavar="ENVIRONMENT_ID"), "--environment", "H8"),
    (["--exclude-dynamic-system-prompt-sections"], dict(dest="exclude_dynamic_system_prompt_sections", action="store_true"),
        "--exclude-dynamic-system-prompt-sections", "H5"),
    (["--fallback-model"], dict(dest="fallback_model", default=None, metavar="MODEL"), "--fallback-model", "H6"),
    (["--file"], dict(dest="file", nargs="+", default=None, metavar="SPEC"), "--file", "H8"),
    (["--fork-session"], dict(dest="fork_session", action="store_true"), "--fork-session", "H6"),
    (["--forward-subagent-text"], dict(dest="forward_subagent_text", action="store_true"), "--forward-subagent-text", "H6"),
    (["--from-pr"], dict(dest="from_pr", nargs="?", const="", default=None), "--from-pr", "H8"),
    (["--ide"], dict(dest="ide", action="store_true"), "--ide", "H8"),
    (["--include-hook-events"], dict(dest="include_hook_events", action="store_true"), "--include-hook-events", "H4"),
    (["--mcp-config"], dict(dest="mcp_config", nargs="+", default=None, metavar="CONFIG"), "--mcp-config", "H3"),
    (["-n", "--name"], dict(dest="name", default=None), "--name", "H6"),
    (["--no-chrome"], dict(dest="no_chrome", action="store_true"), "--no-chrome", "H3"),
    (["--no-session-persistence"], dict(dest="no_session_persistence", action="store_true"), "--no-session-persistence", "H6"),
    (["--permission-prompt-tool"], dict(dest="permission_prompt_tool", default=None, metavar="TOOL"), "--permission-prompt-tool", "H4"),
    (["--permission-prompts"], dict(dest="permission_prompts", choices=["host", "none"], default=None), "--permission-prompts", "H4"),
    (["--plugin-dir"], dict(dest="plugin_dir", action="append", default=None, metavar="PATH"), "--plugin-dir", "H4"),
    (["--plugin-url"], dict(dest="plugin_url", action="append", default=None, metavar="URL"), "--plugin-url", "H4"),
    (["--playwright"], dict(dest="playwright", action="store_true"), "--playwright", "H3"),
    (["--prompt-suggestions"], dict(dest="prompt_suggestions", nargs="?", const="true", default=None,
        choices=["true", "false", "1", "0", "yes", "no", "on", "off"]), "--prompt-suggestions", "U3"),
    (["--remote-control"], dict(dest="remote_control", nargs="?", const="", default=None), "--remote-control", "H8"),
    (["--remote-control-session-name-prefix"], dict(dest="remote_control_session_name_prefix", default=None),
        "--remote-control-session-name-prefix", "H8"),
    (["--restricted"], dict(dest="restricted", action="store_true"), "--restricted", "H4"),
    (["-r", "--resume"], dict(dest="resume", nargs="?", const="", default=None), "--resume", "H6"),
    (["--safe-mode"], dict(dest="safe_mode", action="store_true"), "--safe-mode", "H4"),
    (["--strict-mcp-config"], dict(dest="strict_mcp_config", action="store_true"), "--strict-mcp-config", "H3"),
    (["--system-prompt-snapshot"], dict(dest="system_prompt_snapshot", choices=["on", "off"], default=None),
        "--system-prompt-snapshot", "H5"),
    (["--teleport"], dict(dest="teleport", nargs="?", const="", default=None), "--teleport", "H8"),
    (["--tmux"], dict(dest="tmux", nargs="?", const="default", default=None), "--tmux", "H8"),
    (["-w", "--worktree"], dict(dest="worktree", nargs="?", const="", default=None), "--worktree", "H8"),
]


def _flag_was_set(value) -> bool:
    """True iff argparse gave `value` something other than an untouched
    flag's own default (every not-yet flag above is defined with default
    None/False, so this one check works uniformly for store_true, nargs="?"
    with a const, nargs="+"/append lists, and plain single values alike)."""
    return value not in (None, False)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rolo-claude", add_help=True,
        description="rolo-claude - starts an interactive session by default, use -p/--print for non-interactive output",
        epilog="Commands: proxy, mcp, models, config, doctor (run `rolo-claude <command> --help`)",
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


def main(argv: Optional[list] = None) -> int:
    _make_streams_utf8_safe()
    try:  # best-effort: SIGTERM -> exit 143 (not available on every platform/thread)
        signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(143))
    except (ValueError, AttributeError, OSError):
        pass
    argv = sys.argv[1:] if argv is None else list(argv)

    if argv and argv[0] == "proxy":
        return _cmd_proxy(argv[1:])
    if argv and argv[0] == "models":
        from rolo_claude.catalog_cli import cmd_models
        return cmd_models(argv[1:])
    if argv and argv[0] == "mcp":
        from rolo_claude.mcp_cli import cmd_mcp
        return cmd_mcp(argv[1:])
    if argv and argv[0] == "config":
        from rolo_claude.config_cli import cmd_config
        return cmd_config(argv[1:])
    if argv and argv[0] == "doctor":
        from rolo_claude.doctor import cmd_doctor
        return cmd_doctor(argv[1:])

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
        print(f"rolo-claude {__version__}")
        return 0

    for _flags, kwargs, label, milestone in _NOT_YET_FLAGS:
        if _flag_was_set(getattr(args, kwargs["dest"])):
            print_not_yet(label, milestone)

    if args.demo:
        from rolo_claude.testing.fake_controller import run_demo
        demo_format = args.output_format if args.output_format in ("text", "json") else "text"
        return run_demo(output_format=demo_format, stress=args.stress)

    if not args.print_mode:
        print("TUI arrives in U2", file=sys.stderr)
        return 2

    system_prompt_text = args.system_prompt
    if args.system_prompt_file and system_prompt_text is None:
        try:
            system_prompt_text = Path(args.system_prompt_file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"rolo-claude: could not read --system-prompt-file: {e}", file=sys.stderr)
            return 2
    append_system_prompt_text = args.append_system_prompt
    if args.append_system_prompt_file:
        try:
            file_text = Path(args.append_system_prompt_file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"rolo-claude: could not read --append-system-prompt-file: {e}", file=sys.stderr)
            return 2
        append_system_prompt_text = f"{append_system_prompt_text}\n\n{file_text}" if append_system_prompt_text else file_text

    setting_sources = [s.strip() for s in args.setting_sources.split(",") if s.strip()] if args.setting_sources else None

    stdin_lines = None
    prompt_text = args.prompt
    if args.input_format == "stream-json":
        raw, err = _read_stdin_prompt()
        if err is not None:
            print(f"rolo-claude: {err}", file=sys.stderr)
            return 2
        stdin_lines = _parse_stream_json_lines(raw)
    elif prompt_text is None:
        if sys.stdin.isatty():
            print("rolo-claude: -p requires a prompt (inline or piped via stdin)", file=sys.stderr)
            return 2
        prompt_text, err = _read_stdin_prompt()
        if err is not None:
            print(f"rolo-claude: {err}", file=sys.stderr)
            return 2
        if not prompt_text:
            print("rolo-claude: -p requires a prompt (inline or piped via stdin)", file=sys.stderr)
            return 2

    from rolo_claude.headless import run_print_mode
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
        )
    except InvalidModelError as e:
        # a bad --model/alias must be a clean config error (exit 2), not an
        # uncaught traceback (exit 1 is reserved for a request that ran and
        # failed, not a bad invocation).
        print(f"rolo-claude: invalid --model: {e}", file=sys.stderr)
        if args.model:
            from rolo_claude.catalog_cli import near_miss_slug
            from rolo_claude.config.paths import bridge_home
            from rolo_claude.providers.databricks import load_models_json
            try:
                known = load_models_json(bridge_home())
                matches = near_miss_slug(args.model, known)
                if matches:
                    print(f"rolo-claude: did you mean: {', '.join('or:' + m for m in matches)}", file=sys.stderr)
            except Exception:
                pass
        return 2
    except KeyboardInterrupt:
        # Ctrl+C during a print-mode turn: POSIX exit-code convention
        # (128 + SIGINT's 2 = 130), never an uncaught-traceback exit 1.
        print("\nrolo-claude: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
