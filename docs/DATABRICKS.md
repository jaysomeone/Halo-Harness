# Databricks

Zero-setup reuse of a Claude-Code-configured work box, workspace discovery
(never a vendored endpoint list), how a model reference resolves to a real
URL, team onboarding, and troubleshooting by HTTP status. Verified against
`providers/config.py`, `providers/dbx_routing.py`, `providers/databricks.py`,
`team_config.py`, `init_cli.py`, and `doctor.py`. See
[MODELS.md](MODELS.md) for model-reference forms in general and
[COMMANDS.md](COMMANDS.md) for `init`/`doctor`/`models`'s own flags.

## The two-input setup: host + token

Everything else is derived. `providers/config.py::resolve_databricks` tries,
in order, and never raises:

1. `HALO_DBX_BASE_URL` + `HALO_DBX_TOKEN` (legacy `BRIDGE_DBX_BASE_URL`/
   `BRIDGE_DBX_TOKEN` still honoured) -- an explicit override, wins
   outright, unfiltered.
2. `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` from the process
   environment -- **only** when the host looks like a Databricks domain
   (`*.cloud.databricks.com`/`*.azuredatabricks.net`/`*.gcp.databricks.com`,
   never loopback) -- these are Claude Code's own work-box env var names.
3. The same pair, from the **settings `env` chain**
   (`~/.claude/settings.json` etc.) -- this is the "zero setup at work"
   path: a box already running Claude Code against Databricks needs no
   separate halo configuration at all.
4. `DATABRICKS_HOST` + `DATABRICKS_TOKEN` -- unambiguous by name, no host
   filter, checked against both the process environment and the settings
   chain per-field.
5. `~/.claude/ucode-settings.json` -- written by Databricks' own `ug`
   (unity-gateway) CLI when it's already been run on this box; also accepts
   a Claude-Code-`--settings`-shaped file (an `env` block, or a top-level
   `apiKeyHelper` command to run for the token).
6. `~/.databrickscfg`'s `[DEFAULT]` section -- a generic, possibly stale
   fallback.

