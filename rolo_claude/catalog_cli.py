"""rolo_claude.catalog_cli -- `rolo-claude models` subcommand (H1 scope I):
lists OpenRouter models (from the cached models.json, refreshing via a live
probe when the cache is empty or --refresh is passed) and Databricks
endpoints (from dbx-endpoints.json), with context/output/price columns, and
near-miss slug correction for a `--model` that doesn't match anything.
"""

from __future__ import annotations

import argparse
import difflib
import sys

from pathlib import Path

from rolo_claude.config.paths import bridge_home, home
from rolo_claude.providers.config import load_env_file, resolve_databricks, resolve_openrouter, derive_workspace_root
from rolo_claude.providers.databricks import (
    load_dbx_endpoints_json, load_models_json, probe_databricks_endpoints_full,
    probe_openrouter_models, write_dbx_endpoints_json, write_models_json,
)
from rolo_claude.providers.models_dev import fetch_models_dev, models_dev_json_path, write_models_dev_json


def _fmt_price(v) -> str:
    """USD per token -> "$X.XX/M" (per-million-token, the unit every
    provider's own pricing page uses); "?" for anything unparseable."""
    try:
        return f"${float(v) * 1_000_000:.2f}/M"
    except (TypeError, ValueError):
        return "?"


def near_miss_slug(requested: str, known_ids) -> "list[str]":
    """Ori-style near-miss slug correction (scope I/rule 11): the closest
    known model ids to an unrecognized `--model`, best match first."""
    return difflib.get_close_matches(requested, list(known_ids), n=5, cutoff=0.5)


def cmd_models(argv) -> int:
    parser = argparse.ArgumentParser(prog="rolo-claude models", add_help=True)
    parser.add_argument("--refresh", action="store_true", help="Re-probe OpenRouter/Databricks instead of using the cache")
    args = parser.parse_args(argv)

    import os
    load_env_file(Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env")))

    state_dir = bridge_home()
    models = load_models_json(state_dir)
    if args.refresh or not models:
        orc = resolve_openrouter()
        if orc is not None:
            try:
                fetched = probe_openrouter_models(orc.base_url, orc.api_key)
                write_models_json(state_dir, fetched)
                models = load_models_json(state_dir)
            except Exception as e:
                print(f"rolo-claude models: could not refresh from OpenRouter: {e}", file=sys.stderr)

    print("OpenRouter models (models.json):")
    print(f"{'id':<48} {'context':>10} {'max_out':>10} {'in/M':>10} {'out/M':>10}")
    for mid in sorted(models):
        entry = models[mid]
        pricing = entry.get("pricing") or {}
        print(f"{mid:<48} {str(entry.get('context_length', '?')):>10} {str(entry.get('max_output_tokens', '?')):>10} "
              f"{_fmt_price(pricing.get('prompt')):>10} {_fmt_price(pricing.get('completion')):>10}")

    dbx = resolve_databricks()
    if dbx is not None:
        endpoints = load_dbx_endpoints_json(state_dir)
        if args.refresh or not endpoints:
            try:
                root = derive_workspace_root(dbx.host)
                status, fetched = probe_databricks_endpoints_full(root, dbx.token)
                if status == 200:
                    write_dbx_endpoints_json(state_dir, fetched)
                    endpoints = load_dbx_endpoints_json(state_dir)
            except Exception as e:
                print(f"rolo-claude models: could not refresh from Databricks: {e}", file=sys.stderr)
        if endpoints:
            print("\nDatabricks endpoints (dbx-endpoints.json):")
            print(f"{'name':<48} {'task':<20} {'ready':>6}")
            for name in sorted(endpoints):
                e = endpoints[name]
                print(f"{name:<48} {str(e.get('task', '?')):<20} {str(e.get('ready', '?')):>6}")

    # H8 scope C: models.dev's api.json is public/unauthenticated -- fetched
    # regardless of whether OpenRouter/Databricks are configured, cached to
    # models-dev.json for doctor's own freshness check and for future
    # model_table.json cross-checking (see providers/models_dev.py).
    models_dev_path = models_dev_json_path(state_dir)
    if args.refresh or not models_dev_path.exists():
        try:
            fetched = fetch_models_dev()
            write_models_dev_json(state_dir, fetched)
            print(f"\nmodels.dev: cached {len(fetched)} provider(s) to {models_dev_path}")
        except Exception as e:
            print(f"rolo-claude models: could not refresh from models.dev: {e}", file=sys.stderr)
    return 0
