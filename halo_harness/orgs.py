"""halo_harness.orgs -- Halo 2.0.2 round 2 (brief B): organizations, a tree
of positions saved as `~/.halo/orgs/<name>.json`. Each position is a role
(or a pinned model) plus instructions and the titles of the positions it
may delegate to (`reports`) -- `agent/subagent.py::run_org_call` turns this
into real `config.agents_md.AgentSpec` objects and runs the root position
through the existing Agent-tool machinery, each child restricted to its own
`reports` (`AgentSpec.delegate_restriction`, read by `_build_child_session`).

Schema: `{"name", "description", "positions": [{"title", "role"?, "model"?,
"effort"?, "instructions"?, "tools"?, "reports": [title, ...]}], "max_
concurrent"?}`. Exactly one position is the ROOT: the one title that never
appears in any OTHER position's own `reports` list (nobody is its boss).
`reports` may name the SAME title from more than one position (a later/
other delegator reusing an earlier position -- release-flow's `Fixer`
delegating back to `Tester` is exactly this) -- this is a delegation-
permission graph, not a strict single-parent tree, so that alone is never
an error; `validate_org` below checks only what the brief asks for (unknown
role names, dangling `reports`) plus the basic shape and the single-root
rule the schema itself requires.

Built-in orgs ship in `_BUILTIN_ORGS` (bottom of this file) and are copied
into `~/.halo/orgs/<name>.json` the first time anything asks for the org
list or a specific org (`ensure_builtin_orgs`), and never overwritten once
a file exists at that path -- `/org load <name>` (TUI; mirrors `roles.py::
apply_role_template`'s "a loaded template overwrites" precedent) is the
explicit, opt-in way to reset one back to its shipped definition.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

_ORG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def is_valid_org_name(name: str) -> bool:
    """A safe, single path segment -- `orgs_dir() / f"{name}.json"` can
    never escape that directory or collide with a dotfile. Deliberately
    more permissive than a ROLE name (an org's name is a file stem, not a
    role keyword)."""
    return bool(name) and bool(_ORG_NAME_RE.match(name)) and ".." not in name


def find_position(org: dict, title: str) -> "Optional[dict]":
    for p in (org.get("positions") or []):
        if isinstance(p, dict) and p.get("title") == title:
            return p
    return None


def root_position(org: dict) -> "Optional[dict]":
    """The one position nobody's own `reports` names -- `None` when the
    org has zero or more than one candidate (both are validation errors;
    callers normally run `validate_org` first and only call this once it
    already passed)."""
    positions = org.get("positions") or []
    reported: set = set()
    for p in positions:
        if isinstance(p, dict):
            reported.update(t for t in (p.get("reports") or []) if isinstance(t, str))
    roots = [p for p in positions if isinstance(p, dict) and p.get("title") not in reported]
    return roots[0] if len(roots) == 1 else None


def validate_org(data) -> "list[str]":
    """Every problem with an org's shape -- `[]` means valid. Checked:
    basic shape, a non-empty unique `title` per position, an unknown
    `role` (against `roles.known_role_names()`, error lists the known
    names), a dangling `reports` entry (a title no position actually has,
    error lists the known titles), and not exactly one root. Never
    raises -- a caller (CLI/TUI/`load_org`) reports these as plain lines
    rather than a traceback."""
    if not isinstance(data, dict):
        return ["organization must be a JSON object"]
    problems: list = []
    if "name" in data and not isinstance(data.get("name"), str):
        problems.append('"name" must be a string')
    if "description" in data and not isinstance(data.get("description"), str):
        problems.append('"description" must be a string')
    positions = data.get("positions")
    if not isinstance(positions, list) or not positions:
        return problems + ['"positions" must be a non-empty list']
    from halo_harness.roles import known_role_names
    known_roles = known_role_names()
    titles: list = []
    for i, p in enumerate(positions):
        if not isinstance(p, dict) or not isinstance(p.get("title"), str) or not p.get("title").strip():
            problems.append(f'position #{i}: needs a non-empty "title"')
            continue
        titles.append(p["title"])
    dupes = sorted({t for t in titles if titles.count(t) > 1})
    if dupes:
        problems.append(f"duplicate position title(s): {', '.join(dupes)}")
    title_set = set(titles)
    for p in positions:
        if not isinstance(p, dict) or not isinstance(p.get("title"), str):
            continue
        role = p.get("role")
        if role and role not in known_roles:
            problems.append(f"position {p['title']!r}: unknown role {role!r} "
                             f"(expected one of {', '.join(known_roles)})")
        reports = p.get("reports") or []
        if not isinstance(reports, list):
            problems.append(f'position {p["title"]!r}: "reports" must be a list')
            continue
        dangling = sorted({t for t in reports if t not in title_set})
        if dangling:
            problems.append(f"position {p['title']!r}: reports to unknown title(s) {', '.join(dangling)} "
                             f"(known titles: {', '.join(sorted(title_set))})")
    if not dupes and not any("needs a non-empty" in m for m in problems):
        reported: set = set()
        for p in positions:
            if isinstance(p, dict):
                reported.update(t for t in (p.get("reports") or []) if isinstance(t, str))
        roots = [t for t in titles if t not in reported]
        if len(roots) != 1:
            if not roots:
                problems.append("organization has no root (every position is someone's report -- "
                                 "add one position nobody reports to)")
            else:
                problems.append(f"organization must have exactly one root, found {len(roots)} candidates: "
                                 f"{', '.join(sorted(roots))}")
    return problems


def org_tree_depth(org: dict) -> int:
    """Max number of delegation hops (edges) from the root to the deepest
    reachable position -- cycle-safe: a `reports` edge back toward a
    position already on the CURRENT path (release-flow's `Fixer` ->
    `Tester` is exactly this -- `Tester` is already an ancestor of
    `Fixer`) stops there rather than re-expanding forever. 0 for a
    root-only org (`solo`). `agent/subagent.py::run_org_call` sizes a
    running org's own depth cap from this ("depth comes from the tree")."""
    root = root_position(org)
    if root is None:
        return 0
    by_title = {p["title"]: p for p in (org.get("positions") or []) if isinstance(p, dict) and p.get("title")}

    def _dfs(title: str, visiting: frozenset) -> int:
        position = by_title.get(title)
        if position is None:
            return 0
        best = 0
        for child_title in (position.get("reports") or []):
            if child_title not in by_title:
                continue
            if child_title in visiting:
                # The hop itself still happens at runtime (a FRESH call,
                # e.g. release-flow's Fixer -> Tester again) -- only the
                # re-expansion of its own subtree is skipped, to stay
                # cycle-safe. Counting this as 0 instead of 1 would
                # undercount `run_org_call`'s own depth cap by exactly
                # one hop for every org that re-delegates to an ancestor
                # this way.
                best = max(best, 1)
                continue
            best = max(best, 1 + _dfs(child_title, visiting | {title}))
        return best

    return _dfs(root["title"], frozenset())


