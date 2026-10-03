# Handoff: continuing Halo Harness from a cold session

This folder carries the whole plan so a new session (local or cloud) can
pick the work up without the conversation that produced it. Read this file
first, then `WORKER-RULES.md`, then the round you are starting.

## Where things stand (2026-10-02)

Halo Harness 2.0.1 is seven parts in on `master`, every part verified on
Windows and a Kali Linux VM and pushed:

| Part | Commit | What landed |
|---|---|---|
| 1 | 1165cca | run from any directory, launch without the auth hang, one executable |
| 2 | ca60e27 | GLM on Databricks effort sets, effort as sent, steer-restart, TTFT telemetry |
| 3 | 914ca04 | visible thinking phase line, live status counters, rotating tips |
| 4 | e326d04 | Up-arrow recall fixed, copy and paste in the chat box |
| 5 | cc7c9a7 | pending-card dock, `halo bugreport`, per-turn timeline, remembered model and effort |
| 6 | 5df3502 | credential resolution consistency, lint clean, timeline complete, steadier tests |
| 7 | ef4351b | MCP explains itself, claude.ai connectors bridge, `mcp serve`, generic `mcp login` |
| docs | 94dcb3f | README redo, `docs/HANDBOOK.md` |

Everything else in `2.0.1-gap-list.md` and `2.0.1-w4-plan.md` is still
open. The remaining 2.0.1 rounds, in order:

1. **W4a** (`2.0.1-w4-plan.md`, section "W4a"): the 17 hook events, the 21
   flags plus the 7 declared not applicable, skills in sub-agents,
   sub-agent asks through the dock, `/rewind` for created files and Bash
   changes, the misc items.
2. **W5** (`2.0.1-gap-list.md`, section "W5: coverage gaps"), plus the two
   items carried at the end of `2.0.1-w4-plan.md` (Linux test determinism
   for `tests/test_cc_session.py`; connector cold start in print mode).
3. **Release** (`2.0.1-gap-list.md`, section "Release"): review, fix pass,
   CHANGELOG tidy, tag `v2.0.1`, push. The Kali live checks need a machine
   with a real `claude` login; a cloud session cannot do them, so leave a
   note in the PR for the owner to run them.

After 2.0.1: `ROADMAP.md` (see its RENUMBERED section) lists 2.0.2 through 2.0.8 with their briefs here; 2.0.2 is `2.0.2-brief.md` (roles v2, organizations, sub-agent scale, MCP repair, Qwen, terminal title).

## How to work from a cold session

- Base yourself on the current `master`. Work on a branch named
  `wip/<round>` (for example `wip/w4a`), commit there, push the branch and
  open a pull request; the owner verifies on Windows and the Kali VM and
  merges. Do not push to `master` directly from a cloud session.
- Commit as the repository owner's noreply identity and end every commit
  message with the line `Co-Authored-By: Claude <noreply@anthropic.com>`
  for the model that wrote it. Never add a session link line.
- Verification before a hand-back is the three hermetic suites from the
  repo root, all green, with both guard lines reading `ok`:
  `python test_bridge.py`, `python tests/run_all.py`, `python test_tui.py`.
  Install first with `pip install -e ".[dev]"` (or `uv pip install -e .`).
  The suites need no credentials and no network beyond PyPI.
- `WORKER-RULES.md` has the standing rules: writes of at most 250 lines, no
  safety or refusal wording anywhere, never read Claude Code's credentials
  file, never write `~/.claude.json` or `~/.claude/settings.json`, every
  test scopes its home, the Databricks host in the repo is always the
  placeholder `your-workspace.cloud.databricks.com`.
- Paths in the briefs that read `<repo>`, `plans/`, `<kali-vm>` or `~` were
  the owner's machine paths; in a cloud session the repo root is the
  checkout and `plans/` is this folder.

## Files here

| File | Purpose |
|---|---|
| `WORKER-RULES.md` | standing rules, verification recipe, hand-back format |
| `ROADMAP.md` | versions 2.0.0 to 2.0.5 and which brief covers each |
| `2.0.1-gap-list.md` | the complete 2.0.1 list: W3 and W4 sections, MCP additions, W5 coverage, release steps |
| `2.0.1-glm.md`, `2.0.1-liveness-tips.md`, `2.0.1-w2c-history-clipboard.md`, `2.0.1-w3-plan.md` | rounds already shipped (reference) |
| `2.0.1-w4-plan.md` | W4a (open) and W4b (shipped), plus carried items |
| `2.0.2-brief.md` | 2.0.2: roles v2, organizations, sub-agent visibility and scale, MCP repair, Qwen tool calling, terminal title |
| `2.0.3-brief.md`, `2.0.3-jev.md`, `2.0.3-research.md` | 2.0.3: picker columns, balances, enumeration, Codex and OpenAI, jev, learned rules |
| `2.0.4-brief.md` | 2.0.4: model gym, soak test and watchdog, CI, invariants |
| `2.0.5-ollama-brief.md` | 2.0.5: local and LAN Ollama models |
| `2.0.8-signal-brief.md` | 2.0.8: remote control of sessions from Signal |
