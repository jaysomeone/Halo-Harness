# Organizations

Halo 2.0.2 round 2 (brief B): a named, reusable TREE of sub-agent
positions, saved as `~/.halo/orgs/<name>.json`, that runs as a single
delegated unit through the exact same Agent-tool machinery an ordinary
`Agent(subagent_type=...)` call already uses -- see
[ROLES.md](ROLES.md) for how a position's own `role`/`model`/`effort`
resolves, and `plans/WORKER-RULES.md` (the repo this project builds
itself with) for the hand-back format the `release-flow` built-in's own
positions quote. Verified against `halo_harness/orgs.py`,
`agent/subagent.py`, `tools/agent.py`, `commands/builtins.py`,
`tui/slash.py`, `tui/dialogs/org_editor.py`, and `org_cli.py`.

## Schema

```json
{
  "name": "release-flow",
  "description": "...",
  "max_concurrent": 4,
  "positions": [
    {
      "title": "Implementer",
      "role": "coder",
      "model": "or:vendor/x",
      "effort": "high",
      "instructions": "Implement the brief you were given...",
      "tools": ["Read", "Edit", "Write", "Bash"],
      "reports": ["Tester"]
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `name` | the org's own name (also the file stem) |
| `description` | one line, shown by `/org list`/`halo org list` |
| `max_concurrent` | optional; overrides `agents.max_concurrent` (config, default 4) for every position in THIS org only |
| `positions` | the tree, one entry per position -- see below |
| `positions[].title` | the position's identity: its `subagent_type`, unique within the org, renameable in the editor (every other position's own `reports` entry is updated with it) |
| `positions[].role` | a role name (`roles.known_role_names()`) resolved through the SAME table a role-bearing agent uses -- mutually exclusive with `model` in practice (the editor clears whichever you're not setting) |
| `positions[].model` | a literal model reference, pinned regardless of the role table |
| `positions[].effort` | optional reasoning effort, applied the same way a role's own `effort` is |
| `positions[].instructions` | this position's system prompt (its own paragraph -- a rendered view of the whole org chart and of the positions it may itself delegate to is appended automatically, see "Running one" below) |
| `positions[].tools` | optional tool-name allowlist for this position only; omitted means every tool the PARENT session's own catalog already has (Claude Code's usual default) |
| `positions[].reports` | the titles this position may delegate to via the Agent tool -- see "The root, and `reports`" below |

Neither `role` nor `model` is required: a position with neither resolves
to the session's own model, exactly like an agent with no role at all.

## The root, and `reports`

Exactly one position is the **root**: the one title that never appears
in any OTHER position's own `reports` list (nobody is its boss) --
`/org new`/`halo org new` starts you with a single root position named
"Orchestrator" and an empty `reports` list.

`reports` is a delegation-PERMISSION graph, not a strict single-parent
tree: the same title may appear in more than one position's own
`reports` (release-flow's `Fixer` delegates back to `Tester`, the exact
same position `Implementer` delegated to earlier) -- this is never an
error. `orgs.validate_org` checks: every position has a non-empty, unique
`title`; every `role` is a currently-known role name (error lists the
known ones); every `reports` entry names an existing title (error lists
the known titles); and there is exactly one root.

## Built-ins

Shipped in the package and copied into `~/.halo/orgs/<name>.json` the
first time anything asks for the org list or a specific org, and **never
overwritten once a file exists at that path** -- even by your own prior
edit, or by a later halo version with a changed built-in definition.
`/org load <name>` (TUI only) is the explicit, opt-in way to reset one
back to its shipped form, overwriting your local copy.

- **`solo`** -- a single `Orchestrator` position (role `orchestrator`,
  no `reports`): the plain one-agent baseline, equivalent to not using an
  organization at all.
- **`release-flow`** -- the loop this project is itself built with:
  `Orchestrator` (writes a brief) -> `Implementer` (`coder`, writes in
  >=250-line chunks) -> `Tester` (`tester`, runs the suites) -> `Reviewer`
  (`reviewer`, lists findings) -> `Fixer` (`coder`, applies them) ->
  back to `Tester` (confirms the fix) -> unwinds back up to
  `Orchestrator`, whose own instructions quote the hand-back format from
  `plans/WORKER-RULES.md`.
- **`company`** -- `CEO` -> `VP Engineering`/`VP Marketing`/`VP Research`
  -> one manager each -> two workers each (13 positions, sensible
  built-in roles, empty `instructions` slots for you to fill in).

## Running one

`/org run <name> "<goal>"`, `Agent(org=<name>, prompt=<goal>)` (the Agent/
Task tool itself, alongside its existing `subagent_type`), and `halo org
run <name> "<goal>"` (CLI, builds its own bare print-mode session to act
as the caller) all reach the same `agent/subagent.py::run_org_call`:

1. The org is loaded and validated; its root is found.
2. Every position becomes a real `config.agents_md.AgentSpec`, keyed by
   `title` -- a fresh `AgentRuntime` swaps this WHOLE set in for the
   usual discovered-agents dict, for the duration of this one call only.
3. The root position is spawned exactly like an ordinary
   `Agent(subagent_type=<root title>, prompt=<goal>)` call -- its own
   system prompt is `instructions` plus a rendered view of the full org
   chart and of the positions named in its own `reports` (so it knows
   who it may call and roughly what they do).
4. When it delegates, that call goes through the SAME Agent tool: each
   child's own `subagent_type` must be one of ITS CALLER's `reports`
   (`AgentSpec.delegate_restriction`, enforced the same way a
   `tools: ["Agent(name)"]`-restricted `.claude/agents/*.md` file already
   restricts its own children) -- a call outside that list is refused
   with a clear tool error, never silently ignored.
5. Results flow back up exactly like any other sub-agent's: wrapped in
   `<task_result>`, rolled into the parent's own cost/usage, visible to
   `/stats`.

**Depth** comes from the org's own tree shape, not the usual depth-1 cap:
`org_tree_depth(org) + 1` (the longest root-to-leaf chain the `reports`
graph actually has, re-delegation hops like `Fixer` -> `Tester` counted
once each). **Concurrency** (how many positions one position may spawn
at once, in a single turn) is `agents.max_concurrent` (config, default
4) unless the org's own `max_concurrent` is set -- the same knob now
governs every session's own parallel `Agent` calls, org or not.

**The dock** shows each running position as `<title> (<role>)` (or
`(<model>)`, or `(<role>, effort=<level>)`) instead of the bare title --
`AgentSpec.dock_label`, read by the SAME `SubAgentCard`/transcript note
every other sub-agent already uses.

## Editing

`/org edit <name>` (TUI, `tui/dialogs/org_editor.py`): a tree view on the
left (one row per position, indented by depth, root first) and the
selected position's own fields on the right -- title, role-or-model
(free text, or `ctrl+p` for the same `ModelPicker` `/model` uses),
effort, instructions, and `reports` (a comma-separated list of titles).
`ctrl+n` adds a new, unlinked position; `ctrl+d` deletes the selected one
(pruning it from every other position's own `reports`); `ctrl+s`
validates and saves (refusing, with the reasons shown, when
`validate_org` rejects the result -- e.g. a just-added position is still
unlinked from the root); `Escape` cancels with nothing written. `halo org
edit <name>` (CLI) opens `$EDITOR`/`$VISUAL` on the raw JSON file instead
(creating a one-position starter first if it doesn't exist), re-validating
on save.

## Limits

- Exactly one root; `reports` may reuse a title (a delegation graph, not
  a strict tree) but every OTHER title it names must exist.
- A position's `role` must be a currently-known role name
  (`roles.known_role_names()`) -- a `model` is never validated against
  any catalog (the same as an agent file's own `model:`), so a typo there
  surfaces as an ordinary provider error only once that position actually
  runs.
- Running an org replaces the usual discovered-agents set for that one
  call -- a position may only delegate to another position in the SAME
  org, never to a `.claude/agents/*.md` file or a built-in like
  `general-purpose` by name.
- `halo org run`'s own `--model` flag is only a fallback for a position
  with neither `role` nor `model` set; every built-in's own positions
  always set one, so it normally never matters there.
