# Codex CLI research (round 5i part 2, 2026-10-05)

Build host has `codex-cli 0.155.1` installed via `npm install -g @openai/codex`
(the real binary is `@openai/codex-win32-x64`'s `codex.exe`, launched through
a Node shim; Windows also ships `codex.cmd`/`codex.ps1`/`codex` (sh) wrappers
next to it). Nobody is logged in on this box (`codex login status` ->
"Not logged in", exit 1) and no login was attempted. Everything below is
either (a) read from `codex --help`/`codex exec --help`/`codex <sub> --help`
output on this exact installed version, (b) read from static strings inside
the installed `codex.exe` (safe, no execution), or (c) fetched from
docs.openai.com/github docs on 2026-10-05 -- each marked. Nothing below was
produced by actually running `codex exec`.

## 1. Detection and login (CONFIRMED, live on this box + docs)

- Windows wrapper resolution mirrors `cc:`'s `find_claude_exe`: `shutil.which`
  on `codex`, `codex.exe`, `codex.cmd` only (default Windows `PATHEXT`
  resolves a bare `codex` to `codex.cmd` directly, which `subprocess.Popen`
  can launch same as `claude.cmd` already does). `codex.ps1` is deliberately
  never probed, same omission the existing `cc:` resolver already makes for
  `claude.ps1` -- a PowerShell script isn't directly spawnable via
  `CreateProcess` without invoking `powershell.exe` itself.
- `codex login status` prints PLAIN TEXT, never JSON (no `--json` flag on
  that subcommand, confirmed from `--help`) -- exit 0 when logged in, exit 1
  when not ("Not logged in", confirmed live). The exact strings for each
  login kind are baked into `codex.exe` (confirmed by reading the binary's
  string table, never executed): `"Logged in using ChatGPT"`, `"Logged in
  using an API key - "`, `"Logged in using access token"`, `"Logged in using
  Amazon Bedrock API key"`, `"Logged in using Amazon Bedrock AWS access
  keys"`, `"Logged in using personal access token"`, `"Logged in using
  workload identity"`. Only the first is a ChatGPT subscription login --
  `codex_models.CODEX_SUBSCRIPTION_MARKER` checks for it by substring,
  mirroring `cc_models.SUBSCRIPTION_AUTH_METHODS`'s `"claude.ai"` check.
  Never parsed as JSON; never reads `~/.codex/auth.json`.
