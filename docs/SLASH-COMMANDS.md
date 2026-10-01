# Slash commands, keys, and prompt prefixes

Everything the full-screen TUI's prompt input understands: `/name` slash
commands (built-in, custom, skill-provided, MCP-provided), the `@path`/
`!cmd` prefixes, key bindings and chords, and the cards a permission/
question/plan/rewind/improve moment shows -- with the exact keys each one
answers to. For the headless CLI surface (`halo <subcommand>`), see
[COMMANDS.md](COMMANDS.md).

**How this page is kept honest**: `tests/test_docs_slash_commands.py`
imports the real command registry (`halo_harness.commands.registry.Registry`)
and `halo_harness.commands.builtins._BUILTIN_SPECS`, and fails the suite if a
registered built-in command has no `### \`/name\`` section here.

Every built-in command works the same in `-p` print mode and the TUI for
its **core**-kind behavior (`/cost`, `/help`, `/context`, ...); a handful of
**ui**-kind commands (`/resume`, `/plan`, `/rewind`, ...) need the
interactive picker/card machinery and, in `-p`, print one honest line
saying so instead of performing the action -- these are marked `[TUI-only]`
below. `/init` is the one **prompt**-kind command: its "output" is a real
prompt fed back into the model, not printed directly.

## Built-in commands

### `/help`
Lists every registered command (built-ins, custom commands, skills, MCP
prompts), one line each, alphabetically, with its description.

### `/clear`
Starts a genuinely new session: fires `SessionEnd(clear)` then
`SessionStart(clear)`, builds a fresh session log, and clears the visible
transcript -- not just a view reset. Bare `-p` prints a no-op note (each
`-p` call is already a single fresh turn with nothing to clear).

