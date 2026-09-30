# Commands

Every CLI subcommand `rolo-claude` accepts, and every flag any of them
accepts, verified against the real `--help` output and the argparse
definitions in `rolo_claude/cli.py` and each subcommand's own module. For
the in-app `/slash` commands, key bindings, `@file` and `!cmd` prefixes, see
[SLASH-COMMANDS.md](SLASH-COMMANDS.md); for the architecture behind these
commands, see [ARCHITECTURE.md](ARCHITECTURE.md).

**How this page is organised (and kept honest)**: every flag/subcommand
heading below wraps the exact token in backticks, e.g. `#### \`--model\``
or `## \`rolo-claude doctor\``. `tests/test_docs_commands.py` calls every
real `--help` in-process (no subprocess, no network) and fails the suite if
a flag/subcommand exists in the code but isn't mentioned here, or if a
`` `-x`/`--xxx` `` token inside a heading here doesn't match a real one --
so this page cannot silently drift from `cli.py` again in either direction.
A `[not-yet]` flag parses (never an argparse error) but its feature isn't
built; see [Flags parsed but not implemented yet](#flags-parsed-but-not-implemented-yet).

Every worked example below uses `--demo` (a scripted turn needing no
network, API key, or model) or a scratch `BRIDGE_TEST_HOME` so it is safe to
paste into a shell verbatim; swap in your own model/prompt once you have a
provider configured (`rolo-claude init`).

```sh
export PYTHONPATH=/path/to/rolo-claude   # a dev checkout; skip if installed
rolo-claude --version
```
```
rolo-claude 1.0.0
```

There is no `sessions` subcommand in this build -- session resume/fork/
rename/list lives entirely under the default command's own `-c`/`-r`/
`--fork-session`/`-n` flags and the TUI's `/resume`/`/rename`/`/fork` (see
`docs/SLASH-COMMANDS.md`).

## `rolo-claude` (the main command)

```
rolo-claude [PROMPT] [flags...]
```

With no `-p`/`--print`, `rolo-claude` opens the full-screen TUI (see
`docs/SLASH-COMMANDS.md`); a `PROMPT` positional argument pre-fills it as the
first turn. `-p`/`--print` runs one headless turn instead and exits -- this
is what every worked example on this page uses. Outside a real terminal
(piped stdin, cron, a subprocess with no tty), the bare (non-`-p`) form
prints one line and exits 2 rather than hanging, since nothing could ever
drive a full-screen UI there:

```sh
echo | rolo-claude
```
```
rolo-claude: a full-screen session requires an interactive terminal (stdin is not a tty) -- use -p/--print for a non-interactive run
```
(exit code 2)

A bare `-p` positional prompt, or stdin (UTF-8, 10 MB cap) when no positional
is given, is the text of the first turn:

```sh
rolo-claude --demo -p
```
```
All done -- that's a scripted demo turn.
```

```sh
rolo-claude --demo -p --output-format json
```
```json
{"type": "result", "subtype": "success", "is_error": false, "result": "All done -- that's a scripted demo turn.", "session_id": "demo-session", "num_turns": 1, "stop_reason": "end_turn", "usage": {"input_tokens": 270, "output_tokens": 100}, "total_cost_usd": 0.0, "structured_output": null, "model": "or:deepseek/deepseek-v4.1-flash", "permission_denials": [], "background_notices": []}
```

An unresolvable `--model` is a clean, scriptable usage error (exit 2), with a
near-miss suggestion when the OpenRouter catalog is cached (`rolo-claude
models --refresh`) and something close matches:

```sh
rolo-claude -p --model not-a-real-model-ref "hi"
```
```
rolo-claude: invalid --model: no route: 'not-a-real-model-ref' (accepted forms are dbx:, or:, ant:, cc:, vendor/model, a bare databricks-*/system.ai.* name, a subscription-model alias, or a routes.json alias)
```

Exit codes across every route: `0` success, `1` the turn ran and ended in an
error (upstream failure, budget exceeded), `2` a usage/config error (bad
flag value, no prompt given, invalid model), `130` `Ctrl+C` during a `-p`
turn (POSIX `128+SIGINT`).

### Model & session flags

