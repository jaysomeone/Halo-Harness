"""rolo_claude.permissions -- the permission engine (H2 scope C), per plan
D6/D-CFG's "Permission grammar and matcher" + "Rule writes" sections and
`docs/harness/claude-code-2.1.281-binary-facts.md` sec.3-5. Grammar and
modes ONLY -- rolo's "no cyber blocks" decision (plan "Decisions taken with
rolo"): **no classifier, no destructive-command list, no protected paths,
no security-tool flagging anywhere**. `auto` = allow everything not matched
by an explicit deny/ask rule; `bypassPermissions` = allow everything not
matched by a deny rule. The four manual modes (`default`, `acceptEdits`,
`plan`, `dontAsk`) keep Claude Code's own mode-table semantics because the
user selected them deliberately, but carry no extra heuristics either.

Module layout (one file, sectioned):
  1. Pattern-content helpers (star-escape handling, Bash/PowerShell content
     classification, the PowerShell alias table).
  2. Path-rule glob translation (`//abs`, `~/`, `/rel-to-source`, gitignore
     `**`/`*`/`?`/braces, allow-anchored vs deny/ask-any-depth).
  3. Bash command normalization (segment splitting, wrapper/env stripping,
     the read-only whitelist incl. git/gh subcommand maps) and WebFetch
     domain matching.
  4. `Rule` + `parse_rule` (the grammar itself, incl. MCP forms).
  5. `PermissionEngine` (`decide`, the mode table, `suggested_rules`,
     `add_allow_rule`, bare-name catalog removal).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# =============================================================================
# 1. Pattern-content helpers.
# =============================================================================

_STAR_SENTINEL = "\x00LITERAL_STAR\x00"


def _protect_escaped_stars(s: str) -> str:
    return s.replace("\\*", _STAR_SENTINEL)


def _restore_stars(s: str) -> str:
    return s.replace(_STAR_SENTINEL, "*")


def classify_pattern_content(content: str) -> dict:
    """Classify Bash/PowerShell/generic rule CONTENT (already paren+
    backslash-unescaped) into one of exact/prefix/glob/invalid [bin sec.5]:
    an unescaped trailing `:*` -> prefix (validated: must be at the end,
    prefix non-empty); trailing unescaped ` *` -> prefix (matches the bare
    form too, e.g. `Bash(ls *)` matches `ls`/`ls -la`, not `lsof`); any
    other unescaped `*` -> glob (`Bash(ls*)` DOES match `lsof`); else exact.
    `\\*` is always a literal star, never a wildcard marker."""
    protected = _protect_escaped_stars(content)
    if ":*" in protected:
        if not protected.endswith(":*"):
            return {"kind": "invalid", "error": "The :* pattern must be at the end", "value": _restore_stars(content)}
        prefix = protected[:-2]
        if prefix == "":
            return {"kind": "invalid", "error": "Prefix cannot be empty before :*", "value": _restore_stars(content)}
        return {"kind": "prefix", "value": _restore_stars(prefix)}
    if protected.endswith(" *"):
        return {"kind": "prefix", "value": _restore_stars(protected[:-2])}
    if "*" in protected:
        return {"kind": "glob", "value": _restore_stars(protected)}
    return {"kind": "exact", "value": _restore_stars(protected)}


def _pattern_matches_text(kind: str, value: str, text: str, *, case_insensitive: bool = False) -> bool:
    if case_insensitive:
        value_cmp, text_cmp = value.lower(), text.lower()
    else:
        value_cmp, text_cmp = value, text
    if kind == "exact":
        return text_cmp == value_cmp
    if kind == "prefix":
        return text_cmp == value_cmp or text_cmp.startswith(value_cmp + " ")
    if kind == "glob":
        return fnmatch.fnmatchcase(text_cmp, value_cmp)
    return False


# ---- PowerShell alias canonicalisation [bin sec.4] -------------------------

_PS_ALIASES = {
    "ls": "Get-ChildItem", "dir": "Get-ChildItem", "gci": "Get-ChildItem",
    "cat": "Get-Content", "type": "Get-Content", "gc": "Get-Content",
    "cd": "Set-Location", "sl": "Set-Location", "chdir": "Set-Location",
    "ri": "Remove-Item", "del": "Remove-Item", "rd": "Remove-Item", "rmdir": "Remove-Item",
    "rm": "Remove-Item", "erase": "Remove-Item",
    "mi": "Move-Item", "mv": "Move-Item", "move": "Move-Item",
    "ci": "Copy-Item", "cp": "Copy-Item", "cpi": "Copy-Item", "copy": "Copy-Item",
    "iex": "Invoke-Expression", "iwr": "Invoke-WebRequest", "irm": "Invoke-RestMethod",
    "%": "ForEach-Object", "foreach": "ForEach-Object",
    "?": "Where-Object", "where": "Where-Object",
    "sls": "Select-String", "select": "Select-Object",
    "gp": "Get-ItemProperty", "gm": "Get-Member", "gcm": "Get-Command",
    "gv": "Get-Variable", "sv": "Set-Variable", "gsv": "Get-Service",
    "echo": "Write-Output", "write": "Write-Output",
    "sort": "Sort-Object", "group": "Group-Object", "measure": "Measure-Object",
    "kill": "Stop-Process", "ps": "Get-Process", "pwd": "Get-Location",
    "md": "New-Item", "mkdir": "New-Item", "ni": "New-Item",
    "cls": "Clear-Host", "clear": "Clear-Host", "diff": "Compare-Object",
}
_EXE_EXT_RE = re.compile(r"\.(exe|cmd|bat|com)$", re.IGNORECASE)


def canonicalize_powershell(command: str) -> str:
    """Lowercase the first token; strip .exe/.cmd/.bat/.com when there's no
    path separator; map the alias table. The REST of the command (args)
    passes through unchanged."""
    stripped = command.strip()
    m = re.match(r"^(\S+)(.*)$", stripped, re.DOTALL)
    if not m:
        return stripped
    first, rest = m.group(1), m.group(2)
    low = first.lower()
    if "/" not in low and "\\" not in low:
        low = _EXE_EXT_RE.sub("", low)
    canonical = _PS_ALIASES.get(low, first)
    return canonical + rest


def powershell_rule_matches(kind: str, value: str, command: str) -> bool:
    """PowerShell matching is case-insensitive and tried against BOTH the
    raw and the canonicalised command [bin sec.4]."""
    canon = canonicalize_powershell(command)
    return (_pattern_matches_text(kind, value, command, case_insensitive=True)
            or _pattern_matches_text(kind, value, canon, case_insensitive=True))


# =============================================================================
# 2. Path-rule glob translation.
# =============================================================================

def _abs_posix(path) -> str:
    p = Path(path).resolve()
    s = str(p).replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", s):
        s = s[0].upper() + s[1:]
    return s.rstrip("/") or "/"


def _translate_glob_tail(tail: str) -> str:
    """gitignore-ish glob -> a regex FRAGMENT (no ^/$ anchors): `**` -> any
    depth, `*` -> one path segment, `?` -> one char (not `/`), `{a,b}` ->
    alternation; everything else is escaped literally."""
    i, n = 0, len(tail)
    out = []
    while i < n:
        c = tail[i]
        if tail[i:i + 2] == "**":
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        if c == "{":
            j = tail.find("}", i)
            if j == -1:
                out.append(re.escape(c))
                i += 1
                continue
            options = tail[i + 1:j].split(",")
            out.append("(?:" + "|".join(re.escape(o) for o in options) + ")")
            i = j + 1
            continue
        out.append(re.escape(c))
        i += 1
    return "".join(out)


def path_rule_regex(rule_value: str, base_dir: Path, *, any_depth: bool) -> "tuple[re.Pattern, bool]":
    """(compiled regex matching an ABSOLUTE posix target path, is_absolute).
    `//abs`, `~/`, and a Windows drive form name ONE specific location
    (is_absolute=True, `any_depth` irrelevant); everything else resolves
    against `base_dir` -- ALLOW callers pass any_depth=False (anchored at
    the base), DENY/ASK callers pass any_depth=True (the pattern's tail may
    start matching at any directory level under the base, gitignore-style
    for a pattern with no `/` of its own)."""
    value = rule_value
    if value.startswith("//"):
        rest = value[2:]
        full = ("/" + rest) if not re.match(r"^[A-Za-z]:", rest) else rest
        tail_pattern = _translate_glob_tail(full.replace("\\", "/").rstrip("/"))
        return re.compile(f"^{tail_pattern}(?:/.*)?$"), True
    if value.startswith("~/") or value == "~":
        full = str(Path(value).expanduser()).replace("\\", "/").rstrip("/")
        tail_pattern = _translate_glob_tail(full)
        return re.compile(f"^{tail_pattern}(?:/.*)?$"), True
    if re.match(r"^[A-Za-z]:[\\/]", value):
        full = value.replace("\\", "/").rstrip("/")
        tail_pattern = _translate_glob_tail(full)
        return re.compile(f"^{tail_pattern}(?:/.*)?$", re.IGNORECASE), True

    if value.startswith("/"):
        value = value[1:]
    elif value.startswith("./"):
        value = value[2:]
    tail = value.rstrip("/")
    base_posix = _abs_posix(base_dir)
    tail_pattern = _translate_glob_tail(tail) if tail else ""
    base_fragment = re.escape(base_posix)
    if not tail_pattern:
        return re.compile(f"^{base_fragment}(?:/.*)?$"), False
    if any_depth:
        pattern = f"^{base_fragment}/(?:.*/)?{tail_pattern}(?:/.*)?$"
    else:
        pattern = f"^{base_fragment}/{tail_pattern}(?:/.*)?$"
    return re.compile(pattern), False


# =============================================================================
# 3. Bash normalization (segments, wrappers/env, the read-only whitelist)
#    and WebFetch domain matching.
# =============================================================================

def split_bash_segments(command: str) -> list:
    """Quote/escape-aware split on `&&`, `||`, `;`, `;;`, `|`, `|&`, `&`,
    newline -- never splits inside a quoted string, a backtick span, or a
    `$( )`/`( )`/`{ }` group (heredocs aren't specially recognized; a `<<`
    body is opaque text that happens not to contain these operators in the
    common case)."""
    segments, buf = [], []
    i, n = 0, len(command)
    depth = 0
    in_single = in_double = False

    def _flush():
        segments.append("".join(buf))
        buf.clear()

    while i < n:
        c = command[i]
        if in_single:
            buf.append(c)
            in_single = c != "'"
            i += 1
            continue
        if in_double:
            if c == "\\" and i + 1 < n:
                buf.append(c); buf.append(command[i + 1]); i += 2
                continue
            buf.append(c)
            if c == '"':
                in_double = False
            i += 1
            continue
        if c == "'":
            in_single = True; buf.append(c); i += 1; continue
        if c == '"':
            in_double = True; buf.append(c); i += 1; continue
        if c == "\\" and i + 1 < n:
            buf.append(c); buf.append(command[i + 1]); i += 2
            continue
        if c == "`":
            buf.append(c); i += 1
            while i < n and command[i] != "`":
                buf.append(command[i]); i += 1
            if i < n:
                buf.append(command[i]); i += 1
            continue
        if command[i:i + 2] == "$(":
            depth += 1; buf.append(command[i]); buf.append(command[i + 1]); i += 2
            continue
        if c in "({":
            depth += 1; buf.append(c); i += 1
            continue
        if c in ")}" and depth > 0:
            depth -= 1; buf.append(c); i += 1
            continue
        if depth == 0:
            two = command[i:i + 2]
            if two in ("&&", "||", ";;", "|&"):
                _flush(); i += 2; continue
            if c in (";", "|", "&", "\n"):
                _flush(); i += 1; continue
        buf.append(c)
        i += 1
    if buf:
        _flush()
    return [s.strip() for s in segments if s.strip()]


def extract_subshell_bodies(command: str) -> list:
    """Every `$( ... )` and `` ` ... ` `` span's INNER text (DENY/ASK also
    scan these, per D-CFG: "raw + normalised + subshell bodies are all
    checked, one hit fires")."""
    bodies = []
    i, n = 0, len(command)
    while i < n:
        if command[i:i + 2] == "$(":
            depth = 1
            j = i + 2
            start = j
            while j < n and depth > 0:
                if command[j] == "(":
                    depth += 1
                elif command[j] == ")":
                    depth -= 1
                j += 1
            bodies.append(command[start:j - 1] if depth == 0 else command[start:j])
            i = j
            continue
        if command[i] == "`":
            j = command.find("`", i + 1)
            if j == -1:
                break
            bodies.append(command[i + 1:j])
            i = j + 1
            continue
        i += 1
    return bodies


