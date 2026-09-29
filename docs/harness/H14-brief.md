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
