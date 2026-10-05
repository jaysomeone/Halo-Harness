"""halo_harness.providers.catalog -- vendored, packaged fallback model
catalogs (H8 scope C). Shipped INSIDE the package (unlike `~/.halo/
models.json`/`dbx-endpoints.json`, which are live caches under the user's
state dir) so a fresh install with no network yet -- most notably the
Databricks work box, behind a VPN that may not be up yet -- still resolves
a `ModelProfile` (context/output/pricing/vision) for the models this
harness ships pinned defaults for, instead of falling all the way back to
generic guessed defaults.

Three files, all small, all real data (fetched live and trimmed, see
`halo_harness/providers/models_dev.py`'s own module docstring for how to
regenerate them):
  - `openrouter_fallback.json` -- OpenRouter's own `/api/v1/models` shape
    (same as `providers.databricks.write_models_json`'s own output),
    covering every OpenRouter model id `model_table.json` names
    (2026-09-24).
  - `models_dev_databricks_fallback.json` -- models.dev's `databricks`
    provider entry (`{id: {limit:{context,output}, cost:{...},
    modalities:{...}, reasoning, tool_call, ...}}`), trimmed down to just
    the rows `model_table.json` references (2026-09-24) -- the ONE
    models.dev provider whose model ids are directly Databricks serving-
    endpoint names.
  - `models_dev_openai_fallback.json` -- models.dev's `openai` provider
    entry, same shape, ALL 53 ids (2026-10-04, round 5i part 1) -- unlike
    the Databricks file, never trimmed against `model_table.json` (the
    `oai:` route has no rows there at all; its profile comes entirely
    from this file plus the generic openai-chat-dialect fallback). Every
    other models.dev provider is a cross-check/reference source for
    `model_table.json`, not a live fallback path (their model ids don't
    match this harness's own OpenRouter/Databricks/OpenAI routing).

Never mutated at runtime -- only `tools/ingest_model_table.py`-style
regeneration (a human/CI step) rewrites these files.
"""
