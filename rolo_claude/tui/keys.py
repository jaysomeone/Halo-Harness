"""rolo_claude.tui.keys -- key/mode constants shared by app.py and its
widgets, kept in one pure module so tests can import the mode-cycle order
without pulling in textual.
"""

from __future__ import annotations

# Shift+Tab cycles exactly these four (D-TUI); bypassPermissions/dontAsk are
# CLI-only starting modes, never reached by cycling.
MODE_CYCLE = ("default", "acceptEdits", "plan", "auto")


def next_mode(current: str) -> str:
    try:
        i = MODE_CYCLE.index(current)
    except ValueError:
        return MODE_CYCLE[0]
    return MODE_CYCLE[(i + 1) % len(MODE_CYCLE)]


# Ctrl+C "double press to quit" window (D-TUI: 1.5s).
DOUBLE_CTRL_C_WINDOW_S = 1.5

# Auto-grow bounds for PromptInput (D-TUI: "1-8 lines").
PROMPT_MIN_LINES = 1
PROMPT_MAX_LINES = 8

# A run this long or longer collapses into one FoldedHistory placeholder
# widget (D-TUI scope C/U5: "old turns folded after 300 widgets").
FOLD_AFTER_WIDGETS = 300

# `_drain`'s per-tick time budget and the timer's rate (D-TUI: "30 Hz ...
# 8ms budget").
DRAIN_HZ = 30
DRAIN_BUDGET_S = 0.008

# Paste placeholder threshold (D-TUI: "paste >=4 lines").
PASTE_PLACEHOLDER_MIN_LINES = 4
