# Halo Harness roadmap (as approved by rolo through 2026-10-01 ~22:30)

Repo: github.com/roloVibes/Halo-Harness (public). rolo-claude is frozen
at v1.0.1. rolo authorised autonomous completion of every version below
and upload to GitHub; no input needed to move forward.

- 2.0.0 (DONE, tagged): rename, state-dir migration, intro.
- 2.0.1: `HALO-2.0.1-gaplist-brief.md` (rounds W1..W5 + W3 additions),
  `GLM-brief.md`, `HALO-2.0.1-liveness-tips-brief.md` (Parts A, B, C).
  Rounds: W1 run-anywhere (done, uncommitted) -> W1b launch hang +
  single executable (running) -> W2 GLM + liveness + tips + effective
  values -> W3a defects + consistency + pending-card dock + bugreport +
  timeline + mode toast + cc version pin -> W3b hygiene + model quality
  -> W4 unbuilt surfaces (thin) -> W5 coverage -> release (3 platforms,
  Kali live, Opus review, fix pass, CHANGELOG, tag v2.0.1, push).
- 2.0.2: `HALO-2.0.2-brief.md` (A three columns, B balances, C
  enumeration, C2 Codex + OpenAI incl. the Responses dialect, D jev,
  E verification, F research corrections, G learned rules + error
  translation + catalog auto-refresh + command consolidation + group
  labels + Codex pin + legacy-env WARN). Also `JEV-brief.md`,
  `RESEARCH-2.0.2.md`.
- 2.0.3: `HALO-2.0.3-brief.md` (model gym, soak test + watchdog, CI +
  release script, invariants doc + test, legacy env file no longer read,
  proxy maintained-only, alias code removed).
- 2.0.4: `HALO-2.0.4-ollama-brief.md` (local and LAN Ollama models:
  Phase 0 research doc, Phase 1 `ol:` provider on the native API with
  per-request context ownership and timing telemetry, Phase 2 hardware
  and host analysis `/ollama`, Phase 3 roles main/small/sub-agent/
  compaction + `/local`, Phase 4 gym ranking + init tab + docs/OLLAMA.md).
  Deferred by rolo: modular agents from other repos.
- 2.0.5 (approved 2026-10-01 ~23:15, "let's defer to your perspective
  on this and add those to future updates"): semantic search over
  auto-memory and sessions on a local embedding model via Ollama
  `/api/embed` (index under `~/.halo/index/`, `/recall <query>`, memory
  topic files ranked by similarity before they are read); a generic
  `local:` route for OpenAI-compatible local servers (LM Studio,
  llama.cpp server, vLLM) reusing the OpenAI compat profile with a
  configurable base URL plus per-server context discovery. Brief to be
  written when 2.0.4 ships. Modular agents from other repos stay
  undesigned until rolo asks.

## RENUMBERED 2026-10-02 (rolo, /plan): everything after 2.0.1 moves back by one

- 2.0.2 (NEW): roles v2 (planner, judge, tester, compaction, subagent_default,
  per-role effort, tab completion, templates), organizations (CEO / VPs /
  managers / workers, the release-flow org), sub-agent visibility and scale
  (/tasks, Ctrl+T, count/batch spawning, configurable concurrency and
  depth), MCP repair actions, Qwen tool calling, terminal tab title.
  Brief: `2.0.2-brief.md`.
- 2.0.3 = the old 2.0.2 (`2.0.3-brief.md`, `2.0.3-jev.md`, `2.0.3-research.md`).
- 2.0.4 = the old 2.0.3 hardening (`2.0.4-brief.md`).
- 2.0.5 = the old 2.0.4 Ollama (`2.0.5-ollama-brief.md`).
- 2.0.6 = the old 2.0.5 local embeddings + generic local: route.
- 2.0.7 (NEW): DOOM theme (section G of `2.0.2-brief.md`).
The version numbers inside the older brief files still say their old
numbers; this section is the authority.
- 2.0.8 (NEW, approved 2026-10-02): remote control of sessions from Signal,
  self-hosted through signal-cli, both identity models (dedicated number
  first, linked device documented), pairing + safety-number pinning +
  signed commands + local control socket. Brief: `2.0.8-signal-brief.md`.
- 2.0.9 (NEW, 2026-10-02): deep code review as a Halo feature (/review,
  parallel dimension reviewers + judge verification + optional fix pass,
  on the 2.0.2 roles and orgs) and a privacy/secret audit tool for the
  tree and git history with a documented remediation workflow. Brief:
  `2.0.9-review-privacy-brief.md`. The content scrub of personal fixture
  text and docs happens earlier, in 2.0.1 W5.
  2.0.9 ALSO (rolo 2026-10-03): a security review of the code itself,
  separate from the privacy audit -- the harness's own attack surface
  (credential handling and storage, subprocess and shell construction,
  path handling in the file tools, MCP and connector trust boundaries,
  OAuth token storage, the 2.0.8 remote-control crypto and control socket,
  correctness of the user's own permission rules, the install script and
  dependency supply chain, what logs and telemetry record), with findings
  fixed before the tag. This is protection of the user's machine and
  credentials; it adds no refusal or safety logic to the harness.
- 3.0.1.1 (rolo 2026-10-03, RESEARCH ONLY, starts after 2.0.9 ships): a
  secure way for separate Halo instances to work together with no cloud
  and no externally hosted server -- a team at work, friends on different
  home networks, or one person's own sessions on several machines, same
  network or not. Brief with the research questions and deliverables:
  `3.0.1.1-p2p-research-brief.md`.

## REORDERED 2026-10-03 (rolo): Ollama moves before the old 2.0.3

The brief files keep the numbers they were written under; this section is
the authority for the order after 2.0.2.
- 2.0.3 = local and LAN Ollama models (brief `2.0.5-ollama-brief.md`):
  research, the native `ol:` provider, hardware and host analysis, roles
  for local models and `/local`, init tab, docs. The model-gym ranking of
  local models that brief mentions waits for the gym itself (now 2.0.5);
  2.0.3 ships a built-in capability probe instead (context window, tool
  calling, streaming) so `/local` can still say which models can run a
  session.
- 2.0.4 = the old 2.0.3 (briefs `2.0.3-brief.md`, `2.0.3-jev.md`,
  `2.0.3-research.md`): three picker columns, balances for every provider,
  `cc:`/`ant:` enumeration, Codex and OpenAI with the Responses dialect,
  jev, learned gateway rules, error translation, catalog auto-refresh,
  command consolidation and group labels, the `cc:` route v2 (section H).
  PLUS the legacy env file no longer being read, because the 2.0.1
  CHANGELOG already announces that for 2.0.4 and the notice stays true.
- 2.0.5 = hardening (brief `2.0.4-brief.md` minus the env-file drop): model
  gym including the local-model ranking, soak test and watchdog, CI and
  release script, invariants doc and test.
- 2.0.6 embeddings, 2.0.7 DOOM theme, 2.0.8 Signal remote control, 2.0.9
  review + security review + privacy audit + front page, then the 3.0.1.1
  research: unchanged.

## ADDED 2026-10-03 (rolo): self-update and clear install steps, in 2.0.2

- 2.0.2 round 6: `halo update` on the CLI and `/update` inside halo (check,
  apply, update-and-restart with the session resumed), the installed
  commit shown by `halo --version` and `halo doctor`, a once-a-day startup
  note when an update is available, a PowerShell install one-liner, and
  README + docs/INSTALL.md rewritten as short fresh-install and update
  steps per platform. Brief: `2.0.2-round6-update-brief.md`.