#### `--model MODEL`, `--small-model MODEL`
What: the main model for this session, and the model used for
background/small tasks (title generation, `/improve` drafting when
`improve.model` isn't set) respectively. Reads: `~/.rolo-claude/config.json`
(`model` key), `routes.json`'s `aliases`/`default` and `small`, `BRIDGE_MODEL`
env, only when neither flag is given -- see `docs/MODELS.md` for the full
resolution order. Default: `or:deepseek/deepseek-v4.1-flash` (see
`model.DEFAULT_MODEL_REF`) when nothing else resolves.
```sh
rolo-claude -p --model or:deepseek/deepseek-v3.2 --small-model or:deepseek/deepseek-v3.2 "hi"
```

#### `--effort {low,medium,high,xhigh,max}`
What: requests a reasoning-effort level, mapped per-provider (a thinking
budget for `ant:`/Databricks Claude passthrough, `reasoning.effort` on
OpenRouter, `reasoning_effort` on Databricks chat) -- ignored (no field
added) for a model whose profile doesn't support it. Related config:
`effortLevel`/`modelSettings.<id>.effortLevel` in `settings.json` set a
session's *default* effort when `--effort` is omitted (`docs/CONFIG.md`).

#### `-c`, `--continue`
What: resumes the most recently modified session for the current directory
(by the session `.jsonl` file's own mtime). Reads:
`~/.rolo-claude/sessions/<slug>/*.jsonl`.
```sh
rolo-claude -p -c "keep going"
```

#### `-r`, `--resume [ID_OR_TEXT]`
What: with an id (or unique id prefix), resumes that exact session; with
free text, resumes the one session that text uniquely matches by title/first
prompt/cwd/model, or -- in print mode -- lists the ambiguous candidates
instead of guessing; with nothing at all, opens the TUI's session picker
(pre-filtered if this flag's own value made the CLI resume ambiguous).
```sh
rolo-claude -p -r a1b2c3d4 "one more thing"
```

#### `--fork-session`
What: combined with `-c`/`-r`, continues from that session's log *without
overwriting it* -- the new turns land in a fresh session id, the source is
untouched (same file-copy mechanism as `/fork`).

#### `--session-id UUID`
What: use this exact session id for a brand-new session (must not already
exist). Mirrors Claude Code's own flag; mainly useful for scripted/SDK-style
callers that want a known id up front.

#### `-n`, `--name NAME`
What: sets this session's title (same effect as `/rename` after the fact).
Writes: `~/.rolo-claude/sessions/<slug>/index.json`.

#### `--agent AGENT`, `--agents JSON_OR_FILE`
What: `--agent` pins this top-level session to run as if it were the named
sub-agent (its own tool/model/permission-mode restrictions apply from turn
one); `--agents` adds extra agent definitions inline -- a literal JSON object
string in the TUI, or (in `-p`) either a literal JSON string or a path to a
JSON file holding `{name: {description, prompt, tools, model, ...}}`. See
`docs/CONFIG.md`'s agents section for the full discovery precedence these
sit on top of.

#### `--role NAME=MODEL`

What: overrides one of the five roles (`orchestrator`, `coder`, `reviewer`,
`researcher`, `small`) for this run only -- a built-in/custom sub-agent whose
own role resolves to `NAME` uses `MODEL` instead of whatever `~/.rolo-claude/
config.json`/`team.json`'s own `roles` table (or the cost-aware default) says,
even beating that agent's own file `model:` (a freshly-typed, run-only
override the user gets to trump a shared/managed agent file with). Repeatable
(`--role coder=... --role researcher=...`); a later repeat of the SAME role
wins. A bad `NAME=MODEL` (missing `=`, an unrecognized role name, an empty
model) is a clean exit-2 usage error before any Session is built. See
`docs/ROLES.md` for the full precedence chain, `/roles`, and `stats --roles`.
```sh
rolo-claude -p --role researcher=or:deepseek/deepseek-v4.1-flash "use the Researcher agent to summarize this repo"
```

#### `--file SPEC [SPEC ...]`
What: attaches one or more local files as context before the first turn (an
image becomes a real vision content block when the model profile supports
it; anything else becomes a text snapshot) -- the same path resolution a
model's own Read tool call would use. **Argparse quirk**: this flag is
`nargs="+"`, so it greedily consumes every following bare word as another
SPEC -- put the prompt *before* `--file`, or use a `--` separator, or the
prompt itself disappears into the file-spec list:
```sh
rolo-claude -p "summarize the attached file" --file ./notes.txt
```
Claude Code's own `file_id:relative_path` cloud-resource form (a
claude.ai-hosted file) has no backing store in this standalone harness and
prints one clear stderr notice instead of pretending to work; on Windows, a
path to a file that does *not* exist can be misread as that form too (both
shapes contain a `:`) -- see `docs/TROUBLESHOOTING.md`.

#### `--add-dir DIRECTORY [DIRECTORY ...]`
What: extends the session's allowed working directories beyond `--cwd`
(path-rule matching and the mode table's "in workdir" checks then also
accept these). Related config: `permissions.additionalDirectories` in
`settings.json` does the same thing persistently.

#### `--cwd CWD`
What: the working directory for this session (settings/CLAUDE.md/memory/
trust are all resolved relative to it). Default: the process's actual `cwd`.

### Output & input format flags

#### `--output-format {text,json,stream-json}`
What: `text` (default) prints the final reply only (or, with `--verbose`,
every intermediate message too, reasoning dimmed); `json` prints one
result object at the end (`session_id`, `num_turns`, `usage`,
`total_cost_usd`, `permission_denials`, `structured_output`, ...); `stream-json`
emits one JSON line per event (`init`, `assistant`/`user` messages, a final
`result`) -- Claude Code's own SDK wire shape.
```sh
rolo-claude --demo -p --output-format stream-json
```

#### `--input-format {text,stream-json}`
What: `text` (default) takes one prompt (positional or stdin); `stream-json`
reads one JSON turn object per stdin line, incrementally -- so an SDK-style
caller that waits for each turn's `result` before writing the next line
never deadlocks.

#### `--include-partial-messages`
What: with `--output-format stream-json`, also emits a `stream_event` line
per text/thinking delta (not just the final assembled message).

#### `-d`, `--debug [FILTER]`
What: turns on DEBUG file logging for the whole run, TUI or print mode. The
log goes to `~/.rolo-claude/bridge.log` (rotating, secrets redacted) and the
path is printed once on stderr as `rolo-claude: debug log -> <path>`. The
optional FILTER value is accepted for Claude Code parity and ignored:
everything is logged. Use it when the TUI misbehaves or a route fails, then
send the last lines of the log.
Reads: nothing new. Writes: `~/.rolo-claude/bridge.log`.
```sh
rolo-claude --debug
tail -60 ~/.rolo-claude/bridge.log
```

#### `--debug-file PATH`
What: same as `--debug`, but the log is written to `PATH` (parent
directories are created) instead of the state directory.
```sh
rolo-claude --debug-file /tmp/rolo-claude-debug.log -p "reply with the single word pong"
```

#### `--verbose`
What: in text mode, also prints every intermediate assistant message this
turn produced (dimmed) on the way to the final reply, plus reasoning; a
running commentary of model/tool calls on stderr.

#### `--json-schema SCHEMA`
What: appends the schema to the system prompt as an instruction and, on
success, re-parses the final reply text as JSON into the `json` result
object's `structured_output` field (`null` if the reply wasn't valid JSON --
no separate schema-validation library is used, this only checks "is it
JSON at all").

#### `--replay-user-messages`
What: mirrors every stdin line back out (stream-json modes) once actually
consumed -- what the `cc:` route's own steering-confirmation mechanism relies
on internally; rarely needed directly.

### System prompt flags

#### `--system-prompt PROMPT`, `--system-prompt-file FILE`
What: **replaces** the built-in system prompt entirely (the harness
self-description, tool guidance, family notation -- see
`docs/ARCHITECTURE.md`) with this text. The `-file` form reads it from disk
(UTF-8); if both are given, the inline value wins.

#### `--append-system-prompt TEXT`, `--append-system-prompt-file FILE`
What: adds one more section after the built-in system prompt instead of
replacing it -- the common case (skills/commands/agents use the same
mechanism internally). Both may be combined with each other (file content is
appended after the inline text).

### Settings & permissions flags

#### `--settings JSON_OR_PATH`
What: one more settings layer, highest precedence except `policySettings`
(managed) -- a literal JSON string, or a path to a JSON file in the same
shape as `.claude/settings.json`. See `docs/CONFIG.md`'s precedence table.

#### `--setting-sources SOURCES`
What: a comma list restricting which of `user,project,local` settings layers
are read at all (managed/policy and `--settings` itself are never affected).
```sh
rolo-claude -p --setting-sources user "hi"
```

#### `--permission-mode {default,acceptEdits,plan,auto,dontAsk,bypassPermissions,manual}`
What: the session's starting permission mode (`manual` is a display alias
for `default`). See `docs/ARCHITECTURE.md`'s permissions section for exactly
what each mode allows/asks/denies -- there is no classifier or heuristic
layered on top of any of them.
```sh
rolo-claude -p --permission-mode auto "hi"
```

