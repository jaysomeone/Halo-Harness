# OpenCode provider/model layer and model-robustness machinery (verified 2026-09-24)

Scope note: "verified" below means I fetched the raw file from the `dev` branch of `github.com/anomalyco/opencode` (formerly `sst/opencode`) on 2026-09-24, or the live page/API. Line numbers refer to those `dev` copies and will drift. Everything under "Inferences" is my reading, not a quoted fact. Code quotes are trimmed to <= 15 lines.

Files fetched and cited throughout (all under `packages/opencode/src/` unless noted):
- `provider/transform.ts` (1,922 lines), `provider/provider.ts`, `provider/error.ts`
- `session/llm.ts`, `session/llm/request.ts`, `session/llm/ai-sdk.ts`
- `session/processor.ts`, `session/prompt.ts`, `session/retry.ts`, `session/overflow.ts`, `session/compaction.ts`, `session/message-v2.ts`, `session/session.ts`, `session/system.ts`, `session/prompt/kimi.txt`
- `tool/invalid.ts`, `effect/runtime-flags.ts`
- `packages/core/src/session/compaction.ts`, `packages/core/src/util/token.ts`, `packages/llm/src/provider-error.ts`, `packages/llm/src/providers/openrouter.ts`
- `patches/` directory listing (SDK patches OpenCode ships)
- `https://opencode.ai/config.json` (the published JSON schema), `https://models.dev/api.json` (4.9 MB, fetched)
- `databricks/ucode` repo: `src/ucode/agents/opencode.py`, `src/ucode/databricks.py`, `README.md`

---

## KQ1. Provider system: AI SDK usage, config schema, models.dev catalog, custom providers (incl. Databricks), auth

### Takeaway
OpenCode owns no HTTP client of its own: each provider is a Vercel AI SDK package named by the `npm` field (default `@ai-sdk/openai-compatible`), instantiated as `factory({ name: providerID, ...options })` with a wrapped `fetch` that adds header/chunk timeouts. Model metadata (limits, cost, reasoning/tool flags, `interleaved` reasoning field, `reasoning_options`) comes from models.dev `api.json`, deep-merged with `opencode.json` overrides. Databricks' own `ucode`/`ug` tool writes an `opencode.json` with three providers (`@ai-sdk/anthropic` at `/ai-gateway/anthropic/v1`, `@ai-sdk/google` at `/ai-gateway/gemini/v1beta`, `@ai-sdk/openai` at `/ai-gateway/mlflow/v1`), while models.dev registers a `databricks` provider as `@ai-sdk/openai-compatible` at `https://${DATABRICKS_HOST}/ai-gateway/mlflow/v1` with env `DATABRICKS_HOST` + `DATABRICKS_TOKEN`.

### Cited Findings