**Host/gateway split**: whatever host resolves above is always split into
the bare workspace root (every API call is built from this) plus, when the
original value carried a `/ai-gateway/anthropic[/v1[/messages]]` suffix
(Claude Code's own `ANTHROPIC_BASE_URL` shape), that gateway URL
remembered separately -- `derive_workspace_root()` strips the suffix either
way, so scope D's routing table can always build the gateway URL from the
bare root regardless of which form the original config used.

**Custom headers**: `ANTHROPIC_CUSTOM_HEADERS` (one-or-more `Name: value`
lines, read from the same env-then-settings-chain order as above) is parsed
and merged onto every Databricks request, on top of the default
`x-databricks-use-coding-agent-mode: true` -- an explicit override (from
the env, or a future `--header` flag) always wins by header name,
case-insensitively.

**Nothing about the real workspace ever leaves the box**: the hostname and
token live only in the local env file (or wherever Claude Code's own
settings already put them) and are never written into a session log, a
cache file, or printed by `doctor` -- `doctor --work` shows header **names**
only, never values, and the workspace root itself is the only thing echoed
back.

## `halo init --provider databricks`

(the deprecated `init --preset work` still works, as a one-line-noticed
alias)

With a host already known (from Claude Code's own settings env, or a
team.json -- see below), asks **only** for the personal Databricks token
(hidden input), writes `DATABRICKS_HOST`/`DATABRICKS_TOKEN` to the env file
(mode 0600), refreshes the catalog, and prints how many endpoints are
available and the default model. With no host known at all, it prompts for
both. See `docs/COMMANDS.md`'s `init` section for the full flag table and a
worked example.

## The team preset (`team.json`)

A shared, checked-into-the-project (or personal) preset -- `host`,
`default_model`, a per-family `gateway_preference` map, and `dbu_price_usd`
-- **never a token, never an endpoint list** (`team_config.py`).
Discovered at `<cwd>/.halo/team.json`, then `~/.halo/team.json`,
then an explicit `--team <path|url>` (which always wins outright). Any key
that isn't one of those four, or that merely *looks* like a credential
(matches `token`/`secret`/`key`/`password`/`credential`/`auth` anywhere in
its own name), is dropped with a loud warning rather than silently
accepted -- team.json is meant to be committed/shared, so this is a hard
guard against someone accidentally checking in a real token. See
`team.example.json` for the shape.

```json
{
  "host": "https://your-workspace.cloud.databricks.com",
  "default_model": "dbx:databricks-claude-opus-4-6",
  "gateway_preference": {"databricks-kimi-k3": "anthropic"},
  "dbu_price_usd": 0.07
}
```

## Discovery and cache: `dbx-endpoints.json`

`halo models --refresh` (or `init --provider databricks`, the
deprecated `init --preset work` alias) lists the workspace's own serving
endpoints (`GET /api/2.0/serving-endpoints`) and caches them, per user, to
`~/.halo/dbx-endpoints.json`: name, `api_types`, `endpoint_type`,
`task`, `foundation_model.name`, `model_class`, and any `usage_policy` DBU
rate -- **the only source of truth for what a workspace serves.** Nothing
about routing is ever hand-maintained per model name. "Chat-capable" (the
`/models`/`/model` table's own `chat` column, and `init`'s own "N
chat-capable" catalog summary) always means `task == "llm/v1/chat"` --
never a name-based family guess, which can legitimately disagree with the
endpoint's own advertised task.

**Old-cache migration**: a `dbx-endpoints.json` written before this
milestone never recorded `api_types` at all -- read against today's route
logic, that empty field looks identical to "the platform reports zero
api_types for this endpoint" and silently degrades every openai-chat-
dialect family's route to `invocations` (never the real `mlflow`/`cursor`
path), while `init`'s own "chat-capable" count (which used to check
`bool(api_types)` instead of `task`) read that same empty field as "0
chat-capable" -- the table and the summary visibly disagreeing on the
exact same rows. Detected on the cache's own shape (no `api_types` key
present on ANY entry, not merely an empty list -- a real endpoint can
legitimately have no api_types) and refreshed automatically the next time
the existing background refresh triggers (session start, opening
`/model`); a bare `/models`/`halo models` (which never touches the
network on its own) shows `path unknown (refresh needed)` for every row
until that happens, rather than the old silently-wrong `invocations`
guess.

## Gateway paths per endpoint type and family

`providers/dbx_routing.py::classify_family` turns an endpoint's own name
(and, once cached, its `foundation_model_name`/`model_class`) into one of:
`claude_foundation`, `claude_external` (Bedrock), `glm`, `kimi`, `deepseek`,
`qwen`, `llama`, `gemma`, `gpt_oss`, `gpt`, `gpt_pro`, `grok`, `gemini`,
`inkling`, `non_chat`, or `unknown`. That family decides the **dialect**
first (`resolve_databricks_dialect`):

- **`claude_foundation`** (an endpoint named `databricks-claude-*`, or any
  other Claude-named endpoint not matching the Bedrock pattern below) ->
  **native Anthropic Messages passthrough** by default
  (`/ai-gateway/anthropic/v1/messages`).
- **`claude_external`** (Bedrock, matched narrowly by AWS's own dated id
  shape -- `us-anthropic-claude-*`, `claude-3-5-sonnet-20240620-v1-0`) ->
  **invocations-only**, despite "claude" being in the name -- these are
  never the native passthrough.
- Every other family -> **openai-chat** dialect by default.

A `dbx:<endpoint>@anthropic` suffix (one call) or
`databricks.gateway.<endpoint>: anthropic` in `~/.halo/config.json`
(standing, per-model) opts a GLM/Kimi endpoint into the native gateway
instead of its openai-chat default. A known non-chat endpoint (embeddings/
whisper, by name-hint or `model_class`) is refused with a clear message
before ever building a request, and hidden from `/model`.

