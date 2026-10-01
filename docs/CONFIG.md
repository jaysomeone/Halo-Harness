# Configuration

Exactly which files this harness reads (Claude Code's own config, unchanged)
and which it owns (its own small state under `~/.rolo-claude/`), how they're
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
is set to any non-empty value; or `~/.rolo-claude/trust.json` has an entry
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
  session; **written only by `rolo-claude mcp add`/`add-json`/`remove`**
  (see `docs/COMMANDS.md`), which preserve the file's own existing indent
  width, trailing newline, BOM, and every key they don't touch, exactly
  matching what the real `claude mcp add`/`remove` would do. No other code
  path in this harness ever writes this file.
- **`.mcp.json`** (project-scope MCP servers): read from `<cwd>/.mcp.json`;
  a server there needs first-time approval (`enabledMcpjsonServers`/
  `enableAllProjectMcpServers` in `~/.claude.json` or settings, or a
  print-mode run, or a prior approval recorded in
  `~/.rolo-claude/mcp-approvals.json`) before it's ever started.
- **Plugins**: `~/.claude/plugins/installed_plugins.json` (both the V1
  single-dict and the real V2 per-scope-array manifest shapes), each
  enabled plugin's own `.mcp.json`/`.claude-plugin/plugin.json` (MCP
  servers) and `hooks/hooks.json` (hooks), with `${CLAUDE_PLUGIN_ROOT}`
  expanded to that plugin's own install directory.

## rolo-claude's own files

None of these are read by Claude Code; nothing here is ever confused with
the files above.

| Path | Holds |
|---|---|
| `~/.rolo-claude/config.json` | this harness's own settings -- see the key table below |
| `~/.config/vibes-hacker/env` (`BRIDGE_ENV_FILE` to override) | `OPENROUTER_API_KEY`/`DATABRICKS_HOST`/`DATABRICKS_TOKEN` etc., `KEY=value` lines, `#` comments, optional `export`; written mode 0600, dir 0700 (POSIX) by `rolo-claude init` |
| `~/.rolo-claude/sessions/<project-slug>/<id>.jsonl` | one append-only session log per session (see `docs/ARCHITECTURE.md`) |
| `~/.rolo-claude/sessions/<project-slug>/index.json` | per-session title/first-prompt/turns/cost, for `/resume`'s picker and `-r <text>` |
| `~/.rolo-claude/models.json`, `dbx-endpoints.json`, `models-dev.json`, `cc-models.json` | cached model catalogs (`docs/MODELS.md`) |
| `~/.rolo-claude/stats-cache.json` | telemetry aggregation cache, keyed by (path, size, mtime) |
| `~/.rolo-claude/mcp/tools-cache/<server>.json`, `~/.rolo-claude/mcp/<server>.log` | a lazy MCP server's cached tool list, and its stderr |
| `~/.rolo-claude/mcp-approvals.json` | remembered `.mcp.json` server approvals |
| `~/.rolo-claude/trust.json` | this harness's own trust-dialog store |
| `~/.rolo-claude/history.jsonl` | this harness's own prompt history (merged with Claude Code's own `~/.claude/history.jsonl` at read time, never written to) |
| `~/.rolo-claude/improve/dismissed.json`, `~/.rolo-claude/improve/<ts>.json` | `/improve` dismissed-candidate hashes, and saved candidate batches |
| `~/.rolo-claude/work-matrix-<date>.json` | `doctor --work --probe-all` reports (endpoint names only, never a host or token) |
| `~/.rolo-claude/sessions/<slug>/<id>/shadow/`, `shadow-index.jsonl` | `/rewind`'s git-shadow snapshots |
| `<state_dir>/routes.json` | optional, proxy-era per-box defaults (`aliases`, `default`/`small` model, per-model `profiles`) -- still read by the harness's own default-model resolution chain; see `routes.example.json` |
| `.rolo-claude/team.json` (project) or `~/.rolo-claude/team.json` | shared Databricks team preset -- see `docs/DATABRICKS.md` |

### `~/.rolo-claude/config.json` keys

Read/written with `rolo-claude config get/set` (dotted paths supported) or
directly by the features that own them:

| Key | Default | Set by |
|---|---|---|
| `model` | unset (built-in default applies) | `rolo-claude init`, `rolo-claude config set model ...` |
| `theme` | auto-detected from terminal truecolor support | `/theme`, `rolo-claude config set theme ...` |
| `images` | `"inline"` | `--no-inline-images` overrides per-run |
| `mcpPreload` | unset | hand-edited: a list of wire tool names to preload regardless of the catalog's own `alwaysLoad` rule |
| `compactionModel` | unset (uses the session model) | hand-edited |
| `databricks.gateway.<endpoint>` | unset | `rolo-claude init --provider databricks` (from a team.json `gateway_preference`), or hand-edited |
| `databricks.dbu_price_usd` | unset (costs show as raw DBUs) | team.json, or hand-edited |
| `databricks.catalog_max_age_hours` | `24` | hand-edited |
| `improve.enabled` | `true` | hand-edited |
| `improve.hint` | `true` | hand-edited |
| `improve.model` | unset (small model, then session model) | `rolo-claude config set improve.model or:...` |
| `improve.since_days` | `7` | hand-edited |
| `improve.max_candidates` | `8` (hard-capped at 8) | hand-edited |
| `improve.hint_threshold.{repairs,edit_failures,loop_breaker}` | `3`/`2`/`1` | hand-edited |

