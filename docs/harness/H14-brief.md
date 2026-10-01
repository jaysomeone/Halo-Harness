# H14 brief — Databricks work-config parity (zero-setup at work from Claude Code's settings)

Facts from the work box (2026-09-29): Claude Code there runs from `~/.claude/settings.json` whose
`env` block sets `ANTHROPIC_BASE_URL=https://<workspace>/ai-gateway/anthropic`,
`ANTHROPIC_AUTH_TOKEN=<dapi… PAT>`, `ANTHROPIC_MODEL=databricks-claude-opus-4-6`,
`ANTHROPIC_DEFAULT_OPUS_MODEL=databricks-claude-opus-4-6`, `ANTHROPIC_DEFAULT_SONNET_MODEL=
databricks-claude-opus-5`, `ANTHROPIC_DEFAULT_HAIKU_MODEL=databricks-claude-opus-4-8`,
`ANTHROPIC_CUSTOM_HEADERS="x-databricks-use-coding-agent-mode: true"`, plus `model: opus`,
`effortLevel: xhigh`, `modelSettings.<id>.effortLevel`, `permissions.defaultMode: default`. The
workspace serves the endpoints listed in the vendored Databricks catalog (H13): Databricks-hosted
Claude foundation endpoints (`databricks-claude-*`), Bedrock external Claude chat endpoints
(`us-anthropic-claude-*`), DeepSeek/Kimi/GLM/Qwen/GPT/Grok/Gemini/Llama foundation endpoints. Each
endpoint's JSON carries `foundation_model.api_types` (e.g. `mlflow/v1/chat/completions`,
`openai/v1/responses`, `codex/v1/responses`, and for Claude `anthropic/v1/messages`) and
`ai_gateway_v2_supported`. The workspace is behind an IP access list (403 from other addresses).
NEVER write the workspace hostname into the repo: fixtures use `your-workspace.cloud.databricks.com`.

Verified gaps (Fable, simulated under a temp HOME with that exact settings shape and a fake token):
1. `resolve_databricks()` keeps the gateway path inside `host` (`…/ai-gateway/anthropic`), so
   `doctor --work` lists endpoints at `…/ai-gateway/anthropic/api/2.0/serving-endpoints` and
   misreads the failure as "token may only run inference".
2. `ANTHROPIC_CUSTOM_HEADERS` from the settings env does not surface in the resolved config
   (`extract_custom_headers` exists but the value is not carried on `DbxConfig`).
3. Bare `opus|sonnet|haiku` resolve to `cc:` and the default model stays the OpenRouter default;
   at work Claude Code maps them through `ANTHROPIC_DEFAULT_*_MODEL` and `ANTHROPIC_MODEL`.

## Scope
A. **Host derivation**: split `ANTHROPIC_BASE_URL` into workspace root + gateway path; `host` =
   root; remember `anthropic_gateway = <root>/ai-gateway/anthropic` when present. Same for
   `DATABRICKS_HOST` values that include a path. All API calls (`/api/2.0/serving-endpoints`,
   `/serving-endpoints/<name>/invocations`, `/ai-gateway/...`) build from the root.
B. **Headers**: `ANTHROPIC_CUSTOM_HEADERS` (one or more `Name: value` lines) parsed into
   `DbxConfig.custom_headers` and sent on EVERY Databricks request (native passthrough and chat),
   merged under explicit `--header`/config headers; `x-databricks-use-coding-agent-mode` is also
   sent by default on the anthropic gateway when the env does not set it.
C. **Model defaults and aliases at work**: when the resolved Databricks config comes from Claude
   Code's settings env (base URL or `DATABRICKS_HOST`), the default model (after `--model`,
   `BRIDGE_MODEL`/routes.json and config.json) is `dbx:<ANTHROPIC_MODEL>`; bare `opus`/`sonnet`/
   `haiku` resolve to `dbx:<ANTHROPIC_DEFAULT_*_MODEL>` (falling back to the Databricks Claude
   endpoints `databricks-claude-opus-5-5` / `-sonnet-5-5` / `-haiku-4-5` when unset); the `cc:`
   resolution for bare names applies only when no Databricks env is active and a claude.ai login
   exists. `effortLevel` and `modelSettings.<id>.effortLevel` from settings become the default
   effort for that model (already partly wired — verify and test with this exact file).
