# Architecture

How a keystroke becomes a model request, how that request is reconstructed
from history every single time, and how every subsystem around that loop
(permissions, hooks, MCP, compaction, sub-agents, telemetry) plugs into it.
Verified against the modules named in each section, not memory. For
*where* the config driving each of these lives, see
[CONFIG.md](CONFIG.md); for the CLI/slash-command surface built on top of
it, see [COMMANDS.md](COMMANDS.md) and [SLASH-COMMANDS.md](SLASH-COMMANDS.md).

```
 prompt / steer                                    ┌─────────────┐
      │                                             │  TUI (app.py)│
      ▼                                             │  or -p print │
┌───────────┐   append_user()   ┌───────────────┐   │  mode        │
│  Session  │ ─────────────────▶│  SessionLog   │◀──┤  (headless.py)│
│ (agent/   │                   │ (append-only  │   └──────┬──────┘
│  loop.py) │◀── derive_request()  JSONL)        │          │ events
└─────┬─────┘                   └───────────────┘          ▼
      │ build request body                              drain_queue /
      ▼                                                  apply_event
┌───────────────┐   profile    ┌────────────────────┐
│ providers/    │◀─────────────│ providers/profiles  │
│ request.py /  │              │  .py (per-family)   │
│ anthropic_sse │              └────────────────────┘
└──────┬────────┘
       ▼
  HTTP to OpenRouter / Databricks / api.anthropic.com / the `claude` binary
       │
       ▼
┌───────────────┐  tool_use   ┌────────────────┐  permission  ┌──────────┐
│ providers/    │────────────▶│ repair layer   │─────decide──▶│ Permission│
│ stream.py     │             │ (agent/repair) │              │ Engine    │
└───────────────┘             └───────┬────────┘              └────┬─────┘
                                       ▼                            │allow
                                ┌─────────────┐   dispatch          ▼
                                │ ToolRegistry│◀───────────── tools/*.py
                                └──────┬──────┘
                                       ▼
                              tool_result → SessionLog.append_tool_result()
                                       (back to the top: derive again)
```

## The session log is the single source of truth