#### `--dangerously-skip-permissions`
What: shorthand for `--permission-mode bypassPermissions` (allow everything
not matched by an explicit deny rule).

#### `--allowedTools TOOLS` / `--allowed-tools`, `--disallowedTools TOOLS` / `--disallowed-tools`
What: a space- or comma-separated list of permission-rule-shaped strings
(`Bash(git *)`, `mcp__server`, a bare tool name, ...), merged into the
session's allow/deny rule set at the same precedence a settings-file rule of
the same kind would have.

#### `--tools TOOLS`
What: restricts the session's tool catalog itself to this comma/space list
of tool names (a tool not in this list is never offered to the model at
all, unlike `--disallowedTools`, which still shows the tool but denies
calling it).

#### `--bare`
What: skips CLAUDE.md/AGENTS.md/rules discovery, memory, hooks that read
those files, and `/improve`'s drafting -- a minimal-context session, mirrors
Claude Code's own `--bare` intent.

#### `--disable-slash-commands`
What: turns off `/name` parsing in this session entirely -- a line starting
with `/` is sent to the model as plain text instead.

### Budget flags

#### `--max-turns MAX_TURNS`
What: caps the number of *model calls* (not tool calls) a single `turn()`
may make before the harness forces a wrap-up reply and ends the turn.
Default: `50`.

#### `--max-budget-usd MAX_BUDGET_USD`
What: stops the session (mid-turn, at the next safe point) once cumulative
cost meets or exceeds this many US dollars -- a route with no real per-token
pricing (Databricks, a `cc:` subscription turn) never counts toward it.

### Browser flags

#### `--chrome`, `--no-chrome`
What: `--chrome` starts the claude-in-chrome extension's native-messaging
bridge as a dynamic MCP server for this session (screenshots, page
reading/interaction tools); `--no-chrome` forces it off even if a settings
file would otherwise enable it by default. `rolo-claude doctor` reports
whether the native host is registered at all.

#### `--playwright`, `--playwright-cdp ENDPOINT`, `--playwright-headless`
What: `--playwright` drives a real local Chromium/Chrome/Brave via
Playwright as a dynamic MCP server instead; `--playwright-cdp` attaches to
an already-running browser's DevTools Protocol endpoint instead of
launching a new one; `--playwright-headless` runs without a visible window.
Needs `node`/`npx` on PATH (`rolo-claude doctor` checks for both).

### MCP flags

#### `--mcp-config CONFIG [CONFIG ...]`
What: one or more extra MCP server configs for this run only -- each value
is a JSON string or a path to a JSON file, `{"mcpServers": {...}}` or a bare
`{name: {...}}` map. Same precedence tier as `--chrome`/`--playwright`'s own
dynamic servers.

