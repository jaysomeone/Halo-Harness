"""halo_harness.config.claude_md -- CLAUDE.md / AGENTS.md instruction
discovery, comment stripping, and @import expansion (plan D-CFG). Hand-
written (not drafted): the fence-detection logic needs literal triple-
backtick strings, which repeatedly confused the OpenRouter-drafting
pipeline's own code-fence extraction -- see wip/harness/spec-H0-config-
claude_md.md for the full behavioral spec this implements.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from halo_harness.config.paths import claude_config_dir, home, managed_dir

_MAX_IMPORT_HOPS = 4
_MAX_FILE_BYTES = 4 * 1024 * 1024  # 4 MiB

# `(?<![\w`@])@((?:~/|\.{1,2}/|//?)?[^\s`'"()<>]+)` -- an @mention that is
# NOT preceded by a word char, backtick, or another '@' (so "user@host" or
# "@@x" don't trigger), capturing a path-shaped token.
_IMPORT_RE = re.compile(r"(?<![\w`@])@((?:~/|\.{1,2}/|//?)?[^\s`'\"()<>]+)")

# A line that (after stripping leading whitespace) opens or closes a fenced
# code block: three-or-more of the SAME fence character (backtick or tilde).
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")


@dataclass
class InstructionFile:
    path: Path
    text: str
    source: str


@dataclass
class ScopedRule:
    """A `.claude/rules/*.md` file whose frontmatter has a `paths:` glob (or
    comma-separated string) -- loaded on first matching Read/Edit/Glob
    rather than unconditionally at launch [bin sec.7, finding 9], unlike an
    unscoped rule (which behaves exactly like a CLAUDE.md and lands in
    `InstructionBundle.files` instead)."""
    path: Path
    text: str
    patterns: list


@dataclass
class InstructionBundle:
    files: list = field(default_factory=list)
    scoped_rules: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def render(self) -> str:
        if not self.files:
            return ""
        return "\n\n".join(f"## {f.path}\n\n{f.text}" for f in self.files)

    def scoped_rules_for(self, touched_path) -> list:
        """Rules whose `paths:` glob matches `touched_path` (relative-to-cwd
        semantics aren't re-derived here -- callers pass whatever path shape
        the rule's own glob was written against, matched via fnmatch)."""
        touched = str(touched_path).replace("\\", "/")
        return [r for r in self.scoped_rules if any(fnmatch.fnmatch(touched, pat) for pat in r.patterns)]


# ---- fence-aware helpers ----------------------------------------------------

def _fenced_line_mask(text: str) -> list:
    """Return a list, one bool per line of `text.splitlines()`, True when
    that line is INSIDE a fenced code block (the fence delimiter lines
    themselves count as inside, matching how a comment/import touching a
    delimiter line should still be left alone)."""
    lines = text.splitlines()
    mask = [False] * len(lines)
    fence_char = None
    fence_len = 0
    in_fence = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        m = _FENCE_RE.match(stripped)
        if m:
            token = m.group(1)
            if not in_fence:
                in_fence = True
                fence_char = token[0]
                fence_len = len(token)
                mask[i] = True
            elif token[0] == fence_char and len(token) >= fence_len:
                mask[i] = True
                in_fence = False
                fence_char = None
                fence_len = 0
            else:
                mask[i] = True  # a fence-shaped line of the WRONG char, still inside
        else:
            mask[i] = in_fence
    return mask


def strip_block_html_comments(text: str) -> str:
    """Remove <!-- ... --> spans that cover one or more WHOLE lines, leaving
    a comment that sits inside a fenced code block untouched. Works line-
    oriented: a comment is only stripped when both its opening `<!--` and
    closing `-->` are found outside any fence; an inline (same-line,
    non-block) comment is left alone -- this project's own CLAUDE.md-style
    files only ever use the block form for this purpose."""
    lines = text.splitlines(keepends=True)
    mask = _fenced_line_mask(text)

    # Work on the whole non-fenced text as one blob so a comment spanning
    # multiple lines is still caught, then re-stitch with the fenced lines
    # left untouched.
    out = []
    i = 0
    n = len(lines)
    while i < n:
        if mask[i]:
            out.append(lines[i])
            i += 1
            continue
        # Not fenced: look for a comment start on this (or the running)
        # non-fenced text starting here.
        line = lines[i]
        start_idx = line.find("<!--")
        if start_idx == -1:
            out.append(line)
            i += 1
            continue
        # Search forward (only across further NON-fenced lines) for -->.
        buf = line
        j = i
        end_idx = buf.find("-->", start_idx)
        while end_idx == -1 and j + 1 < n and not mask[j + 1]:
            j += 1
            buf += lines[j]
            end_idx = buf.find("-->", start_idx)
        if end_idx == -1:
            # No closer found before EOF or a fence boundary -- leave as-is.
            out.append(line)
            i += 1
            continue
        cleaned = buf[:start_idx] + buf[end_idx + 3:]
        out.append(cleaned)
        i = j + 1
    return "".join(out)


def _is_inside_inline_code(line: str, pos: int) -> bool:
    """True if character index `pos` in `line` falls inside a `` `...` ``
    inline code span (simple odd/even backtick-run counting)."""
    count_before = 0
    idx = 0
    while idx < pos:
        if line[idx] == "`":
            run = 1
            while idx + run < len(line) and line[idx + run] == "`":
                run += 1
            count_before += 1
            idx += run
        else:
            idx += 1
    return count_before % 2 == 1


def _find_imports(text: str) -> list:
    """Return [(match_text_without_leading_@, start, end), ...] for every
    @import token in `text` that is outside a fenced block and outside an
    inline code span."""
    mask = _fenced_line_mask(text)
    lines = text.splitlines(keepends=True)
    bare_lines = text.splitlines()
    results = []
    offset = 0
    for i, line in enumerate(lines):
        bare = bare_lines[i] if i < len(bare_lines) else line.rstrip("\n")
        if not mask[i]:
            for m in _IMPORT_RE.finditer(bare):
                if not _is_inside_inline_code(bare, m.start()):
                    results.append(m.group(1))
        offset += len(line)
    return results


# ---- import expansion -------------------------------------------------------

def _resolve_import_target(token: str, importer_dir: Path) -> Path:
    if token.startswith("~/"):
        return (home() / token[2:]).resolve()
    if token.startswith("//"):
        return Path(token[1:]).resolve()
    if token.startswith("/"):
        return Path(token).resolve()
    return (importer_dir / token).resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _expand_one(
    file_path: Path,
    text: str,
    source: str,
    *,
    cwd: Path,
    trusted: bool,
    skip_unapproved_silently: bool,
    warnings: list,
    visited: set,
    hop: int,
    out: list,
) -> None:
    """Append `InstructionFile(file_path, text, source)` to `out`, then
    recursively append any @imports it contains (already comment-stripped
    text is expected)."""
    out.append(InstructionFile(path=file_path, text=text, source=source))
    if hop >= _MAX_IMPORT_HOPS:
        return

    home_claude = claude_config_dir().resolve()
    for token in _find_imports(text):
        target = _resolve_import_target(token, file_path.parent)
        if target in visited:
            continue  # cycle-safe: skip silently
        if not target.exists() or not target.is_file():
            warnings.append(f"import not found: {target} (from {file_path})")
            continue
        try:
            size = target.stat().st_size
        except OSError:
            warnings.append(f"import not found: {target} (from {file_path})")
            continue
        if size > _MAX_FILE_BYTES:
            warnings.append(f"skipped {target} (larger than 4 MiB)")
            continue
        outside_cwd = not _is_within(target, cwd.resolve())
        outside_home_claude = not _is_within(target, home_claude)
        if outside_cwd and outside_home_claude and not trusted:
            if not skip_unapproved_silently:
                warnings.append(f"import not approved (untrusted, outside cwd/~/.claude): {target} (from {file_path})")
            continue
        try:
            imported_text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            warnings.append(f"import not found: {target} (from {file_path})")
            continue
        imported_text = strip_block_html_comments(imported_text)
        visited.add(target)
        _expand_one(
            target, imported_text, f"import:{target}",
            cwd=cwd, trusted=trusted, skip_unapproved_silently=skip_unapproved_silently,
            warnings=warnings, visited=visited, hop=hop + 1, out=out,
        )


# ---- top-level file loading --------------------------------------------------

def _load_one(
    path: Path, source: str, *, cwd: Path, trusted: bool, skip_unapproved_silently: bool,
    warnings: list, out: list, seen: set,
) -> bool:
    """Load one root-level instruction file (not itself an @import target)
    if it exists and passes the size check; returns True iff it was loaded
    (used by the AGENTS.md-fallback / CLAUDE.md-presence tracking above).
    `seen` is ONE set shared across the WHOLE discover_instructions() call
    (finding 10) -- a file already loaded anywhere (managed/user/an earlier
    ancestor/an earlier import) is silently skipped here instead of
    reappearing, and the same set backs @import cycle-safety in
    `_expand_one` so an import shared by two different root files is only
    ever included once."""
    resolved = path.resolve()
    if resolved in seen:
        return False
    if not path.exists() or not path.is_file():
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size > _MAX_FILE_BYTES:
        warnings.append(f"skipped {path} (larger than 4 MiB)")
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    text = strip_block_html_comments(text)
    seen.add(resolved)
    _expand_one(
        path, text, source, cwd=cwd, trusted=trusted,
        skip_unapproved_silently=skip_unapproved_silently, warnings=warnings,
        visited=seen, hop=0, out=out,
    )
    return True


def _ancestors_root_to_leaf(cwd: Path) -> list:
    """cwd and every ancestor, root-first, EXCLUDING the bare filesystem
    root itself (`C:\\`, `/`) -- Claude Code never treats the root as a
    project directory [finding 9]. Test seam: BRIDGE_TEST_ANCESTOR_ROOT
    stops the walk at (and includes) that directory instead of the real
    filesystem root -- otherwise a fixture built under the OS temp dir
    (e.g. `C:\\Users\\rolo\\AppData\\Local\\Temp\\...`) would also probe
    real ancestors like the actual `C:\\Users\\rolo` for a CLAUDE.md that
    has nothing to do with the fixture (finding 14)."""
    cwd = cwd.resolve()
    boundary = os.environ.get("BRIDGE_TEST_ANCESTOR_ROOT")
    chain = [cwd] + list(cwd.parents)
    if boundary:
        boundary_resolved = Path(boundary).resolve()
        trimmed = []
        for p in chain:
            trimmed.append(p)
            if p == boundary_resolved:
                break
        chain = trimmed
        chain.reverse()
        return chain
    chain.reverse()  # root first
    return chain[1:] if len(chain) > 1 else chain


def _excluded(path: Path, patterns: list) -> bool:
    if not patterns:
        return False
    norm = str(path.resolve()).replace("\\", "/")
    return any(fnmatch.fnmatch(norm, pat) for pat in patterns)


def _split_paths_field(value) -> list:
    """A rule's `paths:` frontmatter is documented as "globs (list or comma
    string, braces)" [bin sec.7] -- accept either shape."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return []


def _load_rules_dir(
    rules_dir: Path, source: str, *, cwd: Path, trusted: bool, skip_unapproved_silently: bool,
    warnings: list, out: list, scoped_out: list, seen: set,
) -> None:
    """Load every `*.md` under `rules_dir` (recursively): a file with a
    `paths:` frontmatter key is SCOPED (appended to `scoped_out`, not
    rendered at launch); an unscoped rule loads immediately like a CLAUDE.md
    file [bin sec.7: "unscoped rules load at launch like .claude/CLAUDE.md"]."""
    if not rules_dir.is_dir():
        return
    for path in sorted(rules_dir.rglob("*.md")):
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                warnings.append(f"skipped {path} (larger than 4 MiB)")
                continue
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        from halo_harness.config.frontmatter import parse as parse_frontmatter
        fm, body = parse_frontmatter(raw)
        patterns = _split_paths_field(fm.get("paths")) if isinstance(fm, dict) else []
        seen.add(resolved)
        text = strip_block_html_comments(body if fm else raw)
        if patterns:
            scoped_out.append(ScopedRule(path=path, text=text, patterns=patterns))
        else:
            _expand_one(path, text, source, cwd=cwd, trusted=trusted,
                        skip_unapproved_silently=skip_unapproved_silently,
                        warnings=warnings, visited=seen, hop=0, out=out)


def discover_instructions(
    cwd,
    settings,
    trusted: bool,
    bare: bool = False,
    skip_unapproved_silently: bool = False,
) -> InstructionBundle:
    """managed -> ~/.claude/CLAUDE.md + ~/.claude/rules/** -> root-to-leaf
    walk (CLAUDE.md / .claude/CLAUDE.md / AGENTS.md fallback / .claude/rules/**
    / CLAUDE.local.md, at EVERY ancestor, filesystem root excluded) [bin
    sec.7, finding 9]; one `seen` path-set for the whole call dedups a file
    reachable through more than one route (finding 10, e.g. cwd==home makes
    `~/.claude/CLAUDE.md` and the home ancestor's `.claude/CLAUDE.md`
    literally the same file). `instructionFiles=="managed-only"` suppresses
    everything below the managed tier."""
    if bare:
        return InstructionBundle(files=[], scoped_rules=[], warnings=[])

    cwd = Path(cwd)
    claude_md_excludes = list(getattr(settings, "claude_md_excludes", None) or [])
    instruction_files = getattr(settings, "instruction_files", None) or "claude-md-or-agents-md"

    warnings: list = []
    files: list = []
    scoped_rules: list = []
    seen: set = set()
    common = dict(cwd=cwd, trusted=trusted, skip_unapproved_silently=skip_unapproved_silently,
                  warnings=warnings, seen=seen)

    # 1. Managed CLAUDE.md -- never subject to claudeMdExcludes, always loaded.
    _load_one(managed_dir() / "CLAUDE.md", "managed", out=files, **common)
    if instruction_files == "managed-only":
        return InstructionBundle(files=files, scoped_rules=scoped_rules, warnings=warnings)

    # 2. ~/.claude/CLAUDE.md + ~/.claude/rules/** (loaded first [bin sec.7]).
    user_path = claude_config_dir() / "CLAUDE.md"
    if not _excluded(user_path, claude_md_excludes):
        _load_one(user_path, "user", out=files, **common)
    elif user_path.exists():
        warnings.append(f"excluded by claudeMdExcludes: {user_path}")
    _load_rules_dir(claude_config_dir() / "rules", "user:rules", out=files, scoped_out=scoped_rules, **common)

    # 3. Root-to-leaf ancestor walk.
    for directory in _ancestors_root_to_leaf(cwd):
        found_claude_md = False
        for candidate, tag in ((directory / "CLAUDE.md", f"project:{directory}"),
                                (directory / ".claude" / "CLAUDE.md", f"project:{directory}/.claude")):
            if candidate.exists():
                if _excluded(candidate, claude_md_excludes):
                    warnings.append(f"excluded by claudeMdExcludes: {candidate}")
                    continue
                if _load_one(candidate, tag, out=files, **common):
                    found_claude_md = True

        want_agents = (
            instruction_files == "claude-md-and-agents-md"
            or (instruction_files == "claude-md-or-agents-md" and not found_claude_md)
        )
        if want_agents and instruction_files != "claude-md":
            for candidate, tag in ((directory / "AGENTS.md", f"project:{directory}"),
                                    (directory / ".claude" / "AGENTS.md", f"project:{directory}/.claude")):
                if candidate.exists() and candidate.resolve() not in seen:
                    if _excluded(candidate, claude_md_excludes):
                        warnings.append(f"excluded by claudeMdExcludes: {candidate}")
                        continue
                    _load_one(candidate, tag + ":agents", out=files, **common)

        # .claude/rules/** at THIS ancestor, then CLAUDE.local.md at THIS
        # ancestor (finding 9: per-ancestor, not just once at cwd).
        _load_rules_dir(directory / ".claude" / "rules", f"project:{directory}/.claude/rules",
                        out=files, scoped_out=scoped_rules, **common)
        local_path = directory / "CLAUDE.local.md"
        if local_path.exists():
            if _excluded(local_path, claude_md_excludes):
                warnings.append(f"excluded by claudeMdExcludes: {local_path}")
            else:
                _load_one(local_path, f"local:{directory}", out=files, **common)

    return InstructionBundle(files=files, scoped_rules=scoped_rules, warnings=warnings)
