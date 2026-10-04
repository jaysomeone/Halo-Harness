# GPU research for Halo 2.0.3 round 5a

Compiled 2026-10-04. Scope: the "Round 5a: GPU research" topic list in
`plans/2.0.3-ollama-round2-brief.md`, written for rounds 5b (local-model
excellence / VRAM-aware fit), 5c (find-and-use local model files) and 5f
(Apple Silicon in-process `mlx-lm` backend) to build on. Docs only, no
code, no tests. Method: WebFetch only (no WebSearch) against documentation
pages, GitHub source/docs, and PyPI; the one exception is
`nvidia-smi --help-query-gpu` and a handful of read-only `nvidia-smi`
invocations run live against the build host's own NVIDIA card this round
to confirm field names and output shapes -- per the brief, only shapes are
quoted below, never the build host's own identifying values (its exact
memory size, driver/CUDA version, GPU name, etc. are never written into
this file; illustrative numbers in worked examples are invented, not read
off the real card). Builds on, and tries hard not to repeat,
`docs/harness/LOCAL-MODELS-RESEARCH.md` sections 2 and 3 -- see
"Corrections to LOCAL-MODELS-RESEARCH.md" near the end for where this
round's findings change that document instead of just adding to it, per
the brief's own instruction to correct there, not here, by addition only.
Every claim cites the URL read and the date; anything not confirmed from a
fetched primary source is marked **UNCONFIRMED** rather than guessed.

## Summary: what changes for rounds 5b, 5c and 5f

Round 5b's fit/calibration work gets concrete new inputs it didn't have
before: a confirmed per-card overhead knob (`OLLAMA_GPU_OVERHEAD`, "set
aside VRAM per GPU", default 0) and a confirmed multi-GPU spread rule
straight from Ollama's own FAQ and source ("if the model will entirely fit
on any single GPU... that GPU. If the model does not fit... spread across
all the available GPUs", plus `OLLAMA_SCHED_SPREAD` to force spreading
early); a corrected, not approximated, set of KV-cache bytes-per-element
constants that change the fit arithmetic's own numbers for `q8_0`/`q4_0`
(both of Halo's current guesses under-count memory by 6-12%, the wrong
direction for a budget estimate); a second memory knob for Apple Silicon
confirmed from a primary source (`iogpu.wired_limit_mb`, sourced from
mlx-lm's own README rather than Apple's docs, which still would not render
readable text to this round's fetches); and concrete new functions named
against the real code (`probe_nvidia_cuda_version`, `multi_gpu_fit_estimate`,
`probe_apple_unified_memory`, each detailed below) rather than a wishlist.
The AMD and Intel branches move from "no vendor doc fetched at all" to
concrete, citable flags (`amd-smi`, `xpu-smi`), though AMD's legacy
`rocm-smi` script itself stays exactly as unconfirmed as before.

Round 5c's asset-fetch feature gets a materially different answer on
checksums than its own brief text assumed: llama.cpp's releases carry no
project-published sums file at all (none was found on the release
inspected this round), so "checksum-verified against the release's
published sums" has to mean GitHub's own Releases-API `digest` field
instead of a file the project doesn't publish. The driver-version-to-asset
rule also turns out to need no hand-maintained lookup table: `nvidia-smi
-q`'s own `CUDA Version` line already reports the ceiling directly (a
field absent from the scriptable `--query-gpu` list, confirmed live this
round, which is itself the concrete code change this finding implies).
Round 5f's comparison plan gets a fairness checklist (same quant family,
context length set explicitly on both engines, warm starts before timing,
and shared-memory reporting since both engines compete for the same
unified-memory pool on one Mac) and the finding that mlx-lm's own README --
not Apple's own documentation site, which this round still could not
extract body text from -- is the best current primary source for how
Apple Silicon's GPU memory ceiling actually behaves in practice.

## 1. Vendor and OS memory/telemetry probes

### NVIDIA

Source: `docs.nvidia.com/deploy/nvidia-smi/index.html` (seen 2026-10-04)
plus `nvidia-smi --help-query-gpu` and several read-only `nvidia-smi`
invocations run directly against the build host this round (shapes only).

- Confirms `halo_harness/providers/ollama_hw.py`'s existing invocation:
  `nvidia-smi --query-gpu=memory.total,memory.used,memory.free,name
  --format=csv,noheader,nounits` is exactly the documented
  `--query-gpu`/`--format=csv[,noheader][,nounits]` pattern (`csv` is
  "MANDATORY", `noheader` "skip first line with column headers",
  `nounits` "don't print units for numerical values", per the page).
- **New field confirmed, not currently read: `memory.reserved`** --
  "Total memory reserved by the NVIDIA driver and firmware" (the page's
  own field list; live-confirmed on the build host: querying
  `memory.total,memory.reserved,memory.used,memory.free` together gives
  four numbers where `total = reserved + used + free` to within
  rounding). `memory.free` already excludes the reserved slice, so a
  caller computing `free = total - used` by hand would OVERESTIMATE free
  memory. `ollama_hw.py` does not do that today (it reads `memory.free`
  directly) -- no bug, but worth a code comment and worth surfacing
  `memory.reserved` in the host panel's own arithmetic note.
