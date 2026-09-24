# H5 brief — compaction + pruning, native Anthropic routes, reasoning display, cost (rolo-claude)

Repo: `C:\Users\user\Documents\vibes\appDev\rolo-claude\` (Windows build host; **Kali Linux primary**
— OS-neutral). Baseline = the H4 commit on master, both suites (+ `test_tui.py`) green on Windows
and WSL. Do not commit.

## Read first
1. `~/.claude/plans/typed-tickling-squirrel.md` → "Research-driven revisions" points 5 (Anthropic
   passthrough for Claude-family routes), 9 (**compaction = dsh replay + OpenCode pruning**, exact
   thresholds and the 8-section checkpoint), 12 (defaults), D3 (native Anthropic decoder,
   `ant:` provider), D8 compaction bullet, D-CFG "Compaction" knobs from finding C
   (`autoCompactEnabled`, `autoCompactWindow`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW`,
   `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, `DISABLE_COMPACT`, `/compact [instructions]`, what is
   re-injected after compaction, SessionStart(compact) + PreCompact/PostCompact hooks).
2. `reports/DeepSeek and OpenRouter ori harnesses.md` → the dsh compaction paragraph (prefix replay,
   8 sections: Primary Request and Intent, Key Technical Concepts, Files and Code, Errors and Fixes,
   Pending Jobs, Current Work, Next Step, Critical Context; preamble "continue without acknowledging";
   `max_tokens` finish = failure; merge prior `<compacted-summary>`), OpenCode pruning numbers, and
   the wire-format table for the Anthropic routes (Databricks `/ai-gateway/anthropic/v1/messages`,
   `/serving-endpoints/anthropic/v1/messages`; FMAPI reasoning blocks).
3. `reports/Open weight model adapter rules.md` → the Claude family rows (thinking blocks + signature
   replay, `output_config.effort`, cache_control breakpoints on OpenRouter) and `reasoning_echo`
   for Messages routes.
4. Current code: `rolo_claude/providers/{stream,anthropic_sse,request,profiles,hooks}.py`,
   `agent/{loop,log,derive,prompt}.py`, `output.py`, `tui/` (thinking block, status bar context %),
   `hooks.py` (PreCompact/PostCompact/SessionStart(compact) call sites exist as no-ops from H4).

## Scope
A. **Pruning before summarising** (`agent/prune.py`): tool results > 8 192 chars → head 4 096 /
   tail 1 024 in the DERIVED request (the log keeps the full text); tool bodies outside a 40k-token
   protection window stubbed to 2 000-char excerpts; images offloaded after N turns; all deterministic
   and cache-prefix-preserving (only the tail changes).
B. **Compaction** (`agent/compact.py`): trigger at 80 % of (context − reserved output − 65 536) using
   the provider's last `prompt_tokens` (else estimate), ≈70 % on 128k-class endpoints; honour the
   Claude Code knobs above; `/compact [instructions]`; on `ContextOverflow` compact then retry once,
   second overflow → error. Mechanism = one summarisation call that replays the EXACT cached prefix
   (system node + tools + messages) plus one final user instruction demanding the 8 sections; result
   validated (all 8 headings present, no `max_tokens` finish) with one corrective retry; new transcript
   = `<compacted-summary>` user node (preamble + summary, merging any prior summary) + the last 16 %
   of context verbatim (never splitting a reasoning + tool_use + tool_result unit) + re-injected
   snapshots (project-root CLAUDE.md, unscoped rules, memory index, plan file) + "files read this
   session" list; logged as a `compacted` node with `surface_op: replace` so `derive_request` skips
   the shadowed range; PreCompact (`manual|auto`, `custom_instructions`) → summarise → PostCompact
   (`compact_summary`) → SessionStart(compact) hooks; `compaction` event for the UI; `compactionModel`
   config (default main).
C. **Native Anthropic routes** end-to-end in the harness: `ant:` provider (`ANTHROPIC_API_KEY`,
   `api.anthropic.com`, `anthropic-version`), Databricks Claude passthrough (`dbx:databricks-claude-*`,
   `dbx:system.ai.claude-*` → `/ai-gateway/anthropic/v1/messages` / `/serving-endpoints/anthropic/v1/
   messages` with `x-databricks-use-coding-agent-mode`), and OpenRouter `anthropic/claude-*` through
   the OpenAI dialect with `cache_control` breakpoints (≤ 4, on the system node and the last tool
   result). Request builder for Messages: thinking `{type: enabled, budget_tokens}` from `--effort`,
   `output_config.effort` for Opus/Fable-class, tools as `input_schema`, thinking blocks + signature
   replayed verbatim, `tool_reference` → text, stream via `AnthropicSSEDecoder` incl. `thinking_delta`
   / `signature_delta`, usage with cache fields, count_tokens relay for the estimate. Every Claude
   route gets the same session log/invariants/permissions as the OpenAI ones.
D. **Reasoning display + cost**: `thinking_delta` events for OpenAI-dialect reasoning
   (`reasoning`/`reasoning_content`/Databricks blocks) rendered dimmed/collapsible in the TUI and
   under `--verbose` in print mode; `CostMeter` completes per-family pricing from `models.json`
   (OpenRouter) and a Databricks "n/a"; `/cost` and `/context` (breakdown: system, tools, messages,
   snapshots, pruned) show live numbers; status bar context % uses the provider's `prompt_tokens`.
E. **Robustness**: streaming 429/5xx backoff already exists — add Retry-After parsing on the
   Anthropic routes (`retry-after`, `anthropic-ratelimit-*` headers), overflow wording for the
   Anthropic API ("prompt is too long: N tokens > M maximum") → compaction.

## Tests (≥ 60, OS-neutral)
Pruning determinism and prefix stability (bytes before the pruned tail identical); compaction
trigger math for three profiles; the summariser call replays the exact prefix (assert bytes) and the
final user instruction; 8-heading validation + retry; `<compacted-summary>` merge; unit atomicity;
re-injected snapshots; `compacted` node and `derive_request` skipping; overflow → compact → retry →
second overflow error; `/compact` with instructions; knobs (`DISABLE_COMPACT`, pct override,
window override); hooks Pre/PostCompact/SessionStart(compact) payloads; `mock_anthropic.py` (native
SSE incl. thinking + signature, tool_use with `input_json_delta`, ping, mid-stream error, 429 with
retry-after, overflow 400 in Anthropic shape; Databricks anthropic paths); `ant:`/`dbx:claude`
request bodies field-by-field; cache_control placement on OpenRouter Claude; thinking display events;
cost meter per family; `/context` breakdown.

## Acceptance
Both suites + `test_tui.py` green on Windows and WSL. Live (home): force a compaction with a tiny
window (`CLAUDE_CODE_AUTO_COMPACT_WINDOW=20000`) on a multi-step Read/Grep task with the default
model → the session log shows `compacted`, the next request is smaller, the model continues
correctly; `/compact focus on file names` in the TUI; an OpenRouter Claude run
(`--model or:anthropic/claude-sonnet-4.5` or whatever `models.json` lists) with `--effort high
--verbose` shows thinking dimmed and cache fields in usage; `ant:` skipped unless an
`ANTHROPIC_API_KEY` exists (report); Read line count, memory question, proxy pong unchanged;
settings.json unchanged. Work box (VPN, later): `dbx:databricks-claude-*` passthrough tool call.
Report ≤ 60 lines. Rules as in the other briefs.