D. **Gateway path per endpoint from `api_types`**: the endpoint listing is cached with each
   endpoint's `api_types`, `endpoint_type`, `task` and capabilities; route candidates are ordered
   from it: `anthropic/v1/messages` → native passthrough at `<root>/ai-gateway/anthropic/v1/
   messages` (model = endpoint name); `mlflow/v1/chat/completions` → `<root>/ai-gateway/mlflow/v1/
   chat/completions` with `model: system.ai.<name>` (or `<name>` per the existing rule);
   otherwise `<root>/serving-endpoints/<name>/invocations`; EXTERNAL_MODEL endpoints → invocations
   chat; non-chat tasks refused with a clear message. Unknown endpoint (no cache) → today's
   candidate order with the route cache. This closes the plan's "route split" question by data.
E. **doctor --work**: prints the derived root, the gateway path, the header names (never values),
   the default model and effort, the token source; distinguishes 401 (bad token), 403 with the IP
   access-list message (connect to the VPN), 403 without it (token lacks list permission — the
   inference probe still runs), and wrong-path 404; `init --preset work` detects "configured via
   Claude Code settings" and skips host/token prompts.
F. Tests: a fixture `settings.json` in exactly the work shape (synthetic token, placeholder host),
   host/gateway split, header parsing (single and multi-line), alias and default resolution in
   both environments (Databricks env present vs. claude.ai login only), api_types-driven candidate
   order for each endpoint class with the vendored catalog, doctor --work outputs for 401/403-IP/
   403-other/404 mocks, `init --preset work` skip path, byte-identical requests otherwise.
G. CHANGELOG, README "Databricks at work" section (what is read from Claude Code's settings, what
   never leaves the box), `__version__` bump.

Report ≤ 50 lines. Rules as in the other briefs; no hostnames, no tokens, ≤ 250 lines per write,
suites green on Windows, WSL and the Kali VM, no commits.

## Verified catalog api_types (from the workspace listing, 2026-09-29) — the source of truth for D
Per endpoint the gateway model id is `foundation_model.name` (NOT derivable by prefixing the
endpoint name); the anthropic gateway takes the ENDPOINT name as `model`. Families:
- Claude foundation (`databricks-claude-opus-4-1|4-5|4-6|4-7|4-8|5|5-5`, `databricks-claude-sonnet-4|
  4-5|4-6|5|5-5`, `databricks-claude-haiku-4-5`): `mlflow/v1/chat/completions`, `anthropic/v1/messages`
  (model = endpoint name), `cursor/v1/chat/completions`, `mlflow/v1/responses`. Default: anthropic.
- GLM (`databricks-glm-5-3`, `-5-3-flash`, `-5-2`) and Kimi (`databricks-kimi-k3`): `mlflow/v1/chat/
  completions`, `mlflow/v1/responses`, `anthropic/v1/messages`, `codex/v1/responses`. Default: mlflow
  chat (the tested path); `anthropic` selectable per model (`databricks.gateway.<endpoint>: anthropic`
  or a `dbx:<endpoint>@anthropic` suffix) — the work matrix compares both.
- DeepSeek (`databricks-deepseek-v4-1-flash`, `-v4-pro-0813`, `-v4-flash-0731`), Qwen
  (`databricks-qwen35-122b-a10b` → `system.ai.qwen35-122b-a10b`, `databricks-qwen3-next-80b-a3b-instruct`
  → `system.ai.qwen3-next-80b-a3b-instruct`), Llama (`databricks-llama-4-maverick` → `system.ai.llama-4-
  maverick`, `databricks-meta-llama-3-1-8b-instruct` → `system.ai.meta_llama_v3_1_8b_instruct`,
  `databricks-meta-llama-3-3-70b-instruct` → `system.ai.llama_v3_3_70b_instruct`), Gemma
  (`databricks-gemma-3-12b` → `system.ai.gemma-3-12b-it`), gpt-oss (`databricks-gpt-oss-120b` →
  `system.ai.gpt-oss-120b`, `-20b` → `system.ai.gpt-oss-20b`), Inkling: `mlflow/v1/chat/completions` +
  `mlflow/v1/responses` only. Default: mlflow chat.
- GPT (`databricks-gpt-5`, `-5-1`, `-5-2`, `-5-4`, `-5-4-mini`, `-5-4-nano`, `-5-5`, `-5-6-sol|luna|terra`,
  `-6-sol|luna|astra`, `-5-mini`, `-5-nano`): `mlflow/v1/chat/completions`, `openai/v1/responses`,
  `cursor/v1/chat/completions`, `mlflow/v1/responses`, `codex/v1/responses`. EXCEPTION
  `databricks-gpt-5-5-pro`: NO mlflow chat — `openai/v1/responses`, `cursor/v1/chat/completions`,
  `mlflow/v1/responses`, `codex/v1/responses` → use `cursor/v1/chat/completions` (chat-shaped), then
  invocations. Grok (`-4-6`, `-4-7`): `mlflow/v1/chat/completions`, `openai/v1/responses`.
