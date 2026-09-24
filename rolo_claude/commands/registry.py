"""rolo_claude.commands.registry -- `SlashCommand` + `Registry` (U0 scope B),
plus the `$ARGUMENTS`/`$0..$9`/`` !`cmd` `` substitution shared by a custom
command's and a skill's prompt body (plan D-TUI: "same fields as skills
minus name/paths"; binary facts/plan: 0-based `$ARGUMENTS[N]`/`$N`, no
placeholder -> `ARGUMENTS: <input>` appended, `` !`cmd` `` pre-execution
gated by `allowed-tools`, a failed/unpermitted command aborts the
invocation). `@path` expansion is explicitly left for the core (the agent
loop's own prompt assembly), never done here.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

_BACKTICK_CMD_RE = re.compile(r"!`([^`]*)`")


@dataclass
class SlashCommand:
    name: str  # e.g. "help", "git:commit", "anthropic-skills:dataviz" (no leading "/")
    description: str = ""
    kind: str = "core"  # "ui" | "core" | "prompt"
    argument_hint: Optional[str] = None
    aliases: tuple = ()
    source: str = "builtin"  # "builtin" | "custom" | "skill"
    path: Optional[Path] = None
    run: Optional[Callable] = None  # (args_text: str, facade) -> str
    # H4 scope C: a skill's OWN frontmatter, separate from `user-invocable`
    # (which gates the `/` slash surface instead) -- `disable-model-
    # invocation: true` hides it from the Skill TOOL only; a skill may
    # still be typed as `/name` by the user even when this is False.
    model_invocable: bool = True
    allowed_tools: tuple = ()
    context_mode: Optional[str] = None  # None | "fork" | "agent" (frontmatter `context:`)

    def invocation(self) -> str:
        return f"/{self.name}"


class Registry:
    """A flat name -> SlashCommand map plus an alias map (a synced skill's
    bare name, when it doesn't collide -- binary facts sec.11). Callers
    control precedence entirely by the ORDER/METHOD they register in:
    `add()` never overwrites an existing name; `add_skill()` (skills.py's
    own registration path) DOES overwrite a same-named CUSTOM command (plan:
    "a skill wins over a same-named command") but never a builtin."""

    def __init__(self):
        self._commands: dict = {}
        self._aliases: dict = {}  # alias -> primary name

    def add(self, cmd: SlashCommand, *, replace: bool = False) -> bool:
        if cmd.name in self._commands and not replace:
            return False
        self._commands[cmd.name] = cmd
        self._register_aliases(cmd)
        return True

    def add_skill(self, cmd: SlashCommand) -> bool:
        existing = self._commands.get(cmd.name)
        if existing is not None and existing.source == "builtin":
            return False
        self._commands[cmd.name] = cmd
        self._register_aliases(cmd)
        return True

    def _register_aliases(self, cmd: SlashCommand) -> None:
        for alias in cmd.aliases:
            if alias not in self._commands and alias not in self._aliases:
                self._aliases[alias] = cmd.name

    def resolve(self, name: str) -> Optional[SlashCommand]:
        name = name.lstrip("/")
        if name in self._commands:
            return self._commands[name]
        if name in self._aliases:
            return self._commands.get(self._aliases[name])
        return None

    def complete(self, prefix: str) -> list:
        prefix = prefix.lstrip("/")
        names = sorted(set(self._commands) | set(self._aliases))
        seen = set()
        out = []
        for n in names:
            if not n.startswith(prefix):
                continue
            cmd = self.resolve(n)
            if cmd is not None and id(cmd) not in seen:
                seen.add(id(cmd))
                out.append(cmd)
        return out

    def help_rows(self) -> list:
        """`[(invocation, description), ...]`, primary names only (not
        aliases), sorted alphabetically."""
        return [(f"/{name}", self._commands[name].description) for name in sorted(self._commands)]

    def all(self) -> list:
        return [self._commands[name] for name in sorted(self._commands)]

    @staticmethod
    def discover(cwd, home=None) -> "Registry":
        from rolo_claude.commands.builtins import register_builtins
        from rolo_claude.commands.custom import register_custom_commands
        from rolo_claude.commands.skills import register_skills

        reg = Registry()
        register_builtins(reg)
        register_custom_commands(reg, cwd=Path(cwd), home=home)
        register_skills(reg, cwd=Path(cwd), home=home)
        return reg


# =============================================================================
# Shared prompt-body substitution (custom.py + skills.py).
# =============================================================================

@dataclass
class ExpansionResult:
    text: str
    error: Optional[str] = None


def split_args(args_text: str) -> list:
    try:
        return shlex.split(args_text)
    except ValueError:
        return args_text.split()


def substitute_arguments(body: str, args_text: str) -> str:
    """`$ARGUMENTS` -> the raw joined text; `$0`..`$9` -> 0-based
    whitespace/quote-split tokens (skills/commands convention -- explicitly
    NOT 1-based). No placeholder used AND arguments were given -> append
    `ARGUMENTS: <input>`."""
    tokens = split_args(args_text)
    used_placeholder = "$ARGUMENTS" in body
    out = body.replace("$ARGUMENTS", args_text)
    for i in range(10):
        placeholder = f"${i}"
        if placeholder in out:
            used_placeholder = True
            out = out.replace(placeholder, tokens[i] if i < len(tokens) else "")
    if not used_placeholder and args_text:
        out = out.rstrip("\n") + f"\n\nARGUMENTS: {args_text}"
    return out


def _preexec_command_permitted(command: str, allow_rules: list) -> bool:
    """Strict: ONLY an explicit Bash allow rule counts -- unlike the
    permission engine's own `bash_allow_matches` (used for an interactive
    tool call), there is NO read-only-commands-never-need-a-rule carve-out
    here. A command file's `` !`cmd` `` pre-execution list is deliberately
    minimal-trust: every command it runs before the model ever sees the
    prompt must be named, in full, in its OWN frontmatter."""
    from rolo_claude.permissions import _pattern_matches_text
    stripped = command.strip()
    return any(rule.kind in ("exact", "prefix", "glob") and _pattern_matches_text(rule.kind, rule.value, stripped)
               for rule in allow_rules)


def run_preexec_commands(body: str, *, allowed_tools: Optional[list] = None, cwd: Path) -> ExpansionResult:
    """Replace every `` !`cmd` `` span with that command's stripped stdout,
    run through the system shell -- ONLY when `allowed_tools` (the
    frontmatter list, already split like `--allowedTools`) contains a Bash
    rule permitting it (matched via permissions.py's own grammar, so the
    exact same rule syntax works here as in settings.json). An unpermitted
    or nonzero-exit command ABORTS the whole invocation (D-TUI: "a failed
    command aborts the invocation") -- the first such span stops and this
    returns with `.error` set."""
    from rolo_claude.permissions import parse_rule

    allow_rules = [parse_rule(r, source="frontmatter", base_dir=Path(cwd), action="allow")
                   for r in (allowed_tools or []) if isinstance(r, str) and r.strip().startswith("Bash")]

    out_parts = []
    pos = 0
    for m in _BACKTICK_CMD_RE.finditer(body):
        command = m.group(1)
        out_parts.append(body[pos:m.start()])
        if not _preexec_command_permitted(command, allow_rules):
            return ExpansionResult(text="", error=(
                f"pre-execution command not permitted by this command's allowed-tools: {command!r} "
                f"(add a matching Bash(...) rule to its frontmatter)"))
        try:
            proc = subprocess.run(command, shell=True, cwd=str(cwd), capture_output=True,
                                   text=True, timeout=30)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            return ExpansionResult(text="", error=f"pre-execution command failed to start: {command!r} ({e})")
        if proc.returncode != 0:
            return ExpansionResult(text="", error=(
                f"pre-execution command exited {proc.returncode}: {command!r}\n{proc.stderr}"))
        out_parts.append(proc.stdout.rstrip("\n"))
        pos = m.end()
    out_parts.append(body[pos:])
    return ExpansionResult(text="".join(out_parts))


def expand_command_body(body: str, args_text: str, *, allowed_tools: Optional[list] = None, cwd: Path) -> ExpansionResult:
    """`$ARGUMENTS`/`$N` substitution, then `` !`cmd` `` pre-execution --
    the one entry point custom.py and skills.py both call."""
    substituted = substitute_arguments(body, args_text)
    return run_preexec_commands(substituted, allowed_tools=allowed_tools, cwd=cwd)


# =============================================================================
# H4 scope C: `@path` attachments in an EXPANDED command/skill body -- read
# via the same path-resolution a Read tool call would use and appended as
# SNAPSHOTS (never inlined into the prompt text itself), mirroring
# config/claude_md.py's own `@import` convention but for arbitrary file
# mentions in a slash-invoked prompt body. The `@`-mention text itself is
# left untouched in the submitted turn -- the model still sees exactly what
# the command/skill author wrote, plus the real file content alongside it.
# =============================================================================

_AT_MENTION_RE = re.compile(r"(?<![\w`@])@((?:~/|\.{1,2}/|//?)?[^\s`'\"()<>]+)")
_AT_MENTION_MAX_BYTES = 200_000


def extract_at_mentions(text: str, *, cwd: Path) -> "list[Path]":
    """Every `@path` mention that resolves to a REAL, existing regular
    file under (or reachable from) `cwd` -- a mention that doesn't
    resolve is left alone for the model to notice/ask about, never an
    error here. Deduplicated, in first-seen order."""
    out: "list[Path]" = []
    seen: set = set()
    for m in _AT_MENTION_RE.finditer(text or ""):
        raw = m.group(1).rstrip(".,;:!?)")
        if not raw:
            continue
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = Path(cwd) / p
        try:
            resolved = p.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def read_at_mention_snapshots(text: str, *, cwd: Path, max_bytes: int = _AT_MENTION_MAX_BYTES) -> "list[tuple[str, str]]":
    """`[(path_str, content_text), ...]` for every `@path` mention
    `extract_at_mentions` finds -- best-effort: an unreadable or
    oversized file is silently skipped (its `@mention` stays in the
    submitted text either way; a model that needs it can still Read it
    directly), never raised."""
    out: "list[tuple[str, str]]" = []
    for path in extract_at_mentions(text, cwd=cwd):
        try:
            if path.stat().st_size > max_bytes:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.append((str(path), content))
    return out