#### `--strict-mcp-config`
What: use *only* `--mcp-config` (plus `--chrome`/`--playwright` if also
given) -- every other source (`.mcp.json`, `~/.claude.json`, plugins) is
skipped for this run.
```sh
rolo-claude -p --strict-mcp-config --mcp-config '{"mcpServers":{}}' "hi"
```

### Image flags

#### `--no-inline-images`
What: forces the plain type/size/dimensions caption for every image tool
result this run, same as `images: "caption"` in `~/.rolo-claude/config.json`
but scoped to one invocation (TUI only -- print mode has no inline-image
concept to disable).

### Misc flags

#### `--theme THEME`
What: one of `claude-dark`/`claude-light` (`-daltonized`/`-ansi` variants of
each) for this run; `/theme` persists a choice for future sessions.

#### `--demo`
What: runs (or, with `-p`, prints) a scripted walkthrough turn that needs no
network, no API key and no configured model -- the safe example used
throughout this page and the README's own 10-minute walkthrough.

#### `--stress N`
What: repeats the demo script's own synthetic load N times (a throughput/UI
smoke test, not a real benchmark).

#### `-v`, `--version`
What: prints `rolo-claude <version>` and exits 0, before any config is read.

### Flags parsed but not implemented yet

Every flag below is accepted by the argument parser (never an "unrecognized
arguments" error) and, the first time its value differs from "never touched
at all", prints one line to stderr naming the milestone it's planned for,
then continues the run as if the flag had not been given:

```sh
rolo-claude --ide -p "hi"
```
```
rolo-claude: --ide is not supported yet (planned: H8)
```

| Flag | Planned |
|---|---|
| `--allow-dangerously-skip-permissions` | H4 |
| `--autocompact AUTO_OR_TOKENS` | H5 |
| `--ax-screen-reader` | U2 |
| `--bg`, `--background` | H8 |
| `--betas BETA [BETA ...]` | H8 |
| `--brief` | H4 |
| `--cloud [CLOUD]` | H8 |
| `--environment ENVIRONMENT_ID` | H8 |
| `--exclude-dynamic-system-prompt-sections` | H5 |
| `--fallback-model MODEL` | H6 |
| `--forward-subagent-text` | H6 |
| `--from-pr [FROM_PR]` | H8 |
| `--ide` | H8 |
| `--include-hook-events` | H4 |
| `--no-session-persistence` | H6 |
| `--permission-prompt-tool TOOL` | H4 |
| `--permission-prompts {host,none}` | H4 |
| `--plugin-dir PATH` | H4 |
| `--plugin-url URL` | H4 |
| `--prompt-suggestions [...]` | U3 |
| `--remote-control [REMOTE_CONTROL]` | H8 |
| `--remote-control-session-name-prefix PREFIX` | H8 |
| `--restricted` | H4 |
| `--safe-mode` | H4 |
| `--system-prompt-snapshot {on,off}` | H5 |
| `--teleport [TELEPORT]` | H8 |
| `--tmux [TMUX]` | H8 |
| `-w`, `--worktree [WORKTREE]` | H8 |

"Planned" names the internal milestone id this project tracks its own
roadmap with (see `docs/harness/README.md`) -- it is not a promise of a
release date. The canonical, always-current list is
`rolo_claude/cli.py`'s own `_NOT_YET_FLAGS` table; this page's test
(`tests/test_docs_commands.py`) fails if the two ever disagree.

## `rolo-claude init`

One command that sets up a fresh box: picks a preset, configures
credentials, sets the default model, runs `doctor`, refreshes the model
catalogs, sends one live "pong", and (Linux only) offers the `rg`/PATH
fixes. Every step is idempotent -- re-running only reports what's already
correct. Never writes `~/.claude.json`/`~/.claude/settings.json`, never
prints a key or token.

```sh
rolo-claude init --help
```
```
usage: rolo-claude init [-h] [--preset {home,work,claude}] [--model REF]
                        [--yes] [--no-live] [--no-fixes] [--team PATH|URL]

Set up rolo-claude in one command: pick a preset, configure credentials, set a
default model, run doctor, send a live pong, and offer the Linux setup fixes.

options:
  -h, --help            show this help message and exit
  --preset {home,work,claude}
                        home=OpenRouter, work=Databricks, claude=your Claude
                        subscription
  --model REF           override the preset's own default model
  --yes                 accept every default without prompting
  --no-live             skip the catalog refresh and the live pong
  --no-fixes            skip the Linux rg/PATH fixes step
  --team PATH|URL       a team.json preset (host/default model/gateway
                        preference/DBU price -- never a token); overrides
                        .rolo-claude/team.json / ~/.rolo-claude/team.json
```

| Flag | Reads/writes | Default |
|---|---|---|
| `--preset {home,work,claude}` | auto-detected (existing `OPENROUTER_API_KEY` -> `home`; a Databricks host/`ucode-settings.json` -> `work`; a `claude.ai` login and nothing else -> `claude`) when omitted | auto-detect |
| `--model REF` | writes `~/.rolo-claude/config.json`'s `model` key | the preset's own default (`or:deepseek/deepseek-v4.1-flash` / `dbx:databricks-deepseek-v4-1-flash` / `cc:sonnet`) |
| `--yes` | -- | off (interactive prompts) |
| `--no-live` | skips `models --refresh` and the live pong | off |
| `--no-fixes` | skips the `rg`/PATH steps (Linux) | off |
| `--team PATH\|URL` | reads a `team.json`-shaped file/URL (see `docs/DATABRICKS.md`) | `.rolo-claude/team.json`, then `~/.rolo-claude/team.json` |

`--preset ... --yes` is fully non-interactive whenever the needed value is
already discoverable; when it isn't (e.g. no OpenRouter key found and
`--yes` was passed), it prints a `[WARN]` naming the env var to set instead
of blocking on a prompt. Credentials go to the same env file every other
part of the harness reads (`BRIDGE_ENV_FILE`, else
`~/.config/vibes-hacker/env`, mode 0600 on POSIX).

