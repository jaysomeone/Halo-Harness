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
`tui/slash.py`, `tui/dialogs/org_editor.py`, `org_cli.py`, `setup_cli.py`,
and `tui/dialogs/init_wizard.py` (round 7).

## Setting up with the wizard (`orgs.enabled`, the default org)

The init wizard's own Organizations step (`halo init`, step 6; `halo
setup orgs`/`/setup orgs` later; "roles, then orgs, then summary" for a
bare `halo setup`) starts with a switch, **`orgs.enabled`** (default
**off**, unlike roles): off hides `/org` from `/help`/tab-completion/the
rotating tips, the `/tasks` board tab, and the `Agent` tool's own `org=`
parameter (never from `resolve()`/`run_org_call` itself -- `/org run` by
name, and an already-running org, both still work; this is a
discoverability default, never a functional gate).

With it on, the step lists every built-in and saved org with a tree
preview of the highlighted one; `Make this the default org` (same action
as the step's own `Next`) writes **`orgs.default`**, what `/org run`/
`halo org run` resolve to when no name is given (`halo org run
"<goal>"`, one argument -- refuses cleanly with no default set instead
of guessing); `Edit org...` opens the round-2 form (`tui/dialogs/
org_editor.py`) inside the wizard and returns here -- that form itself
gained the SAME "start from: solo / release-flow / company / <saved>"
template picker at its top this round (choosing one REPLACES the
positions below, never merges; `ctrl+s` still saves under the SAME
name); `Skip for now` leaves config untouched.

### Budgets and goals (ideas borrowed from Paperclip)

Two more `~/.halo/orgs/<name>.json` fields, enforced through the
existing cost meter:

- **`budget_usd`** (a number, on the org and/or any position): a hard
  stop -- `Agent(org=...)`'s own delegation check (the same place the
  depth cap is enforced) refuses to spawn a FURTHER position once the
  org's total spend since the run started, or that one position's own
  cumulative spend, reaches its budget; a result crossing 80% gets a
  warning line appended instead. A position whose own budget is hit is
  refused only for a LATER spawn attempt -- the call already running
  always finishes.
- **Goals**: `/org run <name> "<goal>"` (`Agent(org=...)`, `halo org run`)
  records the goal as a root task on the session's shared task board
  (`kind: "goal"`) before the root position ever runs; every position is
  told its own id and to pass `parent=<that id>` on its own `TaskCreate`
  calls, so `/tasks`' board tab shows goal -> tasks -> results instead of
  a flat list.

### Approval gates

A third `~/.halo/orgs/<name>.json` position field: **`requires_
approval`** (bool, per position) holds that position's just-finished
result as a PENDING card -- `agent/subagent.py::_apply_approval_gate`,
called right after a position's own spawn (foreground, background, or a
resumed `task_id`) finishes, before its text ever reaches the parent
that delegated to it:

- **accept**: the result flows up unchanged.
- **edit, re-run**: the reviewer's own instruction plus the position's
  previous result become a fresh prompt for the SAME position (a real
  new spawn, through the ordinary Agent-tool path -- its own cost/hooks/
  task-board bookkeeping all apply normally); the NEW result is gated
  the same way, up to 5 revisions, after which the gate stops the
  position itself rather than asking again.
- **stop**: the result becomes a clear refusal instead of whatever the
  position produced (the original text is kept below it, for reference).

The card reuses the EXACT dock every permission/question/plan-review ask
already uses (`ApprovalCard`, `tui/widgets/cards.py`; `approval_request`/
`Session.resolve_approval`/`Controller.answer_approval` mirror `plan_
review`/`resolve_plan`/`answer_plan` throughout) -- never a new kind of
prompt. With no live dock reachable at all (`halo org run`/`halo org
resume` from a plain terminal, or any bare/print-mode session), the gate
prints the result and waits on one stdin line instead, the SAME
convention `halo init`'s own non-interactive prompts already use
(`a`/`e`/`s`, `e` then reads one more line for the instruction; a blank
or unrecognized line -- including EOF -- is treated as `s`, never a
silent accept). `--yes` on `halo org run`/`halo org resume`, or the
session's own `dontAsk` permission mode, accepts every gate
automatically instead of asking -- the OPPOSITE of `dontAsk`'s usual
"ask converts to deny" rule (an approval gate is never a tool-permission
ask).

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
| `positions[].requires_approval` | optional bool (default false) -- see "Approval gates" above |

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

## Templates and installing

`halo org install <name> [--force]` / `/org install <name> [--force]`
copies a TEMPLATE into `~/.halo/orgs/<name>.json`, refusing to overwrite
an existing file there unless `--force` -- distinct from the built-ins'
own automatic, hands-off copy above (which already happens for `solo`/
`release-flow`/`company` the moment anything asks for the org list), this
is an explicit, opt-in action, useful for recreating one after deleting
your own copy, or for installing a SAVED template (below) under its own
name for the first time.

A template's source is either:

- **shipped** -- the three built-ins above, each already carrying its
  own one-line README (its `description` field, the SAME one `/org
  list`/`halo org list` show for an installed org, and the wizard's
  Organizations step's own tree preview already renders); or
- **saved** -- a plain org JSON file placed in `~/.halo/org-
  templates/<name>.json` (`orgs.org_templates_dir()`), a pool separate
  from the live, runnable `~/.halo/orgs/` directory -- never auto-
  populated, the mirror of `roles.py`'s own template-pool-vs-live-table
  split for role templates.

A shipped name always wins a collision with a same-named saved one.

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

## Export and import

`halo org export <name> [file]` / `/org export <name> [file]` writes the
org's own validated JSON (`orgs.load_org`'s shape, always carrying a
`"name"`) to `file`, or to stdout when `file` is omitted so it can be
piped or redirected. `halo org import <file>` / `/org import <file>`
reads it back -- validated the SAME way any other org write is
(`orgs.save_org` -> `validate_org`: role names, reports, `budget_usd`),
with every problem listed, not just the first; a file with no usable
`"name"` field falls back to its own basename. Plain JSON, no secrets to
scrub (an org carries no credentials of its own).

`halo roles template export <name> [file]` / `import <file>` is the same
pair for a saved ROLE template (`~/.halo/roles/<name>.json`) -- see
[ROLES.md](ROLES.md).

## Resuming an interrupted run

Every `run_org_call` (`/org run`, `Agent(org=...)`, `halo org run`) writes
a small record, `<session_dir>/org-run.json` (org name, goal, the goal
task's own id, and the caps/budget this run actually started with), right
as the run begins -- best-effort, alongside the goal task itself.

`/org resume` (TUI/print-mode, no argument -- continues THIS session's
own interrupted run) and `halo org resume <session-id> [--cwd DIR]
[--model REF] [--yes]` (CLI, a PAST session possibly from a different
process entirely -- `<session-id>` is whatever `-r`/`--resume` already
accepts: an exact id, a `.jsonl` path, or a title/first-message substring
match) both reach `agent/subagent.py::resume_org_run`:

1. Reads that session's own `org-run.json` record and its shared task
   board (`tools/task_board.py`).
2. Splits the board into DONE tasks (kept -- named in the new prompt as
   already finished, never redone) and OPEN/CLAIMED tasks (the new root
   position's own work list).
3. Starts a FRESH `run_org_call` for the SAME org, with a goal built from
   the original goal plus that done/pending split, and with the org's own
   `max_concurrent`/`budget_usd` (and any per-position `budget_usd`)
   OVERRIDDEN from the saved record -- never from whatever `~/.halo/
   orgs/<name>.json` currently says, so editing the org after the
   original run started can never change what the RESUMED run is bound
   by. (`max_depth` is the one exception: always recomputed fresh, for
   the resuming caller's own actual depth, which has nothing to do with
   the original run's caller.)

A session with no run record at all (one that predates this feature, or
never ran an organization) reports exactly that instead of guessing.

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
