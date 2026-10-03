"""`halo accounts` -- guided subscription-account setup."""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from halo_harness.accounts import AccountSetupError, add_codex_account, format_accounts


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="halo accounts", description="Manage isolated subscription logins")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="List managed subscription accounts")
    add = sub.add_parser("add", help="Add a subscription account")
    add.add_argument("provider", choices=["codex"])
    add.add_argument("--name", default=None, help="Friendly account name, such as personal or work")
    return parser


def cmd_accounts(argv: Optional[list] = None) -> int:
    args = _parser().parse_args(list(argv or []))
    if args.command in (None, "list"):
        print(format_accounts())
        return 0
    name = args.name
    if not name:
        if not sys.stdin.isatty():
            print("halo accounts add: --name is required when input is not interactive", file=sys.stderr)
            return 2
        name = input("Account name (for example personal or work): ").strip()
    print(f"Opening the official Codex login for account {name!r}.")
    print("Finish signing in through the browser. Halo never sees your password.")
    try:
        profile, status = add_codex_account(name)
    except AccountSetupError as exc:
        print(f"halo accounts: {exc}", file=sys.stderr)
        return 1
    print(f"Added Codex account {profile.name!r}: {status}")
    return 0
