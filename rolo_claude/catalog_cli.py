"""rolo_claude.catalog_cli -- `rolo-claude models` subcommand (H1 scope I):
lists OpenRouter models (from the cached models.json, refreshing via a live
probe when the cache is empty or --refresh is passed) and Databricks
endpoints (from dbx-endpoints.json), with context/output/price columns, and
near-miss slug correction for a `--model` that doesn't match anything.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys

from pathlib import Path

from rolo_claude.config.paths import bridge_home, home
from rolo_claude.providers.config import load_env_file, resolve_databricks, resolve_openrouter, derive_workspace_root
from rolo_claude.providers.databricks import (
    load_dbx_endpoints_json, load_models_json, probe_openrouter_models, write_models_json,
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


def _cmd_models_cc(state_dir, *, refresh: bool) -> int:
    """H11 Part A: `rolo-claude models --cc` -- the nine subscription-model
    alias names, their `cc:`/`ant:` targets, and (when a profile row is
    known) context/output/pricing. Never touches the credentials file --
    only `claude auth status` (login line) and, with --refresh, a handful
    of cheap `-p --max-turns 1` pings (providers.cc_models.
    refresh_cc_catalog)."""
    from rolo_claude.providers.cc_models import (
        ANT_ALIASES, CC_ALIASES, claude_auth_status, profile_fields_for_cc_model, refresh_cc_catalog,
    )
    status = claude_auth_status()
    if status is None:
        print("Claude subscription: claude not found -- cc: models unavailable (install Claude Code)")
    elif not status.logged_in:
        print("Claude subscription: claude found but not logged in -- run `claude` once to log in")
    else:
        print(f"Claude subscription: logged in ({status.auth_method or 'claude.ai'}) -- cc: models available")

    if refresh:
        refresh_cc_catalog(state_dir=state_dir)
        print("(refreshed cc-models.json from live -p pings)\n")

    print(f"{'alias':<12} {'cc: target':<24} {'ant: target':<28} {'context':>10} {'out cap':>9} {'in/M':>9} {'out/M':>9}")
    for name in CC_ALIASES:
        cc_target = CC_ALIASES[name]
        ant_target = ANT_ALIASES.get(name, "")
        fields = profile_fields_for_cc_model(cc_target) or {}
        ctx = fields.get("context_tokens", "?")
        out_cap = fields.get("max_output_tokens", "?")
        print(f"{name:<12} {cc_target:<24} {ant_target:<28} {str(ctx):>10} {str(out_cap):>9} "
              f"{_fmt_price(fields.get('price_in')):>9} {_fmt_price(fields.get('price_out')):>9}")
    return 0


def _endpoint_url_and_path_type(root: str, name: str, state_dir) -> "tuple[str, str]":
    """H14 scope J ("--urls"): the exact URL + path-type label
    `chat_route_candidates`/`resolve_databricks_dialect` would actually pick
    FIRST for `name` -- what a real request from this box would hit."""
    from rolo_claude.providers.dbx_routing import chat_route_candidates, resolve_databricks_dialect
    _clean, dialect = resolve_databricks_dialect(name, state_dir)
    if dialect == "anthropic-passthrough":
        return f"{root}/ai-gateway/anthropic/v1/messages", "anthropic"
    cands = chat_route_candidates(name, state_dir)
    if not cands:
        return "(refused -- non-chat endpoint)", "none"
    return f"{root}{cands[0].path}", cands[0].key


def _dbx_rows(endpoints: dict, root: str, state_dir, *, urls: bool) -> dict:
    from rolo_claude.providers.dbx_routing import classify_family
    rows = {}
    for name, e in endpoints.items():
        family = classify_family(name, foundation_model_name=e.get("foundation_model_name") or "",
                                  model_class=e.get("model_class") or "")
        row = {"family": family, "task": e.get("task"), "chat": family != "non_chat",
               "api_types": e.get("api_types") or []}
        if urls and root:
            row["url"], row["path_type"] = _endpoint_url_and_path_type(root, name, state_dir)
        rows[name] = row
    return rows


def cmd_models(argv) -> int:
    parser = argparse.ArgumentParser(prog="rolo-claude models", add_help=True)
    parser.add_argument("--refresh", action="store_true", help="Re-probe OpenRouter/Databricks instead of using the cache")
    parser.add_argument("--cc", action="store_true",
                         help="List the Claude subscription models (cc:/ant: aliases) instead of the "
                              "OpenRouter/Databricks catalog; with --refresh, re-pings each alias to "
                              "confirm its current canonical id")
    parser.add_argument("--urls", action="store_true",
                         help="Databricks endpoints: also print the exact URL and path type each one resolves to")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    args = parser.parse_args(argv)

    import os
    load_env_file(Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env")))

    state_dir = bridge_home()
    if args.cc:
        return _cmd_models_cc(state_dir, refresh=args.refresh)
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

    dbx = resolve_databricks()
    endpoints = load_dbx_endpoints_json(state_dir)
    dbx_diff = None
    if dbx is not None and (args.refresh or not endpoints):
        from rolo_claude.providers.databricks import refresh_dbx_catalog
        root = derive_workspace_root(dbx.host)
        ok, diff, note = refresh_dbx_catalog(state_dir, root, dbx.token)
        if ok:
            endpoints = load_dbx_endpoints_json(state_dir)
            if args.refresh:
                dbx_diff = diff
        else:
            print(f"rolo-claude models: could not refresh from Databricks: {note}", file=sys.stderr)

    if args.json:
        payload = {"openrouter": models}
        if dbx is not None:
            payload["databricks"] = _dbx_rows(endpoints, derive_workspace_root(dbx.host), state_dir, urls=args.urls)
            if dbx_diff is not None:
                payload["databricks_diff"] = dbx_diff
        print(json.dumps(payload, indent=2, default=str))
        return 0

    print("OpenRouter models (models.json):")
    print(f"{'id':<48} {'context':>10} {'max_out':>10} {'in/M':>10} {'out/M':>10}")
    for mid in sorted(models):
        entry = models[mid]
        pricing = entry.get("pricing") or {}
        print(f"{mid:<48} {str(entry.get('context_length', '?')):>10} {str(entry.get('max_output_tokens', '?')):>10} "
              f"{_fmt_price(pricing.get('prompt')):>10} {_fmt_price(pricing.get('completion')):>10}")

    if dbx is not None and endpoints:
        root = derive_workspace_root(dbx.host)
        rows = _dbx_rows(endpoints, root, state_dir, urls=args.urls)
        print("\nDatabricks endpoints (dbx-endpoints.json):")
        header = f"{'name':<42} {'family':<16} {'path':<12} {'chat':>5}"
        print(header + ("  url" if args.urls else ""))
        for name in sorted(rows):
            r = rows[name]
            line = f"{name:<42} {r['family']:<16} {r.get('path_type', '?'):<12} {('yes' if r['chat'] else 'no'):>5}"
            if args.urls:
                line += f"  {r.get('url', '?')}"
            print(line)
        if dbx_diff is not None:
            from rolo_claude.providers.databricks import format_dbx_diff
            print(f"\nDatabricks catalog diff (this refresh): {format_dbx_diff(dbx_diff)}")

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
