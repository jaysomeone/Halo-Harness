# Models

How a `--model`/`/model` string resolves to a real upstream call, per-family
request-shaping rules, the model catalogs and how they refresh, and pricing.
Verified against `halo_harness/model.py`, `providers/profiles.py`,
`providers/cc_models.py`, `providers/dbx_routing.py`, and
`providers/model_table.json`.

## Model reference forms

| Form | Example | Resolves to |
|---|---|---|
| `or:vendor/model` | `or:deepseek/deepseek-v3.2` | OpenRouter |
| bare `vendor/model` (exactly one `/`) | `deepseek/deepseek-v3.2` | OpenRouter |
| `dbx:databricks-<name>` | `dbx:databricks-kimi-k3` | Databricks |
| `dbx:system.ai.<name>` | `dbx:system.ai.my_model` | Databricks |
| `dbx:<endpoint>@anthropic` | `dbx:databricks-kimi-k3@anthropic` | Databricks, forced onto the native Anthropic gateway for this one call |
| bare `databricks-*`/`system.ai.*` | `databricks-claude-opus-5-5` | Databricks (same as the `dbx:` form) |
| `cc:<name>` | `cc:opus`, `cc:sonnet` | your Claude subscription, via the installed `claude` binary |
| `ant:<name>` | `ant:opus`, `ant:claude-3-5-haiku` | `api.anthropic.com` pay-as-you-go (`ANTHROPIC_API_KEY`) |
| a bare subscription alias, no prefix | `opus`, `sonnet`, `fable`, `haiku` | at a Databricks work box: `dbx:<ANTHROPIC_DEFAULT_*_MODEL>`; else `cc:` if logged in and no key is set; else `ant:` if a key is set; else an error naming both |
| a `routes.json` alias | whatever `aliases` defines | resolved recursively (max 4 hops) before any of the above rules apply |

Parsing order (`model.py::parse_model_ref`): an exact match in
`routes.json`'s `aliases` is resolved first, recursively; everything after
that follows the table above, checked top to bottom. A ref that matches
nothing raises a clean `InvalidModelError` naming every accepted form (exit
2 from the CLI, with a near-miss suggestion when the catalog is cached).

A trailing `[1m]` suffix (a long-context variant marker) passes through
unchanged on every full id/alias form.

### `cc:`/`ant:` alias table

The same bare names resolve for both prefixes (`providers/cc_models.py`
`CC_ALIASES`/`ANT_ALIASES`); `opus` and `sonnet` are deliberately-moving
"latest" pointers (today both resolve to the `-5-5` point release) --
every other name is a specific, pinned version. `/model`, `halo
models` and the init picker show the resolved id next to each alias (e.g.
`cc:opus -> claude-opus-5-5 (latest Opus)`) for exactly this reason: `opus`
and `opus-5.5` are the same model.

| Alias | `cc:` resolves to | `ant:` resolves to |
|---|---|---|
| `fable` | `claude-fable-5-1` | `claude-fable-5-1` |
| `opus` (latest Opus) | `claude-opus-5-5` | `claude-opus-5-5` |
| `opus-5.5` | `claude-opus-5-5` | `claude-opus-5-5` |
| `opus-5` / `opus-5.0` | `claude-opus-5` | `claude-opus-5` |
| `opus-4.8` | `claude-opus-4-8` | `claude-opus-4-8` |
| `opus-4.6` | `claude-opus-4-6` | `claude-opus-4-6` |
| `sonnet` (latest Sonnet) | Claude Code's own `sonnet` pointer (today `claude-sonnet-5-5`) | `claude-sonnet-5-5` |
| `sonnet-5.5` | `claude-sonnet-5-5` | `claude-sonnet-5-5` |
| `sonnet-5` | `claude-sonnet-5` | `claude-sonnet-5` |
| `haiku` | Claude Code's own `haiku` pointer (today `claude-haiku-4-5-20251001`) | `claude-haiku-4-5-20251001` |

