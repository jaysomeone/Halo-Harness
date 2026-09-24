# Milestones 4–6 brief (Databricks live route, Claude passthrough, overflow/compaction, reasoning log, probe)

Repo: `C:\Users\user\Documents\vibes\appDev\claude-bridge\` — `bridge.py` (foundation M0–3 done by Worker A2),
`test_bridge.py` (46 black-box tests, keep green), `docs/design-review-2026-09-23.md` (items 6, 7, 12, 13 are yours).
Home box has NO Databricks access; everything Databricks is verified hermetically here (mock upstream) and live
at work on the VPN (Linux) via `--probe`.

## M4 — Databricks openai-chat route (review item 12)
- Discovery (`resolve_databricks`): override `BRIDGE_DBX_BASE_URL`/`BRIDGE_DBX_TOKEN` → process
  `ANTHROPIC_BASE_URL`+`ANTHROPIC_AUTH_TOKEN` only if host matches `*.databricks.com` / `*.azuredatabricks.net`
  / `*.gcp.databricks.com` (never loopback) → settings `env` chain (managed › project local › project › user) →
  `DATABRICKS_HOST`+`DATABRICKS_TOKEN` → `~/.databrickscfg` `[DEFAULT]` host/token.
  Workspace root = base URL with `/ai-gateway/anthropic[/v1[/messages]]` stripped. Mirror `ANTHROPIC_CUSTOM_HEADERS`
  (settings chain or env; e.g. `x-databricks-use-coding-agent-mode: true`) on every Databricks request, after
  removing the bridge's own `x-bridge-*` lines.
- Routes: `dbx:databricks-X` / bare `databricks-X` → `POST <root>/serving-endpoints/X/invocations` with NO
  `model` field; `dbx:system.ai.X` / bare `system.ai.X` → `POST <root>/ai-gateway/mlflow/v1/chat/completions`
  WITH `model`. On 404 try the other route once; cache the working route per model in memory + `<state>/routes-cache.json`.
- Body allowlist: messages, max_tokens, temperature, top_p, stop, stream, tools, tool_choice (no `provider`,
  no `usage`, no `reasoning`). Response `content` may be a list of `{"type":"text"}` and
  `{"type":"reasoning","summary":[{"type":"summary_text","text":…}]}` parts — flatten text parts, log reasoning.
- 400 naming a max_tokens limit (regex the number) → clamp, retry once, cache the limit per model.
- Connect/DNS failure → 502 `api_error`, `x-should-retry: false`, message ends with
  "(are you on the VPN? Databricks is whitelisted)".
- `--probe`: `GET <root>/api/2.0/serving-endpoints` (list names; 401/403 → "token can run inference but not
  list endpoints — pass names explicitly"), plus `GET https://openrouter.ai/api/v1/models` → cache
  `id`, `context_length`, `top_provider.max_completion_tokens` into `<state>/models.json` (launcher reads it for
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS` / `CLAUDE_CODE_MAX_OUTPUT_TOKENS`). Report a busy port plainly.

## M5 — Databricks Claude passthrough (review §3 last block + item 1 passthrough note)
- Names containing `claude` under `dbx:` → raw streaming relay to `<root>/ai-gateway/anthropic/v1/messages`
  (`read1(65536)` loop, never `read(n)`), same `?beta=true` query, status + headers relayed, `anthropic-version`
  and `anthropic-beta` forwarded but drop any `tool-search-tool-*` beta; body: rewrite `model` from `dbx:X` to
  `X`, resolve tier/`claude-*` aliases the same way, apply the same tool-cap/tool_reference handling, drop
  thinking blocks whose signature starts with `bridge1.`; headers: drop `x-api-key`, bridge `authorization`,
  `host`, `content-length`, `accept-encoding`; add `Authorization: Bearer <gateway token>` + custom headers.
  `count_tokens` for passthrough refs → relay to `<root>/ai-gateway/anthropic/v1/messages/count_tokens`, falling
  back to the estimate on any non-2xx.
- Launcher: for passthrough refs do NOT set `MAX_THINKING_TOKENS=0`, and set `ENABLE_TOOL_SEARCH` only if the
  settings chain already had it.

## M6 — overflow/compaction + errors (review items 6, 7, 15) and reasoning logging (item 13)
- `parse_context_overflow(status, body)` → (L, A, B, T) from: OpenAI/vLLM "maximum context length is L …
  (A in the messages, B in the completion)"; OpenRouter "requested about T"; `error.metadata.raw`; Databricks
  "input length and max_tokens exceed context limit" variants. If A ≤ L → retry once with
  `max_tokens = L − A − 256` (silent, before any block was sent). Else 400 `invalid_request_error` with message
  exactly `prompt is too long: T tokens > L maximum` where T > L is guaranteed (T = max(T, L+1)).
- Up-front clamp: `max_tokens ≤ min(profile_max_output, context − 1.1×estimate − 512)` with profile from
  `<state>/models.json` when present else routes defaults.
- Mid-stream upstream death → `event: error` `{"type":"error","error":{"type":"overloaded_error",…}}`, close, no
  `message_stop`. Upstream error chunk `{"error":…}` inside SSE → same.
- Reasoning: `reasoning` / `reasoning_content` deltas and Databricks reasoning parts are appended to the dump
  and logged at DEBUG with lengths; never emitted to Claude Code in this phase.
- Tests to add (own section at the end of `test_bridge.py`, same style, keep exit-code gating): Databricks mock
  upstream (both routes, 404 fallback + cache, no-`model` body on invocations, allowlist, reasoning parts, custom
  header mirroring, max_tokens 400-retry, DNS-failure hint), passthrough mock (byte relay, header swap,
  `bridge1.` block drop, beta filter, model rewrite), overflow table incl. the A ≤ L retry, `--probe` against
  mocks (`BRIDGE_OPENROUTER_BASE_URL` + `BRIDGE_DBX_BASE_URL`), models.json → launcher env values.

## Acceptance at home (hermetic) — `python test_bridge.py; echo exit=$?` → 0; `python bridge.py --config` with
`BRIDGE_DBX_BASE_URL=https://example.cloud.databricks.com/ai-gateway/anthropic BRIDGE_DBX_TOKEN=x` shows the
derived root + both routes; `python bridge.py --probe` reports Databricks unreachable with the VPN hint and lists
OpenRouter models into `<state>/models.json`.
## Acceptance at work (VPN, Linux) — `bridge.py --probe` lists endpoints; `bin/claude-bridge --model
dbx:databricks-kimi-k3 -- -p "reply with the single word pong"` → pong; `--model dbx:databricks-claude-<name>`
behaves like plain work `claude`; `bridge.log` shows the mirrored `x-databricks-use-coding-agent-mode` header.