# ALLOW-only normalization: these wrap a real command without changing
# WHAT it does from a permission standpoint, so an allow rule for the
# inner command should still fire; sudo/doas/pkexec/env/eval are NEVER
# stripped (D-CFG: escalation/re-exec wrappers must not be transparent to
# the matcher).
_WRAPPER_CMDS = frozenset({"timeout", "time", "nice", "nohup", "stdbuf", "command",
                            "builtin", "noglob", "nocorrect", "setsid", "ionice", "xargs"})
_WRAPPER_TAKES_VALUE = frozenset({"timeout", "nice", "ionice"})
_ENV_KEEP_RE = re.compile(
    r"^(PATH|LD_[A-Z_]*|DYLD_[A-Z_]*|BASH_ENV|ENV|ZDOTDIR|HOME|SHELL|IFS|PS4|PROMPT_COMMAND|CDPATH|GIT_[A-Z_]*)$"
)
_ENV_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=\S*(?:\s+(.*))?$")


def strip_wrappers_and_env(segment: str) -> str:
    """ALLOW-matching normalization only: repeatedly drop a leading
    wrapper command (and, for timeout/nice/ionice, its one value token) and
    a leading `NAME=value` env assignment UNLESS NAME is in the "load
    bearing" keep-list."""
    seg = segment.strip()
    changed = True
    while changed:
        changed = False
        m = _ENV_ASSIGN_RE.match(seg)
        if m and not _ENV_KEEP_RE.match(m.group(1)):
            seg = (m.group(2) or "").strip()
            changed = True
            continue
        try:
            tokens = shlex.split(seg, posix=True)
        except ValueError:
            tokens = seg.split()
        if not tokens:
            break
        first = tokens[0]
        if first in _WRAPPER_CMDS:
            rest = seg[len(first):].lstrip()
            if first in _WRAPPER_TAKES_VALUE:
                m2 = re.match(r"^(-\S+|\S+)\s+(.*)$", rest)
                if m2:
                    rest = m2.group(2)
            seg = rest.strip()
            changed = True
    return seg


