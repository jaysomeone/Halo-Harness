"""halo_harness.debug_timeline -- 2.0.1 launch-hang investigation: an
opt-in, one-line-per-phase startup timeline (settings, instructions,
session build, MCP discovery, first paint), each with a millisecond
elapsed-since-start figure, so a hang on a real box can be localized to a
phase without guesswork. Wired in ONLY under `--debug`/`-d`
(`cli.py::_enable_debug_logging` calls `enable()`) -- `mark()` is a cheap
no-op otherwise (a plain bool check, no clock read), so sprinkling it
through `headless.build_session`/`tui/app.py` costs nothing on an ordinary
run.

Lines land in TWO places at once, per the brief ("in bridge.log and on
stderr with --debug"): stderr directly (visible immediately, even if
file logging itself failed to set up) and the "halo_harness" DEBUG logger
(`<state dir>/bridge.log`, already rotated/redacted by `providers.config.
setup_logging`/`RedactingFormatter` -- this module never opens a file of
its own).
"""

from __future__ import annotations

import sys
import time
from typing import Optional

_enabled = False
_t0: Optional[float] = None


def enable() -> None:
    """Starts the clock. Idempotent-ish: a second call just restarts it
    (there is only ever one real startup per process), never raises."""
    global _enabled, _t0
    _enabled = True
    _t0 = time.monotonic()


def is_enabled() -> bool:
    return _enabled


def mark(phase: str) -> None:
    """No-op unless `enable()` ran first (plain `halo`, no `--debug`).
    `phase`: a short label -- "settings", "instructions", "mcp discovery",
    "session build", "first paint" are this milestone's own five, but any
    caller may add more without changing this function."""
    if not _enabled or _t0 is None:
        return
    elapsed_ms = int((time.monotonic() - _t0) * 1000)
    line = f"halo: [timeline] {phase}: +{elapsed_ms}ms"
    try:
        print(line, file=sys.stderr)
    except Exception:
        pass
    try:
        import logging
        logging.getLogger("halo_harness").debug("[timeline] %s: +%dms", phase, elapsed_ms)
    except Exception:
        pass


def reset() -> None:
    """Test seam: clear state between tests (mirrors `cc_models.reset_
    cached_claude_auth_status`'s own convention)."""
    global _enabled, _t0
    _enabled = False
    _t0 = None
