# Changelog

All notable changes to Halo Harness (continuing `rolo-claude`, renamed at
version 2.0.0) are recorded here, newest first. Every entry older than the
rename keeps the `rolo-claude`/`rolo_claude` names it was written under --
history, not rewritten. This project does not (yet) follow strict semver
across the 0.3.x line -- each 0.3.0 milestone below was a working
checkpoint toward the single 0.3.0 release, not a separate published
version.

## [2.0.0] - 2026-10-01

The rename release: `rolo-claude` 1.0.1 continues unchanged, as its own
repository; this one, Halo Harness, is where every later feature lands.
Nothing about runtime *behavior* changes here beyond the migration and
alias notices below -- every 1.0.1 fix ships exactly as it was.

1. **Product renamed to Halo Harness**: console script `halo` (`python -m
   halo_harness`); distribution `halo-harness` on PyPI (`halo` itself is a
   spinner library -- never collide); Python package `halo_harness`
   (`rolo_claude` renamed in place, every import/reference updated).
   `rolo-claude` keeps working forever as a deprecated console-script
   alias -- one notice line to stderr (`halo: 'rolo-claude' is deprecated,
   use 'halo' instead`), then runs exactly like `halo`, same process, same
   parser, including `rolo-claude proxy`. `halo proxy ...` is the renamed
   spelling of what `rolo-claude proxy`/`claude-bridge` already did
   (`bridge.py`, unchanged).
2. **State directory migration**: the default state dir is now `~/.halo`
   (was `~/.rolo-claude`). The first time anything in a fresh Halo Harness
   process resolves it, an existing `~/.rolo-claude` is renamed (never
   copied) to `~/.halo`, announced with one stderr line; every call after
   that (in this or any later process) just finds `~/.halo` already there
   and says nothing further. No link is created at the old location (`rm
   -rf ~/.rolo-claude/` with a trailing slash follows a symlink/junction
   and would empty `~/.halo` right along with it) -- a still-installed
   `rolo-claude` 1.0.1 must never be run again after this, since it would
   start from a truly empty `~/.rolo-claude`. A rename that fails (a
   locked file on Windows, a read-only/cross-device home on Linux) prints
   one warning and keeps using `~/.rolo-claude` for that run instead of
   silently losing state in a `~/.halo` that was never actually populated;
   when the rename instead loses a race to a concurrent process that
   migrates first, the `~/.halo` it left behind is adopted silently.
   `halo doctor` reports a standing WARN whenever both directories end up
   holding real data. `BRIDGE_STATE_DIR` keeps overriding the state dir
   outright, exactly as before (no migration logic runs when it's set).
   Test seams (`BRIDGE_TEST_HOME`, `BRIDGE_TEST_NO_BACKGROUND_NET`,
   `BRIDGE_TEST_CC_AUTH_STATUS`, and `BRIDGE_STATE_DIR` itself) keep their
   exact names -- no `HALO_` twin for these, by design.
3. **Env file**: `halo init` now writes `~/.config/halo/env` (was
   `~/.config/vibes-hacker/env`), copying the old file's content forward
   (directory 0700, file 0600) the first time it writes on a box where
   only the old one exists yet, so nothing already configured there is
   orphaned. The old file is never modified or renamed (other tools on
   the same box read it); the copied-forward new file starts with an
   import marker line, and once that marker is present the old file is
   no longer consulted, so a key you delete from the new file stays
   deleted. Without the marker (a hand-written new file) reading still
   checks the new file, then the old one, so a credential that only
   ever lived in the old file keeps working even before `halo init`
   runs again. `HALO_ENV_FILE` is the new override
   name; legacy `BRIDGE_ENV_FILE` keeps working (an explicit override
   always wins outright, with no blending between the two files).
4. **Env vars**: every harness-owned `BRIDGE_*`/`ROLO_CLAUDE_*` knob
   (`OPENROUTER_BASE_URL`, `ANTHROPIC_BASE_URL`, `DBX_BASE_URL`/
   `DBX_TOKEN`, `MODEL`/`MODEL_SMALL`, `ENV_FILE`, `CLAUDE_EXE`, `DUMP`,
   `CA_BUNDLE`, `THEME`) now has a canonical `HALO_*` name, checked first;
   the old name still works, logging one DEBUG line the first time it's
   what actually supplied the value. See `docs/CONFIG.md`'s "Every
   environment variable" table for the full list.
5. **Launch intro**: a fresh interactive launch (TUI, including
   `--continue`/`--resume`) types out `I am just a copy, of a copy, of a
   copy... halo 2.0.0` character by character above the first turn, like
   someone typing it, with a block cursor while typing; any keypress or a
   submitted prompt finishes it instantly, and the prompt input keeps
   focus throughout so nothing typed during it is ever lost. Never shown
   in print mode, a `--demo` run, or when stdout isn't a real terminal;
   `"intro": false` in `~/.halo/config.json`, or `--no-intro`, turns it
   off for good; `/intro` replays it mid-session.
6. **Compatibility odds and ends**: a project's `.rolo-claude/team.json`
   (checked into a repo before this rename) is still found, with one
   deprecation line, when `.halo/team.json` is absent. `/improve`'s
   provenance marker and the `~/.local/bin`-on-PATH rc-file marker both
   still recognize the exact text 1.0.1 wrote (a new write always uses the
   new text) -- so a file or rc-file line 1.0.1 already produced is never
   mistaken for user-authored, and `halo init` re-run on an upgraded box
   never appends a second PATH block. The `stream-json` `system/init` line's
   version field is now `halo_harness_version`; `rolo_claude_version` (same
   value) ships alongside it for a script that already reads the old name.
   The state directory itself (item 2) gets no compatibility link of its
   own, unlike these -- no link is created there, on purpose, since a
   leftover link would make `rm -rf ~/.rolo-claude/` dangerous.
7. **Docs/tests**: README/CHANGELOG/docs/briefs/guard tests all updated to
   the new names; this entry is the only CHANGELOG section written under
   them -- every entry below keeps the name it was written under.
8. **Shadow-repo long paths on Windows**: the `/rewind` git-shadow repo
   (`~/.halo/sessions/<slug>/<session_id>/shadow/`) now sets `core.
   longpaths true` right after `git init` on Windows -- the shadow repo's
   own path plus a mangled absolute source path underneath it can exceed
   Windows' 260-character limit under a long home or cwd, which git itself
   refuses without this setting.

## [1.0.1] - 2026-09-30

A hotfix release from the owner's first real 1.0.0 run on the Kali work VM
(DNS down, then fixed), a live `/model` screenshot from it, and several
days of live use on a work VM after that. Nineteen fixes, all with pinning
tests:

1. **Completion popup keys**: with the `/`/`@` completion popup open,
   Up/Down move the highlight, Tab/Enter accept it, Esc closes it, typing
   still filters -- the prompt's own TextArea no longer swallows Up/Down
   (history nav) while the popup is open.
2. **Fail fast on DNS/connection failures**: a hung/unresolvable host used
   to take upwards of 60s to fail (the OS resolver's own retry policy,
   completely unbounded); every connect (DNS included, not just the TCP
   handshake) is now capped at 8s, and a DNS/refused/unreachable failure
   never rides the 1-2-4-8-16s retry ladder (one immediate retry, then
   terminal) -- across `-p`, the TUI, `init`'s pong, `models --refresh`,
   `/models refresh`, and `doctor`. The error names the host: "cannot
   resolve/reach `<host>` -- check the machine's network, DNS or VPN".
3. **`/models` shows the cache instantly**: bare `/models` (TUI or
   headless) and `rolo-claude models` (CLI) never touch the network any
   more -- a bug in the TUI's own do-refresh check made even a BARE
   `/models` refresh over the network. Only `/models refresh`/`/dbx`/
   `--refresh` do; a failed refresh still shows the (unchanged) cached
   table plus one line naming the error.
4. **Cache carries the gateway types**: every cached row now shows a real
   `path` (family default, from `dbx_routing`) and `chat-capable` counts
   the endpoint's own `task == "llm/v1/chat"` (never a name-based guess or
   `bool(api_types)`, which used to disagree with the table on the same
   rows). An old cache from before this milestone (no `api_types` at all)
   is detected and migrated on next background refresh, else shown as
   "path unknown (refresh needed)" rather than a silently wrong guess.
