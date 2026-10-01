# Open-model coding-harness patterns (OpenCode, Cline, Roo, Kilo, Aider, Goose, Qwen Code, Kimi Code, Gemini CLI, Crush, Continue, Codex, oh-my-pi)

Research date: 2026-09-23. Scope: how Claude Code-like harnesses that target DeepSeek / Kimi K2 / Qwen3 / GLM-4.x / Gemini structure their loops, prompts, tool handling and context management, plus a Claude Code feature-parity map for "halo".

Source-quality legend used below:
- **[P]** = primary page fetched and read in full (GitHub issue/PR/docs page/raw file).
- **[S]** = taken from a search-result snippet only; page not independently fetched. Treat numbers marked [S] as "verify before quoting".

Repo note: sst/opencode issues now redirect to `anomalyco/opencode`; Roo-Code's repo was archived read-only on 2026-05-15 ([P] https://github.com/RooCodeInc/Roo-Code/issues/9551); MoonshotAI/kimi-cli (Python) is archived and replaced by MoonshotAI/kimi-code (TypeScript) ([S] https://github.com/MoonshotAI/kimi-cli).

---

## KQ1. Tool-call robustness: native vs text protocols, parsing/repair, hallucinated names, missing params

### Takeaway
Every surviving harness has converged on **native (OpenAI-style JSON) function calling as the default** and keeps a text/XML protocol only as an opt-in or legacy path; the reliability work has moved from "parse XML out of prose" to (a) schema tolerance (make optional what weak models omit), (b) JSON-argument repair before validation, (c) routing unparseable calls to an "invalid" pseudo-tool that feeds an error string back to the model instead of aborting, and (d) provider-side parser selection (`--tool-call-parser kimi_k2 | qwen3_coder | glm47 | hermes`) when self-hosting.

### Cited Findings