Once the dialect is openai-chat, `chat_route_candidates` picks an **ordered
list of sub-paths to try**, filtered to whichever ones the endpoint's own
discovered `api_types` actually list (never a type the endpoint doesn't
advertise):

| Family | Order | Wire `model` value |
|---|---|---|
| `glm`, `kimi` | `mlflow` chat, then `anthropic` | the endpoint's own `foundation_model.name` |
| `deepseek`, `qwen`, `llama`, `gemma`, `gpt_oss`, `inkling` | `mlflow` chat only | the endpoint's own `foundation_model.name` (never a guessed `system.ai.` prefix) |
| `gpt` | `mlflow` chat, then `cursor` chat | as above |
| `gpt_pro` (`databricks-gpt-5-5-pro`) | `cursor` chat only -- no mlflow chat exists for it | as above |
| `grok`, `gemini` | `mlflow` chat (`gemini` also tries `cursor`) | as above |
| `claude_external` | (openai-chat dialect never applies) | -- |
| unknown family, or an endpoint not yet in the cache | the pre-discovery static order | the bare requested name |

Every candidate always falls back, last, to plain
`/serving-endpoints/<name>/invocations`. `halo models --refresh
--urls` (and `doctor --work --probe-all`) print the exact URL and path-type
label (`mlflow`/`cursor`/`anthropic`/`invocations`) each configured model
actually resolves to right now; the SAME default path (this table's own
"Order" column, first entry) is what `halo models`'s bare table and
the `/model`/init pickers show WITHOUT `--urls` and without any network
call -- it's derived purely from the cached `api_types` + this family
table, never a live probe.

## Context, output and pricing columns

`/model`, `/models`, `halo models`, and `init`'s own per-provider
picker all show the same three columns for a Databricks row -- context
window, max output, and USD-per-million-token input/output prices -- from
one ordered rule (`model_display.databricks_row_fields`):

