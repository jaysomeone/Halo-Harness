# Models

How a `--model`/`/model` string resolves to a real upstream call, per-family
request-shaping rules, the model catalogs and how they refresh, and pricing.
Verified against `halo_harness/model.py`, `providers/profiles.py`,
`providers/cc_models.py`, `providers/dbx_routing.py`, and
`providers/model_table.json`.

## Model reference forms

| Form | Example | Resolves to |
|---|---|---|
| `or:vendor/model` | `or:deepseek/deepseek-v3.2` | OpenRouter |
| bare `vendor/model` (exactly one `/`) | `deepseek/deepseek-v3.2` | OpenRouter |
| `dbx:databricks-<name>` | `dbx:databricks-kimi-k3` | Databricks |
| `dbx:system.ai.<name>` | `dbx:system.ai.my_model` | Databricks |
| `dbx:<endpoint>@anthropic` | `dbx:databricks-kimi-k3@anthropic` | Databricks, forced onto the native Anthropic gateway for this one call |
| bare `databricks-*`/`system.ai.*` | `databricks-claude-opus-5-5` | Databricks (same as the `dbx:` form) |
| `cc:<name>` | `cc:opus`, `cc:sonnet` | your Claude subscription, via the installed `claude` binary |
| `ant:<name>` | `ant:opus`, `ant:claude-3-5-haiku` | `api.anthropic.com` pay-as-you-go (`ANTHROPIC_API_KEY`) |
| a bare subscription alias, no prefix | `opus`, `sonnet`, `fable`, `haiku` | at a Databricks work box: `dbx:<ANTHROPIC_DEFAULT_*_MODEL>`; else `cc:` if logged in and no key is set; else `ant:` if a key is set; else an error naming both |
| `ol:<model>` | `ol:qwen3:30b` | Ollama, the default host (see "Ollama" below) |
| `ol:<model>@<hostname>` | `ol:qwen3:30b@lan`, `ol:gpt-oss:20b@cloud` | Ollama, a named entry in `ollama.hosts` -- local, LAN, or Ollama Cloud |
| `hf:<org>/<model>` | `hf:Qwen/Qwen3-32B` | Hugging Face Inference Providers (the router), `:fastest` (highest throughput) implied |
| `hf:<org>/<model>:<suffix>` | `hf:openai/gpt-oss-120b:groq`, `hf:Qwen/Qwen3-32B:cheapest`, `hf:...:preferred` | same router, a specific partner provider or the `:cheapest`/`:preferred` selection mode -- the whole suffix passes through verbatim on the wire |
| `hf:endpoint/<name>` | `hf:endpoint/my-prod` | a dedicated Hugging Face Inference Endpoint, a named entry in `huggingface.endpoints` (see "Hugging Face" below) |
| `hf:local/<model>` | `hf:local/qwen3-30b` | a local OpenAI-compatible server (llama-server, vLLM, LM Studio, ...) -- a configured `huggingface.local_servers` default entry, else the first auto-detected one (see "Hugging Face" below) |
| `hf:local/<model>@<name>` | `hf:local/qwen3-30b@bench` | the same, a NAMED entry in `huggingface.local_servers` -- never an auto-detected server, which has no name of its own to address |
| `hf:mlx/<org>/<repo>` | `hf:mlx/mlx-community/Qwen2.5-7B-Instruct-4bit` | round 5f, experimental, Apple Silicon only: a Halo-managed `mlx_lm.server` for this exact Hugging Face Hub repo, started on first use and reused afterward -- see "Apple Silicon (`hf:mlx/*`, round 5f)" below and [MAC.md](MAC.md) |
| `oai:<model>` | `oai:gpt-5`, `oai:gpt-6-astra` | the real OpenAI API (`OPENAI_API_KEY`) -- chat completions, or the Responses dialect for the two models that need it -- see "OpenAI API" below |
| `cx:<model>` | `cx:astra`, `cx:gpt-6.1-sol` | your Codex ChatGPT subscription, via the installed `codex` binary -- see "Codex subscription (ChatGPT)" below |
| a `routes.json` alias | whatever `aliases` defines | resolved recursively (max 4 hops) before any of the above rules apply |

Parsing order (`model.py::parse_model_ref`): an exact match in
`routes.json`'s `aliases` is resolved first, recursively; everything after
that follows the table above, checked top to bottom. A ref that matches
nothing raises a clean `InvalidModelError` naming every accepted form (exit
2 from the CLI, with a near-miss suggestion when the catalog is cached).

A trailing `[1m]` suffix (a long-context variant marker) passes through
unchanged on every full id/alias form.

### `cc:`/`ant:` alias table

The same bare names resolve for both prefixes (`providers/cc_models.py`
`CC_ALIASES`/`ANT_ALIASES`); `opus` and `sonnet` are deliberately-moving
"latest" pointers (today both resolve to the `-5-5` point release) --
every other name is a specific, pinned version. `/model`, `halo
models` and the init picker show the resolved id next to each alias (e.g.
`cc:opus -> claude-opus-5-5 (latest Opus)`) for exactly this reason: `opus`
and `opus-5.5` are the same model.

| Alias | `cc:` resolves to | `ant:` resolves to |
|---|---|---|
| `fable` | `claude-fable-5-1` | `claude-fable-5-1` |
| `opus` (latest Opus) | `claude-opus-5-5` | `claude-opus-5-5` |
| `opus-5.5` | `claude-opus-5-5` | `claude-opus-5-5` |
| `opus-5` / `opus-5.0` | `claude-opus-5` | `claude-opus-5` |
| `opus-4.8` | `claude-opus-4-8` | `claude-opus-4-8` |
| `opus-4.6` | `claude-opus-4-6` | `claude-opus-4-6` |
| `sonnet` (latest Sonnet) | Claude Code's own `sonnet` pointer (today `claude-sonnet-5-5`) | `claude-sonnet-5-5` |
| `sonnet-5.5` | `claude-sonnet-5-5` | `claude-sonnet-5-5` |
| `sonnet-5` | `claude-sonnet-5` | `claude-sonnet-5` |
| `haiku` | Claude Code's own `haiku` pointer (today `claude-haiku-4-5-20251001`) | `claude-haiku-4-5-20251001` |

`halo models --cc --refresh` re-pings each alias (a cheap `-p
--max-turns 1` call) and caches the REAL id it got back to
`~/.halo/cc-models.json`, consulted before this static table for
`sonnet`/`haiku` specifically (the two whose target moves as Anthropic
ships new point releases) -- so a stale hardcoded id here is never the
only source once a refresh has run.

### `cx:` alias table

Short names Halo adds on top of the documented ChatGPT-plan ids
(`providers/codex_models.py` `CODEX_ALIASES`) -- every one is a specific,
pinned id; none of them move the way `cc:opus`/`cc:sonnet` do.

| Alias | `cx:` resolves to |
|---|---|
| `astra` | `gpt-6-astra` |
| `sol` | `gpt-6.1-sol` |
| `luna` | `gpt-6-luna` |

The full id also works directly (`cx:gpt-6-astra`). No live `/models
refresh` has run against this table (no ChatGPT login on the build host) --
`halo models --cx --refresh` is implemented the same way `--cc --refresh`
is (a cheap one-token headless call per id, refused ones marked) but
unexercised; see `docs/harness/CODEX-RESEARCH.md` section 2.

## Provider enablement

A provider's models reach `/model`/`halo models`/the `init` default
pick/`doctor` once it is **enabled** -- and (H15 part 2 addendum) that
happens AUTOMATICALLY, straight from real credentials, no `init`/`providers
enable` step required: OpenRouter/Anthropic API (key)/TypeSafe auto-enable
once their key is found (env file, shell env, or the settings env chain);
Databricks once a host AND token are found (same sources, plus
`~/.databrickscfg`); Claude Code subscription (`cc:`) ONLY when `claude
auth status` reports `loggedIn` with `authMethod` exactly `claude.ai` -- a
`claude` driven by an API token or a custom base URL (a work box's own
settings-driven login) never auto-enables it; Hugging Face (Halo 2.0.3
round 4, extended round 5) once `HF_TOKEN` is found, OR at least one
`huggingface.endpoints` entry is configured, OR at least one `huggingface.
local_servers` entry is configured -- any ONE of the three sources alone
is enough; an auto-DETECTED local server (no config entry at all) is
never checked here (that needs a live network probe, out of scope for
this config/env-only check) -- see `/local`/`halo local` for that.
`~/.halo/config.json`'s
`"providers"` block stores OVERRIDES only: `halo providers enable/
disable <name>` (or `/providers enable/disable <name>`, or completing a
tab in `halo init`) writes an explicit `true`/`false` there that
always wins over auto-detection -- `enabled: false` hides an auto-enabled
provider, `enabled: true` forces one on with no credentials at all. The
prefix table (also used by the `/model` picker's group headers, `init`'s
own tabs, `halo providers`/`/providers`, and `doctor`):

| Prefix | Label | Provider name (`halo providers`) |
|---|---|---|
| `dbx:` | Databricks | `databricks` |
| `or:` | OpenRouter | `openrouter` |
| `ant:` | Anthropic API (key) | `anthropic` |
| `cc:` | Claude Code subscription | `claude_subscription` |
| `hf:` | Hugging Face | `huggingface` |
| *(none yet)* | TypeSafe | `typesafe` -- stores `TYPESAFE_API_KEY` only, for a later feature |

Ollama (`ol:`) is deliberately NOT in this table -- see "Ollama" below,
which covers its own multi-host reachability model instead of a single
on/off flag.

A hand-typed ref whose provider isn't enabled is refused with a one-line
message -- for `claude_subscription` specifically (when auto-detection
found nothing, never for an explicit override) that message is the SAME
precise reason (not logged in / claude not installed / logged in via a
non-claude.ai authMethod, each with its own fix) a turn would have failed
with anyway, just surfaced earlier; every other provider names the
`halo providers enable <name>` fix. A provider that's detected (real
credentials/login) but explicitly disabled shows one dim hint line in
`/model` instead of a selectable row. `halo providers`/`/providers`
shows each provider's status as one of `auto (detected from <source>)`,
`disabled by you`, `enabled by you`, or `not set up`, plus reachable/cached
model count.

## Ollama

Halo 2.0.3 rounds 2-3 (`plans/2.0.3-ollama-round2-brief.md`, design doc
`docs/harness/LOCAL-MODELS-RESEARCH.md`). `ol:<model>` (the default host)
or `ol:<model>@<hostname>` (a named entry in `~/.halo/config.json`'s
`ollama.hosts`) -- local, LAN, or Ollama Cloud, all through the SAME
request shape. An Ollama model tag may itself contain a `:` (`qwen3:30b`);
the `@<hostname>` split is always on the first (and only) `@`, never on a
colon. Not yet covered by the fixed provider-enablement table above (that
table assumes one on/off flag per provider; Ollama is multi-host
reachability instead) -- an explicit `providers.ollama.enabled: false`
override still refuses an `ol:` ref the same way it would for any other
provider name, but nothing auto-detects "enabled" the way a single API key
would.

**Why the native API, never the OpenAI-compatible shim**: Ollama's
`/v1/chat/completions` endpoint has no equivalent of `options.num_ctx` --
its own docs state plainly that context size requires a custom model via
`Modelfile`, not per-request configuration. `ol:` always uses `/api/chat`
instead, so Halo can size the context window itself, every request,
without the user ever having to find a server knob.

**Host config** (`ollama.hosts`, `halo config set ollama.hosts '[...]'`,
or hand-edited): a list of `{name, url, default, keep_alive, max_ctx,
num_parallel_hint, api_key}`. With no entries configured at all, Halo
synthesizes exactly one default host from `OLLAMA_HOST` (Ollama's own env
var; a bare `host:port` is normalized to a full URL) falling back to
`127.0.0.1:11434`, picking up an ambient `OLLAMA_API_KEY` for that one
host's `api_key` too (Ollama Cloud). Every non-loopback host is treated as
unauthenticated by definition unless its own `api_key` is set -- Ollama
itself has no auth mechanism at all; a reverse-proxy/bearer setup on a LAN
host is the user's own responsibility, never assumed.