Worked example (a scratch home, so nothing real is touched):
```sh
BRIDGE_TEST_HOME=/tmp/demo-home rolo-claude init --preset home --yes --no-live
```
```
rolo-claude init
1. Preset: home (from --preset)
2. Credentials:
   [WARN] OpenRouter key not found, and --yes skips the prompt -- set OPENROUTER_API_KEY (or re-run `rolo-claude init` without --yes).
3. Default model: or:deepseek/deepseek-v4.1-flash -- wrote /tmp/demo-home/.rolo-claude/config.json
4. Checks:
   ...doctor lines...
   (catalog refresh skipped: --no-live)
5. Live pong: skipped (--no-live)
6. Linux fixes:
   ...
7. Summary:
   wrote /tmp/demo-home/.rolo-claude/config.json
   Run `rolo-claude` to start.
```

## `rolo-claude doctor`

A read-only environment check; every `[WARN]`/`[MISSING]` line ends with
`-> fix: <command>` or `-> see: <reference>`. Never writes anything.

```sh
rolo-claude doctor --help
```
```
usage: rolo-claude doctor [-h] [--work] [--json] [--probe-all] [--both]
                          [--tools] [--only GLOB]

Check the health of your rolo-claude installation.

options:
  -h, --help   show this help message and exit
  --work       Run the Databricks work-box preset (VPN reachability, token
               validity, route-split/reasoning-replay probes) instead of the
               general checks
  --json       Machine-readable output: a JSON list of {id, status, message,
               fix, see}
  --probe-all  With --work: the full work matrix -- one short pong per chat-
               shaped Databricks endpoint on its chosen path
  --both       With --probe-all: also probe Claude/GLM/Kimi endpoints via the
               anthropic gateway
  --tools      With --probe-all: also check one Read tool-call per endpoint
  --only GLOB  With --probe-all: only endpoints matching this glob
```

Bare `rolo-claude doctor` checks: Python version, `~/.claude` layout, the env
file, OpenRouter/Databricks/Claude-subscription configuration, `claude`/
`node`/`npx`/`rg`/Git Bash on PATH, the Chrome native-messaging host,
plugin-provided MCP servers, the OS/WSL/Kali platform hint, `~/.local/bin` on
PATH (Linux), tmux mouse mode (inside tmux), the three cached-catalog ages,
session count + `/improve` config, clipboard backend, configured MCP servers
(eager vs. lazy), and the resolved default model. Exit 0 unless something is
`[MISSING]` (a `[WARN]` alone, e.g. "no Databricks configured", never fails
the command).

```sh
BRIDGE_TEST_HOME=/tmp/demo-home rolo-claude doctor
```
```
rolo-claude doctor
  [OK] Python 3.11.7
  [MISSING] ~/.claude directory: /tmp/demo-home/.claude -> fix: claude
  [WARN] OpenRouter: not configured (no OPENROUTER_API_KEY found) -> fix: rolo-claude init --preset home
  [WARN] Databricks: not configured (no host/token found) -> fix: rolo-claude init --preset work
  [OK] Sessions: 0 logged under ~/.rolo-claude/sessions; stats cache last written never
  [OK] /improve: enabled=True hint=True model=(small/session model) since_days=7 max_candidates=8
  [OK] MCP servers: none configured
  [OK] Default model: not set in config.json -- built-in default 'or:deepseek/deepseek-v4.1-flash' applies (BRIDGE_MODEL/routes.json still win when set)
```
(trimmed -- a real run has one line per check; see `docs/TROUBLESHOOTING.md`
for what each WARN/MISSING line means)

`doctor --work` and `doctor --work --probe-all` are Databricks-specific --
see `docs/DATABRICKS.md`.

## `rolo-claude work-matrix`

V2b: turns a `doctor --work --probe-all` JSON report (`~/.rolo-claude/
work-matrix-<date>.json`, or one copied off the owner's real work VM -- the
report holds endpoint names only, never a host or a token) into a suggested
action per failure. See `docs/DATABRICKS.md`'s own work-matrix section for
exactly which failure classes map to which suggestion.

```sh
rolo-claude work-matrix --help
```
```
usage: rolo-claude work-matrix [-h] {show,apply} ...

Interpret a `doctor --work --probe-all` JSON report.

positional arguments:
  {show,apply}
    show        Render the report as a table with a suggested action per
                failure
    apply       Write the report's suggested per-endpoint overrides into
                config.json

options:
  -h, --help    show this help message and exit
```

