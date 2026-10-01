# V2 brief — Databricks first: every served model works like a proper harness for that model

rolo (2026-09-29): "v2 should just be for using models in databricks but work as any harness should
for each specific model in databricks … role delegation (x model is orchestrator and x models are
sub agents) … rename to databricks-claude in v2 … host it to public github too." v3 = broader
features and where models come from beyond Databricks. Repo is PUBLIC now: no hostnames (use
`your-workspace.cloud.databricks.com`), LAN addresses, usernames, home paths or tokens anywhere.

Baseline = the v0.6.0 line after the docs pass (tip `0bf95d9`, rewritten history): run_all 1744,
test_bridge 97, test_tui 53, green on Windows, WSL and the Kali VM. Only one worker on the tree at
a time; no sub-agents that edit files; no commits (Fable verifies, commits, tags).

## Phases (each verified on the three platforms, committed and tagged by Fable)

### V2a — per-family Databricks correctness (mock-verified now, live-confirmed by the work matrix)
For each gateway type the harness already routes to (`docs/DATABRICKS.md`, `providers/
dbx_routing.py`), make the request, stream and result handling exact per family, with a fixture
test matrix family × type driven by `tests/helpers/mock_databricks.py` speaking each dialect:
- `anthropic/v1/messages` (Claude foundation default; GLM and Kimi K3 optional): native Messages
  in and SSE out — thinking blocks + signatures replayed, tool_use/tool_result blocks, cache_control
  where the gateway accepts it, `x-databricks-use-coding-agent-mode` header, Kimi's verbatim ids,
  GLM/Kimi thinking shapes through the gateway (test both with and without thinking).
- `mlflow/v1/chat/completions` (DeepSeek, Qwen, Llama, Gemma, gpt-oss, GPT, Grok, Gemini, GLM/Kimi
  default): OpenAI-style chat with the family's reasoning shape — DeepSeek `reasoning_content`
  replay rules, GPT/Grok/Gemini `openai_reasoning` summary blocks decoded and displayed, no sampling
  params for thinking families, `max_tokens` pre-admitted against OTPM, `model` = the catalog's
  `foundation_model.name`, streaming `stream: true` explicit, 32-tool cap + schema simplifier.
- `cursor/v1/chat/completions` (gpt-5-5-pro): same as mlflow chat; confirm the model id form.
- `/serving-endpoints/<name>/invocations` (Bedrock external Claude, and the universal fallback):
  chat body without `model`, tools supported, streaming shape.
- Usage and cost per type: prompt/completion tokens, cached tokens where reported, DBUs from the
  catalog prices, `databricks.dbu_price_usd` conversion, `/cost` and `stats` rows per endpoint.
- Errors per type: 400 unknown field (strict allowlist per type), 401/403 (IP list), 404 (wrong
  path → next candidate, cached), 413/overflow, 429 with `limit_type`/`retry_after`, 5xx retries,
  `finish_reason: length` with ≤1 token = provider failure (re-route, never retry in place).
- The two open questions as runnable probes in `doctor --work --probe-all --tools`: (1) reasoning
  replay after a tool call per family, (2) route split per endpoint from the cache; the report
  records the answer per endpoint so the harness can flip the family default from data.

### V2b — matrix-driven fixes
`halo work-matrix show <report.json>` renders a matrix JSON (from the owner's work VM run)
as a table with a suggested action per failure (wrong path → set `databricks.gateway.<endpoint>`,
tool-call failure → family rule, thinking replay failure → disable thinking for tool loops on that
endpoint); `work-matrix apply` writes the per-endpoint overrides into `~/.halo/config.json`
after a confirmation. Fable feeds each real report back into fixes + pinning tests.

### V2c — roles (H15)
`team.json` / config `roles`: `orchestrator` (the session model), `coder`, `reviewer`,
`researcher`, `small` (summaries, titles, /improve drafts); built-in agents (`general-purpose`,
`Explore`, `Plan` and new `Coder`, `Reviewer`, `Researcher`) resolve their model from the role
table unless the agent file sets `model:`; `--role name=model` overrides; `/roles` shows the table
with the endpoint, path type and DBU price per role; the Agent tool accepts `role:`; cost-aware
defaults (cheap model for exploration, strong for planning/review) documented, never automatic
beyond the table; sub-agent spend per role in `stats`.

### V2d — the rename (LAST; re-confirm with rolo before starting)
`databricks-claude`: package `databricks_claude`, console script `databricks-claude` with a
`halo` alias kept, state dir `~/.databricks-claude` with a one-time migration from
`~/.halo` (or a fallback read), env var names unchanged, docs and tests regenerated, then
the GitHub repo rename (old URL redirects). Fable raised once that the name carries two other
companies' trademarks and implies an official Databricks project; rolo decided to proceed in v2.

### V2e — docs + release
Docs pass repeated for the new name and commands; CHANGELOG; tag `v2.0.0`; public.

Rules as in every brief; ≤ 250 lines per write; OS-neutral; Linux first; suites green on the
three platforms; `test_docs_hygiene.py` stays green.
