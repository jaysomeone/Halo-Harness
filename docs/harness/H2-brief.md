# H2 brief — built-in tools, repair layer, permissions (rolo-claude 0.3.x)

Repo: `C:\Users\user\Documents\vibes\appDev\rolo-claude\` (Windows build host; **Kali Linux is the
primary platform** — everything must be OS-neutral, `/bin/bash` on Linux, Git Bash only on win32).
Baseline: H1 commit on master — `python test_bridge.py` → 97 green, `python tests/run_all.py` →
185 green, both also green in WSL Ubuntu (`wsl -e bash -lc 'rsync -a --delete --exclude .git
--exclude __pycache__ /mnt/c/Users/user/Documents/vibes/appDev/rolo-claude/ ~/rolo-claude-wt/ && cd
~/rolo-claude-wt && python3 tests/run_all.py | tail -1 && python3 test_bridge.py | tail -1'`).
Keep all of it green on both. Do not commit.

## Read first
1. `docs/harness/review-findings-h1.md` (Opus review of H1) — fix every finding first, then do the
   "H2 must-do" paragraph.
2. `~/.claude/plans/typed-tickling-squirrel.md` → "Primary use case", "Research-driven revisions"
   (esp. 2, 4, 7, 8, 13), "Decisions taken with rolo" (**no safety heuristics: no classifier, no
   destructive-command prompts, no protected paths; `auto` = allow all except deny/ask rules**),
   D4 (execution, repair layer), D5 (tools), D6 (permissions grammar/modes), D-CFG "Permission grammar
   and matcher" + "Rule writes", D9.
3. `docs/harness/claude-code-2.1.281-binary-facts.md` §3 (read-only Bash lists), §4 (PowerShell
   aliases, case rules), §5 (rule parsing, `:*`, ` *`, escapes, WebFetch domain, MCP rule forms — the
   protected-path list is NOT reproduced), §14 (tool wording).
