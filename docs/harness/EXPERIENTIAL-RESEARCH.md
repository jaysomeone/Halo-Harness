# Experiential Labs research (round 5g)

Written for round 5h, the implementer of the `xp:` route (`plans/2.0.3-ollama-round2-brief.md`,
"Round 5h"). Method: WebFetch only, no API key, no WebSearch. Every claim below cites the page
it came from and the date read (all pages were read 2026-10-04). Citations to
`platform.experientiallabs.ai` are abbreviated to their path, e.g. `/docs/waterfall` means
`https://platform.experientiallabs.ai/docs/waterfall`; GitHub citations spell out the full path.
Anything that could not be confirmed from a primary source is marked **UNCONFIRMED** in bold at
the point it comes up, and all of them are collected again in section 13. WebFetch summarizes
through a small model before the text reaches this doc, so treat quoted strings as "as read by
that pass," not a byte-for-byte guarantee -- where it mattered for round 5h's wire format I
re-fetched with a narrower prompt to get a second pass on the same page.

This is a docs-only research round. It does not touch code, tests, or any other file in this
repo.

## 1. Summary: what the `xp:` route must do differently from Halo's `or:` (OpenRouter) route

Experiential Labs is "an OpenAI-compatible model gateway: one base URL in front of every model:
hosted providers, your own provider keys, our platform-funded credits, and self-hosted or custom
models" (`/docs`, 2026-10-04). That description sounds like OpenRouter, and the wire shape for a
plain chat-completions call is close enough that Halo's `openai` dialect code is the right base to
build on -- but the two gateways diverge in ways that will break a route built by just pointing
the OpenRouter profile at a new base URL, which is exactly what Halo already tried and got a 400
for (`ROADMAP.md`, "ADDED 2026-10-04: Experiential Labs," confirmed again below). Four things are
different at the request level: (1) Experiential's own dialect has no `usage.include`, no
`provider` preference object, and no `transforms` array -- those are OpenRouter-only fields and
Experiential's schema simply doesn't define them, so sending any of them is a foreign field, not a
disabled one; (2) Experiential refuses a fixed list of OpenAI fields outright (`audio`,
`modalities`, `logit_bias`, `seed`, `functions`/`function_call`, `prediction`,
`prompt_cache_retention`, and a top-level `route` object) with a 400, while silently dropping
other unsupported fields and disclosing exactly which ones through a response header,
`x-experiential-ignored-parameters` (`/docs/openai-compatibility`, 2026-10-04); (3) routing and
retry are their own nested JSON objects, `gateway.routing` and `gateway.retry`, with a documented
rung model ("the waterfall") underneath every slug, not a single opaque provider choice
(`/docs/waterfall`, 2026-10-04); (4) cost does not reliably land where the docs say it lands --
`/docs/data-controls` (2026-10-04) states non-streaming bodies carry a top-level `cost` field
alongside `provider` and `is_byok`, but the owner's own live call on 2026-10-04 (`ROADMAP.md`)
got `cost` only inside `usage.cost`. Round 5h's plan to read `usage.cost` and fall back is
correct; this doc confirms the docs/live mismatch is real on the gateway's side, not a
misreading.

Beyond the wire format, three account-shaped differences matter for how Halo stores and spends
the key. First, there are two kinds of key: an inference key (`EXPLABS_API_KEY`, prefix `xpl_`)
that calls models and reads `/api/v1/credits` and `/api/v1/usage`, and a separate provisioning
key (`EXPLABS_PROVISIONING_KEY`) required for key management and the full Spend API -- an
inference key gets a 403 on those (`/docs/integrate`, `/docs/spend-api`, 2026-10-04). Round 5h's
brief only names `EXPLABS_API_KEY`; it should stay scoped to what that key can do (credits,
settled usage rows, single-generation lookups) and not assume the richer Spend dashboard's API is
reachable without asking the user for a second key, which nothing in the brief currently plans
for. Second, the gateway publishes two base-URL families: `/v1` (bare, OpenAI/Anthropic SDK
shape, used for inference) and `/api/v1` (account, cost, catalog management)
(`/docs/integrate`, 2026-10-04) -- `BRIDGE_EXPERIENTIAL_BASE_URL` needs to override (or at least
clearly document which of) both roots, not one. Third, the waterfall means a model slug is not a
fixed backend: the same slug can be served by a platform-credit rung on one call and a BYOK or
local rung on the next, and `allow_fallbacks`/`route_id` change that behavior per request, so
Halo's cost meter and escalation policy need to read `provider` and `is_byok` off every response
rather than assuming a slug implies a fixed cost and a fixed capability set.

## 2. Authentication and key handling

