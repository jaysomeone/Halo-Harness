"""halo_harness.providers_cli -- `halo providers` subcommand (H15
item 21.5, table columns updated by the H15 part 2 addendum): a table
(status -- `auto (detected from <source>)` / `disabled by you` / `enabled
by you` / `not set up`, reachable, cached model count) plus `enable
<name>`/`disable <name>`/`setup <name>`. The TUI's `/providers`
(`commands/builtins.py::_cmd_providers`) renders the SAME table text
(`format_providers_table`/`provider_rows`), so the two surfaces never
drift apart.
"""

from __future__ import annotations

import sys

from halo_harness.providers.enablement import (
    PROVIDER_NAMES, canonical, credentials_present, credentials_source, disable, enable, enablement_display,
    is_enabled, label_for,
)


def _model_count(name: str) -> "int | None":
    """Cached model count for the table -- None (never a bare 0) when this
    provider has no catalog concept of its own (TypeSafe)."""
    if name == "typesafe":
        return None
    from halo_harness.config.paths import bridge_home
    from halo_harness.init_providers import model_entries_for_provider
    picker_name = "claude" if name == "claude_subscription" else name
    try:
        return len(model_entries_for_provider(picker_name, bridge_home()))
    except Exception:
        return None


def provider_rows(*, cwd=None, settings_flag=None, env=None) -> "list[dict]":
    """`[{"name", "label", "enabled", "status", "credentials",
    "credentials_source", "reachable", "model_count"}, ...]`, one row per
    `PROVIDER_NAMES` entry, in that fixed order. `status` (H15 part 2
    addendum) is the user-facing enablement string: `"auto (detected from
    <source>)"`, `"disabled by you"`, `"enabled by you"` or `"not set up"`
    -- `enabled` stays as a plain bool for any caller that just wants the
    yes/no answer.

    1.0.1 part 2 fixpass critical finding 1: `credentials_present(name,
    env=...)` is called EXACTLY ONCE per row now (`detected`), reused for
    `enabled`/`status`/`credentials`/`credentials_source`/`reachable` --
    before this fix the `claude_subscription` row alone re-derived it 5-6
    times, each an UNCACHED `claude auth status` subprocess spawn (up to a
    10s timeout apiece). `env` (finding 3) is `providers.config.
    listing_effective_env()`, computed once for the whole table, so a
    credential living only in a settings.json `env` block is seen here the
    same way a real session would resolve it.

    Findings 22/23 (2.0.1): `env`, when given, is used as-is -- the exact
    `Settings.effective_env` of a REAL running session (`/providers`'s own
    `commands.builtins._cmd_providers`, when a live session is attached,
    passes this instead of letting the table re-derive a possibly-different
    one), never re-resolved. Without `env`, `cwd`/`settings_flag` (a
    caller's own `--cwd`/`--settings` flag values, when it has them -- the
    standalone `halo providers` CLI) are threaded into `listing_effective_
    env` so this never disagrees with what that same flag combination would
    resolve in a real session."""
    from halo_harness.providers.config import listing_effective_env
    from halo_harness.providers.reachability import reachability_tag
    env = env if env is not None else listing_effective_env(cwd, settings_flag)
    rows = []
    for name in PROVIDER_NAMES:
        detected = credentials_present(name, env=env)
        rows.append({
            "name": name, "label": label_for(name), "enabled": is_enabled(name, detected=detected),
            "status": enablement_display(name, detected=detected),
            "credentials": detected, "credentials_source": credentials_source(name, detected=detected),
            "reachable": reachability_tag(name, detected=detected), "model_count": _model_count(name),
        })
    return rows


def format_providers_table(rows: "list[dict]", *, openai_spend_line: "str | None" = None) -> str:
    """`openai_spend_line` (round 5i part 1): an already-formatted "this
    session: $x.xxxx across N turn(s)" fragment -- `_cmd_providers`
    passes one when a live session's `cost_meter` has data for an `oai:`
    model; `None` (every OTHER caller, including the standalone `halo
    providers` CLI, which has no live session at all) falls back to a
    plain pointer at `/cost`/`halo cost` instead of a number. Either way
    the note only appears when the `openai` row is actually enabled --
    nothing to say about a balance endpoint nobody configured."""
    header = f"{'provider':<26} {'status':<40} {'reachable':<42} {'models':>6}"
    lines = [header]
    for r in rows:
        models = str(r["model_count"]) if r["model_count"] is not None else "-"
        lines.append(f"{r['label']:<26} {r['status']:<40} {r['reachable']:<42} {models:>6}")
    # H15 part 2 addendum 4: the same balance figure the status bar/`/cost`
    # show, with the key label and reading time -- appended once, after the
    # table, when a fetch has ever succeeded (OpenRouter only, this round).
    from halo_harness.providers.openrouter_account import format_balance_line
    balance_line = format_balance_line()
    if balance_line:
        lines.append("")
        lines.append(balance_line)
    # Round 5i part 1 (brief item 1): "the OpenAI API has no public
    # balance endpoint for ordinary keys -- say so and show computed
    # spend" (the same rule part B of 2.0.3-brief.md gives for TypeSafe/
    # Databricks). Plain and static -- never a network call from this
    # function, which both the TUI's `/providers` and the standalone CLI
    # call with no session attached half the time.
    oai_row = next((r for r in rows if r["name"] == "openai"), None)
    if oai_row is not None and oai_row["enabled"]:
        lines.append("")
        lines.append(
            "OpenAI API has no public balance endpoint for ordinary keys -- " +
            (openai_spend_line or "spend is computed per session from oai: catalog prices (see /cost).")
        )
    return "\n".join(lines)


