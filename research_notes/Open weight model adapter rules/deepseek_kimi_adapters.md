# DeepSeek and Kimi (Moonshot) per-model adapter rules for a Claude-Code-like harness over OpenAI-compatible APIs (as of 2026-09-23)

Scope: DeepSeek V3.1 / V3.1-Terminus / V3.2 / V3.2-Exp / V3.2-Speciale / R1 (third-party hosted) and V4 Flash 0731 / V4.1 Flash / V4 Pro 0813 (official `deepseek-flash`, `deepseek-v4-pro`; OpenRouter `deepseek/*`; Databricks `databricks-deepseek-*`); Kimi K2 / K2-Instruct-0905 / K2-Thinking / K2.5 / K2.6 / K2.7-code / K3 (Moonshot official, OpenRouter `moonshotai/*`, Databricks `databricks-kimi-k3`).

Method note: OpenRouter data below was pulled live on 2026-09-23 from `https://openrouter.ai/api/v1/models` and `https://openrouter.ai/api/v1/models/<author>/<slug>/endpoints` (raw JSON saved in the session scratchpad as `or_models.json` and `or_ep_*.json`). Vendor docs were fetched the same day. "VERIFIED" = quoted from a primary source; "INFERRED" = my conclusion from those sources.

Current live ID inventory (VERIFIED from `/api/v1/models`, 2026-09-23):
- DeepSeek on OpenRouter: `deepseek/deepseek-chat`, `deepseek/deepseek-chat-v3-0324`, `deepseek/deepseek-chat-v3.1`, `deepseek/deepseek-v3.1-terminus`, `deepseek/deepseek-v3.2-exp`, `deepseek/deepseek-v3.2`, `deepseek/deepseek-r1`, `deepseek/deepseek-r1-0528`, `deepseek/deepseek-r1-distill-llama-70b`, `deepseek/deepseek-v4-flash` (Apr-2026 original), `deepseek/deepseek-v4-flash-0731`, `deepseek/deepseek-v4-flash-vision-exp`, `deepseek/deepseek-v4-pro` (Apr-2026 original), `deepseek/deepseek-v4-pro-0813`, `deepseek/deepseek-v4.1-flash`, `deepseek/deepseek-v4.1-flash:batch`, plus alias IDs `~deepseek/deepseek-flash-latest`, `~deepseek/deepseek-pro-latest`, `~deepseek/deepseek-v4-flash-latest`. No `speciale` string exists anywhere in the OpenRouter model list.
- Kimi on OpenRouter: `moonshotai/kimi-k2`, `moonshotai/kimi-k2-0905`, `moonshotai/kimi-k2-thinking`, `moonshotai/kimi-k2.5`, `moonshotai/kimi-k2.6`, `moonshotai/kimi-k2.7-code`, `moonshotai/kimi-k3`, `moonshotai/kimi-k3:batch`, alias `~moonshotai/kimi-latest`.
- DeepSeek official API model IDs today: only `deepseek-flash` and `deepseek-v4-pro` — [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing); `deepseek-chat` and `deepseek-reasoner` were "discontinued in three months (2026-07-24)" after the 2026-04-24 V4 launch — [DeepSeek changelog](https://api-docs.deepseek.com/updates/).
- Moonshot official model IDs today: `kimi-k3`, `kimi-k2.7-code`, `kimi-k2.7-code-highspeed`, `kimi-k2.6` — [Kimi models](https://platform.kimi.ai/docs/models.md). Legacy `kimi-k2-0711-preview`, `kimi-k2-0905-preview`, `kimi-k2-turbo-preview`, `kimi-k2-thinking`, `kimi-k2-thinking-turbo` ("kimi-k2 series models were officially discontinued" May 25, 2026), `kimi-latest` (Jan 28, 2026), `kimi-k2.5` and all `moonshot-v1-*` (Aug 31, 2026) — "All discontinued models route to `kimi-k3`" — [Kimi models](https://platform.kimi.ai/docs/models.md).
- Databricks endpoint names: `databricks-deepseek-v4-1-flash` (inputs "text, image"), `databricks-deepseek-v4-pro-0813` (text), `databricks-deepseek-v4-flash-0731` (text), `databricks-kimi-k3` (inputs "text, image") — [Databricks supported models](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/supported-models). models.dev additionally lists `databricks-kimi-k2-7-code` (tool_call true, temperature false, 262144 ctx) — [models.dev api.json](https://models.dev/api.json) — but I could not find it on the Databricks docs page (Gap).

## Key Question 1: Tool calling (native support, id formats, parallel calls, streaming, limits, strict mode, text-serialised leakage, OpenRouter provider support)

### Takeaway
Both families do native OpenAI-style function calling, but each has one hard wire-format rule: Kimi K2-family models expect tool_call ids of the exact form `functions.{name}:{idx}` and break when a harness rewrites/sanitises them, while DeepSeek V3.2+/V4 models emit DSML tag blocks (`<｜DSML｜tool_calls>` / `<｜DSML｜invoke name=...>`) that leak into `content` on any endpoint whose parser misses them. On OpenRouter, tool support is per-endpoint: several endpoints for these IDs do not list `tools` in `supported_parameters`, so `require_parameters: true` plus an explicit provider list is necessary.

### Cited Findings

DeepSeek — official API
- Tool calls are supported in non-thinking mode for DeepSeek-Flash and others, and "From DeepSeek-V3.2, the API supports tool use in the thinking mode" — [DeepSeek tool calls guide](https://api-docs.deepseek.com/guides/tool_calls)
- Strict mode (beta): use `base_url="https://api.deepseek.com/beta"`, set `"strict": true` per function; "All properties of every `object` must be set as `required`, and the `additionalProperties` attribute of the `object` must be set to `false`"; supported schema types object, string, number, integer, boolean, array, enum, anyOf, $ref, $def; string `pattern` and `format` (email, hostname, ipv4, ipv6, uuid); unsupported: minLength/maxLength, minItems/maxItems — [DeepSeek tool calls guide](https://api-docs.deepseek.com/guides/tool_calls)
- Tool result shape in the official example: `{"role": "tool", "tool_call_id": tool.id, "content": "result"}` (no `name` field) — [DeepSeek tool calls guide](https://api-docs.deepseek.com/guides/tool_calls)
- `tool_choice`: "`required` and named tool choices are not supported in thinking mode; the API returns a `400` error" — [DeepSeek chat completion reference](https://api-docs.deepseek.com/api/create-chat-completion)
- `tools`: "A list of tools the model may call. Currently, only functions are supported as a tool." Tool message `content` accepts "string OR array" — [DeepSeek chat completion reference](https://api-docs.deepseek.com/api/create-chat-completion)
- `finish_reason` values: `stop`, `length`, `content_filter`, `tool_calls`, `insufficient_system_resource`, `aborted` — [DeepSeek chat completion reference](https://api-docs.deepseek.com/api/create-chat-completion)
- Third-party claim (NOT vendor-verified): "DeepSeek V4 Pro and V4 Flash both support up to 128 parallel tool calls per turn" — [chat-deep.ai](https://chat-deep.ai/docs/deepseek-tool-calls/). The official docs I fetched do not state a parallel-call limit.
- A harness bug titled "Parallel tool calls replay without reasoning, breaking DeepSeek thinking-mode continuations" confirms DeepSeek does emit multiple tool_calls per assistant turn in thinking mode — [llame #925](https://github.com/leon0399/llame/issues/925)

DeepSeek — wire markup and leakage
- V3.1 raw markup: `<｜tool▁calls▁begin｜>`, `<｜tool▁call▁begin｜>`, `<｜tool▁sep｜>`, `<｜tool▁call▁end｜>`, `<｜tool▁calls▁end｜>`; "Toolcall is supported in non-thinking mode" (V3.1 only) — [HF DeepSeek-V3.1](https://huggingface.co/deepseek-ai/DeepSeek-V3.1)
- V3.2: "The DeepSeek-V3.2-Speciale variant is designed exclusively for deep reasoning tasks and does not support the tool-calling functionality"; tool encoding is defined by `encoding/encoding_dsv32.py` rather than a Jinja template — [HF DeepSeek-V3.2](https://huggingface.co/deepseek-ai/DeepSeek-V3.2)
- V3.2 emits two tool-call formats ("the old version without DSML and the new version with DSML") and the `encoding_v32` parser requires DSML tokens, "which frequently leads to tool_call_parser errors" — [HF DeepSeek-V3.2 discussion #29](https://huggingface.co/deepseek-ai/DeepSeek-V3.2/discussions/29)
- V4 DSML markers: `<｜DSML｜tool_calls>` / `</｜DSML｜tool_calls>`, `<｜DSML｜invoke name="...">` / `</｜DSML｜invoke>`, `<｜DSML｜parameter name="..." string="true">` / `</｜DSML｜parameter>`; leak classes seen in production: mis-spelled opener `<｜DSML｜tool-calls` (32%), wrong parameter close `</｜DSML｜>` (49%), unrecognized opener (21%); affected models "DeepSeek-V3.2, DeepSeek-V4-Flash-0731, DeepSeek-V4-Pro, DeepSeek-V4.1-Flash"; vLLM flags `--tool-call-parser deepseek_v4 --reasoning-parser deepseek_v4 --tokenizer-mode deepseek_v4` — [vLLM PR #54686](https://github.com/vllm-project/vllm/pull/54686)
- V4.1-Flash vLLM recipe: `--tokenizer-mode deepseek_v41 --tool-call-parser deepseek_v41 --reasoning-parser deepseek_v41 --enable-auto-tool-choice`; "Tool calls are wrapped in DSML tag blocks"; tool output uses "`<tool_result>` tags" — [vLLM recipe DeepSeek-V4.1-Flash](https://recipes.vllm.ai/deepseek-ai/DeepSeek-V4.1-Flash)
- Leaks observed in agents: raw `<｜DSML｜function_calls>` / `<｜DSML｜invoke>` in user-facing output with `deepseek.v3.2` on AWS Bedrock — [hermes-agent #54283](https://github.com/NousResearch/hermes-agent/issues/54283); "DeepSeek V4-pro web search outputs raw DSML tool_calls tokens instead of invoking the tool" — [cherry-studio #14714](https://github.com/CherryHQ/cherry-studio/issues/14714); "DSML control tokens leak into text-dialect tool calls; the call is dropped without a diagnostic" — [tinyagents #204](https://github.com/tinyhumansai/tinyagents/issues/204)
- Legacy vLLM parsers: `deepseek_v3` (DeepSeek-V3-0324, R1-0528; chat templates `tool_chat_template_deepseekv3.jinja` / `tool_chat_template_deepseekr1.jinja`), `deepseek_v31` (`tool_chat_template_deepseekv31.jinja`), `kimi_k2` (Kimi-K2-Instruct) — [vLLM tool calling docs](https://docs.vllm.ai/en/latest/features/tool_calling.html)

Kimi — official API and open-weight guidance
- K2 tool_call id: "functions.{function_name}:{index}", e.g. `functions.get_weather:0`; raw markup `<|tool_calls_section_begin|>` … `<|tool_call_begin|>` id `<|tool_call_argument_begin|>` args `<|tool_call_end|>` … `<|tool_calls_section_end|>`; parse regex `<|tool_call_begin|>\s*(?P<tool_call_id>[\w\.]+:\d+)\s*<|tool_call_argument_begin|>\s*(?P<function_arguments>.*?)\s*<|tool_call_end|>`; tool result `{"role":"tool","tool_call_id":"{tool_call_id}","name":"{function_name}","content":"{json_stringified_result}"}`; "finish_reason may vary across different inference engines" — [HF Kimi-K2-Instruct tool_call_guidance.md](https://huggingface.co/moonshotai/Kimi-K2-Instruct/blob/main/docs/tool_call_guidance.md)
- "K2 expects the ID to follow the format functions.func_name:idx … idx is a global counter that starts at 0 and increments with each function invocation"; if history contains a malformed id like `search:0` the model "might get confused … and attempt to generate a 'similar' but incorrect ID"; fix: "ensure all historical tool call IDs are normalized to the functions.func_name:idx format before sending them to the model" — [HF Kimi-K2-Instruct discussion #48](https://huggingface.co/moonshotai/Kimi-K2-Instruct/discussions/48); same recommendation and root causes (missing `add_generation_prompt`, empty `content: ''` converted to a list breaking the template, parser `IndexError` on non-compliant ids) — [vLLM blog: Kimi K2 accuracy](https://vllm.ai/blog/2025-10-28-kimi-k2-accuracy)
- K2.5 via openclaw: sanitising `functions.read:0` to `functionsread0` dropped pass rate to "20% (1/5 trials)" vs 100% with native ids; symptom is `finish_reason: "stop"` with text saying it will call a tool; both "Kimi" and "Moonshot" providers affected; fix `transcriptToolCallIdMode: "default"` — [openclaw #62319](https://github.com/openclaw/openclaw/issues/62319)
- SGLang feature request to enforce `tool_call.index` increasing with history count for the kimi-k2 parser — [sglang #10600](https://github.com/sgl-project/sglang/issues/10600)
- Moonshot tool-call guide: function names use "English letters, numbers, hyphens, and underscores"; parameters must be `"type": "object"` with `properties`; the model "can choose to call multiple tools at once, which can be different tools or the same tool with different parameters"; streaming: `tool_call.id` and `tool_call.function.name` arrive in the first chunk, then only `arguments`; an `index` field identifies the call; "`delta.content` will be output first, followed by `delta.tool_calls`"; "Every `tool_call` has a corresponding message with `role=tool`" or a `"tool_call_id not found"` error occurs; `finish_reason` `tool_calls` vs `stop` — [Kimi tool calls guide](https://platform.kimi.ai/docs/guide/use-kimi-api-to-complete-tool-calls)
- Chat API: `tool_choice` `"auto"` (default), `"none"`, `"required"`, or a named function; `response_format.type` `"text"|"json_object"|"json_schema"`; `stop` max 5 strings of <=32 bytes; `finish_reason` `stop|length|tool_calls` — [Kimi chat API](https://platform.kimi.ai/docs/api/chat)
- Per-model `tool_choice`: kimi-k3 "auto / none / required"; kimi-k2.7-code and kimi-k2.6 "auto / none only" — [Kimi models overview](https://platform.kimi.ai/docs/api/models-overview.md); K2.6 with thinking enabled: `tool_choice` limited to auto/none, "other values error"; `$web_search` incompatible with thinking — [Kimi K2.6 quickstart](https://platform.kimi.ai/docs/guide/kimi-k2-6-quickstart.md)
- K3: `tool_choice: "required"` supported; structured output via `json_schema` with `strict: true` ("Parse only that field, not `reasoning_content`"); dynamic tool loading "via system messages containing tool definitions without `content` field" — [Kimi K3 quickstart](https://platform.kimi.ai/docs/guide/kimi-k3-quickstart)
- K3 best practice: "don't put every tool definition into the request — they eat up context"; declare a `search_tools` function plus a small core set; "Dynamic tool declarations apply per request and are not retained by the server"; first turn `tool_choice: "required"` then `"auto"`; changing `tool_choice` "does not invalidate the prefix cache" — [Kimi K3 tool-calling best practice](https://platform.kimi.ai/docs/guide/kimi-k3-tool-calling-best-practice.md)
- Kimi K2 (0711) on OpenRouter returned tool calls as JSON text in `content` with `finish_reason: "stop"` — [gist ben-vargas](https://gist.github.com/ben-vargas/c7c9633e6f482ea99041dd7bd90fbe09); same in Zed — [zed #34761](https://github.com/zed-industries/zed/issues/34761); raw `<|tool_calls_section_begin|>` … `functions.bash:0` markup leaked inside the thinking block via OpenRouter — [opencode #8851](https://github.com/anomalyco/opencode/issues/8851)
- SGLang `KimiK2Detector` streaming parser "silently drops / hangs" with multiple tool calls per turn, JSON arguments >5 KB containing markdown/emoji/backticks/nested braces, or a marker split across SSE chunks; symptoms: client "hangs forever in `partial: true`", `functions.*` tokens leak into `content`, only the first of N calls emitted; mitigation "disable streaming on the client side for Kimi-K2.5 endpoints (`"stream": false`)"; the vLLM port (vllm #37184) raised schema accuracy "from 87% → 98–100%" — [sglang #23363](https://github.com/sgl-project/sglang/issues/23363)

OpenRouter endpoint tool support (VERIFIED `supported_parameters` per endpoint, 2026-09-23; slug = `tag`)
- `deepseek/deepseek-chat-v3.1`: tools on `deepinfra/fp4`, `siliconflow/fp8`, `novita/fp8`, `coreweave/fp8`, `google-vertex/us-west2` (status -5), `sambanova/fp8` (max_out 7168, status -2); NO tools on `atlas-cloud/fp8`, `mara`.
- `deepseek/deepseek-v3.1-terminus`: tools on `siliconflow/fp8`, `novita/fp8`, `atlas-cloud/fp8`; NO tools on `streamlake`.
- `deepseek/deepseek-v3.2-exp`: tools on `siliconflow/fp8`, `atlas-cloud/fp8`, `novita/fp8`.
- `deepseek/deepseek-v3.2`: tools on `gmicloud/fp8`, `streamlake/fp8`, `siliconflow/fp8`, `deepinfra/fp4` (max_out 16384), `atlas-cloud/fp8` (status -2), `venice`, `novita/fp8`, `alibaba/fp8`, `friendli`, `google-vertex`, `phala`; NO tools on `baidu/fp8`, `digitalocean`, `mara`, `sambanova`.
- `deepseek/deepseek-r1`: only `novita/fp8` (tools yes). `deepseek/deepseek-r1-0528`: tools on `siliconflow/fp8`, `novita/fp8`; NO tools on `deepinfra/fp4`, `streamlake`.
- `deepseek/deepseek-v4-flash-0731`: 30 endpoints, all list `tools` and `reasoning_effort`; `parallel_tool_calls` only on `inceptron/fp4` and `cohere`; no first-party `deepseek` endpoint.
- `deepseek/deepseek-v4.1-flash`: first-party `deepseek` endpoint (ctx 1048576, max_out 393216, uptime 99.99, tools yes) plus ~26 others; NO tools on `dekallm`.
- `deepseek/deepseek-v4-pro-0813`: first-party `deepseek` endpoint (max_out 393216, uptime 100) plus ~22 others; `fireworks` uptime 79.1 / status -5; `deepinfra/fp8` max_out 16384; `venice` max_out 32768.
- `moonshotai/kimi-k2`: only `novita/fp8` (tools yes; no `response_format`, no `reasoning`). `moonshotai/kimi-k2-0905`: only `novita/fp8`.
- `moonshotai/kimi-k2-thinking`: `google-vertex` (max_out 235929), `novita/bf16` (98304); both tools.
- `moonshotai/kimi-k2.5`: `siliconflow/int4`, `atlas-cloud/int4`, `venice` (65536 out), `novita`, `amazon-bedrock/us-east-2` (131072 out); all tools.
- `moonshotai/kimi-k2.6`: 22 endpoints incl. first-party `moonshotai/int4` (uptime 100; its `supported_parameters` omit `temperature`/`top_p`); `deepinfra/fp4` max_out 16384; `chutes/int4` 65535; `parallel_tool_calls` only on `inceptron/int4`.
- `moonshotai/kimi-k2.7-code`: first-party `moonshotai/int4` and `moonshotai/highspeed`; `deepinfra/fp4` status -2 / 16384 out; `alibaba/fp8` 16384 out; `streamlake` 32000 out and no `temperature`.
- `moonshotai/kimi-k3`: first-party `moonshotai/mxfp4` (uptime 99.95, max_out 943718; no `temperature`/`top_p` in params); NO tools on `chutes/mxfp4` and `fireworks/fast`; NO `tool_choice` on `modal/mxfp4`; `phala` uptime 38 / status -5; `deepinfra/bf16` uptime 80 / status -2 / 16384 out; `alibaba` lacks `temperature`.
- Source for all of the above: [OpenRouter endpoints API](https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash/endpoints) (same path pattern per ID).

### Inferences
- For every Kimi model, never rewrite, shorten, or re-generate assistant `tool_calls[].id`; replay ids byte-for-byte and, when importing history from another model, rename them to `functions.{name}:{n}` with a per-conversation monotonically increasing `n`.
- For DeepSeek V3.2/V4 on any non-first-party endpoint, add a post-parser that detects `<｜DSML｜` or `<｜tool▁calls▁begin｜>` in `content` and either re-parses into tool calls or triggers a retry; treat their presence as a provider-parser failure, not a model refusal.
- Use non-streaming (`stream: false`) for Kimi endpoints served by SGLang-based providers when tool arguments are large (file writes), or validate assembled JSON before dispatch.
- On OpenRouter always send `provider.require_parameters: true` together with `tools`, so endpoints lacking `tools` are excluded automatically.

### Gaps
- No official statement of a maximum number of tools or schema size for the DeepSeek or Moonshot APIs (Databricks' 32-function cap is the only hard number found; see Q6).
- DeepSeek's tool_call `id` format is undocumented (opaque string; no constraint found).
- No official statement that Kimi K3 still uses `functions.{name}:{idx}` ids; the K3 rule is only "pass the complete assistant message back as-is".
- Whether Kimi `tools[].function.strict` is honoured is undocumented (only `response_format.json_schema` strict is documented).

## Key Question 2: Reasoning/thinking (field names, toggles, pass-back rules, exact 400 text, temperature interaction, budgets, `<think>` leakage)

### Takeaway
DeepSeek V4/V4.1 (official and via OpenRouter) hard-fail with HTTP 400 `The \`reasoning_content\` in the thinking mode must be passed back to the API.` when any tool-bearing request drops historical `reasoning_content` (even an empty string), while Kimi K3 and K2.7-code require the full assistant message (including `reasoning_content` and `tool_calls`) to be replayed "as-is" and K2.6 only recommends it. Sampling parameters are silently ignored (DeepSeek thinking mode) or fixed and rejected (Kimi).

### Cited Findings

DeepSeek official (`deepseek-flash`, `deepseek-v4-pro`)
- Toggle: `thinking` object with `type` `"enabled"|"disabled"` (default enabled) and `reasoning_effort` `"none"|"low"|"high"|"max"` (default `high`); "`none` disables thinking mode; `low` / `high` / `max` enable thinking mode" — [DeepSeek chat completion reference](https://api-docs.deepseek.com/api/create-chat-completion); zh-cn guide: thinking mode is on by default and effort defaults to `high`; mapping table sends `minimal`→`low`, `medium`→`high`, `ultra`→`max` — [DeepSeek thinking mode (zh-cn)](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode)
- Response field: `reasoning_content` (nullable string, thinking mode only), "the reasoning contents of the assistant message, before the final answer"; `usage.completion_tokens_details.reasoning_tokens` — [DeepSeek chat completion reference](https://api-docs.deepseek.com/api/create-chat-completion)
- Pass-back rule: when the request includes `tools`, all historical `reasoning_content` must be passed back and is concatenated into context; otherwise "API 会返回 400 报错" (the API returns a 400 error). Without `tools`, `reasoning_content` "无需回传；即使传入 API，也会被忽略" (need not be passed back; ignored if sent) — [DeepSeek thinking mode (zh-cn)](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode). English wording quoted by a harness: "Between two `user` messages, if the model performed a tool call, the intermediate assistant's `reasoning_content` must participate in the context concatenation and must be passed back to the API" — [opencode #24190](https://github.com/anomalyco/opencode/issues/24190)
- Exact 400 body: `{"error": {"message": "The \`reasoning_content\` in the thinking mode must be passed back to the API.", "type": "invalid_request_error", "param": null, "code": "invalid_request_error"}}`; affects `deepseek-v4-pro`, `deepseek-v4-flash` on the official API and OpenRouter; "Failure initiates on the second turn after tool invocation"; fixed in OpenCode PR #24435 by preserving the field on tool-call assistant messages — [opencode #24190](https://github.com/anomalyco/opencode/issues/24190)
- Empty string must also be replayed: DeepSeek returns `reasoning_content: ""` on some assistant messages in a tool chain "and this empty string must be sent back verbatim" — [CodeRouter blog](https://www.coderouter.io/blog/deepseek-reasoning-content-400-error-fix)
- Same error and the fact that the Anthropic-compatible `/v1/messages` path in claude-code-router bypassed its DeepSeek transformer (issue open, no fix) — [claude-code-router #1378](https://github.com/musistudio/claude-code-router/issues/1378); Kilo Code fix injects "empty `reasoning_content` as fallback for all assistant messages lacking reasoning parts" — [kilocode #9501](https://github.com/Kilo-Org/kilocode/issues/9501); other trackers: [openclaw #71435](https://github.com/openclaw/openclaw/issues/71435), [goclaw #1186](https://github.com/nextlevelbuilder/goclaw/issues/1186), [oh-my-pi #12977](https://github.com/can1357/oh-my-pi/issues/12977), [opencode #24722](https://github.com/anomalyco/opencode/issues/24722)
- Silent variant: when the field is dropped instead of rejected, "the model gets confused by the malformed request and returns `finish_reason: 'stop'`, causing the session loop to exit prematurely" (DeepSeek-V4-Flash, V4 Pro, Kimi K2 via interleaved `reasoning_content`) — [opencode #35689](https://github.com/anomalyco/opencode/issues/35689)
- Sampling in thinking mode: "思考模式不支持 `temperature`、`presence_penalty`、`frequency_penalty` 参数 … 设置参数不会报错，但也不会生效" (not supported; setting them raises no error but has no effect); `top_p` works only in thinking mode with valid range 0.95–1.0, lower values default to 0.95 — [DeepSeek thinking mode (zh-cn)](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode). API reference: `frequency_penalty` / `presence_penalty` "This parameter is no longer supported. It will not take effect if you pass it to the API." — [DeepSeek chat completion reference](https://api-docs.deepseek.com/api/create-chat-completion)
- Budgets: `max_tokens` default "8K in non-thinking mode, 64K in thinking mode (128K with `reasoning_effort` set to `max`)", range 1–384K — [DeepSeek chat completion reference](https://api-docs.deepseek.com/api/create-chat-completion)
- Chat-prefix beta: assistant `reasoning_content` (beta) is "Used for the thinking mode in the Chat Prefix Completion feature as the input for the CoT in the last assistant message" — [DeepSeek chat completion reference](https://api-docs.deepseek.com/api/create-chat-completion)
- V4 Pro GA (2026-08-13) "Supports three thinking effort levels: low / high / max" — [DeepSeek changelog](https://api-docs.deepseek.com/updates/)
- Open-weight V4.1-Flash: "continuously controllable reasoning effort" 1–100; vLLM maps "Low/High/XHigh/Max … to 25/50/75/100" whereas "the DeepSeek API uses low / high / max tiers to 50 / 75 / 100"; "With both keys unset, thinking is ON at effort 50" — [vLLM recipe DeepSeek-V4.1-Flash](https://recipes.vllm.ai/deepseek-ai/DeepSeek-V4.1-Flash); raw completions use `<think>` … `</think>` (Non-think emits only a `</think>` summary marker; Think Max needs a special system prompt and >=384K context) — [HF DeepSeek-V4-Flash](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash), [HF DeepSeek-V4-Pro](https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro)
- V3.1 multi-turn: "the thinking token in the last turn will be dropped but the `</think>` is retained in every turn of context" — [HF DeepSeek-V3.1](https://huggingface.co/deepseek-ai/DeepSeek-V3.1)
- Legacy `<think>` leakage evidence: Aider sets `reasoning_tag: think` for `fireworks_ai/accounts/fireworks/models/deepseek-r1` and `use_temperature: false` for `deepseek/deepseek-reasoner` and R1 variants — [Aider model-settings.yml](https://raw.githubusercontent.com/Aider-AI/aider/main/aider/resources/model-settings.yml)

OpenRouter normalisation (applies to all `deepseek/*` and `moonshotai/*` reasoning endpoints)
- Request `reasoning: { effort: "max|xhigh|high|medium|low|minimal|none" | max_tokens: N, exclude: bool, enabled: bool }`; legacy `include_reasoning: true` = `reasoning: {}`; response `reasoning` (string) and `reasoning_details[]` of `reasoning.text` (`text`, optional `signature`), `reasoning.summary`, `reasoning.encrypted` (`data`), each with `id`, `format`, `index`, `type`; in tool loops "Pass back unmodified" the `reasoning_details` array on assistant messages, or use the `reasoning` string as `reasoning_content` — [OpenRouter reasoning tokens](https://openrouter.ai/docs/use-cases/reasoning-tokens)
- `reasoning_effort` appears in `supported_parameters` for `deepseek/deepseek-v4*`, `deepseek/deepseek-v4.1-flash` and `moonshotai/kimi-k3` endpoints but NOT for `moonshotai/kimi-k2.6` / `kimi-k2.7-code` (only `reasoning`/`include_reasoning`) — [OpenRouter endpoints API](https://openrouter.ai/api/v1/models/moonshotai/kimi-k2.6/endpoints)

Kimi official
- Field: `reasoning_content` in `choices[].message` / stream deltas; SDK access via `getattr(obj, "reasoning_content")`; "In streaming output (`stream=True`), the `reasoning_content` field will always appear before the `content` field"; billing: "the sum of tokens in `reasoning_content` and `content` must be less than or equal to `max_tokens`"; "Do not set `temperature`" for thinking models — [Kimi thinking models guide](https://platform.kimi.ai/docs/guide/use-thinking-models.md)
- Replay rule: "keep the `reasoning_content` from every historical assistant message in `messages` as-is. The simplest way is to append the assistant message returned from the previous API call directly back into `messages`" — [Kimi thinking models guide](https://platform.kimi.ai/docs/guide/use-thinking-models.md); K3: "Return the complete assistant message unchanged in multi-turn conversations and tool calls"; "K3 always has thinking mode enabled"; `reasoning_effort` `low|high|max`, default `max`; "temperature=1.0, top_p=0.95, n=1, presence_penalty=0, and frequency_penalty=0 are fixed; omit them from requests" — [Kimi K3 quickstart](https://platform.kimi.ai/docs/guide/kimi-k3-quickstart); "Kimi K3 was trained in the preserved thinking history mode … requires the complete assistant message returned by the API to be passed back to `messages` as-is — including `reasoning_content` and `tool_calls`" — [HF Kimi-K3](https://huggingface.co/moonshotai/Kimi-K3)
- Migration: "When migrating from K2.x to K3, remove the K2.x thinking configuration and use top-level reasoning_effort as needed" — [Kimi reasoning effort guide](https://platform.kimi.ai/docs/guide/use-reasoning-effort)
- `thinking.keep`: kimi-k2.6 default `null` ("Historical `reasoning_content` is ignored. Shorter context and lower cost."), `"all"` enables Preserved Thinking; kimi-k2.7-code only `"all"` ("always keeps it"); kimi-k3 not applicable — [Kimi thinking models guide](https://platform.kimi.ai/docs/guide/use-thinking-models.md); chat API: `thinking.type` `"enabled"` (k2.7-code only) / `"enabled"|"disabled"` (k2.6); "For kimi-k2.7-code, only `enabled` is accepted; passing `disabled` returns an error" — [Kimi chat API](https://platform.kimi.ai/docs/api/chat)
- K2.6: "we recommend keeping the `reasoning_content` from the assistant message in the current turn's tool call within the context; omitting it does not cause an error, but may degrade reasoning continuity and tool-call quality"; temperature fixed 1.0 (thinking) / 0.6 (non-thinking), other values "will result in an error"; top_p fixed 0.95 — [Kimi K2.6 quickstart](https://platform.kimi.ai/docs/guide/kimi-k2-6-quickstart.md)
- K2.7-code: thinking mandatory ("will throw an error if the thinking mode is disabled"); temperature "fixed value 1.0", top_p "fixed value 0.95", n=1, penalties 0.0, other values "will result in an error" — [Kimi K2.7 Code quickstart](https://platform.kimi.ai/docs/guide/kimi-k2-7-code-quickstart.md)
- K2-Thinking (open weights): "The recommended temperature for Kimi-K2-Thinking is `temperature = 1.0`"; native INT4 via QAT — [HF Kimi-K2-Thinking](https://huggingface.co/moonshotai/Kimi-K2-Thinking)
- Cross-model history: flattening prior thinking into text so that "zero `reasoning_content` fields" remain makes kimi-k3 "drop out of reasoning mode and emit deliberation as ordinary content"; fix appends `reasoning_content: ""` to assistant messages for kimi-k3 and deepseek-v4 families only — [kimchi PR #1209](https://github.com/getkimchi/kimchi/pull/1209)

Databricks
- Reasoning surfaces as a `ContentItem` of type `"reasoning"` with summary entries `"summary_text"` or `"summary_encrypted_text"`; `reasoning_effort` accepts `"minimal"|"low"|"medium"|"high"|"xhigh"|"max"|"none"`; `usage.reasoning_tokens` — [Databricks FMAPI reference](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/api-reference)
- "If it's dropped or modified, the model can't reason over its earlier thinking"; preserve `encrypted_content` verbatim when continuing — [Databricks query reasoning models](https://docs.databricks.com/aws/en/machine-learning/model-serving/query-reason-models)
- DeepSeek V4.1 Flash on Databricks: "accepts `"low"`, `"high"`, `"xhigh"`, or `"max"` (default). `"minimal"` maps to `"low"`, and `"medium"` maps to `"high"`. `"none"` or `"disabled"` disables reasoning." "Other values are rejected." DeepSeek V4 Pro (0813), V4 Flash (0731), Kimi K3: `low|high|max`, default `max`, "Other effort values fall back to 'max'" — [Databricks query reasoning models](https://docs.databricks.com/aws/en/machine-learning/model-serving/query-reason-models)
- goose had to forward `reasoning_effort` for Databricks Kimi K3 "defaulting to max and clamping unsupported generic values" — [goose PR #12079](https://github.com/aaif-goose/goose/pull/12079)

### Inferences
- Adapter rule for DeepSeek V4-family (official, OpenRouter first-party, Databricks): store `reasoning_content` on every assistant message; on replay emit it verbatim (including `""`); if an assistant message has none, emit `reasoning_content: ""`. Do this whether or not `thinking` is enabled on the current request (harmless when ignored, mandatory once `tools` is present).
- Adapter rule for Kimi K3/K2.7-code: replay the assistant message object exactly as received (`content`, `reasoning_content`, `tool_calls` with original ids). For K2.6 send `thinking: {"type":"enabled","keep":"all"}` in agent loops so continuity matches K3 behaviour.
- Do not send `temperature`, `top_p`, `n`, `presence_penalty`, `frequency_penalty` to any current Moonshot model; do not rely on `temperature` for DeepSeek thinking mode (ignored); `top_p` below 0.95 is clamped there.
- Via OpenRouter, keep `reasoning_details` unmodified and additionally set `reasoning_content` from `reasoning` when targeting DeepSeek first-party, because the 400 is generated by DeepSeek and passes through OpenRouter.

### Gaps
- DeepSeek docs do not say whether `reasoning_content` must be replayed for turns that were generated with thinking disabled (rule is stated for "thinking mode" only); harnesses replay unconditionally as a safe superset.
- Databricks does not document whether DeepSeek/Kimi reasoning must be passed back for tool loops on its endpoints, nor whether it exposes `reasoning_content` verbatim versus a summary item.
- No exact error text was found for Kimi when `temperature` is set to a non-fixed value or when `thinking.type: disabled` is sent to k2.7-code (docs only say "returns an error").

## Key Question 3: Sampling and limits (temperature/top_p, context, output defaults/caps, overflow wording, rate limits, prompt caching)

### Takeaway
DeepSeek V4-family: 1M context, 384K max output, defaults 8K/64K/128K by mode, vendor-recommended temperature 1.0 with top_p 0.95–1.0 and off-peak pricing at 50%. Kimi: K3 1M context with `max_completion_tokens` default 131072 (cap 1,048,576); K2.6/K2.7 256K context with 32K default output; all sampling fixed. Overflow errors are model-length strings on DeepSeek (plus a terse `quota_limit_reached` variant) and `exceeded model token limit: N (requested: M)` on Kimi.

### Cited Findings

DeepSeek
- `deepseek-flash`: context 1M, max output 384K, thinking (default) and non-thinking, JSON output, tool calls, chat prefix, FIM (non-thinking only); pricing per 1M tokens: cache hit $0.003 off-peak / $0.006 peak, cache miss $0.15 / $0.30, output $0.60 / $1.20. `deepseek-v4-pro`: 1M / 384K, "Vision: Not supported"; cache hit $0.022 / $0.044, miss $0.66 / $1.32, output $1.98 / $3.96 — [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing)
- `temperature` 0–2 default 1 (non-thinking); `top_p` (0,1] default 1; `max_tokens` 1–384K with mode defaults above; `stop` up to 16 sequences; `top_logprobs` 0–20; usage fields `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens`, `prompt_tokens_details.cached_tokens`, `completion_tokens_details.reasoning_tokens` — [DeepSeek chat completion reference](https://api-docs.deepseek.com/api/create-chat-completion)
- Open-weight recommendations: V4-Flash / V4-Pro "temperature = 1.0, top_p = 1.0"; Think Max needs >=384K context — [HF DeepSeek-V4-Flash](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash), [HF DeepSeek-V4-Pro](https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro); V4.1-Flash "`temperature` 1.0, `top_p` 0.95 or 1.0", "`max_tokens` ≥ 256K" — [HF DeepSeek-V4.1-Flash](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash); V3.2 "temperature = 1.0, top_p = 0.95" — [HF DeepSeek-V3.2](https://huggingface.co/deepseek-ai/DeepSeek-V3.2)
- Aider settings: `deepseek/deepseek-chat` max_tokens 8192, caches_by_default; `deepseek/deepseek-reasoner` max_tokens 64000, use_temperature false; `openrouter/deepseek/deepseek-r1` max_tokens 8192 + `include_reasoning: true`; `openrouter/deepseek/deepseek-chat-v3-0324` max_tokens 65536 — [Aider model-settings.yml](https://raw.githubusercontent.com/Aider-AI/aider/main/aider/resources/model-settings.yml)
- Overflow wording (official API): "This model's maximum context length is 1048576 tokens. However, you requested 1049647 tokens (985647 in the messages, 64000 in the completion)." — [command-code #892](https://github.com/CommandCodeAI/command-code/issues/892); full form ends "Please reduce the length of the messages or completion." — [DeepSeekV4.tech](https://deepseekv4.tech/en/errors/context-length-exceeded) (secondary); terse variant `{"message":"Input token exceed the limit (request id: ...)","type":"api_error","param":"","code":"quota_limit_reached"}` with HTTP 400 — [deepseek-harness discussion #3399](https://github.com/deepseek-ai/deepseek-harness/discussions/3399)
- Harness cause: clients reserve a static 64,000-token completion on top of input — [zed #57718](https://github.com/zed-industries/zed/issues/57718), [zed #45456](https://github.com/zed-industries/zed/issues/45456)
- Error code table: 400 "Invalid request body format", 401, 402 "Insufficient Balance", 422 "Your request contains invalid parameters", 429 "You are sending requests too quickly", 500, 503 "The server is overloaded due to high traffic" — [DeepSeek error codes](https://api-docs.deepseek.com/quick_start/error_codes)
- Pricing changes: V4.1-Flash pricing effective "04:00 UTC on Sept 10, 2026", "Off-peak rates are 50% of peak rates"; V4 Pro peak/off-peak effective "August 16, 2026" — [DeepSeek V4.1-Flash news](https://api-docs.deepseek.com/news/news260910/), [DeepSeek changelog](https://api-docs.deepseek.com/updates/)
- CONFLICT on V4 Pro availability: the news page says "All `deepseek-v4-pro` requests will route to V4.1-Flash at V4.1-Flash rates" from "04:00 UTC on Sept 14, 2026" — [DeepSeek V4.1-Flash news](https://api-docs.deepseek.com/news/news260910/); the pricing page fetched 2026-09-23 still lists `deepseek-v4-pro` with separate pricing — [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing); secondary reports say DeepSeek "reversed this decision, and V4 Pro API service continues after September 14 with billing unchanged" — [digitalapplied](https://www.digitalapplied.com/blog/deepseek-v4-1-flash-pro-routing-prices-early-tests)

Kimi
- Context/pricing: `kimi-k3` 1,048,576 ctx, $0.30 cache hit / $3.00 miss / $15.00 output per 1M, cache write $3.00 (5m TTL) / $6.00 (1h); `kimi-k2.7-code` 262,144, $0.19 / $0.95 / $4.00; `kimi-k2.7-code-highspeed` $0.38 / $1.90 / $8.00; `kimi-k2.6` 262,144, $0.16 / $0.95 / $4.00 — [Kimi pricing](https://platform.kimi.ai/docs/pricing/chat)
- `max_completion_tokens`: K3 "Defaults to 131072; can be set up to 1048576" — [Kimi K3 quickstart](https://platform.kimi.ai/docs/guide/kimi-k3-quickstart); K2.6 and K2.7-code "Default to be 32k aka 32768" — [Kimi K2.6 quickstart](https://platform.kimi.ai/docs/guide/kimi-k2-6-quickstart.md), [Kimi K2.7 Code quickstart](https://platform.kimi.ai/docs/guide/kimi-k2-7-code-quickstart.md); older thinking guidance recommended max_tokens >= 16000 — [Kimi thinking models guide](https://platform.kimi.ai/docs/guide/use-kimi-k2-thinking-model)
- Fixed sampling: k3 "Fixed at 1.0" temperature / top_p 0.95 / n 1 / penalties 0; k2.7-code same; k2.6 "1.0 thinking / 0.6 non-thinking" — [Kimi models overview](https://platform.kimi.ai/docs/api/models-overview.md); open-weight K3: "top-p = 0.95" single-step, "top-p = 1.0" agentic, temperature 1.0 — [GitHub MoonshotAI/Kimi-K3](https://github.com/MoonshotAI/Kimi-K3)
- Legacy K2 sampling: Aider `openrouter/moonshotai/kimi-k2` `temperature: 0.6` — [Aider model-settings.yml](https://raw.githubusercontent.com/Aider-AI/aider/main/aider/resources/model-settings.yml); HF tool-call guidance examples use 0.3 — [HF Kimi-K2-Instruct tool_call_guidance.md](https://huggingface.co/moonshotai/Kimi-K2-Instruct/blob/main/docs/tool_call_guidance.md); models.dev marks `temperature: false` for kimi-k3, kimi-k2.7-code, kimi-k2.7-code-highspeed and `true` for kimi-k2.6 — [models.dev api.json](https://models.dev/api.json)
- Overflow wording: "if input plus max_completion_tokens exceeds the model context window, the API returns invalid_request_error" — [Kimi chat API](https://platform.kimi.ai/docs/api/chat); observed: `400 invalid_request_error` "Invalid request: Your request exceeded model token limit: 262144 (requested: 269030)" — [kimi-cli #2011](https://github.com/MoonshotAI/kimi-cli/issues/2011); documented messages "Input token length too long" and "prompt tokens + max_tokens exceeds the model specification" — [Kimi error codes](https://platform.kimi.ai/docs/api/errors.md); vague variant "Invalid request Error" when context held many large tool results — [Kimi-K2.5 #20](https://github.com/MoonshotAI/Kimi-K2.5/issues/20)
- Error types: 400 `content_filter` "The request was rejected because it was considered high risk", `invalid_request_error`; 401 `invalid_authentication_error`, `incorrect_api_key_error`; 403 `permission_denied_error`; 404 `resource_not_found_error` "Model not found, or this account does not have permission to access the model"; 429 `engine_overloaded_error` "The engine is currently overloaded, please try again later", `exceeded_current_quota_error`, `rate_limit_reached_error` "Organization-level concurrency|RPM|TPM|TPD limit reached"; 499 `client_closed_request`; 500 `server_error`/`unexpected_output`; 503 `server_unavailable`; 504 — [Kimi error codes](https://platform.kimi.ai/docs/api/errors.md)
- Rate tiers (cumulative recharge → concurrency / RPM / TPM / TPD): Tier0 $1 → 1 / 3 / 500,000 / 1,500,000; Tier1 $10 → 15 / 100 / 2,000,000 / unlimited; Tier2 $20 → 40 / 100 / 3,000,000; Tier3 $100 → 50 / 200 / 3,000,000; Tier4 $1,000 → 60 / 200 / 4,000,000; Tier5 $3,000 → 100 / 300 / 5,000,000; HTTP 429 with `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` — [Kimi limits](https://platform.kimi.ai/docs/pricing/limits.md)
- Prompt caching: automatic; "A new request can hit the prefix cache only when the previous request's prompt tokens exceed 256"; `prompt_cache_options.ttl` `"5m"|"1h"`; usage `cached_tokens`, `prompt_tokens_details.cached_tokens`, `cache_write_tokens`; "caches are never shared across organizations" — [Kimi chat API](https://platform.kimi.ai/docs/api/chat), [Kimi K3 quickstart](https://platform.kimi.ai/docs/guide/kimi-k3-quickstart)

OpenRouter (VERIFIED model-level `context_length` / `top_provider.max_completion_tokens`)
- `deepseek/deepseek-chat-v3.1` 163840 / 32768; `deepseek-v3.1-terminus` 163840 / 32768; `deepseek-v3.2-exp` 163840 / 65536; `deepseek-v3.2` 163840 / 65536; `deepseek-r1` 64000 / 16000; `deepseek-r1-0528` 163840 / 32768; `deepseek-v4-flash` 1048576 / 384000; `deepseek-v4-flash-0731` 1310720 / 943718; `deepseek-v4-flash-vision-exp` 1048576 / 943718; `deepseek-v4-pro` 1048576 / 384000; `deepseek-v4-pro-0813` 1048576 / 384000; `deepseek-v4.1-flash` 1048576 / 131072; `moonshotai/kimi-k2` 131072 / 98304; `kimi-k2-0905` 262144 / 98304; `kimi-k2-thinking` 262144 / 98304; `kimi-k2.5` 262144 / 235929; `kimi-k2.6` 262144 / 235929; `kimi-k2.7-code` 262144 / 235929; `kimi-k3` 1048576 / 943718; `kimi-k3:batch` 1048576 / 16384 — [OpenRouter models API](https://openrouter.ai/api/v1/models)

Databricks
- Pay-per-token limits: DeepSeek V4.1 Flash and Kimi K3: ITPM "2,000,000", OTPM "40,000", QPH "7,200"; DeepSeek V4 Pro (0813): ITPM 200,000, OTPM "4,000", QPH "7,200"; DeepSeek V4 Flash (0731): ITPM 200,000, OTPM "10,000"; payload "4 MB", execution "597 seconds", "200" QPS per workspace; unused reserved `max_tokens` are credited back — [Databricks FMAPI limits](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/limits)
- `temperature` [0,2] default 1.0; `top_p` (0,1] default 1.0; `max_tokens` null = no limit; `stream` default true — [Databricks FMAPI reference](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/api-reference)

### Inferences
- For DeepSeek V4-family set `max_tokens` explicitly to (context − prompt_tokens) capped at 384000, never a fixed 64K reserve; the overflow message includes both numbers so a regex can compute the overage.
- For Kimi K3 leave `max_completion_tokens` at the 131072 default unless generating huge outputs; the same input+output sum rule applies as DeepSeek.
- Databricks OTPM of 4,000 for V4 Pro 0813 makes long thinking outputs impractical there; prefer V4.1 Flash or K3 on Databricks for agent loops.

### Gaps
- DeepSeek official rate limits are not published beyond the 429 text.
- No exact Databricks context-overflow error string was found.
- No vendor temperature recommendation exists for DeepSeek V3.1/Terminus or R1 beyond Aider's "use_temperature: false" for reasoners.

## Key Question 4: Message shape constraints (system placement, multipart content, vision, tool result format, empty assistant content)

### Takeaway
DeepSeek accepts a string-only system message, multipart user/tool content (text, image_url on `deepseek-flash` only, file), and nullable assistant content; Kimi accepts extra system messages (used for dynamic tool loading), requires image/video content as base64 or `ms://<file-id>` parts (no public URLs), and tool results carrying `tool_call_id` (+ `name` in examples). Databricks allows the system role only once as the first message and caps tools at 32.

### Cited Findings
- DeepSeek: system message "Text content only via `content` (string)"; user `content` string or array of parts `text`, `image_url` (JPEG, PNG, GIF, WebP; detail `low|high|original|auto`), `file` (`file_id` or `file_data`); assistant `content` nullable string with beta `prefix` and `reasoning_content`; tool message content string or array — [DeepSeek chat completion reference](https://api-docs.deepseek.com/api/create-chat-completion); `deepseek-v4-pro` "Vision: Not supported" — [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing); V4.1-Flash is "native multimodal" — [DeepSeek V4.1-Flash news](https://api-docs.deepseek.com/news/news260910/)
- Kimi K3: "Vision input does not support public image URLs. Use base64 or `ms://<file-id>`"; content "must be an array of objects, not a serialized string"; video via `files.create` — [Kimi K3 quickstart](https://platform.kimi.ai/docs/guide/kimi-k3-quickstart); K2.6: images png/jpeg/webp/gif, videos mp4/mpeg/mov/avi/x-flv/mpg/webm/wmv/3gpp, "No URL images; only base64-encoded content", request body under 100MB — [Kimi K2.6 quickstart](https://platform.kimi.ai/docs/guide/kimi-k2-6-quickstart.md)
- Kimi tool result: `{"role":"tool","tool_call_id": <id>, "name": <function name>, "content": <JSON string>}`; assistant messages containing `tool_calls` must precede the tool responses — [Kimi tool calls guide](https://platform.kimi.ai/docs/guide/use-kimi-api-to-complete-tool-calls); tool definitions may be injected later "via a `system` message" — [Kimi K3 tool-calling best practice](https://platform.kimi.ai/docs/guide/kimi-k3-tool-calling-best-practice.md)
- Kimi chat API messages support multimodal parts `text`, `image_url`, `video_url` — [Kimi chat API](https://platform.kimi.ai/docs/api/chat)
- Empty assistant content pitfall (self-hosted Kimi K2): vLLM converted `''` to `[{'type': 'text', 'text': ''}]`, which the Jinja template inserted literally before `<|tool_calls_section_begin|>` — [vLLM blog: Kimi K2 accuracy](https://vllm.ai/blog/2025-10-28-kimi-k2-accuracy)
- Databricks: roles `system|user|assistant|tool`; "The `system` role can only be used once, as the first message in a conversation"; `content` string or list of content items (list documented for Claude; "string-typed content required" for other models); `tools` "a max of 32 functions are supported"; `tool_choice` default `"auto"` if tools present else `"none"`; `response_format` `text|json_object|json_schema` — [Databricks FMAPI reference](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/api-reference)
- OpenRouter modalities: `deepseek/deepseek-v4.1-flash` and `deepseek-v4-flash-vision-exp` text+image; all other `deepseek/*` text; `moonshotai/kimi-k2.5`, `kimi-k2.6`, `kimi-k2.7-code` text+image; `kimi-k3` text+image+video; `kimi-k2`, `kimi-k2-0905`, `kimi-k2-thinking` text — [OpenRouter models API](https://openrouter.ai/api/v1/models)

### Inferences
- Send a single leading `system` message for portability (Databricks requirement); when using Kimi dynamic tool loading, only do it on Moonshot direct/OpenRouter, not Databricks.
- For assistant messages that carry only `tool_calls`, replay `content` exactly as the API returned it (null or "") rather than normalising to an empty text part.
- Convert `image_url` http(s) URLs to base64 data URLs before sending to Moonshot; DeepSeek `deepseek-flash` accepts URLs per its `image_url` part.

### Gaps
- Neither DeepSeek nor Moonshot documents the `developer` role or multiple/trailing system messages explicitly (Moonshot implicitly allows later system messages for tool loading).
- Whether the `name` field on tool messages is required by Kimi is not stated (present in all official examples).

## Key Question 5: Known agent-loop failure modes and fixes (harness issue trackers)

### Takeaway
The recurring failures are (1) DeepSeek 400/silent-stop from dropped `reasoning_content`, (2) Kimi tool-call id mangling or streaming-parser truncation, (3) DSML/`<|tool_calls_section_begin|>` markup leaking as text, (4) DeepSeek V4 repeated identical tool calls and zero-token empty completions after tool results, and (5) context-reservation overflow from static 64K `max_tokens`.

### Cited Findings
- DeepSeek V4 Pro: empty responses `content=""`, `reasoning_content=""`, `completion_tokens=0` returned in "1-2 seconds" after tool messages, 22 of 46 turns in one session, retries did not recover; closed as stale — [DeepSeek-V3 #1453](https://github.com/deepseek-ai/DeepSeek-V3/issues/1453)
- DeepSeek V4 Pro repeats the identical `run_shell_command` `git status --short` call from ~100k tokens of context (not over-limit; 400,013-token single requests worked), via Bailian/DashScope with thinking on; proposed circuit breaker: after >=3 identical `(tool_name, normalized_args)` inject "You already ran the same tool 3 times with identical arguments. Reply in text—do not call this tool again." and a 30-round cap with `tool_choice="none"` — [qwen-code #4695](https://github.com/QwenLM/qwen-code/issues/4695); "thinking/action inconsistency causes infinite tool-calling loop" — [hermes-agent #37255](https://github.com/NousResearch/hermes-agent/issues/37255); duplicate commands/edits with V4 Pro and Flash in Kilo Code — [kilocode #10175](https://github.com/Kilo-Org/kilocode/issues/10175)
- DeepSeek V4 Flash via DeepInfra: `write`/`edit` tools "repeatedly passes empty 'content'" when the payload contains HTML-like tags — [opencode #26913](https://github.com/anomalyco/opencode/issues/26913)
- DeepSeek V4.1 Flash / V4 Pro infinite loop "due to missing stop sequence handling" — [opencode-cmd-provider #211](https://github.com/rashidrazak/opencode-cmd-provider/issues/211)
- Moonshot's own guidance on repeated identical tool calls: verify "the returned `choice.message` has been added to the `messages` list as is", "each `tool_call` has a corresponding message with `role=tool`", "`tool_call_id` … exactly matches the corresponding `tool_call.id`", and streamed `function.arguments` were assembled correctly; then client-side reminders at 3 ("You are repeating the exact same tool call with identical parameters"), 5 ("Do not call this exact same tool with the exact same arguments again"), and 8+ repetitions — [Kimi tool-call repeat guide](https://platform.kimi.ai/docs/guide/tool-call-repeat.md)
- Kimi via OpenRouter: `Invalid request: tokenization failed` on every request for `moonshotai/kimi-k2.6` from one harness while the same minimal payload via curl succeeded (open, unresolved) — [pi #5159](https://github.com/earendil-works/pi/issues/5159); Kimi K2.5 with OpenRouter problems — [opencode #11541](https://github.com/anomalyco/opencode/issues/11541)
- Kimi tool-call tokens inside thinking block / premature termination on OpenRouter — [opencode #8851](https://github.com/anomalyco/opencode/issues/8851); Kimi K2 tool calls as text — [gist ben-vargas](https://gist.github.com/ben-vargas/c7c9633e6f482ea99041dd7bd90fbe09), [zed #34761](https://github.com/zed-industries/zed/issues/34761); id sanitisation — [openclaw #62319](https://github.com/openclaw/openclaw/issues/62319); streaming argument truncation/hang — [sglang #23363](https://github.com/sgl-project/sglang/issues/23363); "Function Call Format Error in Multi-turn Dialogues" — [HF Kimi-K2-Instruct discussion #45](https://huggingface.co/moonshotai/Kimi-K2-Instruct/discussions/45)
- DeepSeek reasoning_content 400 / silent stop: see Q2 citations ([opencode #24190](https://github.com/anomalyco/opencode/issues/24190), [opencode #35689](https://github.com/anomalyco/opencode/issues/35689), [claude-code-router #1378](https://github.com/musistudio/claude-code-router/issues/1378), [kilocode #9501](https://github.com/Kilo-Org/kilocode/issues/9501))
- DeepSeek DSML leakage: see Q1 citations ([vLLM PR #54686](https://github.com/vllm-project/vllm/pull/54686), [hermes-agent #54283](https://github.com/NousResearch/hermes-agent/issues/54283), [cherry-studio #14714](https://github.com/CherryHQ/cherry-studio/issues/14714))
- Context reservation overflow with static `max_tokens` 64000 — [zed #57718](https://github.com/zed-industries/zed/issues/57718), [zed #45456](https://github.com/zed-industries/zed/issues/45456); harness misclassifies DeepSeek's `quota_limit_reached` as INVALID_REQUEST so auto-compaction never triggers — [deepseek-harness discussion #3399](https://github.com/deepseek-ai/deepseek-harness/discussions/3399)

### Inferences
- Implement in the harness: (a) reasoning replay (Q2), (b) id preservation (Q1), (c) DSML/Kimi-token leak detector with one automatic retry, (d) identical-call circuit breaker at 3 with an injected user/system reminder, (e) empty-completion detector (completion_tokens==0 with tools present) → retry once, then fall back to `thinking.type: disabled` (DeepSeek) or a different provider, (f) overflow classifier using both regexes in Q3, (g) `stream: false` fallback for Kimi endpoints that emit truncated arguments.

### Gaps
- No root-cause statement from DeepSeek for the zero-token empty completions or the repeated-call loop; only client-side mitigations exist.
- The `tokenization failed` OpenRouter/Moonshot error has no documented trigger.

## Key Question 6: Provider pinning on OpenRouter and Databricks availability/limits

### Takeaway
Pin DeepSeek V4.1 Flash and V4 Pro 0813 to the first-party `deepseek` endpoint and Kimi K2.6/K2.7-code/K3 to `moonshotai` (which enforces the vendor's fixed sampling and reasoning semantics), with `allow_fallbacks: false` and `require_parameters: true`; older IDs have no first-party endpoint and need an explicit `order`/`ignore` list because several endpoints lack `tools`, cap output at 7–16K, or are degraded. Databricks hosts four of the IDs with a 32-function cap, single leading system message, and per-model `reasoning_effort` defaults of `max`.

### Cited Findings
- OpenRouter `provider` object: `order` ("List of provider slugs to try in order"), `allow_fallbacks` (default true), `require_parameters` (default false, "Only use providers that support all parameters"), `only`, `ignore`, `quantizations` (`int4, int8, fp4, mxfp4, nvfp4, fp6, fp8, mxfp8, fp16, bf16, fp32, unknown`), `sort` (`price|throughput|latency`), `max_price`, `zdr`, `data_collection`, `preferred_min_throughput`, `preferred_max_latency`; base slug `"deepinfra"` matches `deepinfra/turbo` etc.; `:nitro` / `:floor` suffixes — [OpenRouter provider routing](https://openrouter.ai/docs/features/provider-routing)
- First-party endpoints present (VERIFIED): `deepseek` on `deepseek/deepseek-v4.1-flash` ($0.15/$0.60 per 1M, max_out 393216) and `deepseek/deepseek-v4-pro-0813` ($0.66/$1.98, max_out 393216); `moonshotai/int4` on `kimi-k2.6` and `kimi-k2.7-code`, `moonshotai/highspeed` on `kimi-k2.7-code` ($1.90/$8.00), `moonshotai/mxfp4` on `kimi-k3` ($3.00/$15.00) — [OpenRouter endpoints API](https://openrouter.ai/api/v1/models/moonshotai/kimi-k3/endpoints)
- Endpoints to avoid (VERIFIED per Q1 table): no-tools endpoints (`dekallm` on v4.1-flash; `chutes/mxfp4`, `fireworks/fast` on k3; `baidu/fp8`, `digitalocean`, `mara`, `sambanova` on v3.2; `atlas-cloud/fp8`, `mara` on v3.1; `streamlake` on terminus; `deepinfra/fp4`, `streamlake` on r1-0528), tiny output caps (`deepinfra/fp8` 16384 on v4-pro-0813 and v4-pro; `deepinfra/fp4` 16384 on v3.2, kimi-k2.6, kimi-k2.7-code; `deepinfra/bf16` 16384 on k3; `venice` 32768 on v4-pro/v4-flash; `sambanova/fp8` 7168 on v3.1; `alibaba/fp8` 16384 on k2.7-code), reduced context (`reka/fp4` and `coreweave/fp8` 262144 on v4-flash-0731), degraded status (`fireworks` on v4-pro-0813 status -5 / uptime 79; `phala` on k3 status -5 / uptime 38; `deepinfra/fp8` on v4-flash-vision-exp status -5 / uptime 58; `google-vertex/us-west2` on v3.1 status -5) — [OpenRouter endpoints API](https://openrouter.ai/api/v1/models/deepseek/deepseek-v4-pro-0813/endpoints)
- Quantization on OpenRouter: DeepSeek V4-family first-party endpoints report `unknown`; third parties are mostly `fp8` with `fp4` at DeepInfra/AtlasCloud/BaseTen/Sail/Relace; Kimi K2.5/K2.6/K2.7 first-party is `int4` (native QAT), K3 first-party `mxfp4` (K3 is trained "MXFP4 weights / MXFP8 activations (quantization-aware training)" — [GitHub MoonshotAI/Kimi-K3](https://github.com/MoonshotAI/Kimi-K3)); several K3 third parties are `fp4`, `bf16` (DeepInfra) or `unknown` — [OpenRouter endpoints API](https://openrouter.ai/api/v1/models/moonshotai/kimi-k3/endpoints)
- Databricks availability: `databricks-deepseek-v4-1-flash` ("text, image", "Pay-per-token"), `databricks-deepseek-v4-pro-0813` ("supports reasoning and function calling"), `databricks-deepseek-v4-flash-0731`, `databricks-kimi-k3` ("text, image", 1M context) — [Databricks supported models](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/supported-models); Kimi K3 "US-hosting on Databricks Foundation Model API with native access for AWS and GCP workspaces" via Unity Gateway — [Databricks Kimi K3 blog](https://www.databricks.com/blog/kimi-k3-moonshot-ai-now-available-databricks-through-unity-ai-gateway)
- Databricks limits and reasoning defaults: 32-function cap, single system message — [Databricks FMAPI reference](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/api-reference); `reasoning_effort` defaults `max` for V4.1 Flash (accepts low/high/xhigh/max; none/disabled off; others rejected), V4 Pro 0813, V4 Flash 0731, Kimi K3 (low/high/max; others fall back to max) — [Databricks query reasoning models](https://docs.databricks.com/aws/en/machine-learning/model-serving/query-reason-models); rate limits per Q3 — [Databricks FMAPI limits](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/limits)

### Inferences (recommended `provider` blocks)
- `deepseek/deepseek-v4.1-flash`, `deepseek/deepseek-v4-pro-0813`: `{"order":["deepseek"],"allow_fallbacks":false,"require_parameters":true}`; if fallbacks are wanted, `order: ["deepseek","fireworks","together","parasail","novita"]`, `quantizations: ["fp8","bf16","unknown"]`, `ignore: ["deepinfra","venice","dekallm","phala","mara","sambanova"]`.
- `deepseek/deepseek-v4-flash-0731`: no first party; `order: ["deepinfra","baseten","parasail","nebius","novita","together"]`, `quantizations: ["fp8"]`, `ignore: ["reka","coreweave","venice","baidu"]`, `require_parameters: true`.
- `deepseek/deepseek-v3.2` / `v3.2-exp`: `order: ["siliconflow","novita","gmicloud","atlas-cloud"]`, `ignore: ["baidu","digitalocean","mara","sambanova","deepinfra"]`; `deepseek/deepseek-chat-v3.1` / `v3.1-terminus`: `order: ["siliconflow","novita","coreweave","deepinfra"]`, `ignore: ["atlas-cloud","mara","sambanova","streamlake","google-vertex"]`; `deepseek/deepseek-r1-0528`: `order: ["siliconflow","novita"]`, `only: ["siliconflow","novita"]`.
- `moonshotai/kimi-k3`, `kimi-k2.6`, `kimi-k2.7-code`: `{"order":["moonshotai"],"allow_fallbacks":false,"require_parameters":true}` and omit `temperature`/`top_p` from the request (the first-party endpoints do not list them, so `require_parameters` would otherwise exclude them); fallback candidates for K3: `fireworks`, `together`, `parasail`, `baseten`; ignore `chutes`, `modal`, `phala`, `deepinfra`, `fireworks/fast`, `alibaba`.
- `moonshotai/kimi-k2.5`: `order: ["novita","siliconflow","atlas-cloud"]`; `moonshotai/kimi-k2`, `kimi-k2-0905`: only `novita` exists; `moonshotai/kimi-k2-thinking`: `order: ["google-vertex","novita"]`.
- Databricks: send at most 32 tools (prune the Claude-Code tool set), one leading system message, `reasoning_effort` explicitly (`"high"` for cost, `"max"` default), `max_tokens` bounded by OTPM.

### Gaps
- OpenRouter does not publish tool-call error rates per endpoint in the JSON I could access (only `uptime_last_30m` and `status`; the meaning of negative `status` values is undocumented in the fetched pages).
- Databricks docs do not explicitly list tool-calling support for `databricks-deepseek-v4-1-flash` or `databricks-kimi-k3` (only V4 Pro says "function calling"); models.dev claims `tool_call: true` for `databricks-kimi-k2-7-code`. Verify with a live call.
- No Databricks endpoint exists for Kimi K2.6 or any DeepSeek V3.x/R1.

## adapter_rows (machine-usable)

Notes on encoding: `tools` = native function calling available on the listed official/first-party path; `reasoning_replay` values: `required_400` (dropping it returns HTTP 400 when tools are present), `required_as_is` (vendor says pass the full assistant message back unchanged; no documented error), `recommended` (omission tolerated), `provider_dependent` (third-party hosts; pass OpenRouter `reasoning_details` back unmodified), `none`. `temperature`/`top_p`: `"omit"` means do not send (fixed or rejected); `"ignored_in_thinking"` means silently ignored in thinking mode. `overflow_error_regex` is a Python/JS-compatible pattern over the provider error message. `max_output_*` are official-API numbers where an official ID exists, otherwise the OpenRouter top-provider value. Fields marked `null` are unknown (see Gaps above). `provider_pin` is my recommendation (INFERRED) built from the verified endpoint table.

```json
{
  "adapter_rows": [
    {
      "id_openrouter": "deepseek/deepseek-chat-v3.1",
      "id_official": null,
      "id_databricks": null,
      "family": "deepseek-v3",
      "tools": true,
      "tool_id_format": "opaque",
      "parallel_tools": true,
      "strict_mode": false,
      "reasoning_field": "reasoning (OpenRouter) / reasoning_details",
      "reasoning_replay": "provider_dependent",
      "thinking_toggle": "reasoning.enabled (OpenRouter); tool calls only work in non-thinking mode per HF card",
      "temperature": null,
      "top_p": null,
      "max_output_default": 32768,
      "max_output_cap": 147456,
      "context": 163840,
      "overflow_error_regex": "maximum context length is (\\d+) tokens|context length|too long",
      "system_placement": "single leading system (portable)",
      "vision": false,
      "provider_pin": {"order": ["siliconflow", "novita", "coreweave", "deepinfra"], "ignore": ["atlas-cloud", "mara", "sambanova", "google-vertex"], "allow_fallbacks": true, "require_parameters": true, "quantizations": ["fp8", "fp4"]},
      "quirks": ["disable reasoning when tools are sent (V3.1 'Toolcall is supported in non-thinking mode')", "raw markup <｜tool▁calls▁begin｜>/<｜tool▁sep｜> may leak on unparsed endpoints", "<think> tags may appear in content on hosts without a reasoning parser", "atlas-cloud and mara endpoints lack tools"]
    },
    {
      "id_openrouter": "deepseek/deepseek-v3.1-terminus",
      "id_official": null,
      "id_databricks": null,
      "family": "deepseek-v3",
      "tools": true,
      "tool_id_format": "opaque",
      "parallel_tools": true,
      "strict_mode": false,
      "reasoning_field": "reasoning (OpenRouter) / reasoning_details",
      "reasoning_replay": "provider_dependent",
      "thinking_toggle": "reasoning.enabled (OpenRouter); tools in non-thinking mode only",
      "temperature": null,
      "top_p": null,
      "max_output_default": 32768,
      "max_output_cap": 147456,
      "context": 163840,
      "overflow_error_regex": "maximum context length is (\\d+) tokens|context length|too long",
      "system_placement": "single leading system (portable)",
      "vision": false,
      "provider_pin": {"order": ["siliconflow", "novita", "atlas-cloud"], "ignore": ["streamlake"], "allow_fallbacks": true, "require_parameters": true, "quantizations": ["fp8"]},
      "quirks": ["same wire format as V3.1", "streamlake endpoint lacks tools"]
    },
    {
      "id_openrouter": "deepseek/deepseek-v3.2-exp",
      "id_official": null,
      "id_databricks": null,
      "family": "deepseek-v3.2",
      "tools": true,
      "tool_id_format": "opaque",
      "parallel_tools": true,
      "strict_mode": false,
      "reasoning_field": "reasoning (OpenRouter) / reasoning_details",
      "reasoning_replay": "provider_dependent",
      "thinking_toggle": "reasoning.enabled (OpenRouter); thinking+tools supported from V3.2",
      "temperature": 1.0,
      "top_p": 0.95,
      "max_output_default": 65536,
      "max_output_cap": 147456,
      "context": 163840,
      "overflow_error_regex": "maximum context length is (\\d+) tokens|context length|too long",
      "system_placement": "single leading system (portable)",
      "vision": false,
      "provider_pin": {"order": ["siliconflow", "novita", "atlas-cloud"], "allow_fallbacks": true, "require_parameters": true, "quantizations": ["fp8"]},
      "quirks": ["DSML tool-call markup may leak into content", "model emits both legacy and DSML formats; host parser may miss one"]
    },
    {
      "id_openrouter": "deepseek/deepseek-v3.2",
      "id_official": null,
      "id_databricks": null,
      "family": "deepseek-v3.2",
      "tools": true,
      "tool_id_format": "opaque",
      "parallel_tools": true,
      "strict_mode": false,
      "reasoning_field": "reasoning (OpenRouter) / reasoning_details",
      "reasoning_replay": "provider_dependent",
      "thinking_toggle": "reasoning.enabled (OpenRouter); thinking+tools supported",
      "temperature": 1.0,
      "top_p": 0.95,
      "max_output_default": 65536,
      "max_output_cap": 147456,
      "context": 163840,
      "overflow_error_regex": "maximum context length is (\\d+) tokens|context length|too long",
      "system_placement": "single leading system (portable)",
      "vision": false,
      "provider_pin": {"order": ["siliconflow", "novita", "gmicloud", "atlas-cloud"], "ignore": ["baidu", "digitalocean", "mara", "sambanova", "deepinfra"], "allow_fallbacks": true, "require_parameters": true, "quantizations": ["fp8"]},
      "quirks": ["official deepseek-chat/deepseek-reasoner IDs retired 2026-07-24", "DSML leakage (<｜DSML｜function_calls>/<｜DSML｜invoke>) reported on Bedrock and others", "deepinfra fp4 endpoint caps output at 16384", "V3.2-Speciale variant has no tool calling and no current hosting found"]
    },
    {
      "id_openrouter": null,
      "id_official": "deepseek-v3.2-speciale (temporary endpoint, ended 2025-12-15)",
      "id_databricks": null,
      "family": "deepseek-v3.2",
      "tools": false,
      "tool_id_format": null,
      "parallel_tools": false,
      "strict_mode": false,
      "reasoning_field": "reasoning_content",
      "reasoning_replay": "none",
      "thinking_toggle": "always thinking",
      "temperature": 1.0,
      "top_p": 0.95,
      "max_output_default": null,
      "max_output_cap": null,
      "context": null,
      "overflow_error_regex": null,
      "system_placement": null,
      "vision": false,
      "provider_pin": null,
      "quirks": ["'does not support the tool-calling functionality' (HF card)", "not present on OpenRouter model list as of 2026-09-23"]
    },
    {
      "id_openrouter": "deepseek/deepseek-r1-0528",
      "id_official": null,
      "id_databricks": null,
      "family": "deepseek-r1",
      "tools": true,
      "tool_id_format": "opaque",
      "parallel_tools": true,
      "strict_mode": false,
      "reasoning_field": "reasoning (OpenRouter) / reasoning_details; <think> tags on raw hosts",
      "reasoning_replay": "provider_dependent",
      "thinking_toggle": "always thinking",
      "temperature": "omit (Aider use_temperature: false)",
      "top_p": null,
      "max_output_default": 32768,
      "max_output_cap": 147456,
      "context": 163840,
      "overflow_error_regex": "maximum context length is (\\d+) tokens|context length|too long",
      "system_placement": "single leading system (portable)",
      "vision": false,
      "provider_pin": {"only": ["siliconflow", "novita"], "order": ["siliconflow", "novita"], "allow_fallbacks": false, "require_parameters": true},
      "quirks": ["only siliconflow and novita endpoints expose tools", "deepseek/deepseek-r1 (original) has a single novita endpoint with 64000 ctx / 16000 out", "Aider caps max_tokens 8192 and sets include_reasoning"]
    },
    {
      "id_openrouter": "deepseek/deepseek-v4-flash-0731",
      "id_official": "deepseek-v4-flash (legacy name; now routed to V4.1 Flash)",
      "id_databricks": "databricks-deepseek-v4-flash-0731",
      "family": "deepseek-v4",
      "tools": true,
      "tool_id_format": "opaque",
      "parallel_tools": true,
      "strict_mode": "beta (official only: base_url /beta, strict:true)",
      "reasoning_field": "reasoning_content (official/Databricks) / reasoning + reasoning_details (OpenRouter)",
      "reasoning_replay": "required_400",
      "thinking_toggle": "thinking.type enabled|disabled, thinking.reasoning_effort none|low|high|max (official); reasoning_effort low|high|max default max (Databricks); reasoning.effort (OpenRouter)",
      "temperature": "1.0 (ignored_in_thinking)",
      "top_p": "1.0 (thinking mode clamps to 0.95-1.0)",
      "max_output_default": 65536,
      "max_output_cap": 384000,
      "context": 1048576,
      "overflow_error_regex": "maximum context length is (\\d+) tokens\\. However, you requested (\\d+) tokens|Input token exceed the limit",
      "system_placement": "single leading system (Databricks: system only once, first)",
      "vision": false,
      "provider_pin": {"order": ["deepinfra", "baseten", "parasail", "nebius", "novita", "together"], "ignore": ["reka", "coreweave", "venice", "baidu"], "allow_fallbacks": true, "require_parameters": true, "quantizations": ["fp8"]},
      "quirks": ["no first-party deepseek endpoint on OpenRouter for this ID", "DSML leakage class A/B/C documented", "Databricks OTPM 10,000", "OpenRouter model-level context 1310720 comes from Cloudflare; most endpoints 1048576", "reasoning_content must be replayed verbatim incl. empty string when tools present"]
    },
    {
      "id_openrouter": "deepseek/deepseek-v4.1-flash",
      "id_official": "deepseek-flash",
      "id_databricks": "databricks-deepseek-v4-1-flash",
      "family": "deepseek-v4",
      "tools": true,
      "tool_id_format": "opaque",
      "parallel_tools": true,
      "strict_mode": "beta (official only: base_url /beta, strict:true; all props required, additionalProperties false)",
      "reasoning_field": "reasoning_content (official/Databricks) / reasoning + reasoning_details (OpenRouter)",
      "reasoning_replay": "required_400",
      "thinking_toggle": "thinking.type enabled (default) | disabled; thinking.reasoning_effort none|low|high|max default high (official); reasoning_effort low|high|xhigh|max default max, none|disabled off, others rejected (Databricks); reasoning.effort (OpenRouter)",
      "temperature": "1.0 (ignored_in_thinking; non-thinking range 0-2 default 1)",
      "top_p": "0.95-1.0 (thinking mode clamps below 0.95 to 0.95)",
      "max_output_default": "8K non-thinking / 64K thinking / 128K at reasoning_effort max",
      "max_output_cap": 384000,
      "context": 1048576,
      "overflow_error_regex": "maximum context length is (\\d+) tokens\\. However, you requested (\\d+) tokens|Input token exceed the limit",
      "system_placement": "single leading system (Databricks: system only once, first)",
      "vision": true,
      "provider_pin": {"order": ["deepseek"], "allow_fallbacks": false, "require_parameters": true},
      "quirks": ["tool_choice required/named returns 400 in thinking mode", "temperature/presence_penalty/frequency_penalty silently ignored in thinking mode", "exact 400: The `reasoning_content` in the thinking mode must be passed back to the API.", "legacy names deepseek-v4-flash and deepseek-v4-flash-vision-exp route here", "OpenRouter top_provider max_completion_tokens shows 131072 but the deepseek endpoint allows 393216", "image_url parts JPEG/PNG/GIF/WebP with detail low|high|original|auto", "off-peak pricing 50% of peak"]
    },
    {
      "id_openrouter": "deepseek/deepseek-v4-pro-0813",
      "id_official": "deepseek-v4-pro",
      "id_databricks": "databricks-deepseek-v4-pro-0813",
      "family": "deepseek-v4",
      "tools": true,
      "tool_id_format": "opaque",
      "parallel_tools": true,
      "strict_mode": "beta (official only: base_url /beta, strict:true)",
      "reasoning_field": "reasoning_content (official/Databricks) / reasoning + reasoning_details (OpenRouter)",
      "reasoning_replay": "required_400",
      "thinking_toggle": "thinking.type enabled (default) | disabled; thinking.reasoning_effort none|low|high|max default high (official); reasoning_effort low|high|max default max (Databricks); reasoning.effort (OpenRouter)",
      "temperature": "1.0 (ignored_in_thinking)",
      "top_p": "1.0 (thinking mode clamps to 0.95-1.0)",
      "max_output_default": "8K non-thinking / 64K thinking / 128K at max",
      "max_output_cap": 384000,
      "context": 1048576,
      "overflow_error_regex": "maximum context length is (\\d+) tokens\\. However, you requested (\\d+) tokens|Input token exceed the limit",
      "system_placement": "single leading system (Databricks: system only once, first)",
      "vision": false,
      "provider_pin": {"order": ["deepseek"], "allow_fallbacks": false, "require_parameters": true},
      "quirks": ["CONFLICT: news page says routed to V4.1-Flash from 2026-09-14, pricing page still lists it on 2026-09-23 (reported reversal)", "empty completions (completion_tokens=0) after tool results reported", "repeated identical tool calls from ~100k ctx reported; add circuit breaker", "Databricks OTPM 4,000", "fireworks endpoint degraded (status -5), deepinfra caps 16384 out", "Think Max recommended context >= 384K (open weights)"]
    },
    {
      "id_openrouter": "moonshotai/kimi-k2",
      "id_official": "kimi-k2-0711-preview (discontinued 2026-05-25; routes to kimi-k3)",
      "id_databricks": null,
      "family": "kimi-k2",
      "tools": true,
      "tool_id_format": "functions.{name}:{idx}",
      "parallel_tools": true,
      "strict_mode": false,
      "reasoning_field": null,
      "reasoning_replay": "none",
      "thinking_toggle": "n/a (non-thinking)",
      "temperature": 0.6,
      "top_p": null,
      "max_output_default": 98304,
      "max_output_cap": 98304,
      "context": 131072,
      "overflow_error_regex": "exceeded model token limit: (\\d+) \\(requested: (\\d+)\\)|context length|too long",
      "system_placement": "single leading system (portable)",
      "vision": false,
      "provider_pin": {"only": ["novita"], "allow_fallbacks": false, "require_parameters": true},
      "quirks": ["only novita/fp8 endpoint; no response_format", "tool calls returned as JSON text / raw <|tool_calls_section_begin|> tokens reported via OpenRouter in 2025", "normalize historical tool_call ids to functions.name:idx", "vLLM parser kimi_k2"]
    },
    {
      "id_openrouter": "moonshotai/kimi-k2-0905",
      "id_official": "kimi-k2-0905-preview (discontinued 2026-05-25; routes to kimi-k3)",
      "id_databricks": null,
      "family": "kimi-k2",
      "tools": true,
      "tool_id_format": "functions.{name}:{idx}",
      "parallel_tools": true,
      "strict_mode": false,
      "reasoning_field": null,
      "reasoning_replay": "none",
      "thinking_toggle": "n/a (non-thinking)",
      "temperature": 0.6,
      "top_p": null,
      "max_output_default": 98304,
      "max_output_cap": 98304,
      "context": 262144,
      "overflow_error_regex": "exceeded model token limit: (\\d+) \\(requested: (\\d+)\\)|context length|too long",
      "system_placement": "single leading system (portable)",
      "vision": false,
      "provider_pin": {"only": ["novita"], "allow_fallbacks": false, "require_parameters": true},
      "quirks": ["only novita/fp8 endpoint", "use HF commit 94a4053 or later for fixed chat template (vLLM blog)"]
    },
    {
      "id_openrouter": "moonshotai/kimi-k2-thinking",
      "id_official": "kimi-k2-thinking (discontinued; routes to kimi-k3)",
      "id_databricks": null,
      "family": "kimi-k2",
      "tools": true,
      "tool_id_format": "functions.{name}:{idx}",
      "parallel_tools": true,
      "strict_mode": false,
      "reasoning_field": "reasoning_content (raw) / reasoning + reasoning_details (OpenRouter)",
      "reasoning_replay": "recommended",
      "thinking_toggle": "always thinking",
      "temperature": 1.0,
      "top_p": null,
      "max_output_default": 98304,
      "max_output_cap": 235929,
      "context": 262144,
      "overflow_error_regex": "exceeded model token limit: (\\d+) \\(requested: (\\d+)\\)|context length|too long",
      "system_placement": "single leading system (portable)",
      "vision": false,
      "provider_pin": {"order": ["google-vertex", "novita"], "allow_fallbacks": true, "require_parameters": true},
      "quirks": ["native INT4 (QAT)", "KimiK2Detector streaming parser truncation on SGLang hosts; use stream:false if arguments get cut"]
    },
    {
      "id_openrouter": "moonshotai/kimi-k2.5",
      "id_official": "kimi-k2.5 (discontinued 2026-08-31; routes to kimi-k3)",
      "id_databricks": null,
      "family": "kimi-k2.5",
      "tools": true,
      "tool_id_format": "functions.{name}:{idx}",
      "parallel_tools": true,
      "strict_mode": false,
      "reasoning_field": "reasoning_content (raw) / reasoning + reasoning_details (OpenRouter)",
      "reasoning_replay": "recommended",
      "thinking_toggle": "thinking.type enabled|disabled (legacy K2.x style) / reasoning.enabled (OpenRouter)",
      "temperature": 1.0,
      "top_p": null,
      "max_output_default": 235929,
      "max_output_cap": 235929,
      "context": 262144,
      "overflow_error_regex": "exceeded model token limit: (\\d+) \\(requested: (\\d+)\\)|context length|too long",
      "system_placement": "single leading system (portable)",
      "vision": true,
      "provider_pin": {"order": ["novita", "siliconflow", "atlas-cloud"], "ignore": ["venice"], "allow_fallbacks": true, "require_parameters": true, "quantizations": ["int4", "unknown"]},
      "quirks": ["sanitising tool_call ids (functions.read:0 -> functionsread0) drops pass rate to 20%", "streaming parser hangs/truncation on SGLang hosts; stream:false mitigation", "amazon-bedrock endpoint caps output at 131072"]
    },
    {
      "id_openrouter": "moonshotai/kimi-k2.6",
      "id_official": "kimi-k2.6",
      "id_databricks": null,
      "family": "kimi-k2.6",
      "tools": true,
      "tool_id_format": "functions.{name}:{idx} (preserve as returned)",
      "parallel_tools": true,
      "strict_mode": "response_format json_schema only",
      "reasoning_field": "reasoning_content (official) / reasoning + reasoning_details (OpenRouter)",
      "reasoning_replay": "recommended (set thinking.keep = \"all\" to enforce)",
      "thinking_toggle": "thinking.type enabled (default) | disabled; thinking.keep null|\"all\" (official); reasoning.enabled (OpenRouter)",
      "temperature": "omit (fixed 1.0 thinking / 0.6 non-thinking; other values error)",
      "top_p": "omit (fixed 0.95; other values error)",
      "max_output_default": 32768,
      "max_output_cap": 235929,
      "context": 262144,
      "overflow_error_regex": "exceeded model token limit: (\\d+) \\(requested: (\\d+)\\)|Input token length too long|prompt tokens \\+ max_tokens exceeds",
      "system_placement": "single leading system; extra system messages allowed for dynamic tool loading",
      "vision": true,
      "provider_pin": {"order": ["moonshotai"], "allow_fallbacks": false, "require_parameters": true},
      "quirks": ["tool_choice only auto|none (required errors)", "n=1, penalties=0 fixed", "images base64 only (no URLs)", "$web_search incompatible with thinking", "OpenRouter first-party endpoint omits temperature/top_p from supported_parameters", "OpenRouter 'Invalid request: tokenization failed' reported from one harness", "deepinfra fp4 caps 16384 out, chutes 65535"]
    },
    {
      "id_openrouter": "moonshotai/kimi-k2.7-code",
      "id_official": "kimi-k2.7-code | kimi-k2.7-code-highspeed",
      "id_databricks": "databricks-kimi-k2-7-code (models.dev only; not confirmed on Databricks docs)",
      "family": "kimi-k2.7",
      "tools": true,
      "tool_id_format": "functions.{name}:{idx} (preserve as returned)",
      "parallel_tools": true,
      "strict_mode": "response_format json_schema only",
      "reasoning_field": "reasoning_content (official) / reasoning + reasoning_details (OpenRouter)",
      "reasoning_replay": "required_as_is (Preserved Thinking always on)",
      "thinking_toggle": "always on; thinking.type must be enabled (disabled returns an error); thinking.keep only \"all\"; reasoning_effort not supported",
      "temperature": "omit (fixed 1.0; other values error)",
      "top_p": "omit (fixed 0.95; other values error)",
      "max_output_default": 32768,
      "max_output_cap": 235929,
      "context": 262144,
      "overflow_error_regex": "exceeded model token limit: (\\d+) \\(requested: (\\d+)\\)|Input token length too long|prompt tokens \\+ max_tokens exceeds",
      "system_placement": "single leading system; extra system messages allowed for dynamic tool loading",
      "vision": true,
      "provider_pin": {"order": ["moonshotai"], "allow_fallbacks": false, "require_parameters": true},
      "quirks": ["tool_choice only auto|none", "highspeed variant ~180 tok/s at 2x price (moonshotai/highspeed slug)", "deepinfra endpoint status -2 and 16384 out; alibaba 16384; streamlake 32000 and no temperature param", "OpenRouter endpoints expose reasoning but not reasoning_effort"]
    },
    {
      "id_openrouter": "moonshotai/kimi-k3",
      "id_official": "kimi-k3",
      "id_databricks": "databricks-kimi-k3",
      "family": "kimi-k3",
      "tools": true,
      "tool_id_format": "preserve as returned (K2 lineage functions.{name}:{idx}; not documented for K3)",
      "parallel_tools": true,
      "strict_mode": "response_format json_schema strict:true",
      "reasoning_field": "reasoning_content (official) / reasoning + reasoning_details (OpenRouter) / reasoning content item (Databricks)",
      "reasoning_replay": "required_as_is (full assistant message incl. reasoning_content and tool_calls)",
      "thinking_toggle": "always on; top-level reasoning_effort low|high|max default max (official, Databricks); no thinking param (remove K2.x thinking config); reasoning.effort (OpenRouter)",
      "temperature": "omit (fixed 1.0)",
      "top_p": "omit (fixed 0.95)",
      "max_output_default": 131072,
      "max_output_cap": 1048576,
      "context": 1048576,
      "overflow_error_regex": "exceeded model token limit: (\\d+) \\(requested: (\\d+)\\)|Input token length too long|prompt tokens \\+ max_tokens exceeds",
      "system_placement": "single leading system (Databricks: system only once, first); extra system messages allowed on Moonshot for dynamic tool loading",
      "vision": true,
      "provider_pin": {"order": ["moonshotai"], "allow_fallbacks": false, "require_parameters": true, "fallback_order": ["fireworks", "together", "parasail", "baseten"], "ignore": ["chutes", "modal", "phala", "deepinfra", "fireworks/fast", "alibaba"]},
      "quirks": ["tool_choice required supported", "n=1, penalties=0 fixed; omit all sampling params", "vision: base64 or ms://<file-id> only, content must be an array", "prompt cache hits only when previous prompt > 256 tokens; prompt_cache_options.ttl 5m|1h", "Databricks: other reasoning_effort values fall back to max; ITPM 2M / OTPM 40K / QPH 7,200", "OpenRouter :batch variant caps output at 16384", "flattening prior thinking into text makes K3 drop out of reasoning mode; keep reasoning_content on every assistant message"]
    }
  ]
}
```
