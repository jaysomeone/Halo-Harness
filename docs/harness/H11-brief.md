# H11 brief — Claude models through the user's Claude subscription (`cc:` route) + `ant:` aliases

rolo (2026-09-28): "add the ability of halo to call anthropic subscriptions in the harness
too … fable 5.1, opus 5.5, opus 5, opus 4.8, opus 4.6, and the latest sonnet model if claude
subscription is being used."

Binding constraint: halo never reads, copies or replays Claude Code's OAuth credentials
(`~/.claude/.credentials.json` or the macOS keychain) and never calls `api.anthropic.com` with
them — Anthropic reserves subscription credentials for its own clients and the API rejects them
from anything else. The subscription is used the one legitimate way: **the installed `claude`
binary is the provider.** halo drives it headlessly under the user's own login; halo
keeps its tools, permissions, hooks, cards, session log and telemetry. Pay-as-you-go access to the
same models via `ANTHROPIC_API_KEY` is the existing `ant:` route, which gets first-class aliases.

Repo: `~\Documents\vibes\appDev\halo\` (Windows build host; **Kali Linux is
primary**). Baseline = tag `v0.3.1` (+ the RECOMMENDATIONS docs commit): run_all 1507, test_bridge
97, test_tui 46, green on Windows, WSL and the Kali VM. Only worker on the tree; no commits.

Verified facts (Fable, this box, Claude Code 2.1.281, `claude auth status` → loggedIn=true,
authMethod=claude.ai, no API key): `claude -p --model <id> --output-format json --max-turns 1
"reply pong"` answered for `claude-fable-5-1`, `claude-opus-5-5`, `claude-opus-5`,
`claude-opus-4-8`, `claude-opus-4-6`, `claude-sonnet-5`; alias `sonnet` → `claude-sonnet-5-5`
(the latest Sonnet), alias `opus` → `claude-opus-5-5`. The result JSON carries `modelUsage`,
`usage`, `total_cost_usd` (an estimate — subscriptions are not billed per token), `duration_api_ms`.
2.1.281 has `--tools`, `--disallowedTools`, `--strict-mcp-config`, `--mcp-config` (inline JSON),
`--input-format stream-json`, `--output-format stream-json`, `--include-partial-messages`,
`--settings` (inline JSON), `--session-id <uuid>`, `--resume`, `--append-system-prompt`,
`--system-prompt`, `--bare`, `--max-turns`, `--permission-mode`. Running `claude -p` from inside
another Claude Code session works (the proxy launcher does it every acceptance run).

## Read first
`~/.claude/plans/typed-tickling-squirrel.md` ("Decisions", "Auto mode = uninterrupted +
steering", D3/D4 event + loop model), `docs/harness/claude-help-2.1.281.txt`,
`docs/harness/claude-code-2.1.281-binary-facts.md` (stream-json shapes, MCP naming),
`halo_harness/{model.py,providers/routing.py,providers/stream.py,providers/anthropic_sse.py,
agent/loop.py,agent/log.py,agent/derive.py,tools/registry.py,mcp/manager.py,headless.py,
controller.py,doctor.py,catalog_cli.py}`, `tests/helpers/{mock_anthropic,fake_mcp_server}.py`,
`bridge.py` (`find_claude_exe`, how the proxy launches `claude` on Windows and POSIX).

## A. Model refs, aliases, catalog
- `cc:<name>`: `fable`→`claude-fable-5-1`, `opus`→`claude-opus-5-5`, `opus-5`/`opus-5.0`→
  `claude-opus-5`, `opus-4.8`→`claude-opus-4-8`, `opus-4.6`→`claude-opus-4-6`, `sonnet`→ Claude
  Code's own `sonnet` alias (latest Sonnet, today 5.5), `sonnet-5`→`claude-sonnet-5`, `haiku`→
  `haiku`; any full id and the `[1m]` suffix pass through unchanged to `claude --model`.
- `ant:` gets the same alias names mapped to API ids (`ant:sonnet` → the same id `sonnet` resolves
  to, discovered once per catalog refresh from Claude Code's result `modelUsage` key or the
  OpenRouter `anthropic/*` catalog rows); model_table rows for all six (context, output cap,
  adaptive-thinking rule for 4.6+/5.x, prices from the OpenRouter `anthropic/*` rows, `[1m]`
  variants where Claude Code offers them).
- Bare aliases `fable|opus|opus-5|opus-4.8|opus-4.6|sonnet|haiku` on `--model`/`/model`: resolve
  to `cc:` when `claude auth status` reports a login and no `ANTHROPIC_API_KEY` is set, to `ant:`
  when the key is set, else an error naming both options. `halo models --cc` lists the
  subscription models; the `/model` picker gets a "Claude subscription (via Claude Code)" group.

## B. Transport — one `claude` subprocess per halo session, lazily started
Command (verify every flag live; fall back to `--disallowedTools <all built-ins>` if `--tools ""`
does not disable them): `claude -p --output-format stream-json --input-format stream-json
--verbose --include-partial-messages --model <id> --session-id <uuid5 of the rolo session id>
--permission-mode bypassPermissions --tools "" --strict-mcp-config --mcp-config '<inline: one
stdio server "rolo" = <sys.executable> -m halo_harness.ccbridge --endpoint <endpoint>>' --settings
'{"disableAllHooks": true}' --append-system-prompt <short cc addendum>`; `--resume <uuid>` when
the rolo session resumes (`-c`, `-r`) and the meta node carries the mapping. Keep stdin open
across turns: each rolo turn = one stream-json user line; read events until the `result` line.
`bypassPermissions` is correct here because halo's own engine gates every bridged tool
call before it runs; Claude Code's prompts are unanswerable headlessly anyway.

**The bridge** (`halo_harness/ccbridge/`): parent side `ToolBridgeServer` — a local endpoint per
session (Unix socket `~/.halo/run/<sid>.sock` mode 0600 on POSIX; TCP loopback + a random
per-session token on Windows; a connection without the token is refused) speaking newline JSON-
RPC: `tools/list` → the session's frozen catalog definitions (built-ins, MCP tools, ToolSearch,
Agent, plan-mode tools, AskUserQuestion, TodoWrite, WebFetch/WebSearch, BashOutput/TaskStop);
`tools/call` → the normal dispatch path (permission decide → PreToolUse/PostToolUse hooks → tool
run → tool_use + tool_result nodes in the log → TUI events) returning content blocks. Child side
`python -m halo_harness.ccbridge` — a stdio MCP server on the official `mcp` SDK that forwards
list/call to the parent; Claude Code sees `mcp__rolo__<Name>`; text and image content map both
ways; `isError` for error results. Claude Code's own built-ins (Bash, Read, Edit, Write, Glob,
Grep, WebFetch, WebSearch, Agent/Task, TodoWrite, AskUserQuestion, Skill, EnterPlanMode/
ExitPlanMode, NotebookEdit, BashOutput/KillShell) are all disabled — halo provides them.

**Log and derive**: for `cc:` sessions the model-visible context lives in Claude Code, so
`derive_request` is not used; halo still logs user turns, assistant text (from `assistant`
events), tool_use/tool_result pairs (bridge calls, Claude Code's tool_use ids kept verbatim),
thinking summaries when present, and a `usage` node per turn from the `result` line (`usage`,
`total_cost_usd` marked `estimate: true`, `duration_api_ms`, model, route `cc`). Telemetry,
export, `/stats`, resume and the TUI all keep working. Switching from `cc:` to another route mid-
session derives from our log (works as today); switching into `cc:` mid-session sends the prior
log as one `<conversation-so-far>` user message (documented v1 behaviour).

**Steering / Esc**: a stream-json line sent mid-turn is queued by Claude Code, not a cut — for
`cc:` routes `steer` sends the line immediately and the TUI shows "↳ queued for Claude Code";
Esc terminates the subprocess (process group), synthesizes interrupted results for open
tool_uses, and the next turn restarts with `--resume`; quit kills it; SIGHUP/SIGTERM kill it; no
orphans (pgrep on Linux). CLAUDE.md, rules, memory, skills descriptions are loaded by Claude Code
itself from the same files — halo does NOT inject its instruction/memory snapshots for
`cc:` turns; halo's Skill tool stays the bridged one. Claude Code auto-compacts its own
context; halo's compaction is skipped for `cc:` sessions (`/compact` prints a note).
Sub-agents run in halo via the bridged Agent tool; children inherit `cc:` by default.

**doctor**: "Claude subscription: logged in (claude.ai) via claude <version> — cc: models
available" from `claude auth status` (parse its JSON; never open the credentials file; on the
Kali VM `claude` is at `~/.local/bin`). Not installed / not logged in → precise one-line errors
when a `cc:` model is requested ("install Claude Code" / "run `claude` once and log in").
Never run `claude login`/`logout`, never write `~/.claude.json` or `settings.json`.

## C. Tests (OS-neutral, ≥ 40)
`tests/helpers/fake_claude_cc.py`: an executable that emulates `claude auth status` and the `-p`
stream-json protocol (`system/init`, `stream_event` text deltas, `assistant` with an
`mcp__rolo__Read` tool_use, the `user` tool_result echo, `result` with usage/cost) AND actually
acts as an MCP client: it spawns the bridge server named in the `--mcp-config` it receives and
performs real `tools/list` + `tools/call` over stdio. Cover: alias resolution (`cc:`, `ant:`, bare
names with/without login/key); lazy start, one subprocess per session, kill on quit/Esc/SIGHUP
(Linux pgrep); `tools/list` equals the frozen catalog; `tools/call` obeys a deny rule (error
result), shows a card in `default` mode (TUI pilot), runs in `auto`; hooks fire exactly once;
pairing invariants after Esc mid-call; usage node with `estimate`; resume via the uuid mapping
and `--continue`; steer = queued line; model switch cc→or and or→cc; Windows loopback token
refusal; POSIX socket mode 0600; `claude` missing and not-logged-in errors; a sentinel
`.credentials.json` under BRIDGE_TEST_HOME that is never opened; `--tools ""` vs disallowed
fallback selection. Suites green on Windows, WSL and the Kali VM (`claude` 2.1.263 logged in
there — do not touch its login).

## D. Live acceptance (Fable re-runs on this box, subscription login, no API key)
`halo --model cc:sonnet -p "reply with the single word pong"` → pong; `--model cc:fable -p
"read README.md and reply with only its line count" --permission-mode auto` → the Read runs
through the bridge (tool_use/tool_result in the log) and the count is right; `--model cc:opus-4.6
-p "count the .py files under halo_harness/tools with Glob"`; TUI: `/model cc:opus`, a Bash call
shows a permission card in `default` mode and runs after `y`; Esc mid-turn kills the subprocess
and the next prompt resumes; `--continue` on a `cc:` session; `stats --models` shows route `cc`
rows with estimated cost; `doctor` shows the subscription line; `ant:fable` resolves (mock);
`~/.claude/settings.json`, `~/.claude.json` untouched and `.credentials.json` never read.

## E. Docs and release
README section "Claude models with your subscription" (how it works, what is never read,
limitations: queued steering, Claude Code's own compaction, estimated cost); CHANGELOG;
`__version__` 0.4.0. Report ≤ 60 lines. Rules as in the other briefs (no safety/refusal language
in prompts or tool text, ≤ 250 lines per write, no heredocs with backslashes on Windows).