def cmd_providers(argv: list) -> int:
    # Findings 22/23 (2.0.1): peeled off here (not a full argparse parser,
    # to keep every existing positional `list|enable <name>|disable <name>|
    # setup <name>` form working unchanged) so `halo providers --cwd DIR
    # --settings JSON_OR_PATH` resolves credentials against that explicit
    # cwd/settings-flag value, same as a real session launched with either
    # flag would -- matching `halo doctor`'s own new `--cwd`/`--settings`.
    import argparse
    from pathlib import Path
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument("--cwd", default=None)
    _pre.add_argument("--settings", default=None, dest="settings_flag", metavar="JSON_OR_PATH")
    _ns, argv = _pre.parse_known_args(argv)
    cwd = Path(_ns.cwd).resolve() if _ns.cwd else None
    settings_flag = _ns.settings_flag

    # 1.0.1 part 2 fixpass finding 8: `catalog_cli`/`doctor` both load the
    # env file before resolving anything -- this command didn't, so a key
    # that lives ONLY in the env file (never a real shell export) showed
    # "not set up" here while the TUI's own `/providers` (reached through
    # `Controller`/`HeadlessFacade`, both of which load it earlier in
    # startup) correctly treated it as enabled.
    # 2.0.0 fixpass finding 4: the new env file, then the legacy one too.
    from halo_harness.providers.config import load_provider_env_files
    load_provider_env_files()

    from halo_harness.providers.enablement import ensure_providers_migrated
    migration_note = ensure_providers_migrated()
    if migration_note:
        print(migration_note)

    if not argv or argv[0] in ("list",):
        # 2.0.1 launch-hang follow-up: `provider_rows()` reaches
        # `claude_login_available()`, which is cache-only since the fix --
        # and this subcommand is always a fresh process, so without priming
        # the cache once here a real claude.ai login printed "not set up"
        # (seen live on the Kali VM). Same staleness-gated refresh the
        # headless `/providers` does; `refresh_cached_claude_auth_status()`
        # itself never spawns anything when `claude` is gateway-driven.
        try:
            from halo_harness.providers.cc_models import cached_auth_status_is_stale, refresh_cached_claude_auth_status
            if cached_auth_status_is_stale():
                refresh_cached_claude_auth_status()
        except Exception:
            pass
        print(format_providers_table(provider_rows(cwd=cwd, settings_flag=settings_flag)))
        return 0
    action = argv[0]
    if action in ("-h", "--help"):
        print("Usage: halo providers [list|enable <name>|disable <name>|setup <name>]")
        print(f"Providers: {', '.join(PROVIDER_NAMES)}")
        return 0
    if action in ("enable", "disable"):
        if len(argv) < 2:
            print(f"halo providers {action}: needs a provider name "
                  f"({', '.join(PROVIDER_NAMES)})", file=sys.stderr)
            return 2
        name = canonical(argv[1])
        if name not in PROVIDER_NAMES:
            print(f"halo providers {action}: unknown provider {argv[1]!r} "
                  f"(expected one of {', '.join(PROVIDER_NAMES)})", file=sys.stderr)
            return 2
        if action == "enable":
            enable(name)
            print(f"{label_for(name)}: enabled")
        else:
            disable(name)
            print(f"{label_for(name)}: disabled")
        return 0
    if action == "setup":
        if len(argv) < 2:
            print(f"halo providers setup: needs a provider name ({', '.join(PROVIDER_NAMES)})",
                  file=sys.stderr)
            return 2
        name = canonical(argv[1])
        if name not in PROVIDER_NAMES:
            print(f"halo providers setup: unknown provider {argv[1]!r} "
                  f"(expected one of {', '.join(PROVIDER_NAMES)})", file=sys.stderr)
            return 2
        if name == "typesafe":
            print("TypeSafe is a key-only placeholder for a later feature -- there is no setup flow yet "
                  "(set TYPESAFE_API_KEY in the env file, then `halo providers enable typesafe`).",
                  file=sys.stderr)
            return 2
        picker_name = "claude" if name == "claude_subscription" else name
        from halo_harness.init_cli import cmd_init
        return cmd_init(["--provider", picker_name] + argv[2:])
    print(f"halo providers: unrecognized arguments: {' '.join(argv)}", file=sys.stderr)
    return 2
