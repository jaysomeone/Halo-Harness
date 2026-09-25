# H9 brief — bug hunt, Linux-first acceptance, MCP compatibility matrix, release v0.3.0 (rolo-claude)

Repo: `C:\Users\user\Documents\vibes\appDev\rolo-claude\` (Windows build host; **Kali Linux is the
primary platform**). Baseline = the H8 commit on master with all suites green on Windows and WSL and
every earlier review's findings closed. Do not commit (Fable commits and tags).

## Read first
1. `~/.claude/plans/typed-tickling-squirrel.md` → "Closing milestone H9" (rolo's exit criteria),
   "Primary use case", "Auto mode = uninterrupted + steering", "Decisions" (no safety heuristics).
2. Every `docs/harness/review-findings-*.md` (confirm each finding is closed; reopen anything that is
   not) and every `docs/harness/*-brief.md` "Acceptance" section (this is the acceptance table).
3. `docs/harness/INSTALL.md`, `README.md`, `docs/harness/claude-help-2.1.281.txt` (parity list).
4. `reports/OpenCode harness deep review.md` H9 rows (period-2 doom-loop detector, per-sub-command
   Bash rule matching with the trailing-` *` wildcard, doctor checks for `rg`/`xclip`/`wl-copy`/
   `$EDITOR`, sampling-table audit, tmux/kitty acceptance).

## Part A — Linux-first acceptance (WSL Ubuntu now; the Kali VM `linux-vm.lan` user `kali` if
`ssh -i ~/.ssh/linux_vm user@linux-vm.lan` answers — try it; zsh, single-line commands)
- Fresh clone from GitHub (`git clone https://github.com/roloVibes/rolo-claude`), install via
  `uv tool install --editable .` AND `pip install --user -e .` (PEP 668 note), `rolo-claude --version`,
  `rolo-claude doctor`, `rolo-claude models`, `rolo-claude mcp list` with that box's real `~/.claude`
  (if the VM has none, create a realistic one from `tests/helpers/fake_home.py` under a temp HOME and
  ALSO run against the WSL user's real `~/.claude` copy of rolo's Windows config: rsync
  `/mnt/c/Users/user/.claude` and `.claude.json` into a temp HOME with paths rewritten — the MCP
  commands will fail to start because they point at Windows venvs; that is expected, statuses must be
  honest, nothing may crash).
- All three suites; then every brief's acceptance lines end-to-end on Linux with the real default
  model, recorded verbatim in `docs/harness/ACCEPTANCE-<date>.md` (a table: milestone, command,
  expected, observed, pass/fail, platform).
- TUI under `script`/tmux: launch, stream, tool card, permission card in `default`, `/model` switch,
  Esc, steering mid-turn, Ctrl+C twice; `--chrome` (report precondition status on Linux) and
  `--playwright --browser chromium` (install chromium via `npx playwright install chromium` if allowed;
  report exactly what happened).
- rg present vs absent parity (WSL has no rg; install `ripgrep` via apt in a second run if possible).
- Everything must also stay green on Windows (secondary): run the three suites there once at the end.

## Part B — MCP compatibility matrix (rolo's parity promise)
Build fixtures + live checks proving: user-scope servers from `~/.claude.json` work; a project
`.mcp.json` server works after approval (and shows `⏸ Pending approval` before); local
`projects[cwd].mcpServers` (both key forms); `--mcp-config` file + inline JSON; a plugin-provided
server from a real marketplace plugin copy (`mcp__plugin_<p>_<s>__<tool>`); a server added AFTER
install by `claude mcp add` (run the real `claude mcp add --scope user rc-test -- python <fake
server>` if `claude` is on PATH, else edit `~/.claude.json` in a temp HOME) is picked up by the next
rolo-claude session; a server added by `rolo-claude mcp add` is listed by `claude mcp list` (or, without
`claude`, the JSON matches Claude Code's schema byte-for-byte per `docs/harness/claude-code-2.1.281-
binary-facts.md` §9); `mcp remove`; tool schemas of unusual shape (nested objects, `$ref`, `anyOf`,
enums, 300 tools) survive OpenRouter and the Databricks 32-key/no-`$ref` simplifier; ToolSearch finds
any newly added tool by name and by description; `/mcp` reconnect after editing config mid-session;
http and sse transports (fake servers); a server dying and restarting; names with dots/spaces.
Record results in the acceptance file.

## Part C — Bug hunt
- Fuzz the loop with the mock upstream: malformed chunks, truncated JSON, huge outputs, unicode
  edge cases (surrogates, RTL, emoji), interrupts/steers injected at every event boundary, 429/5xx
  storms, overflow every N calls, tool results with control characters; ≥ 200 randomised runs must
  end with a valid log (invariants hold), no hangs (per-run timeout), no leaked threads/processes.
- Static pass: `python -X dev -W error::ResourceWarning tests/run_all.py` clean; `pyflakes`/`ruff`
  (install if available) on `rolo_claude/`; grep for `print(` in library code, bare `except:`,
  `eval(`, `shell=True` without need, hard-coded Windows paths, `/tmp` literals.
- OpenCode H9 items: period-2 doom-loop detector (A/B alternating identical calls) with the breaker
  still armed in auto mode; per-sub-command Bash rule matching audit; `doctor` checks for `rg`,
  `xclip`/`wl-copy`, `$EDITOR`, Git Bash (win32), `claude` (for `--chrome`), `npx` (Playwright);
  sampling-table audit against `reports/Open weight model adapter rules.md`.
- Every bug found: fix + pinning test; list them in the report.

## Part D — Release
- Close every open finding; all suites green on Linux and Windows; acceptance file complete;
  README/INSTALL current; `__version__` 0.3.0; `CHANGELOG.md` summarising milestones; Fable tags
  `v0.3.0` after verification.

Report ≤ 80 lines: acceptance table summary (counts per platform), MCP matrix results, bugs found/
fixed with tests, anything that could not be exercised (VM unreachable, no Chrome on Linux, etc.).
Rules as in the other briefs (≤ 250 lines per write, no heredocs with backslashes, no commits, no
safety language).

## Must-dos carried over from `review-findings-h4-h5-h3c.md` (H9 section) — added 2026-09-24
- Linux: `bash -lc` PATH reset by Debian/Kali `/etc/profile` (settings `env.PATH` and the
  `CLAUDE_ENV_FILE` additions must survive — test on WSL Ubuntu AND in a Debian-style profile);
  dash (`/bin/sh`) never used for `!` pre-execution (must be `/bin/bash`); a real
  `claude plugin install` manifest (array-of-records V2 shape) drives plugin MCP servers + hooks;
  128k/256k-context models tested with the real `~/.rolo-claude/models.json` shapes (compaction
  trigger never 0, never back-to-back); an SDK-style `--input-format stream-json` client that
  writes one line and waits for `result` before the next (must not deadlock; a mid-turn line is a
  steer); the lazy MCP start inside ToolSearch (serial today, cannot be aborted, up to MCP_TIMEOUT
  per server) — make it parallel + abortable or document the cap.
- Fuzz: send Esc or a steer at EVERY event boundary — including during hooks, compaction, and
  retry waits — and after each turn assert that every `tool_use` in the log has a `tool_result`
  and that no assistant node is empty.

## Must-dos carried over from `review-findings-h5b.md` (H9 section) — added 2026-09-24
- Linux re-runs of the H5b review repros on Kali and Ubuntu: SessionStart env file through a
  real Session (finding 10); a 600-line Read checking what the model actually receives on the
  next request (finding 1); a 32k-window profile and the zero-trigger models.json rows (finding
  3); stream-json late steer + EOF (finding 13); Esc during Stop and PostToolUse hooks (finding
  12); more than 4 sub-agents and background sub-agents with Esc (finding 7).
- Fuzz additions: steer or Esc at every event including retry waits, hooks, auto-compaction and
  permission cards; after each turn assert every tool_use has exactly one result, no assistant
  message is empty after `prepare_anthropic_messages`, tool_result blocks come first in the user
  message that follows a tool_use, and nothing is written to `session.log` from a non-worker
  thread while `busy`.