# ---- read-only Bash whitelist [bin sec.3] ---------------------------------

_RO_ANY_ARGS = frozenset({
    "ls", "cat", "head", "tail", "wc", "stat", "grep", "egrep", "fgrep", "diff", "du", "df",
    "echo", "strings", "hexdump", "od", "nl", "cut", "column", "tr", "tac", "rev", "cmp",
    "basename", "dirname", "realpath", "readlink", "sha256sum", "sha1sum", "md5sum", "cd", "pwd", "which",
})
_RO_GIT_BARE = frozenset({
    "diff", "log", "show", "shortlog", "reflog", "ls-remote", "status", "blame", "ls-files",
    "remote", "merge-base", "rev-parse", "rev-list", "describe", "cat-file", "for-each-ref",
    "grep", "worktree", "tag", "branch",
})
_RO_GH_TWO_WORD = frozenset({
    ("pr", "view"), ("pr", "list"), ("pr", "diff"), ("pr", "checks"), ("pr", "status"),
    ("issue", "view"), ("issue", "list"), ("issue", "status"), ("repo", "view"),
    ("run", "list"), ("run", "view"), ("auth", "status"), ("release", "list"), ("release", "view"),
    ("workflow", "list"), ("workflow", "view"), ("label", "list"),
})


def _git_read_only(tokens: list) -> bool:
    if len(tokens) < 2:
        return False
    sub = tokens[1]
    if sub == "config":
        return "--get" in tokens[2:]
    if sub == "stash":
        return len(tokens) >= 3 and tokens[2] in ("list", "show")
    if sub == "worktree":
        return len(tokens) >= 3 and tokens[2] == "list"
    return sub in _RO_GIT_BARE


def _gh_read_only(tokens: list) -> bool:
    if len(tokens) >= 2 and tokens[1] == "search":
        return True
    return len(tokens) >= 3 and (tokens[1], tokens[2]) in _RO_GH_TWO_WORD


def is_read_only_bash_segment(segment: str) -> bool:
    """A single (already `;`/`&&`/`|`/...-split) segment, with NO
    redirection and NO subshell of its own, whose command is on the
    read-only whitelist (git/gh need a subcommand-level check; `find`
    needs no `-exec`/`-delete`)."""
    if any(tok in segment for tok in (">", "<", "$(", "`")):
        return False
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return False
    if not tokens:
        return False
    cmd = os.path.basename(tokens[0]).lower()
    cmd = _EXE_EXT_RE.sub("", cmd) if os.name == "nt" else cmd
    if cmd in ("pwd", "whoami", "alias") and len(tokens) == 1:
        return True
    if cmd in _RO_ANY_ARGS:
        return True
    if cmd == "git":
        return _git_read_only(tokens)
    if cmd == "gh":
        return _gh_read_only(tokens)
    if cmd == "find":
        return not any(t in ("-exec", "-delete", "-execdir", "-ok", "-okdir") for t in tokens)
    return False


# ---- WebFetch domain matching [bin sec.5] ---------------------------------

def domain_rule_matches(rule_value: str, url: str) -> bool:
    """`domain:*` matches all; `domain:*.x` -> subdomains of x only (not x
    itself); a bare `*` inside becomes `[^.:]*`; case-insensitive; host
    lowercased, trailing dots stripped."""
    if not rule_value.startswith("domain:"):
        return False
    pattern = rule_value[len("domain:"):].strip().lower()
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        return False
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        suffix = pattern[2:]
        rx = re.compile(r"^(?:[^.:]+\.)+" + re.escape(suffix) + r"$")
        return bool(rx.match(host))
    rx = re.compile("^" + re.escape(pattern).replace(r"\*", "[^.:]*") + "$")
    return bool(rx.match(host))


