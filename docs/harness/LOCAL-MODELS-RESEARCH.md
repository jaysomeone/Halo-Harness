# Local and cloud models research: Ollama and Hugging Face (2.0.3 round 1)

Compiled 2026-10-04. Scope: the ten research questions in
`plans/2.0.3-ollama-round1-research-brief.md`, for the design in
`plans/2.0.5-ollama-brief.md` (written under its old number; Ollama now
ships as 2.0.3 per `plans/ROADMAP.md`'s REORDERED section, with Hugging
Face added to the same release per ROADMAP's "Hugging Face and
cloud-hosted models join the 2.0.3 pack" section). Docs only, no code.

Every external fact below cites the page it came from and is marked
"(seen 2026-10-04)" for the date fetched; `github.com/ollama/ollama/blob/
main/docs/*.md` is cited separately from `docs.ollama.com/*.md` where the
two disagree -- Ollama moved its docs to a new site recently and the old
GitHub copies are not all current (see "Documentation inconsistencies
found" near the end). Anything not confirmed by a fetched page is marked
**UNCONFIRMED** rather than guessed, per the brief. `WebSearch` was not
used this round (budget exhausted for this session); every citation below
is a direct `WebFetch` of a documentation page.

## 1. The native Ollama API

Source: `github.com/ollama/ollama/blob/main/docs/api.md` (seen
2026-10-04; the page itself now carries a banner: "Ollama's API docs are
moving to https://docs.ollama.com/api") and the newer
`docs.ollama.com/capabilities/{tool-calling,thinking,structured-outputs,
embeddings,vision}.md` pages (seen 2026-10-04).

- **`/api/chat` and `/api/generate`**, streaming NDJSON (one JSON object
  per line, `stream: true` by default): each chunk has `model`,
  `created_at`, `message: {role, content, thinking, tool_calls}`
  (generate's equivalent fields are `response` and `thinking` -- no
  `message` wrapper), and `done: false` until the final chunk, which adds
  `done_reason` (`"stop" | "length" | "load" | "unload"`), `total_duration`,
  `load_duration`, `prompt_eval_count`, `prompt_eval_duration`,
  `eval_count`, `eval_duration` (all duration fields in nanoseconds). The
  exact runtime semantics of `done_reason: "load"` vs `"unload"` were not
  spelled out in the fetched text (the 2.0.5 brief already assumes "load"
  means the request triggered a model load and is worth retrying once --
  **UNCONFIRMED**, verify against a live server in round 2). Non-streaming
  (`stream: false`) returns one complete object with the same fields.
- **Tool calling**: `tools` is an array of OpenAI-shaped function
  definitions (`{"type": "function", "function": {name, description,
  parameters}}`). Tool calls come back as `message.tool_calls`, each
  `{"type": "function", "function": {"name": ..., "arguments": {...},
  "index": 0}}` -- **arguments is a parsed JSON object, not a
  string** (unlike the OpenAI wire shape), and **there is no tool-call
  id**; Ollama uses a 0-based `index` instead, so a harness that needs
  stable ids for replay has to synthesize its own (confirmed independently
  by `docs.ollama.com/capabilities/tool-calling.md`, seen 2026-10-04,
  which also explicitly demonstrates **parallel tool calls**: "Request
  multiple tool calls in parallel, then send all tool responses back to
  the model"). A tool result is replayed as `{"role": "tool", "tool_name":
  "<name>", "content": "<result>"}`. The only model the current docs name
  by name in the tool-calling examples is **qwen3**; the capability is
  otherwise declared per-model via the `tools` entry in `/api/show`'s
  `capabilities` list (see Q3), not hard-coded to a model list.
- **`think`**: accepts `true`, `false`, `null` ("use the model default"),
  or a level string. `docs.ollama.com/capabilities/thinking.md` (seen
  2026-10-04) names **gpt-oss** as the model with graded levels (`"low"
  | "medium" | "high"`, gpt-oss's own three), and **qwen3** and
  **deepseek-r1** as other thinking-capable models (bool-only for these
  two as far as the fetched text showed). Thinking content streams back
  on a separate field: `message.thinking` for `/api/chat`, bare `thinking`
  for `/api/generate` -- never mixed into `content`/`response`.
- **`options`**: confirmed fields and defaults from `api.md`: `num_ctx`
  (context window size; the options-table default shown there is
  **1024**, which conflicts with the newer context-length guide -- see
  "inconsistencies"), `num_predict`, `temperature` (default 0.8),
  `top_k` (20), `top_p` (0.9), `min_p` (0.0), `repeat_penalty` (1.2),
  `repeat_last_n` (33), `presence_penalty` (1.5), `frequency_penalty`
  (1.0), `seed`, `stop`, `num_keep`, `num_batch`, `num_gpu`, `main_gpu`,
  `num_thread`, `use_mmap`, `numa`. `keep_alive` is a top-level request
  field (not inside `options`), default `"5m"`, accepting duration
  strings, a plain number of seconds, or a negative number for "never
  unload."
- **`format`** (structured output): `"json"` (loose JSON mode) or a full
  JSON-schema object for strict structure. `docs.ollama.com/capabilities/
  structured-outputs.md` (seen 2026-10-04) stresses that the schema should
  **also** be restated in the prompt text for reliability, recommends
  `temperature: 0`, and states plainly: **"Ollama's Cloud currently does
  not support structured outputs."** The OpenAI-compatible endpoint maps
  this to `response_format`.
- **Images**: a user message carries an `images` array of base64-encoded
  strings (REST API); the official SDKs additionally accept file paths,
  URLs, or raw bytes and encode them client-side (`docs.ollama.com/
  capabilities/vision.md`, seen 2026-10-04). Only `gemma4` appeared by
  name in the fetched vision examples; no documented size/format limits.
- **`/api/tags`**: returns `{"models": [{name, model, modified_at, size,
  digest, details: {family, families, parameter_size,
  quantization_level, format, parent_model}}]}`.
- **`/api/show`**: returns `modelfile`, `parameters`, `template`,
  `details` (same shape as tags), `capabilities` (a string list; `tools`,
  `thinking`, `vision` are confirmed values, `embedding` is implied by
  the embeddings docs -- the full enum was not printed verbatim on any
  fetched page, **UNCONFIRMED** complete list), and `model_info`, a flat
  dict keyed like `general.architecture`, `general.parameter_count`,
  `<family>.context_length`, `<family>.block_count`,
  `<family>.attention.head_count_kv`, `<family>.embedding_length` (the
  `<family>.*` prefix matches the model's `details.family`, e.g.
  `llama.context_length` for a Llama-family GGUF) -- this confirms the
  key shapes the 2.0.5 brief already assumed.
- **`/api/ps`**: lists currently loaded models with `size`, `size_vram`
  (compare the two to see partial CPU offload), `expires_at` (when
  `keep_alive` will unload it), and `context_length` (the context the
  *running* instance was loaded with -- this is the one place a context
  length appears without calling `/api/show`).
- **`/api/embed`**: request `{"model", "input": "<string or array>",
  "truncate"?, "options"?, "keep_alive"?}`; response `{"model",
  "embeddings": [[float,...], ...], "total_duration", "load_duration",
  "prompt_eval_count"}`. Vectors are **L2-normalized**
  (`docs.ollama.com/capabilities/embeddings.md`, seen 2026-10-04, which
  names `embeddinggemma`, `qwen3-embedding`, and `all-minilm` as the
  current recommended embedding models -- directly relevant to the
  2.0.6 embeddings release). A `dimensions` request field appears in the
  brief's own list but was **not** present on the fetched embeddings
  page -- **UNCONFIRMED**, may be older/removed or just undocumented.
- **OpenAI-compatible endpoint** (`docs.ollama.com/api/openai-compatibility.md`,
  seen 2026-10-04): base URL `http://localhost:11434/v1/` locally,
  `https://ollama.com/v1` for cloud. Supports `/v1/chat/completions`,
  `/v1/completions`, `/v1/embeddings`, `/v1/models`, `/v1/models/{model}`,
  and a non-stateful `/v1/responses`. Explicitly **missing**: `logprobs`,
  `tool_choice`, `logit_bias`, `user`, `n` (chat); `best_of`, `echo`,
  token-array prompts (completions); token-array input (embeddings);
  `previous_response_id`/`conversation`/`truncation` (responses); image
  **URLs** (base64 only). Most importantly for Halo's design: **"Context
  size requires creating a custom model via `Modelfile` rather than
  per-request configuration"** -- the OpenAI shim has no equivalent of
  `options.num_ctx`, so there is no way to size the context window
  per-request through it. This is the concrete, documented reason for
  Halo's `ol:` route to use the native API, confirming the 2.0.5 brief's
  starting position rather than just asserting it.

## 2. Context windows

Source: `docs.ollama.com/context-length.md` and `docs.ollama.com/
modelfile.md` (seen 2026-10-04).

- **Default when nothing is set**: this is the single most important,
  and most inconsistent, fact found this round. The dedicated
  context-length guide states Ollama now picks a default **based on
  detected VRAM** when `OLLAMA_CONTEXT_LENGTH` is unset and no
  per-request `num_ctx` is given: **below 24 GiB VRAM, 4096 tokens;
  24-48 GiB VRAM, 32768 tokens; 48 GiB VRAM or more, 262144 tokens.**
  This is a server-wide default, not per-model. Set it explicitly with
  `OLLAMA_CONTEXT_LENGTH=64000 ollama serve` (environment variable, read
  at server start). Two other pages still print older, flat defaults
  that contradict this (see "inconsistencies" below) -- treat the VRAM-
  tiered rule as current and the flat numbers as stale until verified
  live.
- **Per-request**: `options.num_ctx` on `/api/chat`/`/api/generate`
  (native API only -- see Q1 on why the OpenAI shim can't do this).
  **Per-model default**: a Modelfile `PARAMETER num_ctx <n>` bakes a
  default into a named model; the fetched Modelfile reference page's own
  parameter table lists `num_ctx`'s default as **2048** -- again
  inconsistent with context-length.md's VRAM-tiered rule (see below).
  Whether a request's `options.num_ctx` overrides a Modelfile's baked-in
  value was **not stated** on either fetched page -- **UNCONFIRMED**,
  but 2.0.5's existing design (Halo always sends `options.num_ctx`
  itself every request) makes this moot in practice: Halo should never
  rely on a Modelfile default when it can just always send the field.
- **Finding a model's trained/maximum context**: not a single documented
  field name on the context-length page itself, but Q1's `/api/show`
  findings give the answer: `model_info["<family>.context_length"]` is
  the model's trained maximum; nothing in `/api/tags` or `/api/ps` gives
  this without a `details.family` fallback. `ollama ps`'s CLI output
  shown on the context-length page lists a live example of a `131072`-
  token context next to `"100% GPU"`, confirming `/api/ps`'s
  `context_length` field is the *currently loaded* context, not
  necessarily the trained maximum.
- **Memory cost per token (KV cache)**: no formula or worked numbers
  were given on any fetched Ollama page -- the only guidance found was
  the plain statement that "setting a larger context length will
  increase the amount of memory required." The bytes-per-token formula
  Halo needs (`2 x block_count x head_count_kv x head_dim x
  bytes_per_elem`, where `head_dim = embedding_length / head_count`,
  and `bytes_per_elem` is 2 for f16 KV cache, 1 for q8_0, 0.5 for q4_0)
  is standard GGML/llama.cpp KV-cache accounting carried over unchanged
  from the 2.0.5 brief's own text -- **not independently re-derived from
  a fetched page this round**, but it is the only formula consistent
  with the `model_info` fields `/api/show` actually exposes (Q1), so it
  is the right thing to compute with; mark the formula itself as
  carried-over/standard rather than freshly confirmed.
- **Worked sizing table.** Using that formula against three
  representative architectures (approximate public specs, not
  re-verified against each model's live `/api/show` this round --
  **UNCONFIRMED exact per-model numbers**, but illustrative of the
  method and roughly right): a dense ~8B model (32 layers, 8 KV heads,
  head_dim 128 -> 4 KB/token at f16, 1 KB/token at q4_0), a ~30B MoE
  A3B coder model (48 layers, 8 KV heads, head_dim 128 -> 6 KB/token at
  f16, 1.5 KB/token at q4_0), and a dense ~32B model (64 layers, 8 KV
  heads, head_dim 128 -> 8 KB/token at f16, 2 KB/token at q4_0).
  Weights-on-disk size (the quant Ollama ships, independent of KV
  quant) consumes most of the budget; what is left over is divided by
  the per-token KV cost to get the context that fits fully in VRAM:

  | VRAM/RAM budget | ~8B dense (q4_0 weights, ~4.5 GB) | ~30B MoE A3B (q4_0 weights, ~18 GB) | ~32B dense (q4_0 weights, ~19 GB) |
  |---|---|---|---|
  | 8 GB | fits; ~800k tok headroom at q4_0 KV, far above any trained ctx -> effectively uncapped by VRAM | does not fit (model alone exceeds budget) | does not fit |
  | 12 GB | ~1.8M tok headroom (uncapped) | ~1.3M tok headroom at q4_0 KV (uncapped by VRAM, limited by trained ctx) | does not fit |
  | 16 GB | uncapped by VRAM | uncapped by VRAM at q4_0 KV; ~96k tok at f16 KV | ~1.1M tok headroom at q4_0 (uncapped) |
  | 24 GB | uncapped | uncapped | uncapped at q4_0; ~320k tok at f16 |
  | 32 GB | uncapped | uncapped | uncapped |
  | 48 GB | uncapped | uncapped | uncapped |
  | CPU-only (system RAM) | same formula against free RAM instead of VRAM; same token budget, but prompt/eval speed drops roughly an order of magnitude -- the ceiling is tokens/second patience, not memory, past about 16-32 GB of system RAM | same | same |

  The table's point for the design: **for any single mid-size model on
  8 GB of VRAM or more, the KV cache is essentially never the binding
  constraint once weights fit at all** -- the binding constraint is
  almost always (a) whether the *weights* fit (a quant/size decision made
  at pull time, not request time), and (b) the model's own *trained*
  context ceiling, not available VRAM. Halo's per-request `num_ctx`
  sizing (2.0.5 brief, Phase 1) should clamp to `min(trained context,
  host.max_ctx override, 131072)` and mostly ignore VRAM arithmetic
  except as a "will this model's weights even load" gate (Q3) and a
  warning when `OLLAMA_NUM_PARALLEL` > 1 multiplies the same KV cost
  across slots (confirmed: `docs.ollama.com/faq.md` states required RAM
  "will scale by `OLLAMA_NUM_PARALLEL * OLLAMA_CONTEXT_LENGTH`").

## 3. Hardware and host analysis

Source: `docs.ollama.com/gpu.md` (seen 2026-10-04) plus the `/api/ps` and
`/api/show` fields already confirmed in Q1.

- **What `/api/ps` and `/api/show` expose, combined**: `/api/ps` gives
  the live picture (`size` total resident bytes vs `size_vram` bytes
  actually in VRAM -- the difference is CPU-offloaded weight, i.e. slow;
  `expires_at` for keep_alive; the loaded `context_length`); `/api/show`
  gives the static picture (trained context, quant level, family,
  parameter count, capabilities). Neither endpoint reports total system
  VRAM/RAM directly -- that has to come from the OS (next point).
- **GPU vendor/memory detection is OS tooling, not an Ollama API.**
  `gpu.md` confirms Ollama's own detection logic and env vars (useful for
  explaining *why* a host behaves a certain way) but not a way to query
  detected totals over HTTP:
  - NVIDIA: auto-detected at compute capability 5.0+ with driver 550+
    (570+ for older 5.0-6.2 cards); select GPUs with
    `CUDA_VISIBLE_DEVICES` (comma-separated; "UUIDs are more reliable"
    than numeric indices since ordering can vary) -- `nvidia-smi -L`
    prints the UUIDs. **Windows and Linux both use `nvidia-smi`** for
    totals/usage -- **CONFIRMED live, 2.0.3 round 3** (previously
    UNCONFIRMED at the exact-flag level): `nvidia-smi --query-gpu=
    memory.total,memory.used,memory.free,name --format=csv` prints a
    header row and unit-suffixed values (e.g. "<N> MiB"); adding
    `,noheader,nounits` to `--format` drops the header AND the unit
    suffix, leaving one CSV line of plain MiB integers plus the GPU name
    (e.g. `<total>, <used>, <free>, <name>`) -- the invocation `providers/
    ollama_hw.py` actually uses, since it needs to parse the numbers, not
    just display them. `memory.free` is a valid query field on its own
    (no need to compute `total - used` by hand). On Windows, `Get-
    CimInstance Win32_VideoController` (WMI) is the brief's own non-
    nvidia-smi fallback; moot in practice -- `nvidia-smi` itself already
    works identically on Windows and Linux, so Halo's own code never
    needed the WMI path at all. Still **UNCONFIRMED**: the WMI fallback
    itself (never exercised), and both the AMD/ROCm and Apple branches
    below (no such hardware available to verify this round either).
  - AMD/ROCm: needs ROCm v7 on Linux; select with `ROCR_VISIBLE_DEVICES`;
    `HSA_OVERRIDE_GFX_VERSION` force-targets an LLVM gfx version for
    unsupported cards; `rocminfo` lists devices. `rocm-smi --showmeminfo
    vram` (brief's own naming) and Linux's `/sys/class/drm/card*/device/
    mem_info_vram_total` sysfs file are the standard ROCm/kernel-level
    ways to read totals -- **UNCONFIRMED** against ROCm's own docs this
    round (not fetched), but both are widely-documented standard paths.
  - Apple Metal: automatic detection, no selection env var documented.
    `system_profiler SPDisplaysDataType` (brief's own naming) is macOS's
    standard CLI for this -- **UNCONFIRMED** against Apple's docs this
    round (not fetched).
  - Vulkan (cross-vendor fallback, on by default on Windows/Linux):
    `GGML_VK_VISIBLE_DEVICES` (numeric ids, `-1` disables), or
    `OLLAMA_VULKAN=0` to disable Vulkan entirely.
  - Forcing CPU-only: set the relevant visible-devices variable to an
    invalid id (e.g. `-1`).
  - `gpu.md` did **not** document partial-offload mechanics, a minimum-
    VRAM threshold, or a log line to confirm what Ollama actually
    detected -- **UNCONFIRMED**, all three are exactly what Phase 2 of
    the 2.0.5 brief wants the `/ollama` panel to surface, so they need
    either a live-server check in round 2 or a source-code read of
    `ollama/ollama` (out of scope for a docs-only round).
- **A rule set for "will this model run with this context on this
  host"**: nothing fetched gives Halo a ready-made rule; this round's
  conclusion (combining Q1's `model_info` fields, Q2's KV-cache formula,
  and this section's OS-level VRAM read) is that Halo has to assemble it
  itself: (1) read `details.quantization_level` + `general.
  parameter_count` from `/api/show` to estimate weights-on-disk size
  (roughly bytes-per-parameter by quant: ~0.5 for q4_0/q4_K_M, ~1 for
  q8_0, ~2 for f16); (2) read total VRAM from the OS tool for the host's
  vendor; (3) if weights alone exceed VRAM, the model partially or fully
  CPU-offloads (slow, not blocked -- report it, never gate it, per
  `plans/feedback_no_cyber_blocks.md`-style house policy against blocking
  behavior); (4) whatever VRAM remains after weights, divide by Q2's
  bytes-per-token to get the context ceiling from memory, then clamp
  that against the model's trained `context_length` -- whichever is
  smaller wins. This is exactly the sentence-shaped recommendation the
  2.0.5 brief's Phase 2 already describes (`qwen3-coder:30b at 65k would
  offload; 32k fits with q8_0 KV cache`); this round confirms the
  underlying fields exist to compute it and that no Ollama API shortcuts
  the arithmetic.

## 4. Network hosting

Source: `docs.ollama.com/faq.md`, `windows.md`, `linux.md`, `macos.md`,
`docker.md` (all seen 2026-10-04).

- **`OLLAMA_HOST`**: default `127.0.0.1:11434` (localhost only). Set to
  `0.0.0.0:11434` to bind every interface, which is what makes the
  server reachable from other devices on the LAN.
- **`OLLAMA_ORIGINS`**: CORS allow-list; default permits `127.0.0.1` and
  `0.0.0.0` (i.e. same-origin/localhost tools); browser-extension
  origins (`chrome-extension://*`, `moz-extension://*`,
  `safari-web-extension://*`) can be added explicitly. This governs
  which **web pages'** JavaScript may call the API cross-origin -- it is
  not an auth mechanism and does not restrict which **hosts** on the
  network can reach the port once `OLLAMA_HOST` is `0.0.0.0`.
- **No authentication exists in Ollama itself** -- confirmed explicitly:
  the FAQ states no API key or auth is required for local server
  operation, and `gpu.md`/`openai-compatibility.md` say nothing to the
  contrary. Anyone who can reach the bound port can use the API and pull
  its model list. The safe way to expose it on a LAN, per ordinary
  reverse-proxy practice (not itself documented on any Ollama page
  fetched this round -- this is a general design recommendation, not a
  cited fact): bind `OLLAMA_HOST` to the LAN interface (not `0.0.0.0` on
  an untrusted network) or keep it on `127.0.0.1` behind a reverse proxy
  (nginx/Caddy) that adds a bearer-token check and terminates TLS, plus
  an OS firewall rule scoped to the LAN subnet rather than "Allow all."
  Halo's own config (`ollama.hosts`, 2.0.5 brief Phase 1) should treat
  every non-`127.0.0.1` host as unauthenticated-by-definition and never
  assume a token exists unless the user adds a proxy of their own.
- **Where to set it per OS** (confirmed from the per-OS install docs):
  - **Windows**: Settings (Win 11) or Control Panel (Win 10) ->
    "Environment Variables" -> edit/add a *user* variable -> OK/Apply,
    then **quit the tray app and relaunch it** (or open a new terminal)
    for it to take effect. No `setx`-based method was documented on this
    page.
  - **Linux (systemd)**: `sudo systemctl edit ollama`, add
    `Environment="OLLAMA_HOST=0.0.0.0:11434"` under a `[Service]`
    section in the editor that opens, then `sudo systemctl daemon-reload`
    and restart the service. An equivalent manual override file at
    `/etc/systemd/system/ollama.service.d/override.conf` works too.
  - **macOS**: the fetched `macos.md` page covered install location,
    storage, logs, and uninstall, but **did not document how to set
    environment variables** for the menu-bar app at all --
    **UNCONFIRMED**; community knowledge (not fetched/cited this round)
    is `launchctl setenv OLLAMA_HOST 0.0.0.0:11434` followed by
    relaunching the app, but this needs live verification before it goes
    in product docs.
  - **Docker**: environment variables pass via `-e`; the port mapping in
    every example is `-p 11434:11434`; GPU passthrough is `--gpus=all`
    for NVIDIA or `--device /dev/kfd --device /dev/dri` for AMD/ROCm or
    generic Vulkan access.
- **How another Halo session on a different device discovers and uses
  it**: not an Ollama feature at all (Ollama has no discovery protocol
  of its own that was found) -- this is purely a Halo-side design
  question, answered by the existing `ollama.hosts` config list (name,
  url, default flag) in the 2.0.5 brief; mDNS auto-discovery is
  explicitly deferred there to the later P2P research (ROADMAP 3.0.1.1).

## 5. Local model families: session model vs. sub-agent resource

This question is only partially answerable from documentation pages --
tool-calling *reliability* and *instruction-following quality* are
benchmark/community-report claims, not API reference facts, and
`WebSearch` was unavailable this round to gather current benchmarks or
community threads. What the fetched Ollama docs confirm directly:

- **qwen3** is the only model Ollama's own tool-calling examples name by
  model id, and it is also named as thinking-capable.
- **gpt-oss** is the only model documented with graded `think` levels
  (`low`/`medium`/`high`); also thinking-capable.
- **deepseek-r1** is documented as thinking-capable; the 2.0.5 brief's
  own candidate list separately asserts deepseek-r1 distills do **not**
  support tools, consistent with DeepSeek-R1 distills being reasoning-
  trace models rather than agentic/tool-trained ones, but that specific
  claim was **not** independently re-confirmed against an Ollama doc
  page this round (no fetched page discusses deepseek-r1 and tools
  together) -- **UNCONFIRMED**, carry forward from the existing brief as
  a hypothesis to test with the capability probe, not as settled fact.
- **Llama 3.x, Gemma 3, Mistral/Devstral, Phi-4**: none of these were
  named on any fetched Ollama capability page. Whether each supports
  tools in Ollama's template depends on whether Ollama's own model
  template for that tag includes a tool-call grammar/parser -- this is
  per-tag, discoverable live via `/api/show`'s `capabilities` list (Q1),
  but **not enumerable from documentation alone**. **UNCONFIRMED for all
  four families** -- this is precisely why the brief's Q5 says "cite
  benchmarks and community results," which this round could not do
  without WebSearch, and precisely why the roadmap already moved the
  model-gym *ranking* out of 2.0.3 and into 2.0.5: 2.0.3 should ship the
  **capability probe** (send a trivial tool-call + a trivial `think`
  request + a long-ish prompt to each candidate model, read back
  whether `tool_calls`/`thinking` actually came back and whether
  `prompt_eval_count` matches the sent token estimate) rather than
  publish a trust-the-vendor ranking table that this round cannot
  actually verify.
- **What Claude Code-style harnesses do with local models today**: based
  on general, not freshly fetched, knowledge of the pattern used across
  this class of tool (Halo's own prior art included) -- point an
  OpenAI-compatible-shaped client at a local server, because these
  harnesses are built around OpenAI-style chat-completions tool calling.
  The cost, confirmed concretely by Q1 for Ollama specifically, is losing
  native features behind the shim (here: per-request context sizing,
  `tool_choice`, reasoning-effort exposure). This is the same tradeoff
  2.0.5 already resolved in favor of Ollama's native API for `ol:`; this
  round's findings support extending the same logic to `hf:local/*`
  servers that *only* offer an OpenAI-compatible endpoint (Q8) -- there,
  Halo has no native-API alternative to fall back to, so the context-
  sizing gap has to be closed differently (a server-side `--ctx-size`/
  `--max-model-len` flag set at server *launch* time, not per request;
  see Q8/Q10). **General pattern, not a specific cited source** --
  mark as community/background knowledge, not a fetched fact.

## 6. Recommended design for 2.0.3 (Ollama)

This restates and tightens the existing `plans/2.0.5-ollama-brief.md`
Phase 1-3 design against what this round actually confirmed; it does not
replace that brief, which stays the implementation reference.

- **`ol:` route on the native API**, never the OpenAI shim, for the one
  concrete, documented reason in Q1: the shim has no per-request context
  control at all. Request builder sends `options.num_ctx` on every call
  (never relies on a Modelfile default, sidestepping Q2's override-order
  unknown entirely), `keep_alive` from host config, `think` from the
  session's effort setting (off for low effort on models that default to
  thinking, graded low/medium/high for gpt-oss specifically, bool for
  everything else), and tools in the OpenAI function shape Q1 confirmed
  the native API already expects. Streaming decoder accumulates
  `message.content`, `message.thinking`, and `message.tool_calls`
  per chunk; **Halo must assign its own tool-call ids** on the way in
  (Q1: Ollama sends none, only `index`) so the rest of the harness's
  tool-result/replay machinery (built around ided tool calls elsewhere)
  doesn't need a second code path for this one provider.
- **Capability probe** (this release's stand-in for the deferred model
  gym, per ROADMAP's reordering note): for each catalog model, read
  `capabilities` from `/api/show` for the declared `tools`/`thinking`/
  `vision` support, then -- cheaply, in the background, cached per model
  digest -- send one real trivial tool-call turn and read back whether
  `tool_calls` actually arrived non-empty. Q5 found that declared
  capability (what the template claims) and demonstrated capability
  (what the model actually does when asked) are not the same thing for
  any family except qwen3/gpt-oss/deepseek-r1, which is exactly the gap
  a live probe closes and a documentation read cannot.
- **Context ownership**: `num_ctx = min(trained context from
  `<family>.context_length`, host.max_ctx override, a fit estimate from
  Q2/Q3's arithmetic, 131072)`; compaction trigger at 75% of that number
  for local routes, same as non-local routes elsewhere in Halo.
- **`/local` command and picker columns**: size, quant, effective vs.
  trained context, tools/thinking/vision badges from the probe, "loaded
  now" (from `/api/ps`), measured tok/s (from `eval_count`/
  `eval_duration` once a model has actually run). Group label `Ollama
  (ol:)` plus host name when more than one host is configured.
- **Roles**: local models default to a supporting role (`small`,
  `researcher`, `judge`, `subagent_default`) never `main`/`orchestrator`
  by default; choosing one as main is allowed but prints the plain
  consequence sentence (e.g. "no tool calling: the model can answer but
  not edit files") rather than blocking, consistent with house policy
  against gating behavior.
- **Init tab**: `Ollama (local or LAN)` -- URL field seeded from
  `OLLAMA_HOST` if set else `127.0.0.1:11434` (Q4's confirmed default),
  a probe button, model list with capability badges, role assignment.
- **Config keys and defaults** (names only; values match the sources
  above): `ollama.hosts` (list of `{name, url, default, keep_alive,
  max_ctx, num_parallel_hint}`), `roles.*` (reusing the existing roles
  table rather than a second mechanism -- `halo_harness/roles.py`
  already has the `ROLE_NAMES` tuple and resolution order this should
  plug into, not duplicate).

## 7. Ollama Cloud

Source: `docs.ollama.com/cloud.md` (seen 2026-10-04).

- **Model naming**: cloud-hosted models are pulled/run with a `-cloud`
  suffix at the CLI (the page's own example: `gemma4:cloud`), while a
  direct API request uses the plain tag (the page's example:
  `"gemma4:31b"`) against `https://ollama.com/api/chat`. Cloud models
  **do not need to be downloaded** -- there is no local weights pull at
  all, which matters for Q3's "will this fit on this host" logic: cloud
  refs should skip the VRAM/KV arithmetic entirely rather than report a
  false "won't fit."
- **Auth**: create an API key at `ollama.com/settings/keys`, set
  `OLLAMA_API_KEY`, send it as `Authorization: Bearer $OLLAMA_API_KEY`.
  (The desktop app can also sign in and hand a token to connected apps
  automatically -- not relevant to a headless Halo session.)
- **Client compatibility**: the page states plainly that OpenAI or
  Anthropic clients can also be pointed at the cloud endpoint, "each
  supports a subset of the original API" -- exact subset
  **UNCONFIRMED**, not enumerated on the fetched page.
  `openai-compatibility.md` separately gives the OpenAI-shaped cloud
  base URL as `https://ollama.com/v1`.
- **Which models**: browseable at `ollama.com/search?c=cloud` or via
  `/api/tags` against the cloud host; no fixed list was printed on the
  fetched page beyond the one example.
- **Pricing/quotas**: links to a usage page and a pricing page exist,
  but **no concrete numbers were present on the fetched page itself** --
  **UNCONFIRMED**, needs a direct fetch of `ollama.com/pricing` in a
  later round if it matters for the design (the owner's explicit ask
  this round was capability coverage, not cost modeling).
- **Feature parity with local**: structured output (`format`) is
  **explicitly unsupported on cloud** ("Ollama's Cloud currently does
  not support structured outputs," Q1). Tool calling and streaming
  parity were **not** explicitly confirmed or denied on the fetched
  cloud page -- **UNCONFIRMED**, treat as "probably yes, verify live"
  since the cloud endpoint is documented as the same `/api/chat` shape.
  Data handling: "We do not use them to train models," per the page's
  own privacy-policy reference.
- **How one `ol:` route addresses local, LAN, and cloud by host name**:
  this falls out of the existing `ollama.hosts` config design (Q6)
  almost for free, because the native request shape is identical in all
  three cases (same `/api/chat`, same options). A host entry just needs
  a `url` (`http://127.0.0.1:11434`, `http://<lan-host>:11434`, or
  `https://ollama.com`) and, for the cloud entry only, an `api_key`
  field whose value is `OLLAMA_API_KEY` sent as a bearer header instead
  of no auth at all. `ol:<model>@cloud` (or whatever name the user gives
  the cloud host entry) selects it the same way `ol:<model>@<hostname>`
  already selects any named LAN host -- no new routing concept needed
  beyond "some hosts require an Authorization header and some don't."

## 8. Hugging Face, local

Sources: `huggingface.co/docs/huggingface_hub/en/guides/cli`,
`huggingface.co/docs/transformers/en/serving`,
`huggingface.co/docs/text-generation-inference/en/{index,quicktour}`,
`github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md`,
`docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server.html`,
`lmstudio.ai/docs/app/api`, `jan.ai/docs` (all seen 2026-10-04).

**How people actually run a local HF model, per server:**

- **`llama-server` (llama.cpp)**, for GGUF files (often pulled straight
  from the Hub, since llama.cpp can load a `hf://` reference directly in
  recent builds -- not independently re-confirmed this round,
  **UNCONFIRMED**, but widely known). Default bind `127.0.0.1:8080`
  (`--host`/`--port` to change). Endpoints: `GET /health`, the
  llama.cpp-native `POST /completion`, and OpenAI-compatible `GET
  /v1/models`, `POST /v1/chat/completions`, `POST /v1/embeddings`.
  Tool calling needs the `--jinja` flag (uses the GGUF's own embedded
  chat template, which may itself need `--chat-template-file` for an
  older GGUF without a tool-aware template baked in). Structured output
  via `--json-schema` or a raw `--grammar`. Context: `--ctx-size`/`-c`,
  documented default **0, meaning "load from the model"** -- i.e.
  llama-server can auto-read the GGUF's trained context unless told
  otherwise, unlike Ollama needing the VRAM-tiered guess (Q2).
- **`transformers serve`** (HF's own, "for evaluation, experimentation,
  and moderate load," explicitly **not** recommended for production --
  the docs point production users at vLLM/SGLang instead). Install via
  `pip install transformers[serving]`; default address
  `http://localhost:8000`. Endpoints: `/v1/chat/completions` (text,
  image, audio, video), `/v1/completions`, `/v1/responses`, `/v1/audio/
  transcriptions`, `/v1/models`, plus a non-OpenAI `/load_model` that
  streams load progress over SSE (useful as a health/progress signal
  Halo could poll while a model is loading). Tool-calling support
  specifically was **not confirmed either way** in the fetched excerpt
  (the page describes modality support, not a `tools` section) --
  **UNCONFIRMED**.
- **vLLM**, launched with `vllm serve <model>`; default port **8000**;
  endpoints include `/v1/chat/completions`, `/v1/completions`, batch
  and `/v1/responses` variants, `/v1/embeddings`, audio endpoints.
  Tool-calling flags (`--enable-auto-tool-choice`, `--tool-call-parser`)
  and `--max-model-len` are named in the brief and well-known from
  vLLM's own release notes, but the specific fetched page this round
  (redirected to a newer URL) did not happen to print that flag table in
  the extracted text -- **UNCONFIRMED from this round's fetch
  specifically**, though high confidence from the brief's own framing
  and vLLM's general documentation structure; re-fetch
  `docs.vllm.ai` directly in round 2 if the exact flag spelling matters
  before writing code against it. Page footer showed a "September 16,
  2026" update stamp.
- **Text Generation Inference (TGI)**: the fetched index page opens
  with a maintenance notice: **"text-generation-inference is now in
  maintenance mode. Going forward, we will accept pull requests for
  minor bug fixes, documentation improvements and lightweight
  maintenance tasks,"** and explicitly recommends vLLM, SGLang, or
  llama.cpp/MLX for new work instead. Still runnable: `docker run
  --gpus all ... -p 8080:80 ghcr.io/huggingface/text-generation-
  inference:3.3.5 --model-id <repo>` (host port 8080 mapping container
  port 80, matching the brief's "TGI 8080" guess over the alternative
  "3000"). Exposes its own `/generate` plus an OpenAI-compatible
  "Messages API." Given the maintenance-mode notice, Halo's docs should
  list TGI as supported-if-found but not recommended for a new local
  setup.
- **LM Studio**: default port **1234** (`lms server start --port
  1234`); exposes both a native REST API and a separate "OpenAI
  Compatibility Endpoints" set (chat completions, embeddings, models).
  Streaming and tool calling ("Tool calling and local agents with MCP")
  both listed as supported features. Models download through LM
  Studio's own in-app model browser (Hub-backed) rather than the `hf`
  CLI.
- **Jan**: described as a desktop app ("chat, models, MCP connectors,
  and local API server") plus a separately-distributed agent component.
  A local API server is confirmed to exist; **its default port could
  not be confirmed this round** -- two documentation URLs guessed for
  it (`jan.ai/docs/server`, `jan.ai/llms.txt`) both 404'd, and the
  landing page that did load didn't state a port number.
  **UNCONFIRMED**, needs a direct check (the app's own Settings screen,
  or a fresh search of current `jan.ai` docs) before Halo's docs state
  one.
- **Plain `transformers` in-process (no server at all)**: this is a
  real, commonly-used option (`AutoModelForCausalLM.from_pretrained(...)`
  + `generate()` directly in a Python process) but it means the calling
  process owns the model's memory for its own lifetime, has no HTTP
  boundary, and gets none of continuous batching, an OpenAI-shaped
  interface, or independent health/restart semantics. **Recommendation,
  not a documentation fact**: Halo should not load weights itself this
  way. Every server above already solves discovery, health-checking,
  and the OpenAI-compatible wire shape Halo's provider layer already
  speaks; in-process loading would mean writing and maintaining a
  second, bespoke inference path purely to skip running a one-line
  server command the user would otherwise run once. Recommend against
  it unless literally no server of any kind is present on the host --
  and even then, the better fix is probably "tell the user to run
  `llama-server` or `transformers serve`," not "Halo loads a model."
- **How models arrive on disk**: `hf download <repo_id> [files...]`
  (the current CLI; **replaces** the older `huggingface-cli download`)
  populates the shared Hub cache at `~/.cache/huggingface/hub/`
  (override root with `HF_HOME`, or just the hub subpath with
  `HF_HUB_CACHE`), structured as `models--<org>--<name>/{blobs/,
  snapshots/<revision>/, refs/}` with snapshot files symlinked back to
  content-addressed blobs. `hf cache ls` (optionally `--revisions`)
  lists what's present, `hf cache rm <id>` / `hf cache prune` clean it
  up, `hf cache verify <id>` checks integrity. This cache listing is
  exactly the second data source (alongside a running server's
  `/v1/models`) a shared "local models" discovery view needs (Q10).
  Loose GGUF files downloaded by hand (or by llama.cpp itself) live
  wherever the user put them -- outside this cache structure entirely,
  discoverable only via the server that has one loaded, or a configured
  directory.
- **Reading context length, chat template, and tool-calling support
  from a model on disk**: a Hub model's `config.json` carries the
  architecture's context field, commonly `max_position_embeddings` (the
  exact field name varies by architecture family and was **not**
  independently re-verified against a specific current model's
  config.json this round -- **UNCONFIRMED** at the level of "this exact
  key, for this exact family"). The chat template and tool-calling
  shape live in `tokenizer_config.json`'s `chat_template` (a Jinja2
  string); `apply_chat_template` renders it given a `messages` list and,
  for tool-aware templates, a `tools` keyword argument the template
  itself references via its own `{% if tools %}`-style logic (confirmed
  generally by `huggingface.co/docs/transformers/en/chat_templating`,
  seen 2026-10-04, though that specific page's fetched content covered
  template mechanics and a `reasoning_content`/`thinking` prefill
  feature rather than a worked tool-calling example -- detecting tool
  support programmatically by **inspecting the template string for a
  `tools` reference** is a reasonable heuristic consistent with how the
  template is invoked, but was not spelled out verbatim on the fetched
  page as a recommended detection method -- **UNCONFIRMED as an
  explicitly documented technique**, treat as a derived heuristic to
  validate in round 2, not a confirmed API).
- **Which runtimes honour tool calling and structured output**: llama-
  server (via `--jinja` + a tool-aware template, `--grammar`/
  `--json-schema`), vLLM (via its tool-call-parser flags, per the
  brief), LM Studio (documented as supporting both over its OpenAI-
  compatible endpoint); transformers serve and Jan are **UNCONFIRMED**
  either way from this round's fetches; TGI supports a schema-forcing
  feature it calls "Guidance" plus logprobs, but is maintenance-mode
  (see above).
- **Default ports/health endpoints to detect a running local server**
  (confirms the brief's own list, one correction noted): llama-server
  **8080** (`GET /health`); vLLM **8000** (`/v1/models` doubles as a
  liveness check; no dedicated `/health` path was seen in the fetched
  excerpt -- **UNCONFIRMED** on a dedicated health path specifically);
  `transformers serve` **8000** (same port family as vLLM -- a host
  cannot assume "port 8000 = vLLM," it has to disambiguate by querying
  `/v1/models` and reading back which one answered, or just treating
  both identically since both are OpenAI-compatible anyway); LM Studio
  **1234**; TGI **8080** when launched with the documented Docker
  example (container-internal port is 80, so a differently-run TGI
  could use a different host port -- 8080 is a convention from the
  docs' own examples, not a hard default baked into the binary);
  Ollama **11434** (Q4, for completeness in the same discovery view).
  Jan's port is **UNCONFIRMED** (see above).

## 9. Hugging Face, cloud

Sources: `huggingface.co/docs/inference-providers/en/{index,pricing}`,
`huggingface.co/docs/inference-endpoints/en/index`,
`huggingface.co/docs/api-inference/en/index` (seen 2026-10-04).

- **Inference Providers (the router)**: base URL
  `https://router.huggingface.co/v1`, OpenAI-compatible
  `/v1/chat/completions` (chat only through this exact drop-in path;
  other task types need the `huggingface_hub` `InferenceClient` instead
  of the OpenAI-shaped path). Auth is a single `HF_TOKEN` sent as
  `Authorization: Bearer $HF_TOKEN` -- one token for every partner
  provider, confirmed: "Unified Authentication & Billing: Use a single
  Hugging Face token for all providers." **Provider selection** is a
  suffix on the model id in the `model` field: no suffix or `:fastest`
  (default -- highest throughput), `:cheapest` (lowest price per output
  token), `:preferred` (the user's own saved provider-preference order
  at `hf.co/settings/inference-providers`), or a literal provider name
  (`openai/gpt-oss-120b:groq`). `provider: "auto"` is the equivalent
  field on the native `InferenceClient`. Partner list as of the fetched
  page: Baseten, Cerebras, Cohere, DeepInfra, Fal AI, Featherless AI,
  Fireworks, Groq, HF Inference, Novita, Nscale, OVHcloud, Public AI,
  Replicate, Scaleway, Together, WaveSpeedAI, Z.ai -- chat completion
  support varies per provider (a table on the page marks which of chat/
  VLM-chat/embeddings/image/video/speech each one offers).
  **`GET /v1/models`** on the router returns available models across
  providers including per-provider pricing, context length, latency,
  and throughput where available -- this is the one place HF publishes
  a machine-readable context-length field for cloud models, parallel to
  Ollama's `/api/show`.
- **Billing**: "Routed by Hugging Face" (default: HF bills the user,
  same rate the provider charges with "no markup," and the user's
  monthly free credits apply) vs. "Custom Provider Key" (user supplies
  their own provider account's key; HF does not bill at all, but no
  free-credit pool applies either). Free-tier monthly credits as stated
  on the pricing page: **$0.10/month for free accounts, $2.00/month for
  PRO users, $2.00/seat/month for Team or Enterprise** -- explicitly
  "subject to change." Credits are general compute credits, also usable
  on Inference Endpoints, Spaces GPU/ZeroGPU, and Jobs. Team/Enterprise
  can bill a specific org via an `X-HF-Bill-To` request header.
- **The older serverless "Inference API"**: not removed, **renamed and
  narrowed**. It now appears as just one more provider choice,
  `"hf-inference"`, inside Inference Providers -- functionally the same
  endpoint shape as any other provider. The pricing page states "As of
  July 2025, hf-inference focuses mostly on CPU inference (e.g.
  embedding, text-ranking, text-classification, or smaller LLMs that
  have historical importance like BERT or GPT-2)" -- i.e. it is no
  longer the place to run a current large chat model; current chat
  traffic should default to `:fastest`/`:cheapest` auto-selection across
  the real partner providers instead of pinning `hf-inference`.
- **Dedicated Inference Endpoints**: a separately managed, per-deployment
  service (autoscaling, observability, "deploy vLLM, TGI, or a custom
  container") -- a fundamentally different product from the shared
  router: the user provisions one specific model onto its own compute
  and gets back **its own dedicated URL and token**, billed by compute-
  time rather than per-token/pass-through. The fetched index page was
  high-level marketing copy without a concrete URL-shape or token
  example -- **UNCONFIRMED** on the exact per-endpoint URL pattern
  (expected to be an `*.endpoints.huggingface.cloud`-style hostname
  from general HF knowledge, but not confirmed by a fetched page this
  round).
- **Rate limits, streaming, errors**: **not confirmed this round** --
  no fetched page gave concrete rate-limit numbers, and none of the
  fetched examples exercised a streaming request or an error response
  body. **UNCONFIRMED**, worth a targeted fetch of a provider-specific
  page (e.g. `docs/inference-providers/en/providers/hf-inference`) in
  round 2 if Halo needs to design retry/backoff behavior against it.

## 10. Recommended design for Halo (Ollama + Hugging Face)

- `ol:` reaches a local daemon, a named LAN host, or Ollama Cloud, by
  host name in `ollama.hosts` (Q6/Q7) -- one dialect, one request
  shape, because Q1/Q7 together confirm the native `/api/chat` wire
  format is identical in all three cases; only the base URL and
  (cloud-only) an Authorization header differ.
- `hf:` reaches one of three distinct targets, each needing its own
  resolution path because, unlike Ollama, the three don't share a
  single consistent wire shape or auth model:
  - `hf:<org>/<model>` (optionally `:fastest`/`:cheapest`/`:preferred`/
    `:<provider>`, Q9) -> the router, `https://router.huggingface.co/v1`,
    `HF_TOKEN` bearer auth, OpenAI-compatible chat-completions dialect
    Halo's provider layer already speaks for other OpenAI-shaped
    providers (reuse, don't reinvent).
  - `hf:endpoint/<name>` -> a dedicated Inference Endpoint; `<name>`
    looks up a per-endpoint URL and token from a new `huggingface.
    endpoints` config list (shape mirrors `ollama.hosts`: `name`, `url`,
    `token`) because Q9 confirmed each dedicated endpoint gets its own
    URL and token, not a shared router path.
  - `hf:local/<model>` -> a local OpenAI-compatible server, resolved
    against `huggingface.local_servers` config (explicit entries) plus
    auto-detection on the default ports Q8 confirmed (8080 llama-server,
    8000 vLLM/transformers-serve, 1234 LM Studio, 8080 TGI) by probing
    each port's `/v1/models` (or `/health` where one exists) in the
    background, same `BRIDGE_TEST_NO_BACKGROUND_NET`-respecting pattern
    the 2.0.5 brief already specifies for Ollama host probing. Since
    Q8 found most of these servers can't take a per-request context
    override (vLLM/llama-server set it at server launch), Halo reads
    back whatever the server reports (llama-server's `/v1/models` or
    `/props`; vLLM's `/v1/models` `max_model_len` field) rather than
    trying to request a specific one.
- Shared "local models" discovery (`/local`): one view merging three
  sources Q8 already identified as the right inputs -- (1) each
  configured Ollama host's `/api/tags` + `/api/show` catalog; (2) the HF
  Hub cache listing (`hf cache ls`, or walking `~/.cache/huggingface/
  hub/models--*` directly rather than shelling out, since Halo needs
  structured output, not CLI text) for models present on disk but not
  necessarily being served by anything right now; (3) a live probe of
  each configured/auto-detected local server's `/v1/models`. Columns:
  size, quant (where known), context (trained, from `config.json` where
  locally readable, or the server's own reported max), tools/thinking/
  vision capability (same trivial-probe idea as the Ollama capability
  probe, generalized: one cheap real request per newly-seen model,
  cached by model identity), "loaded now" where the server concept
  supports it, group label (`Ollama (ol:)` / `Hugging Face (hf:)` with
  host/server name).
- Roles, init tab, config keys: identical shape to Q6's Ollama-only
  version, just with `hf:` refs as equally valid role values
  (`roles.small = "hf:local/<model>"` is exactly as legal as
  `roles.small = "ol:<model>"`); the init tab gains a second step
  (`Hugging Face`) alongside `Ollama (local or LAN)`, offering: paste an
  `HF_TOKEN` for the router, add a dedicated endpoint's URL and token,
  or just let auto-detection find local servers -- all optional and
  skippable, matching the round-7 init-wizard's Back/Skip/Next pattern
  already planned elsewhere in the roadmap.

## Comparison table: local runtimes

| Runtime | Default port | Native API | OpenAI-compatible | Tool calling | Structured output | Context control | Model source |
|---|---|---|---|---|---|---|---|
| Ollama | 11434 | Yes (`/api/*`) | Yes, limited (Q1) | Yes, per-model capability | `format` (json/schema) | Per-request (native only) | `ollama pull`, `OLLAMA_MODELS` dir |
| llama.cpp `llama-server` | 8080 | Yes (`/completion`, `/slots`) | Yes (`/v1/*`) | Yes, with `--jinja` | `--grammar` / `--json-schema` | `--ctx-size` (0 = auto from GGUF) | GGUF file, from Hub or built locally |
| vLLM | 8000 | No (OpenAI-only) | Yes (`/v1/*`) | Yes, tool-parser flags (**UNCONFIRMED** exact spelling this round) | Response-format based | `--max-model-len` at launch | Hub model id, downloaded at launch |
| `transformers serve` | 8000 | No (OpenAI-only) | Yes (`/v1/*`, incl. `/v1/responses`) | **UNCONFIRMED** | **UNCONFIRMED** | **UNCONFIRMED** | Hub model id (transformers cache) |
| TGI (maintenance mode) | 8080 (Docker example) | Yes (`/generate`) | Yes ("Messages API") | "Guidance" (schema-forced) | Yes (Guidance) | launch-time flags | Hub model id |
| LM Studio | 1234 | Yes (own REST) | Yes | Yes (incl. MCP-based agents) | **UNCONFIRMED** | GUI/CLI at load time | In-app Hub-backed downloader |
| Jan | **UNCONFIRMED** | **UNCONFIRMED** | Yes (confirmed to exist) | **UNCONFIRMED** | **UNCONFIRMED** | **UNCONFIRMED** | In-app model hub |
| Plain `transformers` (in-process) | n/a, no server | n/a | No | Depends on code written | Depends on code written | Whatever the caller sets | Hub model id (transformers cache) |

## Documentation inconsistencies found (mark anything that changed)

- **Ollama's default `num_ctx` is stated three different ways across
  Ollama's own current docs**: `api.md`'s options table says 1024;
  `modelfile.md`'s `PARAMETER` reference table says 2048; the dedicated
  `context-length.md` guide says there is no single flat default any
  more -- it is VRAM-tiered (4096 / 32768 / 262144 depending on
  detected VRAM) when `OLLAMA_CONTEXT_LENGTH` is unset. Treat
  `context-length.md` as authoritative (it is the newest, most specific
  page, and matches the live `ollama ps` example it shows of a
  131072-token loaded context) and the other two numbers as stale copy
  -- confirm live against an actual server in round 2 before any code
  depends on a specific number.
- **Ollama's docs are mid-migration**: the GitHub-hosted `docs/*.md`
  files Halo's own existing briefs cite (`docs/faq.md`, `docs/openai.md`)
  404 on `github.com/ollama/ollama/blob/main/docs/` as of this fetch --
  the directory now actually contains `.mdx` files (`faq.mdx`,
  `cloud.mdx`, `context-length.mdx`, `gpu.mdx`, etc.) and a separate
  `docs.ollama.com` site is the current canonical location, with plain-
  Markdown mirrors at `docs.ollama.com/<page>.md` (and a full index at
  `docs.ollama.com/llms.txt`) that this round fetched instead. Any
  future Halo doc or script that links to the old `github.com/.../docs/
  openai.md` / `docs/faq.md` paths should be updated to the
  `docs.ollama.com` equivalents.

## What could not be confirmed this round (summary)

- Exact semantics of `done_reason: "load"` vs `"unload"` (Q1).
- The complete enum of `/api/show` `capabilities` values beyond `tools`/
  `thinking`/`vision` (Q1); whether `/api/embed`'s `dimensions` field
  still exists (Q1).
- Whether a request's `options.num_ctx` overrides a Modelfile-baked
  `num_ctx` (Q2); exact per-family KV-cache numbers in the Q2 worked
  table (illustrative, not re-verified per model).
- GPU-memory query flag syntax for WMI/`rocm-smi`/`system_profiler` at
  the exact-flag level (Q3) -- `nvidia-smi` itself was confirmed live in
  2.0.3 round 3 (see section 3 above); any Ollama log line or command
  confirming detected GPUs; partial-offload mechanics; a documented
  minimum-VRAM threshold.
- How to set Ollama's environment variables on macOS at all -- the
  fetched `macos.md` did not cover it (Q4).
- Tool-calling/instruction-following reliability for Llama 3.x, Gemma 3,
  Mistral/Devstral, and Phi-4 specifically under Ollama (Q5) -- no
  WebSearch this round, and no fetched Ollama page named any of them in
  a tool-calling or thinking context.
- Exact cloud pricing/quota numbers (Q7); whether Ollama Cloud's tool
  calling and streaming fully match local behavior (Q7, beyond the
  confirmed structured-output gap).
- vLLM's exact current tool-calling flag names (Q8, page redirected and
  the fetched excerpt did not include that section); `transformers
  serve`'s tool-calling and context-length-setting support (Q8); Jan's
  default port and model-acquisition method entirely (Q8); the exact
  `config.json` field name for context length across architecture
  families, and whether "scan the chat template for a `tools`
  reference" is really how production tooling detects tool-call support
  (Q8, reasonable heuristic, not confirmed as a documented technique).
- Dedicated Inference Endpoints' exact URL pattern; Inference Providers'
  concrete rate limits, streaming behavior, and error-response shape
  (Q9).

## Open questions for round 2 to resolve by live-checking

1. Run an actual Ollama server and diff its real `num_ctx` default and
   `/api/show` `capabilities` enum against what these docs claim.
2. Probe vLLM's and `transformers serve`'s actual `/v1/chat/completions`
   behavior with a `tools` payload to settle Q5/Q8's tool-calling
   unknowns empirically instead of from docs.
3. Find Jan's actual default port and config location (install it, or
   find its current docs page under a different path).
4. Decide, with a live multi-model test, whether the capability probe
   (Q6) should run automatically on first catalog load or only on
   explicit user request, given it costs one real inference call per
   candidate model.
