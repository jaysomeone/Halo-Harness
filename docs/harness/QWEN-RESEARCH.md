# Qwen tool-calling research (for the owner's Databricks-at-work bug report)

Compiled 2026-10-02. Scope: why a Qwen model served through Databricks might
throw tool-calling errors in Halo, and what to change once the real
`halo bugreport` lands. External facts cite their source URL (fetched or
cross-checked today unless noted); Halo-internal facts cite the real
file/field/function name. This reuses and extends `reports/Open weight
model adapter rules.md`'s existing Qwen research rather than re-deriving
it — that file is the source for claims marked "(adapter-rules report)".

## OWNER BUG REPORT — fill in from `halo bugreport`, do not guess further

The owner's literal words were "openjev qwen"; the exact endpoint id, HTTP
status and error body are **not yet known**. Fill in this block from the
real report before acting on anything downstream of it:

- Model ref actually typed/selected in Halo (verbatim, not paraphrased):
- Resolved route: `provider` = ? (databricks / openrouter / fallback
  generic-openai) — see `bugreport.py`'s own `_route_lines`/`_log_meta_model`
  output, which a real bugreport already captures.
- HTTP status + raw error body of the failing call (bugreport.py does not
  capture a failed request body on its own; check whether the last-50-line
  `bridge.log` tail it includes caught it, or get it separately).
- Did the **first** turn fail (no `tool_calls` field reached the model at
  all) or a **later** turn (after a tool result was sent back)?
- Halo version/build, and whether `--session-id` resumption was involved.

**Leading hypothesis, found today and worth checking first:** Databricks'
own supported-models page documents a real endpoint whose service name is
`openjev-qwen35-4b` (endpoint `databricks-openjev-qwen35-4b`), described
verbatim as **"OpenJev (Qwen3.5 4B) evaluates yes or no, choice, and scoring
questions about text or structured data"** — a constrained
classifier/judge model, not a general chat/tool-calling model
([Databricks supported models](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/supported-models),
fetched 2026-10-02). "openjev qwen" matches this almost exactly. This
endpoint has **no row in `halo_harness/providers/model_table.json`** today
(confirmed: only `databricks-qwen3-next-80b-a3b-instruct` and
`databricks-qwen35-122b-a10b` exist under `"databricks"`). If this is the
endpoint the owner hit, Halo's `model_family()` (`halo_harness/providers/
profiles.py:103`) still classifies it as family `"qwen"` on the `"qwen"`
substring alone and applies full chat/tool-calling defaults to a model that
almost certainly was never meant to receive `tools`/`tool_choice` at all —
that mismatch, not a parser bug, would be the root cause. See §4.1 for the
concrete fix once this is confirmed. If it is **not** this endpoint, the
owner likely meant one of the two already-tabled general-purpose rows
(`databricks-qwen35-122b-a10b` or `databricks-qwen3-next-80b-a3b-instruct`)
and picked the wrong name from a model picker that doesn't show Databricks'
own one-line description — see §4.4.

## 1. Qwen3 / Qwen3-Coder tool-call wire format

**Qwen3 general (Hermes-style).** Qwen's own docs say to use "Hermes-style
tool use for Qwen3 to maximize function calling performance"
([Qwen function calling framework docs](https://qwen.readthedocs.io/en/latest/framework/function_call.html),
fetched 2026-10-02). On a parser that understands it, the reply is a native
OpenAI-shaped message:
```json
{"role": "assistant", "tool_calls": [{"id": "chatcmpl-tool-...",
  "type": "function",
  "function": {"name": "get_current_temperature",
               "arguments": "{\"location\": \"San Francisco, CA, USA\"}"}}]}