def bash_allow_matches(command: str, allow_rules: list) -> bool:
    """EVERY segment must match an allow rule OR the read-only whitelist
    (D-CFG). Returns a plain bool -- a segment satisfied ONLY by the
    whitelist (no allow rule ever needed to fire) must still count as a
    pass, so this can never be "no rule matched" == None/falsy-by-Rule;
    only a real per-segment FAILURE is falsy."""
    segments = split_bash_segments(command)
    if not segments:
        return False
    for seg in segments:
        normalized = strip_wrappers_and_env(seg)
        if is_read_only_bash_segment(normalized) or is_read_only_bash_segment(seg):
            continue
        matched = False
        for rule in allow_rules:
            if rule.kind not in ("exact", "prefix", "glob"):
                continue
            if _pattern_matches_text(rule.kind, rule.value, normalized):
                matched = True
                break
        if not matched:
            return False
    return True


def bash_deny_or_ask_matches(command: str, rules: list) -> Optional["Rule"]:
    """RAW command + every raw/normalised segment + every subshell body are
    ALL checked; one hit fires (D-CFG)."""
    candidates = [command]
    for seg in split_bash_segments(command):
        candidates.append(seg)
        candidates.append(strip_wrappers_and_env(seg))
    candidates.extend(extract_subshell_bodies(command))
    for cand in candidates:
        for rule in rules:
            if rule.kind not in ("exact", "prefix", "glob"):
                continue
            if _pattern_matches_text(rule.kind, rule.value, cand):
                return rule
    return None


# =============================================================================
# 4. Rule + parse_rule -- the grammar itself.
# =============================================================================

_PATH_TOOLS = frozenset({"Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit"})
# [bin sec.5]: content that LOOKS like a Windows path gets only the paren
# unescape and KEEPS its backslashes -- gated to path-kind tools only (a
# Bash/PowerShell command is never subject to this exception; see the
# module docstring's own reasoning in the H2 report for why).
_WIN_PATH_EXCEPTION_RE = re.compile(r"^(?:[A-Za-z]:\\|~\\|\\(?![()!#]))")


@dataclass
class Rule:
    tool: str
    kind: str  # bare|exact|prefix|glob|path|domain|mcp_server|mcp_server_all|mcp_tool|tool_glob|agent|skill|skill_prefix|param|invalid
    value: Optional[str] = None
    negated: bool = False
    param_key: Optional[str] = None
    param_value: Optional[str] = None
    source: str = ""
    base_dir: Optional[Path] = None
    raw: str = ""
    error: Optional[str] = None


def _unescape_content(content_raw: str, *, path_exception_eligible: bool) -> str:
    """`\\(`->`(`, `\\)`->`)`, then `\\\\`->`\\` [bin sec.5] -- skipped for
    a Windows-path-shaped path-rule content, which keeps its backslashes."""
    if path_exception_eligible and _WIN_PATH_EXCEPTION_RE.match(content_raw):
        return content_raw.replace("\\(", "(").replace("\\)", ")")
    return content_raw.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")


def _parse_mcp_rule(raw: str, *, source: str, action: Optional[str]) -> Optional[Rule]:
    """`mcp__server`, `mcp__server__*`, `mcp__server__tool` -- bare, NO
    parens (a parenthesised MCP rule is invalid) [bin sec.5]. Allow rules
    permit a glob only in the TOOL position after a literal
    `mcp__<server>__` prefix; deny/ask accept wildcards anywhere."""
    if not raw.startswith("mcp__"):
        return None
    if "(" in raw or ")" in raw:
        return Rule(tool=raw, kind="invalid", raw=raw, source=source,
                    error="MCP rules do not support patterns in parentheses")
    rest = raw[len("mcp__"):]
    if rest.endswith("__*"):
        server = rest[:-3]
        if action == "allow" and "*" in server:
            return Rule(tool=raw, kind="invalid", raw=raw, source=source,
                        error="allow rules permit globs only in the tool position")
        return Rule(tool=server, kind="mcp_server_all", value="*", raw=raw, source=source)
    if "__" in rest:
        server, tool_part = rest.rsplit("__", 1)
        if action == "allow" and "*" in server:
            return Rule(tool=raw, kind="invalid", raw=raw, source=source,
                        error="allow rules permit globs only in the tool position")
        return Rule(tool=server, kind="mcp_tool", value=tool_part, raw=raw, source=source)
    return Rule(tool=rest, kind="mcp_server", value=None, raw=raw, source=source)


_PARAM_RULE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(.*)$")


