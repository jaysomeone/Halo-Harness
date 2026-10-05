# Mac quick-start (Apple Silicon)

Halo 2.0.3 round 5f. This is the script rolo follows on his own Apple
Silicon Mac to confirm the Ollama-on-a-Mac work (round 5b part 1) and the
experimental `hf:mlx/<org>/<repo>` route (round 5f) actually work there --
the worker who wrote this code has no Mac at all, so every claim below is
either read straight from this repo's own code/docs or marked **please
confirm** where the research (`docs/harness/GPU-RESEARCH.md`,
`docs/harness/LOCAL-MODELS-RESEARCH.md`) could not verify it. Run each
step in order; if a step's actual output doesn't match "what you should
see," stop and paste back exactly what you got (the command, the full
output, and your macOS version) rather than guessing past it.

## 1. Install Halo

```sh
uv tool install git+https://github.com/roloVibes/Halo-Harness
```

**You should see**: `uv` resolves and installs, ending with something like
`Installed 1 executable: halo`. If `uv` itself isn't installed, see
[INSTALL.md](INSTALL.md) first (Homebrew: `brew install uv`).

**If not**: paste back the full `uv` output and `uv --version`.

## 2. `halo doctor`

```sh
halo doctor
```

**You should see**: a list of `[OK]`/`[WARN]`/`[MISSING]` lines (Python,
`~/.claude` layout, shell, editor, etc.). A fresh install will show several
`[WARN]`s for providers you haven't configured yet (OpenRouter, Databricks,
...) -- that's expected and not a problem for this quick-start, since every
step below uses local models only. There should be NO line mentioning
"MLX" yet (you haven't installed the `mlx` extra -- step 7).

**If not**: paste back the full output.

## 3. `halo ollama doctor`

```sh
halo ollama doctor
```

This is the Mac's OWN loopback Ollama daemon (round 5b part 1's own
framing: "the PC's Ollama serves the LAN, but the Mac has its own Ollama
models that are NOT shared on the network" -- this command tunes THIS
machine on its own terms, never the LAN one).

**You should see**: a reachability line for the default host
(`http://127.0.0.1:11434` unless `OLLAMA_HOST` says otherwise), the Ollama
version, loaded-model count, and a per-OS tuning checklist as plain
sentences -- `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`,
`OLLAMA_CONTEXT_LENGTH`, keep-alive, and a line about the Ollama menu-bar
app's "Expose to network" switch (off by default) being the thing that
actually controls whether this daemon is LAN-reachable -- the environment
variable alone does not.

**Please confirm**: the exact wording of that menu-bar line, and whether
`launchctl setenv OLLAMA_FLASH_ATTENTION 1` (then restarting the Ollama
app) is really how you'd set one of these on macOS -- round 5b's own
research explicitly could not verify the macOS mechanism for setting a
menu-bar app's environment variables (every `developer.apple.com` fetch
that round returned a page title with no body text); this command's
printed checklist may say "unconfirmed" or similar next to that one line.
If Ollama isn't installed at all yet, `brew install ollama` first, then
`ollama pull qwen2.5:7b` (used below) before re-running this step.

**If not**: paste back the full output, plus `ollama --version` and
whether you've ever used the Ollama menu-bar app's own settings UI.

## 4. `halo local`

```sh
halo local
```

