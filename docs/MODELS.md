# Models

How a `--model`/`/model` string resolves to a real upstream call, per-family
request-shaping rules, the model catalogs and how they refresh, and pricing.
Verified against `rolo_claude/model.py`, `providers/profiles.py`,
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

**The Edit-tool context hint**: a DeepSeek/Kimi/GLM/Qwen/MiniMax-family
session's Edit tool description gets one extra sentence asking for at least
three lines of `old_string` context (computed once per session, so the
wire request stays cache-prefix-stable) -- added after telemetry showed
these families' models under-quote `old_string` and hit "Found multiple
matches" far more often than Claude/GPT do. A specific `(provider,
model_id)` row's own `edit_hint` key (including an explicit empty string)
overrides the family default for just that one model.

## Reasoning effort

`--effort`/`/model`'s effort, or `settings.json`'s `effortLevel`/
`modelSettings.<id>.effortLevel` when `--effort` is omitted, maps through
`providers/profiles.py::map_effort` to whatever field the resolved profile
actually needs: a `thinking.budget_tokens` value (4096/10000/24000/32000/
32000 for low/medium/high/xhigh/max) on the native Anthropic dialect,
`reasoning.effort` on OpenRouter, or `reasoning_effort` on a Databricks chat
endpoint. A profile with `reasoning_no_disable: true` (GLM-5.3/5.3-Flash --
`thinking.type: disabled` is a hard 400 on that family) forces an
explicitly-disabling effort value back up to that row's own default instead
of ever sending `disabled`.

## The model table, catalogs, and refresh

Three cached files under `~/.rolo-claude/` back model-profile resolution,
consulted in this order before falling back to a hardcoded guess
(`model.py::resolve_model_profile`):

1. **`models.json`** (OpenRouter) / **`dbx-endpoints.json`** (Databricks) --
   the live-probed catalog (`rolo-claude models --refresh`; see
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
has an entry for these at all. `rolo-claude models --cc --refresh` re-pings
each of the nine alias names once to confirm the exact ids
`sonnet`/`haiku` currently resolve to (the two whose target moves as
Anthropic ships new releases) and caches the result to
`~/.rolo-claude/cc-models.json`, consulted before the static table.

`models-dev.json` (models.dev's own public, unauthenticated `api.json`) is
fetched by `rolo-claude models --refresh` regardless of which providers are
configured -- it backs the Databricks vendored-fallback tier and
`doctor`'s cache-freshness check.

## Pricing and DBUs

OpenRouter reports real per-token USD pricing in `models.json`
(`price_in`/`price_out`/`price_cache_read`/`price_cache_write`); when a
response itself doesn't carry a `usage.cost` field, `CostMeter` derives one
from those rates (input × price_in + (output + reasoning) × price_out +
cache_read/cache_write at their own rates, falling back to `price_in` for
cache tokens when no specific rate is known). **Databricks never reports
cost** -- `stats`/`/cost` always show `n/a` for a Databricks turn; there is
no per-turn billing field on the wire to read, by design (`CostMeter.
add_usage` treats `provider == "databricks"` as unknown-cost
unconditionally, whatever pricing config exists). Where
`databricks.dbu_price_usd` (`~/.rolo-claude/config.json`, or a team.json's
`dbu_price_usd`) actually applies is narrower: it converts an endpoint's own
**catalog-advertised** DBU rate (`usage_policy.output_dbu_per_1k_tokens`,
when the workspace publishes one) into a dollar figure for the `/model`
picker's informational per-endpoint display (`Controller.list_models()`'s
`dbu` column) -- a reference price, never a real per-turn spend. A
`cc:` row's "cost" is Claude Code's own cumulative `total_cost_usd`,
delta'd since that subprocess's previous turn -- an estimate, never real
per-token billing, and never counted toward `--max-budget-usd`.

## The `/model` picker

Opening `/model` with no argument triggers a background refresh of the
Databricks catalog if it's older than `databricks.catalog_max_age_hours`
(default 24h) -- the picker itself opens immediately with whatever's
already cached; the refresh only ever notifies afterward if something
changed. Databricks endpoints are grouped by family in the picker, each
showing which gateway path type it resolves to, with non-chat endpoints
(embeddings/whisper) hidden entirely. See [DATABRICKS.md](DATABRICKS.md)
for exactly how a Databricks model reference resolves to a URL, and
[COMMANDS.md](COMMANDS.md) for `rolo-claude models`'s own flags.
