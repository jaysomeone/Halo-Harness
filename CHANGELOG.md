# Changelog

All notable changes to Halo Harness (continuing `rolo-claude`, renamed at
version 2.0.0) are recorded here, newest first. Every entry older than the
rename keeps the `rolo-claude`/`rolo_claude` names it was written under --
history, not rewritten. This project does not (yet) follow strict semver
across the 0.3.x line -- each 0.3.0 milestone below was a working
checkpoint toward the single 0.3.0 release, not a separate published
version.

## [2.0.3] - unreleased

Local and cloud models: Ollama + Hugging Face (`plans/2.0.3-ollama-round2-brief.md`
and onward). Rounds 1-5 (research, the `ol:` provider, hardware/host
analysis and roles, the `hf:` route, and `hf:local/*`/the shared `/local`
view/the init tab) landed so far, plus round 5c (finding and serving/
importing file-backed models, folded into item 4 below); round 6 (docs
polish, a live check, and release prep) is still to come under this SAME
version number.

1. **`ol:` provider on Ollama's native API** (round 2): a new `ollama`
   dialect reaches a local daemon, a named LAN host, or Ollama Cloud, all
   through the identical native `/api/chat` request shape --
   `ol:<model>` (the default host) or `ol:<model>@<hostname>` (an entry in
   `~/.halo/config.json`'s `ollama.hosts`). `options.num_ctx` is computed
   and sent on EVERY request (`min(trained context, host.max_ctx,
   131072)`, never a Modelfile default), `keep_alive` is sent only when
   the host entry configures one (the server's own `OLLAMA_KEEP_ALIVE`
   stands otherwise), and `think` is mapped from the session's effort level
   (graded low/medium/high for gpt-oss, bool for every other thinking
   model). The NDJSON streaming decoder synthesizes its own stable
   `toolu_` tool-call ids (Ollama's wire format sends none, only a 0-based
   index) and maps `done_reason: "length"` onto the existing max-tokens
   stop handling; `done_reason: "load"` retries the turn once when nothing
   has been shown yet. A background `/api/version` probe, a short-TTL
   `/api/tags` + `/api/show` catalog per host, and a cheap real-tool-call
   capability probe (cached per model digest, independent of what
   `/api/show` merely declares) round out this round. Round 2b wires the
   dialect into the agent loop itself (`agent/loop.py`'s request/stream
   dispatch, `headless.py`'s shared credential resolver) -- an `ol:` model
   now runs a full turn, including a tool call, end to end in print mode
   and the TUI.
2. **Hardware/host analysis, the fit estimate, and roles** (round 3):
   `/ollama` (TUI dialog) and `halo ollama [--host NAME] [--refresh]`
   (CLI) render one page per configured host -- reachable, version,
   loaded models' `size` vs `size_vram` as one plain offload sentence,
   trained vs. effective context, and the KV-bytes/token figure (standard
   GGML/llama.cpp accounting, not independently re-derived); `halo doctor`
   gains a one-line-per-host Ollama section. The context-ownership rule's
   `fit_estimate` (round 2 always passed `None`) is now real: the largest
   power-of-two context that fits in free GPU memory after a model's own
   weights, from the OS GPU tool for a LOCAL host (`nvidia-smi` on
   Windows/Linux, verified live this round) or a REMOTE host's own
   already-loaded `/api/ps` context when there's one to read, cached
   about a minute so a turn never shells out more than once in that
   window. The `ol:` ProviderProfile's `tools_max` now follows that
   effective context's class (under 16k/16k-32k/32k-64k/64k+ -> 16/32/
   64/128 tools, `ollama.tools_max` overridable, never below this
   platform's own built-in tool count) instead of round 2's permanently-
   unbounded `None` -- the SAME SessionCatalog cap-shrink/LRU-evict `/
   model` already runs on a provider switch does the actual shrinking,
   never a second capping path. Local `ol:` models default to a
   supporting role (the picker's new `u` action pre-selects `small`,
   never main); choosing one as the session's main model anyway prints
   the plain consequence sentence when it doesn't declare tool-calling
   support, and proceeds regardless. `/local <question>` (Ollama-only
   this round) answers from `roles.small` inline, without adding
   anything to the main transcript's context. Fix pass after a live run:
   the fit estimate now consults `/api/ps` before any GPU-memory
   arithmetic (an already-loaded model's own loaded context wins outright,
   never recomputed, so concurrent requests can no longer disagree and
   force a reload), counts other loaded models' `size_vram` as reclaimable
   headroom, and -- when the weights provably don't fit even after that --
   falls back to a conservative 8192 instead of the 131072 hard cap.
   Round 5b (local-model excellence) builds directly on this: `halo
   ollama calibrate <model> [--host NAME] [--start N]` loads a model at a
   candidate `num_ctx` and steps down by powers of two, reading `/api/ps`
   back, until it is fully resident -- the measured `max_full_gpu_ctx`
   (or "does not fit") is recorded in `~/.halo/ollama-fit.json` and never
   expires on its own (re-measured only by an explicit re-run, or
   automatically when the model's digest or the server's version
   changes); the SAME procedure runs automatically the first time a model
   is used on a host with no learned cap, with one plain notice
   (`ollama.auto_calibrate: false` opts out). The context-ownership rule
   now takes that learned cap as its highest-priority "fit" candidate
   (still just one more entry in the same `min()`, never an override that
   bypasses the trained-context/hard-cap ceiling), and a REMOTE host with
   nothing known at all -- the MUST-FIX from a live LAN-host run -- gets a
   conservative 32768 default instead of silently falling through to the
   131072 hard cap (`ollama.hosts[].max_ctx` is the documented explicit
   override). The KV-bytes-per-element constants are corrected from
   llama.cpp's own `ggml-common.h` block structs (q8_0 1.0625, q4_0
   0.5625 -- both old approximations under-counted memory by 6-12%),
   selectable per host via `ollama.hosts[].kv_cache_type`. Multi-GPU: the
   fit estimate now SUMS every detected card's free memory (never the
   minimum) minus a per-card overhead. `ollama.hosts[].ssh: "user@host"`
   adds an OPTIONAL read-only GPU probe over ssh for a remote host
   (never required). Apple Silicon's unified-memory share is estimated
   from total RAM (or `iogpu.wired_limit_mb` when set) and always
   labelled an estimate -- calibration is the ground truth there too. The
   system message/tools/`options` on an `ol:` session are now pinned
   byte-stable turn to turn (so Ollama/llama.cpp can actually reuse the
   cached prompt prefix); the status bar's model chip and `halo ollama`
   now show the last turn's own tokens/second, prefill seconds, and an
   "offloaded" marker. Round 5b part 2 (local-model excellence,
   continued): on the `ollama` dialect (local hosts only) and `hf:local/*`
   servers, a turn Halo decides is EXPECTED to call a tool (tools are
   offered, it isn't the first turn, and the last message is a tool
   result -- `providers/tool_call_schema.py`, the one documented rule
   every caller shares) sends the tool-call schema as the output
   constraint (`format` for `ol:`, alongside `tools` unchanged;
   `response_format` json_schema for `hf:local/*`), falling back to
   unconstrained decoding on a bare 400; a tool call that still fails to
   parse (bad JSON, or a resolved tool whose arguments fail schema
   validation) gets ONE isolated, tools-less local repair round -- the
   exact schema plus the parse error, re-validated before trusting it --
   before the existing plain error ever surfaces; the `ollama`/
   `huggingface` profiles also gain the generic bare-JSON/fenced-JSON
   leak-parser patterns, so a constrained reply that lands in plain text
   instead of native `tool_calls` still gets promoted. VRAM-aware role
   defaults: when the main model is `ol:` on a host where a second
   model's own weights plus the main model's CURRENTLY RESIDENT size
   would exceed the host's total GPU memory, `small`/`researcher`/
   `judge`/`subagent_default`'s TABLE value (never an explicit per-run
   `--role`/`/roles set` override) redirects to the main model instead of
   evicting it -- `/local <question>` applies the same redirection at call
   time; `halo roles`/`/roles` and the picker's `u` action show `(same as
   main: fits beside it: no)` as the reason. `halo ollama doctor [--host
   NAME]` (and `halo doctor`'s matching section) prints the documented
   host-tuning recommendations Halo cannot read back
   (`OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`,
   `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_KEEP_ALIVE`, `OLLAMA_CONTEXT_LENGTH`)
   and exactly where each lives per OS of the host (Windows tray app,
   macOS menu-bar app plus `launchctl setenv`, Linux `systemctl edit
   ollama`), with a one-line hint for a loopback-only host. The `Ollama`/
   `Hugging Face` init-wizard tabs now open with a detection summary (GPU/
   unified memory, Ollama's own models and what fits, running local
   servers, model folders known so far), filled in off the UI thread.
   `mlx_lm.server` (Apple's OpenAI-compatible MLX runtime, same default
   port family as llama-server) is told apart from llama-server by
   whether `/props` answers, labelled `MLX` in `/local`; `mlx-community/*`
   Hub-cache repos are labelled runnable through MLX; LM Studio's own
   model folder (`~/.lmstudio/models`, `huggingface.lmstudio_models_dir`
   overridable) joins the Hub-cache scan under its own group. `halo
   ollama calibrate` now also steps UP from a fitting first guess
   (bounded by the model's own trained context and the 131072 hard cap)
   to find the true ceiling instead of settling for the first lucky guess
   (`--no-up` skips it); the auto-calibrate notice now also surfaces from
   `call_small_model` (via the log, since that call path has no event
   stream of its own to protect) and `_run_compaction` (as an ordinary
   `notification` event), not just the main turn.
3. **`hf:` route -- Hugging Face Inference Providers router and dedicated
   Inference Endpoints** (round 4): a new `huggingface` provider reusing
   the existing openai-chat request/stream code unchanged (no new wire
   format -- tools supported, reasoning passthrough like OpenRouter's own,
   no host-specific fields). `hf:<org>/<model>` (optionally `:fastest`/
   `:cheapest`/`:preferred`/`:<provider>`, passed through verbatim on the
   wire) against the router (`https://router.huggingface.co/v1`,
   `Authorization: Bearer $HF_TOKEN`, `BRIDGE_HF_ROUTER_BASE_URL`
   overridable for tests); `hf:endpoint/<name>` against a fully separate
   `huggingface.endpoints` config entry's own `url`/`token` (mirroring
   `ollama.hosts`' shape) -- the two credential sources never cross-wire.
   An optional `huggingface.bill_to` org name adds `X-HF-Bill-To` on router
   requests only. The router's `GET /v1/models` catalog is cached
   (`~/.halo/huggingface-models.json`, the same TTL knob every other
   network catalog here shares) and surfaces in the `/model` picker under
   a "Hugging Face" group once enabled, with no network cost to the
   picker's first paint when the provider isn't configured. Enablement
   (`HF_TOKEN` present OR at least one endpoint configured), `/providers`/
   `halo providers`/`doctor`'s provider count, and `resolve_model_profile`
   all cover the new provider; a missing/misconfigured credential gives a
   plain message naming the right config key instead of the generic
   "OpenRouter not configured" every other unconfigured chat-dialect route
   used to get mislabeled as. `tests/helpers/mock_openai.py`'s `MockUpstream`
   gained a `path_prefix`/`expected_bearer`/`models_response` constructor
   option so the SAME scripted scenarios serve as the router and endpoint
   stand-ins, rather than a second fake server.
4. **`hf:local/*`, the shared `/local` view, the init tab** (round 5): a
   new `huggingface.local_servers` config list (`{name, url, api_key,
   default}`, mirroring `ollama.hosts`/`huggingface.endpoints`) names a
   MANUAL local/LAN OpenAI-compatible server -- `hf:local/<model>` (the
   default entry, else the first auto-detected one) or `hf:local/<model>
   @<name>`; a manual entry's own `api_key` is pinned apart from both
   `HF_TOKEN` and an endpoint's own token. Auto-detection probes `GET
   /v1/models` on 127.0.0.1 only, on llama-server/TGI's 8080, vLLM/
   `transformers serve`'s 8000 (treated identically on purpose), and LM
   Studio's 1234 -- overridable via `huggingface.local_probe_ports`/
   `HF_LOCAL_PROBE_PORTS` for tests, gated by `BRIDGE_TEST_NO_BACKGROUND_
   NET` like every other background probe; a manual entry is only ever
   probed on demand (`/local refresh`). Context length is read back from
   `/v1/models` (`max_model_len` for vLLM, other names tried heuristically)
   or, as a fallback, llama-server's own `/props` -- never requested.
   `providers.huggingface_hub_cache` walks `$HF_HUB_CACHE`/`$HF_HOME/hub`/
   `~/.cache/huggingface/hub` for models on disk but not necessarily
   served, reporting size and format (`safetensors`/`gguf`) without
   following a symlink outside the cache. The shared `/local` view
   (`providers.local_models`) merges Ollama hosts, running Hugging Face
   local servers, and the Hub cache into one grouped list -- `/local` (TUI,
   no args) opens a dialog, `halo local [--refresh]`/`/local`'s print-mode
   fallback render the same text; `/local <question>` (round 3) now also
   accepts an `hf:` `roles.small` ref, not just `ol:`. `halo init`'s
   interactive Providers step gains `Ollama (local or LAN)` and `Hugging
   Face` tabs (both additive/skippable; neither joins the OLD sequential
   `--provider`/`--preset` CLI picker, which has no sensible hardcoded
   default model for either). `tests/test_privacy_scan.py` now also scans
   untracked, non-ignored files (`git ls-files --others --exclude-
   standard`), not just tracked ones.

   Round 5c adds a fourth discovery source the user controls directly:
   `huggingface.model_dirs`, a list of folders scanned recursively
   (depth-limited, never following a symlinked subdirectory) for `.gguf`
   files and safetensors/MLX model folders (`config.json` beside
   `*.safetensors`) -- managed with `/local add <path>`/`/local forget
   <path>` (persisted immediately) or the init wizard's new "Local
   models" step (right after Providers; a folder field, a preferred-
   runtime choice, and the SAME detection summary round 5b part 2 already
   built). The fit estimate for any of these three on-disk sources (Hub
   cache, LM Studio, `model_dirs`) now comes straight from the FILE: a
   minimal GGUF header reader (`providers.gguf_header`, magic/version/
   key-value metadata, never reading past the metadata block) and a
   safetensors `config.json` reader (`providers.safetensors_config`) both
   build the identical `model_info` shape `/api/show` already produces,
   feeding round 3's `kv_bytes_per_token`/`fit_estimate` unchanged --
   `halo local` now shows format, size, trained context, quantization,
   and whether anything on this machine can actually run each file.
   Two ways to use one: `halo local serve <model> [--runtime llama-
   server|mlx_lm] [--port N] [--keep]` (or the `/local` dialog's `s` key)
   starts a managed child process on a free loopback port, recorded in
   `~/.halo/run/local-servers.json` and stopped when Halo exits unless
   `--keep`; when no runtime is found, Halo offers to fetch the pinned
   llama.cpp release for this OS/GPU backend (asset chosen from the
   NVIDIA driver's own reported CUDA version, Metal on macOS, Vulkan
   otherwise; verified against the GitHub Releases API's own per-asset
   `digest`, since llama.cpp publishes no checksum file of its own) into
   `~/.halo/runtimes/<version>/`, never on PATH, after a plain consent
   sentence (`--yes` or a stdin yes/no) -- `halo local runtime remove`
   deletes it. `halo local import <model> [--name NAME]` is the other
   way: a Modelfile (`FROM <path>`) and Ollama's own `/api/create`
   (streamed status lines as progress) turn a `.gguf` file into an
   ordinary `ol:<name>` -- GGUF only this round, since Ollama's own
   documented list of importable safetensors architectures wasn't found
   in this round's research. Either way the new model defaults to the
   `small` role, same as any other local model.

   Fix pass after a live run found two defects: the runtime fetch listed
   `GET /releases/latest`, which points at llama.cpp's own most recent
   NON-binary release (the actual compiled builds are prereleases tagged
   `b<number>`) -- Halo now lists `GET /releases` and walks it newest-tag-
   first for one that actually carries the needed asset; the CUDA pick is
   now "same major as the driver with minor <= the driver's, else the
   newest 12.x build, else Vulkan" (a driver reporting CUDA 13.2 with only
   12.4/13.4 builds available correctly falls back to 12.4, never silently
   picks a build its own minor version can't actually run), pulls in the
   paired `cudart-*` redistributable unless a CUDA toolkit is already
   installed, and the consent sentence now names the smaller `--backend
   vulkan` alternative's size; `--backend cuda|vulkan|cpu|metal` overrides
   the pick outright. Separately, the import path's `modelfile`/`FROM
   <path>` form turned out obsolete on a real daemon (`HTTP 400`,
   "neither 'from' or 'files' was specified") -- it now computes the
   file's sha256, uploads the blob (`POST /api/blobs/sha256:<hex>`,
   streamed from disk) only when `HEAD` says Ollama doesn't already have
   it, and calls `/api/create` with a `files` map naming that blob,
   exactly matching a live daemon (build 0.34.2). A second fix pass closed
   a POSIX-only zombie-process bug in the managed-server registry: a bare
   `SIGTERM` with no `wait()`/`waitpid()` left a stopped server as a
   zombie on Kali/WSL (gone in every practical sense, but still "alive" to
   a bare `os.kill(pid, 0)`) -- stopping now reaps a same-process child via
   its retained `Popen` handle, or polls `waitpid`/escalates to `SIGKILL`
   after a grace period for a fresh `halo local stop` process with no
   handle at all; Windows' own termination path is unchanged.
6. **The model gym, data-driven roles, and the 60-second acceptance
   check** (round 5d): `halo gym [--models ol:a,ol:b,...] [--roles
   small,judge,...] [--quick]` runs a fixed task battery against each
   local model on THIS machine's own hardware, through the real request/
   decode path -- tool-call accuracy (schema-valid Read calls, with
   malformed/missing ones given exactly one local repair round, counted
   separately from the headline score), edit success (a real Edit call
   applied to a scratch fixture file and diffed), context recall (a
   needle at about 12% depth of a prompt sized to the model's own fitted
   context), instruction adherence (one word when asked for one word, no
   preamble when asked for none), and tokens/second + prefill seconds
   averaged across every real turn sent. `--quick` halves the battery
   size. Results persist at `~/.halo/gym/<host-slug>/<digest>.json` with
   the digest/quantization/fitted-context/Ollama-version/timestamps they
   were measured at; `halo gym show [model]` prints a per-model card.
   `halo gym propose [--apply] [--roles ...] [--main REF]` turns those
   scores into a role-table proposal -- the best LOCAL model per
   supporting role (`small`/`researcher`/`judge`/`subagent_default`),
   weighted per role's own priorities, with the round 5b VRAM-aware rule
   applied to the winner via the existing `roles.vram_aware_override`
   (never a second mechanism); `main` is never touched. One plain
   sentence per role names the composite score behind the choice; `--apply`
   saves the proposal as an ordinary role template through the EXISTING
   `halo roles template import` path. The `/model` picker shows a saved
   gym score (and tok/s) beside a model when one exists. `halo doctor
   --local [--model ol:x]` is the 60-second "works out of the box" proof
   per machine -- load, one real tool call, one structured-output call
   (the same constrained-decoding path the repair loop uses), and one
   summary of a fixture transcript, each printed `[PASS]`/`[FAIL]` with a
   plain reason and the elapsed time, always in that order even after an
   earlier FAIL; docs point a new local-model user at this command first.
   17 new tests (`tests/test_gym_5d.py`, `tests/test_gym_propose_5d.py`,
   `tests/test_doctor_local_5d.py`), hermetic throughout -- never a real
   model.

   Fix pass (2026-10-04, live run on qwen3.8:27b, a thinking-by-default
   model): context recall and instruction adherence scored a flat,
   unexplained 0% -- a 16/32-token output budget left no room for the
   model's own reasoning before the real answer, so the reply came back
   genuinely empty (the NDJSON decoder never turns `message.thinking`
   into checked text in the first place, confirmed with a dedicated
   pin). Both tasks now request a flat 160-token budget; `halo gym
   [show] --show-replies` prints each reply-only task's actual excerpt,
   and the same excerpts are always saved in the result JSON under each
   metric's own `samples` key, so a future 0% is diagnosable without
   re-running anything; the needle check is also now case-insensitive. 5
   more tests pin the thinking/text separation, the new token budget on
   the wire, the tolerant needle match, and the samples/`--show-replies`
   round trip -- live-verified against the real qwen3.8:27b daemon
   (instruction adherence and context recall both went from 0% to 100%).
7. **`hf:mlx/<org>/<repo>` -- Apple Silicon in-process backend, experimental**
   (round 5f): an optional extra, `uv tool install "halo-harness[mlx]"`
   (`pyproject.toml`'s own `mlx` group, an environment marker restricting
   it to macOS on arm64 -- never attempted on any other platform), adds a
   route that resolves a Hugging Face Hub repo id straight to a Halo-
   managed `mlx_lm.server` on a free loopback port: started with `--model
   <repo>` (mlx-lm downloads/reuses the Hub cache itself on first use; a
   plain notice names the repo, its approximate cached size when known,
   and the cache destination), recorded in round 5c's own managed-server
   registry (`~/.halo/run/local-servers.json`), reused by an EXACT repo-id
   match on every later use (never the generic `hf:local/*` "most
   recently started wins" fallback, which would silently hand a specific
   `hf:mlx/<repo>` ref a DIFFERENT repo's server), stopped when the
   session that started it exits unless kept, and stoppable by hand with
   `halo local stop <repo>`. Rides the EXISTING `hf:local/*` tiers
   end to end (`ModelRef.local=True` alongside the new `ModelRef.mlx=True`):
   tools, the repair loop and constrained decoding, the `small`-by-default
   role, the fit arithmetic, tokens/second, `halo doctor --local --model
   hf:mlx/<repo>`, and `halo gym --models hf:mlx/<repo>,ol:<model>`; on
   any platform that isn't Apple Silicon, resolving the ref gives exactly
   one plain sentence, "MLX runs on Apple Silicon only," and nothing else
   changes (`halo doctor` itself only ever mentions the extra on Apple
   Silicon too). `halo local serve <repo> --runtime mlx_lm` is the
   explicit, non-`--model` form of the same route; the `/local` view
   labels an `mlx-community/*` Hub-cache repo with this exact `hf:mlx/*`
   ref and the explicit `--runtime mlx_lm` serve hint. `doctor_local.py`
   gained a second, provider-agnostic request sender (`providers.
   huggingface_send`, the openai-chat dialect `gym_send.py`'s Ollama-only
   sender has no branch for) so `halo doctor --local` now also accepts an
   `hf:local/*`/`hf:mlx/*` `--model`, not just `ol:`. New tests across
   `tests/test_providers_huggingface_mlx.py`, `tests/test_doctor_local_
   mlx.py`, and `tests/test_huggingface_mlx_extras.py` -- hermetic
   throughout (a fake stub process stands in for `mlx_lm.server`, the
   Apple-Silicon platform check is injectable, and the non-macOS sentence
   is pinned on this suite's own real, non-Apple-Silicon host); the live
   check is the owner running [docs/MAC.md](docs/MAC.md)'s quick-start
   on his own Mac.
8. **Trust and escalation: enforced offline mode, hybrid escalation, saved
   versus cloud** (round 5e): `halo --offline`/`/offline on|off`/
   `network.offline` make the one HTTP choke point (`providers/http.py`'s
   `open_upstream`/`urlopen_tls`) refuse any connection whose host isn't
   loopback or an allow-listed local host (every `ollama.hosts`,
   `huggingface.local_servers`, or managed-local-server-registry entry) --
   update checks, catalog refreshes, the Hugging Face router, OpenRouter,
   Databricks, Anthropic, and the WebFetch/WebSearch tools all refuse the
   same way (one plain sentence, "offline mode: not connecting to
   \<host\>", never retried); the claude.ai connectors bridge's background
   discovery is skipped instead (its real network call runs inside a
   spawned `claude` subprocess, outside this choke point) while an
   explicit `--refresh`/reconnect still runs it. A new invariant test
   greps the tracked tree for every raw `urllib`/`http.client` call site
   and fails on one outside the choke point with no listed, reasoned
   exception (today's exceptions: the MCP SDK's own httpx/websockets
   transport for `http`/`sse`/`ws` MCP servers and `mcp/oauth.py`'s token
   calls -- a separate, user-configured-server concern, not this round's
   scope). **Hybrid escalation** (`routing.escalation`: `{to, when:
   [low_confidence, tool_failures, context_overflow], ask}`) is local-
   first and only ever evaluated on an `ol:`/`hf:local/*`/`hf:mlx/*`
   session: `low_confidence` reuses the EXACT role-resolution + one-shot-
   call shape `roles.small` answers already use, pointed at the `judge`
   role instead (never a new judging mechanism); `tool_failures` counts
   `is_error` tool results in the current turn; `context_overflow` is the
   existing compaction-overflow retry path. `ask: true` (default) only
   notifies; `ask: false` switches for the rest of the turn (the two mid-
   turn triggers) or from the next model call on (`low_confidence`), with
   a transcript note; a role-table entry's own `"escalation": false` turns
   it off per role. `/escalation` shows the policy and this session's last
   decisions. **Saved versus cloud**: the cost meter also prices an `ol:`/
   `hf:local/*`/`hf:mlx/*` turn's tokens against the session's escalation
   target (or, when none is configured, the median price across the
   package's own vendored fallback catalogs -- no network call, works
   identically under `--offline`), accumulating the difference; the status
   bar's cost chip gains a " · saved $x" suffix and `/cost` prints the
   full breakdown (turns, tokens, the reference price and why). Docs:
   `docs/CONFIG.md` (`network.offline`, `routing.escalation`),
   `docs/COMMANDS.md` (`--offline`), `docs/SLASH-COMMANDS.md` (`/offline`,
   `/escalation`, the extended `/cost`), `docs/MODELS.md` (one section
   each). Manual verification on the build host: `halo --offline -p
   "reply with the single word pong" --model ol:qwen3-coder:30b` (loopback,
   succeeds) and the same with `--model or:<any>` (refuses with the plain
   sentence).
9. **`oai:` -- the real OpenAI API, chat completions and a new Responses
   dialect** (round 5i part 1, `docs/harness/OPENAI-RESEARCH.md`):
   `oai:<model>` (group "OpenAI API (key)") against `https://
   api.openai.com/v1`, `OPENAI_API_KEY` auto-enabling it the same way
   every other single-key provider does; `BRIDGE_OPENAI_BASE_URL`/
   `HALO_OPENAI_BASE_URL` overrides the base URL for tests. Chat
   completions (the default) reuses the existing OpenAI-family compat
   profile unchanged; a NEW `openai-responses` dialect (`POST /v1/
   responses`: `instructions` for the system prompt, `input` items
   including `function_call`/`function_call_output` for tool use, tools
   as flat function items, `reasoning: {effort}`, `store: false` and
   NEVER `previous_response_id` -- Halo always keeps owning the
   transcript) is wired into the same three dialect-dispatch points the
   `ollama` dialect uses, plus the small-model and compaction paths,
   selected per model by an exact-id table (`gpt-6-astra`, `gpt-6.1-sol`
   -- exactly the two ids the Responses API reference names as requiring
   it for function calling, confirmed live 2026-10-04; similarly-named
   ids the same page does not name are deliberately left on chat
   completions) and `openai.dialect_overrides` config, either direction.
   Reasoning is carried for display only, never replayed on the wire
   (the documented scope cut: `store:false` rules out
   `previous_response_id`, and encrypted-reasoning-content replay is not
   implemented this round). `GET /v1/models` cached in its own state
   file with a TTL (no price/context on this endpoint, confirmed live --
   those come from a new vendored models.dev `openai` fallback, 53 ids);
   error shapes (401, 429 `insufficient_quota`, 404) need no new mapping
   code at all -- the existing OpenAI-shaped error path already produces
   the real plain sentences, pinned with new tests instead. `/providers`
   says plainly that the OpenAI API has no public balance endpoint for
   ordinary keys and shows computed spend instead; the init wizard gets
   an "OpenAI API (key)" tab (same pattern as Hugging Face's). **No
   OpenAI key exists on the build host -- every behaviour above is
   verified against the parameterized `tests/helpers/mock_openai.py`
   fake only, including a live Read-tool round trip on the Responses
   dialect; unverified against the real API until a key is available.**
   Docs: `docs/MODELS.md` ("OpenAI API" section + the ref-form table),
   `docs/CONFIG.md` (`OPENAI_API_KEY`, `HALO_OPENAI_BASE_URL`, `openai.
   dialect_overrides`), `docs/COMMANDS.md` (`halo doctor`/`halo
   providers`).
10. **`cx:` -- the Codex subscription route, and Codex settings/AGENTS.md
    read beside Claude Code's** (round 5i part 2, `docs/harness/
    CODEX-RESEARCH.md`): `cx:<model>` (group "Codex subscription
    (ChatGPT)") drives the installed `codex` CLI headlessly under the
    user's own ChatGPT login, mirroring the `cc:` design wherever Codex's
    architecture allows it: detection is the `codex` binary on PATH plus
    `codex login status`'s plain-text answer (never `~/.codex/auth.json`;
    only `"Logged in using ChatGPT"` counts, an API-key/Bedrock/token
    login points at `oai:` instead); short aliases `astra`/`sol`/`luna`
    for the documented ChatGPT-plan ids, reusing the `oai:` route's own
    vendored models.dev catalog for context/output (price is always
    `None` -- a subscription isn't metered). Unlike `cc:` (one held-open
    `claude` process fed one stdin line per turn), `codex exec` has no
    stdin-streaming protocol at all -- each Halo turn spawns a FRESH
    `codex exec [resume <thread-id>] --json <prompt>` subprocess and runs
    it to completion, so steering is a documented fallback (queued, sent
    as its own follow-up `resume` call the moment the current turn ends,
    repeating until nothing is queued, before one `turn_done` closes the
    whole chain) rather than a live mid-turn channel. Codex keeps its own
    native shell/apply_patch tools running in its own sandbox (no flag
    disables them the way `cc:`'s `--tools ""` does) while Halo's own
    tool catalog is ADDITIONALLY exposed through an inline `-c
    mcp_servers.halo.<field>=<value>` override at the SAME bridge `cc:`
    uses -- a native Codex action is logged read-only after the fact, a
    real `mcp_servers.halo` call is dispatched through Halo's own
    permission engine/hooks exactly like every other route's tools; the
    bridge's own token/socket address ride on the subprocess's
    environment, forwarded to the MCP child by name
    (`mcp_servers.halo.env_vars`), never spelled out on Codex's command
    line. Halo's permission mode maps onto `approval_policy`/
    `sandbox_mode`: bypass/auto -> `never`/`danger-full-access`, default
    -> `on-request`/`workspace-write`, manual -> `untrusted`/`read-only`.
    Also reads (never writes) Codex's own `config.toml` (`$CODEX_HOME`
    plus a trusted project's `.codex/config.toml`, a small hand-rolled
    reader -- this repo ships no TOML dependency) and its `AGENTS.md`
    chain (global override-or-plain, then the SAME rule from the git
    root down to cwd, 32 KiB cap -- a DIFFERENT walk than Halo's existing
    CLAUDE.md/AGENTS.md loader, kept deliberately separate) into one
    merged view beside Claude Code's own settings/CLAUDE.md
    (`providers/settings_merge.py`): non-overlapping entries merge (a
    Codex-only MCP server joins the list, its AGENTS.md is its own
    block); overlapping entries follow halo's own config, then Claude
    Code, then Codex, except on a live `cx:` session where Codex's own
    model/reasoning-effort/approval-and-sandbox policy leads;
    `settings.primary: "claude"|"codex"` flips the Claude-Code-vs-Codex
    half of that order. New `/settings [primary claude|codex]` command,
    `halo doctor`'s `codex_settings` line, and the init wizard's
    "Settings sources" step all show/set the same merged view. **No one
    is logged into Codex on the build host -- every behaviour above is
    verified against the parameterized `tests/helpers/fake_codex.py`
    only, including a real MCP tool-call round trip through the bridge
    and the steer-fallback's follow-up `resume` call; unverified against
    the real CLI until a ChatGPT login is available.** Docs:
    `docs/MODELS.md` ("Codex subscription (ChatGPT)" and "Codex settings
    and instructions" sections + the ref-form/alias tables),
    `docs/CONFIG.md` (the Codex-files section, `settings.primary`),
    `docs/COMMANDS.md` (`halo models --cx`, the init wizard tabs/step),
    `docs/SLASH-COMMANDS.md` (`/settings`, the `/providers` row).

## [2.0.2] - 2026-10-04

W7 rounds 1-7 of the 2.0.2 brief (F, A, B, C, D, E, then the init wizard):
the terminal tab title fix, roles v2, organizations, sub-agent visibility
and scale, MCP repair actions, Qwen tool calling at work, `halo update`,
and one init wizard with Back/Skip/Next buttons plus Roles/Organizations
setup screens.

1. **Terminal tab title stays `halo`**: root cause confirmed by reading
   every `claude`-spawning call site -- each one pipes the child's stdout/
   stderr for its own parsing but never detaches it from the shared
   console/tty, and a real `claude` binary sets the console/terminal title
   itself at startup through a side channel independent of those redirected
   handles (Windows: the console title is a property of the console object
   any attached process can set, regardless of its own stdout; POSIX: an
   OSC 2 write most terminals honour however it arrives) and never restores
   it -- while Textual's own `BridgeApp.TITLE` has never touched the real
   terminal at all (it's only ever the in-app Header widget's text).
   `halo_harness.termtitle` (new) re-asserts `halo` via OSC 2 plus, on
   Windows, a `SetConsoleTitleW` fallback for legacy `conhost`: at TUI
   start and on `/resume`, after every `claude` child exits
   (`agent/cc_process.py`, `providers/cc_models.py`'s probes, `mcp/
   connectors.py`'s discovery), and once more on the drain tick that
   follows one, as insurance. Print mode (`-p`) saves whatever title was
   there at start and restores it at exit; the OSC 2 write itself is
   skipped on a non-interactive default stdout (never spliced into a
   `--output-format json/stream-json` consumer's own piped output).
2. **Roles v2**: `ROLE_NAMES` grows from five to ten -- `planner` (the
   `Plan` agent's own role now, moved off `reviewer`), `judge` and `tester`
   (two new built-in agents with matching roles), `compaction` (a rung
   between `compactionModel` and the session's main model), and
   `subagent_default` (the last rung before the session model, for a
   sub-agent with no role at all). A role's table value may now be
   `{"model", "effort"}` instead of a bare string, applied through the same
   path the session's own effort takes; `/roles` and the role resolver show
   `requested (sent as X)` when a role's effort maps to a different one on
   its route. Any syntactically-valid custom role name (`[a-z][a-z0-9_]*`)
   a team.json or a loaded template defines is now a valid role everywhere
   a built-in one is -- `--role NAME=MODEL[:EFFORT]`, `/role <name> <model>
   [effort]` (new) and `/roles set` all validate against the live set of
   known names. TUI tab completion for `/role`/`/roles set`'s arguments
   (role name, then model ref, then effort -- ranked prefix-then-substring,
   `tui/completion.py::filter_items`) and shell completion (`halo
   completion bash|zsh|powershell`, reading the cached model catalog with
   no network call) are both new. Role TEMPLATES
   (`~/.halo/roles/<name>.json`) are a new named, reusable role table:
   `/roles templates|save|load|new|edit|show` in the TUI (`edit` opens a
   form, `tui/dialogs/roles_editor.py`, or `$EDITOR` when `roles.editor:
   "external"` is configured) and `halo roles template list|save|load|new|
   edit|show` from the CLI.
3. **Organizations**: `halo_harness/orgs.py` (new) -- a named, reusable
   TREE of sub-agent positions (`role`/`model`, `effort`, `instructions`,
   a tool allowlist, and the OTHER positions it may itself delegate to)
   saved as `~/.halo/orgs/<name>.json`, with exactly one root (the title
   nobody else's own `reports` names). Three built-ins ship copied in on
   first use and are never overwritten once present: `solo` (one
   orchestrator), `release-flow` (the brief -> implement -> test -> review
   -> fix -> retest -> report loop this project is itself built with --
   `Fixer` delegates back to the SAME `Tester` position `Implementer`
   delegated to earlier, a reused edge rather than a second copy), and
   `company` (CEO -> three VPs -> one manager each -> two workers each).
   Running one (`/org run <name> "<goal>"`, `Agent(org=<name>,
   prompt=<goal>)`, or `halo org run <name> "<goal>"`) turns every position
   into a real `AgentSpec` and spawns the root through the EXACT sub-agent
   machinery an ordinary `Agent` call already uses -- each position's own
   `reports` becomes its `agent_type_restriction` (a call outside it is
   refused with a clear tool error, the same check a `tools:
   ["Agent(name)"]`-restricted agent file already enforced, now actually
   reaching a child for the first time), depth comes from the org's own
   tree shape instead of the usual depth-1 cap, and concurrency is the new
   `agents.max_concurrent` config knob (default 4, applies to every
   session now, org or not) unless the org sets its own. The dock shows a
   running position as `<title> (<role>)` instead of the bare title
   (`AgentSpec.dock_label`). `/org list|show|new|load|edit|run` in the TUI
   (`edit` opens a tree-view-left/fields-right form, `tui/dialogs/
   org_editor.py`; `load` re-installs a built-in over a local copy) and
   `halo org list|show|new|edit|run` from the CLI. See
   [docs/ORGS.md](docs/ORGS.md).
4. **Sub-agent visibility and scale**: `/tasks`/Ctrl+T (new, `tui/dialogs/
   tasks.py`) -- a full-height panel listing every running, queued,
   background and finished sub-agent (and background job) of this
   session, an org run's own descendants indented under their parent;
   Enter opens a live, follow-mode transcript viewer of that agent's own
   log (`PgUp`/`PgDn` scroll, `o` the full pager); a second tab shows the
   new shared task board. The status bar shows `agents N` (running
   count) when N > 0. `agents.max_depth` (config, default 1, up to 3; an
   org's own tree shape overrides it, unclamped) joins `agents.max_
   concurrent` as a real config knob instead of a hard-coded constant.
   The Agent tool gains `count` (N identical copies of `prompt`) and
   `batch` (a list of `{prompt, role?, model?, effort?}` objects) --
   spawns several sub-agents in one call, capped at `agents.max_
   concurrent` (excess ones queue, announced to the panel immediately via
   a new `subagent_queued` event, and start as slots free); the parent
   waits on all of them and gets back one combined result, one
   `<task_result>` section per child, in spawn order. A new shared task
   board (`TaskCreate`/`TaskUpdate`/`TaskList` tools, `~/.halo/sessions/
   <id>/tasks.json`) lets every sub-agent of a session -- an
   organization's workers, most usefully -- claim and report on open
   work; claiming an already-claimed task is refused, never a lost
   update. Two `/org run` gaps closed on the way: its own result used to
   land in the transcript as a raw `<task_result task_id="...">` block
   with no live progress at all (it now streams through the same live
   sub-agent machinery an `Agent(org=...)` tool call already uses, so the
   root position -- and everything it delegates to -- gets a real card
   with its own position title, and the final text is unwrapped before
   display); and an org run's own spend, though it was already rolling
   into the session's cost meter correctly, never reached the status bar
   (nothing re-reads it once a slash command finishes outside the normal
   turn loop) -- it now pushes a fresh status refresh once the run
   completes. See [docs/SUBAGENTS.md](docs/SUBAGENTS.md).
5. **MCP repair actions**: `/mcp` gains a key legend plus the rest of the
   repair surface -- `R` reconnect every server, `l` login (the 2.0.1
   OAuth flow for a local `http`/`sse` server, or the claude.ai connector
   re-auth pointer), `L` a tail of `~/.halo/mcp/<server>.log` (stdio
   stderr PLUS connect/transport errors, both appended to the one file
   now, rotated at 1 MB instead of 5), `e` edit the entry at its own line
   in `$EDITOR`, or an inline form with no `$EDITOR` set (`tui/dialogs/
   mcp_entry_form.py`, new), `i` an install hint for a command-not-found
   server guessed from the missing command (`npx`/`node`/`uvx`/`uv`/
   `pipx`/`pip`/`python`), `d` disable/enable per directory (Claude
   Code's own `disabledMcpServers`), and `t` a timed `tools/list` round
   trip. Every failed/needs_auth/pending_approval/disabled row carries
   its reason and one fix line (`mcp_cli.fix_line_for`, shared code --
   `halo mcp fix <name> [--apply]` prints and, with `--apply`, runs the
   exact same diagnosis from the CLI; `halo mcp test <name>` is `t` from
   the CLI). A server that dies mid-session reconnects on its next use
   with backoff (1, 2, 4, 8s, then every 30s, giving up after 10 minutes)
   instead of retrying on every single call; the row shows the attempt
   count and next retry, and a manual `r`/`R`/`--apply` always resets it.
   `(McpManager.reconnect_manual`/`McpServerHandle` backoff fields, `mcp/
   manager.py`.) Fixed along the way: approving a pending `.mcp.json`
   server from `halo mcp fix --apply` left the live handle's own stale
   `pending_approval` flag set, so the reconnect that's supposed to
   follow silently no-op'd every time (the TUI's own `a` action already
   cleared it; this CLI path now does too). See the `/mcp` section and
   `halo mcp fix`/`test` in [docs/COMMANDS.md](docs/COMMANDS.md), and the
   "a server shows failed" walkthrough in
   [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md). Two round D fixes
   on the way out: `e`'s external-editor launch no longer passes vim's
   own `+<line>` argument to every `$EDITOR` -- `code`/`subl` get their
   own line syntax now (`--goto file:line`/`file:line`), anything else
   opens the file plain; `halo mcp learned [--forget <endpoint>]` lists
   (or clears, before its TTL) a learned per-endpoint provider rule
   (`providers/learned_rules.py`, round 5's own "tools_rejected" cache).
6. **Qwen tool calling at work**: the owner's work-VM "openjev qwen"
   report traced to `databricks-openjev-qwen35-4b`, Databricks' own
   decision-only "evaluates yes/no, choice, and scoring questions"
   endpoint -- never a chat/tool-calling model. Classified from DATA
   (`model_table.json`'s top-level `decision_only_name_patterns` substring
   net, plus the row's own `capabilities.decision_only`), never a
   hardcoded id check: the `/model` picker now groups it under "Databricks
   (judge / decision)" with that one-line description, `/model`/picking it
   never installs it as the session model -- it's routed to the `judge`
   role automatically instead, with a plain notice -- and a request that
   still carries `tools` for it (or for ANY Databricks endpoint a live
   request already proved rejects tools, learned per-endpoint in
   `~/.halo/learned-rules.json`, even with no table row at all) gets one
   clear error naming the model and the judge role instead of a wire 400
   (`providers.request.ToolsNotSupported`; the live-400 twin lives in
   `agent/loop.py` via the new `providers.errors.is_tools_rejected_
   message`). Separately, two leak-repair patterns every Qwen row had
   declared since H2 but `providers/hooks.py` never implemented are now
   real: `python_repr_args` (a bare single-quoted Python-dict-literal tool
   call, no wrapper at all) and `missing_tool_call_opener` (Qwen3-Coder
   #475's own shape -- a `}</tool_call>` tail with no opening tag).
   `think_tag_strip` now also strips a bare leading `</think>` with no
   opener (Qwen3-235B-Thinking-2507/QwQ's documented replay shape), and
   `providers/request.py`'s Databricks schema simplifier now rewrites a
   forbidden `prefixItems` keyword to `items` plus a `description` note
   instead of leaving it on the wire (the 16-key cap was already enforced
   by the time this round started -- the research doc's claim otherwise
   was stale, not a live defect). See
   [docs/harness/QWEN-RESEARCH.md](docs/harness/QWEN-RESEARCH.md),
   [docs/harness/FAMILY-BASELINE-qwen.md](docs/harness/FAMILY-BASELINE-qwen.md)
   and the new "Qwen at work" section in
   [docs/MODELS.md](docs/MODELS.md). Still unconfirmed until the owner
   sends a real `halo bugreport`: the exact endpoint/HTTP body, whether a
   400 or a silently-ignored 200 is what it actually returns for a
   tool-bearing call, and whether `tools` were present on the first
   failing turn or a later one.

7. **`halo update` and `/update`**: `halo_harness/update.py` (new) knows
   what's installed (PEP 610 `direct_url.json` -- commit and requested
   revision for a git install, `git rev-parse`/`git branch` in the
   checkout for an editable install or a bare PYTHONPATH run) and what's
   available upstream (`git ls-remote` against GitHub, or the REST API
   with no git on PATH; cached 24h in `~/.halo/update-check.json`,
   honouring `update.check: false` and `BRIDGE_TEST_NO_BACKGROUND_NET`).
   `halo --version` now prints `halo 2.0.2 (c93480d, master)` once the
   commit is known; `halo doctor` gains an "install" line naming how,
   from where, and which commit. `halo update --check` reports installed
   vs. available, the commits between them, and the exact reinstall
   command, exiting 0/10/1 (up to date/available/unknown); `halo update`
   (apply, `update_cli.py`) refuses while another halo process looks like
   it's running on this machine (excluding itself) unless `--force` --
   a reinstall under a running TUI has broken the install on Windows
   before -- then runs the command live and reports the before/after
   commit from a fresh `halo --version`. `/update` in the TUI runs the
   same check on a worker and opens a dialog with `Enter: update and
   restart halo` / `Esc: not now`; Enter exits the TUI with a sentinel
   return code, and `cli.main` -- only now past the TUI, which has
   already torn down -- runs the update with its output visible and
   relaunches with `--continue`, the only safe order on Windows (quit,
   then update, then relaunch). A cached "update available" note shows
   once a day at TUI startup, off with `update.check`/`update.notify:
   false`. `scripts/install-halo.ps1` (new) mirrors `install-halo.sh` for
   Windows PowerShell. README/docs/INSTALL.md gain matching Install and
   Update sections; docs/INSTALL.md also gains an Uninstall section.
8. **One init wizard, Roles/Organizations setup screens**: owner report
   -- "I have to press esc then it exits then brings up the next
   section ... there should be a button you select to move it forward
   ... there should be an init step that lets you set up roles first too
   or skip for later ... a setup screen should pop up to set those
   features up in addition to doing it within halo ... selection of
   templates would be good". `tui/dialogs/init_wizard.py` (new): ONE
   Textual app for the whole interactive `halo init`, `Step N of M: <name>`
   header, `Back`/`Skip`/`Next` (`Finish` on the last step) footer, Esc
   asking "Quit setup? What you saved so far stays" instead of ending
   silently -- Providers (the existing tabs content, moved in), Default
   model, Permission mode, **Theme** (new: the six built-in themes, a
   live preview), **Roles** (new: a `roles.enabled` switch, default on,
   three shipped presets -- `balanced`/`quality`/`local-first` -- with a
   preview, `Edit roles...` opening the round-1 form inline), **Organizations**
   (new: an `orgs.enabled` switch, default off, a default-org pick,
   `Edit org...` opening the round-2 form inline), Linux fixes (skipped
   automatically when nothing needs fixing), Summary. Both modes gate
   discoverability only (`/roles`/`/role`/`/org` hidden from `/help`/
   completion/the rotating tips, the `/tasks` board tab, and the `Agent`
   tool's own `org=` parameter while off) -- never a functional gate; an
   org/role invoked by name still works either way. `halo setup
   [roles|orgs]` (new `setup_cli.py`) and `/setup [roles|orgs]` (`tui/
   slash.py`) reopen the Roles/Organizations screens later -- a bare
   `halo setup`/`/setup` chains roles -> orgs -> a short summary; with no
   TTY, `halo setup` prints the current roles table/orgs list and exits
   0. The round-1 roles editor and round-2 org editor both gain a "start
   from a template" picker at their top (roles also gains `Save as
   template...`). Paperclip-derived additions to organizations (brief
   3b): **budgets** (`budget_usd` on an org and/or a position, enforced
   through the existing cost meter -- a hard stop refuses a FURTHER
   delegation once reached, a warning at 80%; the call already running
   always finishes) and **goals** (`/org run`'s own goal becomes a root
   task, `kind: "goal"`, on the shared board before the root position
   runs; every position is told to link its own tasks to it via
   `TaskCreate`'s new `parent`). `orgs.default` (`/org run`/`halo org
   run` with no name) is new too. The rest of brief 3b lands this round
   too: `halo org install <name> [--force]`/`/org install` copies a
   shipped built-in or a template saved under `~/.halo/org-templates/`
   into `~/.halo/orgs/`, refusing to overwrite without `--force` -- each
   shipped template's own `description` IS its one-line README, shown by
   `/org list`/`halo org list` (which now prints it) and the wizard's
   Organizations step (already did, via the tree preview). **Approval
   gates**: a position with `requires_approval: true` holds its just-
   finished result as a pending card in the SAME dock a permission/
   question/plan-review ask already uses (accept, edit the instruction
   and re-run -- recurses through the ordinary spawn path, capped at 5
   revisions -- or stop); with no live dock reachable (`halo org run`/
   `resume` from a plain terminal) it prints the result and waits on
   stdin instead, the same one-line-read convention `halo init`'s own
   non-interactive prompts use; `--yes` on `run`/`resume`, or the
   session's own `dontAsk` permission mode, accepts every gate
   automatically -- the OPPOSITE of `dontAsk`'s usual "ask converts to
   deny" rule, since a gate is never a tool-permission ask.
   **Export/import**: `halo org export <name> [file]` / `import <file>`
   move an org as plain JSON (import validates role names, reports and
   budgets, listing every problem); `halo roles template export|import`
   is the same pair for role templates. **`/org resume`** (and `halo org
   resume <session-id>`) continues an interrupted run from a session's
   own saved run record (`<session_dir>/org-run.json`, written when the
   run starts) and shared task board -- open and claimed tasks become
   the new root position's own work list, done tasks are named and kept
   (never redone); the resumed run's own `max_concurrent`/`budget_usd`
   come from that saved record, not whatever the org definition
   currently says, so an edit made after the original run started can
   never change what the resumed run is bound by.
9. **Release review, two fix rounds**: an Opus review of the whole
   `v2.0.1..` diff found 3 critical, 15 major and 21 minor defects, each
   with a check for whether it was actually confirmed by running or
   tracing the code; both fix rounds closed every one with a test that
   failed before and passed after. Round A (criticals/majors): `halo
   init`'s Default model step
   no longer saves the first catalog row when you press Next without
   picking (RolesStep's own "Next overwrites your current roles" bug,
   reintroduced by the wizard, closed the same way); a decision-only
   endpoint can no longer become the session default from the picker or
   a remembered model, and the Judge sub-agent runs tool-less against one
   instead of raising `ToolsNotSupported`. `halo update`/`/update` work
   on Windows now -- the uv launcher that stays alive as this process's
   own parent is no longer mistaken for another session, and an
   editable/checkout install's `git pull` + reinstall runs in the
   checkout, never whatever directory you happened to be sitting in.
   `/org run` and a resumed org task no longer take the TUI down on an
   unresolvable model, org budgets are enforced against a real per-org
   total instead of a baseline that never moved, a delegating org's
   nested sub-agent cards keep their own identity instead of all ticking
   under the root's, org depth is relative to the caller's own depth, and
   the per-call concurrency cap became one session-wide (or org-wide)
   gate so two fan-out calls in the same turn -- or a nested one -- can
   no longer double the configured cap. MCP repair: OAuth login from
   `/mcp` can be cancelled and the dialog dismissed while it waits, the
   inline entry form's args field accepts spaces and several lines
   without splitting them, and `R` skips a server you disabled. The
   `/tasks` panel refreshes on a worker thread with a cached parse per
   log file instead of re-reading every agent log on the UI thread every
   second. Roles: the `small` role is actually consulted now, and
   `roles.enabled`/`roles.editor` stop being misread as role names.
   Round B (the minors): the `/mcp` dialog re-reads each server's real
   state from the Controller after an action instead of guessing it from
   the result text (a failed `t` no longer flips a row to "connected");
   `/tasks`' transcript viewer keeps your scroll position while the log
   keeps growing, and its own board tab is reachable with `orgs.enabled`
   off too, since the board itself always worked. `count`/`batch` apply
   the call's own `effort`, reject an unknown `role` by name (listing the
   known ones, the same as an unknown `subagent_type` already did) on
   every surface (single spawn, fan-out, resume), and their combined
   result is capped and spilled to disk like every other tool's instead
   of returning megabytes raw. `/role`/`/roles set` now land where the
   model-resolution chain actually treats as winning over an agent file's
   own `model:`, with the model and effort validated; its own effort
   completion reads the model you actually typed, not the role name or
   the literal word "set". An org run's sub-agents list parent-before-
   children instead of by whatever order their random ids sorted into,
   and the tasks panel restores your highlighted row by id, not index.
   The `balanced`/`quality`/`local-first` presets skip a free/negative-
   priced or tool-less catalog entry instead of picking OpenRouter's own
   router alias as "the cheapest model". An org's root position is
   visible to the tasks panel's live cost/phase lookup now (it used to
   get its own disconnected bookkeeping). `halo update`'s relaunch on
   Windows waits for a real child process instead of exiting this one
   outright and racing the console with it. The real terminal title is
   written through Textual's own output thread while the TUI runs
   instead of racing its frames directly on `stdout`. A learned "this
   endpoint rejects tool calls" rule now expires after a day instead of
   needing a hand edit to `learned-rules.json` to undo. The `Agent` tool's
   own description lists the live, configured role names instead of a
   stale fixed five, and no longer claims a sub-agent can never delegate
   further (true only at the default depth). The shared task board
   survives an interrupted write (atomic replace) and keeps a corrupted
   one aside instead of silently replacing it with a single fresh task.
   A long tool-less Qwen answer no longer costs seconds in a leak-
   pattern regex. `halo org edit`/`halo roles template edit` no longer
   crash with a bare traceback when `$EDITOR` is multi-word (`"code
   --wait"`) or needs Windows' own PATH extension resolution. `halo
   completion` lists `update`/`org`/`setup` (missing outright before) and
   its zsh/bash scripts handle a `:`-bearing model ref correctly. The org
   editor's Ctrl+D (delete position) now reaches the screen even with a
   field focused -- Textual's own Input/TextArea binding AND an unrelated
   app-level "quit on empty/delete-right in the chat box" binding were
   both intercepting it first -- and a bare model alias typed into "Role
   or model" (`haiku`, `sonnet`) saves as a model, never a role that then
   fails validation. `/org run <name> "<goal>"` strips the quotes from
   the goal instead of sending and recording them literally.

## [2.0.0] - 2026-10-01

The rename release: `rolo-claude` 1.0.1 continues unchanged, as its own
repository; this one, Halo Harness, is where every later feature lands.
Nothing about runtime *behavior* changes here beyond the migration and
alias notices below -- every 1.0.1 fix ships exactly as it was.

1. **Product renamed to Halo Harness**: console script `halo` (`python -m
   halo_harness`); distribution `halo-harness` on PyPI (`halo` itself is a
   spinner library -- never collide); Python package `halo_harness`
   (`rolo_claude` renamed in place, every import/reference updated).
   `rolo-claude` keeps working forever as a deprecated console-script
   alias -- one notice line to stderr (`halo: 'rolo-claude' is deprecated,
   use 'halo' instead`), then runs exactly like `halo`, same process, same
   parser, including `rolo-claude proxy`. `halo proxy ...` is the renamed
   spelling of what `rolo-claude proxy`/`claude-bridge` already did
   (`bridge.py`, unchanged).
2. **State directory migration**: the default state dir is now `~/.halo`
   (was `~/.rolo-claude`). The first time anything in a fresh Halo Harness
   process resolves it, an existing `~/.rolo-claude` is renamed (never
   copied) to `~/.halo`, announced with one stderr line; every call after
   that (in this or any later process) just finds `~/.halo` already there
   and says nothing further. No link is created at the old location (`rm
   -rf ~/.rolo-claude/` with a trailing slash follows a symlink/junction
   and would empty `~/.halo` right along with it) -- a still-installed
   `rolo-claude` 1.0.1 must never be run again after this, since it would
   start from a truly empty `~/.rolo-claude`. A rename that fails (a
   locked file on Windows, a read-only/cross-device home on Linux) prints
   one warning and keeps using `~/.rolo-claude` for that run instead of
   silently losing state in a `~/.halo` that was never actually populated;
   when the rename instead loses a race to a concurrent process that
   migrates first, the `~/.halo` it left behind is adopted silently.
   `halo doctor` reports a standing WARN whenever both directories end up
   holding real data. `BRIDGE_STATE_DIR` keeps overriding the state dir
   outright, exactly as before (no migration logic runs when it's set).
   Test seams (`BRIDGE_TEST_HOME`, `BRIDGE_TEST_NO_BACKGROUND_NET`,
   `BRIDGE_TEST_CC_AUTH_STATUS`, and `BRIDGE_STATE_DIR` itself) keep their
   exact names -- no `HALO_` twin for these, by design.
3. **Env file**: `halo init` now writes `~/.config/halo/env` (was
   `~/.config/vibes-hacker/env`), copying the old file's content forward
   (directory 0700, file 0600) the first time it writes on a box where
   only the old one exists yet, so nothing already configured there is
   orphaned. The old file is never modified or renamed (other tools on
   the same box read it); the copied-forward new file starts with an
   import marker line, and once that marker is present the old file is
   no longer consulted, so a key you delete from the new file stays
   deleted. Without the marker (a hand-written new file) reading still
   checks the new file, then the old one, so a credential that only
   ever lived in the old file keeps working even before `halo init`
   runs again. `HALO_ENV_FILE` is the new override
   name; legacy `BRIDGE_ENV_FILE` keeps working (an explicit override
   always wins outright, with no blending between the two files).
4. **Env vars**: every harness-owned `BRIDGE_*`/`ROLO_CLAUDE_*` knob
   (`OPENROUTER_BASE_URL`, `ANTHROPIC_BASE_URL`, `DBX_BASE_URL`/
   `DBX_TOKEN`, `MODEL`/`MODEL_SMALL`, `ENV_FILE`, `CLAUDE_EXE`, `DUMP`,
   `CA_BUNDLE`, `THEME`) now has a canonical `HALO_*` name, checked first;
   the old name still works, logging one DEBUG line the first time it's
   what actually supplied the value. See `docs/CONFIG.md`'s "Every
   environment variable" table for the full list.
5. **Launch intro**: a fresh interactive launch (TUI, including
   `--continue`/`--resume`) types out `I am just a copy, of a copy, of a
   copy... halo 2.0.0` character by character above the first turn, like
   someone typing it, with a block cursor while typing; any keypress or a
   submitted prompt finishes it instantly, and the prompt input keeps
   focus throughout so nothing typed during it is ever lost. Never shown
   in print mode, a `--demo` run, or when stdout isn't a real terminal;
   `"intro": false` in `~/.halo/config.json`, or `--no-intro`, turns it
   off for good; `/intro` replays it mid-session.
6. **Compatibility odds and ends**: a project's `.rolo-claude/team.json`
   (checked into a repo before this rename) is still found, with one
   deprecation line, when `.halo/team.json` is absent. `/improve`'s
   provenance marker and the `~/.local/bin`-on-PATH rc-file marker both
   still recognize the exact text 1.0.1 wrote (a new write always uses the
   new text) -- so a file or rc-file line 1.0.1 already produced is never
   mistaken for user-authored, and `halo init` re-run on an upgraded box
   never appends a second PATH block. The `stream-json` `system/init` line's
   version field is now `halo_harness_version`; `rolo_claude_version` (same
   value) ships alongside it for a script that already reads the old name.
   The state directory itself (item 2) gets no compatibility link of its
   own, unlike these -- no link is created there, on purpose, since a
   leftover link would make `rm -rf ~/.rolo-claude/` dangerous.
7. **Docs/tests**: README/CHANGELOG/docs/briefs/guard tests all updated to
   the new names; this entry is the only CHANGELOG section written under
   them -- every entry below keeps the name it was written under.
8. **Shadow-repo long paths on Windows**: the `/rewind` git-shadow repo
   (`~/.halo/sessions/<slug>/<session_id>/shadow/`) now sets `core.
   longpaths true` right after `git init` on Windows -- the shadow repo's
   own path plus a mangled absolute source path underneath it can exceed
   Windows' 260-character limit under a long home or cwd, which git itself
   refuses without this setting.

## [2.0.1] - 2026-10-03

The "run from any directory" release: `halo` already discovered a
directory's `CLAUDE.md` chain, `.claude/rules`, settings, `.mcp.json`,
skills, commands and agents from the cwd the same way `claude` does --
this release makes the INSTALL unmistakable and proves the no-repo-needed
claim with tests, rather than changing that discovery behavior itself.

1. **`docs/INSTALL.md`** (new, top-level, linked from the README):
   leads with one install line per platform -- from a clone,
   `uv tool install --reinstall .` (or `pipx install --force -e .`, with
   a PEP 668 note for Kali/Debian 12+), or directly from GitHub with no
   clone at all, `uv tool install git+https://github.com/roloVibes/Halo-
   Harness` -- then "cd anywhere, type `halo`". The clone directory is for
   `git pull` only; the exact same reinstall command runs again after
   every pull. Keeps its own "Upgrading from rolo-claude 1.0.1" section;
   `docs/harness/INSTALL.md` (the exhaustive walkthrough -- PEP 668 detail,
   the offline work-box recipe, terminal notes) is unchanged and linked
   from the new page.
2. **`doctor`/`init`'s PATH check now names which of three things `halo`
   resolves to**, not just OK/WARN: the installed console script (OK, now
   labelled "installed console script" in the line itself), this
   checkout's own `bin/halo`/`bin/halo.cmd` wrapper (WARN, now says
   plainly "not the installed console script"), or neither at all (WARN,
   now names the PYTHONPATH fallback by name instead of just "not found").
   `halo init`'s summary still ends with this same check plus "Run it
   from any directory -- the checkout is only for `git pull`." whenever
   it isn't the installed copy.
3. **No remaining dependency on the repo directory, proven by test**:
   `bin/halo`, `bin/halo.cmd` and `bin/rolo-claude` were already thin
   fallbacks (prefer an installed console script on PATH, only then fall
   back to `PYTHONPATH=<repo>`) and every vendored data file (the model
   table, the OpenRouter/Databricks catalog fallbacks, the TUI's
   `styles.tcss`) already loaded relative to the package, not the cwd --
   a new test now imports the package with the process `cwd` set to a
   directory outside the checkout and loads each of them directly, so a
   future regression back to a cwd- or repo-relative path fails loudly.
   No test in the suite assumes its own cwd is the repo root (audited).
4. **New tests prove the acceptance criteria directly**: `halo --version`,
   `halo doctor` and `halo -p` each run as a subprocess from a scratch
   directory outside the checkout (literally under the OS temp dir --
   `/tmp` on Linux/Kali); a further `halo -p` test builds a scratch
   project whose `CLAUDE.md` holds one unique sentence and, with a mock
   upstream standing in for the model, asserts that exact sentence
   reached the assembled request body.
5. **Launch hang fixed** (owner's own live use on a Databricks-only work
   VM): `halo` printed the migration line, then sat for a long time before
   the TUI appeared. Cause: `providers.enablement.credentials_present
   ("claude_subscription")`/`init_providers.claude_login_available()` each
   called `claude auth status` directly, uncached, from several places
   startup reaches synchronously -- on a box where the installed `claude`
   is wired to a gateway (Databricks, through Claude Code's own settings
   env) that subprocess hung for its full 10s timeout, every single call.
   Fixed:
   - `credentials_present("claude_subscription")`/`claude_login_
     available()` now read ONLY the existing startup worker's cache
     (`cc_models.cached_claude_auth_status()`) -- a cache miss reads as
     "not detected yet" and never spawns anything itself. The flows that
     genuinely need a fresh, live answer right now (`halo init`'s tabs
     and sequential/`--provider claude` paths, `halo doctor`, and the
     `halo providers` / headless `/providers` listings, which are always
     a fresh process and otherwise printed a real claude.ai login as
     "not set up") call the existing `refresh_cached_claude_auth_status()`
     explicitly instead, once, off the UI thread where one exists.
   - New gateway rule (`cc_models.is_claude_gateway_driven`): when
     `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` (shell env or Claude
     Code's own trust-filtered settings chain) or a settings `apiKeyHelper`
     are present, `claude` is gateway-driven and `claude auth status` is
     never spawned at all, by any caller, including the startup worker and
     `halo doctor`'s own check -- `/providers`/`halo providers` show "not
     set up (claude is configured for a gateway)" instead of an ambiguous
     "not set up". The same check also short-circuits `model.
     parse_model_ref`'s bare-alias resolution (`cc_models.default_bare_
     alias_route`), the other place this exact subprocess could hang
     synchronously before a session's first turn.
   - `tui/app.py`'s existing startup worker remains the ONLY launch-time
     spawner for the TUI; it now also posts a one-time, quiet notify once
     it lands, when (and only when) a usable subscription is actually
     found. Headless mode has no UI thread to protect and no startup
     worker of its own -- `/providers` run headlessly refreshes inline,
     staleness-gated, right there (`commands/builtins.py::_cmd_providers`),
     instead of a thread on every `build_session` call -- verified live,
     that ran one real `claude auth status` subprocess PER SESSION BUILT
     on a box with a genuine `claude` install, slowing this project's own
     test suite considerably; refreshing only where `/providers` is
     actually used headlessly avoids that entirely.
   - `--debug`/`-d` now also prints a `[timeline]` line per startup phase
     (settings, instructions, session build, MCP discovery, first paint),
     each with milliseconds elapsed, to both stderr and `bridge.log`
     (`halo_harness/debug_timeline.py`).
6. **Single executable**: `pyproject.toml`'s `[project.scripts]` now ships
   exactly `halo` -- `rolo-claude` is no longer a second installed console
   script. The 2.0.0 CHANGELOG entry above promised the `rolo-claude`
   alias would keep working "forever" as an installed console script; that
   is superseded by the owner's own first real install attempt on a work
   VM, where an old, separately-installed `rolo-claude` 1.0.1 tool already
   owned that name and made a fresh `uv tool install --editable .` of Halo
   fail outright (`Executable already exists: rolo-claude (use --force to
   overwrite)`) -- `halo` never got installed at all. `rolo-claude` keeps
   working as a deprecated alias two other ways that never register a
   second console script: `bin/rolo-claude` (run straight from a checkout)
   and `cli.main_deprecated_alias` (kept in the source, just no longer a
   pyproject entry point). Uninstalling the old tool first is still
   recommended, so the stale command can't run by mistake -- but a Halo
   install itself must never fail because the old tool exists, and now it
   can't. `halo doctor` gains a WARN when a `rolo-claude` other than this
   checkout's own `bin/rolo-claude` script is still found on PATH, naming
   the matching uninstall command (`uv tool uninstall rolo-claude`/`pipx
   uninstall rolo-claude`/`pip uninstall rolo-claude`) as its fix.
   `docs/INSTALL.md`, `docs/harness/INSTALL.md` and the README's upgrading
   sections are updated to match: uninstalling the old tool is a
   recommendation, not a precondition.
7. **GLM on Databricks: the "pause" was a silent effort mismatch, not a
   hang** (W2a, provider/loop/telemetry side -- see
   [MODELS.md](docs/MODELS.md#databricks-glm-201-glm-briefmd) and
   [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md#glm-seems-to-pause-201)):
   - **The default is sent, not omitted**: an omitted `reasoning_effort`
     on this route means `max`, so a call with no effort configured sends
     `high` explicitly (`ProviderProfile.default_effort_when_unset`, on
     for the Databricks GLM family only), and an effort inherited from
     Claude Code's settings (`effortLevel`, typically `xhigh`) that the
     route does not accept lands on `high` too; an explicit `--effort` or
     `/effort xhigh` is still clamped to `max` as a deliberate choice.
     Switching into a GLM route mid-session applies the same default.
   - **Effort clamp table**: every `databricks-glm-*` endpoint (tabled or
     not) now accepts exactly `low`/`high`/`max`, default `high` (was
     silently sent as whatever the user picked, including values the
     gateway itself then silently coerced to `max` with no error at all --
     `medium -> high`, `minimal -> low`, `xhigh -> max`, `none -> low`,
     via a new `ProviderProfile.effort_clamp_map` `clamp_effort`/`map_effort`
     consult before the generic xhigh/max narrowing). OpenRouter
     `z-ai/glm-5*` keeps the full seven Z.ai-direct values. `--effort`/
     `/effort` show the value actually sent (`medium (sent as high on this
     route)`); a new `providers/effort.py` (`effort_set`, `sent_effort`,
     `requested_vs_sent`) is the one place `/effort`, `/status`, `/context`
     and the stream-json `system/init` line's new `effort`/`effort_sent`
     fields all read this comparison from.
   - **Steer during the silent pre-first-token wait now restarts the
     call**: config `steer.restart_when_silent` (default true) -- when a
     steer arrives and no content has streamed yet for the in-flight call,
     it's aborted and resent immediately with the steer appended (a new
     `steer_restart` event; `--verbose` prints one line) instead of
     waiting for the first chunk to cut in, the way an ordinary mid-stream
     steer already did.
   - **Finish-reason handling**: a chat-dialect `finish_reason:
     "model_context_window_exceeded"` now feeds the existing overflow ->
     compaction -> retry path (previously read as a plain "end_turn");
     `"sensitive"` becomes a clear, never-retried error naming whatever
     text the provider did stream.
   - **GLM never receives `tool_choice: "required"`** (already true via
     `tool_choice_required_supported`, now pinned by test) and temperature
     is clamped to `[0, 1]` on every GLM-family route.
   - **New per-call telemetry** in the session log: `ttfb_ms`,
     `first_reasoning_ms`, `first_text_ms`, `first_tool_ms`,
     `reasoning_streamed` (does this gateway actually stream reasoning
     incrementally, or does it arrive as one lump?). `halo stats --models
     --wide` adds TTFT p50/p95, a count of calls that waited over 20s for
     their first token, and "reasoning streamed" as a percentage, per
     model.
   - **`phase` events** (`events.py`): `request_sent`, `headers`,
     `first_token` (kind: reasoning/text/tool), `waiting_for_model` -- the
     data-side contract a later TUI liveness pass renders a live phase
     line from; print/stream-json output is unchanged (no new stdout
     noise).
8. **Visible thinking, live counters, rotating tips** (W2b, the TUI side
   of the GLM round; see "Is it frozen?" in
   [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)):
   - A live phase line per model call, ticked by the drain timer at least
     once a second: `Sending request to <model>…`, `Thinking… (12 s, no
     tokens yet)`, `Thinking… (18 s · 412 reasoning tokens)`, `Writing…`,
     `Waiting for model…` between tool results and the next call, then
     `Thought for 18 s` as the summary; after 30 s without data the line
     says so and names Esc and steering.
   - The thinking preview shows the last three lines of streamed
     reasoning in every wire shape, collapses to the summary on
     completion and stays open under Ctrl+O; reasoning that arrives only
     with the final chunk shows the summary at that moment.
   - Status bar: spinner, phase word, elapsed seconds and a received-token
     counter that climbs as chunks arrive; idle only on turn done (a
     mid-turn tool step no longer blanks it). Running tool cards show
     elapsed seconds; a nested sub-agent card shows the child's phase and
     tool count live.
   - Rotating tips replace the static prompt placeholder: 52 curated tips
     plus one generated per slash command, a new tip at launch, after
     every turn and every 15 s while idle, never while typing; `/tips`
     lists them; `tips: false` in config.json or `HALO_TIPS=0` restores a
     plain placeholder; a test checks every command, flag and key a tip
     names.
   - The `/effort` card marks a clamped choice inline (`medium (sent as
     high on this route)`) and the status chip shows the sent value.
9. **Prompt history recall, copy and paste in the chat box** (W2c):
   - Up recalls the prompt just typed in halo again: Claude Code's
     history stores millisecond timestamps and halo stored seconds, so
     the merged sort put every Claude Code entry after every halo entry.
     Both units are normalised at read time and halo now writes
     milliseconds; Up on an empty first line recalls the newest entry, a
     typed prefix walks only matching entries, Down past the newest
     restores the draft; a steer or slash command typed over a pending
     card also reaches history.
   - Ctrl+C copies a selection made inside the chat box (OSC 52 plus the
     external-tool fallback) instead of arming quit; Ctrl+X cuts it;
     Ctrl+A selects all; Ctrl+V pastes from the system clipboard through
     xclip, xsel or wl-paste, pbpaste, or PowerShell Get-Clipboard when
     one exists, otherwise it names the terminal's own paste shortcut.
     Bracketed paste is unchanged. The keys are listed in the F1 help and
     in TROUBLESHOOTING.md "Copy and paste".
   - Status bar: the phase word follows the phase line within one drain
     tick (a reasoning model's thinking-to-writing transition fired no
     second phase event); when the bar overflows it drops the MCP count,
     then the balance, then shortens the cwd to its last component
     instead of slicing a path mid-word.
10. **Deprecation notice**: the legacy env file `~/.config/vibes-hacker/env`
    (still read as a fallback behind the new `~/.config/halo/env`, see the
    2.0.0 entry above) stops being read starting in 2.0.4 (the hardening
    release) -- move any credential that still lives only in the old file
    into the new one (`halo init`, or hand-edit) before upgrading past 2.0.3.
11. **MCP "explain the zero"**: `/mcp`, `halo mcp list` and `doctor` now
    name every scope they actually searched -- user scope, this
    directory's project-local scope, this directory's own `.mcp.json`,
    plugins, managed -- with the count found in each, and name another
    directory's `.mcp.json` the user's own history remembers (never a
    bare "0 servers" with no indication where it looked). A server that
    fails to connect now says why instead of a bare "Failed to connect":
    command not found on PATH, connection refused on `host:port`, or a
    disabled transport's own reason.
12. **claude.ai connectors reachable from any model**: when `claude` is
    installed and logged into claude.ai, each account-side connector
    becomes a `connector__<slug>` tool any model can call
    (`{request, tool, args}`) -- it runs a headless `claude -p` scoped to
    just that connector's own tools and returns the result through the
    usual MCP caps, so a connector is no longer usable only from inside a
    real `claude` session. Discovery runs in the background from `claude
    mcp list` and the stream-json init line, cached under `~/.halo/mcp/
    connectors.json`; `/mcp` shows each connector's status and the
    re-auth step; config `connectors.bridge` and a per-connector
    `enabled`/`alwaysLoad`; a settings rule written for the Claude Code
    tool name applies to the bridge tool too.
    - **Cold start**: with an empty cache, `claude` on PATH and a claude.ai
      login, `halo mcp list` discovers synchronously (a 20s cap) and shows
      the connectors without `--refresh`; a print-mode run does the same
      before its tool catalog freezes only when a connector is actually
      wanted (`--tools` naming one, a ToolSearch that asks for one, or
      `connectors.discover_on_start: true`), so a plain `-p` call is never
      held up by a login probe it did not need.
    - **Landing live in a running TUI**: when the cache was cold at
      startup, the background kick used to run before any claude.ai auth
      status was even cached, so eligibility refused it and a cold-cache
      TUI never learned its connectors at all for the whole session (no
      note, 0/0 connectors). The startup worker that primes the auth
      cache now re-kicks the same once-per-process discovery the moment
      its own refresh says claude.ai login, and the connector tools join
      the catalog with one transcript note saying how many arrived,
      typically within 8s of a cold start. The auth-cache priming `halo
      mcp list`/`halo providers` do is skipped once the cache is already
      fresh, so repeated CLI calls stop re-running the `claude` probe
      every time.
13. **`halo mcp serve`, importing from Claude Desktop, and a real OAuth
    login**: no `mcp` subcommand is a stub any more. `halo mcp serve` runs
    Halo's own built-in tools as a stdio MCP server (driven by a real MCP
    client in the tests); `add-from-claude-desktop` imports Claude
    Desktop's own server config; `reset-project-choices` clears a
    project's remembered `.mcp.json` approvals. `halo mcp login`/`logout`
    run a generic OAuth 2.0 authorization-code flow with PKCE, endpoint
    discovery and a local callback, naming no vendor, with tokens under
    `~/.halo/mcp/oauth/`; the tokens are now actually used -- http/sse
    servers send `Authorization: Bearer <access_token>`, a 401 refreshes
    the token once, and otherwise says to run `halo mcp login <name>`
    (tested against a local fake authorization server, never a real one).
14. **14 more hook events, and the flag list finished**: Setup,
    UserPromptExpansion, MessageDisplay, TaskCreated/TaskCompleted
    (sub-agents), StopFailure, InstructionsLoaded, ConfigChange,
    CwdChanged, DirectoryAdded, FileChanged, WorktreeCreated,
    PreModelSwitch and PostModelSwitch now fire from their natural
    trigger points with documented payloads; only WorktreeRemoved
    (nothing removed a worktree yet -- fixed two parts later, see below)
    and the two MCP elicitation events (no elicitation protocol in this
    build) stayed accepted-and-ignored, and the README/handbook/
    architecture doc say exactly that. No CLI flag is left "not yet": 21
    are real (`--restricted`, `--brief`, `--environment`, `--autocompact`,
    `--include-hook-events`, `--permission-prompt-tool`,
    `--permission-prompts`, `--plugin-dir`/`--plugin-url`, `--betas`,
    `--tmux`, `--worktree`, `--ax-screen-reader`, `--bg`,
    `--no-session-persistence`, `--prompt-suggestions`,
    `--fallback-model`, `--forward-subagent-text`,
    `--exclude-dynamic-system-prompt-sections`, `--system-prompt-
    snapshot`) and 7 cloud/IDE flags are declared not applicable with a
    one-line reason each (`--cloud`, `--teleport`, `--remote-control` and
    its prefix, `--from-pr`, `--ide`, `--safe-mode`, accepted with no
    effect); `docs/COMMANDS.md` documents each one.
15. **Skills inside sub-agents, their asks reach the dock, `/rewind`
    covers files a step touched**: a skill with `context: fork` or
    `agent` runs in a general-purpose sub-agent instead of reporting "not
    implemented"; AskUserQuestion and permission asks from a child --
    including a background one -- reach the parent's own pending dock
    tagged with the agent's name and are answered there. `/rewind`/
    `/undo`/`/redo` now cover more than the logged conversation: a step's
    own new files are deleted on undo and recreated on redo, NotebookEdit
    changes are shadow-copied, and a Bash command's new files in a git
    repo are captured through a status diff; a Bash shadow also captures
    TRACKED files a command modified, not just new ones, so rewinding
    past an edit a command itself made restores them too (a file already
    dirty before the command ran is a documented limit either way).
16. **Clipboard quality, and Ctrl+C says what it does**: copying from the
    transcript or a tool card now copies the widget's own SOURCE text,
    never the screen cells -- trailing spaces gone, soft wraps intact,
    the transcript's own glyphs/borders removed, a multi-widget selection
    joined in document order, a code block kept exactly as written, `\n`
    line endings everywhere (`clipboard.crlf` for CRLF). New copy actions
    need no mouse: `/copy` (last reply), `/copy code`/`/copy code N`
    (fenced blocks), `/copy tool` (last tool output), `y` on a focused
    tool card or in the pager (its full content), `Y` (the whole current
    turn); every copy ends with a toast naming what was copied and how
    many characters. OSC 52 is trusted only where the terminal actually
    relays it (Windows Terminal yes, a plain conhost window no); a real
    `clip.exe` fallback (UTF-16 input) exists on Windows and `pbcopy` on
    macOS, and `doctor` names the mechanism actually in use. Ctrl+C's
    toast reads "Press Ctrl+C again to exit halo (your terminal stays
    open)"; config `quit_on_double_ctrl_c: false` turns the double-press
    exit off entirely, so only `/exit`, Ctrl+D or Ctrl+Q leave, and the F1
    help, TROUBLESHOOTING.md and the input placeholder's own rotating tip
    all say so.
17. **Fallback models, background-job hooks, plugin skills/hooks/MCP,
    worktree removal, and more flags wired for real**: `--fallback-model`
    is wired into the retry ladder -- when a retryable provider failure
    exhausts its retries, the session swaps to the next fallback model
    for the rest of the turn with one notice naming both. Background
    Bash jobs now fire TaskCreated/TaskCompleted with the job id,
    command, status and exit code, the way sub-agents already did.
    `--plugin-dir`/`--plugin-url` load a plugin's skills, hooks and MCP
    servers as well as its agents, following Claude Code's own plugin
    layout; plugin skills are namespaced `<plugin>:<skill>` and reachable
    by the Skill tool, not only from the slash menu. `halo worktree rm
    <path>` and config `worktree.remove_on_exit` fire WorktreeRemoved --
    removal itself had never worked at all before this: it ran `git` from
    a directory that was not a working tree, and on Windows from inside
    the very directory being deleted. `--betas` sends the
    `anthropic-beta` header only on Anthropic-family routes (it had been
    leaking to OpenRouter and Databricks chat requests); `--prompt-
    suggestions` also works in the stream-json multi-turn loop; `halo bg
    list|logs|stop|rm` manage a detached `--bg` run.
18. **Process-group kills can no longer hit the harness itself**: `halo bg
    stop`, the Bash tool's timeout kill and the `cc:` child's interrupt/
    kill now signal a child's own process group only when that group is
    not the harness's own, and a PID sharing our group is killed alone;
    killed children are reaped, so a zombie no longer counts as "still
    alive". Found because a new `halo bg stop` test spawned a plain child
    inside the suite runner's own process group and, on Linux, killed the
    runner, its wrapper shell and the whole SSH session every time the
    suite reached that test. The suite runner is now line-buffered too,
    so a run killed mid-way still leaves its true last line on disk
    instead of a stale module header stuck in a block buffer.
19. **A runbook for the checks that need a real terminal or login,
    steadier suites, and a privacy scan test**: `docs/harness/
    LIVE-CHECKS.md` lists exactly what to run and look for the checks a
    mock upstream can't stand in for (a real terminal, a real `claude`
    login, a real MCP server), gated behind `HALO_LIVE=1` in
    `tests/live/`. The `cc:` session, CLI-flag and chrome/playwright smoke
    tests now poll with a bounded deadline instead of a fixed sleep, so a
    loaded machine makes them slower, never red; the flaky first-run
    `cc:` session case on Linux is covered by three standalone repeats in
    the Kali run. Fixture homes, example paths and sample MCP server
    names across tests and docs are synthetic now, never a real machine/
    project/hobby-gear name; `tests/test_privacy_scan.py` fails the suite
    outright if one of those comes back, or a real LAN address, home path
    or key-shaped string does (`tests/privacy_scan_allowlist.txt` holds
    the deliberate test fakes) -- it walks the tree directly rather than
    erroring out on a copy with no `.git` (the Kali suite runs from a tar
    copy).
20. **Two rounds of release review, before the tag**: a full read of the
    whole 2.0.1 diff against every part's own commit message. Round A
    found 2 critical and 16 major defects -- among them a
    `--no-session-persistence` run with `-c`/`--resume` that deleted the
    very conversation it resumed, the TUI silently ignoring
    `--restricted`/`--fallback-model`/`--plugin-dir`/`--betas`/`--brief`/
    `-w`, a cross-provider `--fallback-model` that sent the fallback's
    request with the PRIMARY provider's key, and `isolation: worktree`
    sub-agents whose edits landed outside their own permission scope --
    each fixed with its own pinning test, alongside the
    `--restricted`/`--betas`/`--brief` parity gaps above. Round B found 20
    more minors and the remaining 7 parity gaps: a background sub-agent's
    own dock card that never finalized once it actually finished; a
    steer-restart that silently inflated the next real retry's backoff; a
    `git worktree remove --force` fallback that discarded a session's own
    uncommitted edits on exit; `@server:uri`/`git@host:path` false
    positives in the unresolved-mention warning (and that warning finally
    reaching the user, not just the model, as documented); Windows
    Ctrl+V/`clip.exe` mangling non-ASCII paste/copy text; a connector call
    missing `--no-session-persistence` and running in the wrong cwd; an
    `--autocompact` flag a settings.json `env` block could silently
    outrank; and several telemetry, doctor and timeline-text fixes.
    Docstrings that quoted the owner by name or a private project
    directory were reworded, and the privacy scan above now also checks,
    case-insensitively, for the owner's hobby-gear tool names and the
    owner's own name.

## [1.0.1] - 2026-09-30

A hotfix release from the owner's first real 1.0.0 run on the Kali work VM
(DNS down, then fixed), a live `/model` screenshot from it, and several
days of live use on a work VM after that. Nineteen fixes, all with pinning
tests:

1. **Completion popup keys**: with the `/`/`@` completion popup open,
   Up/Down move the highlight, Tab/Enter accept it, Esc closes it, typing
   still filters -- the prompt's own TextArea no longer swallows Up/Down
   (history nav) while the popup is open.
2. **Fail fast on DNS/connection failures**: a hung/unresolvable host used
   to take upwards of 60s to fail (the OS resolver's own retry policy,
   completely unbounded); every connect (DNS included, not just the TCP
   handshake) is now capped at 8s, and a DNS/refused/unreachable failure
   never rides the 1-2-4-8-16s retry ladder (one immediate retry, then
   terminal) -- across `-p`, the TUI, `init`'s pong, `models --refresh`,
   `/models refresh`, and `doctor`. The error names the host: "cannot
   resolve/reach `<host>` -- check the machine's network, DNS or VPN".
3. **`/models` shows the cache instantly**: bare `/models` (TUI or
   headless) and `rolo-claude models` (CLI) never touch the network any
   more -- a bug in the TUI's own do-refresh check made even a BARE
   `/models` refresh over the network. Only `/models refresh`/`/dbx`/
   `--refresh` do; a failed refresh still shows the (unchanged) cached
   table plus one line naming the error.
4. **Cache carries the gateway types**: every cached row now shows a real
   `path` (family default, from `dbx_routing`) and `chat-capable` counts
   the endpoint's own `task == "llm/v1/chat"` (never a name-based guess or
   `bool(api_types)`, which used to disagree with the table on the same
   rows). An old cache from before this milestone (no `api_types` at all)
   is detected and migrated on next background refresh, else shown as
   "path unknown (refresh needed)" rather than a silently wrong guess.
5. **Interactive model picker in `init`**: a filterable arrow-key list of
   chat-capable endpoints grouped by family (a numbered list with no real
   terminal) offered right after the catalog is refreshed; `--yes`/
   `--model` skip it. The TUI's own `/model` picker got the same grouping.
6. **Bare endpoint names**: `--model databricks-kimi-k3`/`/model
   databricks-kimi-k3`/`init --model databricks-kimi-k3` resolve exactly
   like `dbx:databricks-kimi-k3` whenever the name matches a cached
   endpoint (even a workspace-custom name with no `databricks-`/
   `system.ai.` shape) or that generic shape; an unresolvable name
   suggests the three closest cached endpoints instead of a bare error.
7. **List-dialog keyboard forwarding**: every filter-`Input` + `OptionList`
   dialog (`/model`, `/resume`, the command palette, `init`'s own picker)
   forwards Up/Down/PageUp/PageDown/Home/End to the list and Enter to the
   highlighted row, instead of the `Input` silently swallowing them (Down
   in `/model` used to do nothing at all).
8. **`/model` rows, single line**: grouped with one header per group
   instead of an inline `[group]` tag, each row ellipsized rather than
   wrapped (a long ref/path used to break the column alignment).
9. **`cc:` group gating**: shown only when `claude auth status` reports an
   actual claude.ai login -- a Databricks work box's own settings-driven
   `claude` login used to list nine `cc:` models that would refuse at
   request time.
10. **Unified `ctx`/`out`/price columns**: `/model`, `/models`,
    `rolo-claude models`, and `init`'s own picker now show the SAME
    context/output/USD-per-1M-token columns for every provider, Databricks
    included (previously `path=... dbu=?` only, with "?" everywhere) --
    normalized units (`200000` -> `200k`, `1048576`/`1050000` -> `1M`),
    blank (never `?`) for anything not published. Databricks pricing comes
    from models.dev when that endpoint is listed there, else
    `model_table.json`'s own context (prices blank), else blank.
    `fetch_models_dev` now sends a real `User-Agent` (models.dev's CDN can
    403 a header-less default urllib agent).
11. **`VERIFY_X509_STRICT` cleared on every TLS context**: Python 3.13
    turned this on by default; a corporate TLS-inspection proxy's
    re-signing CA can carry a technically-non-conformant certificate
    extension that only this stricter mode rejects (curl/browsers/Node/
    pre-3.13 Python all accept it) -- seen live as `models --refresh`
    failing with `CERTIFICATE_VERIFY_FAILED: ... basic constraints of CA
    cert not marked critical` on a work VPN while a Databricks pong (a
    different, inspection-exempt host) kept working. Fixed for every
    HTTPS call this harness makes; certificate-chain/hostname verification
    itself is unchanged.
12. **`init` is provider-first**: step 1 is now "select a provider to set
    up" (Databricks/OpenRouter/Anthropic API/Claude subscription) with a
    status tag per row, instead of a home/work/claude "preset" naming a
    bundle of choices ("work" WAS just Databricks, confusingly named).
    Each provider runs its own credentials -> catalog -> model-pick ->
    live-pong path, offers to set up another, and picks one overall
    default when more than one ends up configured. `--provider` (repeatable)
    drives it non-interactively; the old `--preset home|work|claude` still
    works, as a deprecated one-line-noticed alias.
13. **Status bar shows real context/cost for every model, Databricks
    included**: `ctx 12k/1M 1%` (or `ctx 12k` alone with no known limit)
    and a real `$0.0123` (or `in 12k out 3k` token totals with no known
    price) replace the permanent `ctx ?`/`$?` a Databricks model used to
    show. `CostMeter` no longer hard-codes Databricks cost as always
    unknown -- it computes a real fallback cost whenever a models.dev price
    was resolved for that endpoint, same formula every other provider
    already used. The model label truncates from the left on a narrow
    terminal so ctx/cost stay visible; cwd shrinks first.
14. **The transcript follows new output**: streamed text, thinking blocks,
    and expanding tool cards used to leave the view wherever the user's own
    prompt was after the first line -- `Transcript` now anchors to the
    bottom (Textual's own `anchor()`) while "following," releases on a
    manual scroll up (showing a "↓ N new" count in the status bar), and
    re-anchors on `Ctrl+End`, a click on that indicator, or a new prompt.
15. **A pending permission/plan/question card intercepts free text**:
    typing while one is pending used to always steer the turn underneath it
    silently -- it now answers the card directly (deny-with-feedback, keep-
    planning, or "Other", matching Claude Code's own behavior), and
    switching to `auto`/`bypassPermissions`/`dontAsk` (`Shift+Tab`) now
    resolves an already-pending permission card immediately instead of
    leaving it stuck. The status bar shows a "permission needed: ..." tag
    and the prompt placeholder changes while one is pending. This, combined
    with fix 14, was the real mechanism behind a reported TUI "freeze."
16. **`init` also asks for a default permission mode**: `auto` (recommended),
    `acceptEdits`, `default`, `plan` -- written to `~/.rolo-claude/
    config.json`'s own `permission_mode` key, now a layer in the starting-
    mode precedence chain between `--permission-mode` and settings.json's
    `permissions.defaultMode`. `doctor` prints the effective mode and its
    source.
17. **Worker-group isolation and a hard Ctrl+Q**: the git-branch and
    statusline timers (`exclusive=True`, no `group=`) were silently
    cancelling every other in-flight background worker (models refresh,
    catalog refresh, session list, `/improve` drafts, ...) every 5 seconds
    -- each now runs in its own named group, as does every `tui/slash.py`
    worker. `Ctrl+Q` force-quits within 2 seconds even if the session is
    wedged; a Key event with no focused widget now self-heals focus back to
    the prompt (or the active modal's own first focusable widget).
18. **Databricks Claude foundation endpoints reject `xhigh`**: a Databricks
    Claude route's `output_config.effort` only accepts `low`/`medium`/
    `high`/`max` -- a session with no more specific effort set now defaults
    every Anthropic-family route (`cc:`/`ant:`/Databricks Claude foundation)
    to `high` instead of leaving it unset, and `xhigh` is clamped to `max`
    on any route that doesn't list it (a `/model` switch re-clamps too). A
    live 400 naming the effort field retries once with it stripped before
    the turn is treated as failed.
19. **`/effort` actually changes the level**: it used to ignore its own
    argument and only ever echo a value snapshotted at session start
    (typing `/effort medium` kept showing whatever was configured before
    launch). `/effort <level>` now sets the session's effort immediately
    (clamped per fix 18), remembered for the rest of the session; bare
    `/effort` in the TUI opens an inline Claude-Code-style selector card
    (the route's own accepted levels in a row, Left/Right to move, Enter to
    apply, Esc to keep the old value) instead of only printing text. The
    status bar shows a short effort tag next to the mode.
20. **gpt-6 rejects `reasoning_effort` alongside tools**: every turn on
    `dbx:databricks-gpt-6-sol` 400'd ("Function tools with reasoning_effort
    are not supported for gpt-6-sol... set reasoning_effort to 'none'") --
    any endpoint whose name contains `gpt-6` now sends `reasoning_effort:
    "none"` explicitly whenever the request carries tools (a tool-less
    request, or `--effort`/`/effort` on any other family, is unaffected); a
    live 400 with this wording on a different OpenAI-family endpoint
    retries once with the same explicit override.
21. **Enter runs a completed `/` command** (Claude Code parity): with the
    completion popup open, one Enter both inserts the highlighted `/`
    command and runs it (`/mo` + Enter opens the model picker; `/models` +
    Enter runs it). A second Enter used to be required, which read as
    "/models does nothing". Tab still only inserts, and an `@` path
    completion is only ever inserted.
22. **`vendor/model` needs both halves**: `/effort`, `vendor/` and refs
    containing whitespace are refused locally with the usual no-route
    error instead of being accepted as an OpenRouter model and failing
    upstream with a 400 on the first request.

No behavior changes beyond the fixes above; `__version__` is the only
non-test/non-doc change outside the files each fix's own commit touched.

### Review fix pass (same day)

A follow-up review of the fixes above, before this hotfix shipped, found
18 further issues. The following were fixed, each with its own pinning
test; two low-risk minors (a second pending-card slot for concurrent
sub-agent asks, and the connect-timeout retry/leak detail) are deferred --
see "1.0.1 follow-ups" in `docs/harness/RECOMMENDATIONS.md`.

- **`/model` no longer freezes the TUI**: it opened on the UI thread,
  re-read and re-parsed the full `models-dev.json` cache once PER
  Databricks endpoint, and spawned `claude auth status` synchronously --
  2-5s measured on a real catalog. The picker now opens through a worker
  thread; the models.dev cache loads once per `/model` open, not once per
  row; the `claude auth status` check is a cached read, primed once by a
  startup worker, never spawned by `/model` itself.
- **Post-connect network failures ride the retry ladder again**: a
  dropped keep-alive or mid-response reset (after the TCP connection was
  already open) carried the same "cannot resolve/reach" marker a genuine
  DNS/connect failure uses, so it skipped the retry ladder entirely
  instead of riding it like the pre-hotfix 502 behavior. Only a real
  connect-phase failure carries that marker now.
- **Focus self-heal stays on the active screen**: a modal with nothing
  focusable of its own (the pager `o` opens) no longer heals focus to a
  pending card on the screen underneath it, which used to let the modal's
  own keys (`q`/`o`) vanish in favor of the hidden card's (`1`/`y` could
  silently approve a permission the user never saw).
- **`Shift+Tab` re-evaluates a pending permission card for real**:
  switching mode now re-runs the permission engine for the parked
  request instead of blindly allowing/denying it -- an explicit `ask:`
  rule still asks under `auto` (only `bypassPermissions` skips it),
  `acceptEdits` now resolves a pending in-workdir edit/write card, and a
  card the user is already answering (pressed `4`, typing why) is left
  alone instead of being silently finished out from under them.
- **`Ctrl+Q` has its own flag**: it no longer shares `_quitting` with
  double-`Ctrl+C`/`/quit`, so it still works when THAT path is the one
  that's hung; a daemon timer force-kills the process 2.5s after
  `self.exit()` regardless of what Textual/asyncio is still waiting on.
- **Databricks cost is billed once per token, and repriced on `/model`**:
  cached/reasoning tokens were counted at both the full rate and their own
  discounted rate; switching to a new Databricks endpoint mid-session now
  updates the cost meter's own rates instead of billing new usage at the
  old model's prices.
- **OpenRouter prices show again**: `models.json` stores them as numeric
  strings; every price reader now accepts that (`/model`, `rolo-claude
  models`, and `init`'s picker all went blank otherwise).
- **WebFetch and MCP http/sse use the shared TLS policy**: both were
  outside fix 11 above (a different code path -- urllib's own
  `build_opener`/httpx's own client, neither going through
  `open_upstream`).
- **`init` never overwrites an existing `permission_mode`/`model`**:
  `--yes` and Esc now keep whatever's already configured (writing nothing
  at all on a fresh box) instead of forcing `auto`/a fixed-order guess
  over what was actually there.
- **An effort-rejection 400 no longer wastes its one retry on the wrong
  fix**: the gpt-6 "none with tools" classifier (fix 20 above) matched too
  broadly -- any 400 naming both "reasoning_effort" and the bare word
  "param". The general strip-the-field retry can now run after a failed
  "none" retry instead of being blocked by a shared one-shot flag, and a
  successful strip resets the session's effort so later steps don't keep
  re-sending the rejected value first.
- **The Anthropic "high" default (fix 18 above) is scoped to
  adaptive-capable models**: a non-adaptive route (Haiku 4.5, Sonnet 4.5
  or older) no longer gets a thinking budget nearly as large as
  `max_tokens` by default (capped at half of it instead); the version
  parser reads hyphenated ids (`claude-sonnet-4-6`) correctly; the
  `tool_choice: any` repair retry drops thinking (Anthropic rejects that
  combination outright).
- **The gpt-6 "none with tools" rule (fix 20 above) is Databricks-only**,
  driven by explicit `model_table.json` rows for both real `models.dev`
  name shapes -- it no longer also fires for an OpenRouter model that
  happens to have "gpt-6" in its name.
- **Chat-dialect routes no longer accept `max`** (an Anthropic-only
  level) from `/effort`; the retry also recognizes OpenRouter's nested
  `reasoning.effort` 400 wording and a generic "Invalid value" one.
- **Commands typed while a card is pending run instead of denying the
  tool**: a `/`-prefixed submission always goes to slash handling first,
  whatever card is pending; pasted feedback carries the real content, not
  the `[Pasted text #n]` placeholder; the `/effort` card no longer
  intercepts typed text at all.
- **A tall card's auto-scroll is restored once answered**: focusing a
  card too large to fit releases the transcript's bottom anchor (fix 14
  above; Textual's own scroll-to-center on focus); `clear_pending_card`
  now re-anchors when the user was following before the card appeared.
- Wording: `init`'s "default" permission mode no longer implies a risk
  check this project doesn't have ("ask before edits and non-read-only
  tools", not "...or otherwise risky").
- **Rate-limit and stats token counts include reasoning again**: since
  `map_usage` reports reasoning tokens separately (so they are never
  billed twice), the Databricks output-tokens-per-minute tracker and
  `stats` `tokens_out` now add them back, or a reasoning-heavy reply
  under-counted and the 429s the tracker prevents came back.
- **MCP over HTTP prefers the pinned `httpx2`** and falls back to classic
  `httpx` only when `httpx2` is absent, so an environment carrying both
  never hands the MCP SDK the wrong client type.
- **Adaptive thinking is gated to Opus 4.6+**: Opus 4.1 and 4.5 (both
  served on Databricks) keep `budget_tokens`, as Sonnet below 4.6 already
  did; every `opus` id used to count as adaptive. Bedrock-style ids that
  put the version before the family (`us-anthropic-claude-3-7-sonnet-...`)
  now parse their real version instead of the snapshot date.

`__version__` is unchanged.

### Part 2 (same release): tabbed provider setup, hang diagnostics, provider enablement, effort polish

Finishes the 1.0.1 round -- item 13's tabbed provider view, item 15's
remaining hang diagnostics, the new item 21 provider-enablement model, and
item 22's remainder -- plus five minors the reviewer logged after the fix
pass above. All with pinning tests; `__version__` stays 1.0.1.

- **`init`'s provider setup is now tabbed** (item 13): one tab per provider
  -- Databricks, OpenRouter, Anthropic API (key), Claude Code subscription,
  and a new fifth tab, TypeSafe (stores `TYPESAFE_API_KEY` only, for a
  later feature -- no routed models yet) -- replacing the one-provider-at-
  a-time picker loop on a REAL terminal only (a piped/non-tty run, every
  existing script, keeps the exact old sequential-picker-plus-numbered-
  fallback path, byte for byte). Shift+Tab and Left/Right (the latter only
  while the tab bar itself has focus -- a focused credential field's own
  Left/Right still moves the cursor) switch tabs; Up/Down move between a
  tab's own fields/button; Enter activates a field or button; Esc leaves
  the dialog and `init` proceeds to the default-model/permission-mode
  steps exactly as before. Each tab opens showing what the harness already
  picked up ("picked up from `<source>`: `<masked>`", or a discovered
  Databricks host with just the token field left) with the status line
  updating live as a value is typed; a bounded background probe (the same
  8s connect-only cap item 2 introduced) shows "reachable" / "unreachable:
  `<reason>`" / "not set up"; finishing a tab with real credentials fetches
  and caches that provider's catalog right then, off the UI thread. A
  Textual runtime failure (no real terminal after all) falls back to the
  old sequential picker instead of crashing `init` outright.
- **Provider enablement** (item 21, new): a provider's models now reach
  `/model`/`rolo-claude models`/the `init` default pick/`doctor` only once
  it's EXPLICITLY enabled -- by completing its tab above, by `rolo-claude
  providers enable <name>`/`/providers enable <name>`, or (one-time only,
  for a box with credentials from before this existed) an automatic
  migration `doctor`/`init` run the first time. Detected credentials or a
  `claude.ai` login never enable a provider on their own -- a `cc:` group
  no longer appears in `/model` just because `claude auth status` reports
  a real login (item 9's own gate), and a hand-typed ref for a disabled
  provider is refused with a one-line message naming the fix; a detected-
  but-disabled provider shows one dim hint line in `/model` instead of a
  selectable row. `rolo-claude providers`/`/providers`: a table (enabled,
  credentials found and where, reachable, cached model count) plus
  `enable`/`disable`/`setup <name>`. Labels used everywhere (the tabs,
  `/model`'s group headers, `doctor`, `/providers`): `dbx:` Databricks,
  `or:` OpenRouter, `ant:` Anthropic API (key), `cc:` Claude Code
  subscription -- the full prefix table is in `docs/MODELS.md`.
- **Hang diagnostics** (item 15 remainder): a daemon watchdog thread
  (started from `on_mount`, completely independent of the UI's own event
  loop -- the point is that it keeps working when THAT is what's stuck)
  polls a heartbeat `_tick_spinner` bumps every second; once it's stalled
  past 15s it dumps every thread's stack (`sys._current_frames()`), the
  named background-worker list, and the active screen to
  `~/.rolo-claude/hang-<UTC>.log` (one line also to `bridge.log`), at most
  once a minute while it persists. `SIGUSR1` (POSIX) dumps the same
  diagnostics on demand. `--debug` now also traces, to `bridge.log`: every
  Key event (with the focused widget and active screen), window
  focus/blur, and the start/finish/cancel of every named background
  worker. An audit of `_drain` and every `call_from_thread` call site found
  no unbounded blocking of the UI thread (every `subprocess.run` already
  had a timeout and already ran on its own worker thread; no `Worker.
  wait()`/shared-lock call exists on this path) -- the watchdog above is
  the backstop for whatever that audit missed. (The 1Hz heartbeat timer
  itself is now wired through a lambda instead of the bound `_tick_spinner`
  method directly -- `set_interval` keeps calling whatever reference it was
  first given, so a test that needs to simulate a stalled heartbeat by
  replacing the instance's own `_tick_spinner` was silently still ticking
  the original every second; no production behaviour changes, only what a
  test can now actually stop.)
- **`/effort` and the status bar show `none (tools)`** on a route where the
  gpt-6 table rule or a LEARNED per-endpoint rule applies (item 22
  remainder): the EFFECTIVE value actually sent on a tool-carrying turn,
  not the configured one that route would ignore anyway, with the
  selector card's own description line (and `/effort`'s own source line)
  explaining why. The learned per-endpoint `reasoning_effort_with_tools`
  rule (a live 400 proving an untabled Databricks endpoint also needs it)
  now persists to `~/.rolo-claude/learned-rules.json`, not only in memory
  for the rest of one session -- a NEW session against the same endpoint
  sends it correctly from its very first request, never re-paying for the
  failing one. A `model_table.json` row always wins over a learned entry.
- **Five minors from the reviewer's post-fix-pass notes**, each with its
  own pinning test: `Ctrl+Q` now calls `session.job_registry.kill_all()`
  immediately, before its `os._exit` timer is even armed, instead of only
  deep inside the ordinary (possibly also-wedged) `controller.quit()`
  path it exists to route around; `clamp_effort` clamps an unsupported
  `max` to `xhigh` on a chat-dialect route that has `xhigh` but no `max`
  (the mirror of the existing `xhigh`-without-`max` case), instead of
  falling all the way to the bland `medium` default; `/effort`, `/rewind`
  and `/improve` show a one-line note instead of opening their own card
  while any card (a live permission ask, most importantly) is already
  pending; `init`'s per-provider default-model step no longer overwrites a
  custom model already configured when it merely differs from that
  provider's own hardcoded default (only the final cross-provider pick
  previously preserved a custom value) -- an explicit `--model` this run
  still wins outright; `cached_auth_status_is_stale`'s own TTL is now
  actually consulted (bare `/model`'s own stale-Databricks-catalog-refresh
  worker does the same for the claude-auth-status cache), so a `claude.ai`
  login/logout during a long session is reflected the next time `/model`
  opens, not only after restarting the whole app.
- **State-dir scoping** (found during this round's own fix pass): a test
  module that builds a real `Session`/`Controller` without scoping
  `BRIDGE_STATE_DIR` itself used to only avoid leaking into the REAL
  `~/.rolo-claude/sessions` because an EARLIER module happened to leave
  `BRIDGE_TEST_HOME`/`BRIDGE_STATE_DIR` set -- `tests/run_all.py` now
  snapshots and restores every `BRIDGE_*`/`OPENROUTER_*`/`DATABRICKS_*`/
  `ANTHROPIC_*`/`TYPESAFE_*` env var AROUND EACH MODULE, so this can never
  happen regardless of any one module's own hygiene or import order; its
  own REAL SESSIONS GUARD also now catches a new DIRECTORY under the real
  sessions dir, not only a new `.jsonl` file (1972 empty slug directories,
  invisible to the old file-only glob, were found on the Windows build
  host). The same unscoped-real-machine-state bug also reached
  `~/.rolo-claude/mcp/tools-cache/` (lazy MCP tool caching, item 13's own
  H13 Part A, also resolves via `bridge_home()`) through one compat-matrix
  test that scoped its subprocess check but not its in-process one -- fixed
  the same way, by scoping before the in-process `build_manager` call. The
  `!cmd` inline-shell permission card is now registered with the session's
  own permission-waiter table too, so Shift+Tab/`/permissions` re-evaluates
  it through the exact same `reevaluate_pending_permission` path a live
  tool-call ask already uses, instead of leaving it unaffected by a mode
  change until answered by hand.
- **Addendum**: a bare `rolo-claude` typed outside the checkout did not
  work for the owner on the work VM, because the installed console script
  had never been put on PATH (only the checkout's own `bin/` wrapper had
  ever been used, from inside the checkout). `doctor` gained a "command on
  PATH" check -- OK names the resolved path; a WARN (not found, or
  resolving to the checkout's own `bin/` wrapper) names the exact reinstall
  command for whichever tool is present (`uv tool install --reinstall .`
  run from the checkout, else `pip install --user -e .`) and reminds that
  it's needed again after every `git pull`; `init`'s own Summary ends with
  the same check and, when it fails, the same fix line plus "run it from
  any directory -- the checkout is only for `git pull`." README's quick
  start and `docs/COMMANDS.md`'s `doctor`/`init` sections now say so too.

### Part 2 addenda (same release, same day): Opus 5.5, provider auto-detection, catalogs without init, OpenRouter balance

Four follow-up asks from the owner's live use on his personal Mac and his
work VM, landed the same day as Part 2 above. All with pinning tests;
`__version__` stays 1.0.1.

- **`cc:`/`ant:` explicit `opus-5.5`/`sonnet-5.5` aliases**: `opus`/`sonnet`
  are deliberately-moving "latest" pointers (today both resolve to the
  `-5-5` point release) -- nothing was labelled "5.5" anywhere, so a reader
  had to already know `opus` IS `claude-opus-5-5` to find it. The two new
  names resolve to the identical id the bare pointer already does; `/model`,
  `rolo-claude models` and the init picker now show the resolved id next to
  EVERY `cc:`/`ant:` alias (`cc:opus -> claude-opus-5-5 (latest Opus)`,
  `cc:opus-5.5 -> claude-opus-5-5`), reading as an enumeration the same way
  the Databricks group's own `[<family> · <path>]` tag already does. Full
  alias table in `docs/MODELS.md`.
- **Provider enablement rule REPLACED** (rolo, personal Mac: "never ran
  init... loves that the harness already finds the available keys and
  subscription and uses those"): detected credentials/a real claude.ai
  login now AUTO-enable a provider -- OpenRouter/Anthropic API (key)/
  TypeSafe once their key is found (env file, shell env, or the settings
  env chain), Databricks once a host AND token are found (same sources,
  plus `~/.databrickscfg`), Claude Code subscription (`cc:`) ONLY when
  `claude auth status` reports `loggedIn` with `authMethod` exactly
  `claude.ai` (never for an API-token/custom-base-url-driven `claude`, as
  on a work VM, loggedIn or not). `providers` in config.json now stores
  OVERRIDES only: an explicit `enabled: true`/`false` always wins over
  auto-detection; migration is now a permanent no-op (auto-detection
  already computes live what it used to write once). `/providers`/
  `rolo-claude providers` shows each provider as `auto (detected from
  <source>)` / `disabled by you` / `enabled by you` / `not set up`. A
  hand-typed `cc:` ref refused by pure auto-detection (never an explicit
  override) now names the SAME precise reason (not logged in / claude not
  installed / wrong authMethod, each with its own fix) a turn would have
  failed with anyway, reusing `agent.cc_runtime`'s own preflight check
  instead of a generic "not enabled" line; `doctor`'s default-model check
  got the matching fix for a syntactically-fine-but-unconfigured ref.
- **Catalogs exist without `init`, `/model` lists every enabled provider
  regardless of the current model** (rolo, Mac: "`/model` showed one
  OpenRouter row" because models.json had never been fetched, and it
  vanished entirely after switching to a `cc:` model): a background worker
  now fetches every ENABLED provider's catalog (OpenRouter `/models`,
  Anthropic `/v1/models` -> a new `ant-models.json` cache, Databricks
  endpoint discovery) once at app launch and whenever `/model` opens, if
  missing or older than `databricks.catalog_max_age_hours` (default 24h) --
  never on the UI thread, one dim note when something actually changed.
  `/models refresh`/`rolo-claude models --refresh` now refresh every
  enabled provider, not Databricks/OpenRouter only. `Controller.
  list_models()` already built each provider's group from its own cache
  independent of the session's current model -- the real fix was making
  sure that cache actually gets populated; verified a cached OpenRouter
  group survives switching the session to `cc:opus` unchanged.
- **OpenRouter account balance in the status bar** (rolo: "put the
  remaining balance of an api key... next to... the row of numbers under
  the chat"; corrected mid-round against OpenRouter's real OpenAPI spec --
  the key-info endpoint is `GET /key`, not `/auth/key`, and `/credits`
  needs a separate Management key): a new segment right after cost, `OR
  $12.40 left` or `OR $3.21 used`, populated by a background worker (never
  the UI thread) at launch, every 5 minutes, and once after every turn
  (debounced to at most once a minute). Three-way preference: THIS key's
  own `limit_remaining` (`GET /key`, the ordinary `OPENROUTER_API_KEY`)
  when it has a real limit; else the whole account's remaining credits
  (`GET /credits`, requiring a SEPARATE `OPENROUTER_MANAGEMENT_KEY` --
  never the ordinary key) when that key is configured; else this key's own
  `usage` (a spend figure, labelled "used" not "left" -- the honest
  fallback for an unlimited key with no management key). Both calls are
  best-effort and never raise; a failed refresh leaves whatever was cached
  before untouched (never blanks the segment); the figure dims (never
  disappears) once it's more than 10 minutes old; omitted entirely when
  OpenRouter isn't enabled or nothing has ever been fetched. `/cost` and
  `/providers` print the same cached figure, naming which of the three
  kinds it is, plus the key's own label and the time of the reading.
  `OPENROUTER_MANAGEMENT_KEY` documented in `docs/CONFIG.md`.
- **State-dir/env-scoping audit, round 2**: the SAME `BRIDGE_TEST_HOME`-
  leak class D2.1 found earlier in this release turned up again, three
  more times, now as a `providers`-block/credential-env leak instead of a
  session-log one -- each a test that pops a credential env var in its own
  setup but never restores it (only clears), silently wiping every LATER
  test's own default credential for the rest of that file's run once
  nothing upstream masked it anymore. Fixed in `test_h9b_findings.py`
  (`OPENROUTER_API_KEY`), `test_dbx_work_routing.py`'s own `_EnvSandbox`,
  and `test_work_box.py`'s `test_doctor_work_no_databricks_configured` --
  each now saves and restores the full set it touches, not just pops it.
  A new `tests/helpers/provider_env_defaults.py` gives the ~40 other test
  files that build `or:`/`dbx:`/`ant:` refs to test something else
  entirely (tool dispatch, hooks, compaction, permissions, steering, ...)
  a believable (never real) default credential, since `parse_model_ref`
  now refuses an auto-detected-disabled provider the same way it already
  refused an explicitly-disabled one.

`__version__` is unchanged.

### Final pass (same release, same day): secret-env hygiene and trust-aware Databricks resolution

Two more findings from the 1.0.1 part 2 fixpass review, each with its own pinning test; `__version__` stays 1.0.1.

- **`OPENROUTER_MANAGEMENT_KEY`/`TYPESAFE_API_KEY` now strip from every tool
  child's env**: the fixed secret-key list `tool_child_env`/`cc_child_env`
  use (so Bash/PowerShell/MCP/hooks and the `claude` subprocess never see a
  provider credential) was missing both -- the OpenRouter balance
  management key and the TypeSafe key could otherwise have leaked into a
  tool child's own environment.
- **`resolve_databricks()`'s settings-chain re-derivation is now trust-aware**:
  an untrusted project's own `.claude/settings.json`/`settings.local.json`
  `env` block is dropped before it ever reaches Databricks host/token
  resolution (the same trust gate a real session's `Settings.effective_env`
  already enforces), so a project can never pair its own host with the
  user's token; a token exported in the shell still pairs with a host kept
  in the user's own settings.json env block. Credential listings
  (`/providers`, the init tabs, doctor) apply the same trust rule, the
  user's statusLine script runs with the same stripped child env as hooks
  and tools, and the background catalog and balance workers detect a
  provider through the session's effective env, so a key that lives only
  in a settings.json env block still gets its catalog and balance. The
  launch-time catalog refresh,
  `/model`'s own open-time refresh, the OpenRouter balance worker, and the
  headless session-start Databricks thread now resolve credentials from the
  session's own trust-filtered `effective_env` instead of letting each one
  call bare `resolve_databricks()`/read `os.environ` on its own.

`__version__` is unchanged.

## [1.0.0] - 2026-09-30

rolo-claude 1.0.0: the stable general harness release. Summarises the
Databricks-first correctness/tooling work from the `V2-brief.md` line
(`docs/harness/V2-brief.md`) -- per-family request/stream/error correctness
across every gateway type a workspace can serve (the `0.7.0` line below),
and matrix-driven fixes plus roles (the `0.8.0` line below) -- and a
documentation pass on top of them. rolo-claude itself is unchanged in
scope: still the one general harness driving all four routes (OpenRouter,
Databricks, the Anthropic API, and a Claude subscription via `cc:`), not
renamed, with no new console-script alias.

- **0.7.0 (V2a) recap**: per-family correctness for every Databricks gateway
  dialect the harness routes to (native `anthropic/v1/messages`, `mlflow/
  v1/chat/completions`, `cursor/v1/chat/completions`, and the universal
  `/serving-endpoints/<name>/invocations` fallback) -- thinking/reasoning
  replay per family, usage/cost accounting per type, every error shape
  (400/401/403/404/413/429/5xx) classified correctly, and the two
  work-matrix open questions (reasoning replay after a tool call, route
  split from a stale cache) turned into runnable probes.
- **0.8.0 (V2b+V2c) recap**: `rolo-claude work-matrix show`/`apply` turns a
  `doctor --work --probe-all` report into a suggested fix per failing
  endpoint (and writes the one class of fix that maps onto a real
  `databricks.gateway.<endpoint>` config key); roles
  (`orchestrator`/`coder`/`reviewer`/`researcher`/`small`) let a team point
  different kinds of work at different models via `~/.rolo-claude/
  config.json`/`team.json`'s own `roles` table, `--role NAME=MODEL`,
  `Agent(role=...)`, `/roles`, and `stats --roles`.
- **Docs**: README now documents roles and the work-matrix show/apply
  workflow (real 0.8.0 features that had shipped without a README mention
  until now) and points to a separate sibling project,
  [databricks-claude](https://github.com/roloVibes/databricks-claude), for
  teams that want a Databricks-only edition; `docs/DATABRICKS.md` gained an
  end-to-end team-workflow section tying `team.json` -> `init` -> the work
  matrix -> roles together in one walkthrough; `docs/ROLES.md` cross-links
  it; `docs/harness/README.md`'s milestone index gained a `V2-brief.md` row.
- **Naming, for the record**: `docs/harness/V2-brief.md` (kept as originally
  written -- this project's own build-history convention, see
  `docs/harness/README.md`'s opening note) describes a planned V2d/V2e phase
  renaming this project to `databricks-claude`. That did not happen here --
  the owner instead spun off a **separate** `databricks-claude` repository
  as its own Databricks-only edition for teams, and rolo-claude stayed the
  general four-route harness, released as `1.0.0` rather than `2.0.0`.
- `__version__` -> 1.0.0.

## [0.8.0] - 2026-09-29

V2b+V2c: matrix-driven fixes tooling and roles. See `docs/harness/V2-brief.md`.

- **`rolo-claude work-matrix show/apply`** (`rolo_claude/work_matrix.py`):
  `show <report.json>` renders a `doctor --work --probe-all` JSON report as a
  table with a suggested action per failing endpoint -- 403 -> run this on
  the VPN; a non-200 row with a `--both`-probed "(anthropic gateway)"
  companion row that itself answered 200 -> set `databricks.gateway.
  <endpoint>: anthropic`; any other non-200 -> unknown, report it; a 200 with
  `tool_call_ok: false` -> change the family's tool-call rule in
  `model_table.json`; a 200 with `reasoning_replay_ok: false` -> disable
  thinking for tool loops on that endpoint. `apply <report.json> [--yes]`
  writes only the ONE class that maps onto a real config.json knob (the
  gateway override), after listing every write and asking for confirmation;
  the other three classes are report-only (no per-endpoint config key exists
  for them). Two synthetic sample reports under `tests/fixtures/work-matrix/`
  (`all-green.json`, `all-failures.json`, one row per failure class).
- **Fix: `providers/http.py::call_anthropic_native`'s 404 fallback no longer
  hardcodes the literal, non-existent endpoint name "anthropic"** (flagged
  during V2a) -- `/serving-endpoints/anthropic/v1/messages` is not a real
  workspace endpoint; the fallback now builds `/serving-endpoints/<the real
  endpoint name, from body["model"]>/invocations`, the SAME by-name
  universal fallback every other family/dialect already uses, with no query
  suffix (a plain invocations call never uses Databricks' `?beta=true`
  AI-gateway flag). `tests/helpers/mock_databricks.py`'s own anthropic-
  gateway dispatch is now body-shape-based (a top-level `system` field,
  never present on an openai-chat body) instead of matching that same wrong
  literal path.
- **Roles** (`rolo_claude/roles.py`, H15): `orchestrator`/`coder`/`reviewer`/
  `researcher`/`small` in `team.json` and `~/.rolo-claude/config.json`'s own
  `roles` key (team.json seeded into config.json once, at `init --preset
  work` time, by the same idempotent idiom `gateway_preference` already
  uses). Three new built-in agents -- `Coder` (full read/write tool set),
  `Reviewer` (read-only code review), `Researcher` (Explore's tools +
  WebSearch) -- alongside `general-purpose`/`Explore`/`Plan`, each with a
  fixed default role; a custom `.claude/agents/*.md` agent sets the same
  thing with a `role:` frontmatter key. Resolution precedence: an explicit
  `model=` always wins; then a `--role NAME=MODEL` CLI override for that
  agent's own role (repeatable; wins even over the agent's own file
  `model:` -- a deliberate, freshly-typed, run-only override); then the
  agent file's own `model:`; then the role table; then `CLAUDE_CODE_
  SUBAGENT_MODEL`/`settings.subagentModel`; then the parent/session model
  (`orchestrator`'s own documented default). The `Agent`/`Task` tool itself
  accepts a `role` argument overriding an agent's default role for just one
  call. Cost-aware defaults (DeepSeek V4.1 Flash for `researcher`/`small`)
  apply ONLY when the role table is completely empty AND the session's own
  model is a Databricks one -- documented in `docs/ROLES.md`, never
  automatic beyond that one case. `/roles` shows the resolved table (model,
  endpoint/path type, price) per role; `stats --roles` sums sub-agent spend
  per role from each Agent-tool call's own rolled-up usage node (tagged
  `role=`, additive -- a pre-V2c log simply has none).
- **Docs**: new `docs/ROLES.md`; `docs/DATABRICKS.md`'s own work-matrix
  section; `docs/COMMANDS.md`'s `work-matrix`/`--role`/`stats --roles`
  sections; `docs/SLASH-COMMANDS.md`'s `/roles` section.

## [0.7.0] - 2026-09-29

V2a: per-family Databricks correctness across every gateway type the harness
routes to (`anthropic/v1/messages`, `mlflow/v1/chat/completions`,
`cursor/v1/chat/completions`, `/serving-endpoints/<name>/invocations`), a
fixture test matrix driven by an extended `tests/helpers/mock_databricks.py`
speaking every dialect, and the two work-matrix open-question probes. See
`docs/harness/V2-brief.md`.

- **`tests/helpers/mock_databricks.py` speaks every dialect now**: the
  native `anthropic/v1/messages` gateway path is scenario-dispatched (it was
  hardcoded to one fixed "pong" reply) -- thinking blocks + signatures,
  tool_use (including Kimi's own verbatim `functions.<name>:<idx>` id), and
  the dialect's own 401/403-IP/404/overflow-400/429/5xx shapes; the
  openai-chat side gained 401/403-IP/404/500/context-overflow-400/413/
  finish_reason=length-minimal-output/Kimi-native-tool-id/cached-and-
  reasoning-token-usage/two-turn-reasoning-replay scenarios, plus path-aware
  404-then-fallback scenarios (mlflow-then-cursor, cursor-then-mlflow).
- **Per-family/per-type pinning tests** (`test_v2a_<family>_<type>_...`,
  five new files under `tests/`): Claude foundation/GLM/Kimi
  thinking+signature+cache_control+coding-agent-mode-header on the
  anthropic gateway (tested with and without thinking); DeepSeek/GPT/Grok/
  Gemini/gpt-oss reasoning decode+replay rules on mlflow; GLM's fixed
  sampling pair vs. Kimi/DeepSeek's server-fixed omission; the catalog's own
  `foundation_model.name` on the wire, `stream: true` explicit, the 32-tool
  cap + schema simplifier, and OTPM pre-admission against Kimi's real
  published rate limits; `gpt-5-5-pro`'s cursor-only route and the GPT
  family's mlflow-then-cursor fallback; Bedrock EXTERNAL Claude's
  invocations-only, model-less chat body with tool support; every error
  shape (400 unknown-field, 401, 403-IP, 404-to-exhaustion, 413,
  context-overflow-400, 429 with limit_type/retry_after, 5xx) and usage/cost
  accounting (cached + reasoning tokens, Databricks cost always "n/a", the
  catalog's DBU-rate-to-dollars conversion) per type.
- **Fix: a literal HTTP 413 is now non-retryable and classified
  `CONTEXT_WINDOW_EXCEEDED`** (`providers/errors.py`) -- `map_upstream_error`
  had no row for 413 and fell through to its own `should_retry=True`
  default, which won the `e.retryable or is_retryable_message(...)`
  short-circuit in `agent/loop.py` before `is_context_overflow_message`'s
  own (already-correct) unconditional 413-is-overflow rule ever got a
  chance to run -- a real 413 was silently retried up to `MAX_RETRIES`
  times against the identical, still-too-large body instead of surfacing as
  an overflow.
- **The two work-matrix open questions are now runnable probes**
  (`rolo_claude/work_matrix.py`, `doctor --work --probe-all --tools`): (1)
  reasoning replay after a tool call -- a real second turn, built through
  the exact same `providers.request` builders a live session uses (a
  genuinely signed `thinking` block on the anthropic dialect; the family's
  own `reasoning_echo` rule on the openai-chat dialect), is sent and its
  acceptance recorded per endpoint as `reasoning_replay_ok`; (2) route split
  from the cache -- `cached_path_type` (what `routes-cache.json` said
  before this run) alongside `path_type` (what this run actually used/
  re-cached) makes a stale-cache mismatch visible in the JSON report
  without a second run. Both fields are `None` when not applicable (no
  `--tools`, or nothing was cached yet) rather than a misleading default.
- `docs/DATABRICKS.md`/`docs/MODELS.md` updated: the work matrix's two new
  report fields, and a wording correction -- `databricks.dbu_price_usd`
  converts the catalog's own advertised DBU rate for the `/model` picker's
  informational display only; Databricks never reports a per-turn spend, so
  `/cost`/`stats` stay "n/a" regardless of whether that price is configured.
- `__version__` -> 0.7.0.

## [0.6.0] - 2026-09-29

H14: Databricks work-config parity -- zero-setup at work from Claude Code's
own settings, a generic family x api_type routing table (no vendored
endpoint list), doctor accuracy, team onboarding, and the work matrix. See
`docs/harness/H14-brief.md`.

- **Host/gateway split (scope A)**: `resolve_databricks()` now always
  returns the bare workspace root as `.host` -- a gateway path riding along
  in `ANTHROPIC_BASE_URL`/`DATABRICKS_HOST` (`.../ai-gateway/anthropic`) is
  split off into `.anthropic_gateway` instead of leaking into every derived
  URL. Fixes `doctor --work` building `/api/2.0/serving-endpoints` under the
  gateway path.
- **Custom headers (scope B)**: `ANTHROPIC_CUSTOM_HEADERS` (one or more
  "Name: value" lines, Claude Code's own settings.json convention) is parsed
  onto `DbxConfig.custom_headers` and merged onto every Databricks request
  (native passthrough and chat), under the default
  `x-databricks-use-coding-agent-mode: true` and any explicit override.
- **Model defaults/aliases at work (scope C)**: when Claude Code's own
  settings env resolves a Databricks config, the default model is
  `dbx:<ANTHROPIC_MODEL>` and bare `opus`/`sonnet`/`haiku` resolve through
  `ANTHROPIC_DEFAULT_*_MODEL` (falling back to pinned `databricks-claude-*`
  endpoints when unset) instead of the `cc:`/`ant:` subscription route or the
  OpenRouter default; `effortLevel`/`modelSettings.<id>.effortLevel` now
  actually set the session's default effort (previously unwired).
- **Generic family x api_type RULES table (scope D,
  `providers/dbx_routing.py`)**: family detected from the endpoint name (and,
  when cached, `foundation_model.name`/`model_class`) drives an ordered,
  endpoint-specific route built from the endpoint's OWN discovered
  `api_types` -- never a hand-maintained per-model list. Claude foundation
  defaults to the native anthropic gateway; GLM/Kimi default to mlflow chat
  with the anthropic gateway selectable per model (`databricks.gateway.
  <endpoint>` config or a `dbx:<endpoint>@anthropic` suffix); DeepSeek/Qwen/
  Llama/Gemma/gpt-oss default to mlflow chat, using the endpoint's real
  `foundation_model.name` as the wire model id (not a `system.ai.` prefix
  guess); GPT/Grok/Gemini default to mlflow chat, with `databricks-gpt-5-5-
  pro`'s own exception (no mlflow chat, cursor chat instead); Bedrock
  EXTERNAL Claude endpoints (`us-anthropic-claude-*`) are invocations-only
  and no longer misclassified as native Claude passthrough just because
  "claude" is in the name; a known non-chat endpoint (embeddings/whisper) is
  refused with a clear message and hidden from pickers. An unknown endpoint
  (not yet in the discovery cache) falls back to the pre-H14 static order.
- **doctor --work accuracy (scope E)**: prints the derived workspace root,
  gateway path, header NAMES (never values), default model, default effort,
  and which resolution step supplied the token; token-validity now
  distinguishes 401 (bad token), 403 with Databricks' own IP-access-list
  wording ("connect to the VPN"), 403 without it (token lacks list
  permission, inference may still work), and a wrong-path 404 -- previously
  every non-200 read as one generic "may only run inference" line. A host
  configured with no token yet still probes (a 401 without a token proves
  reachability) instead of skipping the network call.
- **Work matrix (scope H, `rolo-claude doctor --work --probe-all [--both]
  [--tools] [--only <glob>]`)**: one short pong per chat-shaped endpoint on
  its chosen path (plus the anthropic gateway too for Claude/GLM/Kimi with
  `--both`), a table of status/latency/output tokens/tool-call support, and
  a JSON report at `~/.rolo-claude/work-matrix-<date>.json` with endpoint
  names only -- no host, no token. Mock-verified (the real workspace is
  behind an IP access list from this box).
- **Team onboarding (scope I)**: a shared `team.json` (host, default model,
  per-family gateway preference, DBU price -- never a token, never an
  endpoint list) discovered at `.rolo-claude/team.json` (project),
  `~/.rolo-claude/team.json`, or `--team <path|url>`; `init --preset work`
  reads it (or Claude Code's own settings env) and asks ONLY for the token
  when a host is already known, hidden input, 0600 env file. `/model`
  groups Databricks endpoints by family, shows the chosen path type, and
  hides non-chat endpoints. `team.example.json` added.
- **`/models refresh` (alias `/dbx`) (scope J)**: re-lists the workspace
  catalog off the UI thread, updates `~/.rolo-claude/dbx-endpoints.json`, and
  reports a one-line added/removed/changed diff; `rolo-claude models
  --refresh --urls [--json]` prints the exact URL and path type per
  endpoint. Auto-refresh (`databricks.catalog_max_age_hours`, default 24h)
  on `/model` open and (silently, for an already-cached catalog only) on
  session start; an offline/403 refresh keeps the existing cache.
- `__version__` -> 0.6.0.

## [0.5.0] - 2026-09-29

H13 (RECOMMENDATIONS.md P1): lazy MCP start by default, inline images in the
terminal, `/resume` search, and the first live family-baseline pass for
GLM-5.3/Qwen/MiniMax/Kimi K2.7-code/Kimi K3. See `docs/harness/H13-brief.md`.

- **Lazy MCP by default**: every configured server is now lazy unless it's
  `alwaysLoad` or explicitly `"mcpLazy": false` (per-server, or globally via
  a new top-level `"mcpLazy": false` in `settings.json`) -- reversing the
  pre-0.5.0 "eager unless `mcpLazy: true`" default. A per-server tool cache
  under `~/.rolo-claude/mcp/tools-cache/<server>.json` (keyed by a hash of
  the server's own command/args/env/url/headers) means a lazy server's tool
  names/descriptions are still known -- and findable/preloadable -- before
  it's ever connected: no cache yet (first run, or the config changed)
  connects it once to learn its tools and writes the cache; a valid cache
  seeds a new `"cached"` handle state with zero connections. A tool call, or
  `/mcp`'s own reconnect, connects a cached/lazy server for real on demand;
  a live tool list that turns out to differ from what the cache promised
  refreshes the catalog and surfaces a short note on that same call. Status
  bar/`/mcp`/`mcp list`/`doctor` all show the new `"cached"` state (`doctor`
  additionally reports each lazy server's cache age). Every uncached server
  needing a real connect (a first run against N configured servers) does so
  CONCURRENTLY (`McpManager.start_many`), not one at a time -- a real bug
  from live dogfooding this milestone's own acceptance line surfaced before
  any fix landed (up to N times MCP_TIMEOUT, worse than the pre-0.5.0 eager
  path, which was already concurrent).
- **Inline images in the terminal**: a screenshot/image tool result renders
  inline via the kitty graphics protocol (kitty, WezTerm, Ghostty, foot) or
  sixel (other terminals, detected live), with the existing type/size/
  dimensions caption as the fallback everywhere else (no real tty, tmux
  without `allow-passthrough on`, detection finds nothing, or `images:
  "caption"`/`"off"` in `~/.rolo-claude/config.json` / `--no-inline-images`).
  `textual-image` (PyPI) was evaluated per the brief and rejected on both
  grounds it named: its own repo declares LGPL-3.0 (not permissive) and it
  requires Python >=3.12 (this project supports >=3.10) -- the kitty/sixel
  encoders are implemented directly instead (`rolo_claude/tui/images.py`);
  Pillow (the existing `vision` extra, now also `pip install rolo-claude
  [images]`) enables the sixel encoder and bounded downscaling for both
  protocols, but nothing here requires it (kitty decodes PNG/JPEG itself).
- **`/resume` search**: the session picker (TUI `/resume`, and an ambiguous/
  no-match `--resume <text>` at TUI startup -- previously silently ignored
  outside print mode, now genuinely wired through `build_session`) gets a
  live text filter, fuzzy-ranked over title/first prompt/cwd/model;
  `--resume <text>` in print mode picks the unique match or lists the
  ambiguous candidates instead of silently guessing the most recent one.
- **Family baselines**: `docs/harness/FAMILY-BASELINE-2026-09-29.md` --
  live acceptance rows for GLM-5.3, Qwen3.8 Flash, MiniMax M3, Kimi
  K2.7-code and Kimi K3 on OpenRouter (pong, a 200-line Read, a Write ->
  Edit -> Bash chain, one steer; Kimi K3 kept to the cheap pong+Read subset
  per the brief).

## [0.4.1] - 2026-09-28

H12 (RECOMMENDATIONS.md P0): the whole first run in one command, a
prescriptive `doctor`, and the cheapest fix for DeepSeek V4.1 Flash's own
telemetry-observed 8% Edit "Found multiple matches" rate. See
`docs/harness/H12-brief.md`.

- **`rolo-claude init`** (`rolo_claude/init_cli.py`): picks a preset
  (`home`/OpenRouter, `work`/Databricks, `claude`/your subscription --
  auto-detected, or `--preset`/`--yes` for non-interactive), asks for a
  missing OpenRouter key or Databricks host/token with hidden input and
  writes it into the same env file the harness already reads (POSIX mode
  0600, dir 0700, existing lines preserved, never echoed), sets
  `~/.rolo-claude/config.json`'s `model` key, runs `doctor` and `models
  --refresh`, sends a live pong, and (Linux only) offers a static `rg`
  install into `~/.local/bin` and the `~/.local/bin`-on-PATH rc-file line
  (`~/.zshenv` for zsh, `~/.profile` otherwise, behind a marker comment) --
  every step is idempotent, so re-running only ever reports the current
  state. Never touches `~/.claude.json`/`~/.claude/settings.json`, never
  prints a key/token. `~/.rolo-claude/config.json`'s `model` now sits in the
  default-model precedence chain (`--model` > `BRIDGE_MODEL`/`routes.json`
  > config.json > the built-in default) that both `-p` and the TUI resolve
  through (`model.resolve_default_model_raw`, `headless.build_session`).
- **Prescriptive `doctor`**: every `[WARN]`/`[MISSING]` line now ends with
  `-> fix: <exact command>` or `-> see: <reference>`; `doctor --json` prints
  the same checks as `{id, status, message, fix, see}` records (what `init`
  consumes for its own summary). New checks: `~/.local/bin` on PATH for a
  non-interactive shell, `tmux` mouse mode when `$TMUX` is set, configured
  MCP servers (eager vs. `mcpLazy`), and the default model in
  `config.json` plus whether its provider actually resolves.
- **Edit tool context hint** (`rolo_claude/providers/model_table.json`'s new
  `edit_hints` map, `providers/profiles.edit_hint_for`): a DeepSeek/Kimi/
  GLM/Qwen/MiniMax-family session's Edit tool description gets one extra
  line asking for at least three lines of `old_string` context, computed
  once when the frozen tool catalog is built so the wire request stays
  cache-prefix-stable; Claude/GPT sessions are unaffected, byte for byte.
- **README/INSTALL.md**: a "Quick start (Kali / Linux)" section leads the
  README now (install, `rolo-claude init`, `rolo-claude`, then Windows in
  five lines); INSTALL.md points to `init` up front.

## [0.4.0] - 2026-09-28

H11: Claude models through the user's own Claude subscription (`cc:` route)
plus first-class `ant:` aliases for the same six models via
`ANTHROPIC_API_KEY`. See `docs/harness/H11-brief.md` (and the H11b fix-pass
review below, `docs/harness/review-findings-h11.md`). Binding constraint: rolo-claude never reads, copies or
replays Claude Code's OAuth credentials (`~/.claude/.credentials.json`) and
never calls `api.anthropic.com` with them -- the subscription is used the one
legitimate way, by driving the installed `claude` binary headlessly under the
user's own login, with rolo-claude's own tools exposed to it through a local
MCP bridge. rolo-claude keeps its own tools, permissions, hooks, session log
and telemetry for every `cc:` turn.

- **Aliases (`rolo_claude/providers/cc_models.py`)**: `cc:fable`/`opus`/
  `opus-5`/`opus-5.0`/`opus-4.8`/`opus-4.6`/`sonnet`/`sonnet-5`/`haiku` ->
  `claude-fable-5-1`/`claude-opus-5-5`/`claude-opus-5`/`claude-opus-5`/
  `claude-opus-4-8`/`claude-opus-4-6`/Claude Code's own `sonnet`/
  `claude-sonnet-5`/Claude Code's own `haiku`; `ant:` gets the SAME nine
  names resolved to real API ids; a bare alias with no prefix resolves to
  `cc:` (a subscription login and no `ANTHROPIC_API_KEY`), `ant:` (the key
  set), or an error naming both; any full id and a trailing `[1m]` suffix
  pass through unchanged. Context/output/pricing for all nine come from a
  vendored table (OpenRouter's own `anthropic/*` catalog rows), refreshable
  live via `rolo-claude models --cc --refresh`. `doctor` and the `/model`
  picker ("Claude subscription (via Claude Code)" group) both surface this.
- **Transport (`rolo_claude/agent/cc_process.py`, `rolo_claude/ccbridge/`)**:
  one `claude -p --output-format stream-json --input-format stream-json
  --verbose --include-partial-messages --tools "" --strict-mcp-config
  --mcp-config <inline> --settings '{"disableAllHooks":true}'
  --permission-mode bypassPermissions --session-id/--resume <uuid5 of the
  rolo session id>` subprocess per `cc:` session, lazily started, stdin held
  open across turns -- every flag verified live against the installed
  claude 2.1.281/2.1.284. The `--mcp-config` names one stdio server, "rolo"
  (`python -m rolo_claude.ccbridge`), so Claude Code exposes every bridged
  tool as `mcp__rolo__<Name>`; the child forwards `tools/list`/`tools/call`
  to a `ToolBridgeServer` in the parent process (a Unix socket, mode 0600,
  on POSIX; a TCP loopback socket + a random per-session token on
  Windows) which runs the real dispatch -- permission decide, PreToolUse/
  PostToolUse hooks, the tool's own run, the session log, TUI events --
  for every call, with Claude Code's own tool_use id kept verbatim. Esc
  kills the subprocess (process group, no orphans) and synthesizes
  interrupted results for anything still in flight; the next turn restarts
  with `--resume`. A steer sends its line to the running subprocess
  immediately (Claude Code queues it on its own). Switching models into
  `cc:` mid-session primes the new subprocess with the prior log as one
  `<conversation-so-far>` message; switching away needs nothing special
  (every `cc:` turn already logged ordinary user/assistant/tool_result
  nodes). `stats --models`/`/cost` show `cc:` rows with `total_cost_usd`
  marked as Claude Code's own estimate, never real per-token billing.
- **Tests**: `tests/helpers/fake_claude_cc.py` (a scripted `claude` stand-in
  that also acts as a REAL MCP client against the real `ccbridge` child) plus
  `tests/test_cc_models.py`, `tests/test_ccbridge_server.py`,
  `tests/test_cc_session.py` -- 53 tests covering alias resolution, the
  bridge's own wire protocol (both transports), lazy start/reuse/kill,
  tools/list parity with the frozen catalog, deny rules, a live interactive
  permission card, hooks firing exactly once, pairing invariants across an
  Esc mid-call, the `estimate` usage flag, `--resume` after a restart and
  after a fresh `--continue`-shaped process, steering, a cc:<->or: model
  switch, and the credentials file never being opened.

### H11b fix pass (same 0.4.0 milestone, no version bump)

A 28-finding review of the H11 work above (2 critical, 12 major, 14 minor --
`docs/harness/review-findings-h11.md`) found the `cc:` route's steering
accounting could hang a turn forever, and the `claude` subprocess inherited
this process's whole environment (provider keys, an outer Claude Code
session's own identity) instead of a stripped one. Both are fixed, along
with the rest of the findings:

- **Steering (critical)**: `claude` now runs with `--replay-user-messages`;
  every stdin line is tracked in a FIFO until its own `isReplay` echo
  confirms claude actually consumed it, and a turn ends at a `result` only
  once that FIFO is empty -- correct whether a steer is absorbed into the
  turn already running (one result covers both) or answered as its own
  follow-up turn (queued lines get a combined reply). A steer is logged
  only once consumed, never at send time.
- **Child environment (critical)**: the `claude` subprocess and its MCP
  child now get `tool_child_env()` minus every `ANTHROPIC_*`/`CLAUDE_CODE_*`
  variable and `CLAUDECODE` -- an ambient API key, base URL or an outer
  session's own identity never reaches it. Doctor and `cc:` now refuse to
  report "available" unless `claude auth status`, checked in that same
  stripped environment, reports `authMethod: claude.ai`.
- **The bridge's dispatch is now the loop's own dispatch**: EnterPlanMode/
  ExitPlanMode, AskUserQuestion, and Agent/Task (streamed live, a child's
  own permission ask reaching the same card the parent's calls use) all
  reuse `Session._resolve_tool_call`/`_finalize_tool_result` directly
  instead of a separate, partial re-implementation -- always-allow rules,
  PreToolUse/PermissionRequest/PermissionDenied hooks and the loop breaker
  now all apply to bridged calls too. Images now flow both ways (a pasted
  image reaches claude in the stream-json message; a bridged tool's image
  result reaches claude as a real MCP `ImageContent`, never "[image
  block]"). `--append-system-prompt` now carries a real addendum (skills
  index, subagent_type list, MCP server instructions, deferred-tool names,
  the plan-mode note, and a sub-agent's own body). Background-job/sub-agent
  notices and a `UserPromptSubmit` hook's context are sent to claude, not
  just logged; Stop hooks fire on `result`.
- **MCP catalog growth**: the bridge sends a real
  `notifications/tools/list_changed` (a long-poll on a dedicated
  connection) when ToolSearch grows the session's catalog, so Claude Code
  can discover and call a tool that loaded mid-session.
- **Session identity**: the cc conversation id is logged in a `meta` node;
  `/clear` drops it (a fresh conversation); `/fork` and a `cc:` model
  change close the live process and restart with `--resume`/
  `--fork-session`; a `--resume` claude rejects ("No conversation found"/
  "already in use") falls back to a fresh `--session-id` primed with the
  prior log instead of surfacing the error.
- **Accounting and errors**: `total_cost_usd` (cumulative per claude
  process) is now logged as the per-turn delta, never double-counted. An
  error-shaped result (no stream deltas) now becomes a real error event,
  a non-zero `-p` exit and an `is_error` result; an unconnected bridge MCP
  server gets a warning instead of silently leaving claude with no tools.
- **Lifecycle**: a sub-agent's own claude subprocess/bridge/socket closes
  when its call ends (not just at process quit); an old bridge is closed
  before a restart replaces it; a tool call's result is paired with its
  tool_use even if the bridge's own dispatch raises.
- **Command-line budget (found by this pass's own live acceptance, not in
  the review)**: the `--append-system-prompt` addendum rides on claude's
  own command line, which on Windows goes through `claude.CMD` -> cmd.exe;
  a real dev box with a dozen verbosely-described skills hit "The command
  line is too long" and the subprocess never started. Every discovered
  skill/agent/MCP description is clipped and the whole addendum is capped
  (~3.5K chars) -- a short hint beats a session that cannot start.
- **Tests**: `tests/test_cc_session.py` grew from 21 to 46 (steer absorbed
  between tool calls / two steers queued behind a turn, the stripped child
  env, plan tools, AskUserQuestion, streamed Agent + a child's live ask,
  images both ways, notices/hook context, `list_changed`, the logged cc
  session id, `/clear`, `/fork`, resume fallback, a cc:->cc: model switch,
  per-turn cost delta, error results, Stop hooks, child close, and --
  POSIX only -- pgrep/SIGHUP orphan checks); `test_tui.py` gained a real
  Textual pilot of a `cc:` session's PermissionCard (46 -> 47); each cc:
  test module now sets its own scratch home, and
  `tests/test_bash_background_jobs.py` restores `BRIDGE_TEST_HOME`.
- Corrected this changelog's own "per-connection token" (it's per-session)
  and "`/compact` no-op" (now a real one, not a failure) claims from
  earlier in this same entry.

## [0.3.1] - 2026-09-25

H10: free L0 telemetry from the existing session logs, plus a human-gated
`/improve` (L1 memory/rules + L3 skills). See `docs/harness/H10-brief.md`. Nothing here lets a model edit its
own instructions silently -- every written artifact is approved on a card
or an explicit headless `--apply`; provenance is information shown on the
card, never a block or a classifier; nothing drafts or writes in `-p`;
nothing interrupts a running turn, auto mode included (status-bar/toast
hint only). Settings tuning, prompt optimisation and code self-edits were
explicitly declined and are not built.

- **Telemetry (`rolo_claude/telemetry.py`)**: `rolo-claude stats [--models]
  [--tools] [--since 7d|30d|all] [--all-projects] [--session ID] [--json]`
  and `/stats --models` in the TUI (run off the UI thread, same worker
  pattern as `/resume`'s session list) aggregate per-(model,provider) and
  per-tool counters -- tokens/cost, avg ttft/latency, `finish=length`%,
  retries/status codes, overflows, tool-call/error rates, repair-hit% by
  kind, edit-failure%, steers/interrupts/compactions/loop-breaker trips --
  entirely from non-wire metadata added to existing `usage`/`assistant`/
  `tool_result` session-log nodes (never a new model-visible field; proved
  with byte-identical derived-request tests across both the OpenAI-dialect
  and native-Anthropic wire bodies). Results cache in
  `~/.rolo-claude/stats-cache.json` keyed by (path, size, mtime); a corrupt
  log line is skipped and counted, never a crash. `doctor` now shows the
  sessions count, cache age and the active `/improve` config.
- **`/improve` (`rolo_claude/improve/`)**: clusters recent failures from the
  telemetry scan (repeated tool errors, repair-layer hits, loop-breaker
  trips, user corrections, Read ENOENT/wrong-cwd, a tool sequence recurring
  across sessions), drafts up to `improve.max_candidates` (8) candidates
  with ONE model call (config `improve.model` -> the small model -> the
  session model), and reviews them one `ImproveCard` at a time (`a` apply,
  `e` edit in `$VISUAL`/`$EDITOR` then apply, `s` skip, `d` dismiss forever,
  `q` stop). A memory candidate writes Claude Code's exact frontmatter
  shape plus a MEMORY.md index line; a rule writes `.claude/rules/*.md`
  (or `~/.claude/rules/`); a skill ships with `disable-model-invocation:
  true`. Only new files are created unless the target already carries the
  `<!-- rolo-claude improve: ... -->` provenance comment (a collision with
  a user-authored file picks a new name instead). Applying appends an
  `improve_applied` log node and refreshes the next turn's CLAUDE.md/
  memory-index snapshot (same mechanism as post-compaction re-injection).
  A session's own counters crossing `improve.hint_threshold` shows a
  one-time, counters-only hint -- never a model call, never a card.
  Headless: `rolo-claude improve [--since] [--all-projects] [--json]
  [--out FILE]` and `rolo-claude improve --apply FILE#ID` (repeatable);
  `-p` never drafts or writes; `--bare` disables it entirely.
- **Config**: `~/.rolo-claude/config.json`'s new `improve` key (`enabled`,
  `hint`, `model`, `since_days`, `max_candidates`, `hint_threshold`),
  settable with dotted paths (`rolo-claude config set improve.model
  or:...`).

## [0.3.0] - 2026-09-25

`rolo-claude` becomes its own standalone, Claude-Code-compatible agent
harness: its own agent loop, built-in tools, permission engine, MCP client
and TUI, reading Claude Code's real config files unchanged and driving
OpenRouter/Databricks-hosted open-weight models (DeepSeek, Kimi, GLM, Qwen,
MiniMax, ...) instead of an Anthropic subscription. Kali Linux is the
primary target platform; Windows is the build/test host. The original
`claude-bridge` proxy (drives the real `claude` binary against the same
providers) is kept unchanged as the `rolo-claude proxy` subcommand.

Built up over milestones H0-H9 (plus U0/U2/U5 for the TUI) -- see
`docs/harness/README.md` for the full per-milestone brief/review index:

- **H0-H1 -- foundation and provider layer**: package split from the
  `bridge.py` proxy; per-provider compat profiles (OpenAI-chat dialect +
  native Anthropic-Messages passthrough); append-only JSONL session log as
  the single source of truth, with "model-visible means logged" enforced by
  a runtime invariant; reasoning replay per model family
  (`reasoning_content`/`reasoning_details`/thinking blocks); explicit
  `max_tokens` budgeting; the cumulative per-turn loop breaker (remind 3 /
  deny 5 / end turn 8).
- **H2 -- built-in tools, repair, permissions**: Read/Write/Edit/Bash/
  PowerShell/Glob/Grep/WebFetch/TodoWrite/AskUserQuestion and friends; a
  tool-call repair layer for weaker models (fenced/leaked calls promoted to
  real `tool_use`); the full Claude Code permission-rule grammar (deny/ask/
  allow, modes, Bash sub-command matching, path globs) with **no safety
  classifier, destructive-command list or protected-path heuristic
  anywhere** -- `auto` allows everything not matched by the user's own
  deny/ask rules, exactly as decided up front.
- **H3/H3b/H3c -- MCP**: the official MCP SDK, stdio/http/sse transports,
  frozen per-session tool catalog with lazy `ToolSearch` loading (parallel
  and abortable as of H9), `--chrome`/`--playwright` browser tools, plugin-
  provided servers, the `mcp` CLI.
- **H4 -- hooks, skills, commands, web**: every Claude Code hook event and
  handler type (`command`/`prompt`/`agent`/`http`/`mcp_tool`); skills and
  custom slash commands from user/project directories; WebFetch/WebSearch;
  **uninterrupted auto mode with mid-turn steering** (Esc is the only hard
  stop; typing during a turn queues a steer that's applied at the next
  chunk boundary; nothing may say an action "isn't allowed in auto mode").
- **H5/H5b/H5c -- compaction, native Anthropic routes, reliability
  hardening**: auto-compaction (OpenCode-style pruning + an 8-section
  checkpoint summary, floored trigger so small-context models still
  compact sanely, never back-to-back); native `ant:`/Databricks-Claude
  thinking replay with signatures; two full review passes (H4/H5/H3c, then
  H5b) closing 18 and then 19 findings covering steer/abort races, hook
  interruptibility, sub-agent permission surfacing, `SessionStart` env-file
  handling, and stream-json mid-turn steering.
- **H6/U5 -- sub-agents, plan mode, resume, TUI polish**: the `Agent`/
  `Task` tool with a worker pool, background sub-agents, plan mode
  (`EnterPlanMode`/`ExitPlanMode`, a plan file, TUI `PlanCard`), session
  resume/fork/rename, chords + a which-key overlay, `/rewind`, `/export
  --sanitize`, `/stats`.
- **H8 -- background jobs, vision, packaging**: `Bash(run_in_background)` +
  `BashOutput`/`TaskStop`; image/vision support (Read, MCP tool results,
  `@path`, `--file`) with captioned tool cards; `NotebookEdit`; catalog
  vendoring (`models --refresh`, a package-vendored offline fallback);
  offline work-box installs (`tools/vendor_wheels.py`); a full README
  rewrite.
- **H9 -- bug hunt, Linux-first acceptance, MCP compatibility, release**:
  whole-tree review; Linux-first acceptance of every milestone's acceptance
  lines (WSL Ubuntu; the Kali VM when reachable); an MCP compatibility
  matrix (user/project/local/plugin scopes, `claude mcp add` <->
  `rolo-claude mcp add` interop, unusual tool schemas surviving OpenRouter
  and the Databricks 32-tool/no-`$ref` simplifier, `/mcp` reconnect,
  http/sse transports); a randomised fuzz harness (malformed streams,
  unicode edge cases, interrupts/steers at every event boundary, 429/5xx
  storms) asserting log-integrity invariants hold with no hangs or leaked
  threads/processes; a period-2 ("ping-pong") doom-loop detector layered on
  the existing per-call breaker; `doctor` checks for `rg`, `$VISUAL`/
  `$EDITOR` and a usable Bash shell; the `ToolSearch`-triggered lazy MCP
  start made parallel and abortable; a sampling-table audit (temperature/
  top_p/top_k per model family) against the open-weight adapter research,
  plus wiring `sampling_unsupported_params` from `model_table.json` into
  the actual request body instead of leaving it decorative; four dead
  `model_table.json` rows recovered (a JSON key had explanatory prose baked
  into it, so `qwen/qwen3-max`, `qwen/qwen3-max-thinking`, a Mistral batch
  variant and a Databricks Llama row could never resolve their real,
  tuned settings); `type: http` MCP servers connect again (a 3-tuple
  unpack against an SDK that yields 2); `/mcp` reconnect re-reads
  `~/.claude.json`/`.mcp.json` so an edited or brand-new server is picked
  up mid-session (`McpManager.resync_from`); progress notifications can
  actually keep a long MCP call alive (the SDK's own non-resettable
  timeout no longer races the keepalive); a family-agnostic MCP tool-
  schema normaliser (local `$ref` inlining, tuple-items, nullable
  `anyOf`) for every OpenAI-shaped family, not just Kimi/Gemini; the
  Databricks simplifier keeps a typed fallback for a wide `anyOf`;
  `rolo-claude mcp add` writes the same `"env": {}` the real binary does;
  `rolo-claude proxy launch` finds a native POSIX `claude` (it only ever
  looked for the Windows shims -- a crash on Kali, the primary platform);
  a PreToolUse hook written in the PermissionRequest `decision.behavior`
  shape is honoured instead of silently ignored; `--playwright` fails
  fast without a runnable `node`; Kimi `functions.{name}:{idx}` tool ids
  continue across the whole session instead of restarting per call;
  parallel sub-agents with colliding tool_use ids get distinct permission
  waiters; a missing `Path` import in `providers/http.py`.
- **H9b -- whole-tree review fix pass**: closed the review's remaining 24
  findings plus several "NEW from H9" cleanup items, all with real-Session
  or real-CLI pinning tests. Background jobs/sub-agents as one contract: a
  sub-agent's own `JobRegistry` is shared with (and killed by) its parent;
  `-p` now handles SIGHUP the same as SIGTERM (an `atexit kill_all` safety
  net too), verified with `pgrep` on WSL AND the Kali VM that both a
  background Bash job and an MCP stdio server subprocess are gone after a
  normal exit and after SIGHUP; a resume/`TaskOutput` on a task_id whose
  background child is still writing its own log is refused instead of
  racing it, and `AgentRuntime.tasks` is rebuilt from each child's own
  `meta.json` on a `-c` resume instead of coming back "Unknown"; a child's
  usage/cost rolls up into the parent's own CostMeter/log/`--max-budget-
  usd`/`stats`; a child's error/max_turns/blocked outcome is carried onto
  its ToolResult instead of handing back stale text as if it finished
  normally. Secrets: ONE sanitizer (`sk-or-v1-`/`sk-ant-`/`dapi`/`ghp_`,
  quoted `export KEY="…"`, JSON env blocks, Bearer headers) now backs both
  `export --sanitize` and the TUI's `/export --sanitize`; the dead,
  never-wired `session_cli.py` sanitizer is deleted; `CLAUDE_ENV_FILE` is
  sourced against the already-stripped `tool_child_env`, not raw
  `os.environ`. Linux PATH: a Debian/Kali `/etc/profile` login shell no
  longer discards the session's own PATH (foreground and background Bash
  alike), verified with the `unshare -rm` bind-mounted-profile trick on
  WSL. TUI: parallel children stream into their own grouped blocks (keyed
  by agent_id/turn/seq) and never touch the parent's status bar, cost or
  `on_turn_done`; `@server:resource` mention resolution is regex-first,
  runs off the UI thread with a cache and a content cap, so a hung/slow
  MCP server can no longer freeze prompt submission. Also: pre-4.5
  notebooks (no cell ids) accept positional `cell-N` addressing and clear
  stale outputs on a code-cell replace; `turn()`'s own `finally` drains
  queued `@mention`/`!cmd` writes before the next prompt, and manual
  `/compact`/`/clear` count as busy for log-write queuing; a truncated
  Read's continuation hint is computed from the actual last included line,
  on a line boundary; image media type comes from sniffing the real bytes,
  not the file extension, and only the two HARD limits (8000px/5MB) gate
  omission without Pillow -- a plain screenshot under the old 1568px soft
  threshold is no longer omitted for nothing; image offload counts real
  prompts only, never a tool_result's own wire message; a spilled tool
  result's continuation hint is Read-allowed under the session's own
  `tool-results/` dir in every permission mode; Databricks default model
  rows (DeepSeek V4.1 Flash, Kimi K3, GLM 5.3) resolve their real
  1M-token context instead of a generic 128k/16k fallback; offline
  packaging installs cleanly on a bare Python >=3.12 venv (setuptools no
  longer assumed preinstalled) and vendors cp313 wheels too; `bin/rolo-
  claude` resolves a symlink (`readlink -f`) instead of failing outside
  the checkout; a `ucode-settings.json` shaped like a Claude Code
  `--settings` file (an `env` block, or an `apiKeyHelper` command) now
  resolves too, not just the gateway-config key spellings; `/stats`/
  `stats` count real prompts only (never a notice/steer/Stop-continuation)
  and report cache-read/cache-creation tokens; a background-job offset/
  lock race that could duplicate or drop BashOutput text after a timeout
  hand-off is fixed; `doctor` accepts the real minimum Python (3.10, not
  3.9) and `--work` probes the actually-configured model with the
  production header instead of the first DeepSeek/Kimi/GLM endpoint it
  finds. Plus: every temp directory a test creates is tracked and cleaned
  up at the end of a `run_all.py` invocation; `python -X dev -W
  error::ResourceWarning tests/run_all.py` is clean (several unclosed
  HTTP connections and subprocess pipes fixed, without reintroducing the
  Windows orphaned-grandchild hang a naive synchronous `.close()` caused);
  a `ruff check --select F,E9,B` pass across `rolo_claude/`/`tests/`
  (dead imports/locals, unused loop variables, explicit `zip(strict=)`,
  explicit exception chaining); confirmed `wip/` is already excluded from
  the built wheel.

`__version__` is `0.3.0` (`rolo_claude/__init__.py`); see
`docs/harness/ACCEPTANCE-2026-09-25.md` for the full Linux/Windows
acceptance record and `docs/harness/review-findings-*.md` for the detailed
per-finding review history behind the H1-H5c/H9b entries above.

## [0.2.1] - claude-bridge (pre-standalone-harness)

The original `claude-bridge`: a single-file, stdlib-only HTTP proxy
(`bridge.py`) that sits in front of the real `claude` binary and translates
its Anthropic-Messages-API calls to OpenRouter or Databricks, leaving
`claude`'s own subscription, config, memory, MCP servers, skills, hooks and
permissions untouched. Kept unchanged as `rolo-claude proxy` (97 tests).