def _who(position: dict) -> str:
    bits = [position.get("role") or position.get("model") or "session model"]
    if position.get("effort"):
        bits.append(f"effort={position['effort']}")
    return ", ".join(bits)


def describe(org: dict) -> str:
    """A text tree from the root down (`/org show`). Cycle-safe like
    `org_tree_depth` (a `reports` edge back toward an ancestor on the
    SAME path is shown once, annotated, never re-expanded); any position
    never reached from the root (only possible on an org that hasn't
    passed `validate_org` yet, e.g. mid-edit in the form) is listed
    separately at the end instead of silently vanishing."""
    positions = org.get("positions") or []
    by_title = {p["title"]: p for p in positions if isinstance(p, dict) and p.get("title")}
    root = root_position(org)
    lines = [f"{org.get('name', '(unnamed)')} -- {org.get('description') or '(no description)'}"]
    visited: set = set()

    def _walk(title: str, prefix: str, visiting: frozenset) -> None:
        position = by_title.get(title)
        if position is None:
            return
        visited.add(title)
        cycle = title in visiting
        lines.append(f"{prefix}{title} ({_who(position)})" + (" (cycle -- stops here)" if cycle else ""))
        if cycle:
            return
        for child in (position.get("reports") or []):
            _walk(child, prefix + "  ", visiting | {title})

    if root is not None:
        _walk(root["title"], "", frozenset())
    else:
        lines.append("(no single root -- see /org edit)")
    orphans = sorted(t for t in by_title if t not in visited)
    if orphans:
        lines.append("Unreachable from the root:")
        for t in orphans:
            lines.append(f"  {t} ({_who(by_title[t])})")
    return "\n".join(lines)