`agent/log.py`'s `SessionLog` is an append-only JSONL file,
`~/.halo/sessions/<project-slug>/<session-id>.jsonl`
(`SessionLog.latest_for_cwd` picks the most-recently-modified file under a
project's slug directory for `-c`/`--continue`). Every node the loop ever
appends is one of: `meta`, `system`, `user`, `assistant` (content blocks
including raw thinking/reasoning, plus a sibling `tool_meta` map -- never a
content block itself -- recording per-tool-call repair status), `tool_result`,
`snapshot` (dynamic context delivered as a user-role block -- permission
mode, CLAUDE.md chain, memory index, skills, notices), `usage`, `error`,
`interrupted`, `compacted` (a pure shadow-range marker, never a rewrite of
earlier nodes), `rewind` (an audit trail entry for `/rewind`/`/undo`/`/redo`),
`prune_commit` (which tool-result stub ids `agent/prune.py`'s stable-batch
commit just fixed in place), and `improve_applied` (an approved `/improve`
write). `NODE_TYPES` in `agent/log.py` is the exhaustive, asserted-on-every-
append list.

The invariant this exists to make checkable (dsh's own phrase, adopted
directly): **"model-visible means logged. Anything that reaches a model
request must be reconstructable from the log, and a runtime invariant
asserts it."** Concretely: `agent/derive.py`'s `derive_request()` rebuilds
the exact `(system_text, messages, tools)` triple a real request would send,
*purely* from the log -- nothing the loop does to build a request reads any
other in-memory state. Two mechanisms back this, at different times: at
*runtime*, `agent/invariants.py::synthesize_missing_results` guarantees the
log itself can never be left in an unreconstructable state (every `tool_use`
id gets exactly one `tool_result` before the next assistant node, even on a
crash/interrupt); at *test* time, the assistant node's own `request_hash`
field (`agent/derive.py::content_hash_from_oai_body`, hashing the wire body
that was **actually sent**, policy fields like `max_tokens`/`temperature`
excluded) is compared against `content_hash()` re-derived from the log up to
(excluding) that same node -- an equal hash proves the request that produced
that reply is byte-reconstructable from the log alone. This is also *why*
`/rewind`, compaction, and session resume can never disagree with each other
or with what actually happened -- they all read the same one append-only
file, never a separate in-memory model.

Two direct consequences:

- **Resume** (`-c`/`-r`, `agent/sessions.py`) just points a new process at
  the same log file and calls `derive_request()` again -- there is no
  separate "session state" to reconstruct.
- **A sub-agent** gets its own log (`<parent_session_dir>/subagents/
  agent-<id>.jsonl`), never appends into the parent's, and its rolled-up
  cost/usage reaches the parent as one ordinary `usage` node the parent's
  own log gains when the child finishes -- so the parent's own history stays
  a complete, ordinary derivable log too.

## Deriving and pruning a request

`derive_request()` (agent/derive.py) walks the log forward once, producing
the model-shaped `(system_text, messages, tools)`. `agent/prune.py`'s
`prune_messages()` then runs **after** derivation and **before**
`providers/request.py` ever builds a wire body, so the log itself always
keeps the full, unpruned text -- only what actually goes out over the wire
shrinks. One rule (from dsh's own tool-result pruner, plus OpenCode's
protection-window constants): a `tool_result` whose distance from the end
of the message list -- summed, in estimated tokens, over every
`tool_result` newer than it -- exceeds `PRUNE_PROTECT_TOKENS` (40,000) is
replaced with a short excerpt plus a fixed marker string. A result inside
that protection window is left exactly as its own tool already truncated it
at logging time (`tools/truncate.py`); this module never re-truncates
something already inside its cap.

`providers/request.py::build_request_body` (OpenAI-chat dialect) and
`providers/anthropic_sse.py::build_anthropic_body` (native Anthropic
Messages) turn the pruned, derived triple into the exact wire body for the
resolved `ProviderProfile` -- system/developer placement, `max_tokens`
field name, the body allowlist (Databricks' gateway 400s on any unlisted
key), tool schema simplification, and per-family sampling parameters all
live there, driven entirely by data (`providers/model_table.json`), never
per-model `if` branches sprinkled through the request builder.

## Providers and compatibility profiles

Four routes, one `ModelRef`/`ModelProfile` pair (`model.py`) describing
each: `openrouter` and `databricks` (both `openai-chat` dialect, i.e. an
OpenAI-style `/chat/completions` body), `anthropic` (native Anthropic
Messages, used by `ant:` and Databricks' own Claude passthrough), and `cc`
(not a wire dialect at all -- see "The subscription route" below).
`providers/profiles.py::resolve_profile(route, model_table)` resolves a
`ProviderProfile` from `(host, model family)` plus any per-model override
row in `model_table.json` -- the central finding this design starts from is
that **a gateway's behavior cannot be guessed from its base URL alone**, so
every field a request needs (system-vs-developer placement, reasoning
replay format, sampling parameter support, tool-call id format, tool-choice
modes, leak-text patterns, context window) is explicit, data-driven, and
per-(family, host) rather than inferred.

`model_family(model_id)` classifies a bare upstream id into one of `claude`,
`deepseek`, `kimi`, `glm`, `qwen`, `gemini`, `grok`, `minimax`, `gpt`, or
`generic` by substring match -- this is what selects a `model_table.json`
row, and what a handful of narrow fallback defaults (for a family the
open-weight adapter research never had to cover, i.e. `claude`/`gpt`/
`gemini`/`generic`) key off when no per-model row exists at all.

## Reasoning replay per family

A "thinking" model's own reasoning tokens have to be sent back on the
*next* request (so a tool-calling turn keeps its own train of thought
intact) in whatever shape that specific API expects them in --
`ProviderProfile.reasoning_replay` picks one of:

- `"text"` / `"deepseek_reasoning_content"` -- a plain `reasoning_content`
  string field alongside the message (DeepSeek/Kimi/GLM/Grok/MiniMax family
  default).
- `"details"` / `"openrouter_details"` -- OpenRouter's own structured
  `reasoning_details` array, verbatim.
- `"thinking"` / `"anthropic_thinking"` -- a real Anthropic `thinking`
  content block with its cryptographic `signature`, for the native
  passthrough dialect (always used for a `dbx:`/`ant:` Claude ref).
- `"empty"` -- the fallback for a family/host with no known replay
  requirement at all.

`providers/hooks.py` (not the settings-hooks protocol -- a same-named,
unrelated module of small per-request transform functions) implements the
actual mechanics per format: `reasoning_echo` (DeepSeek V4's own
dual-field quirk: OpenRouter wants *both* `reasoning_content` and
`reasoning_details` on the same replayed message), `tool_id_normalize`
(`preserve` / `kimi_functions_idx` / `alnum9` / `minimax_preserve` --
different hosts accept different tool-call id shapes), and `leak_parser`
(promoting a text-embedded tool call some models emit instead of a real
`tool_use` block, keyed to that family's own known leak-text patterns).

## The frozen tool catalog and ToolSearch

`agent/catalog.py`'s `SessionCatalog` freezes the tool list **once**, before
a session's first model call: built-ins plus every already-connected MCP
tool are split into **preload** (an MCP tool's own
`_meta["anthropic/alwaysLoad"]`, or a wire name listed in
`~/.halo/config.json`'s `mcpPreload`) versus **deferred** (everything
else, reachable only through the `ToolSearch` tool), respecting the
resolved profile's `tools_max` (32 on Databricks, 128 on OpenRouter).
Growth from a `ToolSearch` load is **append-only** to an explicit ordered
name list, independent of `ToolRegistry.definitions()`'s own
(always-alphabetical) ordering -- once a session exists, that growing list
is what every later request's tool array is built from, and what a new
`meta` log node records, so `derive_request()` reconstructs the exact same
growing-but-never-reordered catalog on every replay. "Adding a tool mid-
session is an accepted one-time cache-miss, never a reorder" is the design
rule this exists to satisfy (reordering the tool array on every turn would
invalidate a provider's own prompt cache far more often than a rare
catalog growth does).

`ToolSearch("select:<name>[,<name>...]")` loads specific deferred names by
exact match; `ToolSearch("free text")` ranks by name/description (an MCP
tool's own `_meta["anthropic/searchHint"]` is weighted above both). A name
that resolves but can't be loaded because the catalog is already at its cap
and nothing loaded-but-deferred can be evicted is reported as refused,
explicitly, in that same call's result -- it is never silently promised for
"next turn" when it isn't actually going to be there.

## Tools and the repair layer

`tools/registry.py::ToolRegistry` holds the frozen catalog (`without()`/
`filtered()` build the deny-rule/`--tools`-restricted view at session
start; `add_tool()`/`remove_tool()` are the only in-place mutations,
reserved for `ToolSearch`'s lazy load/LRU eviction). Every dispatch goes
through the one `ToolRegistry.dispatch()` choke point, which never lets a
tool's own exception escape (a raising `run()` becomes an ordinary
`is_error=True` `ToolResult` instead of killing the loop) and stamps
`duration_ms` for telemetry. A batch of consecutive read-only calls
(`Tool.is_read_only`, informational-only -- see "Permissions" below) runs
concurrently on a small bounded thread pool, results returned in call
order, never completion order.

Before a model's raw `tool_use` block ever reaches dispatch, `agent/
repair.py::repair_assistant_turn` runs, in this fixed order (agent/loop.py's
own scope-D contract): `agent/invariants.py::validate_tool_use` (non-empty
id, dict-shaped input) → name resolution/schema validation+coercion/
duplicate-call detection/promoting a text-embedded call to a real
`tool_use` (via `providers/hooks.py::leak_parser`) → `permissions.py::
PermissionEngine.decide()` → dispatch → `tools/truncate.py::
spill_and_truncate` → the logged `tool_result`. Nothing in this chain
silently drops a call: every outcome is either a usable (possibly repaired)
block or a clear, quoted error text the model can act on.

`agent/invariants.py` also enforces the loop's own "session poisoning"
guards at well-defined points: every `tool_use` id gets exactly one
`tool_result` before the next assistant node (`synthesize_missing_results`
manufactures an `ABORTED_BEFORE_DISPATCH`/"Tool call interrupted by user"
result for any left dangling on cancel/crash/interrupt), and a lone UTF-16
surrogate from a stream truncated mid-character is repaired rather than
left to crash whatever reads it next.

## Permissions: grammar, modes, and what "auto" means here

`permissions.py::PermissionEngine.decide(tool_name, tool_input, tool)` is
grammar and modes **only** -- the owner's explicit "no cyber blocks" decision:
there is no classifier, no destructive-command list, no protected-path
list, and no security-tool flagging anywhere in this engine or the tool
layer. `Tool.is_read_only`/`is_destructive` exist purely for the read-only
concurrency pool and a tool card's own display; the permission engine never
reads either one to gate or auto-deny anything.

`decide()`'s own order, every call, every mode:

1. A `deny` rule match → deny, always (checked before anything else, in
   every mode including `bypassPermissions`).
2. Not in `bypassPermissions`: an `ask` rule match → resolve as an ask
   (interactive: a real card; print mode: a clean deny naming what would
   have been asked, since there is no one to answer it).
3. `auto` or `bypassPermissions` mode, nothing denied/asked above → **allow
   outright**. This is the entire definition of "auto mode": allow
   everything not matched by the user's own explicit deny/ask rule. No
   other logic runs.
4. An `allow` rule match → allow.
5. Otherwise, the **mode table** below decides.

| Category | `default` | `acceptEdits` | `plan` | `dontAsk` |
|---|---|---|---|---|
| Read inside a working dir | allow | allow | allow | allow |
| Read outside a working dir | ask | ask | ask | deny |
| Read-only Bash/PowerShell | allow | allow | allow | allow |
| Edit/Write inside a working dir | ask | allow | deny | deny |
| Everything else | ask | ask | deny | deny |

(`auto` and `bypassPermissions` never consult this table at all -- step 3
above already resolved them.) A handful of tools are categorized outside
this table entirely, in every mode: `TodoWrite`/`ToolSearch` (no side
effects requiring permission), `AskUserQuestion` (allowed everywhere except
`dontAsk`, where it errors instead of hanging), `Agent`/`Task` (spawning is
never mode-gated -- the depth-1 and concurrency caps are the real
guardrail), and `EnterPlanMode` in print mode (always allowed -- it only
ever *narrows* what happens next). Plan mode additionally always allows
Write/Edit on exactly the one active plan file, regardless of the table
above.

Rule grammar (`parse_rule`) matches Claude Code's own exactly: `Tool` (bare,
the whole tool), `Tool(content)` (exact/prefix/glob string, or a `path`
value for a path-taking tool, or `domain:` for WebFetch), `Bash(cmd:*)`-
style glob content with real shell-segment-aware matching (a read-only
allowlist recognizes common git/gh read subcommands so `git status`/`git
log`/etc. never need an explicit rule), `mcp__server`/`mcp__server__tool`/
`mcp__server__*`, `Agent(subagent_type)`, `Skill(name)`, and a
deny/ask-only `Tool(param:value)` form. Rules are assembled once per
session (`build_rules_from_settings`) from the merged settings chain (see
`docs/CONFIG.md`) plus `--allowedTools`/`--disallowedTools` plus a session's
own interactively-granted rules; only `deny`/`ask` rules from an *untrusted*
project/local settings layer survive -- an untrusted layer's own `allow`
entries are dropped before the engine is even built.

Seven mode **names** exist (`default`/`manual` is a display alias for the
same mode, `acceptEdits`, `plan`, `dontAsk`, `auto`, `bypassPermissions`);
`Shift+Tab` cycles the first four of these in the TUI.

**`decide()`'s outcome is not the last word -- the user's own hooks still
run after it, in every mode.** `PreToolUse` (and, on an `ask` outcome,
`PermissionRequest`) fires for every call regardless of what `decide()` just
returned, including a call `auto`/`bypassPermissions` already allowed
outright -- a `PreToolUse` hook can still block the call, rewrite its input
(`decide()` is then re-run against the rewrite, so a rewritten call can't
silently dodge a rule that now matches it), or flip the decision itself. So
the precise, complete statement of what `auto` mode allows is: **everything
not matched by the user's own deny/ask rule *or* blocked by the user's own
hook** -- never a built-in judgment call, but a hook the user configured
themselves is exactly as much "the user's own rule" as a permission rule
is.

## Hooks protocol

`hooks.py` implements Claude Code's hook protocol verbatim -- matcher +
`if`-rule filtering, five handler types run in parallel, and the
`Stop`/`SessionEnd` special cases -- with, again, no safety heuristics of
its own: a hook is entirely the user's own gate.

- **Handler types**: `command` (a shell command; `shell: "bash"` forces
  real bash even on POSIX, where the unmarked default is `/bin/sh`),
  `http` (a URL call), `mcp_tool` (calls a tool on a configured MCP
  server), `prompt` (a small-model call, template `$ARGUMENTS` → the JSON
  payload), `agent` (same as `prompt`, semantically -- both are
  "informational only" about which model runs it, always the session's
  small model).
- **Matcher**: omitted/`""`/`"*"` → matches everything; a
  `^[A-Za-z0-9_,|-]*$`-shaped string → an exact name list split on `,`/`|`;
  anything else → an unanchored regex (an invalid one skips that hook with
  a logged warning, never crashes).
- **`if`**: reuses `permissions.py`'s own rule grammar/matching, so a
  hook's own conditional gate is the identical syntax to a `settings.json`
  permission rule.
- **Events actually fired**: `SessionStart`/`SessionEnd`,
  `UserPromptSubmit`, `PreToolUse`/`PostToolUse`(`Failure`),
  `PostToolBatch`, `PermissionRequest`/`PermissionDenied`,
  `Stop`/`SubagentStart`/`SubagentStop`, `PreCompact`/`PostCompact`, and (W4a/
  W5) `TaskCreated`/`TaskCompleted`, `FileChanged`, `CwdChanged`,
  `DirectoryAdded`, `ConfigChange`, `InstructionsLoaded`, `MessageDisplay`,
  `Setup`, `WorktreeCreated`/`WorktreeRemoved`,
  `PreModelSwitch`/`PostModelSwitch`, `UserPromptExpansion`, `StopFailure`.
  A configured hook for an event Claude Code also defines but this build
  doesn't fire yet (`ElicitationRequest`, `ElicitationResponse` --
  `hooks.NOT_EMITTED_V1`'s full set) parses fine and is silently never
  triggered, rather than erroring.
- **Combining outcomes**: several hooks matching the same event run
  concurrently; their outcomes combine as `deny > defer > ask > allow` for
  the permission decision, `additional_context` concatenated, the *last*
  `updated_input`/`set_mode` wins (well-defined since result order follows
  match order, not completion order).
- **`Stop`/`SubagentStop`**: a consecutive-block cap
  (`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`, default 8) force-ends the turn with an
  explanatory system message once a hook has blocked that many times in a
  row, so a misbehaving Stop hook can never wedge a session forever.
- **`SessionEnd`**: a real time budget (default 1.5s, raised to the largest
  matched hook's own `timeout` up to a 60s cap, or
  `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` outright) -- every matched hook
  runs concurrently within that one shared window, never sequentially.
- **Child environment**: every hook subprocess gets `tool_child_env()`-
  stripped env (no provider API keys) plus `CLAUDE_PROJECT_DIR`/
  `CLAUDE_PLUGIN_ROOT`/`CLAUDE_EFFORT`/`CLAUDE_ENV_FILE` (for the four
  events whose whole point is exporting more env for later Bash calls to
  pick up: `SessionStart`, and three not-yet-emitted events it's ready for).

## MCP: scopes, lazy start, cache, plugin servers

`mcp/manager.py::resolve_server_configs()` assembles the final `{name:
McpServerConfig}` map, first-name-wins, in this exact order: `managed-
mcp.json` (if present at all, **exclusive** -- every other source is
skipped) → else `--mcp-config` (repeatable) and this run's own `--chrome`/
`--playwright` dynamic entries, same precedence tier → (stop here under
`--strict-mcp-config`) → local `projects[cwd].mcpServers` in
`~/.claude.json` → `<cwd>/.mcp.json` (subject to `.mcp.json`-server-only
approval/`disabledMcpjsonServers`) → user `mcpServers` in `~/.claude.json`
→ installed-plugin-provided servers (lowest precedence). Managed
`allowedMcpServers`/`deniedMcpServers` and a project's own
`disabledMcpServers` (no "json") are applied last, on every path,
regardless of which tier a name came from. Every surviving entry then gets
`${VAR}`/`${VAR:-default}` expansion against the session's effective
environment.

**Lazy by default** (H13): a server connects only the first time one of its
tools is actually called, or when `/mcp` explicitly reconnects it --
`alwaysLoad` servers and one marked `"mcpLazy": false` (per-server, or
globally via a top-level `"mcpLazy": false` in `settings.json`) connect
eagerly at session start instead. A per-server cache under
`~/.halo/mcp/tools-cache/<server>.json`, keyed by a hash of the
server's own identity-relevant config fields (command/args/env/url/
headers -- never `cwd`/`timeout`/scope, which don't change what
`tools/list` returns), keeps a lazy server's tool names/descriptions known
-- and therefore searchable/preloadable by `ToolSearch` -- with **zero live
connections**. A cache hit seeds a `"cached"` handle state directly; a miss
(first run, or the identity-relevant config changed) connects that server
for real, once, to learn its tools and write the cache. Every server
needing a real first-time connect does so **concurrently**, one shared
timeout window for the whole batch, never one-at-a-time.

Handle states: `pending` → `pending_approval` (an unapproved `.mcp.json`
server) → `cached` → `connecting` → `connected` | `failed` | `needs_auth` |
`closed`. The status bar's "MCP n/m" counts only real connections, so a
fresh session with several lazy servers typically starts at "MCP 0/m" and
climbs as tools are actually used.

Plugin-provided servers (`config/plugins.py`) are discovered from
`~/.claude/plugins/installed_plugins.json` (both the V1 single-dict and the
real V2 per-scope-array manifest shapes), each plugin's own `.mcp.json` or
`.claude-plugin/plugin.json`, with `${CLAUDE_PLUGIN_ROOT}` expanded to that
one plugin's own install directory. A discovered plugin server is named
`plugin_<plugin>_<server>` on the wire (`mcp__plugin_<plugin>_<server>__
<tool>`), sanitized the same way any other server name is -- nothing
downstream needs to know a tool came from a plugin at all.

## Steering, interrupts, and pairing invariants

Typing while a turn is running calls `Session.steer(text)` directly (the
worker thread is parked inside the running turn's own generator, so there is
no separate command queue to go through). Under one lock, it atomically
checks whether the session is busy and appends to `_steer_queue` -- busy-
check and enqueue must be a single atomic step, or a turn could finish in
the gap between them and the text would never be drained. A steer is
**never** applied instantly; it waits for the next safe point inside
`_step()`'s per-chunk stream loop, `_dispatch_tools()`'s per-call loop, or
`_turn_body()`'s post-dispatch/post-stream points, all funneled through
`_apply_pending_steers_events()`. Each one becomes a user-role log message
through the **same `UserPromptSubmit` hook** the first message of a turn
uses -- a hook that filters/blocks prompts cannot be bypassed by typing
mid-turn. A steer that arrives while a permission/question card is still
waiting for its own answer does not answer that card; it just queues,
exactly like at any other moment.

**Esc (interrupt) is a different signal from a steer in every dimension.**
It sets one `threading.Event` (`Session.abort`) that:

- cuts the current `_step()` mid-stream with no retry (the partial text is
  still logged, tagged `[Request interrupted by user]`, but the model call
  itself is over);
- inside tool dispatch, lets anything **already dispatched** (a running
  read-only batch, a solo call in flight) finish normally, but never starts
  anything after it, and manufactures an `is_error` tool_result for every
  remaining `tool_use` via `agent/invariants.py::synthesize_missing_results`
  (reason `"Tool call interrupted by user"`/`"ABORTED_BEFORE_DISPATCH"`);
- **ends the turn** (`turn_done(reason="interrupted")`) rather than
  continuing, which a steer never does on its own.

A **shared** abort (a foreground sub-agent uses the same `Event` object as
its parent, so the parent's own Esc also interrupts it) is only ever
`.clear()`-ed by whichever `Session` actually owns it -- a child never
clears a signal a concurrent sibling or the parent itself might still be
relying on.

This machinery exists to guarantee one rule everywhere, always: **every
`tool_use` block gets exactly one `tool_result` before the next assistant
node.** Any exception unwinding out of a turn (`GeneratorExit`, a genuine
crash) still runs `synthesize_missing_results` before propagating -- a
provider that 400s on an assistant message with an unanswered tool call
should never be reachable from a real interrupt or a real bug.

**The loop breaker** guards against a model calling the same tool with the
same arguments over and over. The key is `(tool_name, canonical_json(args))`
(property order ignored); a run of identical calls gets a `[reminder: ...]`
suffix on the tool_result once it reaches **3**, is denied outright at **5**,
and force-ends the turn at **8** (`agent/loop.py`'s own
`_LOOP_BREAKER_REMIND_AT`/`_LOOP_BREAKER_DENY_AT`/`_LOOP_BREAKER_END_AT`).
`BashOutput` is fully exempt (polling a background job by the same id is a
normal pattern, not a loop) -- a genuinely runaway poll is bounded by
`--max-turns` instead. A second detector catches strict A/B/A/B alternation
(two *different* calls trading off) the plain repeat-count would miss
entirely; whichever of the two counts is higher for a given call is what's
actually compared against the thresholds above. Both counters reset at the
start of every new `turn()`, never across turns.

## Compaction

Checked once, right after every successful model call
(`Session._maybe_auto_compact`, called from inside `_turn_body`): the
trigger token count is `min(dsh's own 80%-of-(context minus a 65,536-token
headroom) formula, OpenCode's own "usable" formula -- context minus a
capped max-output minus a capped reserve)`, then **floored** at 70% of the
raw context window (so a small-context open-weight model still gets a
sensible trigger instead of one near zero) unless an explicit
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` genuinely lowers the 80% further. A model's
advertised `max_output_tokens` is itself capped at 32,000 before any of this
math runs, fixing real catalog rows whose advertised output made the naive
trigger come out at zero. Two guards stop back-to-back compaction attempts:
`_just_compacted` (skips one check immediately after a compaction ran) and a
5-step backoff after a compaction that failed or didn't free enough room.

A compaction is a real model call: the exact derived+pruned prefix so far,
plus one final instruction message demanding a checkpoint summary under
**eight fixed headings, in order** -- Primary Request and Intent, Key
Technical Concepts, Files and Code, Errors and Fixes, Pending Jobs, Current
Work, Next Step, Critical Context -- explicitly told to merge (not just
re-quote) any prior `<compacted-summary>` block already in the prefix, with
the conversation winning on conflict. A reply missing a heading, or one that
was truncated (`stop_reason` of `max_tokens`/`length`), gets exactly one
corrective retry naming what was wrong; if that still fails, or the call
itself hits a context overflow, one further fallback re-serializes the
whole transcript as flattened plain text and summarizes that instead.
**On any failure the session log is left byte-for-byte unchanged** -- the
`compacted` marker node is only ever written once a usable summary actually
exists. `PreCompact`/`PostCompact` hooks fire around the attempt either way.

On success, the replacement content re-injects every session-start snapshot
kind (CLAUDE.md, memory index, environment, a deferred-tools reminder, and
the active plan file if plan mode is on) plus a "files read this session"
snapshot, then a verbatim tail of the most recent messages (assistant
`tool_use`/matching `tool_result` pairs are kept glued together as one atomic
unit, never split; any `thinking` block in that tail is stripped, since a
replayed signature is bound to the exact prefix that produced it, which
compaction just changed). `/compact [instructions]` runs the identical
mechanism synchronously; a session with no prior history to summarize is a
plain no-op. **A `cc:` session's compaction is a real, documented no-op --
Claude Code manages its own context window**, never a failure.

## Sub-agents and plan mode

The `Agent`/`Task` tool call is dispatched directly from `agent/loop.py`
(bypassing the ordinary `ToolRegistry.dispatch()` choke point) so several
calls in one assistant turn can run **concurrently**, up to
`MAX_CONCURRENT_AGENTS = 4` (`agent/subagent.py`), each streaming its own
live events back into the parent's stream immediately rather than being
buffered until the whole batch finishes -- this is what lets a foreground
child's own permission ask reach a real, answerable card while it's still
running. `MAX_DEPTH = 1`: a sub-agent's own `Agent`/`Task` call is refused
outright (a sub-agent cannot itself spawn a sub-agent).

A child gets its own real `Session`, logging to
`<parent_session_dir>/subagents/agent-<id>.jsonl` (never the parent's own
file): a tool registry **filtered down from the parent's already-frozen
catalog** (`AgentSpec.resolved_tools()` intersects/subtracts, never adds --
this is the concrete mechanism behind "a sub-agent never grows the parent's
catalog"), its own model (invocation override → frontmatter `model:` →
`CLAUDE_CODE_SUBAGENT_MODEL`/`settings.subagentModel` → the parent's own
model, with `haiku` mapping to the parent's *small* model specifically), its
own permission mode/engine (sharing the parent's deny/ask rules by
reference, a **copy** of its allow rules), and its own `HookRunner` tagged
with `agent_id`/`agent_type` so `SubagentStart`/`SubagentStop` matchers can
target it. A **foreground** child shares the parent's own `abort` Event (the
parent's Esc interrupts it too); a **background** one (`run_in_background`,
or an `AgentSpec`'s own `background: true`) gets an independent `Event` of
its own, so an unrelated Esc never kills it -- only `TaskStop(task_id=...)`
or the whole process quitting does. Either way the child shares the
*parent's* `job_registry`, so the parent's own shutdown kills a Bash job a
sub-agent started too.

A foreground child of an **interactive** parent gets genuinely live,
answerable permission cards (the same `_permission_waiters` dict, keyed
`f"{agent_id}:{tool_id}"` so parallel children never collide, resolved by
the exact same `Controller.answer_permission` path a top-level call uses); a
background child, or any child in print mode, falls back to an immediate
deny (recorded in `permission_denials`) since there is no live stream to
show a card on. When a child finishes, its own newly-appended `usage` nodes
are summed and rolled into the parent as **one** ordinary `usage` node
(`CostMeter.add_child_total`), and its `permission_denials` are merged into
the parent's -- the parent's own log stays a complete, ordinary derivable
log, never a log with holes a viewer has to know to look inside a
`subagents/` directory to fill in. A `task_id` lets a caller resume the same
child session later (`Agent(task_id=..., prompt=...)`); a still-running
background task refuses a concurrent resume rather than racing its own log
writer.

**Plan mode** writes to exactly one file, `~/.claude/plans/<three-word-
name>.md` (honoring `settings.plansDirectory`) -- the permission engine's
`plan_file`/`set_plan_file()` plus the mode table's dedicated
`plan_file_write` category (allow, in every mode, but only when the target
*is* that one file and the session is actually in `plan` mode) is what makes
it the sole writable path while every other Write/Edit is denied.
`EnterPlanMode`/`ExitPlanMode` are wire-schema-only tool definitions;
`agent/loop.py` special-cases both names the same tier as `AskUserQuestion`,
never running them through the ordinary `decide()`/dispatch path. Entering
plan mode (model-initiated, or `--permission-mode plan`/Shift+Tab) injects
a fixed system-prompt note: investigate and propose, make no changes except
to the plan file itself. `ExitPlanMode(plan)` writes the file and surfaces a
`plan_review` event; the reply -- the TUI's `PlanCard` offers exactly three
-- is one of `{"approved": true, "mode_after": "acceptEdits"}` (approve,
auto-accept edits from here), `{"approved": true, "mode_after": "default"}`
(approve, manual), or `{"approved": false, "feedback": "..."}` (keep
planning, with optional free-text feedback borrowed from the prompt input).

## The subscription route and its bridge

`cc:` is not a wire dialect at all -- there is no `CompletionRequest`, no
`stream_completion`, no `ProviderProfile`. The **first** `cc:` turn in a
session lazily starts one `claude` subprocess
(`agent/cc_process.py::build_cc_argv`/`ClaudeCodeProcess`) and keeps it
running, stdin held open, across every later turn on that route:

```
claude -p --model <id> --output-format stream-json --input-format stream-json \
  --verbose --include-partial-messages --replay-user-messages --tools "" \
  --strict-mcp-config --mcp-config <inline JSON, one server named "rolo"> \
  --settings '{"disableAllHooks": true}' --permission-mode bypassPermissions \
  [--resume <id> | --session-id <uuid4>] [--fork-session] \
  [--append-system-prompt <capped ~3500 chars>] [--max-turns N] [--effort LEVEL]
```

The session id claude actually uses comes from a **logged `meta` node**
(`cc_session_id`), read back on every later turn -- not derived
deterministically from halo's own session id (a `uuid5` helper for
that exists in the code but is unused dead code; the log is the real
mechanism, consistent with "model-visible means logged" applying here too).
A `--resume` that claude itself rejects (a stale/in-use conversation) falls
back to a fresh `--session-id`, primed with a capped, rendered tail of the
prior log as one `<conversation-so-far>` message rather than surfacing the
error.

**Every halo tool is exposed to that subprocess through a small local
bridge** (`halo_harness/ccbridge/`), as one inline stdio MCP server named
`"rolo"` -- Claude Code sees every bridged tool as `mcp__rolo__<Name>`. The
transport is a Unix domain socket at `~/.halo/run/<session>.sock`
(directory mode 0700, socket mode 0600 -- filesystem permissions are the
whole authentication) on POSIX, or a TCP loopback socket plus a random
per-session `secrets.token_hex(16)` token (compared with `hmac.compare_digest`)
on Windows, where sockets have no filesystem-permission equivalent; either
way the endpoint reaches the child **only** through its own environment
variables, never its command line. A `tools/call` on that bridge routes
straight into `Session._resolve_tool_call`/`_finalize_tool_result` -- the
literal same methods every other route's tool dispatch uses -- so
permission decisions, `PreToolUse`/`PermissionRequest`/`PostToolUse` hooks,
the loop breaker, always-allow rules, and the session log all apply to a
bridged call exactly as they would anywhere else. A second, dedicated bridge
connection long-polls for `notifications/tools/list_changed` so a tool
`ToolSearch` loads mid-session becomes visible to Claude Code without
restarting it.

A steer sent mid-turn is forwarded to the subprocess's stdin immediately
(`--replay-user-messages` echoes each stdin line back once claude actually
*consumes* it -- that echo, not send time, is what the harness logs as
"applied"); a turn's own `result` event is only treated as the end of the
turn once every sent-but-unconfirmed line has echoed back, so one `result`
correctly covers either a steer folded into the reply already in progress or
a queued follow-up turn. `total_cost_usd` on a `result` is cumulative for
the whole `claude` process; only the **delta** since the previous turn is
ever logged/metered, never the raw cumulative figure. Switching the active
model into `cc:` mid-session (or back out of it) primes/drains the prior
log as one capped plain-text message rather than true native history;
`/clear` drops the logged `cc_session_id` (a fresh conversation); `/fork`
passes `--fork-session` through.

## Telemetry and `/improve`

`halo_harness/telemetry.py` derives **everything** it reports from
non-wire metadata already sitting on existing log nodes (`usage.route`/
`.provider`/`.finish_reason`/`.ttft_ms`/`.latency_ms`/`.retries`/`.status`,
an assistant node's `tool_meta`, a `tool_result`'s `tool`/`error_class`/`ms`/
`bytes`/`spilled`) -- never a new model-visible field, and a corrupt JSONL
line is skipped and counted, never a crash. `telemetry.scan()` walks
`~/.halo/sessions/<slug>/*.jsonl`, caching one `SessionSummary` per
file at `~/.halo/stats-cache.json` keyed by `(path, size, mtime)` so
an unchanged log is never re-parsed; `aggregate_by_model`/`aggregate_by_tool`
turn a batch of summaries into the rows `halo stats --models/--tools`
and `/stats --models` render.

`/improve` is entirely human-gated and never runs on its own: `improve/
evidence.py` clusters the same telemetry-adjacent signal into six kinds
(repeated tool errors by `error_class`, repair-layer hits by kind, loop-
breaker trips, user corrections following a tool error or matching a fixed
cue list, `Read` ENOENT/wrong-cwd patterns, and a 3-tool sequence recurring
across at least 3 sessions), each excerpt tagged with whether it came from
the user's own words or from tool output. `improve/draft.py` makes **one**
model call (config `improve.model` → the session's small model → the
session model) asking for at most 8 JSON-shaped candidates, with one retry
on malformed JSON; a candidate citing an evidence reference that never
actually appeared in a cluster has that reference silently dropped, never
invented. Nothing reaches disk until a card's `a`/`e` key (or an explicit
headless `improve --apply FILE#ID`) approves it: `improve/apply.py` writes
atomically, tags the file with an HTML-comment provenance marker
(`<!-- halo improve: created=... sessions=... model=... -->`), and
only ever **updates** a file that already carries that exact marker --
colliding with a user-authored file picks a new name instead of touching
it. `d` (dismiss forever) records a content hash
(`sha256(kind|scope|path|body)`) in `~/.halo/improve/dismissed.json`
so the same candidate never resurfaces. A separate, much cheaper check
(`improve/hint.py`) counts a session's own repair-hits/edit-failures/loop-
breaker-trips against a configurable threshold and, if crossed, shows a
**one-time, counters-only** notification -- never a model call, never a
card, identical in auto mode.

## The TUI event model

`halo_harness/events.py` is pure data with no dependency on the rest of the
package: an `Event(kind, data, turn, agent_id, ts)` / `Command(kind, data)`
pair, each `kind` drawn from a fixed, validated vocabulary
(`EVENT_KINDS`/`COMMAND_KINDS`), with one small factory function per kind
documenting its own `data` shape. `Session.turn()` is a generator that
yields these events as the turn actually progresses; **print mode and the
TUI consume the literal same iterator** -- `PrintModeSink`/`StreamJsonSink`
(`output.py`) drain it into text/JSON, and the TUI's `Controller` runs it on
a worker thread and pushes each event onto a queue instead.

`tui/app.py`'s `BridgeApp` polls that queue on a 30 Hz timer (`_drain`, an
8 ms per-tick budget), coalesces what it collected
(`tui/events.py::drain_queue`), and hands each item to
`tui/dispatch.py::apply_event` -- **the only code path that ever touches a
widget**, so nothing in this application is ever mutated from a thread other
than the UI thread. `apply_event` wraps its real work in a broad
try/except: a handler that raises on a reachable-but-unusual payload (a
repair-rejected tool call's raw `None` argument, an unfamiliar question
shape) becomes one error note plus a toast, never a crashed session with an
orphaned child process. A `tool_use_ready`/`permission_request`/`question`/
`plan_review` event each mount their own widget (`ToolCard`/`PermissionCard`/
`QuestionCard`/`PlanCard`) and, for the three cards, call
`app.set_pending_card()`, which both gives the card keyboard focus (so its
own number-key/letter `BINDINGS` win over the prompt input) and rings the
terminal bell; a card that needs one line of free-text follow-up
temporarily borrows the prompt input (`app.borrow_input`) rather than
growing a text widget of its own. Every event carries an `agent_id`
(`None` for the top-level session); a sub-agent's own `status`/`message_end`/
`turn_done` events are applied to its own transcript block but never touch
the main status bar or the auto-title/idle bookkeeping those same event
kinds drive for the top-level session.

**The status bar** (`tui/widgets/statusbar.py`) reads `context_tokens`/
`context_limit`/`cost_usd`/`total_input_tokens`/`total_output_tokens` off
every `status` and `message_end` event (1.0.1 hotfix 14) -- the same raw
numbers `Session.status_event`/`agent/loop.py`'s per-step `message_end`
already compute for `/cost`/`stats`, never re-derived in the UI. Two pure
formatters in `halo_harness/model_display.py` (shared with the hotfix-12
model-listing row format) turn those numbers into text that is NEVER the
bare `ctx ?`/`$?` a Databricks model with no known context limit/price
showed permanently before 1.0.1: `format_status_context(tokens, limit)`
renders `"ctx 12k/1M 1%"` with a limit or `"ctx 12k"` (used tokens alone)
without one; `format_status_cost(cost_usd, total_in, total_out)` renders a
real `"$0.0123"` when a cost is known (provider-reported, or `CostMeter`'s
fallback formula computed from a models.dev-resolved Databricks price) or
`"in 12k out 3k"` (running token totals) when no price is known at all. On
a narrow terminal the cwd/branch segment shrinks first (down to nothing),
and only then is the model label itself left-truncated with a leading
ellipsis (`truncate_label_left`) -- ctx and cost stay visible before cwd
does.

**OpenRouter balance (H15 part 2 addendum 4).** A segment right after
cost, two decimals (`StatusBar.set_or_balance`) -- the one provider with a
balance API, this round. Populated by a background worker
(`tui/slash.py::or_balance_refresh_worker`, never the UI thread): once at
app launch, again every `BALANCE_REFRESH_INTERVAL_S` (5 minutes,
`providers/openrouter_account.py`), and once after every turn (debounced to
at most once every `BALANCE_POST_TURN_DEBOUNCE_S`, 60 seconds).

Two DIFFERENT OpenRouter endpoints, two different keys (`resolve_balance`'s
own 3-way preference order, checked in this exact sequence):
1. `"OR $12.40 left"` -- `GET /key`'s own `limit_remaining`, sent with the
   ordinary `OPENROUTER_API_KEY` (same key every other call already uses),
   when THIS key has a real `limit` set.
2. `"OR $12.40 left"` -- else, when a SEPARATE, higher-privilege
   `OPENROUTER_MANAGEMENT_KEY` is configured, `GET /credits`'s whole-account
   `total_credits - total_usage` (OpenRouter's own spec requires the
   management key here -- the ordinary key is never sent to `/credits`, and
   the management key is never sent to `/key` or anywhere else).
3. `"OR $3.21 used"` -- else, this key's own `/key` `usage` figure (a
   SPEND, not a remaining balance -- the honest fallback for an unlimited
   key with no management key, where no "remaining" number can be known).

