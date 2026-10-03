# Troubleshooting

Symptom -> the `doctor` line that names it -> the fix. Run
`halo doctor` (or `doctor --work` at a Databricks box) first for
almost everything below -- every `[WARN]`/`[MISSING]` line already ends
with `-> fix: <command>` or `-> see: <reference>`. See
[DATABRICKS.md](DATABRICKS.md) for the full Databricks status-code table
and [ARCHITECTURE.md](ARCHITECTURE.md) for how compaction/retries work.

## Install

- **`error: externally-managed-environment` from `pip install --user -e .`**
  -- Debian/Kali's PEP 668 guard on the system Python. Fixes, in order of
  preference: use `uv tool install --editable .` instead (never hits this
  at all); install into your own venv
  (`python3 -m venv ~/.venvs/halo && ~/.venvs/halo/bin/pip
  install -e .`); last resort, `pip install --user -e . --break-system-packages`.
- **`halo: command not found` right after installing** -- `~/.local/bin`
  (where `uv tool install`/`pip install --user` puts the console script)
  isn't on `PATH` yet, especially for a non-interactive shell/tmux/`ssh
  host cmd`. `doctor`'s `local_bin_on_path` check names the exact rc-file
  line to add (`halo init` offers to add it for you on Linux);
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
  automatically. `halo init` offers to install a static binary into
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

## Copy and paste

- **Selecting and copying**: select with the mouse (a drag, anywhere --
  the chat box or the transcript) and `Ctrl+C` copies it; `Ctrl+A` selects
  everything currently in the chat box, `Shift`+arrow keys extends a
  selection from the keyboard there. A selection always wins over `Ctrl+C`'s
  other job (interrupt/quit) and `Ctrl+X`'s other job (the session-actions
  chord prefix) -- `Ctrl+X` on a chat-box selection cuts it instead of
  opening the which-key overlay. 2.0.1 (W4c): a transcript selection always
  copies each touched widget's own SOURCE text (the assistant message's
  markdown, a tool card's full output, ...), never whatever happens to be
  on screen -- no box-drawing borders or `⏺`/`❯`/`✻` glyphs end up in the
  copy, and a selection spanning several messages/cards concatenates them
  in order.
- **Copying without a mouse**: `/copy` copies the last assistant reply,
  `/copy code` its last fenced code block (`/copy code 2` for the 2nd, in
  order), `/copy tool` the last tool call's full output. `y` on a focused
  tool card -- or inside its `o` pager -- copies that card's full output
  directly; `Y` copies the whole current turn (the last prompt plus
  everything produced for it so far), but only while the chat box does NOT
  have focus (a bare `Y` there just types a capital Y as normal). Every one
  of these shows a toast naming what was copied and how many characters.
- **Pasting**: a terminal's own native paste (bracketed paste -- most
  reliable) always works in the chat box already: `Ctrl+Shift+V` on most
  Linux terminals, right-click or `Ctrl+V` in Windows Terminal, or
  `Shift`+middle-click anywhere (X11's PRIMARY-selection paste; `Shift`
  bypasses the app's own mouse capture the same way it does for a
  transcript drag-select). Plain `Ctrl+V` ALSO works directly in the chat
  box now, reading the real system clipboard through whatever tool is on
  PATH (`xclip`/`xsel`/`wl-paste` on Linux, `pbpaste` on macOS,
  PowerShell's `Get-Clipboard` on Windows) -- if none is found, it notifies
  you to use your terminal's own shortcut instead rather than silently
  doing nothing. A paste of 4 or more lines becomes a `[Pasted text #1]`
  placeholder either way (the model still sees the full text).
- **Why a selection sometimes doesn't copy** -- see "PATH, `rg`, clipboard
  (Linux)" above and "Windows specifics" below: `doctor`'s "Clipboard
  backend" line always names the mechanism actually in use. On Linux/macOS
  the primary mechanism is OSC 52, which works over SSH with no extra
  tooling, but a terminal/multiplexer that doesn't relay it needs
  `xclip`/`xsel`/`wl-clipboard` (or `pbcopy`, already on every Mac) on PATH
  as the fallback. Line endings in a copy are always `\n`, even on Windows
  (every editor and terminal this has been checked against accepts it);
  set `clipboard.crlf: true` in `~/.halo/config.json` if you specifically
  need `\r\n`.
- **Does Ctrl+C close my terminal?** No. Ctrl+C is captured entirely inside
  halo -- the first press copies a selection if one exists, otherwise
  interrupts the running turn; a second press within the countdown quits
  HALO (never the terminal window itself, which stays open either way).
  Set `quit_on_double_ctrl_c: false` in `~/.halo/config.json` to turn the
  second-press quit off entirely, leaving `/exit`, `Ctrl+D` (on an empty
  prompt) and `Ctrl+Q` as the only ways to leave.

## MCP servers

- **A server shows `✗ Failed to connect`** -- `halo mcp get <name>`
  prints the real underlying error (a missing binary, a bad URL, ...);
  `halo doctor`'s MCP line shows the eager/lazy split and each lazy
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
| `PROVIDER_FAILURE` | 5xx, or a 404/other | the upstream itself failed, or (Databricks) a wrong path/endpoint name | retried automatically for 5xx; a 404 usually means a stale/incorrect endpoint name -- `halo models --refresh` |
| `EMPTY_RESPONSE` | 200 with no usable content | the model returned nothing usable | retried once automatically; persistent emptiness usually means the model/route itself is having an outage |

## DNS / connection failures (fail fast, 1.0.1)

A failure during the CONNECT phase itself -- unresolvable hostname
(DNS/`getaddrinfo`), connection refused, no route to host, or a hung/
black-holed resolver -- is a DIFFERENT class from the status-code table
above: it never rides the 1-2-4-8-16s retry ladder (phase 1 makes ONE
immediate retry with no delay, then surfaces it as terminal), and the
error message always names the host: `cannot resolve/reach <host> --
check the machine's network, DNS or VPN`. Bounded at 8 seconds
(`providers.http.open_upstream`'s own connect timeout, which now covers
DNS resolution too, not just the TCP handshake) -- before 1.0.1, an
unresolvable Databricks host could take upwards of 60 seconds to fail
(the OS resolver's own retry policy, completely unbounded from this
harness's side) on `-p`, a TUI turn, `init`'s own live pong, `models
--refresh`, `/models refresh`, and `doctor` alike. If you see this
message: check the VPN/network the host actually needs, not the model or
route -- retrying the same command won't help until connectivity itself is
fixed.

This fail-fast behavior is scoped to the CONNECT phase itself (H14c
fixpass finding 2) -- a failure AFTER the connection was already open (a
load balancer dropping a keep-alive, a mid-response reset while Databricks
queued the request) is a different, usually transient problem and rides
the ordinary 1-2-4-8-16s retry ladder instead, exactly like a 502 from the
upstream itself; it never shows the "cannot resolve/reach" wording above.

## `CERTIFICATE_VERIFY_FAILED` / TLS errors (1.0.1)

**"basic constraints of CA cert not marked critical"** -- seen fetching
models.dev or OpenRouter's catalog (`models --refresh`/`/models refresh`/
`init`), never Databricks (see why below): Python 3.13 turned on stricter
certificate verification (`ssl.VERIFY_X509_STRICT`) by default; a
network's TLS-inspecting proxy (common on a corporate VPN) re-signs
outside traffic with its own CA certificate, and some of those CA
certificates carry a technically-non-conformant (but universally accepted
by curl, browsers, Node, and Python before 3.13) certificate extension
that ONLY `X509_STRICT` rejects. Databricks itself is typically exempted
from that same inspection (which is why a live pong through it still
works when this fails) -- models.dev/OpenRouter are not. Fixed in 1.0.1:
every TLS context this harness builds (`providers.http.
default_tls_context`) clears `VERIFY_X509_STRICT` when the running
Python's `ssl` module has it at all, matching what every other HTTP client
on the same network already does -- nothing about certificate-chain or
hostname verification itself is weakened. A refresh that still fails this
way (an older, un-upgraded build) reports it as one line and keeps using
the vendored/cached catalog -- it's never fatal on its own. "Every TLS
context" genuinely means every one as of the H14c fixpass (finding 8):
WebFetch (its own `urllib.request.build_opener`) and MCP `http`/`sse`
servers (via `httpx`) were still on their own, unpatched default before
that -- the same error on a WebFetch call or an http/sse MCP server behind
the same inspecting proxy is fixed by the same build, no separate action
needed.

**"unable to get local issuer certificate"** -- a DIFFERENT problem: the
corporate CA itself isn't in this machine's trust store at all (not a
strictness issue). Export `HALO_CA_BUNDLE` (legacy `BRIDGE_CA_BUNDLE` still
honoured; `NODE_EXTRA_CA_CERTS`/`REQUESTS_CA_BUNDLE` are tried first if
set, then this one, first one present that loads wins) pointing at the
corporate CA's PEM file.

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

## `Function tools with reasoning_effort are not supported for gpt-6-sol` (1.0.1)

The gpt-6 family's own chat-completions route 400s when `reasoning_effort`
(any value but `none`) is sent alongside `tools` -- fixed in 1.0.1:
Databricks endpoints named `databricks-gpt-6-{sol,luna,terra}` OR
`databricks-gpt-5-6-{sol,luna,terra}` (models.dev's real catalog naming --
the H14c fixpass, finding 12, added explicit `model_table.json` rows for
both shapes; the original hotfix's bare "gpt-6" substring check matched
neither an OpenRouter model that happens to have "gpt-6" in its name, nor,
accidentally, the real `-5-6-` Databricks shape) force
`reasoning_effort: "none"` whenever the request carries tools (tool-less
requests are unaffected; `/effort`/the status bar still show whatever
level you picked). An untabled Databricks OpenAI-family endpoint that hits
the IDENTICAL wording ("Function tool..." + "reasoning_effort") on a
different model name retries once with `reasoning_effort` set explicitly
to `none`; if that retry ALSO fails, a second, independent retry strips
the field entirely rather than giving up (the H14c fixpass, finding 10) --
if you still see this exact message after both retries, report the
endpoint name.

## GLM seems to pause (2.0.1)

GLM (`z-ai/glm-5*` on OpenRouter, `databricks-glm-*` on Databricks) can go
quiet for a long stretch -- the status bar spinner keeps ticking, nothing
streams, and a steer you type seems to queue up and sit there ("↳
steering…") instead of taking effect. This is the model's own THINKING
phase, not a hang: GLM sends response headers immediately but then holds
the connection open, silent -- no reasoning text, no ping -- until it has
something to say. Two things make this worse at higher effort:

- **The effort level that's actually running may not be what you picked.**
  Databricks GLM endpoints accept exactly `low`, `high`, `max` for
  `reasoning_effort` -- anything else (`medium`, `minimal`, `xhigh`,
  `none`) the GATEWAY itself silently coerces to `max`, its slowest,
  priciest setting, with no error at all. Before 2.0.1 the harness didn't
  know this and let `medium` (etc.) through unclamped, so `/effort medium`
  silently ran as `max` every turn. `/effort` (no argument) now shows the
  value THIS route will actually send -- e.g. `medium (sent as high on
  this route)` -- and `/status`/`/context` list it under "this route
  changed" whenever a shown value differs from what's sent; run `/effort
  low` for the fastest setting. See
  [MODELS.md](MODELS.md#databricks-glm-201-glm-briefmd) for the full clamp
  table (OpenRouter GLM keeps the seven real Z.ai levels, unaffected).
- **A steer sent during the silent phase now cuts in immediately** (2.0.1,
  `steer.restart_when_silent` in `~/.halo/config.json`, default on): when
  no content has streamed yet for the in-flight call, typing something
  aborts that call and resends it with your text appended right away,
  rather than waiting for GLM to finish thinking first -- the transcript
  shows `↳ steering (restarting the model call)` so you know nothing from
  the aborted call was lost. Set it to `false` to go back to the old
  "cuts at the next chunk" behavior if you'd rather GLM finish its current
  thought first.

If the pause is still surprising with the above understood, `halo stats
--models --since 1d` (`--wide` for the full column set) shows TTFT p50/p95
and a count of calls that waited over 20s for their first token, per
model -- useful for telling "this is just how long GLM thinks at this
effort" from a genuinely stuck route.

## Nothing happens after I type: a permission card is waiting (1.0.1)

If the TUI seems to stop accepting input -- prompts you type appear to do
nothing, or only ever show up as a "↳ steering…" note -- check the status
bar first: a **"permission needed: 1 yes · 2 session · 3 always · 4 no"**
tag in the warning color means a `PermissionCard` is pending somewhere in
the transcript (possibly scrolled out of view before 1.0.1's auto-scroll
fix landed on your version). Press `1`-`4` to answer it, or just type why
not and press Enter -- 1.0.1 makes free text answer a pending permission/
plan/question card directly (deny-with-feedback for a permission ask, keep-
planning for a plan card, "Other" for a question), rather than silently
steering the turn underneath it. A `/`-prefixed submission (typed, or a
`/` completion accepted with Enter) always runs that command instead,
whatever card is pending (the H14c fixpass, finding 14 -- it used to deny
the tool with "The user said: /whatever"); the card itself is untouched
either way, still waiting for a later plain-text answer. Switching to
`auto`/`bypassPermissions`/`dontAsk` (`Shift+Tab`) re-decides an already-
pending card through the real permission engine right away (finding 4) --
an explicit `ask:` rule still asks even under `auto`, and a card you're
already answering (pressed `4`, mid-feedback) is left alone, never auto-
resolved out from under you. If the bottom bar's own spinner/elapsed time
is still ticking, the turn itself is not frozen -- something is just
waiting on you specifically. `Ctrl+End` re-anchors the transcript to the
bottom if it stopped following new output, and answering a card that
scrolled the view away from the bottom (a tall card, on a short terminal)
now re-anchors it automatically too, as long as you were following before
the card appeared (finding 15). If `Ctrl+C` twice doesn't exit (or gets
stuck itself), `Ctrl+Q` force-quits on its OWN timer -- a 2s head start for
a clean shutdown, then the process exits unconditionally 2.5s after that
regardless of what's still hung (finding 5; it no longer shares a flag
with `Ctrl+C`'s own quit path, so it still works even when THAT is the
thing that's stuck); Ctrl+Q also kills every background job
(`job_registry.kill_all()`) before that timer is even armed, so a
backgrounded `!cmd`/Bash job never outlives the process either, win or
lose on the ordinary quit path. Run with `--debug` and send `bridge.log`
if none of this explains what you're seeing.

## Is it frozen? (2.0.1)

Short answer: if ANY of the pieces below are still changing, the model is
working (or the network is slow) -- nothing is actually stuck. A real
freeze (the whole app stops responding to keys too) is the separate,
rarer case covered next, in "The TUI seems hung".

- **The live phase line**, one per model call, directly above wherever
  the answer is about to stream in: `✻ Sending request to <model>…` until
  the connection is made, `✻ Thinking… (12 s, no tokens yet)` once
  headers arrive but nothing has streamed yet, `✻ Thinking… (18 s · 412
  reasoning tokens)` once reasoning starts arriving, `✻ Writing… (23 s ·
  1.2k tokens)` once the answer (or a tool call) starts streaming,
  `✻ Waiting for model… (3 s)` between a tool result and the next call.
  The elapsed seconds tick every second on their own, driven by a timer,
  not by whatever the model happens to send -- a number that stopped
  moving for well over 30 seconds is the real signal to look at twice (see
  the next bullet), not silence by itself. On completion it collapses to
  `✻ Thought for 18 s (412 tokens)` when there was any reasoning, or
  disappears entirely when there wasn't; Ctrl+O expands the full reasoning
  text back out from the collapsed summary.
- **After 30 seconds with no data at all**, the phase line adds
  ` · no data for 30 s, Esc interrupts, typing steers` (refreshed every 10
  seconds: 40 s, 50 s, ...) -- naming the two things you can actually do
  about it right there, rather than just making you wait and wonder.
- **The status bar's own right-hand cluster** mirrors the same liveness
  while a turn runs: `⠋ thinking 18 s · ↓412`, `⠙ writing 23 s · ↓1.2k`,
  `⠹ tool Bash 4 s`, `⠸ waiting 3 s` -- the spinner frame and the elapsed
  number both change at least once a second, so a frozen terminal (nothing
  in this cluster moving at all, keys doing nothing either) reads
  completely differently from a model that's just being slow. It returns
  to idle the moment the turn actually finishes.
- **A running tool card** shows its own elapsed seconds right in the
  header (`⏺ Bash(pytest -q) · 12 s`) -- Bash also streams its live output
  underneath; Grep/Glob/WebFetch/MCP/Agent calls at least get the counter.
- **A running sub-agent** gets a one-line live summary in the parent's own
  transcript: `agent reviewer · thinking 9 s · 3 tools` -- its own phase
  and tool count, updating the same way.
- Tips cycle through the empty input box's placeholder (`/tips` lists them
  all) -- if even THAT has stopped changing after 15+ idle seconds with
  nothing typed and no turn running, something is more seriously wrong;
  see the next section.

If every one of these has been genuinely frozen -- not just slow -- for
more than about 15 seconds (no spinner movement, Ctrl+C/Ctrl+Q also doing
nothing), that's a real hang, not a slow model; see the next section.

## The TUI seems hung (1.0.1 part 2)

If the whole app stops responding -- no spinner movement, Ctrl+C/Ctrl+Q
both seem to do nothing for a few seconds -- a background watchdog thread
(independent of the UI's own event loop, so it keeps working even when
THAT is what's stuck) is already writing diagnostics for you: once the UI
thread's own heartbeat (bumped every second by the status bar's spinner
timer) goes stale for more than 15s, it dumps every thread's stack, the
named background-worker list, and the active screen to
**`~/.halo/hang-<UTC-timestamp>.log`** (one line also lands in
`bridge.log` naming the exact path), at most once a minute for as long as
the stall continues. Send that file along with a bug report -- the thread
stacks almost always show exactly which call is stuck (a lock shared with
the session thread, an unbounded wait, a subprocess with no timeout). On
POSIX, sending the process `SIGUSR1` (`kill -USR1 <pid>`) dumps the same
diagnostics on demand, without waiting for the 15s threshold at all. Run
with `--debug` for finer-grained tracing leading up to a hang -- every key
press (with the focused widget and active screen), window focus/blur, and
the start/finish/cancel of every named background worker all land in
`bridge.log`, so you can usually see the LAST thing that happened before
things went quiet.

## The starting permission mode isn't what I expected (1.0.1)

`halo doctor` prints the effective starting permission mode and
which layer decided it (`Permission mode: ... (source: ...)`). Precedence,
highest first: `--dangerously-skip-permissions` > `--permission-mode` (this
run's own flag) > `~/.halo/config.json`'s `permission_mode` (set
once via `halo init`'s own "Default permission mode" step, or
`halo config set permission_mode auto`) > `settings.json`'s
`permissions.defaultMode` > the hardcoded `default`. If you want every
session on this box to start in `auto` (never Claude Code's own `default`,
which asks before touching anything outside the working directory), either
re-run `halo init` or `halo config set permission_mode auto`
directly -- this never touches `~/.claude/settings.json`.

As of the H14c fixpass (finding 9), re-running `init` with `--yes` or
cancelling (Esc) out of its own permission-mode/default-model pickers
never overwrites a value already set -- it used to force `permission_mode`
to `auto` on every `--yes`/non-interactive run, and Esc at either picker
could write the literal `"default"`/an arbitrary other-provider's model
over whatever was actually configured. A fresh box with nothing chosen now
writes nothing for `permission_mode` (so `settings.json`'s own
`permissions.defaultMode` keeps working); `model` only ever falls back to
a guess when there is truly no existing value yet.

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
- **A copy doesn't reach the clipboard in a plain `cmd.exe`/PowerShell
  window** (2.0.1, W4c) -- OSC 52 (the primary mechanism everywhere else)
  is only relayed by Windows Terminal, which sets `WT_SESSION`; a bare
  `conhost.exe` console host (not inside Windows Terminal) doesn't relay it
  at all, so halo skips that write there and uses `clip.exe` (bundled with
  Windows) instead -- `doctor`'s "Clipboard backend" line names exactly
  which of the two is active. Either way the copy still happens; open the
  same session in Windows Terminal if you specifically want the OSC 52
  path (works over SSH/tmux too).

## Resetting caches

Every cache under `~/.halo/` is safe to delete and will be rebuilt
on next use: `models.json`/`dbx-endpoints.json`/`models-dev.json`/
`cc-models.json` (re-fetched by `halo models --refresh`),
`stats-cache.json` (re-derived from the session logs themselves, nothing
is lost), `mcp/tools-cache/*.json` (a lazy server just reconnects for real
on next use instead of using a cached tool list).

## Where logs live

- **Session transcripts**: `~/.halo/sessions/<project-slug>/<id>.jsonl`
  (`halo export`/`/export` reads these; `halo stats`/`/stats`
  aggregates them).
- **A running commentary of model/tool calls**: `--verbose` (print mode,
  stderr) or `Ctrl+O` (TUI, expands every tool card and shows reasoning).
- **The older proxy mode's own log**: `~/.claude-bridge/bridge.log`
  (`halo proxy` only -- redacted, rotated at 2 MB).
- **`-d`/`--debug`** (or `--debug-file PATH`): real DEBUG-level file
  logging for the whole run, TUI or print mode -- default
  `~/.halo/bridge.log` (secrets redacted by the same
  `RedactingFormatter` every route uses). In the TUI this also turns on
  per-key/focus/worker-lifecycle tracing (see "The TUI seems hung" above);
  `--verbose` is a separate, UI-only "expand every tool card" toggle
  (`Ctrl+O`), not a logging level.
- **A stuck/hung TUI**: `~/.halo/hang-<UTC-timestamp>.log` -- see
  "The TUI seems hung" above.
