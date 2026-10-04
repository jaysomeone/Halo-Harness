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
- 2.0.2 round 7 (rolo 2026-10-03): the init becomes one wizard with
  explicit Back / Skip / Next / Finish buttons (today the provider tabs
  are a separate app the user leaves with Esc before console prompts take
  over), with new Roles and Organizations steps (template presets, the
  editors embedded, skip for later) and `halo setup roles|orgs` plus
  `/setup` to reopen those screens from the CLI or inside halo. Brief:
  `2.0.2-round7-init-wizard-brief.md`.
- Round 7 additions (rolo 2026-10-03): a Theme step, roles/orgs mode
  switches, and the org features borrowed from Paperclip (templates with
  preview and install, budgets with warning and hard stop, goals on the
  task board, approval gates per position, export/import, `/org resume`).
  Later, from the same source: scheduled routines and heartbeats (cron,
  webhook, API triggers), other agent runtimes as positions (after the
  2.0.4 Codex route), a web dashboard; its private LAN/Tailscale mode
  feeds the 3.0.1.1 research.
- MOVED TWICE (rolo 2026-10-03): the Governor round runs as the LAST round of 2.0.4, after the Codex/OpenAI work ('at the tail end of the openAI update version'). The kit is vendored and reviewed in `plans/governor-import/`. Originally written as 2.0.2 round 8: the Governor, the owner's cross-process
  adaptive rate limiter from another project (report in
  `plans/governor-import/README.md`): AIMD per gateway host with
  Retry-After, priority fairness, hard in-flight cap, health-classified
  failover to a different host, and role lanes (verifier never weaker than
  coder). Ported with improvements (Windows locking, atomic state, log
  rotation, timeout classification). Brief: `2.0.2-round8-governor-brief.md`.

## ADDED 2026-10-03 (rolo): Hugging Face and cloud-hosted models join the 2.0.3 pack

- 2.0.3 covers local AND cloud-hosted models on both Ollama and Hugging
  Face: the `ol:` route reaches a local daemon, a LAN host and the
  ollama.com cloud models by host; a new `hf:` route reaches the Hugging
  Face router (Inference Providers, HF_TOKEN), dedicated Inference
  Endpoints, and local OpenAI-compatible servers running Hub models
  (llama-server, transformers serve, vLLM, TGI, LM Studio, Jan), with one
  `/local` discovery across Ollama models, the HF cache and running
  servers. Research first (how users actually run local HF models), in the
  2.0.3 round-1 research brief.


## ADDED 2026-10-04 (rolo): local-model excellence and "find and use" join 2.0.3

- rolo, after the Ollama LAN fix on the build host: "being able to use local
  llms as best as possible or better than anything out there would be a
  huge win"; the Mac has its own unshared Ollama models; Hugging Face files
  saved locally must be found and USED, with a way to tell Halo where they
  are in a session or in the wizard.
