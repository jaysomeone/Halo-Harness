# Configuration

Exactly which files this harness reads (Claude Code's own config, unchanged)
and which it owns (its own small state under `~/.halo/`), how they're
merged, every environment variable, and precisely what is never written.
Permissions/hooks *behavior* (as opposed to where their config lives) is
covered in [ARCHITECTURE.md](ARCHITECTURE.md); Databricks credential
discovery specifically is covered in [DATABRICKS.md](DATABRICKS.md).

## Claude Code files -- read only, never written (except where noted)

### `settings.json` precedence

Five layers, later wins, merged by `config/settings.py::resolve_settings`:

| Layer | Path | Notes |
|---|---|---|
| `policySettings` (managed) | `C:\Program Files\ClaudeCode\managed-settings.json` (Windows) / `/etc/claude-code/managed-settings.json` / `/Library/Application Support/ClaudeCode/managed-settings.json`, plus every `*.json` under a sibling `managed-settings.d/` | highest precedence, always read, never trust-gated |
| `flagSettings` | `--settings` (inline JSON or a file path) | second-highest |
| `localSettings` | `<cwd>/.claude/settings.local.json` | |
| `projectSettings` | `<cwd>/.claude/settings.json` | |
| `userSettings` | `~/.claude/settings.json` (`$CLAUDE_CONFIG_DIR` if set) | lowest precedence |

`--setting-sources` restricts which of `user`/`project`/`local` are read at
all (`policySettings`/`flagSettings` are always included). Merge rules,
applied key by key across the whole stack: most scalar keys are plain
highest-layer-wins; a **list**-valued key is concatenated across every layer
that set it and de-duplicated (order-preserving) -- *unless* the
highest-precedence layer that touched it set something else entirely
(a scalar), in which case that wins outright, never merged; `env` and
`hooks` are always accumulated (a lower layer's own hook/env entry is never
discarded by a higher layer setting more); `permissions.allow/ask/deny/
additionalDirectories` follow the same list-concatenation rule, scoped to
that one sub-object. `fallbackModel`/`modelPicker`/`availableModels`/
`modelSettings` are whole-value, highest-layer-wins, never merged field by
field.

**Trust**: an *untrusted* `project`/`local` layer has `permissions.allow`,
`permissions.additionalDirectories`, `env`, `hooks`, and
`autoMemoryDirectory` stripped before merging -- `permissions.deny`/`.ask`
always survive regardless of trust (a repo you haven't trusted yet can still
have its own denials honored, just never its own extra permissions/secrets/
hooks). Trust itself (`config/claude_json.py::is_trusted`) is true when any
of: `projects[cwd].hasTrustDialogAccepted` for `cwd` or an ancestor up to
the git root (or the filesystem root if not in a repo); `CLAUDE_CODE_SANDBOXED`
is set to any non-empty value; or `~/.halo/trust.json` has an entry
for the normalized cwd (this harness's own trust store -- written by a
future trust-prompt flow, not yet interactive).

### Permissions and hooks

The rule grammar, mode table, and hook protocol are architecture, not
config -- see `docs/ARCHITECTURE.md`'s "Permissions" and "Hooks protocol"
sections. Configuration-relevant summary: `permissions.allow`/`.ask`/`.deny`
live inside `settings.json` at every layer above; hooks live in that same
file's top-level `hooks` key at every layer (concatenated, never
overridden -- every matching hook from every layer runs); a plugin's own
`hooks/hooks.json` is a separate, additional source, with
`${CLAUDE_PLUGIN_ROOT}` substituted first. There is no separate freestanding
`hooks.json` for an ordinary user/project outside a plugin.

### CLAUDE.md / AGENTS.md / rules / memory

