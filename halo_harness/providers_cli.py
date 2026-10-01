"""rolo_claude.providers_cli -- `rolo-claude providers` subcommand (H15
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

from rolo_claude.providers.enablement import (
    PROVIDER_NAMES, canonical, credentials_present, credentials_source, disable, enable, enablement_display,
    is_enabled, label_for,
)


def _model_count(name: str) -> "int | None":
    """Cached model count for the table -- None (never a bare 0) when this
    provider has no catalog concept of its own (TypeSafe)."""
    if name == "typesafe":
        return None
    from rolo_claude.config.paths import bridge_home
    from rolo_claude.init_providers import model_entries_for_provider
    picker_name = "claude" if name == "claude_subscription" else name
    try:
        return len(model_entries_for_provider(picker_name, bridge_home()))
    except Exception:
        return None


def provider_rows() -> "list[dict]":
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
    same way a real session would resolve it."""
    from rolo_claude.providers.config import listing_effective_env
    from rolo_claude.providers.reachability import reachability_tag
    env = listing_effective_env()
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


def format_providers_table(rows: "list[dict]") -> str:
    header = f"{'provider':<26} {'status':<40} {'reachable':<42} {'models':>6}"
    lines = [header]
    for r in rows:
        models = str(r["model_count"]) if r["model_count"] is not None else "-"
        lines.append(f"{r['label']:<26} {r['status']:<40} {r['reachable']:<42} {models:>6}")
    # H15 part 2 addendum 4: the same balance figure the status bar/`/cost`
    # show, with the key label and reading time -- appended once, after the
    # table, when a fetch has ever succeeded (OpenRouter only, this round).
    from rolo_claude.providers.openrouter_account import format_balance_line
    balance_line = format_balance_line()
    if balance_line:
        lines.append("")
        lines.append(balance_line)
    return "\n".join(lines)


def cmd_providers(argv: list) -> int:
    # 1.0.1 part 2 fixpass finding 8: `catalog_cli`/`doctor` both load the
    # env file before resolving anything -- this command didn't, so a key
    # that lives ONLY in the env file (never a real shell export) showed
    # "not set up" here while the TUI's own `/providers` (reached through
    # `Controller`/`HeadlessFacade`, both of which load it earlier in
    # startup) correctly treated it as enabled.
    import os
    from pathlib import Path
    from rolo_claude.config.paths import home
    from rolo_claude.providers.config import load_env_file
    load_env_file(Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env")))

    from rolo_claude.providers.enablement import ensure_providers_migrated
    migration_note = ensure_providers_migrated()
    if migration_note:
        print(migration_note)

    if not argv or argv[0] in ("list",):
        print(format_providers_table(provider_rows()))
        return 0
    action = argv[0]
    if action in ("-h", "--help"):
        print("Usage: rolo-claude providers [list|enable <name>|disable <name>|setup <name>]")
        print(f"Providers: {', '.join(PROVIDER_NAMES)}")
        return 0
    if action in ("enable", "disable"):
        if len(argv) < 2:
            print(f"rolo-claude providers {action}: needs a provider name "
                  f"({', '.join(PROVIDER_NAMES)})", file=sys.stderr)
            return 2
        name = canonical(argv[1])
        if name not in PROVIDER_NAMES:
            print(f"rolo-claude providers {action}: unknown provider {argv[1]!r} "
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
            print(f"rolo-claude providers setup: needs a provider name ({', '.join(PROVIDER_NAMES)})",
                  file=sys.stderr)
            return 2
        name = canonical(argv[1])
        if name not in PROVIDER_NAMES:
            print(f"rolo-claude providers setup: unknown provider {argv[1]!r} "
                  f"(expected one of {', '.join(PROVIDER_NAMES)})", file=sys.stderr)
            return 2
        if name == "typesafe":
            print("TypeSafe is a key-only placeholder for a later feature -- there is no setup flow yet "
                  "(set TYPESAFE_API_KEY in the env file, then `rolo-claude providers enable typesafe`).",
                  file=sys.stderr)
            return 2
        picker_name = "claude" if name == "claude_subscription" else name
        from rolo_claude.init_cli import cmd_init
        return cmd_init(["--provider", picker_name] + argv[2:])
    print(f"rolo-claude providers: unrecognized arguments: {' '.join(argv)}", file=sys.stderr)
    return 2
