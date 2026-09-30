# H5c brief — fix pass for the H5b review (rolo-claude)

Repo: `~\Documents\vibes\appDev\rolo-claude\` (Windows build host; **Kali Linux is the
primary platform**). Baseline = the H8 commit on master, all three suites green on Windows and WSL.
You are the ONLY worker on the tree. Do not commit (Fable verifies and commits).

## Read first
1. `docs/harness/review-findings-h5b.md` — the spec. Findings **1, 2, 3, 4 and 10 were assigned to
   H8** (prune scope + protection window, stable stub cut-off, gate math on small windows, cached
   Claude token counting, env-file merge through Session): verify each is closed by a real-Session
   test in the H8 commit and reopen anything that is not. **This pass owns findings 5–9, 11–24**
   plus the items below. Line numbers refer to the `4680804` snapshot; re-locate by symbol.
2. `~/.claude/plans/typed-tickling-squirrel.md` → "Auto mode = uninterrupted + steering"
   (binding: steer cuts the model at the next chunk, running tools finish, Esc is the hard stop,
   nothing else interrupts), "Decisions" (no safety heuristics), D10 ("sub-agent permission
   prompts are surfaced to the user with the agent tag, never auto-denied in interactive mode").
3. `docs/harness/claude-code-2.1.281-binary-facts.md` (PermissionRequest `updatedPermissions` is a
   LIST of `{type: setMode|addRules|…, destination}`; hook payloads), the Claude API docs on
   extended thinking (adaptive thinking + `output_config.effort` on 4.6+/Sonnet 5; signed thinking
   blocks with empty text when display is omitted must be echoed unchanged).

## Scope — every item closed with a real-Session pinning test (name it `test_h5c_f<NN>_…`)
- **F5** never log an empty assistant node on a cut: no replayable block → log nothing (or partial
  text + marker as the abort path does); `prepare_anthropic_messages` drops empty assistant
  messages and merges adjacent user turns. Cover: steer at `message_start`, mid-thinking before
  `signature_delta`, during a retry wait.
- **F6** steer check BEFORE each dispatch iteration (and before `_resolve_tool_call`), synthesizing
  "not run: the user sent a new message first" for the stopped call; batched read-only calls ahead
  of a queued steer do not run; closed `tool_use` blocks from a steered cut are not dispatched.
- **F7** shared abort: a child never clears an Event it did not create; queued children check
  `abort` before starting; background agents and `_resume_task` likewise. Test: 5 Agent calls,
  Esc after 4 started → all 5 end interrupted, parent ends `interrupted`.
- **F8** sub-agent `ask` surfaced live: stream child events through a queue + worker (rewrite
  `_run_child_to_completion` from buffered to streaming), park the child's ask on the parent's
  `_permission_waiters` tagged `agent_id`/`name`; the TUI shows an answerable, agent-tagged
  PermissionCard; `-p` keeps deny + `permission_denials`. Remove the "no live approval for
  sub-agents in this build" wording everywhere.
- **F9** PermissionRequest: apply `updatedInput` then re-run `decide()`; `interrupt: true` ends the
  turn as interrupted; parse `updatedPermissions` as the binary's list shape and apply `setMode`
  and `addRules`; reject non-list with a warning.
- **F11** `Session.clear()` updates `hook_runner.session_id`, `transcript_path`, `CLAUDE_ENV_FILE`
  and every session-keyed value BEFORE `SessionStart(clear)`; exports from clear hooks reach Bash.
- **F12** `abort` passed to EVERY in-turn hook run (Stop, PostToolUse, PostToolUseFailure,
  PostToolBatch, UserPromptSubmit, PreCompact, SessionStart); Esc during a 12 s Stop hook ends the
  turn `interrupted` within ~1 s.
- **F13** stream-json: leftover steers are drained ahead of the EOF `None` (deque `appendleft` or
  drain `_leftover_steer_texts` before acting on `None`); a line written during a Stop hook just
  before stdin closes still runs.
- **F14** `@file` mentions in steers, `@path` snapshots from prompt-kind slash commands and inline
  `!cmd` never write to `session.log` from a non-worker thread while busy: queue them to the worker
  and apply at the next safe point together with the steer text; tool_result blocks always come
  first in the user message after a tool_use.
- **F15** `/skill-name` slash path builds the same `claude_vars`, "Base directory for this skill:"
  line and sibling list as the Skill tool (`_make_run` has the skill path).
- **F16** native thinking: drop only UNSIGNED blocks (signed + empty text replayed unchanged);
  `{type: "adaptive"}` + `output_config.effort` on 4.6+/Sonnet 5 ids, `budget_tokens` only where
  the API accepts it and always < `max_tokens` with real headroom; strip thinking blocks from the
  compaction tail whose signatures bind to the old prefix. Mock-verified; live via `ant:` only if
  `doctor` shows an Anthropic key on this box (else say so).
- **F17** `replace_all` with overlapping fuzzy windows: keep the first non-overlapping set or
  refuse; never corrupt.
- **F18** `compactionModel` read from `~/.rolo-claude/config.json` first, Claude settings as
  fallback; test through the real config path, not injected knobs.
- **F19** `steer_queued` emitted once (at submit); `steer_applied` covers apply time.
- **F20** MAX_STEPS wrap-up: synthesize results for (or strip) any tool_use the provider still
  returned despite `tool_choice: "none"`.
- **F21** plugin V1 records: read `installPath`, strip the `@marketplace` suffix.
- **F22** overflow fallback serialises only the head, capped to a share of the window; after a
  failed auto-compaction back off for N steps instead of retrying the full ladder every step.
- **F23** steers pass through UserPromptSubmit hooks before being appended; `blocked` honoured;
  `additionalContext` attached.
- **F24** replace the wrong-reason tests named in the finding with real-Session tests: prune
  (H8's), F06 through Session on Linux, no-back-to-back without hand-set `_just_compacted`,
  zero-trigger models.json rows, `/clear` with a real session id, F10 mid-turn test that asserts
  the steer (not "either"), F13 PermissionRequest through the loop, F12 through both paths.
- **Extra (from the H8 must-do list, cheap):** price cache-read/cache-creation tokens in
  `CostMeter` for Anthropic/OpenRouter Claude routes; `count_tokens` relay wired if H8 did not.

## Tests
Suites: `python test_bridge.py`, `python tests/run_all.py`, `python test_tui.py` — exit 0 on
Windows AND WSL (rsync recipe in `docs/harness/INSTALL.md`; venv `~/rolo-claude-wt-venv`). Linux
failures are fixed, never skipped. Mock-upstream tests drive a real `Session`.

## Acceptance (Fable re-runs)
All suites green both platforms; live DeepSeek V4.1 Flash: a steer typed at `message_start` leaves
no empty assistant node and the next turn works; a steer queued before dispatch stops a pending
Write ("not run" result, file absent); 5 sub-agents + Esc → parent interrupted; a sub-agent hitting
a user `ask` rule shows an agent-tagged card in the TUI and a `permission_denials` entry in `-p`;
Esc during a slow Stop hook returns within ~1 s; stream-json late line + EOF still runs; `/clear`
hooks see the new session id; proxy pong; `~/.claude/settings.json` and `~/.claude.json` untouched.

Report ≤ 60 lines: per finding "closed + test name" or "NOT closed + why". Rules as in the other
briefs (≤ 250 lines per write, no heredocs with backslashes on Windows, no commits, no safety /
refusal / "cyber" language, never write `~/.claude.json` or `settings.json`).
