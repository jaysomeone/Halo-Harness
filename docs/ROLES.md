# Roles

Halo 2.0.2 ("roles v2", W7 round 1), carrying forward V2c (H15): a small
vocabulary that lets a team point different kinds of work at different
models -- cheap for exploration, strong for planning/review -- without
editing every agent file by hand, and (new this round) pin a specific
REASONING EFFORT per role too. Verified against `halo_harness/roles.py`,
`config/agents_md.py`, `agent/subagent.py`, `agent/loop.py`,
`tools/agent.py`, `cli.py`, `roles_cli.py`, `completion_cli.py`,
`commands/builtins.py`, `tui/slash.py`, `tui/completion.py`,
`tui/dialogs/roles_editor.py`, and `telemetry.py`. See
[MODELS.md](MODELS.md) for how a model reference itself resolves,
[DATABRICKS.md](DATABRICKS.md)'s work-matrix section for endpoint health (a
separate concern), and that same doc's **End-to-end team workflow** section
for how a shared `team.json`'s own `roles` table gets onto everyone's box
via `halo init --provider databricks`. Round 7 (the init wizard) is
verified against `halo_harness/tui/dialogs/init_wizard.py` and
`setup_cli.py` too.

## Setting up with the wizard (`roles.enabled`, presets)

The init wizard's own Roles step (`halo init`, step 5; `halo setup
roles`/`/setup roles` later; "step 5, then orgs" for a bare `halo setup`)
starts with a switch, **`roles.enabled`** (default **on**): off means
every role resolves to the session model -- `resolve_role_table()`
returns `{}` outright, the same shape a session with no role table
configured at all already falls through to -- and `/roles`/`/role` are
hidden from `/help`/tab-completion/the rotating tips (never from
`resolve()` itself: typing either by hand still works, this is a
discoverability default, never a functional gate).

With it on, the step shows three shipped **presets** -- written as
ordinary templates (below) into `~/.halo/roles/` the first time this
screen runs, computed from whatever is actually configured right then,
and never overwritten after that (same "copy on first use" rule as
[ORGS.md](ORGS.md)'s own built-ins):

| Preset | What it sets |
|---|---|
| `balanced` | the cost-aware defaults: the session model for most roles; `researcher`/`small` drop to the cheapest configured model |
| `quality` | the session model everywhere; `judge`/`reviewer` pinned to the strongest configured model (the session's own default at setup time) |
| `local-first` | `small`/`researcher`/`judge` on a local `ol:` model when one is configured (forward-compatible with the 2.0.3 Ollama release -- nothing resolves one yet), else the cheapest configured model |

A preview of the highlighted template's own table is shown; `Use this
template` (same action as the step's own `Next`) applies it -- through
the SAME `apply_role_template` a `/roles load`/`halo roles template
load` would use, also pushed straight into a LIVE session's own role
table when this runs via `/setup roles` inside one, no restart needed;
`Edit roles...` opens the round-1 form (below) inside the wizard and
returns here; `Skip for now` leaves config untouched and the summary
step prints a one-line `/roles` hint. The round-1 form itself
(`/roles edit <name>`, `tui/dialogs/roles_editor.py`) gained the SAME
"start from a template" picker at its top (choosing one REPLACES the
form's own roles, never merges) plus a `Save as template...` action
(a COPY under a new name, distinct from `ctrl+s`'s save-to-this-name).

## The ten roles

| Role | Default meaning when unset |
|---|---|
| `orchestrator` | the session's own model (no entry needed to mean this) |
| `planner` | the session's own model, unless configured -- the `Plan` agent's role |
| `coder` | the session's own model, unless configured |
| `reviewer` | the session's own model, unless configured |
| `judge` | the session's own model, unless configured -- adjudicates candidate outputs/verifies a result against acceptance criteria |
| `researcher` | the session's own model, unless configured (see "cost-aware defaults" below) |
| `tester` | the session's own model, unless configured -- runs named suites/live checks and reports |
| `compaction` | the rung right after `compactionModel` (below `compactionModel`, above the session's main model) -- see "Resolution precedence" |
| `small` | the session's own small model, unless configured; used for summaries, titles, `/improve` drafts, and compaction when NEITHER `compactionModel` NOR the `compaction` role is set |
| `subagent_default` | the last rung before the session model, for a sub-agent with NO role at all (not a built-in, no `role:` frontmatter) |

`planner`/`judge`/`tester`/`compaction`/`subagent_default` are new in Halo
2.0.2; everything else carries forward unchanged from V2c. `Plan` moved from
`reviewer` to its own `planner` role this round (it no longer shares a rung
with `Reviewer`).

### Custom role names

Any OTHER name matching `[a-z][a-z0-9_]*` that a `team.json` or a loaded
role TEMPLATE (see below) actually defines a value for is an equally valid
role everywhere a built-in name is: `Agent(role=...)`, `--role`,
frontmatter `role:`, `/role`. `roles.known_role_names()` is the live
"what's valid right now" set (the ten built-ins plus whatever is currently
defined) every one of those validates a name against -- an unknown name
errors with that list.

## A role's VALUE: model, or model + effort

Every role table (config.json, team.json, a template, a `--role`/`/role`
override) holds either a bare model-reference string, or
`{"model": "...", "effort": "..."}`. The effort half is optional; when
present, a sub-agent that resolves its MODEL from that same role also
applies that EFFORT -- through the identical `agent/loop.py` `set_model`/
effort path a normal session uses, resolved via `providers/effort.py` so an
effort level the route doesn't support maps the same way `/effort` would
(shown as `requested (sent as X)`, never silently dropped). Precedence for
the effort half mirrors the model half: a CLI `--role`/`/role` override's
own effort beats the role table's, which beats inheriting the parent's.

## Where the table lives

`~/.halo/config.json`'s own `roles` key (`roles.<name>`, dotted-path, the
same convention `databricks.gateway.<endpoint>` uses) -- read/written with
`halo config get/set roles.<name> ...`, `/role`/`/roles set` (session-only,
see below), or hand-edited. A shared `team.json`'s own `roles` map (see
[DATABRICKS.md](DATABRICKS.md)'s team preset section) is seeded into
config.json exactly once, at `halo init --preset work` time, by the SAME
idempotent idiom `gateway_preference` already uses (`roles.py::
apply_role_preference`): a role the user already configured locally is
never overwritten by the team default. A loaded TEMPLATE instead
OVERWRITES (`roles.py::apply_role_template`) -- see "Role templates" below
for why that's the right behavior for an explicit, named load.

## How an agent gets a role

A built-in agent has a fixed default role:

| Agent | Role | Why |
|---|---|---|
| `general-purpose` | `orchestrator` | the default, heaviest-duty agent -- stays on the session model unless the team pins `roles.orchestrator` |
| `Explore` | `researcher` | fast, read-only, cheap |
| `Researcher` | `researcher` | Explore's tool set + WebSearch, for open-ended research |
| `Plan` | `planner` | strong-model planning (its own role as of 2.0.2) |
| `Reviewer` | `reviewer` | strong-model code review |
| `Coder` | `coder` | implementation work |
| `Judge` | `judge` | adjudication / acceptance-criteria verification |
| `Tester` | `tester` | runs suites/live checks, reports verbatim |

A custom `.claude/agents/*.md` file sets the same thing with a `role:`
frontmatter key (a built-in name above, or any OTHER currently-known custom
name); it needs no `role:` at all if it doesn't want to participate in the
table. `compaction` and `subagent_default` are never a sub-agent's own
`role:` -- they're consulted directly by `agent/loop.py` (see the table
above and "Resolution precedence" below), not through an `AgentSpec`.

## Resolution precedence

Full chain, per sub-agent call (`config/agents_md.py::resolve_agent_model`):

1. An explicit `model=` argument on the `Agent`/`Task` tool call, or `--agent
   NAME --model ...` -- always wins outright, exactly as before roles existed.
2. A `--role NAME=MODEL[:EFFORT]`/`/role`/`Agent(role=...)` override, for
   THIS agent's own role (an `Agent(role=...)` call-time override, else the
   agent's file/built-in default role) -- wins even over the agent's own
   file `model:`. This is deliberate: a freshly-typed, run-only flag is more
   explicit than a shared/managed agent file, so the user gets to trump it
   for one run.
3. The agent's own file `model:` frontmatter, when set.
4. The role table (config.json/team.json/a loaded template, or the
   cost-aware default below) for this agent's own role.
5. `CLAUDE_CODE_SUBAGENT_MODEL` (env) / `settings.subagentModel`.
6. `subagent_default` (new in 2.0.2) -- ONLY for a sub-agent with NO role
   name at all (steps 2 and 4 above never applied). A role-bearing agent
   with nothing configured for its OWN role does NOT fall through to this;
   that still means "the session model", unchanged.
7. The parent/session's own model (unchanged) -- `orchestrator`'s own
   documented default is exactly this outcome, reached by having no entry at
   all rather than a special case.

An agent with NO role at all is affected only by step 6 above (new this
round) and otherwise unchanged from the pre-V2c chain.

**Compaction** is a parallel, separate chain (`agent/loop.py::
_compaction_model_override`, `agent/compact.py::resolve_knobs`): a plain
`compactionModel` key in config.json wins first, then Claude Code's own
settings chain's `compactionModel`, THEN (new in 2.0.2) the `compaction`
role, then the session's own main model.

## `--role NAME=MODEL[:EFFORT]` (CLI)

Repeatable; a later repeat of the same role name wins. Validated once, right
after argument parsing, before either `-p` or the TUI starts building a
session -- a bad `NAME=MODEL` (no `=`, an unrecognized role name, an empty
model) is a clean exit-2 usage error, never a traceback. The `:EFFORT`
suffix is optional and must be one of the harness's own accepted effort
words (`low`/`medium`/`high`/`xhigh`/`max`, same set `--effort` itself
takes) -- anything else is read as part of the model id, so a model ref
that legitimately contains colons (`or:vendor/model`, `dbx:endpoint`,
`cc:fable[1m]`) is never mis-split.
```sh
halo -p --role researcher=or:deepseek/deepseek-v4.1-flash:low \
  "use the Researcher agent to summarize this repo"
```

## `Agent(role=...)` (the tool)

The `Agent`/`Task` tool itself accepts a `role` argument, alongside the
existing `model`: `Agent(subagent_type="general-purpose", role="researcher",
...)` makes THIS ONE call resolve against the `researcher` role's model
(and effort, if that role has one), overriding `general-purpose`'s own
default role (`orchestrator`) for just that call. It works identically for
a custom `.claude/agents/*.md` agent that sets its own `role:` frontmatter
-- the call-time `role=` still overrides it for that one call.

## `/role` and `/roles set` (session-only, TUI and print mode)

```
/role <name> <model> [effort]
/roles set <name> <model> [effort]
```
The same operation under two names -- sets ONE role for THIS session only
(mutates the live session's own role table directly; never persisted --
`/roles save <name>` below is the explicit "keep this" action). `<name>`
must already be a known role (see "Custom role names" above); an unknown
name errors with the list of known ones. In the TUI, both tab-complete:
the role-name argument first (`roles.known_role_names()`), then a model ref
(the same enumerated catalog `/model`'s own picker uses --
`controller.list_models()`), then the effort levels valid for whichever
model was just typed -- ranked prefix-matches-first-then-substring
(`tui/completion.py::filter_items`, reused for all three).

## Role templates: `~/.halo/roles/<name>.json`

```json
{"name": "release-flow", "description": "...", "roles": {"coder": "or:vendor/x", "judge": {"model": "or:vendor/j", "effort": "high"}}}
```
A named, reusable role table, independent of any one project's
config.json. TUI: `/roles templates` (list), `/roles save <name>` (the
CURRENT table), `/roles load <name>` (apply), `/roles new <name>` (empty),
`/roles show <name>`, `/roles edit <name>` (a form: one row per role, Enter
opens the same model picker `/model` uses, then an inline effort prompt;
`ctrl+s` saves, Esc cancels with nothing written -- `tui/dialogs/
roles_editor.py`). CLI: `halo roles template
list|save|load|new|edit|show|export|import` (`edit` opens `$EDITOR`/
`$VISUAL` on the raw file, no form; `export <name> [file]`/`import
<file>` move a template as plain JSON -- `export` writes to `file` or
stdout when omitted, `import` validates the same way any other template
write is, listing every problem, and falls back to the file's own
basename when the JSON has no `"name"` field; `halo org export`/`import`
is the same pair for an organization, see [ORGS.md](ORGS.md)). Setting
`roles.editor: "external"` in config.json makes the TUI's own `/roles edit`
use the SAME `$EDITOR` flow instead of opening the form.

**Precedence**: CLI `--role`/`/role` (session-only, always wins) > a loaded
template (explicit, overwrites config.json's `roles` key outright) >
team.json (idempotent seed, only fills a gap) > whatever was already in
config.json by hand > the cost-aware defaults below.

## Shell completion

```sh
halo completion bash      # eval "$(halo completion bash)" in ~/.bashrc
halo completion zsh       # eval "$(halo completion zsh)" in ~/.zshrc, or save as `_halo` on $fpath
halo completion powershell  # dot-source from $PROFILE
```
Completes `halo`'s own subcommands, every known role name, and every model
ref already cached under `~/.halo` (`models.json`/`dbx-endpoints.json`,
plain file reads -- no network call of its own; an empty/missing cache just
means fewer model-ref completions offered).

## Cost-aware defaults (documented, never automatic beyond this)

When the role table (config.json's `roles` key, itself already including
anything a team.json/template seeded) is **completely empty** -- no team, no
local override, nothing -- and this session's own model is a Databricks one
(the practical signature of the `work` preset; presets themselves are an
init-time-only choice and are never persisted, so this is the same proxy
`model.py`/`providers.config` already use elsewhere for "acting like
work"), exactly two roles get a built-in default:

| Role | Default |
|---|---|
| `researcher` | `dbx:databricks-deepseek-v4-1-flash` (cheap, for exploration) |
| `small` | `dbx:databricks-deepseek-v4-1-flash` (cheap, for summaries/titles) |

Every other role is deliberately left out of this default -- "the session
model for the rest" is already what an absent entry means, so nothing needs
to be written for them. This is the ONLY place a model is ever picked for a
role without the user, a team.json, or a template asking for it by name --
any role table entry at all, even a single one, turns this off entirely
(`roles.py::resolve_role_table`), and no other code path in the harness ever
invents a role's model on its own.

## `/roles`

Shows the resolved table: model, effort (the plain sent value, or
`requested (sent as X)` when a role's own effort maps to a different one on
its route), endpoint/path type, and price per role, straight off the live
session's own `agent_runtime.role_table`/`.cli_role_overrides` -- the SAME
table a role-bearing `Agent` call actually resolves against, so this never
drifts from real behavior. Includes every built-in role PLUS any custom
name actually defined in the table.
```
Role table (model/effort/endpoint/path type/price per role):
  orchestrator  or:deepseek/deepseek-v4.1-flash  -     openrouter  $0.14/1M in, $0.28/1M out  (session model)
  coder         or:deepseek/deepseek-v4.1-flash  high  openrouter  $0.14/1M in, $0.28/1M out  (session model)
  researcher    dbx:databricks-deepseek-v4-1-flash  -  databricks  mlflow  ? DBU  (role table)
  small         dbx:databricks-deepseek-v4-1-flash  -  databricks  mlflow  ? DBU  (role table)
```

## `stats --roles`

Sub-agent spend (sessions/calls/tokens/cost) per role, summed from every
`Agent`/`Task` call's own rolled-up usage node -- see
[COMMANDS.md](COMMANDS.md)'s `stats` section for the full flag table and
`--json` shape. A rolled-up node with no `role` (every pre-V2c log, and any
role-less agent's own calls) simply doesn't contribute to this table; the
existing `--models`/`--tools` tables are completely unaffected. The row set
is whatever roles actually appear in the scanned logs -- built-in or
custom alike, never limited to a fixed list. Halo 2.0.2: each row also
carries that role's CURRENTLY CONFIGURED model/effort
(`roles.configured_role_table()`, no live session needed) -- `null`/`-`
when nothing is configured for it today, even if it had spend in the
window (it was reconfigured since, or it always meant the session model).
Price is not repeated here (that needs a parent model to fall back to,
which this standalone command has no live session to borrow -- see
`/roles` above for that).
