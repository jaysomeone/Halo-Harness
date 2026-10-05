# OpenAI API research (round 5i part 1, 2026-10-04)

Fetched via WebFetch; `platform.openai.com/docs/*` 403s directly and
`platform.openai.com/docs/guides/*` 301-redirects to
`developers.openai.com/api/docs/guides/*`, which serves content -- every
URL below is the one that actually returned a body, fetched 2026-10-04.
Separately, `models.dev/api.json` (fetched live via `providers.models_dev.
fetch_models_dev`, not WebFetch) already carries a real `openai` provider
entry (53 ids) -- see "models.dev cross-check" below.

## 1. `POST /v1/responses` -- request shape
Source: `developers.openai.com/api/docs/api-reference/responses/create`.

- `input`: a plain string, a single item, or an array of items.
- Message item: `role` is `user|assistant|system|developer`; `content` is
  a string or a list of text/image/file parts; `type: "message"` optional.
- `function_call` item: `type`, `call_id`, `name`, `arguments` (a JSON
  **string**, not an object), optional `status`.
- `function_call_output` item: `type`, `call_id` (links to the call),
  `output` (string or content list), optional `status`.
- `tools` function item: `{"type":"function","name","description",
  "parameters","strict"}` -- flat, NOT nested under a `"function"` key the
  way chat-completions tools are.
- `reasoning`: `{"effort": ..., "summary": ...}`. `instructions`,
  `tool_choice`, `store`, `previous_response_id`, `stream`,
  `max_output_tokens`, `parallel_tool_calls` all confirmed present.
- UNCONFIRMED: `store`'s default value; exact `summary` enum values;
  whether `text`/`text.format`/`truncation` exist (not seen either way).
- Response object: `id`, `object`, `status`, `output` (array), `usage`
  (`input_tokens`, `output_tokens`, `output_tokens_details.
  reasoning_tokens`, `input_tokens_details.cached_tokens` -- field names
  confirmed, no example numeric body seen).

## 2. Streaming events
Sources: `.../guides/streaming-responses`, `.../guides/function-calling`
(the dedicated streaming API-reference page kept resolving to the "Cancel
a response" page -- a routing quirk; event names came from the guide's
own union type instead).

Confirmed event `type` strings: `response.created`, `response.
in_progress`, `response.output_item.added`, `response.output_item.done`,
`response.content_part.added`, `response.content_part.done`, `response.
output_text.delta`, `response.output_text.annotation_added`, `response.
text.done`, `response.refusal.delta`, `response.refusal.done`, `response.
function_call_arguments.delta`, `response.function_call_arguments.done`,
file-search/code-interpreter events, `response.completed`, `response.
failed`, plain `error`.
- UNCONFIRMED: `response.output_text.done` as a distinct name (only
  `response.text.done` appeared in the union -- treated as the real
  terminal name for a text part below); `response.incomplete` as a
  separate event (may just be `response.completed` with `status:
  "incomplete"`).

Exact payloads quoted from the function-calling guide (confirmed, not
inferred):
```
{"type":"response.output_item.added","response_id":"resp_...","output_index":0,
 "item":{"type":"function_call","id":"fc_...","call_id":"call_...","name":"get_weather","arguments":""}}
{"type":"response.function_call_arguments.delta","response_id":"resp_...",
 "item_id":"fc_...","output_index":0,"delta":"{\"location\":\"Paris"}
{"type":"response.function_call_arguments.done","response_id":"resp_...",
 "output_index":0,"item_id":"fc_...","arguments":"{\"location\":\"Paris, France\"}"}
```
Deltas key off `item_id`; the done event carries the full string;
`call_id` lives on the item, not the delta/done envelope.
- `response.output_text.delta`'s own fields were not independently
  fetched with an example -- UNCONFIRMED, inferred by analogy to the
  function-call shape above (`item_id`, `output_index`, `content_index`,
  `delta`); the decoder reads whichever of `item_id`/`output_index` is
  present and tolerates either being absent.
- Reasoning item, confirmed with an exact example
  (`.../guides/reasoning`): `{"id":"rs_...","type":"reasoning","summary":
  [{"type":"summary_text","text":"..."}]}`. UNCONFIRMED: no streaming
  delta event for reasoning-summary text appears anywhere in the fetched
  guide content -- only the final item on `response.output_item.done`.
  The decoder therefore never streams reasoning incrementally; it reads
  the summary text once, on that item's done/added event.
- UNCONFIRMED: whether `response.completed` inlines the full `response`
  object (id/status/output/usage) was not independently re-fetched this
  round; treated as true by analogy with the same object's documented
  shape elsewhere (REST create/cancel responses). The decoder reads
  `usage` from `response.completed.response.usage` when present, and
  falls back to a rough estimate otherwise -- never raises if absent.

## 3. `GET /v1/models`
Source: `.../api-reference/models/list`.
- `{"object":"list","data":[{"id","object":"model","created","owned_by",
  "shutdown_date"?}]}`. Confirmed ABSENT: no context length, no pricing,
  on this endpoint, in either example on the page -- pricing/context must
  come from models.dev, same as every other router in this harness.

