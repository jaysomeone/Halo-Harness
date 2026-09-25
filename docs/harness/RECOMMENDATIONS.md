# rolo-claude — recommendations (Fable's review, 2026-09-25)

rolo asked for a written review, after the milestones, of what should be added or changed to make
rolo-claude work as well and as easily as possible. Everything below is grounded in what was
built and verified this week: eleven milestones, eight review passes, three platforms (Windows
build host, WSL Ubuntu, the Kali VM), and live acceptance lines against DeepSeek V4.1 Flash on
OpenRouter. Section 7 ranks the items.

## 1. Where it stands

- **v0.3.0** = the full harness: an agent loop over an append-only session log, the Claude Code
  config surface (settings and permission grammar, hooks, CLAUDE.md, rules, memory, skills,
  commands, agents, plans, keybindings, MCP servers including plugin servers), OpenRouter,
  Databricks and Anthropic routes with per-family compat profiles, sub-agents, plan mode, steering,
  background jobs, images, compaction, the Textual TUI, print mode with stream-json, `--chrome`,
  `--playwright`, and the proxy mode. Every review finding is closed.
- **v0.3.1** adds the two self-improvement items rolo chose: `stats --models` telemetry from the
  logs and the human-gated `/improve`.
- **Verified**: 1507 harness tests, 97 proxy tests and 46 TUI tests green on Windows, WSL Ubuntu
  and the Kali VM; the acceptance tables in `ACCEPTANCE-2026-09-24.md` and `ACCEPTANCE-2026-09-25.md`.
- **Not built by decision**: settings self-tuning, prompt optimisation, code self-edits. **Not
  built by scope**: `--ide`, `--worktree`, `--remote-control`, `--teleport`, claude.ai connectors
  (each prints a one-line notice).

## 2. Make first run one command (highest leverage)

The harness works once a box is set up, but setup is still several manual steps. On the VM today
it took a PATH line, a static `rg`, a venv and a catalog refresh before daily use was smooth.

1. **`rolo-claude init`** (new subcommand): pick a preset (`home` = DeepSeek V4.1 Flash via
   OpenRouter; `work` = the Databricks trio), set up the provider credentials through the existing
   config chain, run `doctor`, run `models --refresh`, send a live "pong", and offer the two Linux
   fixes doctor already knows about (a static `rg` into `~/.local/bin`, `~/.local/bin` on the PATH
   for non-interactive shells). One command, then `rolo-claude`.
2. **Prescriptive `doctor`**: every WARN line ends with the exact command that fixes it (some do
   already). Add checks for the PATH line, `$EDITOR`, tmux mouse mode, `xclip`/`wl-copy`, and the
   startup cost of each configured MCP server (see §3).
3. **README**: put a ten-line Kali quick start first; the rest of the README is complete but long.

## 3. Daily-use ergonomics on Kali

- **MCP startup cost is the biggest perceived-speed item.** Every session starts every configured
  server from the Claude Code config (plugin servers, the claude-mem vector server, others). Lazy
  start exists; make it the default with a "MCP 2/5" status-bar indicator until a server is
  touched, and consider a per-user MCP supervisor (one daemon owning long-lived stdio servers,
  sessions attaching over a local socket) so a new session costs nothing.
- **Quick questions**: document `--bare` prominently and add a `rolo-claude ask "…"` alias that
  skips hooks, MCP and memory.
- **Images in the terminal**: tool results with images are captioned today (type, size,
  dimensions). Add inline rendering via the kitty graphics protocol and sixel, with the caption as
  the fallback. Playwright and Chrome screenshots are the main users.
- **`/resume` search by text** across session titles and first prompts (the picker lists by age
  only); the titles from U5 make this cheap.
- **Edit failures are the top model-quality signal** (see §4): show the "Found multiple matches"
  count in the status bar's session stats so the user notices when a model is drifting.

## 4. Model levers per family (what the telemetry shows)

`stats --models --since 30d --all-projects` over 1474 real sessions, after the test-artifact
sessions were removed:

| Model | Sessions | Calls | Cost | Tool calls | Tool error | Edit failures | Compactions |
|---|---|---|---|---|---|---|---|
| DeepSeek V4.1 Flash (OpenRouter) | 125 | 496 | $0.68 | 476 | 9 % | 8 % | 7 |
| DeepSeek V3.2 (OpenRouter) | 7 | 16 | $0.03 | 9 | 0 % | 0 % | 0 |
| Claude Sonnet 5 (OpenRouter) | 4 | 4 | $0.09 | 0 | 0 % | 0 % | 0 |

- **DeepSeek V4.1 Flash is the right default**: cheap, fast (about 1.2 s latency in the live
  rows), reliable tool calling, and 125 sessions of evidence. Its 8 % Edit failure rate is almost
  entirely "Found multiple matches": the model sends too little context in `old_string`. The
  cheapest fix is a per-family line in the Edit tool description from `model_table.json`
  ("include at least three lines of surrounding context; the match must be unique"), then re-read
  the number in two weeks. Bash errors (4 %) are mostly `denied_by_rule` from print mode's default
  permission mode, which is expected; the README examples should show `--permission-mode auto`
  for unattended runs.
- **Kimi K3**: tool ids are kept verbatim and no sampling parameters are sent, both required by
  the API; it is the most expensive row on OpenRouter, so prefer the Databricks route at work.
- **GLM-5.3, Qwen, MiniMax**: the per-family defaults from the adapter research are in the table;
  no live volume yet. Run each for a week of real work, then set `edit_format`, output budgets and
  effort per family from the telemetry rather than from the papers. Temperature stays untouched
  for Kimi and DeepSeek thinking modes, where the API ignores or rejects it.
- **Provider pins**: rolo's OpenRouter account settings exclude DeepSeek's first-party endpoint,
  so the pins keep fallbacks enabled; `doctor` should say which provider actually answered last.
- **Model-level behaviour varies**: DeepSeek V4.1 Flash declined to echo a token-shaped string
  during acceptance. The harness adds no refusal logic of its own; pick the model for the task.

## 5. Known small leftovers

- A few GC-time "unclosed socket" warnings from the mock servers on Windows test runs (cosmetic).
- The `ant:` route and the Databricks routes are mock-verified only (no Anthropic key on this box,
  no VPN); the Databricks Claude passthrough now sends the bearer header but has not been run live.
- Two milestones lost time to worker agents editing the tree concurrently; a process note, not a
  product issue, but the whole-tree review found real defects in that code, all closed since.
- `/improve` candidate quality scales with session volume; a weekly review is the right cadence.
- The test suites leave `/tmp` unchanged now; keep the run_all guard that fails on leaked sessions.

## 6. Work-box checklist (VPN)

1. `rolo-claude doctor --work` (reachability, token validity, catalog probe).
2. `dbx:databricks-deepseek-v4-1-flash` pong, then a Read-then-Edit turn (tool loop through the
   Unity Gateway with the 32-tool cap and the schema simplifier).
3. The two open questions as probes: does Databricks forward replayed `reasoning_content` after a
   tool call (else disable thinking for tool loops on that route); does the invocations-without-
   model versus mlflow-with-`system.ai` split hold for every catalogue model.
4. Databricks Claude passthrough live (both paths, thinking replay, cache usage in the gate).
5. Offline install from `tools/vendor_wheels.py` output if PyPI is blocked; `ug` settings discovery.

## 7. Roadmap, ranked

- **P0 (this week, before daily use)**: `rolo-claude init`; prescriptive doctor lines; the Edit
  context line per family; README quick start; a weekly `/improve` review habit.
- **P1 (two to three weeks)**: lazy MCP start by default with the status indicator; the
  telemetry-driven pass over `model_table.json` once GLM, Qwen and MiniMax have real volume;
  the work-box checklist above; inline images; `/resume` search.
- **P2 (later)**: the MCP supervisor daemon; `rolo-claude plugin install` parity with the
  marketplace; `--worktree`; live validation of the `ant:` route when a key exists; a Databricks
  endpoint-catalog cache for `/model` at work.
- **P3 (declined for now, revisit with data)**: settings proposals (L2) and a prompt-optimisation
  pilot (L4) only once an evaluation suite of a few hundred tasks exists; code self-edits stay a
  no-go, as the research report argued.