- Base URL for inference: `https://api.experientiallabs.ai/v1` (`/docs`, `/docs/quickstart`,
  2026-10-04). Account/cost/catalog-management base: `https://api.experientiallabs.ai/api/v1`
  (`/docs/integrate`, `/docs/cost-api`, `/docs/account-api`, 2026-10-04). `/docs/integrate`
  states both `/api/v1` and bare `/v1` serve the same inference routes ("the `/api/v1` routes keep
  the same path shapes") -- **UNCONFIRMED** whether every inference call truly works on both
  roots interchangeably or only specific ones do; Halo should pick one (the brief already assumes
  `/v1` for chat/responses/messages) and not rely on the other without a live check.
- Primary auth header: `Authorization: Bearer <key>`. On `/v1/messages` specifically, `x-api-key:
  <key>` is also accepted, matching Anthropic SDK behavior (`/docs/authentication`,
  `/docs/anthropic`, 2026-10-04).
- Key format: `xpl_` prefix followed by 40 lowercase hex characters (`/docs/authentication`,
  2026-10-04).
- Two key kinds, not one: an **inference key** (what `EXPLABS_API_KEY` should hold) calls models
  and reads `/api/v1/credits`, `/api/v1/usage`, `/api/v1/key`, `/api/whoami`; a **provisioning
  key** (`EXPLABS_PROVISIONING_KEY`) is required for `/api/v1/keys` management, org budgets, org
  identities, and the entire Spend API -- "inference keys receive 403" there
  (`/docs/integrate`, `/docs/account-api`, `/docs/spend-api`, 2026-10-04). Round 5h's scope
  (balance chip, settled-usage view, per-model catalog) only needs the inference key.
- Org scoping is automatic and implicit: "keys belong to exactly one organization; tenancy
  automatically scopes each API call to the key's organization" -- there is no separate
  org-selection header (`/docs/authentication`, 2026-10-04). This matches the brief's plan of one
  key, one `/providers` entry; there is no multi-org switch to wire.
- Attribution label: the brief's `safety_identifier` plan is confirmed -- `/docs/integrate`
  (2026-10-04) says to "pass `safety_identifier` (or `user`) for customer tracking across billing
  exports," i.e. either field name is accepted, `safety_identifier` preferred. `/docs/spend-api`
  (2026-10-04) separately lists `end_user` as a Spend-API-side attribution dimension, and
  `/docs/embeddings` (2026-10-04) documents a `user` field (max 1,024 chars) on embeddings
  requests specifically -- same idea, different endpoint, worth sending consistently.
- Data controls (org-level, not per-key): `capture_prompt_content` (prompt/response storage,
  default on for Free, configurable on Pro), `require_zdr` org-wide or `provider: {"zdr": true}`
  per request (Pro), `require_no_training` (all plans), `allowed_processing_regions` (all plans),
  `allowed_providers` (Enterprise) (`/docs/data-controls`, 2026-10-04). These are organization
  dashboard settings, not fields Halo's request builder sets per turn, except the per-request `zdr`
  override. Response headers `x-gateway-provider`, `x-gateway-zdr`, and `x-gateway-route-depth`
  (0 = first rung) let a caller verify which rung and data posture actually served a given call
  (`/docs/data-controls`, 2026-10-04) -- worth surfacing in a verbose/debug view rather than the
  normal transcript line.
- Keys are created and revoked **only** through the web app (Settings > API Keys); the secret is
  shown once at creation; reads of `/api/v1/keys` never return secret material, only the last four
  characters (`/docs/authentication`, `/docs/account-api`, 2026-10-04). There is no "mint a key by
  API call" path for Halo's init wizard to automate -- the wizard can only ask the user to paste
  one they already created in the browser, same as the OpenRouter/Hugging Face tabs today.

## 3. Request grammar

### 3.1 Chat Completions (`POST /v1/chat/completions`)

Standard OpenAI shape (`model`, `messages`, `stream`, `tools`, etc.) plus a `gateway` object (see
3.4) and the data-control overrides from section 2. Minimal example from `/docs/quickstart`
(2026-10-04):

```json
{"model": "qwen3.8-27b", "messages": [{"role": "user", "content": "Hello"}]}
```

**Refused outright (400)** -- confirmed on two separate passes over `/docs/openai-compatibility`
(2026-10-04), same list both times: `audio`, `modalities`, `logit_bias`, `seed`,
`functions`/`function_call`, `prediction`, `prompt_cache_retention`, and a top-level `route`
object. Error `code` is `unsupported_parameter` or `invalid_parameter` (cross-checked against the
error table in section 7). Unknown message-level keys (e.g. a stray `messages[].agent`) are
rejected "identically to OpenAI's rejection" (same page). Note what is conspicuously **not** on
this refused list: OpenRouter's `usage.include`, `provider` object, and `transforms` are not
refused-with-a-named-rule at all -- they are simply undefined in Experiential's schema, so sending
them lands as an unrecognized/invalid parameter instead of hitting a documented named rule. That
is consistent with the 400 Halo already observed through its `or:`-profile experiment
(`ROADMAP.md`, 2026-10-04: `"The parameter 'usage' is not supported by this gateway profile.
Remove the field and resend the request."`) without needing a special case in the error table.

**Dropped silently, disclosed via header** -- `x-experiential-ignored-parameters` is a response
header carrying an array of `field_name->dropped(reason)` entries for fields the gateway accepted
syntactically but the serving rung could not honor, e.g. `top_p->dropped(...)` on a
non-sampling-tunable provider or `text.verbosity->dropped(...)` on a non-OpenAI rung
(`/docs/openai-compatibility`, 2026-10-04, confirmed on both passes). On a streamed response this
same disclosure "rides in the chunk carrying the finish reason" rather than in a header (same
page) -- Halo's streaming decoder needs a second place to look for it. A related header,
`x-gateway-replay-repair`, is disclosed specifically when the gateway had to repair encrypted
reasoning content across a provider failover (same page) -- round 5h's learned-rule mechanism
should treat this as a distinct signal from the ignored-parameters list, not fold it in.

**Structured output never silently degrades.** If no rung on a model's route can honor
`response_format`, the gateway refuses with `400`, `code: unsupported_capability`, `param:
response_format` -- "the gateway never downgrades a schema request to prose silently"
(`/docs/openai-compatibility`, 2026-10-04). This is the one capability error round 5h's plain-
sentence table most needs to get right, since the brief's whole plan for `supports_structured_
output` hinges on knowing in advance whether a model can take the hit before sending the request.

### 3.2 `/v1/responses`

Confirmed to exist and to be the dialect OpenAI Codex uses by setting `wire_api = "responses"`
(`/docs/coding-agents`, 2026-10-04). The quickstart page mentions `previous_response_id` for
continuations (`/docs/quickstart`, 2026-10-04). **UNCONFIRMED**: no page reached gave the full
Responses-specific parameter/refusal list the way `/docs/openai-compatibility` did for Chat
Completions; round 5h should assume the same refused/dropped mechanics (same gateway, same
`x-experiential-ignored-parameters` header) apply, but verify against a live response rather than
assuming the list is byte-identical to Chat Completions'.

### 3.3 `/v1/messages` (Anthropic)

Full detail in section covering the Anthropic dialect below (this section continues the grammar
point): accepts **any** catalog slug, not only Claude models -- "name any slug from `GET
/v1/models` as the model" (`/docs/anthropic`, 2026-10-04). `stream: true` gives Anthropic's native
SSE shape (`message_start`, `content_block_delta`, `message_stop`, etc.) unchanged
(`/docs/anthropic`, 2026-10-04). `Idempotency-Key` is **not** honored on this endpoint, nor is
`gateway.retry`/`gateway.routing` valid "on token counting or batch operations"
(`/docs/anthropic`, 2026-10-04) -- implying they *are* valid on ordinary `/v1/messages` generation
calls, just not on `/v1/messages/count_tokens` or any batch endpoint. `/v1/messages/count_tokens`
returns "the gateway's token estimate rather than the provider's count" (same page) -- Halo should
not treat that number as authoritative for a Claude-specific budget calculation.

Thinking/extended-reasoning translation (important, and a correction to one of round 5h's own
assumptions -- see section 1 and section 12): **only** while the request is actually served by an
Anthropic-shaped rung does `thinking` pass through unchanged with native thinking/redacted-
thinking blocks. If the waterfall fails over to a non-Anthropic reasoning-capable rung, `thinking`
is translated to that route's own reasoning-effort tier, disclosed as
`thinking->reasoning_effort:<tier>` (same mechanism/header family as the ignored-parameters
disclosure); on a non-reasoning rung, `thinking` is dropped entirely, also disclosed
(`/docs/anthropic`, 2026-10-04). Round 5h's plan says "Claude slugs go through Halo's existing
Anthropic passthrough... so thinking stays native" -- that is only true as long as the serving
rung stays Anthropic. A fallback inside the waterfall (BYOK outage, platform rung unhealthy) can
silently change that, and Halo's transcript should show the disclosed translation/drop rather than
assume native thinking just because the request went to `/v1/messages`.

Other Anthropic-path notes: PDF/document blocks need a document-capable route; image blocks
support "up to 100 per request on capable routes"; tool-error results get translated across
non-Anthropic providers (`/docs/anthropic`, 2026-10-04). Claude Code connects with
`ANTHROPIC_BASE_URL` pointed at the gateway and `ANTHROPIC_API_KEY` set to an `xpl_` key -- see
section 11 for the exact env var shape, which has a surprising detail (no `/v1` suffix on the
base URL for Claude Code specifically).

### 3.4 `gateway.routing` and `gateway.retry`

Both nest under a top-level `gateway` object on Chat Completions, Responses, and ordinary
`/v1/messages` generation calls (not on `/v1/messages/count_tokens` or batch endpoints)
(`/docs/waterfall`, `/docs/anthropic`, 2026-10-04).

- `gateway.routing.route_id` -- an opaque handle naming one specific rung, obtained from `GET
  /api/models/<slug>/providers` (`/docs/waterfall`, confirmed again on `/docs/models`,
  2026-10-04: "its deployments are `GET /api/models/<slug>/providers`"). Round 5h's plan for
  `/xp routes <slug>` to print this list is confirmed as the right call to make.
- `gateway.routing.allow_fallbacks` -- boolean. `true` (the implicit default when the field is
  omitted) lets the waterfall fall through past the selected/first rung; `false` pins the request
  to the selected route only (`/docs/waterfall`, 2026-10-04).
- `gateway.retry.max_attempts_per_route` -- integer 1 to 4, inclusive of the first dispatch.
- `gateway.retry.max_total_attempts` -- integer 1 to 8, counted across every rung tried.
- `gateway.retry.backoff` -- `{"type": "none"}` or `{"type": "exponential", "base_delay_ms": ...,
  "max_delay_ms": ..., "multiplier": ...}`. "Omitting them keeps the existing operator policy;
  there is no default backoff mode" (`/docs/waterfall`, 2026-10-04) -- i.e. leaving `gateway.retry`
  out entirely is a real, documented choice, not an oversight, and round 5h's plan to set
  `max_attempts_per_route: 1` by default to avoid double-retrying under Halo's own outer retry
  loop is directly supported by the docs' own framing of retries-within-a-rung and
  fallbacks-across-rungs as distinct, separately-counted attempts.
- A related, separately-documented header is not part of `gateway.retry` but interacts with it:
  **`Idempotency-Key`** is honored on Chat Completions and Responses (not on `/v1/messages`, not
  on token counting, not on batch) (`/docs/errors` codes `idempotency_conflict` /
  `idempotency_replay_unavailable`, `/docs/anthropic`, 2026-10-04). Recommendation for round 5h
  beyond what the brief asked for: have Halo's own outer retry loop send a stable
  `Idempotency-Key` per logical turn on the two endpoints that support it, so Halo's retries and
  the gateway's own `gateway.retry` attempts cannot double-bill the same logical request -- this
  is a safer mechanism than only tuning `max_attempts_per_route` down to 1, and the brief did not
  mention it.
- Waterfall-level behavioral rules that affect what `allow_fallbacks: false` actually buys you:
  "at least one rung must remain enabled; the last enabled rung cannot be toggled off until
  another is activated," and "if every on rung is a BYOK rung and none can serve, the request
  fails closed" -- an unhealthy BYOK connection is dropped rather than silently rerouted onto
  platform credits (`/docs/waterfall`, 2026-10-04). A pinned local-first role (5e's escalation
  policy) that sets `allow_fallbacks: false` against a BYOK-only or local-only route needs to
  surface that fail-closed outcome as a plain sentence, not a generic error.

### 3.5 Tool search (`defer_loading`)

This is a specific wire convention, not a boolean flag on the request as the brief's shorthand
might suggest. Declare a sentinel tool-search tool alongside the ordinary function tools, and mark
individual function tools the model rarely needs with `defer_loading: true`:

```json
"tools": [
  {"type": "openrouter:tool_search"},
  {"type": "function", "defer_loading": true, "function": {...}}
]
```

(`/docs/openai-compatibility`, 2026-10-04, confirmed on a targeted second pass). Note the literal
type string is `"openrouter:tool_search"` even on Experiential's own, independently-run gateway --
this looks like a borrowed/shared convention with OpenRouter's own tool-search feature rather than
an Experiential-original name; the pages reached do not explain the shared naming, so treat it as
**UNCONFIRMED** *why* it is named that way, but confirmed that this is the literal string to send.
"A request gets up to 3 search rounds by default" (same page). If no tool in the array is marked
`defer_loading: true`, the whole `tool_search` declaration is pointless and gets dropped, disclosed
as `tool_search->dropped(no_deferred_tools)` in `x-experiential-ignored-parameters` (same page).

This is directly relevant to the lean-prompt problem Round 3's hand-off and Round 6 flag (a 27B
local model fitted to a 16k context pays about 6.7k prompt tokens just for 24 built-in tool
definitions): on `xp:` routes specifically, Halo could mark its rarely-used built-in tools
`defer_loading: true` and let the gateway's own tool search keep the hot path's prompt small,
rather than (or in addition to) round 6's own lean-mode tool-catalog trimming. This is an
opportunity worth flagging to round 6, not something round 5h needs to ship.

## 4. The catalog

- **`GET /v1/models`** (bare, OpenAI-shaped) and **`GET /api/models`** (richer; "returns the
  public rows" and, when called with an authenticated org key, adds that organization's custom and
  local models on top) both exist and both list the catalog, but the pages reached never
  reconcile them side by side field-for-field (`/docs/core-loop`, `/docs/models`, 2026-10-04) --
  **UNCONFIRMED** whether they return identical per-model JSON shapes or `/api/models` is a
  superset in fields as well as rows. Round 5h should pick one (the brief already assumes
  `GET /v1/models`) and treat the other as unverified rather than interchangeable.
- Fields the `/docs/models` prose itself actually documents, verbatim or near-verbatim: `slug`
  (examples given: `claude-opus-5`, `gpt-5.5`, `gemini-3.7-flash`), a display name, context
  window, input/output modalities, pricing, `retention` ("a verdict over its platform-funded
  routes' documented provider postures"), `data_policy` (explicitly: "`GET /v1/models` carries
  each model's `data_policy`"), `stats_source` (`'openrouter'` for seeded values vs. `'observed'`),
  and `pricing_source` (`'openrouter'`, `'provider-docs'`, `'aws-price-list'`, or `'estimate'`)
  (`/docs/models`, 2026-10-04, two passes, consistent both times).
- Fields `ROADMAP.md`'s live-measurement bullet lists that a **second, deliberately narrow pass
  over this same page could not find in the prose**: `context_window_tokens`, `maximum_output_
  tokens`, the four-way per-million price breakdown (input/output/cached-input/cache-write),
  `supports_tools`, `supports_structured_output`, `supports_reasoning`, `supported_reasoning_
  efforts`, `reasoning_content_native`, `reports_cached_input_tokens`, `owned_by`, and sampling
  bounds. This is not a contradiction -- it means those exact field names rest on the owner's own
  live response from 2026-10-04, not on anything stated in the documentation's prose, and this
  round had no key to re-check them. Round 5h's catalog parser should treat ROADMAP's field list as
  the live-measured ground truth it already is, and not expect to find a matching schema table in
  the docs to cross-check against.
- `GET /api/models/<slug>` returns one model's detail; `GET /api/models/<slug>/providers` returns
  its deployments/rungs (`/docs/models`, 2026-10-04) -- this is the endpoint `gateway.routing.
  route_id` values come from (section 3.4) and what `/xp routes <slug>` should call.
- What the `/models` marketing page shows beyond the API: tokens/second, time-to-first-token
  (TTFT), an uptime percentage, input/output $ per million tokens, a release date, and context
  window, with filters for Provider, Modality, Params, Category, Context, Price, Age, and
  Retention, plus a model-compare view (`platform.experientiallabs.ai/models`, 2026-10-04). The
  page states **1,089 models** total, which matches `ROADMAP.md`'s "about 1,089" figure exactly
  (confirmed, see section 12). **UNCONFIRMED**: the exact measurement methodology behind the
  tok/s/TTFT/uptime numbers (synthetic probe vs. rolling real-traffic aggregate) -- the
  `/docs/telemetry` page (section 6) describes per-organization request logging, which is a
  different telemetry concept from these public, cross-model comparison stats, and no page
  reached explains how the public numbers are computed.
- Model slug grammar: no formal grammar is documented anywhere reached -- only illustrative
  examples (`claude-opus-5`, `gpt-5.5`, `gemini-3.7-flash`, `qwen3.8-27b`). **UNCONFIRMED**: any
  general "-latest" alias rule. The only confirmed `-latest` alias actually observed is
  `jev-latest` itself (`platform.experientiallabs.ai/models/jev-latest`, 2026-10-04; also matches
  the pre-existing internal brief `plans/2.0.3-jev.md`, which separately recorded `jev-latest` and
  `jev-preview` as TypeSafe-side aliases both pointing at `jev-1.13.0` "today," sourced from
  typesafe.ai/docs.typesafe.ai on 2026-09-30, not re-verified by this round). The model this
  round's own task instructions used as a second example, `claude-fable-latest`, does **not**
  appear to exist as a slug: the live catalog's Fable entry on the models page is slugged
  `claude-fable-5.1` (`platform.experientiallabs.ai/models`, 2026-10-04). This is not a correction
  to `ROADMAP.md` -- `ROADMAP.md` itself never mentions `claude-fable-latest` -- it is a
  correction to an illustrative example in this round's own task instructions, recorded here so
  round 5h does not go looking for a slug that is not there.

## 5. The waterfall in detail

A "waterfall" is the ordered list of provider routes ("rungs") a model slug can resolve to; the
gateway serves the highest-ranked enabled rung that can handle the request (`/docs/waterfall`,
2026-10-04).

**Rung kinds, in the order the docs present them, each with who pays and the Pro requirement:**

1. **Platform-funded ("house") rung** -- Experiential-hosted, paid from the caller's platform
   credits at catalog rates, no Pro requirement. This is the rung `qwen3.8-27b` answered from in
   `ROADMAP.md`'s live check (`provider: "experiential_cloud"`).
2. **BYOK rung** -- the caller's own provider account (OpenAI, Anthropic, Azure OpenAI, Bedrock,
   Vertex, etc.); billed directly by that provider, "zero markup," never draws the platform credit
   balance (`/docs/adding-models`, `/docs/billing`, 2026-10-04). Requires "Add an API key" unless
   the organization already has a connected provider of that type; not itself gated to Pro, though
   the docs note generally that "adding a [model/]plan needs the Pro plan" in one place and the
   per-way gating differs (see below).
3. **"Serve it yourself"** -- the caller's own endpoint/proxy that already speaks a supported
   dialect (plain URL + key only: Anthropic-shaped, OpenAI-compatible, or OpenRouter-shaped;
   explicitly **not** eligible: Bedrock, Vertex, Azure, Gemini, "which need vendor-specific
   credentials"). **Pro feature.** No markup; "your host bills you directly"
   (`/docs/adding-models`, 2026-10-04).
4. **"Add a local model"** -- the caller's own privately-registered OpenAI-compatible server
   (anything from a laptop running llama-server to an internal cluster). **Pro feature.** Same
   zero-markup, self-billed model as rung 3 (`/docs/adding-models`, 2026-10-04).

**BYOK setup, step by step** (`/docs/adding-models`, 2026-10-04): from `/models`, "Add model ->
Add an API key," search the catalog, pick the model; list existing provider connections first to
choose an unused `setup_alias` (`GET /api/orgs/<org_id>/provider-connections`), then create the
connection (`POST /api/orgs/<org_id>/provider-connections/<provider>` with `{"setup_alias": ...,
"secret": ..., "config": {}}`). "Never overwrite an existing account to connect another key"; a
`409 account_exists` means pick a different alias. Keys are write-only -- reads only ever return
the last four characters. Per-provider required fields differ: OpenAI/Anthropic just need an API
key; Azure OpenAI needs the key, resource endpoint, `api_version`, and a model-to-deployment map;
Bedrock needs AWS credentials and region; Vertex needs a service-account JSON key, GCP project ID,
and location.

**"Serve it yourself," step by step** (`/docs/adding-models`, 2026-10-04): from a model's page,
"Add a way -> Serve it yourself," enter the endpoint URL and API key, optionally a served-model
ID if the host names the model differently, "Test connection." The gateway "sends one small
request in the model's dialect and only lets you add the lane once it answers correctly"; a
failed check "shows what came back and blocks the add, so a broken endpoint never lands." The key
is stored encrypted and never echoed back.

**"Add a local model," step by step** (`/docs/adding-models`, 2026-10-04): "Add model -> Add a
local model" (Pro-gated in the web app). Fields: Name, Base URL (e.g.
`https://your-host:8000/v1`), Model ID (what the server itself expects), a set of Supported-
Parameters checkboxes (temperature, tools, reasoning, response format, structured outputs -- "the
gateway rejects unsupported fields," i.e. declare only what the server can actually do), and an
optional Endpoint API Key sent as `Authorization: Bearer` to that server. Equivalent API call:

```json
{
  "slug": "my-local-model",
  "display_name": "My Local Model",
  "supported_params": {"temperature": true, "tools": true},
  "providers": [{"provider": "local", "provider_model_id": "my-model",
                 "base_url": "https://your-host:8000/v1",
                 "endpoint_api_key": "the-key-your-server-requires"}]
}
```

Rotate or clear that key later with `PUT /api/models/<slug>/providers/<id>/endpoint-credential`.
The result is "a private, organization-scoped model callable by slug with identical telemetry to
hosted models" (same page) -- i.e. it shows up in `/v1/models`/`/api/models` for that org exactly
like a catalog model, which matters for round 5h's picker integration (a user's own local model
added this way should appear as an ordinary `xp:<slug>` entry, not a special case).

**Pro plan, generally:** "Adding a [local] model needs the Pro plan (upgrade at the platform's `/credits` page). A 402
with code `pro_required` means the organization is not on Pro yet" (`/docs/plans`, 2026-10-04).
Separately, `/docs/plans` (2026-10-04) describes a different Pro feature not mentioned in the
brief: pooling ChatGPT (Plus/Pro/Team/Enterprise) and Claude (Pro/Max/Team) **subscription seats**
into the org, with automatic rotation across pooled plans/keys and live 5-hour/weekly usage-window
tracking, with one caveat -- "a request that sets an output ceiling (`max_tokens`) skips ChatGPT
plan rungs." **UNCONFIRMED** how this subscription-seat pooling relates structurally to the
four-rung model above (it reads like a fifth rung kind, or a BYOK variant specific to chat-app
subscriptions rather than API keys); no page reached states where it sits in waterfall order. Not
in round 5h's scope, but worth a note in the doc it writes so nobody assumes BYOK means "API keys
only."

**Failover and fail-closed rules** (`/docs/waterfall`, 2026-10-04): the gateway attempts rungs
top-to-bottom; an operational failure (auth, transport, or provider error) on the active rung
triggers fallback to the next enabled one. "If every on rung is a BYOK rung and none can serve,
the request fails closed" -- an unhealthy BYOK connection is dropped rather than silently
rerouted onto platform credits. At least one rung must stay enabled at all times (the last one
cannot be switched off until another is turned on). A BYOK rung "never replaces the platform
lane and is never dialed first" by default ordering. Retries within one rung and fallbacks across
rungs are counted as distinct attempts, both against `gateway.retry.max_total_attempts` (section
3.4).

**Becoming a provider** (the supply side, `/docs/providers`, `/docs/providers/guide`,
2026-10-04) is a different, unrelated flow from all four rungs above: an operator runs a public
OpenAI-compatible endpoint, declares a data-handling manifest, and gets listed in the public
catalog with measured latency/throughput/uptime; "the gateway routes customer traffic to you,
bills at the rates you publish ... net of an agreed platform fee, with payouts on request."
Providers set their own pricing (integer nano-USD per million tokens), capacity (reject via HTTP
429), and lifecycle (add/deprecate models). `/docs/providers/guide`'s actual operational detail
(running lanes, pricing config, "canary ramp" procedures, payout mechanics) is **UNCONFIRMED** --
the page states it is "shown to provider orgs only" and the content was gated when read.

## 6. Cost and credits

- **Per-response cost**: `/docs/data-controls` (2026-10-04) states non-streaming Chat/Responses/
  Messages bodies carry a top-level `provider`, `cost`, and `is_byok`. The live call in
  `ROADMAP.md` (2026-10-04) got `cost` only nested at `usage.cost`, with top-level `cost` absent --
  confirmed as a real docs/live mismatch, not a misreading (see section 1, section 12). Round 5h
  should read `usage.cost` as primary and treat a top-level `cost` as a bonus if a future gateway
  version adds it, not the other way around. `provider` and `is_byok` should still be read
  top-level per the docs and the live check both agreeing on that part.
- **`GET /api/v1/credits`** -- returns `{"data": {"total_credits": <USD>, "total_usage": <USD>}}`;
  remaining balance is `total_credits - total_usage` (`/docs/cost-api`, 2026-10-04). Matches
  `ROADMAP.md`'s live-measured shape exactly (confirmed, section 12). Works with the inference key.
- **`GET /api/v1/usage`** -- settled-row export, paginated (`{"data": [...], "next_cursor": {...}
  | null}`). Per-row fields: `id`, `created_at` (ISO 8601 settlement time), `model`, `provider`
  (winning provider), `attribution_label`, `real_cost_usd` (credits + BYOK pass-through combined),
  `cost_usd` (platform-credit cost only), `estimated_cost_usd`, `pricing_known` (boolean),
  `input_tokens`, `output_tokens`, `cached_input_tokens`, `reasoning_tokens`, `status`, and
  `api_key_id` (`/docs/cost-api`, 2026-10-04). This is what `halo cost --experiential` should
  page through. Works with the inference key.
- **`GET /api/v1/generation?id=<request_id>`** -- settled cost/token detail for one request
  (`/docs/cost-api`, 2026-10-04). Useful for a "show the cost of that last turn" drill-down.
- **`GET /api/v1/activity`** -- "recent activity rollup for the organization" (`/docs/cost-api`,
  2026-10-04); fields not detailed on the page reached -- **UNCONFIRMED** beyond its existence.
- **`GET /api/v1/key`** -- usage/limit metadata for the calling key itself: daily spend cap and
  remaining balance (`/docs/account-api`, 2026-10-04). Possibly a cheaper per-turn check than
  `/api/v1/credits` for a status-bar refresh, since it's scoped to one key rather than the org;
  **UNCONFIRMED** whether its balance figure is identical to `/api/v1/credits`' org-wide figure
  for an org with only one key.
- **Spend API** (`/docs/spend-api`, 2026-10-04) -- a materially different, heavier system gated to
  the **provisioning key only** ("inference keys receive 403"). Base
  `/api/orgs/<org_id>/spend`, endpoints `POST /analytics` (start an analysis job), `GET
  /analytics/<load_id>` (poll/resume), `POST /query` (paginate a frozen report), `POST /view`
  (registered snapshot views), `POST /series` (dimension-ranked data), `POST /export` (CSV), `POST
  /facets` (discover dimensions). Reports carry eleven core figures (`count`, `rejected_count`,
  `errors`, `input_tokens`, `output_tokens`, `cached_input_tokens`, `reasoning_tokens`,
  `paid_nano_usd`, `byok_nano_usd`, `free_nano_usd`, `unknown_usage_count`); money fields are
  decimal-integer **nano-USD strings** (1 USD = 1,000,000,000 nano-USD, "requiring BigInt
  arithmetic" per the page's own framing) -- a different unit from the credits/usage APIs' plain
  USD floats, so any shared cost-formatting helper in Halo needs to handle both units, not assume
  one. Attribution dimensions: `member`, `end_user`, `key`, `app` (classified from request
  headers), `source`, `tags`. Out of scope for round 5h (needs a key the brief never asks for);
  worth a one-line mention in the doc round 5h writes so a future "org spend dashboard" feature
  knows it needs its own key-acquisition step.
- **Spend & Intelligence dashboard** (`/docs/spend`, 2026-10-04) is the web-UI face of the Spend
  API: financial overview (credits charged, BYOK estimated value, promotional usage at list
  price), breakdowns by model/key/member/end-user/provider/funding-source/API-surface/outcome/
  error-class/prompt-config/usage-source, hourly/daily windows, period comparisons, and a
  Pro-only, platform-admin-only "Intelligence" tab that runs AI-generated analysis over the same
  data. Nothing here is an API Halo calls; it's context for what the owner sees in the browser.
- **Billing mechanics** (`/docs/billing`, 2026-10-04): credits are prepaid at $0.01/credit,
  charged at catalog token rates with no markup. Pro tiers are $20+/$200+/$2,000+ (monthly or
  annual). Free accounts get a recurring credit benefit that starts with a one-time $1 card-
  verification charge, credited back. Some models carry a promotional free daily token allowance
  (example given: `gpt-6-astra`); once exhausted, requests return `429 insufficient_quota` and
  don't spend credits unless "credits overflow" is enabled, which then bills overage at list
  price. In-flight requests reserve their expected cost against the balance until usage settles;
  available balance is credits minus pending reservations minus settled charges.

## 7. Errors

Full table as documented on `/docs/errors` (2026-10-04). "Retry?" is the page's own guidance, not
an inference. Round 5h's plain-sentence column is this doc's own suggestion for Halo's error
translation layer, not from the gateway's docs.

| `code` | HTTP | `type` | Retry? | Plain sentence for Halo to show |
| --- | --- | --- | --- | --- |
| `model_location_not_supported` | 403 | `permission_error` | No | This model can't be served to your account's region; pick a different one. |
| `invalid_json` | 400 | `invalid_request_error` | No | The request body wasn't valid JSON (a Halo bug, not a user error). |
| `invalid_request` | 400 | `invalid_request_error` | No | The gateway rejected this request; see its message for which field. |
| `invalid_parameter` | 400 | `invalid_request_error` | No | One field in the request was invalid (max 65,536 chars on text fields). |
| `unsupported_capability` | 400 | `invalid_request_error` | No | This model can't do what was asked (e.g. structured output); pick a model whose catalog row says it can. |
| `unsupported_parameter` | 400 | `invalid_request_error` | No | This gateway doesn't accept that field; it was removed from the request. |
| `refusal` | 400 | `invalid_request_error` | No | The model or provider declined this request (see `refusal_reason`, e.g. `cyber_policy`, `content_policy`); try a different model or rephrase. |
| `previous_response_not_found` | 400 | `invalid_request_error` | No | That conversation handle expired; resend the full conversation instead of continuing it. |
| `invalid_key` | 401 | `authentication_error` | No | The Experiential Labs key is missing or wrong; check it in `/providers`. |
| `model_not_granted` | 403 | `permission_error` | No | This account can't use that model slug; use the dotted slug from the catalog. |
| `idempotency_conflict` | 409 | `invalid_request_error` | No | That idempotency key was already used for a different request; Halo should mint a fresh one per turn. |
| `idempotency_replay_unavailable` | 409/500 | `invalid_request_error` | Yes | The gateway couldn't replay the earlier result; resend with a new idempotency key. |
| `insufficient_quota` | 429 | `rate_limit_error` | No | Out of credit (or past a free daily allowance); add credits or wait for the allowance to reset. |
| `org_under_review` | 429 | `rate_limit_error` | No | The account is paused for review; contact Experiential Labs support. |
| `unavailable_route` | 429/503 | `rate_limit_error` | Yes | No working route for this model right now; Halo retries once after a short backoff, same as the gateway recommends. |
| `gateway_overloaded` | 429 | `rate_limit_error` | Yes | The gateway itself is overloaded; back off and retry. |
| `request_cancelled` | 499 | `server_error` | Yes | The request was cancelled mid-flight; resend if the result is still needed. |
| `provider_internal` | 502 | `server_error` | Yes | The upstream provider had an internal error; retry with backoff. |
| `empty_completion` | 200 (+ `x-gateway-warning` header) | `warning` | No | The model answered with nothing; try rephrasing or trimming the last turn, or switch models. |
| `all_routes_failed` | 502 | `server_error` | Yes | Every rung for this model failed; retry, and if this is a BYOK model, check that key. |
| `provider_output_too_large` | 502 | `server_error` | No | The model's answer was too large for the provider to return; lower the output-length limit. |
| `gateway_draining` | 503 | `server_error` | Yes | This gateway instance is shutting down; retry (a different instance will pick it up). |
| `deadline_exceeded` | 504 | `server_error` | Yes | The request took too long; shorten the task or retry. |
| `internal_error` | 500 | `server_error` | Yes | The gateway had an internal error; retry with backoff. |

Notes beyond the table (`/docs/errors`, 2026-10-04): `refusal` errors carry an extra
`refusal_reason` field (examples given: `cyber_policy`, `content_policy`) -- per
`plans/WORKER-RULES.md`'s no-cyber-blocks rule, Halo must not turn this into a second refusal
layer of its own; it should just surface the gateway's own refusal as a plain sentence and let the
user decide, exactly like any other upstream error. `empty_completion` is the one "error" that is
actually an HTTP 200 with a warning header, not a failure status -- Halo's error-translation
dispatch needs to check that header on *every* 200 response from this provider, not just branch on
status code. Idempotency support exists only on Chat Completions and Responses (confirmed again
here, matching section 3.4). The page's own retry guidance for `unavailable_route` is exponential
backoff -- this matches `ROADMAP.md`'s observed 503 on `jev-latest` and the brief's plan to retry
it once after a short delay; today's `/models/jev-latest` page (2026-10-04) shows that model
"active" with 99.9% uptime, which is consistent with the 503 having been a transient outage at the
hour it was measured, not a standing problem.

## 8. The local gateway (`exp run`)

This section draws on the GitHub repo directly: `github.com/experientiallabs/experiential`
(README, `SETUP.md`, `docs/usage.md`, `docs/reference/gateway-architecture.md`, all read
2026-10-04) rather than the hosted docs site, which doesn't cover the local gateway.

- Repo: "Experiential is the open source, zero markup gateway for BYOK, self-hosted and 1000+
  marketplace models," license Apache-2.0, ~8.9k stars / 228 forks at read time (point-in-time,
  will drift) (`github.com/experientiallabs/experiential`, 2026-10-04).
- **Install**: `pip install experiential`, then `exp` (or `exp run`) to start the local gateway
  (README, 2026-10-04). This is a Python-packaged CLI (`pyproject.toml`, `uv.lock` at repo root),
  **not** a Rust toolchain the user needs to build anything with.
- **The actual serving engine is a compiled Rust extension, not Python.** "The gateway has one
  data plane: the native Rust HTTP server in the PyO3 extension `exp_gateway_native` ... every
  launch uses it; a missing extension fails with its build command, never a Python fallback"
  (`docs/reference/gateway-architecture.md`, 2026-10-04). Socket handling, HTTP dispatch, and SSE
  encoding run in that native layer, outside Python's GIL; Python retains authority over auth,
  decoding, and SQLite transactions (same page). This reconciles `ROADMAP.md`'s "Apache-2.0, Rust"
  shorthand with what a GitHub listing alone would suggest (a Python project) -- see the
  correction in section 12.
- **Default port**: `127.0.0.1:8000` (README example: `base_url="http://127.0.0.1:8000/v1"`,
  2026-10-04) -- confirms `ROADMAP.md`'s note that this collides with vLLM's default in Halo's own
  local-server probe list, and confirms round 5h's "detect by `/v1/models` shape, not by port" plan
  is the right call, not an optional nicety.
- **Local state**: `docs/usage.md` (2026-10-04) says the gateway reads from a SQLite store at
  `gateway/traffic.db` and from `.exp/models.toml`; `docs/reference/gateway-architecture.md`
  (2026-10-04) separately says "private serving authority lives in `ROOT/gateway/gateway.db`,
  including identities, keys, grants, provider connections and revisions, aliases and revisions."
  **UNCONFIRMED** which of `traffic.db`/`gateway.db` is canonical or whether both exist for
  different purposes (traffic logging vs. serving authority) -- the two repo doc files were not
  reconciled against each other on this pass.
- **`exp login`**: "sign in to Experiential Cloud, save the returned organization key, and
  synchronize account-visible models with the catalog's default-route capabilities and
  undiscounted prices," writing "user-local credential plus secret-free hosted provider/model
  records" into `.exp/models.toml` (`docs/usage.md`, 2026-10-04).
- **`exp config`**: subcommands seen include `exp config gateway call <alias> <prompt> [--json]`,
  `exp config gateway models [--json]`, `exp config gateway key check [--json]`, `exp config
  providers [--provider NAME ...]`, `exp config budget [USD] --root ROOT`, `exp config telemetry
  status|enable|disable` (`docs/usage.md`, 2026-10-04).
- **Ollama and llama.cpp attachment**: **UNCONFIRMED.** None of the four repo documents read
  (README, `SETUP.md`, `docs/usage.md`, `docs/reference/gateway-architecture.md`) mention Ollama
  or llama.cpp by name, or describe any dedicated auto-detection of either. The best-supported
  inference, not a documented fact, is that `exp run` would front them the same generic way the
  hosted platform's "Add a local model" flow works (section 5): point a `provider: "local"` entry
  at Ollama's or llama-server's own OpenAI-compatible base URL (e.g. `http://127.0.0.1:11434/v1`
  for Ollama) via `exp config providers`/the `.exp/models.toml` file, giving it a name/alias in the
  process, rather than `exp` having Ollama- or llama.cpp-specific code. Round 5h's doc should state
  this as an open question for whoever actually runs `exp` for the first time, not as settled fact.
  Local model naming through `exp`: the only concrete example seen is a plain alias like
  `opus-5` for a *hosted* model (README, 2026-10-04) -- no example of a locally-attached model's
  alias was found.
- **How this coexists with Halo's own local layer**: round 5h's plan to recognize a running `exp
  run` gateway via its `/v1/models` response shape (not its port, since 8000 collides with vLLM)
  and expose it as `hf:local/<model>@exp` is consistent with everything confirmed above -- `exp`
  is, at minimum, an OpenAI-compatible server on loopback, which is exactly what Halo's existing
  local-server probe already knows how to shape-detect.

## 9. Embeddings

- **`POST /v1/embeddings`**, headers `Authorization: Bearer <xpl_ key>` and `Content-Type:
  application/json` (`/docs/embeddings`, 2026-10-04).
- Request fields: `model` (required), `input` (required: string, list of strings, a token-ID
  array, or a list of token-ID arrays), `dimensions` (optional, positive integer, only on models
  that support reduced dimensions), `encoding_format` (`"float"` or `"base64"`, optional), `user`
  (optional, end-user attribution string, max 1,024 characters) (`/docs/embeddings`, 2026-10-04).
- Response: `{"object": "list", "model": ..., "data": [{"object": "embedding", "index": 0,
  "embedding": [...]}, ...], "usage": {"prompt_tokens": N, "total_tokens": N}}` (`/docs/
  embeddings`, 2026-10-04).
- Models named explicitly on this page: `text-embedding-3-small`, `text-embedding-3-large`,
  `text-embedding-ada-002` (all OpenAI's own) (`/docs/embeddings`, 2026-10-04). The page says to
  "verify availability via `GET /v1/models`" and that a `supports_embeddings` catalog flag
  "identifies compatible models" -- confirming that flag is real and documented, matching round
  5h's plan to record it. **UNCONFIRMED**: the full list of embedding-capable models in the
  ~1,089-model catalog; only these three were named by name, and a catalog this size plausibly has
  more (e.g. any provider that offers its own embedding models), but no page reached enumerated
  them.
- No caching (repeated identical requests re-bill), no streaming, dimension reduction only on the
  `text-embedding-3-*` models (not `ada-002`), billing is input-tokens-only (`/docs/embeddings`,
  2026-10-04). Round 5h correctly scopes this to "recorded for 2.0.6, no embedding calls this
  round" -- nothing above changes that.

## 10. The `jev-latest` model

- **What it is**: a "typed decision model," not a chat or coding model -- it takes structured
  input and returns typed, calibrated answers to three question shapes: `choice` (pick from named
  options), `score` (rate an ordered scale), and `noul` (a yes/no probability)
  (`platform.experientiallabs.ai/models/jev-latest`, 2026-10-04). It is explicitly not a drop-in
  LLM: no streaming, processes structured JSON requests. Context is "about 32,000 input tokens,
  shared by state and all question definitions; not a total context window or an output limit";
  maximum output tokens shows "no data." Pricing shown: "$0.042/M in · $0/M out" with a
  "Promotion: Free" label. Listed as "active," 99.9% uptime at read time.
- **Who makes it**: "TypeSafe," served through "Experiential Cloud" per that page. This matches,
  independently, the pre-existing internal research already in this repo at `plans/2.0.3-jev.md`
  (dated 2026-09-30, sourced from typesafe.ai and docs.typesafe.ai, not re-fetched by this round):
  TypeSafe AI's "Jev" is described there as "TypeSafe's first public System One Model, optimized
  for automation," a model that "returns typed decisions with calibrated probabilities," "does not
  generate text," with the identical three question types (`choice`, `score`, `noul`), the same
  $0.042-per-million-input/free-output pricing, and the same approximate 64k-token request budget
  (32k for `state` plus the longest question) -- all consistent with what the Experiential catalog
  page shows today. `plans/WORKER-RULES.md`'s own list of provider env-var prefixes Halo already
  guards (`TYPESAFE_*` alongside `OPENROUTER_*`, `DATABRICKS_*`, etc.) confirms TypeSafe is already
  a known, named provider in this codebase, not a new discovery.
- **Relation to "jev" on Databricks and OpenRouter**: the Experiential catalog page itself says
  nothing about Databricks or OpenRouter -- that link is only established by the pre-existing
  internal doc, not by anything this round fetched from experientiallabs.ai. `plans/2.0.3-jev.md`
  records that OpenRouter separately lists `typesafe/jev-router`, "a chat-completions router" that
  "picks the best model and reasoning effort for each request, running on Jev" -- described there
  as "a different product from the decision API," already assessed and parked by the owner
  2026-10-03 as "not as a Halo session model." Nothing found this round changes that assessment.
  **UNCONFIRMED**: any specific Databricks-hosted "jev" model or deployment; `plans/2.0.3-jev.md`
  doesn't mention Databricks either, so that part of this round's task instructions may be
  describing something not yet captured anywhere in this repo's research -- flagged here rather
  than guessed at.
- **Availability**: the `503 unavailable_route` ROADMAP recorded at a specific hour on 2026-10-04
  is a documented, retryable error code (section 7); it does not conflict with the catalog page
  showing the model "active" today, since the page is a static catalog listing, not a live health
  check.

## 11. Experiential feature to Halo surface

| Experiential feature | Halo surface |
| --- | --- |
| `xp:<slug>` on its own provider profile (no `usage.include`, `provider` prefs, `transforms`) | New `experiential` provider; `xp` alias beside `cc`/`dbx`/`or`/`ant`/`hf`; `/providers` |
| `EXPLABS_API_KEY` (inference) vs `EXPLABS_PROVISIONING_KEY` (management) | Init wizard asks only for the inference key this round; provisioning key deliberately out of scope -- document why so a later round doesn't assume it was overlooked |
| `GET /v1/models` catalog (context, prices, `supports_*`, `data_policy`, `owned_by`) | Picker's Experiential group/columns; `resolve_model_profile` |
| `GET /api/models/<slug>/providers` (rung list) | `/xp routes <slug>` |
| `usage.cost` (primary) / top-level `cost` (docs say top-level, live says nested -- read both, prefer nested) | Transcript model line; cost meter |
| `provider`, `is_byok`, `x-request-id` | Transcript model line (which rung actually served this turn, and whether it was BYOK) |
| `GET /api/v1/credits` | Balance chip (`total_credits - total_usage`) |
| `GET /api/v1/usage`, `GET /api/v1/generation` | `halo cost --experiential` |
| `gateway.routing.route_id` / `allow_fallbacks` | Per-role routing override in the roles table; a pinned local-first role sets `allow_fallbacks: false` |
| `gateway.retry` (`max_attempts_per_route`, `max_total_attempts`, `backoff`) | Mapped from Halo's own retry policy, `max_attempts_per_route: 1` by default |
| `Idempotency-Key` (Chat Completions/Responses only) | Recommended addition beyond the brief: Halo's outer retry loop sends one per logical turn on those two endpoints so its own retries can't double-bill |
| `x-experiential-ignored-parameters`, `x-gateway-replay-repair` | Learned per-model rule store (same mechanism as the existing endpoint-rejects-tools rule) plus a one-time plain notice; the replay-repair header is a distinct signal, not folded into the ignored-parameters list |
| `unsupported_capability`/`response_format` (structured output never silently degrades) | A specific plain sentence in the error table, not a generic 400 handler, since this is the one capability check `supports_structured_output` exists to prevent |
| Full error table (section 7) | Error translation layer, one row per code, retry flag honored |
| `{"type": "openrouter:tool_search"}` + `defer_loading: true` | Candidate mechanism for shrinking the built-in tool catalog's prompt cost on `xp:` routes specifically; flagged to round 6's lean-prompt work, not required this round |
| Thinking native only while the serving rung is Anthropic-shaped; translated to `reasoning_effort:<tier>` or dropped on fallback | Transcript must show the disclosed translation/drop, not assume native thinking just because the call hit `/v1/messages` |
| Data controls (`capture_prompt_content`, `require_zdr`, `require_no_training`, `allowed_processing_regions`, `allowed_providers`) and their response headers (`x-gateway-provider`, `x-gateway-zdr`, `x-gateway-route-depth`) | Not in round 5h's plan at all; candidate for a debug/verbose view now, an explicit privacy-settings surface later -- gap noted for a future round |
| Waterfall rungs (platform/BYOK/serve-it-yourself/add-a-local-model) and their Pro gating | `pro_required` (402) through the existing error path; BYOK/local-model setup documented as the user's own choice, same framing as the `hf:`/`ol:` routes |
| `exp run` local gateway, detected by `/v1/models` shape (port 8000 collides with vLLM) | `/local`; usable as `hf:local/<model>@exp` |
| `/v1/embeddings`, `supports_embeddings` catalog flag | Recorded now, parked for 2.0.6, no calls this round |
| `jev-latest` (TypeSafe's typed-decision model, not a chat model) | Informational only this round -- not a `main`/session-chat candidate; the real integration target is the Jev decision API already scoped in `plans/2.0.3-jev.md`, which this gateway alias does not change |
| Two base-URL families (`/v1` inference, `/api/v1` account/cost) | `BRIDGE_EXPERIENTIAL_BASE_URL` override must cover (or clearly scope to) both roots |

## 12. Corrections to `ROADMAP.md`'s live-facts bullets

Most of the "ADDED 2026-10-04 (rolo): Experiential Labs" section's measured facts check out
against the documentation; this round did not find a factual error in them, only places where the
docs add detail, a caveat, or a slightly different picture. Listed as confirmations and amendments
rather than reversals:

1. **Confirmed exactly, no change**: `GET /v1/models` returning fewer rows than the public
   catalog ("the key's enabled rungs decide what the list shows") is consistent with how the
   catalog is described (`/docs/models`, `/docs/core-loop`, 2026-10-04); the public catalog figure
   itself, "about 1,089," matches the `/models` page's own stated count, **1,089**, exactly
   (`platform.experientiallabs.ai/models`, 2026-10-04). The 299-for-this-key figure itself rests on
   the owner's own live call and could not be re-measured this round (no key).
2. **Confirmed, with a provenance caveat**: of the per-model field list ROADMAP records
   (`context_window_tokens`, `maximum_output_tokens`, the four price fields, `supports_tools`,
   `supports_structured_output`, `supports_reasoning`, `supported_reasoning_efforts`, `reasoning_
   content_native`, `reports_cached_input_tokens`, `data_policy`, `owned_by`, sampling bounds),
   only `data_policy` is spelled out in the `/docs/models` prose itself (two passes, section 4).
   The rest are real -- ROADMAP measured them directly off a live response -- but they are not
   independently confirmable from documentation text with no API key. Record them as
   live-measured, not documented, so a future re-check knows to hit the API rather than reread the
   docs.
3. **Confirmed exactly**: the top-level-`cost`-vs-`usage.cost` mismatch. `/docs/data-controls`
   (2026-10-04) does say non-streaming bodies carry a top-level `cost` (with `provider` and
   `is_byok`); ROADMAP's live call got it only in `usage.cost`. ROADMAP's framing ("the docs say
   top-level; the live response puts it in `usage.cost`") is accurate, not a misreading.
4. **Confirmed exactly**: `GET /api/v1/credits` returning `{total_credits, total_usage}` in USD
   matches `/docs/cost-api` (2026-10-04) field-for-field.
5. **Confirmed, mechanism now documented**: the `or:`-profile 400 on a `usage` field. The refused-
   parameters list on `/docs/openai-compatibility` (2026-10-04, two passes) never names `usage` or
   `usage.include` specifically -- because that field doesn't exist in Experiential's own schema
   at all, so it is rejected as a foreign/invalid parameter rather than via a special rule. Same
   outcome ROADMAP already observed live; this round adds the "why."
6. **New clarification, not in ROADMAP**: ROADMAP (and the pre-existing project memory) describe
   the GitHub project simply as "open-source (Apache-2.0, Rust)." That is directionally right but
   can mislead an implementer: the repo installs as an ordinary Python package (`pip install
   experiential`, `pyproject.toml`/`uv.lock`), and the Rust is a compiled native extension
   (`exp_gateway_native`, built with PyO3) that the Python CLI loads for the actual data plane
   (`docs/reference/gateway-architecture.md`, 2026-10-04) -- nobody integrating with `exp run`
   needs a Rust toolchain; "Python package wrapping a Rust-compiled data plane" is the more useful
   shorthand than "Rust."
7. **New clarification, not in ROADMAP**: ROADMAP's description of the local gateway as fronting
   "Ollama and llama.cpp" states this as settled fact. This round could not confirm any
   Ollama/llama.cpp-specific code or documentation in the repo's README, `SETUP.md`,
   `docs/usage.md`, or `docs/reference/gateway-architecture.md` (section 8). It is a reasonable
   inference from the generic "add a local model" mechanism the hosted platform documents, but it
   is an inference, not a confirmed fact, until someone runs `exp` and tries it.
8. **New addition, not a correction**: ROADMAP's section does not mention the inference-key vs.
   provisioning-key split (`EXPLABS_API_KEY` vs `EXPLABS_PROVISIONING_KEY`) at all. This matters
   because the Spend API and full key-management API are unreachable with the key round 5h's
   brief already plans to collect -- worth adding to ROADMAP or the round 5h brief so a later
   "org spend dashboard" idea doesn't assume the existing key already covers it.
9. **New addition, not a correction**: "gateway-side tool search (`defer_loading`)" is more
   specific than ROADMAP's one-line mention suggests -- it's a `{"type":
   "openrouter:tool_search"}` sentinel tool plus per-tool `defer_loading: true`, capped at 3 search
   rounds by default (section 3.5), not a request-level boolean.

## 13. What could not be confirmed

- The complete JSON field schema for a `GET /v1/models` model object beyond `slug`, display name,
  context window, modalities, pricing, `retention`, `data_policy`, `stats_source`, and
  `pricing_source` -- the richer field set ROADMAP records rests on live measurement only
  (sections 4, 12).
- Whether `GET /v1/models` and `GET /api/models` return identical per-model fields, or `/api/
  models` is a superset in fields as well as in rows (section 4).
- The `GET /api/models/<slug>` single-model detail response shape (only that the endpoint exists
  was confirmed).
- `/docs/providers/guide`'s actual operational content (running lanes, pricing config, "canary
  ramp," payout mechanics) -- the page states it is shown to provider orgs only and was gated when
  read (section 5).
- Any formal model-slug grammar, or a general "-latest"-alias rule; only `jev-latest` was actually
  observed as a live `-latest` alias, and the task-instruction example `claude-fable-latest` does
  not appear to exist (the real slug is `claude-fable-5.1`) (section 4).
- Whether an alias like `jev-latest` can change its pinned underlying version without notice; no
  explicit versioning-policy sentence was found, only the pre-existing internal doc's observation
  that `jev-latest` and `jev-preview` both point at `jev-1.13.0` "today" (section 4, section 10).
- The exact measurement methodology behind the public `/models` page's tok/s, TTFT, and uptime
  figures (synthetic probe vs. rolling real-traffic aggregate) (section 4).
- Which of `gateway/traffic.db` or `ROOT/gateway/gateway.db` is the canonical local-state store
  for `exp run`, or whether both exist for different purposes (section 8).
- Any Ollama- or llama.cpp-specific attachment mechanism for `exp run` (section 8) -- the generic
  "add a local model" mechanism is documented; a dedicated integration is not.
- The full list of embedding-capable models in the catalog beyond the three named OpenAI models
  (section 9).
- Any documented relationship between `jev-latest` and a Databricks-hosted "jev" model -- neither
  the Experiential catalog page nor this repo's pre-existing Jev research mentions Databricks
  (section 10).
- `Idempotency-Key`'s exact mechanics (header name confirmed; TTL and scope were not detailed on
  any page reached) (section 3.4).
- Numeric rate limits for the gateway itself or per key, beyond the `429` error codes' existence;
  no requests/sec or tokens/sec figures were found for Experiential's gateway (contrast with
  TypeSafe's own direct Jev API, which the pre-existing internal doc separately records as 100K
  tokens/s and 40 requests/s -- that is TypeSafe's API, not this gateway's).
- Whether the Responses dialect (`/v1/responses`) shares Chat Completions' exact refused/dropped
  parameter list; no page gave a Responses-specific list (section 3.2).
- Where ChatGPT/Claude subscription-seat pooling (`/docs/plans`) sits in the four-rung waterfall
  model, structurally (section 5).

## 14. Glossary

- **Rung**: one specific provider route a model slug can resolve to (platform-funded, BYOK, serve-
  it-yourself, or a registered local server).
- **Waterfall**: the ordered list of rungs for one model slug; the gateway serves the highest
  enabled rung that can handle the request.
- **BYOK**: Bring Your Own Key -- a rung billed directly by the caller's own provider account.
- **ZDR**: Zero Data Retention -- a data-control posture restricting routing to providers with a
  no-retention commitment.
- **Inference key**: an `xpl_`-prefixed key (`EXPLABS_API_KEY`) that calls models and reads
  credits/usage.
- **Provisioning key**: a separate key (`EXPLABS_PROVISIONING_KEY`) required for key management,
  org budgets/identities, and the full Spend API; an inference key gets 403 on those.
- **`route_id`**: an opaque handle naming one specific rung, obtained from `GET /api/models/<slug>
  /providers`, used in `gateway.routing.route_id` to pin a request to that rung.
- **`setup_alias`**: the name chosen when creating a BYOK provider connection, so a second key for
  the same provider doesn't collide with the first.
- **`is_byok`**: a response field marking whether the rung that served a request was a BYOK rung.
- **`data_policy`**: a per-model catalog field describing that model's data-handling posture.
- **`stats_source` / `pricing_source`**: catalog provenance tags (`'openrouter'`, `'observed'`,
  `'provider-docs'`, `'aws-price-list'`, `'estimate'`) describing where a model's stats or price
  came from, not the model's own output.
- **`safety_identifier`**: the attribution field Halo should set to the session id so per-session
  spend is visible in the owner's dashboard.
- **`attribution_label`**: the corresponding field name on settled usage rows (`/api/v1/usage`).
- **nano-USD**: the Spend API's money unit; 1 USD = 1,000,000,000 nano-USD, carried as decimal-
  integer strings (needs 64-bit/BigInt arithmetic, not float).
- **`defer_loading`**: a per-tool flag that keeps that tool's full definition out of context until
  the gateway's own tool-search mechanism decides it's needed.
- **`exp run` / `exp`**: the open-source local gateway CLI (`pip install experiential`); binds
  `127.0.0.1:8000` by default; OpenAI-compatible.
- **`exp_gateway_native`**: the compiled Rust/PyO3 extension that is the actual data plane behind
  both the hosted gateway and the local `exp run` gateway.
- **Jev / `jev-latest`**: TypeSafe's typed-decision model (not a chat model), reachable through
  this gateway as a catalog slug; see `plans/2.0.3-jev.md` for the fuller pre-existing research.
- **System One Model**: TypeSafe's own term (per `plans/2.0.3-jev.md`) for the category of model
  Jev belongs to -- returns typed, calibrated decisions rather than generated text.
