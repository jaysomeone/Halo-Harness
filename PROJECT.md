# Halo multi-subscription support

## Goal

Let Halo manage multiple user-owned AI subscriptions and continue work when one account reaches a confirmed usage limit. Selection stays on the current provider first, moves to another account on that provider, and only then considers another configured subscription provider.

## Current stage

BUILD

## Completed

- Added private, isolated Codex account profiles under `~/.halo/accounts/codex/<name>/codex-home/`.
- Added guided official Codex browser login through `halo accounts add codex --name <name>` and `/accounts add codex <name>`.
- Forced file-based Codex credential storage for new profiles; Halo checks login status without reading or printing credential contents.
- Added account listing, active-account selection, name/path validation, duplicate-login protection, documentation, and focused tests.
- Made Codex app-server launch, conversation-thread resume, and usage caches account-aware.
- Added confirmed-limit failover to another logged-in Codex account. Tool-free turns continue automatically; turns that already executed a tool require an explicit `continue` to prevent duplicate side effects.
- Verified focused account, command, documentation, compilation, and whitespace checks.
- Made the status bar retain the last successful five-hour/weekly reading separately for `cc:` and `cx:`, replace it on a successful update, and hide it completely on non-subscription models.
- Made subscription status events read the active Codex account's profile cache so account switches and failover replace the toolbar values correctly.
- Verified the subscription toolbar suite (13/13), managed-account suite (10/10), syntax, and whitespace checks.
- Added live session and weekly reset countdowns beside the subscription usage percentages in the toolbar, with reset timestamps carried for both Claude and Codex subscriptions.
- Installed this local fork into the pipx `halo-harness` environment and verified `halo accounts list` plus the interactive `/accounts` registration.

## Known baseline issue

`tests/test_cx_session.py` currently has 11 broken-pipe failures. The same failures reproduce from the untouched `d1a603a` source, so they predate this feature and are not caused by the account-profile milestone.

## Next action

Add isolated Claude subscription profiles, then implement cross-provider fallback after all accounts on the active provider are unavailable.