4. `reports/Open weight model adapter rules.md` → sections on edit tooling, tool-call leakage
   parsers and args repair (the `leak_parser`/`args_repair` code branches) and the consolidated JSON
   block (H1 left the full 64-row ingestion + explicit 8 code-branch hooks as deferred work — do it
   in H2 if the review's must-do list confirms).
5. Current code: `rolo_claude/tools/{base,registry,read}.py`, `agent/{loop,invariants,derive,
   prompt}.py`, `providers/{request,profiles}.py`, `tests/helpers/{fake_home,mock_openai,runner}.py`.

## Scope
A. **Built-in tools** (`rolo_claude/tools/`), Claude Code names/schemas/wording (binary facts §14):
   `Write` (must-Read-first, parents created, BOM/CRLF preserved), `Edit` (`old_string`/`new_string`/
   `replace_all`; exact match once; whitespace-tolerant fallback match; structured "not found / N
   near-matches" error; mtime check since Read), `Bash` (`command`, `description`, `timeout` ms ≤ 600 000,
   `run_in_background` deferred to H8; `/bin/bash -lc` on POSIX, Git Bash on win32; merged stdout/stderr
   streamed as `tool_progress` every 0.5 s; process-group kill on timeout/abort; `cd` persistence per
   session via a trailing marker line that reports the final cwd; `CLAUDECODE=1`; `effective_env`),
   `PowerShell` (win32 only), `Glob` (rglob, prune `.git/node_modules/.venv/__pycache__`, mtime sort,
   500 max), `Grep` (`rg` when on PATH else pure Python; output modes `files_with_matches|content|count`,
   `-i -n -A -B -C`, `glob`, `type`, `head_limit`, `multiline`), `WebFetch` (urllib, 30 s, same-host
   redirects, HTML→text, 15-min cache, optional small-model summary), `TodoWrite`, `ToolSearch`
   (deferred-tool loader over the frozen catalog — MCP tools arrive in H3, so ToolSearch indexes the
   registry now), `AskUserQuestion` (emits `question`; print mode → error result), `Skill` stub (real
   skills in H4). Every tool: `is_read_only`, `is_destructive` (informational only — NOT a gate),
   `summary(input)`, `permission_content(input)`, result truncation to disk (`<session>/tool-results/
   <id>.txt`; model sees head 60 % + tail 30 % + pointer; Bash 30 000 chars, Read 2 000 lines /
   2 000 chars per line, WebFetch 100 KB). Consecutive read-only calls run on a 4-thread pool with
   results re-ordered; everything else sequential. Tool result blocks follow Anthropic shapes.
B. **Repair layer** (`agent/repair.py`): text-embedded calls only behind the profile flag and only
   when no native call exists (DSML `<｜DSML｜invoke>`, Hermes/Qwen `<tool_call>`/`<function=…>`,
   Kimi `<|tool_call_begin|>`, GLM `<tool_call>name<arg_key>`, MiniMax `<minimax:tool_call>`, fenced
   JSON with `name` + `arguments|input|parameters`); lenient JSON args (trailing commas, single
   quotes, Python-repr → JSON for GLM, unbalanced braces); unknown tool name → normalise
   (`read_file`→`Read`, case/`_`/`-`) then `difflib` ≥ 0.85 else error listing 5 closest; 60-line
   schema validator (required, types, enum, coercions) → error result naming missing params;
   duplicate (name, args) in one message → "(duplicate of <id>)"; `length`-truncated call → "split the
   operation" error (never `invalid`); one retry with `tool_choice: required` ONLY where the profile
   says it is supported (never DeepSeek thinking / GLM / Qwen).
C. **Permissions** (`rolo_claude/permissions.py`) per D-CFG grammar exactly: `Rule` kinds, `parse_rule`
   with `\(` `\)` `\\` unescape (Windows path exception), `:*` / trailing ` *` / inner `*` / exact,
   PowerShell alias canonicalisation + case-insensitive matching (binary facts §4), WebFetch
   `domain:` incl. `*.` semantics, `mcp__srv`/`mcp__srv__*`/`mcp__srv__tool` (parenthesised → invalid),
   tool-name globs (deny/ask only), `Agent(x)`, `Skill(x)`/`Skill(x *)`, `Tool(param:value)` (deny/ask
   only), path rules (`//abs`, `~/`, `/rel-to-source` with per-source base dirs, cwd-relative,
   gitignore `**`/braces/`!`, Windows drive → `/c/` form, allow anchored at cwd vs deny/ask any depth,
   Read deny blocks Edit/Write, Edit rules cover Write, `Write(...)`/`Glob(...)` never consulted), Bash
   normalisation (quote-aware split on `&&`, `||`, `;`, `;;`, `|`, `|&`, `&`, newline; wrapper stripping
   for allow; deny/ask see raw + subshell bodies; every segment must match for allow), read-only Bash
   whitelist (binary facts §3, incl. the `git`/`gh` read-only maps), `decide` order deny → ask →
   allow → mode; sources policy > flag > local > project > user (+ `projects[cwd].allowedTools`,
   `--allowedTools/--disallowedTools`, session rules; project/local only when trusted); modes
   `default` (reads inside working dirs allowed, else ask), `acceptEdits` (+ Edit/Write/`mkdir touch rm
   rmdir mv cp sed` inside working dirs), `plan` (reads + plan file only), `dontAsk` (ask → deny),
   **`auto` = allow everything not matched by deny/ask, `bypassPermissions` = allow everything not
   matched by deny** — NO classifier, NO destructive-pattern list, NO protected paths; print mode:
   `ask` → deny + `is_error` tool_result naming `suggested_rules[0]` + `permission_denials`;
   `suggested_rules` per D-CFG; `add_allow_rule(text, "local")` writes to `<cwd>/.claude/
   settings.local.json` with Claude Code's escaping (tmp + `os.replace`), `"user"`/`"project"`/
   `"session"` destinations; bare tool names in deny/`--disallowedTools` remove the tool from the
   catalog for the session (catalog stays frozen afterwards). Interactive `ask` = `permission_request`
   event + wait for `permission_reply` (allow_once | allow_session | allow_always | deny + message).
D. **Loop integration**: execution order per turn = schema validate → repair → permission decide →
   (hooks placeholder for H4) → run → truncate → result; interrupt semantics; `--max-turns`;
   `--allowedTools`/`--disallowedTools`/`--permission-mode`/`--dangerously-skip-permissions`/`--tools`
   wired in `cli.py`; the harness self-description in `prompt.py` now lists every registered tool with
   one guidance sentence each (byte-stable); `permission_denials` in the json result.
E. **Canonical model table + code-branch hooks** (if the review's must-do confirms): ingest the
   report's consolidated JSON block (64 rows, family defaults + overrides, unverified flags kept) and
   expose the 8 named hooks in `profiles.py`.

## Tests (≥ 70 new, exit-code gated, OS-neutral)
Tools on a temp tree (CRLF/BOM/unicode, Windows and POSIX paths), Edit near-miss + tolerant match,
Bash timeout/kill/cd persistence/`[exit code]`, Grep parity between `rg` and the Python fallback,
Glob pruning/limits, truncation spill, read-only pool ordering, WebFetch against a local HTTP server,
ToolSearch over the registry; repair: every leak format above, lenient JSON, near-miss rename,
schema coercions, duplicate calls, length-vs-malformed; permissions: the D-CFG test enumeration
(the 19 real rules round-trip, `ls *` vs `lsof`, `:*` placement, compound/subshell, wrapper/env
stripping, `Read(src/**)` allow vs deny, `//`/`~/`/`/`-relative, negation, Read-deny-blocks-Edit,
domain wildcards, mcp forms, param rules, deny>ask>allow, user deny beats project allow, untrusted
project allow ignored, every mode-table row incl. **auto allows a non-whitelisted command with no
prompt and honours a deny rule**, print-mode denial + suggested rule, `add_allow_rule` escaping
round-trip, PowerShell alias canonicalisation, bare-name removal from the catalog); e2e through the
mock upstream with `ScriptedTurns`: Read→Edit→Bash loop, permission deny in `-p`, breaker.

## Acceptance (paste verbatim, trimmed)
1. Both suites green on Windows and in WSL (RESULT lines + exit codes for all four runs).
2. Live (default model `or:deepseek/deepseek-v4.1-flash`): `python -m rolo_claude -p "create a file
   C:\Users\user\Documents\vibes\appDev\rolo-claude\wip\h2_scratch.txt containing the three words
   alpha beta gamma on one line, then change beta to delta, then run a shell command that prints the
   file, and reply with only the final file content" --permission-mode auto` → `alpha delta gamma`
   (Write → Edit → Bash, three tool calls visible with `--verbose`); the same prompt with
   `--permission-mode default` in `-p` → the Write is denied with a suggested rule and
   `permission_denials` populated in `--output-format json`; `-p "grep for the string
   stream_completion in rolo_claude and reply with the file names"` → correct files (Grep); the H1
   memory question and Read line-count still answer; `proxy launch … -- -p pong` → pong; `proxy --stop`.
3. `grep -o '"model": "[^"]*"' ~/.claude/settings.json` unchanged; delete `wip/h2_scratch.txt`.

## Rules
≤ 250 lines per edit/write call; Write tool or Python-by-path for multi-line content; small tool
output; DeepSeek drafting via `tools/or_draft.py` allowed (cap $2, specs under
`wip/harness/spec-H2-*.md`); never weaken existing tests; no commits; no safety/refusal/"cyber"
language anywhere; report ≤ 60 lines with verbatim acceptance, deviations, spend.
