# rolo-claude

A standalone, Claude-Code-compatible agent harness: full-screen TUI, `-p`
print mode, and the same config, session and tool conventions as the real
`claude` CLI, driving models over four routes -- OpenRouter (DeepSeek, Kimi,
GLM, Qwen and more), Databricks (every endpoint a workspace serves, including
its Claude endpoints), the Anthropic API, and your own Claude subscription
through the installed `claude` binary. Roles (`orchestrator`/`coder`/
`reviewer`/`researcher`/`small`, [docs/ROLES.md](docs/ROLES.md)) let a team
point different kinds of work at different models without hand-editing every
agent file, and the work-matrix workflow (`doctor --work --probe-all` ->
`work-matrix show`/`apply`) turns a live probe of your own Databricks
workspace into a suggested per-endpoint fix. It reads your existing Claude
Code configuration (settings, permissions, hooks, CLAUDE.md, memory, skills,
commands, agents, MCP servers) so nothing has to be set up twice. **Kali
Linux is the primary target platform** -- Windows is the secondary/build
host. It also ships the older `claude-bridge` proxy (drives the REAL `claude`
binary against OpenRouter or Databricks) as the `rolo-claude proxy`
subcommand -- see **Proxy mode** near the end.

## Quick start (Kali / Linux)

```sh
cd /path/to/rolo-claude
uv tool install --editable .        # or: pipx install --editable .
rolo-claude init                    # one command: pick a provider, credentials, default model, doctor, live pong
rolo-claude                         # full-screen TUI
```

`rolo-claude init` shows an arrow-key list of the four providers it can set up (Databricks, OpenRouter,
the Anthropic API, your Claude subscription), cursor already on whichever one auto-detection would pick
(an existing OpenRouter key, a Databricks host/`ucode-settings.json`, an `ANTHROPIC_API_KEY`, or a
claude.ai login, in that order) -- pick one, or a different one, and it asks only for that provider's
missing credential; `rolo-claude init --provider openrouter --yes` runs it fully non-interactively for
one provider (repeat `--provider` for more than one; the deprecated `--preset home|work|claude` still
works too). Re-running is always safe -- it shows the current state and changes nothing already
configured. See
`docs/harness/INSTALL.md` for the full walkthrough (offline/work-box install, PEP 668 workarounds,
terminal notes) and `rolo-claude doctor` for a read-only environment check with a fix for every WARN.

**Install once, run anywhere.** `uv tool install --editable .`/`pip install --user -e .` puts a real
console script on PATH -- once it's there, `rolo-claude` works from any directory, not just this
checkout (the checkout is only ever needed for `git pull`). Re-run `uv tool install --reinstall .`
(or `pip install --user -e .` again) after every `git pull` so the installed command actually picks up
the update; `rolo-claude doctor`'s own "command on PATH" check (and `init`'s own summary line) catches
it and names the exact command if you forget.

### Windows (five lines)

```powershell
cd C:\path\to\rolo-claude
uv tool install --editable .
rolo-claude init
rolo-claude --version
rolo-claude
```

## What it is

`rolo-claude` is its own agent loop, not a wrapper around `claude`: it reads
your real `~/.claude.json`, `~/.claude/settings.json`, `CLAUDE.md`, hooks,
skills, custom slash commands, subagent definitions and MCP server configs
directly, builds the same kind of system prompt and tool set Claude Code
would, and talks to the model provider itself (no local proxy server, no
`claude` subprocess, no Anthropic account required). Every model turn is
derived from an append-only session log (`~/.rolo-claude/sessions/<slug>/
<id>.jsonl`) -- if it isn't in that log, the model never sees it, which is
also what makes `/rewind`, compaction, and session resume all agree with
each other and with what actually happened.

No safety/refusal classifier, destructive-command blocklist, or protected-
path heuristic exists anywhere in the permission engine or tool layer --
permission decisions are pure grammar (deny/ask/allow rules + mode), the
same as Claude Code's own, never an extra layer of "is this dangerous"
judgment.

**What it is not**: a fork of Claude Code's own source (none of it is
reused -- the compatibility is behavioral, built by reading its documented
config/permission/hook conventions); a hosted service (everything runs on
your own machine, talking straight to whichever provider you configured);
or dependent on an Anthropic account for its open-weight routes (`or:`/
`dbx:` need no Anthropic relationship at all -- only `ant:`/`cc:` do, and
`cc:` reuses a subscription login you already have rather than creating a
new dependency).

## A 10-minute walkthrough

```sh
rolo-claude init                    # pick a provider, set credentials, doctor, live pong
rolo-claude                         # full-screen TUI
```

1. **`rolo-claude init`** lets you pick which provider to set up -- Databricks,
   OpenRouter, the Anthropic API, or your Claude subscription -- asks for the
   one credential it's missing, picks a default model from that provider's
   own catalog, and ends with a real "pong" from it; it then offers to set up
   another provider, looping until you're done (with more than one
   configured, one last pick chooses the overall default) -- see
   `docs/COMMANDS.md`'s `init` section for exactly what each step reads and
   writes.