**You should see**: a merged list with a `Ollama (default)` group showing
`qwen2.5:7b` (or whatever you've pulled), and a `Hugging Face Hub cache`
group -- empty unless you've already used `hf download`/LM Studio/mlx_lm
on this Mac before.

**If not**: paste back the full output.

## 5. `halo ollama calibrate qwen2.5:7b`

```sh
halo ollama calibrate qwen2.5:7b
```

(substitute whatever Ollama model you actually pulled)

**You should see**: `Calibrating qwen2.5:7b on 'default' (...) locally...`,
a starting `num_ctx` candidate, then either `fits fully in GPU memory at
num_ctx=<N>` or a "does not fit" line, ending with `recorded in
~/.halo/ollama-fit.json`. This is the GROUND TRUTH measurement for "how
much unified memory this model class actually uses" -- the Apple memory-
share fraction below is only a first guess BEFORE this number exists.

**Please confirm**: that this actually completes in a reasonable time on
your machine (a Mac has no `nvidia-smi`-style tool to watch along the
way) and that the reported `num_ctx` looks sane for a 7B model's size.

**If not**: paste back the full output.

## 6. `halo gym --quick` (Ollama only, before touching MLX)

```sh
halo gym --models ol:qwen2.5:7b --quick
```

**You should see**: a short run (the brief's own "halve the battery size
N for a faster, noisier read"), ending with `halo gym show` printing one
card: tool-call accuracy, edit success, context recall, tokens/second,
prefill seconds, and the Ollama version/quant/fitted-context this was
measured at.

**If not**: paste back the full output and how long it took.

## 7. Install the MLX extra

```sh
uv tool install "halo-harness[mlx]"
```

This installs `mlx-lm` -- Apple's MLX inference runtime -- ONLY because
your `sys_platform`/`platform_machine` match `darwin`/`arm64`
(`pyproject.toml`'s own environment marker); on any other machine this
extra is simply unavailable to install. Re-run `halo doctor` afterward:

```sh
halo doctor
```

**You should see**, somewhere in the output: `[OK] MLX (Apple Silicon):
mlx-lm installed -- hf:mlx/<org>/<repo> is ready to use`.

**If not**: paste back both the `uv tool install` output and the new
`halo doctor` output (specifically whether an MLX line appears at all,
and whether it says `[OK]` or `[WARN]`).

## 8. One `hf:mlx` turn

```sh
halo -p "Reply with the single word pong." --model hf:mlx/mlx-community/Qwen2.5-7B-Instruct-4bit
```

The FIRST run will print a plain notice to stderr naming the repo and
where mlx-lm will cache the weights (`~/.cache/huggingface/hub` by
default), then start downloading -- this can take a few minutes depending
on your connection (a 4-bit 7B model is roughly 4-5 GB). Every run AFTER
the first reuses the already-downloaded weights and the already-running
managed server (no second download, no second consent notice).

**You should see**: the notice line(s), then eventually `pong` printed as
the reply.

**Please confirm**: `mlx_lm.server`'s own startup time and whether it
prints anything unusual to the terminal despite Halo redirecting its
stdout/stderr away -- this round's research
(`docs/harness/GPU-RESEARCH.md` section 7) could NOT confirm
`mlx_lm.server`'s exact flags (host/port defaults, a context-length-at-
launch flag name) from its README alone; Halo's own managed-server code
only ever passes `--model <repo> --port <port>` (no `--host`, no context
flag) -- if the server doesn't come up, or comes up on an unexpected
interface, that is the first thing to check.

**If not**: paste back the FULL output (including stderr) of the command
above, and `ps aux | grep mlx_lm` from another terminal while it's
running (or right after it fails).

## 9. `halo doctor --local` against the MLX model

```sh
halo doctor --local --model hf:mlx/mlx-community/Qwen2.5-7B-Instruct-4bit
```

**You should see**: four `[PASS]` lines (load, tool call, structured
output, compaction summary), each with an elapsed time. This reuses the
SAME managed server step 8 started (no second download, no second
notice) -- "reused when already running" is exactly what should happen
here.

**If not**: paste back the full output. A `[FAIL]` on "structured output"
specifically would mean mlx_lm.server's OpenAI-compatible endpoint
doesn't accept the `response_format` JSON-schema shape the way
llama-server/Ollama do -- useful to know either way.

## 10. Compare the two cards

```sh
halo gym --models hf:mlx/mlx-community/Qwen2.5-7B-Instruct-4bit,ol:qwen2.5:7b --quick
```

**You should see**: two cards, one per model, through round 5i's own
`hf:`/`ol:` dispatch in `gym_send.send_turn_for`. Both show the same four
scores (tool-call accuracy, edit success, context recall, instruction
adherence) and a tokens/second figure. Prefill seconds and the quant/
engine-version line are honestly asymmetric, not a guessed pair: the
Ollama card has real prefill seconds (from `/api/chat`'s own
`prompt_eval_duration`) and Ollama's version string; the `hf:mlx` card's
`tokens_per_second` is a wall-clock estimate (the shared openai-chat
sender exposes no per-phase timing to a caller outside it) and its
prefill seconds comes back `None` rather than a faked number -- no
mlx-lm version is captured either. A stale comparison is still visible
from the scores and tok/s alone; it just isn't a version-string diff.
Note the comparison is also NOT apples-to-apples weight-for-weight (MLX's
own 4-bit quantization and GGUF's `q4_K_M` are different schemes, round
5f's own research says so explicitly) -- it's a real, measured "how does
this engine feel on THIS Mac" comparison, not a benchmark of the model
itself.

**Please paste back regardless of pass/fail**: both full cards. This is
the actual deliverable rolo asked for -- "measurements, as the roadmap
says" -- not a guess from the worker who wrote this code.

## Please confirm -- summary of every unverified claim above

- The exact wording/location of the Ollama menu-bar app's environment
  variables and "Expose to network" switch (step 3).
- Whether `launchctl setenv <VAR> <value>` plus restarting the Ollama app
  is really the mechanism for setting one of its env vars on macOS (step
  3) -- round 5b's own research marked this UNCONFIRMED (no Apple page
  rendered body text for any fetch this round tried).
- The Apple unified-memory GPU-usable-share fraction (docs/harness/
  GPU-RESEARCH.md's "Apple Silicon memory-share rule": "about two-thirds
  to three-quarters of RAM," carried over from the round 5b brief's own
  text, never confirmed against an Apple or mlx-lm primary source) --
  step 5's calibration is the real ground truth; this fraction is only
  ever the WIZARD's first guess before that.
- `mlx_lm.server`'s own exact flags beyond `--model`/`--port` (host
  default, any context-length-at-launch flag) -- step 8/9 is where this
  gets resolved in practice.

## See also

- [MODELS.md](MODELS.md) -- the full `hf:mlx/*` reference and every other
  model route.
- [INSTALL.md](INSTALL.md) -- installing Halo itself.
- `docs/harness/GPU-RESEARCH.md` section 7 and the Apple memory-share
  rule -- the research this quick-start is built on.