`halo models --cc --refresh` re-pings each alias (a cheap `-p
--max-turns 1` call) and caches the REAL id it got back to
`~/.halo/cc-models.json`, consulted before this static table for
`sonnet`/`haiku` specifically (the two whose target moves as Anthropic
ships new point releases) -- so a stale hardcoded id here is never the
only source once a refresh has run.

## Provider enablement

A provider's models reach `/model`/`halo models`/the `init` default
pick/`doctor` once it is **enabled** -- and (H15 part 2 addendum) that
happens AUTOMATICALLY, straight from real credentials, no `init`/`providers
enable` step required: OpenRouter/Anthropic API (key)/TypeSafe auto-enable
once their key is found (env file, shell env, or the settings env chain);
Databricks once a host AND token are found (same sources, plus
`~/.databrickscfg`); Claude Code subscription (`cc:`) ONLY when `claude
auth status` reports `loggedIn` with `authMethod` exactly `claude.ai` -- a
`claude` driven by an API token or a custom base URL (a work box's own
settings-driven login) never auto-enables it. `~/.halo/config.json`'s
`"providers"` block stores OVERRIDES only: `halo providers enable/
disable <name>` (or `/providers enable/disable <name>`, or completing a
tab in `halo init`) writes an explicit `true`/`false` there that
always wins over auto-detection -- `enabled: false` hides an auto-enabled
provider, `enabled: true` forces one on with no credentials at all. The
prefix table (also used by the `/model` picker's group headers, `init`'s
own tabs, `halo providers`/`/providers`, and `doctor`):

| Prefix | Label | Provider name (`halo providers`) |
|---|---|---|
| `dbx:` | Databricks | `databricks` |
| `or:` | OpenRouter | `openrouter` |
| `ant:` | Anthropic API (key) | `anthropic` |
| `cc:` | Claude Code subscription | `claude_subscription` |
| *(none yet)* | TypeSafe | `typesafe` -- stores `TYPESAFE_API_KEY` only, for a later feature |

A hand-typed ref whose provider isn't enabled is refused with a one-line
message -- for `claude_subscription` specifically (when auto-detection
found nothing, never for an explicit override) that message is the SAME
precise reason (not logged in / claude not installed / logged in via a
non-claude.ai authMethod, each with its own fix) a turn would have failed
with anyway, just surfaced earlier; every other provider names the
`halo providers enable <name>` fix. A provider that's detected (real
credentials/login) but explicitly disabled shows one dim hint line in
`/model` instead of a selectable row. `halo providers`/`/providers`
shows each provider's status as one of `auto (detected from <source>)`,
`disabled by you`, `enabled by you`, or `not set up`, plus reachable/cached
model count.

## Families and their rules

`providers/profiles.py::model_family(model_id)` classifies a bare upstream
id into `claude`, `deepseek`, `kimi`, `glm`, `qwen`, `gemini`, `grok`,
`minimax`, `gpt`, or `generic` by substring match. That family (plus the
exact model id, plus the host) looks up a row in
`providers/model_table.json` -- the single data file every per-model
request-shaping decision comes from, never a per-model `if` branch in the
request builder. Fields a row can override:

