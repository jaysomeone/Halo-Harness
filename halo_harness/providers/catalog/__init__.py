"""halo_harness.providers.catalog -- vendored, packaged fallback model
catalogs (H8 scope C). Shipped INSIDE the package (unlike `~/.halo/
models.json`/`dbx-endpoints.json`, which are live caches under the user's
state dir) so a fresh install with no network yet -- most notably the
Databricks work box, behind a VPN that may not be up yet -- still resolves
a `ModelProfile` (context/output/pricing/vision) for the models this
harness ships pinned defaults for, instead of falling all the way back to
generic guessed defaults.

Two files, both small, both real data (fetched live and trimmed down to
just the rows `halo_harness/providers/model_table.json` references, 2026-09-
24 -- see `halo_harness/providers/models_dev.py`'s own module docstring for
how to regenerate them):
  - `openrouter_fallback.json` -- OpenRouter's own `/api/v1/models` shape
    (same as `providers.databricks.write_models_json`'s own output),
    covering every OpenRouter model id `model_table.json` names.
  - `models_dev_databricks_fallback.json` -- models.dev's `databricks`
    provider entry (`{id: {limit:{context,output}, cost:{...},
    modalities:{...}, reasoning, tool_call, ...}}`), the ONE models.dev
    provider whose model ids are directly Databricks serving-endpoint
    names -- every other models.dev provider is a cross-check/reference
    source for `model_table.json`, not a live fallback path (their model
    ids don't match this harness's own OpenRouter/Databricks routing).

Never mutated at runtime -- only `tools/ingest_model_table.py`-style
regeneration (a human/CI step) rewrites these files.
"""
