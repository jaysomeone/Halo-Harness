"""halo_harness.catalog_cli -- `halo models` subcommand (H1 scope I):
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

from halo_harness.config.paths import bridge_home
from halo_harness.model_display import ROW_HEADER, format_price_per_m, format_token_count
from halo_harness.providers.config import (
    load_provider_env_files, resolve_databricks, resolve_openrouter, derive_workspace_root,
)
from halo_harness.providers.databricks import (
    load_dbx_endpoints_json, load_models_json, probe_openrouter_models, write_models_json,
)
from halo_harness.providers.models_dev import fetch_models_dev, models_dev_json_path, write_models_dev_json


def _fmt_price(v) -> str:
    """USD per token -> "$X.XX/M" (per-million-token, the unit every
    provider's own pricing page uses); "?" for anything unparseable.
    Kept (only) for `--cc`'s own table below, which predates `model_display`
    and still wants "?" (not blank) for an unknown value in ITS OWN
    columns -- `models --cc` is a separate, narrower table this hotfix
    didn't touch."""
    try:
        return f"${float(v) * 1_000_000:.2f}/M"
    except (TypeError, ValueError):
        return "?"


def near_miss_slug(requested: str, known_ids) -> "list[str]":
    """Ori-style near-miss slug correction (scope I/rule 11): the closest
    known model ids to an unrecognized `--model`, best match first."""
    return difflib.get_close_matches(requested, list(known_ids), n=5, cutoff=0.5)


def _cmd_models_cc(state_dir, *, refresh: bool) -> int:
    """H11 Part A: `halo models --cc` -- the nine subscription-model
    alias names, their `cc:`/`ant:` targets, and (when a profile row is
    known) context/output/pricing. Never touches the credentials file --
    only `claude auth status` (login line) and, with --refresh, a handful
    of cheap `-p --max-turns 1` pings (providers.cc_models.
    refresh_cc_catalog)."""
    from halo_harness.providers.cc_models import (
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


def _endpoint_url_for_path_type(root: str, name: str, path_type: str) -> str:
    """The exact URL for an already-computed `path_type` (1.0.1 hotfix 4:
    split out of the old `_endpoint_url_and_path_type` so `_dbx_rows` can
    compute `path_type` ONCE, always, and only additionally build the full
    URL when `--urls` actually asked for it)."""
    if path_type == "anthropic":
        return f"{root}/ai-gateway/anthropic/v1/messages"
    if path_type in ("none", "?"):
        return "(refused -- non-chat endpoint)" if path_type == "none" else "?"
    from halo_harness.providers.dbx_routing import API_TYPE_INFO
    info = API_TYPE_INFO.get(path_type)
    if info is not None:
        return f"{root}{info['path']}"
    return f"{root}/serving-endpoints/{name}/invocations"


def _dbx_rows(endpoints: dict, root: str, state_dir, *, urls: bool) -> dict:
    """1.0.1 hotfix 4: `path_type`/`chat` are now ALWAYS computed (never
    gated behind `--urls`, which used to leave every bare-table row showing
    "path ?" even when a real route existed) and sourced from data the cache
    actually carries: `path_type` is `dbx_routing.default_path_type` (the
    SAME family/api_types decision `chat_route_candidates` makes for a real
    request -- no network call, it's all local), `chat` is
    `dbx_routing.is_chat_task` (the endpoint's own `task`, not a name-based
    family guess). An OLD-SHAPE cache (hotfix 4's own migration -- see
    `databricks.dbx_endpoints_cache_is_old_shape`) shows `path_type="unknown"`
    for every row instead of silently degrading to a wrong "invocations"
    (`chat_route_candidates` can't tell a real invocations-only route from a
    cache that simply never recorded `api_types` at all)."""
    from halo_harness.model_display import databricks_row_fields
    from halo_harness.providers.databricks import dbx_endpoints_cache_is_old_shape
    from halo_harness.providers.dbx_routing import classify_family, default_path_type, is_chat_task
    old_shape = dbx_endpoints_cache_is_old_shape(endpoints)
    rows = {}
    for name, e in endpoints.items():
        family = classify_family(name, foundation_model_name=e.get("foundation_model_name") or "",
                                  model_class=e.get("model_class") or "")
        path_type = "unknown" if old_shape else default_path_type(name, state_dir)
        row = {"family": family, "task": e.get("task"), "chat": is_chat_task(e.get("task")),
               "api_types": e.get("api_types") or [], "path_type": path_type}
        # 1.0.1 hotfix 12: ctx/output/price columns, same
        # models.dev-then-model_table.json-then-blank rule `Controller.
        # list_models()`'s own Databricks rows and the init picker use.
        try:
            row.update(databricks_row_fields(name, state_dir=state_dir))
        except Exception:
            pass
        if urls:
            row["url"] = ("(refresh needed -- cached before this version tracked gateway types)" if old_shape
                           else _endpoint_url_for_path_type(root, name, path_type) if root else "?")
        rows[name] = row
    return rows


def _path_display(path_type: str) -> str:
    """The human-readable `path` column label for the TEXT table only
    (`unknown` -- hotfix 4's old-cache migration marker -- reads as "unknown
    (refresh needed)"; a real route key gets dbx_routing's own
    `mlflow-chat`/`cursor-chat` spelling). `--json`/`--urls`' machine-
    readable `path_type` field is untouched by this -- see `_dbx_rows`."""
    from halo_harness.providers.dbx_routing import PATH_TYPE_DISPLAY
    if path_type == "unknown":
        return "unknown (refresh needed)"
    return PATH_TYPE_DISPLAY.get(path_type, path_type)


def format_dbx_table_lines(rows: dict, *, urls: bool = False) -> "list[str]":
    """The Databricks endpoint table's exact text rendering (name/family/
    path/chat/ctx/out/price[/url] columns) -- ONE implementation shared by
    `halo models`, the headless `/models` (`commands/builtins.py::
    _cmd_models`), and the TUI's own `/models` bare rendering (`tui/
    slash.py`), so the three surfaces can never drift apart on column
    widths or wording. `rows` is `_dbx_rows`'s own per-endpoint dict.

    1.0.1 hotfix 12: ctx/out/price columns (`model_display.
    format_token_count`/`format_price_per_m` -- blank, never "?", when
    unknown) alongside the existing family/path/chat/url diagnostic
    columns this CLI table already had (this command's whole purpose is
    diagnostic depth, so nothing here was REMOVED -- only the `/model`/
    `/models`/init picker surfaces, which show a compact single ref+price
    line per row via `format_model_row`, stay narrower)."""
    header = (f"{'name':<42} {'family':<16} {'path':<18} {'chat':>5} "
              f"{'ctx':>6} {'out':>6} {'in/M':>9} {'out/M':>9}")
    lines = [header + ("  url" if urls else ""), f"  ({ROW_HEADER})"]
    for name in sorted(rows):
        r = rows[name]
        line = (f"{name:<42} {r['family']:<16} {_path_display(r.get('path_type', '?')):<18} "
                f"{('yes' if r['chat'] else 'no'):>5} "
                f"{format_token_count(r.get('context_tokens')):>6} {format_token_count(r.get('max_output_tokens')):>6} "
                f"{format_price_per_m(r.get('price_in_per_m')):>9} {format_price_per_m(r.get('price_out_per_m')):>9}")
        if urls:
            line += f"  {r.get('url', '?')}"
        lines.append(line)
    return lines


def cmd_models(argv) -> int:
    parser = argparse.ArgumentParser(prog="halo models", add_help=True)
    parser.add_argument("--refresh", action="store_true", help="Re-probe OpenRouter/Databricks instead of using the cache")
    parser.add_argument("--cc", action="store_true",
                         help="List the Claude subscription models (cc:/ant: aliases) instead of the "
                              "OpenRouter/Databricks catalog; with --refresh, re-pings each alias to "
                              "confirm its current canonical id")
    parser.add_argument("--urls", action="store_true",
                         help="Databricks endpoints: also print the exact URL and path type each one resolves to")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    args = parser.parse_args(argv)

    # 2.0.0 fixpass finding 4: the new env file, then the legacy one too.
    load_provider_env_files()

    state_dir = bridge_home()
    if args.cc:
        return _cmd_models_cc(state_dir, refresh=args.refresh)
    # 1.0.1 hotfix 3: bare `halo models` (no --refresh) NEVER touches
    # the network, full stop -- not even "the first time the cache is
    # empty" (the old behavior here, and still `--cc`'s own documented
    # first-use exception, which this leaves alone). A DNS/VPN-down box
    # must be able to run this to see "nothing cached yet" instantly rather
    # than hanging on an unreachable host it never asked to probe.
    # H15 part 2 addendum 3.2b: `--refresh` refreshes every ENABLED
    # provider's catalog now -- an explicitly-disabled provider (even with
    # real credentials) is left alone, same rule every other surface
    # follows.
    from halo_harness.providers.enablement import is_enabled
    models = load_models_json(state_dir)
    if args.refresh:
        orc = resolve_openrouter() if is_enabled("openrouter") else None
        if orc is not None:
            try:
                fetched = probe_openrouter_models(orc.base_url, orc.api_key)
                write_models_json(state_dir, fetched)
                models = load_models_json(state_dir)
            except Exception as e:
                print(f"halo models: could not refresh from OpenRouter: {e}", file=sys.stderr)

    dbx = resolve_databricks() if is_enabled("databricks") else None
    endpoints = load_dbx_endpoints_json(state_dir)
    dbx_diff = None
    if dbx is not None and args.refresh:
        from halo_harness.providers.databricks import refresh_dbx_catalog
        root = derive_workspace_root(dbx.host)
        ok, diff, note = refresh_dbx_catalog(state_dir, root, dbx.token)
        if ok:
            endpoints = load_dbx_endpoints_json(state_dir)
            dbx_diff = diff
        else:
            print(f"halo models: could not refresh from Databricks: {note}", file=sys.stderr)

    if args.refresh and is_enabled("anthropic"):
        from halo_harness.providers.anthropic_catalog import refresh_anthropic_catalog_if_stale
        ok = refresh_anthropic_catalog_if_stale(state_dir, force=True)
        if ok is False:
            print("halo models: could not refresh from Anthropic", file=sys.stderr)

    # H15 item 21.2: a provider with real credentials but not ENABLED shows
    # one line instead of its table -- same rule `/model`/the init picker/
    # doctor all follow; `halo providers enable <name>` is the fix
    # every one of those surfaces names too.
    from halo_harness.providers.enablement import is_enabled
    or_enabled = is_enabled("openrouter")
    dbx_enabled = is_enabled("databricks")

    if args.json:
        payload = {"openrouter": models if or_enabled else {}}
        if not or_enabled and models:
            payload["openrouter_disabled"] = True
        if dbx is not None:
            if dbx_enabled:
                payload["databricks"] = _dbx_rows(endpoints, derive_workspace_root(dbx.host), state_dir,
                                                    urls=args.urls)
                if dbx_diff is not None:
                    payload["databricks_diff"] = dbx_diff
            elif endpoints:
                payload["databricks_disabled"] = True
        print(json.dumps(payload, indent=2, default=str))
        return 0

    if not or_enabled and models:
        print(f"OpenRouter: {len(models)} model(s) cached, but OpenRouter is not enabled -- "
              f"run `halo providers enable openrouter` to show them.")
        models = {}
    print("OpenRouter models (models.json):")
    print(f"{'id':<48} {'ctx':>8} {'out':>8} {'in/M':>10} {'out/M':>10}    ({ROW_HEADER})")
    for mid in sorted(models):
        entry = models[mid]
        pricing = entry.get("pricing") or {}
        # 1.0.1 fixpass finding 7: models.json stores EVERY OpenRouter price
        # as a STRING (e.g. "0.0000008") -- float(v) in a try/except, same
        # as the pre-1.0.1 code, so a numeric string is never treated as
        # unknown and this column goes blank.
        def _price_per_m(v) -> "float | None":
            if isinstance(v, bool):
                return None
            try:
                return float(v) * 1_000_000
            except (TypeError, ValueError):
                return None
        print(f"{mid:<48} {format_token_count(entry.get('context_length')):>8} "
              f"{format_token_count(entry.get('max_output_tokens')):>8} "
              f"{format_price_per_m(_price_per_m(pricing.get('prompt'))):>10} "
              f"{format_price_per_m(_price_per_m(pricing.get('completion'))):>10}")

    if dbx is not None and endpoints and not dbx_enabled:
        print(f"\nDatabricks: {len(endpoints)} endpoint(s) cached, but Databricks is not enabled -- "
              f"run `halo providers enable databricks` to show them.")
    elif dbx is not None and endpoints:
        root = derive_workspace_root(dbx.host)
        rows = _dbx_rows(endpoints, root, state_dir, urls=args.urls)
        print("\nDatabricks endpoints (dbx-endpoints.json):")
        for line in format_dbx_table_lines(rows, urls=args.urls):
            print(line)
        if dbx_diff is not None:
            from halo_harness.providers.databricks import format_dbx_diff
            print(f"\nDatabricks catalog diff (this refresh): {format_dbx_diff(dbx_diff)}")

    # H8 scope C: models.dev's api.json is public/unauthenticated -- cached
    # to models-dev.json for doctor's own freshness check and for future
    # model_table.json cross-checking (see providers/models_dev.py). 1.0.1
    # hotfix 3: only ever fetched with --refresh now (see the OpenRouter/
    # Databricks sections above for why "the first time it's empty" no
    # longer triggers a network call on its own).
    models_dev_path = models_dev_json_path(state_dir)
    if args.refresh:
        try:
            fetched = fetch_models_dev()
            write_models_dev_json(state_dir, fetched)
            print(f"\nmodels.dev: cached {len(fetched)} provider(s) to {models_dev_path}")
        except Exception as e:
            # 1.0.1 hotfix 11: names the real failure (often a
            # CERTIFICATE_VERIFY_FAILED at a TLS-inspecting work network --
            # see docs/TROUBLESHOOTING.md) AND says plainly that the
            # vendored/cached catalog stays in use either way -- this was
            # previously just the bare exception, with no indication that
            # nothing else was actually broken by it.
            print(f"halo models: could not refresh from models.dev: {e} "
                  f"-- the vendored/cached models.dev catalog stays in use.", file=sys.stderr)
    return 0