1. The [models.dev](https://models.dev) `databricks` provider's own entry
   whose id EQUALS the endpoint name (`~/.halo/models-dev.json` if
   cached, else the package-vendored fallback) -- `limit.context`/
   `limit.output` for context/output, `cost.input`/`cost.output` (already
   USD per million tokens, models.dev's own unit) for the prices.
2. Missing that: `model_table.json`'s own `context_tokens` for this
   endpoint (request-shaping data, not economics) -- output and prices stay
   blank.
3. Neither: every field blank -- **never a bare `?`** (a blank field just
   keeps its column's width, so the row never wraps or misaligns).

A Bedrock EXTERNAL Claude endpoint (`us-anthropic-claude-*`) follows this
exact same rule -- today that means context blank and no price, since
models.dev's `databricks` provider doesn't list it under its own endpoint
name.

## Keeping the catalog fresh

`/models refresh` (alias `/dbx`, off the UI thread) and `halo models
--refresh` re-list the workspace endpoints AND re-fetch models.dev's own
pricing/context data, reporting a one-line added/removed/changed diff for
the endpoint list (a models.dev fetch failure is reported separately, on
its own line, and never rolls back the endpoint list refresh that already
succeeded). The cache auto-refreshes (silently, for an already-cached
catalog only -- never a first discovery, and always for an OLD-SHAPE cache
regardless of age, see above) on session start, and opening `/model`
refreshes it in the background whenever it's older than
`databricks.catalog_max_age_hours` (default 24h). A refresh that fails
(offline, or 403 from the IP access list) just keeps the existing cache and
says so -- an offline/stale-cache work box is never treated as "broken."

## `doctor --work`

Prints the derived workspace root, the anthropic-gateway URL, the header
**names** it will send (never values), the resolved default model and
effort, and which of the six discovery steps above actually supplied the
token. Two live checks:

- **VPN/reachability**: a real TCP+TLS connect to the resolved host (the
  identical connect path a real request uses).
- **Token validity**: `GET /api/2.0/serving-endpoints`, classified by
  status:

| Status | Meaning |
|---|---|
| `200` | token is valid and can list endpoints |
| `401` | bad token |
| `403`, Databricks' own IP-access-list wording in the body | off the VPN -- "connect to the VPN" |
| `403`, without that wording | token lacks permission to list endpoints; inference may still work |
| `404` | wrong path -- the derived workspace root is probably incorrect |
| anything else | reported as-is |

A host with no token configured yet still probes (a 401 without a token
still proves the host is reachable) rather than skipping the network call.

## `doctor --work --probe-all`

The full work matrix: one short "pong" through **every** chat-shaped
endpoint the cached catalog knows about, on its own chosen path
(`--both` also probes the anthropic gateway for Claude/GLM/Kimi endpoints
that don't already use it by default; `--tools` adds a one-tool-call check
per endpoint; `--only <glob>` filters by endpoint name). Prints a table of
status/latency/output tokens/tool-call support and writes
`~/.halo/work-matrix-<date>.json` -- **endpoint names only, never a
host or a token** -- safe to paste back for review. This spends real
tokens/DBUs against the real workspace; the command says so before running.

V2a closes the plan's own two open questions as two extra fields in that
same JSON report, per endpoint:

- **`cached_path_type`** vs. **`path_type`**: `cached_path_type` is whatever
  `~/.halo/routes-cache.json` already named for this endpoint
  *before* this run; `path_type` is what this run actually used (and just
  re-cached). The two differing is a real **route split** -- a stale cached
  candidate that no longer answers, most often right after a family's own
  `api_types` change without a catalog refresh in between.
- **`reasoning_replay_ok`** (only when `--tools` produced a tool call to
  replay; `None`/absent otherwise): a **second** turn, built through the
  exact same `providers.request.build_request_body`/
  `build_anthropic_request_body` a live session uses -- replaying whatever
  reasoning/thinking the first turn produced, plus the tool result -- is
  sent for real, and whether the endpoint accepted it is recorded here.
  This is what answers "does reasoning replay after a tool call actually
  work for this family" from live data instead of a guess.

## `halo work-matrix show` / `apply`

V2b: matrix-driven fixes tooling over a `doctor --work --probe-all` report
(the owner's own real work-VM run, or one of the two synthetic samples under
`tests/fixtures/work-matrix/`) -- endpoint names only, still never a host or
a token. `work_matrix.py::classify_row` turns each row into one of:

| Row shape | Suggested action | `apply` writes? |
|---|---|---|
| `status == "403"` | "run this on the VPN, then re-probe" | no -- nothing to write |
| non-200, and a `"<name> (anthropic gateway)"` companion row (only present when `--both` probed it) answered 200 | the endpoint's default dialect is the wrong path -- set `databricks.gateway.<endpoint> = anthropic` | **yes** -- the only failure class `apply` ever writes anything for |
| non-200, no working companion to point at | genuinely unknown from this data alone | no -- report it |
| 200, but `tool_call_ok is False` (only meaningful with `--tools`) | a FAMILY-wide rule (`model_table.json`'s `tool_choice_required_supported`/schema simplifier), never a per-endpoint config key | no |
| 200, but `reasoning_replay_ok is False` (only meaningful when `--tools` produced a tool_use to replay) | disable thinking for tool loops on that endpoint -- no existing per-endpoint knob to write | no |
| 200, neither flag False | clean -- not a failure at all | n/a |

`halo work-matrix show <report.json>` prints one line per failing row
(status, issue, suggested action); a report with nothing failing says so
plainly. `halo work-matrix apply <report.json> [--yes]` lists every
`databricks.gateway.<endpoint>` override the report's wrong-path rows imply,
asks for confirmation (`--yes` skips it), then writes them to `~/.halo/
config.json` -- never `~/.claude.json`/`~/.claude/settings.json`. See
`docs/COMMANDS.md`'s own `work-matrix` section for the exact `--help` output.

## How Claude Code's own work settings are reused

At a Databricks work box already running Claude Code, `halo` needs
zero setup beyond installing it: the same `ANTHROPIC_BASE_URL`/
`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_MODEL`/`ANTHROPIC_DEFAULT_*_MODEL`/
`ANTHROPIC_CUSTOM_HEADERS` env block Claude Code itself reads from
`settings.json` resolves the credential (step 3 above), sets the default
model to `dbx:<ANTHROPIC_MODEL>`, and maps bare `opus`/`sonnet`/`haiku`
through `ANTHROPIC_DEFAULT_*_MODEL` (falling back to a pinned
`databricks-claude-*` endpoint per tier when a specific var is unset) --
instead of the `cc:`/`ant:` subscription route or the OpenRouter default
those bare names would otherwise resolve to everywhere else. `settings.json`'s
top-level `effortLevel`/`modelSettings.<id>.effortLevel` set the session's
default effort the same way.

## End-to-end team workflow

How a team actually rolls this out, tying together every piece documented
above into one ordered walkthrough:

1. **One person writes `team.json`** (see **The team preset** above) --
   workspace host, a default model, a per-family gateway preference, a DBU
   price, and optionally a shared `roles` table (see
   [ROLES.md](ROLES.md)) -- and commits it to the project as
   `.halo/team.json`, or shares it as a URL for `--team`.
2. **Each teammate runs `halo init --provider databricks`.** With the host
   already known from `team.json` (or from Claude Code's own settings,
   zero-setup), it asks ONLY for a personal Databricks token, seeds
   `gateway_preference`/`roles` into `~/.halo/config.json` (never
   overwriting a value the teammate already set locally), refreshes the
   endpoint catalog, and ends with a live pong.
3. **`halo doctor --work --probe-all --tools`** runs the full work
   matrix against the real workspace -- one short pong (plus a tool-call
   check) per chat-shaped endpoint, on its own chosen path -- and writes
   `~/.halo/work-matrix-<date>.json` (endpoint names only, safe to
   hand to someone else for review).
4. **`halo work-matrix show <that report>`** turns any failing row
   into a suggested action; **`work-matrix apply <report> --yes`** writes
   the one failure class that maps onto a real
   `databricks.gateway.<endpoint>` config override, after listing exactly
   what it's about to write and asking for confirmation.
5. **`/roles` (in-session) or `halo stats --roles`** confirm the
   team's model-per-role choices actually took effect and show what
   sub-agent work is costing per role (see [ROLES.md](ROLES.md)).

Steps 3-5 are also exactly what to re-run after the workspace's own catalog
changes (a new endpoint, a renamed one, a model deprecated) -- none of this
is a one-time setup step.

## Troubleshooting by HTTP status

| Symptom | Cause | Fix |
|---|---|---|
| `doctor --work` reachability check fails outright | off the VPN, or a DNS/firewall issue | connect to the VPN, re-run `doctor --work` |
| Token validity: `401` | the token itself is wrong/expired | get a fresh personal access token, `halo init --provider databricks` again |
| Token validity: `403` with VPN wording | off the VPN (Databricks' IP access list) | connect to the VPN |
| Token validity: `403` without VPN wording | token lacks the "list serving endpoints" permission | ask a workspace admin for a token with that scope; inference may still work even so |
| Token validity: `404` | the derived workspace root is wrong (an unusual gateway path shape) | check `doctor --work`'s own printed "Workspace root" line against what you expect |
| A specific model 400s with an "unknown field" error | that field isn't on `DATABRICKS_BODY_ALLOWLIST` for this gateway | file it as a `model_table.json` gap -- the request builder only ever sends allow-listed fields to Databricks |
| A specific model 400s on `tool_choice: "required"` | that family doesn't support it (`tool_choice_required_supported: false`) | this is expected; the harness already avoids sending it once a row says so |
| `models --refresh` silently keeps stale data | offline, or a 403 from the IP access list | this is by design (never breaks with a stale-but-present cache); connect to the VPN and refresh again |
