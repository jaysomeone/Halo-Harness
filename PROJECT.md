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

## Known baseline issue

`tests/test_cx_session.py` tests the app-server runtime (`agent/cx_runtime.py`), which nothing calls since the upstream merge; 13 of its tests fail because `cx:` now parses to provider `codex`. Decide whether to delete that module or bring it back.

## Next action

Add isolated Claude subscription profiles, then implement cross-provider fallback after all accounts on the active provider are unavailable.
