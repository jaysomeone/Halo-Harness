"""tests.test_docs_slash_commands -- H14b brief: docs/SLASH-COMMANDS.md must
document every registered built-in `/command`. Gathered from the real
registry (`rolo_claude.commands.builtins._BUILTIN_SPECS`), never a hardcoded
list here, so this can't silently drift from the code.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()

_HEADING_RE = re.compile(r"^#{1,4}\s+.*$", re.MULTILINE)


@test
def test_every_builtin_command_has_a_section(ctx: Ctx):
    from rolo_claude.commands.builtins import _BUILTIN_SPECS

    text = (REPO_DIR / "docs" / "SLASH-COMMANDS.md").read_text(encoding="utf-8")
    ctx.check("_BUILTIN_SPECS is non-empty (sanity)", _BUILTIN_SPECS)
    missing = []
    for name in sorted(_BUILTIN_SPECS):
        # a heading containing `/name` as its own backticked token, e.g.
        # "### `/model [ref]`" or "### `/exit`, `/quit`" -- word-boundary
        # so `/model` doesn't accidentally satisfy a check for `/mode`.
        pattern = re.compile(rf"^#{{1,4}}\s+.*`/{re.escape(name)}(?![\w-])", re.MULTILINE)
        if not pattern.search(text):
            missing.append(name)
    ctx.check(f"every built-in command has a heading in SLASH-COMMANDS.md; missing: {missing}", not missing)


@test
def test_no_heading_documents_a_fictional_builtin(ctx: Ctx):
    """Every `/name`-shaped token that opens a heading (the doc's own
    per-command section marker) must be a real built-in, a documented
    non-built-in alias this same registry resolves (`/quit`), or a name
    this page explicitly documents as NOT a built-in (custom commands,
    skills, MCP prompts -- covered in prose, not their own heading here)."""
    from rolo_claude.commands.builtins import _BUILTIN_SPECS

    text = (REPO_DIR / "docs" / "SLASH-COMMANDS.md").read_text(encoding="utf-8")
    known = set(_BUILTIN_SPECS) | {"quit"}  # /quit: a real TUI alias for /exit, see tui/slash.py
    bogus = []
    for heading in _HEADING_RE.findall(text):
        for name in re.findall(r"`/([A-Za-z][\w-]*)", heading):
            if name not in known:
                bogus.append(name)
    ctx.check(f"no fictional /command in a SLASH-COMMANDS.md heading; found: {bogus}", not bogus)


@test
def test_keybindings_defaults_are_represented(ctx: Ctx):
    """Spot-check that every DEFAULT_KEYBINDINGS action string (the
    canonical source, `rolo_claude.tui.keys`) has some mention on the
    page -- catches a chord/binding added to the code but never
    documented."""
    from rolo_claude.tui.keys import DEFAULT_KEYBINDINGS

    text = (REPO_DIR / "docs" / "SLASH-COMMANDS.md").read_text(encoding="utf-8").lower()
    # Display aliases the doc prose uses for the same physical key/chord
    # step (a docs-readability choice, not a code behavior difference):
    # "escape" is always written "Esc", and the two child-navigation chords
    # are written with arrow glyphs rather than the word "down"/"up".
    _ALIASES = {"escape": "esc", "down": "↓", "up": "↑"}
    missing = []
    for group in DEFAULT_KEYBINDINGS:
        for keystroke, action in (group.get("bindings") or {}).items():
            # Actions are documented by their EFFECT in prose (e.g.
            # "session:export" -> "export the session"), not the raw action
            # string itself -- so this only checks that every KEY/CHORD-PART
            # (e.g. "ctrl+x" and "ctrl+s" from "ctrl+x ctrl+s") appears
            # somewhere on the page, case-insensitively, aliases applied.
            parts = keystroke.split(" ")
            if not all(_ALIASES.get(p, p) in text for p in parts):
                missing.append(f"{group.get('context')}: {keystroke!r} ({action!r})")
    ctx.check(f"every default keybinding is mentioned in SLASH-COMMANDS.md; missing: {missing}", not missing)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
