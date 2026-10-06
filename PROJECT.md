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
- Fixed `cx:` follow-up turns failing with "the codex subprocess ended unexpectedly": `codex exec resume` rejects `-s`, so the sandbox is now passed as `-c sandbox_mode=...`. The fake Codex now rejects `-s` on resume, and the resume test checks the second turn has no error.

- Found that the upstream merge (`521063d`) routes `cx:` turns through `agent/codex_turn.py` (one `codex exec` per turn), so the failover in `agent/cx_runtime.py` never ran. Moved the account work onto the live path:
  - Codex thread ids are recorded per account; a switched account starts its own thread primed with the conversation so far ("no rollout found" fix), and a thread Codex no longer has restarts fresh without an error.
  - "Out of credits" / usage-limit errors switch to the next logged-in Codex account and continue; a turn that already ran a tool asks for `continue` instead.
  - Codex's own shell commands now show as Bash tool cards with their output, and the log keeps the real output.
- Separated consecutive Codex replies into paragraphs and made a finished streamed reply re-render only when its text was clipped.
- `cc:` no longer forwards Halo's default `--max-turns 50` to Claude Code (long tasks stopped with `error_max_turns`); an explicit `--max-turns` still applies, and the error now explains how to continue.

## Upstream compatibility cleanup

- Standardized the active internal provider as `codex`, preserving `cx:` model references and `codex_subscription` enablement. Account catalogs supply aliases and supported effort; the selected effort is passed to `codex exec`.
- Migrated setup and account tests to the active runtime and removed the unused app-server turn engine. Kept the app-server client for model discovery and usage queries.
- Resume accepts both `cx_session_id` and legacy `cx_thread_id` with account ownership checks. Conversation forks persist a boundary so every account starts an independent thread, including after reopening.
- Codex usage now refreshes from the owning account once per minute, with a five-second toolbar repaint while idle or busy. Failed reads retain that account's last success; another account's percentages are never substituted.
- Regression coverage lives in `tests/test_codex_compat.py`; the retained `tests/test_cx_session.py` covers catalog discovery. Full MCP bridge integration requires permission to bind local sockets.

## Validation and installation status

The compatibility suite (9), account suite (10), catalog suite (3), and Codex settings suite (16) pass. A supplemental run of 22 existing Codex tests passes with only the unused MCP listener startup stubbed; it still runs the fake Codex executable and real session logic. The full bridge tests cannot bind sockets in this workspace sandbox. Eleven non-UI usage checks pass; the existing Claude context-prime check and full Textual usage test time out here. Compilation and diff checks pass.

A clean installable wheel is prepared in `../halo-fix-dist/`. This session cannot write the pipx installation outside the workspace, so the installed harness still needs updating. The previous checkout at `/home/jay/Halo-Harness` and GitHub have not been changed.

## Before merging upstream

Check the changes to routing, `codex_runtime.py`, `codex_turn.py`, model resolution, and status events. Run the Codex, compatibility, catalog, account, settings, and usage suites. Investigate new failures instead of labeling them baseline failures. Keep the single active conversation runtime; migrate account behavior when upstream changes its interfaces.

## Next action

Add isolated Claude subscription profiles, then implement cross-provider fallback after all accounts on the active provider are unavailable.
