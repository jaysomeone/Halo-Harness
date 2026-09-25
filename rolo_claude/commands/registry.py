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


def substitute_claude_vars(text: str, extra_vars: Optional[dict]) -> str:
    """finding 12 (major, h4-h5-h3c review): `${CLAUDE_SKILL_DIR}`/
    `${CLAUDE_SESSION_ID}`/`${CLAUDE_PROJECT_DIR}`/`${CLAUDE_EFFORT}` (the
    documented variables a skill/command body may reference -- mirrors
    `${CLAUDE_PLUGIN_ROOT}`'s own already-working substitution in
    config/plugins.py, generalised) substituted against `extra_vars`
    (`{NAME: value}`, value coerced to `str`; a var the caller didn't
    supply, or whose value is falsy/None, is left UNSUBSTITUTED rather
    than silently becoming an empty string, so a skill author can tell
    the difference between "genuinely empty" and "not wired up yet"). A
    no-op for a body with no `${...}` at all, or `extra_vars=None`."""
    if not extra_vars or "${" not in text:
        return text
    out = text
    for name, value in extra_vars.items():
        if value is None or value == "":
            continue
        out = out.replace(f"${{{name}}}", str(value))
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


def run_preexec_commands(body: str, *, allowed_tools: Optional[list] = None, cwd: Path,
                          permission_engine: Optional[object] = None,
                          env: Optional[dict] = None) -> ExpansionResult:
    """Replace every `` !`cmd` `` span with that command's stripped stdout.

    finding 9 (major, h4-h5-h3c review) -- two auto-mode leftovers fixed:
    (1) the pre-fix version refused, in EVERY permission mode including
    `auto`/`bypassPermissions`, any command not named verbatim in the
    command's OWN frontmatter `allowed-tools` ("a skill whose
    `` !`git status` `` isn't in its frontmatter fails even in
    bypassPermissions" -- verified); (2) it ran via
    `subprocess.run(shell=True)` (`/bin/sh`, i.e. `dash` on Kali/Debian --
    `[[ ... ]]` fails -- or `cmd.exe` on Windows) with the harness's raw
    `os.environ`, provider API keys included, and no deny-rule/hook check
    at all.

    When `permission_engine` is supplied (every real session call site),
    each frontmatter Bash rule is first registered as a TEMPORARY session
    allow rule (mirrors the Skill tool's own tool-calling `allowed-tools`,
    cleared at the next user message -- so it still works standalone, with
    no separate user-authored rule needed), then EVERY `!cmd` span is
    routed through `permission_engine.decide("Bash", ...)` -- `auto`/
    `bypassPermissions` allow it outright, `default`/`acceptEdits`/`plan`
    honour the user's own ask/deny rules (an `ask` outcome is treated as a
    denial here: there is no interactive card at pre-execution time,
    before the model has even seen the prompt). Executed via the SAME
    shell-selection function the Bash tool itself uses (`config.paths.
    git_bash()`: `/bin/bash -lc` on POSIX, Git Bash on Windows -- never
    `/bin/sh`/`cmd.exe`) and `env` (the caller's own `tool_child_env()`-
    stripped environment -- never a raw `os.environ` read here).
    `permission_engine=None` (a caller that never built one, e.g. a
    standalone unit test) falls back to the OLD strict-frontmatter-only
    gate and raw `subprocess.run(shell=True, env=env)` for backward
    compatibility.

    An unpermitted/denied or nonzero-exit command ABORTS the whole
    invocation (D-TUI: "a failed command aborts the invocation") -- the
    first such span stops and this returns with `.error` set."""
    from rolo_claude.permissions import parse_rule

    frontmatter_bash_rules = [r.strip() for r in (allowed_tools or [])
                               if isinstance(r, str) and r.strip().startswith("Bash")]

    if permission_engine is not None:
        for rule_text in frontmatter_bash_rules:
            permission_engine.add_session_allow_rule(rule_text, temporary=True)
    else:
        allow_rules = [parse_rule(r, source="frontmatter", base_dir=Path(cwd), action="allow")
                       for r in frontmatter_bash_rules]

    out_parts = []
    pos = 0
    for m in _BACKTICK_CMD_RE.finditer(body):
        command = m.group(1)
        out_parts.append(body[pos:m.start()])

        if permission_engine is not None:
            decision = permission_engine.decide("Bash", {"command": command})
            if decision.action != "allow":
                return ExpansionResult(text="", error=(
                    f"pre-execution command not permitted: {command!r} ({decision.reason})"))
            from rolo_claude.config.paths import git_bash
            shell_path = git_bash()
            if shell_path is None:
                return ExpansionResult(text="", error=(
                    f"pre-execution command failed to start: {command!r} (no POSIX shell available)"))
            argv = [str(shell_path), "-lc", command]
        else:
            if not _preexec_command_permitted(command, allow_rules):
                return ExpansionResult(text="", error=(
                    f"pre-execution command not permitted by this command's allowed-tools: {command!r} "
                    f"(add a matching Bash(...) rule to its frontmatter)"))
            argv = command  # shell=True below

        try:
            proc = subprocess.run(argv, shell=(permission_engine is None), cwd=str(cwd), env=env,
                                   capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            return ExpansionResult(text="", error=f"pre-execution command failed to start: {command!r} ({e})")
        if proc.returncode != 0:
            return ExpansionResult(text="", error=(
                f"pre-execution command exited {proc.returncode}: {command!r}\n{proc.stderr}"))
        out_parts.append(proc.stdout.rstrip("\n"))
        pos = m.end()
    out_parts.append(body[pos:])
    return ExpansionResult(text="".join(out_parts))


def expand_command_body(body: str, args_text: str, *, allowed_tools: Optional[list] = None, cwd: Path,
                         permission_engine: Optional[object] = None, env: Optional[dict] = None,
                         claude_vars: Optional[dict] = None) -> ExpansionResult:
    """`$ARGUMENTS`/`$N` substitution, then `${CLAUDE_...}` variable
    substitution (finding 12), then `` !`cmd` `` pre-execution -- the one
    entry point custom.py/skills.py/controller.py/tools/skill.py all
    call. `permission_engine`/`env` (finding 9) are threaded straight
    through to `run_preexec_commands`; `claude_vars` to
    `substitute_claude_vars`. All optional for a caller with no real
    session (a standalone unit test)."""
    substituted = substitute_arguments(body, args_text)
    substituted = substitute_claude_vars(substituted, claude_vars)
    return run_preexec_commands(substituted, allowed_tools=allowed_tools, cwd=cwd,
                                 permission_engine=permission_engine, env=env)


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
