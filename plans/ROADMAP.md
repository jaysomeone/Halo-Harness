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