def parse_rule(raw: str, *, source: str = "", base_dir: Optional[Path] = None,
                action: Optional[str] = None) -> Rule:
    """Parse ONE permission rule string [bin sec.5]. `Tool(content)`: first
    `(` and the LAST `)` (which must be the final char); no parens -> bare
    (the whole tool, or -- with an unescaped `*` -- a tool-name glob,
    deny/ask only); `Bash()` (empty content) == bare. `action`
    ("deny"|"ask"|"allow"|None) only affects MCP/tool-glob/param-rule
    validation (some shapes are deny/ask only)."""
    raw = raw.strip()
    mcp = _parse_mcp_rule(raw, source=source, action=action)
    if mcp is not None:
        return mcp

    idx = raw.find("(")
    if idx == -1:
        if "*" in raw:
            if action == "allow":
                return Rule(tool=raw, kind="invalid", raw=raw, source=source,
                            error="a bare tool-name glob is deny/ask only")
            return Rule(tool=raw, kind="tool_glob", raw=raw, source=source)
        return Rule(tool=raw, kind="bare", raw=raw, source=source)

    tool = raw[:idx]
    if not raw.endswith(")") or "(" in tool or ")" in tool:
        return Rule(tool=tool, kind="invalid", raw=raw, source=source, error="malformed rule")
    content_raw = raw[idx + 1:-1]
    if content_raw == "":
        return Rule(tool=tool, kind="bare", raw=raw, source=source)  # Bash() == whole tool

    is_path_tool = tool in _PATH_TOOLS
    content = _unescape_content(content_raw, path_exception_eligible=is_path_tool)

    if tool in ("Bash", "PowerShell"):
        classified = classify_pattern_content(content)
        if classified["kind"] == "invalid":
            return Rule(tool=tool, kind="invalid", raw=raw, source=source, error=classified["error"])
        return Rule(tool=tool, kind=classified["kind"], value=classified["value"], raw=raw, source=source, base_dir=base_dir)

    if is_path_tool:
        negated = content.startswith("!")
        value = content[1:] if negated else content
        return Rule(tool=tool, kind="path", value=value, negated=negated, raw=raw, source=source, base_dir=base_dir)

    if tool == "WebFetch":
        if not content.startswith("domain:"):
            return Rule(tool=tool, kind="invalid", raw=raw, source=source, error="WebFetch rules must use domain:")
        return Rule(tool=tool, kind="domain", value=content, raw=raw, source=source)

    if tool == "Agent":
        return Rule(tool=tool, kind="agent", value=content, raw=raw, source=source)

    if tool == "Skill":
        if content.endswith(" *"):
            return Rule(tool=tool, kind="skill_prefix", value=content[:-2], raw=raw, source=source)
        return Rule(tool=tool, kind="skill", value=content, raw=raw, source=source)

    m = _PARAM_RULE_RE.match(content)
    if m:
        if action == "allow":
            return Rule(tool=tool, kind="invalid", raw=raw, source=source, error="Tool(param:value) rules are deny/ask only")
        return Rule(tool=tool, kind="param", param_key=m.group(1), param_value=m.group(2), raw=raw, source=source)

    classified = classify_pattern_content(content)
    if classified["kind"] == "invalid":
        return Rule(tool=tool, kind="invalid", raw=raw, source=source, error=classified["error"])
    return Rule(tool=tool, kind=classified["kind"], value=classified["value"], raw=raw, source=source, base_dir=base_dir)


# =============================================================================
# 5. PermissionEngine -- decide(), the mode table, suggested_rules,
#    add_allow_rule, bare-name catalog removal.
# =============================================================================

@dataclass
class Decision:
    action: str  # "allow" | "deny" | "ask"
    reason: str
    source: str = ""
    suggested_rule: Optional[str] = None
    matched_rule: Optional[Rule] = None
    # print-mode only: {"tool_name","tool_input","reason"} -- what
    # headless.py/output.py surface as `permission_denials` in the json
    # result (D6: an `ask` outcome in print mode -> deny + this).
    permission_denial: Optional[dict] = None


def _escape_rule_content(text: str) -> str:
    """`\\`->`\\\\` FIRST, then `(`->`\\(`, `)`->`\\)` -- the inverse of
    `parse_rule`'s own unescape, used by both `add_allow_rule` and
    `PermissionEngine.suggest_rule`."""
    text = text.replace("\\", "\\\\")
    return text.replace("(", "\\(").replace(")", "\\)")


def build_rule_text(tool: str, content: str) -> str:
    return f"{tool}({_escape_rule_content(content)})"


_EDIT_LIKE_BASH_CMDS = frozenset({"mkdir", "touch", "rm", "rmdir", "mv", "cp", "sed"})
_MULTI_WORD_VERBS = frozenset({"git", "npm", "npx", "pnpm", "yarn", "uv", "pip", "python",
                                "cargo", "go", "make", "docker", "gh", "kubectl", "dotnet"})


def _applicable_path_rule_source_tools(calling_tool_name: str, action: str) -> set:
    """Which rule.tool VALUES are consulted when deciding a call to
    `calling_tool_name` [D-CFG]: Read-deny blocks Edit/Write; Edit rules
    cover Write/NotebookEdit; Write(...)/Glob(...) are NEVER consulted (not
    even for a Write call -- only Edit-kind rules cover it); Glob/Grep
    themselves consult no path-kind rule in this build (mode-table only)."""
    if calling_tool_name == "Read":
        return {"Read"}
    if calling_tool_name in ("Edit", "NotebookEdit"):
        return {"Edit", "Read"} if action == "deny" else {"Edit"}
    if calling_tool_name == "Write":
        return {"Edit", "Read"} if action == "deny" else {"Edit"}
    return set()


def _bash_command_all_segments_readonly(command: str) -> bool:
    segments = split_bash_segments(command)
    return bool(segments) and all(is_read_only_bash_segment(s) for s in segments)


_MODE_TABLE = {
    "read_in_workdir":       {"default": "allow", "acceptEdits": "allow", "plan": "allow", "dontAsk": "allow"},
    "read_outside":          {"default": "ask", "acceptEdits": "ask", "plan": "ask", "dontAsk": "deny"},
    "readonly_bash":         {"default": "allow", "acceptEdits": "allow", "plan": "allow", "dontAsk": "allow"},
    "edit_write_in_workdir": {"default": "ask", "acceptEdits": "allow", "plan": "deny", "dontAsk": "deny"},
    "other":                 {"default": "ask", "acceptEdits": "ask", "plan": "deny", "dontAsk": "deny"},
}


def bare_deny_tool_names(deny_rules: list) -> set:
    """Bare tool names (kind == "bare") among DENY rules / --disallowedTools
    -- these remove the tool from the session's catalog entirely, for the
    whole session, rather than being consulted per call [D-CFG]."""
    return {r.tool for r in deny_rules if r.kind == "bare"}