- Gemini (`databricks-gemini-2-5-flash|pro`, `-3-1-flash-lite`, `-3-5|3-7|3-8-flash`): `mlflow/v1/chat/
  completions`, `gemini/v1/generateContent`, `gemini/v1/streamGenerateContent`, `cursor/v1/chat/
  completions`, `mlflow/v1/responses`. Default: mlflow chat.
- Bedrock EXTERNAL Claude (`claude-3-5-sonnet-20240620-v1-0`, `us-anthropic-claude-*`): invocations
  ONLY (chat format, no model field).
- Non-chat (hide): `databricks-gte-large-en`, `databricks-bge-large-en` (`mlflow/v1/embeddings`),
  `databricks-qwen3-embedding-0-6b`, `titan-embed-text-v1|v2-0`, `sandbox_whisper_large_v3`.
Candidate order per endpoint = [family default] → other chat-shaped types it lists (`mlflow` chat,
`cursor` chat) → `/serving-endpoints/<name>/invocations`; never a type the endpoint does not list.

## H. `doctor --work --probe-all` (the work matrix)
Sends one short pong through EVERY chat endpoint on its chosen path (and, for GLM/Kimi/Claude, on
the anthropic gateway too when `--both`), prints a table: endpoint, path, HTTP status, first tokens,
latency, DBUs, tool-call support (one Read tool call when `--tools`), and writes
`~/.halo/work-matrix-<date>.json` (no token, no host in the report — endpoint names only) so
the owner can paste it back. Costs are shown up front; `--only <glob>` narrows the set.

## I. Team preset + token-only onboarding (rolo, 2026-09-29: "the team just inserts their databricks
token and they're off")
- The workspace listing is the model list: `init --preset work` and `models --refresh` discover and
  cache it (`~/.halo/dbx-endpoints.json` with api_types, ids, capabilities, DBU prices); the
  H13 vendored snapshot is only the offline fallback and picker seed. Never a hand-maintained list.
- A shared `team.json` preset (host, default model, per-family gateway preference, DBU price,
  optional role table for H15) found at `.halo/team.json` in the project, `~/.halo/
  team.json`, or `--team <path|url>`; it never holds tokens. `init --preset work` reads it (or the
  Claude Code work settings env), asks ONLY for the user's token (hidden input, 0600 env file),
  refreshes the catalog, and prints the count of models available and the default. Re-running
  reports state. `/model` groups by family, shows DBUs and the path type, hides non-chat endpoints.
- Docs: a "Databricks at work — team setup" README section and a sample `team.example.json`
  (placeholder host).

## Later (H15, after the work matrix): roles
Role table in the preset (`orchestrator`, `coder`, `reviewer`, `researcher`, `small`), built-in
agents wired to roles, cost-aware defaults, `/roles` view, `--role` overrides; decided by the matrix.

## J. `/models refresh` (alias `/dbx`) and `models --refresh --urls` (rolo, 2026-09-29)
- TUI `/models refresh`: re-list the workspace with the user's token, print the table (endpoint,
  family, chosen path type, gateway model id, DBU in/out, capabilities flags), update the cached
  catalog (`~/.halo/dbx-endpoints.json`) so `/model` reflects it immediately, and print a
  diff against the previous cache (added / removed / changed types or ids). Runs off the UI thread.
- CLI `halo models --refresh --urls`: same table plus the exact URL per endpoint and path
  type (what the listing script printed), `--json` for tooling.
- Auto-refresh: catalog older than 24 h (configurable `databricks.catalog_max_age_hours`) refreshes
  in the background when `/model` opens or a session starts, with a one-line notification of the
  diff; failures (offline, 403 IP list) keep the cache and say so. Never touches Claude Code's files.
- Tests: refresh diff on fixture listings (added/removed/changed), stale-cache trigger, offline
  keeps cache, `--urls` output shape, TUI pilot for `/models refresh`.

## Correction (rolo, 2026-09-29): discovery only — no workspace endpoint list in the repo
Host + token are the only inputs. `init --preset work` / `models --refresh` discover the endpoints
and cache them per user (`~/.halo/dbx-endpoints.json`); that cache is the offline fallback.
The repo ships only the generic family/api_type RULES table (H13 correction) and synthetic test
fixtures. Wherever this brief says "vendored snapshot" read "per-user cache". `team.json` (§I) holds
host, defaults and preferences only — never endpoint lists, never tokens.
