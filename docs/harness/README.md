# `docs/harness/` -- the build history

This directory is this project's own **development record**, not
end-user documentation -- for how to use rolo-claude, start at the
repo-root [README.md](../../README.md) and [docs/](..). Everything here was
written *during* development, in the order it happened, and is kept as-is:
a brief states scope and constraints **before** a milestone's work; a
review is an independent bug-hunt pass **after** it; an acceptance record
is the cross-platform (Windows/WSL/Kali) verification that ran at the end
of a batch of milestones, with real command output. Where this history and
`docs/`'s own description of current behavior would ever disagree, `docs/`
wins -- these files are never updated retroactively to match later code.

[docs/DEVELOPMENT.md](../DEVELOPMENT.md) explains how to use this workflow
for new work.

## Milestone briefs

Each names its own version bump; several milestones landed inside one
release (this project didn't use strict per-milestone semver until H10).

| Brief | Released in | What it built |
|---|---|---|
| [H1-brief.md](H1-brief.md) | 0.3.0 | package split from the `bridge.py` proxy; per-provider compat profiles; the append-only session log; reasoning replay; the loop breaker |
| [H2-brief.md](H2-brief.md) | 0.3.0 | the built-in tools (Read/Write/Edit/Bash/...); the tool-call repair layer; the full permission-rule grammar, with no safety classifier |
| [H3-brief.md](H3-brief.md) | 0.3.0 | MCP: the official SDK, stdio/http/sse transports, the frozen tool catalog with lazy `ToolSearch` loading, browser tools, the `mcp` CLI |
| [H4-brief.md](H4-brief.md) | 0.3.0 | hooks (every event/handler type); skills and custom slash commands; WebFetch/WebSearch; uninterrupted auto mode with mid-turn steering |
| [H5-brief.md](H5-brief.md) | 0.3.0 | auto-compaction; native `ant:`/Databricks-Claude thinking replay with signatures |
| [H5b-brief.md](H5b-brief.md) | 0.3.0 | a review-driven reliability-hardening pass (steer/abort races, hook interruptibility, sub-agent permission surfacing) |
| [H5c-brief.md](H5c-brief.md) | 0.3.0 | prompt-cache read/write pricing as their own cost line items |
| [H6-brief.md](H6-brief.md) | 0.3.0 | the `Agent`/`Task` tool and sub-agents; plan mode; session resume/fork/rename |
| [H8-brief.md](H8-brief.md) | 0.3.0 | background jobs (`BashOutput`/`TaskStop`); image/vision support; catalog vendoring; offline work-box installs |
| [H9-brief.md](H9-brief.md) | 0.3.0 | the whole-tree bug hunt; Linux-first acceptance; an MCP compatibility matrix; a randomized fuzz harness; the release itself |
| [H9b-brief.md](H9b-brief.md) | 0.3.0 | the H9 whole-tree review's own fix pass (24 findings) |
| [U0-brief.md](U0-brief.md) | 0.3.0 | the CLI flag table (Claude-Code flag parity) and the built-in slash-command scaffolding |
| [U2-brief.md](U2-brief.md) | 0.3.0 | the full-screen Textual TUI |
| [U5-brief.md](U5-brief.md) | 0.3.0 | session titles/rename/fork/export/stats; git-shadow `/rewind`; chords + the which-key overlay |
| [H10-brief.md](H10-brief.md) | 0.3.1 | free telemetry from the existing session logs; the human-gated `/improve` |
| [H11-brief.md](H11-brief.md) | 0.4.0 | the `cc:` route (your Claude subscription via the installed `claude` binary) and `ant:` aliases |
| [H12-brief.md](H12-brief.md) | 0.4.1 | `rolo-claude init`; the prescriptive `doctor`; the per-family Edit-tool context hint |
| [H13-brief.md](H13-brief.md) | 0.5.0 | lazy MCP by default; inline terminal images; `/resume` search; the first live family-baseline pass |
| [H14-brief.md](H14-brief.md) | 0.6.0 | Databricks work-config parity: the family x api_type routing table, `doctor --work` accuracy, team onboarding, the work matrix |
| [H14b-brief.md](H14b-brief.md) | -- | this documentation pass, immediately before the repo goes public |
| [V2-brief.md](V2-brief.md) | 0.7.0, 0.8.0 | Databricks-first per-family correctness (V2a); matrix-driven fixes (V2b); roles (V2c). The brief's own V2d/V2e (renaming this project to `databricks-claude`) was superseded: the owner spun off a separate `databricks-claude` repository instead, and rolo-claude stayed the general four-route harness, released as `1.0.0` -- see CHANGELOG.md's own `[1.0.0]` entry |

## Reviews

An independent pass looking for defects in already-landed code -- each
finding is `severity -- file:line -- defect -- failure scenario -- fix`.

| File | Reviewed |
|---|---|
| [review-findings-h0.md](review-findings-h0.md) | the very first pass, on the code that became [H1-brief.md](H1-brief.md) |
| [review-findings-h1.md](review-findings-h1.md) | H1 |
| [review-findings-h2.md](review-findings-h2.md) | H2 |
| [review-findings-h3.md](review-findings-h3.md) | H3 |
| [review-findings-u2-h3b.md](review-findings-u2-h3b.md) | U2 (the TUI) plus an MCP follow-up scope |
| [review-findings-h4-h5-h3c.md](review-findings-h4-h5-h3c.md) | H4, H5, and an MCP follow-up scope, together |
| [review-findings-h5b.md](review-findings-h5b.md) | the pass that produced the [H5b-brief.md](H5b-brief.md) fix list |
| [review-findings-h9-tree.md](review-findings-h9-tree.md) | the H9 whole-tree review (fixed by [H9b-brief.md](H9b-brief.md)) |
| [review-findings-h11.md](review-findings-h11.md) | H11 (fixed by the H11b pass folded into the same 0.4.0 release) |

## Acceptance records and other reference material

| File | What it is |
|---|---|
| [ACCEPTANCE-2026-09-24.md](ACCEPTANCE-2026-09-24.md), [ACCEPTANCE-2026-09-25.md](ACCEPTANCE-2026-09-25.md) | cross-platform (Windows/WSL Ubuntu/Kali VM) verification runs, with real command output, for a batch of milestones |
| [FAMILY-BASELINE-2026-09-29.md](FAMILY-BASELINE-2026-09-29.md) | live acceptance rows (pong, a 200-line Read, a Write/Edit/Bash chain, a steer) for GLM-5.3, Qwen3.8 Flash, MiniMax M3, Kimi K2.7-code and Kimi K3 on OpenRouter |
| [claude-code-2.1.281-binary-facts.md](claude-code-2.1.281-binary-facts.md) | facts about the real `claude` binary, extracted directly from it, that this project matches on purpose (trust rules, settings-merge algorithm, permission-rule grammar, ...) |
| [claude-in-chrome-integration.md](claude-in-chrome-integration.md) | design notes for the `--chrome` native-messaging bridge |
| [INSTALL.md](INSTALL.md) | the full install walkthrough -- linked from the repo-root README; read that copy, this is the same file |
| [RECOMMENDATIONS.md](RECOMMENDATIONS.md) | a point-in-time roadmap review (2026-09-25); largely superseded by later milestones landing what it recommended -- see the milestone table above for what actually shipped |
