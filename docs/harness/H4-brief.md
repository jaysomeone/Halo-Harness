# H4 brief — hooks, Skill tool, custom command execution, AskUserQuestion seam, WebSearch (rolo-claude)

Repo: `C:\Users\user\Documents\vibes\appDev\rolo-claude\` (Windows build host; **Kali Linux primary**
— OS-neutral). Baseline = the H3 commit on master, both suites green on Windows and WSL. Do not
commit.

## Read first
1. `~/.claude/plans/typed-tickling-squirrel.md` → "D7. Hooks" AND the D-CFG **"Hooks refinements"**
   paragraph (authoritative: events, matcher forms, `if` rules, handler types command/http/mcp_tool/
   prompt/agent, timeouts 600/30/60 s + SessionEnd 1.5–60 s budget, `CLAUDE_ENV_FILE`, stdin payloads
   per event, exit-code and JSON semantics, combination rules, call sites, the events NOT emitted in
   v1), "D5" (Skill, WebSearch), "Decisions" (no safety heuristics; hooks are the user's own gates).
2. `docs/harness/claude-code-2.1.281-binary-facts.md` §2 (hook constants: `Ha=600000`, prompt 30 s,
   agent 60 s, SessionEnd budget rule, `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` 8), §10 (payload fields,
   `permissionDecision` vocab incl. `defer`, output caps), §11 (synced skills naming).
3. `docs/harness/review-findings-h2.md` → "H3/H4 must-do" hooks items: PreToolUse after `decide`
   and before dispatch for EVERY call (batched read-only calls collect verdicts before the batch is
   submitted, never inside the pool); PostToolUse before `spill_and_truncate`; hooks get
   `effective_env` (minus provider secrets, like tools).
4. Current code: `rolo_claude/agent/loop.py` (dispatch pipeline), `commands/{skills,custom}.py`
   (U0's readers — reuse), `tools/{skill,ask_user_question,webfetch}.py`, `config/settings.py`
   (`hooks` accessor), `controller.py`, `permissions.py`, `tests/helpers/*`.

## Scope
0. **Auto mode = uninterrupted, plus steering (rolo, binding — read the plan section "Auto mode =
   uninterrupted + steering")**: (a) audit and guarantee that in `auto`/`bypassPermissions` no
   tool, MCP server, browser tool (`claude-in-chrome`, Playwright), WebFetch or Bash call ever
   produces a `permission_request` or a refusal — only the user's own deny/ask rules and hooks
   apply; remove ANY prompt/tool-description/error/UI wording that says something is not allowed
   "in auto mode" (grep the tree); (b) Esc / Ctrl+C semantics unchanged (hard stop); (c) implement
   **steering**: `Command("steer", text)` from the TUI (input stays enabled while a turn runs;
   Enter = steer) and from `--input-format stream-json` lines arriving mid-turn; the in-flight
   model call is cut at the next chunk (partial assistant text logged), running tools finish, the
   text is appended as a user-role message and the loop continues at once; steers queue in order;
   events `steer_queued` / `steer_applied`; TUI shows "↳ steering…"; tests: steer mid-stream changes
   the next request, steer during a tool call applies after the tool result, two steers in order,
   steer during a pending card does not answer the card.
A. **`rolo_claude/hooks.py`** per D-CFG: `HookDef`, `HookResult`, `HookOutcome`, `normalize_hooks`
   (settings levels — untrusted project/local dropped — plus plugin `hooks/hooks.json` with
   `${CLAUDE_PLUGIN_ROOT}`, skill/agent frontmatter `hooks:` with scope), `HookRunner.run(event,
   payload, matched, tool_name, tool_input)`: matcher semantics (omitted/``/`*` all; `[A-Za-z0-9_\-
   ,|]` exact list; else unanchored regex, invalid → skip + warning), `if` via `permissions.parse_rule`
   on tool events, handler types `command` (`args` exec form without shell; `shell: powershell`;
   default Git Bash `bash -c` on win32 / `/bin/sh -c` on POSIX), `http` (POST JSON, `allowedEnvVars`
   interpolation only, credential names never), `mcp_tool` (via the MCP manager), `prompt`/`agent`
   (small model via the provider layer; `$ARGUMENTS` = payload JSON; reply `{"ok", "reason"}`);
   stdin payload builder per event; env `CLAUDE_PROJECT_DIR`, `CLAUDE_PLUGIN_ROOT`, `CLAUDE_EFFORT`,
   `CLAUDE_CODE_REMOTE=false`, `CLAUDE_ENV_FILE` for SessionStart/Setup/CwdChanged/FileChanged whose
   `export` lines feed the Bash tool env; timeouts and the SessionEnd budget; parallel execution +
   dedup + `once`; interpretation (exit 0 JSON iff `{…}`, plain stdout → context for
   UserPromptSubmit/UserPromptExpansion/SessionStart/PostModelSwitch; exit 2 blocks regardless of
   JSON; exit 1 non-blocking; Stop cap 8); combination `deny > defer > ask > allow`, contexts
   concatenated, last `updatedInput` wins; `disableAllHooks`/`--bare`.
B. **Call sites in the loop**: SessionStart(startup|resume|clear|compact), UserPromptSubmit
   (blocked → prompt dropped + reason shown), PreToolUse (after decide, before dispatch; hook allow
   skips ask/mode but deny rules still apply; `updatedInput` replaces input), PermissionRequest
   (when decide → ask, before the UI card; hook answer wins), PermissionDenied, PostToolUse /
   PostToolUseFailure (before truncation), PostToolBatch, Stop (with `stop_hook_active` on re-entry),
   Notification (permission_request / idle / elicitation), SessionEnd (quit, `/clear`); SubagentStart/
   Stop and Pre/PostCompact wired as no-ops until H5/H6. Hook-added `additionalContext` becomes a
   user-role snapshot in the log (never the system node).
C. **Skill tool** (real): reads via `commands/skills.py`, returns the SKILL.md body (frontmatter
   stripped, substitutions applied) + sibling file list; `allowed-tools` → session rules until the next
   user message; `disable-model-invocation` hides it from the tool; `context: fork` / `agent` →
   "deferred to H6" error result for now. Slash-invoked skills and custom commands (U0's registry) run
   through the loop in `-p` and (later) the TUI: `prompt`-kind expansion, `` !`cmd` `` pre-execution
   through the Bash tool path gated by `allowed-tools`, `@path` attachments read via the Read tool
   path and appended as snapshots.
D. **AskUserQuestion seam**: `question` event + `question_reply` wait in `Session` (keyed by
   `request_id`, abort-aware) mirroring the permission wait H2c/U2 use; print mode → error result
   (unchanged); `controller.answer_question` wired.
E. **WebSearch**: registered only when the main provider is OpenRouter; implemented as a side call
   with OpenRouter's web plugin / `:online` variant on the small or main model, returning the answer
   + `annotations[].url_citation` list; on other providers the tool is absent (Claude Code's
   WebSearch is server-side; we document the difference).
F. **Prompt**: the harness self-description gains one sentence each for hooks, skills, custom
   commands and WebSearch, still byte-stable and registry-driven.

## Tests (≥ 60 new, OS-neutral)
`tests/helpers/hook_scripts.py` (Python scripts: exit-2-with-JSON-allow, JSON deny, plain-context,
sleep-for-timeout, env-file writer, updatedInput); matcher forms incl. invalid regex skip; `if`
filtering; every handler type (http against a local server; mcp_tool against the fake MCP server;
prompt/agent against the mock upstream); exit-code table; JSON output fields incl. `defer` on
PreToolUse only; combination rules; caps; timeouts + SessionEnd budget; dedup + `once`; Stop cap 9th
block overrides; `CLAUDE_ENV_FILE` → Bash env; untrusted layer drop; `disableAllHooks`; call-site
ordering (PreToolUse after decide / before dispatch, batched pool verdicts first; PostToolUse before
truncation); Skill tool body + substitutions + `allowed-tools` session rule + hidden skills; custom
command `!` gating; AskUser wait/reply; WebSearch mock (plugin field present only on OpenRouter).

## Acceptance
Both suites green on Windows and WSL. Live (default model): with a temp project whose
`.claude/settings.json` has a PreToolUse hook script that rewrites a Bash command's `updatedInput`
and a UserPromptSubmit hook that adds context, `python -m rolo_claude -p "run echo original and
reply with the output" --permission-mode auto` shows the rewritten command in `--verbose` and the
added context in the session log; a Stop hook that exits 2 once makes the model continue exactly
one more step; `python -m rolo_claude -p "/anthropic-skills:docx" ...` invocation expands the synced
skill body (visible in the log); `python -m rolo_claude -p "search the web for the current DeepSeek
V4 pricing and cite a URL"` → answer with a citation (WebSearch on OpenRouter); Read line count and
memory question unchanged; proxy pong; settings.json unchanged; `~/.claude.json` checksum unchanged.
Report ≤ 60 lines. Rules as in the other briefs.