2. **The first session** opens with an empty prompt line and a status bar
   showing the model, permission mode, and MCP server count. Type a prompt
   and press `Enter`.
3. **`/model`** opens a picker of every model this box can currently reach
   (OpenRouter's cache, Databricks' own discovered endpoints grouped by
   family, and your subscription's nine names if logged in); pick one, or
   type `/model or:deepseek/deepseek-v3.2` directly.
4. **A tool call with a permission card**: ask for something that edits a
   file (`"add a .gitignore entry for *.log"`). In the default permission
   mode, an `Edit`/`Write` inside your working directory shows a
   `PermissionCard` -- `1` allow once, `2` allow for the rest of this
   session, `3` allow always (writes a rule to
   `.claude/settings.local.json`), `4`/`Esc` deny (optionally typing one
   line telling the model what to do differently first). `Shift+Tab`
   cycles into `auto` mode any time you'd rather not be asked at all --
   auto mode allows everything except a rule *you* wrote yourself, never a
   built-in judgment call.
5. **Steering**: type another line and hit `Enter` while a turn is still
   running -- it queues and is woven in at the next safe point (a chunk
   boundary, or right after the tool call in progress finishes), shown as
   "steering..." until then. It never needs you to wait for the turn to
   finish first, and it never answers a permission/question card that
   happens to be pending at the same moment.
6. **`/resume`** (or `-r` on the command line next time) opens a picker of
   recent sessions for this directory, live-filtered as you type by title,
   first prompt, cwd, or model.
7. **`/stats --models`** aggregates tokens/cost/tool-error-rate/repair-hit-
   rate across every session logged for this project -- the same data
   `rolo-claude stats --models` prints headlessly (`docs/COMMANDS.md`).
8. **`/improve`** scans recent sessions for repeated friction (tool errors,
   repair-layer hits, corrections you typed), drafts up to eight candidate
   memory/rule/skill files with one model call, and reviews them one card
   at a time -- `a` apply, `e` edit first, `s` skip, `d` dismiss forever,
   `q` stop. Nothing is written to disk until you say so.

## Documentation

| Doc | Covers |
|---|---|
| [docs/COMMANDS.md](docs/COMMANDS.md) | every CLI subcommand and flag, with worked examples |
| [docs/SLASH-COMMANDS.md](docs/SLASH-COMMANDS.md) | every `/command`, key binding, chord, and `@file`/`!cmd` prefix |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | the session log, request derivation, providers, permissions, hooks, MCP, compaction, sub-agents, the `cc:` bridge, telemetry, the TUI event model |
| [docs/CONFIG.md](docs/CONFIG.md) | exactly which Claude Code files are read (and how), rolo-claude's own files, every environment variable |
| [docs/MODELS.md](docs/MODELS.md) | model reference forms, per-family request-shaping rules, catalogs, pricing |
| [docs/DATABRICKS.md](docs/DATABRICKS.md) | the work-box setup, discovery, routing, team onboarding, troubleshooting by HTTP status, the work-matrix fixes tooling |
| [docs/ROLES.md](docs/ROLES.md) | orchestrator/coder/reviewer/researcher/small, resolution precedence, `--role`, `/roles`, `stats --roles` |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | symptom -> `doctor` line -> fix |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | repo layout, the three test suites, how to extend the harness |
| [docs/harness/README.md](docs/harness/README.md) | the build history (milestone briefs, reviews, acceptance records) |
| [docs/harness/INSTALL.md](docs/harness/INSTALL.md) | the full install walkthrough (PEP 668, offline work-box, terminal notes) |

## Install

See `docs/harness/INSTALL.md` for the full walkthrough (PEP 668/externally-
managed-environment workarounds, PATH setup, reproducible installs via
`requirements.lock`, an offline **work box** recipe for a machine with no
PyPI access, and terminal-specific notes). Short version:

```sh
# Kali / any Linux, recommended (never touches system/apt Python):
cd /path/to/rolo-claude && uv tool install --editable .

# or plain pip:
cd /path/to/rolo-claude && pip install --user -e .
```

```powershell
# Windows (build host):
cd C:\path\to\rolo-claude
uv tool install --editable .
```

Both produce a `rolo-claude` console script. Prerequisite: Python 3.10+;
`uv` is recommended but optional (everything falls back to `pip`/`python3`).

## Configuration

`rolo-claude init` (see Quick start above) does everything below in one
command; this section is the manual/reference version of the same steps.

Put `OPENROUTER_API_KEY` in `~/.config/vibes-hacker/env` (`KEY=value`, `#`
comments, optional leading `export`; override the path with
`BRIDGE_ENV_FILE`) or export it yourself -- that's the only required setup
for OpenRouter models. Databricks credentials are discovered automatically,
same chain the whole project has always used: explicit `BRIDGE_DBX_BASE_
URL`+`BRIDGE_DBX_TOKEN` wins outright, then `ANTHROPIC_BASE_URL`+
`ANTHROPIC_AUTH_TOKEN`/the settings `env` chain when the host is a real
Databricks host, then `DATABRICKS_HOST`+`DATABRICKS_TOKEN`, then (H8)
`~/.claude/ucode-settings.json` if Databricks' own `ug`/unity-gateway CLI
already wrote one, then `~/.databrickscfg`'s `[DEFAULT]` section last (a
generic, possibly stale/unrelated-workspace fallback).
Nothing to configure if any of those already work for the real `claude` CLI
on the same box.