- Auth docs (learn.chatgpt.com/docs/auth, fetched 2026-10-05): ChatGPT login
  via `codex login` (browser flow), cached at `~/.codex/auth.json` ("treat
  like a password"); API-key login via `OPENAI_API_KEY` +
  `codex login --with-api-key`. `CODEX_HOME` (default `~/.codex`) redirects
  the whole home dir. The non-interactive doc separately shows
  `CODEX_API_KEY=<key> codex exec ...` as an automation shortcut -- UNCONFIRMED
  whether this is a distinct env var from `OPENAI_API_KEY` or an alias; `cx:`
  never uses either (subscription only, per the brief).

## 2. Model ids for a ChatGPT login (CONFIRMED from docs, UNCONFIRMED live)

learn.chatgpt.com/docs/models (2026-10-05): ChatGPT Plus/Pro/Business logins
get `gpt-6-astra` ("most capable"), `gpt-6.1-sol` ("near-Astra, lower cost"),
`gpt-6-luna` ("most efficient"). `gpt-5.5` is API-key only (retired from
ChatGPT products 2026-10-14). All four already have real vendored rows in
`halo_harness/providers/catalog/models_dev_openai_fallback.json` (context
1,050,000 / output 128,000, real API pricing) from round 5i part 1's
`oai:` work -- `codex_models.py` reuses that same file for context/output/
vision instead of inventing numbers, but always reports `price_in`/
`price_out` as `None` for `cx:` (a ChatGPT subscription has no metered
per-token price). `codex_models.CODEX_ALIASES` adds short names `astra`/
`sol`/`luna`. Reasoning effort display names (Light/Medium/High/Extra
High/Max/Ultra) map onto `config.toml`'s own values low/medium/high/xhigh/
max/ultra (confirmed key values; the display-name mapping is approximate).
No `--refresh`/`/models refresh` probe has run live (no login) -- the three
ids are shipped as the seed table, `refresh_cx_catalog` (mirrors
`refresh_cc_catalog`) is implemented but UNEXERCISED on this box.

## 3. `codex exec` flags (CONFIRMED from `--help` on this version)

No `-a/--ask-for-approval` flag on `codex exec` at all (confirmed: passing
`-a` to `exec` errors "unexpected argument '-a' found" -- that flag only
exists on the top-level interactive `codex` command). Approval policy for
`exec` is set ONLY via `-c approval_policy=<value>` override. `exec`'s own
flags used by `cx:`: `--json` (JSONL to stdout), `-m/--model`, `-s/--sandbox
<read-only|workspace-write|danger-full-access>`, `-c key=value` (repeatable,
TOML-parsed value, dotted path for nested keys), `--skip-git-repo-check`
(Halo always passes this -- a session cwd need not be a git repo),
`--ephemeral` (no session persistence -- used only for the stateless
one-shot small-model call, mirroring `--no-session-persistence` on `cc:`),
`-i/--image <FILE>` (repeatable, needs real file paths -- Halo writes a
turn's image blocks to a temp dir and cleans up after), `-o/--output-last-
message <FILE>` (not used; Halo reads the event stream instead), `--approve-
for-me` (auto-reviews approval requests under workspace-write -- NOT used by
default; see risk note below), `--dangerously-bypass-approvals-and-sandbox`
(NOT used -- `-c approval_policy=never -s danger-full-access` is the
documented equivalent the brief asks for).

`-a`/sandbox values confirmed (binary string table): `approval_policy` enum
has FOUR variants -- `untrusted`, `on-failure`, `on-request`, `never` (the
dev-portal config-reference page only documents two of these plus a newer
`granular` object; `untrusted`/`on-failure` are still compiled in and are
almost certainly the pre-"v2" enum the brief's "manual: untrusted" mapping
refers to). `sandbox_mode`: `read-only`, `workspace-write`,
`danger-full-access` (confirmed both places).

Halo's permission-mode mapping (brief: "bypass and auto: approvals never,
sandbox full access; default: on-request; manual: untrusted" -- sandbox
values for default/manual are Halo's own choice, UNCONFIRMED live):
- bypass / auto -> `-c approval_policy=never -s danger-full-access`
- default -> `-c approval_policy=on-request -s workspace-write`
- manual -> `-c approval_policy=untrusted -s read-only`

**Risk, UNCONFIRMED (no login to verify live):** `codex exec` is
non-interactive -- there is no human to answer an approval request if one is
raised under `on-request`/`untrusted` and neither `--approve-for-me` nor
`--dangerously-bypass-approvals-and-sandbox` is set. The non-interactive doc
never shows this case. It may hang waiting for an approval that can never
arrive, or Codex may auto-deny and continue. Halo ships the brief's literal
mapping above; if a live run ever hangs on `default`/`manual`, the fix is to
add `--approve-for-me` to the `default` mapping (documented here so the next
worker doesn't have to re-derive it).

## 4. MCP servers (CONFIRMED shape, UNCONFIRMED `-c` override live)

learn.chatgpt.com/docs/extend/mcp (2026-10-05): stdio server fields are
`command` (required), `args`, `env`, `env_vars`, `cwd`, `startup_timeout_
sec` (default 10s), `tool_timeout_sec` (default 60s), `enabled`, `required`.
HTTP server fields are `url`, `bearer_token_env_var`, `auth`, `http_headers`,
`http_headers_helper`. `codex mcp add <name> -- <command> <args...>`
PERSISTS a server into `config.toml` (never used by Halo -- "Halo never
writes to Codex's files"). The brief's own inline form, `-c mcp_servers.
<name>.command=...`, is Halo's chosen mechanism instead: a transient,
per-invocation `-c` override (confirmed general `-c` dotted-path semantics
from `--help`: "`-c shell_environment_policy.inherit=all`" is the documented
example for a nested key, so `-c mcp_servers.halo.env.PYTHONPATH=...` is the
same mechanism one level deeper) -- NOT live-verified that Codex actually
starts an MCP server from a `-c` override the same way `codex mcp add`
would from `config.toml` (both read the same `mcp_servers` table shape per
the config reference, so this is treated as very likely true, not certain).
`halo_harness/agent/codex_process.py`'s `build_mcp_override_args` builds one
`-c mcp_servers.halo.<field>=<value>` flag per field (`command`, each `args`
entry as a TOML array literal, one `env.<KEY>` per bridge env var) pointing
at the SAME `python -m halo_harness.ccbridge` child + `ToolBridgeServer`
`cc:` already uses (`halo_harness/ccbridge/`) -- that module is a plain
MCP-SDK stdio server named "rolo" (renamed conceptually to "halo" here only
via the `mcp_servers.halo` config key; the child process itself is unchanged
and still identifies as server name "rolo" to whichever MCP client starts
it). No evidence Codex requires a specific tool-name-prefix convention the
way Claude Code's transcript shows `mcp__<server>__<tool>` -- the actual
wire `tools/call` always carries the tool's bare name regardless of client;
only DISPLAY events (`item.*` of type `mcp_tool_call`) might show a
server+tool pair, and the exact field names for that are UNCONFIRMED (see
section 6).

## 5. `.codex/config.toml` project scoping (CONFIRMED)

Same MCP doc: `.codex/config.toml` at a project root scopes MCP servers (and
presumably other config) to that project for a TRUSTED project, layered
over `~/.codex/config.toml`. This is Codex's own answer to "any `.codex/`
project folder" -- `codex_settings.py` reads it the same way as the home
config (best-effort merge, home first then `.codex/config.toml` layered on
top) but Halo never writes to either.

## 6. `codex exec --json` event protocol (PARTIALLY CONFIRMED)

learn.chatgpt.com/docs/non-interactive-mode (2026-10-05) names the event
types: `thread.started`, `turn.started`, `turn.completed`, `turn.failed`,
`item.*`, `error`. One (garbled in the fetched page -- two examples ran
together) sample line showed `turn.completed` carrying
`"usage":{"input_tokens":24763,"cached_input_tokens":24448,
"output_tokens":122}` -- no cost/dollar field anywhere in the shown
example; `cx:` turns record token usage on Halo's cost meter but leave
`cost_usd=None` (no invented number) since a ChatGPT subscription isn't
metered per token. Reading `codex.exe`'s own string table (never executed)
additionally confirms the finer-grained event vocabulary the doc prose
doesn't spell out: `item.started` / `item.updated` / `item.completed`
(three-phase, not two -- `item.updated` almost certainly carries partial
text/thinking as an item grows, UNCONFIRMED exact field name) and `item`
TYPE values `agent_message`, `reasoning`, `command_execution`,
`file_change`, `mcp_tool_call`, `web_search_call`, `todo_list`, plus the
approval item types `exec_approval_request`/`apply_patch_approval_request`
(Rust names `ExecApprovalRequest`/`PatchApprovalRequest`). EXACT per-item
JSON field names beyond `id`/`type`/`text` (for `agent_message`/`reasoning`)
are UNCONFIRMED -- no full example line was ever fetched or run. Halo's
decoder (`agent/codex_turn.py::_events_for_cx_line`) is written
defensively: it tries several plausible field names per item type, never
raises on an unrecognized shape, and falls back to a plain notification
line for anything it can't place, exactly like the rest of this codebase's
decoders degrade.

No evidence of a per-turn dollar-cost JSON field (`cost_microusd`-shaped
strings in the binary are OTEL/telemetry metric NAMES like
`codex_turn_cost_microusd`, not a field in the exec JSON protocol itself --
do not confuse the two).

## 7. Steering / mid-turn input (CONFIRMED architecture, fallback shipped)

`codex exec [PROMPT]` is ONE bounded subprocess invocation per turn, NOT a
long-held process reading a stream of stdin lines the way `claude -p
--input-format stream-json` is (confirmed: no stdin-streaming flag exists on
`exec`; `[PROMPT]` is a single CLI argument or a single stdin blob). Multi-
turn continuation is `codex exec resume <SESSION_ID|--last> [PROMPT]` --
each turn is its OWN process, resuming the previous one by id (confirmed via
`codex exec resume --help`; the id is a UUID Codex itself assigns and
reports back, read from the first `thread.started` event, mirrored from how
`cc:` reads back `session_id` on every stream-json line). This means `cx:`
has NO live channel into an already-running turn's subprocess at all.

A separate `codex queue --thread <UUID> --message <TEXT>` command exists
("Queue a message for an existing session") and `codex agents` describes "a
shared local app-server daemon" tracking sessions -- these two facts
together suggest `codex queue` MIGHT be able to inject into a still-running
`exec` turn from a second process. This is UNCONFIRMED (no login to test,
and the task description forbids running `codex exec` at all) and risky to
rely on blind (possible double-delivery or an error on a thread the daemon
doesn't recognize yet). `cx:` therefore ships ONLY the documented fallback:
`steer_cx` queues the text and returns True immediately with a notification
explaining the fallback; the moment the current turn's subprocess exits,
`codex_turn.turn_body_cx` drains the queue and sends it as its own
`codex exec resume <id> <text>` call (logged as a `steer` node), looping
until the queue is empty, before yielding this Halo turn's single
`turn_done`. A future round with a live ChatGPT login should try wiring
`codex queue` for true mid-turn delivery and update this section.

## 8. `config.toml` keys and `AGENTS.md` discovery (CONFIRMED from docs)

learn.chatgpt.com/docs/config-file/config-reference (2026-10-05): `model`,
`model_provider` (default `"openai"`), `model_reasoning_effort` (low/medium/
high/xhigh/max/ultra), `approval_policy`, `sandbox_mode` (section 3),
`mcp_servers.<id>` (section 4), `projects.<path>.trust_level` (`trusted`/
`untrusted`), `project_root_markers`, `shell_environment_policy.{inherit,
filters,set,exclude_slash_tmp}`, `allow_login_shell`, `history.persistence`
(`save-all`/`none`), `notify` (array of strings, a command invoked with a
JSON payload). `CODEX_HOME` defaults to `~/.codex`. `profiles.<name>` is
documented there as an inline config.toml table, but the INSTALLED CLI's own
`-p/--profile` flag help describes a DIFFERENT mechanism ("Layer
$CODEX_HOME/<name>.config.toml on top of the base user config",
"CONFIG_PROFILE_V2") -- a separate per-profile FILE, not a table inside the
one file. Both may coexist (old table-based profiles plus a newer per-file
"v2" scheme); `codex_settings.py` reads the inline `profiles.<name>` table
when present but does NOT guess which profile (if any) is "active" by
default -- no persisted "default profile" pointer was found in either
scheme, so Halo's settings view surfaces the BASE config.toml only and
notes profiles are selected per-invocation via `-p`.

learn.chatgpt.com/docs/agent-configuration/agents-md (2026-10-05) --
AGENTS.md discovery, CONFIRMED and notably DIFFERENT from how Halo already
walks for CLAUDE.md/AGENTS.md (`halo_harness/config/claude_md.py`, a
filesystem-root-to-cwd walk with no git-root concept): Codex's own rule is
(1) global: `~/.codex/AGENTS.override.md` else `~/.codex/AGENTS.md` (first
non-empty wins, `CODEX_HOME`-relative); (2) project: from the GIT REPO ROOT
down to cwd, each directory checked for `AGENTS.override.md` then
`AGENTS.md` then `project_doc_fallback_filenames`, override always beating
the plain name at the same level; (3) merge: concatenated root-to-leaf,
blank-line joined, closer-to-cwd text appearing LATER (so it reads as the
`override`); (4) cap: `project_doc_max_bytes`, default 32 KiB, combined;
empty files skipped entirely. `codex_settings.load_codex_agents_md_chain`
implements exactly this (its own git-root detection, never reusing
`claude_md.py`'s filesystem-root walk, since the two rules genuinely
differ) and is kept completely separate from Halo's EXISTING CLAUDE.md/
AGENTS.md loader, which is unchanged and still only implements Claude
Code's own walk for real `cc:`-adjacent behaviour.

## 9. Design decision: Codex's native tools stay native

Unlike `cc:` (`--tools ""` fully disables Claude Code's own built-ins, every
tool call is Halo's bridge), no flag was found that disables Codex's own
built-in shell/apply_patch tools for `exec` mode. `cx:` does not attempt to
turn them off: Codex keeps using its own native tools (sandboxed per
section 3's mapping), and Halo's own tool catalog is ADDITIONALLY exposed
via the `mcp_servers.halo` override so the model can also reach
`mcp__halo__<Name>`-style tools when useful. Codex's native tool calls
(`command_execution`/`file_change`/...) are logged into Halo's transcript
read-only, after the fact, from the `item.completed` event (the action has
already happened inside Codex's own sandbox by the time Halo sees it) --
they are never dispatched through Halo's permission engine, only displayed.
Only `mcp_tool_call` items that round-trip through `mcp_servers.halo` go
through `agent/codex_runtime.py`'s own bridge dispatch (permission decide,
hooks, the same `_build_tool_context`/log/finalize path `cc:` uses).