### `work-matrix show <report.json>`
Prints one line per FAILING endpoint (a clean row across the board prints
"nothing to fix"): the endpoint name, its HTTP status, the issue, and the
suggested action. Never writes anything.
```sh
rolo-claude work-matrix show ~/.rolo-claude/work-matrix-2026-09-29.json
```

### `work-matrix apply <report.json> [--yes]`
Recomputes the SAME classification and writes only the one failure class that
maps onto a real config.json knob (`databricks.gateway.<endpoint>: anthropic`,
for an endpoint whose default dialect failed but a `--both`-probed anthropic-
gateway companion row answered 200) -- lists every override it's about to
write and asks for confirmation first; `--yes` skips the prompt (for
scripting/CI). The other three failure classes (403/IP, a family-wide
tool-call rule, a reasoning-replay rule) have no per-endpoint config.json
knob to write at all -- `show` reports them; fixing them is a code/model-table
change, not a config write. Never touches `~/.claude.json`/`~/.claude/
settings.json`.
```sh
rolo-claude work-matrix apply ~/.rolo-claude/work-matrix-2026-09-29.json --yes
```

## `rolo-claude models`

Lists (and refreshes) the OpenRouter and Databricks model catalogs.

```sh
rolo-claude models --help
```
```
usage: rolo-claude models [-h] [--refresh] [--cc] [--urls] [--json]

options:
  -h, --help  show this help message and exit
  --refresh   Re-probe OpenRouter/Databricks instead of using the cache
  --cc        List the Claude subscription models (cc:/ant: aliases) instead
              of the OpenRouter/Databricks catalog; with --refresh, re-pings
              each alias to confirm its current canonical id
  --urls      Databricks endpoints: also print the exact URL and path type
              each one resolves to
  --json      Machine-readable JSON output
```

| Flag | Reads/writes |
|---|---|
| (bare) | reads `~/.rolo-claude/models.json`/`dbx-endpoints.json`; refreshes automatically the first time either cache is empty |
| `--refresh` | live probe: OpenRouter `GET /api/v1/models` -> `models.json`; Databricks `GET /api/2.0/serving-endpoints` -> `dbx-endpoints.json`; models.dev's public `api.json` -> `models-dev.json` |
| `--cc` | reads `claude auth status` + the `cc-models.json` cache; `--cc --refresh` also sends nine tiny `-p --max-turns 1` pings under your subscription |
| `--urls` | Databricks rows only: adds the exact resolved URL + path type (`mlflow`/`cursor`/`anthropic`/`invocations`) per endpoint -- see `docs/DATABRICKS.md` |
| `--json` | same data as machine-readable JSON |

```sh
BRIDGE_TEST_HOME=/tmp/demo-home rolo-claude models --cc
```
```
Claude subscription: claude found but not logged in -- run `claude` once to log in
alias        cc: target               ant: target                    context   out cap    in/M     out/M
fable        claude-fable-5-1         claude-fable-5-1              1000000    128000     $10.00    $50.00
opus         claude-opus-5-5          claude-opus-5-5               1000000    128000      $4.00    $20.00
...
```

An unresolvable `--model` elsewhere in the CLI prints a near-miss suggestion
sourced from whatever this command last cached (`difflib`-style, cutoff
0.5) -- keeping the cache fresh with `rolo-claude models --refresh` makes
that suggestion useful.

## `rolo-claude mcp`

Inspect and edit MCP server configuration -- reads/writes the exact same
`~/.claude.json`/`.mcp.json` files `claude mcp add`/`claude mcp list` do, so
a server either CLI adds is immediately visible to the other.

```sh
rolo-claude mcp --help
```
```
Usage: rolo-claude mcp [options] [command]

Commands:
  list                    List configured MCP servers with live health
  get <name>              Get details about an MCP server
  add [options] <name> <commandOrUrl> [args...]  Add a server
  add-json <name> <json>  Add a server via JSON
  remove <name>           Remove a server
```

### `mcp list [--cwd DIR]`
Builds a real (temporary) MCP manager against the resolved config and prints
one line per server, same format as the real `claude` binary:
`<name>: <command> <args> - <status>`, or `<name>: <url> (HTTP|SSE) - <status>`.
Status labels: `✔ Connected`, `✗ Failed to connect`, `! Needs authentication`,
`⏸ Pending approval`, `- Not configured`, `! Connected · tools fetch failed`,
and rolo-claude's own `◐ Cached (connects on first use)` for a lazy server
that hasn't connected yet (Claude Code has no lazy-start concept, so there is
no binary-derived wording for this one state).
```sh
rolo-claude mcp list
```
```
Checking MCP server health...
No MCP servers configured.
```

### `mcp get <name> [--cwd DIR]`
Prints one server's Scope/Status/Type/Command-or-URL/Instructions/Error/Tool
count. Exit 1, `No MCP server found with name: <name>`, if unconfigured.

### `mcp add [-t stdio|sse|http] [-s local|user|project] [-e KEY=VALUE ...] [-H 'Key: Value' ...] <name> <commandOrUrl> [args...]`
Writes a new server entry. `-s local` (default) -> `~/.claude.json`'s
`projects[cwd].mcpServers`; `-s user` -> `~/.claude.json`'s top-level
`mcpServers`; `-s project` -> `<cwd>/.mcp.json`. A stdio entry always writes
`"env": {}` even with no `-e`, byte-for-byte matching the real `claude mcp
add`. A `--` separator lets the command's own flags follow without argparse
mistaking them for `mcp add`'s own:
```sh
rolo-claude mcp add my-server -- npx -y @some/mcp-server --flag
```
```
Added stdio MCP server 'my-server' (local scope) to ~/.claude.json: npx -y @some/mcp-server --flag
```

