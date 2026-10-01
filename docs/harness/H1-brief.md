# H1 brief — provider layer, session log, invariants, dsh-shaped loop (halo 0.3.x)

Repo: `~\Documents\vibes\appDev\halo\` (Windows 11, `python` 3.11, Git Bash).
Baseline (commit `4a4ca04`): `python test_bridge.py` → 97 green (proxy), `python tests/run_all.py`
→ 102 green (harness). Keep both green. Do not commit.

## Read first, in this order
1. `~/.claude/plans/typed-tickling-squirrel.md` → sections "Approach in one screen", "Research-driven
   revisions" (points 1–16 are binding for H1), "Decisions taken with rolo" (no safety heuristics),
   D2, D3, D4, D-CFG (paths/settings only).
2. `reports/DeepSeek and OpenRouter ori harnesses.md` → "Adopt", "Avoid", "Decide", "Wire formats
   the provider layer must speak", "Rules the provider layer must implement", "Per-family sampling
   and budget defaults" (the last three are the spec for `providers/`).
3. `docs/harness/claude-code-2.1.281-binary-facts.md` §14 (tool wording) and §1 (flags).
4. `docs/harness/review-findings-h0.md` if present (Opus review of H0 — fix its findings first).
5. The current package: `halo_harness/providers/{stream,translate,oai_stream,http,databricks,
   routing,config,errors,anthropic_sse}.py`, `halo_harness/agent/{loop,prompt,assemble,
   session_store}.py`, `halo_harness/{events,model,headless,output,cli}.py`, `tests/helpers/*`.

## Scope
A. **Provider compat profiles** (`providers/profiles.py` + data file `providers/model_table.json`):
   `ProviderProfile(system_vs_developer, max_tokens_field, reasoning_effort_supported,
   thinking_format: none|deepseek_reasoning_content|openrouter_details|anthropic_thinking|fmapi_blocks,
   reasoning_replay: text|empty|details|thinking, stream_usage, store, strict, tool_result_name,
   body_allowlist, tools_max (Databricks 32), supports_temperature_in_thinking, host_specific_fields)`
   resolved from the route (host + model family) with overrides from `model_table.json`
   (Aider-style per-model rows: edit_format, use_temperature, temperature/top_p defaults,
   max_tokens default/cap, reasoning defaults, openrouter_pin {order, allow_fallbacks,
   require_parameters, quantizations}, caches_by_default, notes). Seed rows for: OpenRouter
   `deepseek/deepseek-v3.2` (third-party only: pin `siliconflow, novita, gmicloud`, fp8, ≤65 536
   out), the DeepSeek V4 ids present in `~/.halo/models.json` (look them up; first-party
   `deepseek` endpoint), `moonshotai/kimi-k2*`/K3, `qwen/qwen3-*`, `z-ai/glm-*`, `google/gemini-3*`,
   `anthropic/claude-*`; Databricks `databricks-deepseek-v4-1-flash`, `-v4-pro-0813`,
   `-v4-flash-0731`, `databricks-kimi-k3`, `databricks-glm-5-3`, `-5-3-flash`, `-5-2`,
   `databricks-qwen35-122b-a10b`, `databricks-claude-*` (passthrough), `system.ai.*` twins.
B. **Request builder** (`providers/request.py`, replacing the ad-hoc parts of `anthropic_to_openai`
   usage in the loop): builds the OpenAI-dialect body from the derived transcript using the profile:
   strict `body_allowlist` (Databricks: messages, max_tokens, temperature, top_p, stop, stream, tools,
   tool_choice, reasoning_effort, stream_options; OpenRouter adds provider/reasoning/models/plugins/
   usage; OpenRouter fields only when host is `openrouter.ai`), `stream: true` always explicit,
   exactly one leading system message (later system content → user), `max_tokens` = min(profile cap,
   context − estimate − buffer) with Databricks pay-per-token default ≤16 384, no temperature/top_p/
   penalties to thinking endpoints, tools name-sorted and frozen per session (an `H1` stub of the
   catalog freeze: the tool list passed in is used verbatim; the 32-cap check raises a clear error).
   Reasoning replay per profile: DeepSeek/Kimi/Databricks → `reasoning_content` on every assistant
   message (`""` when absent) whenever `tools` are present; OpenRouter → `reasoning_details[]`
   verbatim, in order; Messages routes → thinking blocks + signature; never trim/reorder; ids never
   renumbered (keep provider ids verbatim in the log; `toolu_` minting only for display).
C. **Response decoding**: `oai_stream.py` gains both Databricks reasoning shapes (top-level
   `reasoning_content` deltas AND `{"type":"reasoning","summary":[…]}` content blocks), OpenRouter
   `reasoning_details` capture, `finish_reason` handling incl. `length` with ≤1 token → `ProviderFailure`
   (re-route/re-pin, never retry in place), `length`-truncated tool call → error result telling the model
   to split (distinct from malformed JSON → `invalid` pseudo-result with the schema error), strip leading
   `<think>…</think>` and `<｜end▁of▁sentence｜>` from displayed text (keep raw in the log), usage incl.
   `reasoning_tokens`, cached tokens, OpenRouter `cost`.
D. **Errors/retries** (`providers/errors.py` + loop): taxonomy `AUTH, RATE_LIMIT (Databricks 429 body
   with retry_after/limit_type; OpenRouter/DeepSeek 429), CONTEXT_WINDOW_EXCEEDED (all wordings incl.
   DeepSeek "requested N tokens (A in the messages, B in the completion)"), EMPTY_RESPONSE (retry once),
   STREAM_CLOSED, MALFORMED_RESPONSE, PROVIDER_FAILURE`; backoff ladder 1-2-4-8-16 s (+retry_after),
   max 5; DeepSeek 400 "reasoning_content … must be passed back" surfaces as a named error (it means
   a replay bug — never swallow).
E. **Session log as source of truth** (`agent/log.py`, `agent/derive.py`; replaces `session_store.py`):
   append-only JSONL `~/.halo/sessions/<slug>/<id>.jsonl` with node types `meta, system,
   user, assistant (content blocks incl. thinking/reasoning raw), tool_use, tool_result, snapshot
   (dynamic context: permission mode, CLAUDE.md chain, memory index, skills, notices), usage, error,
   interrupted, compacted (H5)`; `derive_request(log, profile) -> (system_text, messages, tools)`;
   runtime assertion "model-visible means logged" (hash of the derived request stored on each
   `assistant` node; a request whose bytes differ from the derived form fails loudly in tests).
F. **Serialize-time invariants** (`agent/invariants.py`): every tool_use id has exactly one
   tool_result before the next assistant node (synthetic `is_error` results `ABORTED_BEFORE_DISPATCH`
   / "Tool call interrupted by user" written on cancel/crash/interrupt); non-empty ids; valid JSON
   args; UTF-16 surrogates repaired on truncation; reasoning + tool_use + tool_result of a turn move
   as one unit.
G. **Loop** (`agent/loop.py`): dsh-shaped turn/step driver over the log; **loop breaker**
   (cumulative per-turn hash of tool name + canonical args: remind at 3, deny at 5, end turn at 8);
   `--max-turns`; interrupt semantics; minimal tool execution path with a **`Read` tool only**
   (`tools/base.py`, `tools/registry.py`, `tools/read.py` — cat -n numbering, offset/limit, absolute
   path, images later) so multi-turn tool loops are testable now; the rest of the tools are H2.
H. **System prompt** (`agent/prompt.py`): dsh-shaped ordered sections (identity → persona
   "coding agent powered by {model}" → plan-mode policy line → one guidance sentence per tool
   (Claude Code wording from binary-facts §14; only Read in H1) → MCP instructions placeholder →
   "Your working directory is {cwd}."), byte-stable; **no safety/refusal/"cyber" language**;
   CLAUDE.md chain, MEMORY.md index + topic list, skills/commands placeholders as USER-role
   snapshots (H0 folded memory into the system prompt — move it to a snapshot); nested CLAUDE.md
   snapshot on first Read under that dir.
I. **Catalog** (`model.py` + `providers/databricks.py`): `models.json` refresh stores
   `supported_parameters`, `architecture.input_modalities`, pricing, `top_provider` caps; Databricks
   `GET /api/2.0/serving-endpoints` cached to `~/.halo/dbx-endpoints.json`; `halo
   models` subcommand lists both; near-miss slug correction for `--model`.
J. **Defaults**: home default model = the DeepSeek V4 first-party id on OpenRouter (verify in
   `models.json`; fall back to `deepseek/deepseek-v3.2` with the pin); work defaults per plan point 12;
   `--effort` → profile mapping (`reasoning.effort` on OpenRouter, `reasoning_effort` on Databricks,
   thinking budget on Messages).

## Tests (append to `tests/`, exit-code gated; ≥ 60 new)
Mock upstream scenarios (extend `tests/helpers/mock_openai.py`, add `mock_databricks.py`):
OpenRouter `reasoning_details` round-trip (assert verbatim, ordered replay on the next request);
DeepSeek `reasoning_content` replay with `""` injection when tools are present and NO
`reasoning_content` when tools are absent; Databricks both reasoning shapes decoded; Databricks
unknown-field guard (the mock returns 400 `json: unknown field` if any non-allowlisted key appears);
32-tool cap error; 429 body with `retry_after` honoured; `length` with 1 token → reroute path;
`length`-truncated tool call vs malformed JSON; overflow wordings (DeepSeek/OpenAI/OpenRouter/
Databricks) → correct taxonomy; single-system normalisation; `max_tokens` budgeting table; no
temperature on thinking endpoints; loop breaker 3/5/8 with a scripted repeating tool call; pairing
invariants on interrupt/crash (synthetic results present, next request valid); session log derive →
byte-identical request across two turns; Read tool end-to-end (`-p "read <file> and reply with the
number of lines"`); profile resolution for every seeded model_table row; models.json field capture.

## Acceptance (paste verbatim, trimmed)
1. `python test_bridge.py 2>&1 | tail -2; echo exit=$?` → 97 green. `python tests/run_all.py 2>&1 |
   tail -2; echo exit=$?` → ≥ 162 green.
2. Live (home): `halo -p "read ~\Documents\vibes\appDev\halo\README.md and
   reply with only the number of lines" --model <default V4 id>` → the correct count (compare
   `wc -l`); the same with `--model or:deepseek/deepseek-v3.2` (pinned providers) → same count; the
   memory question from H0 still answers; `--output-format json` shape unchanged; `halo
   models | head` lists OpenRouter models with context/out/price; `halo proxy launch --model
   or:deepseek/deepseek-v3.2 -- -p "reply with the single word pong"` → pong; `proxy --stop`.
3. Reasoning replay live: `halo -p "read <file> then tell me its first heading" --model <a
   thinking-capable V4 id> --effort high --verbose` → two model calls, no 400, thinking shown dimmed
   in verbose output; the session JSONL contains the raw reasoning on the assistant node.
4. `grep -o '"model": "[^"]*"' ~/.claude/settings.json` unchanged.

## Rules
≤ 250 lines per edit/write call; Write tool or Python-by-path for multi-line content (Bash heredocs
corrupt backslashes here); keep tool output small; bulk new modules may be drafted with
`tools/or_draft.py` (DeepSeek, cap $2) from specs under `wip/harness/spec-H1-*.md`; hand-write what
truncates; never weaken existing tests; no commits; report ≤ 60 lines with verbatim acceptance,
deviations, spend.
