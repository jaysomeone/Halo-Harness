# Troubleshooting

Symptom -> the `doctor` line that names it -> the fix. Run
`rolo-claude doctor` (or `doctor --work` at a Databricks box) first for
almost everything below -- every `[WARN]`/`[MISSING]` line already ends
with `-> fix: <command>` or `-> see: <reference>`. See
[DATABRICKS.md](DATABRICKS.md) for the full Databricks status-code table
and [ARCHITECTURE.md](ARCHITECTURE.md) for how compaction/retries work.

## Install

- **`error: externally-managed-environment` from `pip install --user -e .`**
  -- Debian/Kali's PEP 668 guard on the system Python. Fixes, in order of
  preference: use `uv tool install --editable .` instead (never hits this
  at all); install into your own venv
  (`python3 -m venv ~/.venvs/rolo-claude && ~/.venvs/rolo-claude/bin/pip
  install -e .`); last resort, `pip install --user -e . --break-system-packages`.
- **`rolo-claude: command not found` right after installing** -- `~/.local/bin`
  (where `uv tool install`/`pip install --user` puts the console script)
  isn't on `PATH` yet, especially for a non-interactive shell/tmux/`ssh
  host cmd`. `doctor`'s `local_bin_on_path` check names the exact rc-file
  line to add (`rolo-claude init` offers to add it for you on Linux);
  `~/.zshenv` for zsh, `~/.profile` otherwise (read by every invocation,
  not just interactive login shells).
- **Offline/work-box install fails with `ModuleNotFoundError: No module
  named 'setuptools'`** -- a fresh Python >=3.12 venv has no setuptools
  preinstalled; see `docs/harness/INSTALL.md`'s "Work box" section
  (`tools/vendor_wheels.py`, and specifically **do not** pass
  `--no-build-isolation`).
- **A different `python3`/version than expected gets picked up** -- both
  `uv tool install` and `pip install --user -e .` bind to whichever
  interpreter ran the install command; pin one explicitly with
  `python3.X -m pip install --user -e .` or `uv tool install --python 3.X
  --editable .`.

## PATH, `rg`, clipboard (Linux)

- **`rg` (ripgrep) not on PATH** -- `doctor` reports this as a `WARN`, not a
  failure: the Grep tool falls back to a slower pure-Python search engine
  automatically. `rolo-claude init` offers to install a static binary into
  `~/.local/bin` straight from ripgrep's own GitHub releases (Linux only);
  otherwise use your package manager (`doctor` names the one it detects on
  PATH).
- **`Ctrl+E`/`/improve`'s `e` do nothing** -- `$VISUAL`/`$EDITOR` isn't set;
  `export EDITOR=nano` (or your preferred editor) in your shell rc.
- **Copy (`Ctrl+C` on a selection) doesn't reach the system clipboard** --
  the primary mechanism is OSC 52 (works over SSH/tmux with no extra
  tooling); a terminal/multiplexer that doesn't relay it needs `xclip`/
  `xsel` (X11) or `wl-clipboard` (Wayland) on PATH as the best-effort
  fallback `doctor`'s clipboard check names.
- **tmux: mouse click-to-focus/drag-scroll doesn't work** -- `doctor`
  reports tmux mouse mode when `$TMUX` is set; `tmux set -g mouse on`. Hold
  `Shift` while dragging any time you want the terminal's own native text
  selection instead, regardless of mouse mode.

## MCP servers

- **A server shows `✗ Failed to connect`** -- `rolo-claude mcp get <name>`
  prints the real underlying error (a missing binary, a bad URL, ...);
  `rolo-claude doctor`'s MCP line shows the eager/lazy split and each lazy
  server's cache age.
- **A tool isn't visible to the model** -- check whether it's a deferred
  tool from a *lazy* server that hasn't been called yet (`ToolSearch` finds
  it by name/description without connecting anything), or excluded by
  `--tools`/a deny rule/a bare-tool-name `--disallowedTools` entry.
- **Startup feels slow with several MCP servers configured** -- lazy start
  is the default since H13 (a server connects on first real use, not at
  session start); an unusually slow server can be marked
  `"alwaysLoad": true` if you specifically want it eager, but that's the
  opposite of the fix for slow startup -- leave it lazy.
- **A newly-added/edited server doesn't show up mid-session** -- `/mcp`'s
  own reconnect re-reads `~/.claude.json`/`.mcp.json` from disk
  (`resync_from`); a plain reconnect of an already-known server does not
  pick up a config *edit* on its own.
- **A `.mcp.json` (project-scope) server never connects, but `mcp list`
  shows it configured** -- it's pending first-time approval; either add
  it to `enabledMcpjsonServers`/set `enableAllProjectMcpServers` in
  settings, or approve it interactively (`/mcp` in the TUI). A `-p` run
  auto-approves a `.mcp.json` server (there's no one to ask), but `mcp
  list`/`mcp get` deliberately do **not** auto-approve just to run a health
  check.

## Permission denials in print mode

`-p` has no interactive card -- an `ask`-category decision (per the mode
table in `docs/ARCHITECTURE.md`) is answered as a clean **deny**, naming
what would have been asked and a suggested rule, surfaced both as a
tool_result the model sees and in the `json` result object's
`permission_denials` array. This is expected, not a bug: for an unattended
`-p` run, pass `--permission-mode auto` (allow everything except an
explicit deny/ask rule) or pre-authorize specific commands with
`--allowedTools`.

## Upstream errors by status/category

Every route retries a **retryable** failure up to `MAX_RETRIES` (5) times
with a doubling backoff (1s, 2s, 4s, 8s, 16s, or a provider's own
`Retry-After` header when present, capped at 300s total wait) before
surfacing it; `CONTEXT_WINDOW_EXCEEDED` is **never** retried (compaction
runs instead -- see `docs/ARCHITECTURE.md`), and a reasoning-replay bug
(this harness sending a malformed replay of its own) is never retried
either, since retrying wouldn't fix it.

| Category | Typical status | Meaning | What to do |
|---|---|---|---|
| `AUTH` | 401, 403 | a bad/expired key or token | Databricks: see `docs/DATABRICKS.md`'s status table; OpenRouter/Anthropic: check the key in your env file |
| `RATE_LIMIT` | 429 | too many requests | retried automatically; if it persists, the route/model is genuinely saturated |
| `CONTEXT_WINDOW_EXCEEDED` | 400 (overflow-shaped message) | the request is too large for the model's window | auto-compaction should have caught this first; `/compact` manually, or `/clear`/start a new session |
| `MALFORMED_RESPONSE` | 400 (anything else) | a bad request body -- usually a model/family whose quirks aren't yet in `model_table.json` | file it; include the model id and the exact error text |
| `PROVIDER_FAILURE` | 5xx, or a 404/other | the upstream itself failed, or (Databricks) a wrong path/endpoint name | retried automatically for 5xx; a 404 usually means a stale/incorrect endpoint name -- `rolo-claude models --refresh` |
| `EMPTY_RESPONSE` | 200 with no usable content | the model returned nothing usable | retried once automatically; persistent emptiness usually means the model/route itself is having an outage |

## The subscription route (`cc:`)

- **`doctor` says "claude not found"** -- install Claude Code
  (https://claude.com/claude-code); `cc:` models are simply unavailable
  until then (every other route is unaffected).
- **"claude found but not logged in"** -- run `claude` once interactively
  and log in.
- **"logged in via api_key, not claude.ai"** -- an `ANTHROPIC_API_KEY` is
  set (or was, in the environment `claude auth status` itself saw), so
  Claude Code is using pay-as-you-go, not your subscription; `cc:` refuses
  to claim "available" in that case (the `ant:` route is the pay-as-you-go
  equivalent for the same nine model names). `unset ANTHROPIC_API_KEY &&
  claude` to log in with the subscription instead.
- **A `cc:` session behaves oddly after switching models mid-session** --
  expected: switching into/out of `cc:` primes/drains a capped plain-text
  summary of prior turns rather than true native history; `/clear`/`/fork`
  each start a genuinely new Claude Code conversation instead.

## Windows specifics

- **`--file <path>` on a path that doesn't exist prints "looks like Claude
  Code's file_id:relative_path cloud-resource form" instead of a plain "not
  a file" message** -- a Windows absolute path (`C:\...`) and Claude Code's
  cloud-file shape (`file_id:relative_path`) both contain a `:`; a real,
  existing local file is always checked first and always wins, but a
  *missing* file falls through to the ambiguous-shape check. Fix: double-check
  the path, or use a `--cwd`-relative path instead of an absolute one.
- **`--file`/`--add-dir`/`--mcp-config`/`--betas` swallow the prompt that
  follows them** -- these flags are `nargs="+"` (they accept more than one
  value) and will keep consuming bare words until the next flag; put
  `PROMPT` *before* any of them, or use `--` to separate.
- **The Bash tool doesn't work at all** -- Git for Windows isn't installed,
  or `CLAUDE_CODE_GIT_BASH_PATH` points somewhere wrong; `doctor` reports
  this as `[MISSING]` (not a soft warning) since the Bash tool, every
  `command`-type hook, and `` !`cmd` `` pre-execution all need it.

## Resetting caches

Every cache under `~/.rolo-claude/` is safe to delete and will be rebuilt
on next use: `models.json`/`dbx-endpoints.json`/`models-dev.json`/
`cc-models.json` (re-fetched by `rolo-claude models --refresh`),
`stats-cache.json` (re-derived from the session logs themselves, nothing
is lost), `mcp/tools-cache/*.json` (a lazy server just reconnects for real
on next use instead of using a cached tool list).

## Where logs live

- **Session transcripts**: `~/.rolo-claude/sessions/<project-slug>/<id>.jsonl`
  (`rolo-claude export`/`/export` reads these; `rolo-claude stats`/`/stats`
  aggregates them).
- **A running commentary of model/tool calls**: `--verbose` (print mode,
  stderr) or `Ctrl+O` (TUI, expands every tool card and shows reasoning).
- **The older proxy mode's own log**: `~/.claude-bridge/bridge.log`
  (`rolo-claude proxy` only -- redacted, rotated at 2 MB).
- **`-d`/`--debug` parses but does nothing yet** (see `docs/COMMANDS.md`'s
  not-yet-flags table) -- `--verbose` is the real equivalent today.