Discovery order (`config/claude_md.py::discover_instructions`; `bare`/
`--bare` skips this whole section): managed `CLAUDE.md` (never excluded,
always loaded) -> `~/.claude/CLAUDE.md` + `~/.claude/rules/**/*.md` -> a
root-to-leaf walk of every ancestor directory from the filesystem root down
to `cwd` (root itself excluded), at each one: `CLAUDE.md` and/or
`.claude/CLAUDE.md`, then `AGENTS.md`/`.claude/AGENTS.md` if
`instructionFiles` calls for it (see below), then that ancestor's own
`.claude/rules/**/*.md`, then `CLAUDE.local.md`. A `.claude/rules/*.md` file
with a `paths:` frontmatter glob is **scoped** -- held back and only
injected the first time a matching file is Read/Edited/Globbed this
session, rather than loaded unconditionally at launch like an ordinary
`CLAUDE.md`. `@path`/`@~/path`/`@//abs/path` imports inside any of these
files are expanded recursively (max 4 hops, 4 MiB per file, cycle-safe); an
import outside `cwd`/`~/.claude` from an **untrusted** project is skipped
with a warning, never silently included.

`settings.instructionFiles` (really
`pluginConfigs.<the agents-md plugin>.options.instructionFiles`, with a
legacy top-level `projectInstructions` mapping onto the same four values)
picks the `CLAUDE.md`/`AGENTS.md` policy per ancestor: `managed-only`
(nothing below the managed tier), `claude-md` (never read `AGENTS.md`),
`claude-md-or-agents-md` (the default -- `AGENTS.md` only where no
`CLAUDE.md` was found at that ancestor), or `claude-md-and-agents-md`
(both, always). `claudeMdExcludes` (a glob list) removes specific files from
this whole walk regardless of policy.

