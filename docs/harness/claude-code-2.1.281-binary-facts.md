# Claude Code 2.1.281 — facts verified in the installed binary (read-only, 2026-09-23)

Source: `claude.exe` 2.1.281 help output, `claude mcp list`, `claude auto-mode defaults`, and string
greps of the binary. Minified identifiers (`Fm`, `qY`, …) are the binary's own. These override the
docs-derived notes in the plan where they differ. halo mirrors these unless a decision in the
plan says otherwise (e.g. "no cyber blocks": the auto-mode defaults, protected paths and other
safety heuristics are NOT reproduced).

## 1. CLI flags (`claude --help`)
- `--permission-mode <mode>` choices shown: `acceptEdits, auto, bypassPermissions, manual, dontAsk,
  plan`; `default` is still accepted (`manual` is the display alias: `Fm(e)=e==="manual"?"default":e`).
- `--allowedTools, --allowed-tools <tools...>` — comma or space separated (e.g. `"Bash(git *) Edit"`).
- `--disallowedTools, --disallowed-tools <tools...>` — same wording, "to deny".
- `--dangerously-skip-permissions`; `--allow-dangerously-skip-permissions` (enable bypass as an
  option without making it the default).
- `--permission-prompts <target>`: `host` (default; SDK host or `--permission-prompt-tool`) | `none`
  (anything that would prompt is denied). `--print` only.
- `--settings <file-or-json>`; `--setting-sources <sources>` (comma list of `user, project, local`).
- `--mcp-config <configs...>` (JSON files or strings, space separated); `--strict-mcp-config`.
- `--output-format text|json|stream-json` (`--print` only); `--input-format text|stream-json`;
  `--include-partial-messages` (stream-json only); `--max-budget-usd <amount>` (`--print` only).
- `--model <model>` (alias `fable|opus|sonnet` or full name); `--fallback-model <model>` (comma list;
  "re-tries the primary at the start of each user turn"); `--effort low|medium|high|xhigh|max`.
- `--add-dir <directories...>`; `--system-prompt <prompt>`; `--append-system-prompt <prompt>`.
- `-c, --continue`; `-r, --resume [value]` (session ID or picker); `--session-id <uuid>`;
  `--fork-session`.
- `--agents <json-or-file>`; `--agent <agent>`; `--bare` (skips hooks, LSP, plugin sync, auto-memory,
  CLAUDE.md discovery; sets `CLAUDE_CODE_SIMPLE=1`); `--verbose`; `-d, --debug [filter]`
  (`"api,hooks"`, `"!1p,!file"`); `--json-schema <schema>`; `--tools <tools...>` (`""` none,
  `"default"` all, or names `"Bash,Edit,Read"`).
- Hidden (not in help): `--permission-prompt-tool <tool>` (print only), `--max-turns <turns>` (print
  only), `--system-prompt-file <file>`, `--append-system-prompt-file <file>`.
- `--theme`: does not exist. (halo may add its own `--theme`.)
- Other flags rolo asked for by name (`--enable-auto-mode`, `--chrome`) were not in this capture:
  re-run `claude --help` and grep the binary for `--chrome`/`enable-auto-mode` when writing `cli.py`,
  and treat every flag in the live help as required (parity rule).
- `claude auto-mode defaults` → `{allow[17], soft_deny[70], hard_deny[1], environment[21]}`, all
  strings. NOT reproduced in halo (no cyber blocks).

## 2. Hook timeouts and caps
- Constants: `Ha=600000, Whe=30000, kwt=6000, Ghe=30000, vXe=5000`. Command hook default 600 s
  (`timeout` in config is SECONDS: `e.timeout*1000`); prompt hook 30 s; agent hook 60 s.
- SessionEnd budget: default 1500 ms, raised to the largest per-hook timeout, capped at 60 000 ms;
  env `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` overrides.