`rolo-claude doctor` is a read-only environment check (Python version,
`~/.claude` layout, env file, OpenRouter/Databricks reachability, catalog
cache ages, chrome/playwright/plugin detection, `rg`/`$VISUAL`/`$EDITOR`/
tmux mouse mode/Bash-shell presence, clipboard backend, configured MCP
servers, the default model in `~/.rolo-claude/config.json`); every `WARN`/
`MISSING` line ends with the exact fix command (or a doc reference when
there's no single command), and `doctor --json` prints the same checks as
`{id, status, message, fix}` records. `rolo-claude doctor --work`
is the Databricks-specific preset for a VPN-gated work box (VPN
reachability, token validity, the reasoning-replay/route-split probes) --
see INSTALL.md's "Work box" section.

## First run

```sh
rolo-claude                          # full-screen TUI (needs a real terminal)
rolo-claude "read README.md"         # TUI, prompt pre-filled as the first turn
rolo-claude -p "reply with the word pong"   # print mode, scriptable/headless
rolo-claude --demo                   # scripted TUI walkthrough, no network/model needed
```

Bare `rolo-claude` (no `-p`) checks `stdin.isatty()` before importing
`textual`; outside a real terminal it prints one line to stderr and exits 2
instead of hanging -- expected, use `-p` for anything non-interactive
(cron, CI, a subprocess).

## Config reuse from Claude Code

The whole point is that a box already set up for `claude` needs nothing
extra: `~/.claude.json` (trust, MCP servers, project state), `~/.claude/
settings.json` (managed -> user -> project -> project-local, later wins),
every `CLAUDE.md` up the directory tree, `~/.claude/agents/*.md` and
`.claude/agents/*.md` (subagent definitions), `~/.claude/commands/` and
`.claude/commands/` (custom slash commands), `~/.claude/skills/` and
`.claude/skills/` (skills), `.mcp.json` (with the same first-time-approval
flow), plugins under `~/.claude/plugins/`, and `~/.claude/keybindings.json`
are all read as-is. A server/command/skill/agent added to any of those
files (by hand, by `claude mcp add`, or by the real `claude` CLI) is picked
up by the next `rolo-claude` session with no separate config step; a server
added via `rolo-claude mcp add` is equally visible to `claude mcp list`.
`--settings`/`--setting-sources`/`--bare`/`--strict-mcp-config`/`--mcp-
config` all work the same as Claude Code's own flags.

## Models and providers

Model reference forms:

| Form | Example | Resolves to |
|---|---|---|
| `or:vendor/model` | `or:deepseek/deepseek-v3.2` | OpenRouter |
| bare `vendor/model` (contains `/`) | `deepseek/deepseek-v3.2` | OpenRouter |
| `dbx:databricks-<name>` | `dbx:databricks-kimi-k3` | Databricks, OpenAI-chat dialect |
| `dbx:system.ai.<name>` | `dbx:system.ai.my_model` | Databricks, OpenAI-chat dialect |
| any of the above with `claude` in the name | `dbx:databricks-claude-sonnet` | Databricks, raw Anthropic passthrough |
| `cc:<name>` | `cc:opus`, `cc:sonnet` | Your Claude subscription, via the installed `claude` binary |
| `ant:<name>` | `ant:opus`, `ant:claude-3-5-haiku` | `api.anthropic.com`, pay-as-you-go (`ANTHROPIC_API_KEY`) |
| a bare subscription-model name, no prefix | `opus`, `sonnet`, `fable` | `cc:` if logged in and no key is set, else `ant:` if a key is set, else an error |

`--model`/`--small-model` pick the main/background-task model for the
session; `rolo-claude models --refresh` pulls OpenRouter's `/api/v1/models`
and models.dev's `api.json` into `~/.rolo-claude/` (context window, max
output tokens, vision support per model); without ever having run that, a
vendored fallback copy shipped inside the package (`rolo_claude/providers/
catalog/`) still lets profile resolution work completely offline. Databricks'
own endpoint list is cached by the same probe. Per-family prompt notation
(including a Kimi-specific block adapted from OpenCode's own) adjusts tool-
call/thinking conventions per model family automatically.

### Claude models with your subscription (`cc:`)

`cc:fable`, `cc:opus`, `cc:opus-5`, `cc:opus-5.0`, `cc:opus-4.8`,
`cc:opus-4.6`, `cc:sonnet`, `cc:sonnet-5` and `cc:haiku` run on the Claude
models included in your Claude subscription -- **not** the Anthropic API,
and rolo-claude never touches your Claude Code login to get there.
`ant:<same names>` reach the same models through `api.anthropic.com`
pay-as-you-go instead (needs `ANTHROPIC_API_KEY`); a bare name with no
prefix (`--model opus`) picks whichever of the two is actually available,
preferring `cc:` when you're logged in and no key is set.

**How it works**: rolo-claude never reads, copies or replays Claude Code's
OAuth credentials (`~/.claude/.credentials.json` is never opened, not even
to check it exists -- `claude auth status`'s own JSON answers that) and
never sends them to `api.anthropic.com` itself. Instead it drives the
`claude` binary you already have installed and logged in, headlessly, as
the model: one `claude -p --input-format stream-json --output-format
stream-json ...` subprocess per session, started the first time you use a
`cc:` model and kept running across turns. rolo-claude's own tools (Read,
Bash, MCP servers, everything) are handed to that subprocess through a
small local bridge -- Claude Code sees them as one MCP server (`mcp__rolo__
<Name>`) -- so every `cc:` tool call still goes through rolo-claude's OWN
permissions, hooks, session log and telemetry, exactly like every other
route. `doctor` shows "Claude subscription: logged in ... -- cc: models
available" when this is usable; `rolo-claude models --cc` lists all nine
names with their current targets and pricing.

**Limitations of this v1**: a steer sent mid-turn is forwarded to Claude
Code immediately, which queues it on its own terms rather than rolo-claude
cutting the current reply the way it does for every other route -- Claude
Code may fold it into the reply already in progress, or answer it as its
own follow-up turn once that one finishes; either way rolo-claude waits
for however many turns it actually takes and shows "queued for Claude
Code" the moment it's sent. Claude Code applies its own auto-compaction to
a `cc:` conversation; rolo-claude's own `/compact` is a real no-op there (a
note explains why, rather than the confusing failure earlier builds gave).
`stats --models`/`/cost` show a `cc:` row's cost as Claude Code's own
estimate, logged as the delta since that same `claude` process's previous
turn (its own `total_cost_usd` is cumulative for the whole process) -- a
subscription isn't billed per token, so this is never exact spend the way
every other route's real per-token pricing is, and it never counts toward
`--max-budget-usd`. Switching models into `cc:` mid-session (or resuming a
process that idled on another route for a while) hands the live/new
subprocess the prior conversation as one capped plain-text summary message
rather than true native history; `/clear`, `/fork` and a `cc:` model
change each start (or branch, for `/fork`) a genuinely new Claude Code
conversation instead. Claude Code's own tool-call loop drives `cc:`, so a
sub-agent's ask now gets a real, answerable card (live-forwarded from the
child) the same way a native tool call's does -- but a bridged call
inherits the harness's own loop-breaker the same way, so an unusually
repetitive bridged tool-call pattern can be denied/end the call the same
way it would on any other route.