**Memory** (`config/memory.py`): the directory is
`~/.claude/projects/<slug-of-the-git-root-or-cwd>/memory/`, unless
`settings.autoMemoryDirectory` overrides it (that key is **never** honored
from an untrusted, or any, `projectSettings` layer -- policy/flag/local/user
only). `MEMORY.md` there is the index (capped at 200 lines / 25,000 bytes
when injected into a session, truncated with a marker beyond that); every
sibling `*.md` is a topic file, indexed by its own frontmatter
`name`/`description`/`type`/`modified` (checked at the top level first, a
nested `metadata:` block overriding per-key if present). `autoMemoryEnabled`
(default true) and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` both disable it
entirely.

### Skills, custom commands, agents

- **Skills**: `~/.claude/skills/`, `.claude/skills/`, one `SKILL.md` per
  directory. `disable-model-invocation: true` hides a skill from the
  model's own Skill *tool* only -- a user can still always type `/name`.
- **Custom commands**: `~/.claude/commands/`, `.claude/commands/`, one
  `.md` file per command. See `docs/SLASH-COMMANDS.md` for the shared
  `$ARGUMENTS`/`` !`cmd` ``/`@path` body-expansion pipeline both of these
  (and skills) go through.
- **Agents** (`config/agents_md.py::discover_agents`), nearest-wins within
  each tier, later tier overrides an earlier one by `name`: three built-ins
  (`general-purpose`, `Explore`, `Plan`) -> `~/.claude/agents/**/*.md` ->
  every `.claude/agents/**/*.md` walking from `cwd` up to the git root
  (nearest directory wins) -> `--agents` (inline JSON or a file) -> plugin
  `agents/**/*.md` -> managed `agents/**/*.md` (highest). Frontmatter
  `tools:`/`disallowedTools:`/`skills:` accept a comma string or a YAML
  list; `model:` resolves invocation override -> frontmatter -> env
  `CLAUDE_CODE_SUBAGENT_MODEL`/`settings.subagentModel` -> the parent
  session's own model (`haiku` maps to the parent's *small* model
  specifically); a bare `Agent`/`Task` tools entry (or `Agent(name)`/
  `Task(name)`) restricts which `subagent_type`s that agent may itself
  invoke.

### Plans, keybindings, `~/.claude.json`, `.mcp.json`, plugins

- **Plans**: `~/.claude/plans/` by default, or `settings.plansDirectory`.
- **Keybindings**: `~/.claude/keybindings.json`, merged additively onto the
  built-in defaults (see `docs/SLASH-COMMANDS.md`).
- **`~/.claude.json`**: trust dialog state (`projects[cwd].
  hasTrustDialogAccepted`), user-scope `mcpServers`, and
  `projects[cwd].mcpServers` (local-scope MCP servers) -- read on every
  session; **written only by `halo mcp add`/`add-json`/`remove`**
  (see `docs/COMMANDS.md`), which preserve the file's own existing indent
  width, trailing newline, BOM, and every key they don't touch, exactly
  matching what the real `claude mcp add`/`remove` would do. No other code
  path in this harness ever writes this file.
- **`.mcp.json`** (project-scope MCP servers): read from `<cwd>/.mcp.json`;
  a server there needs first-time approval (`enabledMcpjsonServers`/
  `enableAllProjectMcpServers` in `~/.claude.json` or settings, or a
  print-mode run, or a prior approval recorded in
  `~/.halo/mcp-approvals.json`) before it's ever started.
- **Plugins**: `~/.claude/plugins/installed_plugins.json` (both the V1
  single-dict and the real V2 per-scope-array manifest shapes), each
  enabled plugin's own `.mcp.json`/`.claude-plugin/plugin.json` (MCP
  servers) and `hooks/hooks.json` (hooks), with `${CLAUDE_PLUGIN_ROOT}`
  expanded to that plugin's own install directory.

## halo's own files

None of these are read by Claude Code; nothing here is ever confused with
the files above.

| Path | Holds |
|---|---|
| `~/.halo/config.json` | this harness's own settings -- see the key table below |
| `~/.config/halo/env` (`HALO_ENV_FILE`/legacy `BRIDGE_ENV_FILE` to override; legacy `~/.config/vibes-hacker/env` still READ when this one is absent) | `OPENROUTER_API_KEY`/`DATABRICKS_HOST`/`DATABRICKS_TOKEN` etc., `KEY=value` lines, `#` comments, optional `export`; written mode 0600, dir 0700 (POSIX) by `halo init` |
| `~/.halo/sessions/<project-slug>/<id>.jsonl` | one append-only session log per session (see `docs/ARCHITECTURE.md`) |
| `~/.halo/sessions/<project-slug>/index.json` | per-session title/first-prompt/turns/cost, for `/resume`'s picker and `-r <text>` |
| `~/.halo/models.json`, `dbx-endpoints.json`, `models-dev.json`, `cc-models.json` | cached model catalogs (`docs/MODELS.md`) |
| `~/.halo/ollama-capabilities.json` | the `ol:` capability probe's durable cache, keyed by model digest (`docs/MODELS.md`'s "Ollama" section) -- the `/api/tags`+`/api/show` catalog itself is cached in memory only, short TTL, never written to disk |
| `~/.halo/stats-cache.json` | telemetry aggregation cache, keyed by (path, size, mtime) |
| `~/.halo/mcp/tools-cache/<server>.json`, `~/.halo/mcp/<server>.log` | a lazy MCP server's cached tool list, and its stderr |
| `~/.halo/mcp-approvals.json` | remembered `.mcp.json` server approvals |
| `~/.halo/mcp/connectors.json` | cached claude.ai connectors (name/url/status/tools) discovered through `claude mcp list` -- see `docs/COMMANDS.md`'s "claude.ai connectors bridge" section |
| `~/.halo/mcp/oauth/<server-name>.json` | `halo mcp login`'s own OAuth tokens for one remote (http/sse) MCP server -- never written to any Claude Code file |
| `~/.halo/trust.json` | this harness's own trust-dialog store |
| `~/.halo/history.jsonl` | this harness's own prompt history (merged with Claude Code's own `~/.claude/history.jsonl` at read time, never written to) |
| `~/.halo/improve/dismissed.json`, `~/.halo/improve/<ts>.json` | `/improve` dismissed-candidate hashes, and saved candidate batches |
| `~/.halo/work-matrix-<date>.json` | `doctor --work --probe-all` reports (endpoint names only, never a host or token) |
| `~/.halo/sessions/<slug>/<id>/shadow/`, `shadow-index.jsonl` | `/rewind`'s git-shadow snapshots |
| `<state_dir>/routes.json` | optional, proxy-era per-box defaults (`aliases`, `default`/`small` model, per-model `profiles`) -- still read by the harness's own default-model resolution chain; see `routes.example.json` |
| `.halo/team.json` (project) or `~/.halo/team.json` | shared Databricks team preset -- see `docs/DATABRICKS.md` |

