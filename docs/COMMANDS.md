# Commands

Every CLI subcommand `halo` accepts, and every flag any of them
accepts, verified against the real `--help` output and the argparse
definitions in `halo_harness/cli.py` and each subcommand's own module. For
the in-app `/slash` commands, key bindings, `@file` and `!cmd` prefixes, see
[SLASH-COMMANDS.md](SLASH-COMMANDS.md); for the architecture behind these
commands, see [ARCHITECTURE.md](ARCHITECTURE.md).

**How this page is organised (and kept honest)**: every flag/subcommand
heading below wraps the exact token in backticks, e.g. `#### \`--model\``
or `## \`halo doctor\``. `tests/test_docs_commands.py` calls every
real `--help` in-process (no subprocess, no network) and fails the suite if
a flag/subcommand exists in the code but isn't mentioned here, or if a
`` `-x`/`--xxx` `` token inside a heading here doesn't match a real one --
so this page cannot silently drift from `cli.py` again in either direction.
A `[not-yet]` flag parses (never an argparse error) but its feature isn't
built; see [Flags parsed but not implemented yet](#flags-parsed-but-not-implemented-yet).

Every worked example below uses `--demo` (a scripted turn needing no
network, API key, or model) or a scratch `BRIDGE_TEST_HOME` so it is safe to
paste into a shell verbatim; swap in your own model/prompt once you have a
provider configured (`halo init`).

```sh
export PYTHONPATH=/path/to/halo-harness   # a dev checkout; skip if installed
halo --version
```
```
halo 1.0.1
```

There is no `sessions` subcommand in this build -- session resume/fork/
rename/list lives entirely under the default command's own `-c`/`-r`/
`--fork-session`/`-n` flags and the TUI's `/resume`/`/rename`/`/fork` (see
`docs/SLASH-COMMANDS.md`).

## `halo` (the main command)

```
halo [PROMPT] [flags...]
```

With no `-p`/`--print`, `halo` opens the full-screen TUI (see
`docs/SLASH-COMMANDS.md`); a `PROMPT` positional argument pre-fills it as the
first turn. `-p`/`--print` runs one headless turn instead and exits -- this
is what every worked example on this page uses. Outside a real terminal
(piped stdin, cron, a subprocess with no tty), the bare (non-`-p`) form
prints one line and exits 2 rather than hanging, since nothing could ever
drive a full-screen UI there:

```sh
echo | halo
```
```
halo: a full-screen session requires an interactive terminal (stdin is not a tty) -- use -p/--print for a non-interactive run
```
(exit code 2)

A bare `-p` positional prompt, or stdin (UTF-8, 10 MB cap) when no positional
is given, is the text of the first turn:

```sh
halo --demo -p
```
```
All done -- that's a scripted demo turn.
```

```sh
halo --demo -p --output-format json
```
```json
{"type": "result", "subtype": "success", "is_error": false, "result": "All done -- that's a scripted demo turn.", "session_id": "demo-session", "num_turns": 1, "stop_reason": "end_turn", "usage": {"input_tokens": 270, "output_tokens": 100}, "total_cost_usd": 0.0, "structured_output": null, "model": "or:deepseek/deepseek-v4.1-flash", "permission_denials": [], "background_notices": []}
```

An unresolvable `--model` is a clean, scriptable usage error (exit 2), with a
near-miss suggestion when the OpenRouter catalog is cached (`halo
models --refresh`) and something close matches:

```sh
halo -p --model not-a-real-model-ref "hi"
```
```
halo: invalid --model: no route: 'not-a-real-model-ref' (accepted forms are dbx:, or:, ant:, cc:, vendor/model, a bare databricks-*/system.ai.* name, a subscription-model alias, or a routes.json alias)
```

Exit codes across every route: `0` success, `1` the turn ran and ended in an
error (upstream failure, budget exceeded), `2` a usage/config error (bad
flag value, no prompt given, invalid model), `130` `Ctrl+C` during a `-p`
turn (POSIX `128+SIGINT`).

### Model & session flags

