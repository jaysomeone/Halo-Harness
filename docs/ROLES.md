# Roles

V2c (H15): a small, fixed vocabulary -- `orchestrator`, `coder`, `reviewer`,
`researcher`, `small` -- that lets a team point different kinds of work at
different models (cheap for exploration, strong for planning/review) without
editing every agent file by hand. Verified against `rolo_claude/roles.py`,
`config/agents_md.py`, `agent/subagent.py`, `tools/agent.py`, `cli.py`,
`commands/builtins.py`, and `telemetry.py`. See [MODELS.md](MODELS.md) for
how a model reference itself resolves, [DATABRICKS.md](DATABRICKS.md)'s
work-matrix section for endpoint health (a separate concern), and that same
doc's **End-to-end team workflow** section for how a shared `team.json`'s
own `roles` table actually gets onto everyone's box via `rolo-claude init
--preset work`.

## The five roles

| Role | Default meaning when unset |
|---|---|
| `orchestrator` | the session's own model (no entry needed to mean this) |
| `coder` | the session's own model, unless configured |
| `reviewer` | the session's own model, unless configured |
| `researcher` | the session's own model, unless configured (see "cost-aware defaults" below) |
| `small` | the session's own small model, unless configured; used for summaries, titles, `/improve` drafts, and compaction when `compactionModel` is unset |

## Where the table lives

`~/.rolo-claude/config.json`'s own `roles` key (`roles.<name>`, dotted-path,
the same convention `databricks.gateway.<endpoint>` uses) -- read/written with
`rolo-claude config get/set roles.<name> ...` or hand-edited. A shared
`team.json`'s own `roles` map (see [DATABRICKS.md](DATABRICKS.md)'s team
preset section) is seeded into config.json exactly once, at `rolo-claude init
--preset work` time, by the SAME idempotent idiom `gateway_preference` already
uses (`roles.py::apply_role_preference`): a role the user already configured
locally is never overwritten by the team default. Neither file ever holds
anything but the five names above and a model-reference string.

## How an agent gets a role

A built-in agent has a fixed default role:

| Agent | Role | Why |
|---|---|---|
| `general-purpose` | `orchestrator` | the default, heaviest-duty agent -- stays on the session model unless the team pins `roles.orchestrator` |
| `Explore` | `researcher` | fast, read-only, cheap |
| `Researcher` | `researcher` | Explore's tool set + WebSearch, for open-ended research |
| `Plan` | `reviewer` | strong-model planning |
| `Reviewer` | `reviewer` | strong-model code review |
| `Coder` | `coder` | implementation work |

A custom `.claude/agents/*.md` file sets the same thing with a `role:`
frontmatter key (one of the five names above); it needs no `role:` at all if
it doesn't want to participate in the table.

## Resolution precedence

Full chain, per sub-agent call (`config/agents_md.py::resolve_agent_model`):

1. An explicit `model=` argument on the `Agent`/`Task` tool call, or `--agent
   NAME --model ...` -- always wins outright, exactly as before roles existed.
2. A `--role NAME=MODEL` CLI override (see below), for THIS agent's own role
   (an `Agent(role=...)` call-time override, else the agent's file/built-in
   default role) -- wins even over the agent's own file `model:`. This is
   deliberate: a freshly-typed, run-only flag is more explicit than a shared/
   managed agent file, so the user gets to trump it for one run.
3. The agent's own file `model:` frontmatter, when set.
4. The role table (config.json/team.json, or the cost-aware default below)
   for this agent's own role.
5. `CLAUDE_CODE_SUBAGENT_MODEL` (env) / `settings.subagentModel`.
6. The parent/session's own model (unchanged) -- `orchestrator`'s own
   documented default is exactly this outcome, reached by having no entry at
   all rather than a special case.

An agent with NO role at all (no frontmatter `role:`, not one of the six
built-ins) is completely unaffected by any of this -- steps 2 and 4 above
simply never apply, and resolution is the pre-V2c chain unchanged.

## `--role NAME=MODEL` (CLI)

Repeatable; a later repeat of the same role name wins. Validated once, right
after argument parsing, before either `-p` or the TUI starts building a
session -- a bad `NAME=MODEL` (no `=`, an unrecognized role name, an empty
model) is a clean exit-2 usage error, never a traceback.
```sh
rolo-claude -p --role researcher=or:deepseek/deepseek-v4.1-flash \
  "use the Researcher agent to summarize this repo"
```

## `Agent(role=...)` (the tool)

The `Agent`/`Task` tool itself accepts a `role` argument, alongside the
existing `model`: `Agent(subagent_type="general-purpose", role="researcher",
...)` makes THIS ONE call resolve against the `researcher` role's model,
overriding `general-purpose`'s own default role (`orchestrator`) for just
that call. It works identically for a custom `.claude/agents/*.md` agent that
sets its own `role:` frontmatter -- the call-time `role=` still overrides it
for that one call.

## Cost-aware defaults (documented, never automatic beyond this)

When the role table (config.json's `roles` key, itself already including
anything a team.json seeded) is **completely empty** -- no team, no local
override, nothing -- and this session's own model is a Databricks one (the
practical signature of the `work` preset; presets themselves are an
init-time-only choice and are never persisted, so this is the same proxy
`model.py`/`providers.config` already use elsewhere for "acting like work"),
exactly two roles get a built-in default:

| Role | Default |
|---|---|
| `researcher` | `dbx:databricks-deepseek-v4-1-flash` (cheap, for exploration) |
| `small` | `dbx:databricks-deepseek-v4-1-flash` (cheap, for summaries/titles) |

`orchestrator`/`coder`/`reviewer` are deliberately left out of this default --
"the session model for the rest" is already what an absent entry means, so
nothing needs to be written for them ("strong model for planning and
review" is satisfied by NOT downgrading them away from whatever strong model
the session itself is already running). This is the ONLY place a model is
ever picked for a role without the user or a team.json asking for it by name
-- any role table entry at all, even a single one, turns this off entirely
(`roles.py::resolve_role_table`), and no other code path in the harness ever
invents a role's model on its own.

## `/roles`

Shows the resolved table: model, endpoint/path type (`mlflow`/`cursor`/
`anthropic`/`invocations` for a Databricks ref, the provider name otherwise),
and price (a DBU-derived dollar figure for Databricks when the workspace
publishes one, `$/1M tokens in and out` otherwise) per role, straight off the
live session's own `agent_runtime.role_table`/`.cli_role_overrides` -- the
SAME table a role-bearing `Agent` call actually resolves against, so this
never drifts from real behavior.
```
Role table (endpoint/path type/price per role):
  orchestrator  or:deepseek/deepseek-v4.1-flash  openrouter  $0.14/1M in, $0.28/1M out  (session model)
  coder         or:deepseek/deepseek-v4.1-flash  openrouter  $0.14/1M in, $0.28/1M out  (session model)
  reviewer      or:deepseek/deepseek-v4.1-flash  openrouter  $0.14/1M in, $0.28/1M out  (session model)
  researcher    dbx:databricks-deepseek-v4-1-flash  databricks  mlflow  ? DBU  (role table)
  small         dbx:databricks-deepseek-v4-1-flash  databricks  mlflow  ? DBU  (role table)
```

## `stats --roles`

Sub-agent spend (sessions/calls/tokens/cost) per role, summed from every
`Agent`/`Task` call's own rolled-up usage node -- see
[COMMANDS.md](COMMANDS.md)'s `stats` section for the full flag table and
`--json` shape. A rolled-up node with no `role` (every pre-V2c log, and any
role-less agent's own calls) simply doesn't contribute to this table; the
existing `--models`/`--tools` tables are completely unaffected.
