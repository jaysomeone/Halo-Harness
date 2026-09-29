# H14b brief — the documentation pass (last code-adjacent step before the public flip)

rolo (2026-09-29): "Make sure the repo has highly detailed explanations of the repo and how every
command works". This pass documents the FINAL v1 code (after H14, version 0.6.0). The repo becomes
public right after it, so: no real hostnames (use `your-workspace.cloud.databricks.com`), no LAN
addresses, no usernames, no home paths (use `~`), no tokens or key fragments. Only worker on the
tree; docs, README, CHANGELOG, `tests/test_docs_*.py` and `pyproject.toml` may change; product code
changes only to fix a doc/behaviour mismatch you find (say so in the report). No commits.

## Read first
`README.md`, `docs/harness/INSTALL.md`, `docs/harness/RECOMMENDATIONS.md`, every `docs/harness/
H*-brief.md` (what was built and why), `CHANGELOG.md`, `rolo_claude/cli.py` (the flag table and
subcommand dispatch), `rolo_claude/commands/builtins.py` + `rolo_claude/tui/slash.py` + `tui/keys.py`
(every slash command and key binding), `rolo_claude/*_cli.py`, `docs/harness/claude-help-2.1.281.txt`
(the Claude Code flags mirrored), `rolo_claude/model.py` + `providers/{profiles,cc_models}.py`,
`config/{settings,claude_json,claude_md,memory,paths}.py`, `permissions.py`, `hooks.py`, `mcp/`,
`agent/{loop,log,derive,compact,subagent,planmode,cc_runtime}.py`, `telemetry.py`, `improve/`.
Verify every claim against the code and the real `--help` output; do not describe from memory.

## Deliverables (all under `docs/` unless noted; Markdown, plain language, examples that run)
1. **README.md**: keep the Kali quick start first; then a guided tour: what rolo-claude is and is
   not (Claude Code-compatible harness; reads Claude Code's config; four routes: OpenRouter,
   Databricks, Anthropic API, Claude subscription via the claude binary); a 10-minute walkthrough
   (init, first session, /model, a tool call with a permission card, steering, /resume, /stats,
   /improve); links to every doc below; the security posture paragraph (what is never read or
   written); licence.
2. **docs/COMMANDS.md**: EVERY CLI subcommand (`rolo-claude`, `-p`, `init`, `doctor`, `models`,
   `mcp`, `config`, `stats`, `export`, `improve`, `proxy`, `sessions` if present, …) and EVERY flag
   accepted (the full Claude Code parity list + rolo-claude's own), each with: what it does, what it
   reads and writes (paths), defaults, a worked example with expected output, related config keys,
   and "not supported yet" flags listed honestly. A test `tests/test_docs_commands.py` parses
   `rolo-claude --help` and each subcommand's `--help` and fails if any subcommand or flag lacks a
   section in COMMANDS.md (and if COMMANDS.md documents a flag that does not exist).
3. **docs/SLASH-COMMANDS.md**: every `/command` in the registry (built-ins, `/models refresh`,
   `/improve`, `/stats`, `/rewind`, `/resume`, `/mcp`, `/model`, `/tasks`, …), every key binding and
   chord, the `@file#L1-20` and `!cmd` prefixes, cards (permission, question, plan, improve) and
   their keys; a test that every registered slash command has a section.
4. **docs/ARCHITECTURE.md**: the append-only session log as the source of truth and what "model-
   visible means logged" guarantees; how a request is derived; providers and compat profiles;
   reasoning replay per family; the frozen tool catalog and ToolSearch; tools and the repair layer;
   permissions grammar, modes, and what auto mode means here (allow everything except the user's
   own deny/ask rules and hooks — no heuristics by design); hooks protocol; MCP (scopes, lazy start,
   cache, plugin servers); steering, interrupts and pairing invariants; compaction; sub-agents and
   plan mode; the subscription route and its bridge; telemetry and `/improve`; the TUI event model.
   Diagrams as ASCII.
5. **docs/CONFIG.md**: exactly which Claude Code files are read and how (settings precedence,
   permissions, hooks, env, CLAUDE.md/rules/imports, memory, skills, commands, agents, plans,
   keybindings, `~/.claude.json` MCP servers, `.mcp.json` approval, plugins); rolo-claude's own
   files (`~/.rolo-claude/config.json` keys with defaults, env file, sessions, caches, `team.json`);
   every environment variable; presets; what is never written.
6. **docs/MODELS.md**: model refs (`or:`, `dbx:`, `ant:`, `cc:`, bare aliases and how they
   resolve), families and their rules (ids, reasoning replay, sampling, edit format, the Edit
   hint), the model table, catalogs and refresh, pricing and DBUs, effort, `/model` picker.
7. **docs/DATABRICKS.md**: the two-input setup (host + token), `init --preset work`, the team
   preset, discovery and cache, gateway paths per endpoint type and family with the model-id
   rules, `models --refresh --urls`, `/models refresh`, `doctor --work` and `--probe-all`, the IP
   access list note, how Claude Code's work settings are reused, troubleshooting by HTTP status.
8. **docs/TROUBLESHOOTING.md**: symptom → doctor line → fix, covering install (PEP 668, uv, pipx),
   PATH on Linux, rg, clipboard, MCP servers failing/slow, permission denials in print mode, 401/
   403/404/429/overflow errors per route, the subscription route (not logged in, API key set),
   Windows specifics, resetting caches, where logs live.
9. **docs/DEVELOPMENT.md**: repo layout, the three suites and how to run them on Windows/WSL/Kali,
   fixtures and mocks, the briefs/review/acceptance workflow, how to add a tool, a provider family,
   a slash command, a doctor check; coding rules (OS-neutral, no secrets in child env, no writes to
   Claude Code files, ≤ 250-line writes are a worker rule not a repo rule).
10. **CHANGELOG.md** tidy (one entry per release, links to briefs); `docs/harness/` index page.

## Acceptance (Fable re-runs)
`tests/test_docs_*.py` green with the suites on Windows, WSL and Kali; `grep` finds no hostname/LAN/
username/home path in `docs/` or README; every command in COMMANDS.md runs as documented (Fable
samples 10); README renders on GitHub (no broken relative links — add a link-check test).
Report ≤ 40 lines: files written, doc-vs-code mismatches found (and fixed or listed).