- **`reasoning_replay`**: how a "thinking" model's prior reasoning is sent
  back on the next request -- `text`/`deepseek_reasoning_content` (a plain
  field), `details`/`openrouter_details` (OpenRouter's structured array),
  `thinking`/`anthropic_thinking` (a real signed Anthropic block, always
  used for the native passthrough dialect), or `empty` (no known
  requirement).
- **Sampling** (`use_temperature`, `use_top_p`, fixed `temperature`/`top_p`/
  `top_k`, `sampling_unsupported_params`): some families reject a sampling
  parameter outright (e.g. Kimi K2.x-K2.7 fix temperature/top_p server-side
  and 400 if you send them); the request builder pops whichever ones a
  row lists right before sending, regardless of what a caller asked for.
- **`edit_format`**: `diff` (the default) vs. a family that prefers a
  different Edit-tool convention.
- **`tool_id_format`**: `preserve` / `kimi_functions_idx` (Kimi's own
  conversation-global `functions.{name}:{idx}` scheme) / `alnum9` /
  `minimax_preserve` -- different hosts accept different tool-call id
  shapes.
- **`tool_choice_required_supported`**: whether the harness may ever send
  `tool_choice: "required"` (the whole Qwen/GLM family and DeepSeek's
  thinking mode reject it).
- **`tool_leak_patterns`**: named per-family regexes for
  `providers/hooks.py::leak_parser` to recognize a text-embedded tool call
  and promote it to a real `tool_use` block (the repair layer -- see
  `docs/ARCHITECTURE.md`).
- **`context_tokens`**, **`max_tokens_default`**, **`max_tokens_cap`**,
  **`databricks_rate_limits`**, **`unverified`** (which fields this row's
  own data has no primary source for -- informational, never gates
  anything).
- **`reasoning_effort_with_tools`** (1.0.1 hotfix 22, narrowed by the H14c
  fixpass finding 12): when set, a request that carries `tools` sends this
  value for `reasoning_effort` REGARDLESS of `--effort`/the session's own
  effort (a tool-less request on the same model is unaffected). Databricks
  ONLY (an OpenRouter model whose id happens to contain "gpt-6" is never
  affected) -- driven by explicit `model_table.json` rows for both real
  models.dev name shapes, `databricks-gpt-6-{sol,luna,terra}` AND
  `databricks-gpt-5-6-{sol,luna,terra}` (the bare substring "gpt-6" is not
  even a substring of the second shape), each setting `"none"`: verified
  live, `dbx:databricks-gpt-6-sol` 400'd on every turn with "Function tools
  with reasoning_effort are not supported for gpt-6-sol in
  /v1/chat/completions... set reasoning_effort to 'none'", since omitting
  the field left the endpoint's own (non-none) default in place. A live
  400 with this exact wording ("function tool" + "reasoning_effort") on
  any OTHER Databricks OpenAI-family endpoint retries once with
  `reasoning_effort` set to `"none"` explicitly; if THAT retry also fails,
  the general strip-the-field retry below gets its own independent chance
  (a shared one-shot flag used to block it after a failed "none" retry),
  and a successful strip resets the session's own effort so later steps
  stop re-sending the rejected value first.

**The Edit-tool context hint**: a DeepSeek/Kimi/GLM/Qwen/MiniMax-family
session's Edit tool description gets one extra sentence asking for at least
three lines of `old_string` context (computed once per session, so the
wire request stays cache-prefix-stable) -- added after telemetry showed
these families' models under-quote `old_string` and hit "Found multiple
matches" far more often than Claude/GPT do. A specific `(provider,
model_id)` row's own `edit_hint` key (including an explicit empty string)
overrides the family default for just that one model.

## Qwen at work

Halo 2.0.2 round 5: a Databricks work box exposes Qwen two different ways,
and the model picker/`/model` no longer let you confuse them.

**General chat/tool-calling Qwen** -- `dbx:databricks-qwen35-122b-a10b` and
`dbx:databricks-qwen3-next-80b-a3b-instruct` (plus every OpenRouter
`qwen/qwen3-*`/`qwen/qwen3.5-*` id): Hermes-style `<tool_call>{json}</tool_call>`
tool calls, parallel calls on OpenRouter (Databricks sends/accepts one tool
call per turn -- no `parallel_tool_calls` field exists on its strict body
allowlist), `arguments` as a JSON-encoded string on the wire (decoded once
into a real dict on the way in, re-encoded with real `json.dumps` -- never
Python's `str()`/repr -- on replay), no `tool_choice: "required"`
(`tool_choice_required_supported: false` on every row), and the Qwen3-Coder/
Qwen3.5 XML shape (`<function=NAME><parameter=KEY>VALUE</parameter></function>`)
on top of the plain Hermes JSON form. `providers/hooks.py::leak_parser`
recovers a call that leaked into plain text under any of FOUR patterns a
Qwen row declares (`hermes_tool_call`, `qwen3_coder_xml`, `python_repr_args`
-- a bare single-quoted Python-dict-literal call with no wrapper at all,
and `missing_tool_call_opener` -- the documented Qwen3-Coder #475 shape, a
`}</tool_call>` tail with no opening tag); `think_tag_strip` also strips a
bare leading `</think>` with no opening tag (Qwen3-235B-Thinking-2507/QwQ's
own documented replay shape).

**OpenJev -- a decision-only judge model, not a chat model.**
`dbx:databricks-openjev-qwen35-4b` is Databricks' own "OpenJev (Qwen3.5 4B)
evaluates yes/no, choice, and scoring questions" endpoint: a constrained
classifier, never meant to receive `tools`/`tool_choice` at all. Halo
classifies this from data, not a hardcoded model id --
`providers/model_table.json`'s top-level `decision_only_name_patterns`
(substrings like `"openjev"`/`"jev-judge"`, so a differently-named judge
endpoint this box has never seen is still caught) and, for the tabled row
above, its own `capabilities.decision_only` + `capabilities.description`.
Effects, all driven by `providers.profiles.decision_only_info`/
`ProviderProfile.decision_only`/`.tools_supported`:
- The `/model` picker lists it under its own **"Databricks (judge /
  decision)"** group with that one-line description as its detail, instead
  of lumping it in with the ordinary `qwen` chat rows.
- `/model dbx:databricks-openjev-qwen35-4b` (or picking it) never becomes
  the session model -- it is written to the `judge` role instead
  (`roles.judge` in `~/.halo/config.json`, the same thing `/roles set judge
  <model>` does) and the command reports the redirect plainly. Launching
  `halo` with `--model`/a remembered last-model pointed at it behaves the
  same way, falling the session back to the ordinary configured default.
- A request that still carries `tools` for this model (or for ANY
  Databricks endpoint a live request previously proved rejects tools, even
  with no table row at all -- see `~/.halo/learned-rules.json`'s
  `tools_rejected` flag, `providers.learned_rules.learn_tools_rejected`)
  never reaches the wire at all: `providers.request.ToolsNotSupported`
  raises a clear, one-line error naming the model and the `judge` role.
  `agent/loop.py` catches the same condition live, the first time an
  UNTABLED endpoint's own 400 says so (Databricks' `json: unknown field
  "tools"`, or OpenRouter's `no endpoints... support tool use`) -- that one
  call teaches the per-endpoint cache so the next one never repeats it.

**Reading the exact 400 from `halo bugreport`.** If a Databricks Qwen
endpoint still errors on tool calls in a shape this round didn't already
cover, run `halo bugreport` right after the failing turn: its route section
names the resolved `provider`/endpoint, and the `bridge.log` tail it embeds
usually carries the raw HTTP status and error body verbatim (Databricks'
own shape is `{"error_code": "BAD_REQUEST", "message": "..."}`; the message
text is what `providers.errors.is_tools_rejected_message`/
`is_effort_with_tools_rejected_message`/`is_reasoning_replay_bug` each try
to recognize). Quote that exact text in a new issue/brief rather than
re-describing it -- see `docs/harness/QWEN-RESEARCH.md` for what was
already confirmed from Databricks' and Qwen's own docs versus what still
needs a real bug report to pin down.

## Reasoning effort

`--effort`, `/effort <level>`, or `settings.json`'s `effortLevel`/
`modelSettings.<id>.effortLevel` when neither of those was given, maps
through `providers/profiles.py::map_effort`/`providers/request.py::
map_effort_anthropic` to whatever field the resolved profile actually
needs: `output_config.effort` (adaptive-thinking Opus/Sonnet 4.6+/Fable/
Mythos) or a `thinking.budget_tokens` value (4096/10000/24000/32000/32000
for low/medium/high/xhigh/max, older Claude models) on the native Anthropic
dialect, `reasoning.effort` on OpenRouter, or `reasoning_effort` on a
Databricks chat endpoint. A profile with `reasoning_no_disable: true`
(GLM-5.3/5.3-Flash -- `thinking.type: disabled` is a hard 400 on that
family) forces an explicitly-disabling effort value back up to that row's
own default instead of ever sending `disabled`.

**The "high" default (1.0.1 hotfix 19, scoped by the H14c fixpass finding
11).** When NOTHING more specific is set anywhere (no `--effort`, no
`/effort`, no settings `effortLevel`) AND the model is ADAPTIVE-capable
(Opus/Fable/Mythos always, Sonnet 4.6+), the route now defaults to `high`
rather than omitting the field. A NON-adaptive Anthropic-family model
(Haiku 4.5, Sonnet 4.5 or older) keeps the old "omit -> provider default"
behavior instead -- that family has no `{type: "adaptive"}` mode at all,
so "high" became a `thinking.budget_tokens` value close to `max_tokens`
(see below), leaving thinking free to crowd out the actual answer. Every
non-Anthropic family keeps the "omit -> provider default" behavior
regardless. A `/model` switch re-clamps (see below) whatever effort the
session already had for the new route, defaulting a chat-route session
that switches INTO an adaptive-capable Anthropic route the same way.

**Accepted levels per route family, and clamping.** Each
`ProviderProfile` carries `effort_values_supported`
(`providers/profiles.py::clamp_effort` enforces it right before a value
reaches the wire):

| Route family | Accepted levels |
|---|---|
| Anthropic Messages (`cc:`, `ant:`, Databricks Claude foundation) | `low`, `medium`, `high`, `max` (no `xhigh` -- that endpoint schema rejects it outright) |
| Databricks GLM (`databricks-glm-*`) | `low`, `high`, `max` ONLY -- narrower than the general chat-dialect set below; see "Databricks GLM" just under this table |
| OpenRouter / Databricks chat, every other family (DeepSeek, Kimi, Qwen, ...) | `low`, `medium`, `high`, `xhigh` (no `max` as of the H14c fixpass finding 13 -- that's an Anthropic-only level; a chat route 400'd on it before this) |
| OpenRouter GLM 5.x (`z-ai/glm-5`, `-5.2`, `-5.3`, `-5.3-flash`) | the full seven Z.ai-direct values: `max`, `high`, `low`, `medium`, `minimal`, `none`, `xhigh` -- OpenRouter forwards them to Z.ai as-is |

A tabled row whose own `reasoning_default_effort` needs a value outside
its route family's default set (GLM's family genuinely defaults to `max`
on OpenRouter/Z.ai directly, per the ingested adapter-rules report --
`reasoning_no_disable` forces a disabling `--effort none` UP to it there)
still accepts that one value too; an explicit `effort_values_supported`
row key, when a future row sets one, always wins outright.

### Databricks GLM (2.0.1, GLM-brief.md)

Verified against the Databricks Foundation Model APIs "supported models"
and "query reasoning models" pages (2026-10-01): every `databricks-glm-*`
endpoint (`databricks-glm-5-2`, `-5-3`, `-5-3-flash`, and any future
`databricks-glm-*` id with no `model_table.json` row of its own yet --
this is a FAMILY-level rule, not per-model) keeps reasoning ALWAYS on and
accepts `reasoning_effort` values `low`, `high`, `max` ONLY -- `max` is
both the default AND the gateway's own SILENT fallback for anything else
(`medium`, `minimal`, `xhigh`, `none`); `none` is rejected outright.
`thinking`/`tool_stream`/`clear_thinking` (all three real Z.ai parameters)
are not in Databricks' parameter list at all, and an unknown field there
is a 400, so none of them can be sent on this route.

**The consequence this fixes**: before 2.0.1, `/effort medium` on a
Databricks GLM route sent `reasoning_effort: "medium"` on the wire, which
the GATEWAY silently coerced to `max` -- the harness never saw an error,
and the user's effort choice was quietly ignored every single turn. That
silent "always max" is also the "GLM seems to pause" symptom
([TROUBLESHOOTING.md](TROUBLESHOOTING.md)): `max` is the model's slowest,
most expensive thinking setting, and a chat-dialect gateway sends nothing
at all -- no reasoning text, no ping -- during that phase; only the
connection stays open. Halo 2.0.1 clamps to the route's REAL default
(`high`, not `max`) and shows the sent value everywhere (`/effort`,
`/status`, `/context`, the stream-json init line's `effort_sent`) instead
of ever silently disagreeing with the gateway.

**The default is sent, not omitted.** Because an omitted field on this
route means `max`, a call with no effort configured anywhere sends
`reasoning_effort: "high"` explicitly (`ProviderProfile.default_effort_
when_unset`, on for the Databricks GLM family only; every other
chat-dialect family keeps "omit -> provider default"). The same rule
applies to an effort inherited from Claude Code's settings (`effortLevel`
or `modelSettings.<id>.effortLevel`, written for Claude models and
typically `xhigh`): a settings value this route does not accept lands on
`high`, not on the clamp map's `max`. An explicit `--effort xhigh` or
`/effort xhigh` is a deliberate choice and still goes through the clamp
map to `max`; `/status` shows the requested and sent values either way.

**Clamp table** (`providers/profiles.py`'s `ProviderProfile.effort_clamp_map`,
consulted by `clamp_effort`/`resolve_effective_effort` before the generic
xhigh/max-narrowing rule):

| Requested | Sent |
|---|---|
| `low` | `low` |
| `medium` | `high` |
| `high` | `high` |
| `xhigh` | `max` |
| `max` | `max` |
| `minimal` | `low` (the cheapest accepted level, not the route default) |
| `none` | `low` (never sent literally -- Databricks rejects it outright) |

`/effort` on a Databricks GLM route therefore offers `low [high] max` (its
real accepted set) and marks any other requested value with "sent as X on
this route" rather than a plain, misleading echo.

Temperature is clamped to `[0, 1]` on every GLM-family route (Z.ai GLM API
docs: "temperature range [0, 1] default 1.0") -- a safety net, since every
seeded GLM row's temperature is already 1.0; and GLM never receives
`tool_choice: "required"` (the gateway accepts `auto` only -- the repair
layer's text-nudge fallback is used instead, same as DeepSeek-thinking/Qwen).

`xhigh` on a route that doesn't list it becomes `max` ONLY when that
route's own accepted set includes `max` (Anthropic, or a GLM-5.3-style
tabled exception) -- that route's own strongest level; any other
unrecognized value (including a now-rejected `max` on an ordinary chat
route) falls back to the route's own default. This is what fixed a live
400 on `dbx:databricks-claude-opus-4-6`: the user's own
`~/.claude/settings.json` `effortLevel: "xhigh"` (valid for real Claude
Code's own routes) reached `output_config.effort` verbatim and was
rejected (`Input should be 'low', 'medium', 'high' or 'max'`) on the very
first prompt. As a backstop for a route whose accepted set isn't modeled
correctly yet, a live 400 naming the effort field retries ONCE with it
stripped from the body before the turn is treated as failed -- recognized
wordings now include Databricks/Anthropic's own `output_config.effort`/
`reasoning_effort` field names, OpenRouter's nested `reasoning.effort`
path, and a generic OpenAI-style "Invalid value: '`max`'"/"'`xhigh`'"
enum-validation 400 that names neither field by name.

**Thinking budget cap (non-adaptive models).** When effort maps to a
`thinking.budget_tokens` value (any non-adaptive Anthropic-family model),
the budget is capped at HALF of `max_tokens` (never below Anthropic's own
1,024-token minimum), not `max_tokens - 1` -- the old near-`max_tokens`
budget left thinking free to crowd out the actual answer. The version
parser behind the adaptive-capability check reads hyphenated ids
(`claude-sonnet-4-6`, `databricks-claude-sonnet-4-6`) as `4.6`, not a bare
major version `4` (a literal-dot `claude-sonnet-4.6` was already read
correctly). Thinking is dropped entirely on the one-shot
`tool_choice: "any"` repair retry (the leak-parser fallback) -- Anthropic
rejects extended thinking together with any forced (non-`"auto"`)
`tool_choice`.

**`/effort`** (1.0.1 hotfix 20): shows the effective level, its source
(`flag`/`settings`/`default`/`session`), and this route's own accepted
levels when given no argument; `/effort <level>` sets it immediately
(clamped the same way), remembered for the rest of the session. In the
TUI, bare `/effort` instead opens an inline selector card in the
transcript -- a horizontal row of this route's accepted levels, current
one bracketed, Left/Right or h/l to move, Enter to apply, Esc to cancel
and keep the old value; a model with no adjustable effort at all shows a
one-line card that closes on any key. The status bar shows a short effort
tag next to the mode glyph.

## The model table, catalogs, and refresh

Three cached files under `~/.halo/` back model-profile resolution,
consulted in this order before falling back to a hardcoded guess
(`model.py::resolve_model_profile`):

1. **`models.json`** (OpenRouter) / **`dbx-endpoints.json`** (Databricks) --
   the live-probed catalog (`halo models --refresh`; see
   `docs/COMMANDS.md`). Supplies real context window, max output tokens,
   vision support, and (OpenRouter) pricing including cache-read/cache-write
   rates.
2. **`routes.json`**'s own `profiles` map -- a hand-authored override,
   keyed by bare model name or `"default"`.
3. **The vendored fallback tier** (`providers/catalog/*.json`, shipped
   inside the package) -- OpenRouter's own catalog and models.dev's
   `databricks` provider entries, frozen at release time, so a **fresh
   install with no network yet** (most notably a Databricks work box behind
   a VPN that might not be up) still resolves real context/output/pricing
   for every model this harness ships defaults for, instead of the bare
   128k/16k dataclass guess.
4. `providers/model_table.json`'s own `context_tokens`/`max_tokens_cap`
   (request-shaping data, not economics, but a real, hand-verified number
   for this harness's own pinned Databricks work-default models even if
   nothing else above has one).

`cc:`/`ant:` subscription models are resolved from a **separate**, fourth
table (`providers/cc_models.py::CC_MODEL_TABLE`) instead -- Claude Code is
the provider, not OpenRouter/Databricks, so neither of the two live catalogs
has an entry for these at all. `halo models --cc --refresh` re-pings
each of the nine alias names once to confirm the exact ids
`sonnet`/`haiku` currently resolve to (the two whose target moves as
Anthropic ships new releases) and caches the result to
`~/.halo/cc-models.json`, consulted before the static table.

`models-dev.json` (models.dev's own public, unauthenticated `api.json`) is
fetched by `halo models --refresh` regardless of which providers are
configured -- it backs the Databricks vendored-fallback tier and
`doctor`'s cache-freshness check.

## Pricing and DBUs

**Live per-turn cost** (`stats`/`/cost`, and the status bar's own `$` field
since 1.0.1 hotfix 14) still works exactly as before for OpenRouter: real
per-token USD pricing in `models.json` (`price_in`/`price_out`/
`price_cache_read`/`price_cache_write`); when a response itself doesn't
carry a `usage.cost` field, `CostMeter` derives one from those rates
(input × price_in + (output + reasoning) × price_out + cache_read/
cache_write at their own rates, falling back to `price_in` for cache tokens
when no specific rate is known). **Databricks** used to always report
`n/a` regardless of pricing; since 1.0.1 hotfix 14, `resolve_model_profile`
feeds the SAME per-endpoint price used for the listed columns below (source
(a), the models.dev `databricks` entry) into this session's `CostMeter` at
start-of-session, so a Databricks turn on an endpoint models.dev prices
gets a real computed running cost through the identical fallback formula
above -- and still shows `n/a` (falls back to token totals in the status
bar) whenever no models.dev price exists for that endpoint, which remains
the common case. A `cc:` row's "cost" is Claude Code's own cumulative
`total_cost_usd`, delta'd since that subprocess's previous turn -- an
estimate, never real per-token billing, and never counted toward
`--max-budget-usd`.

**Listed (not live) context/output/price columns** -- what `/model`,
`/models`, `halo models`, and `init`'s own picker show per row
(`halo_harness.model_display.format_model_row`, one implementation shared
by every one of those surfaces) -- are a DIFFERENT, catalog-level concept:
a normalized (`200000` -> `200k`, `1048576`/`1050000` -> `1M`) reference
figure for the model itself, never a live spend number, sourced per
provider:
  - **OpenRouter**: `models.json`'s own `context_length`/`max_output_tokens`/
    `pricing.prompt`/`pricing.completion` (the exact same fields `CostMeter`
    above reads, just displayed rather than multiplied against real usage).
  - **`cc:`/`ant:`**: the nine subscription-model aliases' own static table
    (`providers.cc_models.profile_fields_for_cc_model`).
  - **Databricks**: (a) the [models.dev](https://models.dev) `databricks`
    provider's entry whose id equals the endpoint name (the live
    `~/.halo/models-dev.json` cache, refreshed by `models --refresh`/
    `/models refresh`, else the package-vendored fallback) -- `limit.context`/
    `limit.output` for context/output, `cost.input`/`cost.output` (already
    USD per million tokens) for the prices, used AS-IS, never re-multiplied;
    (b) missing that, `model_table.json`'s own `context_tokens` (output/
    prices then stay blank); (c) neither -- every field blank. **A blank
    field is never shown as `?`** -- it just keeps its column's width.

`databricks.dbu_price_usd` (`~/.halo/config.json`, or a team.json's
`dbu_price_usd`) is a THIRD, separate figure, in a different unit again: it
converts an endpoint's own **catalog-advertised** DBU rate
(`usage_policy.output_dbu_per_1k_tokens`, when the workspace publishes one)
into a dollar figure, appended after the ctx/output/price columns as
`dbu=<rate>` only when actually known -- a reference price, never a real
per-turn spend, and never conflated with the USD/1M-token prices above.

## The `/model` picker

Opening `/model` with no argument triggers a background refresh of the
Databricks catalog if it's older than `databricks.catalog_max_age_hours`
(default 24h) -- the picker itself opens immediately with whatever's
already cached; the refresh only ever notifies afterward if something
changed. The picker is a filterable, arrow-key list (`Up`/`Down`/
`PageUp`/`PageDown`/`Home`/`End` move the highlight, typing filters,
`Enter` confirms, `Esc` cancels -- the filter box itself keeps keyboard
focus throughout) grouped by provider/family, one header per group:
OpenRouter, `Claude Code subscription` (shown only when `claude auth status`
reports an actual claude.ai login -- never on a box whose `claude` is only
logged in via, say, a Databricks work box's own settings), and
`Databricks (<family>)` per family, each row showing which gateway path
type it resolves to (see [DATABRICKS.md](DATABRICKS.md)) alongside the
ctx/output/price columns above; non-chat endpoints (embeddings/whisper) are
hidden entirely. `halo init`'s own per-provider and final
cross-provider model pickers (see [COMMANDS.md](COMMANDS.md)'s `init`
section) use this exact same list widget and row format. See
[DATABRICKS.md](DATABRICKS.md) for exactly how a Databricks model
reference resolves to a URL, and [COMMANDS.md](COMMANDS.md) for
`halo models`'s own flags.