def add_allow_rule(rule_text: str, destination: str, *, cwd: Path) -> Path:
    """Write `rule_text` into `<dest>/settings[.local].json`'s
    `permissions.allow` list, tmp + `os.replace` (D-CFG). `destination` is
    "local" | "project" | "user"; "session" is in-memory only and must be
    handled by the CALLER (nothing to write to disk)."""
    if destination == "local":
        path = Path(cwd) / ".claude" / "settings.local.json"
    elif destination == "project":
        path = Path(cwd) / ".claude" / "settings.json"
    elif destination == "user":
        from rolo_claude.config.paths import claude_config_dir
        path = claude_config_dir() / "settings.json"
    else:
        raise ValueError(f"add_allow_rule: unsupported destination {destination!r} (use 'session' in-memory instead)")

    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
        data["permissions"] = perms
    allow_list = perms.get("allow")
    if not isinstance(allow_list, list):
        allow_list = []
        perms["allow"] = allow_list
    if rule_text not in allow_list:
        allow_list.append(rule_text)

    tmp_path = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)
    return path


class PermissionEngine:
    """`decide(tool_name, tool_input, tool) -> Decision`. `deny_rules`/
    `ask_rules`/`allow_rules` are flat, ALREADY-precedence-assembled lists
    (the caller -- agent/loop.py / cli.py -- is responsible for gathering
    them from Settings + `--allowedTools`/`--disallowedTools` +
    `projects[cwd].allowedTools` + session rules, dropping untrusted
    project/local ALLOW entries before ever constructing this engine;
    DENY/ASK are never trust-gated). `mode` is one of default|acceptEdits|
    plan|auto|dontAsk|bypassPermissions."""

    def __init__(self, *, deny_rules=None, ask_rules=None, allow_rules=None,
                 mode: str = "default", cwd, extra_dirs=None, print_mode: bool = False):
        self.deny_rules = list(deny_rules or [])
        self.ask_rules = list(ask_rules or [])
        self.allow_rules = list(allow_rules or [])
        self.mode = mode
        self.cwd = Path(cwd)
        self.extra_dirs = [Path(d) for d in (extra_dirs or [])]
        self.print_mode = print_mode

    def working_dirs(self) -> list:
        return [self.cwd] + self.extra_dirs

    def _in_working_dirs(self, path: Path) -> bool:
        target = _abs_posix(path)
        for wd in self.working_dirs():
            wd_posix = _abs_posix(wd)
            if target == wd_posix or target.startswith(wd_posix + "/"):
                return True
        return False

    def _resolve_target_path(self, path_str: str) -> Optional[Path]:
        if not path_str:
            return None
        p = Path(path_str)
        return p if p.is_absolute() else (self.cwd / p)

    # ---- generic (non-Bash) rule matching --------------------------------

    def _rule_matches(self, rule: Rule, tool_name: str, tool_input: dict, tool, *, action: str) -> bool:
        if rule.kind == "invalid":
            return False
        any_depth = action in ("deny", "ask")

        if rule.kind == "bare":
            return rule.tool == tool_name
        if rule.kind == "tool_glob":
            return fnmatch.fnmatchcase(tool_name, rule.tool)
        if rule.kind == "mcp_server":
            return tool_name == f"mcp__{rule.tool}" or tool_name.startswith(f"mcp__{rule.tool}__")
        if rule.kind == "mcp_server_all":
            return tool_name.startswith(f"mcp__{rule.tool}__")
        if rule.kind == "mcp_tool":
            prefix = f"mcp__{rule.tool}__"
            if tool_name.startswith(prefix):
                return fnmatch.fnmatchcase(tool_name[len(prefix):], rule.value or "")
            return fnmatch.fnmatchcase(tool_name, f"mcp__{rule.tool}__{rule.value}")
        if rule.kind == "agent":
            return tool_name == "Agent" and (tool_input.get("subagent_type") or tool_input.get("agent_type")) == rule.value
        if rule.kind == "skill":
            return tool_name == "Skill" and tool_input.get("skill") == rule.value
        if rule.kind == "skill_prefix":
            val = tool_input.get("skill", "")
            return tool_name == "Skill" and (val == rule.value or val.startswith(rule.value + " "))
        if rule.kind == "domain":
            return tool_name == "WebFetch" and domain_rule_matches(rule.value, tool_input.get("url", ""))
        if rule.kind == "param":
            return tool_name == rule.tool and str(tool_input.get(rule.param_key)) == rule.param_value
        if rule.kind == "path":
            applicable = _applicable_path_rule_source_tools(tool_name, action)
            if rule.tool not in applicable:
                return False
            path_str = tool.permission_content(tool_input) if tool is not None else tool_input.get("file_path", "")
            target = self._resolve_target_path(path_str)
            if target is None:
                return False
            base = rule.base_dir or self.cwd
            regex, _is_abs = path_rule_regex(rule.value, base, any_depth=any_depth)
            return bool(regex.match(_abs_posix(target))) != rule.negated
        if rule.kind in ("exact", "prefix", "glob"):
            if rule.tool != tool_name:
                return False
            if tool_name == "PowerShell":
                return powershell_rule_matches(rule.kind, rule.value, tool_input.get("command", ""))
            content = tool.permission_content(tool_input) if tool is not None else ""
            return _pattern_matches_text(rule.kind, rule.value, content)
        return False

    def _match_any(self, rules: list, tool_name: str, tool_input: dict, tool, *, action: str) -> Optional[Rule]:
        for rule in rules:
            if self._rule_matches(rule, tool_name, tool_input, tool, action=action):
                return rule
        return None

    # ---- Bash gets its OWN path: "every segment" is a property of the
    # WHOLE allow-rule list, not any one rule in isolation, so it can never
    # be answered by _match_any's one-rule-at-a-time loop. -----------------

    def _decide_bash(self, tool_input: dict) -> Decision:
        command = tool_input.get("command", "")
        bash_rules = lambda rs: [r for r in rs if r.tool == "Bash"]  # noqa: E731

        deny_bare = [r for r in bash_rules(self.deny_rules) if r.kind == "bare"]
        if deny_bare:
            return Decision("deny", "denied by rule 'Bash' (whole tool)", source=deny_bare[0].source, matched_rule=deny_bare[0])
        hit = bash_deny_or_ask_matches(command, [r for r in bash_rules(self.deny_rules) if r.kind in ("exact", "prefix", "glob")])
        if hit is not None:
            return Decision("deny", f"denied by rule {hit.raw!r}", source=hit.source, matched_rule=hit)

        if self.mode != "bypassPermissions":
            ask_bare = [r for r in bash_rules(self.ask_rules) if r.kind == "bare"]
            if ask_bare:
                return self._resolve_ask("Bash", tool_input, None, ask_bare[0])
            hit = bash_deny_or_ask_matches(command, [r for r in bash_rules(self.ask_rules) if r.kind in ("exact", "prefix", "glob")])
            if hit is not None:
                return self._resolve_ask("Bash", tool_input, None, hit)

        if self.mode in ("auto", "bypassPermissions"):
            return Decision("allow", "mode allows (no deny/ask rule matched)", source="mode")

        allow_bare = [r for r in bash_rules(self.allow_rules) if r.kind == "bare"]
        allow_patterned = [r for r in bash_rules(self.allow_rules) if r.kind in ("exact", "prefix", "glob")]
        if allow_bare or bash_allow_matches(command, allow_patterned):
            return Decision("allow", "allowed by an explicit Bash rule", source="rules")

        return self._mode_table_decision("Bash", tool_input, None)

    def _resolve_ask(self, tool_name: str, tool_input: dict, tool, hit: Rule) -> Decision:
        if self.mode == "dontAsk":
            return Decision("deny", f"dontAsk mode converts ask to deny (matched {hit.raw!r})", source=hit.source, matched_rule=hit)
        if self.print_mode:
            suggestion = self.suggest_rule(tool_name, tool_input)
            reason = f"matched ask rule {hit.raw!r}"
            denial = {"tool_name": tool_name, "tool_input": tool_input, "reason": reason, "suggested_rule": suggestion}
            return Decision("deny", reason + " -- no interactive prompt in print mode", source=hit.source,
                             matched_rule=hit, suggested_rule=suggestion, permission_denial=denial)
        return Decision("ask", f"matched ask rule {hit.raw!r}", source=hit.source, matched_rule=hit)

    # ---- the mode table (only reached once no deny/ask/allow rule fired) --

    def _categorize(self, tool_name: str, tool_input: dict, tool) -> str:
        if tool_name == "Read":
            path_str = tool.permission_content(tool_input) if tool is not None else tool_input.get("file_path", "")
            target = self._resolve_target_path(path_str)
            return "read_in_workdir" if (target is None or self._in_working_dirs(target)) else "read_outside"
        if tool_name in ("Glob", "Grep"):
            target = self._resolve_target_path(tool_input.get("path", ""))
            return "read_in_workdir" if (target is None or self._in_working_dirs(target)) else "read_outside"
        if tool_name == "Bash":
            command = tool_input.get("command", "")
            if _bash_command_all_segments_readonly(command):
                return "readonly_bash"
            if self._bash_is_edit_like_in_workdir(command):
                return "edit_write_in_workdir"
            return "other"
        if tool_name in ("Edit", "Write", "NotebookEdit"):
            target = self._resolve_target_path(tool_input.get("file_path", ""))
            return "edit_write_in_workdir" if (target is not None and self._in_working_dirs(target)) else "other"
        return "other"

    def _bash_is_edit_like_in_workdir(self, command: str) -> bool:
        segments = split_bash_segments(command)
        if not segments:
            return False
        for seg in segments:
            try:
                tokens = shlex.split(seg, posix=True)
            except ValueError:
                return False
            if not tokens or tokens[0] not in _EDIT_LIKE_BASH_CMDS:
                return False
            for tok in tokens[1:]:
                if tok.startswith("-"):
                    continue
                candidate = Path(tok)
                target = candidate if candidate.is_absolute() else (self.cwd / candidate)
                if not self._in_working_dirs(target):
                    return False
        return True

    def _mode_table_decision(self, tool_name: str, tool_input: dict, tool) -> Decision:
        if tool_name == "AskUserQuestion":
            if self.mode == "dontAsk":
                return Decision("deny", "AskUserQuestion errors on a tool call in dontAsk mode", source="mode")
            return Decision("allow", "AskUserQuestion is allowed in every mode except dontAsk", source="mode")
        category = self._categorize(tool_name, tool_input, tool)
        action = _MODE_TABLE.get(category, _MODE_TABLE["other"]).get(self.mode, "ask")
        if action == "ask" and self.print_mode:
            suggestion = self.suggest_rule(tool_name, tool_input)
            reason = f"mode {self.mode!r} would ask for this {tool_name} call ({category})"
            denial = {"tool_name": tool_name, "tool_input": tool_input, "reason": reason, "suggested_rule": suggestion}
            return Decision("deny", reason + " -- no interactive prompt in print mode", source="mode",
                             suggested_rule=suggestion, permission_denial=denial)
        reason = f"mode {self.mode!r} {action}s {category} calls (no explicit rule matched)"
        return Decision(action, reason, source="mode")

    # ---- entry point ------------------------------------------------------

    def decide(self, tool_name: str, tool_input: Optional[dict] = None, tool=None) -> Decision:
        tool_input = tool_input or {}
        if tool_name == "Bash":
            return self._decide_bash(tool_input)

        hit = self._match_any(self.deny_rules, tool_name, tool_input, tool, action="deny")
        if hit is not None:
            return Decision("deny", f"denied by rule {hit.raw!r}", source=hit.source, matched_rule=hit)

        if self.mode != "bypassPermissions":
            hit = self._match_any(self.ask_rules, tool_name, tool_input, tool, action="ask")
            if hit is not None:
                return self._resolve_ask(tool_name, tool_input, tool, hit)

        if self.mode in ("auto", "bypassPermissions"):
            return Decision("allow", "mode allows (no deny/ask rule matched)", source="mode")

        hit = self._match_any(self.allow_rules, tool_name, tool_input, tool, action="allow")
        if hit is not None:
            return Decision("allow", f"allowed by rule {hit.raw!r}", source=hit.source, matched_rule=hit)

        return self._mode_table_decision(tool_name, tool_input, tool)

    # ---- suggested_rules ---------------------------------------------------

    def suggest_rule(self, tool_name: str, tool_input: Optional[dict] = None) -> Optional[str]:
        tool_input = tool_input or {}
        if tool_name == "Bash":
            command = tool_input.get("command", "").strip()
            if not command:
                return None
            try:
                tokens = shlex.split(command, posix=True)
            except ValueError:
                tokens = command.split()
            if not tokens:
                return None
            verb = tokens[0]
            if verb in _MULTI_WORD_VERBS and len(tokens) > 1:
                return f"Bash({_escape_rule_content(verb + ' ' + tokens[1])}:*)"
            return f"Bash({_escape_rule_content(verb)}:*)"
        if tool_name == "PowerShell":
            command = tool_input.get("command", "").strip()
            canon = canonicalize_powershell(command) if command else ""
            cmdlet = canon.split(None, 1)[0] if canon else ""
            return f"PowerShell({_escape_rule_content(cmdlet)}:*)" if cmdlet else None
        if tool_name == "Read":
            path = tool_input.get("file_path", "")
            return f"Read({_escape_rule_content(path)})" if path else None
        if tool_name in ("Edit", "Write"):
            path = tool_input.get("file_path", "")
            if not path:
                return None
            rel_dir = Path(path).parent
            try:
                rel = rel_dir.resolve().relative_to(self.cwd.resolve())
                rel_str = str(rel).replace("\\", "/")
                return f"Edit({_escape_rule_content(rel_str)}/**)"
            except ValueError:
                return f"Edit(//{_escape_rule_content(_abs_posix(rel_dir))}/**)"
        if tool_name == "WebFetch":
            from urllib.parse import urlparse
            host = urlparse(tool_input.get("url", "")).hostname
            return f"WebFetch(domain:{host})" if host else None
        if tool_name.startswith("mcp__"):
            parts = tool_name.split("__")
            if len(parts) >= 3:
                return f"mcp__{parts[1]}__{parts[2]}"
            if len(parts) == 2:
                return f"mcp__{parts[1]}"
            return None
        if tool_name == "Agent":
            t = tool_input.get("subagent_type") or tool_input.get("agent_type")
            return f"Agent({t})" if t else None
        if tool_name == "Skill":
            s = tool_input.get("skill")
            return f"Skill({s})" if s else None
        return tool_name