### `mcp add-json [-s, --scope local|user|project] <name> <json>`
Same storage path as `mcp add`, but the entry is a literal JSON object
string:
```sh
rolo-claude mcp add-json my-remote '{"type":"http","url":"https://example.com/mcp"}'
```

### `mcp remove [-s, --scope local|user|project] <name>`
Without `-s`, tries `local`, then `user`, then `project`, removing from the
first scope where the name is found.

### Not-yet `mcp` subcommands
`add-from-claude-desktop`, `login`, `logout`, `reset-project-choices`,
`serve` all parse and print `rolo-claude: mcp <sub> is not supported yet
(planned: H8)`, exit 0.

## `rolo-claude config`

Reads and writes **this harness's own** small store,
`~/.rolo-claude/config.json` -- never Claude Code's `settings.json`, which
this project only ever reads. See `docs/CONFIG.md` for every key this file
can hold.

```sh
rolo-claude config --help
```
```
Usage: rolo-claude config [list|get <key>|set <key> <value>]
```

```sh
BRIDGE_TEST_HOME=/tmp/demo-home rolo-claude config list
```
```
(no config set -- ~/.rolo-claude/config.json is empty or missing)
```
```sh
BRIDGE_TEST_HOME=/tmp/demo-home rolo-claude config set model or:deepseek/deepseek-v4.1-flash
```
```
model="or:deepseek/deepseek-v4.1-flash"
```
```sh
BRIDGE_TEST_HOME=/tmp/demo-home rolo-claude config get model
```
```
"or:deepseek/deepseek-v4.1-flash"
```

A key may be dotted (`improve.model`) to read/write a nested value without
disturbing its siblings; `rolo-claude config key=value` (one argv token) is
accepted as shorthand for `config set key value`. The value is parsed as
JSON when possible (`true`, `123`, a quoted string), so non-string values
round-trip too.

## `rolo-claude stats`

A headless, cross-session version of the TUI's own `/stats` -- aggregates
tokens/cost/tool-calls straight from the existing session `.jsonl` logs,
never a new model-visible field.

```sh
rolo-claude stats --help
```
```
usage: rolo-claude stats [-h] [--all] [--all-projects] [--cwd DIR] [--json]
                         [--models] [--tools] [--roles] [--wide]
                         [--since SINCE] [--session ID]

Aggregate tokens/cost/tool-calls across session logs (headless /stats).

options:
  -h, --help      show this help message and exit
  --all           Aggregate every project's sessions, not just the current
                  directory's
  --all-projects  Alias for --all
  --cwd DIR       Project directory to aggregate (default: the current
                  directory; ignored with --all)
  --json          Print machine-readable JSON instead of a text report
  --models        Show the richer per-(model,provider) telemetry table
                  (repairs, edit failures, ttft/latency, ...)
  --tools         Show the per-tool telemetry table
  --roles         Show sub-agent spend per role
                  (orchestrator/coder/reviewer/researcher/small)
  --wide          Show every --models column instead of the terminal-fit
                  compact default
  --since SINCE   Time window for --models/--tools: "all", or "<N>d" (e.g.
                  "1d", "7d", "30d"; default 7d). Ignored by the plain report
                  below.
  --session ID    Scope to one session id
```

Bare `rolo-claude stats` (no `--models`/`--tools`/`--roles`) is the original,
cheap report: turns, total cost, per-model token/cost totals, per-tool call
counts -- works even on a session log recorded before the richer telemetry
fields existed. `--models`/`--tools` switch to `rolo_claude.telemetry`'s
aggregation (cached at `~/.rolo-claude/stats-cache.json`, keyed by
path+size+mtime so an unchanged log is never re-parsed): per-(model,
provider) sessions/calls/tokens/cost/avg-latency/tool-error%/repair-hit%/
edit-failure%/steers/compactions/loop-breaker-trips (14 columns by default,
`--wide` adds route/cached-tokens/avg-ttft/finish=length%/retries/overflow/
interrupts -- columns are dropped right-to-left to fit the terminal width,
never wrapped or truncated when piped to a file). `--roles` (V2c/H15): sub-
agent spend (sessions/calls/tokens/cost) per role name, summed from every
Agent-tool call's own rolled-up usage node -- see `docs/ROLES.md`.

```sh
BRIDGE_TEST_HOME=/tmp/demo-home rolo-claude stats
```
```
rolo-claude stats (/path/to/project, 0 session(s)):
  Turns: 0
  Total cost: $0.0000
```

## `rolo-claude improve`

The headless surface for the same human-gated loop the TUI's `/improve`
drives interactively -- clusters recent failures, drafts up to 8 candidate
memory/rule/skill files with one model call, and **never writes anything
without `--apply`**.