#### `--model MODEL`, `--small-model MODEL`
What: the main model for this session, and the model used for
background/small tasks (title generation, `/improve` drafting when
`improve.model` isn't set) respectively. Reads: `~/.halo/config.json`
(`model` key), `routes.json`'s `aliases`/`default` and `small`, `HALO_MODEL`
(legacy `BRIDGE_MODEL`) env, only when neither flag is given -- see `docs/MODELS.md` for the full
resolution order. Default: `or:deepseek/deepseek-v4.1-flash` (see
`model.DEFAULT_MODEL_REF`) when nothing else resolves.
```sh
halo -p --model or:deepseek/deepseek-v3.2 --small-model or:deepseek/deepseek-v3.2 "hi"
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
`~/.halo/sessions/<slug>/*.jsonl`.
```sh
halo -p -c "keep going"
```

#### `-r`, `--resume [ID_OR_TEXT]`
What: with an id (or unique id prefix), resumes that exact session; with
free text, resumes the one session that text uniquely matches by title/first
prompt/cwd/model, or -- in print mode -- lists the ambiguous candidates
instead of guessing; with nothing at all, opens the TUI's session picker
(pre-filtered if this flag's own value made the CLI resume ambiguous).
```sh
halo -p -r a1b2c3d4 "one more thing"
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
Writes: `~/.halo/sessions/<slug>/index.json`.

#### `--agent AGENT`, `--agents JSON_OR_FILE`
What: `--agent` pins this top-level session to run as if it were the named
sub-agent (its own tool/model/permission-mode restrictions apply from turn
one); `--agents` adds extra agent definitions inline -- a literal JSON object
string in the TUI, or (in `-p`) either a literal JSON string or a path to a
JSON file holding `{name: {description, prompt, tools, model, ...}}`. See
`docs/CONFIG.md`'s agents section for the full discovery precedence these
sit on top of.

#### `--role NAME=MODEL[:EFFORT]`

What: overrides one of the ten built-in roles (`orchestrator`, `planner`,
`coder`, `reviewer`, `judge`, `researcher`, `tester`, `compaction`, `small`,
`subagent_default`) -- or any custom name a team.json/loaded template
already defines -- for this run only -- a built-in/custom sub-agent whose
own role resolves to `NAME` uses `MODEL` (and `EFFORT`, if given) instead of
whatever `~/.halo/config.json`/`team.json`'s own `roles` table (or the
cost-aware default) says, even beating that agent's own file `model:` (a
freshly-typed, run-only override the user gets to trump a shared/managed
agent file with). Repeatable (`--role coder=... --role researcher=...`); a
later repeat of the SAME role wins. A bad `NAME=MODEL` (missing `=`, an
unrecognized role name, an empty model) is a clean exit-2 usage error before
any Session is built. See `docs/ROLES.md` for the full precedence chain,
`/role`/`/roles`, role templates, and `stats --roles`.
```sh
halo -p --role researcher=or:deepseek/deepseek-v4.1-flash "use the Researcher agent to summarize this repo"
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
halo -p "summarize the attached file" --file ./notes.txt
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
halo --demo -p --output-format stream-json
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
log goes to `~/.halo/bridge.log` (rotating, secrets redacted) and the
path is printed once on stderr as `halo: debug log -> <path>`. The
optional FILTER value is accepted for Claude Code parity and ignored:
everything is logged. Use it when the TUI misbehaves or a route fails, then
send the last lines of the log.
Reads: nothing new. Writes: `~/.halo/bridge.log`.
```sh
halo --debug
tail -60 ~/.halo/bridge.log
```

#### `--debug-file PATH`
What: same as `--debug`, but the log is written to `PATH` (parent
directories are created) instead of the state directory.
```sh
halo --debug-file /tmp/halo-debug.log -p "reply with the single word pong"
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
halo -p --setting-sources user "hi"
```

#### `--permission-mode {default,acceptEdits,plan,auto,dontAsk,bypassPermissions,manual}`
What: the session's starting permission mode (`manual` is a display alias
for `default`). See `docs/ARCHITECTURE.md`'s permissions section for exactly
what each mode allows/asks/denies -- there is no classifier or heuristic
layered on top of any of them.
```sh
halo -p --permission-mode auto "hi"
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
file would otherwise enable it by default. `halo doctor` reports
whether the native host is registered at all.

#### `--playwright`, `--playwright-cdp ENDPOINT`, `--playwright-headless`
What: `--playwright` drives a real local Chromium/Chrome/Brave via
Playwright as a dynamic MCP server instead; `--playwright-cdp` attaches to
an already-running browser's DevTools Protocol endpoint instead of
launching a new one; `--playwright-headless` runs without a visible window.
Needs `node`/`npx` on PATH (`halo doctor` checks for both).

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
halo -p --strict-mcp-config --mcp-config '{"mcpServers":{}}' "hi"
```

### Image flags

#### `--no-inline-images`
What: forces the plain type/size/dimensions caption for every image tool
result this run, same as `images: "caption"` in `~/.halo/config.json`
but scoped to one invocation (TUI only -- print mode has no inline-image
concept to disable).

### Misc flags

#### `--theme THEME`
What: one of `claude-dark`/`claude-light` (`-daltonized`/`-ansi` variants of
each) for this run; `/theme` persists a choice for future sessions.

#### `--no-intro`
What: skips the one-time `I am just a copy, of a copy, of a copy... halo
<version>` typewriter line a fresh interactive launch otherwise shows
above the first turn -- same effect as `"intro": false` in
`~/.halo/config.json`. Never shown in print mode, a `--demo` run, or when
stdout isn't a real terminal, with or without this flag; `/intro` replays
it mid-session.

#### `--demo`
What: runs (or, with `-p`, prints) a scripted walkthrough turn that needs no
network, no API key and no configured model -- the safe example used
throughout this page and the README's own 10-minute walkthrough.

#### `--stress N`
What: repeats the demo script's own synthetic load N times (a throughput/UI
smoke test, not a real benchmark).

#### `-v`, `--version`
What: prints `halo <version>` and exits 0, before any config is read.

### Flags parsed but not implemented yet

Every flag listed here would be accepted by the argument parser (never an
"unrecognized arguments" error) and, the first time its value differs from
"never touched at all", would print one line to stderr naming the milestone
it's planned for, then continue the run as if the flag had not been given.
As of 2.0.1 (W4a) the table is **empty** -- every flag that used to be here
is now either a real flag (below) or declared not applicable to a
standalone harness (also below). The mechanism itself stays, ready for
whatever a future Claude Code release adds that genuinely isn't built here
yet; the canonical, always-current list is `halo_harness/cli.py`'s own
`_NOT_YET_FLAGS` table -- this page's test (`tests/test_docs_commands.py`)
fails if the two ever disagree.

### Flags not applicable to a standalone harness

These 7 flags are accepted (never an "unrecognized arguments" error) and,
the first time given, print one line to stderr naming the reason, then
continue the run as if the flag had not been given -- never "not supported
yet", since there is no future milestone that would change the answer:

```sh
halo --ide -p "hi"
```
```
halo: --ide is not applicable to a standalone harness (needs Anthropic's IDE extension protocol, which halo does not implement)
```

| Flag | Reason |
|---|---|
| `--cloud [DESCRIPTION_OR_ID]` | halo has no cloud session service -- every session runs on this machine |
| `--teleport [SESSION]` | teleport sessions are a claude.ai cloud feature halo has no equivalent of |
| `--remote-control [NAME]` | Remote Control pairs a session with the claude.ai mobile/web app, which halo does not integrate with |
| `--remote-control-session-name-prefix PREFIX` | only meaningful alongside `--remote-control`, which is not applicable here |
| `--from-pr [VALUE]` | resuming a session linked to a PR is a claude.ai cloud-session feature halo does not have |
| `--ide` | needs Anthropic's IDE extension protocol, which halo does not implement |
| `--safe-mode` | halo has no safety heuristics to disable by design -- accepted for compatibility, no effect |

### New in 2.0.1 (W4a)

The 21 flags below were "not supported yet" before 2.0.1 and are real now.

#### `--allow-dangerously-skip-permissions`
What: an alias for `--dangerously-skip-permissions` -- Claude Code's own
name for "enable bypass as an option, without it being on by default".
Identical behaviour either way in halo.

#### `--autocompact {auto|TOKENS}`
What: overrides the auto-compaction trigger window. `auto` (or omitting the
flag) keeps the normal 80%-of-headroom rule; a token count (`150000`,
`150k`) sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW` for this run, the same env
var `/compact`'s own trigger math already reads.

#### `--ax-screen-reader`
What: runs a plain, line-oriented interactive loop instead of the
full-screen Textual UI -- flat text, no borders/animations/spinners, one
line per event. Permission/question/plan asks become a `y`/`n` or numbered
prompt read from stdin. Not the TUI with styling stripped; a genuinely
separate, simpler renderer (`halo_harness/ax_mode.py`) driving the same
`Controller` the real TUI uses.

#### `--bg`, `--background`
What: spawns this same invocation (forced to `-p`, since a detached process
has no terminal a TUI could render into) as a background process; prints
its id and a log file path, then returns immediately. Manage it afterward
with `halo bg list|logs|stop|rm` (below) -- the OS's own process tools
(`kill`, Task Manager, ...) still work too, nothing here is exclusive.

#### `--betas BETA [BETA ...]`
What: one or more Anthropic beta feature names, sent as a comma-joined
`anthropic-beta` request header -- only on an Anthropic-family route
(`ant:`, or a Databricks Claude foundation model specifically, i.e. the
`anthropic-passthrough` dialect); accepted but never sent (no error) on
any other route, including an ordinary Databricks-hosted chat-dialect
model and OpenRouter.

#### `--brief`
What: adds the `SendUserMessage` tool to the session's catalog -- lets the
model send a short status update without ending its turn, for terser
running commentary instead of one long reply at the end.

#### `--environment KEY=VALUE`
What: repurposed for a standalone harness -- Claude Code's own
`--environment <id>` runs a cloud session on a self-hosted environment,
which does not exist here. One or more `KEY=VALUE` pairs (repeatable),
merged into the tool child environment (so `Bash`/`PowerShell` see them)
after the settings-env chain, so an explicit flag wins.

#### `--exclude-dynamic-system-prompt-sections`
What: accepted; halo's own system-prompt assembly already keeps the
per-machine sections (cwd, git status, environment) as a separate snapshot
block rather than baking them into the static system prompt, so this flag
describes behaviour halo already has by default.

#### `--fallback-model MODEL`
What: one or more comma-separated fallback models, tried when the primary
is exhausted-by-retries within a turn; reset to the primary fallback LIST at
the start of each new user turn. Wired into the live model-call retry
ladder (`agent/loop.py::Session._step`/`_try_fallback_after_exhaustion`): a
provider failure (the primary's retry ladder exhausted on a retryable
5xx/429-class error) swaps onto the next entry for the rest of the turn,
with one `notification` event (visible in the transcript and in
`--output-format stream-json`) naming the model that failed and the one
switched to; an entry that fails to resolve, or whose tool catalog is too
small for the session, is skipped in favour of the next one.

#### `--forward-subagent-text`
What: accepted; `--output-format stream-json` already forwards a
sub-agent's own text/thinking/tool blocks as `assistant`/`user` lines with
`parent_tool_use_id` set, unconditionally -- a strictly more-forthcoming
default than Claude Code's own opt-in, kept rather than gated behind this
flag to avoid a behaviour-breaking change for zero gain.

#### `--include-hook-events`
What: with `--output-format stream-json`, interleaves one
`{"type": "system", "subtype": "hook_event", ...}` line per hook
invocation (event name, whether it blocked, its permission decision) among
the turn's own lines, in the order they actually happened.

#### `--no-session-persistence`
What: only with `-p`. The turn still runs and logs normally, but its
session log file/directory and `index.json` entry are deleted before the
process exits -- nothing survives to be `-c`/`-r`-resumed later.

#### `--permission-prompt-tool TOOL`
What: `TOOL` is an MCP tool's full catalog name (`mcp__<server>__<tool>`).
In print mode, an "ask" decision that would otherwise be auto-denied is
instead sent to this tool (`{tool_name, tool_input, reason}`); a
`{"behavior": "allow"|"deny"}` reply (or bare `allow`/`deny` text) decides
it. No reply, an unreachable tool, or `--permission-prompts none` falls
back to the ordinary auto-deny.

#### `--permission-prompts {host,none}`
What: `none` (the default either way) auto-denies every print-mode ask, as
before. `host` consults `--permission-prompt-tool` when one is given.

#### `--plugin-dir PATH`
What: loads a plugin's agent definitions from `PATH` for this session only
(repeatable). Also wired into skill, hook and MCP server discovery for
this session (`commands/registry.py`, `hooks.load_plugin_hooks`,
`mcp_setup.build_manager`), plus a model-invoked `Skill` tool call via
`Session.plugin_roots`/`ToolContext.plugin_roots` -- the same sources an
*installed* plugin's skills/hooks/MCP servers (`config/plugins.py`) already
read from, now extended to cover a CLI-supplied directory too.

#### `--plugin-url URL`
What: like `--plugin-dir`, but `URL` is a git URL, shallow-cloned once into
`~/.halo/plugins-cache/` and reused on later launches (never re-cloned).
Claude Code's own `--plugin-url` fetches a `.zip`; halo takes the simpler,
already-everywhere git clone instead.

#### `--prompt-suggestions [true|false]`
What: the single-turn `-p TEXT` path AND (W5, carried from W4a)
`--input-format stream-json`'s own multi-turn loop, one suggestion per
turn. After each turn, one extra small-model call predicts the user's
likely next message; `text` output
prints it as a trailing `[next: ...]` line, `json` adds a
`prompt_suggestion` field, `stream-json` emits its own
`{"type": "prompt_suggestion", ...}` line. Costs one extra model call --
only when you ask for it.

#### `--restricted`
What: a read-only tool set -- removes `Bash`/`PowerShell`/`WebFetch`/
`Edit`/`Write`/`NotebookEdit` and every MCP server tool from the catalog
unless named explicitly in `--tools`, and ignores
`--dangerously-skip-permissions` (falls back to the default permission mode
with a notice). Also honored by an interactive launch now, not only `-p`.
Claude Code's own additional "ignores user/project/local settings files"
and "only a person may approve settings/git/tool-config writes" rules are
not modelled this round.

#### `--system-prompt-snapshot {on,off}`
What: `on` (the default either way): halo already computes the system
prompt once per session and reuses it verbatim for every request, matching
`on`'s own documented behaviour. `off` is accepted but not distinguished
from `on` this round -- halo has no per-request system-prompt re-render to
turn off yet.

#### `--tmux [classic]`
What: when the `tmux` binary exists and this isn't already running inside
one (`$TMUX` unset), re-execs the interactive launch inside a new tmux
window. Tmux-only (no iTerm2 native-pane backend); missing tmux is a
notice, never an error.

#### `-w`, `--worktree [NAME]`
What: creates a new git worktree (on a fresh branch) under
`~/.halo/worktrees/<repo-slug>/<name-or-id>` and runs the session there
instead of the real working tree -- isolates the session's own file edits
from the repo you're actually looking at. Falls back to the current
directory (one notice, never a hard failure) outside a git repo or if `git
worktree add` itself fails. The same mechanism backs a sub-agent's own
`isolation: worktree` frontmatter key. Fires `WorktreeCreated`; the tree is
left on disk when the session ends unless `worktree.remove_on_exit` is set
(see `halo worktree`, below) -- `rm` it yourself, or with `halo worktree rm`.

## `halo init`

One command that sets up a fresh box: **select a provider to set up**
(Databricks, OpenRouter, Anthropic API, or your Claude subscription --
1.0.1 hotfix 13 replaced the old home/work/claude "preset" naming a bundle
of choices with a direct provider picker, since "work" was really just
"Databricks" wearing a confusing label), configure its credentials, pick
its default model, run `doctor`, refresh the model catalogs, send one live
"pong", and (Linux only) offer the `rg`/PATH fixes -- then offer to set up
another provider, looping until you're done; with more than one provider
configured, one last pick chooses the overall default model across all of
them. Then (1.0.1 hotfix 18) **select a default permission mode** -- `auto`
(recommended, listed first), `acceptEdits`, `default`, `plan` -- written to
`~/.halo/config.json`'s own `permission_mode` key, which every
future session (`-p` and the TUI alike) starts in unless `--permission-mode`
or `--dangerously-skip-permissions` is given that run (see
[CONFIG.md](CONFIG.md)'s "Providers" section for the full precedence
chain). Every step is idempotent -- re-running only reports what's already
correct. Never writes `~/.claude.json`/`~/.claude/settings.json`, never
prints a key or token.

**Halo 2.0.2 round 7**: on a real terminal, every interactive step above
(Providers, Default model, Permission mode) plus three more -- **Theme**
(the six built-in themes, a live preview of the transcript/status bar in
each), **Roles** and **Organizations** (see below), and **Linux fixes**
(skipped automatically when nothing needs fixing) -- run inside ONE
Textual app, `Step N of M: <name>` in the header and `Back` / `Skip` /
`Next` (`Finish` on the last step) in the footer; nothing exits to the
console between steps, and Esc asks "Quit setup? What you saved so far
stays" instead of ending the run silently. A piped/non-tty run (every
script, `--yes`, `--provider`) is unaffected -- it keeps the exact
sequential-picker/numbered-fallback behaviour the worked example below
shows.

**Halo 2.0.3 round 5**: the INTERACTIVE Providers step's tab bar gains two
more tabs beyond the four above -- `Ollama (local or LAN)` (detects/
registers the local daemon, or add a LAN/cloud host with an optional key)
and `Hugging Face` (paste `HF_TOKEN`, add a dedicated endpoint, add a
local server by URL with an optional key, or rely on auto-detection --
any subset, all optional) -- both Skippable, same Back/Skip/Next footer.
Deliberately NOT `--provider`/`--preset` CLI-flag choices (the piped/
non-tty fallback above still only ever offers the original four): neither
has one sensible hardcoded default model the way the table below's four
providers each do, so there is nothing non-interactive `--provider
ollama`/`--provider huggingface` could safely default to without a real
catalog already cached. See [MODELS.md](MODELS.md)'s Ollama/Hugging Face
sections and [CONFIG.md](CONFIG.md)'s "Providers" section.

**Halo 2.0.3 round 5b part 2**: both of those two tabs now open with a
short detection summary -- GPU/unified memory, whether Ollama is already
reachable and what it has, any running local server, and model folders
already known -- filled in by a background probe (never the UI thread; a
slow probe just leaves the "detecting..." placeholder a little longer,
the rest of the tab is usable immediately either way). For each already-
installed Ollama model it names the largest fully-resident context
(learned cap or fit estimate, labelled); for free room, one or two
illustrative model classes/quantizations that would fit with a 32k
context -- an estimate, never a download from here. See
[MODELS.md](MODELS.md)'s "The wizard detects before it asks" section.

```sh
halo init --help
```
```
usage: halo init [-h]
                        [--provider {databricks,openrouter,anthropic,claude}]
                        [--preset {home,work,claude}] [--model REF] [--yes]
                        [--no-live] [--no-fixes] [--team PATH|URL]

Set up halo in one command: pick a provider to set up, configure its
credentials, set a default model, run doctor, send a live pong, and offer the
Linux setup fixes. Repeat for another provider, then pick the overall default.

options:
  -h, --help            show this help message and exit
  --provider {databricks,openrouter,anthropic,claude}
                        set up this provider non-interactively (repeatable,
                        first-listed first); omit for the interactive provider
                        picker
  --preset {home,work,claude}
                        deprecated alias for --provider: home=openrouter,
                        work=databricks, claude=claude
  --model REF           override the provider's own default model
  --yes                 accept every default without prompting
  --no-live             skip the catalog refresh and the live pong
  --no-fixes            skip the Linux rg/PATH fixes step
  --team PATH|URL       a team.json preset (host/default model/gateway
                        preference/DBU price -- never a token); overrides
                        .halo/team.json / ~/.halo/team.json
```

| Flag | Reads/writes | Default |
|---|---|---|
| `--provider {databricks,openrouter,anthropic,claude}` | repeatable; each runs its own credentials -> catalog/discovery -> default-model-pick -> live-pong path, in the order given, with no "set up another?" prompt between them | omitted -> the interactive provider picker (arrow keys on a real terminal, a numbered list otherwise), looping until you pick "Done" |
| `--preset {home,work,claude}` | **deprecated**: a one-line-noticed alias for `--provider openrouter\|databricks\|claude` respectively -- kept so existing scripts/docs using it verbatim keep working | -- |
| `--model REF` | writes `~/.halo/config.json`'s `model` key, skipping that provider's own model picker entirely | the provider's own default (`or:deepseek/deepseek-v4.1-flash` / `dbx:databricks-deepseek-v4-1-flash` / `ant:sonnet` / `cc:sonnet`) |
| `--yes` | -- | off (interactive prompts; also skips every provider's own model picker) |
| `--no-live` | skips `models --refresh` and the live pong | off |
| `--no-fixes` | skips the `rg`/PATH steps (Linux) | off |
| `--team PATH\|URL` | reads a `team.json`-shaped file/URL (see `docs/DATABRICKS.md`); Databricks only | `.halo/team.json`, then `~/.halo/team.json` |

`--provider ... --yes` (or the deprecated `--preset ... --yes`) is fully
non-interactive whenever the needed value is already discoverable; when it
isn't (e.g. no OpenRouter key found and `--yes` was passed), it prints a
`[WARN]` naming the env var to set instead of blocking on a prompt.
Credentials go to the same env file every other part of the harness reads
(`HALO_ENV_FILE`/legacy `BRIDGE_ENV_FILE`, else `~/.config/halo/env` --
falling back to a pre-2.0.0 `~/.config/vibes-hacker/env` when that's all
there is -- mode 0600 on POSIX).
The interactive provider list shows each provider's own status tag
(`configured`/`logged in`/`not set up`) and starts the cursor on whichever
one auto-detection would have picked (an existing `OPENROUTER_API_KEY` ->
OpenRouter; else a Databricks host/`ucode-settings.json` -> Databricks; else
an `ANTHROPIC_API_KEY` -> the Anthropic API; else a `claude.ai` login ->
Claude subscription) -- detection only positions the cursor, it never
decides for you. Each provider's own default-model pick (arrow keys, type
to filter, same `ctx`/`out`/price columns documented under
[`/model`](SLASH-COMMANDS.md#model-ref)) is skipped by `--yes`/`--model`;
the interactive picker itself falls back to a numbered list with no real
terminal to draw into.

**Install once, run anywhere.** `init`'s own Summary step ends with the SAME
"command on PATH" line `doctor` prints (see above) -- when `halo`
doesn't resolve to a real installed console script, it names the exact fix
(`uv tool install --reinstall .` when `uv` is present, else `pip install
--user -e .`, both re-run after every `git pull`) and reminds you to run the
command from any directory, since the checkout itself is only ever needed
for `git pull`.

Worked example (a scratch home, so nothing real is touched):
```sh
BRIDGE_TEST_HOME=/tmp/demo-home halo init --provider openrouter --yes --no-live
```
```
halo init
OpenRouter
Credentials:
   [WARN] OpenRouter key not found, and --yes skips the prompt -- set OPENROUTER_API_KEY (or re-run `halo init` without --yes).
   Default model: or:deepseek/deepseek-v4.1-flash -- wrote /tmp/demo-home/.halo/config.json
Checks:
   ...doctor lines...
   (catalog refresh skipped: --no-live)
Live pong: skipped (--no-live)
Default permission mode:
   auto (non-interactive default; pass through settings.json or `halo config set permission_mode ...` to change it)
   Default permission mode: auto -- wrote /tmp/demo-home/.halo/config.json
Linux fixes:
   ...
Summary:
   provider(s) set up this run: openrouter
   default model: or:deepseek/deepseek-v4.1-flash
   wrote /tmp/demo-home/.halo/config.json
   Run `halo` to start.
   [WARN] halo command: not found on PATH -> fix: pip install --user -e . (run from this checkout -- repeat after every `git pull`)
   Run it from any directory -- the checkout is only for `git pull`.
```

## `halo doctor`

A read-only environment check; every `[WARN]`/`[MISSING]` line ends with
`-> fix: <command>` or `-> see: <reference>`. Never writes anything.

```sh
halo doctor --help
```
```
usage: halo doctor [-h] [--work] [--json] [--probe-all] [--both]
                          [--tools] [--only GLOB] [--local] [--model REF]

Check the health of your halo installation.

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
  --local      Run the 60-second local-model acceptance check (load, tool
               call, structured output, compaction summary) instead of the
               general checks
  --model REF  With --local: the ol:/hf:local/hf:mlx model to check (default:
               the configured default model if it is ol:, else the first
               model in the default Ollama host's catalog); an
               hf:mlx/<org>/<repo> ref starts its managed mlx_lm.server
               first if one isn't already running
```

`halo doctor --local [--model ol:x]` (round 5d; round 5f extends `--model`
to `hf:local/*`/`hf:mlx/*`) is the 60-second "works out of the box" proof
for a local model on THIS machine: load, one real Read tool call, one
structured-output call (the same constrained-decoding path the repair loop
uses), and one summary of a small fixture transcript, each printed as
`[PASS]`/`[FAIL]` with a plain reason and the elapsed time, in that fixed
order even when an earlier step failed. Exit 0 iff all four passed. An
`hf:mlx/<org>/<repo>` ref on anything but Apple Silicon fails all four
steps with the one plain "MLX runs on Apple Silicon only" reason -- still
a clean, deterministic answer, never a crash. See [MODELS.md](MODELS.md)'s
"Finding and using file-backed models"/"Apple Silicon (`hf:mlx/*`, round
5f)" sections; new local-model users are pointed at this command first.

Bare `halo doctor` checks: Python version, `~/.claude` layout, the env
file, OpenRouter/Databricks/Claude-subscription configuration, `claude`/
`node`/`npx`/`rg`/Git Bash on PATH, the Chrome native-messaging host,
plugin-provided MCP servers, the OS/WSL/Kali platform hint, `~/.local/bin` on
PATH (Linux), tmux mouse mode (inside tmux), the three cached-catalog ages,
session count + `/improve` config, clipboard backend, configured MCP servers
(eager vs. lazy), the resolved default model, how many of the six providers
are enabled (`databricks`/`openrouter`/`anthropic`/`claude_subscription`/
`typesafe`/`huggingface` -- see "`halo providers`" below), and the `halo`
command itself on PATH. Exit 0 unless something is `[MISSING]` (a `[WARN]`
alone, e.g. "no Databricks configured", never fails the command).

**Install once, run anywhere.** The "command on PATH" check fails WARN when
`halo` either isn't found at all or resolves to this checkout's own
`bin/` wrapper (which only works from inside the checkout) -- its own
`-> fix:` names the exact reinstall command for whichever tool is on this
box: `uv tool install --reinstall .` (run from the checkout) when `uv` is
present, else `pip install --user -e .`. Either one needs re-running after
every `git pull` -- an editable/tool install does not auto-refresh the
installed console script on its own.

```sh
BRIDGE_TEST_HOME=/tmp/demo-home halo doctor
```
```
halo doctor
  [OK] Python 3.11.7
  [MISSING] ~/.claude directory: /tmp/demo-home/.claude -> fix: claude
  [WARN] OpenRouter: not configured (no OPENROUTER_API_KEY found) -> fix: halo init --preset home
  [WARN] Databricks: not configured (no host/token found) -> fix: halo init --preset work
  [OK] Sessions: 0 logged under ~/.halo/sessions; stats cache last written never
  [OK] /improve: enabled=True hint=True model=(small/session model) since_days=7 max_candidates=8
  [OK] MCP servers: none configured
  [OK] Default model: not set in config.json -- built-in default 'or:deepseek/deepseek-v4.1-flash' applies (HALO_MODEL/routes.json still win when set)
  [OK] Providers: 0/6 enabled (none)
  [WARN] halo command: not found on PATH -> fix: pip install --user -e . (run from this checkout -- repeat after every `git pull`)
```
(trimmed -- a real run has one line per check; see `docs/TROUBLESHOOTING.md`
for what each WARN/MISSING line means)

`doctor --work` and `doctor --work --probe-all` are Databricks-specific --
see `docs/DATABRICKS.md`.

## `halo update`

Checks the installed build (PEP 610 `direct_url.json`, or `git` in a live
checkout) against what's available upstream (cached 24h in
`~/.halo/update-check.json`), and, unless `--check`, applies it. See
"Update" in `docs/INSTALL.md` for the full walkthrough; `/update` inside
the TUI is the same check with an update-and-restart dialog.

```sh
halo update --help
```
```
usage: halo update [-h] [--check] [--to TAG_OR_BRANCH_OR_COMMIT]
                   [--channel {stable,main}] [--force] [--refresh]

Check for, or apply, a halo update.

options:
  -h, --help            show this help message and exit
  --check               Report only -- never reinstalls
  --to TAG_OR_BRANCH_OR_COMMIT
                        Pick an exact revision instead of the channel's latest
  --channel {stable,main}
                        stable tracks the newest v* tag, main tracks the
                        branch halo came from
  --force               Reinstall even if another halo process looks like it's
                        running
  --refresh             Ignore the 24h cache, always check live
```

`halo update --check` prints installed vs. available, up to 15 commits
between them, and the exact reinstall command this box would run, then
exits 0 (up to date), 10 (an update is available), or 1 (couldn't tell --
offline, no git, rate-limited). Without `--check`, it refuses (exit 1) if
another `halo` process looks like it's running on this machine (excluding
itself) unless `--force` is also given -- a reinstall under a running TUI
has broken the install on Windows before; close other sessions first, or
pass `--force` once you're sure none is actually using the install. On
success it runs the reinstall command live (output visible) and reports
the before/after commit by re-running `halo --version` as a fresh process.

```sh
halo update --check
```
```
installed: halo 2.0.2 (c93480d, master)
available (main): e1f2a3b (master)
3 commit(s):
  e1f2a3b fix: ...
  ...
update command: uv tool install --reinstall git+https://github.com/roloVibes/Halo-Harness
```

## `halo providers`

```
Usage: halo providers [list|enable <name>|disable <name>|setup <name>]
Providers: databricks, openrouter, anthropic, claude_subscription, typesafe, huggingface
```

Detected credentials/a real claude.ai login AUTO-enable a provider (H15
part 2 addendum) -- OpenRouter/Anthropic API (key)/TypeSafe once their key
is found (env file, shell env, or the settings env chain), Databricks once
a host AND token are found (same sources, plus `~/.databrickscfg`), Claude
Code subscription (`cc:`) ONLY when `claude auth status` reports
`loggedIn` with `authMethod` exactly `claude.ai`, Hugging Face (Halo 2.0.3
round 4) once `HF_TOKEN` is found OR at least one `huggingface.endpoints`
entry is configured. No `halo init` run
is required for this. `~/.halo/config.json`'s `"providers"` block
stores OVERRIDES only -- `enable <name>`/`disable <name>` write an explicit
`true`/`false` there that always wins over auto-detection (`cc`/`ant`/
`dbx`/`or`/`hf` are accepted aliases for the canonical names); `setup <name>`
needs a real terminal and runs the same per-provider tab `halo init`
shows.

Bare `halo providers` (or `list`) prints one row per provider --
status (`auto (detected from <source>)` / `disabled by you` / `enabled by
you` / `not set up`), reachable, cached model count -- plus a trailing
OpenRouter balance line once a background fetch has ever succeeded (see
`halo models`/`/cost`). See `docs/MODELS.md`'s "Provider
enablement" section for the full prefix/label table.

```sh
halo providers
```
```
provider                    status                                   reachable                                  models
Databricks                  not set up                               not set up                                      -
OpenRouter                  auto (detected from env file / shell env) reachable                                     412
Anthropic API (key)         not set up                                not set up                                      -
Claude Code subscription    not set up                                not set up                                      -
TypeSafe                    not set up                                not set up                                      -
```

## `halo work-matrix`

V2b: turns a `doctor --work --probe-all` JSON report (`~/.halo/
work-matrix-<date>.json`, or one copied off the owner's real work VM -- the
report holds endpoint names only, never a host or a token) into a suggested
action per failure. See `docs/DATABRICKS.md`'s own work-matrix section for
exactly which failure classes map to which suggestion.

```sh
halo work-matrix --help
```
```
usage: halo work-matrix [-h] {show,apply} ...

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
halo work-matrix show ~/.halo/work-matrix-2026-09-29.json
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
halo work-matrix apply ~/.halo/work-matrix-2026-09-29.json --yes
```

## `halo models`

Lists (and refreshes) the OpenRouter and Databricks model catalogs.

```sh
halo models --help
```
```
usage: halo models [-h] [--refresh] [--cc] [--urls] [--json]

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
| (bare) | reads `~/.halo/models.json`/`dbx-endpoints.json`; refreshes automatically the first time either cache is empty |
| `--refresh` | live probe: OpenRouter `GET /api/v1/models` -> `models.json`; Databricks `GET /api/2.0/serving-endpoints` -> `dbx-endpoints.json`; models.dev's public `api.json` -> `models-dev.json` |
| `--cc` | reads `claude auth status` + the `cc-models.json` cache; `--cc --refresh` also sends nine tiny `-p --max-turns 1` pings under your subscription |
| `--urls` | Databricks rows only: adds the exact resolved URL + path type (`mlflow`/`cursor`/`anthropic`/`invocations`) per endpoint -- see `docs/DATABRICKS.md` |
| `--json` | same data as machine-readable JSON |

```sh
BRIDGE_TEST_HOME=/tmp/demo-home halo models --cc
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
0.5) -- keeping the cache fresh with `halo models --refresh` makes
that suggestion useful.

## `halo mcp`

Inspect and edit MCP server configuration -- reads/writes the exact same
`~/.claude.json`/`.mcp.json` files `claude mcp add`/`claude mcp list` do, so
a server either CLI adds is immediately visible to the other.

```sh
halo mcp --help
```
```
Usage: halo mcp [options] [command]

Commands:
  list                    List configured MCP servers with live health
  get <name>              Get details about an MCP server
  add [options] <name> <commandOrUrl> [args...]  Add a server
  add-json <name> <json>  Add a server via JSON
  remove <name>           Remove a server
  add-from-claude-desktop Import MCP servers from Claude Desktop's config
  reset-project-choices   Reset approved project-scoped (.mcp.json) servers
  serve                   Expose halo's own built-in tools as an MCP server
  login <name>            OAuth-authenticate a remote MCP server
  logout <name>           Clear stored OAuth credentials for a server
  fix <name> [--apply]    Diagnose (and, with --apply, fix) a failed server
  test <name>             A tools/list round trip with timing
```

### `mcp list [--cwd DIR] [--refresh]`
Builds a real (temporary) MCP manager against the resolved config and prints
one line per server, same format as the real `claude` binary:
`<name>: <command> <args> - <status>`, or `<name>: <url> (HTTP|SSE) - <status>`.
Status labels: `✔ Connected`, `✗ Failed to connect`, `! Needs authentication`,
`⏸ Pending approval`, `- Not configured`, `! Connected · tools fetch failed`,
and halo's own `◐ Cached (connects on first use)` for a lazy server
that hasn't connected yet (Claude Code has no lazy-start concept, so there is
no binary-derived wording for this one state). A `failed`/`needs_auth`/
`disabled` line also names WHY in parentheses (command not found on PATH,
connection refused on host:port, or the reason a transport is disabled).

Before the health check, `mcp list` always explains what it searched:
every scope (user `~/.claude.json`, project-local, this directory's own
`.mcp.json`, plugins, managed) with its own count, another directory's
`.mcp.json` when your own `~/.claude.json` history remembers one, and the
claude.ai connectors `claude` itself reports (see below) -- never a bare
"0 servers". `--refresh` forces a fresh connectors discovery first,
ignoring the `~/.halo/mcp/connectors.json` cache; without it, an EMPTY
cache still gets one bounded (20s) synchronous discovery when eligible
("connector cold start" -- a fresh box's first `mcp list` shows real
connectors, not an empty cache a background worker hadn't gotten to yet),
and an eligible-but-still-empty result says so explicitly rather than
staying silent.
```sh
halo mcp list
```
```
Checking MCP server health...
Searched -- user (~/.claude.json): 0; project-local (~/.claude.json (projects[...])): 0; this directory's .mcp.json (.../.mcp.json): 0; plugins (installed, enabled plugins): 0; managed (...): 0.
No MCP servers configured in this directory (see above for connectors/other scopes).
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
halo mcp add my-server -- npx -y @some/mcp-server --flag
```
```
Added stdio MCP server 'my-server' (local scope) to ~/.claude.json: npx -y @some/mcp-server --flag
```

### `mcp add-json [-s, --scope local|user|project] <name> <json>`
Same storage path as `mcp add`, but the entry is a literal JSON object
string:
```sh
halo mcp add-json my-remote '{"type":"http","url":"https://example.com/mcp"}'
```

### `mcp remove [-s, --scope local|user|project] <name>`
Without `-s`, tries `local`, then `user`, then `project`, removing from the
first scope where the name is found.

### `mcp add-from-claude-desktop [-s local|user|project] [--dry-run]`
Imports every server from Claude Desktop's own `claude_desktop_config.json`
(macOS `~/Library/Application Support/Claude/`, Windows `%APPDATA%\Claude\`,
or -- from WSL -- the Windows side's own AppData under `/mnt/c`; a plain
Linux box has no Claude Desktop to import from, same restriction the real
`claude mcp add-from-claude-desktop` documents). `--dry-run` lists what
would be imported and writes nothing.

### `mcp reset-project-choices [--cwd DIR]`
Forgets every approval halo recorded for a server currently listed in this
directory's `.mcp.json` -- it goes back to `⏸ Pending approval` and halo
asks again next time. Halo's own approval store only ever tracks
approvals (there is no separate "rejected" state to also reset).

### `mcp serve [--cwd DIR]`
Starts halo itself as a stdio MCP server, exposing a curated, stateless
subset of its own built-in tools (Bash/PowerShell, Read, Write, Edit, Glob,
Grep, NotebookEdit, TodoWrite, WebFetch -- never Agent/AskUserQuestion/
ToolSearch/background-job tools, which all need a live session) to any
other MCP client. The ccbridge code (`halo_harness/ccbridge/__main__.py`,
the `cc:` route's own tool bridge) is the base this reuses.

### `mcp login <name> [--no-browser] [--timeout SECONDS]` / `mcp logout <name>`
A generic OAuth 2.0 authorization-code (+ PKCE) flow for one already-added
`http`/`sse` server: opens (or prints, with `--no-browser`) an authorize URL
built from that server's own `oauth` config block (`mcp add --client-id ...
--client-secret ... --callback-port ...`) or, failing that, OAuth 2.0
Authorization Server Metadata discovery (RFC 8414) at the server's own
`/.well-known/oauth-authorization-server`; waits for the browser's redirect
on a local callback, exchanges the code for tokens, and stores them at
`~/.halo/mcp/oauth/<name>.json` -- never in any of Claude Code's files, and
halo never reads `~/.claude/.credentials.json` either. `logout` deletes that
one file. Tested only against a local fake OAuth server
(`tests/helpers/fake_oauth_server.py`); this flow has not been verified
against any specific vendor's real endpoints, and no vendor is named here
or in the code.

### `mcp fix <name> [--apply] [--cwd DIR]`
Halo 2.0.2 round 4: the same diagnosis the `/mcp` dialog shows per row --
prints the server's status, the WHY (`failure_reason`, the same text
`mcp list` shows in parentheses) and the one fix line (`fix_line_for`,
shared code, never duplicated between this command and the dialog).
Without `--apply`, prints only. With it, actually runs the matching
action: approves a pending `.mcp.json` server, runs the OAuth login for a
`needs_auth` http/sse server, reconnects a server that's merely down, or
-- for a command-not-found server -- prints the install hint (never runs
it; that line is for you to run):
```sh
halo mcp fix my-server
```
```
my-server: ✗ Failed to connect
  reason: command not found on PATH: npx
  fix: install Node.js (npx ships with it): https://nodejs.org/
Re-run with --apply to run it.
```

### `mcp test <name> [--cwd DIR]`
The `/mcp` dialog's `t` action from the CLI: connects (lazy servers
connect on first use, same as any real tool call) and times a fresh
`tools/list` round trip:
```sh
halo mcp test my-server
```
```
halo mcp test: my-server: ok in 42ms -- 7 tool(s).
```

### `mcp learned [--forget <endpoint>]`
Halo 2.0.2 round D: lists every endpoint
`providers/learned_rules.py` has learned something about (a learned
`tools_rejected` rule from a live 400, or a learned `reasoning_effort_
with_tools` override), each under its own `"<provider>:<model>"` key;
`--forget <endpoint>` clears that endpoint's `tools_rejected` rule before
its TTL would otherwise expire it naturally, leaving any other learned
field for that same endpoint untouched:
```sh
halo mcp learned
halo mcp learned --forget databricks:my-endpoint
```

## `/mcp` (the repair dialog)

Halo 2.0.2 round 4: lists every configured server and claude.ai connector
with a status glyph, and -- for anything not healthy -- the WHY and the
one fix line (`halo mcp fix`'s own `fix_line_for`, never duplicated) plus,
for a server that died mid-session, its reconnect-backoff attempt count
and next retry. The key legend is always shown at the bottom. All actions
act on the highlighted row (`R` excepted) and run off the UI thread, so a
hung/slow server never freezes the dialog -- Esc while one is in flight
cancels the WAIT on it, never the app.

| Key | Does |
|---|---|
| `r` | Reconnect the highlighted server (re-reads its config first, so an on-disk edit or a brand-new name takes effect without restarting halo). |
| `R` | Reconnect every server, one at a time; also resets the backoff on each one. |
| `a` | Approve a `pending_approval` project (`.mcp.json`) server, then reconnect it. |
| `l` | For a local `http`/`sse` server: the 2.0.1 OAuth login flow (`halo mcp login`). For a claude.ai connector row: the re-auth instructions (there is no local OAuth flow for one of these -- the login lives in claude.ai/claude itself). |
| `L` | A tail of `~/.halo/mcp/<server>.log` (stdio stderr + connect/transport errors, rotated at 1 MB) in a small viewer; Esc returns. |
| `e` | Edit the entry: opens the source file at that server's own line in `$VISUAL`/`$EDITOR`, using THAT editor's own line syntax (`+<line>` for vim/nvim/nano/emacs, `--goto file:line` for `code`, `file:line` for `subl`; any other editor opens the file plain, with no line argument at all), or, with neither set, an inline form (command/args/env, or url/headers) that writes back to the exact same scope file `mcp add` uses; reconnects after either path. |
| `i` | An install hint for a command-not-found server, guessed from the missing command itself (`npx`/`node`/`uvx`/`uv`/`pipx`/`pip`/`python`) -- shown as a copyable line, never run for you. |
| `d` | Disable (or re-enable) the server for THIS directory only -- Claude Code's own per-directory `disabledMcpServers` list, so its definition is untouched and every other project keeps seeing it. |
| `t` | A `tools/list` round trip with timing, shown in the hint line. |
| Esc | Cancel a busy action, or close the dialog. |

**Reconnect backoff**: a server that dies mid-session reconnects on its
next use, but not on every next use while it's still down -- 1, 2, 4, 8
seconds, then every 30 seconds, giving up automatically after 10 minutes
(the row then says so); `r`/`R`/`halo mcp fix --apply` always reset this,
since asking by hand is itself the reset.

## claude.ai connectors bridge

When `claude` is installed and logged in with a claude.ai subscription,
halo discovers the account-side connectors it reports (Claude Docs today;
generic -- any future connector shows up the same way) through `claude`
itself, since halo can never see or authorize them directly and never
reads claude's own credentials file. Discovery (`claude mcp list` plus one
headless `claude -p --output-format stream-json` start, read only for its
`system/init` line's tool names) runs once per session on a background
worker -- never on the UI thread -- and is cached at
`~/.halo/mcp/connectors.json`; `halo mcp list --refresh` and the `/mcp`
dialog's `r` (reconnect) force a fresh one. "Connector cold start": when
that cache is still genuinely empty, both the session build (`-p` and the
TUI's first build alike) and a plain `halo mcp list` (no `--refresh`
needed) instead run ONE bounded (20s) synchronous discovery inline before
anything reads the cache, so the very first look at connectors on a fresh
box is the real list, not an empty one a background worker hadn't reached
yet; once anything real is cached, later launches go back to the
background-only path above.

Each connector becomes one halo tool, `connector__<slug>` (e.g.
`connector__claude_docs`), schema `{request: string, tool?: string, args?:
object}` -- describe what to do in plain words, or name one of the
connector's own tools directly. Running it spawns a scoped, headless
`claude -p --allowedTools "mcp__claude_ai_<Name>__*" --max-turns N
--output-format json` that performs exactly the request and returns the
result verbatim; halo's own permission rules gate the call exactly like
any other tool (`connector__claude_docs(batch)` targets one specific
underlying tool, the same parenthesised-content grammar every other tool
rule already has), and the bridge itself never refuses a call. A deny/ask
rule written for claude's own `mcp__claude_ai_<Name>__*` tool in Claude
Code's settings is automatically translated to the matching
`connector__<slug>` rule, so it still applies here.

Config (`~/.halo/config.json`, dotted `halo config` keys): `connectors.
bridge` (default `true`, `false` disables discovery and every connector
tool outright), `connectors.<slug>.enabled`, `connectors.<slug>.
alwaysLoad` (preload instead of leaving it to ToolSearch), `connectors.
<slug>.max_turns` (default 4). `/mcp`, `halo mcp list` and `halo doctor`
all show each connector's status, labelled `claude.ai connector (via
claude)`; a `needs_auth` one names the exact next step (authorize at
claude.ai or inside `claude` with `/mcp`, then reconnect here). When
`claude` is missing, not logged in with a subscription, or gateway-driven,
the connectors line says so in one sentence instead of being silently
absent. Nothing here is specific to any one vendor's connectors, by design.

## Unsupported MCP transports

`sdk`-type servers are always disabled: `'sdk'` describes embedding
another Claude Agent SDK process in the same run, not a subprocess or
network endpoint, so halo's out-of-process MCP manager has nowhere to
embed one (use stdio/http/sse/ws instead). `ws`/`websocket`-type servers
connect for real once halo's installed `mcp` package ships its own
websocket client (`mcp.client.websocket`) -- the pinned `mcp==2.2.0` this
build ships with does not, so they're disabled too today, with that exact
reason shown next to them (`halo doctor`, `mcp list`/`get`, `/mcp`).

## MCP audio content

An MCP tool's `audio` content block is passed through as a typed `audio`
block (same `{"source": {"type": "base64", ...}}` shape an image block
already uses) to a model whose profile declares audio support; every model
in this build's own table omits that today, so in practice every MCP
audio result still becomes the existing `[audio content (<mime>) omitted
-- this model has no audio support]` text note.

## `halo ollama`

```sh
halo ollama
halo ollama --host <name>
halo ollama --refresh
```

Halo 2.0.3 round 3: per-configured-host analysis for `ollama.hosts`
(`docs/CONFIG.md`) -- reachability, version, loaded models (`size` vs
`size_vram` as one plain offload sentence, trained vs. effective
context, the KV-bytes/token figure, this round's `tools_max` and its
rough per-request prompt-token cost), and -- local hosts only -- OS-level
GPU memory. `--host NAME` narrows to one configured entry instead of
every one; `--refresh` bypasses the short in-memory catalog TTL and
re-reads `/api/tags`+`/api/show` now. With no `ollama.hosts` configured
at all, probes the same synthesized default (`OLLAMA_HOST`, else
`127.0.0.1:11434`) a bare `ol:<model>` ref would use. Round 5b adds a
"what fits" column per catalog model (not just loaded ones) -- the
learned calibration cap or "not calibrated," and the one-phrase source
of the num_ctx decision -- plus the last turn's own tokens/second and
prefill seconds when one has happened on that host/model since. See
[MODELS.md](MODELS.md)'s Ollama section; the TUI's own `/ollama` opens an
interactive dialog instead (`docs/SLASH-COMMANDS.md`).

## `halo ollama calibrate`

```sh
halo ollama calibrate <model>
halo ollama calibrate <model> --host <name>
halo ollama calibrate <model> --start <num_ctx>
halo ollama calibrate <model> --no-up
```

Halo 2.0.3 round 5b: loads `<model>` on the chosen host (the default host
when `--host` is omitted) at a candidate `num_ctx` and reads `/api/ps`
back, stepping DOWN by powers of two until it is fully resident in GPU
memory (`size_vram >= size`) or the candidate drops below 4096, whichever
comes first -- this loads the model several times in a row, by design.
`--start` overrides the first candidate tried; left unset, it is the
GPU-based fit estimate when a local or `ollama.hosts[].ssh` read exists,
else 32768. Once a fitting candidate is found, round 5b part 2 keeps
STEPPING UP by powers of two (bounded by the model's own trained context
and the 131072 hard cap) to find the true ceiling rather than settling for
the first lucky guess -- `--no-up` skips that phase and keeps the first
fitting candidate. Prints the host/model being calibrated, the starting
candidate, and the result (the fully-resident `num_ctx` and how many
steps it took, or "does not fit... even at num_ctx=4096"); either outcome
is recorded in `~/.halo/ollama-fit.json` (a "does not fit" result is a
real, remembered fact too, not a failure to retry on the next run). The
SAME procedure also runs automatically the first time a model is used on
a host with no learned cap at all -- `ollama.auto_calibrate: false`
(`docs/CONFIG.md`) opts out of that; this command is always available
regardless, for an explicit re-measurement after a GPU/driver/other-
process change. See [MODELS.md](MODELS.md)'s "Fit calibration" section.

## `halo ollama doctor`

```sh
halo ollama doctor
halo ollama doctor --host <name>
```

Halo 2.0.3 round 5b part 2: the same per-host analysis `halo ollama`
prints, plus the documented host-tuning recommendations Halo cannot read
back from any API (`OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=
q8_0`, `OLLAMA_NUM_PARALLEL=1` on a single-user box, `OLLAMA_KEEP_ALIVE`,
`OLLAMA_CONTEXT_LENGTH`) and exactly where each one lives per OS of the
HOST -- the OS Halo itself runs on for a local host, all three briefly for
a remote one. A loopback-only host also gets a one-line "reachable from
this machine only" hint naming the per-OS switch that would share it on
the LAN. `halo doctor`'s own Ollama section prints the identical checklist
text, one line per host. See [MODELS.md](MODELS.md)'s "Host setup
checklist" section.

## `halo gym`

```sh
halo gym
halo gym --models ol:qwen3-coder:30b,ol:gpt-oss:20b
halo gym --roles small,judge
halo gym --quick
```

Halo 2.0.3 round 5d: runs the fixed task battery (tool-call accuracy, edit
success, context recall, instruction adherence, tokens/second, prefill
seconds -- see [MODELS.md](MODELS.md)'s "The model gym" section for what
each one means) against each `--models` ref, or every model in the default
Ollama host's own catalog when `--models` is omitted; `hf:local/...` and
`hf:mlx/...` refs run the same battery through their own server (Halo 2.0.3
round 5f follow-up: tokens/second is then a wall-clock estimate and prefill is
not reported); a cloud ref may be named for comparison but is never included
by default. `--roles`
also prints each model's weighted composite for those roles right under
its card (default: `small`, `researcher`, `judge`, `subagent_default`).
`--quick` halves the battery size for a faster, noisier read. Every
model's card is saved to `~/.halo/gym/<host>/<digest>.json` and printed as
it finishes.

## `halo gym show`

```sh
halo gym show
halo gym show qwen3-coder:30b
```

Prints the saved per-model card(s) from `~/.halo/gym/` -- a bare name or
full `ol:` ref both match (tag-aware, same as the Ollama catalog's own
matching); omit it to print every saved card, newest-measured first. Never
runs a fresh probe -- `halo gym` is what measures.

## `halo gym propose`

```sh
halo gym propose
halo gym propose --roles small,judge
halo gym propose --main ol:qwen3-coder:30b
halo gym propose --apply
halo gym propose --apply --name my-local-roles
```

Turns saved `halo gym` scores into a role-table proposal -- the best LOCAL
model per supporting role, respecting the round 5b VRAM-aware rule, with
one plain sentence per role naming the composite score behind it (or
saying plainly that no local model has a usable score for that role yet).
`main` is never proposed; `--main REF` only changes which model the VRAM
rule compares candidates against (the configured default model otherwise).
`--apply` saves the proposal as a role template through the existing `halo
roles template import` path (`gym-proposed` by default, `--name` to
change) -- `halo roles template load <name>` (or the picker) is the
separate step that makes it the live table. See [ROLES.md](ROLES.md)'s
"Data-driven roles" section.

## `halo local`

```sh
halo local
halo local --refresh
```

Halo 2.0.3 round 5: the shared local-model discovery view, merging THREE
sources into one list, in this order, with a group label per source/host:
each configured Ollama host's own catalog (same analysis `halo ollama`
shows), running Hugging Face local servers (auto-detected on the default
ports plus any configured `huggingface.local_servers` entry), and the
Hugging Face Hub cache (models on disk, from `hf download`, not
necessarily being served by anything right now). Bare `halo local` never
probes a MANUAL Hugging Face local-server entry over the network (shown
as "configured, not probed yet" instead) -- `--refresh` additionally
probes every one of those, and bypasses the Ollama catalog's own short
TTL cache; auto-detected servers are probed either way (background-probe-
gated, same `BRIDGE_TEST_NO_BACKGROUND_NET` seam every other probe in this
codebase honours). Round 5c adds a fourth source to that merged list:
every `.gguf` file/safetensors or MLX folder found under `huggingface.
model_dirs` (`/local add`/`forget` manage that list, below), each row
carrying its format, size, and -- read from the file itself -- trained
context and quantization where the file says so. See
[MODELS.md](MODELS.md)'s Hugging Face section; the TUI's own `/local` (no
arguments) opens the same view as an interactive dialog instead
(`docs/SLASH-COMMANDS.md`).

### `halo local add <path>` / `halo local forget <path>`

Adds/removes one folder from `huggingface.model_dirs`, persisted to
`~/.halo/config.json` immediately -- the CLI twin of `/local add`/`/local
forget` (`docs/SLASH-COMMANDS.md`). `add` refuses a path that isn't an
existing directory, or one already in the list; `forget` refuses a path
that isn't in the list. Neither touches the network.

### `halo local serve <model> [--runtime llama-server|mlx_lm] [--port N] [--keep]`

Starts a managed `llama-server` (`.gguf`) or `mlx_lm` (a safetensors/MLX
folder, Apple Silicon only) child process on a free loopback port, sized
to this machine's own fitted context, and records it in `~/.halo/run/
local-servers.json`. `<model>` is either an exact path, or a name shown by
a bare `halo local`. Stopped when Halo exits unless `--keep` is given.
When the chosen runtime isn't found (on `PATH`, or already fetched into
`~/.halo/runtimes/`), `llama-server` is offered as a download (named size
and URL, verified against the GitHub Releases API's own per-asset
`digest`) -- nothing downloads without a `y`/`--yes`; declining prints the
one-line install hint for this OS instead. See [MODELS.md](MODELS.md)'s
"Finding and using file-backed models" section for the full story.

Round 5f: `<model>` with `--runtime mlx_lm` also accepts a bare Hugging
Face Hub repo id instead of a path or folder (`halo local serve
mlx-community/Qwen2.5-7B-Instruct-4bit --runtime mlx_lm`) -- the explicit,
non-`--model` form of `hf:mlx/<org>/<repo>` (see [MODELS.md](MODELS.md)'s
"Apple Silicon" section and [MAC.md](MAC.md)); `mlx_lm` itself is never
fetched by `halo local serve` the way `llama-server` is -- it's the
optional `uv tool install "halo-harness[mlx]"` extra instead.

### `halo local stop <model>`

Stops a model `halo local serve` (or the `/local` dialog's `s` key)
started, by the SAME `<model>` name it was started with. Works even in a
brand new `halo` process -- it reads `~/.halo/run/local-servers.json` and
kills by the recorded pid, rather than needing a live handle.

### `halo local import <model> [--name NAME] [--host NAME] [--yes]`

Copies a `.gguf` file into an Ollama host's own model store (writes a
Modelfile, calls `/api/create`) -- after naming the file's size and that
it's about to be copied, nothing happens without a `y`/`--yes`. The result
is an ordinary `ol:<name>` (default: the file's own name). Only `.gguf`
files are offered this path this round -- see [MODELS.md](MODELS.md).

### `halo local runtime remove [VERSION]`

Deletes a fetched `llama-server` runtime from `~/.halo/runtimes/` -- one
version, or every version when none is named.

## `halo config`

Reads and writes **this harness's own** small store,
`~/.halo/config.json` -- never Claude Code's `settings.json`, which
this project only ever reads. See `docs/CONFIG.md` for every key this file
can hold.

```sh
halo config --help
```
```
Usage: halo config [list|get <key>|set <key> <value>]
```

```sh
BRIDGE_TEST_HOME=/tmp/demo-home halo config list
```
```
(no config set -- ~/.halo/config.json is empty or missing)
```
```sh
BRIDGE_TEST_HOME=/tmp/demo-home halo config set model or:deepseek/deepseek-v4.1-flash
```
```
model="or:deepseek/deepseek-v4.1-flash"
```
```sh
BRIDGE_TEST_HOME=/tmp/demo-home halo config get model
```
```
"or:deepseek/deepseek-v4.1-flash"
```

A key may be dotted (`improve.model`) to read/write a nested value without
disturbing its siblings; `halo config key=value` (one argv token) is
accepted as shorthand for `config set key value`. The value is parsed as
JSON when possible (`true`, `123`, a quoted string), so non-string values
round-trip too.

## `halo stats`

A headless, cross-session version of the TUI's own `/stats` -- aggregates
tokens/cost/tool-calls straight from the existing session `.jsonl` logs,
never a new model-visible field.

```sh
halo stats --help
```
```
usage: halo stats [-h] [--all] [--all-projects] [--cwd DIR] [--json]
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

Bare `halo stats` (no `--models`/`--tools`/`--roles`) is the original,
cheap report: turns, total cost, per-model token/cost totals, per-tool call
counts -- works even on a session log recorded before the richer telemetry
fields existed. `--models`/`--tools` switch to `halo_harness.telemetry`'s
aggregation (cached at `~/.halo/stats-cache.json`, keyed by
path+size+mtime so an unchanged log is never re-parsed): per-(model,
provider) sessions/calls/tokens/cost/avg-latency/tool-error%/repair-hit%/
edit-failure%/steers/compactions/loop-breaker-trips (14 columns by default,
`--wide` adds route/cached-tokens/avg-ttft/finish=length%/retries/overflow/
interrupts -- columns are dropped right-to-left to fit the terminal width,
never wrapped or truncated when piped to a file). `--roles` (V2c/H15): sub-
agent spend (sessions/calls/tokens/cost) per role name, summed from every
Agent-tool call's own rolled-up usage node -- see `docs/ROLES.md`.

```sh
BRIDGE_TEST_HOME=/tmp/demo-home halo stats
```
```
halo stats (/path/to/project, 0 session(s)):
  Turns: 0
  Total cost: $0.0000
```

## `halo improve`

The headless surface for the same human-gated loop the TUI's `/improve`
drives interactively -- clusters recent failures, drafts up to 8 candidate
memory/rule/skill files with one model call, and **never writes anything
without `--apply`**.

```sh
halo improve --help
```
```
usage: halo improve [-h] [--since {7d,30d,all}] [--all-projects]
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

Bare `halo improve` scans, drafts (one model call -- `improve.model`
config, else the small model, else the session model), prints every
candidate, and saves them to `~/.halo/improve/<timestamp>.json`:

```sh
BRIDGE_TEST_HOME=/tmp/demo-home halo improve --bare
```
```
halo improve: --bare disables /improve entirely.
```

`--apply <file>#<id>` (repeatable) writes exactly that saved candidate to
disk -- the only way a headless invocation ever writes a memory/rule/skill
file. `--json`/`--out FILE` are for scripting; `--all-projects` scans every
project's sessions instead of just the current directory's.

## `halo export`

A headless version of the TUI's own `/export` -- writes a session's raw
JSONL transcript to a file or stdout, optionally sanitized.

```sh
halo export --help
```
```
usage: halo export [-h] [--session ID] [--sanitize] [-o FILE]
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
BRIDGE_TEST_HOME=/tmp/demo-home halo export
```
```
halo export: no sessions found for this directory
```

`--sanitize` redacts (never just masks) OpenRouter/Anthropic/Databricks/
GitHub/AWS-shaped tokens, quoted or JSON-encoded `export KEY="value"`/
`"KEY": "value"` assignments (by exact known name, and generically for any
`*_API_KEY`/`*TOKEN*`/`*SECRET*`-shaped name), and `Bearer <token>` headers,
by round-tripping each log node through JSON text -- the same sanitizer the
TUI's own `/export --sanitize` calls, so the two can never disagree.

## `halo bugreport`

"One paste instead of screenshots" -- writes a redacted diagnostic report
to `~/.halo/bugreports/<timestamp>.md` (or `--out FILE`) against the most
recent session for the cwd (or `--session ID`). `/bugreport` in the TUI/`-p`
is the same report, built from the live session instead.

```sh
halo bugreport --help
```
```
usage: halo bugreport [-h] [--last N] [--session ID] [--out FILE] [--copy]
                      [--include-content] [--cwd DIR]

Write a redacted diagnostic report (one paste instead of screenshots).

options:
  -h, --help         show this help message and exit
  --last N           Session events to include (default 20)
  --session ID       Session id or unique prefix
  --out FILE         Write to FILE instead of ~/.halo/bugreports/
  --copy             Also copy the report text to the clipboard
  --include-content  Include prompt/output text, not just shapes
  --cwd DIR
```

Contents, in order: halo version, Python/OS/terminal/shell/cwd/git branch,
install mode; which providers are enabled and WHY (env file, settings env,
`~/.databrickscfg`, claude.ai login -- never a key, not even a fingerprint);
the current route (ref, provider, api_type, dialect, profile, effort
requested and effort SENT); permission mode; MCP servers with state;
`~/.halo/config.json` with secret-shaped values replaced; settings source
paths (paths only); catalog cache age per provider; this session's own
learned permission rules; the last turn's timeline (see `halo timeline`
below); the last N session events (types, durations, tool names, status
codes and error messages -- prompt/output text only with
`--include-content`); and the last 50 `bridge.log` lines. Every line passes
through the same redactor `halo export --sanitize` uses, plus a stronger
pass (long hex/base64 runs, `sk-`/`dapi`/`Bearer ` shapes, and the literal
value of any secret env var this process can see) -- a planted fake key
never survives into the report from any of these sources. `--copy` copies
the report text to the clipboard with whatever exists (`clip` on Windows,
`pbcopy` on macOS, `wl-copy`/`xclip` on Linux; otherwise just the path is
printed).

## `halo timeline`

Reads the per-turn request/tool/hook/permission-wait/compaction timing
timeline a session's own log already recorded (`agent.loop.Session.turn`'s
own wrapper, for every turn regardless of dialect) back from that session's
log file -- works for a session from an earlier process, not just a live
one. `/timeline [N]` in the TUI/`-p` shows the SAME shape for the live
session's own in-memory copy instead (identical fields); `--debug` prints
the same lines live as they happen.

```sh
halo timeline --help
```
```
usage: halo timeline [-h] [--last N] [--session ID] [--cwd DIR] [--json]

Show the per-turn request/tool timing timeline for a session.

options:
  -h, --help    show this help message and exit
  --last N      Turns to show (default 1, the most recent)
  --session ID
  --cwd DIR
  --json        Print raw JSON instead of formatted text
```

Per turn: request-sent/headers/first-reasoning/first-text/first-tool-call/
message-end elapsed milliseconds; each tool call's own name/start/end/
status; each hook that ran (event name + real duration); each permission
ask that blocked the turn (start, end, resolved decision -- "allow"/"deny"/
"dismissed"); compactions (phase, trigger, tokens before/after when
known -- auto-compaction only, a manual `/compact` runs outside any turn);
steers; and retries/errors with status codes. `--json` prints the raw
records instead of the formatted text.

## `halo worktree`

```sh
halo worktree rm <path>
```

Removes a git worktree (`git worktree remove`, falling back to `--force`
once over uncommitted changes -- a session's own scratch worktree is
disposable by design) and fires `WorktreeRemoved`. The explicit counterpart
to `-w/--worktree`'s own creation: that flag's tree is left on disk when the
session ends by default (config `worktree.remove_on_exit`, default `false`
-- set it `true` to have the session remove its OWN worktree automatically
on exit instead, which fires the same event). No `list`/`add` subcommand
yet -- `-w/--worktree` itself is how one gets added.

## `halo bg`

```sh
halo bg list
halo bg logs <id> [-n LINES]
halo bg stop <id>
halo bg rm <id> [--force]
```

The read/manage side of `--bg`/`--background`, over the detached run's own
`~/.halo/bg/<id>/{output.log,meta.json}`: `list` shows each run's id, status
(`running`/`exited`, checked live, never trusted stale), pid, age and
command; `logs` prints the captured stdout+stderr (`-n` for just the tail);
`stop` kills the process tree by pid (same Windows orphan-grandchild-aware
kill background Bash jobs already use); `rm` deletes the run's directory,
refusing a still-running one unless `--force` (which stops it first).

## `halo roles`

```sh
halo roles template list
halo roles template show <name>
halo roles template save <name> [--description TEXT]
halo roles template new <name> [--description TEXT]
halo roles template load <name>
halo roles template edit <name>
```

Halo 2.0.2: manages `~/.halo/roles/<name>.json` role templates (`{"name",
"description", "roles": {role: model_or_{"model","effort"}}}`). `save`
captures the CURRENT `~/.halo/config.json` role table under a name; `new`
starts an empty one; `load` writes every role the template defines back
into config.json, overwriting a stale local value for that role (the one
deliberately non-idempotent write in this whole table -- see
[ROLES.md](ROLES.md)'s own precedence section); `edit` opens `$EDITOR`/
`$VISUAL` on the raw JSON file (creating it first if it doesn't exist),
re-validating on save. The TUI's own `/roles edit <name>` opens a form
instead (`tui/dialogs/roles_editor.py`) unless `roles.editor: "external"`
is configured, in which case it uses the same `$EDITOR` flow as this CLI
command; that form, and the init wizard's own Roles step, both gain a
"start from" template picker in round 7 (three shipped presets --
`balanced`/`quality`/`local-first` -- written on first use, never
overwritten). See [ROLES.md](ROLES.md) for the full picture: the role
table itself, per-role effort, custom role names, `/role`/`/roles` in the
TUI and print mode, the presets, and the `roles.enabled` mode switch.

## `halo org`

```sh
halo org list
halo org show <name>
halo org new <name> [--description TEXT]
halo org install <name> [--force]
halo org edit <name>
halo org run [<name>] "<goal>" [--model REF] [--yes]
halo org export <name> [file]
halo org import <file>
halo org resume <session-id> [--cwd DIR] [--model REF] [--yes]
```

Halo 2.0.2 round 2: manages `~/.halo/orgs/<name>.json` organizations -- a
tree of positions (`halo org new` starts a one-position "Orchestrator"
root); `show` prints the text tree; `edit` opens `$EDITOR`/`$VISUAL` on
the raw JSON file (creating a starter first if it doesn't exist already),
re-validating on save; `run` executes the org's root position on
`<goal>`, delegating through the same sub-agent machinery an
`Agent(org=...)` tool call uses, and prints its final answer. `--model`
on `run` is only a fallback for a position with neither its own `role`
nor `model` set (every built-in template's own positions always set
one). Round 7: `<name>` is optional on `run` -- a bare `halo org run
"<goal>"` (one argument) uses `orgs.default` (set via the init wizard's
own Organizations step, `/setup orgs`, or `halo setup orgs`); with no
default set either, it refuses cleanly instead of guessing. The TUI's
own `/org edit <name>` opens a form instead (`tui/dialogs/org_editor.py`,
also gaining a round 7 "start from: solo / release-flow / company /
<saved>" template picker); `/org load <name>` (TUI-only -- re-installs
one of the three shipped built-ins, overwriting a local copy) has no CLI
equivalent, since `edit`/`new` already cover the same ground from a
script.

Round D: `install <name>` copies a shipped built-in or a template saved
under `~/.halo/org-templates/<name>.json` into `~/.halo/orgs/`, refusing
to overwrite an existing file there without `--force`; `list`/`/org list`
show each one's own one-line README (its `description` field). `export`/
`import` move an org as plain JSON -- `export` writes to `file` or stdout
when omitted; `import` validates the same way any other org write is
(role names, reports, `budget_usd`), listing every problem it finds, and
falls back to the file's own basename when the JSON has no "name" field.
`resume` continues an interrupted run from a PAST session's own saved run
record and shared task board (open and claimed tasks become the work
list, done tasks are kept), with that run's own `max_concurrent`/
`budget_usd` restored from the record even if the org definition has
since been edited; `/org resume` (no argument) is the TUI/print-mode
form, for the CURRENT session's own interrupted run. A position with
`requires_approval: true` holds its just-finished result as a pending
card in the dock (accept, edit the instruction and re-run, or stop)
before its own parent continues; with no live dock at all (`run`/
`resume` from a plain terminal), it prints the result and waits on
stdin the same way; `--yes` on `run`/`resume`, or the session's own
`dontAsk` permission mode, accepts every gate automatically instead of
asking. See [ORGS.md](ORGS.md) for the full schema, the three built-ins,
how a run flows through the tree, budgets, goals, approval gates,
export/import and resume.

## `halo setup` / `/setup` (Halo 2.0.2 round 7)

```sh
halo setup            # the init wizard's Roles step, then Organizations, then a short summary
halo setup roles       # just the Roles setup screen
halo setup orgs        # just the Organizations setup screen
```

The SAME two setup screens `halo init`'s own wizard shows (Roles,
Organizations -- see [ROLES.md](ROLES.md)/[ORGS.md](ORGS.md)), reachable
again later without re-running the whole provider flow -- "a setup
screen should pop up to set those features up in addition to doing it
within halo." On a real terminal this opens the wizard screen(s); with no
TTY it prints the current roles table / orgs list instead of blocking,
and exits 0. `/setup`, `/setup roles`, `/setup orgs` do the same thing
inside a running session (`tui/slash.py`'s own handler pushes the screen
onto the live app -- a modal stack over the session, not a separate
program): saving a role template there updates the LIVE session's own
role table immediately, no restart needed.

## `/tasks` (Ctrl+T)

Halo 2.0.2 round 3: a full-height panel listing every running, queued,
background and finished sub-agent of this session (position/role, model,
status, elapsed, tool count, cost, and an org run's place in the tree),
plus a second tab for the shared task board (`TaskCreate`/`TaskUpdate`/
`TaskList`). Enter opens a live transcript viewer of the highlighted
agent's own log; `Tab` switches tabs; Ctrl+T or Esc closes it. The Agent
tool's own `count`/`batch` parameters spawn several sub-agents in one
call (capped at `agents.max_concurrent`, excess ones queue); see
[SUBAGENTS.md](SUBAGENTS.md) for the full picture.

## `halo completion`

```sh
halo completion bash
halo completion zsh
halo completion powershell
```

Halo 2.0.2: prints a shell-completion script for the `halo` command
itself -- every top-level subcommand, every known role name
([ROLES.md](ROLES.md)), and every model ref already cached under
`~/.halo` (`models.json`/`dbx-endpoints.json` -- read as plain files, no
network call of its own). `eval "$(halo completion bash)"` (or `zsh`) in
your shell's rc file, or dot-source the `powershell` form from your
`$PROFILE`.

## `halo proxy`

The original `claude-bridge`: a single-file HTTP proxy (`bridge.py`) that
sits in front of the **real** `claude` binary and translates its
Anthropic-Messages-API calls to OpenRouter/Databricks, leaving `claude`'s
own subscription, config, hooks and permissions completely untouched. Kept
unchanged, as its own subcommand, for anyone who wants the real `claude` CLI
itself (its own update cadence, its own bug-for-bug behavior) driven by a
non-Anthropic model, rather than halo's own agent loop.

```sh
halo proxy --help
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
| `halo proxy launch [claude flags...] [prompt]` | starts (or reuses) the proxy server, then execs the **real** `claude` binary pointed at it -- every argument after `launch` is forwarded to `claude` verbatim, including `-h`/`--help` (so `halo proxy launch --help` prints the real Claude Code help, not this project's own) |
| `halo proxy --serve [--port PORT] [--state-dir DIR]` | runs the proxy server in the foreground |
| `halo proxy --probe` | outbound reachability check for both providers, caches the OpenRouter model list |
| `halo proxy --config` | resolved configuration as JSON, secrets redacted |
| `halo proxy --stop` | shuts down a running proxy server |

```sh
BRIDGE_TEST_HOME=/tmp/demo-home halo proxy --probe
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