# =============================================================================
# 6. Loop/CLI integration helpers: assembling rule lists from Settings +
#    CLI flags, and freezing a session's tool catalog.
# =============================================================================

def split_tool_rule_list(text: str) -> list:
    """Split an `--allowedTools`/`--disallowedTools`-shaped VALUE (comma or
    space separated, e.g. `"Bash(git *) Edit"` [bin sec.1]) into individual
    rule strings, never splitting on whitespace/commas INSIDE a rule's own
    `Tool(...)` parens."""
    parts, buf, depth = [], [], 0
    for c in text:
        if c == "(":
            depth += 1
            buf.append(c)
            continue
        if c == ")":
            depth = max(0, depth - 1)
            buf.append(c)
            continue
        if depth == 0 and (c == "," or c.isspace()):
            if buf:
                parts.append("".join(buf))
                buf = []
            continue
        buf.append(c)
    if buf:
        parts.append("".join(buf))
    return parts


def build_rules_from_settings(settings, *, cwd: Path, claude_json_allowed_tools=None,
                               cli_allow=None, cli_disallow=None,
                               session_allow=None) -> "tuple[list, list, list]":
    """Assemble (deny, ask, allow) `Rule` lists for a `PermissionEngine`
    from a resolved `Settings` object (already trust-filtered -- an
    untrusted project/local layer's `permissions.allow` was dropped before
    `settings.permissions_allow` was ever computed, so this function
    doesn't need to know about trust itself) plus the CLI/claude.json/
    session sources D-CFG names. `claude_json_allowed_tools` is
    `lookup_project(claude_json, cwd).get("allowedTools")`; `cli_allow`/
    `cli_disallow` are raw strings from `--allowedTools`/`--disallowedTools`
    (already split via `split_tool_rule_list`); `session_allow` is
    in-memory-only rules accumulated this run (e.g. a future interactive
    `allow_session` reply)."""
    deny = [parse_rule(r, source="settings", base_dir=cwd, action="deny") for r in settings.permissions_deny]
    ask = [parse_rule(r, source="settings", base_dir=cwd, action="ask") for r in settings.permissions_ask]
    allow = [parse_rule(r, source="settings", base_dir=cwd, action="allow") for r in settings.permissions_allow]
    for name in (claude_json_allowed_tools or []):
        allow.append(parse_rule(name, source="claude_json", base_dir=cwd, action="allow"))
    for r in (cli_disallow or []):
        deny.append(parse_rule(r, source="cli_disallow", base_dir=cwd, action="deny"))
    for r in (cli_allow or []):
        allow.append(parse_rule(r, source="cli_allow", base_dir=cwd, action="allow"))
    for r in (session_allow or []):
        allow.append(parse_rule(r, source="session", base_dir=cwd, action="allow"))
    return deny, ask, allow