5. **Interactive model picker in `init`**: a filterable arrow-key list of
   chat-capable endpoints grouped by family (a numbered list with no real
   terminal) offered right after the catalog is refreshed; `--yes`/
   `--model` skip it. The TUI's own `/model` picker got the same grouping.
6. **Bare endpoint names**: `--model databricks-kimi-k3`/`/model
   databricks-kimi-k3`/`init --model databricks-kimi-k3` resolve exactly
   like `dbx:databricks-kimi-k3` whenever the name matches a cached
   endpoint (even a workspace-custom name with no `databricks-`/
   `system.ai.` shape) or that generic shape; an unresolvable name
   suggests the three closest cached endpoints instead of a bare error.
7. **List-dialog keyboard forwarding**: every filter-`Input` + `OptionList`
   dialog (`/model`, `/resume`, the command palette, `init`'s own picker)
   forwards Up/Down/PageUp/PageDown/Home/End to the list and Enter to the
   highlighted row, instead of the `Input` silently swallowing them (Down
   in `/model` used to do nothing at all).
8. **`/model` rows, single line**: grouped with one header per group
   instead of an inline `[group]` tag, each row ellipsized rather than
   wrapped (a long ref/path used to break the column alignment).
9. **`cc:` group gating**: shown only when `claude auth status` reports an
   actual claude.ai login -- a Databricks work box's own settings-driven
   `claude` login used to list nine `cc:` models that would refuse at
   request time.
10. **Unified `ctx`/`out`/price columns**: `/model`, `/models`,
    `rolo-claude models`, and `init`'s own picker now show the SAME
    context/output/USD-per-1M-token columns for every provider, Databricks
    included (previously `path=... dbu=?` only, with "?" everywhere) --
    normalized units (`200000` -> `200k`, `1048576`/`1050000` -> `1M`),
    blank (never `?`) for anything not published. Databricks pricing comes
    from models.dev when that endpoint is listed there, else
    `model_table.json`'s own context (prices blank), else blank.
    `fetch_models_dev` now sends a real `User-Agent` (models.dev's CDN can
    403 a header-less default urllib agent).
11. **`VERIFY_X509_STRICT` cleared on every TLS context**: Python 3.13
    turned this on by default; a corporate TLS-inspection proxy's
    re-signing CA can carry a technically-non-conformant certificate
    extension that only this stricter mode rejects (curl/browsers/Node/
    pre-3.13 Python all accept it) -- seen live as `models --refresh`
    failing with `CERTIFICATE_VERIFY_FAILED: ... basic constraints of CA
    cert not marked critical` on a work VPN while a Databricks pong (a
    different, inspection-exempt host) kept working. Fixed for every
    HTTPS call this harness makes; certificate-chain/hostname verification
    itself is unchanged.
12. **`init` is provider-first**: step 1 is now "select a provider to set
    up" (Databricks/OpenRouter/Anthropic API/Claude subscription) with a
    status tag per row, instead of a home/work/claude "preset" naming a
    bundle of choices ("work" WAS just Databricks, confusingly named).
    Each provider runs its own credentials -> catalog -> model-pick ->
    live-pong path, offers to set up another, and picks one overall
    default when more than one ends up configured. `--provider` (repeatable)
    drives it non-interactively; the old `--preset home|work|claude` still
    works, as a deprecated one-line-noticed alias.
13. **Status bar shows real context/cost for every model, Databricks
    included**: `ctx 12k/1M 1%` (or `ctx 12k` alone with no known limit)
    and a real `$0.0123` (or `in 12k out 3k` token totals with no known
    price) replace the permanent `ctx ?`/`$?` a Databricks model used to
    show. `CostMeter` no longer hard-codes Databricks cost as always
    unknown -- it computes a real fallback cost whenever a models.dev price
    was resolved for that endpoint, same formula every other provider
    already used. The model label truncates from the left on a narrow
    terminal so ctx/cost stay visible; cwd shrinks first.
14. **The transcript follows new output**: streamed text, thinking blocks,
    and expanding tool cards used to leave the view wherever the user's own
    prompt was after the first line -- `Transcript` now anchors to the
    bottom (Textual's own `anchor()`) while "following," releases on a
    manual scroll up (showing a "↓ N new" count in the status bar), and
    re-anchors on `Ctrl+End`, a click on that indicator, or a new prompt.
15. **A pending permission/plan/question card intercepts free text**:
    typing while one is pending used to always steer the turn underneath it
    silently -- it now answers the card directly (deny-with-feedback, keep-
    planning, or "Other", matching Claude Code's own behavior), and
    switching to `auto`/`bypassPermissions`/`dontAsk` (`Shift+Tab`) now
    resolves an already-pending permission card immediately instead of
    leaving it stuck. The status bar shows a "permission needed: ..." tag
    and the prompt placeholder changes while one is pending. This, combined
    with fix 14, was the real mechanism behind a reported TUI "freeze."
16. **`init` also asks for a default permission mode**: `auto` (recommended),
    `acceptEdits`, `default`, `plan` -- written to `~/.rolo-claude/
    config.json`'s own `permission_mode` key, now a layer in the starting-
    mode precedence chain between `--permission-mode` and settings.json's
    `permissions.defaultMode`. `doctor` prints the effective mode and its
    source.
17. **Worker-group isolation and a hard Ctrl+Q**: the git-branch and
    statusline timers (`exclusive=True`, no `group=`) were silently
    cancelling every other in-flight background worker (models refresh,
    catalog refresh, session list, `/improve` drafts, ...) every 5 seconds
    -- each now runs in its own named group, as does every `tui/slash.py`
    worker. `Ctrl+Q` force-quits within 2 seconds even if the session is
    wedged; a Key event with no focused widget now self-heals focus back to
    the prompt (or the active modal's own first focusable widget).
18. **Databricks Claude foundation endpoints reject `xhigh`**: a Databricks
    Claude route's `output_config.effort` only accepts `low`/`medium`/
    `high`/`max` -- a session with no more specific effort set now defaults
    every Anthropic-family route (`cc:`/`ant:`/Databricks Claude foundation)
    to `high` instead of leaving it unset, and `xhigh` is clamped to `max`
    on any route that doesn't list it (a `/model` switch re-clamps too). A
    live 400 naming the effort field retries once with it stripped before
    the turn is treated as failed.
19. **`/effort` actually changes the level**: it used to ignore its own
    argument and only ever echo a value snapshotted at session start
    (typing `/effort medium` kept showing whatever was configured before
    launch). `/effort <level>` now sets the session's effort immediately
    (clamped per fix 18), remembered for the rest of the session; bare
    `/effort` in the TUI opens an inline Claude-Code-style selector card
    (the route's own accepted levels in a row, Left/Right to move, Enter to
    apply, Esc to keep the old value) instead of only printing text. The
    status bar shows a short effort tag next to the mode.
20. **gpt-6 rejects `reasoning_effort` alongside tools**: every turn on
    `dbx:databricks-gpt-6-sol` 400'd ("Function tools with reasoning_effort
    are not supported for gpt-6-sol... set reasoning_effort to 'none'") --
    any endpoint whose name contains `gpt-6` now sends `reasoning_effort:
    "none"` explicitly whenever the request carries tools (a tool-less
    request, or `--effort`/`/effort` on any other family, is unaffected); a
    live 400 with this wording on a different OpenAI-family endpoint
    retries once with the same explicit override.
21. **Enter runs a completed `/` command** (Claude Code parity): with the
    completion popup open, one Enter both inserts the highlighted `/`
    command and runs it (`/mo` + Enter opens the model picker; `/models` +
    Enter runs it). A second Enter used to be required, which read as
    "/models does nothing". Tab still only inserts, and an `@` path
    completion is only ever inserted.
22. **`vendor/model` needs both halves**: `/effort`, `vendor/` and refs
    containing whitespace are refused locally with the usual no-route
    error instead of being accepted as an OpenRouter model and failing
    upstream with a 400 on the first request.

No behavior changes beyond the fixes above; `__version__` is the only
non-test/non-doc change outside the files each fix's own commit touched.

### Review fix pass (same day)

A follow-up review of the fixes above, before this hotfix shipped, found
18 further issues. The following were fixed, each with its own pinning
test; two low-risk minors (a second pending-card slot for concurrent
sub-agent asks, and the connect-timeout retry/leak detail) are deferred --
see "1.0.1 follow-ups" in `docs/harness/RECOMMENDATIONS.md`.

- **`/model` no longer freezes the TUI**: it opened on the UI thread,
  re-read and re-parsed the full `models-dev.json` cache once PER
  Databricks endpoint, and spawned `claude auth status` synchronously --
  2-5s measured on a real catalog. The picker now opens through a worker
  thread; the models.dev cache loads once per `/model` open, not once per
  row; the `claude auth status` check is a cached read, primed once by a
  startup worker, never spawned by `/model` itself.
- **Post-connect network failures ride the retry ladder again**: a
  dropped keep-alive or mid-response reset (after the TCP connection was
  already open) carried the same "cannot resolve/reach" marker a genuine
  DNS/connect failure uses, so it skipped the retry ladder entirely
  instead of riding it like the pre-hotfix 502 behavior. Only a real
  connect-phase failure carries that marker now.
- **Focus self-heal stays on the active screen**: a modal with nothing
  focusable of its own (the pager `o` opens) no longer heals focus to a
  pending card on the screen underneath it, which used to let the modal's
  own keys (`q`/`o`) vanish in favor of the hidden card's (`1`/`y` could
  silently approve a permission the user never saw).
- **`Shift+Tab` re-evaluates a pending permission card for real**:
  switching mode now re-runs the permission engine for the parked
  request instead of blindly allowing/denying it -- an explicit `ask:`
  rule still asks under `auto` (only `bypassPermissions` skips it),
  `acceptEdits` now resolves a pending in-workdir edit/write card, and a
  card the user is already answering (pressed `4`, typing why) is left
  alone instead of being silently finished out from under them.
- **`Ctrl+Q` has its own flag**: it no longer shares `_quitting` with
  double-`Ctrl+C`/`/quit`, so it still works when THAT path is the one
  that's hung; a daemon timer force-kills the process 2.5s after
  `self.exit()` regardless of what Textual/asyncio is still waiting on.
- **Databricks cost is billed once per token, and repriced on `/model`**:
  cached/reasoning tokens were counted at both the full rate and their own
  discounted rate; switching to a new Databricks endpoint mid-session now
  updates the cost meter's own rates instead of billing new usage at the
  old model's prices.
- **OpenRouter prices show again**: `models.json` stores them as numeric
  strings; every price reader now accepts that (`/model`, `rolo-claude
  models`, and `init`'s picker all went blank otherwise).
- **WebFetch and MCP http/sse use the shared TLS policy**: both were
  outside fix 11 above (a different code path -- urllib's own
  `build_opener`/httpx's own client, neither going through
  `open_upstream`).
- **`init` never overwrites an existing `permission_mode`/`model`**:
  `--yes` and Esc now keep whatever's already configured (writing nothing
  at all on a fresh box) instead of forcing `auto`/a fixed-order guess
  over what was actually there.
- **An effort-rejection 400 no longer wastes its one retry on the wrong
  fix**: the gpt-6 "none with tools" classifier (fix 20 above) matched too
  broadly -- any 400 naming both "reasoning_effort" and the bare word
  "param". The general strip-the-field retry can now run after a failed
  "none" retry instead of being blocked by a shared one-shot flag, and a
  successful strip resets the session's effort so later steps don't keep
  re-sending the rejected value first.
- **The Anthropic "high" default (fix 18 above) is scoped to
  adaptive-capable models**: a non-adaptive route (Haiku 4.5, Sonnet 4.5
  or older) no longer gets a thinking budget nearly as large as
  `max_tokens` by default (capped at half of it instead); the version
  parser reads hyphenated ids (`claude-sonnet-4-6`) correctly; the
  `tool_choice: any` repair retry drops thinking (Anthropic rejects that
  combination outright).
- **The gpt-6 "none with tools" rule (fix 20 above) is Databricks-only**,
  driven by explicit `model_table.json` rows for both real `models.dev`
  name shapes -- it no longer also fires for an OpenRouter model that
  happens to have "gpt-6" in its name.
- **Chat-dialect routes no longer accept `max`** (an Anthropic-only
  level) from `/effort`; the retry also recognizes OpenRouter's nested
  `reasoning.effort` 400 wording and a generic "Invalid value" one.
- **Commands typed while a card is pending run instead of denying the
  tool**: a `/`-prefixed submission always goes to slash handling first,
  whatever card is pending; pasted feedback carries the real content, not
  the `[Pasted text #n]` placeholder; the `/effort` card no longer
  intercepts typed text at all.
- **A tall card's auto-scroll is restored once answered**: focusing a
  card too large to fit releases the transcript's bottom anchor (fix 14
  above; Textual's own scroll-to-center on focus); `clear_pending_card`
  now re-anchors when the user was following before the card appeared.
- Wording: `init`'s "default" permission mode no longer implies a risk
  check this project doesn't have ("ask before edits and non-read-only
  tools", not "...or otherwise risky").
- **Rate-limit and stats token counts include reasoning again**: since
  `map_usage` reports reasoning tokens separately (so they are never
  billed twice), the Databricks output-tokens-per-minute tracker and
  `stats` `tokens_out` now add them back, or a reasoning-heavy reply
  under-counted and the 429s the tracker prevents came back.
- **MCP over HTTP prefers the pinned `httpx2`** and falls back to classic
  `httpx` only when `httpx2` is absent, so an environment carrying both
  never hands the MCP SDK the wrong client type.
- **Adaptive thinking is gated to Opus 4.6+**: Opus 4.1 and 4.5 (both
  served on Databricks) keep `budget_tokens`, as Sonnet below 4.6 already
  did; every `opus` id used to count as adaptive. Bedrock-style ids that
  put the version before the family (`us-anthropic-claude-3-7-sonnet-...`)
  now parse their real version instead of the snapshot date.

`__version__` is unchanged.

### Part 2 (same release): tabbed provider setup, hang diagnostics, provider enablement, effort polish

Finishes the 1.0.1 round -- item 13's tabbed provider view, item 15's
remaining hang diagnostics, the new item 21 provider-enablement model, and
item 22's remainder -- plus five minors the reviewer logged after the fix
pass above. All with pinning tests; `__version__` stays 1.0.1.

- **`init`'s provider setup is now tabbed** (item 13): one tab per provider
  -- Databricks, OpenRouter, Anthropic API (key), Claude Code subscription,
  and a new fifth tab, TypeSafe (stores `TYPESAFE_API_KEY` only, for a
  later feature -- no routed models yet) -- replacing the one-provider-at-
  a-time picker loop on a REAL terminal only (a piped/non-tty run, every
  existing script, keeps the exact old sequential-picker-plus-numbered-
  fallback path, byte for byte). Shift+Tab and Left/Right (the latter only
  while the tab bar itself has focus -- a focused credential field's own
  Left/Right still moves the cursor) switch tabs; Up/Down move between a
  tab's own fields/button; Enter activates a field or button; Esc leaves
  the dialog and `init` proceeds to the default-model/permission-mode
  steps exactly as before. Each tab opens showing what the harness already
  picked up ("picked up from `<source>`: `<masked>`", or a discovered
  Databricks host with just the token field left) with the status line
  updating live as a value is typed; a bounded background probe (the same
  8s connect-only cap item 2 introduced) shows "reachable" / "unreachable:
  `<reason>`" / "not set up"; finishing a tab with real credentials fetches
  and caches that provider's catalog right then, off the UI thread. A
  Textual runtime failure (no real terminal after all) falls back to the
  old sequential picker instead of crashing `init` outright.
- **Provider enablement** (item 21, new): a provider's models now reach
  `/model`/`rolo-claude models`/the `init` default pick/`doctor` only once
  it's EXPLICITLY enabled -- by completing its tab above, by `rolo-claude
  providers enable <name>`/`/providers enable <name>`, or (one-time only,
  for a box with credentials from before this existed) an automatic
  migration `doctor`/`init` run the first time. Detected credentials or a
  `claude.ai` login never enable a provider on their own -- a `cc:` group
  no longer appears in `/model` just because `claude auth status` reports
  a real login (item 9's own gate), and a hand-typed ref for a disabled
  provider is refused with a one-line message naming the fix; a detected-
  but-disabled provider shows one dim hint line in `/model` instead of a
  selectable row. `rolo-claude providers`/`/providers`: a table (enabled,
  credentials found and where, reachable, cached model count) plus
  `enable`/`disable`/`setup <name>`. Labels used everywhere (the tabs,
  `/model`'s group headers, `doctor`, `/providers`): `dbx:` Databricks,
  `or:` OpenRouter, `ant:` Anthropic API (key), `cc:` Claude Code
  subscription -- the full prefix table is in `docs/MODELS.md`.
- **Hang diagnostics** (item 15 remainder): a daemon watchdog thread
  (started from `on_mount`, completely independent of the UI's own event
  loop -- the point is that it keeps working when THAT is what's stuck)
  polls a heartbeat `_tick_spinner` bumps every second; once it's stalled
  past 15s it dumps every thread's stack (`sys._current_frames()`), the
  named background-worker list, and the active screen to
  `~/.rolo-claude/hang-<UTC>.log` (one line also to `bridge.log`), at most
  once a minute while it persists. `SIGUSR1` (POSIX) dumps the same
  diagnostics on demand. `--debug` now also traces, to `bridge.log`: every
  Key event (with the focused widget and active screen), window
  focus/blur, and the start/finish/cancel of every named background
  worker. An audit of `_drain` and every `call_from_thread` call site found
  no unbounded blocking of the UI thread (every `subprocess.run` already
  had a timeout and already ran on its own worker thread; no `Worker.
  wait()`/shared-lock call exists on this path) -- the watchdog above is
  the backstop for whatever that audit missed. (The 1Hz heartbeat timer
  itself is now wired through a lambda instead of the bound `_tick_spinner`
  method directly -- `set_interval` keeps calling whatever reference it was
  first given, so a test that needs to simulate a stalled heartbeat by
  replacing the instance's own `_tick_spinner` was silently still ticking
  the original every second; no production behaviour changes, only what a
  test can now actually stop.)
- **`/effort` and the status bar show `none (tools)`** on a route where the
  gpt-6 table rule or a LEARNED per-endpoint rule applies (item 22
  remainder): the EFFECTIVE value actually sent on a tool-carrying turn,
  not the configured one that route would ignore anyway, with the
  selector card's own description line (and `/effort`'s own source line)
  explaining why. The learned per-endpoint `reasoning_effort_with_tools`
  rule (a live 400 proving an untabled Databricks endpoint also needs it)
  now persists to `~/.rolo-claude/learned-rules.json`, not only in memory
  for the rest of one session -- a NEW session against the same endpoint
  sends it correctly from its very first request, never re-paying for the
  failing one. A `model_table.json` row always wins over a learned entry.
- **Five minors from the reviewer's post-fix-pass notes**, each with its
  own pinning test: `Ctrl+Q` now calls `session.job_registry.kill_all()`
  immediately, before its `os._exit` timer is even armed, instead of only
  deep inside the ordinary (possibly also-wedged) `controller.quit()`
  path it exists to route around; `clamp_effort` clamps an unsupported
  `max` to `xhigh` on a chat-dialect route that has `xhigh` but no `max`
  (the mirror of the existing `xhigh`-without-`max` case), instead of
  falling all the way to the bland `medium` default; `/effort`, `/rewind`
  and `/improve` show a one-line note instead of opening their own card
  while any card (a live permission ask, most importantly) is already
  pending; `init`'s per-provider default-model step no longer overwrites a
  custom model already configured when it merely differs from that
  provider's own hardcoded default (only the final cross-provider pick
  previously preserved a custom value) -- an explicit `--model` this run
  still wins outright; `cached_auth_status_is_stale`'s own TTL is now
  actually consulted (bare `/model`'s own stale-Databricks-catalog-refresh
  worker does the same for the claude-auth-status cache), so a `claude.ai`
  login/logout during a long session is reflected the next time `/model`
  opens, not only after restarting the whole app.
- **State-dir scoping** (found during this round's own fix pass): a test
  module that builds a real `Session`/`Controller` without scoping
  `BRIDGE_STATE_DIR` itself used to only avoid leaking into the REAL
  `~/.rolo-claude/sessions` because an EARLIER module happened to leave
  `BRIDGE_TEST_HOME`/`BRIDGE_STATE_DIR` set -- `tests/run_all.py` now
  snapshots and restores every `BRIDGE_*`/`OPENROUTER_*`/`DATABRICKS_*`/
  `ANTHROPIC_*`/`TYPESAFE_*` env var AROUND EACH MODULE, so this can never
  happen regardless of any one module's own hygiene or import order; its
  own REAL SESSIONS GUARD also now catches a new DIRECTORY under the real
  sessions dir, not only a new `.jsonl` file (1972 empty slug directories,
  invisible to the old file-only glob, were found on the Windows build
  host). The same unscoped-real-machine-state bug also reached
  `~/.rolo-claude/mcp/tools-cache/` (lazy MCP tool caching, item 13's own
  H13 Part A, also resolves via `bridge_home()`) through one compat-matrix
  test that scoped its subprocess check but not its in-process one -- fixed
  the same way, by scoping before the in-process `build_manager` call. The
  `!cmd` inline-shell permission card is now registered with the session's
  own permission-waiter table too, so Shift+Tab/`/permissions` re-evaluates
  it through the exact same `reevaluate_pending_permission` path a live
  tool-call ask already uses, instead of leaving it unaffected by a mode
  change until answered by hand.
- **Addendum**: a bare `rolo-claude` typed outside the checkout did not
  work for the owner on the work VM, because the installed console script
  had never been put on PATH (only the checkout's own `bin/` wrapper had
  ever been used, from inside the checkout). `doctor` gained a "command on
  PATH" check -- OK names the resolved path; a WARN (not found, or
  resolving to the checkout's own `bin/` wrapper) names the exact reinstall
  command for whichever tool is present (`uv tool install --reinstall .`
  run from the checkout, else `pip install --user -e .`) and reminds that
  it's needed again after every `git pull`; `init`'s own Summary ends with
  the same check and, when it fails, the same fix line plus "run it from
  any directory -- the checkout is only for `git pull`." README's quick
  start and `docs/COMMANDS.md`'s `doctor`/`init` sections now say so too.

### Part 2 addenda (same release, same day): Opus 5.5, provider auto-detection, catalogs without init, OpenRouter balance

Four follow-up asks from the owner's live use on his personal Mac and his
work VM, landed the same day as Part 2 above. All with pinning tests;
`__version__` stays 1.0.1.

- **`cc:`/`ant:` explicit `opus-5.5`/`sonnet-5.5` aliases**: `opus`/`sonnet`
  are deliberately-moving "latest" pointers (today both resolve to the
  `-5-5` point release) -- nothing was labelled "5.5" anywhere, so a reader
  had to already know `opus` IS `claude-opus-5-5` to find it. The two new
  names resolve to the identical id the bare pointer already does; `/model`,
  `rolo-claude models` and the init picker now show the resolved id next to
  EVERY `cc:`/`ant:` alias (`cc:opus -> claude-opus-5-5 (latest Opus)`,
  `cc:opus-5.5 -> claude-opus-5-5`), reading as an enumeration the same way
  the Databricks group's own `[<family> · <path>]` tag already does. Full
  alias table in `docs/MODELS.md`.
- **Provider enablement rule REPLACED** (rolo, personal Mac: "never ran
  init... loves that the harness already finds the available keys and
  subscription and uses those"): detected credentials/a real claude.ai
  login now AUTO-enable a provider -- OpenRouter/Anthropic API (key)/
  TypeSafe once their key is found (env file, shell env, or the settings
  env chain), Databricks once a host AND token are found (same sources,
  plus `~/.databrickscfg`), Claude Code subscription (`cc:`) ONLY when
  `claude auth status` reports `loggedIn` with `authMethod` exactly
  `claude.ai` (never for an API-token/custom-base-url-driven `claude`, as
  on a work VM, loggedIn or not). `providers` in config.json now stores
  OVERRIDES only: an explicit `enabled: true`/`false` always wins over
  auto-detection; migration is now a permanent no-op (auto-detection
  already computes live what it used to write once). `/providers`/
  `rolo-claude providers` shows each provider as `auto (detected from
  <source>)` / `disabled by you` / `enabled by you` / `not set up`. A
  hand-typed `cc:` ref refused by pure auto-detection (never an explicit
  override) now names the SAME precise reason (not logged in / claude not
  installed / wrong authMethod, each with its own fix) a turn would have
  failed with anyway, reusing `agent.cc_runtime`'s own preflight check
  instead of a generic "not enabled" line; `doctor`'s default-model check
  got the matching fix for a syntactically-fine-but-unconfigured ref.
- **Catalogs exist without `init`, `/model` lists every enabled provider
  regardless of the current model** (rolo, Mac: "`/model` showed one
  OpenRouter row" because models.json had never been fetched, and it
  vanished entirely after switching to a `cc:` model): a background worker
  now fetches every ENABLED provider's catalog (OpenRouter `/models`,
  Anthropic `/v1/models` -> a new `ant-models.json` cache, Databricks
  endpoint discovery) once at app launch and whenever `/model` opens, if
  missing or older than `databricks.catalog_max_age_hours` (default 24h) --
  never on the UI thread, one dim note when something actually changed.
  `/models refresh`/`rolo-claude models --refresh` now refresh every
  enabled provider, not Databricks/OpenRouter only. `Controller.
  list_models()` already built each provider's group from its own cache
  independent of the session's current model -- the real fix was making
  sure that cache actually gets populated; verified a cached OpenRouter
  group survives switching the session to `cc:opus` unchanged.
- **OpenRouter account balance in the status bar** (rolo: "put the
  remaining balance of an api key... next to... the row of numbers under
  the chat"; corrected mid-round against OpenRouter's real OpenAPI spec --
  the key-info endpoint is `GET /key`, not `/auth/key`, and `/credits`
  needs a separate Management key): a new segment right after cost, `OR
  $12.40 left` or `OR $3.21 used`, populated by a background worker (never
  the UI thread) at launch, every 5 minutes, and once after every turn
  (debounced to at most once a minute). Three-way preference: THIS key's
  own `limit_remaining` (`GET /key`, the ordinary `OPENROUTER_API_KEY`)
  when it has a real limit; else the whole account's remaining credits
  (`GET /credits`, requiring a SEPARATE `OPENROUTER_MANAGEMENT_KEY` --
  never the ordinary key) when that key is configured; else this key's own
  `usage` (a spend figure, labelled "used" not "left" -- the honest
  fallback for an unlimited key with no management key). Both calls are
  best-effort and never raise; a failed refresh leaves whatever was cached
  before untouched (never blanks the segment); the figure dims (never
  disappears) once it's more than 10 minutes old; omitted entirely when
  OpenRouter isn't enabled or nothing has ever been fetched. `/cost` and
  `/providers` print the same cached figure, naming which of the three
  kinds it is, plus the key's own label and the time of the reading.
  `OPENROUTER_MANAGEMENT_KEY` documented in `docs/CONFIG.md`.
- **State-dir/env-scoping audit, round 2**: the SAME `BRIDGE_TEST_HOME`-
  leak class D2.1 found earlier in this release turned up again, three
  more times, now as a `providers`-block/credential-env leak instead of a
  session-log one -- each a test that pops a credential env var in its own
  setup but never restores it (only clears), silently wiping every LATER
  test's own default credential for the rest of that file's run once
  nothing upstream masked it anymore. Fixed in `test_h9b_findings.py`
  (`OPENROUTER_API_KEY`), `test_dbx_work_routing.py`'s own `_EnvSandbox`,
  and `test_work_box.py`'s `test_doctor_work_no_databricks_configured` --
  each now saves and restores the full set it touches, not just pops it.
  A new `tests/helpers/provider_env_defaults.py` gives the ~40 other test
  files that build `or:`/`dbx:`/`ant:` refs to test something else
  entirely (tool dispatch, hooks, compaction, permissions, steering, ...)
  a believable (never real) default credential, since `parse_model_ref`
  now refuses an auto-detected-disabled provider the same way it already
  refused an explicitly-disabled one.

`__version__` is unchanged.

### Final pass (same release, same day): secret-env hygiene and trust-aware Databricks resolution

Two more findings from the 1.0.1 part 2 fixpass review, each with its own pinning test; `__version__` stays 1.0.1.

- **`OPENROUTER_MANAGEMENT_KEY`/`TYPESAFE_API_KEY` now strip from every tool
  child's env**: the fixed secret-key list `tool_child_env`/`cc_child_env`
  use (so Bash/PowerShell/MCP/hooks and the `claude` subprocess never see a
  provider credential) was missing both -- the OpenRouter balance
  management key and the TypeSafe key could otherwise have leaked into a
  tool child's own environment.
- **`resolve_databricks()`'s settings-chain re-derivation is now trust-aware**:
  an untrusted project's own `.claude/settings.json`/`settings.local.json`
  `env` block is dropped before it ever reaches Databricks host/token
  resolution (the same trust gate a real session's `Settings.effective_env`
  already enforces), so a project can never pair its own host with the
  user's token; a token exported in the shell still pairs with a host kept
  in the user's own settings.json env block. Credential listings
  (`/providers`, the init tabs, doctor) apply the same trust rule, the
  user's statusLine script runs with the same stripped child env as hooks
  and tools, and the background catalog and balance workers detect a
  provider through the session's effective env, so a key that lives only
  in a settings.json env block still gets its catalog and balance. The
  launch-time catalog refresh,
  `/model`'s own open-time refresh, the OpenRouter balance worker, and the
  headless session-start Databricks thread now resolve credentials from the
  session's own trust-filtered `effective_env` instead of letting each one
  call bare `resolve_databricks()`/read `os.environ` on its own.

`__version__` is unchanged.

## [1.0.0] - 2026-09-30

rolo-claude 1.0.0: the stable general harness release. Summarises the
Databricks-first correctness/tooling work from the `V2-brief.md` line
(`docs/harness/V2-brief.md`) -- per-family request/stream/error correctness
across every gateway type a workspace can serve (the `0.7.0` line below),
and matrix-driven fixes plus roles (the `0.8.0` line below) -- and a
documentation pass on top of them. rolo-claude itself is unchanged in
scope: still the one general harness driving all four routes (OpenRouter,
Databricks, the Anthropic API, and a Claude subscription via `cc:`), not
renamed, with no new console-script alias.

- **0.7.0 (V2a) recap**: per-family correctness for every Databricks gateway
  dialect the harness routes to (native `anthropic/v1/messages`, `mlflow/
  v1/chat/completions`, `cursor/v1/chat/completions`, and the universal
  `/serving-endpoints/<name>/invocations` fallback) -- thinking/reasoning
  replay per family, usage/cost accounting per type, every error shape
  (400/401/403/404/413/429/5xx) classified correctly, and the two
  work-matrix open questions (reasoning replay after a tool call, route
  split from a stale cache) turned into runnable probes.
- **0.8.0 (V2b+V2c) recap**: `rolo-claude work-matrix show`/`apply` turns a
  `doctor --work --probe-all` report into a suggested fix per failing
  endpoint (and writes the one class of fix that maps onto a real
  `databricks.gateway.<endpoint>` config key); roles
  (`orchestrator`/`coder`/`reviewer`/`researcher`/`small`) let a team point
  different kinds of work at different models via `~/.rolo-claude/
  config.json`/`team.json`'s own `roles` table, `--role NAME=MODEL`,
  `Agent(role=...)`, `/roles`, and `stats --roles`.
- **Docs**: README now documents roles and the work-matrix show/apply
  workflow (real 0.8.0 features that had shipped without a README mention
  until now) and points to a separate sibling project,
  [databricks-claude](https://github.com/roloVibes/databricks-claude), for
  teams that want a Databricks-only edition; `docs/DATABRICKS.md` gained an
  end-to-end team-workflow section tying `team.json` -> `init` -> the work
  matrix -> roles together in one walkthrough; `docs/ROLES.md` cross-links
  it; `docs/harness/README.md`'s milestone index gained a `V2-brief.md` row.
- **Naming, for the record**: `docs/harness/V2-brief.md` (kept as originally
  written -- this project's own build-history convention, see
  `docs/harness/README.md`'s opening note) describes a planned V2d/V2e phase
  renaming this project to `databricks-claude`. That did not happen here --
  the owner instead spun off a **separate** `databricks-claude` repository
  as its own Databricks-only edition for teams, and rolo-claude stayed the
  general four-route harness, released as `1.0.0` rather than `2.0.0`.
- `__version__` -> 1.0.0.

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
