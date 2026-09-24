# GLM / Qwen / MiniMax per-model adapter rules for a Claude-Code-like harness over OpenAI-compatible APIs (as of 2026-09-23)

Scope: Z.ai (Zhipu) GLM-4.5 / 4.6 / 4.7 / 5 / 5.2 / 5.3 / 5.3-Flash; Alibaba Qwen3-235B-A22B-2507 (instruct/thinking), Qwen3-Coder (480B/Plus, Flash, Next), Qwen3-Next-80B, Qwen3.5, Qwen3-Max, QwQ; MiniMax M1 / M2 / M2.1 — on vendor APIs, OpenRouter and Databricks. Live OpenRouter catalog/endpoint data was pulled with `curl` from `https://openrouter.ai/api/v1/models` and `https://openrouter.ai/api/v1/models/<id>/endpoints` on 2026-09-23; models.dev registry (`https://models.dev/api.json`) was pulled the same day. Everything marked **[inference]** is my reading, not a vendor statement.

Landscape note (verified from the live catalog): the requested IDs are now "previous generation". OpenRouter also lists `z-ai/glm-5.1`, `glm-5-turbo`, `glm-5.3-flashx`, `glm-5.3-prime`, `glm-5v-turbo`; `qwen/qwen3.6-*`, `qwen3.7-*`, `qwen3.8-*`; `minimax/minimax-m2.5`, `m2.7`, `m3`. `qwen/qwq-32b`, `qwen/qwen3-coder:free` and `qwen/qwen3-235b-a22b:free` **no longer exist** on OpenRouter (the `/endpoints` call returns no data); the only `:free` variants in these three families today are `qwen/qwen3.8-27b:free` and `z-ai/glm-5.2:free` (the latter has **no tool support**). No `:exacto` variants exist for these families. — [OpenRouter /api/v1/models](https://openrouter.ai/api/v1/models)

---

## Key question 1 — Tool calling (native support, id formats, parallel calls, streaming, limits, strict mode, text-serialised leakage/markup, server-side parsers, OpenRouter provider support)

### Takeaway
All three families are native function-callers on their vendor APIs, but each has a different text wire format that leaks whenever a parser is mis-configured: GLM emits `<tool_call>name<arg_key>k</arg_key><arg_value>v</arg_value></tool_call>` (vLLM `glm45`/`glm47`), Qwen3 general models emit Hermes `<tool_call>{json}</tool_call>` (`hermes`), Qwen3-Coder/Qwen3.5 emit `<tool_call><function=name><parameter=k>v</parameter></function></tool_call>` (`qwen3_coder` / `qwen3_xml`), MiniMax M2.x emits `<minimax:tool_call><invoke name=...><parameter name=...>` (`minimax_m2`), MiniMax M1 emits `<tool_calls>{json}</tool_calls>` (`minimax`). Z.ai caps tools at 128 and only supports `tool_choice: "auto"`; Databricks caps tools at 32 and says parallel calls are unsupported; DashScope defaults `parallel_tool_calls` to false and rejects `tool_choice: "required"`; MiniMax validates client-supplied `tool_call_id`s (error 2013).

### Cited Findings

**GLM (Z.ai official API)**
- Request supports `tools` with "Max 128 functions supported" (types `function`, `retrieval`, `web_search`); `tool_choice` "`auto` (only supported value)"; `tool_stream` boolean "Default `false`; supported by GLM-5.3/5.2/5.1/5/4.7/4.6"; response `tool_calls[].id` is a "Unique call identifier", `function.name` "(a-z, A-Z, 0-9, underscore, dash; max 64 chars)"; `finish_reason` values are `"stop"`, `"tool_calls"`, `"length"`, `"sensitive"`, `"model_context_window_exceeded"`, `"network_error"` — [Z.ai Chat Completion API reference](https://docs.z.ai/api-reference/llm/chat-completion)
- Function-calling guide: tool results go back as `{"role": "tool", "content": json.dumps(result), "tool_call_id": tool_call.id}`; `function.arguments` is a "JSON format string"; only `tool_choice='auto'` documented — [Z.ai Function Calling](https://docs.z.ai/guides/capabilities/function-calling)
- `response_format` supports only `{"type": "json_object"}`; models listed: glm-5, glm-4.7, glm-4.5, glm-4.6; no `json_schema`/strict mode documented — [Z.ai Structured Output](https://docs.z.ai/guides/capabilities/struct-output)
- GLM-5.3 model page lists "Function Calling", "Structured Output", "Context Caching" capabilities; "GLM-5.3 currently supports text-only inputs" — [Z.ai GLM-5.3](https://docs.z.ai/guides/llm/glm-5.3)
- On the Z.ai Messages (Anthropic-protocol) API, the Responses API produced "2 parallel function_call items in one turn", and "Messages API continues producing tool calls after `tool_choice: {"type": "none"}`, unlike Chat Completions where this parameter works correctly" — [AIHubMix GLM-5.3 hands-on guide (third party)](https://aihubmix.com/blog/glm-5-3-hands-on-guide-always-on-thinking-three-effort-levels-and-the-api-support-matrix)
- OpenClaw's Z.AI provider notes: "tool_stream is enabled by default for Z.AI tool-call streaming" — [OpenClaw Z.AI provider docs](https://docs.openclaw.ai/providers/zai)
- Alibaba Model Studio's function-calling doc (which also hosts GLM) warns that for GLM models "you must include `extra_body={"tool_stream": True}`" or results won't return tool calls; "this constraint does not apply to Qwen" — [Model Studio: Qwen function calling](https://www.alibabacloud.com/help/en/model-studio/qwen-function-calling)

**GLM text wire format and parsers**
- vLLM: `--tool-call-parser glm45` for "zai-org/GLM-4.5, GLM-4.5-Air, GLM-4.6"; `--tool-call-parser glm47` for "zai-org/GLM-4.7, GLM-4.7-Flash"; generic launch pattern `vllm serve [model] --enable-auto-tool-choice --tool-call-parser [parser-name]` — [vLLM Tool Calling docs](https://docs.vllm.ai/en/latest/features/tool_calling.html)
- GLM-4.7 HF card serving commands: vLLM `--tool-call-parser glm47 --reasoning-parser glm45`; SGLang `--tool-call-parser glm47 --reasoning-parser glm45` — [HF zai-org/GLM-4.7](https://huggingface.co/zai-org/GLM-4.7)
- GLM-5 HF card: `vllm serve zai-org/GLM-5 --tensor-parallel-size 8 --tool-call-parser glm47 --reasoning-parser glm45`; same for SGLang — [HF zai-org/GLM-5](https://huggingface.co/zai-org/GLM-5)
- glm47 parser format: `<tool_call>` opening tag, function name on first line, paired `<arg_key>`/`<arg_value>` tags, `</tool_call>`; regex `"<tool_call>\\s*([^\\n<]+?)(?:\\n|\\s*)(<arg_key>.*?)?</tool_call>"`; values deserialised with "json.loads for type coercion, falls back to raw string"; streaming: "Once <tool_call> is detected, buffer everything until it closes. Do NOT emit content deltas here"; parallel calls via `findall()`; ids `"call_{uuid.hex[:8]}"` — [vllm-mlx glm47_tool_parser reference](https://vllm-mlx.is-a.dev/reference/api/vllm_mlx/tool_parsers/glm47_tool_parser/)
- With `tool_choice: "required"`, GLM-5.2 emitted `"<tool_call>[{"name": "bash", "parameters": {"command": "cd /tmp/repo && git status", ...}}]"` (a JSON array inside `<tool_call>`), which the glm47 parser left unparsed in `content`; with `auto` it parses correctly — [vLLM issue #48095](https://github.com/vllm-project/vllm/issues/48095)
- GLM-5.3-Flash with forced named `tool_choice` "fails to converge — runs to max_tokens and returns ... truncated tool_call.arguments" — [vLLM issue #55541](https://github.com/vllm-project/vllm/issues/55541)
- The official GLM chat templates render tool-call argument values with `"{{ v | tojson(ensure_ascii=False) if v is not string else v }}"`; ms-swift's GLM-4.5/4.7/5.1 agent templates instead rendered Python reprs (`True` not `true`, `None` not `null`, `{'tags': ['a', 'b']}`) and "A model fine-tuned on this data learns to emit Python reprs that the GLM tool parsers cannot read as JSON" — [ms-swift PR #10169](https://github.com/modelscope/ms-swift/pull/10169)
- GLM-5.3-Flash chat template crashes with `"jinja2.exceptions.UndefinedError: 'str object' has no attribute 'items'"` (line 163 calls `tc.arguments.items()` unconditionally) when history `tool_calls[].function.arguments` is a JSON **string** (OpenAI wire format) rather than a dict; "sglang server is unaffected because it normalizes strings to dicts internally before templating" — [zai-org/GLM-5 issue #158](https://github.com/zai-org/GLM-5/issues/158)
- GLM-4.5 template issue: "chat template can render Python dict arguments as a dict representation instead of JSON, which causes the null content field of the assistant message to be rendered as Python's None string"; reasoning parser loses `reasoning_content` separation from turn 3 onward after tool calls (`<think>` leaks into `content`) — [vLLM issue #27703](https://github.com/vllm-project/vllm/issues/27703)
- GLM-4.7 via SGLang + Claude Code: streamed markup like `"<tool_call><tool_call><tool_call>Read"` and `"<tool cal>Todo...TodoWrite"`, tool name polluted to `"<tool_call>Read"`, parser crash `"AttributeError: 'NoneType' object has no attribute 'strip'"` in `glm47_moe_detector.py` (flags `--tool-call-parser glm47 --reasoning-parser glm45`) — [SGLang issue #15721](https://github.com/sgl-project/sglang/issues/15721)
- GLM-4.7-Flash in vLLM 0.16.0 "does not return tool_calls field ... even with --tool-call-parser glm47" — [vLLM issue #36833](https://github.com/vllm-project/vllm/issues/36833)
- GLM-5.1-FP8: tool results ignored via `/v1/chat/completions` with `--tool-call-parser glm47` but work via `/v1/completions` — [vLLM issue #39611](https://github.com/vllm-project/vllm/issues/39611)
- Tool-call ids on Mistral-hosted GLM-5.2/5.3 change mid-stream: first chunk `"chatcmpl-tool-b6f76882211cffb5"`, continuation chunks `"toolcall0"`, on the same `index`; clients keyed by id split them into two calls; fix is to aggregate deltas by `index` — [mistral-vibe issue #1127](https://github.com/mistralai/mistral-vibe/issues/1127)

**Qwen (DashScope / Model Studio official)**
- Supported: Qwen3-Max, Qwen3-Coder, Qwen3.5/3.6/3.7/3.8, Qwen3 open-source, Qwen-VL; `parallel_tool_calls` — "Default appears to be `false` (serial calling)"; `tool_choice` values `"auto"`, `{"type":"function","function":{"name":...}}`, `"none"`; `"required"` — "not supported for Qwen models"; ids follow `"call_[hexadecimal]"` e.g. `"call_6596dafa2a6a46f7a217da"`; streaming: "The parameter information for the tool call is returned in chunks as a data stream" (accumulate `arguments`); "Make sure the tool's output is in string format"; no documented max tools; strict/JSON-schema not mentioned — [Model Studio: Qwen function calling](https://www.alibabacloud.com/help/en/model-studio/qwen-function-calling)
- Qwen3 general models: "the chat template in tokenizer_config.json has already included support for the Hermes-style tool use"; serve with `--tool-call-parser hermes --reasoning-parser deepseek_r1`; parallel calls demonstrated; `reasoning_content` appears alongside `tool_calls` — [Qwen docs: Function Calling](https://qwen.readthedocs.io/en/latest/framework/function_call.html)
- Hermes parser format: `<tool_call>{"name": "function_name", "arguments": {"param": "value"}}</tool_call>`; regex `"<tool_call>(.*?)</tool_call>|<tool_call>(.*)"` (DOTALL); streaming diffs arguments via `_compute_args_diff()` — [vLLM hermes_tool_parser reference](https://docs.vllm.ai/en/latest/api/vllm/tool_parsers/hermes_tool_parser/)
- Qwen3-Coder parser (shipped with the model): delimiters `"<tool_call>"`, `"</tool_call>"`, `"<function="`, `"</function>"`, `"<parameter="`, `"</parameter>"`; regexes `r"<tool_call>(.*?)</tool_call>"`, `r"<function=(.*?)</function>|<function=(.*)$"`, `r"<parameter=(.*?)(?:</parameter>|(?=<parameter=)|(?=</function>)|$)"`; ids `"call_{uuid.uuid4().hex[:24]}"`; `_convert_param_value`: `'null'` → None; string/str/text/varchar/char/enum unchanged; `int*`/`uint*`/`long`/`short`/`unsigned` via `int()` (warning on failure); `num*`/`float*` via `float()`; boolean compares `"true"`/`"false"`; object/array/list/dict* via `json.loads()` falling back to `ast.literal_eval()`; "Logs warning if parameter not in schema and returns the string value" — [HF Qwen3-Coder-480B qwen3coder_tool_parser.py](https://huggingface.co/Qwen/Qwen3-Coder-480B-A35B-Instruct/raw/main/qwen3coder_tool_parser.py)
- vLLM docs: `--tool-call-parser qwen3_xml` "Recommended for: Qwen/Qwen3-Coder models (30B and 480B variants)" — [vLLM Tool Calling docs](https://docs.vllm.ai/en/latest/features/tool_calling.html); Qwen3-Coder-Next card: `vllm serve Qwen/Qwen3-Coder-Next ... --enable-auto-tool-choice --tool-call-parser qwen3_coder` — [HF Qwen/Qwen3-Coder-Next](https://huggingface.co/Qwen/Qwen3-Coder-Next); Qwen3.5-122B card: "Use `--tool-call-parser qwen3_coder`" and `--reasoning-parser qwen3` — [HF Qwen/Qwen3.5-122B-A10B](https://huggingface.co/Qwen/Qwen3.5-122B-A10B)
- Qwen3-Coder-30B FP8 "would frequently omit the initial <tool_call> tag on tool calls, especially when the tool call was attempted after a textual response"; the corrected (Unsloth) template adds: "Function calls HAVE to be enclosed within <tool_call> and </tool_call> tags" and "Do NOT omit the initial <tool_call> tag" — [QwenLM/Qwen3-Coder issue #475](https://github.com/QwenLM/Qwen3-Coder/issues/475)
- Qwen models "occasionally emit tool_calls[].function.arguments as Python literals" e.g. `"{'city': 'Paris'}"`; replaying that verbatim gets HTTP 400 from strict providers: `"messages 字段值不符合规范，tool_calls的function的arguments必须是有效的JSON格式"` — [agno issue #10231](https://github.com/agno-agi/agno/issues/10231)
- Qwen Code's own system prompt switches tool-call example notation by model regex: `/qwen[^-]*-coder/i` → `<function=SHELL><parameter=command>node server.js</parameter></function>`; `/qwen[^-]*-vl/i` → `{"name": "SHELL", "arguments": {"command": "node server.js"}}`; `/gemma[-_]?4/i` → `<|tool_call>call:SHELL{command:<|"|>node server.js<|"|>}<tool_call|>`; default → `[tool_call: SHELL for 'node server.js']`; override env `QWEN_CODE_TOOL_CALL_STYLE` — [qwen-code prompts.ts](https://raw.githubusercontent.com/QwenLM/qwen-code/main/packages/core/src/core/prompts.ts)

**MiniMax**
- OpenAI-compatible API (`https://api.minimax.io/v1`): `tools` supported; deprecated `function_call` "unsupported"; `n` only 1; `presence_penalty, frequency_penalty, logit_bias` ignored; "The complete response 'including the `tool_calls` field' must be appended to conversation history" — [MiniMax OpenAI-compatible API](https://platform.minimax.io/docs/api-reference/text-openai-api)
- Anthropic-compatible API (`https://api.minimax.io/anthropic`): models MiniMax-M3, M2.7(+highspeed), M2.5(+highspeed), M2.1(+highspeed), M2; `tools`, `tool_choice`, `thinking`, `stream` "Fully Supported"; `top_k`, `stop_sequences`, `mcp_servers`, `context_management`, `container` ignored; temperature "Range [0, 2]" and "values outside this range will return an error" — [MiniMax Anthropic-compatible API](https://platform.minimax.io/docs/api-reference/text-anthropic-api)
- M2.1 text markup: `<minimax:tool_call>\n<invoke name="function_name">\n<parameter name="param_name">param_value</parameter>\n</invoke>\n</minimax:tool_call>`; regexes `r"<minimax:tool_call>(.*?)</minimax:tool_call>"`, `r"<invoke name=(.*?)</invoke>"`, `r"<parameter name=(.*?)</parameter>"`; tool results as `"role": "tool", "content": [{"name": function_name, "type": "text", "text": ...}]`; "We strongly recommend using vLLM or SGLang for parsing tool calls." — [MiniMax-M2.1 tool_calling_guide.md](https://raw.githubusercontent.com/MiniMax-AI/MiniMax-M2.1/main/docs/tool_calling_guide.md)
- vLLM minimax_m2 parser format: `<minimax:tool_call><invoke name="get_weather">\n<parameter name="city">Seattle</parameter>\n</invoke></minimax:tool_call>` — [vLLM minimax_m2 parser reference](https://docs.vllm.ai/en/latest/api/vllm/parser/minimax_m2/); recommended flags `--tool-call-parser minimax_m2 --reasoning-parser minimax_m2_append_think` — [JarvisLabs M2.1 vLLM guide (third party)](https://jarvislabs.ai/blog/minimax-m21-vllm-deployment-guide)
- Tool-call arguments "cannot safely contain raw `</parameter>` delimiters" (M2.1/M2.5/M2.7) — [vLLM issue #44060](https://github.com/vllm-project/vllm/issues/44060); combining `--tool-call-parser minimax_m2` with `--reasoning-parser minimax_m2_append_think` "drops the closing </think> from content when parsing a tool call" — [vLLM issue #58486](https://github.com/vllm-project/vllm/issues/58486)
- M1 markup: `<tool_calls>\n{"name": "search_web", "arguments": {...}}\n</tool_calls>` (one JSON object per line); tools introduced with `"If you need to call tools, please respond with <tool_calls></tool_calls> XML tags"`; vLLM `"--tool-call-parser minimax"` — [MiniMax-M1 function_call_guide.md](https://huggingface.co/MiniMaxAI/MiniMax-M1-80k/raw/main/docs/function_call_guide.md)
- MiniMax rejects malformed tool calls with code 2013: `"invalid params, invalid function arguments json string, tool_call_id: call_function_60llhpv7vhhb_1 (2013)"` (HTTP 400, non-retryable) — [hermes-agent issue #12167](https://github.com/NousResearch/hermes-agent/issues/12167); `"[Model Rejected] invalid request: tool_call id format is not supported"` when a compaction plugin generated its own ids — [openclaw issue #66892](https://github.com/openclaw/openclaw/issues/66892); `"Minimax error: invalid params, tool call id is invalid (2013)"` (Kilo Code, native-tool-calls label) — [kilocode issue #3967](https://github.com/Kilo-Org/kilocode/issues/3967)

**Databricks (all families)**
- "The maximum number of functions that can be defined in `tools` is 32 functions."; `tool_choice` `"auto"` (default), `"required"`, named function, `"none"`; "Parallel function calling is not supported."; "During Public Preview, function calling on Databricks is optimized for single turn function calling."; JSON-schema restrictions: no `pattern`, no `anyOf`/`oneOf`/`allOf`, no `$ref`, "Maximum of 16 keys permitted in schemas"; GLM 5.3 / 5.3 Flash / 5.2, Qwen3.5 122B, Qwen3-Next 80B are in the supported list — [Databricks function calling](https://docs.databricks.com/aws/en/machine-learning/model-serving/function-calling)
- But the GLM-5.3 model row says it "supports function calling, parallel tool calls, structured output" — [Databricks supported models (AWS)](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/supported-models) — contradicting the generic "Parallel function calling is not supported" sentence above.

**OpenRouter tool support per ID (live `/endpoints`, 2026-09-23; `T` = `tools` in `supported_parameters`)**
- `z-ai/glm-4.5`: only Z.AI (fp8), tools ✓. `z-ai/glm-4.6`: Venice (fp4, **16,384 max out**, structured ✓), DeepInfra (fp4, 131,072), Novita (bf16), Z.AI (fp4) — all tools ✓. `z-ai/glm-4.7`: DeepInfra, Venice (16K out), AtlasCloud (57% uptime), Novita, Google Vertex, Z.AI, **Mancer 2 (no tools)**. `z-ai/glm-5`: StreamLake, GMICloud, Baidu, SiliconFlow, Amazon Bedrock, Venice (32K out), Novita, Z.AI — tools ✓. `z-ai/glm-5.2`: ~30 endpoints incl. Z.AI fp8 (1,048,576 ctx / 131,072 out, **no structured_outputs**), Fireworks, Together, BaseTen, DeepInfra, DigitalOcean/Parasail/Cloudflare (only 262,144 ctx); `z-ai/glm-5.2:free` = Decart fp4, 32,768 ctx, **no tools**. `z-ai/glm-5.3`: ~34 endpoints; Z.AI fp8 1,048,576/131,072 (no structured_outputs); Reka and Io Net only 262,144 ctx; Cloudflare 1,310,720 ctx. `z-ai/glm-5.3-flash`: ~32 endpoints, modality text+image+video; Z.AI fp8 1,048,576/131,072. — [OpenRouter endpoints API](https://openrouter.ai/api/v1/models/z-ai/glm-5.3/endpoints)
- `qwen/qwen3-235b-a22b-2507`: GMICloud, DeepInfra (40.8% uptime), Novita (131,072/16,384), Parasail, Alibaba (131,072/32,768), Venice (13% uptime), Nebius (68.7%), StreamLake, Google Vertex (one Vertex endpoint has **no tools**); no `reasoning` param. `qwen/qwen3-235b-a22b-thinking-2507`: Alibaba (131,072/117,964), Novita, Venice — uptime `None` for all (stale). `qwen/qwen3-coder`: Google Vertex, DeepInfra (turbo/fp4), Venice, Novita, Alibaba (opensource) — 262,144/65,536, tools ✓, no reasoning. `qwen/qwen3-coder-flash` and `qwen/qwen3-coder-plus`: Alibaba only, 1,000,000/65,536. `qwen/qwen3-coder-next`: Parasail (bf16), StreamLake, Novita, Alibaba. `qwen/qwen3-next-80b-a3b-instruct`: DeepInfra (16,384 out), Alibaba (131,072/32,768), Parasail, Google Vertex, Novita (71% uptime). `qwen/qwen3.5-122b-a10b`: **SiliconFlow (no tools, 16.8% uptime)**, Alibaba, DeepInfra (fp4), AtlasCloud, Novita — modality text+image+video, reasoning ✓. `qwen/qwen3.5-397b-a17b`: Alibaba, DeepInfra, Parasail, DigitalOcean, Phala, AtlasCloud, **StreamLake (no tools)**, GMICloud, Novita, Venice. `qwen/qwen3-max`: Alibaba only, 262,144/65,536, tools ✓, **no reasoning param** (separate `qwen/qwen3-max-thinking` has reasoning). `qwen/qwq-32b`: not listed. — [OpenRouter endpoints API](https://openrouter.ai/api/v1/models/qwen/qwen3-coder/endpoints)
- `minimax/minimax-m1`: **Minimax official endpoint has no tools**; Novita (bf16) has tools; 1,000,000/40,000. `minimax/minimax-m2`: Minimax fp8 (204,800/131,072, no structured_outputs), Google Vertex (196,608/176,947, structured ✓), Novita. `minimax/minimax-m2.1`: Novita, Minimax fp8, Minimax highspeed (2× output price). — [OpenRouter endpoints API](https://openrouter.ai/api/v1/models/minimax/minimax-m2.1/endpoints)
- OpenRouter's 404 when routing lands on an endpoint without tools: `"No endpoints found that support tool use. To learn more about provider routing, visit: https://openrouter.ai/docs/provider-routing"` (seen with `qwen/qwen3-coder` in claude-code-router; fix = pin providers or use paid ID) — [claude-code-router issue #409](https://github.com/musistudio/claude-code-router/issues/409); same error class in [continuedev #3849](https://github.com/continuedev/continue/issues/3849), [mastra #2839](https://github.com/mastra-ai/mastra/issues/2839)
- Provider-routing fields: `order`, `allow_fallbacks` (default true), `require_parameters` (default false), `data_collection`, `only`, `ignore`, `quantizations`, `sort`; `:nitro` = sort by throughput, `:floor` = sort by price — [OpenRouter provider routing](https://openrouter.ai/docs/features/provider-routing)

### Inferences
- **[inference]** Provider slugs for `provider.order`/`only` are the prefix of the endpoint `tag` field in the `/endpoints` response: `z-ai`, `alibaba`, `minimax`, `novita`, `deepinfra`, `fireworks`, `baseten`, `together`, `parasail`, `google-vertex`, `cloudflare`, `venice`, etc.
- **[inference]** Set `provider.require_parameters: true` plus `tools` in every request so OpenRouter never routes a tool-bearing request to a tool-less endpoint (Mancer 2 for glm-4.7, one Vertex endpoint for 235B-2507, SiliconFlow for qwen3.5-122b, StreamLake for qwen3.5-397b, Minimax for minimax-m1, Decart for glm-5.2:free).
- **[inference]** GLM tool-call ids: treat as opaque; never re-key streaming deltas by `id` (they can change mid-stream on some hosts); aggregate by `index`. MiniMax ids: replay exactly as received (`call_function_<12 alnum>_<n>` observed) — synthesised ids trigger 2013.
- **[inference]** "Python-repr arguments" for GLM is a training-data/template artefact (ms-swift templates, and the GLM template rendering dict history as Python repr): the harness should (a) always send history `arguments` as a JSON **string** to APIs, but (b) when self-hosting GLM-5.3-Flash with the stock template, pre-parse to dict (or use SGLang), and (c) run an `ast.literal_eval`-style repair before JSON-parsing incoming arguments, then re-encode as JSON before replay (the agno fix).
- **[inference]** For Qwen3-Coder the "wrong argument types" class (`expected string, received object`) is the parser's schema-driven coercion (`json.loads`/`ast.literal_eval` for object/array types, string-typed values passed through) plus the model emitting bare or quoted values; keep every free-form string parameter typed `string` (never `anyOf`), avoid nested objects in edit/write tools, and be lenient when an object arrives for a string field (`JSON.stringify` it).

### Gaps
- Z.ai does not document its `tool_call.id` format, parallel-call semantics, or per-model tool-schema depth limits.
- Exact MiniMax `tool_call_id` validation regex is unpublished (only the error strings are known).
- Whether `strict: true` on function schemas is honoured (or rejected) by Z.ai, DashScope or MiniMax is undocumented; OpenRouter flags `structured_outputs` per endpoint only.
- No first-party statement from Z.ai on `tool_stream=false` semantics (whole-call-at-end vs. fragments) — only the OpenClaw note that they enable it by default.

---

## Key question 2 — Reasoning / thinking (fields, toggles, pass-back rules, always-reason models, interleaved/preserved thinking, `<think>` leakage)

### Takeaway
Field names: `reasoning_content` on Z.ai, DashScope, MiniMax (with `reasoning_split`), and Databricks-style content items; `reasoning`/`reasoning_details` on OpenRouter; MiniMax additionally returns `<think>…</think>` inline in `content` by default. GLM-5.3 / 5.3-Flash and MiniMax M2.x cannot disable thinking (Z.ai returns HTTP 400 code 1210 with a Chinese message; Databricks rejects `none`); GLM-4.5–5.2 accept `thinking.type: disabled`; Qwen3 hybrid models use `enable_thinking` (default on for Qwen3.5+, off for `qwen3-max`), the 2507 Thinking/QwQ models cannot disable. GLM and MiniMax require replaying the model's reasoning verbatim inside tool loops; Qwen's thinking models want thinking stripped from history except where DashScope's `preserve_thinking` applies.

### Cited Findings

**GLM**
- Request: `thinking.type` `"enabled"` | `"disabled"` (default `"enabled"`), `thinking.clear_thinking` default `true` ("whether to clear prior reasoning blocks"), supported GLM-4.5+; `reasoning_effort` default `"max"`, values `max`, `high`, `low`, `minimal`, `none`; "GLM-5.2: `none`/`minimal` skip thinking; `low`/`medium` → `high`; `xhigh` → `max`"; "GLM-5.3/5.3-FLASH: only `low`/`high`/`max`"; response `message.reasoning_content` "(GLM-4.5+)"; `content` "may include `<think></think>` or `<|begin_of_box|><|end_of_box|>` tags for GLM-4.5V" — [Z.ai Chat Completion API reference](https://docs.z.ai/api-reference/llm/chat-completion)
- "GLM-5.3 and GLM-5.3-FLASH use forced thinking and cannot be disabled"; multi-turn: "you must return the complete, unmodified reasoning_content back to the API"; "All consecutive reasoning_content blocks must exactly match the original sequence"; assistant history shape `{"role": "assistant", "content": content, "reasoning_content": reasoning, "tool_calls": [...]}`; streaming via `delta.reasoning_content`; "Interleaved" thinking "Available since GLM-4.5 (default behavior)"; "Preserved" thinking "enabled by default on Coding Plan endpoint only" (`clear_thinking: False`); "Turn-level thinking: Introduced in GLM-4.7" — [Z.ai Thinking Mode](https://docs.z.ai/guides/capabilities/thinking-mode)
- Deep-thinking page: GLM-5.3/5.3-FLASH "only `max`, `high` and `low` are supported. Any other input will result in an error"; GLM-5.2 options "`max` (default and recommended, for deep inference), `xhigh`, `high` (enhanced inference), `medium`, `low`, `minimal`, and `none`"; Coding-Plan mapping for 5.3: `none`/`minimal`→`low`; `medium`/`high`→`high`; `xhigh`/`max`→`max`; 5.3 models "no longer support disabling thinking (an error will occur if the `thinking.type` parameter in the API request is set to `disabled`)"; GLM-5.2 and below accept `"thinking": {"type": "disabled"}` — [Z.ai Deep Thinking](https://docs.z.ai/guides/capabilities/thinking)
- GLM-5.3 page: "GLM-5.3 always operates with reasoning enabled and supports three reasoning effort levels"; "Disabling reasoning is no longer supported"; `thinking.type` "values `enabled` only"; `reasoning_effort` "`low` – Lightweight Reasoning; `high` – Enhanced Reasoning; `max` – Deep Reasoning", default `max` — [Z.ai GLM-5.3](https://docs.z.ai/guides/llm/glm-5.3); GLM-5.3-Flash page: `thinking.type` "only supports enabled" — [Z.ai GLM-5.3-Flash/FlashX](https://docs.z.ai/guides/vlm/glm-5.3-flash)
- GLM-4.7 page: "Interleaved Reasoning", "Retention-Based Reasoning: Automatically preserves reasoning blocks across multi-turn dialogues", "Round-Level Reasoning" (per-turn enable/disable) — [Z.ai GLM-4.7](https://docs.z.ai/guides/llm/glm-4.7); HF card: chat-template kwargs `"enable_thinking": true`, `"clear_thinking": false`; "Preserved Thinking" = model "automatically retains all thinking blocks across multi-turn conversations" — [HF zai-org/GLM-4.7](https://huggingface.co/zai-org/GLM-4.7)
- Exact rejection: HTTP 400, code 1210, message `"该模型始终思考，不支持关闭思考；请使用 low、high 或 max。"` ("The model always thinks and doesn't support disabling thinking; please use low, high, or max") when `thinking: {"type": "disabled"}` is sent for GLM-5.2 **or** GLM-5.3, on both `api.z.ai` and `open.bigmodel.cn/api/paas/v4` — [hermes-agent issue #96373](https://github.com/NousResearch/hermes-agent/issues/96373). Contradicted for GLM-5.2 by the Z.ai docs above (which say 5.2 accepts `disabled`), and contradicted for GLM-5.3 by AIHubMix, which observed `{"type": "disabled"}` returning "HTTP 200 with thinking still occurring ... the value is converted automatically" and out-of-enum `reasoning_effort` "HTTP 200 without error, defaulting to `max`" — [AIHubMix guide (third party)](https://aihubmix.com/blog/glm-5-3-hands-on-guide-always-on-thinking-three-effort-levels-and-the-api-support-matrix). Related: `reasoning_effort: medium` rejected on the China endpoint — [hermes-agent #96222](https://github.com/NousResearch/hermes-agent/issues/96222); glm-5.3-flash 400 on streaming for `medium`/disabled — [onegw #16](https://github.com/FreePeak/onegw/issues/16)
- Z.ai error table: 1210 (HTTP 400) `"Invalid API parameter, please check the documentation."` — [Z.ai Errors](https://docs.z.ai/api-reference/api-code)
- OpenClaw's mapping (a harness that targets Z.ai): GLM-5.3/Flash `"off"` maps to `"reasoning_effort: 'low'"`; GLM-5.2 `"off"`, `"low"`, `"high"`, `"max"` with `"low"` and `"high"` both → Z.AI `"high"`; other GLM: `"off"` sends `"thinking: { type: 'disabled' }"`; "Preserved thinking requires replaying full `reasoning_content`, increasing prompt tokens" — [OpenClaw Z.AI docs](https://docs.openclaw.ai/providers/zai)
- Coding Plan `/effort` default `"max"`; "`thinking.type` not set, `true`, `enabled`, `adaptive`" all → `"max"` — [Z.ai How to Switch Models](https://docs.z.ai/devpack/latest-model)
- Self-hosted GLM-5.3: the chat template "has no thinking on/off switch and its only knobs are `reasoning_effort` (`'low'`/`'high'`, anything else → `'max'`) and `clear_thinking`"; sending `chat_template_kwargs: {"enable_thinking": false}` makes vLLM's parser stop extracting while the model still thinks, so reasoning "leaks into the response content with a dangling `</think>` tag" — [vLLM issue #54744](https://github.com/vllm-project/vllm/issues/54744); Ollama cloud `glm-5.3-flash:cloud` maps `reasoning_effort` low/high inconsistently — [ollama #18121](https://github.com/ollama/ollama/issues/18121)
- Anthropic-protocol endpoint: Z.ai's own feedback tracker records a 400 `"invalid request: budget_tokens is required and must be positive when thinking type is enabled"` when `thinking: {"type": "enabled"}` is sent without `budget_tokens`; effort tier is carried separately as `output_config.effort` (low/high/max) — [zai-org/feedback #780](https://github.com/zai-org/feedback/issues/780). Third-party gateway docs claim `budget_tokens`/`effort` are ignored for GLM on the Anthropic route and only the `type` switch takes effect — [EvoLink GLM messages reference (third party)](https://evolink.ai/docs/en/api-manual/language-series/glm/messages/messages-reference); a Z.ai-facing proxy issue notes Zhipu ignores `budget_tokens` for glm-5.3-flash — [sub2api #7181](https://github.com/Wei-Shaw/sub2api/issues/7181)
- Databricks: `databricks-glm-5-3` — "Reasoning is always enabled: `reasoning_effort` accepts `low`, `high`, and `max`; omitted, `minimal`, `medium`, or `xhigh` values use `max`, and `none` is rejected."; `databricks-glm-5-3-flash` — "This model always reasons, and reasoning cannot be disabled." — [Databricks supported models](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/supported-models); `databricks-glm-5-2` default `max`, accepts `high`, `max`, "Other values fall back to `max`", classed as *hybrid*; `glm-5-3` and `glm-5-3-flash` classed *reasoning-only*; `glm-5-3-flash`: "`none` is not supported and returns an error" — [Databricks Query reasoning models](https://docs.databricks.com/aws/en/machine-learning/model-serving/query-reason-models)
- models.dev marks Z.ai `glm-4.7`, `glm-5`, `glm-5.2`, `glm-5.3`, `glm-5.3-flash` with `interleaved: {"field": "reasoning_content"}`; on OpenRouter it marks `z-ai/glm-4.7` as `reasoning_details` and `z-ai/glm-5*` as `reasoning_content` — [models.dev api.json](https://models.dev/api.json)

**Qwen**
- DashScope: `enable_thinking` toggles thinking, `thinking_budget` "sets the maximum Token number of Thinking Process" (after exceeding, "the Model immediately Output Response"); response field `reasoning_content`; thinking **on by default**: Qwen3.8/3.7/3.6/3.5 series and open-source Qwen3; **off by default**: `qwen3-max` (commercial), `qwen-plus`, `qwen-flash`, `qwen-turbo`; **cannot be disabled**: `qwen3.8-2.4t-a95b`, `qwen3-235b-a22b-thinking-2507`, `qwen3-30b-a3b-thinking-2507`, `qwq-plus`, `deepseek-r1`, `glm-5.3`, `kimi-k2.7-code`, `kimi-k2-thinking`, `MiniMax-M2.5`; `preserve_thinking: true` replays prior `reasoning_content` (supported: `qwen3.8-max`, `qwen3.7-*`, `qwen3.6-*`, `kimi-k2.*`); error `parameter.enable_thinking only support stream call` for models that "only support Stream output" — [Model Studio deep thinking](https://www.alibabacloud.com/help/en/model-studio/deep-thinking)
- QwenCloud: `thinking_budget` range `[1, 32768]`; `reasoning_effort` for `qwen3.8-max`: `low`, `medium`, `xhigh` (default); "Qwen3 open-source models and the Qwen3.8 open-source series require streaming"; Responses API surfaces `reasoning_text` events; "Thinking tokens are billed as output tokens" — [QwenCloud Thinking](https://docs.qwencloud.com/developer-guides/text-generation/thinking)
- For Model Studio "please use `"enable_thinking": False` instead of `"chat_template_kwargs": {"enable_thinking": False}`" — [Qwen docs: Function Calling](https://qwen.readthedocs.io/en/latest/framework/function_call.html)
- Qwen3.5-plus on DashScope's coding endpoint keeps returning thinking blocks unless `enable_thinking` is sent explicitly, because "Qwen 3.5+/3.6+ models have thinking enabled server-side by default" — [pi issue #2770](https://github.com/earendil-works/pi/issues/2770)
- Qwen3-235B-A22B-Instruct-2507 "supports only non-thinking mode and does not generate `<think></think>` blocks in its output" — [HF Instruct-2507](https://huggingface.co/Qwen/Qwen3-235B-A22B-Instruct-2507); Thinking-2507 is thinking-only, "output may contain only </think> without an opening <think> tag", serve with `--reasoning-parser deepseek_r1`, and "Historical model output should only include the final output part and does not need to include the thinking content." — [HF Thinking-2507](https://huggingface.co/Qwen/Qwen3-235B-A22B-Thinking-2507); Qwen3-Next-80B-A3B-Instruct "supports only instruct (non-thinking) mode" — [HF Qwen3-Next-80B-A3B-Instruct](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct); Qwen3-Coder-Next "supports only non-thinking mode" — [HF Qwen3-Coder-Next](https://huggingface.co/Qwen/Qwen3-Coder-Next)
- Qwen3.5-122B-A10B: thinking "Default enabled; disable via `"chat_template_kwargs": {"enable_thinking": False}`"; `--reasoning-parser qwen3` — [HF Qwen3.5-122B-A10B](https://huggingface.co/Qwen/Qwen3.5-122B-A10B); on Databricks it is "a reasoning-only model ... always reasons before responding, and reasoning cannot be disabled" — [Databricks supported models](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/supported-models)
- QwQ-32B: start generation with `"<think>\n"`; "the historical model output should only include the final output part and does not need to include the thinking content" — [HF QwQ-32B](https://huggingface.co/Qwen/QwQ-32B)
- OpenRouter: `qwen/qwen3-max` exposes no `reasoning` parameter; `qwen/qwen3-max-thinking`, `qwen3-235b-a22b-thinking-2507`, `qwen3-next-80b-a3b-thinking`, `qwen3.5-*` do; models.dev shows `alibaba-cn` `qwen3-max` (updated 2026-01-23) with `reasoning: true, interleaved: reasoning_content` while the international `alibaba` entry has `reasoning: false` — [models.dev api.json](https://models.dev/api.json)

**MiniMax**
- OpenAI-compatible: `reasoning_split` "Separates thinking into `reasoning_content` and `reasoning_details` fields"; "For M2.x models, thinking cannot be disabled; thinking remains on"; native responses keep thinking in `content` with `<think>...</think>` tags "which must be preserved completely" — [MiniMax OpenAI-compatible API](https://platform.minimax.io/docs/api-reference/text-openai-api)
- Anthropic-compatible: M3 "If `thinking` is omitted, thinking is off by default"; "For M2.x: thinking cannot be disabled"; "the complete model response...must be append to the conversation history to maintain the continuity of the reasoning chain" and "preserve them unchanged in later turns, especially in tool-use conversations" — [MiniMax Anthropic-compatible API](https://platform.minimax.io/docs/api-reference/text-anthropic-api)
- Interleaved thinking: "A separate `reasoning_details` field: The model's reasoning process is returned in a separate `reasoning_details` field, no longer mixed with the `content`"; Anthropic route: "append the model's complete output from each round (including `thinking_blocks`) to the `messages` history"; dropping it costs "SWE-Bench Verified 69.4 vs. 67.2", "Tau^2 87 vs. 64", "BrowseComp 44.0 vs. 31.4" — [MiniMax: Interleaved Thinking for M2](https://www.minimax.io/news/why-is-interleaved-thinking-important-for-m2)
- HF M2 card: "Do not remove the `<think>...</think>` part, otherwise, the model's performance will be negatively affected." — [HF MiniMaxAI/MiniMax-M2](https://huggingface.co/MiniMaxAI/MiniMax-M2)
- OpenRouter/models.dev mark `minimax/minimax-m2`, `m2.1`, `m2.5` with `interleaved: {"field": "reasoning_details"}` — [models.dev api.json](https://models.dev/api.json)

**OpenRouter reasoning contract (all families)**
- Request `reasoning: {effort: "max"|"xhigh"|"high"|"medium"|"low"|"minimal"|"none", max_tokens, exclude, enabled}` (`effort` and `max_tokens` mutually exclusive); response `reasoning` (string) and `reasoning_details[]` with types `reasoning.text`, `reasoning.summary`, `reasoning.encrypted` (fields `type`, `id`, `format`, `index`); tool loops: "the entire sequence of consecutive reasoning blocks must match the outputs generated by the model during the original request; you cannot rearrange or modify the sequence" — [OpenRouter reasoning tokens](https://openrouter.ai/docs/use-cases/reasoning-tokens)

**Databricks reasoning shape**
- Reasoning is returned as content items of `"type": "reasoning"` (Claude example: `{"type": "reasoning", "summary": [{"type": "summary_text", "text": "..."}]}`); Open Responses returns `{"type": "reasoning", "id": "rs_...", "content": [{"type": "reasoning_text", "text": "..."}], "encrypted_content": "..."}` and "If it's dropped or modified, the model can't reason over its earlier thinking" — [Databricks Query reasoning models](https://docs.databricks.com/aws/en/machine-learning/model-serving/query-reason-models)

### Inferences
- **[inference]** Adapter rule for GLM: never send `thinking.type: disabled` to 5.3/5.3-Flash; map "off" → `reasoning_effort: "low"`; for 5.2 map "off" → `thinking: {type: disabled}` only if the endpoint accepts it (docs say yes, one field report says 400) — implement as try-then-fallback-to-`low`. For 4.5–5 use `thinking.type` only (no `reasoning_effort` documented).
- **[inference]** GLM `reasoning_content` must be echoed verbatim in every assistant turn that carries `tool_calls`; on the Coding-Plan endpoint prior turns' reasoning is retained server-side by default, so the harness should not strip it there either.
- **[inference]** Databricks' generic "default reasoning_effort is low" (search snippet) does not apply to GLM rows, whose per-model defaults are `max`; harness should set effort explicitly per row.
- **[inference]** Qwen: for Instruct/Coder rows send nothing; for hybrid rows always send `enable_thinking` explicitly (DashScope) or `chat_template_kwargs.enable_thinking` (self-hosted/OpenRouter-hosted open weights); for Thinking-2507/QwQ strip reasoning from history; for DashScope 3.6+ models optionally `preserve_thinking: true`.
- **[inference]** MiniMax on OpenAI-compatible: send `reasoning_split: true` and replay `reasoning_details` verbatim; otherwise keep the `<think>…</think>` text inside assistant `content` when replaying.

### Gaps
- Z.ai does not publish the exact error string for the "cannot disable" case in English docs (only the Chinese 1210 message from a field report), and conflicting reports exist on whether 5.2/5.3 reject vs. silently coerce.
- Exact JSON shape of Databricks reasoning items for GLM/Qwen (vs. the Claude/Gemini examples) is not shown.
- DashScope behaviour when an assistant history message includes `reasoning_content` for models *without* `preserve_thinking` support (e.g. Qwen3.5 open-weights, Qwen3-235B-Thinking) — ignored or error — is undocumented.
- Whether `reasoning_effort` is honoured for GLM-4.5–5.1 on Z.ai is undocumented ("not explicitly documented").

---

## Key question 3 — Sampling and limits (recommended temperature/top_p/top_k/penalties, tool-call corruption vs temperature, context, output caps, overflow error wording, rate limits, caching)

### Takeaway
GLM ships temperature 1.0 / top_p 0.95 (GLM-4.5: 0.6/0.95), clamps temperature to [0,1], and caps output at 128K (96K for 4.5); Qwen has per-model presets (Instruct 0.7/0.8/20, Thinking 0.6/0.95/20, Coder-480B 0.7/0.8/20 + rep 1.05, Coder-Next 1.0/0.95/40, Qwen3.5 thinking 1.0/0.95/20 or 0.6 for precise coding); MiniMax M1/M2/M2.1 use 1.0/0.95/40. The only quantitative tool-call-vs-temperature data found is from a 2.05-bpw local GLM-5.3-Flash (4.2% malformed at T=1.0 vs 0.8% at 0.6). Overflow errors: Z.ai `{"code":"1261","message":"Prompt too long"}`; DashScope `Range of input length should be [1, N]`; MiniMax `context window exceeds limit (2013)`; OpenRouter `This endpoint's maximum context length is X tokens. However, you requested about Y tokens`.

### Cited Findings

**GLM**
- Z.ai: `temperature` range `[0.0, 1.0]`, default `1.0` for GLM-5.3/5.2/5.1/5/4.7/4.6, `0.6` for GLM-4.5, `0.8` for GLM-4.5V; `top_p` range `[0.01, 1.0]`, default `0.95` (0.6 for 4.5V); `max_tokens` "max 128K output" for 5.3/5.2/5.1/5/4.7/4.6, "max 96K" GLM-4.5, 32K GLM-4.6v, 16K GLM-4.5v; `do_sample` default true; `stop` max 4 — [Z.ai Chat Completion API reference](https://docs.z.ai/api-reference/llm/chat-completion)
- Messages API rejects `temperature: 3` with 400 `"temperature parameter invalid: value must be within [0,1]"`; Chat Completions silently accept out-of-range; "Z.ai recommends tuning only one of the two" (temperature or top_p) — [AIHubMix guide (third party)](https://aihubmix.com/blog/glm-5-3-hands-on-guide-always-on-thinking-three-effort-levels-and-the-api-support-matrix)
- GLM-5.3-Flash page recommends Temperature `"1"`, Top_p `"0.95"`; context `"1M"`, max output `"128K"`; inputs "Video / Image / Text / File"; "GLM-5.3-FlashX is not yet available on the GLM Coding Plan" — [Z.ai GLM-5.3-Flash/FlashX](https://docs.z.ai/guides/vlm/glm-5.3-flash); GLM-5.2: "1M" / "128K", text-only — [Z.ai GLM-5.2](https://docs.z.ai/guides/llm/glm-5.2); GLM-4.7: "200K" / "128K" — [Z.ai GLM-4.7](https://docs.z.ai/guides/llm/glm-4.7); GLM-5.3: "1M-token context window", "maximum output length of 128K tokens" — [Z.ai GLM-5.3](https://docs.z.ai/guides/llm/glm-5.3)
- HF GLM-4.7 eval settings: default `"temperature: 1.0"`, `"top-p: 0.95"`, `"max new tokens: 131072"`; Terminal-Bench & SWE-bench `"temperature: 0.7"`, `"top-p: 1.0"`, `"max new tokens: 16384"`; τ²-Bench `"Temperature: 0"` — [HF zai-org/GLM-4.7](https://huggingface.co/zai-org/GLM-4.7); GLM-5: reasoning `"temperature=1.0, top_p=0.95, max_new_tokens=131072"`, SWE-bench `"temperature=0.7, top_p=0.95, max_new_tokens=16384"`, Terminal-Bench `"temperature=0.7, top_p=1.0, max_new_tokens=8192"`; HLE-with-tools context "202,752 tokens" — [HF zai-org/GLM-5](https://huggingface.co/zai-org/GLM-5)
- Tool-call corruption vs temperature (GLM-5.3-Flash, "2.05 bpw checkpoint", "16 tools, streaming"): T=1.0 "5/120 (4.2%)" malformed overall and "3/20" in long-reasoning cases; T=0.6 "1/120 (0.8%)" and "0/20"; T=0.0 "0/120"; failure shapes: "reasoning tail glued to the tool **name**: `... </think><tool_call>bash`", "speculative multi-call spam with invented names (`bashCommand`)", "truncated JSON arguments (e.g. a `todowrite` call missing its final `}`)"; `--tool-call-parser` "**must stay `glm47`**" ("`glm45` ... extracts **zero** tool calls") — [GLM-5.3-Flash-DGX-Spark PR #3](https://github.com/0xSero/GLM-5.3-Flash-DGX-Spark/pull/3)
- Overflow: HTTP 400 `{"code":"1261","message":"Prompt too long"}` from `https://api.z.ai/api/paas/v4/chat/completions` for glm-5.2 above 1,048,576 tokens — [pi issue #9805](https://github.com/earendil-works/pi/issues/9805); error table: 1261 (400) "Prompt too long", 1302 (429) "Rate limit reached for requests", 1305 (429) "The service may be temporarily overloaded, please try again later", 1113 (429) "Insufficient balance or no resource package. Please recharge.", 1301 (400) sensitive content; "Streaming responses use `finish_reason` parameters instead of standard error codes when abnormally terminated" (cf. `finish_reason: "model_context_window_exceeded"`) — [Z.ai Errors](https://docs.z.ai/api-reference/api-code)
- Caching: "Automatic Cache Recognition: Implicit caching"; "Cache hit tokens: Billed at discounted prices (usually 50% of standard price)"; reported in `usage.prompt_tokens_details.cached_tokens`; "Supports all mainstream models, including GLM-5, GLM-4.7, GLM-4.6, GLM-4.5 series" — [Z.ai Context Caching](https://docs.z.ai/guides/capabilities/cache); OpenRouter shows Z.AI cache-read pricing (e.g. glm-4.5 $0.11/M) — [OpenRouter endpoints](https://openrouter.ai/api/v1/models/z-ai/glm-4.5/endpoints)
- Rate limits: the docs "Rate Limits" page redirects to `https://z.ai/manage-apikey/rate-limits`, which rendered no numbers for me (login-gated console) — [Z.ai rate-limit redirect](https://docs.z.ai/api-reference/rate-limit). GLM Coding Plan: "Lite: 2,000", "Pro: 12,000", "Max: 28,000" credits per 5-hour window; "All plans support GLM-5.3, GLM-5.3-Flash"; GLM-5.2/5.1 requests redirect to GLM-5.3, GLM-4.7 → GLM-5.3-Flash — [Z.ai GLM Coding Plan overview](https://docs.z.ai/devpack/overview); Claude Code config `API_TIMEOUT_MS: "3000000"` — [Z.ai Claude Code guide](https://docs.z.ai/devpack/tool/claude)
- models.dev Z.ai limits: glm-4.5 131,072/98,304; glm-4.6 204,800/131,072; glm-4.7 204,800/131,072; glm-5 204,800/131,072; glm-5.2 1,000,000/131,072; glm-5.3 1,000,000/131,072; glm-5.3-flash 1,000,000/131,072 (attachments text/image/video/pdf) — [models.dev api.json](https://models.dev/api.json)

**Qwen**
- Instruct-2507: "Temperature=0.7, TopP=0.8, TopK=20, and MinP=0", `presence_penalty` 0–2 (higher may cause language mixing), "output length of 16,384 tokens for most queries", native 262,144, up to 1,010,000 with YaRN — [HF Instruct-2507](https://huggingface.co/Qwen/Qwen3-235B-A22B-Instruct-2507)
- Thinking-2507: "Temperature=0.6, TopP=0.95, TopK=20, and MinP=0"; output "32,768 tokens for most queries" and "81,920 tokens" for hard math/coding — [HF Thinking-2507](https://huggingface.co/Qwen/Qwen3-235B-A22B-Thinking-2507)
- Qwen3-Coder-480B: "temperature=0.7, top_p=0.8, top_k=20, repetition_penalty=1.05"; "262,144 natively", "extendable up to 1M tokens using Yarn" — [HF Qwen3-Coder-480B](https://huggingface.co/Qwen/Qwen3-Coder-480B-A35B-Instruct)
- Qwen3-Coder-Next: "temperature=1.0, top_p=0.95, top_k=40"; "262,144 natively"; "80B in total and 3B activated" — [HF Qwen3-Coder-Next](https://huggingface.co/Qwen/Qwen3-Coder-Next)
- Qwen3-Next-80B-A3B-Instruct: "Temperature=0.7, TopP=0.8, TopK=20, and MinP=0"; 262,144 native, 1,010,000 with YaRN factor 4.0 — [HF Qwen3-Next-80B-A3B-Instruct](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct)
- Qwen3.5-122B-A10B: thinking general `"temperature=1.0, top_p=0.95, top_k=20, min_p=0.0, presence_penalty=1.5"`; thinking precise coding `"temperature=0.6, top_p=0.95, top_k=20, min_p=0.0, presence_penalty=0.0"`; instruct general `"temperature=0.7, top_p=0.8, top_k=20, min_p=0.0, presence_penalty=1.5"`; instruct reasoning `"temperature=1.0, top_p=1.0, top_k=40, min_p=0.0, presence_penalty=2.0"`; 262,144 → 1,010,000 — [HF Qwen3.5-122B-A10B](https://huggingface.co/Qwen/Qwen3.5-122B-A10B)
- QwQ-32B: "Use Temperature=0.6, TopP=0.95, MinP=0 instead of Greedy decoding to avoid endless repetitions", TopK 20–40; 131,072 ctx; YaRN for prompts > 8,192 — [HF QwQ-32B](https://huggingface.co/Qwen/QwQ-32B)
- DashScope official limits (models.dev `alibaba`/`alibaba-cn`): `qwen3-max` 262,144 / 65,536; `qwen3-coder-plus` 1,048,576 / 65,536; `qwen3-coder-flash` 1,000,000 / 65,536; `qwen3-coder-480b-a35b-instruct` 262,144 / 65,536; `qwen3-235b-a22b` 131,072 / 16,384; `qwen3-next-80b-a3b-instruct`/`-thinking` 131,072 / 32,768; `qwen3.5-122b-a10b`, `qwen3.5-397b-a17b` 262,144 / 65,536; `qwen3.5-plus`/`qwen3.5-flash` 1,000,000 / 65,536; `qwq-plus` and `qwq-32b` 131,072 / 8,192 — [models.dev api.json](https://models.dev/api.json)
- DashScope overflow: `"Error code: 400 - BadRequestError InternalError.Algo.InvalidParameter: Range of input length should be [1, 169984]"` (Alibaba Coding Plan) — [hermes-agent issue #2220](https://github.com/NousResearch/hermes-agent/issues/2220); Coding-Plan FAQ: "the request content ... exceeds the maximum input limit" → `/compact` or switch to a larger-context model — [Model Studio coding-plan FAQ](https://www.alibabacloud.com/help/en/model-studio/coding-plan-faq)
- Caching: implicit prefix cache when "a common prefix of at least 1024 tokens exists"; `cached_token` "**20%** of the `input_token` unit price"; explicit `"cache_control": {"type": "ephemeral"}` (5-minute validity, creation ~125%, hit ~10%); models include `qwen3-max`, `qwen3.5-plus`, `qwen3-coder-plus`, `qwen3-coder-flash`; reported in `usage.prompt_tokens_details.cached_tokens` — [Model Studio context cache](https://www.alibabacloud.com/help/en/model-studio/context-cache)

**MiniMax**
- API: `temperature` "[0, 2], default 1"; `top_p` "Default 0.9 for M2.x models"; `max_completion_tokens` preferred; context 204,800 for M2/M2.1/M2.5/M2.7 (~60 tps; highspeed ~100 tps) — [MiniMax OpenAI-compatible API](https://platform.minimax.io/docs/api-reference/text-openai-api); HF cards recommend `"temperature=1.0, top_p = 0.95, top_k = 40"` for M2 and M2.1 — [HF MiniMax-M2](https://huggingface.co/MiniMaxAI/MiniMax-M2), [HF MiniMax-M2.1](https://huggingface.co/MiniMaxAI/MiniMax-M2.1) (note 0.9 vs 0.95 top_p discrepancy between API default and card recommendation)
- M1-80k: Temperature `1.0`, Top_p `0.95`; 1M context; "Thinking budget: 80K tokens"; vLLM ≥ 0.9.2 — [HF MiniMax-M1-80k](https://huggingface.co/MiniMaxAI/MiniMax-M1-80k); M1-80k listed "Deprecated" on SiliconFlow — [SiliconFlow M1-80k page (third party)](https://www.siliconflow.com/models/minimax-m1-80k); OpenRouter `minimax/minimax-m1` 1,000,000 ctx / 40,000 max out — [OpenRouter models](https://openrouter.ai/api/v1/models)
- Overflow/param errors carry code 2013: "[Bug]: invalid params, context window exceeds limit (2013)" (M2.7-highspeed) — [MiniMax-M2 issue #119](https://github.com/MiniMax-AI/MiniMax-M2/issues/119); community guide: "Do not retry 2013 errors without fixing the underlying cause" — [minimax-ai.chat error guide (third party)](https://minimax-ai.chat/guide/minimax-api-error-codes/)
- models.dev: official `MiniMax-M2`, `MiniMax-M2.1` 204,800 / 131,072 — [models.dev api.json](https://models.dev/api.json)

**OpenRouter / Databricks limits**
- OpenRouter overflow: `"This endpoint's maximum context length is [X] tokens. However, you requested about [Y] tokens"` (400), with `middle-out` transform as mitigation — [agno issue #3980](https://github.com/agno-agi/agno/issues/3980), [Roo-Code issue #11998](https://github.com/RooCodeInc/Roo-Code/issues/11998), [OpenRouter message transforms](https://openrouter.ai/docs/guides/features/message-transforms); free variants: 20 RPM, 50 req/day (1,000/day with ≥ $10 credits), 429 body `{"error":{"code":429,"message":"Rate limit exceeded","metadata":{"error_type":"rate_limit_exceeded"}}}` — [OpenRouter limits](https://openrouter.ai/docs/api-reference/limits)
- Databricks pay-per-token: GLM 5.3 & 5.3 Flash — ITPM 2,000,000, OTPM 40,000, QPH 7,200; GLM 5.2 — ITPM 200,000, OTPM 20,000, QPH 7,200; Qwen3.5 122B (Public Preview) — ITPM 1,000,000, OTPM 100,000, QPH 360,000; 429 text `"Rate limit exceeded: ITPM limit of 200,000 tokens reached"` with `retry_after`; payload max "4 MB"; ">1 MB will not be logged" — [Databricks FMAPI limits](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/limits); GLM-5.3 "context window of up to 1,048,576 tokens, and up to 65,536 output tokens"; Qwen3.5 122B "256K context window and up to 25K output tokens" — [Databricks supported models](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/supported-models)

### Inferences
- **[inference]** For agentic loops over API-hosted GLM keep vendor defaults (1.0/0.95) unless malformed-tool-call rate is observed; for self-hosted/quantised GLM (and any provider serving fp4) drop to 0.6–0.7 (matches Z.ai's own SWE/Terminal-Bench settings of 0.7). Do not send `temperature` > 1.0 to Z.ai (Messages API 400s; Chat Completions silently clamps/accepts).
- **[inference]** Qwen: always send `top_k`/`min_p`/`repetition_penalty` only where the transport supports them (DashScope OpenAI-compatible accepts `top_k`? undocumented — use `extra_body`); OpenRouter passes `top_k` only to endpoints listing it in `supported_parameters`.
- **[inference]** Overflow detection regexes per family: Z.ai `"code"\s*:\s*"?1261"?|Prompt too long|model_context_window_exceeded`; DashScope `Range of input length should be \[1, ?\d+\]|InvalidParameter`; MiniMax `context window exceeds limit \(2013\)`; OpenRouter `maximum context length is \d+ tokens\. However, you requested about \d+ tokens`; Databricks — not observed.

### Gaps
- Z.ai does not publish `max_tokens` defaults (only caps) or public per-model RPM/TPM (console-gated).
- No vendor data on tool-call corruption vs temperature for API-hosted GLM; the only numbers are from a 2.05-bpw local checkpoint.
- No official sampling recommendation for `qwen3-max`, `qwen3-coder-plus/flash` (commercial IDs); MiniMax M2.1 API `top_p` default (0.9) vs card (0.95) unresolved.
- Databricks output caps for `databricks-glm-5-2` and `databricks-qwen3-next-80b-a3b-instruct` not stated on the page I could read; Databricks overflow error wording not found.

---

## Key question 4 — Message-shape constraints (system placement, multiple/trailing system, `developer` role, multipart content, vision, tool-result shapes)

### Takeaway
The hard constraint is Qwen3.5+/3.6+: the official chat template raises `System message must be at the beginning.` for any `system` message not at index 0 (so a second or trailing system message errors wherever the stock template is used — vLLM, llama.cpp, LocalAI, NVIDIA NIM, several OpenRouter providers). Z.ai accepts only `user`/`system`/`assistant`/`tool` roles; MiniMax's Anthropic route takes `system` as a top-level field; tool results are `role: tool` + `tool_call_id` + string content on all three vendors (MiniMax's raw/self-hosted format instead uses a content array with `name`/`type: text`).

### Cited Findings
- Qwen3.5 template: `{%- if message.role == "system" %}{%- if not loop.first %}{{- raise_exception('System message must be at the beginning.') }}` — [HF Qwen3.5-35B-A3B discussion #5](https://huggingface.co/Qwen/Qwen3.5-35B-A3B/discussions/5); reported for Qwen3.6-27B on vLLM ("400 format_error") — [vLLM issue #41114](https://github.com/vllm-project/vllm/issues/41114), [hermes-agent #20866](https://github.com/NousResearch/hermes-agent/issues/20866); Qwen3.5-397B-A17B in OpenCode — [opencode #20785](https://github.com/anomalyco/opencode/issues/20785); qwen3.5-122b-a10b via the NVIDIA provider — [opencode #16560](https://github.com/anomalyco/opencode/issues/16560); llama.cpp with a memory plugin that "creates multiple system messages" — [opencode-agent-memory #11](https://github.com/joshuadavidthomas/opencode-agent-memory/issues/11); LocalAI regression — [abacus #2](https://github.com/empero-org/abacus/issues/2); Qwen3.8-27B GGUF — [HF unsloth discussion](https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/discussions/10)
- Z.ai `messages` roles: `user`, `system`, `assistant`, `tool`; `Accept-Language` header default `"en-US,en"` — [Z.ai Chat Completion API reference](https://docs.z.ai/api-reference/llm/chat-completion)
- Z.ai tool result: `{"role": "tool", "content": "...", "tool_call_id": "..."}` — [Z.ai Function Calling](https://docs.z.ai/guides/capabilities/function-calling); DashScope: `{"role": "tool", "tool_call_id": "[id]", "content": "[result]"}` and "Make sure the tool's output is in string format" — [Model Studio function calling](https://www.alibabacloud.com/help/en/model-studio/qwen-function-calling); MiniMax raw/self-hosted: `"role": "tool", "content": [{"name": function_name, "type": "text", "text": ...}]` — [MiniMax-M2.1 tool_calling_guide.md](https://raw.githubusercontent.com/MiniMax-AI/MiniMax-M2.1/main/docs/tool_calling_guide.md); Anthropic route: "Append the full `response.content` list to the message history" and pass results as `tool_result` blocks — [MiniMax M3 tool-use guide](https://platform.minimax.io/docs/guides/text-m3-function-call), [MiniMax Anthropic-compatible API](https://platform.minimax.io/docs/api-reference/text-anthropic-api)
- Vision: GLM-4.5V `content` "may include `<think></think>` or `<|begin_of_box|><|end_of_box|>` tags"; vision model IDs `glm-5.3-flashx`, `glm-5.3-flash`, `glm-4.6v`, `glm-4.6v-flash(x)`, `glm-4.5v`; GLM-5.3 text-only — [Z.ai Chat Completion API reference](https://docs.z.ai/api-reference/llm/chat-completion); OpenRouter modalities: `z-ai/glm-4.5v` text+image (65,536 ctx / 16,384 out), `z-ai/glm-4.6v` text+image+video, `z-ai/glm-5.3-flash` text+image+video, `qwen/qwen3-vl-235b-a22b-instruct|thinking`, `qwen3-vl-30b-a3b-*`, `qwen3-vl-8b-*`, `qwen3-vl-32b-instruct` text+image with tools, `qwen/qwen3.5-*` text+image+video — [OpenRouter models](https://openrouter.ai/api/v1/models); Qwen3.5-122B is an "Image-Text-to-Text" model — [HF Qwen3.5-122B-A10B](https://huggingface.co/Qwen/Qwen3.5-122B-A10B); Databricks `databricks-glm-5-3-flash` inputs text+image, `databricks-glm-5-3`, `-5-2`, `qwen35-122b-a10b`, `qwen3-next-80b` text-only — [Databricks supported models](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/supported-models)
- GLM-5.2 on Z.ai intermittently rendered tool results "as image content blocks instead of text (context-length correlated)" — [zai-org/GLM-5 issue #110](https://github.com/zai-org/GLM-5/issues/110)
- GLM-4.7-Flash (llama.cpp) "enters corrupted state with grammar trigger loop" possibly from "malformed prompts such as consecutive user messages without assistant responses" — [llama.cpp issue #19068](https://github.com/ggml-org/llama.cpp/issues/19068)

### Inferences
- **[inference]** Harness rule for every Qwen row: merge all system content into one `system` message at index 0 and never append a trailing/interleaved system message (convert mid-conversation "system reminders" to `user` text). This is harmless for GLM/MiniMax and required for Qwen3.5+ open weights on any host.
- **[inference]** Do not use the `developer` role with any of these vendors (Z.ai enumerates only four roles; DashScope/MiniMax don't document it); map `developer` → `system`.
- **[inference]** Keep tool-result `content` as a plain string (DashScope requires it; Z.ai examples use it); flatten multipart tool results to text for GLM to avoid the image-block mis-rendering seen in GLM-5 #110.

### Gaps
- Z.ai, DashScope and MiniMax do not document behaviour for multiple or non-leading `system` messages on their hosted endpoints (the template rule is verified only for open-weight Qwen3.5+ hosting).
- `developer` role support is undocumented for all three vendors.
- Multipart (`content: [{type:"text"}...]`) support for text-only GLM/Qwen endpoints is undocumented.

---

## Key question 5 — Known agent-loop failures and fixes (Cline / Roo / OpenCode / Kilo / Crush / Aider / Qwen Code / claude-code-router and others)

### Takeaway
The recurring GLM failures are malformed/large-argument JSON (Crush #3153, GLM-5 #15), streaming-id drift (mistral-vibe #1127), `finish_reason=tool_calls` with empty content (ds4 #1), plan-vs-emission drift loops (GLM-5 #116), duplicated `<tool_call>` markers (SGLang #15721), and thinking-off 400s (hermes-agent #96373). Qwen's are the system-message template rule, wrong argument types on `qwen/qwen3-coder:free` (OpenCode #6918), omitted `<tool_call>` tags (Qwen3-Coder #475), Python-literal arguments (agno #10231), and OpenRouter's "No endpoints found that support tool use" 404 (CCR #409). MiniMax's are code-2013 rejections of client-generated tool ids or non-JSON arguments. Aider ships no model settings for any of these families.

### Cited Findings
- **Crush + GLM-4.6/5.2 (Fireworks `accounts/fireworks/models/glm-5p2`, Crush v0.76.0)**: "Invalid tool call in messages: tool_calls[].function.arguments for function 'edit' must be a JSON object string (or an object), got invalid JSON." — triggers on `edit`/`write` "code blobs with newlines, quotes, and special characters"; `read`/`grep` fine; other Fireworks models (DeepSeek V4 Pro, Kimi K2.7) fine; proposed: repair pass + treat as recoverable turn error — [crush issue #3153](https://github.com/charmbracelet/crush/issues/3153)
- **OpenCode + GLM-5 via NVIDIA NIM (`z-ai/glm5`)**: arguments like `{\"query\":\"Geoffrey Huntley LinkedIn ...\"` missing the closing brace; parser error `"JSON Parse error: Expected '}'"`; unclear whether model/streaming/client — [zai-org/GLM-5 issue #15](https://github.com/zai-org/GLM-5/issues/15), [NVIDIA forum thread](https://forums.developer.nvidia.com/t/nim-glm-5-malformed-tool-call-json-missing-via-openai-compatible-endpoint-opencode/360809)
- **Claude Code CLI v2.1.215 + GLM-5.2 (Anthropic-compatible endpoint)**: planned `"cd /workspaces/myrepo && pwd && git worktree list"` in thinking but emitted `{"command": "pwd && git worktree list"}` — 10 consecutive identical failures; workaround `git -C <path>` — [zai-org/GLM-5 issue #116](https://github.com/zai-org/GLM-5/issues/116)
- **Pi + GLM-5.3-Flash (local ds4/llama-swap)**: `"stopReason": "toolUse", "rawStopReason": "tool_calls"` with `"content": []`; agents "loop indefinitely in clients that trust finish_reason=tool_calls"; fix: validate that a usable call exists before acting on `finish_reason` — [ds4 issue #1](https://github.com/IngeniousIdiocy/ds4/issues/1)
- **hermes-agent + glm-5.3-flash (ollama-cloud)**: "string-encodes tool_call 'calls'" (Family-A: `"calls"` becomes a string with a dropped brace and `"name"` promoted outside; Family-D: `"name"` inside the string with orphaned closers); narrow repair PR — [hermes-agent PR #114656](https://github.com/NousResearch/hermes-agent/pull/114656)
- **GLM-5.3 on ds4** emits `"false"` (string) for boolean args, which Codex cannot parse — [local-llm issue #41](https://github.com/evanwtf/local-llm/issues/41); Qwen tool-call structure "decodes at request temperature — ~20 malformed calls per sweep" — [local-llm issue #219](https://github.com/evanwtf/local-llm/issues/219)
- **OpenRouter + qwen/qwen3-coder (claude-code-router)**: 404 "No endpoints found that support tool use..."; fixes: pin a tool-capable provider (Baseten, Parasail, DeepInfra confirmed), use paid ID, `require_parameters` — [claude-code-router issue #409](https://github.com/musistudio/claude-code-router/issues/409)
- **OpenCode v1.1.1 + `qwen/qwen3-coder:free`**: "Invalid input: expected string, received object" for `oldString`/`newString`; closed "not planned" — [opencode issue #6918](https://github.com/anomalyco/opencode/issues/6918); Rapid-MLX: "Constrained tool calling truncates and corrupts string arguments on the Qwen3-Coder XML wire" (grammar forces quoted JSON strings the model emits bare); workaround `RAPID_MLX_CONSTRAIN_TOOLS=0` — [Rapid-MLX issue #1996](https://github.com/raullenchai/Rapid-MLX/issues/1996)
- **Roo/Cline/Kilo custom (non-native) tool syntax with Qwen3-Coder**: the model prefers its trained XML (`<function=…>`) over in-context bracket syntax; Unsloth template + "Do NOT omit the initial <tool_call> tag" — [QwenLM/Qwen3-Coder issue #475](https://github.com/QwenLM/Qwen3-Coder/issues/475), [Unsloth Qwen3-Coder-30B template fixes](https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/discussions/10); Qwen3-Coder-Next GGUF jinja template errors in LM Studio + OpenCode/Qwen Code/Kilo — [HF unsloth Qwen3-Coder-Next-GGUF discussion](https://huggingface.co/unsloth/Qwen3-Coder-Next-GGUF/discussions/2)
- **Qwen Code**: model-specific tool-call example notation only (no behavioural forks) — [qwen-code prompts.ts](https://raw.githubusercontent.com/QwenLM/qwen-code/main/packages/core/src/core/prompts.ts); context-window bug tracker — [qwen-code issue #4089](https://github.com/QwenLM/qwen-code/issues/4089)
- **Kilo Code + MiniMax M2 (free)**: "Minimax error: invalid params, tool call id is invalid (2013)" — [kilocode issue #3967](https://github.com/Kilo-Org/kilocode/issues/3967); **openclaw**: "[Model Rejected] invalid request: tool_call id format is not supported" after LCM compaction rewrote ids — [openclaw issue #66892](https://github.com/openclaw/openclaw/issues/66892); **hermes-agent**: "invalid params, invalid function arguments json string, tool_call_id: call_function_60llhpv7vhhb_1 (2013)" — [hermes-agent issue #12167](https://github.com/NousResearch/hermes-agent/issues/12167)
- **QwenPaw + MiniMax Anthropic-compatible**: 'context window exceeds limit (2013)' when `max_tokens` missing — [QwenPaw issue #1273](https://github.com/agentscope-ai/QwenPaw/issues/1273)
- **hermes-agent + GLM-5.2 Coding Plan**: 429 / code 1305 whenever the system prompt contains the exact phrase "Hermes Agent" — [hermes-agent issue #47685](https://github.com/NousResearch/hermes-agent/issues/47685)
- **Aider**: `model-settings.yml` contains **no** entries for glm/qwen/qwq/minimax/zai — [aider model-settings.yml](https://raw.githubusercontent.com/Aider-AI/aider/main/aider/resources/model-settings.yml)
- **LiteLLM**: added mapping of `thinking`/`reasoning_effort` → DashScope fields — [litellm PR #40959](https://github.com/BerriAI/litellm/pull/40959); **ms-swift**: GLM template Python-repr bug — [ms-swift PR #10169](https://github.com/modelscope/ms-swift/pull/10169); llama.cpp: streamed/non-streamed args with "mixed single/double quotes" on large payloads — [llama.cpp issue #20359](https://github.com/ggml-org/llama.cpp/issues/20359)

### Inferences
- **[inference]** Harness defaults derived from these: (1) lenient argument parser (strip fences, `ast.literal_eval` fallback, brace-balance repair) → re-encode as canonical JSON before replay; (2) treat malformed calls as a tool error fed back to the model, never a fatal stream error; (3) aggregate streamed tool-call deltas by `index`; (4) verify at least one parseable call before honouring `finish_reason: tool_calls`, else re-prompt; (5) detect N identical consecutive tool calls and inject a "you already ran this; the output was …" nudge (GLM-5.2 loop); (6) keep edit/write payloads small for GLM on fp4/NIM/Fireworks hosts (split large writes) — the failure is payload-size correlated; (7) never synthesise or rewrite MiniMax tool ids (compaction must preserve them).

### Gaps
- No Cline- or Roo-specific issue with an exact error string for GLM/MiniMax native tool use was found in this pass (only the Qwen3-Coder custom-syntax discussion); Roo's `zai` provider settings were not examined.
- claude-code-router's transformer catalogue is no longer in the README (moved to an external docs site), so I could not verify which transformers it applies to Z.ai/DashScope/MiniMax.
- Whether the Crush GLM-5.2 large-argument failure is Fireworks-specific (fp4/streaming) or reproducible on Z.ai is unknown.

---

## Key question 6 — Provider pinning on OpenRouter per ID; Databricks availability and limits

### Takeaway
Pin `z-ai` for every GLM ID (it is the only provider for glm-4.5; for 4.6/4.7 it is listed as fp4, so consider `novita` bf16 for 4.6 or `google-vertex` for 4.7 if quantisation matters); pin `alibaba` for every Qwen commercial ID (it is the only provider for `qwen3-max`, `qwen3-coder-plus`, `qwen3-coder-flash`, `qwen3-235b-a22b`) and `alibaba`/`parasail`/`deepinfra` for open-weight Qwen; pin `minimax` for M2/M2.1 (but `novita` for M1 since the official M1 endpoint lacks tools). Databricks offers `databricks-glm-5-3`, `-5-3-flash`, `-5-2`, `databricks-qwen35-122b-a10b`, `databricks-qwen3-next-80b-a3b-instruct` (no GLM-4.x, no other Qwen, no MiniMax), with a 32-tool cap and no parallel calls.

### Cited Findings
- Endpoint inventory with quantisation/uptime/limits per provider (see Key question 1 list) — [OpenRouter endpoints API](https://openrouter.ai/api/v1/models/z-ai/glm-4.6/endpoints); notable: Z.AI's own endpoints for glm-4.6 and glm-4.7 are tagged `z-ai/fp4`, glm-4.5 and glm-5.x `z-ai/fp8`; Venice caps output at 16,384 for glm-4.6/4.7/4.7-flash; DeepInfra caps qwen3-235b-a22b-2507 and qwen3-next-80b at 16,384 out; DigitalOcean/Parasail/Cloudflare serve glm-5.2 at only 262,144 ctx; Cloudflare serves glm-5.3 at 1,310,720 ctx
- OpenRouter top-level (`top_provider`) numbers: `z-ai/glm-4.6` 204,800 / **16,384**; `z-ai/glm-4.7` 204,800 / 131,072; `z-ai/glm-5` 204,800 / 128,000; `z-ai/glm-5.2` 1,048,576 / 131,072; `z-ai/glm-5.3` 1,310,720 / 131,072; `z-ai/glm-5.3-flash` 1,310,720 / 943,718; `qwen/qwen3-235b-a22b-2507` 262,144 / 235,929; `qwen/qwen3-235b-a22b-thinking-2507` 131,072 / 117,964; `qwen/qwen3-coder` 262,144 / 65,536; `qwen/qwen3-coder-flash` 1,000,000 / 65,536; `qwen/qwen3-coder-next` 262,144 / 235,929; `qwen/qwen3-next-80b-a3b-instruct` 262,144 / 235,929; `qwen/qwen3.5-122b-a10b` 262,144 / 65,536; `qwen/qwen3-max` 262,144 / 65,536; `minimax/minimax-m1` 1,000,000 / 40,000; `minimax/minimax-m2` 204,800 / 176,947; `minimax/minimax-m2.1` 204,800 / 131,072 — [OpenRouter models](https://openrouter.ai/api/v1/models)
- Provider routing controls (`order`, `only`, `ignore`, `allow_fallbacks`, `require_parameters`, `quantizations`, `sort`; `:nitro`, `:floor`) — [OpenRouter provider routing](https://openrouter.ai/docs/features/provider-routing)
- Databricks catalogue rows and reasoning rules (GLM 5.3 / 5.3 Flash / 5.2; Qwen3.5 122B "Public Preview"; Qwen3-Next 80B "Public Preview") — [Databricks supported models](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/supported-models); 32-tool cap, no parallel calls, single-turn optimised, schema limits — [Databricks function calling](https://docs.databricks.com/aws/en/machine-learning/model-serving/function-calling); rate limits per model — [Databricks FMAPI limits](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/limits); models.dev `databricks` entry lists only `databricks-glm-5-2` (1,000,000 / 131,072) via `https://${DATABRICKS_HOST}/ai-gateway/mlflow/v1` — [models.dev api.json](https://models.dev/api.json)
- Z.ai endpoints: general `https://api.z.ai/api/paas/v4` (China `https://open.bigmodel.cn/api/paas/v4`), Coding Plan `https://api.z.ai/api/coding/paas/v4` (China `https://open.bigmodel.cn/api/coding/paas/v4`), Anthropic `https://api.z.ai/api/anthropic`; "Coding Plan keys should use a Coding Plan endpoint ... general API keys should use a general API endpoint" — [OpenClaw Z.AI docs](https://docs.openclaw.ai/providers/zai), [cherry-studio discussion #13965](https://github.com/CherryHQ/cherry-studio/discussions/13965), [models.dev api.json](https://models.dev/api.json); Claude Code mapping OPUS/SONNET → `glm-5.3`, HAIKU → `glm-5.3-flash` — [Z.ai Claude Code guide](https://docs.z.ai/devpack/tool/claude)
- DashScope endpoints: international `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`, China `https://dashscope.aliyuncs.com/compatible-mode/v1`, coding `coding.dashscope.aliyuncs.com/v1` — [models.dev api.json](https://models.dev/api.json), [pi issue #2770](https://github.com/earendil-works/pi/issues/2770)
- MiniMax endpoints: OpenAI-compatible `https://api.minimax.io/v1`, Anthropic `https://api.minimax.io/anthropic`; models.dev/OpenCode route MiniMax through `@ai-sdk/anthropic` at `https://api.minimax.io/anthropic/v1` — [MiniMax OpenAI-compatible API](https://platform.minimax.io/docs/api-reference/text-openai-api), [models.dev api.json](https://models.dev/api.json)

### Inferences
- **[inference]** Pin lists (`provider.order`, `allow_fallbacks: false`, `require_parameters: true`): glm-4.5 → `["z-ai"]`; glm-4.6 → `["z-ai","novita","deepinfra"]` (avoid `venice`, 16K out); glm-4.7 → `["z-ai","google-vertex","deepinfra","novita"]` (never `mancer`, no tools; `atlas-cloud` 57% uptime); glm-5 → `["z-ai","novita","baidu","streamlake"]`; glm-5.2 → `["z-ai","fireworks","together","baseten","novita"]` (avoid 262K-ctx hosts; note Crush's Fireworks failure); glm-5.3 → `["z-ai","fireworks","together","baseten","novita"]`; glm-5.3-flash → `["z-ai","fireworks","together","novita"]`; qwen3-235b-a22b-2507 → `["alibaba","gmicloud","parasail"]` (avoid `venice`/`deepinfra`/`nebius` uptime); thinking-2507 → `["alibaba"]`; qwen3-coder → `["alibaba","google-vertex","novita"]`; coder-flash/coder-plus/qwen3-max → `["alibaba"]`; coder-next → `["alibaba","parasail","novita"]`; qwen3-next-80b → `["alibaba","parasail","google-vertex"]`; qwen3.5-122b → `["alibaba","novita","atlas-cloud"]` (never `siliconflow`); minimax-m1 → `["novita"]`; minimax-m2/m2.1 → `["minimax","novita"]`.
- **[inference]** Databricks rows should set `max_tools ≤ 32`, `parallel_tool_calls: false`, flatten schemas (≤16 keys, no `$ref`/`anyOf`/`pattern`), and expect single-turn-optimised function calling.

### Gaps
- Databricks region availability tables were not readable through my fetch; output caps for `databricks-glm-5-2`/`qwen3-next` unconfirmed.
- OpenRouter uptime figures are 30-minute snapshots (2026-09-23 ~23:45 UTC) and will drift.

---

## Adapter rows (machine-usable)

Conventions: `null` = not documented/unknown; strings containing "(inferred)" are my recommendations, not vendor statements; `temperature`/`top_p`/`top_k` are vendor-recommended values for agentic use; `max_output_default` is the vendor default where documented (Z.ai publishes caps only); `overflow_error_regex` is per-transport (vendor API first, then OpenRouter).

```json
{
  "adapter_rows": [
    {
      "id_openrouter": "z-ai/glm-4.5",
      "id_official": "glm-4.5",
      "id_databricks": null,
      "family": "glm",
      "tools": true,
      "tool_id_format": "opaque string (undocumented); self-hosted vLLM glm45 parser emits call_<8hex>",
      "parallel_tools": "yes via multiple <tool_call> blocks; no API toggle; not documented by Z.ai",
      "strict_mode": "response_format json_object only; no json_schema/strict",
      "reasoning_field": "reasoning_content",
      "reasoning_replay": "echo reasoning_content verbatim in assistant turns with tool_calls (interleaved thinking since 4.5); clear_thinking=true default",
      "thinking_toggle": "thinking: {type: enabled|disabled} (default enabled); reasoning_effort not documented for 4.5",
      "temperature": 0.6,
      "top_p": 0.95,
      "top_k": null,
      "max_output_default": null,
      "max_output_cap": 98304,
      "context": 131072,
      "overflow_error_regex": "\"code\"\\s*:\\s*\"?1261\"?|Prompt too long|model_context_window_exceeded|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "system role accepted; placement constraint undocumented; put one system message at index 0 (inferred)",
      "vision": false,
      "provider_pin": "z-ai (only provider)",
      "quirks": [
        "Z.ai temperature range [0,1]; Messages API 400s on >1",
        "tool_choice only 'auto'; tools max 128",
        "reasoning parser (glm45) can leak <think> into content from turn 3+ after tool calls when self-hosted (vLLM #27703)",
        "history arguments given as dict render as Python repr in stock template; send JSON strings",
        "Python-repr argument emission (True/None/single quotes) documented as template/training artefact — run literal_eval repair then re-encode JSON"
      ]
    },
    {
      "id_openrouter": "z-ai/glm-4.6",
      "id_official": "glm-4.6",
      "id_databricks": null,
      "family": "glm",
      "tools": true,
      "tool_id_format": "opaque string (undocumented)",
      "parallel_tools": "yes via multiple <tool_call> blocks; no API toggle",
      "strict_mode": "response_format json_object only on Z.ai; OpenRouter structured_outputs only on venice endpoint",
      "reasoning_field": "reasoning_content (OpenRouter: reasoning/reasoning_details)",
      "reasoning_replay": "echo reasoning_content verbatim in tool loops",
      "thinking_toggle": "thinking: {type: enabled|disabled} (default enabled); tool_stream supported (default false)",
      "temperature": 1.0,
      "top_p": 0.95,
      "top_k": null,
      "max_output_default": null,
      "max_output_cap": 131072,
      "context": 204800,
      "overflow_error_regex": "\"code\"\\s*:\\s*\"?1261\"?|Prompt too long|model_context_window_exceeded|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "undocumented; single system at index 0 (inferred)",
      "vision": false,
      "provider_pin": "z-ai (tagged fp4), novita (bf16), deepinfra; avoid venice (16,384 max output)",
      "quirks": [
        "OpenRouter top_provider max_completion_tokens is 16,384 (Venice) — set provider pin or max_tokens explicitly",
        "Crush: Fireworks-hosted GLM-4.6 emitted invalid-JSON arguments on large edit/write payloads (crush #3153)",
        "vLLM parser glm45 (not glm47) for 4.6"
      ]
    },
    {
      "id_openrouter": "z-ai/glm-4.7",
      "id_official": "glm-4.7",
      "id_databricks": null,
      "family": "glm",
      "tools": true,
      "tool_id_format": "opaque string (undocumented); vLLM glm47 parser emits call_<8hex>",
      "parallel_tools": "yes via multiple <tool_call> blocks; no API toggle",
      "strict_mode": "response_format json_object only on Z.ai; structured_outputs on deepinfra/venice/atlas/google-vertex endpoints",
      "reasoning_field": "reasoning_content (OpenRouter models.dev: reasoning_details)",
      "reasoning_replay": "echo reasoning_content verbatim; 'Preserved Thinking' retains prior-turn reasoning (clear_thinking:false; on by default on Coding Plan endpoint)",
      "thinking_toggle": "thinking: {type: enabled|disabled} per turn (turn-level thinking introduced in 4.7); tool_stream supported",
      "temperature": 1.0,
      "top_p": 0.95,
      "top_k": null,
      "max_output_default": null,
      "max_output_cap": 131072,
      "context": 204800,
      "overflow_error_regex": "\"code\"\\s*:\\s*\"?1261\"?|Prompt too long|model_context_window_exceeded|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "undocumented; single system at index 0 (inferred)",
      "vision": false,
      "provider_pin": "z-ai, google-vertex, deepinfra, novita; never mancer (no tools); atlas-cloud low uptime; venice 16K out",
      "quirks": [
        "Agentic benchmark settings from Z.ai: temperature 0.7 / top_p 1.0 (Terminal-Bench, SWE-bench)",
        "GLM Coding Plan now routes glm-4.7 requests to glm-5.3-flash",
        "SGLang+Claude Code: duplicated '<tool_call><tool_call><tool_call>Read' markers and parser crash (sglang #15721)",
        "Text wire format: <tool_call>name\\n<arg_key>k</arg_key>\\n<arg_value>v</arg_value></tool_call>; parser glm47 + reasoning parser glm45"
      ]
    },
    {
      "id_openrouter": "z-ai/glm-5",
      "id_official": "glm-5",
      "id_databricks": null,
      "family": "glm",
      "tools": true,
      "tool_id_format": "opaque string (undocumented)",
      "parallel_tools": "yes via multiple <tool_call> blocks; no API toggle",
      "strict_mode": "response_format json_object; structured output listed for glm-5 on Z.ai",
      "reasoning_field": "reasoning_content",
      "reasoning_replay": "echo reasoning_content verbatim (interleaved + preserved thinking)",
      "thinking_toggle": "thinking: {type: enabled|disabled} (default enabled); tool_stream supported",
      "temperature": 1.0,
      "top_p": 0.95,
      "top_k": null,
      "max_output_default": null,
      "max_output_cap": 131072,
      "context": 204800,
      "overflow_error_regex": "\"code\"\\s*:\\s*\"?1261\"?|Prompt too long|model_context_window_exceeded|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "undocumented; single system at index 0 (inferred)",
      "vision": false,
      "provider_pin": "z-ai, novita, baidu, streamlake, gmicloud; amazon-bedrock and venice (32K out) also list it",
      "quirks": [
        "SWE-bench settings temperature 0.7 / top_p 0.95; Terminal-Bench 0.7 / 1.0",
        "NVIDIA NIM hosting emitted truncated JSON arguments (missing '}') in OpenCode (GLM-5 #15)",
        "parsers: --tool-call-parser glm47 --reasoning-parser glm45"
      ]
    },
    {
      "id_openrouter": "z-ai/glm-5.2",
      "id_official": "glm-5.2",
      "id_databricks": "databricks-glm-5-2",
      "family": "glm",
      "tools": true,
      "tool_id_format": "opaque string; on Mistral-hosted OpenRouter endpoint the id changed mid-stream (chatcmpl-tool-… then toolcall0) — aggregate by index",
      "parallel_tools": "yes on Z.ai (multiple <tool_call> blocks); Databricks: parallel function calling not supported (32-tool cap)",
      "strict_mode": "Z.ai json_object only (Z.AI OpenRouter endpoint lacks structured_outputs); Databricks structured output supported with schema limits (16 keys, no $ref/anyOf/pattern)",
      "reasoning_field": "reasoning_content (Databricks: content items type 'reasoning')",
      "reasoning_replay": "echo reasoning_content verbatim; preserved thinking default on Coding Plan endpoint",
      "thinking_toggle": "Z.ai: thinking.type enabled|disabled (docs) — but field report of 400 code 1210 on disabled; reasoning_effort max(default)|xhigh|high|medium|low|minimal|none (none/minimal skip thinking, low/medium→high, xhigh→max); Databricks: reasoning_effort high|max (default max, others→max, hybrid)",
      "temperature": 1.0,
      "top_p": 0.95,
      "top_k": null,
      "max_output_default": null,
      "max_output_cap": 131072,
      "context": 1048576,
      "overflow_error_regex": "\"code\"\\s*:\\s*\"?1261\"?|Prompt too long|model_context_window_exceeded|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "undocumented; single system at index 0 (inferred)",
      "vision": false,
      "provider_pin": "z-ai (fp8, 1M/131K), fireworks, together, baseten, novita; avoid digitalocean/parasail/cloudflare (262K ctx); :free variant has no tools",
      "quirks": [
        "Crush hard crash on Fireworks: 'tool_calls[].function.arguments for function \\'edit\\' must be a JSON object string (or an object), got invalid JSON' on large edit/write args",
        "Plan-vs-emission drift loop: planned 'cd … &&' prefix dropped from emitted bash call 10x in a row via Anthropic-compatible endpoint + Claude Code (GLM-5 #116)",
        "tool_choice:'required' makes the model emit a JSON array inside <tool_call> that glm47 cannot parse (vLLM #48095)",
        "Tool results intermittently rendered as image blocks at long context (GLM-5 #110)",
        "Databricks rate limit: ITPM 200,000 / OTPM 20,000 / QPH 7,200",
        "GLM Coding Plan routes glm-5.2 to glm-5.3"
      ]
    },
    {
      "id_openrouter": "z-ai/glm-5.3",
      "id_official": "glm-5.3",
      "id_databricks": "databricks-glm-5-3",
      "family": "glm",
      "tools": true,
      "tool_id_format": "opaque string (undocumented)",
      "parallel_tools": "yes (Databricks row: 'parallel tool calls'; Z.ai Responses API verified 2 parallel calls); Databricks generic doc says parallel unsupported",
      "strict_mode": "Z.ai lists 'Structured Output' for 5.3 but API doc only shows json_object; Databricks structured output with schema limits",
      "reasoning_field": "reasoning_content",
      "reasoning_replay": "echo reasoning_content verbatim; 'All consecutive reasoning_content blocks must exactly match the original sequence'",
      "thinking_toggle": "ALWAYS REASONS. thinking.type must be 'enabled'; reasoning_effort low|high|max only (default max); 'disabled' → HTTP 400 code 1210 '该模型始终思考，不支持关闭思考；请使用 low、high 或 max。' (Databricks: none rejected; minimal/medium/xhigh→max); map 'off'→low",
      "temperature": 1.0,
      "top_p": 0.95,
      "top_k": null,
      "max_output_default": null,
      "max_output_cap": 131072,
      "context": 1048576,
      "overflow_error_regex": "\"code\"\\s*:\\s*\"?1261\"?|Prompt too long|model_context_window_exceeded|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "undocumented; single system at index 0 (inferred)",
      "vision": false,
      "provider_pin": "z-ai (fp8 1,048,576/131,072), fireworks, together, baseten, novita; avoid reka/io-net (262K ctx); cloudflare offers 1,310,720 ctx",
      "quirks": [
        "Databricks caps output at 65,536; rate limit ITPM 2,000,000 / OTPM 40,000 / QPH 7,200",
        "Z.ai Anthropic endpoint: thinking.type enabled needs budget_tokens ≥1 or 400 'budget_tokens is required and must be positive when thinking type is enabled' (zai feedback #780); effort carried as output_config.effort",
        "Coding Plan default model; Claude Code mapping OPUS/SONNET→glm-5.3; /effort default max",
        "Self-hosted: template ignores enable_thinking; passing it makes vLLM leak reasoning with dangling </think> (vLLM #54744)"
      ]
    },
    {
      "id_openrouter": "z-ai/glm-5.3-flash",
      "id_official": "glm-5.3-flash",
      "id_databricks": "databricks-glm-5-3-flash",
      "family": "glm",
      "tools": true,
      "tool_id_format": "opaque string (undocumented)",
      "parallel_tools": "yes via multiple <tool_call> blocks; Databricks: no parallel",
      "strict_mode": "json_object on Z.ai; structured_outputs on most OpenRouter endpoints (not Z.AI's own)",
      "reasoning_field": "reasoning_content",
      "reasoning_replay": "echo reasoning_content verbatim",
      "thinking_toggle": "ALWAYS REASONS ('thinking.type only supports enabled'); reasoning_effort low|high|max; Databricks: none returns error",
      "temperature": 1.0,
      "top_p": 0.95,
      "top_k": null,
      "max_output_default": null,
      "max_output_cap": 131072,
      "context": 1048576,
      "overflow_error_regex": "\"code\"\\s*:\\s*\"?1261\"?|Prompt too long|model_context_window_exceeded|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "undocumented; single system at index 0 (inferred)",
      "vision": true,
      "provider_pin": "z-ai (fp8), fireworks, together, novita, deepinfra; io-net/reka 262K ctx; morph 78% uptime",
      "quirks": [
        "Inputs: video/image/text/file (Z.ai); Databricks: text+image",
        "Quantised local serving: 4.2% malformed tool calls at T=1.0 vs 0.8% at T=0.6 (2.05bpw, 16 tools, streaming); parser must be glm47 (glm45 extracts zero calls)",
        "finish_reason=tool_calls with empty content observed on local serving (ds4 #1) — validate before acting",
        "Stock chat template crashes when history tool_calls[].function.arguments is a JSON string ('str object' has no attribute 'items') — pre-parse to dict or use SGLang",
        "ollama-cloud variant string-encoded the tool_call 'calls' payload (hermes-agent PR #114656)",
        "Claude Code HAIKU mapping target on GLM Coding Plan; glm-5.3-flash[1m] suffix enables 1M with CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000"
      ]
    },
    {
      "id_openrouter": "qwen/qwen3-235b-a22b-2507",
      "id_official": "qwen3-235b-a22b-instruct-2507 (DashScope ID inferred from naming; HF Qwen/Qwen3-235B-A22B-Instruct-2507)",
      "id_databricks": null,
      "family": "qwen",
      "tools": true,
      "tool_id_format": "DashScope call_<22hex> (e.g. call_6596dafa2a6a46f7a217da); vLLM hermes parser ids vary by host",
      "parallel_tools": "DashScope parallel_tool_calls default false (set true); open-weight hermes template supports multiple <tool_call>",
      "strict_mode": "response_format json_object; strict not documented; OpenRouter structured_outputs on gmicloud/deepinfra/novita/parasail/nebius/google-vertex, not alibaba",
      "reasoning_field": "none (non-thinking model)",
      "reasoning_replay": "n/a",
      "thinking_toggle": "none — 'supports only non-thinking mode'; do not send enable_thinking",
      "temperature": 0.7,
      "top_p": 0.8,
      "top_k": 20,
      "max_output_default": null,
      "max_output_cap": 16384,
      "context": 262144,
      "overflow_error_regex": "Range of input length should be \\[1, ?\\d+\\]|InvalidParameter|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "Qwen3 template: system first recommended; Qwen3.5+ rule not present in Qwen3 template (unverified for this model)",
      "vision": false,
      "provider_pin": "alibaba (131,072/32,768), gmicloud (262,144), parasail; avoid deepinfra (40% uptime, 16K out), venice (13%), nebius (69%); one google-vertex endpoint lacks tools",
      "quirks": [
        "min_p 0; presence_penalty 0-2 (higher → language mixing); recommended output 16,384",
        "Hermes text format <tool_call>{\"name\":…,\"arguments\":{…}}</tool_call>; parser hermes",
        "Qwen models occasionally emit Python-literal arguments {'city': 'Paris'} — repair + re-encode before replay (agno #10231)"
      ]
    },
    {
      "id_openrouter": "qwen/qwen3-235b-a22b-thinking-2507",
      "id_official": "qwen3-235b-a22b-thinking-2507",
      "id_databricks": null,
      "family": "qwen",
      "tools": true,
      "tool_id_format": "DashScope call_<hex>",
      "parallel_tools": "DashScope parallel_tool_calls default false",
      "strict_mode": "json_object; no structured_outputs on OpenRouter endpoints",
      "reasoning_field": "reasoning_content (OpenRouter: reasoning/reasoning_details)",
      "reasoning_replay": "do NOT replay: 'Historical model output should only include the final output part'",
      "thinking_toggle": "cannot disable (DashScope thinking-only list); streaming required on DashScope for Qwen3 open-source ('parameter.enable_thinking only support stream call')",
      "temperature": 0.6,
      "top_p": 0.95,
      "top_k": 20,
      "max_output_default": null,
      "max_output_cap": 81920,
      "context": 131072,
      "overflow_error_regex": "Range of input length should be \\[1, ?\\d+\\]|InvalidParameter|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "system first (inferred)",
      "vision": false,
      "provider_pin": "alibaba (only endpoint with 117,964 out); novita/venice stale",
      "quirks": [
        "Output may contain only '</think>' (template pre-inserts <think>) — strip both",
        "Recommended output 32,768 (81,920 for hard problems); OpenRouter context 131,072 on this ID",
        "--reasoning-parser deepseek_r1 when self-hosted"
      ]
    },
    {
      "id_openrouter": "qwen/qwen3-coder",
      "id_official": "qwen3-coder-plus (commercial 480B); qwen3-coder-480b-a35b-instruct (open weights on DashScope)",
      "id_databricks": null,
      "family": "qwen-coder",
      "tools": true,
      "tool_id_format": "DashScope call_<hex>; bundled parser emits call_<24hex>",
      "parallel_tools": "DashScope parallel_tool_calls default false; parser handles multiple <function> blocks",
      "strict_mode": "json_object; structured_outputs on most OpenRouter endpoints (google-vertex/deepinfra/venice/novita/alibaba-opensource)",
      "reasoning_field": "none",
      "reasoning_replay": "n/a",
      "thinking_toggle": "none (non-thinking)",
      "temperature": 0.7,
      "top_p": 0.8,
      "top_k": 20,
      "max_output_default": null,
      "max_output_cap": 65536,
      "context": 262144,
      "overflow_error_regex": "Range of input length should be \\[1, ?\\d+\\]|InvalidParameter|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "system first (inferred)",
      "vision": false,
      "provider_pin": "alibaba, google-vertex, novita, deepinfra(turbo/fp4), venice; ':free' variant no longer exists — its absence caused 'No endpoints found that support tool use' 404s",
      "quirks": [
        "repetition_penalty 1.05 recommended",
        "Wire format <tool_call><function=name><parameter=key>value</parameter></function></tool_call>; parser qwen3_coder / qwen3_xml types values from the tool schema (json.loads→ast.literal_eval for object/array; unknown params stay strings)",
        "OpenCode: 'Invalid input: expected string, received object' on edit oldString/newString via qwen/qwen3-coder:free — keep string params typed 'string', coerce objects to strings",
        "Model omits leading <tool_call> tag after prose (Qwen3-Coder #475); prefers its XML over Roo/Cline bracket syntax",
        "qwen3-coder-plus: 1,048,576 ctx / 65,536 out; implicit cache ≥1024-token prefix at 20% price"
      ]
    },
    {
      "id_openrouter": "qwen/qwen3-coder-flash",
      "id_official": "qwen3-coder-flash",
      "id_databricks": null,
      "family": "qwen-coder",
      "tools": true,
      "tool_id_format": "DashScope call_<hex>",
      "parallel_tools": "DashScope parallel_tool_calls default false",
      "strict_mode": "json_object; no structured_outputs on OpenRouter",
      "reasoning_field": "none",
      "reasoning_replay": "n/a",
      "thinking_toggle": "none (non-thinking)",
      "temperature": 0.7,
      "top_p": 0.8,
      "top_k": 20,
      "max_output_default": null,
      "max_output_cap": 65536,
      "context": 1000000,
      "overflow_error_regex": "Range of input length should be \\[1, ?\\d+\\]|InvalidParameter|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "system first (inferred)",
      "vision": false,
      "provider_pin": "alibaba (only provider)",
      "quirks": [
        "Sampling preset inferred from Qwen3-Coder-480B card (no card for the Flash commercial ID)",
        "Alibaba Coding Plan input limit observed as 169,984 tokens ('Range of input length should be [1, 169984]')"
      ]
    },
    {
      "id_openrouter": "qwen/qwen3-coder-next",
      "id_official": "qwen3-coder-next (DashScope ID inferred; HF Qwen/Qwen3-Coder-Next)",
      "id_databricks": null,
      "family": "qwen-coder",
      "tools": true,
      "tool_id_format": "DashScope call_<hex>; parser call_<24hex>",
      "parallel_tools": "DashScope parallel_tool_calls default false",
      "strict_mode": "json_object; structured_outputs on parasail endpoint",
      "reasoning_field": "none",
      "reasoning_replay": "n/a",
      "thinking_toggle": "none — 'supports only non-thinking mode'",
      "temperature": 1.0,
      "top_p": 0.95,
      "top_k": 40,
      "max_output_default": null,
      "max_output_cap": 65536,
      "context": 262144,
      "overflow_error_regex": "Range of input length should be \\[1, ?\\d+\\]|InvalidParameter|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "system first (inferred)",
      "vision": false,
      "provider_pin": "alibaba, parasail (bf16, 235,929 out), novita, streamlake",
      "quirks": [
        "80B total / 3B active; --tool-call-parser qwen3_coder",
        "Card explicitly targets Claude Code, Qwen Code, Kilo, Cline, Trae scaffolds",
        "GGUF template errors reported in LM Studio + OpenCode/Qwen Code/Kilo (unsloth discussion)"
      ]
    },
    {
      "id_openrouter": "qwen/qwen3-next-80b-a3b-instruct",
      "id_official": "qwen3-next-80b-a3b-instruct",
      "id_databricks": "databricks-qwen3-next-80b-a3b-instruct",
      "family": "qwen",
      "tools": true,
      "tool_id_format": "DashScope call_<hex>",
      "parallel_tools": "DashScope parallel_tool_calls default false; Databricks: no parallel, 32-tool cap",
      "strict_mode": "json_object; structured_outputs on deepinfra/parasail/google-vertex",
      "reasoning_field": "none",
      "reasoning_replay": "n/a",
      "thinking_toggle": "none — instruct-only; sibling qwen/qwen3-next-80b-a3b-thinking is thinking-only (reasoning_content, strip from history)",
      "temperature": 0.7,
      "top_p": 0.8,
      "top_k": 20,
      "max_output_default": null,
      "max_output_cap": 32768,
      "context": 262144,
      "overflow_error_regex": "Range of input length should be \\[1, ?\\d+\\]|InvalidParameter|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "system first (inferred)",
      "vision": false,
      "provider_pin": "alibaba (131,072/32,768), parasail, google-vertex (262,144/235,929); deepinfra caps 16,384 out; novita 71% uptime",
      "quirks": [
        "Hermes tool format (parser hermes); MinP 0; presence_penalty 0-2",
        "Databricks: Public Preview, text-only; context/output caps not published on page read",
        "DashScope official context 131,072 vs 262,144 native"
      ]
    },
    {
      "id_openrouter": "qwen/qwen3.5-122b-a10b",
      "id_official": "qwen3.5-122b-a10b",
      "id_databricks": "databricks-qwen35-122b-a10b",
      "family": "qwen3.5",
      "tools": true,
      "tool_id_format": "DashScope call_<hex>; parser call_<24hex>",
      "parallel_tools": "DashScope parallel_tool_calls default false; Databricks no parallel",
      "strict_mode": "json_object; structured_outputs on alibaba/atlas-cloud/siliconflow endpoints; Databricks structured output",
      "reasoning_field": "reasoning_content (OpenRouter reasoning_details; Databricks reasoning items)",
      "reasoning_replay": "undocumented for 3.5 on DashScope (preserve_thinking listed only for 3.6+/3.8-max); Databricks: keep reasoning items if the API returns them (inferred)",
      "thinking_toggle": "hybrid, thinking ON by default; disable with enable_thinking:false (DashScope top-level) or chat_template_kwargs.enable_thinking:false (open weights); Databricks: 'reasoning cannot be disabled'; ALWAYS send enable_thinking explicitly",
      "temperature": 1.0,
      "top_p": 0.95,
      "top_k": 20,
      "max_output_default": null,
      "max_output_cap": 65536,
      "context": 262144,
      "overflow_error_regex": "Range of input length should be \\[1, ?\\d+\\]|InvalidParameter|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "HARD: exactly one system message at index 0 — template raises 'System message must be at the beginning.' otherwise (any host using the stock template)",
      "vision": true,
      "provider_pin": "alibaba, novita (bf16), atlas-cloud, deepinfra (fp4); NEVER siliconflow (no tools, 17% uptime)",
      "quirks": [
        "Presets: thinking-general 1.0/0.95/20 presence 1.5; thinking-precise-coding 0.6/0.95/20 presence 0; instruct-general 0.7/0.8/20 presence 1.5",
        "--tool-call-parser qwen3_coder --reasoning-parser qwen3 (Qwen3-Coder XML wire)",
        "Databricks: 256K ctx / 25K out; ITPM 1,000,000 / OTPM 100,000 / QPH 360,000; Public Preview",
        "Modalities text+image+video (OpenRouter), +audio per models.dev alibaba"
      ]
    },
    {
      "id_openrouter": "qwen/qwen3.5-397b-a17b",
      "id_official": "qwen3.5-397b-a17b (also commercial qwen3.5-plus / qwen3.5-flash: 1,000,000 ctx / 65,536 out)",
      "id_databricks": null,
      "family": "qwen3.5",
      "tools": true,
      "tool_id_format": "DashScope call_<hex>",
      "parallel_tools": "DashScope parallel_tool_calls default false",
      "strict_mode": "json_object; structured_outputs on most endpoints",
      "reasoning_field": "reasoning_content",
      "reasoning_replay": "undocumented for 3.5 (preserve_thinking is 3.6+)",
      "thinking_toggle": "hybrid, ON by default; send enable_thinking explicitly (pi #2770: coding endpoint keeps thinking unless told)",
      "temperature": 1.0,
      "top_p": 0.95,
      "top_k": 20,
      "max_output_default": null,
      "max_output_cap": 65536,
      "context": 262144,
      "overflow_error_regex": "Range of input length should be \\[1, ?\\d+\\]|InvalidParameter|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "HARD: single system at index 0 (opencode #20785 hit 'system message must be at the beginning')",
      "vision": true,
      "provider_pin": "alibaba, parasail, deepinfra, phala; avoid streamlake (no tools)",
      "quirks": [
        "Same Qwen3.5 presets/parsers as 122B",
        "OpenCode superpowers-style plugins that inject a second system prompt break this model"
      ]
    },
    {
      "id_openrouter": "qwen/qwen3-max",
      "id_official": "qwen3-max (thinking sibling on OpenRouter: qwen/qwen3-max-thinking)",
      "id_databricks": null,
      "family": "qwen",
      "tools": true,
      "tool_id_format": "DashScope call_<hex>",
      "parallel_tools": "DashScope parallel_tool_calls default false",
      "strict_mode": "json_object; structured_outputs on alibaba endpoint",
      "reasoning_field": "reasoning_content when enable_thinking:true on DashScope (China listing shows reasoning support since 2026-01); OpenRouter qwen/qwen3-max exposes no reasoning param",
      "reasoning_replay": "not documented (preserve_thinking not listed for qwen3-max)",
      "thinking_toggle": "thinking OFF by default on DashScope ('qwen3-max (commercial)'); enable_thinking:true to turn on; thinking_budget supported",
      "temperature": null,
      "top_p": null,
      "top_k": null,
      "max_output_default": null,
      "max_output_cap": 65536,
      "context": 262144,
      "overflow_error_regex": "Range of input length should be \\[1, ?\\d+\\]|InvalidParameter|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "undocumented for commercial API; single system at index 0 (inferred)",
      "vision": false,
      "provider_pin": "alibaba (only provider)",
      "quirks": [
        "No official sampling recommendation found — use Qwen3 instruct preset 0.7/0.8/20 (inferred)",
        "tool_choice 'required' not supported for Qwen on DashScope",
        "Implicit prefix cache (≥1024 tokens) at 20% input price"
      ]
    },
    {
      "id_openrouter": null,
      "id_official": "qwq-plus (international DashScope, thinking-only); qwq-32b (China DashScope; open weights Qwen/QwQ-32B)",
      "id_databricks": null,
      "family": "qwen-qwq",
      "tools": true,
      "tool_id_format": "DashScope call_<hex>",
      "parallel_tools": "DashScope parallel_tool_calls default false",
      "strict_mode": "json_object (undocumented for QwQ)",
      "reasoning_field": "reasoning_content",
      "reasoning_replay": "do NOT replay thinking ('historical model output should only include the final output part')",
      "thinking_toggle": "always thinks (qwq-plus on DashScope 'cannot be disabled'); enforce '<think>\\n' at generation start when self-hosting",
      "temperature": 0.6,
      "top_p": 0.95,
      "top_k": 20,
      "max_output_default": null,
      "max_output_cap": 8192,
      "context": 131072,
      "overflow_error_regex": "Range of input length should be \\[1, ?\\d+\\]|InvalidParameter",
      "system_placement": "system first (inferred)",
      "vision": false,
      "provider_pin": "none — qwen/qwq-32b removed from OpenRouter; use DashScope directly",
      "quirks": [
        "Avoid greedy decoding (endless repetition); min_p 0; top_k 20-40",
        "YaRN needed for prompts > 8,192 when self-hosting; vLLM static YaRN caveat",
        "HF card documents no tool-calling guidance; DashScope lists qwq-plus as tool-capable (models.dev)"
      ]
    },
    {
      "id_openrouter": "minimax/minimax-m1",
      "id_official": "MiniMax-M1 (official API status unverified; M1-80k marked Deprecated on SiliconFlow)",
      "id_databricks": null,
      "family": "minimax",
      "tools": true,
      "tool_id_format": "host-generated; official OpenRouter Minimax endpoint exposes NO tools for M1",
      "parallel_tools": "multiple JSON lines inside one <tool_calls> block",
      "strict_mode": "none documented",
      "reasoning_field": "<think>…</think> in content (reasoning/reasoning_details on OpenRouter Novita endpoint)",
      "reasoning_replay": "keep prior thinking per MiniMax guidance for M-series (inferred from M2 docs)",
      "thinking_toggle": "always thinks; 'Thinking budget: 80K tokens' (M1-80k)",
      "temperature": 1.0,
      "top_p": 0.95,
      "top_k": null,
      "max_output_default": null,
      "max_output_cap": 40000,
      "context": 1000000,
      "overflow_error_regex": "context window exceeds limit \\(2013\\)|\\(2013\\)|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "undocumented",
      "vision": false,
      "provider_pin": "novita (only OpenRouter endpoint with tools)",
      "quirks": [
        "Text wire format <tool_calls>\\n{\"name\":…,\"arguments\":{…}}\\n</tool_calls>; vLLM --tool-call-parser minimax; vLLM ≥ 0.9.2",
        "Legacy model — prefer M2.1+"
      ]
    },
    {
      "id_openrouter": "minimax/minimax-m2",
      "id_official": "MiniMax-M2",
      "id_databricks": null,
      "family": "minimax",
      "tools": true,
      "tool_id_format": "call_function_<12 lowercase alnum>_<n> observed (e.g. call_function_60llhpv7vhhb_1); MiniMax validates ids — replay verbatim, never synthesise",
      "parallel_tools": "multiple <invoke> blocks in one <minimax:tool_call>; API toggle undocumented",
      "strict_mode": "none on official endpoint (OpenRouter Minimax endpoint lacks structured_outputs/response_format); google-vertex endpoint has structured_outputs",
      "reasoning_field": "reasoning_details (with reasoning_split:true) else <think>…</think> inside content; OpenRouter: reasoning_details",
      "reasoning_replay": "REQUIRED: replay reasoning_details / <think> content unchanged with tool_calls ('Do not remove the <think>...</think> part'); Anthropic route: append full content incl. thinking blocks",
      "thinking_toggle": "cannot disable (M2.x); reasoning_split only changes where thinking is returned",
      "temperature": 1.0,
      "top_p": 0.95,
      "top_k": 40,
      "max_output_default": null,
      "max_output_cap": 131072,
      "context": 204800,
      "overflow_error_regex": "context window exceeds limit \\(2013\\)|invalid params.*\\(2013\\)|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "OpenAI route: undocumented; Anthropic route: top-level system field",
      "vision": false,
      "provider_pin": "minimax (fp8, 204,800/131,072), novita; google-vertex (196,608/176,947)",
      "quirks": [
        "API top_p default 0.9 vs card recommendation 0.95; temperature range [0,2] default 1",
        "presence_penalty/frequency_penalty/logit_bias ignored; n must be 1; function_call unsupported; prefer max_completion_tokens",
        "Error 2013 covers context overflow, bad argument JSON and unsupported tool_call id formats — non-retryable",
        "Anthropic-compatible route ignores top_k/stop_sequences; missing max_tokens can trigger 2013",
        "Interleaved-thinking ablation: Tau^2 87→64, BrowseComp 44.0→31.4 when thinking dropped"
      ]
    },
    {
      "id_openrouter": "minimax/minimax-m2.1",
      "id_official": "MiniMax-M2.1 (also MiniMax-M2.1-highspeed)",
      "id_databricks": null,
      "family": "minimax",
      "tools": true,
      "tool_id_format": "call_function_<12 alnum>_<n> observed; replay verbatim",
      "parallel_tools": "multiple <invoke> blocks per <minimax:tool_call>",
      "strict_mode": "none documented on official endpoint",
      "reasoning_field": "reasoning_details (reasoning_split:true) else <think> in content; OpenRouter reasoning_details",
      "reasoning_replay": "REQUIRED: replay unchanged incl. tool_calls turns",
      "thinking_toggle": "cannot disable",
      "temperature": 1.0,
      "top_p": 0.95,
      "top_k": 40,
      "max_output_default": null,
      "max_output_cap": 131072,
      "context": 204800,
      "overflow_error_regex": "context window exceeds limit \\(2013\\)|invalid params.*\\(2013\\)|maximum context length is \\d+ tokens\\. However, you requested about \\d+ tokens",
      "system_placement": "OpenAI route: undocumented; Anthropic route: top-level system",
      "vision": false,
      "provider_pin": "minimax (fp8) or minimax/highspeed (2x output price, ~100 tps), novita",
      "quirks": [
        "Wire format <minimax:tool_call><invoke name=\"f\"><parameter name=\"p\">v</parameter></invoke></minimax:tool_call>; vLLM --tool-call-parser minimax_m2 --reasoning-parser minimax_m2_append_think (parser drops closing </think> in some versions; args cannot contain raw </parameter>)",
        "Self-hosted tool results use content array [{name,type:'text',text}]; hosted API uses role tool + tool_call_id",
        "Evaluated with Claude Code as scaffold (SWE-bench, Terminal-bench 2.0)"
      ]
    }
  ]
}
```