## 4. Error shapes
Source: `.../guides/error-codes`.
- Envelope: `error.message`, `error.type`, `error.param`, `error.code`.
- 401: `type: authentication_error`, `code: invalid_api_key`.
- 429: either plain rate limiting (`code: rate_limit_error` or
  `slow_down`) or quota exhaustion (`type: insufficient_quota`, with
  `code` one of `organization_spend_limit_exceeded`, `project_spend_limit_
  exceeded`, `credit_balance_exhausted`) -- distinguished by `error.type`,
  not by status code alone.
- 404 model-not-found: UNCONFIRMED -- not covered by the fetched page at
  all. Halo's existing generic `map_upstream_error` 404 row (`not_found_
  error`) applies regardless; the plain `error.message` text (whatever it
  says) still reaches the user either way.
- No change needed to `providers/errors.py`: this envelope shape is
  exactly what `upstream_error_text`/`map_upstream_error` already parse
  (it's the shape they were designed for); pinned with new tests instead
  of new code.

## 5. Reasoning-with-tools and the effort enum
Source: `.../guides/reasoning`.
- Confirmed requirement, quoted: "Use the Responses API for function
  calling. Chat Completions does not support function calling with GPT-6
  Astra or GPT-6.1 Sol." -- exactly these two named models; no other
  family is named on this page as *requiring* Responses.
- Confirmed effort enum (all 7 on the page): `none, minimal, low, medium,
  high, xhigh, max` -- real, current, documented values, not a harness
  invention. `none` is latency-critical/no reasoning; `xhigh` is deep
  research/long agentic runs; `max` is hardest tasks. GPT-6 Astra is
  called out as NOT supporting `none`.

## models.dev cross-check (live fetch, 2026-10-04)
`fetch_models_dev()["openai"]["models"]` has 53 ids, including
`gpt-6-astra` and `gpt-6.1-sol` (confirmed matches for the two names
above), plus `gpt-6-sol`, `gpt-6-luna`, `gpt-6-terra`, `gpt-5.6-sol/luna/
terra`, `gpt-5.4`, `gpt-5.4-mini/nano/pro`, `o1`, `o3`, `o4-mini`, etc.
Both `gpt-6-astra`/`gpt-6.1-sol` rows carry `reasoning_options: [{"type":
"effort","values":["low","medium","high","xhigh","max"]}]` -- a
*narrower* 5-value list than the reasoning guide's 7 (no `none`/
`minimal`); the guide's wider enum is treated as authoritative for what
the wire accepts, since it is the API reference, not a metadata
aggregator's summary. `GET /v1/models` on the real API gives no pricing,
so this harness's vendored fallback
(`providers/catalog/models_dev_openai_fallback.json`, all 53 ids, same
trim as the Databricks fallback: id/name/family/cost/limit/modalities/
reasoning/reasoning_options/tool_call/temperature) is the price/context
source, refreshed the same way the Databricks one is
(`halo models --refresh` writes `~/.halo/models-dev.json`, read first).

## Dialect-selection table this round uses
Exactly the two confirmed-required ids, matched by exact bare id (never a
substring, since `gpt-6-sol`/`gpt-6-luna` are real, DIFFERENT ids the
guide does not name): `{"gpt-6-astra", "gpt-6.1-sol"}` default to the
`openai-responses` dialect; every other `oai:` model defaults to
`openai-chat`. `openai.dialect_overrides` (`~/.halo/config.json`, `{"<bare
id>": "chat"|"responses"}`) always wins over the table in either
direction, since the guide itself may be incomplete (it only ever
documents a requirement, never a complete negative list) and a future
model could need the same treatment with no code change.

## What the brief assumed that this research corrects
- The brief's own item 1 doesn't name exact models; the 2.0.3-brief.md
  "F" section's `gpt-6`/`gpt-5-6` substring guess (from the unrelated
  Databricks `reasoning_effort_with_tools` rule) is NOT the same set the
  Responses guide actually names -- `gpt-6-sol`/`gpt-6-luna`/`gpt-6-terra`/
  `gpt-5.6-*` are real, separate ids from `gpt-6-astra`/`gpt-6.1-sol`, so a
  substring match on `"gpt-6"` would have wrongly swept in every `gpt-6-*`
  id. The table above uses exact ids instead, documented above.
- The reasoning-effort vocabulary (`none/minimal/low/medium/high/xhigh/
  max`) is REAL and current on this API, not a harness-only vocabulary --
  confirmed, so `effort_values_supported` for the Responses profile is
  exactly this 7-tuple (a strict superset of the harness's own
  `EFFORT_LEVELS`), needing no clamping for any value the harness's own
  `--effort`/`/effort` can already send.
- `reasoning.encrypted_content`/`include` (replaying reasoning across
  requests when `store:false`) was NOT investigated this round -- out of
  scope per the brief's own "never previous_response_id" framing; this
  harness's Responses dialect carries reasoning as display-only (like the
  `ollama` dialect's `reasoning_replay="empty"`), never replayed on the
  wire. Flag this as the one deliberate scope cut, not an oversight.