- Stop-hook cap: `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP ?? 8`; the 9th consecutive block force-ends the
  turn with "A hook blocked the turn from ending N consecutive times — overriding and ending turn."

## 3. Read-only Bash lists (no prompt in every mode)
- Any arguments (regex `^cmd(?:\s|$)[^<>()$`|{}&;\n\r]*$`): `docker ps, docker images, cal, uptime,
  cat, head, tail, wc, stat, strings, hexdump, od, nl, id, uname, free, df, du, locale, groups,
  nproc, basename, dirname, realpath, cut, paste, tr, column, tac, rev, fold, expand, unexpand, fmt,
  comm, cmp, numfmt, readlink, diff, true, false, sleep, which, type, expr, seq, tsort, pr`.
- No arguments only: `pwd, whoami, alias`. Exact argv: `claude -h|--help`, `node -v|--version`,
  `python|python3 --version`, `ip addr`.
- Second set (role unconfirmed, next to glob/variable checks): `ls cat head tail wc stat grep egrep
  fgrep diff du df echo strings hexdump od nl cut column tr tac rev cmp basename dirname realpath
  readlink sha256sum sha1sum md5sum cd`.
- Flag-by-flag safe lists exist for: `xargs`, `file`, `sed`, `sort`, `man`, `help`, `netstat`, `ps`,
  `base64`, `grep/egrep/fgrep`, `rg`, `sha*sum`, `tree`, `date`, `hostname`, `lsof`, `pgrep`, `tput`,
  `ss`, `fd/fdfind`, `docker logs/inspect`, `test`.
- Read-only git (each with `safeFlags`): `git diff, log, show, shortlog, reflog, stash list,
  ls-remote, status, blame, ls-files, config --get, remote show, remote, merge-base, rev-parse,
  rev-list, describe, cat-file, for-each-ref, grep, stash show, worktree list, tag, branch`.
- Read-only `gh`: `pr view/list/diff/checks/status`, `issue view/list/status`, `repo view`,
  `run list/view`, `auth status`, `release list/view`, `workflow list/view`, `label list`, `search *`.

## 4. PowerShell
- Alias table (~90 entries) e.g. `ls|dir|gci→Get-ChildItem`, `cat|type|gc→Get-Content`,
  `cd→Set-Location`, `ri|del|rd|rmdir|rm|erase→Remove-Item`, `mi|mv|move→Move-Item`,
  `ci|cp|copy|cpi→Copy-Item`, `iex→Invoke-Expression`, `iwr→Invoke-WebRequest`,
  `irm→Invoke-RestMethod`, `%→ForEach-Object`, `?→Where-Object`, `sls→Select-String`.
- Canonicaliser: lowercase; strip `.exe|.cmd|.bat|.com` when no path separator; map alias.
- PowerShell rule matching is case-insensitive (exact, prefix and wildcard with the `i` flag); each
  rule is tried against the raw and the canonicalised command. Bash matching is case-sensitive
  (`n===prefix || n.startsWith(prefix+" ")`, whitespace collapsed; `xargs <prefix>` also accepted).

## 5. Permission rule parsing
- `Tool(content)`: first `(` and LAST `)`; the `)` must be the final char and the tool name must not
  contain parens, else `malformed`; no parens → `bare`. `Bash()` (empty) = whole tool.
- Unescape: `\(`→`(`, `\)`→`)`, then `\\`→`\`. On Windows, content that looks like a path
  (`^(?:[A-Za-z]:\\|~\\|\\(?![()!#]))`) gets only the paren unescape and keeps its backslashes.
- `:*` suffix: `^(.+):\*$` → prefix; otherwise an unescaped `*` → wildcard, else exact. Validator
  messages: "The :* pattern must be at the end"; "Prefix cannot be empty before :*".
- Trailing ` *` in a wildcard pattern becomes `( .*)?` so `git *` also matches bare `git`.
  `\*` and `\\` are escapes. Suggested rules are written as `` `${n} *` ``.
- WebFetch: must use `domain:`; `domain:*` matches all; `domain:*.x` → `^domain:(?:[^.:]+\.)+x$`
  (subdomains only); bare `*` inside → `[^.:]*`; case-insensitive; host lowercased, trailing dots
  stripped.
- MCP: `mcp__server`, `mcp__server__*`, `mcp__*` remove every tool of the server; "MCP rules do not
  support patterns in parentheses"; allow rules permit globs only in the tool position after a
  literal `mcp__<server>__` prefix (`mcp__puppeteer__*`, `mcp__github__get_*`); deny/ask accept
  wildcards anywhere.
- Protected files (lowercased compare) and dirs (`.git .vscode .idea .claude .husky .cargo
  .devcontainer .yarn .mvn`, plus `.config/git`) exist in Claude Code — NOT reproduced (no cyber
  blocks).

## 6. `defaultMode` sources
- Only `policySettings`, `flagSettings`, `userSettings` may set `defaultMode`; from project/local
  `bypassPermissions` and `auto` are ignored with "settings defaultMode "…" ignored — only
  policy/user/flag settings may grant … (projectSettings and localSettings are repo-controllable)";
  other modes from project/local are ignored if they would widen an inherited agent mode.

## 7. `.claude/rules`
- Loaded from EVERY ancestor directory from cwd up to (not including) the filesystem root: per dir
  `CLAUDE.md`, `.claude/CLAUDE.md`, `.claude/rules/`, `CLAUDE.local.md` (needs projectSettings /
  localSettings enabled; skips main-repo dirs outside the current worktree). Also user
  `~/.claude/rules`, a managed rules dir, and `--add-dir` dirs when
  `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` is set.

## 8. `instructionFiles` and `CLAUDE_CONFIG_DIR`
- `instructionFiles` is an option of the built-in `agents-md` plugin: stored at
  `pluginConfigs[<plugin>].options.instructionFiles`; values `claude-md | claude-md-or-agents-md
  (default) | claude-md-and-agents-md | managed-only`; legacy `projectInstructions` maps
  `none→managed-only, claude→claude-md, agents-fallback→claude-md-or-agents-md, both→claude-md-and-agents-md`.
- `.claude.json` = `$CLAUDE_CONFIG_DIR/.claude.json` when set, else `.claude.json` in the home-ish dir
  (`Ot()`); a legacy `<configDir>/.config.json` wins if present.

## 9. MCP
- Name sanitising: `wn(e)=e.replace(/[^a-zA-Z0-9_-]/g,"_")` (claude.ai servers collapse `_+`);
  tool = `mcp__${wn(server)}__${wn(tool)}`; parsing splits on `__`, tail rejoined.
- `headersHelper`: `shell:true`, timeout 10 000 ms, maxBuffer 1e6; exit 0 + non-empty stdout that is a
  JSON object of strings (errors `exec_failed|parse_failed|non_object|non_string_value`); env gets
  `CLAUDE_CODE_MCP_SERVER_NAME`, `CLAUDE_CODE_MCP_SERVER_URL`, `CLAUDE_PLUGIN_ROOT`; repo-resident
  config needs persisted trust; helper headers override static `headers`.
- `MCP_TIMEOUT` default 30 000 (clamped ≤ 2^31−1); `MCP_CONNECT_TIMEOUT_MS` default 5 000;
  tool timeout = server `timeout` (≥1000) ?? `MCP_TOOL_TIMEOUT` ?? 1e8 ms (~27.8 h), clamped to
  [1000, 2^31−1]; HTTP request timeout floor 60 000.
- Output: `MAX_MCP_OUTPUT_TOKENS` default 25 000; cut at limit×4 chars with
  "[OUTPUT TRUNCATED - exceeded N token limit]"; images count 1 600 tokens; tokens counted only when
  the estimate exceeds 0.5×limit; UI warns above 10 000 tokens.
- `_meta`: `anthropic/searchHint`, `anthropic/alwaysLoad` (must be `=== true`),
  `anthropic/maxResultSizeChars` (positive finite), `anthropic/requiresUserInteraction`;
  `annotations.readOnlyHint` drives isReadOnly and isConcurrencySafe.
- `mcp add`: `-t, --transport stdio|sse|http` (default stdio), `-s, --scope local|user|project`
  (default local), `-e, --env`, `-H, --header`, `--client-id`, `--client-secret`, `--callback-port`;
  `add-json` also accepts WebSocket.
- `mcp list`: "Checking MCP server health…" then per server `${name}: ${url} (SSE|HTTP) - ${status}`
  or `${name}: ${command} ${args} - ${status}`; statuses `✔ Connected`, `! Needs authentication`,
  `! Connected · tools fetch failed`, `- Not configured`, `✗ Failed to connect`, `✗ Connection error`,
  `⏸ Pending approval`. Example: `codriver: node ~\codriver-mcp\dist\index.js - ✔ Connected`.

## 10. Hook payloads and decisions
- Base fields on every event: `session_id, transcript_path, cwd, scratchpad_dir?, prompt_id?,
  permission_mode?, agent_id?, agent_type?, effort?`.
- `PostToolBatch`: `tool_calls: [{tool_name, tool_input, tool_use_id, tool_response?}]`;
  `PostCompact`: `trigger: manual|auto, compact_summary`; `Notification`: `message, title?,
  notification_type`.
- PreToolUse `permissionDecision ∈ allow|deny|ask|defer`; other events `allow|deny|ask`.
- Output caps (`reason 2000, stopReason 2000, systemMessage 4000, additionalContext 8000` chars;
  line caps 20 / 200) apply only to hooks answered from an attached machine; no general cap found for
  local hooks.

## 11. Synced skills
- Always displayed as `anthropic-skills:<name>` (`vx(e)`); the bare name is an alias only when it
  doesn't collide and has no `:`/`mcp__`; manifest `source` does not change the prefix; plugin skills
  are `<plugin>:<skill>`. Location `<configDir>/skills/synced/` with `manifest.json`
  (`{lastUpdated, skills:[{name, skillId, description, source, updatedAt|null}], staleDirs?,
  pendingClaims?}`), staging `skills/.staging`, trash `skills/.trash`.

## 12. Auto-memory
- Slug: `e.replace(/[^a-zA-Z0-9]/g,"-")`; if > 200 chars → `slice(0,200) + "-" + abs(javaHash(e)).toString(36)`
  (`(h<<5)-h+charCode|0`). Path: `join(CLAUDE_CODE_REMOTE_MEMORY_DIR || configDir, "projects",
  slug(git root or cwd), "memory")` — note: **git root when inside a repo, else cwd**.
- `MEMORY.md` caps: 200 lines, 25 000 bytes ("lines after 200 will be truncated"; error "MEMORY.md
  content exceeds the prompt-index cap").

## 14. Tool-usage wording to adapt for weaker models
- "Performs exact string replacements in files."
- "You must use your Read tool at least once in the conversation before editing." / "…or the call will fail."
- "When editing text from Read tool output, ensure you preserve the exact indentation (tabs/spaces)
  as it appears AFTER the line number prefix… Never include any part of the line number prefix in
  the old_string or new_string."
- "The edit will FAIL if `old_string` is not unique in the file. Either provide a larger string with
  more surrounding context to make it unique or use `replace_all`…"
- "Keep `old_string` minimal — usually 1-3 lines, only enough to be unique in the file."
- "ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required."
- "The file_path parameter must be an absolute path, not a relative path"
- "Avoid using this tool to run ${cmds} commands, unless explicitly instructed or after you have
  verified that a dedicated tool cannot accomplish your task."
- "Try to maintain your current working directory throughout the session by using absolute paths
  and avoiding usage of `cd`."