- 2.0.3 gains two rounds between round 5 and the docs round (brief
  `2.0.3-ollama-round2-brief.md`):
  - Round 5b "local-model excellence": fit calibration from Ollama's own
    `/api/ps` telemetry with learned per-host caps (fixes the remote-host
    overload seen live), optional ssh GPU read for remote hosts, stable
    request prefix for KV-cache reuse, VRAM-aware role defaults, tokens per
    second and offload in the UI, a per-OS host checklist (`halo ollama
    doctor`), Apple Silicon unified-memory handling and the Mac's loopback-
    only daemon, MLX and LM Studio detection.
  - Round 5c "find local model files and make them usable": user-named
    model folders (`huggingface.model_dirs`, `/local add`, a wizard "Local
    models" step), fit read from GGUF headers and safetensors configs,
    managed runtime child processes (`halo local serve`) with a consented,
    checksum-verified fetch of the pinned llama.cpp release when none is
    installed, and import into Ollama (`halo local import`).
- Decision recorded (rolo asked about using saved models with no server at
  all): no in-process inference by default; the managed runtime gives the
  same engine and speed without tying the model to the TUI process or
  adding compiled bindings to the install, and it still serves the LAN
  topology. Parked for 2.0.5 as an optional experiment: an in-process MLX
  backend on Apple Silicon (pip-only), measured against the managed server
  before it can become a default.

## ADDED 2026-10-04 (rolo): what makes Halo the best harness for local models

rolo asked for the ideas that give Halo a massive edge for local LLMs and GPU
hardware. Placed as follows (2.0.3 items are in the brief's Round 5b):

- 2.0.3 (Round 5b): schema-constrained tool calls on hosts that support it
  plus a one-round local repair loop; the wizard detects hardware and models
  first and recommends fits in plain sentences.
- 2.0.5 (hardening, the model gym): the gym runs on THIS machine's models
  and hardware, scores each local model per role on real Halo tasks
  (tool-call accuracy, edit success, context recall, tokens per second) and
  proposes the role table from data; a 60-second local acceptance check in
  `halo doctor` (load, tool call, structured output, compaction) with
  PASS/FAIL per model; an enforced offline mode ("nothing leaves this
  machine": only allow-listed local hosts reachable, shown in the status
  bar); hybrid escalation rules (local first, cloud when the local judge
  reports low confidence or tools fail twice) as an explicit, visible
  policy; a "saved versus cloud" meter per session; the in-process MLX
  experiment on Apple Silicon.
- 2.0.6 (embeddings): local embeddings through Ollama or a managed server
  for memory and search, same fit and calibration rules.
- 3.0.1.1 (research): peer Halo instances sharing their GPUs on a LAN
  (the Mac's small model as a helper to the PC's main model and back).

## MOVED 2026-10-04 (rolo): the local-model edge items join 2.0.3

rolo: "Let's do all that and move any you support or research in other
updates added to 2.0.3." Applied as:

- Into 2.0.3 (brief Rounds 5d and 5e): the model gym on this machine with
  `halo gym propose` for the role table, the 60-second local acceptance
  check in `halo doctor --local`, enforced offline mode, explicit hybrid
  escalation rules, the "saved versus cloud" meter.
- Stays where it was, with the reason: the in-process MLX backend stays a
  2.0.5 experiment (it can only be verified on rolo's Mac, which workers
  cannot reach); peer GPU sharing stays 3.0.1.1 research under rolo's own
  rule that it starts only after 2.0.9 ships; the embeddings pack stays
  2.0.6 as its own version (its local fit rules reuse 2.0.3's).
- 2.0.5 hardening therefore keeps: soak and watchdog, MCP connects off the
  TUI startup path, CI and release script, invariants, the carried fix-pass
  items, and the gym's cloud-model ranking extension.
- 2.0.3 round order: 5 (running) -> 5b -> 5c -> 5d -> 5e -> 6 -> Opus review
  -> fix pass -> tag v2.0.3.
- Correction the same hour (rolo: "Any GPU support or research*"): every
  GPU-related item joins 2.0.3 after all. The in-process MLX backend becomes
  2.0.3 Round 5f (experimental extra, live-verified by rolo on the Mac), and
  a docs-only GPU research round (Round 5a: vendor probes, multi-GPU fit,
  remote telemetry, the llama.cpp release matrix, KV quantization per
  backend, power draw, mlx-lm) runs right after round 5. Only the
  Halo-to-Halo collaboration research stays at 3.0.1.1. Round order:
  5 (running) -> 5a -> 5b -> 5c -> 5d -> 5e -> 5f -> 6 -> review -> fix pass
  -> tag v2.0.3.

## ADDED 2026-10-04 (rolo): Experiential Labs (experientiallabs.ai)

rolo found platform.experientiallabs.ai and asked that Halo "know how to use
this platform completely and integrate it". What it is (read from its docs
2026-10-04): an open-source (Apache-2.0, Rust) AI gateway, hosted at
`https://api.experientiallabs.ai/v1`, keys `xpl_...`, OpenAI-compatible
chat completions AND `/v1/responses` AND an Anthropic Messages endpoint
(`/v1/messages`, Claude Code connects with ANTHROPIC_BASE_URL), a catalog
of about 1,089 hosted models by slug (`claude-opus-5`, `qwen3.8-27b`) with
pricing, context, tok/s, TTFT and uptime, a per-model "waterfall" of rungs
(platform credits, BYOK, your own endpoint, a local OpenAI-compatible
server: the last two Pro-only) with request-level routing controls
(`gateway.routing.route_id`, `allow_fallbacks`, `gateway.retry`), cached-
token and `usage.cost` reporting, cost and credits APIs (`/api/v1/credits`,
`/api/v1/usage`), gateway-side tool search (`defer_loading`), structured
output with capability errors instead of silent degradation, disclosed
dropped parameters (`x-experiential-ignored-parameters`), embeddings, and a
local gateway (`exp run`) that fronts Ollama and llama.cpp. It is NOT a
model-file hub like Hugging Face: everything is hosted inference or a
gateway in front of servers you run.

Placement:
- 2.0.3 Round 5g (docs only, after 5f): `docs/harness/EXPERIENTIAL-RESEARCH.md`
  covering every docs page (quickstart, authentication, models, waterfall,
  providers guide, adding models, openai-compatibility, anthropic,
  embeddings, cost and spend APIs, plans and billing, data controls,
  telemetry, coding-agents, `exp run` local gateway config and how local
  models appear), plus the free `jev-latest` model and its relation to the
  "jev" models already seen on Databricks and OpenRouter.
- 2.0.4 round 1 (implementation, the cloud-provider pack): an `xp:<slug>`
  route on the existing openai dialect with the catalog in the picker
  (pricing, context, tok/s, TTFT, uptime columns: the same columns the
  picker work in 2.0.4 adds for every provider), `xp:` Claude slugs through
  the Anthropic Messages passthrough so thinking stays native, cost from
  `usage.cost` and the credits API feeding the balances item, waterfall
  controls mapped onto roles and the 5e escalation policy (`allow_fallbacks`
  off for pinned local-first work), the ignored-parameters header into the
  error translation and learned rules, the `/v1/responses` dialect shared
  with the Codex/OpenAI Responses work, and the local `exp run` gateway
  recognised by `/local` as an OpenAI-compatible server (note its default
  port 8000 collides with vLLM in Halo's probe; detect by `/v1/models`
  shape). BYOK rungs and the Pro-only local rung documented as the user's
  choice; Halo never signs the user up for anything.
- Live facts for the 5g research round and the 2.0.4 `xp:` route (measured
  2026-10-04 with rolo's key, which lives OUTSIDE the repo in the vibes
  folder; never copy it into any file): `GET /v1/models` returned 299 models
  for this key (the public catalog page shows about 1,089; the key's
  enabled rungs decide what the list shows); each entry carries
  `context_window_tokens`, `maximum_output_tokens`, per-million input,
  output, cached-input and cache-write costs, `supports_tools`,
  `supports_structured_output`, `supports_reasoning`, `supported_reasoning_
  efforts`, `reasoning_content_native`, `reports_cached_input_tokens`,
  `data_policy`, `owned_by`, sampling bounds; `jev-latest` answered 503
  `unavailable_route` ("ask the gateway operator to check the alias
  deployments") at that hour; `qwen3.8-27b` answered "pong" with
  `usage.cost` 0.00018208 USD, `completion_tokens_details.reasoning_tokens`
  140 of 144 completion tokens (a thinking model by default), top-level
  `provider` "experiential_cloud", top-level `cost` absent (the docs say
  top-level; the live response puts it in `usage.cost`); `GET
  /api/v1/credits` returned `{total_credits, total_usage}` in USD.
- Halo today cannot drive the gateway through the `or:` route with a base
  URL override: the gateway refuses the OpenRouter dialect's `usage` field
  ("The parameter 'usage' is not supported by this gateway profile. Remove
  the field and resend the request.") with a 400 before anything runs. So
  the 2.0.4 `xp:` route needs its own provider profile (no OpenRouter-only
  fields: `usage.include`, `provider` preferences, `transforms`), reading
  cost from `usage.cost`, and the slug grammar accepts ids without a slash.
