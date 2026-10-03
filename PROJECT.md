# Halo multi-subscription support

## Goal

Let Halo manage multiple user-owned AI subscriptions and continue work when one account reaches a confirmed usage limit. Selection stays on the current provider first, moves to another account on that provider, and only then considers another configured subscription provider.

## Current stage

BUILD

## Completed

- Added private, isolated Codex account profiles under `~/.halo/accounts/codex/<name>/codex-home/`.
- Added guided official Codex browser login through `halo accounts add codex --name <name>` and `/accounts add codex <name>`.
- Forced file-based Codex credential storage for new profiles; Halo checks login status without reading or printing credential contents.
- Added account listing, name/path validation, duplicate-login protection, documentation, and focused tests.
- Verified 24 focused tests, Python compilation, and whitespace checks.

## Known baseline issue

`tests/test_cx_session.py` currently has 11 broken-pipe failures. The same failures reproduce from the untouched `d1a603a` source, so they predate this feature and are not caused by the account-profile milestone.

## Next action

Make Codex session startup account-aware, track account availability independently, and add same-provider selection before implementing exhaustion-triggered failover.