```sh
rolo-claude improve --help
```
```
usage: rolo-claude improve [-h] [--since {7d,30d,all}] [--all-projects]
                           [--json] [--out FILE] [--apply FILE#ID] [--cwd DIR]
                           [--bare] [--max-candidates MAX_CANDIDATES]

Human-gated self-improvement: draft memory/rule/skill candidates from recent
session failures (never applied without --apply).

options:
  -h, --help            show this help message and exit
  --since {7d,30d,all}
  --all-projects
  --json
  --out FILE
  --apply FILE#ID       Write exactly this candidate (repeatable). Skips
                        drafting.
  --cwd DIR
  --bare                Disable everything -- no scan, no draft, no apply
  --max-candidates MAX_CANDIDATES
```

Bare `rolo-claude improve` scans, drafts (one model call -- `improve.model`
config, else the small model, else the session model), prints every
candidate, and saves them to `~/.rolo-claude/improve/<timestamp>.json`:

```sh
BRIDGE_TEST_HOME=/tmp/demo-home rolo-claude improve --bare
```
```
rolo-claude improve: --bare disables /improve entirely.
```

`--apply <file>#<id>` (repeatable) writes exactly that saved candidate to
disk -- the only way a headless invocation ever writes a memory/rule/skill
file. `--json`/`--out FILE` are for scripting; `--all-projects` scans every
project's sessions instead of just the current directory's.

## `rolo-claude export`

A headless version of the TUI's own `/export` -- writes a session's raw
JSONL transcript to a file or stdout, optionally sanitized.

```sh
rolo-claude export --help
```
```
usage: rolo-claude export [-h] [--session ID] [--sanitize] [-o FILE]
                          [--cwd DIR]

Export a session's transcript as JSONL (headless /export).

options:
  -h, --help            show this help message and exit
  --session ID          Session id or unique prefix (default: the latest
                        session for this directory)
  --sanitize            Redact common credential shapes (API keys, tokens,
                        secret env var values) before writing
  -o FILE, --output FILE
                        Write to FILE instead of stdout
  --cwd DIR             Project directory whose sessions to look in (default:
                        the current directory)
```

```sh
BRIDGE_TEST_HOME=/tmp/demo-home rolo-claude export
```
```
rolo-claude export: no sessions found for this directory
```

`--sanitize` redacts (never just masks) OpenRouter/Anthropic/Databricks/
GitHub/AWS-shaped tokens, quoted or JSON-encoded `export KEY="value"`/
`"KEY": "value"` assignments (by exact known name, and generically for any
`*_API_KEY`/`*TOKEN*`/`*SECRET*`-shaped name), and `Bearer <token>` headers,
by round-tripping each log node through JSON text -- the same sanitizer the
TUI's own `/export --sanitize` calls, so the two can never disagree.

## `rolo-claude proxy`

The original `claude-bridge`: a single-file HTTP proxy (`bridge.py`) that
sits in front of the **real** `claude` binary and translates its
Anthropic-Messages-API calls to OpenRouter/Databricks, leaving `claude`'s
own subscription, config, hooks and permissions completely untouched. Kept
unchanged, as its own subcommand, for anyone who wants the real `claude` CLI
itself (its own update cadence, its own bug-for-bug behavior) driven by a
non-Anthropic model, rather than rolo-claude's own agent loop.

```sh
rolo-claude proxy --help
```
```
usage: claude-bridge [-h] [--serve] [--port PORT] [--state-dir STATE_DIR]
                     [--config] [--stop] [--version] [--probe]

options:
  -h, --help            show this help message and exit
  --serve               Run the bridge server
  --port PORT           Port to bind (default: 8787)
  --state-dir STATE_DIR
                        State directory path
  --config              Print configuration and exit
  --stop                Stop a running server
  --version             Print version and exit
  --probe               Probe Databricks/OpenRouter reachability and cache
                        model list
```

| Form | What |
|---|---|
| `rolo-claude proxy launch [claude flags...] [prompt]` | starts (or reuses) the proxy server, then execs the **real** `claude` binary pointed at it -- every argument after `launch` is forwarded to `claude` verbatim, including `-h`/`--help` (so `rolo-claude proxy launch --help` prints the real Claude Code help, not this project's own) |
| `rolo-claude proxy --serve [--port PORT] [--state-dir DIR]` | runs the proxy server in the foreground |
| `rolo-claude proxy --probe` | outbound reachability check for both providers, caches the OpenRouter model list |
| `rolo-claude proxy --config` | resolved configuration as JSON, secrets redacted |
| `rolo-claude proxy --stop` | shuts down a running proxy server |

```sh
BRIDGE_TEST_HOME=/tmp/demo-home rolo-claude proxy --probe
```
```
Databricks: not configured
OpenRouter: not configured (no OPENROUTER_API_KEY)
```

State lives in `~/.claude-bridge/` (`BRIDGE_STATE_DIR` to override),
loopback-only bind, bearer-token-gated, redacted logs -- see
`docs/harness/` (via `docs/harness/README.md`) for the proxy's own
historical design notes (request translation, the 128-tool cap,
context-overflow rewriting) if you need the low-level detail; they are
unchanged from before this project became a standalone harness and are not
re-derived on this page.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | the command ran and something it reported on failed (a `-p` turn ended in error, `doctor` found a `[MISSING]`, `improve --apply` on a bad spec) |
| `2` | usage/config error (bad flag value, no prompt given, an invalid `--model`) |
| `130` | `Ctrl+C` during a `-p` turn |
| `129` | `SIGHUP` (a closed terminal/dropped SSH connection) during any run |
| `143` | `SIGTERM` |
