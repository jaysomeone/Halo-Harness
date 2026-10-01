# H5b brief — fix pass for the H4/H5/H3c review + H6/U5 must-dos (halo)

Repo: `~\Documents\vibes\appDev\halo\` (Windows build host; **Kali Linux is the
primary platform**, so every fix must be OS-neutral and tested for the Linux code paths). Baseline =
commit `c5a62fe` ("H6 + U5") on master: `python test_bridge.py` 97/97, `python tests/run_all.py`
1111 (1107 passed, 4 skipped), `python test_tui.py` 37/37, green on Windows and WSL Ubuntu.
You are the ONLY worker on the tree during this pass. Do not commit (Fable verifies and commits).

## Read first
1. `docs/harness/review-findings-h4-h5-h3c.md` — ALL 18 findings + the "H6/U5/H8/H9 must-do"
   section. This brief does not restate them; the findings file is the spec. Line numbers there
   refer to `e19b6f0`; H6/U5 (`c5a62fe`) moved things — re-locate by symbol name.
2. `~/.claude/plans/typed-tickling-squirrel.md` → "Research-driven revisions" 2, 6, 7, 9 (invariants,
   reasoning replay, max_tokens, compaction), "Auto mode = uninterrupted + steering" (binding),
   "Decisions" (no safety heuristics: never add refusal/protected-path/classifier logic while fixing).
3. `reports/OpenCode harness deep review.md` → compaction gate/prune constants, Edit fuzzy stages
   ("Found multiple matches"), retry ladder.
4. `docs/harness/claude-code-2.1.281-binary-facts.md` → hook schemas (`PermissionRequest` =
   `hookSpecificOutput.decision.behavior`), `CLAUDE_ENV_FILE` sourcing, plugin V2 manifest shape,
   Skill "Base directory for this skill:" line.
5. Current code for each cited file; `tests/helpers/{mock_openai,mock_anthropic,fake_home,
   hook_scripts}.py`; `agent/{loop,compact,prune,log,invariants}.py`; `tools/edit.py`; `hooks.py`;
   `headless.py`; `controller.py`; `commands/{registry,builtins}.py`; `tools/skill.py`;
   `config/plugins.py`; `providers/{http,request}.py`; `tui/{app,slash,dispatch}.py`.

## Scope — close every item below; none may be deferred
A. **Findings 1–18** of `review-findings-h4-h5-h3c.md`, each with the fix the finding prescribes
   and a pinning test (finding 17 lists the eight highest-value tests; add all of them).
   Specific notes:
   - F1 compaction gate: reserve `min(requested max_tokens, 32 000)`; floor the trigger at ≈70 % of
     the window; no back-to-back compaction; test against the REAL shapes in
     `~/.halo/models.json` (copy the 29 zero-trigger rows into a fixture).
   - F2 Edit: a fuzzy stage is accepted only when it yields exactly ONE candidate (all with
     `replace_all`); ambiguous → "Found multiple matches…" error; adversarial near-duplicate tests.
   - F3/F5 steering: synthesize `is_error` results ("not run: the user sent a new message first")
     for every unrun call before applying a steer; pairing checked across ALL assistant nodes;
     busy-check + enqueue atomic under `_steer_lock`; `turn()` drains leftovers and resubmits them
     as the next `user_input`; `steer_queued` at enqueue time; never log an empty assistant node.
   - F4 compaction safety: wire `agent/prune.py` into `_derive_and_build` and before summarising;
     overflow → OpenCode-style serialised head; `_step`'s retry ladder; on failure NO marker.
   - F6 `CLAUDE_ENV_FILE`: source it in the Bash tool's shell before each command (POSIX `. file`;
     Git Bash on Windows), re-read after compact/clear SessionStart hooks; test
     `export PATH="$PATH:/x"` keeps Bash working on Linux (`bash -lc` + Debian `/etc/profile` reset).
   - F7 WebSearch registered before the catalog snapshot; assert in the first `meta` node.
   - F8 plugins V2: iterate the record LIST (`scope == "user"` + project/local whose `projectPath`
     matches cwd), V1 `installPath`, cache `cache/<marketplace>/<plugin>/<version>`,
     `enabledPlugins` gate in `build_hook_runner`; fixture in the binary's exact shape.
   - F9 auto-mode leftovers: `` !`cmd` `` pre-execution goes through `permission_engine.decide`
     (frontmatter `allowed-tools` = temporary allows) and the Bash tool's runner + `tool_child_env()`
     (no provider secrets, `/bin/bash` on POSIX, Git Bash on Windows); claude-in-chrome ALWAYS
     spawned with `CLAUDE_CHROME_PERMISSION_MODE=skip_all_permission_checks`.
   - F10 stream-json input: stdin reader thread; first line starts the first turn; lines arriving
     while busy → `Session.steer`; otherwise queued turns; test with stdin held open.
   - F11 `/compact` in the TUI: a worker `Command` handled between turns (deferred while busy),
     streaming `compaction` events; slash commands never dispatched on the UI thread mid-turn.
   - F12 Skill tool: "Base directory for this skill: <abs>" first line, `${CLAUDE_SKILL_DIR}`,
     `${CLAUDE_SESSION_ID}`, `${CLAUDE_PROJECT_DIR}`, `${CLAUDE_EFFORT}` substituted, siblings listed
     as absolute paths.
   - F13 hooks: PermissionRequest `hookSpecificOutput.decision.behavior` (+ `updatedInput`,
     `updatedPermissions`, `message`, `interrupt`); re-run deny/ask after any PreToolUse
     `updatedInput` rewrite.
   - F14 hook processes: `tool_child_env()`; `start_new_session` + `killpg` on POSIX, `taskkill /T`
     on Windows; poll `abort` while waiting and re-check before dispatch; SessionEnd budget from
     explicit timeouts only (else 1.5 s); `shell: "bash"` → `/bin/bash`.
   - F15 native thinking: store/replay the `thinking` key; strip non-wire keys; drop unsigned/empty
     thinking and empty text blocks from cut replies; `budget_tokens < max_tokens`; no thinking on
     the summariser call; merge `message_start.message.usage`; downgrade reasoning to text on a
     mid-session model change.
   - F16 Databricks Claude passthrough: bearer header inside the databricks branch; the test uses
     the headers `build_session` produces.
   - F18 post-compaction re-injection: deferred-names + environment snapshots re-appended after the
     marker; plan file + skills re-attached; MAX_STEPS wrap-up keeps `tools` with
     `tool_choice: "none"`.
B. **H6 must-dos** (findings file): `_nodes` loading on resume (done in H6 — verify with a test that
   resumes twice); sub-agents reuse steer/abort/hook plumbing and fire SubagentStart/Stop (verify);
   **AskUserQuestion** has Claude Code's schema and its interactive branch goes through PreToolUse
   and `decide` (dontAsk → no card); `plan_reply` does what D8 says (verify after H6).
   **H6 known v1 gap to close now:** sub-agent permission `ask` outcomes currently resolve as deny —
   in interactive mode they must be surfaced to the user as a `permission_request` tagged with the
   agent id/name (D10: "surfaced to the user with the agent tag, never auto-denied"); print mode
   keeps deny + `permission_denials`.
C. **U5 must-dos**: render `compaction` events in the TUI; `steer_queued` shown at submit and the
   steer text shown once; model-calling slash commands off the UI thread (`/compact`, `/rename`,
   titles); `/clear` starts a NEW log with SessionEnd(clear) + SessionStart(clear); `/model` and
   `/status` read from the live session; `doctor` reports the clipboard backend (OSC 52 / xclip /
   wl-copy / pbcopy / win32) — U5's leftover.
D. **Cheap H8 must-dos that belong here** (they are one-liners next to code you already touch):
   `compactionModel` from `~/.halo/config.json` actually used by the summariser; parse
   `anthropic-ratelimit-*-reset` as RFC 3339 (`datetime.fromisoformat`, `Z` accepted); `_step` no
   longer caps a longer `Retry-After` at 60 s (cap at 300 s, log it).

## Tests
Every fix gets a pinning test named after its finding (`test_h5b_f01_…`). Suites: `python
test_bridge.py`, `python tests/run_all.py`, `python test_tui.py` — all exit 0 on Windows AND on
WSL (`wsl -e bash -lc 'rsync … && source ~/halo-harness-wt-venv/bin/activate && python3 tests/
run_all.py'` — see `docs/harness/INSTALL.md`; the venv already exists). The mock-upstream Session
tests must drive a real `Session` (finding 17's point), not helper functions in isolation.

## Acceptance (Fable re-runs these)
All three suites green on Windows and WSL; live DeepSeek V4.1 Flash: `-p "reply pong"`, an Edit
round trip on a scratch file with a near-duplicate block (must refuse ambiguous), a two-step Read
turn on a Kimi K2.6 profile performs NO compaction, `--input-format stream-json` with a second line
sent mid-turn is applied as a steer, `/compact` in the TUI keeps the UI responsive; proxy pong;
`~/.claude/settings.json` byte-identical before/after.

Report ≤ 60 lines: per finding "closed + test name" or "NOT closed + why". Rules as in the other
briefs (≤ 250 lines per write, no heredocs with backslashes on Windows, no commits, no safety /
refusal / "cyber" language anywhere, never touch `~/.claude.json`).