### `~/.halo/config.json` keys

Read/written with `halo config get/set` (dotted paths supported) or
directly by the features that own them:

| Key | Default | Set by |
|---|---|---|
| `model` | unset (built-in default applies) | `halo init`, `halo config set model ...` |
| `theme` | auto-detected from terminal truecolor support | `/theme`, `halo config set theme ...` |
| `images` | `"inline"` | `--no-inline-images` overrides per-run |
| `intro` | `true` | `--no-intro` overrides per-run; set `false` to turn off the launch intro for good |
| `mcpPreload` | unset | hand-edited: a list of wire tool names to preload regardless of the catalog's own `alwaysLoad` rule |
| `compactionModel` | unset (uses the session model) | hand-edited |
| `databricks.gateway.<endpoint>` | unset | `halo init --provider databricks` (from a team.json `gateway_preference`), or hand-edited |
| `databricks.dbu_price_usd` | unset (costs show as raw DBUs) | team.json, or hand-edited |
| `databricks.catalog_max_age_hours` | `24` | hand-edited |
| `improve.enabled` | `true` | hand-edited |
| `improve.hint` | `true` | hand-edited |
| `improve.model` | unset (small model, then session model) | `halo config set improve.model or:...` |
| `improve.since_days` | `7` | hand-edited |
| `improve.max_candidates` | `8` (hard-capped at 8) | hand-edited |
| `improve.hint_threshold.{repairs,edit_failures,loop_breaker}` | `3`/`2`/`1` | hand-edited |
| `connectors.bridge` | `true` | `halo config set connectors.bridge false` disables claude.ai connector discovery and every `connector__<slug>` tool outright |
| `connectors.<slug>.enabled` | `true` | hand-edited; `false` removes just that one connector's tool |
| `connectors.<slug>.alwaysLoad` | `false` | hand-edited; preloads that connector's tool instead of leaving it to ToolSearch |
| `connectors.<slug>.max_turns` | `4` | hand-edited; forwarded as the bridge's own `claude -p --max-turns` |
| `connectors.discover_on_start` | `false` | `halo config set connectors.discover_on_start true`; print mode (`-p`) normally never blocks a run on a cold-cache connector discovery -- this opts every run in (the TUI always discovers in the background regardless; `--tools` naming a `connector__<slug>` tool, or a `ToolSearch` call mentioning "connector", also triggers it for just that run) |
| `quit_on_double_ctrl_c` | `true` | `halo config set quit_on_double_ctrl_c false` turns off the second-Ctrl+C quit (only `/exit`/`Ctrl+D`/`Ctrl+Q` leave) |
| `clipboard.crlf` | `false` | hand-edited; `true` makes a copy use `\r\n` line endings instead of `\n` |
| `worktree.remove_on_exit` | `false` | `halo config set worktree.remove_on_exit true`; a `-w/--worktree` session removes its OWN worktree (fires `WorktreeRemoved`) when it ends instead of leaving it on disk |
| `update.check` | `true` | `halo config set update.check false` turns off `halo update`/`/update`'s own check entirely (never shells out, never touches the network) |
| `update.notify` | `true` | `halo config set update.notify false` turns off just the once-a-day "update available" startup note (the check itself, and `/update`'s own dialog, still work) |
| `update.channel` | unset (derived from the install: `stable` when pinned to a `v*` tag, else `main`) | `halo update --channel stable\|main` |
| `ollama.hosts` | unset (synthesizes one default host from `OLLAMA_HOST`/`127.0.0.1:11434`) | hand-edited; a list of `{name, url, default, keep_alive, max_ctx, num_parallel_hint, api_key}` -- `ol:<model>@<hostname>` selects an entry by `name`; see `docs/MODELS.md`'s "Ollama" section |
| `ollama.tools_max` | unset (derived from the effective `num_ctx`'s context class -- under 16k: 16, 16k-32k: 32, 32k-64k: 64, 64k+: 128) | `halo config set ollama.tools_max 64`; overrides the `ol:` ProviderProfile's tool-catalog cap outright, still never below however many built-in tools this platform ships -- see `docs/MODELS.md`'s "Ollama" section |
| `huggingface.endpoints` | unset (no dedicated endpoints configured) | hand-edited; a list of `{name, url, token, default}` -- `hf:endpoint/<name>` selects an entry by `name`; each entry's own `url`/`token` are used as-is, never the router's `HF_TOKEN`/base URL; see `docs/MODELS.md`'s "Hugging Face" section |
| `huggingface.bill_to` | unset (no header sent) | `halo config set huggingface.bill_to my-org`; a Team/Enterprise org name sent as `X-HF-Bill-To` on every ROUTER (`hf:<org>/<model>`) request only -- never on an `hf:endpoint/<name>` call |
| `huggingface.local_servers` | unset (relies on auto-detection alone) | hand-edited, or `halo init`'s Hugging Face tab; a list of `{name, url, api_key, default}` -- `hf:local/<model>@<name>` selects an entry by `name`; `hf:local/<model>` (bare) prefers this list's default entry, else the first AUTO-detected local server; see `docs/MODELS.md`'s "Hugging Face" section |
| `huggingface.local_probe_ports` | unset (`8080, 8000, 1234` -- see `docs/MODELS.md`) | hand-edited; a list of ints overriding which ports the `hf:local/*` auto-detect sweep probes (loopback only); `HF_LOCAL_PROBE_PORTS` (env) wins over this when both are set |

## Every environment variable

2.0.0 rename: every harness-owned knob's CANONICAL name is now `HALO_*`;
the legacy `BRIDGE_*` name (and, for the handful that briefly carried it,
`ROLO_CLAUDE_*`) each still work exactly as before, forever -- resolved in
that order (`HALO_*` first), with one DEBUG-level log line the first time a
legacy name is what actually supplied the value (`--debug`'s own log file).
A handful of test-only seams (`BRIDGE_STATE_DIR`, `BRIDGE_TEST_*`) are
deliberately NOT part of this -- they keep their bare `BRIDGE_` names for
good, with no `HALO_` twin, since the test suites depend on the exact name.

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter credential |
| `OPENROUTER_MANAGEMENT_KEY` | a SEPARATE, higher-privilege OpenRouter key (never the ordinary one above) for the whole-account balance (`GET /credits`) the status bar's "OR $X left" segment/`/cost`/`/providers` show (H15 part 2 addendum 4) -- optional; without it, that segment falls back to the ordinary key's own `/key` usage/limit figures |
| `HALO_OPENROUTER_BASE_URL` (legacy `BRIDGE_OPENROUTER_BASE_URL`) | override the OpenRouter base URL (default `https://openrouter.ai/api/v1`) |
| `DATABRICKS_HOST`, `DATABRICKS_TOKEN` | Databricks credential (see `docs/DATABRICKS.md` for the full discovery chain) |
| `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` | Claude Code's own work-box env names -- read as a Databricks credential when the host matches a Databricks domain |
| `ANTHROPIC_API_KEY` | `ant:` route credential (`api.anthropic.com` directly) |
| `HALO_ANTHROPIC_BASE_URL` (legacy `BRIDGE_ANTHROPIC_BASE_URL`) | override the `ant:` route's own base URL (default `https://api.anthropic.com`) |
| `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL` | at a Databricks work box, set the default model and what bare `opus`/`sonnet`/`haiku` resolve to |
| `ANTHROPIC_CUSTOM_HEADERS` | one-or-more `Name: value` lines, merged onto every Databricks request |
| `HALO_DBX_BASE_URL`, `HALO_DBX_TOKEN` (legacy `BRIDGE_DBX_BASE_URL`/`BRIDGE_DBX_TOKEN`) | explicit override, wins outright over every other Databricks discovery step |
| `HALO_MODEL`, `HALO_MODEL_SMALL` (legacy `BRIDGE_MODEL`/`BRIDGE_MODEL_SMALL`) | override the resolved default main/small model (also used by `halo proxy`) |
| `HALO_ENV_FILE` (legacy `BRIDGE_ENV_FILE`) | path to the `KEY=value` env file (default `~/.config/halo/env`, falling back to legacy `~/.config/vibes-hacker/env` when that's absent) |
| `BRIDGE_STATE_DIR` | this harness's own state directory (default `~/.halo`; a test-only-style seam that keeps its bare `BRIDGE_` name for good -- see the note above) |
| `BRIDGE_TEST_HOME` | test/scratch seam: overrides `home()` everywhere (`~/.claude`, `~/.halo`, ...) -- never set this for real use |
| `HALO_CLAUDE_EXE` (legacy `BRIDGE_CLAUDE_EXE`) | override how the `claude` binary is launched (`cc:` route, `--chrome`) |
| `HALO_DUMP=1` (legacy `BRIDGE_DUMP=1`) | dump raw request/response JSON for debugging (proxy mode) |
| `HALO_CA_BUNDLE` (legacy `BRIDGE_CA_BUNDLE`) | a custom CA bundle path, tried after `NODE_EXTRA_CA_CERTS`/`REQUESTS_CA_BUNDLE` |
| `CLAUDE_CONFIG_DIR` | relocate `~/.claude` (and `~/.claude.json`, honoring a legacy `<dir>/.config.json`) |
| `CLAUDE_CODE_SANDBOXED` | any non-empty value counts as "trusted" for the current directory |
| `CLAUDE_CODE_GIT_BASH_PATH` | override Git Bash's location on Windows |
| `CLAUDE_CODE_SUBAGENT_MODEL` | a settings-independent way to set the sub-agent model fallback |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | disables auto-memory entirely |
| `VISUAL`, `EDITOR` | external editor for `Ctrl+E` (prompt draft) and `/improve`'s `e` (edit a candidate) |
| `HALO_THEME` (legacy `CLAUDE_BRIDGE_THEME`, `ROLO_CLAUDE_THEME`) | override the resolved theme (checked in that order -- `HALO_THEME` first) |
| `NO_COLOR` | disables ANSI color in `stats`' rich-rendered tables |
| `RC_TERM_WIDTH` | pin `stats`' table width instead of auto-detecting the terminal |
| `DISABLE_COMPACT` | disable auto-compaction entirely (manual `/compact` still works) |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | override the compaction trigger's context-window figure |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | lower (never raise) the 80% compaction trigger percentage, 1-100 |
| `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` | consecutive `Stop`-hook-block cap before the turn is force-ended (default 8) |
| `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` | override the `SessionEnd` hook time budget outright |
| `TMUX` | presence gates `doctor`'s tmux-mouse-mode check |
| `OLLAMA_HOST` | seeds the `ol:` default host's URL when `ollama.hosts` has no entries configured yet (Ollama's own documented var; a bare `host:port` is normalized to a full URL) |
| `OLLAMA_API_KEY` | seeds that SAME synthesized default host's `api_key` (Ollama Cloud, `Authorization: Bearer`) -- a configured `ollama.hosts` entry's own `api_key` always wins once one exists |
| `HF_TOKEN` | Hugging Face router credential (`hf:<org>/<model>`) -- the ONLY accepted name; `HUGGING_FACE_HUB_TOKEN`/`HF_API_TOKEN` are NOT read (neither was confirmed as a router-specific alias) |
| `HALO_HF_ROUTER_BASE_URL` (legacy `BRIDGE_HF_ROUTER_BASE_URL`) | override the Hugging Face router base URL (default `https://router.huggingface.co/v1`) -- never consulted for `hf:endpoint/<name>`, which always uses that entry's own configured `url` |
| `HF_HUB_CACHE` | the Hugging Face Hub local cache directory itself (used as-is); `/local`'s hub-cache scan walks this when set, else `$HF_HOME/hub`, else `~/.cache/huggingface/hub` |
| `HF_HOME` | the Hugging Face Hub root directory -- `/local`'s hub-cache scan walks `$HF_HOME/hub` when `HF_HUB_CACHE` is unset |
| `HF_LOCAL_PROBE_PORTS` | comma-separated ints overriding which ports the `hf:local/*` auto-detect sweep probes (default `8080,8000,1234` -- see `docs/MODELS.md`); wins over `huggingface.local_probe_ports` when both are set |

## Providers (`halo init --provider ...`)

1.0.1 hotfix 13: `init` selects a PROVIDER to set up, not a "preset" naming
a bundle of choices; the old `--preset home|work|claude` still works, as a
deprecated alias for `--provider openrouter|databricks|claude` respectively
(one-line notice printed, no behavior change).

| Provider | Default model | Credential asked for |
|---|---|---|
| `openrouter` | `or:deepseek/deepseek-v4.1-flash` | `OPENROUTER_API_KEY` |
| `databricks` | `dbx:databricks-deepseek-v4-1-flash` | `DATABRICKS_HOST` + `DATABRICKS_TOKEN` (host often already known -- see `docs/DATABRICKS.md`) |
| `anthropic` | `ant:sonnet` | `ANTHROPIC_API_KEY` |
| `claude` | `cc:sonnet` | none -- uses your existing `claude` login as-is |

Ollama and Hugging Face each get their own tab in `halo init`'s
INTERACTIVE Providers step (Halo 2.0.3 round 5, `init_providers.
TAB_PROVIDERS`). `halo setup`/`/setup` never reach a Providers step at all
(their own step list is `roles`/`orgs`/`summary` only -- see `docs/
COMMANDS.md`'s `setup` section), so neither tab appears there; only `halo
init` itself shows them. Neither is a `halo init --provider`/
`--preset` CLI-flag CHOICE, though, and that's deliberate: that flag
drives the OLDER sequential, non-interactive picker, which has no sensible
single hardcoded default model for either (unlike the four providers in
the table above, which always have one well-known catalog entry) -- the
same reason that picker's own no-TTY/Textual-failure fallback never offers
either tab. Configure `HF_TOKEN`/`huggingface.endpoints`/`huggingface.
local_servers`/`ollama.hosts` directly instead on a non-interactive box
(see `docs/MODELS.md`'s "Ollama"/"Hugging Face" sections).

### Default permission mode (1.0.1 hotfix 18)

After the provider(s)/default model are settled, `init` also asks for a
**default permission mode** -- `auto` (recommended, listed first),
`acceptEdits`, `default`, `plan` -- written to `~/.halo/config.json`'s
own flat `permission_mode` key. Precedence for a session's actual starting
mode, highest first:

1. `--dangerously-skip-permissions`
2. `--permission-mode` (this run's own flag)
3. `~/.halo/config.json`'s `permission_mode` (this section)
4. `settings.json`'s `permissions.defaultMode` (user layer, or
   project/local for the manual modes, as before)
5. the hardcoded `default`

`halo doctor` prints the effective mode and which layer decided it.
Never touches `~/.claude/settings.json` -- `halo config set
permission_mode auto` (or any of the four modes) sets the same key directly
without re-running `init`.

As of the H14c fixpass (finding 9), this step (and the default-model pick
just before it) only ever writes a value you actually chose THIS run --
`--yes`/non-interactive and Esc both keep whatever is already in
`config.json` unchanged (writing nothing at all for `permission_mode` on a
fresh box, so layer 4 above still applies) instead of forcing `auto`/an
arbitrary other-provider's model over it.

## What is never written

- `~/.claude.json`, `~/.claude/settings.json` -- **never** touched by
  `init`/`doctor`/the agent loop; the ONE deliberate exception is
  `halo mcp add`/`add-json`/`remove` writing `~/.claude.json`'s
  `mcpServers` keys, precisely mirroring what `claude mcp add`/`remove`
  itself writes.
- `~/.claude/.credentials.json` -- **never opened at all**, not even to
  check it exists. The `cc:`/`ant:` subscription check reads only
  `claude auth status`'s own JSON output.
- A provider API key or Databricks token is never written into a session
  log, a cache file, or a `doctor`/`--config`/`stats` line -- `redact()`
  keeps the first four characters and elides the rest; `halo export
  --sanitize`/`/export --sanitize` additionally scrub anything that slipped
  into a transcript via a tool's own output (a Bash `env` dump, a Read of a
  settings file).
