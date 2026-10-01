# U0 brief — CLI flag parity, slash-command registry, history, theme, fake controller (halo)

Repo: `~\Documents\vibes\appDev\halo\` (Windows build host; **Kali Linux primary** —
OS-neutral code, `/bin/bash` on Linux). Baseline = the H2b commit on master (both suites green on
Windows and WSL). Do not commit.

## Read first
1. `~/.claude/plans/typed-tickling-squirrel.md` → "Approach in one screen" (naming + **flag parity
   rule**), "Primary use case", "D-TUI" (modules, print mode, slash commands, packaging),
   "D-Contract reconciliation", "Decisions" (no safety heuristics).
2. `docs/harness/claude-help-2.1.281.txt` — the installed Claude Code's full `--help`, `mcp --help`,
   `mcp add --help`. **Every flag and subcommand listed there must be accepted by `halo` with
   the same name, arity and meaning** (rolo: "same flags should work the same way like
   `--enable-auto-mode` or `--chrome` and etc, but it starts with halo"). Flags whose feature
   exists in the harness behave identically; flags whose feature is not built yet are parsed and
   answered with one line `halo: --<flag> is not supported yet (planned: <milestone>)` on
   stderr — never an argparse error. Add harness-only flags (`--small-model`, `--cwd`, `--theme`,
   `--playwright`, `--demo`) and the `proxy`, `models`, `doctor`, `config` subcommands.
3. `docs/harness/claude-code-2.1.281-binary-facts.md` §1 (flag semantics incl. hidden flags:
   `--permission-prompt-tool`, `--max-turns`, `--system-prompt-file`, `--append-system-prompt-file`),
   §11 (synced skills naming).
4. Current code: `halo_harness/{cli,headless,output}.py`, `halo_harness/config/{settings,skills,
   commands}.py` if present (H2b may have added skills/commands readers — reuse, do not duplicate),
   `halo_harness/permissions.py` (its CLI hooks), `tests/helpers/*`.

## Scope
A. **`cli.py` rewrite** as the single argparse surface: positional `prompt` (opens the TUI with it
   when no `-p`; TUI itself is U2 — until then bare `halo` prints "TUI arrives in U2" and exits
   2), `-p/--print`, every Claude Code flag from the help capture (grouped exactly as Claude Code
   groups them; `--permission-mode` choices `acceptEdits auto bypassPermissions manual dontAsk plan`
   plus `default` accepted as the alias), subcommands `proxy` (delegates to `bridge.main`), `mcp`
   (`list|add|remove|get|add-json` — `list` real once H3 lands; others print the not-yet line),
   `models`, `config`, `doctor` (checks: Python version, `~/.claude` layout, env file, OpenRouter key,
   Databricks discovery, `claude.exe` for `--chrome`, node/npx for `--playwright`, WSL/Kali hints),
   `--version`. Unknown flags → argparse error exit 2 (parity means the list is complete, so unknown
   really is unknown). `test_cli.py` asserts that every long option in the help capture is accepted
   (parse the capture, feed each flag with a dummy value).
B. **`commands/` package**: `registry.py` (`SlashCommand`, `Registry.discover/resolve/complete/
   help_rows`), `builtins.py` (`/help /clear /compact /cost /context /model /mcp /memory /permissions
   /plan /resume /status /config /skills /agents /effort /init /doctor /export /add-dir /theme /exit`
   with `kind ui|core|prompt` and headless facade behaviour), `custom.py` (`.claude/commands/**/*.md`
   + `~/.claude/commands/**/*.md`, namespace by subdir, frontmatter via `config/frontmatter.py`,
   `$ARGUMENTS`/`$0…$9` (0-based like skills), `` !`cmd` `` pre-execution gated by `allowed-tools`,
   `@path` left for the core), `skills.py` (skills surfaced as `/name` and `/anthropic-skills:<name>`,
   honouring `user-invocable`/`disable-model-invocation`; reuse the H2b/H4 skills reader if it exists).
   `-p "/cost"` and `-p "/help"` work through the headless facade.
C. **`history.py`**: merged prompt history (`~/.claude/history.jsonl` read-only + `~/.halo/
   history.jsonl` in the identical schema, project filter normalised for both separator forms).
D. **`theme.py`**: precedence `--theme` > `CLAUDE_BRIDGE_THEME`/`ROLO_CLAUDE_THEME` > settings
   `theme` > dark; theme names `claude-dark`, `claude-light`, `*-daltonized`, `*-ansi`; persisted to
   `~/.halo/config.json` by `/theme`. (Textual is NOT imported in U0 — pure data.)
E. **`testing/fake_controller.py`** + `--demo [--stress N]`: scripted event queue per the contract in
   `events.py`; `--demo` in print mode replays a scripted transcript through the text/json sinks.
F. **`headless.py`**: `--output-format stream-json` (init line with `session_id, cwd, model,
   permissionMode, tools, mcp_servers, slash_commands, halo_harness_version`; `assistant`/`user`
   message lines; optional `stream_event` with `--include-partial-messages`; final `result`),
   `--input-format stream-json` (one user message per line = one turn), `--max-budget-usd`, exit codes
   0/1/2/130 (and 143 on SIGTERM), `--json-schema` → `structured_output` via `response_format`
   where the profile allows.

## Tests (≥ 50 new, OS-neutral)
Every long option in the help capture is accepted; not-yet flags print the stderr line and continue;
`--permission-mode manual` = default; positional prompt vs `-p`; `proxy --version` delegation; each
built-in slash command's headless output; custom command discovery/namespace/`$0`/`!` gating; skills
`/anthropic-skills:x` naming; history merge with both key forms; theme precedence; fake controller
replay; stream-json line order and result shape; input-format stream-json two turns; budget exit.

## Acceptance
Both suites green on Windows and WSL; `python -m halo_harness --help` lists every Claude Code flag;
`python -m halo_harness -p "reply with the single word pong" --output-format stream-json` shows init →
assistant → result lines; `python -m halo_harness -p "/cost"` prints a cost line; `python -m
halo_harness doctor` runs; `--chrome`/`--playwright` print the not-yet line and the prompt still runs.
Report ≤ 50 lines. Rules as in the other briefs (≤ 250 lines per write, no heredocs with
backslashes, no commits, no safety language).