Both calls are best-effort and never raise; a failed refresh leaves
whatever was cached before untouched rather than blanking the segment, and
the segment dims (never disappears) once that reading is more than 10
minutes old. Omitted entirely (no segment, no trailing separator) when
OpenRouter isn't enabled or a fetch has never once succeeded. `/cost` and
`/providers` print the same cached figure
(`providers/openrouter_account.py::format_balance_line`), naming WHICH of
the three it is, plus the key's own label and the wall-clock time of the
reading.

**Auto-scroll (1.0.1 hotfix 16).** `Transcript` (`tui/widgets/transcript.py`,
a `VerticalScroll` itself) calls Textual's own `self.anchor()` once in
`on_mount` -- the compositor then keeps `scroll_y` pinned to the live
bottom on every layout pass for as long as the widget stays "following",
which covers a child growing mid-stream (a `MarkdownStream.write()`
reflow), not just a fresh mount. Textual's own `scroll_y` watcher releases
that following on any ordinary user scroll (PageUp, mouse wheel) and
re-acquires it automatically once the view is scrolled back to the true
bottom -- `Transcript.is_following()` reads this indirectly (`is_at_bottom()`,
already used for the pre-1.0.1 one-shot mount scroll) rather than reaching
into a private Textual flag. `new_since_scroll` (the status bar's "↓ N new")
increments on every mount AND on every delta that lands while released
(`Transcript.note_growth()`, called from `append_text`/`append_thinking`
for a block that already exists); `Ctrl+End`, a click on the indicator, or
submitting a new prompt all call `scroll_end()` and reset the counter,
which is exactly what Textual's own watcher treats as "the user returned
to the bottom".

**A pending card intercepts free text (1.0.1 hotfix 17).**
`on_prompt_input_submitted` (`tui/app.py`) checks `self.pending_card`
before ever reaching `Controller.submit`'s steer path: when the card
implements `resolve_with_message` (`PermissionCard`/`PlanCard`/
`QuestionCard` all already did, for their own borrowed-input flows), the
typed text answers the card directly instead of becoming a "↳ steering…"
note the pending ask never sees. `action_cycle_mode` (Shift+Tab) likewise
re-evaluates an already-pending `PermissionCard` under the NEW mode right
away (`auto`/`bypassPermissions` resolves it allowed, `dontAsk` denied) --
`Controller.set_permission_mode` itself only ever affected FUTURE asks,
a plain attribute write with no knowledge of one already parked in
`Session._await_permission_decision`.