def _render_context(org: dict, position: dict) -> str:
    """Appended to a position's own `instructions` to become its full
    system prompt (brief item 2: "its instructions plus a rendered view
    of the tree and of the positions it may delegate to")."""
    lines = [f"You are the '{position['title']}' position in the '{org.get('name', '?')}' organization.",
              "", "Full organization chart:", describe(org)]
    reports = position.get("reports") or []
    if reports:
        lines += ["", 'You may delegate to these positions (Agent tool, subagent_type="<title>"):']
        for title in reports:
            target = find_position(org, title)
            if target is None:
                continue
            first_line = (target.get("instructions") or "").strip().splitlines()
            summary = first_line[0] if first_line else "(no instructions)"
            lines.append(f"  - {title} ({_who(target)}): {summary}")
    else:
        lines += ["", "You have nobody to delegate to -- do this yourself and report your final answer."]
    return "\n".join(lines)


def orgs_dir(state_dir=None) -> Path:
    """`~/.halo/orgs/` -- created on first write; a read on a fresh box
    with no directory yet just sees an empty one once `ensure_builtin_
    orgs` runs."""
    from halo_harness.config.paths import bridge_home
    base = state_dir if state_dir is not None else bridge_home()
    return Path(base) / "orgs"


def ensure_builtin_orgs(state_dir=None) -> None:
    """Copies every `_BUILTIN_ORGS` entry into `orgs_dir()` the first time
    anything asks -- skipped per-name whenever a file already sits there
    (brief: "never overwritten once present"), whatever its content, even
    a user's own edited copy or an unrelated file they happened to save
    under a built-in's name first."""
    d = orgs_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    for name, data in _BUILTIN_ORGS.items():
        path = d / f"{name}.json"
        if not path.exists():
            path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def list_orgs(state_dir=None) -> "list[str]":
    ensure_builtin_orgs(state_dir)
    try:
        return sorted(p.stem for p in orgs_dir(state_dir).glob("*.json") if p.is_file())
    except OSError:
        return []


def load_org(name: str, state_dir=None) -> "Optional[dict]":
    """The parsed, VALIDATED org `name` -- `None` for a missing file, bad
    JSON, or a shape `validate_org` rejects (never raises)."""
    ensure_builtin_orgs(state_dir)
    path = orgs_dir(state_dir) / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if validate_org(data):
        return None
    if isinstance(data, dict):
        data.setdefault("name", name)
    return data


def save_org(name: str, data: dict, state_dir=None) -> "tuple[bool, list[str]]":
    """`(True, [])` on success; `(False, problems)` for a bad `name` or a
    `data` shape `validate_org` rejects -- never raises, never writes a
    partial file."""
    if not is_valid_org_name(name):
        return False, [f"invalid organization name {name!r}"]
    problems = validate_org(data)
    if problems:
        return False, problems
    d = orgs_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload.setdefault("name", name)
    (d / f"{name}.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return True, []


def reload_builtin_org(name: str, state_dir=None) -> "tuple[bool, list[str]]":
    """`/org load <name>` (TUI, brief item 3): re-installs a KNOWN
    built-in's shipped definition, OVERWRITING whatever is currently
    saved under that name -- the explicit, opt-in counterpart to
    `ensure_builtin_orgs`'s hands-off "never overwritten" default,
    mirroring `roles.py::apply_role_template`'s own "a loaded template
    overwrites" precedent. `(False, [reason])` for any name that isn't
    one of the three shipped built-ins."""
    if name not in _BUILTIN_ORGS:
        return False, [f"{name!r} is not a built-in organization (built-ins: {', '.join(sorted(_BUILTIN_ORGS))})"]
    d = orgs_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(_BUILTIN_ORGS[name], indent=2, sort_keys=True) + "\n",
                                     encoding="utf-8")
    return True, []


def position_agent_specs(org: dict) -> "dict[str, object]":
    """One `config.agents_md.AgentSpec` per position, keyed by title --
    `agent/subagent.py::run_org_call` hands this whole dict to a fresh
    `AgentRuntime.agents` so every position is spawnable by `subagent_
    type` exactly like a discovered `.claude/agents/*.md` file, with its
    own `reports` enforced through the SAME `agent_type_restriction`
    check an agent file's own `tools: ["Agent(name)"]` already drives
    (`AgentSpec.delegate_restriction`, read by `_build_child_session`)."""
    from halo_harness.config.agents_md import AgentSpec
    specs: dict = {}
    for p in (org.get("positions") or []):
        if not isinstance(p, dict) or not p.get("title"):
            continue
        title = p["title"]
        tools = p.get("tools")
        body = f"{(p.get('instructions') or '').strip()}\n\n{_render_context(org, p)}".strip()
        specs[title] = AgentSpec(
            name=title, description=f"Organization position: {title}",
            tools=(list(tools) if isinstance(tools, list) and tools else None),
            model=p.get("model") or None, role=p.get("role") or None, effort=p.get("effort") or None,
            body=body, delegate_restriction=set(p.get("reports") or []),
            # brief item 2: "the dock shows each running position as
            # `<title> (<role>)`" -- `_who` already formats this exact
            # "role-or-model[, effort=X]" tail for `describe()`.
            dock_label=f"{title} ({_who(p)})",
            source=f"org:{org.get('name', '?')}",
        )
    return specs


