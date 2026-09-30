# Changelog

All notable changes to `rolo-claude` are recorded here, newest first. This
project does not (yet) follow strict semver across the 0.3.x line -- each
0.3.0 milestone below was a working checkpoint toward the single 0.3.0
release, not a separate published version.

## [0.8.0] - 2026-09-29

V2b+V2c: matrix-driven fixes tooling and roles. See `docs/harness/V2-brief.md`.

- **`rolo-claude work-matrix show/apply`** (`rolo_claude/work_matrix.py`):
  `show <report.json>` renders a `doctor --work --probe-all` JSON report as a
  table with a suggested action per failing endpoint -- 403 -> run this on
  the VPN; a non-200 row with a `--both`-probed "(anthropic gateway)"
  companion row that itself answered 200 -> set `databricks.gateway.
  <endpoint>: anthropic`; any other non-200 -> unknown, report it; a 200 with
  `tool_call_ok: false` -> change the family's tool-call rule in
  `model_table.json`; a 200 with `reasoning_replay_ok: false` -> disable
  thinking for tool loops on that endpoint. `apply <report.json> [--yes]`
  writes only the ONE class that maps onto a real config.json knob (the
  gateway override), after listing every write and asking for confirmation;
  the other three classes are report-only (no per-endpoint config key exists
  for them). Two synthetic sample reports under `tests/fixtures/work-matrix/`
  (`all-green.json`, `all-failures.json`, one row per failure class).
- **Fix: `providers/http.py::call_anthropic_native`'s 404 fallback no longer
  hardcodes the literal, non-existent endpoint name "anthropic"** (flagged
  during V2a) -- `/serving-endpoints/anthropic/v1/messages` is not a real
  workspace endpoint; the fallback now builds `/serving-endpoints/<the real
  endpoint name, from body["model"]>/invocations`, the SAME by-name
  universal fallback every other family/dialect already uses, with no query
  suffix (a plain invocations call never uses Databricks' `?beta=true`
  AI-gateway flag). `tests/helpers/mock_databricks.py`'s own anthropic-
  gateway dispatch is now body-shape-based (a top-level `system` field,
  never present on an openai-chat body) instead of matching that same wrong
  literal path.
