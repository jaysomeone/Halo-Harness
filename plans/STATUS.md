# Status board (updated 2026-10-03 ~09:55)

Legend: [x] shipped and pushed, [~] in progress, [ ] not started. The
owner authorised running the whole roadmap without approvals; every round
is verified on Windows and the Kali VM and pushed as it lands.

## 2.0.1

- [x] Part 1 (1165cca): run from any directory, launch-hang fix, single `halo` executable, `--debug` startup timeline
- [x] Part 2 (ca60e27): GLM effort sets with explicit high default, effort shown as sent, steer restart on silent calls, finish reasons, TTFT telemetry, phase events
- [x] Part 3 (914ca04): live phase line, reasoning preview, status-bar counters, tool and sub-agent elapsed, rotating tips, `/tips`, effort card
- [x] Part 4 (e326d04): Up-arrow recall, prefix filter, Ctrl+C/X/A/V in the chat box, status-bar overflow cascade
- [x] Part 5 (cc7c9a7): permission queue + pending-card dock, `halo bugreport` + `/timeline`, remembered model and effort, connect timeout, refresh-lock message, SIGUSR1 line, mode toast, Claude Code version pin
- [x] Part 6 (5df3502): credential chain for every provider, `--cwd`/`--settings` on listings, lint clean, settings.local.json indent, vendored models fallback, docs drift, Edit hint, timeline completeness, installer `--dry-run`
- [x] Part 7 (ef4351b): MCP explain-the-zero and failure reasons, claude.ai connectors bridge, `mcp serve`, import, generic `mcp login`, websocket groundwork
- [x] Part 8 (b4d53b7): 14 hook events, all 28 flags resolved, skills in sub-agents, sub-agent asks in the dock, rewind for created files, misc
- [x] Part 9 (3343f27, W4c): source-text copy, `/copy` forms, `y`/`Y`, clip.exe and pbcopy fallbacks, OSC 52 trust gate, Ctrl+C toast and `quit_on_double_ctrl_c`
- [x] Part 10 (W5a): fallback-model retry, background-job task hooks, plugin-dir skills/hooks/MCP, Bash shadow of tracked files, WorktreeRemoved, connector cold start for `halo mcp list`, --betas gating, --prompt-suggestions in stream-json, `halo bg` subcommands; process-group kills can no longer hit the harness (the Linux suite killer)
- [x] Part 11 (aa973ed, W5b): live-checks runbook and HALO_LIVE tests; connector discovery landing live in the TUI (re-kicked from the startup worker once the auth cache is primed, found live on Kali) and on request in print mode; steadier cc-session and CLI-flag tests; identifying-content scrub of fixtures and docs with a privacy scan test (git-less fallback walk)
- [x] Part 12 (1ed5b3f, W6a): release review fixes round A, all 18 critical/major findings + `--restricted`/`--betas`/`--brief` parity, each with a test (Windows 2572/0, Kali 2572/0; live Kali: `halo bg` round trip with the new wrapper, cold `mcp list`)
- [x] Part 13 (2e91814, W6b): release review fixes round B (findings 19-38 + 7 parity gaps), CLI tests hermetic (whole-run `BRIDGE_TEST_NO_BACKGROUND_NET` + logged-out `BRIDGE_TEST_CC_AUTH_STATUS`), hobby-tool scrub + case-insensitive privacy scan, CHANGELOG [2.0.1] items 11-20
- [x] Release v2.0.1 TAGGED + PUSHED: three platforms green on the final tree (Windows 2611/0 tui 212+1; WSL 2611/0 tui 213; Kali 2611/0 tui 213, real-binary modules 22/22 + 28/28 with no skips, privacy scan on the git-less copy, cold `mcp list` 4 connectors in 2 s, cold TUI note, Playwright round trip 'Example Domain')

## Side work done this session

- [x] rolo-claude repo private; Halo-Harness description and topics
- [x] README redo, `docs/HANDBOOK.md`, no mention of the old name
- [x] `scripts/install-halo.sh`, fresh-install docs, installs refreshed
- [x] `plans/` handoff, roadmap, briefs 2.0.1 to 2.0.9
- [x] `docs/harness/QWEN-RESEARCH.md` (OpenJev lead, leak-pattern defects)
- [x] `docs/harness/SIGNAL-RESEARCH.md`
- [x] First privacy pass of the full history (no real secrets; identifying content listed in `2.0.9-review-privacy-brief.md`)

## 2.0.2 (`2.0.2-brief.md`)

- [~] F + A (round 1 RUNNING, `2.0.2-round1-brief.md`): terminal title; roles v2 (planner, judge, tester, compaction, subagent_default), per-role effort, custom role names, tab completion, shell completion, role templates with editor
- [ ] B: organizations (trees, editor, `/org run`), built-in `solo`, `release-flow`, `company`
- [ ] C: `/tasks` panel with live sub-agent transcripts, configurable concurrency and depth, `count`/`batch` spawning, shared task board
- [ ] D: MCP repair actions in `/mcp`, `halo mcp fix`
- [ ] E: Qwen fix (decision-only endpoints to the judge role; leak patterns, string arguments, Coder XML, bare `</think>`, Databricks schema limits); owner bugreport still wanted for the exact error text
- [ ] Release 2.0.2

## 2.0.3 (`2.0.3-brief.md`, `2.0.3-jev.md`, `2.0.3-research.md`)

- [ ] three picker columns, [ ] balances for every provider, [ ] `cc:`/`ant:` enumeration, [ ] Codex + OpenAI + Responses dialect, [ ] jev, [ ] learned gateway rules, [ ] error translation, [ ] catalog auto-refresh, [ ] command consolidation and group labels, [ ] `cc:` route v2 (control-channel steer, set_model/set_permission_mode, /compact passthrough, native history research, context duplication audit, conformance test), [ ] release

## 2.0.4 (`2.0.4-brief.md`)

- [ ] model gym, [ ] soak test + watchdog, [ ] CI + release script, [ ] invariants doc + test, [ ] legacy env file no longer read, [ ] release

## 2.0.5 (`2.0.5-ollama-brief.md`)

- [ ] research, [ ] `ol:` provider on the native API, [ ] hardware and host analysis, [ ] roles for local models + `/local`, [ ] gym ranking, init tab, docs, [ ] release

## 2.0.6

- [ ] local embeddings + `/recall`, [ ] generic `local:` route, [ ] release

## 2.0.7

- [ ] DOOM theme with the HUD-style status bar, `/doom` toggle restoring the previous theme, [ ] release

## 2.0.8 (`2.0.8-signal-brief.md`)

- [x] research, [ ] control socket + sessions registry, [ ] transport, bridge, pairing, signing, rendering, [ ] CLI, TUI, docs, [ ] release

## 2.0.9 (`2.0.9-review-privacy-brief.md`)

- [ ] `/review` deep code review, [ ] security review of the code (`2.0.9-review-privacy-brief.md` A2), [ ] `halo audit privacy`, [ ] scrub verified and history rewrite with a mirror backup (announced, not asked), [ ] front page redo with real captures, diagrams, animations and the "why Halo" stories, [ ] release

## 3.0.1.1 (`3.0.1.1-p2p-research-brief.md`, RESEARCH ONLY, not before 2.0.9 ships)

- [ ] research round: Halo instances working together with no cloud and no hosted server (team, friends, own sessions; same network or not) -> `docs/harness/P2P-RESEARCH.md` + a draft implementation brief
