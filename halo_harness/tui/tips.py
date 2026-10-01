"""halo_harness.tui.tips -- Halo 2.0.1 W2b (HALO-2.0.1-liveness-tips-brief.md
Part B): rotating tips in the input placeholder, replacing the single
hardcoded 'Try "read README.md and summarise it"' text (rolo: "instead of
the same ... background in the chat box, have it give short info on how to
use different slash commands or features in halo because we have lots of
amazing features").

Every curated tip below was checked against the REAL command/key/flag it
names (`commands/builtins.py`'s `_BUILTIN_SPECS`, `tui/slash.py`'s own
handler dict, `tui/keys.py`'s `DEFAULT_KEYBINDINGS`, `cli.py`'s `_REAL_FLAGS`)
-- several of the brief's own draft lines named something that doesn't
actually work that way (`/plan` and `/config`/`/add-dir` are decorative in
the interactive TUI today -- no `tui/slash.py` handler overrides their
headless, hardcoded-refusal body; `/init` is a DIFFERENT command from the
CLI's own `halo init` provider wizard; `/stats --models` in the TUI has no
time-to-first-token column, only the CLI's own `halo stats --models` does)
-- corrected or dropped rather than copied verbatim, per this round's own
brief ("the worker keeps only the ones that exist in this tree, corrects
wording against the real command and key names").
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Optional

# B3: the off-switch placeholder (`tips: false` in ~/.halo/config.json, or
# HALO_TIPS=0/BRIDGE_TIPS=0/ROLO_CLAUDE_TIPS=0 via env_compat) -- the ONLY
# other thing shown in the input placeholder besides a rotating tip.
STATIC_PLACEHOLDER = "Ask anything · / commands · @ files · ! shell"

MAX_TIP_LEN = 96


@dataclass(frozen=True)
class Tip:
    text: str
    # Names a provider/feature this tip only makes sense once it's actually
    # enabled/present -- "cc" (Claude Code subscription), "dbx" (Databricks),
    # "or" (OpenRouter), "chrome" (`--chrome`), "mcp" (an MCP server
    # configured). Empty (the common case) means "always applicable".
    needs: tuple = ()


# ---- curated tips (B1: "about 40 ... each at most 96 chars, imperative,
# no filler") -----------------------------------------------------------

TIPS: "tuple[Tip, ...]" = (
    Tip('/model opens the picker: arrows move, Enter selects, type to filter'),
    Tip('/model <ref> switches the model directly, no picker needed'),
    Tip("/models refresh re-reads every provider's catalog: prices, context, new models"),
    Tip('/effort sets thinking depth; GLM on Databricks accepts low, high and max', needs=("dbx",)),
    Tip('--effort high on the command line sets the starting effort for the session'),
    Tip('Shift+Tab cycles permission modes: default, acceptEdits, plan, auto'),
    Tip('Type while a turn runs to steer it; the model picks it up at the next chunk'),
    Tip('Esc interrupts the current step; Ctrl+C twice quits'),
    Tip('Ctrl+Q force-quits immediately, skipping the ordinary quit path'),
    Tip('@path mentions a file in your prompt; Tab completes the path'),
    Tip('@path#L10-20 attaches only those line numbers from the file'),
    Tip('!cmd runs a shell command via the Bash tool; the model sees its output next turn'),
    Tip('/rewind restores the working tree to a recorded step'),
    Tip('/undo and /redo step one recorded change back or forward'),
    Tip('/compact [instructions] summarizes the conversation to free up context'),
    Tip('/stats --models shows per-model edit failures, tool errors and cost'),
    Tip('/stats --tools shows error rates per tool this session'),
    Tip('/improve drafts memory and rule candidates from recent sessions; you approve each one'),
    Tip('/providers shows which providers are enabled and why'),
    Tip('/mcp lists configured MCP servers and reconnects one', needs=("mcp",)),
    Tip('/memory shows the auto-memory directory and index'),
    Tip('/resume picks an earlier session to continue'),
    Tip('halo -c continues the last session in this directory'),
    Tip('/doctor checks install, credentials and tools'),
    Tip('halo init walks through provider setup step by step'),
    Tip('/cost shows tokens and spend for this session'),
    Tip('/cost also shows your OpenRouter balance once a fetch has succeeded', needs=("or",)),
    Tip('/theme switches themes, saved in ~/.halo/config.json'),
    Tip('/intro replays the launch intro'),
    Tip('Ctrl+O toggles verbose: full thinking and tool output'),
    Tip('Ctrl+R searches your prompt history'),
    Tip('Paste 4 or more lines and it becomes a [Pasted text #1] placeholder'),
    Tip('halo -p "prompt" prints an answer without the TUI'),
    Tip('--output-format json turns a -p answer into a script-friendly payload'),
    Tip('halo --chrome connects the Claude in Chrome extension'),
    Tip('--playwright adds browser tools over the Chrome DevTools Protocol'),
    Tip('/agents lists sub-agents; @agent-<name> asks for one by name'),
    Tip('/skills lists discovered skills; run one as /<skill-name>'),
    Tip('/permissions shows the allow, deny and ask rules from your settings'),
    Tip('/context shows what is actually in the context window right now'),
    Tip('/export writes this conversation to a file'),
    Tip('halo --add-dir DIRECTORY adds an extra working directory for the session'),
    Tip("halo stats --since 7d summarizes the week's sessions"),
    Tip('/dbx refreshes and lists Databricks endpoints', needs=("dbx",)),
    Tip('cc: models use your Claude subscription login, no API key needed', needs=("cc",)),
    Tip('/fork continues this conversation in a brand-new session'),
    Tip('/rename titles this session so it is easy to find later in /resume'),
    Tip('Ctrl+E opens your $EDITOR to compose a longer prompt'),
    Tip('/keybindings shows every key and chord bound right now'),
    Tip('Ctrl+X cuts a selection in the chat box, or is a chord prefix for session actions'),
    Tip('/config shows the current model, permission mode and theme at a glance'),
    Tip('/tips shows every tip that applies to this session right now'),
    # W2c items 2/3: "copy / paste is off in the chat box" -- two tips
    # naming the real keys now that Ctrl+C/Ctrl+X/Ctrl+A/Ctrl+V all work on
    # an in-box selection, not just a screen/transcript one.
    Tip('Ctrl+A selects all text in the chat box; Ctrl+C then copies it, Ctrl+X cuts it'),
    Tip('Ctrl+V pastes in the chat box when a clipboard tool is available, or use your terminal shortcut'),
)


def _covered_command_names(tips: "tuple[Tip, ...]") -> "set[str]":
    """Every `/name` a curated tip already names -- `generated_tips_for_
    registry` skips these so a command never gets BOTH a curated and a
    generated tip."""
    names: "set[str]" = set()
    for tip in tips:
        for m in re.finditer(r"/([a-zA-Z][\w-]*)", tip.text):
            names.add(m.group(1))
    return names


def generated_tips_for_registry(registry, *, curated: "tuple[Tip, ...]" = TIPS) -> "list[Tip]":
    """B1: one GENERATED tip per registry command the curated list above
    does NOT already cover -- `"/name <argument-hint>: <description>"`
    (description cut at 70 chars) -- including custom commands and skills
    discovered for this cwd, via the SAME `Registry` a real session
    already built (`Registry.discover`/`all()`)."""
    if registry is None:
        return []
    covered = _covered_command_names(curated)
    out: "list[Tip]" = []
    for cmd in registry.all():
        if cmd.name in covered:
            continue
        hint = f" {cmd.argument_hint}" if cmd.argument_hint else ""
        desc = (cmd.description or "").strip()
        if len(desc) > 70:
            desc = desc[:70].rstrip() + "…"
        text = f"/{cmd.name}{hint}: {desc}" if desc else f"/{cmd.name}{hint}"
        out.append(Tip(text[:MAX_TIP_LEN]))
    return out


def detect_enabled_needs(*, facade=None, controller=None) -> "frozenset[str]":
    """Best-effort "what's actually usable right now" for `Tip.needs`
    filtering (B1: "shown only when it is enabled or present"). Every
    check is independently best-effort -- an exception or a missing
    attribute (a bare/fake facade, a unit test) just means "not detected",
    never a crash; this runs from the TUI's own startup/rotation path,
    which must never fail a launch over a tips cosmetic."""
    needs: "set[str]" = set()
    try:
        from halo_harness.providers.enablement import is_enabled
        for flag, name in (("cc", "claude_subscription"), ("dbx", "databricks"), ("or", "openrouter")):
            try:
                if is_enabled(name):
                    needs.add(flag)
            except Exception:
                pass
    except Exception:
        pass
    tool_registry = getattr(facade, "tool_registry", None)
    get_tool = getattr(tool_registry, "get", None)
    if callable(get_tool):
        try:
            if get_tool("mcp__claude-in-chrome__navigate") is not None:
                needs.add("chrome")
        except Exception:
            pass
    mcp_status = getattr(controller, "mcp_status", None)
    if callable(mcp_status):
        try:
            if (mcp_status() or {}).get("total", 0) > 0:
                needs.add("mcp")
        except Exception:
            pass
    else:
        mcp_servers = getattr(facade, "mcp_servers", None)
        if mcp_servers:
            needs.add("mcp")
    return frozenset(needs)


def applicable_tips(all_tips: "list[Tip]", enabled_needs: "frozenset[str]") -> "list[Tip]":
    return [t for t in all_tips if not t.needs or any(n in enabled_needs for n in t.needs)]


def all_applicable_tips(registry=None, *, facade=None, controller=None) -> "list[Tip]":
    """The curated list plus every generated one, filtered by `needs` --
    what `/tips` prints and what `TipRotator` rotates through."""
    needs = detect_enabled_needs(facade=facade, controller=controller)
    combined = list(TIPS) + generated_tips_for_registry(registry)
    return applicable_tips(combined, needs)


# ---- B3: the config/env off switch -----------------------------------

def tips_enabled() -> bool:
    """`tips: false` in `~/.halo/config.json`, or `HALO_TIPS=0` (legacy
    `BRIDGE_TIPS=0`/`ROLO_CLAUDE_TIPS=0` via `env_compat`) -- either one
    turns tips off; the env var (when set at all) wins over the config
    file, matching every other HALO_*-vs-config.json knob in this app."""
    from halo_harness.config.paths import env_compat
    env_val = env_compat("TIPS")
    if env_val is not None:
        return env_val.strip().lower() not in ("0", "false", "no", "")
    from halo_harness.theme import get_config_value
    return bool(get_config_value("tips", True))


# ---- B2: rotation -------------------------------------------------------

class TipRotator:
    """B2: "a random tip at launch; the next tip (shuffled order, no
    repeat until the list is exhausted) at every turn_done and every 15s
    while the input is empty and no turn is running". `rng` is an
    injectable `random.Random` -- a test seeds it for a reproducible
    sequence ("launch tip differs from the after-turn tip (seeded RNG)")."""

    def __init__(self, tips: "list[Tip]", *, rng: "Optional[random.Random]" = None) -> None:
        self._tips = list(tips)
        self._rng = rng if rng is not None else random.Random()
        self._deck: "list[Tip]" = []
        # `current` must exist (as None) BEFORE the first `_draw()` call --
        # `_refill()` reads `self.current` (to avoid an immediate repeat
        # across a reshuffle boundary), and that first draw happens right
        # here, before this attribute would otherwise ever be set.
        self.current: "Optional[Tip]" = None
        if self._tips:
            self.current = self._draw()

    def _refill(self) -> None:
        self._deck = list(self._tips)
        self._rng.shuffle(self._deck)
        # Never let a reshuffle immediately repeat the tip just shown (only
        # matters once more than one tip exists at all).
        if self.current is not None and len(self._deck) > 1 and self._deck[0] is self.current:
            self._deck.append(self._deck.pop(0))

    def _draw(self) -> "Optional[Tip]":
        if not self._tips:
            return None
        if not self._deck:
            self._refill()
        return self._deck.pop(0)

    def advance(self) -> "Optional[Tip]":
        self.current = self._draw()
        return self.current


def format_tip_placeholder(tip_text: str, width: int) -> str:
    """B2: `"Tip: <text>"`, fitted to `width - 6` with an ellipsis."""
    text = f"Tip: {tip_text}"
    max_len = max(1, width - 6)
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return "…"
    return text[: max_len - 1].rstrip() + "…"


# ---- B5: tips may only name things that exist --------------------------

_WORD_RE = re.compile(r"/([a-zA-Z][\w-]*)")
_FLAG_RE = re.compile(r"(?<![\w-])(--[a-zA-Z][\w-]*)")
_KEY_RE = re.compile(
    r"\b(?:Ctrl|Shift|Alt|Cmd|Meta)(?:\+[A-Za-z0-9]+)+\b|\b(?:Esc|Tab|Enter)\b")


def slash_words_in(text: str) -> "list[str]":
    """Every `/word` token (never `/<placeholder>` -- a literal `<` right
    after the slash, used for a generic "name goes here" mention like
    `/<skill-name>`, is deliberately NOT a word character so this regex
    never matches it -- that's the point: a placeholder names no SPECIFIC
    command to validate)."""
    return [m.group(1) for m in _WORD_RE.finditer(text)]


def flags_in(text: str) -> "list[str]":
    return [m.group(1) for m in _FLAG_RE.finditer(text)]


def key_names_in(text: str) -> "list[str]":
    return [m.group(0) for m in _KEY_RE.finditer(text)]


def key_is_bound(key_text: str, keymap: "dict[str, dict[str, str]]") -> bool:
    """`key_text` (e.g. "Ctrl+O", "Shift+Tab", "Esc") resolves against
    `tui.keys.load_keymap()`'s own `{context: {chord: action}}` shape --
    bound as an ordinary (leaf) key in ANY context, OR as the FIRST
    keystroke of some multi-key chord (e.g. "Ctrl+X" alone, named in a tip
    about the chord prefix itself, is never a leaf action on its own)."""
    from halo_harness.tui.keys import STATIC_APP_BINDINGS, normalize_keystroke

    normalized = normalize_keystroke(key_text)
    if not normalized:
        return False
    if normalized in STATIC_APP_BINDINGS:
        return True
    for bucket in keymap.values():
        if normalized in bucket:
            return True
        needle = normalized + " "
        if any(chord.startswith(needle) for chord in bucket):
            return True
    return False