**Config schema (authoritative field names)** — from the published schema [opencode.ai/config.json](https://opencode.ai/config.json), `$defs/ProviderConfig`:
- Provider-level keys: `npm`, `name`, `env`, `api`, `options` (free-form object; `baseURL` typed string), `models`, plus `blacklist`/`whitelist` per docs ("when both options are used, the whitelist is applied first, then entries are removed by the blacklist") — [Providers docs](https://opencode.ai/docs/providers/).
- Model-level keys (`$defs/ProviderConfig/properties/models/additionalProperties`, `additionalProperties: false`): `id`, `name`, `family`, `release_date`, `attachment`, `reasoning`, `temperature`, `tool_call`, `interleaved` (boolean | `"reasoning"`/`"reasoning_content"`/`"reasoning_text"`/any string | `{ "field": string }`), `cost` (`input`, `output` required; `cache_read`, `cache_write`, `context_over_200k{input,output,cache_read,cache_write}`), `limit` (`context` and `output` required; optional `input`), `modalities{input[],output[]}` (enum text/audio/image/video/pdf), `experimental`, `status` (`alpha|beta|deprecated|active`), `provider{npm,api}` (per-model SDK/API override), `options` (object), `headers` (string map), `variants` (map of variant-name -> object; schema only formalises `disabled: boolean`, described as "Variant-specific configuration") — [config.json](https://opencode.ai/config.json).
- Top-level: `model` (string `provider/model`), `small_model` ("Small model to use for tasks like title generation in the format of provider/model"), `disabled_providers` ("Disable providers that are loaded automatically"), `enabled_providers`, `compaction{auto,prune,tail_turns,preserve_recent_tokens,reserved}` — [config.json](https://opencode.ai/config.json); docs add `"disabled_providers": ["openai","gemini"]` takes priority over enabled lists — [Config docs](https://opencode.ai/docs/config/).
- Provider option timeouts documented: `"timeout"` (default 300000 ms), `"headerTimeout"` (default 300000 ms for response headers), `"chunkTimeout"` (default 300000 ms between chunks), `"setCacheKey"` — [Config docs](https://opencode.ai/docs/config/).
- There is NO `retry` config key in the schema or docs (docs explicitly contain no retry/variants top-level options) — [Config docs](https://opencode.ai/docs/config/), [config.json](https://opencode.ai/config.json).

**Generic OpenAI-compatible custom provider (docs example, verbatim)** — [Providers docs](https://opencode.ai/docs/providers/):
```json
{
  "provider": {
    "myprovider": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "My AI Provider Display Name",
      "options": {
        "baseURL": "https://api.myprovider.com/v1",
        "apiKey": "{env:ANTHROPIC_API_KEY}",
        "headers": { "Authorization": "Bearer custom-token" }
      },
      "models": {
        "my-model-name": { "name": "My Model Display Name", "limit": { "context": 200000, "output": 65536 } }
      }
    }
  }
}
```
- Docs: `npm: @ai-sdk/openai-compatible` is "for `/v1/chat/completions`"; secrets via `{env:VARIABLE_NAME}` or `{file:~/.secrets/key}`; `/connect` stores keys in `~/.local/share/opencode/auth.json`; browser OAuth for OpenAI/Copilot/GitLab — [Providers docs](https://opencode.ai/docs/providers/).

**How the SDK is instantiated (provider.ts)** — [provider.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/provider.ts):
- Bundled loaders (line ~123): `"@ai-sdk/openai-compatible": () => import("@ai-sdk/openai-compatible").then((m) => m.createOpenAICompatible)`, `"@openrouter/ai-sdk-provider": ... m.createOpenRouter`, `"@ai-sdk/alibaba": ... m.createAlibaba`; anything else is installed on demand (`Npm.add(model.api.npm)`) and the first export starting with `create` is called.
- `resolveSDK` (line ~1734): forces `includeUsage` on for chat-completions SDKs, resolves `${VAR}` in the base URL from env, falls back to the provider key, merges per-model headers, and wraps fetch:
```ts
if (model.api.npm.includes("@ai-sdk/openai-compatible") && options["includeUsage"] !== false) {
  options["includeUsage"] = true
}
...
url = url.replace(/\$\{([^}]+)\}/g, (item, key) => { const val = envs[String(key)]; return val ?? item })
...
if (options["apiKey"] === undefined && provider.key) options["apiKey"] = provider.key
if (model.headers) options["headers"] = { ...options["headers"], ...model.headers }
...
const chunkTimeout = options["chunkTimeout"] ?? 300_000
const headerTimeout = options["headerTimeout"] ?? 300_000
```
- Fetch wrapper combines `AbortSignal.any([callerSignal, chunkAbort, headerTimeout, AbortSignal.timeout(options.timeout)])` and wraps `text/event-stream` bodies with `wrapSSE`, which aborts with `ProviderError.ResponseStreamError("SSE read timed out")` if no chunk arrives within `chunkTimeout` — [provider.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/provider.ts) lines 37-90, 1799-1828.
- Factory call: `factory({ name: model.providerID, ...options })` — the `name` becomes the AI SDK `providerOptions` key for openai-compatible SDKs (see KQ2 `providerOptions()`).
- OpenRouter/llmgateway/nvidia/vercel loaders only add attribution headers (`"HTTP-Referer": "https://opencode.ai/"`, `"X-Title": "opencode"`), `autoload: false` — provider.ts lines 466-508.
- Env-var auth: `const apiKey = provider.env.map((item) => envs[item]).find(Boolean)` -> source `"env"` (line ~1587). Model filtering: `status === "alpha"` hidden unless `OPENCODE_ENABLE_EXPERIMENTAL_MODELS`, `deprecated` removed, then `blacklist`/`whitelist` (line ~1680-1700).
- Config->model merge (line ~1480-1560): `apiNpm = model.provider?.npm ?? provider.npm ?? existingModel?.api.npm ?? cloudflareGatewayNpm(...) ?? modelsDev[providerID]?.npm ?? "@ai-sdk/openai-compatible"`; `toolcall: model.tool_call ?? existingModel?.capabilities.toolcall ?? true`; and the DeepSeek default:
```ts
interleaved:
  (typeof model.interleaved === "string" ? { field: model.interleaved } : model.interleaved) ??
  existingModel?.capabilities.interleaved ??
  (!existingModel && apiNpm === "@ai-sdk/openai-compatible" && apiID.includes("deepseek")
    ? { field: "reasoning_content" }
    : false),
```

**models.dev catalog** — [models.dev/api.json](https://models.dev/api.json) (fetched 2026-09-24):
- Provider record fields: `id`, `env` (array of env var names), `npm`, `api`, `name`, `doc`, `models`. Model fields: `id`, `name`, `description`, `family`, `attachment`, `reasoning`, `reasoning_options` (array of `{type:"toggle"}`, `{type:"effort", values:[...]}`, `{type:"budget_tokens", min?, max?}`), `tool_call`, `structured_output`, `temperature`, `interleaved` (`{field: "reasoning_content" | "reasoning_details" | ...}`), `knowledge`, `release_date`, `last_updated`, `modalities`, `open_weights`, `limit{context,output,input?}`, `cost{input,output,cache_read,cache_write,reasoning?}`, `status`, `options`, `provider{npm,api}`.
- Provider headers relevant to rolo-claude (verbatim values):
  - `openrouter`: env `["OPENROUTER_API_KEY"]`, npm `@openrouter/ai-sdk-provider`, api `https://openrouter.ai/api/v1`, 385 models.
  - `deepseek`: env `["DEEPSEEK_API_KEY"]`, npm `@ai-sdk/openai-compatible`, api `https://api.deepseek.com`, models `deepseek-v4-pro`, `deepseek-v4-flash` (status deprecated), `deepseek-flash`, `deepseek-v4-flash-vision-exp`.
  - `moonshotai`: env `["MOONSHOT_API_KEY"]`, openai-compatible, api `https://api.moonshot.ai/v1`, models `kimi-k3`, `kimi-k2.6`, `kimi-k2.7-code`, `kimi-k2.7-code-highspeed`.
  - `zai`: env `["ZHIPU_API_KEY"]`, openai-compatible, api `https://api.z.ai/api/paas/v4`, 18 GLM models (glm-4.5 … glm-5.3); `zai-coding-plan`: same env, api `https://api.z.ai/api/coding/paas/v4`.
  - `alibaba`: env `["DASHSCOPE_API_KEY"]`, openai-compatible, api `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` (56 models incl. `qwen3-coder-plus`, `qwen3.7-max`, `kimi-k3`); `alibaba-cn`: `https://dashscope.aliyuncs.com/compatible-mode/v1` (90 models).
  - `minimax`: env `["MINIMAX_API_KEY"]`, npm `@ai-sdk/anthropic`, api `https://api.minimax.io/anthropic/v1` (MiniMax is served over the Anthropic Messages dialect).
  - `databricks`: env `["DATABRICKS_HOST", "DATABRICKS_TOKEN"]`, npm `@ai-sdk/openai-compatible`, api `https://${DATABRICKS_HOST}/ai-gateway/mlflow/v1`, 30 models: `databricks-claude-{haiku-4-5,opus-4-1,opus-4-5,opus-4-6,opus-4-7,sonnet-4,sonnet-4-5,sonnet-4-6}`, `databricks-gemini-*`, `databricks-gpt-5*`, `databricks-gpt-oss-{120b,20b}`, `databricks-glm-5-2`, `databricks-kimi-k2-7-code`.
- Sample entries (trimmed):
  - `deepseek/deepseek-v4-pro`: reasoning true, `reasoning_options: [{toggle},{effort:[low,high,max]}]`, `interleaved:{field:"reasoning_content"}`, `limit {context:1000000, output:393216}`, `cost {input:0.435, output:0.87, reasoning:0.87, cache_read:0.003625}`.
  - `moonshotai/kimi-k2.6`: toggle-only reasoning, `interleaved reasoning_content`, `limit {262144/262144}`, cost `{0.95, 4, cache_read 0.16}`; `moonshotai/kimi-k3`: `temperature:false`, effort `[low,high,max]`, `limit {1048576/1048576}`, cost `{3,15,cache_read 0.3}`.
  - `zai/glm-5.2`: effort `[high,max]`, `interleaved reasoning_content`, `limit {1000000/131072}`, cost `{1.4,4.4,cache_read 0.26}`; `zai/glm-5.3`: effort `[low,high,max]`.
  - `alibaba/qwen3-coder-plus`: `reasoning:false`, `limit {1048576/65536}`, cost `{1,5}`; `alibaba/qwen3.7-max`: `[toggle, budget_tokens]`.
  - `minimax/MiniMax-M2.7`: `reasoning_options: []`, `limit {204800/131072}`.
  - OpenRouter re-listings differ: `openrouter/moonshotai/kimi-k2.6` has `interleaved:{field:"reasoning_details"}` (OpenRouter's field name), `openrouter/deepseek/deepseek-v4-pro` effort `[high,xhigh]` + `interleaved reasoning_content`, `openrouter/z-ai/glm-5.2` effort `[high,xhigh]`, `openrouter/moonshotai/kimi-k3` `interleaved: null`.
  - `databricks/databricks-glm-5-2`: effort `[low,medium,high]`, `interleaved: null`; `databricks/databricks-kimi-k2-7-code`: `temperature:false`, `limit {262144/262144}`.

**Databricks Unity AI Gateway + OpenCode**
- Databricks docs config (verbatim) — [Connect OpenCode](https://docs.databricks.com/aws/en/ai-gateway/coding-agent-opencode):
```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "databricks-anthropic/system.ai.claude-sonnet-4-6",
  "provider": {
    "databricks-anthropic": {
      "npm": "@ai-sdk/anthropic",
      "options": {
        "baseURL": "https://<workspace-hostname>/ai-gateway/anthropic/v1",
        "apiKey": "<databricks-pat>",
        "headers": { "Authorization": "Bearer <databricks-pat>" }
      },
      "models": { "system.ai.claude-sonnet-4-6": { "options": { "toolStreaming": false } } }
    }
  }
}
```
  The page says to run `ug opencode` (automatic), or merge the JSON into `~/.config/opencode/opencode.json`, keep `"toolStreaming": false` for gateway compatibility, mentions Gemini via `@ai-sdk/google` and cheaper open models such as `"system.ai.glm-5-2"`, and that MCP/skills live at `ai-gateway/mcp-services/` and `ai-gateway/skills/` — [Connect OpenCode](https://docs.databricks.com/aws/en/ai-gateway/coding-agent-opencode).
- ucode source (what `ug opencode` actually writes) — [opencode.py](https://github.com/databricks/ucode/blob/main/src/ucode/agents/opencode.py): three providers, `databricks-anthropic` (`@ai-sdk/anthropic`, base `opencode_base_urls["anthropic"]`, per-model `"options": {"toolStreaming": False}`), `databricks-google` (`@ai-sdk/google`), `databricks-oss` (`"npm": "@ai-sdk/openai"`, base `opencode_base_urls["oss"]`, per-model `limit` from a token-limits table "so OpenCode clamps `max_tokens` to a value the gateway accepts"). Comment in source: "`@ai-sdk/anthropic` injects `eager_input_streaming: true` on tool defs; the Databricks gateway's strict validator rejects it. opencode's auto-disable in transform.ts skips models whose id contains "claude", so we opt out per-model."
- Base URLs — [databricks.py](https://github.com/databricks/ucode/blob/main/src/ucode/databricks.py) lines 3149-3155:
```python
def build_opencode_base_urls(workspace: str) -> dict[str, str]:
    return {
        "anthropic": build_tool_base_url("claude", workspace) + "/v1",   # <ws>/ai-gateway/anthropic/v1
        "gemini": build_tool_base_url("gemini", workspace) + "/v1beta",  # <ws>/ai-gateway/gemini/v1beta
        "oss": f"{workspace}/ai-gateway/mlflow/v1",
    }
```
  and the section header reads `# URL builders (AI Gateway v2 only — no fallback to /serving-endpoints)`.
- Per-model `User-Agent` header trick — ucode comment: "OpenCode hardcodes `User-Agent: opencode/<ver>` in session/llm.ts for every provider … provider-level `headers` are clobbered by that injection, but per-model `headers` are merged AFTER and win" — [opencode.py](https://github.com/databricks/ucode/blob/main/src/ucode/agents/opencode.py). (Confirmed by `request.ts`: headers are built as `{ "x-session-affinity", "X-Session-Id", "User-Agent": USER_AGENT, ...input.model.headers, ...headers }` — [request.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/llm/request.ts).)
- ucode config file locations: OpenCode -> `~/.ucode/opencode-xdg/opencode/opencode.json` and `~/.ucode/opencode-xdg/opencode/plugin/ucode-auth.js` (a generated OpenCode plugin that runs `ug auth-token` and sets `Authorization: Bearer <token>` on each request so OAuth tokens never expire mid-session) — [ucode README](https://github.com/databricks/ucode/blob/main/README.md), [opencode.py](https://github.com/databricks/ucode/blob/main/src/ucode/agents/opencode.py).
- Databricks capability matrix: "OpenCode (CLI)" via `ug opencode`, Databricks models Yes, external providers "—", MCP Yes, Skills Yes — [Supported agents](https://docs.databricks.com/aws/en/ai-gateway/coding-agent-supported-agents).

**OpenRouter specifics in OpenCode**
- Docs example of per-model OpenRouter provider routing (verbatim) — [Providers docs](https://opencode.ai/docs/providers/):
```json
{ "provider": { "openrouter": { "models": { "moonshotai/kimi-k2": {
  "options": { "provider": { "order": ["baseten"], "allow_fallbacks": false } } } } } }
```
- `@openrouter/ai-sdk-provider` README: reasoning via `providerOptions.openrouter.reasoning` (`max_tokens`, `effort`, `exclude`, `enabled`); "Reasoning Details Preservation" replays `reasoning_details` across tool-call turns via `providerMetadata.openrouter.reasoning_details`; usage accounting with `usage: { include: true }` exposing `providerMetadata.openrouter.usage.cost` / `costDetails`; routing prefs `provider.order/allow_fallbacks/only/ignore/sort/data_collection/require_parameters`; `models` fallback arrays; `cacheControl: { type: 'ephemeral' }`; "middle-out" transforms — [OpenRouterTeam/ai-sdk-provider](https://github.com/OpenRouterTeam/ai-sdk-provider).
- OpenCode's native (experimental, `OPENCODE_EXPERIMENTAL_NATIVE_LLM`) OpenRouter provider forwards `usage` and `reasoning` bodies the same way: `...(openrouter.usage === true ? { usage: { include: true } } : ...)`, `...(isRecord(openrouter.reasoning) ? { reasoning: openrouter.reasoning } : {})` — [packages/llm/src/providers/openrouter.ts](https://github.com/anomalyco/opencode/blob/dev/packages/llm/src/providers/openrouter.ts).

### Inferences
- The implementable pattern: a catalog layer (models.dev `api.json` + local override), a "protocol adapter" chosen per provider (chat-completions / Anthropic Messages / OpenRouter-chat), and per-model `options` that are merged in this order: provider defaults -> `model.options` -> `agent.options` -> selected variant (see KQ2 `request.ts`). rolo-claude can copy this merge order verbatim.
- For Databricks, ucode chose `@ai-sdk/openai` (a Responses-API-capable SDK) for OSS models while models.dev says `@ai-sdk/openai-compatible` at the same `/ai-gateway/mlflow/v1` URL. Both cannot be wrong only if the endpoint speaks chat-completions; whether it also speaks the Responses API is unverified. For a Python harness the safe choice is chat-completions at `https://<ws>/ai-gateway/mlflow/v1/chat/completions` with `Authorization: Bearer <PAT or OAuth token>`, and the Anthropic Messages dialect at `/ai-gateway/anthropic/v1` for Claude.
- `toolStreaming: false` on Databricks maps to disabling `eager_input_streaming`/fine-grained tool streaming; a Python harness that never sends the `fine-grained-tool-streaming` beta is unaffected.

### Gaps
- The Databricks docs pages fetched do not list the full `system.ai.*` open-model catalogue (only `system.ai.claude-sonnet-4-6` and `system.ai.glm-5-2` are named); the ucode CLI discovers models dynamically via `/api/ai-gateway/v2/...` APIs.
- No fetched source documents an OpenCode config against the legacy `/serving-endpoints` Foundation Model API; ucode explicitly refuses to fall back to it.
- I did not fetch the `@ai-sdk/openai-compatible` SDK source to confirm exactly how message-level `providerOptions.openaiCompatible.<field>` is serialised onto the wire (see KQ2).

---

## KQ2. Per-provider/model transforms and robustness machinery (sampling defaults, providerOptions, schema sanitising, output caps, reasoning replay, tool-call repair, doom loops, retries, timeouts, streaming)

### Takeaway
All model-family quirks are concentrated in `provider/transform.ts` (sampling defaults keyed on model-id substrings; `variants()`/`options()` producing SDK `providerOptions`; `normalizeMessages()` fixing tool-call IDs, injecting empty DeepSeek reasoning, and moving reasoning into the `interleaved` field; per-provider JSON-schema sanitising; a hard `OUTPUT_TOKEN_MAX = 32_000`), plus three loop guards in `session/*`: `experimental_repairToolCall` -> an `invalid` tool that feeds the error back to the model, a `doom_loop` permission after 3 identical consecutive calls, and an Effect retry policy (max 5, 2 s base, x2, 25 % jitter, honours `retry-after`) that re-runs the whole stream on 429/5xx/network errors.

### Cited Findings

**Where params are assembled** — [session/llm/request.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/llm/request.ts):
```ts
const base = input.small
  ? ProviderTransform.smallOptions(input.model)
  : ProviderTransform.options({ model: input.model, sessionID: input.sessionID, providerOptions: input.provider.options })
const options = mergeOptions(mergeOptions(mergeOptions(base, input.model.options), input.agent.options), variant)
...
temperature: input.model.capabilities.temperature
  ? (input.agent.temperature ?? ProviderTransform.temperature(input.model))
  : undefined,
topP: input.agent.topP ?? ProviderTransform.topP(input.model),
topK: ProviderTransform.topK(input.model),
maxOutputTokens: ProviderTransform.maxOutputTokens(input.model, input.flags.outputTokenMax),
```
  Plugins can override via the `chat.params` and `chat.headers` hooks; tools are sorted alphabetically (`toSorted`) for cache stability; OpenAI Responses-family SDKs get `strict: false` on every tool ("Codex parity") — same file.

**Sampling defaults per family** — [transform.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts):
```ts
export function temperature(model: Provider.Model) {
  const id = model.api.id.toLowerCase()
  if (id.includes("north-mini-code")) return 1.0
  if (id.includes("claude")) return undefined
  if (id.includes("gemini")) return GEMINI_MODELS_WITH_SAMPLING_DEFAULTS.some((model) => model.test(id)) ? 1.0 : undefined
  if (id.includes("glm-4.6")) return 1.0
  if (id.includes("glm-4.7")) return 1.0
  if (id.includes("minimax-m2")) return 1.0
  if (id.includes("kimi-k2")) {
    if (["thinking", "k2.", "k2p", "k2-5"].some((s) => id.includes(s))) return 1.0
    return 0.6
  }
  return undefined
}
```
  `topP`: Gemini 2.5/3 -> 0.95; `minimax-m2`, `kimi-k2.5/k2p5/k2-5` -> 0.95; `deepseek-v4-flash` (on `deepseek` or `opencode*` providers, or the `0731` snapshot) -> 0.95. `topK`: `minimax-m2` -> 40 for m2.x/m25/m21 else 20; Gemini 2.5/3 -> 64. There is NO Qwen-specific temperature (the "0.55" figure in the brief does not appear anywhere in transform.ts); Qwen falls through to `undefined` (provider default). Note `temperature()` is only applied when `model.capabilities.temperature` is true (models.dev `temperature` flag; e.g. `kimi-k3` is `temperature:false`).

**`options()` (default providerOptions by provider/family)** — [transform.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts):
- `toolStreaming = false` for `@ai-sdk/google-vertex/anthropic` and for any `@ai-sdk/anthropic` model whose id lacks "claude" (i.e. MiniMax/Kimi over Anthropic dialect).
- `store = false` for openai / `@ai-sdk/openai` / copilot / bedrock-mantle / xai / azure.
- OpenRouter (and llmgateway): `result["usage"] = { include: true }`; non-legacy Gemini via OpenRouter gets `reasoning: { effort: "high" }`.
- `chat_template_args: { enable_thinking: true }` for baseten and opencode-hosted `kimi-k2-thinking`/`glm-4.6`.
- Z.ai over openai-compatible: `result["thinking"] = { type: "enabled", clear_thinking: false }` (providerID contains `zai`/`zhipuai`).
- Google: `thinkingConfig: { includeThoughts: true, thinkingLevel: "high" }` for reasoning models.
- MiniMax-M3 over Anthropic dialect: `thinking: { type: "adaptive" }`; Kimi family over Anthropic dialect: `thinking: { type: "adaptive", display: "summarized" }, effort: "high"`.
- `alibaba-cn` (DashScope) reasoning models over openai-compatible: `enable_thinking = true` (comment: "DashScope's OpenAI-compatible API requires `enable_thinking: true` in the request body to return reasoning_content … kimi-k2-thinking is excluded as it returns reasoning_content by default").
- Prompt-cache keys: `prompt_cache_key = sessionID` for deepinfra/cerebras; `promptCacheKey = sessionID` for openai/azure/xai/mistral/venice or when provider `setCacheKey: true`.
- GPT-5 family: `reasoningEffort: "medium"`, `reasoningSummary: "auto"`, `include: ["reasoning.encrypted_content"]`, `textVerbosity: "low"` (only for `@ai-sdk/openai`/mantle).
- `smallOptions()` (title/summary calls): first variant if any; for OpenRouter/llmgateway Google models with no variants -> `{ reasoning: { enabled: false } }`; venice -> `disableThinking`.

**`providerOptions()` key routing** — `sdkKey(npm)` maps `@ai-sdk/openai`->`openai`, `@ai-sdk/anthropic`->`anthropic`, `@openrouter/ai-sdk-provider`->`openrouter`, `@ai-sdk/alibaba`->`alibaba`, … ; for `@ai-sdk/openai-compatible` (no entry) the key is `model.providerID.split(".")[0]`, i.e. the provider id you configured (because the SDK was created with `name: providerID`) — [transform.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts).

**Reasoning variants (`variants()` + models.dev `reasoning_options`)** — [transform.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts):
- Hard-coded: returns `{}` (no variants) for `deepseek-chat|deepseek-reasoner|deepseek-r1|deepseek-v3|minimax|glm (non-5.2)|kimi|k2p|qwen|big-pickle`; GLM-5.2 gets `high/xhigh` (`reasoning.effort`) on OpenRouter, `high/max` (`reasoningEffort`) on openai-compatible, `high/max` (`effort`) on Anthropic dialect; `@ai-sdk/openai-compatible` generic -> `low/medium/high` (+`max` if id contains `deepseek-v4`) as `reasoningEffort`; OpenRouter generic -> `low/medium/high` as `{ reasoning: { effort } }`; Anthropic -> adaptive `effort` (`low/medium/high/xhigh/max` for Claude 4.7+) or `thinking.budgetTokens` (`high` = `min(16_000, floor(limit.output/2 - 1))`, `max` = `min(31_999, limit.output - 1)`).
- Catalog-driven: `reasoningVariants()` turns models.dev `reasoning_options` into variants — `effort` values map per SDK (`reasoningEffort` for openai-compatible/xai/mistral/groq/…; `{reasoning:{effort}}` for OpenRouter; `thinking.type: adaptive` + `effort` for Anthropic); `budget_tokens` map to `{reasoning:{max_tokens}}` (OpenRouter), `thinking.budgetTokens` (Anthropic), `thinkingConfig.thinkingBudget` (Google), `enableThinking + thinkingBudget` (`@ai-sdk/alibaba`); `toggle` maps to `enableThinking: false/true` only for `@ai-sdk/alibaba` and `thinking.type` for cohere (else no variant). Budget maths:
```ts
const maximum = Math.min(max ?? OUTPUT_TOKEN_MAX - 1, model.limit.output - 1, OUTPUT_TOKEN_MAX - 1)
const high = Math.min(Math.max(min ?? 0, Math.floor((maximum + 1) / 2)), maximum)
```

**Max output tokens (the 32k cap)** — [transform.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts):
```ts
export const OUTPUT_TOKEN_MAX = 32_000
...
export function maxOutputTokens(model: Provider.Model, outputTokenMax = OUTPUT_TOKEN_MAX): number {
  return Math.min(model.limit.output, outputTokenMax) || outputTokenMax
}
```
  Override: env `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX` (positive integer) — [runtime-flags.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/effect/runtime-flags.ts). Issue #18108 documents the consequences: Opus 128k output capped to 32k, large `write()` tool JSON truncated mid-call, "Truncated JSON misclassified as invalid tool calls", "`finishReason: "length"` treated as normal completion", doom-loop detection fails on truncation; proposed fixes (detect truncation, auto-continue on length, coordinate thinking budget, raise to 64k); no resolution PR linked — [#18108](https://github.com/anomalyco/opencode/issues/18108).

**`finish_reason` handling / empty responses** — [prompt.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/prompt.ts) loop:
```ts
// Some providers return "stop" even when the assistant message contains
// tool calls. Keep the loop running so tool results can be sent back to
// the model, but ignore cleanup-marked interrupted orphans.
const hasToolCalls = lastAssistantMsg?.parts.some((part) => part.type === "tool" && ...) ?? false
if (lastAssistant?.finish && !["tool-calls", "unknown"].includes(lastAssistant.finish) && !hasToolCalls && lastAssistant.parentID === lastUser.id) {
  ... break
}
```
  `content-filter` finishes are surfaced as `ContentFilterError`; a `length` finish exits the loop like `stop` (no auto-continue) — [prompt.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/prompt.ts) lines ~1094-1130, 1295-1312. The AI SDK adapter converts `rawFinishReason === "network_error"` into a retryable `ResponseStreamError` — [ai-sdk.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/llm/ai-sdk.ts). Interrupted/orphaned tool parts are rewritten as `state: "output-error", errorText: "[Tool execution was interrupted]"` on replay — [message-v2.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/message-v2.ts).

**Tool-call repair and the `invalid` tool** — [llm.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/llm.ts):
```ts
async experimental_repairToolCall(failed) {
  const lower = failed.toolCall.toolName.toLowerCase()
  if (lower !== failed.toolCall.toolName && prepared.tools[lower]) {
    return { ...failed.toolCall, toolName: lower }
  }
  return {
    ...failed.toolCall,
    input: JSON.stringify({ tool: failed.toolCall.toolName, error: failed.error.message }),
    toolName: "invalid",
  }
},
...
activeTools: Object.keys(prepared.tools).filter((x) => x !== "invalid"),
maxRetries: input.retries ?? 0,
```
  and [tool/invalid.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/tool/invalid.ts): description `"Do not use"`, execute returns `output: \`The arguments provided to the tool are invalid: ${params.error}\``. So a malformed or unknown call becomes a tool result the model sees on the next turn instead of a crash; the SDK's own retries are disabled (`maxRetries: 0`; title generation passes `retries: 2`).

**Doom-loop detection** — [processor.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/processor.ts):
```ts
const DOOM_LOOP_THRESHOLD = 3
...
const parts = yield* MessageV2.parts(ctx.assistantMessage.id)...
const recentParts = parts.slice(-DOOM_LOOP_THRESHOLD)
if (recentParts.length !== DOOM_LOOP_THRESHOLD ||
    !recentParts.every((part) => part.type === "tool" && part.tool === value.name &&
      part.state.status !== "pending" && JSON.stringify(part.state.input) === JSON.stringify(input))) { return }
const agent = yield* agents.get(ctx.assistantMessage.agent)
yield* permission.ask({ permission: "doom_loop", patterns: [value.name], ..., metadata: { tool: value.name, input }, always: [value.name], ruleset: agent.permission })
```
  Docs: `doom_loop` "triggered when the same tool call repeats 3 times with identical input", default action `"ask"`, configurable via `"permission": { "doom_loop": "allow" }` — [Permissions docs](https://opencode.ai/docs/permissions/). Known weaknesses: only the current assistant message is inspected and `slice` precedes `filter`, so repeats across turns or with an interleaved text part evade it (PR #32089 closed) — [#25254](https://github.com/anomalyco/opencode/issues/25254); alternating A,B,A,B cycles are missed — [#47759](https://github.com/anomalyco/opencode/issues/47759); threshold not configurable — [#23531](https://github.com/anomalyco/opencode/issues/23531); a subagent on `deepseek-v4-flash` (OpenAI-compatible provider) ran 364 identical `grep` calls for ~50 min (~184k input / 40k output / 142M cache-read tokens) with "no repeated-action detection, no step limit, no heartbeat timeout" — open, related PR #46272 — [#45442](https://github.com/anomalyco/opencode/issues/45442). A PR to add a guard for repeated reasoning/output text also exists — [PR #12623](https://github.com/anomalyco/opencode/pull/12623). Agents can cap steps: `const maxSteps = agent.steps ?? Infinity` and a `MAX_STEPS_PROMPT` assistant message is appended on the last step — [prompt.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/prompt.ts).

**Retries / backoff** — [retry.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/retry.ts):
```ts
export const RETRY_INITIAL_DELAY = 2000
export const RETRY_BACKOFF_FACTOR = 2
export const RETRY_JITTER_FACTOR = 0.25
export const RETRY_MAX_DELAY_NO_HEADERS = 30_000 // 30 seconds
export const RETRY_MAX_DELAY = 2_147_483_647
export const RETRY_MAX_RETRIES = 5
const RETRYABLE_MESSAGE_PATTERNS = [
  /429|500|502|503|504|524/i,
  /rate increased too quickly|rate limit|rate-limit|rate_limit|too many requests/i,
  /overloaded|service unavailable|...|provider returned error|provider_returned_error|.../i,
  /terminated|fetch failed|...|econnrefused|econnreset|etimedout/i,
  /^timeout$|\b(?:request|response|connection|network|stream|read) (?:timeout|timed out|time out)\b/i,
  /try your request again|retry your request|resource exhausted|resource_exhausted/i,
  /\btry again (?:later|in\b)|\b(?:currently|temporarily) at capacity\b/i,
]
```
  `delay()` prefers `retry-after-ms`, then `retry-after` (seconds or HTTP-date), else exponential `base + base*0.25*random` capped at 30 s when no headers; `retryable()` never retries `ContextOverflowError`, always retries status >= 500 or `isRetryable`, and also matches the message/body patterns; the policy is applied with `Effect.retry(SessionRetry.policy(...))` around the entire stream in `processor.process`, publishing `status: "retry"` with `attempt/message/next` for the UI — [retry.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/retry.ts), [processor.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/processor.ts). OpenAI 404s are treated as retryable ("openai sometimes returns 404 for models that are actually available") — [provider/error.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/error.ts).

**Context-overflow detection from provider errors** — [packages/llm/src/provider-error.ts](https://github.com/anomalyco/opencode/blob/dev/packages/llm/src/provider-error.ts): a list of ~27 regexes (`/prompt is too long/i`, `/request_too_large/i`, `/exceeds the context window/i`, `/maximum context length is \d+ tokens/i`, `/context[_ ]length[_ ]exceeded/i`, `/too many tokens/i`, `/token limit exceeded/i`, `/model_context_window_exceeded/i`, …) with exclusions `/rate limit/i`, `/too many requests/i`, `/^(throttling error|service unavailable):/i`; plus HTTP 413 or `body.error.code === "context_length_exceeded"` — [provider/error.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/error.ts). Comment in the same file: "Providers not reliably handled in this function: - z.ai: can accept overflow silently (needs token-count/context-window checks)".

**Message normalisation (`normalizeMessages`)** — [transform.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts):
- All text sanitised for lone surrogates (`sanitizeSurrogates`).
- Anthropic/Bedrock: drop empty messages and empty/unsigned reasoning parts.
- Claude: tool-call IDs scrubbed to `[a-zA-Z0-9_-]`; Mistral family: IDs -> 9 alphanumerics, and an assistant `"Done."` message is inserted when a tool message is followed by a user message.
- DeepSeek: `// Deepseek requires all assistant messages to have reasoning on them` -> appends `{ type: "reasoning", text: "" }` to any assistant message lacking one.
- Interleaved field replay (all OpenAI-compatible providers except the OpenRouter SDK, which handles `reasoning_details` itself):
```ts
if (typeof model.capabilities.interleaved === "object" && model.capabilities.interleaved.field &&
    model.api.npm !== "@openrouter/ai-sdk-provider") {
  const field = model.capabilities.interleaved.field
  return msgs.map((msg) => {
    if (msg.role === "assistant" && Array.isArray(msg.content)) {
      const reasoningText = msg.content.filter((p: any) => p.type === "reasoning").map((p: any) => p.text).join("")
      const filteredContent = msg.content.filter((p: any) => p.type !== "reasoning")
      // Always set the field even when empty — some providers (e.g. DeepSeek) may return empty
      // reasoning_content which still needs to be sent back in subsequent requests.
      return { ...msg, content: filteredContent,
        providerOptions: { ...msg.providerOptions, openaiCompatible: { ...msg.providerOptions?.openaiCompatible, [field]: reasoningText } } }
    }
    return msg
  })
}
```
- Cross-model replay: when history was produced by a different `provider/model`, reasoning parts become plain text parts and `providerMetadata` (signatures) is dropped — [message-v2.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/message-v2.ts) (`differentModel`).
- Reasoning capture during streaming: `reasoning-start/delta/end` events keep `providerMetadata` on the reasoning part (`ctx.reasoningMap[value.id].metadata = value.providerMetadata`) — [processor.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/processor.ts).

**Prompt caching (`applyCaching`)** — applied for Anthropic-ish models (`providerID === "anthropic"`, ids containing `anthropic`/`claude`, `@ai-sdk/anthropic`, `@ai-sdk/alibaba`) and NOT for `@ai-sdk/gateway`: cache breakpoints on the first two system messages and the last two non-system messages, with provider-specific shapes (`anthropic.cacheControl`, `openrouter.cacheControl`, `bedrock.cachePoint`, `openaiCompatible.cache_control`, `copilot.copilot_cache_control`, `alibaba.cacheControl`); message-level for anthropic/bedrock, else on the last content part — [transform.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts).

**Tool-schema sanitising (`schema()`)** — [transform.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts):
- OpenAI/Azure: `sanitizeOpenAISchema` ("Mirrors Codex's Rust JSON schema compatibility lowering") — boolean schemas -> `{type:"string"}`, `const` -> `enum`, keeps only `$ref/description/enum/properties/required/items/additionalProperties/anyOf/oneOf/allOf/$defs/definitions/type`, infers a type when missing, adds `properties: {}` / `items: {type:"string"}` defaults.
- Moonshot/Kimi (`providerID === "moonshotai"` or id contains `kimi`): "Moonshot expands $ref before validation and rejects sibling keywords like description on the same node" -> `$ref` nodes stripped to `{ $ref }`; "MFJS does not support tuple-style `items` arrays" -> `items[0]`.
- Google/Gemini: integer enums -> string enums (and type -> string), type arrays split into `anyOf` + `nullable`, `required` filtered to existing properties, `items` defaulted, `properties/required` removed from non-object types. (No `$schema` stripping appears in this function.)

**Timeouts** — `chunkTimeout`/`headerTimeout` default 300 000 ms, `timeout` optional, OpenAI `headerTimeout` default 300 000 — [provider.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/provider.ts) lines 35, 1799-1828; `HeaderTimeoutError`/`ResponseStreamError` are classified `isRetryable: true` — [message-v2.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/message-v2.ts) `fromError`.

**Streaming aggregation** — AI SDK `fullStream` events are mapped 1:1 to `LLMEvent`s (`stepStart/stepFinish/finish/text*/reasoning*/toolInput*/toolCall/toolResult/toolError`), orphan reasoning deltas are silently dropped, usage is normalised (`reasoningTokens` from `outputTokenDetails`, cache read/write from `inputTokenDetails`), and state is reset after `finish` — [ai-sdk.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/llm/ai-sdk.ts), [processor.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/processor.ts). The stream is cut immediately when overflow is detected at `step-finish` (`Stream.takeUntil(() => ctx.needsCompaction)`) — [processor.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/processor.ts).

**SDK patches shipped** — `patches/` contains `@ai-sdk/openai-compatible@2.0.41.patch` (only change: forward the full `chunk.value.error` object instead of `.message` on streamed error chunks), plus patches for anthropic, openai, google, groq, mistral, xai, amazon-bedrock — [patches/](https://github.com/anomalyco/opencode/tree/dev/patches).

**Per-family system prompts** — `SystemPrompt.provider()` picks `kimi.txt` when the id contains `kimi` or providerID is `kimi-for-coding`/`moonshotai`/`moonshotai-cn`; Claude -> `anthropic.txt`, Gemini -> `gemini.txt`, GPT -> `gpt.txt`/`codex.txt`/`beast.txt`, everything else (DeepSeek/GLM/Qwen/MiniMax) -> `default.txt` — [system.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/system.ts). `kimi.txt` tells the model: "When calling tools, do not provide explanations because the tool calls themselves should be self-explanatory. You MUST follow the description of each tool and its parameters when calling tools." and "If you anticipate making multiple non-interfering tool calls, you are HIGHLY RECOMMENDED to make them in parallel" — [kimi.txt](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/prompt/kimi.txt).

### Inferences
- The `providerOptions.openaiCompatible[field]` trick relies on `@ai-sdk/openai-compatible` spreading message-level `providerOptions.openaiCompatible` into the outgoing JSON message (so `reasoning_content` rides on the assistant message). I did not fetch the SDK source to confirm; for a Python harness the equivalent is simply: store the assistant's `reasoning_content` verbatim and re-send it on every assistant message that carries `tool_calls` (DeepSeek/Kimi/GLM all require it), sending `""` rather than omitting it for DeepSeek.
- OpenCode has no automatic continuation on `finish_reason: "length"`; a harness that wants robustness with 32k-capped open models should add one (issue #18108 lists exactly this).
- The `invalid` tool pattern is cheap and effective: register a hidden tool that echoes the parse error; route every failed parse/unknown name there; never expose it in `tools` offered to the model as callable (OpenCode hides it via `activeTools`).
- Because the doom-loop guard only looks at the current assistant message, rolo-claude should hash `(tool, canonical-JSON args)` across the whole turn since the last user message and also detect period-2 cycles, which the OpenCode issues show are the real failure modes with DeepSeek-class models.

### Gaps
- No PR/commit was found that fixes the DeepSeek/Kimi `reasoning_content` drop on tool-call turns: #24722 and #29619 are "closed, not planned", #35689 is open (PR #28352 unmerged) even though `normalizeMessages` already sets the field. Whether current `dev` actually reproduces the bug is unverified.
- `finish_reason: length` auto-continue, empty-response retry and a "headed summary retry" do not exist in the fetched code; if the brief's v2 design mentions them, they are not in `dev` as of 2026-09-24.
- The Gemini `$schema` stripping mentioned in the brief is not in `schema()`; it may live in `packages/llm/src/protocols/utils/gemini-tool-schema.ts` (native runtime), which I did not read.

---

## KQ3. Context management: compaction trigger, prune, summaries, token counting, caching order, cost accounting

### Takeaway
Overflow is checked after every step from provider-reported usage: `count >= usable`, where `usable = limit.input - reserved` (reserved = `compaction.reserved` or `min(20_000, maxOutputTokens)`) when the model declares an input limit, else `context - maxOutputTokens`. Compaction (a) optionally prunes old tool outputs beyond a 40k-token protected tail if it frees > 20k, then (b) keeps a verbatim tail of recent turns within `min(15k, max(2k, 25 % of usable))` tokens and asks the model (the `compaction` agent's model or the session model) for a fixed headed Markdown summary (Objective / Important Details / Work State / Next Move / Relevant Files); token counts are estimated as `chars/4`; cost is computed locally from models.dev prices with reasoning billed at the output rate, ignoring OpenRouter's reported `usage.cost`.

### Cited Findings

**Overflow formula** — [overflow.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/overflow.ts) (entire logic):
```ts
const COMPACTION_BUFFER = 20_000
export function usable(input) {
  const context = input.model.limit.context
  if (context === 0) return 0
  const reserved = input.cfg.compaction?.reserved ??
    Math.min(COMPACTION_BUFFER, ProviderTransform.maxOutputTokens(input.model, input.outputTokenMax))
  return input.model.limit.input
    ? Math.max(0, input.model.limit.input - reserved)
    : Math.max(0, context - ProviderTransform.maxOutputTokens(input.model, input.outputTokenMax))
}
export function isOverflow(input) {
  if (input.cfg.compaction?.auto === false) return false
  if (input.model.limit.context === 0) return false
  const count = input.tokens.total || input.tokens.input + input.tokens.output + input.tokens.cache.read + input.tokens.cache.write
  return count >= usable(input)
}
```
- The v2 core package has a second, request-side check: compact when `estimate({system, messages, tools}) > context - Math.max(output, buffer)` with `DEFAULT_BUFFER = 20_000`, `DEFAULT_KEEP_TOKENS = 8_000`, `SUMMARY_OUTPUT_TOKENS = 4_096`, and it refuses if `Token.estimate(summaryPrompt) > context - summaryOutput` — [packages/core/src/session/compaction.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/compaction.ts).
- Trigger points: after each `step-finish` (`isOverflow(...) -> ctx.needsCompaction = true`) and when a provider error is classified `ContextOverflowError` (unless `compaction.auto === false`) — [processor.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/processor.ts); the loop then calls `compaction.create({ ..., auto: true, overflow: !handle.message.finish })` and processes it on the next iteration — [prompt.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/prompt.ts).

**Prune** — [compaction.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/compaction.ts):
```ts
export const PRUNE_MINIMUM = 20_000
export const PRUNE_PROTECT = 40_000
const TOOL_OUTPUT_MAX_CHARS = 2_000
const PRUNE_PROTECTED_TOOLS = ["skill"]
const MIN_PRESERVE_RECENT_TOKENS = 2_000
const MAX_PRESERVE_RECENT_TOKENS = 15_000
```
  `prune()` runs only if `cfg.compaction.prune` is true (docs default: false), is forked after each loop exit, walks messages backwards, skips the two most recent user turns, stops at a prior summary or an already-compacted part, protects the newest `PRUNE_PROTECT` tokens of completed tool output, and if the remainder exceeds `PRUNE_MINIMUM` stamps `part.state.time.compacted = Date.now()`; on replay those outputs render as `"[Old tool result content cleared]"` — [compaction.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/compaction.ts), [message-v2.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/message-v2.ts). Env kill-switch `OPENCODE_DISABLE_PRUNE` forces `prune: false` — [config.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/config/config.ts) line ~596.

**Tail preservation & summary** — [compaction.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/compaction.ts):
```ts
function preserveRecentBudget(input) {
  return input.cfg.compaction?.preserve_recent_tokens ??
    Math.min(MAX_PRESERVE_RECENT_TOKENS, Math.max(MIN_PRESERVE_RECENT_TOKENS, Math.floor(usable(input) * 0.25)))
}
```
  `select()` walks user turns from newest to oldest (optionally capped by `compaction.tail_turns`), keeps whole turns while they fit, and can split the boundary turn; the "head" is serialised (`[User]: …`, `[Assistant]: …`, `[Assistant reasoning]: …`, `[Assistant tool call]: name({...})`, `[Tool result]: <truncated to 2 000 chars + "\n[truncated]">`) and sent, with `tools: {}` and `system: []`, to the `compaction` agent's model (else the session model); a tool call during summary throws `"Tool call not allowed while generating summary"` — same file and [processor.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/processor.ts).
- Summary template (verbatim headings) — [packages/core/src/session/compaction.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/compaction.ts): `## Objective`, `## Important Details`, `## Work State` (`### Completed`, `### Active`, `### Blocked`), `## Next Move`, `## Relevant Files`; rules: "Keep every section, even when empty", "Preserve exact file paths, symbols, commands, error strings, URLs, and identifiers", "Do not mention the summary process or that context was compacted". With a prior summary, `SUMMARY_UPDATE_INSTRUCTIONS` says "The <prior-summary> is discarded after this: anything you do not carry into the new summary is lost … Where they conflict, the conversation wins".
- Post-compaction: if the trigger was an overflow error, the last real user message is replayed with media attachments replaced by `[Attached <mime>: <name>]`; otherwise a synthetic user message `"Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed."` (tagged `metadata: { compaction_continue: true }`) is appended, gated by plugin hook `experimental.compaction.autocontinue`; if the summary itself overflows, the session stops with `ContextOverflowError("Conversation history too large to compact - exceeds model context limit")` — [compaction.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/compaction.ts).
- Manual: `/compact` command; config `compaction: { auto, prune, reserved }` documented — [Config docs](https://opencode.ai/docs/config/); a third-party explainer confirms "Prune … retaining the most recent 40,000 tokens … only executes pruning when it can free more than 20,000 tokens" — [justin3go.com](https://justin3go.com/en/posts/2026/04/09-context-compaction-in-codex-claude-code-and-opencode).

**Token counting** — `const CHARS_PER_TOKEN = 4; export const estimate = (input: string) => Math.max(0, Math.round(input.length / CHARS_PER_TOKEN))` — [packages/core/src/util/token.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/util/token.ts); compaction estimates `Token.estimate(JSON.stringify(modelMessages))`; live overflow checks use provider-reported usage, not estimates — [compaction.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/compaction.ts), [overflow.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/overflow.ts).

**Caching-aware ordering** — system prompt is collapsed to at most two system messages (`header` + rest) so the two cache breakpoints cover it; tools are name-sorted; cache breakpoints go on the first two system and last two messages (KQ2) — [request.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/llm/request.ts), [transform.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts).

**Cost accounting** — [session.ts `getUsage`](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/session.ts):
```ts
const adjustedInputTokens = safe(inputTokens - cacheReadInputTokens - cacheWriteInputTokens)
...
cost: ... safe(new Decimal(0)
  .add(new Decimal(tokens.input).mul(finite(costInfo?.input ?? 0)).div(1_000_000))
  .add(new Decimal(tokens.output).mul(finite(costInfo?.output ?? 0)).div(1_000_000))
  .add(new Decimal(tokens.cache.read).mul(finite(costInfo?.cache?.read ?? 0)).div(1_000_000))
  .add(new Decimal(tokens.cache.write).mul(finite(costInfo?.cache?.write ?? 0)).div(1_000_000))
  // TODO: update models.dev to have better pricing model, for now:
  // charge reasoning tokens at the same rate as output tokens
  .add(new Decimal(tokens.reasoning).mul(finite(costInfo?.output ?? 0)).div(1_000_000))
  .toNumber()),
```
  Tiered pricing: picks `model.cost.tiers` with `tier.type === "context"` by input size, else `experimentalOver200K` when `inputTokens > 200_000`; the only provider-reported cost used is GitHub Copilot's `totalNanoAiu / 1e11`. Cost and tokens are accumulated per assistant message at each `step-finish` (`ctx.assistantMessage.cost += usage.cost`) — [processor.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/processor.ts).
- OpenRouter: OpenCode requests `usage: { include: true }` (KQ2) but `getUsage` does not read `providerMetadata.openrouter.usage.cost`; the OpenRouter SDK exposes it as `result.providerMetadata?.openrouter?.usage.cost` — [OpenRouterTeam/ai-sdk-provider](https://github.com/OpenRouterTeam/ai-sdk-provider).
- `/cost`-style reporting: no built-in `/usage` or `/cost` command; feature request "[FEATURE]: /usage — dedicated session token & cost report (alias /cost)" is open — [#41915](https://github.com/anomalyco/opencode/issues/41915); related "[FEATURE]: Display token usage information in the TUI" [#13003](https://github.com/anomalyco/opencode/issues/13003), "Add /context command" [#10575](https://github.com/anomalyco/opencode/issues/10575), a v2 TUI bug "token, cost, and usage information does not display correctly" [#35463](https://github.com/anomalyco/opencode/issues/35463); community plugins fill the gap ([opencode-usage-panel](https://github.com/shiv-source/opencode-usage-panel), [opencode-tokenmeter](https://github.com/jonasotoaguilar/opencode-tokenmeter), [opencode-tui-token-usage](https://github.com/welium/opencode-tui-token-usage)).

### Inferences
- The brief's formula "min(input_limit − 20k, ctx − max(output_reserve, 20k))" is a blend of the two code paths above: v1 `overflow.ts` uses `limit.input − reserved` OR `context − maxOutputTokens`; v2 core uses `context − max(output, 20k)`. For rolo-claude, implement: `usable = (limit.input or limit.context) − max(min(20k, max_output), configured_reserve)` and compact when reported `prompt_tokens + completion_tokens (+cache)` of the last step >= usable.
- Using provider-reported usage (not estimates) for the trigger is the key design choice; the `chars/4` estimator is used only where no usage exists (tail selection, prune sizing). With OpenRouter/Databricks, `usage.prompt_tokens` is available on every response, so the same approach works.
- Because reasoning is billed at the output rate and OpenRouter's authoritative `usage.cost` is ignored, OpenCode's displayed cost for DeepSeek/GLM via OpenRouter can drift from the invoice; a harness should prefer `usage.cost` when present and fall back to models.dev pricing.

### Gaps
- No source found for a "headed summary retry" (re-asking for the summary if the headings are missing); the code only validates non-empty text.
- The docs page for compaction was not fetched as a standalone page (config docs cover `compaction.*`); exact default of `tail_turns` is "unbounded" per the schema description, but I saw no docs prose for `tail_turns`/`preserve_recent_tokens`.

---

## KQ4. Model selection UX: `/models`, `--model`, variants, `small_model`, fallbacks

### Takeaway
Selection is `provider/model` everywhere (`--model`/`-m`, `model` config, `/models` picker), with priority CLI flag -> config -> last-used (`~/.local/state/.../model.json` recents) -> first model by an internal family priority; reasoning "variants" (`high`/`max`, effort names) are per-model option bundles cycled with the `variant_cycle` keybind; `small_model` (or per-provider family heuristics: gemini-flash > gpt-nano > claude-haiku) is used for titles; there is no built-in model fallback (several open issues, one PR proposing a per-model `fallback: []` list, and a community plugin).

### Cited Findings
- `/models` interactive picker; `--model`/`-m` in `provider_id/model_id` form (e.g. `opencode --model anthropic/claude-sonnet-4-20250514`); config `"model": "lmstudio/google/gemma-3n-e4b"`; loading priority "1. `--model` command line flag 2. `model` setting in config file 3. Last used model 4. First model by internal priority" — [Models docs](https://opencode.ai/docs/models/).
- Built-in variants by provider: Anthropic `high` (default) and `max`; OpenAI `none/minimal/low/medium/high/xhigh`; Google `low`/`high`; custom overrides like `"reasoningEffort": "high"` or `"thinking": {"type": "enabled", "budgetTokens": 16000}`; `variant_cycle` keybind — [Models docs](https://opencode.ai/docs/models/). Variant selection is stored per user message (`input.user.model.variant`) and skipped for `small` calls — [request.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/llm/request.ts).
- Community config for reasoning effort on OpenRouter Kimi (`"variants": { "thinking": { "reasoningEffort": "high", "textVerbosity": "low" } }`, with `maxTokens: 16384`, `temperature: 1.0`) — [gist lkoelman](https://gist.github.com/lkoelman/044ffe43256241e4869794a376257018); a bug that model `options` (reasoning/thinking) were not forwarded for `@ai-sdk/openai-compatible` in headless mode — [#27361](https://github.com/anomalyco/opencode/issues/27361).
- Default model ordering: `const priority = ["gpt-5", "claude-sonnet-4", "big-pickle", "gemini-3-pro"]`; small-model families `const smallModelFamilyPriority = ["gemini-flash", "gpt-nano", "claude-haiku"]` (opencode-hosted -> `gpt-nano`; copilot -> `gpt-mini` first); `getSmallModel` honours `cfg.small_model` first, then the `experimental.provider.small_model` plugin hook, then the family list sorted by `release_date` desc — [provider.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/provider.ts) lines 1939-2065.
- Title generation uses the `title` agent's model, else the small model, else the session model, with `small: true`, `retries: 2`, no tools — [prompt.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/prompt.ts) lines ~217-240; compaction uses the `compaction` agent's model or the session model (KQ3).
- Per-agent models: `"agent": { "code-reviewer": { "model": "anthropic/claude-sonnet-4-5" } }` — [Config docs](https://opencode.ai/docs/config/).
- Recommended models per docs: GPT 5.2, GPT 5.1 Codex, Claude Opus 4.5, Claude Sonnet 4.5, Minimax M2.1, Gemini 3 Pro — [Models docs](https://opencode.ai/docs/models/).
- Fallbacks: not implemented; requests [#7602](https://github.com/anomalyco/opencode/issues/7602), [#8687](https://github.com/anomalyco/opencode/issues/8687), [#20100](https://github.com/anomalyco/opencode/issues/20100), [#25150](https://github.com/anomalyco/opencode/issues/25150), [#48991](https://github.com/anomalyco/opencode/issues/48991); PR "feat(provider): configurable model fallback on transient errors and timeouts" proposes `"models": { "claude-sonnet-4": { "fallback": ["openai/gpt-5", "google/gemini-3-pro"] } }`, switching after the retry policy is exhausted on 429/5xx/404/timeout — [PR #49125](https://github.com/anomalyco/opencode/pull/49125); community plugin [renjfk/opencode-model-fallback](https://github.com/renjfk/opencode-model-fallback).

### Inferences
- rolo-claude should store the selected variant with each user turn (as OpenCode does) so replays and compaction use the same reasoning setting, and treat `small_model` as a separate, cheaper route for titles/summaries with reasoning disabled (OpenCode's `smallOptions` disables reasoning for OpenRouter Google models and uses the first variant otherwise).
- Fallback should sit *after* the retry policy, keyed on the same retryable classification, and re-run the whole step (that is what PR #49125 proposes and what the processor's retry wrapper already does).

### Gaps
- Whether PR #49125 merged after 2026-09-24 is unknown.
- I did not verify the exact `model.json` recents path beyond `path.join(Global.Path.state, "model.json")`.

---

## KQ5. Field experience with DeepSeek / Kimi / GLM / Qwen / MiniMax on OpenRouter, Databricks and native APIs

### Takeaway
The recurring failure classes are (1) reasoning content not round-tripped on tool-call turns (DeepSeek V4 "reasoning_content must be passed back", Kimi K2.5/K2.6 "thinking is enabled but reasoning_content is missing in assistant tool call message"), (2) malformed tool-call JSON from open-weight models on third-party hosts (Kimi K2.5/K2.7 on Moonshot/Together/Fireworks, GLM-5 on NVIDIA NIM, Qwen3-Coder on OpenRouter/Zen), (3) provider-template leaks (Kimi K2 `<|tool_calls_section_begin|>` in thinking on OpenRouter, MiniMax `<think>`/`<minimax:tool_call>` in content), and (4) silent doom loops. Most upstream issues were closed "not planned"; OpenCode's fixes are the generic ones in KQ2 (interleaved field replay, `invalid` tool, doom-loop permission, Moonshot schema sanitiser, OpenRouter `provider.order` pinning).

### Cited Findings

**DeepSeek (V3.2 / V4 Flash / V4 Pro)**
- "DeepSeek thinking mode: reasoning_content not passed back for tool call turns, causing 400 errors" — DeepSeek docs quoted: "For turns that do perform tool calls, the reasoning_content must be fully passed back to the API in all subsequent requests"; first turn works, subsequent turns 400; closed "not planned", no workaround — [#24722](https://github.com/anomalyco/opencode/issues/24722). Sibling reports: [#24104](https://github.com/anomalyco/opencode/issues/24104), [#24130](https://github.com/anomalyco/opencode/issues/24130) (V4 Flash), [#24190](https://github.com/anomalyco/opencode/issues/24190) (V4), [#25000](https://github.com/anomalyco/opencode/issues/25000) (V4 Pro via zen/go), [#25058](https://github.com/anomalyco/opencode/issues/25058).
- "DeepSeek silently stops executing (interleaved reasoning_content dropped in tool call messages)" — DeepSeek-V4-Flash (also V4 Pro, Kimi K2) over an OpenAI-compatible provider: "The message conversion in the interleaved handler (`normalizeMessages`/`toModelMessagesEffect`) fails to propagate `reasoning_content` when the assistant message also contains `tool_calls`"; open, PR #28352 unmerged; only workaround is disabling thinking — [#35689](https://github.com/anomalyco/opencode/issues/35689).
- A community "solution" thread shows enabling DeepSeek V4 thinking through DeepSeek's Anthropic-compatible endpoint instead — [#24122](https://github.com/anomalyco/opencode/issues/24122).
- OpenCode's mitigations in code: DeepSeek ids always get a reasoning part; `interleaved: { field: "reasoning_content" }` defaulted for any openai-compatible model whose id contains "deepseek"; `topP 0.95` for V4 Flash; `max` effort variant for `deepseek-v4` on openai-compatible — [transform.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts), [provider.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/provider.ts).
- Doom loop on `deepseek-v4-flash` via a private OpenAI-compatible provider: 364 identical grep calls, ~50 min — [#45442](https://github.com/anomalyco/opencode/issues/45442).

**Kimi (K2 / K2.5 / K2.6 / K2.7 / K3)**
- K2 via OpenRouter: raw `<|tool_calls_section_begin|>`, `<|tool_call_begin|>` tokens appear in the thinking block and the model stops before answering; attributed to OpenRouter routing/chat template; closed without a documented fix — [#8851](https://github.com/anomalyco/opencode/issues/8851). The docs' OpenRouter example pins `moonshotai/kimi-k2` to `"provider": { "order": ["baseten"], "allow_fallbacks": false }` — [Providers docs](https://opencode.ai/docs/providers/).
- K2 (early): "Unable to use Kimi K2: error due to parsing of tool calls" — [#929](https://github.com/anomalyco/opencode/issues/929); "Kimi K2 Thinking tool calling problems - missing arguments" — [#9878](https://github.com/anomalyco/opencode/issues/9878).
- K2.5 on Moonshot: `invalid [tool=bash, error=Invalid input for tool bash: JSON parsing failed` / "Unterminated string"; closed not planned (PR #24289) — [#20650](https://github.com/anomalyco/opencode/issues/20650).
- "Kimi for Coding (k2p5) fails with tool calls when thinking enabled: reasoning_content is missing in assistant tool call message" (HTTP 400) — [#10996](https://github.com/anomalyco/opencode/issues/10996).
- K2.6: `"[Moonshot AI] thinking is enabled but reasoning_content is missing in assistant tool call message at index N"`, OpenCode 1.15.11, closed not planned — [#29619](https://github.com/anomalyco/opencode/issues/29619); "fix: missing reasoning_content for tool-call assistant messages with Kimi K2.6" — [#23831](https://github.com/anomalyco/opencode/issues/23831).
- K2.7 via Together AI and Fireworks (OpenCode 1.17.11): repeated calls to the `invalid` tool, schema errors "missing required keys (`pattern` for glob, `command` for bash)"; closed not planned — [#34071](https://github.com/anomalyco/opencode/issues/34071).
- OpenCode's Kimi-specific code: temperature 1.0 (thinking/K2.5+) or 0.6 (K2), topP 0.95 for K2.5; Moonshot schema sanitiser (`$ref` siblings, tuple `items`); Anthropic-dialect Kimi gets `thinking: adaptive, display: summarized, effort: high`; dedicated `kimi.txt` system prompt — [transform.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts), [system.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/system.ts).

**GLM (4.6 / 4.7 / 5 / 5.2 / 5.3)**
- GLM-5 via NVIDIA NIM in OpenCode: intermittently malformed tool-call JSON (missing/truncated braces) causing parse failures and retry loops — [zai-org/GLM-5 #15](https://github.com/zai-org/GLM-5/issues/15), [NVIDIA forum](https://forums.developer.nvidia.com/t/nim-glm-5-malformed-tool-call-json-missing-via-openai-compatible-endpoint-opencode/360809).
- GLM relayed by Mistral (zai-glm-5/5-2/5-3): first tool call aborts with a SchemaError (missing `tool_call` id in continuation chunks); fixed upstream in `@ai-sdk/mistral 3.0.59` ("schema relaxed to `id: z.string().nullish()`, deltas routed through StreamingToolCallTracker … accumulating by index") — [#43199](https://github.com/anomalyco/opencode/issues/43199), [#49692](https://github.com/anomalyco/opencode/issues/49692).
- Large tool calls silently dropped for z-ai models on a gateway ("finish=stop, tokens billed, no tool executed"); z.ai's `tool_stream=true` is the documented opt-in against this — [Kilo-Org/kilocode #13691](https://github.com/Kilo-Org/kilocode/issues/13691).
- GLM-5.2 support request for the Z.AI provider — [#32172](https://github.com/anomalyco/opencode/issues/32172); OpenCode now special-cases GLM-5.2 variants (`high/max` on openai-compatible & Anthropic dialect; `high/xhigh` on OpenRouter) and sends `thinking: { type: "enabled", clear_thinking: false }` on Z.ai — [transform.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts). Temperature 1.0 for GLM-4.6/4.7 — same file.
- Error-classifier caveat: "z.ai: can accept overflow silently (needs token-count/context-window checks)" — [provider/error.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/error.ts).

**Qwen3-Coder**
- Via OpenRouter: edit tool fails with type validation errors (`oldString`/`newString` received as objects instead of strings); write tool JSON with duplicate keys/unrecognised tokens — [#6918](https://github.com/anomalyco/opencode/issues/6918). Qwen3-Coder-30B-A3B "not able to call any tool" (local) — [#1809](https://github.com/anomalyco/opencode/issues/1809). On OpenCode Zen: "Tool call metadata being printed to chat instead of actually being called", open, workaround "switching to GLM 4.7 via OpenRouter" — [#10855](https://github.com/anomalyco/opencode/issues/10855). Model-side: HF discussion of "very specific json formatting issue in tool calls" in GGUF/AWQ variants — [Qwen3-Coder-Next discussion](https://huggingface.co/Qwen/Qwen3-Coder-Next/discussions/14); vLLM `qwen3_xml` tool-parser fixes — [vllm PR #26345](https://github.com/vllm-project/vllm/pull/26345).
- OpenCode has no Qwen-specific transform except: `qwen` ids get no reasoning variants, and `alibaba-cn` reasoning models get `enable_thinking: true` — [transform.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts). Qwen's own recommended sampling for Qwen3-Coder is temperature 0.7, top_p 0.8, top_k 20, repetition_penalty 1.05 — [Qwen3-Coder README](https://github.com/QwenLM/Qwen3-Coder).

**MiniMax (M2 / M2.5 / M3)**
- "MiniMax-M2 reasoning content not properly handled in OpenAI-compatible mode": reasoning arrives as `<think>` tags inside `delta.content` rather than `delta.reasoning_content` — [#3555](https://github.com/anomalyco/opencode/issues/3555); "[MiniMax M2] Agent stop at the middle of the work" — [#4112](https://github.com/anomalyco/opencode/issues/4112); Windows-only tool-call compatibility bug — [#11091](https://github.com/anomalyco/opencode/issues/11091); "Minimax M2.5 experience is weird" — [#15092](https://github.com/anomalyco/opencode/issues/15092); OpenCode Go `minimax-m3` 400 "tool call result does not follow tool call (2013)" — [#32608](https://github.com/anomalyco/opencode/issues/32608). In another agent, M2.5 emitted tool calls as `<minimax:tool_call><invoke name=...>` XML in text — [openclaw #41839](https://github.com/openclaw/openclaw/issues/41839); MiniMax-M2 streaming emits the tool name first with empty params then fragments (sglang) — [sglang #23071](https://github.com/sgl-project/sglang/issues/23071).
- OpenCode's answer: MiniMax is catalogued as `@ai-sdk/anthropic` at `https://api.minimax.io/anthropic/v1` (models.dev); `minimax-m2` gets temperature 1.0 / topP 0.95 / topK 40 or 20; M3 gets `thinking: adaptive` and `none/thinking` variants (or `chat_template_kwargs.thinking_mode` on nvidia/lilac) — [models.dev](https://models.dev/api.json), [transform.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts).

**Cross-cutting**
- Auto-compaction still let `context_length_exceeded` errors through in agent workflows — [#8089](https://github.com/anomalyco/opencode/issues/8089); configurable threshold requested — [#11314](https://github.com/anomalyco/opencode/issues/11314).
- Zen/big-pickle infinite loop after tool calls complete — [#26220](https://github.com/anomalyco/opencode/issues/26220).

### Inferences
- For rolo-claude, the single highest-value rule is: persist and replay the provider's reasoning field verbatim (`reasoning_content` for DeepSeek/Moonshot/Z.ai direct; `reasoning_details` array for OpenRouter) on every assistant message that carries `tool_calls`, and send an empty string for DeepSeek when there was none. This is the fix every DeepSeek/Kimi/GLM issue above asks for and the thing OpenCode's `normalizeMessages` attempts.
- Second: treat tool-argument parse failures as data, not exceptions (OpenCode's `invalid` tool), and add a repair pass for the two documented Qwen/Kimi shapes (string-vs-object args, unterminated JSON), plus a strip/parse pass for provider-template leaks (`<think>…</think>` in content, `<|tool_call…|>` tokens, `<minimax:tool_call>` XML).
- Third: on OpenRouter, pin `provider.order` + `allow_fallbacks: false` (or `require_parameters: true`) for Kimi/GLM to avoid hosts with broken chat templates; this is the only OpenRouter-specific mitigation OpenCode documents.
- Fourth: implement the doom-loop check across the whole turn with a period-2 detector and a hard step cap, since OpenCode's own guard demonstrably misses the DeepSeek loop in #45442.

### Gaps
- No OpenCode PR was found that definitively resolves any of the reasoning_content issues; the reporters' PRs (#28352, #32089) were not merged, so "what the fix was" is, for most of these, "closed, not planned" or a generic transform.
- No OpenCode-specific reports were found for DeepSeek/Kimi/GLM/Qwen *on Databricks Unity Gateway*; the only Databricks-specific findings are `toolStreaming: false` and per-model `limit` clamping in ucode.
- I did not find OpenCode issues specific to Qwen3.7-Max/Qwen3.8 or Kimi K3; the K3 models.dev entry (`temperature: false`, 1M context) is the only datum.