### Databricks at work

If the box already runs Claude Code against a Databricks workspace, rolo-claude
needs zero setup: it reads the SAME `~/.claude/settings.json` `env` block
Claude Code itself uses (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`,
`ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`/`_SONNET_MODEL`/
`_HAIKU_MODEL`, `ANTHROPIC_CUSTOM_HEADERS`), the same host/gateway-path
convention (`https://<workspace>/ai-gateway/anthropic` splits into the bare
workspace root plus the gateway path), and maps the default model and bare
`opus`/`sonnet`/`haiku` through it -- `run rolo-claude` and it drives the
same models Claude Code would. **Nothing about the real workspace ever
leaves the box**: the hostname and token live only in the local env file
(or wherever Claude Code's own settings already put them) and are never
written into a session log, a cache file, or printed by `doctor`.

**Discovery, not a vendored list.** `rolo-claude models --refresh` (or
`init --provider databricks`) lists the workspace's own serving endpoints and caches
them, per user, to `~/.rolo-claude/dbx-endpoints.json` (name, `api_types`,
`foundation_model.name`, task) -- the ONLY source of truth for what a
workspace serves. A generic family x api_type table (detected from the
endpoint's own name) then decides each endpoint's route: Claude foundation
endpoints default to the native `anthropic/v1/messages` gateway; GLM/Kimi
default to `mlflow/v1/chat/completions` chat (the anthropic gateway is
selectable per model with `databricks.gateway.<endpoint>: anthropic` in
`~/.rolo-claude/config.json`, or a one-off `dbx:<endpoint>@anthropic` suffix);
DeepSeek/Qwen/Llama/Gemma/gpt-oss/GPT/Grok/Gemini default to mlflow chat
(the wire "model" value is the endpoint's own discovered
`foundation_model.name`, e.g. `system.ai.qwen35-122b-a10b`, never guessed by
prefixing); `databricks-gpt-5-5-pro` has no mlflow chat and uses `cursor/v1/
chat/completions` instead; Bedrock EXTERNAL Claude endpoints
(`us-anthropic-claude-*`) are invocations-only, never the native passthrough,
despite "claude" being in the name; an embeddings/whisper endpoint is
refused with a clear message and hidden from `/model`. An endpoint the cache
doesn't know about yet still works (the pre-discovery candidate order).

**Team setup.** A shared, checked-in `team.json` (see `team.example.json`)
holds the workspace host, a default model, a per-family gateway preference,
and a DBU price -- **never a token, never an endpoint list** -- discovered
at `.rolo-claude/team.json` in the project, `~/.rolo-claude/team.json`, or
`--team <path|url>`. With a host already known (from `team.json` or Claude
Code's own settings), `rolo-claude init --provider databricks` asks ONLY for the
personal Databricks token (hidden input, written to the env file at 0600),
refreshes the catalog, and prints how many endpoints are available and the
default model.

**Keeping the catalog fresh.** `/models refresh` (alias `/dbx`) re-lists the
workspace off the UI thread and reports what changed since last time;
`rolo-claude models --refresh --urls [--json]` prints the exact URL and path
type (mlflow/cursor/anthropic/invocations) each endpoint resolves to, for
scripting. The cache auto-refreshes off the UI thread, with a one-line diff
notification, whenever it's older than `databricks.catalog_max_age_hours`
(default 24) and `/model` is opened; an already-cached catalog also
refreshes silently (never a first-time discovery call) when a Databricks
session starts. A refresh that fails (offline, or 403 from the IP access
list) just keeps the existing cache and says so.

**Diagnostics.** `rolo-claude doctor --work` prints the derived workspace
root, the gateway path, the header NAMES it sends (never values), the
resolved default model and effort, and which config source supplied the
token; it distinguishes a bad token (401) from the IP access list (403 with
Databricks' own wording -- "connect to the VPN") from a token that can run
inference but not list endpoints (403 without that wording) from a wrong
path (404). `rolo-claude doctor --work --probe-all [--both] [--tools]
[--only <glob>]` is the full work matrix: one short pong through every
chat-shaped endpoint on its chosen path (plus the anthropic gateway too for
Claude/GLM/Kimi with `--both`; a tool-call check with `--tools`), a table of
status/latency/output tokens, and a JSON report at `~/.rolo-claude/
work-matrix-<date>.json` naming endpoints only -- no host, no token -- so it
can be pasted back for review.

**Turning probes into fixes.** `rolo-claude work-matrix show <report.json>`
reads that same JSON report and prints one line per failing endpoint with a
suggested action (a 403 -> get on the VPN; a wrong default gateway path ->
set `databricks.gateway.<endpoint>`; a family-wide tool-call/reasoning-replay
issue -> report only, no per-endpoint fix exists); `rolo-claude work-matrix
apply <report.json> [--yes]` writes the one class of fix that maps onto a
real `~/.rolo-claude/config.json` key, after listing exactly what it's about
to write and asking for confirmation. See
[docs/DATABRICKS.md](docs/DATABRICKS.md)'s own **End-to-end team workflow**
section for how this fits together with `team.json` and `init`.

**A Databricks-only edition.** If your whole team only ever talks to a
Databricks workspace, a separate repository,
[databricks-claude](https://github.com/roloVibes/databricks-claude), is a
dedicated Databricks-only edition built for that case. rolo-claude itself
stays the general harness across all four routes above.

### Roles

`orchestrator`/`coder`/`reviewer`/`researcher`/`small` (V2c) let a team
point different kinds of work at different models -- cheap for exploration,
strong for planning/review -- without hand-editing every agent file.
`~/.rolo-claude/config.json`'s (or a shared `team.json`'s) `roles` table
sets a model per role; built-in agents (`general-purpose`, `Explore`,
`Researcher`, `Plan`, `Reviewer`, `Coder`) each carry a fixed default role, a
custom `.claude/agents/*.md` sets one with a `role:` frontmatter key, and
`--role NAME=MODEL`/`Agent(role=...)` override one for a single run/call.
`/roles` shows the resolved table (model, endpoint/path type, price) per
role; `rolo-claude stats --roles` sums sub-agent spend per role. See
[docs/ROLES.md](docs/ROLES.md) for the full resolution precedence and the
(documented, never automatic beyond one specific case) cost-aware defaults.

## Permissions and auto mode

Seven modes, same names and mode-table semantics as Claude Code:
`default` (`manual` is a display alias for the same thing), `acceptEdits`,
`plan`, `dontAsk`, `auto`, `bypassPermissions`. Grammar and modes ONLY --
**no classifier, no destructive-command list, no protected-path list, no
security-tool flagging, anywhere**: `auto` allows everything not matched by
an explicit deny/ask rule or blocked by the user's own hook;
`bypassPermissions` allows everything not matched by a deny rule (hooks
still apply to both -- a `PreToolUse`/`PermissionRequest` hook the user
configured runs after every decision, in every mode, and can still block
or rewrite a call `auto` already allowed); the manual modes keep Claude
Code's own semantics because the user chose them deliberately, with no
extra heuristics bolted on. Rules are the same grammar Claude Code accepts
(`Bash(git status:*)`,
path globs, `mcp__server`/`mcp__server__tool`, WebFetch domains, etc.),
read from `permissions.allow`/`.ask`/`.deny` in the same settings chain,
plus `--allowedTools`/`--disallowedTools`/`--dangerously-skip-permissions`.

## Steering

Typing while a turn is running queues a mid-turn steer (multiple steers
apply in order); a steer that arrives while a permission/question card is
still waiting for its own answer doesn't answer that card, it just queues.
Print mode gets the same thing via `--input-format stream-json` lines
arriving mid-turn. The transcript shows "steering..." while one is in
flight.

## Hooks, skills, commands, MCP, browser

**Hooks**: the events this harness actually fires are `SessionStart`/
`SessionEnd`, `UserPromptSubmit`, `PreToolUse`/`PostToolUse`(`Failure`),
`PostToolBatch`, `PermissionRequest`/`PermissionDenied`, `Stop`/
`SubagentStart`/`SubagentStop`, and `PreCompact`/`PostCompact` -- and every
handler type a hook definition can invoke for them -- `command` (shell),
`prompt` (inject text), `agent` (run a sub-agent), `http` (call a URL),
`mcp_tool` (call a tool on a configured MCP server) -- is wired. A settings.
json/hooks.json entry for a name Claude Code also recognizes but this
build doesn't fire yet (`Notification`, `TaskCreated`/`TaskCompleted`,
`PreModelSwitch`/`PostModelSwitch`, the worktree lifecycle events, and a
few others -- see `hooks.py`'s own `NOT_EMITTED_V1`) parses fine and is
silently never triggered, rather than erroring.

**Skills and custom commands**: discovered from both user and project
directories exactly like `claude`; a skill's/command's body can reference
`@path` (a local file, read the same way a model's own Read call would) and
`@server:resource` (an MCP server's resource, read via `resources/read`) --
both are appended as a separate context snapshot, never inlined into the
submitted text. An MCP server's own prompts show up as `/mcp__<server>__
<prompt>` slash commands automatically.

**MCP**: stdio/http/sse transports, user/project/local/managed/plugin
scopes, the `.mcp.json` first-time-approval flow, always-load servers,
`ToolSearch` for a large/deferred tool set, resources and prompts
(`ListMcpResourcesTool`/`ReadMcpResourceTool`, `@server:resource` mentions,
`/mcp__server__prompt`), `/mcp` to inspect/reconnect mid-session. **Lazy by
default**: a server connects the first time one of its tools is actually
called, or when `/mcp` reconnects it -- `alwaysLoad` servers and one marked
`"mcpLazy": false` (per-server, or globally via a top-level `"mcpLazy":
false` in `settings.json`) connect eagerly at session start instead. A
per-server cache (`~/.rolo-claude/mcp/tools-cache/`) keeps a lazy server's
tool names/descriptions searchable/preloadable even with zero live
connections -- the status bar's "MCP n/m" only counts real connections, so
a fresh session typically starts "MCP 0/m" and climbs as tools are used;
`doctor`/`mcp list`/`/mcp` show a `cached` (not yet connected, but known)
state alongside the usual connected/failed/pending ones.

**Browser**: `--chrome` (the claude-in-chrome extension's native-messaging
bridge) and `--playwright`/`--playwright-cdp`/`--playwright-headless`
(a real Chromium/Chrome/Brave via Playwright); a screenshot from either one
shows up as its own tool card in the TUI transcript. In a terminal that
speaks the kitty graphics protocol (kitty, WezTerm, Ghostty, foot) or sixel
(detected live; tmux needs `allow-passthrough on` first) it renders inline,
downscaled to a bounded size; everywhere else it's captioned with its media
type, real dimensions and size (e.g. "image/png, 1280x800, 84.2 KB"), same
as before -- `images: "inline"|"caption"|"off"` in `~/.rolo-claude/
config.json` (default `"inline"`) or `--no-inline-images` forces the plain
caption. The real image block is still what the model itself sees (subject
to the same vision/size gate as any other image) regardless of how it's
displayed to you.

**Images/vision**: Read returns a real `image` content block (not just a
path string) for png/jpg/gif/webp when the active model's profile says it
supports vision, resized/downscaled to at most 1568px and 5MB (omitted with
a plain note instead when it can't be brought under that even after
resizing, or when Pillow isn't installed at all -- the `vision` extra,
`pip install --user -e '.[vision]'` (or `uv tool install --editable
'.[vision]'`) from the checkout, same as every other install command in
this doc, is optional, never required); an MCP
tool's own image results get the same treatment. `@path` mentions to an
image file (TUI or a skill/command body) and `--file PATH [PATH ...]`
(a local path -- see `rolo-claude --help`; Claude Code's own `file_id:
relative_path` cloud-resource form isn't backed by anything in a
standalone harness and gets a clear notice instead of pretending to work)
both attach the same way.

**Background jobs**: `Bash(run_in_background: true)`, and a foreground
command that outruns its timeout is moved to the background instead of
being killed (Claude Code's own behavior) -- `BashOutput`/`TaskStop` poll/
kill a job by its shell id, completion is delivered as a notice at the next
turn (never silently lost), `/tasks` lists what's running, and every job is
killed when the session quits.

## Sessions

`-c`/`--continue` resumes the most recent session for the current
directory; `-r`/`--resume [ID]` resumes a specific one, or opens a picker
with a live fuzzy text filter (title, first prompt, cwd, model -- `/resume
<text>` in the TUI opens it pre-filtered) when given no id at all; `-r
<text>` on the command line resumes the one session that text uniquely
matches, or (print mode) lists the ambiguous candidates instead of
silently guessing, or (interactive) opens that same picker pre-filtered by
`<text>`; `--fork-session` continues from one without overwriting it;
`-n`/`--name` labels a session. `/rewind` (and the TUI's undo) restores both the
conversation log and any file a Write/Edit touched, via a shadow-copy
mechanism scoped to those two tools by design -- a Bash- or NotebookEdit-
made change is not shadow-copied and `/rewind` won't undo it (detecting
which files a shell command touched would need a blocking `git status`
call; see `tui/dispatch.py`'s `_maybe_record_shadow_step`). Auto-compaction
triggers well before the model's real context ceiling (an 80%-of-usable
default, floored so a small-context open-weight model still gets a
sensible trigger point instead of ~0), summarizing older turns while
keeping a verbatim recent tail; `count_tokens` is used for the trigger
instead of an estimate wherever the route actually supports it (Databricks/
Anthropic passthrough), falling back to an estimate otherwise.

## Print mode

```sh
rolo-claude -p "reply pong"
rolo-claude -p --output-format json "..."
rolo-claude -p --output-format stream-json --input-format stream-json < turns.jsonl
```

`--output-format text|json|stream-json`, `--input-format text|stream-json`
(one turn per line, read incrementally so an SDK-style client that waits
for each turn's result before sending the next one never deadlocks),
`--include-partial-messages`, `--max-turns`, `--max-budget-usd`,
`--json-schema` (structured final output), `--replay-user-messages`. Exit
code is the last turn's; a background job still running when a single-
prompt `-p` call ends gets its completion notice printed before the
process exits rather than dropped.

`rolo-claude export --sanitize` and `rolo-claude stats` are headless
versions of the TUI's own `/export`/`/stats` slash commands -- the former
redacts API keys/tokens/secrets from a session log before handing it to
someone else, the latter aggregates turn/cost/token stats across sessions.

## Telemetry and `/improve`

`rolo-claude stats --models [--tools] [--since 7d|30d|all] [--all-projects]
[--session ID] [--json]` (and `/stats --models` in the TUI, off the UI
thread) aggregates per-model/provider and per-tool counters -- tokens,
cost, avg ttft/latency, retries, overflows, tool error rates, repair-layer
hit rates, edit failures -- from the existing session logs; nothing here
adds a model-visible field. `doctor` shows the sessions count, stats-cache
age and the active `/improve` config.

`/improve` is human-gated self-improvement: it clusters recent failures
(repeated tool errors, repair-layer hits, loop-breaker trips, user
corrections, Read ENOENT, a recurring tool sequence), drafts candidate
memory notes / `.claude/rules/*.md` entries / skills with ONE model call,
and reviews them one card at a time -- `a` apply, `e` edit in
`$VISUAL`/`$EDITOR` then apply, `s` skip, `d` dismiss forever, `q` stop.
Nothing is written without that approval (or an explicit headless
`rolo-claude improve --apply FILE#ID`); provenance (source sessions,
evidence count, drafting model, whether an excerpt came from tool output)
is shown on the card, never used to block anything. `rolo-claude improve
[--since] [--all-projects] [--json]` prints/saves candidates without
applying; `-p` sessions never draft or write on their own. Configured via
`~/.rolo-claude/config.json`'s `improve` key, e.g. `rolo-claude config set
improve.model or:deepseek/deepseek-v4-flash`.

## Troubleshooting

- **VPN hint**: any Databricks connect failure (`doctor --work`, `models
  --refresh`, or a live request) names the VPN as the likely cause.
- **`externally-managed-environment` from pip**: see INSTALL.md's PEP 668
  note; `uv tool install --editable .` sidesteps it entirely.
- **`rolo-claude: command not found` right after installing**: `~/.local/
  bin` isn't on PATH yet for a non-login shell -- see INSTALL.md.
- **Bare `rolo-claude` exits 2 immediately**: expected outside a real
  terminal (piped input, cron, a subprocess with no tty) -- use `-p`.
- **A tool isn't visible to the model**: check whether it's a deferred MCP
  tool not yet referenced this conversation (`ToolSearch` finds it by
  name/description) or excluded by `--tools`/a deny rule.
- **`--file file_id:relative_path` prints a notice and does nothing**:
  expected -- that's Claude Code's claude.ai-hosted file-resource form,
  which this standalone harness has no backing store for; a local path
  works directly.
- **A flag prints a `not yet` notice**: that flag is parsed (never an
  argparse error) but its feature isn't built yet; `--ide`, `--worktree`,
  `--remote-control`, `--teleport` and a handful of others are intentionally
  still in that state -- see `rolo_claude/cli.py`'s `_NOT_YET_FLAGS`.
- **Where to look**: session logs under `~/.rolo-claude/sessions/`;
  `rolo-claude doctor` / `doctor --work` for environment issues;
  `--verbose` for a running commentary of intermediate model
  calls/tool calls on stderr in print mode (`-d`/`--debug` is one of the
  not-yet flags above -- it parses but does nothing yet).

## Security posture

- No local server/open port in the harness itself for the OpenRouter/
  Databricks/`ant:` routes -- a direct in-process HTTP client, not a proxy
  something else connects to. (`rolo-claude proxy` is one exception, see
  below, security posture unchanged from `claude-bridge`'s; a `cc:` session
  is the other -- see just below.)
- **`cc:` never touches Claude Code's login.** `~/.claude/.credentials.json`
  is never opened, not even to check it exists (`claude auth status`'s own
  JSON is the only thing read, and its OAuth token itself is never
  extracted or forwarded anywhere); that check runs with every
  `ANTHROPIC_*`/`CLAUDE_CODE_*` variable stripped from its own environment,
  so an API key or an outer Claude Code session's identity can never make
  it misreport which login is active. The `claude` subprocess itself (and
  the MCP child it spawns) gets the same stripped environment, so the
  subscription login is always what a `cc:` turn actually uses -- doctor
  and `cc:` refuse to say "available" unless that check's own `authMethod`
  is `claude.ai`. The local tool bridge a `cc:` session opens (a Unix
  socket, mode 0600, on POSIX; a TCP loopback socket + a random per-session
  token on Windows, passed to the `claude` subprocess through its
  environment, never its command line) only ever talks to the ONE `claude`
  subprocess THIS session started, on this machine, for this session's
  lifetime -- closed, along with any of its own sub-agent children's, when
  the session restarts (`/clear`, `/fork`, a model switch) or ends.
- Credentials come from the environment, the settings chain, or
  `~/.databrickscfg`/`ucode-settings.json`; they're never written to the
  repo, a command line, or committed config. `rolo-claude --config`-style
  output and logs redact keys/tokens/auth headers.
- The permission engine is pure grammar (see Permissions above) -- no
  hidden allow-list, no "trusted command" special-casing beyond what a
  rule/mode explicitly says.
- `export --sanitize` exists specifically for handing a session log to
  someone else without also handing them your API keys.

## Tests

```sh
python tests/run_all.py; echo exit=$?      # the core suite (agent loop, tools, config, MCP, permissions, ...)
python test_tui.py; echo exit=$?           # the TUI suite (Textual pilots + SVG snapshots)
python test_bridge.py; echo exit=$?        # the claude-bridge proxy's own black-box suite
```

All three are exercised on both Windows and WSL/Linux every milestone (see
INSTALL.md's "Cross-checking on WSL" recipe) -- Kali Linux being the actual
target, not an afterthought. Suites use mock upstreams throughout; no live
network call happens as part of `python tests/run_all.py` itself.

## Proxy mode (`claude-bridge` / `rolo-claude proxy`)

Before rolo-claude became its own harness, this repo was `claude-bridge`: a
single-file HTTP proxy (`bridge.py`, stdlib only) that sits in front of the
REAL `claude` binary and translates its Anthropic-Messages-API calls to
OpenRouter or Databricks, leaving `claude`'s own subscription, `CLAUDE.md`,
settings, memory, MCP servers, skills, hooks and permissions completely
untouched -- it only intercepts the one HTTP call `claude` makes to its
model. That still exists, unchanged, as `rolo-claude proxy` (equivalently,
`python bridge.py ...` directly):

```sh
rolo-claude proxy launch --model or:deepseek/deepseek-v3.2 -p "reply pong"
rolo-claude proxy --probe        # outbound reachability check for both providers
rolo-claude proxy --config       # resolved configuration as JSON, secrets redacted
rolo-claude proxy --stop         # shut down a running proxy server
```

Use this mode specifically when you want to keep using the real `claude`
CLI itself (its own update cadence, its own bug-for-bug behavior) with a
non-Anthropic model underneath, rather than rolo-claude's own agent loop.
State lives in `~/.claude-bridge/` (`BRIDGE_STATE_DIR` to override); loopback-
only bind, bearer-token-gated, redacted logs -- see `docs/harness/` for the
proxy's own historical design notes if you need the low-level details
(request translation, the 128-tool cap, context-overflow rewriting, etc.),
which are unchanged from before this became a standalone harness.

## Licence

MIT. See `LICENSE`.
