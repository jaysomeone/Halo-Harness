"""rolo_claude.cli -- command-line entry point (`rolo-claude` console
script / `python -m rolo_claude`). H0 scope, kept deliberately small (U0
extends this with the full flag surface, commands registry, history,
theme, and --demo):

  * `rolo-claude proxy ...`        -> delegates to bridge.py's own main()
                                       (today's proxy launcher/server,
                                       unchanged behavior).
  * `rolo-claude -p PROMPT ...`    -> one print-mode turn through the H0
                                       agent loop (see headless.py).
  * anything else (bare `rolo-claude`, or a prompt with no -p)
                                       -> "TUI not built yet" notice, exit 2
                                       (the TUI itself is U2's job; per
                                       plan, bare `rolo-claude` should
                                       eventually behave like plain `claude`).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from rolo_claude import __version__
from rolo_claude.providers.routing import InvalidModelError

_STDIN_CAP_BYTES = 10 * 1024 * 1024  # 10 MB, matches Claude Code's own -p stdin cap


def _cmd_proxy(rest: list) -> int:
    """`rolo-claude proxy ...` -- import bridge.py (the file sitting beside
    this package, per the packaging layout) and hand it the remaining argv
    verbatim, exactly as if the user had run `python bridge.py ...` (or the
    installed `claude-bridge`/`rolo-claude` proxy alias) themselves."""
    import bridge
    result = bridge.main(rest)
    return result if isinstance(result, int) else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rolo-claude", add_help=True)
    # finding 7: `-p`/`--print` is a plain boolean flag (like real `claude`),
    # NOT an option that swallows the next token as its value -- the prompt
    # is always the separate `prompt` positional, so it works regardless of
    # whether it comes before or after `-p` (`rolo-claude -p "q"` and
    # `rolo-claude "q" -p` both worked before this fix only ate one shape;
    # `-p --output-format json "q"` didn't work at all).
    parser.add_argument("-p", "--print", dest="print_mode", action="store_true",
                         help="Run one prompt in print (non-interactive) mode")
    parser.add_argument("prompt", nargs="?", default=None, metavar="PROMPT",
                         help="The prompt text; reads stdin (UTF-8, 10 MB cap) if omitted")
    parser.add_argument("--model", default=None, help="Model ref for the main model (or:, dbx:, ant:, vendor/model, or an alias)")
    parser.add_argument("--small-model", default=None, help="Model ref for the small/fast model (accepted, not yet used in H0)")
    parser.add_argument("--cwd", default=None, help="Working directory for config discovery (default: process cwd)")
    parser.add_argument("--output-format", default="text", choices=["text", "json"],
                         help="text (default) or json (H0 supports only these two; stream-json is a later milestone)")
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--append-system-prompt", default=None, metavar="TEXT")
    parser.add_argument("--settings", default=None, metavar="JSON_OR_PATH")
    parser.add_argument("--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"],
                         help="Reasoning effort, mapped per provider profile (reasoning.effort on "
                              "OpenRouter, reasoning_effort on Databricks, thinking budget on Messages routes)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="store_true")
    # H2 scope D: permission-engine flags. A single string value (comma or
    # space separated internally, e.g. "Bash(git *) Edit" -- binary facts
    # sec.1's own example), never argparse nargs="+", so a rule containing
    # spaces inside Tool(...) parens is never split across argv tokens by
    # the shell/argparse before rolo-claude ever sees it as one string.
    parser.add_argument("--allowedTools", "--allowed-tools", dest="allowed_tools", default=None, metavar="TOOLS")
    parser.add_argument("--disallowedTools", "--disallowed-tools", dest="disallowed_tools", default=None, metavar="TOOLS")
    parser.add_argument("--permission-mode", default=None,
                         choices=["default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions", "manual"],
                         help="Permission mode (manual is the display alias for default)")
    parser.add_argument("--dangerously-skip-permissions", action="store_true",
                         help="Run with permission mode bypassPermissions (allow everything except an explicit deny rule)")
    parser.add_argument("--tools", default=None, metavar="TOOLS",
                         help='"" for none, "default" (or omit) for all, or a name list e.g. "Bash,Edit,Read"')
    return parser


def _make_streams_utf8_safe() -> None:
    """A model's reply is free to contain any Unicode character (an arrow,
    an em dash, ...); Windows consoles/pipes commonly default stdout/stderr
    to a legacy codepage (cp1252) that can't represent most of them, which
    previously crashed print mode mid-reply with UnicodeEncodeError. Python
    3.7+'s TextIOWrapper.reconfigure lets us switch to UTF-8 with lossy-safe
    replacement instead of failing outright; guarded because reconfigure()
    doesn't exist on every stream (e.g. some test harnesses replace stdout
    with a plain io.StringIO)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _read_stdin_prompt() -> "tuple[Optional[str], Optional[str]]":
    """Read a prompt from stdin as UTF-8 with a 10 MB cap. Returns
    (text, error_message) -- exactly one is non-None. Never raises: a
    UnicodeDecodeError from a legacy-codepage terminal reading raw bytes is
    what finding 7 reproduced (piped UTF-8 prompts turning into mojibake or
    raising outright) -- reading `.buffer` in bytes mode and decoding
    ourselves sidesteps the platform's default text-mode codec entirely."""
    buf = getattr(sys.stdin, "buffer", None)
    if buf is not None:
        raw = buf.read(_STDIN_CAP_BYTES + 1)
        if len(raw) > _STDIN_CAP_BYTES:
            return None, f"stdin prompt exceeds the {_STDIN_CAP_BYTES // (1024 * 1024)} MB cap"
        return raw.decode("utf-8", "replace"), None
    return sys.stdin.read(), None  # fallback for a replaced (non-buffer) stdin, e.g. some test harnesses


def main(argv: Optional[list] = None) -> int:
    _make_streams_utf8_safe()
    argv = sys.argv[1:] if argv is None else list(argv)

    if argv and argv[0] == "proxy":
        return _cmd_proxy(argv[1:])
    if argv and argv[0] == "models":
        from rolo_claude.catalog_cli import cmd_models
        return cmd_models(argv[1:])

    parser = _build_parser()
    try:
        args = parser.parse_intermixed_args(argv)
    except TypeError:
        # parse_intermixed_args raises TypeError on a shape it can't handle
        # (documented limitation); fall back to plain parsing rather than
        # crash -- this only changes behavior for argv shapes that would
        # have been ambiguous anyway.
        args, _ = parser.parse_known_args(argv)

    if args.version:
        print(f"rolo-claude {__version__}")
        return 0

    if not args.print_mode:
        print("TUI not built yet (U2)", file=sys.stderr)
        return 2

    prompt_text = args.prompt
    if prompt_text is None:
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
            max_turns=args.max_turns,
            append_system_prompt=args.append_system_prompt,
            settings_flag=args.settings,
            verbose=args.verbose,
            effort=getattr(args, "effort", None),
            allowed_tools=args.allowed_tools,
            disallowed_tools=args.disallowed_tools,
            permission_mode=args.permission_mode,
            dangerously_skip_permissions=args.dangerously_skip_permissions,
            tools=args.tools,
        )
    except InvalidModelError as e:
        # finding 7: a bad --model/alias must be a clean config error (exit
        # 2), not an uncaught traceback (which argparse-style CLIs reserve
        # exit 1 for -- a request that ran and failed, not a bad invocation).
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


if __name__ == "__main__":
    sys.exit(main())
