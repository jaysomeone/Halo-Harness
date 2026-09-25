# rolo-claude

A standalone, Claude-Code-compatible agent harness: full-screen TUI, `-p`
print mode, the same config/session/tool conventions as the real `claude`
CLI, driving OpenRouter and Databricks-hosted models (including Databricks'
own Claude endpoints) instead of an Anthropic subscription. **Kali Linux is
the primary target platform** -- Windows is the secondary/build host. It
also ships the older `claude-bridge` proxy (drives the REAL `claude` binary
against those same providers) as the `rolo-claude proxy` subcommand -- see
**Proxy mode** near the end.

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

Put `OPENROUTER_API_KEY` in `~/.config/vibes-hacker/env` (`KEY=value`, `#`
comments, optional leading `export`; override the path with
`BRIDGE_ENV_FILE`) or export it yourself -- that's the only required setup
for OpenRouter models. Databricks credentials are discovered automatically,
same chain the whole project has always used: explicit `BRIDGE_DBX_BASE_
URL`+`BRIDGE_DBX_TOKEN` wins outright, then `ANTHROPIC_BASE_URL`+
`ANTHROPIC_AUTH_TOKEN`/the settings `env` chain when the host is a real
Databricks host, then `DATABRICKS_HOST`+`DATABRICKS_TOKEN`, then
`~/.databrickscfg`'s `[DEFAULT]` section, then (H8) `~/.claude/ucode-
settings.json` if Databricks' own `ug`/unity-gateway CLI already wrote one.
Nothing to configure if any of those already work for the real `claude` CLI
on the same box.

`rolo-claude doctor` is a read-only environment check (Python version,
`~/.claude` layout, env file, OpenRouter/Databricks reachability, catalog
cache ages, chrome/playwright/plugin detection, `rg`/`$VISUAL`/`$EDITOR`/
Bash-shell presence, clipboard backend); `rolo-claude doctor --work`
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

`--model`/`--small-model` pick the main/background-task model for the
session; `rolo-claude models --refresh` pulls OpenRouter's `/api/v1/models`
and models.dev's `api.json` into `~/.rolo-claude/` (context window, max
output tokens, vision support per model); without ever having run that, a
vendored fallback copy shipped inside the package (`rolo_claude/providers/
catalog/`) still lets profile resolution work completely offline. Databricks'
own endpoint list is cached by the same probe. Per-family prompt notation
(including a Kimi-specific block adapted from OpenCode's own) adjusts tool-
call/thinking conventions per model family automatically.

## Permissions and auto mode

Seven modes, same names and mode-table semantics as Claude Code:
`default` (`manual` is a display alias for the same thing), `acceptEdits`,
`plan`, `dontAsk`, `auto`, `bypassPermissions`. Grammar and modes ONLY --
**no classifier, no destructive-command list, no protected-path list, no
security-tool flagging, anywhere**: `auto` allows everything not matched by
an explicit deny/ask rule; `bypassPermissions` allows everything not
matched by a deny rule; the manual modes keep Claude Code's own semantics
because the user chose them deliberately, with no extra heuristics bolted
on. Rules are the same grammar Claude Code accepts (`Bash(git status:*)`,
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

**Hooks**: every hook event Claude Code fires (`PreToolUse`, `PostToolUse`,
`UserPromptSubmit`, `SessionStart`/`SessionEnd`, `Stop`/`SubagentStop`,
`Notification`, `PreCompact`, plus the newer `TaskCreated`/`TaskCompleted`,
`PreModelSwitch`/`PostModelSwitch`, worktree lifecycle events) and every
handler type a hook definition can invoke -- `command` (shell),
`prompt` (inject text), `agent` (run a sub-agent), `http` (call a URL),
`mcp_tool` (call a tool on a configured MCP server) -- are all wired.

**Skills and custom commands**: discovered from both user and project
directories exactly like `claude`; a skill's/command's body can reference
`@path` (a local file, read the same way a model's own Read call would) and
`@server:resource` (an MCP server's resource, read via `resources/read`) --
both are appended as a separate context snapshot, never inlined into the
submitted text. An MCP server's own prompts show up as `/mcp__<server>__
<prompt>` slash commands automatically.

**MCP**: stdio/http/sse transports, user/project/local/managed/plugin
scopes, the `.mcp.json` first-time-approval flow, lazy servers, always-load
servers, `ToolSearch` for a large/deferred tool set, resources and prompts
(`ListMcpResourcesTool`/`ReadMcpResourceTool`, `@server:resource` mentions,
`/mcp__server__prompt`), `/mcp` to inspect/reconnect mid-session.

**Browser**: `--chrome` (the claude-in-chrome extension's native-messaging
bridge) and `--playwright`/`--playwright-cdp`/`--playwright-headless`
(a real Chromium/Chrome/Brave via Playwright); a screenshot from either one
shows up as its own tool card in the TUI transcript, captioned with its
media type, real dimensions and size (e.g. "image/png, 1280x800, 84.2 KB")
-- not literal pixels: rendering actual terminal graphics would need a
cross-terminal protocol (Kitty/iTerm2/Sixel) this harness doesn't attempt.
The real image block is still what the model itself sees (subject to the
same vision/size gate as any other image); only the human-facing card is a
caption.

**Images/vision**: Read returns a real `image` content block (not just a
path string) for png/jpg/gif/webp when the active model's profile says it
supports vision, resized/downscaled to at most 1568px and 5MB (omitted with
a plain note instead when it can't be brought under that even after
resizing, or when Pillow isn't installed at all -- the `vision` extra,
`pip install 'rolo-claude[vision]'`, is optional, never required); an MCP
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
directory; `-r`/`--resume [ID]` resumes a specific one (or shows a picker);
`--fork-session` continues from one without overwriting it; `-n`/`--name`
labels a session. `/rewind` (and the TUI's undo) restores both the
conversation log and any file a Write/Edit touched, via the same shadow-
copy mechanism regardless of which tool changed the file. Auto-compaction
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
  `rolo-claude doctor` / `doctor --work` for environment issues; `-d`/
  `--debug` for verbose stderr.

## Security posture

- No local server/open port in the harness itself -- it's a direct
  in-process HTTP client to OpenRouter/Databricks, not a proxy something
  else connects to. (`rolo-claude proxy` is the one exception; see below,
  and its own security posture is unchanged from `claude-bridge`'s.)
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
