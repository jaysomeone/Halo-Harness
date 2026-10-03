"""halo_harness.not_yet -- the ONE canonical "this isn't built yet" message
(U0 scope A / flag-parity rule): "flags whose feature is not built yet are
parsed and answered with one line `halo: --<flag> is not supported
yet (planned: <milestone>)` on stderr -- never an argparse error". Used by
`cli.py` for flags and by `mcp_cli.py`/`config_cli.py` for subcommands
(`what` is then a phrase like "mcp add" rather than a bare flag name).

W4a: a SECOND, permanent category -- a flag whose Claude Code feature makes
no sense for a standalone harness at all (cloud sessions, IDE extension
protocol, ...) is never "planned" for a future milestone; `not_applicable_
line`/`print_not_applicable` say so with a one-line reason instead, and
`--safe-mode` (no safety heuristics exist here to disable) gets the same
treatment with its own wording.
"""

from __future__ import annotations

import sys


def not_yet_line(what: str, milestone: str) -> str:
    return f"halo: {what} is not supported yet (planned: {milestone})"


def print_not_yet(what: str, milestone: str) -> None:
    print(not_yet_line(what, milestone), file=sys.stderr)


def not_applicable_line(what: str, reason: str) -> str:
    return f"halo: {what} is not applicable to a standalone harness ({reason})"


def print_not_applicable(what: str, reason: str) -> None:
    print(not_applicable_line(what, reason), file=sys.stderr)