- **Roles** (`rolo_claude/roles.py`, H15): `orchestrator`/`coder`/`reviewer`/
  `researcher`/`small` in `team.json` and `~/.rolo-claude/config.json`'s own
  `roles` key (team.json seeded into config.json once, at `init --preset
  work` time, by the same idempotent idiom `gateway_preference` already
  uses). Three new built-in agents -- `Coder` (full read/write tool set),
  `Reviewer` (read-only code review), `Researcher` (Explore's tools +
  WebSearch) -- alongside `general-purpose`/`Explore`/`Plan`, each with a
  fixed default role; a custom `.claude/agents/*.md` agent sets the same
  thing with a `role:` frontmatter key. Resolution precedence: an explicit
  `model=` always wins; then a `--role NAME=MODEL` CLI override for that
  agent's own role (repeatable; wins even over the agent's own file
  `model:` -- a deliberate, freshly-typed, run-only override); then the
  agent file's own `model:`; then the role table; then `CLAUDE_CODE_
  SUBAGENT_MODEL`/`settings.subagentModel`; then the parent/session model
  (`orchestrator`'s own documented default). The `Agent`/`Task` tool itself
  accepts a `role` argument overriding an agent's default role for just one
  call. Cost-aware defaults (DeepSeek V4.1 Flash for `researcher`/`small`)
  apply ONLY when the role table is completely empty AND the session's own
  model is a Databricks one -- documented in `docs/ROLES.md`, never
  automatic beyond that one case. `/roles` shows the resolved table (model,
  endpoint/path type, price) per role; `stats --roles` sums sub-agent spend
  per role from each Agent-tool call's own rolled-up usage node (tagged
  `role=`, additive -- a pre-V2c log simply has none).
- **Docs**: new `docs/ROLES.md`; `docs/DATABRICKS.md`'s own work-matrix
  section; `docs/COMMANDS.md`'s `work-matrix`/`--role`/`stats --roles`
  sections; `docs/SLASH-COMMANDS.md`'s `/roles` section.

## [0.7.0] - 2026-09-29

V2a: per-family Databricks correctness across every gateway type the harness
routes to (`anthropic/v1/messages`, `mlflow/v1/chat/completions`,
`cursor/v1/chat/completions`, `/serving-endpoints/<name>/invocations`), a
fixture test matrix driven by an extended `tests/helpers/mock_databricks.py`
speaking every dialect, and the two work-matrix open-question probes. See
`docs/harness/V2-brief.md`.

- **`tests/helpers/mock_databricks.py` speaks every dialect now**: the
  native `anthropic/v1/messages` gateway path is scenario-dispatched (it was
  hardcoded to one fixed "pong" reply) -- thinking blocks + signatures,
  tool_use (including Kimi's own verbatim `functions.<name>:<idx>` id), and
  the dialect's own 401/403-IP/404/overflow-400/429/5xx shapes; the
  openai-chat side gained 401/403-IP/404/500/context-overflow-400/413/
  finish_reason=length-minimal-output/Kimi-native-tool-id/cached-and-
  reasoning-token-usage/two-turn-reasoning-replay scenarios, plus path-aware
  404-then-fallback scenarios (mlflow-then-cursor, cursor-then-mlflow).
- **Per-family/per-type pinning tests** (`test_v2a_<family>_<type>_...`,
  five new files under `tests/`): Claude foundation/GLM/Kimi
  thinking+signature+cache_control+coding-agent-mode-header on the
  anthropic gateway (tested with and without thinking); DeepSeek/GPT/Grok/
  Gemini/gpt-oss reasoning decode+replay rules on mlflow; GLM's fixed
  sampling pair vs. Kimi/DeepSeek's server-fixed omission; the catalog's own
  `foundation_model.name` on the wire, `stream: true` explicit, the 32-tool
  cap + schema simplifier, and OTPM pre-admission against Kimi's real
  published rate limits; `gpt-5-5-pro`'s cursor-only route and the GPT
  family's mlflow-then-cursor fallback; Bedrock EXTERNAL Claude's
  invocations-only, model-less chat body with tool support; every error
  shape (400 unknown-field, 401, 403-IP, 404-to-exhaustion, 413,
  context-overflow-400, 429 with limit_type/retry_after, 5xx) and usage/cost
  accounting (cached + reasoning tokens, Databricks cost always "n/a", the
  catalog's DBU-rate-to-dollars conversion) per type.
- **Fix: a literal HTTP 413 is now non-retryable and classified
  `CONTEXT_WINDOW_EXCEEDED`** (`providers/errors.py`) -- `map_upstream_error`
  had no row for 413 and fell through to its own `should_retry=True`
  default, which won the `e.retryable or is_retryable_message(...)`
  short-circuit in `agent/loop.py` before `is_context_overflow_message`'s
  own (already-correct) unconditional 413-is-overflow rule ever got a
  chance to run -- a real 413 was silently retried up to `MAX_RETRIES`
  times against the identical, still-too-large body instead of surfacing as
  an overflow.
- **The two work-matrix open questions are now runnable probes**
  (`rolo_claude/work_matrix.py`, `doctor --work --probe-all --tools`): (1)
  reasoning replay after a tool call -- a real second turn, built through
  the exact same `providers.request` builders a live session uses (a
  genuinely signed `thinking` block on the anthropic dialect; the family's
  own `reasoning_echo` rule on the openai-chat dialect), is sent and its
  acceptance recorded per endpoint as `reasoning_replay_ok`; (2) route split
  from the cache -- `cached_path_type` (what `routes-cache.json` said
  before this run) alongside `path_type` (what this run actually used/
  re-cached) makes a stale-cache mismatch visible in the JSON report
  without a second run. Both fields are `None` when not applicable (no
  `--tools`, or nothing was cached yet) rather than a misleading default.
- `docs/DATABRICKS.md`/`docs/MODELS.md` updated: the work matrix's two new
  report fields, and a wording correction -- `databricks.dbu_price_usd`
  converts the catalog's own advertised DBU rate for the `/model` picker's
  informational display only; Databricks never reports a per-turn spend, so
  `/cost`/`stats` stay "n/a" regardless of whether that price is configured.
- `__version__` -> 0.7.0.

## [0.6.0] - 2026-09-29

H14: Databricks work-config parity -- zero-setup at work from Claude Code's
own settings, a generic family x api_type routing table (no vendored
endpoint list), doctor accuracy, team onboarding, and the work matrix. See
`docs/harness/H14-brief.md`.

- **Host/gateway split (scope A)**: `resolve_databricks()` now always
  returns the bare workspace root as `.host` -- a gateway path riding along
  in `ANTHROPIC_BASE_URL`/`DATABRICKS_HOST` (`.../ai-gateway/anthropic`) is
  split off into `.anthropic_gateway` instead of leaking into every derived
  URL. Fixes `doctor --work` building `/api/2.0/serving-endpoints` under the
  gateway path.
- **Custom headers (scope B)**: `ANTHROPIC_CUSTOM_HEADERS` (one or more
  "Name: value" lines, Claude Code's own settings.json convention) is parsed
  onto `DbxConfig.custom_headers` and merged onto every Databricks request
  (native passthrough and chat), under the default
  `x-databricks-use-coding-agent-mode: true` and any explicit override.
- **Model defaults/aliases at work (scope C)**: when Claude Code's own
  settings env resolves a Databricks config, the default model is
  `dbx:<ANTHROPIC_MODEL>` and bare `opus`/`sonnet`/`haiku` resolve through
  `ANTHROPIC_DEFAULT_*_MODEL` (falling back to pinned `databricks-claude-*`
  endpoints when unset) instead of the `cc:`/`ant:` subscription route or the
  OpenRouter default; `effortLevel`/`modelSettings.<id>.effortLevel` now
  actually set the session's default effort (previously unwired).
- **Generic family x api_type RULES table (scope D,
  `providers/dbx_routing.py`)**: family detected from the endpoint name (and,
  when cached, `foundation_model.name`/`model_class`) drives an ordered,
  endpoint-specific route built from the endpoint's OWN discovered
  `api_types` -- never a hand-maintained per-model list. Claude foundation
  defaults to the native anthropic gateway; GLM/Kimi default to mlflow chat
  with the anthropic gateway selectable per model (`databricks.gateway.
  <endpoint>` config or a `dbx:<endpoint>@anthropic` suffix); DeepSeek/Qwen/
  Llama/Gemma/gpt-oss default to mlflow chat, using the endpoint's real
  `foundation_model.name` as the wire model id (not a `system.ai.` prefix
  guess); GPT/Grok/Gemini default to mlflow chat, with `databricks-gpt-5-5-
  pro`'s own exception (no mlflow chat, cursor chat instead); Bedrock
  EXTERNAL Claude endpoints (`us-anthropic-claude-*`) are invocations-only
  and no longer misclassified as native Claude passthrough just because
  "claude" is in the name; a known non-chat endpoint (embeddings/whisper) is
  refused with a clear message and hidden from pickers. An unknown endpoint
  (not yet in the discovery cache) falls back to the pre-H14 static order.
- **doctor --work accuracy (scope E)**: prints the derived workspace root,
  gateway path, header NAMES (never values), default model, default effort,
  and which resolution step supplied the token; token-validity now
  distinguishes 401 (bad token), 403 with Databricks' own IP-access-list
  wording ("connect to the VPN"), 403 without it (token lacks list
  permission, inference may still work), and a wrong-path 404 -- previously
  every non-200 read as one generic "may only run inference" line. A host
  configured with no token yet still probes (a 401 without a token proves
  reachability) instead of skipping the network call.
- **Work matrix (scope H, `rolo-claude doctor --work --probe-all [--both]
  [--tools] [--only <glob>]`)**: one short pong per chat-shaped endpoint on
  its chosen path (plus the anthropic gateway too for Claude/GLM/Kimi with
  `--both`), a table of status/latency/output tokens/tool-call support, and
  a JSON report at `~/.rolo-claude/work-matrix-<date>.json` with endpoint
  names only -- no host, no token. Mock-verified (the real workspace is
  behind an IP access list from this box).
- **Team onboarding (scope I)**: a shared `team.json` (host, default model,
  per-family gateway preference, DBU price -- never a token, never an
  endpoint list) discovered at `.rolo-claude/team.json` (project),
  `~/.rolo-claude/team.json`, or `--team <path|url>`; `init --preset work`
  reads it (or Claude Code's own settings env) and asks ONLY for the token
  when a host is already known, hidden input, 0600 env file. `/model`
  groups Databricks endpoints by family, shows the chosen path type, and
  hides non-chat endpoints. `team.example.json` added.
- **`/models refresh` (alias `/dbx`) (scope J)**: re-lists the workspace
  catalog off the UI thread, updates `~/.rolo-claude/dbx-endpoints.json`, and
  reports a one-line added/removed/changed diff; `rolo-claude models
  --refresh --urls [--json]` prints the exact URL and path type per
  endpoint. Auto-refresh (`databricks.catalog_max_age_hours`, default 24h)
  on `/model` open and (silently, for an already-cached catalog only) on
  session start; an offline/403 refresh keeps the existing cache.
- `__version__` -> 0.6.0.

## [0.5.0] - 2026-09-29

H13 (RECOMMENDATIONS.md P1): lazy MCP start by default, inline images in the
terminal, `/resume` search, and the first live family-baseline pass for
GLM-5.3/Qwen/MiniMax/Kimi K2.7-code/Kimi K3. See `docs/harness/H13-brief.md`.

- **Lazy MCP by default**: every configured server is now lazy unless it's
  `alwaysLoad` or explicitly `"mcpLazy": false` (per-server, or globally via
  a new top-level `"mcpLazy": false` in `settings.json`) -- reversing the
  pre-0.5.0 "eager unless `mcpLazy: true`" default. A per-server tool cache
  under `~/.rolo-claude/mcp/tools-cache/<server>.json` (keyed by a hash of
  the server's own command/args/env/url/headers) means a lazy server's tool
  names/descriptions are still known -- and findable/preloadable -- before
  it's ever connected: no cache yet (first run, or the config changed)
  connects it once to learn its tools and writes the cache; a valid cache
  seeds a new `"cached"` handle state with zero connections. A tool call, or
  `/mcp`'s own reconnect, connects a cached/lazy server for real on demand;
  a live tool list that turns out to differ from what the cache promised
  refreshes the catalog and surfaces a short note on that same call. Status
  bar/`/mcp`/`mcp list`/`doctor` all show the new `"cached"` state (`doctor`
  additionally reports each lazy server's cache age). Every uncached server
  needing a real connect (a first run against N configured servers) does so
  CONCURRENTLY (`McpManager.start_many`), not one at a time -- a real bug
  from live dogfooding this milestone's own acceptance line surfaced before
  any fix landed (up to N times MCP_TIMEOUT, worse than the pre-0.5.0 eager
  path, which was already concurrent).
- **Inline images in the terminal**: a screenshot/image tool result renders
  inline via the kitty graphics protocol (kitty, WezTerm, Ghostty, foot) or
  sixel (other terminals, detected live), with the existing type/size/
  dimensions caption as the fallback everywhere else (no real tty, tmux
  without `allow-passthrough on`, detection finds nothing, or `images:
  "caption"`/`"off"` in `~/.rolo-claude/config.json` / `--no-inline-images`).
  `textual-image` (PyPI) was evaluated per the brief and rejected on both
  grounds it named: its own repo declares LGPL-3.0 (not permissive) and it
  requires Python >=3.12 (this project supports >=3.10) -- the kitty/sixel
  encoders are implemented directly instead (`rolo_claude/tui/images.py`);
  Pillow (the existing `vision` extra, now also `pip install rolo-claude
  [images]`) enables the sixel encoder and bounded downscaling for both
  protocols, but nothing here requires it (kitty decodes PNG/JPEG itself).
- **`/resume` search**: the session picker (TUI `/resume`, and an ambiguous/
  no-match `--resume <text>` at TUI startup -- previously silently ignored
  outside print mode, now genuinely wired through `build_session`) gets a
  live text filter, fuzzy-ranked over title/first prompt/cwd/model;
  `--resume <text>` in print mode picks the unique match or lists the
  ambiguous candidates instead of silently guessing the most recent one.
- **Family baselines**: `docs/harness/FAMILY-BASELINE-2026-09-29.md` --
  live acceptance rows for GLM-5.3, Qwen3.8 Flash, MiniMax M3, Kimi
  K2.7-code and Kimi K3 on OpenRouter (pong, a 200-line Read, a Write ->
  Edit -> Bash chain, one steer; Kimi K3 kept to the cheap pong+Read subset
  per the brief).

## [0.4.1] - 2026-09-28

H12 (RECOMMENDATIONS.md P0): the whole first run in one command, a
prescriptive `doctor`, and the cheapest fix for DeepSeek V4.1 Flash's own
telemetry-observed 8% Edit "Found multiple matches" rate. See
`docs/harness/H12-brief.md`.

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
`ANTHROPIC_API_KEY`. See `docs/harness/H11-brief.md` (and the H11b fix-pass
review below, `docs/harness/review-findings-h11.md`). Binding constraint: rolo-claude never reads, copies or
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
`/improve` (L1 memory/rules + L3 skills). See `docs/harness/H10-brief.md`. Nothing here lets a model edit its
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

Built up over milestones H0-H9 (plus U0/U2/U5 for the TUI) -- see
`docs/harness/README.md` for the full per-milestone brief/review index:

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