**Request shape**: `options.num_ctx` on every request (never a Modelfile
default -- see above), `keep_alive` only when the host entry configures
one (a request-level `keep_alive` overrides the server's own
`OLLAMA_KEEP_ALIVE`, so an unconfigured host leaves the field out and the
server's setting stands), `think` mapped from the session's
effort level (off for `low` on a bool-only thinking model, graded
low/medium/high for the gpt-oss family specifically, on for medium and
above everywhere else; omitted entirely when no effort is configured,
letting the model's own default apply), and tools in the same OpenAI
function shape (`{"type": "function", "function": {name, description,
parameters}}`) the native API already expects -- no second tool-shape
conversion needed. No `tool_choice` field exists on this dialect.

**Context ownership**: `num_ctx = min(trained context from
model_info["<family>.context_length"], host.max_ctx override, a fit
estimate, 131072)`, falling back to a conservative 8192 when the trained
context isn't known yet (before the catalog has loaded for that model
once). Compaction triggers at 75% of whatever `num_ctx` that request
actually sent. The fit estimate (Halo 2.0.3 round 3) is the largest
power-of-two context whose KV cache fits in free memory after the
model's own weights: on the LOCAL host, free/total VRAM comes from the OS
GPU tool for the detected vendor (`nvidia-smi` on Windows/Linux, verified
live; `rocm-smi`/sysfs on AMD and `system_profiler` on Apple stay
UNCONFIRMED -- no such hardware available this round), cached for about a
minute so a turn never shells out more than once in that window; on a
REMOTE host, there is no OS-level read at all, so the fit estimate is
just that model's OWN already-loaded `context_length` from `/api/ps`
when it happens to be loaded there already, else unknown. Either way
`None` (unknown) simply drops out of the `min(...)` -- never a crash,
never a guessed number.

**An untagged name IS its own `:latest`** (fix pass after a live run
surfaced the gap): Ollama stores a model created/imported with no
explicit tag (`ollama create foo`, `halo local import ... --name foo`)
as `foo:latest`, and reports it that way from `/api/tags`/`/api/ps` --
every name comparison Halo makes (the catalog lookup, the trained-
context lookup, the fit estimate's `/api/ps` matching, calibration,
panel rows, `/local`) normalizes both sides through ONE helper
(`providers.ollama.ollama_names_match`) before comparing, so `ol:foo`
and `ol:foo:latest` are always treated as the same model. Before this
fix, an untagged model's own catalog row went unmatched, `trained_
context` silently came back `None`, and `num_ctx` fell all the way to
the conservative 8192 fallback -- discovered live when a model imported
without `--name`'s tag then overflowed on an ordinary prompt.

**Ollama DOES answer a 400 on overflow** (the SAME live run corrected a
second wrong assumption): a prompt longer than `num_ctx` is NOT silently
truncated server-side -- build 0.34.2 answers `HTTP 400 {"error":
{"code":400,"message":"...","type":"exceed_context_size_error",
"n_prompt_tokens":N,"n_ctx":M}}`. Halo now handles it in two tiers: if a
BIGGER `num_ctx` is actually allowed (the smallest of whichever of the
learned cap, `host.max_ctx`, and the live fit estimate are known, never
exceeding the usual 131072 hard cap) covers `n_prompt_tokens` plus the
turn's own output budget, Halo retries that ONE request once more at the
smallest power-of-two context that holds both -- no compaction, the
model never even sees a shorter prompt. Only when no bigger window is
available (or the retry overflows too) does this become an ordinary
context-overflow event, triggering the existing compaction-and-retry
path exactly like every other dialect.

**Three outcomes, not two** (fix pass after a live run surfaced the gap):
`/api/ps` is checked FIRST, local hosts included. (1) **Already loaded**
-- that model's own loaded `context_length` IS the fit estimate, full
stop; the GPU-memory arithmetic is never repeated for it, because two
slightly different free-VRAM readings (e.g. two concurrent `halo -p`
processes) used to compute two different `num_ctx` values for the SAME
loaded model and force Ollama to reload it, with partial offload, just
to change its context size. (2) **Not loaded, fits** -- the usual
headroom math, except free memory is first topped up with every OTHER
currently-loaded model's own `size_vram` (Ollama can evict any of them
to make room), since that VRAM is reclaimable, not actually unavailable.
(3) **Not loaded, does not fit** even after reclaiming -- a distinct,
POSITIVELY-known outcome from plain "unknown": `num_ctx` falls back to
the conservative 8192 (never the 131072 hard cap, which is only correct
when nothing at all is known) -- a model already spilling to system RAM
must not also be handed the largest possible KV-cache budget.

**Streaming and tool-call ids**: the response is NDJSON (one complete
JSON object per line, never SSE) -- `message.content`/`message.thinking`/
`message.tool_calls` accumulate per line; `message.thinking` is captured
for display only and never replayed on a later request (undocumented
whether Ollama expects it back at all). Ollama sends no id for a tool
call, only a 0-based `index` -- Halo synthesizes its own stable `toolu_`
id the moment an index is first seen, so the rest of the tool-result/
replay machinery never needs a second code path for this provider; a
tool-result message replays as `{"role": "tool", "tool_name": "<name>",
"content": "<result>"}` (no id at all, keyed by name). `done_reason:
"length"` maps onto the ordinary `max_tokens` stop reason; `done_reason:
"load"` (semantics UNCONFIRMED against a live server) retries the turn
ONCE, silently, but only while nothing has been shown to the user yet for
that turn -- real output alongside a "load" reason is never discarded.

**Catalog and capability probe**: `/api/tags` + `/api/show` per model are
merged into one per-host catalog, cached in memory with a short TTL (never
written to disk) -- `details.quantization_level`/`family` and
`model_info`'s trained context come from here. Separately, a background
`/api/version` probe (honouring the same `BRIDGE_TEST_NO_BACKGROUND_NET`
test seam every other background probe does) reports whether a host is
reachable at all. What `/api/show`'s `capabilities` list CLAIMS and what a
model actually DOES when asked are kept deliberately separate: the first
time Halo sees a given model digest, it sends one cheap, real tool-call
turn and records whether `tool_calls` actually came back non-empty --
cached durably per digest (`~/.halo/ollama-capabilities.json`), so the
cost (one real inference call) is paid at most once per distinct model
digest, never once per catalog read. A model re-pulled under the same
name/tag with different weights (a new digest) is probed again exactly
once. This is surfaced later as a measured badge, never a vendor claim.

**Host analysis (`/ollama`, `halo ollama`, `halo doctor`)**: Halo 2.0.3
round 3. One panel/CLI page per configured host: reachable, version, and
every currently-loaded model's `size` vs `size_vram` as ONE plain
sentence ("fully loaded in GPU memory" or "partially offloaded: X of Y
in GPU memory (N%), the rest in system RAM (slower)") -- never a block,
never gated on; a partially-offloaded model still runs, just slower.
Per model: trained context (the catalog's `model_info`) next to the
EFFECTIVE context it's actually loaded at (`/api/ps`'s own
`context_length` -- ground truth for a loaded model, not a guess), and
the KV-cache bytes/token figure -- standard GGML/llama.cpp accounting
(`2 x layers x kv_heads x head_dim x bytes_per_elem`, f16 `bytes_per_elem`
assumed since Ollama exposes no per-model KV-quant field), not
independently re-derived this round; the panel's own help text repeats
this origin note. `halo doctor` gains a lightweight one-line-per-host
Ollama section (reachable, version, loaded count only -- never the full
per-model `/api/show` fan-out the richer panel does).

**Host setup checklist (`halo ollama doctor [--host NAME]`, round 5b part
2)**: the SAME per-host analysis above, plus the documented host-tuning
recommendations Halo cannot read back from any API, as plain sentences,
never a block: `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`,
`OLLAMA_NUM_PARALLEL=1` on a single-user box, `OLLAMA_KEEP_ALIVE`,
`OLLAMA_CONTEXT_LENGTH` -- and WHERE each one actually lives, per OS of
the HOST (a local host: the OS Halo itself is running on; a remote host:
unknown, so all three print briefly): **Windows** -- the tray app's own
"Expose Ollama to the network" switch (its Settings screen) overrides
`OLLAMA_HOST` outright; setting it yourself is a user-scope environment
variable plus quitting and relaunching the tray app. **macOS** -- the
menu-bar app's own "Expose to network" switch, same override relationship;
setting it yourself is `launchctl setenv OLLAMA_HOST 0.0.0.0:11434` plus
relaunching the app -- from the research doc's own community-knowledge
note, NOT independently confirmed live (rolo's own Mac, when available, is
the live check). **Linux** -- `sudo systemctl edit ollama`, one
`Environment="VAR=value"` line per variable under `[Service]`, then
`sudo systemctl daemon-reload` and a restart. A loopback-only host (its
own configured URL is `127.0.0.1`/`localhost`) gets one extra plain line:
"this daemon is reachable from this machine only; to share it on the LAN
flip <the per-OS switch above>." `halo doctor`'s own Ollama section prints
the identical checklist text per host, one `[OK]`-prefixed line per
sentence, never a WARN just for this.

**Tool-catalog sizing by context class**: Halo 2.0.3 round 3. The `ol:`
ProviderProfile's `tools_max` now follows the EFFECTIVE `num_ctx` instead
of round 2's permanently-unbounded `None`: under 16k tokens -> 16 tools,
16k-32k -> 32, 32k-64k -> 64, 64k and above -> 128 -- overridable with the
`ollama.tools_max` config key (`docs/CONFIG.md`), and never below however
many built-in tools this platform actually ships (they're frozen into
every session before any cap is computed, so a smaller number would be
unreachable). The SessionCatalog -- not a second mechanism -- is what
actually shrinks the live tool list to fit (the same cap-shrink/LRU-evict
dance `/model` already runs on a provider switch); the panel shows the
resulting catalog's rough per-request prompt-token cost next to each
loaded model's `tools_max`.

**Roles**: a local `ol:` model defaults to a SUPPORTING role (the model
picker's `u` action pre-selects `small`, never `orchestrator`/main) when
assigning it a role -- `roles.<name>` is the exact same config-table
mechanism `/roles`/`halo roles` already read, no second one. Choosing a
local model as the session's main model anyway is never blocked; when
its catalog row doesn't declare tool-calling support, the plain
consequence sentence prints ("... can answer questions as the main
model, but cannot edit files, run commands, or call any other tool") and
the choice proceeds regardless.

**`/local`** (round 5 widens this from Ollama-only): `/local <question>`
is a one-shot call to `roles.small`'s model -- an `ol:` OR `hf:` ref --
answered inline and NEVER logged into the session's own transcript, the
exact same `call_small_model` path title generation/`/improve` already
use, which builds its body directly rather than through `derive_request`.
Bare `/local` (or `/local refresh`) instead opens the shared local-model
discovery view described under "Hugging Face" below.

**Not yet in this round**: images (`images`, a user message's base64
array), `format` (structured output) -- see `plans/2.0.3-ollama-round2-
brief.md` and the rounds after it.

**Fit calibration (round 5b)**: `halo ollama calibrate <model> [--host
NAME] [--start N]` loads `model` at a candidate `num_ctx` (the GPU-based
estimate when a local or `ssh:`-configured read exists, else 32768),
reads `/api/ps` back (`size` vs `size_vram`), and steps DOWN by powers of
two until it is fully resident in GPU memory -- or stops at 4096 and
reports "does not fit." The result -- `{host url, model, digest,
max_full_gpu_ctx, measured_at, ollama_version}` -- is recorded in
`~/.halo/ollama-fit.json` and takes precedence over everything else once
known: a MEASURED fact outranks a computed guess. It never expires on its
own; it is re-measured only by an explicit re-run of the command, or
automatically the moment the model's own `digest` (re-pulled weights) or
the host's `ollama_version` (a server upgrade) changes. The FIRST time a
model is used on a host with no learned cap at all, Halo runs the same
procedure automatically before the first request, with one plain notice
line ("Halo calibrated ... fully resident at num_ctx=...") -- opt out
with `ollama.auto_calibrate: false` (docs/CONFIG.md). `/ollama`/`halo
ollama` show every catalog model's learned cap (or "not calibrated") next
to a "what fits" column and the one-phrase source of that decision
("learned cap" / "host max_ctx" / "fit estimate" / "remote default" /
"trained context" / "hard cap" / "fallback").

**Calibration also steps UP (round 5b part 2)**: once the DOWN-stepping
loop above finds a candidate that fits fully resident, calibration keeps
DOUBLING from there -- `halo ollama calibrate`'s own first guess on a
roomy card is often not the model's true ceiling, and settling for it
would under-use real headroom. Bounded by the model's own trained context
and the hard cap (131072) either way, and stopping at the FIRST candidate
that does NOT fit (never stepping back down to search for a smaller gap)
-- the recorded `max_full_gpu_ctx` is the LAST value that was still fully
resident. `halo ollama calibrate --no-up` skips this phase and keeps the
first fitting guess, for a faster (but possibly smaller) measurement. The
automatic first-use trigger steps up too, bounded by that model's own
catalog-reported trained context.

**Context-ownership precedence (round 5b, corrected)**: `num_ctx` is the
SMALLEST of every candidate that is actually known -- the hard cap
(131072) always included -- where the "fit" candidate is the learned
calibration cap when one exists, else the live fit estimate; `host.
max_ctx`, when configured, is an ordinary candidate in that same
minimum, not a separate override tier. The one MUST-FIX from a live
LAN-host run: a REMOTE host (no `ssh:` configured, nothing loaded yet)
with no `host.max_ctx` and no fit signal at all now gets a conservative
32768 default instead of silently falling through to the 131072 hard
cap -- `ollama.hosts[].max_ctx` is the documented explicit override for
this exact case. A LOCAL host never hits this path (it always has an OS
GPU probe to fall back on).

**Multi-GPU**: when the local probe (or an `ssh:` read) sees more than
one card, the fit estimate SUMS every card's free memory -- never the
minimum, which would waste every card past the smallest -- minus a
per-card overhead (`OLLAMA_GPU_OVERHEAD`'s own documented meaning,
"set aside VRAM per GPU," taken out once PER CARD). This matches Ollama's
own documented spread rule: a model that fits on one card loads there;
one that doesn't is spread across every configured card, never a partial
subset.

**Optional ssh GPU read for a remote host**: `ollama.hosts[].ssh:
"user@host"` runs the exact same vendor probes (NVIDIA/AMD/Apple) over
`ssh -o BatchMode=yes -o ConnectTimeout=3 user@host '<command>'` instead
of a local subprocess -- read-only, never prompted for a password (
`BatchMode` refuses outright rather than hanging). Entirely optional:
calibration already works without it (it needs no GPU read at all, only
Ollama's own `/api/ps`). Not covered: the sysfs AMD branch (it reads a
local file path directly, with no ssh-shaped equivalent) -- a documented
gap, not a silent one.

**KV cache type per host**: `ollama.hosts[].kv_cache_type` ("f16"
default, "q8_0", or "q4_0" -- Ollama's own `OLLAMA_KV_CACHE_TYPE` values)
is a HINT the operator sets to match their server's own flag; Ollama
exposes no per-model readback of what's actually running, so this is
never auto-detected. The exact bytes/element the fit arithmetic now uses,
hand-summed from llama.cpp's own `ggml-common.h` block structs: f16/bf16
2.0 (unchanged), q8_0 1.0625 (was approximated as 1.0, 6% too low), q4_0
0.5625 (was approximated as 0.5, 12% too low) -- both old approximations
under-counted memory, the wrong direction for a budget estimate.

**Apple Silicon memory model**: unified memory, no discrete VRAM to
query. The local probe reads total RAM (`sysctl hw.memsize`) and, when
set, `sysctl iogpu.wired_limit_mb` (confirmed from mlx-lm's own README --
"wires the memory occupied by the model and cache," `sudo sysctl
iogpu.wired_limit_mb=<N>` is the documented way to raise it); with the
limit unset, the GPU-usable share is estimated as roughly two-thirds to
three-quarters of total RAM (the exact fraction is UNCONFIRMED against
any Apple/mlx-lm primary source -- a wizard/panel FIRST GUESS only,
always labelled an estimate). `halo ollama calibrate` is the ground
truth on a Mac exactly as it is everywhere else; the fraction never
overrides an actual calibration result.

**Stable request prefix**: Ollama/llama.cpp reuse the cached prompt
prefix when a request is byte-identical up to the first change, so the
system message, the tools array, and `options` must never carry per-turn
volatile content (a live timestamp, a context percentage, a counter) on
an `ol:` session. Pinned directly (`tests/test_ollama_precedence_5b.py`):
building two consecutive request bodies for the same session produces a
byte-identical leading system message, tools array, and `options`, even
when the caller's own tool list happens to arrive in a different order
each time (`convert_tools`'s own alphabetical sort absorbs that). What
remains volatile BY DESIGN, never claimed stable: the full messages list
legitimately GROWS turn to turn (each new turn's own content is new, and
context pruning may reshape an OLDER tool result's text once the
transcript grows past a threshold -- a deliberate context-management
trade-off, not a bug); and `num_ctx` may change exactly ONCE, from
before a brand-new model's first load to after it, on a host with no
learned cap yet (round 3's own fix already makes it stable from the
SECOND turn onward, since a loaded model's context is then read back
from `/api/ps` as ground truth rather than recomputed).

**Throughput in the UI**: for an `ol:` turn, the status bar's model chip
shows a compact segment next to the model name -- `"41 tok/s · prefill
1.2 s"` -- computed from the final NDJSON line's `eval_count`/`eval_
duration`/`prompt_eval_duration` (Ollama's own documented nanosecond
timing fields), plus `"· offloaded"` when the last `/api/ps` read (a side
effect of the SAME read the fit estimate already makes, never a second
probe) found the model partially in system RAM. `halo ollama` prints the
last turn's own numbers per host/model, persisted in the same `~/.halo/
ollama-fit.json` file as the learned caps.

**Reliable tool calls from small models (round 5b part 2, corrected by a
fix pass the same day)**: local models fail mostly by emitting malformed
or half-formed tool calls, not by choosing the wrong tool. **Constrained
decoding is used ONLY to repair a malformed call, never to force one** --
an ordinary turn, including one right after a tool result, is ALWAYS
decoded free: the model must always be able to answer in prose. The first
version of this round instead sent the tool-call schema as an output
CONSTRAINT on every turn it judged "expected to call a tool" (right after
a tool result) -- a real run on a local box showed why that is wrong: once
the model had nothing useful left to call, it could no longer just say so
in prose, so it emitted one meaningless call (`TaskStop` on a task that
didn't exist) every time, Halo dispatched it, and the NEXT turn was ALSO
"right after a tool result" and ALSO constrained -- the session looped for
25 minutes (175 requests) until killed by hand. That gate (`providers.
tool_call_schema.expected_to_call_tool`) is deleted outright. Three
measures remain, all behind the `ollama` dialect (local hosts only -- never
Ollama Cloud, which the research confirms rejects structured output) and
`hf:local/*` servers, every other dialect completely untouched:

1. **One local repair round, constrained.** A tool call that fails to
   parse (bad JSON arguments, or a resolved tool whose arguments fail
   schema validation -- covering "missing required argument") gets ONE
   isolated, tools-less, history-less completion: Halo sends the tool's
   own exact `input_schema` as the output constraint (`format` on `ol:`,
   the OpenAI `response_format` json_schema shape on `hf:local/*`) plus
   the parse/validation error, and uses whatever comes back (re-validated
   against that same schema) as the repaired call -- before anything
   reaches the user. A second failure (or the repair reply itself not
   validating) surfaces the ordinary plain error, exactly as it always
   has. This is the ONLY place either dialect ever puts a structured-
   output constraint on the wire now -- a real tool call was already
   ATTEMPTED and failed, so constraining the fix to that one tool's own
   schema is correct; constraining an attempt that hasn't happened yet is
   what caused the live-run loop. An UNRESOLVED tool name is never offered
   this repair round at all -- there is no single schema to constrain
   against until a name is known, and the existing alias-table/difflib
   name resolution already recovers the overwhelming majority of near-miss
   names locally, with no network round trip either.
2. **Leak-parser promotion stays on regardless.** The generic bare-JSON/
   fenced-JSON leak extractors (`tool_leak_patterns=("python_repr_args",
   "json_text_call")`) are enabled for the `ollama`/`huggingface` profiles
   -- harmless now that nothing forces a model into that shape: if a model
   spills a `{"name": ..., "arguments": {...}}`-shaped attempt into plain
   text on its OWN initiative (unprompted), it still gets promoted to a
   real tool_use via the existing, dialect-agnostic leak_parser call.
3. **A stricter identical-call loop guard.** Independent of the generic,
   all-dialect loop breaker (5 denies, 8 ends the turn, counted per turn
   but NOT required to be consecutive), `ollama`/`huggingface` sessions
   also track an unbroken run of the IDENTICAL (tool, arguments) pair --
   three in a row stops the turn immediately with one plain sentence
   ("... the model repeated the same tool call three times in a row --
   stopping this turn so you can steer it"), reset every turn, with the
   same `BashOutput`-polling exemption the generic breaker already has.
   This is the backstop against a model stuck repeating one call for ANY
   reason, not just the specific constraint-driven loop above.

**VRAM-aware role defaults (round 5b part 2)**: when the session's main
model is `ol:` on a host where a SECOND model's own on-disk weight size
plus the main model's CURRENTLY RESIDENT size (`/api/ps`'s `size_vram` --
a measured fact, main must actually be loaded) would exceed that host's
total GPU memory, the `small`/`researcher`/`judge`/`subagent_default`
roles' TABLE value (never an explicit `--role`/`/roles set` override for
this run -- that is never second-guessed) is redirected to the main model
itself instead of evicting it -- `providers.ollama_hw.fits_beside_main`
is the measurement, `roles.vram_aware_override` the redirection; unknown
(no GPU read, main not loaded, candidate not in the catalog, different
hosts) always means "leave it alone," never a guess. `/local <question>`
applies the SAME redirection at call time to whatever `roles.small`
resolved to. `halo roles`/`/roles` and the model picker's `u` action show
`(same as main: fits beside it: no)` as the reason when this fired.

**The model gym (`halo gym`, round 5d; `hf:local/*`/`hf:mlx/*` added round
5i)**: a fixed task battery run against each local model on THIS
machine's own hardware, through the real request/decode path (never a
second, parallel wire format) -- `halo gym [--models ol:a,hf:local/b,...]
[--roles small,judge,...] [--quick]`. `ol:`, `hf:local/*`, and `hf:mlx/*`
all count as "local"; every OTHER ref (the router, a dedicated endpoint,
any cloud provider) may still be NAMED explicitly for comparison but is
never run by default. `gym_send.send_turn_for` picks the sender -- the
native Ollama path (round 5d) or `providers.huggingface_send.send_hf_turn`
(round 5f's shared openai-chat sender, reused as-is here too) -- so the
same four measurements, each a plain ratio (never a vendor claim), apply
either way:
  - **tool-call accuracy**: out of N real attempts to call a Read tool, how
    many came back schema-valid on the FIRST try. A malformed/missing call
    gets exactly ONE local repair round (the SAME constrained-decoding/
    free-decode choice `supports_constrained_tool_calls` makes for the
    repair loop below) before counting as failed -- repaired calls and
    repair rounds are reported SEPARATELY, never blended into the headline
    number, so a model that leans on the repair loop a lot never looks as
    good as one that doesn't.
  - **edit success**: out of N attempts, how many produced a real Edit
    call that -- actually applied to a scratch fixture file -- left it
    reading exactly as expected.
  - **context recall**: out of N attempts, how many answers contained a
    short needle fact planted at about 12% depth of a prompt sized to this
    model's own FITTED context -- `providers.ollama_hw.resolve_context_
    decision`'s own `num_ctx` for an `ol:` ref, or (round 5i) `model.
    resolve_model_profile`'s `context_tokens` for an `hf:` one (the local
    server's own reported context, else the bare 128000 profile default)
    -- this tests the context size Halo actually grants the model on this
    host, not the advertised trained one.
  - **instruction adherence**: out of N attempts at the plain-sentence
    reply rules (one word when asked for one word, no preamble when asked
    for none), how many were followed exactly.
  - **tokens/second and prefill seconds**, averaged across every real
    battery turn, from the identical `eval_count`/`eval_duration`/
    `prompt_eval_duration` fields the status bar already reads -- for an
    `ol:` model. The shared openai-chat sender exposes no server-side
    usage/per-phase timing to a caller outside it, so an `hf:` card's
    `tokens_per_second` is a wall-clock estimate instead (reply length
    over call duration, never the server's own figure) and its
    `prefill_seconds` is honestly `None` rather than a guessed number.
`--quick` halves N (and context recall's own smaller base count) for a
faster, noisier read. `--show-replies` prints each reply-only task's
actual reply excerpt (first 200 characters) alongside its score; the same
excerpts are ALWAYS saved in the result JSON under each metric's own
`samples` key, so a 0% is diagnosable from the file alone even without the
flag. Fix pass (2026-10-04 live run, a thinking-by-default model):
context recall and instruction adherence used to request only 16-32
output tokens -- too small for a model that spends its OWN tokens
reasoning before emitting the real answer, so the reply came back
genuinely empty (never thinking text mistaken for the answer -- the
decoder never turns `message.thinking` into checked text in the first
place) and scored a flat, unexplained 0%. Both now request a flat,
generous 160-token budget; the needle check is also now case-insensitive
(already tolerant of surrounding punctuation/quotes as a plain substring
check). Results persist at `~/.halo/gym/<host-slug>/<id>.json` with the
model name, a stable id, fitted context, and timestamps measured AT THAT
TIME -- `halo gym show [model]` prints a per-model card from the saved
file, never a fresh probe. For an `ol:` ref, `<host-slug>` is the Ollama
host's own configured name and `<id>` is the model's real digest
(quantization and the Ollama version are recorded too). For `hf:local/*`/
`hf:mlx/*` (round 5i), every result groups under the single shared
`huggingface` host-slug instead (there is no multi-model "host" concept
for an arbitrary local server the way an Ollama daemon is one), keyed by
a stable id of its own -- the bare Hugging Face Hub repo id for `hf:mlx/
*` (`ensure_mlx_server` already names its managed server by that exact
id), or the resolved server's own configured/auto-detected name for a
generic `hf:local/*` ref (no per-model digest concept exists for an
arbitrary OpenAI-compatible server, so the SERVER is the measured unit
there); neither quantization nor an engine version string is recorded
for this branch. A cloud/router/endpoint ref may be named explicitly for
comparison; the battery never runs against one by default (cost).

**From scores to a role table (`halo gym propose`)**: turns saved gym
scores into a role-table proposal in the EXISTING roles v2 shape (nothing
new) -- the best LOCAL (`ol:`/`hf:local/*`/`hf:mlx/*`) model per supporting
role (`small`/`researcher`/`judge`/`subagent_default`), each role weighting
the four raw ratios (plus normalized tok/s) by what that role's own job
leans on most (documented in `gym_propose.py`'s own `ROLE_WEIGHTS`), with
the round 5b VRAM-aware rule (`roles.vram_aware_override`) applied to the
winner exactly as a live session would -- that rule already no-ops on its
own whenever either the winner or `main` isn't an `ollama` ref (its own
existing guard clauses), so an `hf:` winner, or `--main` pointed at an
`hf:`/cloud ref, simply skips it with no separate branch needed. `main` is
never touched -- "left as configured" means
propose never writes an `orchestrator` entry at all; `--main REF` only
supplies the reference point the VRAM rule compares candidates against,
same reason. One plain sentence per role names the composite score and the
measurements behind it, or says plainly that no local model has a usable
score for that role yet. `--apply` saves the proposal as an ordinary role
template through the EXISTING `halo roles template import` path
(`~/.halo/roles/gym-proposed.json` by default) -- `halo roles template
load gym-proposed` (or the picker) is the separate, explicit step that
makes it the live table. The `/model` picker shows a model's gym score
(and tok/s) beside it, read straight from the saved file, whenever one
exists.

**The 60-second acceptance check (`halo doctor --local [--model ol:x]`)**:
the "works out of the box" proof per machine, and the first thing the docs
below point a new local-model user at. Four steps, always run in this
order and each printed as `[PASS]`/`[FAIL]` with a plain reason and the
elapsed time, even when an earlier one failed: load the model (one plain
turn), one real tool call (Read on a scratch file, dispatched and
diffed), one structured output (the SAME constrained-decoding path the
repair loop uses), and one summary of a small fixture transcript (a
compaction-shaped call, never the full `agent/loop.py` compaction
machinery itself -- that has its own dedicated tests). Exit 0 iff all four
passed.

## Hugging Face

Halo 2.0.3 round 4 (`plans/2.0.3-ollama-round2-brief.md`, design doc
`docs/harness/LOCAL-MODELS-RESEARCH.md` section 9). Two fully separate
products, both OpenAI-compatible, both reusing the SAME openai-chat
request/response code every OpenRouter/Databricks-chat call already goes
through (no second wire format) -- the brief's own framing: "this should
be mostly config/routing, not a new wire format."

**Inference Providers (the router)**: `hf:<org>/<model>` (optionally
`:fastest`/`:cheapest`/`:preferred`/`:<provider-name>`, e.g.
`hf:openai/gpt-oss-120b:groq` -- passed through VERBATIM in the wire
`model` field, never parsed or validated by Halo) against
`https://router.huggingface.co/v1`, `Authorization: Bearer $HF_TOKEN`.
`HF_TOKEN` is the ONLY env var name read for this -- the research doc
confirmed only this exact name ("Use a single Hugging Face token for all
providers"); `HUGGING_FACE_HUB_TOKEN`/`HF_API_TOKEN` are NOT accepted as
fallbacks since neither was confirmed as an alias for the router
specifically. `BRIDGE_HF_ROUTER_BASE_URL` (or the `HALO_`/`ROLO_CLAUDE_`
twin, `config/paths.py::env_compat`) overrides the base URL for tests,
exactly like `BRIDGE_OPENROUTER_BASE_URL`. A Team/Enterprise
`huggingface.bill_to` config value (an org name) adds `X-HF-Bill-To` on
every ROUTER request only -- never on a dedicated endpoint call, which has
its own compute-time billing with no such header.

**Dedicated Inference Endpoints**: a fundamentally different product (the
research doc's own framing) -- the user provisions one specific model onto
its own compute and gets back ITS OWN url and token, billed by compute
time, never per-token. `hf:endpoint/<name>` looks `<name>` up in
`huggingface.endpoints` (`~/.halo/config.json`, mirroring `ollama.hosts`'
own shape: a list of `{name, url, token, default}`) and uses that entry's
`url`/`token` AS-IS -- never the router's base URL, never `HF_TOKEN`. An
entry with no `token` sends no `Authorization` header at all (the same
"unauthenticated unless configured otherwise" default `ollama.hosts`
uses); a name that isn't configured gives a plain message naming
`huggingface.endpoints`, never a silent fall-back to the router. The exact
per-endpoint URL *pattern* was UNCONFIRMED this round (no fetched page
gave a concrete example) -- irrelevant to Halo either way, since the user
supplies the full URL directly.

**Dialect**: `openai-chat`, same as OpenRouter -- tools supported,
reasoning passthrough the same way OpenRouter's own `reasoning.effort`
field works, no OpenRouter-only fields (`provider`/`reasoning`/`models`/
`plugins`/`usage`) ever sent to this host. `resolve_model_profile` for an
`hf:` ref: the router's own cached catalog (below) when the bare
`<org>/<model>` id -- suffix stripped -- is known, else the bare
`ModelProfile` dataclass default; a dedicated endpoint ref always gets the
dataclass default (it was never listed by the router's catalog at all).

**Catalog**: `GET /v1/models` on the router, cached in `~/.halo/
huggingface-models.json` (a SEPARATE file from OpenRouter's own
`models.json` -- a colliding bare id between the two routers would
otherwise cross-contaminate one flat file) with the same TTL knob every
other network catalog here shares (`databricks.catalog_max_age_hours`,
default 24h). Refreshed the same way OpenRouter's own catalog is: a
background, staleness-gated worker on `/model` open and at app launch
(never on the UI thread, never when the token is absent -- no network at
all for the picker's first paint), plus an explicit force-refresh. Router
models appear in the `/model` picker under a "Hugging Face" group once the
provider is enabled. The router's exact per-entry `GET /v1/models` field
names were NOT confirmed this round (no fetched page gave a concrete
response body) -- the parser ASSUMES the same shape `probe_openrouter_
models` already parses (OpenRouter's own `/models`: top-level
`context_length`/`pricing.{prompt,completion}`), the closest confirmed
precedent, and degrades to omitted fields rather than raising if a real
response differs. Treat context/pricing from this catalog as best-effort
until verified live.

`/providers`, `halo providers`, and `doctor`'s provider count already show
Hugging Face (unlike Ollama, which stays out of that generic table -- see
above); round 5's three local-server additions below don't change that.

**`hf:local/*` -- a local OpenAI-compatible server** (round 5, research
doc section 8/10): `llama-server`, vLLM, `transformers serve`, LM Studio
or TGI, running ANYWHERE Halo can reach -- `hf:local/<model>` (the default
server) or `hf:local/<model>@<name>` (a NAMED entry in `huggingface.
local_servers`, a list of `{name, url, api_key, default}` mirroring
`ollama.hosts`/`huggingface.endpoints`'s own shape). A manual entry's
`api_key`, when set, goes out as `Authorization: Bearer` on every call to
THAT entry -- pinned apart from `HF_TOKEN` and an `huggingface.endpoints`
token, never a fallback for either. With no manual entry configured (or
none marked `default`), the default server is the FIRST auto-detected one
(below); a NAMED ref that doesn't match a manual entry fails plainly,
never silently falling back to auto-detection. Dialect: `openai-chat`,
identical to the router/an endpoint -- no new wire format.

**Auto-detection**: a background probe of `GET /v1/models` on 127.0.0.1
ONLY, on the documented default ports (research doc section 8): **8080**
(llama-server, TGI), **8000** (vLLM AND `transformers serve` -- both
plain OpenAI-compatible, deliberately treated identically rather than
guessing which one answered), **1234** (LM Studio). Jan's own default
port was never confirmed by the research doc and is NOT guessed -- add it
as a manual entry instead. Overridable for tests (or an unusual setup)
by `huggingface.local_probe_ports` (a config.json int list) or
`HF_LOCAL_PROBE_PORTS` (a comma-separated env var, wins when both are
set); honours `BRIDGE_TEST_NO_BACKGROUND_NET` like every other background
probe in this codebase. A manual entry is never probed in the background
-- only on demand, via `/local refresh` -- using this SAME `GET /v1/
models` call with its own `api_key` as the bearer. Most of these servers
fix their context length at launch time rather than per request (research
doc section 8/10), so Halo reads back whatever `/v1/models` reports (`max_
model_len` for vLLM; other field names are tried heuristically, since no
runtime's exact name beyond vLLM's was confirmed this round) or, as a
secondary best-effort read for any model that reported none, llama-
server's own `/props` endpoint (also unconfirmed at the JSON-shape level
-- degrades to "unknown" rather than guessing). Unknown context falls back
to the plain `ModelProfile` default, same as an unlisted router model.

**mlx_lm.server (round 5b part 2, Apple Silicon)**: Apple's MLX runtime
answers the SAME OpenAI-compatible shape on the SAME default port family
as llama-server (8080) -- the research doc never confirmed `mlx_lm.
server`'s own exact flags, so Halo tells the two apart by what `/props`
answers instead of by port: a server that answers `/props` is labelled
`llama-server` (a confirmed, llama.cpp-specific native endpoint); one that
does NOT, on Apple Silicon (`sys.platform == "darwin"`) specifically, is
labelled `mlx` (a heuristic -- the absence of one tool's own marker, not
a positive MLX signal -- UNCONFIRMED, round 6's live Mac check should
verify or correct it); on any other platform a missing `/props` stays
genuinely unlabelled rather than guessing. `/local` shows the label in
the server's own group name.

**The Hugging Face Hub cache**: `/local` (below) also walks `$HF_HUB_
CACHE`, else `$HF_HOME/hub`, else `~/.cache/huggingface/hub` for `models--
<org>--<name>` directories -- models present on disk (from `hf download`)
but not necessarily being served by anything right now. Reports the
repo id, real on-disk size (summed from `blobs/`, never double-counting
the `snapshots/` symlinks that point back to them), and the format(s)
present (`safetensors`/`gguf`, read from the snapshot symlinks' own
filenames -- a blob's name is a bare content hash). Never follows a
top-level symlink that resolves outside the cache root. A repo under the
`mlx-community` org is labelled "runnable through MLX" in its capability
column -- the Hub org MLX-quantized repos are actually published under.

**LM Studio's own model folder (round 5b part 2)**: `~/.lmstudio/models`
(the documented default; `huggingface.lmstudio_models_dir` overrides it
for a box where LM Studio's own in-app "Model Storage" setting moved it --
that setting's own persistence was not independently confirmed this
round) joins the Hub-cache scan as a second on-disk source, under its own
`LM Studio (cache, not served)` group in `/local`. LM Studio mirrors the
Hub's `<publisher>/<model>/` folder nesting directly (never the Hub
cache's content-addressed `models--org--name`/`blobs`/`snapshots`
structure) -- a bare `.gguf` file in a model folder, or a transformers-
style folder (`config.json` beside `*.safetensors`), each become one row.

**The shared `/local` view** (round 5): `/local` with no arguments (TUI)
or `halo local [--refresh]` (CLI) merge THREE sources into one list, in
this order, with a group label per source/host: each configured Ollama
host's catalog (round 3's analysis, loaded-now and fit included), running
Hugging Face local servers (auto-detected, always; manual entries too,
but only probed for real on `/local refresh` -- a bare open shows them as
"configured, not probed yet"), and the Hugging Face Hub cache. Columns:
size, quant (Ollama only -- not reported by `/v1/models` or derivable from
a cached GGUF without opening it), context, a capability badge (`tools:
probed yes/no` once round 2's own real-inference capability probe has
run for that Ollama model digest, else `tools: declared yes/no` from the
catalog's own claim -- a local HF server's `/v1/models` response declares
no such field at all, so that badge reads `declared: unknown` there), and
"loaded now" where the concept applies (Ollama only). `/local <question>`
answers from `roles.small` (now `ol:` OR `hf:`, see "Ollama" above);
`/local refresh` re-probes everything a bare open doesn't.

**Init tab**: `halo init`'s interactive Providers step gains an `Ollama
(local or LAN)` tab (detects/registers the local daemon, or add a LAN/
cloud host with an optional key, writing `ollama.hosts`) and a `Hugging
Face` tab (paste `HF_TOKEN`, add a dedicated endpoint, add a local server
by URL with an optional key, or rely on auto-detection -- any subset, all
optional, both tabs skippable) -- see `docs/CONFIG.md`'s "Providers"
section for why neither is a `halo init --provider` CLI-flag choice.

**The wizard detects before it asks (round 5b part 2)**: both tabs open
with a short detection summary, filled in by a background worker (never
the UI thread -- every field/button renders immediately regardless of
how long the probe takes; a slow probe just means the summary line stays
"detecting..." a little longer, never a stuck wizard): GPU or unified
memory (labelled an estimate where it is one), whether Ollama is
reachable and what it already has pulled, any running local server, and
model folders already known. For each already-installed Ollama model it
names the largest fully-resident context (the learned cap when one
exists, else the live fit estimate, labelled either way); for whatever
free room is left, one or two illustrative model classes and quantizations
that would likely fit with a 32k context -- derived from the same exact
per-parameter byte table the KV-cache arithmetic uses for weights, plus a
flat ~2 GiB reservation for the context itself (not that class's own real
KV formula, which needs an architecture no not-yet-installed model has)
-- explicitly an estimate, never a download from the wizard itself.

**Finding and using file-backed models (round 5c)**

A model you already have as files on disk -- downloaded by hand, by `hf
download`, or by LM Studio -- does nothing by itself until something
loads it. This is Halo's answer to "I have model files somewhere, how do
I actually talk to them."

**Where Halo looks**: the Hugging Face Hub cache and LM Studio's folder
(both above) plus `huggingface.model_dirs`, a list of your OWN folders
(docs/CONFIG.md) Halo scans recursively (a symlinked subfolder is never
followed, matching every other scanner in this codebase) for three
shapes: a bare `.gguf` file anywhere, a folder holding `config.json`
beside one or more `*.safetensors` files (an ordinary Hugging Face
"transformers" model folder), and the same safetensors shape again when
it looks like an MLX-quantized model (its folder name mentions "mlx", or
its `config.json` carries mlx-lm's own `quantization` key -- a best-
effort guess, not a confirmed marker). Manage the list from a running
session with `/local add <path>`/`/local forget <path>` (persisted to
`huggingface.model_dirs` right away), or from `halo init`'s own "Local
models" step, which also shows the same detection summary the Ollama/
Hugging Face tabs show (round 5b part 2, above) and lets you pick a
preferred runtime (below) before you've served anything at all.

**`halo local`** (the merged view above) lists every file it finds this
way with its format, its on-disk size, and -- read directly from the
file's own header, never by loading it -- its trained context length and
quantization where the file says so: a `.gguf` file's own metadata block
(magic, version, then key-value pairs that already include the context
length, layer count, attention head counts and the quantization name) or
a safetensors folder's `config.json` (`max_position_embeddings`,
`num_hidden_layers`, `num_key_value_heads`, `hidden_size`,
`num_attention_heads`). The same arithmetic the Ollama panel uses
(`/ollama`, above) turns those numbers into a fitted context size for
this machine's free GPU/unified memory -- nothing new, no model ever
opened to learn any of this. The capability column also says whether
anything on this machine can actually run the file right now, and names
the exact command.

**Option A -- serve it.** `halo local serve <path-or-name> [--runtime
llama-server|mlx_lm] [--port N] [--keep]` (or, in the TUI, the `s` key on
`/local`'s own view) starts a small background program on your own
machine, listening on a free port on `127.0.0.1` only, that answers the
same OpenAI-compatible shape every other local server in this doc does --
`llama-server` for a `.gguf` file, `mlx_lm` for a safetensors/MLX folder
on an Apple Silicon Mac (nothing else serves that shape today; option B
is the answer for it elsewhere). Halo picks the context size itself, from
the fit arithmetic above, and once it's running you can use it for the
rest of the session as `hf:local/<name>` -- no config edit needed. It
keeps running only as long as the Halo process that started it does,
unless you pass `--keep`; `halo local stop <name>` stops it by hand
either way.

If neither runtime is anywhere Halo can find it (on your `PATH`, or
already fetched into `~/.halo/runtimes/`), `llama-server` is the one Halo
offers to fetch for you: it names the file(s), their size, and where they
will go, and does nothing until you say yes (`--yes`, or type it when
asked). A live run found llama.cpp's actual releases page a little
different from the first-pass assumption: `GET /releases/latest` points
at the project's own most recent NON-binary release, not a compiled
build, so Halo instead lists recent releases and walks them newest-first
looking for one tagged `b<number>` that actually carries what this OS/GPU
needs. For CUDA specifically: it reads your NVIDIA driver's own reported
CUDA version and picks the newest build with the SAME major version whose
minor doesn't exceed the driver's (a driver reporting CUDA 13.2 skips a
13.4 build -- its minor is too new -- and falls back to the newest
available 12.x build instead); with no matching major AND no 12.x build
either, or with no NVIDIA driver at all, it falls back to a cross-vendor
Vulkan build; Metal is always the pick on a Mac. `--backend cuda|vulkan|
cpu|metal` overrides this choice outright. A CUDA pick also downloads the
paired `cudart-*` redistributable runtime UNLESS a CUDA toolkit is already
on your machine (detected by a `cudart64_*.dll` on `PATH` on Windows, or
`libcudart.so*` on Linux) -- the consent sentence names both downloads,
their combined size, and mentions the smaller, slower `--backend vulkan`
build as an alternative. Every downloaded file is checked against the
exact byte-for-byte digest GitHub's own API reports for it (llama.cpp
doesn't publish a checksum file of its own) before being unpacked into
`~/.halo/runtimes/<tag>/` -- never onto your `PATH`. Say no and Halo just
tells you the one-line install command for your OS instead (Homebrew,
winget, or the release page) and stops there. `halo local runtime remove
[VERSION]` deletes a fetched copy. `mlx_lm` is never fetched this way --
it's a small Python package (`pip install mlx-lm`); Halo tells you that
one line too, if it's missing.

**Option B -- import it into Ollama.** If you already run Ollama, `halo
local import <path-or-name> [--name NAME]` is the other way to use a
`.gguf` file: Halo tells you the file's size and that it's about to be
copied into Ollama's own model store, and once you say yes, it does
exactly that -- the result is an ordinary `ol:<name>`, with every one of
this doc's own Ollama rules (context sizing, roles, calibration) applying
to it automatically. Under the hood (a live run corrected the first-pass
assumption here too: Ollama's OLDER "write a Modelfile, `FROM <path>`"
approach now answers `HTTP 400`, "neither 'from' or 'files' was
specified"): Halo computes the file's own sha256, asks Ollama whether it
already has a blob by that digest, uploads the raw bytes only if it
doesn't (streamed from disk, never held fully in memory -- these files
are commonly hundreds of MB to tens of GB, with a plain progress line
while it happens), and then creates the model by naming that blob. This
round only imports `.gguf` files: Ollama's own documentation of which
other model shapes (safetensors folders) it can import directly wasn't
found this round, so Halo doesn't guess -- option A is the answer for
those.

**Roles**: either way, a model that becomes usable this way defaults to
the `small` role, the same as any other local model -- never picked as
the session's main model without you asking for it (see "Roles" above/
docs/ROLES.md).

**Apple Silicon (`hf:mlx/*`, round 5f, experimental)**

`hf:mlx/<org>/<repo>` names a Hugging Face Hub repo directly (for
example `hf:mlx/mlx-community/Qwen2.5-7B-Instruct-4bit`) rather than a
file already on disk -- the difference from `hf:local/*`/option A above:
nothing needs to be downloaded or discovered first. The FIRST use starts
a Halo-managed `mlx_lm.server` on a free loopback port with `--model
<repo>` (mlx-lm itself downloads the weights into the Hugging Face Hub
cache, or reuses them if already there, the first time it runs -- Halo
never downloads anything itself here); a plain notice names the repo,
its approximate size when the Hub cache already knows it, and the cache
destination. Every later use of the SAME repo id reuses that already-
running server -- no second download, no second notice. Recorded in the
exact same `~/.halo/run/local-servers.json` registry option A's managed
servers use, so `halo local stop <repo>` stops it, and it is stopped when
the `halo` process that started it exits unless kept.

Off by default -- `mlx-lm` is an optional extra, never installed unless
you ask for it:

```sh
uv tool install "halo-harness[mlx]"
```

(`pyproject.toml`'s own environment marker restricts this to macOS on
arm64 -- `uv`/`pip` never even attempt it on any other platform.) `halo
doctor` names whether the extra is present on Apple Silicon; on every
other platform, resolving an `hf:mlx/*` ref gives exactly one plain
sentence, **"MLX runs on Apple Silicon only."**, and nothing else about
the install, the doctor output, or any other route changes.

Because the ref rides the SAME `hf:local/*` tiers everywhere else in this
codebase (`ref.local=True` alongside the new `ref.mlx=True`), everything
else about using it is identical to any other local model: tools, the
repair loop and constrained decoding (behind the `huggingface` profile,
same as `hf:local/*`), the `small`-by-default role, the fit arithmetic
(the Apple unified-memory share above, calibrated against whatever
context `mlx_lm.server` itself reports, or the repo's own cached
`config.json` when it doesn't), tokens/second on the status bar, `halo
doctor --local --model hf:mlx/<repo>` (the 60-second acceptance check),
and `halo gym --models hf:mlx/<repo>,ol:<same model>` (a side-by-side
card, including both engines' version strings, so a stale comparison is
visibly stale). `halo local` lists an `mlx-community/*` repo already in
your Hub cache with this exact ref and a `halo local serve <repo-or-
folder> --runtime mlx_lm` hint -- the explicit, non-ref form of the same
thing.

`mlx_lm.server`'s own flags beyond `--model`/`--port` (a host default, a
context-length-at-launch flag) were not confirmed from its README this
round (`docs/harness/GPU-RESEARCH.md` section 7) -- see
[MAC.md](MAC.md), the live Mac quick-start that resolves this and the
Apple memory-share fraction in practice.

## OpenAI API

Halo 2.0.3 round 5i part 1 (`plans/2.0.3-ollama-round2-brief.md` "Round
5i", design doc `docs/harness/OPENAI-RESEARCH.md`). `oai:<model>` against
the real `https://api.openai.com/v1`, `Authorization: Bearer
$OPENAI_API_KEY` -- separate from a Codex subscription login (`cx:`,
5i part 2), and separate from a Databricks-hosted `gpt-*` foundation-model
endpoint (`dbx:databricks-gpt-...`), which is a different host entirely.
`BRIDGE_OPENAI_BASE_URL` (or the `HALO_`/`ROLO_CLAUDE_` twin,
`config/paths.py::env_compat`) overrides the base URL for tests, exactly
like every other provider's own base-URL knob. **No OpenAI key exists on
the build host as of this round -- every behaviour below is verified
against the parameterized fake (`tests/helpers/mock_openai.py`) only,
never against the real API; treat it as unverified live until a key is
available and the orchestrator runs a real check.**

**Two dialects.** Chat completions (`openai-chat`, the default) goes
through the SAME request/stream/profile code every other openai-chat-
dialect provider already uses -- tools supported, reasoning effort
passthrough, no OpenRouter-only fields. The Responses dialect
(`openai-responses`, `POST /v1/responses`) is a full second wire shape:
`instructions` for the system prompt, `input` items (message items;
`function_call`/`function_call_output` items for tool use, replacing
chat completions' `tool_calls`/`tool` messages) instead of `messages`,
`tools` as flat function items (never nested under a `"function"` key),
`reasoning: {effort}` from the session's effort, `store: false` and
NEVER `previous_response_id` on every request -- Halo always keeps
owning the transcript itself, the same way every other dialect here
replays its own history rather than leaning on server-side state. A
reply's reasoning item is captured for DISPLAY only (like the `ollama`
dialect's own `reasoning_replay="empty"`) -- never replayed on the wire;
replaying it would need `store` on plus `previous_response_id`, or the
encrypted-reasoning-content alternative, neither of which this round
implements (`docs/harness/OPENAI-RESEARCH.md`'s own documented scope
cut). Streamed `response.*` SSE events decode into the same Anthropic-
shaped events every other dialect's decoder produces
(`providers/responses_stream.py`).

**Dialect selection.** Exactly the two bare ids the Responses API
reference names as REQUIRING this dialect for function calling --
`gpt-6-astra`, `gpt-6.1-sol` -- default to `openai-responses`; every
other `oai:` model defaults to `openai-chat`. Similarly-named ids the
same page does NOT name (`gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`, ...)
are deliberately left on chat completions by default -- a substring match
would have wrongly swept them in. `openai.dialect_overrides`
(`~/.halo/config.json`, `{"<bare id>": "chat"|"responses"}`) always wins
over the table, in either direction, since the guide only ever documents
a requirement, never a complete negative list.

**Reasoning effort.** `none`, `minimal`, `low`, `medium`, `high`,
`xhigh`, `max` are all real, current, documented values on this dialect
(confirmed live against the Responses API reference) -- a strict
superset of this harness's own `--effort`/`/effort` vocabulary, so
nothing the harness itself can ask for is ever clamped on this route.

**Catalog.** `GET /v1/models` cached in its own `~/.halo/openai-
models.json` with the same TTL knob every other network catalog here
shares (`databricks.catalog_max_age_hours`) -- this endpoint carries no
context length or pricing at all (confirmed live), so it only ever
proves "this key can see this id" for the `/model` picker's OpenAI group
and `halo providers`' model-count column. Price and context come from
the SEPARATE models.dev cross-check instead (`providers/catalog/
models_dev_openai_fallback.json`, all 53 current ids, vendored the same
way the Databricks fallback is; a refreshed `~/.halo/models-dev.json`
cache -- `halo models --refresh` -- wins when fresher).

**Balance.** The OpenAI API has no public balance endpoint for ordinary
keys -- `/providers`/`halo providers` say so plainly and show this
session's computed spend instead (from the catalog's own price, the
same rule part B of the original 2.0.3 brief gives for TypeSafe), falling
back to "see `/cost`" when no live session is attached (the standalone
CLI).

**Setup.** `halo init`'s "OpenAI API (key)" tab (same pattern as the
Hugging Face tab: paste the key, Save) or hand-write `OPENAI_API_KEY` to
the env file; auto-enabled once detected, same rule every other key-only
provider here follows. `halo doctor`'s provider count already includes
it once enabled (the generic `PROVIDER_NAMES` table, no bespoke doctor
line needed).

## Codex subscription (ChatGPT)

Halo 2.0.3 round 5i part 2 (`plans/2.0.3-ollama-round2-brief.md` "Round
5i", design doc `docs/harness/CODEX-RESEARCH.md`). `cx:<model>` drives
the installed `codex` CLI headlessly under the user's own ChatGPT
subscription login -- the Codex counterpart of `cc:`, mirroring its
design wherever Codex's own architecture allows and documenting where it
genuinely differs. **No one is logged into Codex on the build host as of
this round -- every behaviour below is verified against the parameterized
fake (`tests/helpers/fake_codex.py`) only, never against the real CLI;
treat it as unverified live until a ChatGPT login is available and the
orchestrator runs a real check.**

**Detection.** The `codex` binary on PATH (bare, `.exe`, or `.cmd` -- the
same resolution `cc:` uses for `claude`, deliberately never probing
`codex.ps1`) plus `codex login status` reporting a real ChatGPT login.
That command prints PLAIN TEXT, never JSON -- the one string that counts
as a subscription login is `"Logged in using ChatGPT"`; every other
answer (an API key, Bedrock, an access/personal-access token, workload
identity) is logged in but NOT the subscription cx: wants, so it is
refused with a message pointing at `oai:` instead. Halo never reads
`~/.codex/auth.json`.

**Models.** `gpt-6-astra`, `gpt-6.1-sol`, `gpt-6-luna` are the documented
ChatGPT-plan ids (`gpt-5.5` is API-key only, retired from ChatGPT products
2026-10-14) -- short aliases `cx:astra`/`cx:sol`/`cx:luna`, or the full id
directly. Context/output/vision come from the SAME vendored `openai`
models.dev catalog `oai:` reads (the model is identical either way); price
is always `None` on this route -- a ChatGPT subscription has no metered
per-token price, so the cost meter records token usage with no dollar
figure rather than inventing one.

**Execution, one subprocess per turn.** Unlike `cc:` (one `claude`
process held open across the whole session, fed one stdin line per turn),
`codex exec` has no stdin-streaming protocol at all -- each Halo turn
spawns a FRESH `codex exec [resume <thread-id>] --json <prompt>` and runs
it to completion. The thread id is Codex's own (learned from its first
`thread.started` event, logged as a `cx_session_id` meta node, exactly
like `cc_session_id`) -- every turn after the first resumes it, so the
conversation is continuous on Codex's own side even though the OS process
is not. Halo's permission mode maps onto Codex's `approval_policy`/
`sandbox_mode`: bypass and auto -> `never`/`danger-full-access`; default
-> `on-request`/`workspace-write`; manual -> `untrusted`/`read-only`.

**Tools.** Codex keeps its OWN native tools (shell, apply_patch) running
in its own sandbox -- no flag was found to disable them the way `cc:`'s
`--tools ""` disables Claude Code's. Halo's own tool catalog is
ADDITIONALLY exposed through an inline `-c mcp_servers.halo.<field>=
<value>` override pointing at the SAME `python -m halo_harness.ccbridge`
child + bridge server `cc:` already uses -- a genuine MCP tool call
through it is dispatched through Halo's own permission engine/hooks/log
exactly like every other route's tools; a native Codex action is logged
read-only, after the fact, from its own `item.completed` event, never
gated by Halo (there is nothing left to gate -- it already ran). The
bridge's own secret token/socket address are never spelled out on Codex's
command line -- they ride on the subprocess's own environment, forwarded
to the MCP child by NAME via `mcp_servers.halo.env_vars`, the same privacy
property `cc:` gets from folding its bridge env into the claude
subprocess's own env.

**Steering.** `codex exec` has no live channel into an already-running
turn (no queued-stdin-line concept the way `cc:` has). A steer is
accepted immediately and queued; the moment the CURRENT subprocess exits,
it is sent as its own follow-up `codex exec resume <id> <text>` call
(logged as a `steer` node), repeating until nothing is left queued,
before the whole chain reports one `turn_done` back to Halo -- "finish
the turn, then send", literally. (A separate `codex queue --thread <id>
--message <text>` command exists and MIGHT inject into a still-running
turn from a second process -- unconfirmed without a live login, and not
wired in; see `docs/harness/CODEX-RESEARCH.md` section 7.)

**Setup.** `halo init`'s "Codex subscription" tab (same "Check login"
button as the Claude Code tab -- nothing is stored, it only confirms the
login) or run `codex login` directly; auto-enabled only on a genuine
ChatGPT login, never merely because `codex` is installed. `/providers`
and `halo doctor` show it under `codex_subscription`.

## Codex settings and instructions

Round 5i part 2 also reads Codex's OWN `config.toml` and `AGENTS.md`
chain, beside Claude Code's settings/CLAUDE.md, into one merged view --
`/settings`, `halo doctor`'s `codex_settings` line, and the init wizard's
"Settings sources" step all show the SAME thing. Halo never writes to
either Claude Code's or Codex's own files; this is read-only awareness,
not a second configuration mechanism.

**What gets read.** Codex's `config.toml` (`$CODEX_HOME`, default
`~/.codex`, plus `.codex/config.toml` at a trusted project root, layered
over the home one) for `model`/`model_provider`/`model_reasoning_effort`/
`approval_policy`/`sandbox_mode`/`mcp_servers.<name>`/`profiles.<name>`/
`shell_environment_policy`/`notify`/`history`; its `AGENTS.md` chain
(global `AGENTS.override.md` else `AGENTS.md` under `$CODEX_HOME`, then
the SAME override-or-plain rule from the git repo root down to the
working directory, concatenated root-to-leaf, capped at `project_doc_
max_bytes`, 32 KiB by default) -- a DIFFERENT walk than the one Halo
already uses for CLAUDE.md/AGENTS.md (`halo_harness/config/claude_md.py`,
a filesystem-root walk with no git-root concept), kept deliberately
separate since the two rules genuinely differ. `config.toml`'s own
`profiles.<name>` table is read when present; no persisted "default
profile" pointer was found in either the table or the installed CLI's
separate `-p/--profile` per-file scheme, so Halo never guesses which
profile (if any) is active.

**How it merges.** Each setting Halo tracks (model, permission/approval
mode, sandbox, reasoning effort) gets one row showing EVERY source's own
value plus which one is "effective". Non-overlapping entries always
merge: a Codex-only MCP server joins the MCP list labeled `(codex)`;
Codex's AGENTS.md chain is its own block, never silently folded into
Claude Code's own CLAUDE.md block. Overlapping entries follow one
precedence: Halo's own config first, then Claude Code's settings, then
Codex's -- EXCEPT on a live `cx:` session, where Codex's own model/
reasoning-effort/approval-and-sandbox policy leads for those specific
keys (it is, after all, what is actually about to run). `settings.
primary: "claude"|"codex"` (`~/.halo/config.json`, default `"claude"`)
flips which of Claude Code's or Codex's value is preferred when both are
set and Halo's own config and a live cx: session don't already decide
it -- set it from the init wizard's "Settings sources" step, `/settings
primary claude|codex`, or `halo config set settings.primary codex`.

## Families and their rules

`providers/profiles.py::model_family(model_id)` classifies a bare upstream
id into `claude`, `deepseek`, `kimi`, `glm`, `qwen`, `gemini`, `grok`,
`minimax`, `gpt`, or `generic` by substring match. That family (plus the
exact model id, plus the host) looks up a row in
`providers/model_table.json` -- the single data file every per-model
request-shaping decision comes from, never a per-model `if` branch in the
request builder. Fields a row can override:

- **`reasoning_replay`**: how a "thinking" model's prior reasoning is sent
  back on the next request -- `text`/`deepseek_reasoning_content` (a plain
  field), `details`/`openrouter_details` (OpenRouter's structured array),
  `thinking`/`anthropic_thinking` (a real signed Anthropic block, always
  used for the native passthrough dialect), or `empty` (no known
  requirement).
- **Sampling** (`use_temperature`, `use_top_p`, fixed `temperature`/`top_p`/
  `top_k`, `sampling_unsupported_params`): some families reject a sampling
  parameter outright (e.g. Kimi K2.x-K2.7 fix temperature/top_p server-side
  and 400 if you send them); the request builder pops whichever ones a
  row lists right before sending, regardless of what a caller asked for.
- **`edit_format`**: `diff` (the default) vs. a family that prefers a
  different Edit-tool convention.
- **`tool_id_format`**: `preserve` / `kimi_functions_idx` (Kimi's own
  conversation-global `functions.{name}:{idx}` scheme) / `alnum9` /
  `minimax_preserve` -- different hosts accept different tool-call id
  shapes.
- **`tool_choice_required_supported`**: whether the harness may ever send
  `tool_choice: "required"` (the whole Qwen/GLM family and DeepSeek's
  thinking mode reject it).
- **`tool_leak_patterns`**: named per-family regexes for
  `providers/hooks.py::leak_parser` to recognize a text-embedded tool call
  and promote it to a real `tool_use` block (the repair layer -- see
  `docs/ARCHITECTURE.md`).
- **`context_tokens`**, **`max_tokens_default`**, **`max_tokens_cap`**,
  **`databricks_rate_limits`**, **`unverified`** (which fields this row's
  own data has no primary source for -- informational, never gates
  anything).
- **`reasoning_effort_with_tools`** (1.0.1 hotfix 22, narrowed by the H14c
  fixpass finding 12): when set, a request that carries `tools` sends this
  value for `reasoning_effort` REGARDLESS of `--effort`/the session's own
  effort (a tool-less request on the same model is unaffected). Databricks
  ONLY (an OpenRouter model whose id happens to contain "gpt-6" is never
  affected) -- driven by explicit `model_table.json` rows for both real
  models.dev name shapes, `databricks-gpt-6-{sol,luna,terra}` AND
  `databricks-gpt-5-6-{sol,luna,terra}` (the bare substring "gpt-6" is not
  even a substring of the second shape), each setting `"none"`: verified
  live, `dbx:databricks-gpt-6-sol` 400'd on every turn with "Function tools
  with reasoning_effort are not supported for gpt-6-sol in
  /v1/chat/completions... set reasoning_effort to 'none'", since omitting
  the field left the endpoint's own (non-none) default in place. A live
  400 with this exact wording ("function tool" + "reasoning_effort") on
  any OTHER Databricks OpenAI-family endpoint retries once with
  `reasoning_effort` set to `"none"` explicitly; if THAT retry also fails,
  the general strip-the-field retry below gets its own independent chance
  (a shared one-shot flag used to block it after a failed "none" retry),
  and a successful strip resets the session's own effort so later steps
  stop re-sending the rejected value first.

**The Edit-tool context hint**: a DeepSeek/Kimi/GLM/Qwen/MiniMax-family
session's Edit tool description gets one extra sentence asking for at least
three lines of `old_string` context (computed once per session, so the
wire request stays cache-prefix-stable) -- added after telemetry showed
these families' models under-quote `old_string` and hit "Found multiple
matches" far more often than Claude/GPT do. A specific `(provider,
model_id)` row's own `edit_hint` key (including an explicit empty string)
overrides the family default for just that one model.

## Qwen at work

Halo 2.0.2 round 5: a Databricks work box exposes Qwen two different ways,
and the model picker/`/model` no longer let you confuse them.

**General chat/tool-calling Qwen** -- `dbx:databricks-qwen35-122b-a10b` and
`dbx:databricks-qwen3-next-80b-a3b-instruct` (plus every OpenRouter
`qwen/qwen3-*`/`qwen/qwen3.5-*` id): Hermes-style `<tool_call>{json}</tool_call>`
tool calls, parallel calls on OpenRouter (Databricks sends/accepts one tool
call per turn -- no `parallel_tool_calls` field exists on its strict body
allowlist), `arguments` as a JSON-encoded string on the wire (decoded once
into a real dict on the way in, re-encoded with real `json.dumps` -- never
Python's `str()`/repr -- on replay), no `tool_choice: "required"`
(`tool_choice_required_supported: false` on every row), and the Qwen3-Coder/
Qwen3.5 XML shape (`<function=NAME><parameter=KEY>VALUE</parameter></function>`)
on top of the plain Hermes JSON form. `providers/hooks.py::leak_parser`
recovers a call that leaked into plain text under any of FOUR patterns a
Qwen row declares (`hermes_tool_call`, `qwen3_coder_xml`, `python_repr_args`
-- a bare single-quoted Python-dict-literal call with no wrapper at all,
and `missing_tool_call_opener` -- the documented Qwen3-Coder #475 shape, a
`}</tool_call>` tail with no opening tag); `think_tag_strip` also strips a
bare leading `</think>` with no opening tag (Qwen3-235B-Thinking-2507/QwQ's
own documented replay shape).

**OpenJev -- a decision-only judge model, not a chat model.**
`dbx:databricks-openjev-qwen35-4b` is Databricks' own "OpenJev (Qwen3.5 4B)
evaluates yes/no, choice, and scoring questions" endpoint: a constrained
classifier, never meant to receive `tools`/`tool_choice` at all. Halo
classifies this from data, not a hardcoded model id --
`providers/model_table.json`'s top-level `decision_only_name_patterns`
(substrings like `"openjev"`/`"jev-judge"`, so a differently-named judge
endpoint this box has never seen is still caught) and, for the tabled row
above, its own `capabilities.decision_only` + `capabilities.description`.
Effects, all driven by `providers.profiles.decision_only_info`/
`ProviderProfile.decision_only`/`.tools_supported`:
- The `/model` picker lists it under its own **"Databricks (judge /
  decision)"** group with that one-line description as its detail, instead
  of lumping it in with the ordinary `qwen` chat rows.
- `/model dbx:databricks-openjev-qwen35-4b` (or picking it) never becomes
  the session model -- it is written to the `judge` role instead
  (`roles.judge` in `~/.halo/config.json`, the same thing `/roles set judge
  <model>` does) and the command reports the redirect plainly. Launching
  `halo` with `--model`/a remembered last-model pointed at it behaves the
  same way, falling the session back to the ordinary configured default.
- A request that still carries `tools` for this model (or for ANY
  Databricks endpoint a live request previously proved rejects tools, even
  with no table row at all -- see `~/.halo/learned-rules.json`'s
  `tools_rejected` flag, `providers.learned_rules.learn_tools_rejected`)
  never reaches the wire at all: `providers.request.ToolsNotSupported`
  raises a clear, one-line error naming the model and the `judge` role.
  `agent/loop.py` catches the same condition live, the first time an
  UNTABLED endpoint's own 400 says so (Databricks' `json: unknown field
  "tools"`, or OpenRouter's `no endpoints... support tool use`) -- that one
  call teaches the per-endpoint cache so the next one never repeats it.

**Reading the exact 400 from `halo bugreport`.** If a Databricks Qwen
endpoint still errors on tool calls in a shape this round didn't already
cover, run `halo bugreport` right after the failing turn: its route section
names the resolved `provider`/endpoint, and the `bridge.log` tail it embeds
usually carries the raw HTTP status and error body verbatim (Databricks'
own shape is `{"error_code": "BAD_REQUEST", "message": "..."}`; the message
text is what `providers.errors.is_tools_rejected_message`/
`is_effort_with_tools_rejected_message`/`is_reasoning_replay_bug` each try
to recognize). Quote that exact text in a new issue/brief rather than
re-describing it -- see `docs/harness/QWEN-RESEARCH.md` for what was
already confirmed from Databricks' and Qwen's own docs versus what still
needs a real bug report to pin down.

## Reasoning effort

`--effort`, `/effort <level>`, or `settings.json`'s `effortLevel`/
`modelSettings.<id>.effortLevel` when neither of those was given, maps
through `providers/profiles.py::map_effort`/`providers/request.py::
map_effort_anthropic` to whatever field the resolved profile actually
needs: `output_config.effort` (adaptive-thinking Opus/Sonnet 4.6+/Fable/
Mythos) or a `thinking.budget_tokens` value (4096/10000/24000/32000/32000
for low/medium/high/xhigh/max, older Claude models) on the native Anthropic
dialect, `reasoning.effort` on OpenRouter, or `reasoning_effort` on a
Databricks chat endpoint. A profile with `reasoning_no_disable: true`
(GLM-5.3/5.3-Flash -- `thinking.type: disabled` is a hard 400 on that
family) forces an explicitly-disabling effort value back up to that row's
own default instead of ever sending `disabled`.

**The "high" default (1.0.1 hotfix 19, scoped by the H14c fixpass finding
11).** When NOTHING more specific is set anywhere (no `--effort`, no
`/effort`, no settings `effortLevel`) AND the model is ADAPTIVE-capable
(Opus/Fable/Mythos always, Sonnet 4.6+), the route now defaults to `high`
rather than omitting the field. A NON-adaptive Anthropic-family model
(Haiku 4.5, Sonnet 4.5 or older) keeps the old "omit -> provider default"
behavior instead -- that family has no `{type: "adaptive"}` mode at all,
so "high" became a `thinking.budget_tokens` value close to `max_tokens`
(see below), leaving thinking free to crowd out the actual answer. Every
non-Anthropic family keeps the "omit -> provider default" behavior
regardless. A `/model` switch re-clamps (see below) whatever effort the
session already had for the new route, defaulting a chat-route session
that switches INTO an adaptive-capable Anthropic route the same way.

**Accepted levels per route family, and clamping.** Each
`ProviderProfile` carries `effort_values_supported`
(`providers/profiles.py::clamp_effort` enforces it right before a value
reaches the wire):

| Route family | Accepted levels |
|---|---|
| Anthropic Messages (`cc:`, `ant:`, Databricks Claude foundation) | `low`, `medium`, `high`, `max` (no `xhigh` -- that endpoint schema rejects it outright) |
| Databricks GLM (`databricks-glm-*`) | `low`, `high`, `max` ONLY -- narrower than the general chat-dialect set below; see "Databricks GLM" just under this table |
| OpenRouter / Databricks chat, every other family (DeepSeek, Kimi, Qwen, ...) | `low`, `medium`, `high`, `xhigh` (no `max` as of the H14c fixpass finding 13 -- that's an Anthropic-only level; a chat route 400'd on it before this) |
| OpenRouter GLM 5.x (`z-ai/glm-5`, `-5.2`, `-5.3`, `-5.3-flash`) | the full seven Z.ai-direct values: `max`, `high`, `low`, `medium`, `minimal`, `none`, `xhigh` -- OpenRouter forwards them to Z.ai as-is |

A tabled row whose own `reasoning_default_effort` needs a value outside
its route family's default set (GLM's family genuinely defaults to `max`
on OpenRouter/Z.ai directly, per the ingested adapter-rules report --
`reasoning_no_disable` forces a disabling `--effort none` UP to it there)
still accepts that one value too; an explicit `effort_values_supported`
row key, when a future row sets one, always wins outright.

### Databricks GLM (2.0.1, GLM-brief.md)

Verified against the Databricks Foundation Model APIs "supported models"
and "query reasoning models" pages (2026-10-01): every `databricks-glm-*`
endpoint (`databricks-glm-5-2`, `-5-3`, `-5-3-flash`, and any future
`databricks-glm-*` id with no `model_table.json` row of its own yet --
this is a FAMILY-level rule, not per-model) keeps reasoning ALWAYS on and
accepts `reasoning_effort` values `low`, `high`, `max` ONLY -- `max` is
both the default AND the gateway's own SILENT fallback for anything else
(`medium`, `minimal`, `xhigh`, `none`); `none` is rejected outright.
`thinking`/`tool_stream`/`clear_thinking` (all three real Z.ai parameters)
are not in Databricks' parameter list at all, and an unknown field there
is a 400, so none of them can be sent on this route.

**The consequence this fixes**: before 2.0.1, `/effort medium` on a
Databricks GLM route sent `reasoning_effort: "medium"` on the wire, which
the GATEWAY silently coerced to `max` -- the harness never saw an error,
and the user's effort choice was quietly ignored every single turn. That
silent "always max" is also the "GLM seems to pause" symptom
([TROUBLESHOOTING.md](TROUBLESHOOTING.md)): `max` is the model's slowest,
most expensive thinking setting, and a chat-dialect gateway sends nothing
at all -- no reasoning text, no ping -- during that phase; only the
connection stays open. Halo 2.0.1 clamps to the route's REAL default
(`high`, not `max`) and shows the sent value everywhere (`/effort`,
`/status`, `/context`, the stream-json init line's `effort_sent`) instead
of ever silently disagreeing with the gateway.

**The default is sent, not omitted.** Because an omitted field on this
route means `max`, a call with no effort configured anywhere sends
`reasoning_effort: "high"` explicitly (`ProviderProfile.default_effort_
when_unset`, on for the Databricks GLM family only; every other
chat-dialect family keeps "omit -> provider default"). The same rule
applies to an effort inherited from Claude Code's settings (`effortLevel`
or `modelSettings.<id>.effortLevel`, written for Claude models and
typically `xhigh`): a settings value this route does not accept lands on
`high`, not on the clamp map's `max`. An explicit `--effort xhigh` or
`/effort xhigh` is a deliberate choice and still goes through the clamp
map to `max`; `/status` shows the requested and sent values either way.

**Clamp table** (`providers/profiles.py`'s `ProviderProfile.effort_clamp_map`,
consulted by `clamp_effort`/`resolve_effective_effort` before the generic
xhigh/max-narrowing rule):

| Requested | Sent |
|---|---|
| `low` | `low` |
| `medium` | `high` |
| `high` | `high` |
| `xhigh` | `max` |
| `max` | `max` |
| `minimal` | `low` (the cheapest accepted level, not the route default) |
| `none` | `low` (never sent literally -- Databricks rejects it outright) |

`/effort` on a Databricks GLM route therefore offers `low [high] max` (its
real accepted set) and marks any other requested value with "sent as X on
this route" rather than a plain, misleading echo.

Temperature is clamped to `[0, 1]` on every GLM-family route (Z.ai GLM API
docs: "temperature range [0, 1] default 1.0") -- a safety net, since every
seeded GLM row's temperature is already 1.0; and GLM never receives
`tool_choice: "required"` (the gateway accepts `auto` only -- the repair
layer's text-nudge fallback is used instead, same as DeepSeek-thinking/Qwen).

`xhigh` on a route that doesn't list it becomes `max` ONLY when that
route's own accepted set includes `max` (Anthropic, or a GLM-5.3-style
tabled exception) -- that route's own strongest level; any other
unrecognized value (including a now-rejected `max` on an ordinary chat
route) falls back to the route's own default. This is what fixed a live
400 on `dbx:databricks-claude-opus-4-6`: the user's own
`~/.claude/settings.json` `effortLevel: "xhigh"` (valid for real Claude
Code's own routes) reached `output_config.effort` verbatim and was
rejected (`Input should be 'low', 'medium', 'high' or 'max'`) on the very
first prompt. As a backstop for a route whose accepted set isn't modeled
correctly yet, a live 400 naming the effort field retries ONCE with it
stripped from the body before the turn is treated as failed -- recognized
wordings now include Databricks/Anthropic's own `output_config.effort`/
`reasoning_effort` field names, OpenRouter's nested `reasoning.effort`
path, and a generic OpenAI-style "Invalid value: '`max`'"/"'`xhigh`'"
enum-validation 400 that names neither field by name.

**Thinking budget cap (non-adaptive models).** When effort maps to a
`thinking.budget_tokens` value (any non-adaptive Anthropic-family model),
the budget is capped at HALF of `max_tokens` (never below Anthropic's own
1,024-token minimum), not `max_tokens - 1` -- the old near-`max_tokens`
budget left thinking free to crowd out the actual answer. The version
parser behind the adaptive-capability check reads hyphenated ids
(`claude-sonnet-4-6`, `databricks-claude-sonnet-4-6`) as `4.6`, not a bare
major version `4` (a literal-dot `claude-sonnet-4.6` was already read
correctly). Thinking is dropped entirely on the one-shot
`tool_choice: "any"` repair retry (the leak-parser fallback) -- Anthropic
rejects extended thinking together with any forced (non-`"auto"`)
`tool_choice`.

**`/effort`** (1.0.1 hotfix 20): shows the effective level, its source
(`flag`/`settings`/`default`/`session`), and this route's own accepted
levels when given no argument; `/effort <level>` sets it immediately
(clamped the same way), remembered for the rest of the session. In the
TUI, bare `/effort` instead opens an inline selector card in the
transcript -- a horizontal row of this route's accepted levels, current
one bracketed, Left/Right or h/l to move, Enter to apply, Esc to cancel
and keep the old value; a model with no adjustable effort at all shows a
one-line card that closes on any key. The status bar shows a short effort
tag next to the mode glyph.

## The model table, catalogs, and refresh

Three cached files under `~/.halo/` back model-profile resolution,
consulted in this order before falling back to a hardcoded guess
(`model.py::resolve_model_profile`):

1. **`models.json`** (OpenRouter) / **`dbx-endpoints.json`** (Databricks) --
   the live-probed catalog (`halo models --refresh`; see
   `docs/COMMANDS.md`). Supplies real context window, max output tokens,
   vision support, and (OpenRouter) pricing including cache-read/cache-write
   rates.
2. **`routes.json`**'s own `profiles` map -- a hand-authored override,
   keyed by bare model name or `"default"`.
3. **The vendored fallback tier** (`providers/catalog/*.json`, shipped
   inside the package) -- OpenRouter's own catalog and models.dev's
   `databricks` provider entries, frozen at release time, so a **fresh
   install with no network yet** (most notably a Databricks work box behind
   a VPN that might not be up) still resolves real context/output/pricing
   for every model this harness ships defaults for, instead of the bare
   128k/16k dataclass guess.
4. `providers/model_table.json`'s own `context_tokens`/`max_tokens_cap`
   (request-shaping data, not economics, but a real, hand-verified number
   for this harness's own pinned Databricks work-default models even if
   nothing else above has one).

`cc:`/`ant:` subscription models are resolved from a **separate**, fourth
table (`providers/cc_models.py::CC_MODEL_TABLE`) instead -- Claude Code is
the provider, not OpenRouter/Databricks, so neither of the two live catalogs
has an entry for these at all. `halo models --cc --refresh` re-pings
each of the nine alias names once to confirm the exact ids
`sonnet`/`haiku` currently resolve to (the two whose target moves as
Anthropic ships new releases) and caches the result to
`~/.halo/cc-models.json`, consulted before the static table.

`models-dev.json` (models.dev's own public, unauthenticated `api.json`) is
fetched by `halo models --refresh` regardless of which providers are
configured -- it backs the Databricks vendored-fallback tier and
`doctor`'s cache-freshness check.

## Pricing and DBUs

**Live per-turn cost** (`stats`/`/cost`, and the status bar's own `$` field
since 1.0.1 hotfix 14) still works exactly as before for OpenRouter: real
per-token USD pricing in `models.json` (`price_in`/`price_out`/
`price_cache_read`/`price_cache_write`); when a response itself doesn't
carry a `usage.cost` field, `CostMeter` derives one from those rates
(input × price_in + (output + reasoning) × price_out + cache_read/
cache_write at their own rates, falling back to `price_in` for cache tokens
when no specific rate is known). **Databricks** used to always report
`n/a` regardless of pricing; since 1.0.1 hotfix 14, `resolve_model_profile`
feeds the SAME per-endpoint price used for the listed columns below (source
(a), the models.dev `databricks` entry) into this session's `CostMeter` at
start-of-session, so a Databricks turn on an endpoint models.dev prices
gets a real computed running cost through the identical fallback formula
above -- and still shows `n/a` (falls back to token totals in the status
bar) whenever no models.dev price exists for that endpoint, which remains
the common case. A `cc:` row's "cost" is Claude Code's own cumulative
`total_cost_usd`, delta'd since that subprocess's previous turn -- an
estimate, never real per-token billing, and never counted toward
`--max-budget-usd`.

**Listed (not live) context/output/price columns** -- what `/model`,
`/models`, `halo models`, and `init`'s own picker show per row
(`halo_harness.model_display.format_model_row`, one implementation shared
by every one of those surfaces) -- are a DIFFERENT, catalog-level concept:
a normalized (`200000` -> `200k`, `1048576`/`1050000` -> `1M`) reference
figure for the model itself, never a live spend number, sourced per
provider:
  - **OpenRouter**: `models.json`'s own `context_length`/`max_output_tokens`/
    `pricing.prompt`/`pricing.completion` (the exact same fields `CostMeter`
    above reads, just displayed rather than multiplied against real usage).
  - **`cc:`/`ant:`**: the nine subscription-model aliases' own static table
    (`providers.cc_models.profile_fields_for_cc_model`).
  - **Databricks**: (a) the [models.dev](https://models.dev) `databricks`
    provider's entry whose id equals the endpoint name (the live
    `~/.halo/models-dev.json` cache, refreshed by `models --refresh`/
    `/models refresh`, else the package-vendored fallback) -- `limit.context`/
    `limit.output` for context/output, `cost.input`/`cost.output` (already
    USD per million tokens) for the prices, used AS-IS, never re-multiplied;
    (b) missing that, `model_table.json`'s own `context_tokens` (output/
    prices then stay blank); (c) neither -- every field blank. **A blank
    field is never shown as `?`** -- it just keeps its column's width.

`databricks.dbu_price_usd` (`~/.halo/config.json`, or a team.json's
`dbu_price_usd`) is a THIRD, separate figure, in a different unit again: it
converts an endpoint's own **catalog-advertised** DBU rate
(`usage_policy.output_dbu_per_1k_tokens`, when the workspace publishes one)
into a dollar figure, appended after the ctx/output/price columns as
`dbu=<rate>` only when actually known -- a reference price, never a real
per-turn spend, and never conflated with the USD/1M-token prices above.

## The `/model` picker

Opening `/model` with no argument triggers a background refresh of the
Databricks/OpenRouter/Hugging Face catalogs (each gated on that provider
being enabled) if older than `databricks.catalog_max_age_hours` (default
24h, the one shared staleness knob every network catalog here uses) --
the picker itself opens immediately with whatever's already cached; the
refresh only ever notifies afterward if something changed. The picker is
a filterable, arrow-key list (`Up`/`Down`/`PageUp`/`PageDown`/`Home`/`End`
move the highlight, typing filters, `Enter` confirms, `Esc` cancels -- the
filter box itself keeps keyboard focus throughout) grouped by provider/
family, one header per group: OpenRouter, `Hugging Face` (Halo 2.0.3
round 4 -- the router's own cached catalog, shown once the provider is
enabled), `Claude Code subscription` (shown only when `claude auth status`
reports an actual claude.ai login -- never on a box whose `claude` is only
logged in via, say, a Databricks work box's own settings), and
`Databricks (<family>)` per family, each row showing which gateway path
type it resolves to (see [DATABRICKS.md](DATABRICKS.md)) alongside the
ctx/output/price columns above; non-chat endpoints (embeddings/whisper) are
hidden entirely. `halo init`'s own per-provider and final
cross-provider model pickers (see [COMMANDS.md](COMMANDS.md)'s `init`
section) use this exact same list widget and row format. See
[DATABRICKS.md](DATABRICKS.md) for exactly how a Databricks model
reference resolves to a URL, and [COMMANDS.md](COMMANDS.md) for
`halo models`'s own flags.

Ollama hosts and Hugging Face local servers never appear in THIS picker
(by design -- an `ol:`/`hf:local/*` model is a `roles.small` pick far more
often than "the one main model," and listing every host's live catalog
here would mean a network read on every `/model` open); `/local` (round
5) is the dedicated discovery view for both.

## Enforced offline mode (Halo 2.0.3 round 5e)

`halo --offline`/`/offline on`/`network.offline` in `~/.halo/config.json`
make the one HTTP choke point (`providers/http.py`'s `open_upstream`/
`urlopen_tls` -- every provider call in this document, every catalog
refresh, every update check, and the WebFetch/WebSearch tools all funnel
through one of the two) refuse any connection whose host is not loopback
or an allow-listed local host (`ollama.hosts`, `huggingface.
local_servers`, or a `halo local serve` managed server); a refusal is
always the one plain sentence "offline mode: not connecting to \<host\>",
never retried. In practice this means every route this document describes
except `ol:`, `hf:local/*` and `hf:mlx/*` (and a `hf:endpoint`/manual
`hf:local` entry that happens to point at a LAN address the user never
configured) simply refuses outright while offline mode is on -- see
[CONFIG.md](CONFIG.md)'s `network.offline` section for the full allow-list
and [COMMANDS.md](COMMANDS.md)'s `--offline`/[SLASH-COMMANDS.md](SLASH-COMMANDS.md)'s
`/offline` for how to turn it on.

## Hybrid escalation (Halo 2.0.3 round 5e)

`routing.escalation` (`~/.halo/config.json`) lets a local-first session
(`ol:`/`hf:local/*`/`hf:mlx/*` -- never a cloud-model session) name a
cloud fallback ref and the conditions that should escalate to it:
`low_confidence` (a one-word CONFIDENT/UNSURE check against the `judge`
role -- the exact same role-resolution and one-shot-call shape `roles.
small`'s own answers already use, never a new scoring mechanism of its
own), `tool_failures` (two or more failed tool calls in the current
turn), and `context_overflow` (the existing compaction-overflow retry path
firing this turn). `ask: true` (the default) only ever notifies, staying
on the local model; `ask: false` switches for the rest of the turn (`tool_
failures`/`context_overflow`) or starting with the next model call
(`low_confidence`, since the triggering turn has already finished) and
says so in the transcript. A role-table entry's own `"escalation": false`
turns this off for every sub-agent resolved under that role, regardless of
the top-level policy. `/escalation` shows the active policy and this
session's last few decisions; see [CONFIG.md](CONFIG.md)'s `routing.
escalation` section for the full config shape.

## Saved versus cloud (Halo 2.0.3 round 5e)

For an `ol:`/`hf:local/*`/`hf:mlx/*` turn, the cost meter also prices that
turn's real input/output/cache token counts (the exact same arithmetic
`CostMeter`'s own cloud-pricing fallback formula uses) against a
REFERENCE price -- the session's configured `routing.escalation.to`, when
one is set, else the median `price_in`/`price_out` across every model the
package's own vendored fallback catalogs carry a real price for (no
network call, so this works identically online or under `--offline`) --
and accumulates the difference as "saved". The status bar's cost chip
shows it as " · saved $x" next to the ordinary cost figure; `/cost` prints
the running total, the turn/token counts it is over, and which reference
price it used and why. Always `$0.00`/omitted on a cloud-model session --
there is nothing to compare it against.