def normalize_permission_mode(raw: Optional[str]) -> str:
    """`manual` is Claude Code's display alias for `default` [bin sec.1];
    None/empty -> "default" (Claude Code's own factory default)."""
    if not raw:
        return "default"
    return "default" if raw == "manual" else raw


def freeze_tool_registry(registry, *, tools_flag: Optional[str] = None,
                          disallowed_tools: Optional[list] = None, deny_rules: Optional[list] = None):
    """Apply `--tools` (`""` -> none, `"default"`/None -> unchanged, a name
    list -> exactly those) and then bare-name catalog removal (deny rules'
    bare tool names + any bare `--disallowedTools` entries) -- called ONCE
    at session start; the result is the session's FROZEN catalog for its
    whole lifetime [D-CFG]."""
    if tools_flag is not None and tools_flag.strip() != "" and tools_flag.strip().lower() != "default":
        names = split_tool_rule_list(tools_flag)
        registry = registry.filtered(names)
    elif tools_flag is not None and tools_flag.strip() == "":
        registry = registry.filtered([])

    bare = set(bare_deny_tool_names(deny_rules or []))
    for raw in (disallowed_tools or []):
        if "(" not in raw:
            bare.add(raw.strip())
    if bare:
        registry = registry.without(bare)
    return registry