# ---------------------------------------------------------------------------
# Brief B: three built-in orgs, copied into ~/.halo/orgs/ on first use
# (`ensure_builtin_orgs`) and never overwritten once present.
# ---------------------------------------------------------------------------

_BUILTIN_ORGS = {
    "solo": {
        "name": "solo",
        "description": "A single orchestrator position with nobody to delegate to -- the plain one-agent baseline.",
        "positions": [
            {"title": "Orchestrator", "role": "orchestrator", "reports": [],
             "instructions": "Handle the goal yourself, start to finish, and report a clear final answer."},
        ],
    },
    "release-flow": {
        "name": "release-flow",
        "description": "Halo's own build loop: brief -> implement -> test -> review -> fix -> retest -> report.",
        "positions": [
            {"title": "Orchestrator", "role": "orchestrator", "reports": ["Implementer"], "instructions": (
                "Write a short, clear brief describing the task from the goal you were given, then "
                "delegate it to Implementer. Once the chain below reports back to you, write the final "
                "report in the hand-back format from plans/WORKER-RULES.md: RESULT LINES, FILES TOUCHED, "
                "WHAT YOU FOUND."
            )},
            {"title": "Implementer", "role": "coder", "reports": ["Tester"], "instructions": (
                "Implement the brief you were given. Every file write must be 250 lines or fewer per call; "
                "split larger changes across several writes. When the implementation is done, delegate to "
                "Tester to run the suites."
            )},
            {"title": "Tester", "role": "tester", "reports": ["Reviewer"], "instructions": (
                "Run the relevant test suites and any live checks the brief calls for; report pass/fail "
                "counts and the first failure's output in full, verbatim. The FIRST time you run, delegate "
                "your results to Reviewer next. If you were delegated to by Fixer (a re-test after a fix), "
                "just report your results back -- do not delegate again."
            )},
            {"title": "Reviewer", "role": "reviewer", "reports": ["Fixer"], "instructions": (
                "Review the implementation for correctness, reuse/simplification, and consistency with the "
                "surrounding code; list concrete findings with file paths and line numbers. Delegate your "
                "findings to Fixer to apply. If you have no findings, say so and report back without "
                "delegating."
            )},
            {"title": "Fixer", "role": "coder", "reports": ["Tester"], "instructions": (
                "Apply the reviewer's findings, in writes of 250 lines or fewer per call. When done, "
                "delegate back to Tester to re-run the suites and confirm the fix."
            )},
        ],
    },
    "company": {
        "name": "company",
        "description": ("CEO -> VP Engineering, VP Marketing, VP Research -> one manager each -> two workers "
                         "each; sensible roles, empty instruction slots to fill in."),
        "positions": [
            {"title": "CEO", "role": "orchestrator", "instructions": "",
             "reports": ["VP Engineering", "VP Marketing", "VP Research"]},
            {"title": "VP Engineering", "role": "planner", "instructions": "", "reports": ["Engineering Manager"]},
            {"title": "VP Marketing", "role": "planner", "instructions": "", "reports": ["Marketing Manager"]},
            {"title": "VP Research", "role": "planner", "instructions": "", "reports": ["Research Manager"]},
            {"title": "Engineering Manager", "role": "coder", "instructions": "",
             "reports": ["Engineer 1", "Engineer 2"]},
            {"title": "Marketing Manager", "role": "researcher", "instructions": "",
             "reports": ["Marketing Associate 1", "Marketing Associate 2"]},
            {"title": "Research Manager", "role": "researcher", "instructions": "",
             "reports": ["Research Analyst 1", "Research Analyst 2"]},
            {"title": "Engineer 1", "role": "coder", "instructions": "", "reports": []},
            {"title": "Engineer 2", "role": "coder", "instructions": "", "reports": []},
            {"title": "Marketing Associate 1", "role": "researcher", "instructions": "", "reports": []},
            {"title": "Marketing Associate 2", "role": "researcher", "instructions": "", "reports": []},
            {"title": "Research Analyst 1", "role": "researcher", "instructions": "", "reports": []},
            {"title": "Research Analyst 2", "role": "researcher", "instructions": "", "reports": []},
        ],
    },
}