- **Power and temperature fields** (brief's own ask): `power.draw`
  ("last measured power draw for the entire board, in watts... accurate
  to within +/- 5 watts"), `power.draw.average`/`power.draw.instant`
  (Ampere-or-newer split), `power.limit` (software cap); `temperature.gpu`
  (core, degrees C), `temperature.gpu.tlimit` (throttle margin),
  `temperature.memory` (HBM). Live-confirmed shape: in plain
  `--format=csv` (no `noheader,nounits`), `power.draw`/`power.limit`/
  `utilization.gpu` carry a unit suffix in BOTH the header cell (e.g.
  `"power.draw [W]"`) and the value cell (e.g. `"7.35 W"`);
  `temperature.gpu`'s header carries NO unit bracket at all and its bare
  value has no suffix to strip either way. `--format=csv,noheader,nounits`
  strips both consistently, which is exactly why `ollama_hw.py` already
  always requests that combination.
- **Driver/CUDA version, for round 5c's asset picker**: the page
  documents "KMD Version" (kernel-mode/display driver) and "CUDA UMD
  Version" -- "the latest CUDA version supported by the driver... usually,
  but not always, the version of the CUDA toolkit installed." **This
  field is absent from `--query-gpu`'s own field list** (confirmed live:
  no `cuda_version`-shaped entry appears in `--help-query-gpu`'s output)
  -- it only appears in the plain table header (`nvidia-smi` with no
  arguments prints a `Driver Version: <X>  CUDA Version: <Y>` row) or in
  `nvidia-smi -q`'s detailed colon-delimited dump, which prints a
  dedicated `CUDA Version                                           : <Y>`
  line (both shapes live-confirmed on the build host). **What Halo should
  do**: `ollama_hw._probe_nvidia` only ever calls `--query-gpu=...`; add a
  separate `probe_nvidia_cuda_version()` function in `ollama_hw.py` that
  shells `nvidia-smi -q` and greps the `CUDA Version` line, returning
  `None` on anything else -- it cannot be folded into the existing query
  as one more comma-separated field, and should only be called when round
  5c's asset picker actually needs it, not on every hardware probe.
- Multi-GPU: "information for all available GPUs... is displayed" by
  default; `-i` selects one GPU by index/serial/UUID/PCI-bus-id; a
  multi-GPU query prints one CSV line per card. This confirms
  `_probe_nvidia`'s own docstring caveat ("the first (index 0) is
  reported... short of the full picture on a multi-GPU" host) is accurate
  and gives the exact fix: stop taking `.splitlines()[0]` and iterate
  every line. **What Halo should do**: give `_probe_nvidia` a
  `probe_all=False` parameter that, when true, returns `list[GpuMemory]`
  by iterating every output line instead of only the first -- see the
  "Multi-GPU fit formula" section below for what calls it with `True`.

### AMD / ROCm

Sources: `rocm.docs.amd.com/projects/amdsmi/en/latest/how-to/amdsmi-cli-tool.html`
and the Linux kernel's own amdgpu driver docs,
`kernel.org/doc/html/latest/gpu/amdgpu/driver-misc.html` (both seen
2026-10-04).

- **`amd-smi` (now confirmed) is AMD's current documented CLI; `rocm-smi`
  (still shipped) stays exactly as unconfirmed as before**. AMD's own
  current docs site documents `amd-smi`, not `rocm-smi`. Confirmed
  commands: `amd-smi static --vram` ("all vram information", total
  capacity), `amd-smi metric --mem-usage` ("memory usage per block"),
  `amd-smi metric --temperature`, `amd-smi metric --power`, `amd-smi
  metric --usage` (engine usage); a `--json` flag works on every
  subcommand (field names seen include `VRAM_MEM`/`MEMORY_USAGE`/`SIZE`);
  device selection via `-g`/`--gpu` (index, `all`, or BDF/UUID). `rocm-smi`'s
  own exact flag syntax (the brief's and `ollama_hw.py`'s own naming,
  `--showmeminfo vram`) could **not** be re-confirmed this round -- the
  ROCm SMI library's docs site covers only its C/Python API, and the CLI
  script's source did not surface its argument parser to this round's
  fetches. **UNCONFIRMED at the exact-flag level, unchanged from before.**
- **sysfs, upgraded from UNCONFIRMED to confirmed**: the kernel's own
  amdgpu docs confirm `mem_info_vram_total` ("returns the total amount of
  VRAM in bytes") and `mem_info_vram_used` ("returns the total amount of
  currently used VRAM in bytes") -- this round adds the USED file
  `ollama_hw.py`'s own docstring said had "no documented sibling file"
  for computing free memory; there is still no `mem_info_vram_free` file,
  but `total - used` is now computable from two confirmed files instead
  of one. Also confirmed, not currently needed by Halo's fit arithmetic:
  `mem_info_vis_vram_total`/`_used` (CPU-visible VRAM) and
  `mem_info_gtt_total`/`_used` (GTT, relevant to APUs/iGPUs).
- **What Halo should do**: upgrade `_probe_amd_sysfs` (ollama_hw.py) to
  also read `mem_info_vram_used` from the same directory as
  `mem_info_vram_total` and compute `free_bytes = total - used` instead of
  always leaving `free_bytes=None`; add a new `_probe_amd_smi` function
  (preferred over `_probe_amd_rocm_smi`, since `amd-smi` is the
  vendor-documented current tool) that shells `amd-smi static --vram
  --json` and `amd-smi metric --mem-usage --json`; keep
  `_probe_amd_rocm_smi` as a fallback behind its existing UNCONFIRMED
  caveat, tried after `_probe_amd_smi` rather than instead of it.

### Intel Arc

Source: `github.com/intel/xpumanager`'s README and `doc/smi_user_guide.md`
(seen 2026-10-04; `intel.github.io/xpumanager/smi_user_guide.html` 404s --
the guide now lives only in the GitHub repo's `doc/` tree).

- `xpu-smi discovery -d <id>` lists static device info including
  `"Memory Physical Size"` (total); `xpu-smi stats -d <id>` is the
  live-metrics command, with confirmed plain-text columns `"GPU Memory
  Used (MiB)"`, `"GPU Memory Util (%)"`, `"GPU Core Temperature (C)"`,
  `"GPU Power (W)"`, and an all-engines utilization percentage. A `-j`
  flag adds JSON to both commands (confirmed to exist; the exact JSON key
  names were not shown verbatim beyond the plain-text columns above --
  **UNCONFIRMED** at the JSON-key level). Device selection is `-d
  <index>` or `-d <PCI BDF>`; no comma-separated multi-device syntax was
  shown for `stats` -- **UNCONFIRMED** whether one invocation can cover
  every device at once the way `nvidia-smi` does.
- Install: `apt install xpu-smi` (Ubuntu, after adding Intel's graphics
  PPA) or a `.deb`/Windows installer from the GitHub releases page.
  Targets Intel Arc Pro Series and Data Center GPU product lines per the
  README's own description.
- **What Halo should do**: add `_probe_intel_xpu` to `ollama_hw.py`,
  parallel to `_probe_amd_rocm_smi` -- shell `xpu-smi discovery -d 0` for
  total memory and `xpu-smi stats -d 0` for used/free, with the same
  "`None` on anything unexpected, never a guess" discipline every probe
  in this module already follows; call it from `probe_local_gpu_memory`
  only after NVIDIA and (on Linux) the AMD probes both come back empty,
  gated by `sys.platform != "darwin"` the same way the AMD branch already
  is.

### Apple Silicon

Sources: `pypi.org/project/mlx-lm` and `github.com/ml-explore/mlx-lm`'s
README (both seen 2026-10-04). Apple's own Metal documentation pages
(`developer.apple.com/documentation/metal/mtldevice/recommendedmaxworkingsetsize`
and an Xcode memory-footprint guide) returned only a page title with no
body text to every fetch attempted this round (a JavaScript-rendered
documentation site WebFetch's HTML-to-markdown conversion could not get
past) -- **UNCONFIRMED from an Apple primary source this round**, the
same conclusion `ollama_hw.py`'s own `_probe_apple` docstring and the
research doc's section 3 already reached for this whole branch.

- **`iogpu.wired_limit_mb` is now confirmed, from a primary (if
  non-Apple) source**: mlx-lm's own README states that for large models
  it "will attempt to make them faster by wiring the memory occupied by
  the model and cache. This requires macOS 15 or higher," and names
  `sudo sysctl iogpu.wired_limit_mb=<N>` as the documented way to raise
  the ceiling for models that need more than the OS default. This is the
  exact sysctl the brief named, now confirmed real and actively used by a
  current, maintained, Apple-adjacent project (MLX is Apple's own
  framework), even though Apple's own web docs did not render readable
  text to this round's fetches.
- `system_profiler SPDisplaysDataType` and `sysctl hw.memsize` themselves
  remain **UNCONFIRMED against any fetched primary source this round**
  (unchanged from `_probe_apple`'s own docstring) -- both are long-standing
  stable macOS CLI surfaces, but no Apple documentation page for either
  rendered readable text this round.
- **What Halo should do**: no change to `_probe_apple` itself (it still
  correctly returns `None` on unified-memory Macs, since `system_profiler`
  doesn't itemize VRAM there, per its own docstring). Add a NEW function,
  `probe_apple_unified_memory()`, to `ollama_hw.py` -- it shells `sysctl
  -n hw.memsize` (total RAM, bytes) and `sysctl -n iogpu.wired_limit_mb`
  (0/absent when unset); this is the function round 5b's Apple
  memory-share rule (its own dedicated section below) needs, and it is a
  different function from `_probe_apple` because it reads different
  sysctl keys, not `SPDisplaysDataType` text.

### Windows fallbacks

Sources: `learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-videocontroller`
and `.../api/dxgi1_4/nf-dxgi1_4-idxgiadapter3-queryvideomemoryinfo` (both
seen 2026-10-04).

- **`Win32_VideoController.AdapterRAM`**: confirmed `uint32`,
  `Units("bytes")`, in the class's own MOF syntax block. A `uint32` tops
  out at 4,294,967,295 -- one byte short of 4 GiB -- **by construction**,
  which is the documented basis for the brief's "known 4 GB limitation."
  Microsoft's page does not separately spell out overflow behavior past
  that value (wrap, clamp, or zero is **UNCONFIRMED**) -- moot for Halo
  today regardless, since `ollama_hw.py` never shells out to WMI at all.
  The class also exposes `DriverVersion` (string), `AdapterCompatibility`
  (string, vendor), and `VideoProcessor` (free-form string); none carry a
  usable memory figure.
- **DXGI budget path confirmed as native-code-only**:
  `IDXGIAdapter3::QueryVideoMemoryInfo` is a COM method (`HRESULT`,
  requires linking `Dxgi.lib`/`Dxgi.dll`) returning a
  `DXGI_QUERY_VIDEO_MEMORY_INFO` struct (`Budget`, `CurrentUsage`,
  `AvailableForReservation`, `CurrentReservation`, confirmed from that
  struct's own doc page) split by `DXGI_MEMORY_SEGMENT_GROUP`
  (`LOCAL`/`NON_LOCAL`). **No PowerShell or .NET path is documented on
  either page** -- every example and "Requirements" block names C++/COM
  only. **UNCONFIRMED** whether a usable P/Invoke wrapper exists in the
  wild; out of scope regardless, since Halo ships no compiled Windows
  helper today.
- **What Halo should do**: since `nvidia-smi` already works identically
  on Windows and Linux (confirmed round 3, reconfirmed this round), add a
  `_probe_windows_wmi` function to `ollama_hw.py` as the LAST fallback
  (after NVIDIA and, where applicable, AMD/Intel) -- `Get-CimInstance
  Win32_VideoController | Select-Object Name,AdapterRAM` via
  `subprocess.run(["powershell", "-NoProfile", "-Command", ...])`,
  documented in its own docstring as total-only (never free/used), capped
  near 4 GiB by the field's own datatype, good only as a last-resort
  "something is there" signal, never a fit-arithmetic input. Do not
  attempt the DXGI path -- no scripting surface exists for it, so the
  fallback chain stops at WMI.

## 2. Multi-GPU: how Ollama and llama.cpp split a model

Sources: `docs.ollama.com/faq.md`, `docs.ollama.com/gpu.md`, and
`raw.githubusercontent.com/ollama/ollama/main/envconfig/config.go` (all
seen 2026-10-04); llama.cpp's server README (seen 2026-10-04).

- **Ollama's own rule, confirmed in plain English from the FAQ**: "If the
  model will entirely fit on any single GPU, Ollama will load the model
  on that GPU... If the model does not fit entirely on one GPU, then it
  will be spread across all the available GPUs." An all-or-nothing choice
  between "one card" and "every configured card," not a tunable ratio.
- **`OLLAMA_SCHED_SPREAD`, confirmed from source, not from either docs
  page** (the FAQ and GPU pages are both silent on it; found in
  `envconfig/config.go`'s own declaration): a bool, doc comment "Always
  schedule model across all GPUs" -- forces the spread even for a model
  that would fit on one card, trading concentration for parallelism.
- **`OLLAMA_GPU_OVERHEAD`, confirmed from the same source, also absent
  from both docs pages**: `uint64`, default `0`, doc comment "Set aside
  VRAM per GPU" -- the documented per-card reserve the brief's multi-GPU
  fit arithmetic needs, answered directly rather than guessed.
- **Device-selection env vars** (confirmed, `gpu.md`): `CUDA_VISIBLE_DEVICES`
  (NVIDIA), `ROCR_VISIBLE_DEVICES` (AMD), `GGML_VK_VISIBLE_DEVICES`
  (Vulkan fallback) -- comma-separated; UUIDs preferred over numeric
  indices for NVIDIA ("ordering can vary").
- **llama.cpp's two knobs are a different shape**: `-ngl`/`--n-gpu-layers`
  ("max. number of layers to store in VRAM, either an exact number,
  'auto', or 'all', default: auto") picks how much of one model goes to
  GPU(s) at all; `-ts`/`--tensor-split N0,N1,N2,...` ("fraction of the
  model to offload to each GPU, comma-separated list of proportions, e.g.
  3,1") is the explicit per-card ratio Ollama has no equivalent of --
  Ollama's spread is automatic and binary, llama.cpp's split is a number
  the caller picks.
- **What `/api/ps` reports for a split model**: unchanged from the
  single-card shape already confirmed (`size`, `size_vram`) -- both are
  already TOTALS across every card, not broken out per device.
  **UNCONFIRMED** whether any Ollama endpoint reports a per-card
  breakdown of a spread model.
- **What Halo should do**: `ollama_hw.estimate_fit_for_host` and
  `ollama_fit.fit_estimate` both currently model exactly one GPU
  (`free_memory_bytes` is one number). Change `probe_local_gpu_memory`
  (section 1's NVIDIA fix) to optionally return `list[GpuMemory]`, and add
  a new pure function to `ollama_fit.py`, `multi_gpu_fit_estimate(
  kv_bytes_per_token, free_bytes_per_card: list[int], resident_weight_bytes,
  overhead_per_card_bytes=0)` implementing "sum of free memory minus
  per-card overhead" -- worked example in its own section below. Surface
  `OLLAMA_GPU_OVERHEAD`, when a host config sets it, as that function's
  `overhead_per_card_bytes` rather than inventing a new config key.

## 3. Remote GPU telemetry options

- **ssh probe (round 5b's own plan)**: run the exact same vendor-probe
  argv list section 1 already improves, over `ssh -o BatchMode=yes -o
  ConnectTimeout=3 user@host <command>`, and parse stdout with the SAME
  `_probe_nvidia`/`_probe_amd_*`/etc. functions -- no new parsing code,
  only a new transport. **What Halo should do**: add
  `run_via_ssh(argv, *, host, timeout)` as a `runner`-shaped callable in
  `ollama_hw.py`, matching the existing `runner` test-seam signature
  every `_probe_*` function already accepts, so
  `probe_local_gpu_memory(runner=run_via_ssh(...))` works with no changes
  to the probe functions themselves -- exactly what `estimate_fit_for_host`'s
  existing `runner` parameter already anticipated.
- **An Ollama-side sidecar**: not a documented Ollama feature (no fetched
  page describes one) -- a Halo-side design option, not a confirmed fact.
  The smallest thing that could report free memory over HTTP is a
  single-file script that runs the same vendor probe and serves its JSON
  on a loopback port, started manually, polled the same way a
  `huggingface.local_servers` manual entry already is. **Not recommended
  as 2.0.3 scope** -- it is one more thing to install on a remote box,
  when the ssh probe above needs nothing beyond an already-running sshd
  and the vendor tool itself.
- **What Ollama itself exposes or plans to expose about GPU memory over
  its own API**: **UNCONFIRMED** -- no fetched documentation page (FAQ,
  GPU, API, context-length) mentions a roadmap item or an existing
  endpoint for raw VRAM totals; `/api/ps`'s `size`/`size_vram` (per loaded
  model, not per card) remains the only GPU-memory-shaped signal Ollama's
  own API exposes remotely, unchanged from the research doc's section 3.

## 4. The llama.cpp release matrix

Source: `github.com/ggml-org/llama.cpp/releases` (seen 2026-10-04, tag
`b11393` at fetch time -- llama.cpp tags a release on nearly every merge,
so the exact tag will have moved on by the time this is read; the NAMING
PATTERN is what matters and is stable) and `docs.github.com/en/rest/releases/assets`
(seen 2026-10-04). Full asset-name-by-platform table is its own section
below (deliverable 4, to avoid printing it twice). Headline finding:
**the inspected release ships no project-published checksum file** (no
`sha256sums.txt`/`.sha256`/`.sig`/`.asc` asset of any name) -- a
correction to the round 5c brief's own assumption ("checksum-verified
against the release's published sums"); see "Corrections" below for the
fix (GitHub's Releases API computes and serves a `digest` field per asset
itself, confirmed present in the asset object's schema, so Halo can
verify against THAT instead of a file the project doesn't publish).

## 5. KV cache quantization per backend

Sources: llama.cpp's server README (cache-type flags) and
`ggml/src/ggml-common.h` (block struct definitions, both
`github.com/ggml-org/llama.cpp`, seen 2026-10-04); `docs.ollama.com/faq.md`
(seen 2026-10-04). Full bytes-per-element table is its own section below
(deliverable 5).

- Ollama's `OLLAMA_KV_CACHE_TYPE` (confirmed directly from the FAQ, not
  just the research doc's earlier general statement): valid values
  **`f16` (default), `q8_0`, `q4_0`** -- a server-wide flag, same
  model_info-blind limitation the research doc already found (no
  per-model readback of what's actually in use).
- llama.cpp's `--cache-type-k`/`--cache-type-v` take a longer confirmed
  list: **`f32, f16, bf16, q8_0, q4_0, q4_1, iq4_nl, q5_0, q5_1`** (default
  `f16`) -- a strictly larger menu than Ollama's. llama.cpp's docs do not
  publish a per-backend (CUDA/Metal/Vulkan/ROCm/CPU) restriction table for
  which of these work where -- **UNCONFIRMED** at that level; `-fa
  [on|off|auto]`'s own default of `auto` is llama.cpp's documented way of
  letting the binary decide rather than Halo needing to know in advance.
- **Exact bytes-per-element, confirmed from `ggml-common.h`'s own struct
  definitions**, computed from the quoted C struct fields directly rather
  than from any one-line summary (one of this round's fetches produced an
  internally-inconsistent summary line for `q4_1`/`q5_1` that contradicted
  its own quoted struct fields; the table below trusts the struct fields).
- **What Halo should do**: `ollama_fit.kv_bytes_per_token`'s
  `bytes_per_elem` parameter already defaults correctly for f16
  (`KV_BYTES_PER_ELEM_F16 = 2.0`, exact, no change needed); add sibling
  constants for the other types using this round's exact figures -- see
  "Corrections" below, the single most concrete numeric fix this round
  makes to existing Halo code.

## 6. Power and thermal

Source: `docs.nvidia.com` (seen 2026-10-04, section 1); the per-turn
energy arithmetic below is a Halo-side design recommendation, not a cited
fact.

- `power.draw` (confirmed, section 1): watts, +/-5W accuracy, one number
  per card, from the same `--query-gpu` call `_probe_nvidia` already
  makes -- adding it costs one more comma-separated field, not a second
  shell-out.
- **Per-turn energy estimate, a design, not a documented API**: sample
  `power.draw` once before a turn's generation starts and once after (or
  poll every few seconds across a long generation and integrate),
  multiply by elapsed seconds for watt-seconds, divide by 3600 for
  watt-hours, multiply by the user's own configured electricity price
  (never fetched/assumed) for a cost figure. Ordinary unit arithmetic,
  not a documented API; belongs in round 5e's "saved versus cloud" meter,
  not in `ollama_hw.py`'s probe functions.
- **`powermetrics` (Apple, needs sudo)**: the brief itself says "document,
  do not use" -- nothing to add beyond repeating that; no fetch was
  attempted, consistent with the brief's own scope.
- **AMD/Intel power fields**: confirmed to exist (`amd-smi metric
  --power`; xpu-smi's `stats` "GPU Power (W)" column, both section 1) but
  not pursued further -- the brief's specific ask was NVIDIA power-draw
  sampling; the other vendors would follow identical arithmetic once
  probed.
- **What Halo should do**: add `sample_power_draw_watts(runner=None)` to
  `ollama_hw.py` (NVIDIA-only for now, `None` on any other vendor,
  matching the module's existing "degrade to `None`, never guess"
  discipline); the cost-meter arithmetic itself belongs in round 5e's own
  module.

## 7. Apple Silicon in-process inference (mlx-lm) for round 5f

Source: `github.com/ml-explore/mlx-lm`'s README and `pypi.org/project/mlx-lm`
(both seen 2026-10-04). Full comparison plan is its own section below
(deliverable 7).

- Install: `pip install mlx-lm` (or `conda install -c conda-forge
  mlx-lm`); current release **0.32.0** (seen 2026-10-04) -- combined
  sdist+wheel under 1 MB, `python_requires >=3.11`. **Not Mac-exclusive at
  the package level**: PyPI's own platform classifiers list both `MacOS`
  and `POSIX :: Linux`, and the package publishes `cuda12`/`cuda13`/`cpu`
  extras -- MLX has grown non-Apple backends. Round 5f's own framing (the
  Apple Silicon in-process backend) is still right for what Halo actually
  wants (Metal/unified memory is the differentiator), but the docs should
  not claim the package itself refuses to install anywhere else.
- `mlx_lm.server` is named as the OpenAI-compatible server command, but
  **its flags (host/port defaults) were not shown in the fetched README
  text** -- **UNCONFIRMED** at the exact-flag level; round 5f's own live
  Mac check is the practical way to confirm before Halo's code assumes
  one.
- Model format: Hub repos under the **`mlx-community`** organization,
  pre-quantized for MLX -- consistent with the existing research doc's
  framing of MLX models as one more Hub-cache-scannable source.
- Memory behaviour, confirmed directly from the README: **"will attempt
  to make them faster by wiring the memory occupied by the model and
  cache. This requires macOS 15 or higher"** for large models, with
  `sudo sysctl iogpu.wired_limit_mb=<N>` as the documented way to raise
  the ceiling -- the same sysctl section 1's Apple subsection already
  cites, now tied specifically to mlx-lm's own behavior.
- Python API: `from mlx_lm import load, generate` plus `stream_generate`
  -- confirmed to exist as an alternative to running the server at all,
  exactly the fork in the road round 5f's own brief text names.
- **What Halo should do**: nothing in `ollama_hw.py`/`ollama_fit.py`
  directly (mlx-lm is 5f-scope, not an Ollama-hardware concern) -- but
  `probe_apple_unified_memory()` (section 1, added for round 5b) is the
  SAME function round 5f's helper process needs for its own fit, so it
  belongs in the shared `ollama_hw.py` module rather than a duplicate
  inside a future `providers/mlx.py`.

## Multi-GPU fit formula (worked example)

Extends `ollama_fit.fit_estimate` (today: one `free_memory_bytes` number)
to N cards, using section 2's confirmed facts: Ollama's own rule is "fits
on one card, or spread across every configured card" (no partial
subset), and `OLLAMA_GPU_OVERHEAD` is a documented per-card reserve.

**Formula**:

```
free_total = sum(free_bytes[card] for card in cards) - overhead_per_card_bytes * len(cards)
headroom   = free_total - resident_weight_bytes      # weights count once, total
max_tokens = floor(headroom / kv_bytes_per_token)
num_ctx    = largest power of two <= max_tokens
```

A SUM across cards (not the smaller of the two, which would waste the
second card's memory), with the per-card overhead subtracted once per
card, because `OLLAMA_GPU_OVERHEAD`'s own doc comment ("set aside VRAM
per GPU") is per-card, not a single flat amount -- this is the brief's
own rule ("sum of free memory minus per-card overhead, not min") written
out in full.

**Worked example, two cards** (illustrative numbers, not any real host's):

- Card A: 24 GiB total, 2 GiB used by other processes -> 22 GiB free.
- Card B: 12 GiB total, 1 GiB used -> 11 GiB free.
- `OLLAMA_GPU_OVERHEAD` left at its documented default, 0.
- A ~30B MoE model's q4_0 weights: ~18 GiB resident.
- `free_total` = 22 + 11 = 33 GiB. `headroom` = 33 - 18 = 15 GiB.
- At this model class's own ~1.5 KB/token KV cost at q4_0 (research doc
  section 2's worked table, carried over unchanged), 15 GiB / 1.5 KB/token
  is far above any trained context ceiling, so the model's OWN trained
  `context_length` wins, not the memory arithmetic -- same conclusion the
  research doc already reached for a single card. The two-card SUM
  matters most in the other case: when the weights themselves only fit by
  spreading (18 GiB would not fit on Card B alone, and barely fits on
  Card A alone) -- the sum turns a "does not fit" into "fits, mostly on
  GPU memory," independent of what it then does to context size.
- With `OLLAMA_GPU_OVERHEAD` set to, say, 512 MiB (a user reserving
  headroom on each card): `free_total` = 33 GiB - 2 * 0.5 GiB = 32 GiB --
  a small, per-card, linear correction, not a per-model one.

**What Halo should do**: add `multi_gpu_fit_estimate` to `ollama_fit.py`
(pure arithmetic, matching `fit_estimate`'s own signature style:
keyword-only, returns `None`/`WEIGHTS_DO_NOT_FIT`/an int, never raises)
and call it from `ollama_hw.estimate_fit_for_host` when
`probe_local_gpu_memory` (section 1's NVIDIA multi-card fix) returns more
than one card; single-card hosts keep calling today's `fit_estimate`
unchanged, since the sum-of-one-card case is identical to today's
formula.

## llama.cpp asset-selection table

Source: `github.com/ggml-org/llama.cpp/releases` (tag `b11393` at fetch
time, seen 2026-10-04) and `docs.github.com/en/rest/releases/assets`
(seen 2026-10-04).

| OS | Backend | Asset name pattern | Notes |
|---|---|---|---|
| Linux (Ubuntu build) | CPU | `llama-<tag>-bin-ubuntu-x64.tar.gz` (also `-arm64`, `-s390x`) | no GPU |
| Linux | Vulkan | `llama-<tag>-bin-ubuntu-vulkan-x64.tar.gz` (also `-arm64`) | cross-vendor fallback |
| Linux | CUDA | `llama-<tag>-bin-ubuntu-cuda-<CUDA_VER>-x64.tar.gz` **+** `cudart-llama-<tag>-bin-ubuntu-cuda-<CUDA_VER>-x64.tar.gz` | two assets per CUDA version; `cudart-*` is the redistributable runtime, needed unless the host already has a matching toolkit; two `CUDA_VER` lines shipped side by side at fetch time, the newer also with an `-arm64` variant |
| Linux | ROCm/HIP | `llama-<tag>-bin-ubuntu-rocm-<ROCM_VER>-x64.tar.gz` | one ROCm version shipped at fetch time |
| Linux | SYCL | `llama-<tag>-bin-ubuntu-sycl-fp32-x64.tar.gz` / `-sycl-fp16-x64.tar.gz` | Intel GPU, two precision variants |
| Linux (Snapdragon) | OpenCL | `llama-<tag>-bin-linux-arm64-snapdragon.tar.gz` | separate from the generic arm64 CPU build |
| Android | CPU | `llama-<tag>-bin-android-arm64.tar.gz` (also `-android-arm64-snapdragon.tar.gz`) | |
| macOS | Metal (Apple Silicon) | `llama-<tag>-bin-macos-arm64.tar.gz` | Metal is on by default on macOS builds (confirmed, `docs/build.md`), not a separate asset suffix |
| macOS | Intel | `llama-<tag>-bin-macos-x64.tar.gz` | CPU-only in practice |
| macOS/iOS | framework | `llama-<tag>-xcframework.zip` | for embedding in an app, not a standalone server |
| Windows | CPU | `llama-<tag>-bin-win-cpu-x64.zip` (also `-cpu-arm64.zip`) | |
| Windows | CUDA | `llama-<tag>-bin-win-cuda-<CUDA_VER>-x64.zip` **+** `cudart-llama-bin-win-cuda-<CUDA_VER>-x64.zip` | same two-asset pattern as Linux; an `-arm64` variant also shipped for the newer CUDA line |
| Windows | Vulkan | `llama-<tag>-bin-win-vulkan-x64.zip` | |
| Windows | ROCm/HIP | `llama-<tag>-bin-win-rocm-<ROCM_VER>-x64.zip` | |
| Windows | OpenCL (Adreno) | `llama-<tag>-bin-win-opencl-adreno-arm64.zip` | ARM Windows devices |
| Windows | SYCL | `llama-<tag>-bin-win-sycl-x64.zip` | |
| any | UI | `llama-<tag>-ui.tar.gz` | web UI bundle, not a backend choice |

**Checksum file**: **none** -- the inspected release carried no
`sha256sums.txt`/`.sha256`/`.sig`/`.asc` asset of any name, contradicting
the round 5c brief's own assumption. GitHub's Releases API computes a
`digest` field per asset server-side instead (confirmed present in the
asset object's schema; the exact string format, e.g. an `algorithm:hex`
prefix, was not itself quoted from the fetched page text --
**UNCONFIRMED** at that exact-format level, confirmed only that the field
exists) -- `GET /repos/ggml-org/llama.cpp/releases/latest`'s asset list is
what to checksum against, not a file the project publishes itself.

**Driver-version rule**: do not maintain a hand-written "driver version N
needs CUDA toolkit M" lookup table. `nvidia-smi -q`'s own `CUDA Version`
line (confirmed, section 1 -- absent from `--query-gpu`, present in
`-q`'s detailed dump) already reports "the latest CUDA version supported
by the driver" directly, per docs.nvidia.com's own description of that
field (there called "CUDA UMD Version"). The rule: read that number, pick
the HIGHEST `cuda-<CUDA_VER>` asset above whose `<CUDA_VER>` is `<=` the
reported number (a driver reporting CUDA Version 12.9 picks the `cuda-12.8`
asset, not `cuda-13.4`) -- never the reverse, since a newer CUDA build's
runtime requires an at-least-as-new driver. ROCm has no confirmed
equivalent single version-probe this round (**UNCONFIRMED** whether
`rocminfo`/`amd-smi` reports an equivalent "max supported ROCm" number);
ROCm asset selection should default to the single version llama.cpp ships
and let the user override, documented as a known gap rather than guessed.

**What Halo should do**: this table is round 5c's `halo local runtime
add`/auto-fetch feature, not `ollama_hw.py`/`ollama_fit.py` -- flagged
here as the concrete input that code needs; no function in either module
changes for this item alone beyond the new `probe_nvidia_cuda_version()`
(section 1) the asset picker calls.

## KV-quantization-per-backend table

| Cache type | Confirmed where | Bytes/element (block size / 32) | Halo's current approximation |
|---|---|---|---|
| f32 | llama.cpp server README | 4.0 (definitional) | not used today |
| f16 | llama.cpp README (default); Ollama FAQ (default) | 2.0 (definitional) | `KV_BYTES_PER_ELEM_F16 = 2.0` -- exact |
| bf16 | llama.cpp server README | 2.0 (definitional) | not used today |
| q8_0 | llama.cpp README; Ollama FAQ | 34B/32 = **1.0625** | docstring says "1" -- **~6% too low** |
| q5_1 | llama.cpp server README only | 24B/32 = **0.75** | not used today |
| q5_0 | llama.cpp server README only | 22B/32 = **0.6875** | not used today |
| q4_1 | llama.cpp server README only | 20B/32 = **0.625** | not used today |
| q4_0 | llama.cpp README; Ollama FAQ | 18B/32 = **0.5625** | docstring says "0.5" -- **~12% too low** |
| iq4_nl | llama.cpp server README only | 18B/32 = **0.5625** | not used today |

Source for the exact byte counts: `ggml/src/ggml-common.h`'s own block
struct definitions (seen 2026-10-04) -- e.g. `block_q4_0 {ggml_half d;
uint8_t qs[16];}` = 2+16 = 18 bytes per 32-element block, and so on for
each type (hand-summed from the quoted struct fields, not from the
fetch's own prose summary, which was internally inconsistent for
`q4_1`/`q5_1`). llama.cpp's README documents which STRING values
`--cache-type-k`/`--cache-type-v` accept, not a per-backend support
matrix -- **UNCONFIRMED** whether every value works identically on every
backend; `-fa auto` is the documented way to sidestep the question.

**What Halo should do**: in `ollama_fit.py`, turn the single
`KV_BYTES_PER_ELEM_F16` constant into a small mapping (e.g.
`KV_BYTES_PER_ELEM = {"f16": 2.0, "q8_0": 1.0625, "q4_0": 0.5625}`,
keeping `KV_BYTES_PER_ELEM_F16` itself as an alias so no caller breaks),
using this round's exact figures rather than the rounder 1.0/0.5 the
2.0.5 brief guessed -- both existing approximations under-count memory,
the wrong direction for a budget estimate. This matters once Halo reads
back a model's actual KV cache type from somewhere (today
`kv_bytes_per_token` always assumes f16, correctly, since no per-model
readback API exists) -- the constants should be right now so a future
`ollama.hosts[].kv_cache_type` config key (mirroring `OLLAMA_KV_CACHE_TYPE`)
has correct numbers from day one.

## Apple Silicon memory-share rule (worked example)

Source for the mechanism: `github.com/ml-explore/mlx-lm`'s README (seen
2026-10-04, sections 1 and 7). Source for the SPECIFIC fraction ("about
two thirds to three quarters of RAM"): the round 5b brief text itself --
**UNCONFIRMED against any Apple or mlx-lm primary source this round**;
neither fetch returned a stated percentage, only the existence of the
`iogpu.wired_limit_mb` override and mlx-lm's "macOS 15+ to wire memory"
behavior. Treat the fraction as a carried-over estimate pending round
5b's own live calibration (`halo ollama calibrate`), exactly as that
brief's text already plans -- calibration is the ground truth; this rule
is only the wizard's first guess before any model has been loaded.

**Rule** (as the wizard/doctor should state it, pending live correction):

```
gpu_usable_bytes = iogpu.wired_limit_mb * 1 MiB      (if sysctl iogpu.wired_limit_mb is set and nonzero)
                 = hw.memsize * default_fraction      (otherwise; default_fraction ~= 0.65-0.75, UNCONFIRMED exact)
fit_estimate     = same fit_estimate() function as a discrete GPU, with
                   free_memory_bytes = gpu_usable_bytes - (RAM already held
                                        by the OS, other apps, and any OTHER
                                        loaded model's size_vram)
```

The second line reuses `ollama_fit.fit_estimate` unchanged -- unified
memory does not need a different FORMULA, only a different SOURCE for
`free_memory_bytes` (no discrete VRAM to query, so `total` comes from
`hw.memsize` and the usable slice is a fraction of it rather than the
whole thing).

**Worked example** (illustrative, not any real Mac's numbers):

- A unified-memory machine reports 36 GiB total RAM (`hw.memsize`).
- `iogpu.wired_limit_mb` unset (the common case -- per mlx-lm's README,
  the override exists specifically because large models often need MORE
  than the OS default).
- Using the midpoint of the carried-over fraction, ~0.7: `gpu_usable_bytes`
  ~= 25.2 GiB.
- The OS, shell, and whatever else is running hold, say, 4 GiB outside
  that share already (illustrative) -> `free_memory_bytes` ~= 21.2 GiB.
- A ~19 GiB q4_0-weight ~32B dense model: headroom ~= 2.2 GiB; at this
  model class's own ~2 KB/token q4_0 KV cost (research doc section 2's
  worked table) that is roughly 1.1M tokens of headroom -- again the
  model's trained context ceiling wins, not memory, exactly like the
  discrete-GPU case above.
- If the user instead runs `sudo sysctl iogpu.wired_limit_mb=30720` (30
  GiB) to push past the default share for a bigger model,
  `gpu_usable_bytes` becomes exactly 30 GiB (the override replaces the
  fraction entirely, per mlx-lm's own description of what the knob is
  for) -- the SAME `fit_estimate` call just receives a bigger number;
  nothing else about the arithmetic changes.

**What Halo should do**: implement `probe_apple_unified_memory()` in
`ollama_hw.py` (named in section 1) returning a `GpuMemory`-shaped result
with `total_bytes` from `sysctl -n hw.memsize`, and `free_bytes` computed
via the fraction rule above when `iogpu.wired_limit_mb` is unset/zero, or
directly from the limit when it is set -- wire it into
`probe_local_gpu_memory`'s existing `sys.platform == "darwin"` branch as a
SECOND attempt after `_probe_apple` returns `None` (today's only Mac
outcome, per that function's own docstring), not a replacement for it -- a
future Mac reporting a real discrete-style VRAM figure should still win
over the fraction-based estimate.

## mlx-lm comparison plan for round 5f

Source: round 5f's own brief text (the comparison is specified there;
this section is the MEASUREMENT PLAN, not a new documented fact) plus
mlx-lm's confirmed install/API shape (section 7). For `halo gym --models
hf:mlx/<model>,ol:<same model>` to be a fair comparison rather than
apples-to-oranges, the plan holds constant everything that isn't the
inference engine itself:

1. **Same weights, equivalent quantization.** An `mlx-community/<model>-4bit`
   repo and the closest matching Ollama quant tag (e.g. `q4_0`/`q4_K_M`)
   for the SAME base model -- MLX and GGUF quantize differently, so
   "4-bit" on each side is not byte-identical, and the comparison should
   say so rather than imply it is.
2. **Same context length, set explicitly on both sides.** Ollama via
   `options.num_ctx` (already Halo's own per-request control);
   `mlx_lm.server`'s equivalent flag is **UNCONFIRMED at the exact name
   this round** (the README didn't show server flags) -- round 5f's live
   Mac check needs to confirm it before the gym can pin it.
3. **Same prompt, same tool-call-or-not shape.** Reuse round 5d's
   existing gym task battery verbatim rather than inventing a second
   battery for this one comparison.
4. **Warm, not cold, for both.** Ollama's `keep_alive` and mlx-lm's own
   process both need the model already loaded before timing starts --
   `halo gym` should run (and discard) one warmup turn before scoring
   either side, if it does not already.
5. **Report memory alongside speed, not speed alone.** Pair each side's
   tokens/second with `probe_apple_unified_memory`'s own reading at the
   moment of the run (both engines share the SAME unified memory pool on
   one Mac, the one comparison axis that is naturally fair without
   adjustment) -- a faster engine that leaves less room for everything
   else is a real tradeoff, not a free win; the round 5d gym card should
   show both numbers side by side rather than ranking on speed alone.
6. **State the engine-version pair.** mlx-lm's release cadence is fast
   (0.32.0 at this round's fetch) and so is llama.cpp/Ollama's -- the gym
   card should record both engines' version strings (already planned for
   Ollama via `ollama_version` in round 5b's calibration-cache shape) so a
   stale comparison is visibly stale rather than silently misleading.

**What Halo should do**: this plan lives in round 5d's gym module and
round 5f's own `providers/mlx.py` (not yet written), not in
`ollama_hw.py`/`ollama_fit.py` -- the one piece that DOES belong in this
round's two modules is item 5's shared memory probe, already covered by
`probe_apple_unified_memory()` above; round 5f's own brief/implementation
should say that function already exists in `ollama_hw.py` by the time 5f
starts, rather than writing a second one.

## Corrections to LOCAL-MODELS-RESEARCH.md

Per the brief: corrections to that document are recorded here, as this
section, rather than by editing it directly.

1. **Section 3, AMD branch**: says `rocm-smi --showmeminfo vram` and the
   sysfs path are both "UNCONFIRMED against ROCm's own docs this round
   (not fetched)." This round fetched AMD's current docs and upgrades
   half of that: `amd-smi` (the tool AMD's own current docs actually
   document, not `rocm-smi`) is now CONFIRMED with exact flags (`amd-smi
   static --vram`, `amd-smi metric --mem-usage`, `--json`, `-g`/`--gpu`;
   `rocm.docs.amd.com/projects/amdsmi/en/latest/how-to/amdsmi-cli-tool.html`,
   seen 2026-10-04); sysfs `mem_info_vram_total`/`mem_info_vram_used` are
   now CONFIRMED from the Linux kernel's own amdgpu driver docs
   (`kernel.org/doc/html/latest/gpu/amdgpu/driver-misc.html`, seen
   2026-10-04) -- including the free-memory sibling that section's own
   text said had "no documented sibling file" (there is still no
   `_free` file, but `total - used` is now computable from two confirmed
   files). `rocm-smi`'s own exact CLI flags remain genuinely
   UNCONFIRMED, not upgraded -- ROCm's current docs site covers the
   library API, not the legacy CLI script's argument parser.
2. **Section 3, Apple branch**: says Apple Metal detection is
   "UNCONFIRMED against Apple's docs this round (not fetched)," referring
   to `system_profiler`. Still true for `system_profiler`/`sysctl
   hw.memsize` specifically (this round's Apple-developer-site fetches
   also returned no body text), but this round adds a DIFFERENT,
   confirmed fact the research doc doesn't have at all: `iogpu.wired_limit_mb`,
   confirmed from mlx-lm's own README (`github.com/ml-explore/mlx-lm`,
   seen 2026-10-04) as a real, actively-documented-by-a-maintained-project
   sysctl, not merely "community knowledge" as round 5b's own brief text
   characterized it.
3. **Section 2's worked KV-cache table caption**, and `ollama_fit.py`'s
   own module docstring, say the bytes-per-element figures are "standard
   GGML/llama.cpp accounting, not independently re-derived this round"
   ("1 for q8_0, 0.5 for q4_0"). This round DID independently re-derive
   them, from `ggml-common.h`'s own block struct definitions, and they
   are not quite what either document assumed: q8_0 is 1.0625
   bytes/element (not 1.0) and q4_0 is 0.5625 bytes/element (not 0.5) --
   see the KV-quantization table above. Both existing figures under-count
   memory use by 6-12%.
4. **Section 8 (Hugging Face, local)** doesn't mention that mlx-lm's
   package classifiers now list Linux and CUDA/CPU extras alongside
   macOS (`pypi.org/project/mlx-lm`, seen 2026-10-04) -- worth a one-line
   addition there so a future reader doesn't assume the package itself
   refuses to install off Apple Silicon (MLX the framework has grown
   non-Apple backends even though round 5f's own scope is specifically
   the Apple/Metal case).
5. **Not a correction, a reinforcement**: section 3's statement that
   `gpu.md` "did not document partial-offload mechanics, a minimum-VRAM
   threshold, or a log line" is still accurate after this round's own
   re-fetch of the same page (re-confirmed, not newly found wrong) --
   recorded here only so round 6 doesn't re-spend a fetch re-checking
   something this round already re-checked.

## What could not be confirmed

- `rocm-smi`'s own exact CLI flag syntax (the legacy tool, as opposed to
  `amd-smi`) -- ROCm's current docs site covers only the library API; the
  CLI script's own source did not surface its argument parser to this
  round's fetches.
- Intel xpu-smi's exact JSON key names (the plain-text column headers ARE
  confirmed; whether `-j` renames them was not shown verbatim in the
  fetched guide) and whether `xpu-smi stats` can report every device in
  one invocation.
- Apple's own primary documentation for `system_profiler SPDisplaysDataType`,
  `sysctl hw.memsize`, `MTLDevice.recommendedMaxWorkingSetSize`'s exact
  wording, and the "about two-thirds to three-quarters of RAM" default
  GPU-usable-share fraction -- every `developer.apple.com` fetch this
  round returned a page title only, no body text (a JavaScript-rendered
  documentation site WebFetch's HTML-to-markdown conversion could not get
  past). `iogpu.wired_limit_mb` itself IS confirmed, but only via
  mlx-lm's README, not an Apple page.
- Whether `nvidia-smi -q`'s "CUDA Version" value, or the plain-table
  header's copy of the same figure, can ever differ between the two on a
  real system (only the SHAPE of both was confirmed, from one build host,
  not a cross-version guarantee).
- Windows `Win32_VideoController.AdapterRAM`'s exact overflow behavior
  past its `uint32` ceiling (wraps, clamps, or reports zero) -- the
  datatype limit itself is confirmed; what a real over-4-GiB card reports
  is not documented on the fetched page.
- Whether a usable P/Invoke/.NET wrapper for DXGI's
  `QueryVideoMemoryInfo` is common enough to recommend -- out of scope
  for a docs-only round regardless, since no fetched Microsoft page
  documents one.
- A per-backend (CUDA/Metal/Vulkan/ROCm/CPU) support matrix for
  llama.cpp's KV-cache-quantization types and Flash Attention -- the
  server README documents the flags and their value lists, not which
  backend accepts which value; `-fa auto` sidesteps rather than answers
  this.
- The exact string format of GitHub's Releases API `digest` field
  (confirmed to exist as a field; its format, e.g. an algorithm prefix,
  was not quoted from the fetched page).
- ROCm's equivalent of `nvidia-smi -q`'s "CUDA Version" line (a single
  number for "max ROCm version this driver/card supports") -- not found
  on any fetched page this round; ROCm asset selection has no confirmed
  analogous rule.
- `mlx_lm.server`'s own flags (host/port defaults, a context-length-at-launch
  flag name) -- the README names the command but not its flag table;
  round 5f's own live Mac check is where this gets resolved.
- vLLM's and `transformers serve`'s tool-calling flag spelling and
  behavior, and Jan's default port -- unchanged from
  LOCAL-MODELS-RESEARCH.md's own list; not part of this round's GPU
  topic list, so not re-attempted here.

## Glossary

- **VRAM**: video RAM, the memory on a discrete GPU card, separate from
  system RAM.
- **Unified memory**: Apple Silicon's single physical memory pool shared
  by CPU and GPU -- there is no separate VRAM to query; "how much the GPU
  can use" is a software-enforced share of total RAM, not a hardware
  partition.
- **KV cache**: the per-token key/value tensors a transformer model keeps
  for every token in its context window, so it doesn't recompute
  attention over the whole prompt on every new token; its size scales
  linearly with context length -- the "KV bytes/token" figure this
  document and `ollama_fit.py` compute.
- **Flash Attention**: a GPU kernel technique that computes attention
  without materializing the full attention matrix, reducing memory use
  and often enabling KV-cache quantization that would otherwise be
  unsupported.
- **Tensor parallelism / tensor-split**: splitting a single model's
  weight tensors across multiple GPUs so each card holds a slice of
  every layer, as opposed to pipeline parallelism (whole layers on
  different cards) -- vLLM's `--tensor-parallel-size` and llama.cpp's
  `--tensor-split` are this mechanism, named differently.
- **Offload**: running part of a model's weights on the CPU/system RAM
  when they don't fit in VRAM -- slower, not blocked, per house policy;
  `/api/ps`'s `size` vs `size_vram` difference is this.
- **Digest (GitHub Releases API)**: a server-computed checksum GitHub
  attaches to each release asset's object in its REST API response,
  independent of whether the project itself publishes a checksum file.
- **sysfs**: the Linux kernel's virtual filesystem (under `/sys`) that
  exposes kernel/driver state as plain text files -- `mem_info_vram_total`
  is one such file, under each GPU's own
  `/sys/class/drm/card*/device/` directory.
- **WMI / CIM**: Windows Management Instrumentation (now "Common
  Information Model" in Microsoft's current docs) -- the subsystem
  `Get-CimInstance`/`Get-WmiObject` query, including `Win32_VideoController`.
- **DXGI**: DirectX Graphics Infrastructure, the low-level Windows API
  layer (below Direct3D) that manages GPU adapters and swap chains,
  including the per-process video-memory-budget API.
