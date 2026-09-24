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
    parser.add_argument("-p", "--print", dest="prompt", metavar="PROMPT", nargs="?", const="", default=None,
                         help="Run one prompt in print (non-interactive) mode; reads stdin if no PROMPT is given")
    parser.add_argument("--model", default=None, help="Model ref for the main model (or:, dbx:, ant:, vendor/model, or an alias)")
    parser.add_argument("--small-model", default=None, help="Model ref for the small/fast model (accepted, not yet used in H0)")
    parser.add_argument("--cwd", default=None, help="Working directory for config discovery (default: process cwd)")
    parser.add_argument("--output-format", default="text", choices=["text", "json"],
                         help="text (default) or json (H0 supports only these two; stream-json is a later milestone)")
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--append-system-prompt", default=None, metavar="TEXT")
    parser.add_argument("--settings", default=None, metavar="JSON_OR_PATH")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="store_true")
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


def main(argv: Optional[list] = None) -> int:
    _make_streams_utf8_safe()
    argv = sys.argv[1:] if argv is None else list(argv)

    if argv and argv[0] == "proxy":
        return _cmd_proxy(argv[1:])

    parser = _build_parser()
    args, _remaining = parser.parse_known_args(argv)

    if args.version:
        print(f"rolo-claude {__version__}")
        return 0

    if args.prompt is None:
        print("TUI not built yet (U2)", file=sys.stderr)
        return 2

    prompt_text = args.prompt
    if not prompt_text:
        if not sys.stdin.isatty():
            prompt_text = sys.stdin.read()
        if not prompt_text:
            print("rolo-claude: -p requires a prompt (inline or piped via stdin)", file=sys.stderr)
            return 2

    from rolo_claude.headless import run_print_mode
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
    )


if __name__ == "__main__":
    sys.exit(main())