### `/compact [instructions]`
Runs the real compaction algorithm synchronously (see
`docs/ARCHITECTURE.md`'s Compaction section) when a live session is
attached; an optional instructions string steers what the checkpoint
summary should emphasize. On a bare `-p` turn with no prior history, it's a
no-op note. A failed attempt leaves the conversation byte-for-byte
unchanged and says so.

### `/cost`
Total cost and turn count for the session so far (`"n/a"` when the route
doesn't report cost, e.g. Databricks). A second line (H15 part 2 addendum
4) appears once a background fetch has ever succeeded -- the same cached
figure the status bar's own "OR $12.40 left"/"OR $3.21 used" segment and
`/providers` show, naming which of three kinds it is: "OpenRouter: $12.40
remaining (this key's own limit; key: <label>, as of HH:MM:SS)", "...
(account credits, via the management key; ...)", or "OpenRouter: $3.21
used so far (this key's spend so far -- no limit set, no management key
configured; ...)". Omitted entirely when OpenRouter isn't enabled or
nothing has been fetched yet. See `docs/ARCHITECTURE.md`'s status bar
section for exactly where the figure comes from and how often it refreshes.

### `/context`
A live breakdown of the current request's system/tools/messages/pruned
token counts against the model's context window, plus the token count at
which auto-compaction will trigger.

### `/model [ref]`
No argument: opens the model picker -- a filterable, arrow-key list
(`Up`/`Down`/`PageUp`/`PageDown`/`Home`/`End` move the highlight, typing
filters, `Enter` confirms, `Esc` cancels; the filter box keeps keyboard
focus throughout) grouped by provider/family with one header per group
(OpenRouter, `Claude Code subscription` -- only shown when that subscription
is actually usable, see below -- and `Databricks (<family>)` per family).
Every row shows the SAME columns regardless of provider: context window,
max output, and USD/1M-token prices (`ctx=200k out=64k in=$1.00/M
out=$5.00/M`, blank -- never `?` -- for anything not published), plus,
for a Databricks row, a `[<family> · <path>]` tag naming which gateway
path it resolves to right now (see `docs/DATABRICKS.md`); a `cc:`/`ant:`
row's own tag instead names the resolved model id (e.g. `cc:opus` shows
`[-> claude-opus-5-5 (latest Opus)]`, see `docs/MODELS.md`'s alias table).
Databricks
pricing/context data comes from models.dev (refreshed by `/models
refresh`) when that endpoint is listed there, else from `model_table.json`
(context only), else blank -- never guessed. With a model reference (any
form `docs/MODELS.md` documents, including a bare `databricks-*`/
`system.ai.*` name or any name that matches a cached Databricks endpoint)
given directly as `/model <ref>`, switches the session to it immediately,
no picker -- mid-session model changes are logged as a fresh `meta` node.
The `Claude Code subscription` group appears only when `claude auth status`
reports an actual claude.ai login (not, e.g., a Databricks work box's own
settings-driven login) -- otherwise a `cc:` row would just fail at request
time with no warning from the picker.

### `/models [refresh]`
Bare: renders the cached Databricks table INSTANTLY, without touching the
network -- one row per endpoint (family, the family's default gateway
path, chat-capable yes/no by the endpoint's own `task`, plus the same
ctx/output/price columns `/model`'s picker shows) plus the catalog's age.
An old cache from before this table tracked gateway types shows `path
unknown (refresh needed)` rather than a silently wrong guess. `refresh`
re-lists the workspace catalog AND re-fetches models.dev's own pricing
data, both off the UI thread, and reports a one-line added/removed/changed
diff; on failure, the (unchanged) cached table is still shown, plus one
line naming the error. See `docs/DATABRICKS.md`.

### `/dbx`
An alias that always behaves like `/models refresh`, regardless of any
argument typed after it.

### `/mcp`
`[TUI-only for the interactive dialog]` -- lists configured MCP servers
with live health in `-p`; in the TUI, opens an interactive status dialog
(reconnect a server, approve a pending `.mcp.json` entry).

### `/memory`
Shows the auto-memory directory path, whether `MEMORY.md` exists, and how
many topic files are indexed.

### `/permissions`
Shows the active permission mode and the allow/ask/deny rule counts.
`[TUI-only]`: opens an interactive rules dialog instead of a text summary.

### `/plan` `[TUI-only]`
In `-p`, prints the current permission mode and a note that plan review
needs the TUI. In the TUI, a plan review is actually driven by the
`plan_review` event/`PlanCard` (see [Cards](#cards-and-their-keys) below),
not by typing `/plan` -- this command is a status check, not how you
respond to one.

### `/resume [id-or-text]` `[TUI-only picker]`
In `-p`: with text, reports the matching session id (you still pass it via
`-r` on the command line to actually resume); with nothing, lists recent
sessions for this directory. In the TUI, opens the session picker with a
live fuzzy filter over title/first prompt/cwd/model, pre-filtered by any
text typed after `/resume`.

### `/status`
A one-screen dashboard: version, current model, cwd, permission mode, MCP
server count, theme, and cost so far.

### `/config [key=value]`
Read-only in headless mode (shows `model=... permissionMode=... theme=...`
and refuses a write with a pointer to `~/.claude/settings.json`/`--settings`).
This is **not** the same store as `halo config` (which reads/writes
`~/.halo/config.json`, this harness's own settings) -- see
`docs/CONFIG.md`.

### `/skills`
Lists every discovered skill as `/name`.

### `/agents`
Lists every discovered sub-agent definition (built-ins plus
`.claude/agents`/`~/.claude/agents`/`--agents`/managed/plugin), pulled from
the live session's own agent runtime when one is attached so it never
drifts from what an `Agent(subagent_type=...)` call would actually see.

### `/roles`
V2c (H15): shows the resolved role table (`orchestrator`/`coder`/`reviewer`/
`researcher`/`small`) -- model, endpoint/path type, and price per role,
pulled from the live session's own `agent_runtime.role_table`/
`.cli_role_overrides` (the SAME table a role-bearing `Agent`/`Task` call
actually resolves against) so it never drifts from real behavior. See
`docs/ROLES.md`.

### `/providers [list|enable <name>|disable <name>|setup <name>]`
H15 item 21 (rule replaced by the H15 part 2 addendum): the provider-
enablement table -- status, reachable, cached model count -- for
`databricks`, `openrouter`, `anthropic`, `claude_subscription` (`cc:`), and
`typesafe` (stores `TYPESAFE_API_KEY` only, for a later feature; no routed
models yet). Each row's status is `auto (detected from <source>)` once a
real credential/login is found (no `init` step required), `disabled by
you`/`enabled by you` once an explicit override exists, or `not set up`.
Bare `/providers` (or `list`) prints the table, plus a trailing OpenRouter
balance line once a background fetch has ever succeeded (see `/cost`);
`enable <name>`/`disable <name>` write an explicit override (`true` forces
a provider on with no credentials at all; `false` hides one auto-detection
would otherwise have turned on) right here (`cc`/`ant`/`dbx`/`or` are
accepted aliases for the canonical names); `setup <name>` needs the
interactive picker `halo init`/`halo providers setup <name>`
show from a real terminal, so headless just points at that command instead
of half-implementing it. See `docs/MODELS.md`'s "Provider enablement"
section for the full prefix/label table and the exact per-provider
detection rule.

### `/effort [level]`
1.0.1 hotfix 19/20. Bare `/effort` shows the effective level, its source
(`flag`/`settings`/`default`/`session`), and the accepted levels for the
current route -- in the TUI it instead opens an inline selector card in
the transcript (Claude Code style): a horizontal row of the route's own
accepted levels (never the full vocabulary -- an Anthropic route never
offers `xhigh`, which that endpoint schema rejects), current one
bracketed, one-line description underneath. **Left/Right** or **h/l**
move, **Enter** applies, **Esc** cancels and keeps the old value (either
way, focus returns to the prompt); a model with no adjustable effort at
all shows a one-line card that closes on any key. `/effort <level>` (in
either mode) sets it immediately, clamped to what the route accepts
(`xhigh` on a route without it becomes `max`) -- effective from the next
message, remembered for the rest of the session. `--effort`/settings
`effortLevel` still set the STARTING value; every Anthropic-family route
(`cc:`/`ant:`/a Databricks Claude foundation endpoint) defaults to `high`
when nothing more specific was set anywhere. See
[MODELS.md](MODELS.md)'s "Reasoning effort" section for the accepted-level
table per route family.

### `/init`
A **prompt**-kind command: its body is a fixed instruction asking the
model to analyze the codebase and write/update `CLAUDE.md` -- the model
does the actual work as an ordinary turn, this command doesn't write
anything itself.

### `/doctor`
Runs the same read-only checks as `halo doctor` and prints the
lines inline in the transcript.

### `/export` `[TUI-only]`
In `-p`, prints a note that export needs the TUI's file picker (use
`halo export` instead -- see `docs/COMMANDS.md`). In the TUI,
`/export [--sanitize] [path]` writes the session's JSONL transcript to disk
immediately.

### `/add-dir <directory>`
In `-p`, explains that a running session can't be extended this way --
pass `--add-dir` on the command line at startup instead. (The TUI shares
this same headless text today; a directory is normally added via
`--add-dir` at launch.)

### `/theme [name]`
No argument: shows the current theme. With a name (`claude-dark`,
`claude-light`, or either with a `-daltonized`/`-ansi` suffix), persists it
to `~/.halo/config.json` and (in the TUI) re-applies it live.

### `/exit`, `/quit` `[TUI-only for /quit]`
`/exit` in `-p` is a no-op note (the call already ends after this turn).
`/quit` is a TUI-only alias that actually closes the app (same as `Ctrl+D`
on an empty prompt).

### `/rename <title>`
Sets the session's title, real in both `-p` and the TUI (no live worker
thread needed -- it's a plain index-file write).

### `/fork` `[TUI-only live switch]`
Copies the current session's log to a new session id right now, leaving
the original untouched. In `-p`, this only gives you the new id to `-r`
into afterward; the TUI additionally switches the live session to the fork
immediately.

### `/stats [--models] [--tools]`
Bare: turns/cost/per-model/per-tool counts for *this* session only (from
already-in-memory log nodes -- cheap, synchronous). `--models`/`--tools`
switch to the richer cross-session telemetry aggregation (run off the UI
thread in the TUI) documented under `halo stats` in
`docs/COMMANDS.md`.

### `/tasks`
Lists every background Bash job started this session
(`run_in_background`, or a foreground command that outran its timeout),
with status and a truncated command line.

### `/rewind [step-id]` `[TUI-only]`
Restores the working tree (every file a Write/Edit touched, shadow-copied
step by step) to a recorded point. In `-p`, this needs the interactive
confirmation card and is refused with a note. With no `step-id` in the
TUI, opens a picker; a `RewindCard` always confirms before touching real
files. A Bash-made change is never shadow-copied and can't be rewound (see
`docs/ARCHITECTURE.md`).

### `/undo`, `/redo` `[TUI-only]`
One step back/forward through the same shadow history `/rewind` uses.

### `/keybindings`
Prints the fully-resolved `{context: {chord: action}}` keymap (built-in
defaults merged with `~/.claude/keybindings.json`).

### `/improve` `[TUI-only card review]`
In `-p`, prints a note pointing at the real headless surface,
`halo improve` (a *separate top-level subcommand* -- see
`docs/COMMANDS.md` -- never this slash command, since `-p` sessions must
never draft or write on their own). In the TUI, scans recent sessions,
drafts candidates with one model call, and reviews them one `ImproveCard`
at a time (see [Cards](#cards-and-their-keys)).

### `/intro` `[TUI-only]`
Replays the 2.0.0 launch intro (the typewriter line a fresh interactive
session shows above its first turn) -- a fresh `IntroLine` mounted at the
current transcript position and typed out again from scratch; any
keypress or a submitted prompt finishes it instantly, same as the
launch-time one. In `-p`, prints a note pointing at the interactive TUI
(there is no transcript to replay it into).

## Custom commands and skills

Discovered from the same directories Claude Code uses, in the same
precedence, alongside the built-ins above: `~/.claude/commands/`,
`.claude/commands/` (custom commands, one `.md` file per command, filename
minus extension is the invocation), `~/.claude/skills/`, `.claude/skills/`
(skills, one `SKILL.md` per directory). A skill's frontmatter may set
`disable-model-invocation: true` -- this hides it from the model's own
Skill *tool* only; the user can still always type it as `/name`. A skill
wins over a same-named custom command; neither ever overrides a built-in.

Both share one body-expansion pipeline
(`commands/registry.py::expand_command_body`):

1. **`$ARGUMENTS`** -> the raw text typed after the command name, verbatim.
   **`$0`..`$9`** -> the typed text's own whitespace/quote-split tokens,
   **0-based** (not 1-based). If neither placeholder appears anywhere in
   the body and arguments were actually given, `ARGUMENTS: <input>` is
   appended to the end automatically.
2. **`${CLAUDE_SKILL_DIR}`, `${CLAUDE_SESSION_ID}`, `${CLAUDE_PROJECT_DIR}`,
   `${CLAUDE_EFFORT}`, `${CLAUDE_PLUGIN_ROOT}`** -- substituted from the
   running session; a variable the caller can't supply (or whose value is
   empty) is left **unsubstituted** rather than silently becoming blank.
3. **`` !`cmd` ``** -- runs `cmd` and splices its stdout in place, gated by
   the *current* permission engine/mode exactly like an ordinary `Bash`
   call would be (`auto`/`bypassPermissions` allow it outright; the manual
   modes honor the user's own ask/deny rules) -- a command's own
   frontmatter `allowed-tools: Bash(...)` entries are registered as
   temporary session allow rules first, cleared again at the next user
   message. An unpermitted or nonzero-exit command **aborts the whole
   invocation**.

A command/skill body may also reference `@path` (a local file) or
`@server:resource` (an MCP resource) -- both are read and appended as a
**separate context snapshot**, never inlined into the text the model
actually receives as the command's own body.

MCP server prompts become their own slash commands automatically, named
`/mcp__<server>__<prompt>` -- typing one calls `prompts/get` and feeds the
result back as the turn's prompt, same "prompt"-kind contract as a custom
command.

## Prefixes in the prompt input

These are recognized only when they're the very first character(s) typed
on an otherwise-plain line (not inside a pasted multi-line block):

- **`/name [args]`** -- a slash command, resolved through the registry
  above (builtin > custom > skill > MCP-provided, by whichever added the
  name first).
- **`!cmd`** -- runs `cmd` through the Bash tool and the ordinary
  permission engine, **outside the model loop entirely**: the command and
  its result appear in the transcript as an ordinary tool card, but the
  model is never invoked for this turn at all. This is the TUI's own
  prompt-line prefix, distinct from a command/skill body's `` !`cmd` ``
  backtick form above (same permission gating, different trigger).
- **`@path`** and **`@path#L10`** / **`@path#L10-20`** -- attaches a file
  (optionally one line, or an inclusive line range) as a log snapshot
  before the turn is sent, resolved the same way the Read tool itself
  would resolve a path. The `@mention` text itself is left in the visible
  prompt verbatim; the file content rides alongside it, never replacing it.

Typing `/` or `@` as the first character of the current word opens a
completion popup (`Tab` to accept the highlighted entry): `/` completes
against every registered command name/alias; `@` walks the filesystem from
`cwd` one directory level at a time (hidden entries and common
build/VCS directories like `.git`/`node_modules`/`__pycache__` are pruned).

## Key bindings

These single keys are bound globally, always available regardless of
focus: `Ctrl+C` (interrupt the turn, or quit on a second press within
1.5s; copies a text selection instead if one exists), `Ctrl+D` (quit when
the prompt is empty, else forward-delete), `Ctrl+Q` (1.0.1: force quit --
exits even if the session is wedged; waits at most 2s for a clean
shutdown, then exits regardless), `Esc` (interrupt), `Shift+Tab` (cycle
permission mode through `default` -> `acceptEdits` -> `plan` -> `auto` ->
back to `default`; 1.0.1: if a permission card is still pending when the
mode lands on `auto`/`bypassPermissions` it's resolved as allowed right
away, `dontAsk` resolves it as denied), `Ctrl+L` (clear the transcript
view), `Ctrl+O` (toggle verbose -- shows every intermediate message/
expands tool cards), `Ctrl+R` (history search), `F1` (help), `Ctrl+P`
(command palette), `Ctrl+E` (edit the current prompt draft in `$VISUAL`/
`$EDITOR`). 1.0.1: `Ctrl+End` re-anchors the transcript to follow new
output (and clears the "N new" indicator); bare `End` does the same
EXCEPT while the prompt input has focus, where it means cursor-to-end-of-
line as usual (the overwhelmingly common case -- use `Ctrl+End`, or click
the "N new" indicator itself, to be sure it reaches the transcript).

`Ctrl+X` is a **chord prefix**: press it, then a second key within 1
second, while a small "which-key" overlay shows the live list of
continuations. The built-in chords:

| Chord | Action |
|---|---|
| `Ctrl+X Ctrl+S` | export the session |
| `Ctrl+X Ctrl+R` | rename the session |
| `Ctrl+X Ctrl+F` | fork the session |
| `Ctrl+X Ctrl+L` | open the resume picker |
| `Ctrl+X Ctrl+U` | rewind: undo |
| `Ctrl+X Ctrl+Y` | rewind: redo |
| `Ctrl+X Ctrl+K` | show session stats |
| `Ctrl+X ↓` | jump to the next sub-agent block in the transcript |
| `Ctrl+X ↑` | jump to the previous sub-agent block |

`~/.claude/keybindings.json` merges onto these defaults the same way
Claude Code's own keybindings file is documented to work: additive (only
name a context to change something in it), a `null` action **unbinds**
that key in that context; `/keybindings` prints the fully-resolved result.
See the `keybindings-help` skill for the file format itself.

Inside the chat area specifically: `Enter` submits, `Ctrl+J`/`Alt+Enter`
inserts a newline without submitting, `Tab` accepts the completion popup,
`Up`/`Down` walk prompt history (prefix-filtered). Inside the transcript:
`o` on a focused tool card opens a full-screen pager of its untruncated
output; `Ctrl+O` there toggles that card's own expanded/collapsed state
(distinct from the global verbose toggle).

**Auto-scroll (1.0.1).** The transcript follows new output (a streaming
reply, a growing tool card) automatically as long as it's scrolled to the
bottom. Scrolling up (`PageUp`, the mouse wheel, a drag-select) releases
that following and shows a "↓ N new" indicator in the status bar, counting
everything that's landed since; scrolling back to the bottom, pressing
`Ctrl+End`, clicking the indicator, or submitting a new prompt all
re-anchor to the bottom and clear the count.

## Cards and their keys

A card takes keyboard focus the instant it's mounted (so its own number/
letter keys win over the prompt input). **1.0.1:** typing free text while
a PermissionCard, PlanCard, or QuestionCard is pending now ANSWERS that
card directly, exactly like pressing its own deny/keep-planning/"Other"
key first -- a PermissionCard denies with the typed text as feedback, a
PlanCard keeps planning with it, a QuestionCard takes it as the "Other"
answer -- rather than becoming a steer on the turn underneath (which is
what typing while ANY other card, e.g. the `/effort` selector or a rewind
confirmation, still does).

- **PermissionCard**: `1`/`y` allow once, `2`/`a` allow for the session,
  `3` allow always (writes a rule to `.claude/settings.local.json`), `4`/
  `n`/`Esc` deny (borrows the prompt input for one line of optional
  feedback first, or just type the feedback directly -- see above). While
  pending, the status bar shows "permission needed: 1 yes · 2 session ·
  3 always · 4 no" and the prompt placeholder reads "1-4 answers the
  request above, or type why not".
- **QuestionCard**: an option list per question (arrow keys + `Enter`, or
  click); `Tab` moves to the next question when more than one was asked;
  `Esc` dismisses without answering; selecting "Other..." (or just typing
  an answer directly) borrows the prompt input for a free-text answer.
- **PlanCard**: `1`/`a` approve with auto-accept-edits mode after, `2`/`m`
  approve with manual mode after, `3`/`k`/`Esc` keep planning (borrows the
  prompt input for optional feedback, or just type it directly).
- **EffortCard** (`/effort` with no argument, 1.0.1): `Left`/`Right` or
  `h`/`l` move between this route's own accepted effort levels, `Enter`
  applies, `Esc` cancels and keeps the old value. A model with no
  adjustable effort shows a one-line card that closes on any key.
- **RewindCard** (`/rewind`/`/undo`/`/redo`): `1`/`y`/`Enter` restore,
  `2`/`n`/`Esc` cancel.
- **ImproveCard** (one per `/improve` candidate): `a` apply, `e` edit the
  drafted body in `$VISUAL`/`$EDITOR` then apply, `s` skip, `d` dismiss
  this exact candidate forever (content-hash-based, survives restarts),
  `q`/`Esc` stop reviewing, `o` opens the first cited evidence excerpt in a
  pager.