## Every environment variable

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter credential |
| `BRIDGE_OPENROUTER_BASE_URL` | override the OpenRouter base URL (default `https://openrouter.ai/api/v1`) |
| `DATABRICKS_HOST`, `DATABRICKS_TOKEN` | Databricks credential (see `docs/DATABRICKS.md` for the full discovery chain) |
| `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` | Claude Code's own work-box env names -- read as a Databricks credential when the host matches a Databricks domain |
| `ANTHROPIC_API_KEY` | `ant:` route credential (`api.anthropic.com` directly) |
| `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL` | at a Databricks work box, set the default model and what bare `opus`/`sonnet`/`haiku` resolve to |
| `ANTHROPIC_CUSTOM_HEADERS` | one-or-more `Name: value` lines, merged onto every Databricks request |
| `BRIDGE_DBX_BASE_URL`, `BRIDGE_DBX_TOKEN` | explicit override, wins outright over every other Databricks discovery step |
| `BRIDGE_MODEL`, `BRIDGE_MODEL_SMALL` | override the resolved default main/small model (`BRIDGE_MODEL` also used by the older proxy) |
| `BRIDGE_ENV_FILE` | path to the `KEY=value` env file (default `~/.config/vibes-hacker/env`) |
| `BRIDGE_STATE_DIR` | this harness's own state directory (default `~/.rolo-claude`) |
| `BRIDGE_TEST_HOME` | test/scratch seam: overrides `home()` everywhere (`~/.claude`, `~/.rolo-claude`, ...) -- never set this for real use |
| `BRIDGE_CLAUDE_EXE` | override how the `claude` binary is launched (`cc:` route, `--chrome`) |
| `BRIDGE_DUMP=1` | dump raw request/response JSON for debugging (proxy mode) |
| `CLAUDE_CONFIG_DIR` | relocate `~/.claude` (and `~/.claude.json`, honoring a legacy `<dir>/.config.json`) |
| `CLAUDE_CODE_SANDBOXED` | any non-empty value counts as "trusted" for the current directory |
| `CLAUDE_CODE_GIT_BASH_PATH` | override Git Bash's location on Windows |
| `CLAUDE_CODE_SUBAGENT_MODEL` | a settings-independent way to set the sub-agent model fallback |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | disables auto-memory entirely |
| `VISUAL`, `EDITOR` | external editor for `Ctrl+E` (prompt draft) and `/improve`'s `e` (edit a candidate) |
| `CLAUDE_BRIDGE_THEME`, `ROLO_CLAUDE_THEME` | override the resolved theme (checked in that order) |
| `NO_COLOR` | disables ANSI color in `stats`' rich-rendered tables |
| `RC_TERM_WIDTH` | pin `stats`' table width instead of auto-detecting the terminal |
| `DISABLE_COMPACT` | disable auto-compaction entirely (manual `/compact` still works) |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | override the compaction trigger's context-window figure |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | lower (never raise) the 80% compaction trigger percentage, 1-100 |
| `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` | consecutive `Stop`-hook-block cap before the turn is force-ended (default 8) |
| `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` | override the `SessionEnd` hook time budget outright |
| `TMUX` | presence gates `doctor`'s tmux-mouse-mode check |

## Providers (`rolo-claude init --provider ...`)

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

### Default permission mode (1.0.1 hotfix 18)

After the provider(s)/default model are settled, `init` also asks for a
**default permission mode** -- `auto` (recommended, listed first),
`acceptEdits`, `default`, `plan` -- written to `~/.rolo-claude/config.json`'s
own flat `permission_mode` key. Precedence for a session's actual starting
mode, highest first:

1. `--dangerously-skip-permissions`
2. `--permission-mode` (this run's own flag)
3. `~/.rolo-claude/config.json`'s `permission_mode` (this section)
4. `settings.json`'s `permissions.defaultMode` (user layer, or
   project/local for the manual modes, as before)
5. the hardcoded `default`

`rolo-claude doctor` prints the effective mode and which layer decided it.
Never touches `~/.claude/settings.json` -- `rolo-claude config set
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
  `rolo-claude mcp add`/`add-json`/`remove` writing `~/.claude.json`'s
  `mcpServers` keys, precisely mirroring what `claude mcp add`/`remove`
  itself writes.
- `~/.claude/.credentials.json` -- **never opened at all**, not even to
  check it exists. The `cc:`/`ant:` subscription check reads only
  `claude auth status`'s own JSON output.
- A provider API key or Databricks token is never written into a session
  log, a cache file, or a `doctor`/`--config`/`stats` line -- `redact()`
  keeps the first four characters and elides the rest; `rolo-claude export
  --sanitize`/`/export --sanitize` additionally scrub anything that slipped
  into a transcript via a tool's own output (a Bash `env` dump, a Read of a
  settings file).
