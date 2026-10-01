"""halo_harness.tui.keys -- key/mode constants shared by app.py and its
widgets, kept in one pure module so tests can import the mode-cycle order
without pulling in textual. U5 scope A adds: our own default keybindings as
a data table, expressed in Claude Code's OWN `~/.claude/keybindings.json`
format (contexts + chords, e.g. "ctrl+x ctrl+s") so a user's real
keybindings.json merges onto it the same way Claude Code merges theirs --
additive, a null action unbinds. Pure logic only (no textual import) so it's
unit-testable directly; `tui/app.py` is the only module that turns this data
into live Textual bindings/dispatch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

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

# Chord prefix -> next-keystroke timeout (keybindings-help skill: "1-second
# timeout between keystrokes"; OpenCode calls the same idea `leader_timeout`).
CHORD_TIMEOUT_S = 1.0

# Halo 2.0.1 W2b (liveness-tips-brief Part B5): the single-key bindings
# `tui/app.py`'s own `BridgeApp.BINDINGS` declares directly (priority
# Textual `Binding`s, never remappable via keybindings.json -- see this
# module's own docstring on why DEFAULT_KEYBINDINGS deliberately excludes
# them). Kept here, as plain normalized strings with no textual import, so
# `tui/tips.py`'s own `key_is_bound` can validate a tip's "Ctrl+O"/"Esc"
# mention against the full, real set of bound keys -- not just the
# remappable chord table -- without this pure module ever importing
# textual itself.
STATIC_APP_BINDINGS = frozenset({
    "ctrl+c", "ctrl+d", "escape", "shift+tab", "ctrl+l", "ctrl+o", "ctrl+r",
    "f1", "ctrl+p", "ctrl+e", "ctrl+x", "ctrl+end", "end", "ctrl+q",
})

# ============================================================================
# Claude Code keybindings.json format (U5 scope A).
# ============================================================================

# Our own defaults, as a data table in Claude Code's OWN file shape: a list
# of {"context": ..., "bindings": {keystroke_or_chord: action}} groups.
# `action` names loosely follow Claude Code's own `area:verb` convention
# (see the keybindings-help skill's "Available Actions" table) so a user
# who already knows that naming recognises ours; they name OUR app's own
# behaviour, not Claude Code's (e.g. "rewind:undo" has no Claude Code
# counterpart). Only NEW keys (not already a static Textual Binding in
# tui/app.py) are introduced as chords here, to keep the existing,
# already-tested single-key bindings completely unchanged -- see
# `tui/app.py`'s own docstring note on this deliberate scope boundary.
DEFAULT_KEYBINDINGS = [
    {"context": "Global", "bindings": {
        "ctrl+c": "app:interrupt",
        "ctrl+d": "app:exit",
        "escape": "app:interrupt",
        "shift+tab": "chat:cycleMode",
        "ctrl+l": "chat:clearScreen",
        "ctrl+o": "app:toggleVerbose",
        "ctrl+r": "history:search",
        "f1": "app:help",
        "ctrl+p": "app:commandPalette",
        "ctrl+e": "chat:externalEditor",
        "ctrl+x ctrl+s": "session:export",
        "ctrl+x ctrl+r": "session:rename",
        "ctrl+x ctrl+f": "session:fork",
        "ctrl+x ctrl+l": "session:resume",
        "ctrl+x ctrl+u": "rewind:undo",
        "ctrl+x ctrl+y": "rewind:redo",
        "ctrl+x ctrl+k": "session:stats",
        "ctrl+x down": "session:nextChild",
        "ctrl+x up": "session:prevChild",
    }},
    {"context": "Chat", "bindings": {
        "enter": "chat:submit",
        "ctrl+j": "chat:newline",
        "alt+enter": "chat:newline",
        "tab": "autocomplete:accept",
        "up": "history:previous",
        "down": "history:next",
    }},
    {"context": "Transcript", "bindings": {
        "o": "transcript:expandCard",
        "ctrl+o": "transcript:toggleShowAll",
    }},
]

_SPECIAL_KEY_ALIASES = {
    "control": "ctrl", "opt": "alt", "option": "alt", "cmd": "meta", "command": "meta",
    "esc": "escape", "return": "enter",
}
_MOD_ORDER = {"ctrl": 0, "alt": 1, "shift": 2, "meta": 3}


def normalize_keystroke(key: str) -> str:
    """One keystroke (no spaces) -> canonical form: modifier aliases folded
    (Claude Code's own keybindings.json syntax: `control`->`ctrl`, `opt`/
    `option`->`alt`, `cmd`/`command`->`meta`, `esc`->`escape`,
    `return`->`enter`) and modifiers reordered ctrl/alt/shift/meta so
    `"shift+ctrl+p"` and `"ctrl+shift+p"` compare equal. An empty/blank
    input returns ""."""
    parts = [p.strip().lower() for p in key.split("+") if p.strip()]
    if not parts:
        return ""
    parts = [_SPECIAL_KEY_ALIASES.get(p, p) for p in parts]
    mods, base = parts[:-1], parts[-1]
    mods = sorted(set(mods), key=lambda m: _MOD_ORDER.get(m, 9))
    return "+".join([*mods, base])


def normalize_chord(chord: str) -> str:
    """A chord is space-separated keystrokes (Claude Code: "1-second
    timeout between keystrokes", e.g. `"ctrl+x ctrl+s"`); each keystroke is
    normalized independently."""
    return " ".join(normalize_keystroke(k) for k in chord.split(" ") if k.strip())


def default_bindings_flat() -> "dict[str, dict[str, str]]":
    """`DEFAULT_KEYBINDINGS` reshaped to `{context: {normalized_chord:
    action}}` -- the shape every other function in this module works with."""
    out: "dict[str, dict[str, str]]" = {}
    for group in DEFAULT_KEYBINDINGS:
        ctx = group.get("context", "Global")
        bucket = out.setdefault(ctx, {})
        for key, action in (group.get("bindings") or {}).items():
            chord = normalize_chord(key)
            if chord:
                bucket[chord] = action
    return out


def merge_user_keybindings(defaults: "dict[str, dict[str, str]]",
                            user_doc: Optional[dict]) -> "dict[str, dict[str, str]]":
    """Merge a parsed `~/.claude/keybindings.json` document onto `defaults`
    the way Claude Code merges user bindings: additive (a context only
    needs to appear if the user wants to change something in it), a `null`
    action UNBINDS that key in that context. `user_doc` is the raw parsed
    JSON (`{"bindings": [{"context": .., "bindings": {...}}, ...]}`) -- a
    malformed/missing document is a no-op (returns a copy of `defaults`
    unchanged), matching Claude Code's own "validation warnings never crash
    the app" behaviour. Never mutates `defaults` itself."""
    merged = {ctx: dict(bucket) for ctx, bucket in defaults.items()}
    if not isinstance(user_doc, dict):
        return merged
    groups = user_doc.get("bindings")
    if not isinstance(groups, list):
        return merged
    for group in groups:
        if not isinstance(group, dict):
            continue
        ctx = group.get("context")
        if not isinstance(ctx, str) or not ctx:
            continue
        bindings = group.get("bindings")
        if not isinstance(bindings, dict):
            continue
        bucket = merged.setdefault(ctx, {})
        for key, action in bindings.items():
            chord = normalize_chord(str(key))
            if not chord:
                continue
            if action is None:
                bucket.pop(chord, None)
            elif isinstance(action, str):
                bucket[chord] = action
    return merged


def load_user_keybindings_doc(path: Path) -> Optional[dict]:
    """Best-effort read of a `keybindings.json` file -- `None` for a
    missing file or invalid JSON (never raises; a stale/typo'd file must
    never crash startup, matching every other config reader in this
    codebase)."""
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def default_keybindings_path() -> Path:
    from halo_harness.config.paths import claude_config_dir
    return claude_config_dir() / "keybindings.json"


def load_keymap(path: Optional[Path] = None) -> "dict[str, dict[str, str]]":
    """The fully resolved `{context: {chord: action}}` map: our own
    defaults merged with the user's `~/.claude/keybindings.json` (or
    `path`, a test seam)."""
    if path is None:
        path = default_keybindings_path()
    return merge_user_keybindings(default_bindings_flat(), load_user_keybindings_doc(path))


# ---- chord matching (which-key) -------------------------------------------

def chord_continuations(keymap_ctx: "dict[str, str]", prefix: str) -> "dict[str, Optional[str]]":
    """Every chord in one context's `{chord: action}` map that continues
    `prefix` -- `{next_keystroke: action}` for a leaf (the chord ends
    there), or `{next_keystroke: None}` when that keystroke is itself
    another prefix (a 3+-keystroke chord; supported generally even though
    every DEFAULT_KEYBINDINGS chord today is exactly 2 keystrokes). Powers
    the which-key overlay: an empty result means `prefix` isn't a live
    chord prefix at all (the caller should treat the keystroke as an
    ordinary, non-chord binding lookup instead)."""
    out: "dict[str, Optional[str]]" = {}
    needle = prefix + " "
    for chord, action in keymap_ctx.items():
        if not chord.startswith(needle):
            continue
        rest = chord[len(needle):]
        first, _, remainder = rest.partition(" ")
        if not first:
            continue
        if remainder:
            out.setdefault(first, None)
        else:
            out[first] = action
    return out


def is_chord_prefix(keymap_ctx: "dict[str, str]", prefix: str) -> bool:
    """True if some chord in `keymap_ctx` starts with `prefix ` -- i.e.
    pressing `prefix` should open the which-key overlay and wait for a
    continuation, rather than (or in addition to) running its own action."""
    needle = prefix + " "
    return any(chord.startswith(needle) for chord in keymap_ctx)


def format_which_key(continuations: "dict[str, Optional[str]]") -> str:
    """One `"key  action"` line per continuation, sorted by key -- the
    which-key overlay's own text content (kept here, pure, so a test can
    assert on it without mounting a widget)."""
    if not continuations:
        return ""
    width = max(len(k) for k in continuations)
    lines = []
    for key in sorted(continuations):
        action = continuations[key] or "…"
        lines.append(f"{key.ljust(width)}  {action}")
    return "\n".join(lines)
