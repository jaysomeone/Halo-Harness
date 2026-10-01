"""rolo_claude.tui -- the Textual full-screen UI (U2+).

Nothing outside this package (and `cli.py`'s lazily-imported TUI-launch
branch) may import `textual`/`rich` at module scope -- `rolo_claude` itself,
and `-p`/print mode, must keep working with neither installed. Every module
in here is allowed to import them freely; this is the one place they're a
real dependency.
"""

from __future__ import annotations
