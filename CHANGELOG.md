# Changelog

All notable changes to `rolo-claude` are recorded here, newest first. This
project does not (yet) follow strict semver across the 0.3.x line -- each
0.3.0 milestone below was a working checkpoint toward the single 0.3.0
release, not a separate published version.

## [0.4.1] - 2026-09-28

H12 (RECOMMENDATIONS.md P0): the whole first run in one command, a
prescriptive `doctor`, and the cheapest fix for DeepSeek V4.1 Flash's own
telemetry-observed 8% Edit "Found multiple matches" rate.

- **`rolo-claude init`** (`rolo_claude/init_cli.py`): picks a preset
  (`home`/OpenRouter, `work`/Databricks, `claude`/your subscription --
  auto-detected, or `--preset`/`--yes` for non-interactive), asks for a
  missing OpenRouter key or Databricks host/token with hidden input and
  writes it into the same env file the harness already reads (POSIX mode
  0600, dir 0700, existing lines preserved, never echoed), sets
  `~/.rolo-claude/config.json`'s `model` key, runs `doctor` and `models
  --refresh`, sends a live pong, and (Linux only) offers a static `rg`
  install into `~/.local/bin` and the `~/.local/bin`-on-PATH rc-file line
  (`~/.zshenv` for zsh, `~/.profile` otherwise, behind a marker comment) --
  every step is idempotent, so re-running only ever reports the current
  state. Never touches `~/.claude.json`/`~/.claude/settings.json`, never
  prints a key/token. `~/.rolo-claude/config.json`'s `model` now sits in the
  default-model precedence chain (`--model` > `BRIDGE_MODEL`/`routes.json`
  > config.json > the built-in default) that both `-p` and the TUI resolve
  through (`model.resolve_default_model_raw`, `headless.build_session`).
- **Prescriptive `doctor`**: every `[WARN]`/`[MISSING]` line now ends with
  `-> fix: <exact command>` or `-> see: <reference>`; `doctor --json` prints
  the same checks as `{id, status, message, fix, see}` records (what `init`
  consumes for its own summary). New checks: `~/.local/bin` on PATH for a
  non-interactive shell, `tmux` mouse mode when `$TMUX` is set, configured
  MCP servers (eager vs. `mcpLazy`), and the default model in
  `config.json` plus whether its provider actually resolves.
- **Edit tool context hint** (`rolo_claude/providers/model_table.json`'s new
  `edit_hints` map, `providers/profiles.edit_hint_for`): a DeepSeek/Kimi/
  GLM/Qwen/MiniMax-family session's Edit tool description gets one extra
  line asking for at least three lines of `old_string` context, computed
  once when the frozen tool catalog is built so the wire request stays
  cache-prefix-stable; Claude/GPT sessions are unaffected, byte for byte.
- **README/INSTALL.md**: a "Quick start (Kali / Linux)" section leads the
  README now (install, `rolo-claude init`, `rolo-claude`, then Windows in
  five lines); INSTALL.md points to `init` up front.

## [0.4.0] - 2026-09-28

H11: Claude models through the user's own Claude subscription (`cc:` route)
plus first-class `ant:` aliases for the same six models via
`ANTHROPIC_API_KEY`. Binding constraint: rolo-claude never reads, copies or
replays Claude Code's OAuth credentials (`~/.claude/.credentials.json`) and
never calls `api.anthropic.com` with them -- the subscription is used the one
legitimate way, by driving the installed `claude` binary headlessly under the
user's own login, with rolo-claude's own tools exposed to it through a local
MCP bridge. rolo-claude keeps its own tools, permissions, hooks, session log
and telemetry for every `cc:` turn.

- **Aliases (`rolo_claude/providers/cc_models.py`)**: `cc:fable`/`opus`/
  `opus-5`/`opus-5.0`/`opus-4.8`/`opus-4.6`/`sonnet`/`sonnet-5`/`haiku` ->
  `claude-fable-5-1`/`claude-opus-5-5`/`claude-opus-5`/`claude-opus-5`/
  `claude-opus-4-8`/`claude-opus-4-6`/Claude Code's own `sonnet`/
  `claude-sonnet-5`/Claude Code's own `haiku`; `ant:` gets the SAME nine
  names resolved to real API ids; a bare alias with no prefix resolves to
  `cc:` (a subscription login and no `ANTHROPIC_API_KEY`), `ant:` (the key
  set), or an error naming both; any full id and a trailing `[1m]` suffix
  pass through unchanged. Context/output/pricing for all nine come from a
  vendored table (OpenRouter's own `anthropic/*` catalog rows), refreshable
  live via `rolo-claude models --cc --refresh`. `doctor` and the `/model`
  picker ("Claude subscription (via Claude Code)" group) both surface this.
- **Transport (`rolo_claude/agent/cc_process.py`, `rolo_claude/ccbridge/`)**:
  one `claude -p --output-format stream-json --input-format stream-json
  --verbose --include-partial-messages --tools "" --strict-mcp-config
  --mcp-config <inline> --settings '{"disableAllHooks":true}'
  --permission-mode bypassPermissions --session-id/--resume <uuid5 of the
  rolo session id>` subprocess per `cc:` session, lazily started, stdin held
  open across turns -- every flag verified live against the installed
  claude 2.1.281/2.1.284. The `--mcp-config` names one stdio server, "rolo"
  (`python -m rolo_claude.ccbridge`), so Claude Code exposes every bridged
  tool as `mcp__rolo__<Name>`; the child forwards `tools/list`/`tools/call`
  to a `ToolBridgeServer` in the parent process (a Unix socket, mode 0600,
  on POSIX; a TCP loopback socket + a random per-session token on
  Windows) which runs the real dispatch -- permission decide, PreToolUse/
  PostToolUse hooks, the tool's own run, the session log, TUI events --
  for every call, with Claude Code's own tool_use id kept verbatim. Esc
  kills the subprocess (process group, no orphans) and synthesizes
  interrupted results for anything still in flight; the next turn restarts
  with `--resume`. A steer sends its line to the running subprocess
  immediately (Claude Code queues it on its own). Switching models into
  `cc:` mid-session primes the new subprocess with the prior log as one
  `<conversation-so-far>` message; switching away needs nothing special
  (every `cc:` turn already logged ordinary user/assistant/tool_result
  nodes). `stats --models`/`/cost` show `cc:` rows with `total_cost_usd`
  marked as Claude Code's own estimate, never real per-token billing.
- **Tests**: `tests/helpers/fake_claude_cc.py` (a scripted `claude` stand-in
  that also acts as a REAL MCP client against the real `ccbridge` child) plus
  `tests/test_cc_models.py`, `tests/test_ccbridge_server.py`,
  `tests/test_cc_session.py` -- 53 tests covering alias resolution, the
  bridge's own wire protocol (both transports), lazy start/reuse/kill,
  tools/list parity with the frozen catalog, deny rules, a live interactive
  permission card, hooks firing exactly once, pairing invariants across an
  Esc mid-call, the `estimate` usage flag, `--resume` after a restart and
  after a fresh `--continue`-shaped process, steering, a cc:<->or: model
  switch, and the credentials file never being opened.

### H11b fix pass (same 0.4.0 milestone, no version bump)

A 28-finding review of the H11 work above (2 critical, 12 major, 14 minor --
`docs/harness/review-findings-h11.md`) found the `cc:` route's steering
accounting could hang a turn forever, and the `claude` subprocess inherited
this process's whole environment (provider keys, an outer Claude Code
session's own identity) instead of a stripped one. Both are fixed, along
with the rest of the findings:

- **Steering (critical)**: `claude` now runs with `--replay-user-messages`;
  every stdin line is tracked in a FIFO until its own `isReplay` echo
  confirms claude actually consumed it, and a turn ends at a `result` only
  once that FIFO is empty -- correct whether a steer is absorbed into the
  turn already running (one result covers both) or answered as its own
  follow-up turn (queued lines get a combined reply). A steer is logged
  only once consumed, never at send time.
- **Child environment (critical)**: the `claude` subprocess and its MCP
  child now get `tool_child_env()` minus every `ANTHROPIC_*`/`CLAUDE_CODE_*`
  variable and `CLAUDECODE` -- an ambient API key, base URL or an outer
  session's own identity never reaches it. Doctor and `cc:` now refuse to
  report "available" unless `claude auth status`, checked in that same
  stripped environment, reports `authMethod: claude.ai`.
- **The bridge's dispatch is now the loop's own dispatch**: EnterPlanMode/
  ExitPlanMode, AskUserQuestion, and Agent/Task (streamed live, a child's
  own permission ask reaching the same card the parent's calls use) all
  reuse `Session._resolve_tool_call`/`_finalize_tool_result` directly
  instead of a separate, partial re-implementation -- always-allow rules,
  PreToolUse/PermissionRequest/PermissionDenied hooks and the loop breaker
  now all apply to bridged calls too. Images now flow both ways (a pasted
  image reaches claude in the stream-json message; a bridged tool's image
  result reaches claude as a real MCP `ImageContent`, never "[image
  block]"). `--append-system-prompt` now carries a real addendum (skills
  index, subagent_type list, MCP server instructions, deferred-tool names,
  the plan-mode note, and a sub-agent's own body). Background-job/sub-agent
  notices and a `UserPromptSubmit` hook's context are sent to claude, not
  just logged; Stop hooks fire on `result`.
- **MCP catalog growth**: the bridge sends a real
  `notifications/tools/list_changed` (a long-poll on a dedicated
  connection) when ToolSearch grows the session's catalog, so Claude Code
  can discover and call a tool that loaded mid-session.
- **Session identity**: the cc conversation id is logged in a `meta` node;
  `/clear` drops it (a fresh conversation); `/fork` and a `cc:` model
  change close the live process and restart with `--resume`/
  `--fork-session`; a `--resume` claude rejects ("No conversation found"/
  "already in use") falls back to a fresh `--session-id` primed with the
  prior log instead of surfacing the error.
- **Accounting and errors**: `total_cost_usd` (cumulative per claude
  process) is now logged as the per-turn delta, never double-counted. An
  error-shaped result (no stream deltas) now becomes a real error event,
  a non-zero `-p` exit and an `is_error` result; an unconnected bridge MCP
  server gets a warning instead of silently leaving claude with no tools.
- **Lifecycle**: a sub-agent's own claude subprocess/bridge/socket closes
  when its call ends (not just at process quit); an old bridge is closed
  before a restart replaces it; a tool call's result is paired with its
  tool_use even if the bridge's own dispatch raises.
- **Command-line budget (found by this pass's own live acceptance, not in
  the review)**: the `--append-system-prompt` addendum rides on claude's
  own command line, which on Windows goes through `claude.CMD` -> cmd.exe;
  a real dev box with a dozen verbosely-described skills hit "The command
  line is too long" and the subprocess never started. Every discovered
  skill/agent/MCP description is clipped and the whole addendum is capped
  (~3.5K chars) -- a short hint beats a session that cannot start.
- **Tests**: `tests/test_cc_session.py` grew from 21 to 46 (steer absorbed
  between tool calls / two steers queued behind a turn, the stripped child
  env, plan tools, AskUserQuestion, streamed Agent + a child's live ask,
  images both ways, notices/hook context, `list_changed`, the logged cc
  session id, `/clear`, `/fork`, resume fallback, a cc:->cc: model switch,
  per-turn cost delta, error results, Stop hooks, child close, and --
  POSIX only -- pgrep/SIGHUP orphan checks); `test_tui.py` gained a real
  Textual pilot of a `cc:` session's PermissionCard (46 -> 47); each cc:
  test module now sets its own scratch home, and
  `tests/test_bash_background_jobs.py` restores `BRIDGE_TEST_HOME`.
- Corrected this changelog's own "per-connection token" (it's per-session)
  and "`/compact` no-op" (now a real one, not a failure) claims from
  earlier in this same entry.

## [0.3.1] - 2026-09-25

H10: free L0 telemetry from the existing session logs, plus a human-gated
`/improve` (L1 memory/rules + L3 skills). Nothing here lets a model edit its
own instructions silently -- every written artifact is approved on a card
or an explicit headless `--apply`; provenance is information shown on the
card, never a block or a classifier; nothing drafts or writes in `-p`;
nothing interrupts a running turn, auto mode included (status-bar/toast
hint only). Settings tuning, prompt optimisation and code self-edits were
explicitly declined and are not built.

- **Telemetry (`rolo_claude/telemetry.py`)**: `rolo-claude stats [--models]
  [--tools] [--since 7d|30d|all] [--all-projects] [--session ID] [--json]`
  and `/stats --models` in the TUI (run off the UI thread, same worker
  pattern as `/resume`'s session list) aggregate per-(model,provider) and
  per-tool counters -- tokens/cost, avg ttft/latency, `finish=length`%,
  retries/status codes, overflows, tool-call/error rates, repair-hit% by
  kind, edit-failure%, steers/interrupts/compactions/loop-breaker trips --
  entirely from non-wire metadata added to existing `usage`/`assistant`/
  `tool_result` session-log nodes (never a new model-visible field; proved
  with byte-identical derived-request tests across both the OpenAI-dialect
  and native-Anthropic wire bodies). Results cache in
  `~/.rolo-claude/stats-cache.json` keyed by (path, size, mtime); a corrupt
  log line is skipped and counted, never a crash. `doctor` now shows the
  sessions count, cache age and the active `/improve` config.
- **`/improve` (`rolo_claude/improve/`)**: clusters recent failures from the
  telemetry scan (repeated tool errors, repair-layer hits, loop-breaker
  trips, user corrections, Read ENOENT/wrong-cwd, a tool sequence recurring
  across sessions), drafts up to `improve.max_candidates` (8) candidates
  with ONE model call (config `improve.model` -> the small model -> the
  session model), and reviews them one `ImproveCard` at a time (`a` apply,
  `e` edit in `$VISUAL`/`$EDITOR` then apply, `s` skip, `d` dismiss forever,
  `q` stop). A memory candidate writes Claude Code's exact frontmatter
  shape plus a MEMORY.md index line; a rule writes `.claude/rules/*.md`
  (or `~/.claude/rules/`); a skill ships with `disable-model-invocation:
  true`. Only new files are created unless the target already carries the
  `<!-- rolo-claude improve: ... -->` provenance comment (a collision with
  a user-authored file picks a new name instead). Applying appends an
  `improve_applied` log node and refreshes the next turn's CLAUDE.md/
  memory-index snapshot (same mechanism as post-compaction re-injection).
  A session's own counters crossing `improve.hint_threshold` shows a
  one-time, counters-only hint -- never a model call, never a card.
  Headless: `rolo-claude improve [--since] [--all-projects] [--json]
  [--out FILE]` and `rolo-claude improve --apply FILE#ID` (repeatable);
  `-p` never drafts or writes; `--bare` disables it entirely.
- **Config**: `~/.rolo-claude/config.json`'s new `improve` key (`enabled`,
  `hint`, `model`, `since_days`, `max_candidates`, `hint_threshold`),
  settable with dotted paths (`rolo-claude config set improve.model
  or:...`).

## [0.3.0] - 2026-09-25

`rolo-claude` becomes its own standalone, Claude-Code-compatible agent
harness: its own agent loop, built-in tools, permission engine, MCP client
and TUI, reading Claude Code's real config files unchanged and driving
OpenRouter/Databricks-hosted open-weight models (DeepSeek, Kimi, GLM, Qwen,
MiniMax, ...) instead of an Anthropic subscription. Kali Linux is the
primary target platform; Windows is the build/test host. The original
`claude-bridge` proxy (drives the real `claude` binary against the same
providers) is kept unchanged as the `rolo-claude proxy` subcommand.

Built up over milestones H0-H9 (plus U0/U2/U5 for the TUI):

- **H0-H1 -- foundation and provider layer**: package split from the
  `bridge.py` proxy; per-provider compat profiles (OpenAI-chat dialect +
  native Anthropic-Messages passthrough); append-only JSONL session log as
  the single source of truth, with "model-visible means logged" enforced by
  a runtime invariant; reasoning replay per model family
  (`reasoning_content`/`reasoning_details`/thinking blocks); explicit
  `max_tokens` budgeting; the cumulative per-turn loop breaker (remind 3 /
  deny 5 / end turn 8).
- **H2 -- built-in tools, repair, permissions**: Read/Write/Edit/Bash/
  PowerShell/Glob/Grep/WebFetch/TodoWrite/AskUserQuestion and friends; a
  tool-call repair layer for weaker models (fenced/leaked calls promoted to
  real `tool_use`); the full Claude Code permission-rule grammar (deny/ask/
  allow, modes, Bash sub-command matching, path globs) with **no safety
  classifier, destructive-command list or protected-path heuristic
  anywhere** -- `auto` allows everything not matched by the user's own
  deny/ask rules, exactly as decided up front.
- **H3/H3b/H3c -- MCP**: the official MCP SDK, stdio/http/sse transports,
  frozen per-session tool catalog with lazy `ToolSearch` loading (parallel
  and abortable as of H9), `--chrome`/`--playwright` browser tools, plugin-
  provided servers, the `mcp` CLI.
- **H4 -- hooks, skills, commands, web**: every Claude Code hook event and
  handler type (`command`/`prompt`/`agent`/`http`/`mcp_tool`); skills and
  custom slash commands from user/project directories; WebFetch/WebSearch;
  **uninterrupted auto mode with mid-turn steering** (Esc is the only hard
  stop; typing during a turn queues a steer that's applied at the next
  chunk boundary; nothing may say an action "isn't allowed in auto mode").
- **H5/H5b/H5c -- compaction, native Anthropic routes, reliability
  hardening**: auto-compaction (OpenCode-style pruning + an 8-section
  checkpoint summary, floored trigger so small-context models still
  compact sanely, never back-to-back); native `ant:`/Databricks-Claude
  thinking replay with signatures; two full review passes (H4/H5/H3c, then
  H5b) closing 18 and then 19 findings covering steer/abort races, hook
  interruptibility, sub-agent permission surfacing, `SessionStart` env-file
  handling, and stream-json mid-turn steering.
- **H6/U5 -- sub-agents, plan mode, resume, TUI polish**: the `Agent`/
  `Task` tool with a worker pool, background sub-agents, plan mode
  (`EnterPlanMode`/`ExitPlanMode`, a plan file, TUI `PlanCard`), session
  resume/fork/rename, chords + a which-key overlay, `/rewind`, `/export
  --sanitize`, `/stats`.
- **H8 -- background jobs, vision, packaging**: `Bash(run_in_background)` +
  `BashOutput`/`TaskStop`; image/vision support (Read, MCP tool results,
  `@path`, `--file`) with captioned tool cards; `NotebookEdit`; catalog
  vendoring (`models --refresh`, a package-vendored offline fallback);
  offline work-box installs (`tools/vendor_wheels.py`); a full README
  rewrite.
- **H9 -- bug hunt, Linux-first acceptance, MCP compatibility, release**:
  whole-tree review; Linux-first acceptance of every milestone's acceptance
  lines (WSL Ubuntu; the Kali VM when reachable); an MCP compatibility
  matrix (user/project/local/plugin scopes, `claude mcp add` <->
  `rolo-claude mcp add` interop, unusual tool schemas surviving OpenRouter
  and the Databricks 32-tool/no-`$ref` simplifier, `/mcp` reconnect,
  http/sse transports); a randomised fuzz harness (malformed streams,
  unicode edge cases, interrupts/steers at every event boundary, 429/5xx
  storms) asserting log-integrity invariants hold with no hangs or leaked
  threads/processes; a period-2 ("ping-pong") doom-loop detector layered on
  the existing per-call breaker; `doctor` checks for `rg`, `$VISUAL`/
  `$EDITOR` and a usable Bash shell; the `ToolSearch`-triggered lazy MCP
  start made parallel and abortable; a sampling-table audit (temperature/
  top_p/top_k per model family) against the open-weight adapter research,
  plus wiring `sampling_unsupported_params` from `model_table.json` into
  the actual request body instead of leaving it decorative; four dead
  `model_table.json` rows recovered (a JSON key had explanatory prose baked
  into it, so `qwen/qwen3-max`, `qwen/qwen3-max-thinking`, a Mistral batch
  variant and a Databricks Llama row could never resolve their real,
  tuned settings); `type: http` MCP servers connect again (a 3-tuple
  unpack against an SDK that yields 2); `/mcp` reconnect re-reads
  `~/.claude.json`/`.mcp.json` so an edited or brand-new server is picked
  up mid-session (`McpManager.resync_from`); progress notifications can
  actually keep a long MCP call alive (the SDK's own non-resettable
  timeout no longer races the keepalive); a family-agnostic MCP tool-
  schema normaliser (local `$ref` inlining, tuple-items, nullable
  `anyOf`) for every OpenAI-shaped family, not just Kimi/Gemini; the
  Databricks simplifier keeps a typed fallback for a wide `anyOf`;
  `rolo-claude mcp add` writes the same `"env": {}` the real binary does;
  `rolo-claude proxy launch` finds a native POSIX `claude` (it only ever
  looked for the Windows shims -- a crash on Kali, the primary platform);
  a PreToolUse hook written in the PermissionRequest `decision.behavior`
  shape is honoured instead of silently ignored; `--playwright` fails
  fast without a runnable `node`; Kimi `functions.{name}:{idx}` tool ids
  continue across the whole session instead of restarting per call;
  parallel sub-agents with colliding tool_use ids get distinct permission
  waiters; a missing `Path` import in `providers/http.py`.
- **H9b -- whole-tree review fix pass**: closed the review's remaining 24
  findings plus several "NEW from H9" cleanup items, all with real-Session
  or real-CLI pinning tests. Background jobs/sub-agents as one contract: a
  sub-agent's own `JobRegistry` is shared with (and killed by) its parent;
  `-p` now handles SIGHUP the same as SIGTERM (an `atexit kill_all` safety
  net too), verified with `pgrep` on WSL AND the Kali VM that both a
  background Bash job and an MCP stdio server subprocess are gone after a
  normal exit and after SIGHUP; a resume/`TaskOutput` on a task_id whose
  background child is still writing its own log is refused instead of
  racing it, and `AgentRuntime.tasks` is rebuilt from each child's own
  `meta.json` on a `-c` resume instead of coming back "Unknown"; a child's
  usage/cost rolls up into the parent's own CostMeter/log/`--max-budget-
  usd`/`stats`; a child's error/max_turns/blocked outcome is carried onto
  its ToolResult instead of handing back stale text as if it finished
  normally. Secrets: ONE sanitizer (`sk-or-v1-`/`sk-ant-`/`dapi`/`ghp_`,
  quoted `export KEY="…"`, JSON env blocks, Bearer headers) now backs both
  `export --sanitize` and the TUI's `/export --sanitize`; the dead,
  never-wired `session_cli.py` sanitizer is deleted; `CLAUDE_ENV_FILE` is
  sourced against the already-stripped `tool_child_env`, not raw
  `os.environ`. Linux PATH: a Debian/Kali `/etc/profile` login shell no
  longer discards the session's own PATH (foreground and background Bash
  alike), verified with the `unshare -rm` bind-mounted-profile trick on
  WSL. TUI: parallel children stream into their own grouped blocks (keyed
  by agent_id/turn/seq) and never touch the parent's status bar, cost or
  `on_turn_done`; `@server:resource` mention resolution is regex-first,
  runs off the UI thread with a cache and a content cap, so a hung/slow
  MCP server can no longer freeze prompt submission. Also: pre-4.5
  notebooks (no cell ids) accept positional `cell-N` addressing and clear
  stale outputs on a code-cell replace; `turn()`'s own `finally` drains
  queued `@mention`/`!cmd` writes before the next prompt, and manual
  `/compact`/`/clear` count as busy for log-write queuing; a truncated
  Read's continuation hint is computed from the actual last included line,
  on a line boundary; image media type comes from sniffing the real bytes,
  not the file extension, and only the two HARD limits (8000px/5MB) gate
  omission without Pillow -- a plain screenshot under the old 1568px soft
  threshold is no longer omitted for nothing; image offload counts real
  prompts only, never a tool_result's own wire message; a spilled tool
  result's continuation hint is Read-allowed under the session's own
  `tool-results/` dir in every permission mode; Databricks default model
  rows (DeepSeek V4.1 Flash, Kimi K3, GLM 5.3) resolve their real
  1M-token context instead of a generic 128k/16k fallback; offline
  packaging installs cleanly on a bare Python >=3.12 venv (setuptools no
  longer assumed preinstalled) and vendors cp313 wheels too; `bin/rolo-
  claude` resolves a symlink (`readlink -f`) instead of failing outside
  the checkout; a `ucode-settings.json` shaped like a Claude Code
  `--settings` file (an `env` block, or an `apiKeyHelper` command) now
  resolves too, not just the gateway-config key spellings; `/stats`/
  `stats` count real prompts only (never a notice/steer/Stop-continuation)
  and report cache-read/cache-creation tokens; a background-job offset/
  lock race that could duplicate or drop BashOutput text after a timeout
  hand-off is fixed; `doctor` accepts the real minimum Python (3.10, not
  3.9) and `--work` probes the actually-configured model with the
  production header instead of the first DeepSeek/Kimi/GLM endpoint it
  finds. Plus: every temp directory a test creates is tracked and cleaned
  up at the end of a `run_all.py` invocation; `python -X dev -W
  error::ResourceWarning tests/run_all.py` is clean (several unclosed
  HTTP connections and subprocess pipes fixed, without reintroducing the
  Windows orphaned-grandchild hang a naive synchronous `.close()` caused);
  a `ruff check --select F,E9,B` pass across `rolo_claude/`/`tests/`
  (dead imports/locals, unused loop variables, explicit `zip(strict=)`,
  explicit exception chaining); confirmed `wip/` is already excluded from
  the built wheel.

`__version__` is `0.3.0` (`rolo_claude/__init__.py`); see
`docs/harness/ACCEPTANCE-2026-09-25.md` for the full Linux/Windows
acceptance record and `docs/harness/review-findings-*.md` for the detailed
per-finding review history behind the H1-H5c/H9b entries above.

## [0.2.1] - claude-bridge (pre-standalone-harness)

The original `claude-bridge`: a single-file, stdlib-only HTTP proxy
(`bridge.py`) that sits in front of the real `claude` binary and translates
its Anthropic-Messages-API calls to OpenRouter or Databricks, leaving
`claude`'s own subscription, config, memory, MCP servers, skills, hooks and
permissions untouched. Kept unchanged as `rolo-claude proxy` (97 tests).
