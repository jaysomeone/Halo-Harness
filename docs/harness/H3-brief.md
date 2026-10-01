# H3 brief — MCP client, frozen catalog + lazy load, `mcp` CLI, Claude in Chrome + Playwright (halo)

Repo: `~\Documents\vibes\appDev\halo\` (Windows build host; **Kali Linux primary**
— OS-neutral, `/bin/bash` on Linux). Baseline = the H2b commit on master with both suites green on
Windows and WSL. Do not commit.

## Read first
1. `~/.claude/plans/typed-tickling-squirrel.md` → "Research-driven revisions" points 4 (frozen catalog,
   Databricks 32-tool cap, lazy load), 5, 13; "D-CFG" → **MCP** paragraph (SDK, transports, scopes,
   approval keys, naming, timeouts, output caps, content conversion) and "B. Claude Code config on this
   box" (rolo's 15 stdio servers in `~/.claude.json`, mixed-separator project keys, no `.mcp.json`);
   "D5" deferred-tool strategy; "Decisions" (no safety heuristics).
2. `docs/harness/claude-code-2.1.281-binary-facts.md` §9 (MCP name sanitising `[^a-zA-Z0-9_-]→_`,
   `headersHelper` contract, `MCP_TIMEOUT` 30 000 / `MCP_CONNECT_TIMEOUT_MS` 5 000 / tool timeout
   ≥1000 else `MCP_TOOL_TIMEOUT` else 1e8, `MAX_MCP_OUTPUT_TOKENS` 25 000 with the truncation string,
   images = 1 600 tokens, `_meta` keys `anthropic/alwaysLoad|maxResultSizeChars|searchHint|
   requiresUserInteraction`, `annotations.readOnlyHint`, `mcp add` flags, `mcp list` line formats and
   statuses).
3. `docs/harness/claude-in-chrome-integration.md` (verified: `--chrome` = spawn
   `claude.exe --claude-in-chrome-mcp` as stdio MCP server `claude-in-chrome`; preconditions; the
   `CLAUDE_CHROME_PERMISSION_MODE=skip_all_permission_checks` env in auto/bypass) and the H7 row of the
   plan's milestone table (Playwright via `@playwright/mcp`).
4. Current code: `halo_harness/tools/{registry,base,toolsearch}.py`, `agent/{loop,prompt,derive}.py`,
   `providers/{request,hooks,profiles}.py` (32-cap + schema simplifier), `permissions.py` (`mcp__`
   rule forms), `config/{claude_json,settings,paths}.py`, `tests/helpers/*`.

## Scope
A. **`halo_harness/mcp/`** on the official `mcp` SDK (`pip install "mcp>=1.26,<3"` into the dev env;
   import lazily; if missing → MCP disabled with one notification): `client.py` (`McpLoop` daemon
   thread owning one asyncio loop; sync facade `run(coro, timeout)`), `stdio.py`
   (`StdioServerParameters(command, args, env, cwd)`; env = `effective_env` + server env +
   `CLAUDE_PROJECT_DIR`; POSIX `start_new_session` + `killpg`, win32 `PATHEXT` resolution +
   `CREATE_NEW_PROCESS_GROUP` + `taskkill /T /F`; stderr → `~/.halo/mcp/<server>.log` 5 MB
   rotate), `http_sse.py` (`streamablehttp_client`, `sse_client` with deprecation warning,
   `headersHelper` via shell when trusted, 10 s, stdout JSON object of strings; `oauth`/401 →
   `needs_auth`), `manager.py` (`McpServerHandle` protocol + `McpManager`: scope resolution
   `managed-mcp.json` exclusive → `--mcp-config` (files or JSON, repeatable; `--strict-mcp-config`) →
   local `projects[cwd].mcpServers` (both key forms) → `<cwd>/.mcp.json` (`${VAR}`/`${VAR:-d}`
   expansion, approval via `enabledMcpjsonServers`/`enableAllProjectMcpServers`/`-p`/our
   `~/.halo/mcp-approvals.json`, `disabledMcpjsonServers` wins) → user `mcpServers`; per-project
   `disabledMcpServers`; parallel startup bounded by `MCP_TIMEOUT`, failures non-fatal + notification;
   `mcpLazy` option; `status()`, `reconnect()`, `close_all()` with a 5 s deadline; resources and
   prompts listing).
B. **Tools**: `tools/mcp_tool.py` → `McpTool(server, tool)` named `mcp__<server>__<tool>` with
   sanitising per §9, `is_read_only` from `readOnlyHint`, result conversion (text; image → `image`
   block when `profile.vision` else a note; embedded resource → text/image; `structuredContent` JSON;
   `isError` → `is_error`), output cap = min(`_meta[anthropic/maxResultSizeChars]`,
   `MAX_MCP_OUTPUT_TOKENS`×4 chars) with the exact truncation string and spill to `tool-results/`.
   `ListMcpResourcesTool` / `ReadMcpResourceTool` built-ins; `@server:resource` mentions expanded in
   the user prompt; `/mcp__server__prompt args` expansion (best-effort).
C. **Frozen catalog + lazy load** (plan revision 4): at session start select the catalog once —
   built-ins + MCP tools marked `alwaysLoad` + the top MCP tools by config (`mcpPreload` list in
   `~/.halo/config.json`, default: none) — respecting the host cap (128 OpenRouter, **32
   Databricks**); everything else is deferred and reachable through `ToolSearch` (returns defs +
   `tool_reference` blocks; loading a deferred tool appends it to the session catalog = one accepted
   cache miss, never reorders; LRU of 100 loaded deferred tools; on Databricks the loaded set may never
   exceed 32 — evict least-recently-used deferred tools first). The system prompt lists MCP servers
   with their `instructions` one-liners + "use ToolSearch", never the tool list. Bare-name deny rules
   and `--disallowedTools`/`--tools` filter the catalog before freezing.
D. **CLI**: `halo mcp list` (Claude Code's exact line formats/statuses incl. `⏸ Pending
   approval`), `mcp get <name>`, `mcp add` (`-t stdio|sse|http`, `-s local|user|project`, `-e`, `-H`)
   writing to `~/.claude.json` ONLY when rolo explicitly runs `mcp add` (the harness never rewrites
   that file otherwise; preserve every other key byte-for-byte via a read-modify-write of the parsed
   JSON with the same indent), `mcp remove`, `mcp add-json`; `--mcp-config`, `--strict-mcp-config`;
   `/mcp` slash command data for the UI.
E. **Browser (brought forward from H7 because it is just two dynamic MCP entries)**:
   `--chrome` / `--no-chrome` / `claudeInChromeDefaultEnabled` → dynamic server `claude-in-chrome` =
   `{type: stdio, command: <claude exe via find_claude_exe()>, args: ["--claude-in-chrome-mcp"]}`
   with `CLAUDE_CHROME_PERMISSION_MODE=skip_all_permission_checks` in auto/bypass modes; `doctor`
   reports the preconditions (claude.exe found, Chrome + extension native host registered — check the
   registry key on win32 / the native-messaging manifest dirs on Linux, pipe presence best-effort).
   `--playwright` → dynamic server `playwright` = `{type: stdio, command: "npx", args: ["-y",
   "@playwright/mcp@latest", …]}` with `--playwright-cdp <endpoint>` passthrough and
   `--playwright-headless`; `doctor` checks node/npx. Both are ordinary MCP servers afterwards
   (deferred tools via ToolSearch unless preloaded).

## Tests (≥ 60 new, OS-neutral)
`tests/helpers/fake_mcp_server.py` (stdio JSON-RPC 2.0 over the SDK's wire format: `initialize`,
`tools/list` with `FAKE_MCP_TOOL_COUNT` 5 or 300, `tools/call` echo + image + `isError` + huge output,
`resources/list|read`, `prompts/list|get`, modes slow/crash/needs-auth); manager scope precedence
(local > .mcp.json > user; `--strict-mcp-config`; `managed-mcp.json` exclusive), approval states,
`${VAR:-d}` expansion + credential-name blanking for remote url/headers, name sanitising, timeouts
(`MCP_TIMEOUT=100` → failed state, others fine), output cap + spill + truncation string, image
conversion vs no-vision note, readOnlyHint → pool, frozen catalog selection under 128 and 32 caps,
ToolSearch load → next request carries the tool + `tool_reference`, LRU eviction on Databricks,
`mcp list` line formats, `mcp add` round-trip on a temp `~/.claude.json` preserving unrelated keys,
`--chrome`/`--playwright` produce the right server specs (spawn is mocked), e2e: `-p "use the fake
server's echo tool"` through the mock upstream with `ScriptedTurns`.

## Acceptance
Both suites green on Windows and WSL (four RESULT lines). Live at home (default model):
`python -m halo_harness mcp list` shows rolo's real 15 servers with health; `python -m halo_harness -p
"use the expanded-models MCP server's list_available_models tool and summarise the roles in one line"`
→ a real answer through `mcp__expanded-models__list_available_models` (visible with `--verbose`);
`python -m halo_harness --chrome -p "list my open browser tabs"` → either the real tab list (Chrome +
extension running) or the documented precondition message from `doctor`, never a crash;
`python -m halo_harness --playwright -p "open https://example.com with playwright and tell me the page
title"` → "Example Domain" (npx downloads @playwright/mcp on first use; if the download is blocked,
report exactly what happened); Read line count and memory question still work; `proxy launch … -- -p
"reply with the single word pong"` → pong; `proxy --stop`; settings.json unchanged; `~/.claude.json`
byte-identical before/after (assert with a checksum — the harness must never rewrite it).
Report ≤ 60 lines. Rules as in the other briefs.
