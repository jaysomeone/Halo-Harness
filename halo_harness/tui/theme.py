"""rolo_claude.tui.theme -- Textual CSS variables for each theme name
`rolo_claude.theme` resolves (U0's module does the NAME resolution/
persistence, pure, no textual import; this module maps a resolved name to
actual colors, textual-only, imported solely from `tui/app.py`).
"""

from __future__ import annotations

from rolo_claude.theme import DEFAULT_THEME, is_valid_theme  # re-exported for tui/app.py

_DARK = {
    "bridge-bg": "#1e1e2e", "bridge-surface": "#282838", "bridge-panel": "#313244",
    "bridge-text": "#cdd6f4", "bridge-muted": "#7f849c", "bridge-border": "#45475a",
    "bridge-accent": "#89b4fa", "bridge-success": "#a6e3a1", "bridge-warning": "#f9e2af",
    "bridge-error": "#f38ba8", "bridge-user": "#94e2d5", "bridge-assistant": "#cdd6f4",
    "bridge-tool": "#fab387", "bridge-thinking": "#585b70",
}
_LIGHT = {
    "bridge-bg": "#eff1f5", "bridge-surface": "#e6e9ef", "bridge-panel": "#dce0e8",
    "bridge-text": "#4c4f69", "bridge-muted": "#6c6f85", "bridge-border": "#ccd0da",
    "bridge-accent": "#1e66f5", "bridge-success": "#40a02b", "bridge-warning": "#df8e1d",
    "bridge-error": "#d20f39", "bridge-user": "#179299", "bridge-assistant": "#4c4f69",
    "bridge-tool": "#fe640b", "bridge-thinking": "#9ca0b0",
}
# A restricted, high-contrast 16-ANSI-color-only palette for terminals/
# users that need it (D-TUI: "+ daltonized, ansi variants") -- kept
# distinct from the daltonized variant, which reuses the base hex palette
# unchanged (daltonized-safe hues are already baked into the base picks
# above; "-daltonized" and the plain base therefore resolve identically
# today, a deliberate, documented simplification -- there is no color pair
# in `_DARK`/`_LIGHT` that relies on red/green discrimination alone).
_ANSI_DARK = {
    "bridge-bg": "#000000", "bridge-surface": "#000000", "bridge-panel": "#000000",
    "bridge-text": "#ffffff", "bridge-muted": "#808080", "bridge-border": "#808080",
    "bridge-accent": "#00ffff", "bridge-success": "#00ff00", "bridge-warning": "#ffff00",
    "bridge-error": "#ff0000", "bridge-user": "#00ffff", "bridge-assistant": "#ffffff",
    "bridge-tool": "#ff00ff", "bridge-thinking": "#808080",
}
_ANSI_LIGHT = {
    "bridge-bg": "#ffffff", "bridge-surface": "#ffffff", "bridge-panel": "#ffffff",
    "bridge-text": "#000000", "bridge-muted": "#808080", "bridge-border": "#808080",
    "bridge-accent": "#0000ff", "bridge-success": "#008000", "bridge-warning": "#808000",
    "bridge-error": "#ff0000", "bridge-user": "#008080", "bridge-assistant": "#000000",
    "bridge-tool": "#800080", "bridge-thinking": "#808080",
}


def variables_for(theme_name: str) -> dict:
    """CSS custom-property values (no leading `$`) for `theme_name` --
    always returns a complete dict, falling back to `claude-dark`'s
    palette for an unrecognized name rather than raising (a stale/typo'd
    theme must never crash startup, matching `rolo_claude.theme`'s own
    fallback philosophy)."""
    if not is_valid_theme(theme_name):
        theme_name = DEFAULT_THEME
    if theme_name.endswith("-ansi"):
        return dict(_ANSI_LIGHT if theme_name.startswith("claude-light") else _ANSI_DARK)
    # "-daltonized" and the plain base share a palette today -- see module note.
    return dict(_LIGHT if theme_name.startswith("claude-light") else _DARK)


MODE_GLYPHS = {
    "default": "⏸ manual", "acceptEdits": "⏩ accept edits",
    "plan": "\U0001f4dd plan", "auto": "⏩ auto",
    "dontAsk": "⏩ don't ask", "bypassPermissions": "⚠ bypass",
}


def mode_glyph(mode: str) -> str:
    return MODE_GLYPHS.get(mode, mode or "?")