```
**`arguments` arrives as a JSON-encoded STRING even in the native shape**,
parsed client-side with `json.loads` (same source, fetched today) — this is
standard OpenAI wire shape, but it means a harness that stores `arguments`
as an already-parsed dict must re-serialize with real `json.dumps` on
replay, never Python's `str()`/repr, or it reproduces the single-quoted
`{'city': 'Paris'}` shape that gets **rejected on replay**
([agno #10231](https://github.com/agno-agi/agno/issues/10231), via
adapter-rules report). When no parser is configured (self-hosted/raw), the
same call leaks into `content` as literal template text:
`<tool_call>{"name": "...", "arguments": {...}}</tool_call>` — here
`arguments` is a nested JSON **object**, not a string, because this is the
model's raw trained output, not a parser's re-encoding (adapter-rules
report table, row "Qwen3 general / Nemotron reasoning").

**Qwen3-Coder / Qwen3.5 (XML-style).** These use a different trained
format: `<tool_call><function=NAME><parameter=KEY>VALUE</parameter>
</function></tool_call>`, with parallel calls as multiple `<tool_call>`
blocks in one message (adapter-rules report, row "Qwen3-Coder / Qwen3.5 /
Nemotron 3"; Qwen3-Coder's own GitHub repo confirms a dedicated parser
exists in both serving stacks — "relies on our new tool parser in both
SGLang and vLLM" — without itself spelling out the tag grammar
([QwenLM/Qwen3-Coder](https://github.com/QwenLM/Qwen3-Coder), fetched
2026-10-02). Two documented failure modes of this format specifically: the
model **omits the opening `<tool_call>` tag** after a prose lead-in
([Qwen3-Coder #475](https://github.com/QwenLM/Qwen3-Coder/issues/475)), and
a **string-typed parameter value arrives as a JSON object** instead of a
plain string ([opencode #6918](https://github.com/anomalyco/opencode/issues/6918)),
both cited via the adapter-rules report.

**Parallel calls.** Supported and demonstrated in Qwen's own docs (two
function calls returned together in one non-thinking-mode example;
[Qwen function_call docs](https://qwen.readthedocs.io/en/latest/framework/function_call.html)).
DashScope's hosted `parallel_tool_calls` flag defaults to **false**
([Model Studio function calling](https://www.alibabacloud.com/help/en/model-studio/qwen-function-calling),
via adapter-rules report) — parallel output from the model is not the same
as the host advertising/accepting the parallel-calls *parameter*.

**`tool_choice`.** DashScope's own docs state `required` is **"not
supported for Qwen models"** (same Model Studio page, via adapter-rules
report). Halo already encodes this: every current Qwen row in
`model_table.json` sets `"tool_choice_required_supported": false`, and
`ProviderProfile.tool_choice_required_supported` gates the content-side
"retry with `tool_choice: required`" fallback off for this family
(`halo_harness/providers/profiles.py`, `resolve_profile`'s
`tc_required_default` line and the per-row `row.get("tool_choice_required_supported", ...)`
overrides).

**Thinking tags and the `enable_thinking`/`/think`/`/no_think` switches.**
`tokenizer.apply_chat_template(..., enable_thinking=...)` defaults to
`True`; setting it `False` disables thinking entirely, matching
Qwen2.5-Instruct behavior. Independent of that, `/think` and `/no_think`
can be added to a user prompt or system message to switch modes **turn to
turn**, with the model following the most recent instruction; when thinking
is enabled a `<think>...</think>` block is always emitted, empty if
suppressed by a soft switch ([Qwen3-235B-A22B model card](https://huggingface.co/Qwen/Qwen3-235B-A22B),
fetched 2026-10-02 — all quotes in this paragraph from that fetch). The same
card is explicit about replay: **"the historical model output should only
include the final output part and does not need to include the thinking
content"** — i.e. strip prior-turn `<think>` blocks before sending history
back, which is exactly `ProviderProfile.reasoning_replay = "empty"` on every
current Qwen row. A sharper trap for Qwen3-235B-Thinking-2507 and QwQ:
their own cards say history may contain **only a closing `</think>` with no
opening tag** ([HF Thinking-2507](https://huggingface.co/Qwen/Qwen3-235B-A22B-Thinking-2507),
[HF QwQ-32B](https://huggingface.co/Qwen/QwQ-32B), via adapter-rules
report) — a tag-stripper that only matches a *paired* `<think>...</think>`
block will not catch this and will leak the bare `</think>` into displayed
text. DashScope adds an opt-in `preserve_thinking: true` for `qwen3.6+`/
`qwen3.8-max` that explicitly does **not** cover Qwen3.5, whose replay
behavior DashScope leaves undocumented (adapter-rules report, row "Qwen
(DashScope)" toggle table) — matches the `"unverified"` tags already on
several OpenRouter Qwen3.5 rows in `model_table.json`
(`"reasoning.replay"`).

## 2. How the gateways expose this

### Databricks Foundation Model APIs
Confirmed today via direct fetch of
[Databricks function calling docs](https://docs.databricks.com/aws/en/machine-learning/model-serving/function-calling):
`tool_choice` supports `"auto"` (default), `"required"`, `"none"`, and a
named-function object; **parallel function calling is explicitly not
supported**; JSON Schema restrictions forbid `pattern`, `anyOf`, `oneOf`,
`allOf`, `prefixItems`, and `$ref`, cap schemas at **16 keys**, and warn
that "heavily nested JSON schemas result in lower quality generation."
Halo already has a mitigation for most of this: `halo_harness/providers/
request.py`'s `_DATABRICKS_STRIP_KEYWORDS` strips `$schema`, `$defs`,
`definitions`, `anyOf`, `oneOf`, `allOf`, `pattern` and replaces `$ref`
nodes with a permissive `{"type": "object"}` — but does **not** strip
`prefixItems` or enforce the 16-key cap, both confirmed restrictions it
misses today. Databricks-wide, not Qwen-specific, but Qwen's own schemas
are as likely as any family's to trip it.

Which Qwen endpoints actually exist on Databricks (confirmed today,
[supported models](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/supported-models)):
`databricks-qwen35-122b-a10b` and `databricks-qwen3-next-80b-a3b-instruct`
(general chat, both already in `model_table.json`), `databricks-qwen3-
embedding-0-6b` (embeddings only, no chat/tools), and `databricks-openjev-
qwen35-4b` (the OpenJev classifier/judge model — see the marked section
above). Generic Databricks 400 shape for a disallowed body field:
`error_code 'BAD_REQUEST' ... json: unknown field "X"` (adapter-rules
report, Databricks community thread), which is why
`DATABRICKS_BODY_ALLOWLIST` in `profiles.py` exists and why
`tests/helpers/mock_databricks.py` enforces it the same way
(`_ALLOWED_KEYS = DATABRICKS_BODY_ALLOWLIST | {"model"}`).

### OpenRouter
Confirmed today via live fetch of
[qwen/qwen3-coder's endpoints](https://openrouter.ai/api/v1/models/qwen/qwen3-coder/endpoints):
every current provider (Google, DeepInfra, Venice, Novita, Alibaba) lists
both `tools` and `tool_choice` in `supported_parameters`, all capped at
`max_completion_tokens: 65536`. This matches the `openrouter_pin` rows
already in `model_table.json` (e.g. `qwen/qwen3-coder`'s `order: [alibaba,
google-vertex, novita]`, `require_parameters: true`). `tools`/`tool_choice`
themselves follow the plain OpenAI shape and values (`none`/`auto`/
`required`/named-function); OpenRouter's own docs defer the specifics of
what a given provider actually accepts to "the model's provider section"
([OpenRouter parameters reference](https://openrouter.ai/docs/api-reference/parameters),
fetched 2026-10-02) — which is exactly what `require_parameters: true`
protects against: without it, OpenRouter may route to a provider that
silently drops `tools` and returns prose instead of a 400
([provider routing / require_parameters](https://openrouter.ai/docs/features/provider-routing),
via adapter-rules report). OpenRouter's Response Healing plugin only
repairs `response_format` JSON, non-streaming, and explicitly "does not
indicate support for tool calls" ([Response healing](https://openrouter.ai/docs/guides/features/plugins/response-healing),
via adapter-rules report) — it will not rescue a malformed Qwen tool call.

### vLLM / SGLang parser names
Confirmed today via direct fetch of
[vLLM tool calling docs](https://docs.vllm.ai/en/latest/features/tool_calling.html):
`--tool-call-parser hermes` for `Qwen/Qwen2.5-*` and `Qwen/QwQ-32B` ("the
chat template in tokenizer_config.json has already included support for
the Hermes-style tool use"), and `--tool-call-parser qwen3_xml` for
`Qwen/Qwen3-Coder-480B-A35B-Instruct` and `Qwen/Qwen3-Coder-30B-A3B-
Instruct`. Note the naming drift: Halo's own `model_table.json` comment
names both `qwen3_coder` and `qwen3_xml`, and `hooks.py`'s pattern name is
`qwen3_coder_xml` — all Halo-internal identifiers, not vLLM's CLI flag.
Today's docs confirm only `qwen3_xml` as the live vLLM flag; verify against
the owner's actual vLLM version rather than assume `qwen3_coder` still
works. SGLang's current Qwen flag name is **unverified** — both
`docs.sglang.ai` and `docs.sglang.io` function-calling pages 404'd today;
confirm via the installed gateway's own `--help`. What SGLang's issue
tracker does confirm for this family of streaming parsers: truncation/hangs
over ~5KB on the sibling Kimi K2 parser
([sglang #23363](https://github.com/sgl-project/sglang/issues/23363)) and
duplicated opening markers on the sibling GLM parser
([sglang #15721](https://github.com/sgl-project/sglang/issues/15721)), via
the adapter-rules report — a known failure class on this tag-delimited wire
format generally, not Qwen-specific.

## 3. Error classes a Claude-Code-style harness hits with Qwen

**(a) Tool call emitted as text instead of a structured call.** Finish
reason `stop`, `tool_calls` empty, `content` holds the leaked markup from
§1. Halo's mechanism: `providers.hooks.leak_parser`, gated by
`profile.tool_leak_patterns`, invoked from `agent/loop.py`'s `_turn_body`
only when a call was expected, and only promoted when the match is
"essentially the whole message" (`_looks_like_the_whole_message`, ≤60
leftover chars) — `agent/repair.py`'s `extract_leaked_call` is a thin
passthrough to it. **Confirmed gap:** every Qwen row in `model_table.json`
declares `"tool_leak_patterns": ["hermes_tool_call", "qwen3_coder_xml",
"python_repr_args", "missing_tool_call_opener"]`, but
`halo_harness/providers/hooks.py` only implements the first two —
`_LEAK_PATTERNS` has no `"python_repr_args"` key and `_LEAK_EXTRACTORS` has
no `"missing_tool_call_opener"` key (verified: neither string appears
anywhere else in `halo_harness/**/*.py`). `leak_parser()`'s own dispatch
loop does `extractor = _LEAK_EXTRACTORS.get(pattern_name); if extractor is
not None: ...; continue` then falls to `rx = _LEAK_PATTERNS.get(pattern_name);
if rx is None: continue` — for these two names both lookups miss and the
loop just silently moves on. That means the two shapes Qwen is *most*
documented to produce — a bare trailing JSON object with no opening
`<tool_call>` tag ([Qwen3-Coder #475](https://github.com/QwenLM/Qwen3-Coder/issues/475))
and a Python-repr'd arguments dict ([agno #10231](https://github.com/agno-agi/agno/issues/10231))
— have **no actual recovery path** in Halo today despite the data table's
declared intent. This is the single most likely concrete defect behind
"tool-calling errors…with a Qwen model," independent of which endpoint the
owner hit.

**(b) Malformed JSON arguments.** `providers.hooks.args_repair` (code-fence
strip → `json.loads` → `ast.literal_eval` → string-aware structural repair
with brace-balancing → last-resort quote swap) already handles Python-repr
dicts generically, but only runs on text a leak extractor already isolated,
or on a native tool_call's `arguments` string in `oai_stream.py`'s
`_finalize` second-attempt path. `agent/repair.py`'s
`validate_and_coerce` separately coerces a JSON-encoded **string** value
for an `array`/`object`-typed schema field — its own docstring names this
exact scenario ("a Qwen/GLM habit of sending TodoWrite's `todos` as
`'[{"content":...}]'` rather than a real array"). Gap (a) above means
args_repair's fix can't even run for the two unimplemented leak patterns,
since nothing isolates the call text to repair in the first place.

**(c) Missing or renumbered tool ids.** Every Qwen row sets
`"tool_id_format": "preserve"` (`hooks.tool_id_normalize` — verbatim
passthrough), correct for a native tool_call's own opaque id. A
**leak-promoted** call (case a) never had a real id, so whatever mints one
during promotion in `agent/loop.py` (the `_promoted_from_leak` marker
`repair_kind_for` checks) is outside `repair.py`'s own scope — confirm the
minted id is the same one the subsequent `tool_result` references, since a
mismatch here is invisible until the *next* turn.

**(d) Reasoning-replay.** Covered in §1; Qwen's `reasoning_replay: "empty"`
(strip prior thinking) is already correct in every current row. The open
edge is the unpaired `</think>` from Thinking-2507/QwQ — `hooks.py`'s
`think_tag_strip` regex (`r"<think>.*?</think>\s*"`) requires a paired
block and won't strip a bare leading `</think>`.

**(e) `strict: true` rejections.** `ProviderProfile.strict` is set `True`
for every Databricks profile regardless of family (`profiles.py`,
`resolve_profile`'s databricks branch) but — confirmed by a repo-wide
grep — **nothing in `halo_harness/**/*.py` currently reads `.strict`
anywhere**. Halo does not emit a literal `"strict": true` today, so this
error class is not presently reachable through Halo's own request
building. It becomes live the moment anyone wires `.strict` into
`request.py`'s body builder, so the fix belongs in §4.1 as a guard rail,
not a currently-firing bug.

**(f) Schema features Qwen/Databricks mishandle.** Databricks rejects
`$ref`/`anyOf`/`oneOf`/`allOf`/`prefixItems`/`pattern` and caps schemas at
16 keys (confirmed today, §2). Separately, at the *model* level (not the
gateway), the adapter-rules report's own Qwen quirks list says to "keep
string params typed string (never anyOf); coerce objects to strings" —
i.e. even on a host that accepts `anyOf` in the schema, Qwen itself tends
to mis-type a parameter declared that way. Large enums have no
Qwen-specific documented limit found today; treat as the general
context-budget problem Halo's catalog/overflow machinery already handles,
not a Qwen-specific one.

**(g) Very long tool descriptions.** No Qwen-specific length limit is
documented beyond the general context/overflow budgeting Halo already does
(`agent/catalog.py`'s frozen-catalog selection, `tools_max` caps of 32
Databricks / 128 OpenRouter in `profiles.py`). Closest documented guidance
is Moonshot's (a different family) "don't put every tool definition into
the request — they eat up context" (adapter-rules report) — by analogy
only, not a Qwen citation.

**(h) What each gateway returns.** Databricks: `error_code 'BAD_REQUEST'`
/ `json: unknown field "X"` for a disallowed body field; a schema
restriction violation's exact 400 body was **not found in today's
fetches** — flag as unverified, capture it from the real bug report.
OpenRouter: `404 No endpoints found that support tool use` when routing
lands on a tool-less endpoint without `require_parameters`
([claude-code-router #409](https://github.com/musistudio/claude-code-router/issues/409),
via adapter-rules report). Raw vLLM/SGLang (self- or third-party-hosted):
usually **no error at all** — `finish_reason: "stop"`, the call sits in
`content` as text, exactly the silent class leak_parser exists for.

## 4. Recommendation for Halo

### 4.1 `model_table.json` / `profiles.py`
- **Add a row (or a refusal rule) for `databricks-openjev-qwen35-4b`** once
  confirmed as the owner's endpoint. `ProviderProfile` has no "this model
  does not do chat/tools at all" field today — the closest is
  `tools_max: Optional[int]` (caps *count*, not a yes/no gate). Two options
  for the follow-up worker: (a) add a new field, e.g. `tools_supported:
  bool = True`, defaulted `False` on this row, checked in `agent/loop.py`
  before the first request goes out, surfacing a clear message ("this
  Databricks endpoint does not support tool calling; try
  `databricks-qwen35-122b-a10b`") instead of a wire-level failure; or (b)
  check it at model-resolution time in `halo_harness/model.py`, next to
  `_is_cached_databricks_endpoint`/`_databricks_model_ref`, which is
  already where Halo distinguishes a workspace-custom endpoint name from
  the standard `databricks-`/`system.ai.` shape. Either way, do not let
  `model_family()`'s `"qwen" in low` substring match keep silently routing
  a judge/classifier endpoint through the full chat tool-calling path.
- **Implement the two declared-but-missing leak patterns** in
  `halo_harness/providers/hooks.py` (§3a): a `"python_repr_args"` entry
  (bare Python-dict-literal leak with no `<tool_call>` or fence wrapper at
  all — reuse `args_repair`'s `ast.literal_eval` path the way
  `_extract_fenced_json` already does, minus the fence requirement) and a
  `"missing_tool_call_opener"` entry (the same JSON-in-`<tool_call>` shape
  `hermes_tool_call` matches, but anchored on the closing
  `</tool_call>` only — the literal Qwen3-Coder #475 shape). No caller
  changes needed elsewhere: `profile.tool_leak_patterns` already names
  both for every Qwen row; they are just no-op names today.
- **Extend `think_tag_strip`** (`hooks.py`) with a second branch for a
  leading bare `</think>` with no opening tag (Thinking-2507/QwQ, §1/§3d).
- **Add `"prefixItems"` to `_DATABRICKS_STRIP_KEYWORDS`** in
  `providers/request.py` and consider a 16-key-cap truncation/warning —
  confirmed Databricks restrictions today that the current frozenset
  misses. Databricks-wide, surfaced by Qwen research.
- Leave `tool_choice_required_supported: false` and `strict` as-is for
  Qwen; if strict-schema support is ever wired into `request.py`, gate it
  off for `family == "qwen"` (DashScope documents no strict-function
  support; Databricks' own restrictions apply regardless, §2/§3e).

### 4.2 `agent/repair.py`
- No change needed to `validate_and_coerce`'s JSON-string-for-array/object
  coercion — it already covers the documented Qwen/GLM habit. Verify (don't
  assume) that `oai_stream.py` already `json.loads`s a native tool_call's
  string `arguments` before `repair_tool_use_block` ever sees
  `block["input"]`, since §1 confirms that string-encoding is the
  documented native shape, not just a malformed one.
- `RepairOutcome.error_kind` (`"unknown_tool"`/`"invalid_args"`) and
  `repair_kind_for`'s separate taxonomy (`"leak_parser"`/`"lenient_json"`/
  `"rename"`/`"args_repair"`/`"none"`) don't cross-reference each other
  today — worth joining before building any "Qwen tool-call error rate"
  telemetry query on top of them, so a leak-promoted call with also-invalid
  arguments doesn't get silently bucketed as just one or the other.

### 4.3 Mock-server scenarios to add
Following `tests/helpers/mock_databricks.py`'s existing `_scn_*(handler,
body)` + `SCENARIOS` dict convention:
- `qwen-leaked-hermes-tool-call` — baseline regression for the pattern
  that already works (`hermes_tool_call`).
- `qwen-missing-opener` — prose lead-in + bare `{"name":...,"arguments":
  {...}}</tool_call>`, no opening tag (new `missing_tool_call_opener`).
- `qwen-python-repr-args` — bare `{'name': 'Read', 'arguments': {'file_path':
  'x', 'ok': True}}`, single quotes + Python `True`, no wrapper (new
  `python_repr_args` + `args_repair`'s `ast.literal_eval` path).
- `qwen-coder-xml-string-param-as-object` —
  `<tool_call><function=Read><parameter=file_path>{"path":"x"}</parameter>
  </function></tool_call>` (opencode #6918's shape; `validate_and_coerce`'s
  string-coercion branch).
- `qwen-unpaired-close-think` — bare leading `</think>`, no opening tag
  (`think_tag_strip` fix).
- `databricks-schema-reject-qwen` — a 400 modeled on Databricks' documented
  `anyOf`/`$ref`/`prefixItems`/16-key restrictions (`_DATABRICKS_STRIP_KEYWORDS`
  regression guard).
- `openjev-not-chat-model` — stub until the real bugreport confirms the
  shape (a 400 on receiving `tools`, or a 200 that silently ignores them
  and returns a bare yes/no/score string, are both plausible here).

### 4.4 Questions the owner's `halo bugreport` must answer
(Ties to §0's placeholder and `bugreport.py`'s own `build_bugreport_text`
fields — route, provider enablement, `bridge.log` tail, session timeline.)
1. The literal model ref typed/selected — confirms or rules out
   `databricks-openjev-qwen35-4b`.
2. Whether the resolved route's `provider` was actually `"databricks"`
   (full profile applied) or an unrecognized-host fallback (profiles.py's
   own stated central finding: an unrecognized gateway is treated as plain
   OpenAI, i.e. none of §2's Databricks-specific handling would have run).
3. The failing call's exact HTTP status and body.
4. Whether `tools` were present on the **first** failing turn or only a
   **later** one (distinguishes "endpoint rejects tools outright" from "a
   replay/id mismatch on turn 2").
5. Halo version/build and whether `--session-id` resumption was involved.
6. Whether the owner's actual intent was a general-purpose chat Qwen model
   — if so this may be a model-picker clarity fix (show Databricks' own
   one-line endpoint description) rather than a protocol fix at all.