**OpenCode (sst/opencode → anomalyco/opencode, TypeScript, Vercel AI SDK)**
- Kimi K2 initially failed on the `bash` tool with `tool call validation failed: parameters for tool bash did not match schema: errors: [missing properties: 'description' ...]`; the fix (PR #1333, issue opened 2025-07-26) simply **made `description` optional** and "completely fixed the issue" — [P] https://github.com/sst/opencode/issues/1334
- Earlier Kimi K2 (via OpenRouter) failures were "error due to parsing of tool calls" (issue #929) and `AI_InvalidToolInputError: Invalid input for tool glob` (issue #1111) — [S] https://github.com/sst/opencode/issues/929 ; [S] https://github.com/sst/opencode/issues/1111
- Kimi K2 Thinking on OpenCode Zen (v1.1.28, 2026-01-21) emitted tool calls with **empty arguments** (`The argument 'file' cannot be empty. Received ''`, `playwright_browser_navigate [url=]`); the issue is closed but the visible thread has no root-cause analysis — [P] https://github.com/anomalyco/opencode/issues/9878
- OpenCode's repair hook is the AI SDK's `experimental_repairToolCall` in `packages/opencode/src/session/llm.ts` (lines ~178-197); when the tool name is valid but JSON parsing fails it routes to an `"invalid"` pseudo-tool. A reported cause of invalid JSON: **XML tags leaking into `function.arguments`** from SiliconFlow's Qwen3-8B — [S] https://github.com/anomalyco/opencode/issues/17750
- PR #23067 ("repair malformed JSON in tool call arguments") added repair for unterminated strings, missing closing brackets/braces, nested-structure completion, duplicated/overlapping JSON objects and missing commas, falling back "to existing 'invalid' tool handler if repair fails"; it was **closed unmerged on 2026-05-08**, the author saying #24289 solved it "in a more efficient manner" (a commenter said that PR was also unmerged) — [P] https://github.com/anomalyco/opencode/pull/23067
- Truncation bug: `maxOutputTokens(model) = Math.min(model.limit.output, OUTPUT_TOKEN_MAX)` with `OUTPUT_TOKEN_MAX = 32k`; a tool call cut off by `finishReason: length` is misclassified as an "invalid tool" and the session doom-loops or exits silently. Proposed fix: when `toolName` is registered but JSON fails, return "output truncated by token limit, split into smaller operations", and add `"length"` to the finish-reason exclusion list in `prompt.ts` (issue opened 2026-03-18) — [P] https://github.com/anomalyco/opencode/issues/18108
- Issue #15906 asks OpenCode to "retry invalid tool-call diff / malformed tool input instead of aborting" — [S] https://github.com/anomalyco/opencode/issues/15906
- Ollama tool-calling problems tracked in issue #3029 — [S] https://github.com/sst/opencode/issues/3029

**Cline (VS Code extension; native tool calling introduced v3.35)**
- v3.35 moved from declaring tools in the system prompt to native tool calling for "Claude 4+, Gemini 2.5, Grok 4, Grok Code, and GPT-5 (excluding gpt-5-chat)" on providers Cline, Anthropic, Gemini, OpenRouter, xAI, OpenAI-native, Vercel AI Gateway; "Models without native support continue using the text-based approach"; reported gains: fewer "invalid API response" errors (biggest for gpt-5-codex), ~15% fewer tokens per request, and parallel tool execution ("read three files ... simultaneously") — [P] https://cline.bot/blog/cline-v3-35
- DeepSeek native tool calling arrived in PR #7888 (merged 2026-01-27): `src/utils/model-utils.ts` adds DeepSeek to the native-tool provider list with `isDeepSeekModelFamily()` (family detection, not exact version); `src/core/api/transform/r1-format.ts` adds `addReasoningContent()`; `providers/deepseek.ts` uses it instead of `convertToR1Format` for reasoner models — [P] https://github.com/cline/cline/pull/7888
- Native tool calling for GLM-4/GLM-5 was still a feature request (discussion #10221) — [S] https://github.com/cline/cline/discussions/10221
- A third-party proxy (`irreg/native_tool_call_adapter`) exists purely to translate Cline/Roo XML tool calls into native API tool calls, evidence that XML-in-prose was the pain point — [S] https://github.com/irreg/native_tool_call_adapter

**Roo Code (fork of Cline)**
- v3.33 (2025-11-18): "Native Tool Calling for OpenAI-compatible Providers" (streaming and non-streaming), experimental native tools for Gemini (off by default), fixes for false "stuck in loop" detection with native tools, de-duplication of `tool_result` blocks, and "Reasoning tokens are now stored in conversation history across all providers" — [P] https://roocodeinc.github.io/Roo-Code/update-notes/v3.33
- v3.37+ **removed the XML tool-protocol selector and forced native tool calling**; this broke SGLang serving `openai/gpt-oss-120b` with HarmonyParser (500 "structure_info not used with HarmonyParser"); v3.36.16 with XML worked; closed "not planned", no XML fallback restored (issue opened 2025-12-24) — [P] https://github.com/RooCodeInc/Roo-Code/issues/10319
- Parser internals: `NativeToolCallParser` keeps `rawChunkTracker` (provider chunks by index) and `streamingToolCalls` (argument strings keyed by tool-call id), uses the **`partial-json`** library to render partial args live, **rejects tool calls without an `id`** ("XML tool calls are no longer supported"), requires "Every `tool_use` with an `id` MUST have a corresponding `tool_result`" (even rejected tools), and executes tools sequentially under a lock (`presentAssistantMessageLocked`); type coercion helpers `coerceOptionalNumber` / `coerceOptionalBoolean` plus `validateToolUse()` for mode/file permissions — [P] https://deepwiki.com/RooCodeInc/Roo-Code/6.2-native-tool-calling-protocol
- Kimi K2 Thinking (GGUF via llama.cpp / ik_llama.cpp, Roo 3.34.2): the model emitted `<|tool_calls_section_begin|>...<|tool_calls_section_end|>` token-format calls; Roo showed "wants to read this file" but never appended results, so the model looped re-issuing the same call — [P] https://github.com/RooCodeInc/Roo-Code/issues/9551

**Kilo Code (fork of Roo)**
- Supports both XML and native JSON; resolution order documented as "Model force > User preference > Model default > Model capability > XML fallback" — [S] https://deepwiki.com/Kilo-Org/kilocode/2.7-telemetry-and-analytics-system (page titled "Tool Protocol Resolution")
- JSON tool calling was added for the OpenRouter and Kilo providers, default-on for Claude Haiku 4.5, toggle at Provider Settings → Advanced → Tool Call Style → JSON — [S] https://blog.kilo.ai/p/this-week-in-kilo-code-cli-tracking
- Native-JSON bug: "missing the assistant message between tool calls" with Kimi k2-0905 on the Moonshot provider (v4.109.2, 2025-10-23; fix PR #3282); did not occur with XML tools — [P] https://github.com/Kilo-Org/kilocode/issues/3237
- Other reports: "Tool Protocol Settings failed to work" (xml selected but native sent) (#5090); `MODEL_NO_TOOLS_USED` with all local providers (#7004); local models fail to invoke tools (#927) — [S] https://github.com/Kilo-Org/kilocode/issues/5090 ; [S] https://github.com/Kilo-Org/kilocode/issues/7004 ; [S] https://github.com/Kilo-Org/kilocode/issues/927

**Continue**
- Tool support is auto-detected per provider/model (`toolSupport.ts`, `@continuedev/llm-info`); users can only *add* capabilities (`capabilities: [tool_use, image_input]`), never override autodetection; `capabilities: []` does not disable it — [P] https://docs.continue.dev/customize/deep-dives/model-capabilities
- "System message tools" (tools rendered into the system prompt, calls parsed from text) exist as an experimental, explicitly-configured feature for models without native tools; docs warn "most models are trained for native tools, so system message tools may not work as well" — [S] https://docs.continue.dev/ide-extensions/agent/model-setup
- Parser weakness: `acceptedToolCallStarts` includes `"tool_name:"` (case-insensitive, "to accommodate poor models") and "```tool\nTOOL_NAME:"; `splitAtCodeblocksAndNewLines()` evaluates chunks with `detectToolCallStart()` without context, so a model *describing* the syntax triggers a real call; closed "not planned" with the observation that without vocabulary-level structural tokens (Harmony-style) any text parser trades precision for recall — [P] https://github.com/continuedev/continue/issues/11070

**Crush (charmbracelet, Go, uses the `fantasy` AI library)**
- GLM 5.2 on Fireworks (Crush v0.76.0) emitted `tool_calls[].function.arguments` that failed the strict JSON parser on large `edit`/`write` payloads with newlines → "bad request: Invalid tool call in messages", killing the session; DeepSeek V4 Pro and Kimi K2.7 on the same provider were fine. Reporter proposed lenient parse + repair, treating malformed calls as recoverable, and feeding the parse error back to the model; closed via `charmbracelet/fantasy#289` — [P] https://github.com/charmbracelet/crush/issues/3153
- Other open-model tool issues: tool calls from local OpenAI-compatible providers "never executed" (#2936); API error on tool use with local models (#749); Ollama Qwen "doesn't support thinking" (#2623); Qwen3.5 hangs on "Working..." possibly because reasoning output lacks expected tags (#2367) — [S] https://github.com/charmbracelet/crush/issues/2936 ; [S] https://github.com/charmbracelet/crush/issues/749 ; [S] https://github.com/charmbracelet/crush/issues/2623 ; [S] https://github.com/charmbracelet/crush/issues/2367

**Goose (Block, Rust) — the "toolshim" pattern**
- Toolshim: tools are described in the system prompt; the primary model outputs JSON-ish intent; a second small "interpreter" model uses Ollama structured outputs to convert that text into valid tool-call JSON, which is then dispatched — [P] https://goose-docs.ai/blog/2025/04/11/finetuning-toolshim/
- Env vars `GOOSE_TOOLSHIM` and `GOOSE_TOOLSHIM_OLLAMA_MODEL`; docs gap acknowledged (which models need it, non-Ollama support) — [S] https://github.com/aaif-goose/goose/issues/8269 ; request for non-Ollama toolshim: [S] https://github.com/block/goose/issues/2313
- Goose's own benchmark: "toolshimmed models consistently score lower, suggesting the toolshims are not very effective at closing the gap in native tool support" — [S] https://block.github.io/goose/blog/2025/03/31/goose-benchmark/

**Model-native tool-call wire formats a harness may have to parse when the server does not**
- Kimi K2 / K2-Thinking: `<|tool_calls_section_begin|>` … `<|tool_call_begin|>` … `<|tool_call_argument_begin|>` … `<|tool_call_end|>` … `<|tool_calls_section_end|>`; the tool-call **id must be `functions.{name}:{global_index}`** (counter starts at 0 across the conversation) or the model/chat template crashes; loop until `finish_reason != "tool_calls"`; serve with `--tool-call-parser kimi_k2` — [P] https://huggingface.co/moonshotai/Kimi-K2-Thinking/blob/main/docs/tool_call_guidance.md
- Qwen3: Hermes-style `<tool_call>{"name": ..., "arguments": ...}</tool_call>`; vLLM flags `--enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser deepseek_r1`; tool results returned as `role: tool` with `tool_call_id` — [P] https://qwen.readthedocs.io/en/latest/framework/function_call.html
- Qwen3-Coder: serve with `--tool-call-parser qwen3_coder`; "try Q6_K or higher quantization when the model doesn't follow tool schemas correctly" — [S] https://unsloth.ai/docs/models/tutorials/qwen3-coder-how-to-run-locally
- GLM-4.7 / 5.x: `--tool-call-parser glm47 --reasoning-parser glm47`; on a 2.05-bpw GLM-5.3-Flash checkpoint the `glm45` parser (what the chat template auto-detects) "extracts **zero** tool calls" — [P] https://github.com/0xSero/GLM-5.3-Flash-DGX-Spark/pull/3 ; [S] https://huggingface.co/zai-org/GLM-4.7
- Qwen Code's system prompt shows tool-call *examples* in a per-model notation chosen by model-name match or `QWEN_CODE_TOOL_CALL_STYLE`: `qwen*-coder` → `<function=name><parameter=key>value</parameter></function>`; `qwen*-vl` → `{"name": "tool", "arguments": {...}}`; `gemma4` → `<|tool_call>call:name{key:value}<tool_call|>`; default → `[tool_call: name ...]` — [P] https://github.com/qwenlm/qwen-code/blob/main/packages/core/src/core/prompts.ts

**Codex CLI (for reference: a harness that hard-codes a model-specific edit tool)**
- `model_providers.<id>.wire_api` accepts only `"responses"` in the current reference; built-in `openai`, `ollama`, `lmstudio` providers cannot be overridden; `oss_provider = lmstudio | ollama` backs `--oss` — [P] https://learn.chatgpt.com/docs/config-file/config-reference
- Ollama recommends "a context window of at least 64k tokens for Codex"; example `model_providers` uses `base_url = "http://localhost:11434/v1/"`, `wire_api = "responses"` — [P] https://docs.ollama.com/integrations/codex

### Inferences
- The practical loop for an OpenAI-compatible harness like halo is: native `tools` always → on parse failure run a JSON repair pass (unterminated strings, missing braces, trailing junk/XML) → if still invalid, dispatch to an `invalid` pseudo-tool whose result text tells the model exactly what was wrong (schema errors listed) → distinguish `finish_reason == "length"` from malformed JSON and tell the model to split the operation. This is exactly the shape OpenCode issues #17750/#18108 and PR #23067 converge on.
- Schema tolerance beats prompt nagging: OpenCode's Kimi fix was to drop a required `description` field, not to add instructions. Keep required params minimal for open models and default the rest.
- Because Roo/Kilo dropped XML, a self-hosted stack must make the *server* parse tool calls (`--tool-call-parser`); when it does not (Roo #9551, Continue #11070), the harness sees token soup in `content`. A cheap fallback regex for `<tool_call>…</tool_call>` and Kimi's `<|tool_call_begin|>` blocks is worth having behind a flag, but Continue's experience shows text parsers false-positive on quoted syntax.
- Kimi's `functions.name:idx` id rule matters when a harness fabricates ids (e.g. Claude-format `toolu_*` ids translated to OpenAI) — a bridge should preserve provider ids verbatim and never renumber.

### Gaps
- Could not read the maintainer discussion on OpenCode #9878 (empty Kimi args) or the superseding PR #24289; whether OpenCode currently ships `jsonrepair` is unverified.
- Kilo's tool-protocol resolution order comes from a DeepWiki summary, not source.
- No harness publishes measured tool-call failure rates per model except the GLM DGX-Spark PR (see KQ7).

---

## KQ2. Edit tooling that works with open models

### Takeaway
Two families dominate: (1) tool-based `edit` with exact old/new string replace plus a `write` fallback (OpenCode, Crush, Qwen Code, Gemini CLI, Kimi Code, Claude Code), and (2) Aider's prompt-driven search/replace blocks with a per-model `edit_format` switch (`diff` for DeepSeek/Kimi, `diff-fenced` for Gemini, `whole` for weak models). Aider's leaderboard is the only public source of edit-format compliance numbers: 92-99% well-formed for DeepSeek V3/V3.2/R1 and Kimi K2 with `diff`, 71.6% for Qwen2.5-Coder-32B with `diff` vs 99.6% with `whole`.

### Cited Findings
- Aider edit formats: `whole` (full file; "slower and costlier"), `diff` (`<<<<<<< SEARCH / ======= / >>>>>>> REPLACE` blocks with the path outside the fence), `diff-fenced` (path inside the fence, "primarily used with the Gemini family of models, which often fail to conform to the fencing approach specified in the diff format"), `udiff` (simplified unified diff, "mainly used to the GPT-4 Turbo family ... because it reduced their lazy coding tendencies"), `editor-diff` / `editor-whole` (streamlined formats for the editor model in architect mode) — [P] https://aider.chat/docs/more/edit-formats.html
- Per-model settings schema (`model-settings.yml`): `edit_format`, `weak_model_name`, `use_repo_map`, `send_undo_reply`, `lazy`, `overeager`, `reminder` (user|sys), `examples_as_sys_msg`, `extra_params` (passed to litellm), `cache_control`, `caches_by_default`, `use_system_prompt`, `use_temperature` (bool or number), `streaming`, `editor_model_name`, `editor_edit_format`, `reasoning_tag` (e.g. `think`), `remove_reasoning`, `system_prompt_prefix`, `accepts_settings` (`thinking_tokens`, `reasoning_effort`) — [P] https://aider.chat/docs/config/adv-model-settings.html
- Verbatim entries from the live file — [P] https://raw.githubusercontent.com/Aider-AI/aider/main/aider/resources/model-settings.yml :
  ```yaml
  - name: deepseek/deepseek-chat
    edit_format: diff
    use_repo_map: true
    reminder: sys
    examples_as_sys_msg: true
    extra_params:
      max_tokens: 8192
    caches_by_default: true
  - name: deepseek/deepseek-reasoner
    edit_format: diff
    weak_model_name: deepseek/deepseek-chat
    use_repo_map: true
    examples_as_sys_msg: true
    extra_params:
      max_tokens: 64000
    caches_by_default: true
    use_temperature: false
    editor_model_name: deepseek/deepseek-chat
    editor_edit_format: editor-diff
  - name: openrouter/moonshotai/kimi-k2
    edit_format: diff
    use_repo_map: true
    examples_as_sys_msg: true
    extra_params:
      temperature: 0.6
  - name: openrouter/deepseek/deepseek-chat
    edit_format: diff
    use_repo_map: true
    reminder: sys
    examples_as_sys_msg: true
  ```
  Gemini entries use `edit_format: diff-fenced`, `use_temperature: false`, `accepts_settings: [thinking_tokens]`; DeepSeek-R1/QwQ entries use `reasoning_tag: think`; the file has 500+ entries, ~65% `diff`. **No entries exist for qwen3/qwen-3, glm/zai, kimi-k2-thinking, kimi-k2-0905, gemini-3, gpt-oss, minimax, deepseek-v3.1/v3.2** (same source).
- Unlisted models silently get `whole`, which "silently discards valid diff-style output (no error, no tracked changes)" — [S] https://github.com/Aider-AI/aider/issues/5486
- Edit-failure causes and mitigations: weaker models "more prone to disobeying the system prompt instructions"; above ~25k tokens of context "models become distracted and less likely to follow formatting instructions"; local Ollama defaults to small context windows that silently discard data; mitigations: add only files to be edited, `/drop`, `/clear`, `--edit-format whole`, `--architect` two-step editing "often produces more reliable edits" — [P] https://aider.chat/docs/troubleshooting/edit-errors.html
- Leaderboard metric definition: "percent using correct edit format" = share of tasks where the model complied with the edit format; on failure aider feeds back the error and asks for a fixed copy — [S] https://aider.chat/docs/leaderboards/edit.html
- Polyglot leaderboard rows (225 tests) — [P] https://raw.githubusercontent.com/Aider-AI/aider/main/aider/website/_data/polyglot_leaderboard.yml :

  | Model | edit_format | pass_rate_2 | % well-formed | error_outputs | date |
  |---|---|---|---|---|---|
  | DeepSeek Chat V3 (2024-12) | diff | 48.4 | 98.7 | 7 | 2024-12-25 |
  | DeepSeek R1 | diff | 56.9 | 96.9 | 8 | 2025-01-20 |
  | DeepSeek-V3.2-Exp (Reasoner) | diff | 74.2 | 97.3 | 8 | 2025-10-03 |
  | DeepSeek-V3.2-Exp (Chat) | diff | 70.2 | 98.2 | 6 | 2025-10-03 |
  | Kimi K2 | diff | 59.1 | 92.9 | 19 | 2025-07-17 |
  | Qwen3 235B A22B (no thinking) | diff | 59.6 | 92.9 | 22 | 2025-05-09 |
  | Qwen3 32B | diff | 40.0 | 83.6 | 119 | 2025-05-08 |
  | Qwen2.5-Coder-32B | diff | 8.0 | 71.6 | 158 | 2024-12-22 |
  | Qwen2.5-Coder-32B | whole | 16.4 | 99.6 | 1 | 2024-12-26 |
  | Gemini 2.5 Pro (06-05, 32k thinking) | diff-fenced | 83.1 | 99.6 | 1 | 2025-06-06 |
  | gemini-2.5-flash (05-20, 24k thinking) | diff | 55.1 | 95.6 | 15 | 2025-05-25 |
  | gemini-2.0-flash-thinking | diff | 18.2 | 77.8 | 182 | 2025-01-21 |

  Range across all entries: 71.6% (Qwen2.5-Coder `diff`) to 100% (several Gemini rows).
- oh-my-pi's alternative: **hash-anchored edits** — "the model points at anchors instead of retyping the lines it wants to change, so whitespace battles and string-not-found loops just stop happening"; claimed 61% fewer output tokens with Grok 4 Fast — [P] https://github.com/can1357/oh-my-pi
- Codex: GPT models are trained on the `apply_patch` V4A grammar (`*** Begin Patch` / `*** Update File: path` / `*** End Patch`); the freeform grammar tool "swaps out write/edit while those models are active" and "other compatible models fall back to a function tool with an input string"; enabled by default in PR #21687 — [S] https://codex.danielvaughan.com/2026/03/31/codex-cli-apply-patch-v4a-diff-format/ ; [S] https://deepwiki.com/openai/codex/5.4-apply-patch-system ; [S] https://github.com/openai/codex/pull/21687
- Read-before-edit and tool-over-shell rules are in Qwen Code's prompt: "Read relevant code, imports, tests, configuration before making changes"; "Read files with READ_FILE, not cat/head/tail"; "Edit with EDIT, not sed/awk"; "Create with WRITE_FILE, not heredoc"; "Prefer editing existing files over creating new ones" — [P] https://github.com/qwenlm/qwen-code/blob/main/packages/core/src/core/prompts.ts
- Crush GLM 5.2 crash (KQ1) was specifically on `edit`/`write` calls carrying multi-line code blobs, i.e. large string arguments are where JSON-argument serialization breaks first — [P] https://github.com/charmbracelet/crush/issues/3153

### Inferences
- For halo reusing Claude Code's `Edit` (exact old_string/new_string) with DeepSeek/Kimi/Qwen: keep it, but add Aider-style whitespace-tolerant matching on the harness side and a structured "old_string not found / N near-matches" error so the model can self-correct in one turn; open models' failures are dominated by whitespace/indentation drift, which is exactly what `whole` and hash-anchoring sidestep.
- Aider's data suggests the switch point: models scoring <85% well-formed on `diff` (Qwen3-32B-class and below) should get a `whole`-file or full-`write` fallback; DeepSeek V3.x, Kimi K2 and Qwen3-235B are fine with search/replace.
- Very large single edits are the riskiest tool call for GLM-class models (JSON escaping); prefer several small edits or a `write` with raw content.

### Gaps
- No harness besides Aider reports per-model edit success rates; OpenCode/Crush/Qwen Code do not publish edit-tool failure telemetry.
- Aider has no tuned settings for Qwen3-Coder, GLM-4.x, Kimi K2-Thinking or Gemini 3 as of the fetch (defaults apply).

---

## KQ3. System prompt patterns (length, sections, model-specific variants, leakage control)

### Takeaway
Open-model harnesses keep one large Claude-Code-style prompt (Qwen Code's renders to roughly 4.5-5.5k words) but make it *composable*: per-model tool-call notation (Qwen Code), per-family prompt variants and a "compact" ~10%-size prompt for local models (Cline), full override files (`GEMINI_SYSTEM_MD`, `QWEN_SYSTEM_MD`, `model_instructions_file`), and explicit "tools for actions, text only for communication" rules to stop scaffold echo.

### Cited Findings
- Qwen Code prompt structure (`packages/core/src/core/prompts.ts`): identity ("You are Qwen Code ... developed by Alibaba Group"); Core Mandates (never assume file contents; follow conventions; comments "default to none"; treat unexpected changes as user-owned); Primary Workflow "Plan → Implement → Adapt → Verify → Report" with "Before reporting a task complete, verify it actually works"; Tool Usage rules (dedicated tools over shell; absolute paths; batch independent calls); Interaction modes (Headless: "Never ask user question... Make reasonable assumptions... report blocker"; Interactive/ACP: use ASK_USER_QUESTION); Plan Mode reminder ("MUST NOT make any edits... This supersedes any other instructions"); Tone ("Professional and direct; omit chitchat"; "Tools for actions, text output ONLY for communication"; "No explanatory comments within tool calls"); Git section (`git status`, `git diff HEAD`, `git log -n 3` before commits; never push without explicit request); Sandbox sections (bwrap / macOS Seatbelt / none); Denied-tool rule ("Do not try to complete denied action through another tool, shell indirection, generated script, alias, symlink"); Risky-action confirmation ("Measure twice, cut once"). Estimated rendered size ~4,500-5,500 words (18-22k chars) varying by model/mode. **No language pinning; no `<thinking>` instruction**. A `CodeModeOnly` branch exposes a single `exec` JavaScript tool with `tools.<name>` calls instead of individual tools. Overrides: `QWEN_SYSTEM_MD` (full replacement, no output style applied), `QWEN_SYSTEM_IDENTITY_MD` (distributor identity), `QWEN_WRITE_SYSTEM_MD` (dump base prompt) — [P] https://github.com/qwenlm/qwen-code/blob/main/packages/core/src/core/prompts.ts
- Qwen Code PR #12360 "Simplify system prompts and remove conflicting examples" — [S] https://github.com/QwenLM/qwen-code/pull/12360
- Qwen3-Coder was released with Qwen Code "adapted with customized prompts and function calling protocols to fully unleash the capabilities of Qwen3-Coder on agentic coding tasks" — [S] https://qwenlm.github.io/blog/qwen3-coder/
- Gemini CLI: `GEMINI_SYSTEM_MD=true|1|<path>` replaces the built-in prompt entirely ("full replacement, not a merge"), shows a `|⌐■_■|` indicator; `GEMINI_WRITE_SYSTEM_MD=1|<path>` dumps the built-in prompt; custom files may use `${AgentSkills}`, `${SubAgents}`, `${AvailableTools}`, `${<tool>_ToolName}` — [P] https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/system-prompt.md
- Cline "compact system prompt": "roughly 10% the length of the full system prompt", disables MCP tools, Focus Chain and MTP; recommended stack Qwen3 Coder 30B A3B Instruct, 262,144-token context, 4-bit quant, KV-cache quantization off (post dated 2025-08-28) — [P] https://cline.bot/blog/local-models ; compact prompt is offered for Ollama/LM Studio but not the generic OpenAI-compatible provider — [S] https://github.com/cline/cline/discussions/7466 ; Roo users requested the same "compact prompt" option (#7550) — [S] https://github.com/RooCodeInc/Roo-Code/issues/7550
- Crush: users asked to customize the coder base prompt because "system prompt changes negatively impact GLM-4.5 and Qwen3-Coder model performance" (#1136) — [S] https://github.com/charmbracelet/crush/issues/1136
- Codex: `model_instructions_file` overrides built-in instructions; `project_doc_max_bytes` caps AGENTS.md; `project_doc_fallback_filenames` adds other doc names — [P] https://learn.chatgpt.com/docs/config-file/config-reference
- Qwen Code headless adds `--system-prompt` (replace) and `--append-system-prompt` (layer) flags — [P] https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/
- Aider strips `<think>` leakage via `reasoning_tag: think` / `remove_reasoning` per model — [P] https://aider.chat/docs/config/adv-model-settings.html
- Cross-harness study (11 systems incl. Claude Code, Codex CLI, Gemini CLI, Aider, OpenCode, Pi, Hermes, OpenHands, Mini-SWE-Agent, Mistral Vibe, OpenClaw; ~4M LOC; arXiv 2609.00006, submitted 2026-07-15): 29 recurring design patterns; skills adopted by 9/11 and MCP by 8/11; no runtime imported a general agent framework — all "hand-rolled async loops"; behavioral policy "migrating from prompt-based approaches to configuration-driven systems" — [P] https://arxiv.org/abs/2609.00006

### Inferences
- The two prompt techniques that most directly help open models are (1) the "tools for actions, text only for communication" rule (curbs echoing tool JSON in prose) and (2) shrinking the prompt for small-context local models (Cline's 10% compact prompt). Neither Qwen Code nor Gemini CLI pins output language or instructs `<think>` usage; leakage is handled by the API layer (`reasoning_content`/parsers) not the prompt.
- Qwen Code's per-model *example notation* (not per-model protocol) is the cheapest form of model-specific prompting: keep one prompt, swap the few-shot tool examples to match what the model was trained on.

### Gaps
- Cline's system-prompt source tree (`src/core/prompts/system-prompt`) returned 404 during research, so the exact family/variant list and token counts are unverified.
- Kimi Code's system prompt text was not obtainable (docs only).
- No harness publishes measured system-prompt token counts per variant except Cline's "~10%" claim.

---

## KQ4. Reasoning handling (`reasoning_content` / `reasoning_details` / thought signatures)

### Takeaway
Three model families now *require* the harness to echo reasoning back on tool-call turns — DeepSeek V3.2+/V4 (`reasoning_content`, 400 if missing when `tools` present), Kimi K2.x thinking/K3 (`reasoning_content`, "keep all"), Gemini 3 (`thought_signature`, 400 in strict function calling) — and OpenRouter unifies them under `reasoning_details[]` that must be passed back "unmodified". Harnesses that dropped reasoning on serialization (OpenCode, Roo, oh-my-pi, n8n, SillyTavern) all hit 400s; Cline's fix is to attach reasoning only to assistant messages after the last user message.

### Cited Findings
- DeepSeek thinking-mode rules: thinking is on by default (`effort: high`), or `extra_body={"thinking": {"type": "enabled"}}`; "If the request carries the `tools` parameter, the `reasoning_content` of all previous turns should be passed back to the API and will be concatenated into the context"; without `tools` it "does not need to be passed back; even if passed... it will be ignored"; thinking mode "does not support the `temperature`, `presence_penalty`, or `frequency_penalty` parameters"; `top_p` outside 0.95-1.0 is treated as 0.95; append `response.choices[0].message` (content + reasoning_content + tool_calls) as-is — [P] https://api-docs.deepseek.com/guides/thinking_mode/
- DeepSeek-V3.2 was "the first model to integrate thinking directly into tool-use" — [S] https://api-docs.deepseek.com/news/news251201/
- Kimi platform: for thinking models "Do not set `temperature`"; "keep all of the reasoning content from the context (the `reasoning_content` field) and send it back with the request" (mandatory for kimi-k3 and kimi-k2.7-code; kimi-k2.6 needs `thinking.keep: "all"`, default `null`); "Set `max_tokens >= 16000`"; thinking cannot be disabled on k3/k2.7-code, `thinking.type: "disabled"` on k2.6 — [P] https://platform.kimi.ai/docs/guide/use-kimi-k2-thinking-model
- OpenRouter `reasoning_details[]`: `type` ∈ `reasoning.summary` | `reasoning.encrypted` (base64 `data`) | `reasoning.text` (`text`, optional `signature`), plus `id`, `format`, `index`; formats `anthropic-claude-v1`, `openai-responses-v1`, `google-gemini-v1`, `xai-responses-v1`, `meta-responses-v1`, `bedrock-*`, `azure-openai-responses-v1`, `unknown`; rule: "the entire sequence of consecutive reasoning blocks must match the outputs generated by the model during the original request; you cannot rearrange or modify the sequence"; request knob `reasoning: {effort: max|xhigh|high|medium|low|minimal|none, max_tokens, exclude, enabled, context: auto|all_turns|current_turn, mode}`; Gemini 3 maps `effort` → `thinkingLevel`; streaming puts `reasoning_details` in `choices[].delta` — [P] https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
- Gemini 3: thought signatures are "encrypted representations of the model's internal thought process"; with manual function calling "you must return the signature exactly as Gemini provided... missing signatures will result in a 400 error" in strict current-turn validation; official SDK chat history handles it automatically — [S] https://ai.google.dev/gemini-api/docs/gemini-3
- Harness failures: OpenCode drops `reasoning_content` when serializing assistant messages → 400 on deepseek-v4-pro/flash from turn 2 (opened 2026-04-28, closed not planned) — [P] https://github.com/anomalyco/opencode/issues/24722 ; same class in oh-my-pi #1484 (Kimi K2.6 & DeepSeek v4) — [S] https://github.com/can1357/oh-my-pi/issues/1484 ; Vercel AI SDK #10778, SillyTavern #4857, n8n #22579 — [S] https://github.com/vercel/ai/issues/10778 ; [S] https://github.com/SillyTavern/SillyTavern/issues/4857 ; [S] https://github.com/n8n-io/n8n/issues/22579
- Cline's `addReasoningContent()` (r1-format.ts): during multi-step tool calls in the *same* turn, thinking blocks are attached as `reasoning_content` to assistant messages after the last user message; for earlier turns reasoning is omitted "to conserve bandwidth" — [P] https://github.com/cline/cline/pull/7888
- Roo: "Reasoning tokens are now stored in conversation history across all providers and included in cost reporting" (v3.33) — [P] https://roocodeinc.github.io/Roo-Code/update-notes/v3.33 ; Gemini 3.1 Pro via OpenRouter broke on turn 2 because `tool_use` blocks were stripped to `content: []` while the tool result still referenced the id, plus `reasoning.text` items mislabeled as `reasoning.encrypted` (regression in v3.50.0) — [P] https://github.com/RooCodeInc/Roo-Code/issues/11629
- Gemini-3-via-OpenRouter "missing thought_signature" failures were also logged by Cherry Studio, LobeHub, claude-code-router (#1024), gptel (#1190), codecompanion.nvim; inspect_ai's fix: replay `reasoning_details` structurally per family, dropping unsigned `reasoning.text` for signed formats (google-gemini-v1, anthropic-claude-v1) but keeping the encrypted continuity blob — [S] https://github.com/UKGovernmentBEIS/inspect_ai/pull/4979 ; [S] https://github.com/musistudio/claude-code-router/issues/1024 ; [S] https://github.com/karthink/gptel/issues/1190
- Display/truncation: Kimi Code print mode writes tool_calls/tool messages to JSONL but "Thinking content is not written to JSONL" — [S] https://moonshotai.github.io/kimi-cli/en/customization/print-mode.html ; OpenRouter `reasoning.exclude: true` hides reasoning while still billing it — [P] https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
- Kimi K2-Thinking HF guide: reasoning must be passed back and the tool-call id format must be preserved across turns or the template crashes — [P] https://huggingface.co/moonshotai/Kimi-K2-Thinking/blob/main/docs/tool_call_guidance.md

### Inferences
- A bridge that translates Claude-format history (thinking blocks + tool_use) into OpenAI-format must carry an opaque per-assistant-message `reasoning_content` (direct DeepSeek/Kimi) or `reasoning_details[]` (OpenRouter) field and replay it byte-for-byte on every subsequent request within the tool loop. Cline's "only since the last user message" trimming is the safe minimum for DeepSeek and matches its docs (earlier reasoning is ignored when no tools; but note DeepSeek says "all previous turns" when `tools` is present — Cline's trimming may be relying on tolerance; treat "keep all" as the conservative default for Kimi K3/K2.7).
- Never send `temperature` to Kimi thinking or DeepSeek thinking endpoints; OpenCode's hard-coded title-agent temperature produced `HTTP 400 invalid temperature: only 1 is allowed for this model` on Moonshot (see KQ7).
- History compaction must treat reasoning + tool_use + tool_result of a turn as an atomic unit (Roo #11629 shows what happens when one part is stripped).

### Gaps
- Could not fetch OpenRouter's tool-calling doc page (404) to confirm its `parallel_tool_calls`/interleaved-thinking notes.
- Whether DeepSeek actually rejects requests that include reasoning only for the current turn (Cline's approach) is not documented; Cline's PR reports it works.

---

## KQ5. Context management (compaction, tool-result truncation, token counting, cache stability)

### Takeaway
All harnesses use LLM-written structured summaries triggered by a threshold, but they differ in *what they protect*: OpenCode adds tool-output pruning behind a 40k-token protection window and 2,000-char tool-output excerpts; Goose compacts at 80% and summarizes old tool outputs "in the background"; Gemini CLI compresses at 70%; Roo defaults to 100% (i.e. only on overflow); Qwen Code caps every JSON-emitted tool result at 65,536 bytes. Prompt-cache stability is not documented by any open-model harness.

### Cited Findings
- OpenCode v2 compaction: trigger `estimated tokens >= min(input_limit - buffer, context_limit - max(output_reserve, buffer))`; defaults `compaction.buffer = 20000`, `compaction.keep.tokens = 15000`, output reserve capped at 32,000; example 128k input → ceiling 108k; the checkpoint prompt demands headings (objective/requirements, decisions, completed & active work, blockers & next moves, relevant files, additional context) and a response lacking a heading like `## Objective` gets one corrective retry then aborts; recent tool results survive as records with "tool output (limited to 2,000 characters)"; summaries appear as historical conversation, not instructions; keys `compaction.auto` (default true) and `compaction.prune` — [P] https://opencode.ai/v2/docs/compaction ; opencode.json also documents `"compaction": {"auto": true, "prune": false}` — [P] https://opencode.ai/docs/config/
- Independent comparison (2025-12-02): Claude Code compacts at ~95% capacity with an LLM summary of the whole history and `/compact [instructions]`; Codex CLI uses absolute token limits (180k-244k by model) with a 95% margin and keeps ~20k tokens of recent user messages; OpenCode triggers on `(context_limit - output_limit)` overflow and separately **prunes old tool outputs beyond a 40k-token protection window when >20k tokens are prunable**, keeping a detailed summary plus a 2-sentence UI summary; Amp is manual-only ("handoff"); "Quality can degrade with multiple compactions" — [P] https://gist.github.com/badlogic/cd2ef65b0697c4dbe2d13fbecb0a0a5f
- Goose: auto-compaction at 80% (`GOOSE_AUTO_COMPACT_THRESHOLD`, `0.0` disables); overflow strategies via `GOOSE_CONTEXT_STRATEGY` = `summarize` | `truncate` (CLI) | `clear` (CLI) | `prompt` (interactive default); it "summarizes older tool call outputs in the background while keeping recent calls in full detail"; `/compact` (was `/summarize`) — [P] https://goose-docs.ai/docs/guides/sessions/smart-context-management/ ; context limit defaults to 128k for unknown custom models (GLM-5.2, MiniMax-M3) — [S] https://github.com/aaif-goose/goose/issues/10058
- Gemini CLI: `chatCompression.contextPercentageThreshold` default 0.7 (applies to auto and `/compress`), later surfaced as editable `model.compressionThreshold` — [S] https://google-gemini.github.io/gemini-cli/docs/get-started/configuration.html ; [S] https://github.com/google-gemini/gemini-cli/pull/12317
- Cline Auto Compact: summarization replaced older truncation; engages at ≥80% with user-configurable threshold (`autoCondenseThreshold` 0-1); "leverages your existing prompt cache ... costs about the same as any other tool call"; the threshold scales with the window so 1M-context models "never compact in practice" (#14329) — [S] https://docs.cline.bot/features/auto-compact ; [S] https://github.com/cline/cline/issues/6439 ; [S] https://github.com/cline/cline/issues/14329
- Roo Intelligent Context Condensing default threshold 100% (slider), per-profile thresholds; default model max_tokens clamped to 20% of context window (PR #6761) — [S] https://docs.roocode.com/features/intelligent-context-condensing ; [S] https://github.com/RooCodeInc/Roo-Code/pull/6761
- Qwen Code: JSON/stream-json `tool_result.content` bounded to 65,536 UTF-8 bytes with deterministic head/tail previews (applies to sessions, SDK transports, subagents; not plain-text mode); `PreCompact`/`PostCompact` hook events exist — [P] https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/ ; [P] https://qwenlm.github.io/qwen-code-docs/en/users/features/hooks/
- Aider: context above ~25k tokens degrades format compliance; `/tokens`, `/drop`, `/clear` as manual controls; repo map on/off per model (`use_repo_map`) — [P] https://aider.chat/docs/troubleshooting/edit-errors.html ; [P] https://aider.chat/docs/config/adv-model-settings.html
- Codex: `model_context_window` override, `history.max_bytes` cap; Ollama recommends ≥64k context — [P] https://learn.chatgpt.com/docs/config-file/config-reference ; [P] https://docs.ollama.com/integrations/codex
- Crush per-model `context-window` and `default-max-tokens` are declared in config (e.g. `--context-window 64000 --default-max-tokens 5000` for deepseek-chat) — [P] https://github.com/charmbracelet/crush

### Inferences
- Token counting for open models is done by *estimation* (OpenCode "estimates the final size"), not tokenizer calls; every harness therefore keeps a safety buffer (OpenCode 20k) — halo can do the same with a chars/4 estimate plus provider `usage` from the previous response.
- The two cheapest wins for DeepSeek/Kimi-class context limits (64k-256k) are OpenCode-style tool-output pruning (drop bodies of old tool results, keep a stub) and Qwen-style hard byte caps on tool results at emission time; both preserve the prefix (system prompt + early turns), which is what provider prompt caching (DeepSeek `caches_by_default`, Kimi) needs even though no harness states this explicitly.

### Gaps
- No harness documents prompt-cache-aware ordering (stable prefix) for open providers; Aider merely flags `caches_by_default: true` for DeepSeek.
- Roo/Kilo condensing prompt text and Cline's summary prompt were not fetched.

---

## KQ6. Feature parity map vs Claude Code (transition checklist)

### Takeaway
Claude Code's *config surface* has become a de-facto standard: OpenCode falls back to `CLAUDE.md` and `~/.claude/CLAUDE.md`, oh-my-pi reads `.claude/` wholesale, Qwen Code accepts Claude hook aliases (`Bash`, `Write`, `Read`) unchanged, and Gemini CLI/Codex/Crush all read `AGENTS.md`. Headless JSON (`-p --output-format json|stream-json`), hooks, subagents, MCP JSON, slash commands and session resume exist in Qwen Code and Gemini CLI with near-identical flags; Crush/OpenCode diverge in config format (Bash `.crushrc`, `opencode.json`).

### Cited Findings (by feature)

**Instruction files**
- OpenCode: reads `AGENTS.md` then `CLAUDE.md` walking up from cwd, then `~/.config/opencode/AGENTS.md`, then `~/.claude/CLAUDE.md` (unless disabled by env); `instructions: [globs, remote URLs (5s timeout)]` in opencode.json; first match per category wins — [P] https://opencode.ai/docs/rules/ ; [P] https://opencode.ai/docs/config/
- Crush: `~/.config/crush/CRUSH.md` and `~/.config/AGENTS.md`, `option global-context-path` — [P] https://github.com/charmbracelet/crush
- Gemini CLI: `GEMINI.md` (`~/.gemini/GEMINI.md`, project); projects commonly symlink one `AGENTS.md` to `CLAUDE.md`/`GEMINI.md` — [S] https://geminicli.com/docs/cli/gemini-md/ ; [S] https://github.com/google-gemini/gemini-cli/discussions/1471
- Qwen Code: `QWEN.md` context layer read once per session, reloaded on refresh — [P] https://github.com/qwenlm/qwen-code/blob/main/packages/core/src/core/prompts.ts
- Codex: `AGENTS.md` with `project_doc_max_bytes` and `project_doc_fallback_filenames` — [P] https://learn.chatgpt.com/docs/config-file/config-reference
- oh-my-pi: auto-inherits `.claude/`, `.cursor/`, `.windsurf/`, `.cline/`, `.codex/`, `.gemini/`, `.github/copilot/`, `AGENTS.md` ("No migration script"), settings in `~/.omp/agent/config.yml` and `models.yml` — [P] https://github.com/can1357/oh-my-pi
- Kimi Code: `AGENTS.md` present in repo; docs list skills/plugins — [P] https://github.com/MoonshotAI/kimi-code ; [S] https://github.com/MoonshotAI/kimi-cli/blob/main/AGENTS.md

**Slash / custom commands**
- OpenCode: `command.<name>.{template, description, agent}` in opencode.json (also `.opencode/command/*.md` per docs summary) — [P] https://opencode.ai/docs/config/
- Gemini CLI: TOML files `~/.gemini/commands/test.toml` → `/test`, `<project>/.gemini/commands/git/commit.toml` → `/git:commit`, `/commands reload` — [S] https://geminicli.com/docs/cli/custom-commands/
- Qwen Code: commands docs page exists — [S] https://qwenlm.github.io/qwen-code-docs/en/users/features/commands/
- Kimi Code: `/login`, `/mcp-config`, `/provider` — [P] https://github.com/MoonshotAI/kimi-code ; [P] https://moonshotai.github.io/kimi-code/en/configuration/providers.html

**MCP config format**
- OpenCode: `mcp.<name>: {type: "local"|"remote", command, args, environment, url, enabled}` — [P] https://opencode.ai/docs/config/
- Crush: `mcp add <name> --type stdio|http|sse --url ... --header ...` in `.crushrc` — [P] https://github.com/charmbracelet/crush
- Codex: `mcp_servers.<id>: {command, args, env, url, bearer_token_env_var, enabled}` (TOML) — [P] https://learn.chatgpt.com/docs/config-file/config-reference
- Kimi Code: `kimi mcp` subcommands and conversational `/mcp-config` — [P] https://github.com/MoonshotAI/kimi-code
- Gemini CLI hooks name MCP tools `mcp_<server>_<tool>` — [S] https://geminicli.com/docs/hooks/reference/

**Hooks**
- Qwen Code: 24+ events (PreToolUse, PostToolUse, PostToolUseFailure, PostToolBatch, SessionStart/End/Delete, UserPromptSubmit, UserPromptExpansion, SubagentStart/Stop, Stop, StopFailure, MessageDisplay, PreCompact, PostCompact, Notification, PermissionRequest, PermissionDenied, TodoCreated/Completed, InstructionsLoaded); types command/http/function/prompt; settings.json (project → user → system → extension); matcher regex with Claude Code display-name aliases; output `{continue, decision: allow|deny|block|ask, reason, hookSpecificOutput.additionalContext}`; exit code 2 = blocking error — [P] https://qwenlm.github.io/qwen-code-docs/en/users/features/hooks/
- Gemini CLI: `hooks` object in settings.json; `BeforeTool`/`AfterTool` (regex matchers on tool name), `SessionStart`/`BeforeAgent` (exact-string matchers) — [S] https://geminicli.com/docs/hooks/reference/
- Codex: inline `hooks` table mirroring `hooks.json`; `notify` command with JSON payload; admins can set `allow_managed_hooks_only` — [P] https://learn.chatgpt.com/docs/config-file/config-reference
- Kimi Code: "Lifecycle hooks for gating tool calls and triggering automation" — [P] https://github.com/MoonshotAI/kimi-code
- Crush: `hook` builtin in `.crushrc`; `PreToolUse` hooks in crush.json — [S] https://github.com/dyoshikawa/rulesync/issues/3016
- OpenCode: `plugin: [...]` (npm plugins) rather than shell hooks — [P] https://opencode.ai/docs/config/

**Sub-agents**
- OpenCode: `agent.<name>: {description, model, prompt, tools, permission}`, `default_agent`, `subagent_depth` — [P] https://opencode.ai/docs/config/
- Gemini CLI: `.gemini/agents/*.md` with explicit tools list; policy engine treats subagents as virtual tool names — [S] https://geminicli.com/docs/core/subagents/
- Qwen Code: fork inherits full context, background by default, `run_in_background: false` to block — [S] https://qwenlm.github.io/qwen-code-docs/en/users/features/sub-agents/
- Kimi Code: built-in `coder`, `explore`, `plan` subagents — [P] https://github.com/MoonshotAI/kimi-code

**Plan mode / approval modes / permissions**
- Qwen Code: `--approval-mode plan|default|auto-edit|auto|yolo`, `--yolo`, `--safe-mode` (disables context files, hooks, skills, MCP); plan-mode system reminder enforces read-only — [P] https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/ ; [P] https://github.com/qwenlm/qwen-code/blob/main/packages/core/src/core/prompts.ts
- OpenCode: `permission: {"*": "ask", bash: {"*": "ask", "rm -rf *": "deny"}, edit: "allow"}`; plan agent as `default_agent` — [P] https://opencode.ai/docs/config/
- Codex: `approval_policy = on-request | never | {granular}`; `sandbox_mode = read-only | workspace-write | danger-full-access` — [P] https://learn.chatgpt.com/docs/config-file/config-reference
- Crush: `permissions allow view ls grep edit ...`, `permissions deny bash ...`, `--yolo` — [P] https://github.com/charmbracelet/crush
- Cline v3.35 added an Auto-Approve menu — [P] https://cline.bot/blog/cline-v3-35

**Session resume**
- Qwen Code: `--continue`, `--resume <sessionId>` (re-pass `--json-schema`) — [P] https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/
- oh-my-pi: sessions persist; `/fresh` resets provider state — [P] https://github.com/can1357/oh-my-pi
- Codex: `history.persistence = save-all|none`, `history.max_bytes` — [P] https://learn.chatgpt.com/docs/config-file/config-reference

**Headless / print mode with JSON**
- Gemini CLI: `-p`, `--output-format text|json|stream-json`; JSON `{response, stats, error}`; stream-json JSONL events `init`, `message`, `tool_use`, `tool_result`, `error`, `result`; exit codes 0/1/42 (input)/53 (turn limit) — [P] https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/headless.md
- Qwen Code: `-p`, `--output-format text|json|stream-json`, `--input-format stream-json`, `--include-partial-messages`, `--json-schema`, `--max-session-turns` (exit 53), `--max-wall-time` / `--max-tool-calls` (exit 55), `QWEN_CODE_UNATTENDED_RETRY=1`; messages carry `type` (system|assistant|result), `subtype` (session_start|success|goal_state), `session_id`, `uuid` — [P] https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/
- Kimi Code: `kimi --print --output-format stream-json "..."` emits NDJSON Assistant/Tool messages; thinking omitted — [S] https://moonshotai.github.io/kimi-cli/en/customization/print-mode.html
- OpenCode: `opencode run` (JSON format available per docs summary) — [S] https://opencode.ai/docs/config/ (not independently verified)
- Crush: `crush run`, `-q`, `-y` — [P] https://github.com/charmbracelet/crush
- oh-my-pi: `omp -p` one-shot; `omp --mode rpc` NDJSON over stdio; SDK `@oh-my-pi/pi-coding-agent` — [P] https://github.com/can1357/oh-my-pi
- Codex: `codex exec --oss --local-provider ollama -m gpt-oss:20b "..."` — [S] https://docs.ollama.com/integrations/codex

**TUI / config format / keybindings**
- OpenCode: TUI keybinds file (`"keybinds": {"command_list": "ctrl+p"}`), config precedence remote → global → `OPENCODE_CONFIG` → project → `.opencode` → inline → managed → MDM; `{env:VAR}` / `{file:path}` substitution — [P] https://opencode.ai/docs/config/
- Crush: v0.88.0 (2026-07-31) replaced JSON with Bash `.crushrc` (builtins `provider`, `model`, `mcp`, `lsp`, `permissions`, `hook`, `option`); `crush.json` deprecated but merged (crushrc wins in same dir) — [S] https://newreleases.io/project/github/charmbracelet/crush/release/v0.88.0 ; [S] https://github.com/Chemaclass/agnostic-ai/issues/674 ; [P] https://github.com/charmbracelet/crush
- Kimi Code: TOML `[providers.<name>] type = kimi|anthropic|openai|openai_responses|google-genai|vertexai`, `base_url`, `api_key` **or** `api_key_env` (exactly one); `[models.<alias>] provider, model, max_context_size`; `/provider` TUI manager; TypeScript, Node ≥24.15, MIT, ACP for Zed/JetBrains — [P] https://moonshotai.github.io/kimi-code/en/configuration/providers.html ; [P] https://github.com/MoonshotAI/kimi-code
- Codex: TOML `~/.codex/config.toml`, profiles — [P] https://learn.chatgpt.com/docs/config-file/config-reference
- Cross-harness: skills 9/11, MCP 8/11 of studied systems — [P] https://arxiv.org/abs/2609.00006

### Transition checklist (Claude Code → open-model harness), derived from the above
| Claude Code feature | Closest open-harness equivalent | Notes for halo |
|---|---|---|
| `CLAUDE.md` (+ `~/.claude/CLAUDE.md`) | OpenCode reads both as fallback; oh-my-pi reads `.claude/`; others use `AGENTS.md`/`GEMINI.md`/`QWEN.md`/`CRUSH.md` | Read `CLAUDE.md` first, then `AGENTS.md`; support `instructions` globs |
| `.claude/commands/*.md` | OpenCode `command.*`/`.opencode/command`, Gemini TOML commands, Qwen commands | Keep Markdown-with-frontmatter format |
| `.mcp.json` / `mcpServers` | OpenCode `mcp` (local/remote), Codex `mcp_servers` TOML, Crush `mcp add` | Accept Claude's `mcpServers` JSON verbatim |
| `settings.json` hooks | Qwen Code (aliases `Bash`/`Write`/`Read` accepted), Gemini `BeforeTool/AfterTool`, Codex `hooks` | Qwen's contract (`decision`, exit 2) is a drop-in |
| Sub-agents (`.claude/agents`) | OpenCode `agent.*`, Gemini `.gemini/agents/*.md`, Qwen forks, Kimi `coder/explore/plan` | Add `subagent_depth`-style cap |
| Plan mode | Qwen `--approval-mode plan` + prompt reminder; OpenCode plan agent | Reuse Qwen's "supersedes any other instructions" reminder text |
| Permissions / `--dangerously-skip-permissions` | OpenCode `permission` map, Codex `approval_policy`+`sandbox_mode`, Crush `--yolo`, Qwen `--yolo` | Bash glob deny-list like OpenCode `"rm -rf *": "deny"` |
| `--resume` / `--continue` | Qwen identical flags; Codex `history.*` | — |
| `-p --output-format json/stream-json` | Qwen (superset incl. budgets/exit codes), Gemini, Kimi `--print` | Copy Qwen's event schema; cap tool results at 64 KiB |
| Memory (`# note`) | Qwen "user memory" deprecated in favor of QWEN.md; none elsewhere | Persist to CLAUDE.md-style file |
| Auto-compact | OpenCode (buffer/keep/prune), Goose 80%, Gemini 70%, Roo 100% | Add tool-output pruning before summarizing |
| Skills | 9/11 harnesses per arXiv study; Kimi/Gemini/Qwen have skills dirs | Keep `.claude/skills` |
| Keyboard shortcuts (Ink) | OpenCode keybinds JSON; Crush Bubble Tea; Kimi TUI | No cross-harness convention found |

### Inferences
- Reusing Claude Code's files is a *supported migration path* in at least three harnesses (OpenCode, Qwen Code hooks, oh-my-pi), so halo's plan to reuse Claude Code config is in line with the ecosystem rather than an outlier.
- Qwen Code is the closest structural clone (flags, hook contract, event schema) and doubles as a reference implementation for open-model prompt/tool-notation tweaks.

### Gaps
- OpenCode's exact headless JSON schema and `.opencode/command` file format were not fetched.
- Keyboard-shortcut conventions were not researched beyond OpenCode's keybinds file.
- Kimi Code config file locations (`~/.kimi-code/...`) and session-resume flags are only partially documented in fetched pages.

---

## KQ7. Best-practice defaults per model family (temperature, max_tokens, tool_choice, parallel tools, provider pinning)

### Takeaway
Vendor guidance diverges sharply by family: DeepSeek and Kimi thinking endpoints *reject or ignore* `temperature`; Qwen3-Coder wants 0.7/0.8/20/1.05; GLM-4.7 ships 1.0/0.95 but measured tool-call corruption drops from 4.2% to 0.8% at 0.6; Gemini 3 must stay at 1.0. Harness defaults (OpenCode 0 / 0.55-for-Qwen; Aider 0.6 for Kimi K2, `use_temperature: false` for reasoners/Gemini) show the same split. On OpenRouter, pin with `provider.order` + `allow_fallbacks: false` (+ `require_parameters: true` when sending `tools`).

### Cited Findings

**DeepSeek (V3.2 / V4 thinking)**
- Thinking mode ignores `temperature`, `presence_penalty`, `frequency_penalty`; `top_p` clamped to 0.95-1.0; `reasoning_content` passback required when `tools` present; default effort `high` — [P] https://api-docs.deepseek.com/guides/thinking_mode/
- Aider: `deepseek-chat` `max_tokens: 8192`, `deepseek-reasoner` `max_tokens: 64000`, `use_temperature: false`, `caches_by_default: true`, editor model = deepseek-chat with `editor-diff` — [P] https://raw.githubusercontent.com/Aider-AI/aider/main/aider/resources/model-settings.yml
- Aider polyglot: V3.2-Exp Reasoner 74.2% pass / 97.3% well-formed; Chat 70.2% / 98.2% (`diff`) — [P] https://raw.githubusercontent.com/Aider-AI/aider/main/aider/website/_data/polyglot_leaderboard.yml
- Crush example model entry: `deepseek/deepseek-chat --context-window 64000 --default-max-tokens 5000 --can-reason true` — [P] https://github.com/charmbracelet/crush

**Kimi K2 / K2-Thinking / K2.6 / K2.7-code / K3**
- Platform docs: "Do not set `temperature`" for thinking models; `max_tokens >= 16000`; keep all `reasoning_content` (k2.6 needs `thinking.keep: "all"`) — [P] https://platform.kimi.ai/docs/guide/use-kimi-k2-thinking-model
- HF K2-Thinking tool guide: "Use `temperature=0.3` for tool-calling scenarios to improve consistency"; id format `functions.{name}:{idx}` — [P] https://huggingface.co/moonshotai/Kimi-K2-Thinking/blob/main/docs/tool_call_guidance.md (note: this conflicts with the platform page's "do not set temperature"; the platform page is newer and endpoint-specific)
- Widely repeated guidance: temperature 1.0 for thinking mode, 0.6 for instant mode; "stable tool-use across 200-300 sequential calls" — [S] https://docs.vllm.ai/projects/recipes/en/latest/moonshotai/Kimi-K2-Think.html ; [S] https://www.datacamp.com/tutorial/kimi-k2-thinking-guide
- Moonshot endpoint rejects non-default temperature: `HTTP 400 invalid temperature: only 1 is allowed for this model` when OpenCode's title agent sent 0.5 (`llm.ts:172` resolves `agent.temperature ?? ProviderTransform.temperature(model)`; reporter also lists glm-4.6/4.7, minimax-m2, gemini, qwen as affected; 2026-05-15, closed not planned) — [P] https://github.com/anomalyco/opencode/issues/27796
- Aider: `openrouter/moonshotai/kimi-k2` → `edit_format: diff`, `temperature: 0.6`; polyglot 59.1% / 92.9% well-formed — [P] model-settings.yml and leaderboard links above
- OpenCode schema fix (optional `description`) and Kilo native-JSON message-ordering fix were both Kimi-triggered (KQ1) — [P] https://github.com/sst/opencode/issues/1334 ; [P] https://github.com/Kilo-Org/kilocode/issues/3237

**Qwen3 / Qwen3-Coder / Qwen3-Coder-Next**
- Qwen3-Coder (30B-A3B, 480B-A35B): `temperature=0.7, top_p=0.8, top_k=20, repetition_penalty=1.05`; Qwen3-Coder-Next: `temperature=1.0, top_p=0.95, top_k=40`; use `--tool-call-parser qwen3_coder`; Q6_K+ if schemas are not followed — [S] https://unsloth.ai/docs/models/tutorials/qwen3-coder-how-to-run-locally ; [S] https://unsloth.ai/docs/models/qwen3-coder-next
- Qwen function-calling docs examples use `temperature=0.7, top_p=0.8, max_tokens=512, repetition_penalty=1.05` with Hermes parser — [P] https://qwen.readthedocs.io/en/latest/framework/function_call.html
- OpenCode defaults temperature 0 for most models and **0.55 for Qwen** (`ProviderTransform.temperature()`), overridable per agent — [S] https://deepwiki.com/sst/opencode/4.3-provider-transformations ; [S] https://github.com/anomalyco/opencode/issues/8101
- Cline local stack: Qwen3 Coder 30B A3B, 262,144 ctx, 4-bit, KV-cache quant off, compact prompt — [P] https://cline.bot/blog/local-models
- Aider polyglot: Qwen3-235B-A22B (no thinking) 59.6% / 92.9%; Qwen3-32B 40.0% / 83.6% — [P] leaderboard link above

**GLM-4.6 / 4.7 / 5.x**
- GLM-4.7 defaults `temperature 1.0, top_p 0.95`; for multi-turn agentic benchmarks (τ²-Bench, Terminal Bench 2) `temperature 0.7, top_p 1.0`; serve with `--tool-call-parser glm47 --reasoning-parser glm47` — [S] https://huggingface.co/zai-org/GLM-4.7 ; [S] https://recipes.vllm.ai/zai-org/GLM-4.7
- Measured tool-call corruption on GLM-5.3-Flash (2.05 bpw, 16-tool "opencode-shaped" surface, 120 samples/cell): **5/120 (4.2%) at T=1.0, 1/120 (0.8%) at T=0.6, 0/120 at T=0.0**; long-reasoning cases 3/20 at 1.0 vs 0/20 at 0.6; recommendation: override `generation_config.json` to 0.6 and handle "doom loops" in the harness — [P] https://github.com/0xSero/GLM-5.3-Flash-DGX-Spark/pull/3
- GLM 5.2 on Fireworks broke Crush on large edit payloads (KQ1) — [P] https://github.com/charmbracelet/crush/issues/3153
- GLM-4.6 tool-integrated-reasoning guide exists (examples use temperature 1.0) — [S] https://github.com/zai-org/GLM-4.5/blob/main/resources/glm_4.6_tir_guide.md

**Gemini 3**
- Google: keep `temperature` at default 1.0; lowering it "may lead to unexpected behavior, such as looping or degraded performance"; thought signatures must be returned exactly; strict 400 on missing signature in function calling — [S] https://ai.google.dev/gemini-api/docs/gemini-3
- OpenRouter maps `reasoning.effort` → Gemini 3 `thinkingLevel` — [P] https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
- Aider: Gemini entries `diff-fenced`, `use_temperature: false`, `accepts_settings: [thinking_tokens]`; Gemini 2.5 Pro 06-05 w/ 32k thinking = 83.1% / 99.6% — [P] model-settings.yml and leaderboard links above
- Community OpenCode plugin pins Gemini 3 Pro to 0.35 (`OPENCODE_GEMINI3_TEMPERATURE`), contradicting Google's guidance; it covers no other family — [P] https://github.com/Lyapsus/opencode-optimal-model-temps

**OpenRouter routing / pinning**
- `provider: {order: [...], allow_fallbacks (default true), require_parameters, data_collection, only, ignore, quantizations (int4|int8|fp8|bf16...), sort: price|throughput|latency, max_price}`; suffixes `:nitro` (throughput) and `:floor` (price); with `tools`/`tool_choice` OpenRouter "makes a best effort to route to providers known to support tool use", and `require_parameters: true` restricts to providers supporting every parameter; for reliability use explicit `order` + `allow_fallbacks: false`; provider uptime tracked over 30-second windows — [P] https://openrouter.ai/docs/features/provider-routing

**Harness-side output caps**
- OpenCode caps `maxOutputTokens` at 32k regardless of model (source of truncated-tool-call bug) — [P] https://github.com/anomalyco/opencode/issues/18108
- Roo clamps default max tokens to 20% of the context window — [S] https://github.com/RooCodeInc/Roo-Code/pull/6761
- Goose truncates agent input prompts at a 4096 default (#7264) — [S] https://github.com/block/goose/issues/7264

### Inferences
- A per-family parameter table for halo, derived from the citations: **DeepSeek thinking** — omit temperature/top_p, max_tokens 32-64k, always replay `reasoning_content`; **DeepSeek chat** — 0-0.3 (Aider uses default; OpenCode 0), 8k max_tokens; **Kimi K2 instant** — 0.6; **Kimi thinking/K3** — omit temperature, max_tokens ≥16k, replay reasoning, never renumber tool-call ids; **Qwen3-Coder** — 0.7/0.8/20/1.05 (or OpenCode's 0.55) with `qwen3_coder` parser; **GLM-4.7/5.x** — 0.6-0.7 for agentic loops (vendor 1.0 measurably worse), `glm47` parser, smaller edits; **Gemini 3** — 1.0, replay signatures, `diff-fenced`-style forgiving edit parsing.
- `tool_choice`: no harness documents forcing it for open models; the loop is universally "auto until finish_reason != tool_calls". Parallel tool calls: Cline/Kilo/Qwen Code exploit them; Roo serializes execution even when the model emits several — safe default for weaker models is "accept parallel, execute sequentially".
- Provider pinning matters most for Kimi/GLM on OpenRouter because quantized third-party endpoints change tool-call fidelity (Roo #9551 and the GLM DGX PR both involve quantized deployments); use `quantizations: ["fp8","bf16"]` plus `require_parameters: true`.

### Gaps
- No public, controlled comparison of tool-call failure rates across DeepSeek/Kimi/Qwen/GLM inside a single harness; only the GLM DGX-Spark PR gives counts.
- OpenRouter's per-model "reasoning_details required" list and its tool-calling page could not be fetched (404).
- Kimi "0.3 for tool calling" (HF) vs "do not set temperature" (platform) is unresolved; likely differs between self-hosted weights and the managed endpoint.
