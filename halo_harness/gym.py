"""halo_harness.gym -- Halo 2.0.3 round 5d: the model gym on THIS machine
(`plans/2.0.3-ollama-round2-brief.md` "Round 5d", `plans/ROADMAP.md`'s
"ADDED 2026-10-04" section). The gym runs a fixed task battery (see
`gym_tasks.py`) against each local model this hardware can actually reach,
through the real request/decode path (`gym_run.py`), and scores it per the
four raw metrics below -- never a vendor claim, always a measurement.

This module owns: the shapes every other gym module reads/writes (never
redefined twice), the result-file store (`~/.halo/gym/<host-slug>/
<digest>.json`), and the per-model card `halo gym show` prints. Splitting
the gym this way (shapes+store here, the battery in `gym_tasks.py`,
orchestration in `gym_run.py`, scoring-to-roles in `gym_propose.py`, CLI
glue in `gym_cli.py`) mirrors the existing `providers/ollama*.py` split --
no module here grows past the house 250-line-per-write habit.

**What each metric means (plain sentences, repeated in docs/MODELS.md)**:
  - `tool_call_accuracy`: out of N real tool-call attempts, how many came
    back schema-valid on the FIRST try (`valid_first_try / attempted`) --
    repaired calls (`valid_after_repair`) and outright failures (`failed`)
    are counted but never blended into this headline number, so a model
    that needs the repair loop a lot never looks as good as one that
    doesn't.
  - `edit_success`: out of N attempts, how many produced a real Edit tool
    call whose arguments were applied to a scratch fixture file and left
    it reading EXACTLY as expected (`succeeded / attempted`).
  - `context_recall`: out of N attempts, how many answers contained a
    short "needle" fact planted at about 12% depth of a prompt sized to
    the model's own fitted context (`succeeded / attempted`) -- a direct
    test of whether the context window this host actually grants the
    model is usable, not just advertised.
  - `instruction_adherence`: out of N attempts at the plain-sentence reply
    rules (one word when asked for one word, no preamble when asked for
    none), how many were followed exactly (`succeeded / attempted`).
  - `tokens_per_second` / `prefill_seconds`: averaged across every real
    turn the battery sent this model, from the SAME `eval_count`/
    `eval_duration`/`prompt_eval_duration` fields (nanoseconds)
    `agent/loop.py::_record_ollama_throughput` already reads for the
    status bar -- identical arithmetic, so a gym card and a live session
    never disagree about what "fast" means on this host.

Any ratio is `None` (never 0.0) when `attempted` is 0 -- "never measured"
and "measured and failed every time" are different facts, and `gym_propose.
py`'s composite scoring treats them differently (a `None` term drops out
of that role's weighted average instead of zeroing it).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# Brief item 1: "--quick halves N". Context recall is far more expensive
# per attempt (a prompt sized to the model's whole fitted context) than the
# other three tasks, so it gets its own, smaller base count -- still halved
# by --quick, same rule, just starting from a cheaper number.
FULL_N = 6
QUICK_N = 3
CONTEXT_RECALL_N_FULL = 2
CONTEXT_RECALL_N_QUICK = 1

# Halo 2.0.3 round 5d: the four roles `halo gym propose` fills are exactly
# `roles.VRAM_AWARE_ROLE_NAMES` (brief: "small, researcher, judge,
# subagent_default") -- re-exported here rather than redefined, so the two
# modules can never quietly disagree about which roles are "supporting."
from halo_harness.roles import VRAM_AWARE_ROLE_NAMES as SUPPORTING_ROLES  # noqa: E402

GYM_DIR_NAME = "gym"


def battery_n(quick: bool) -> int:
    return QUICK_N if quick else FULL_N


def context_recall_n(quick: bool) -> int:
    return CONTEXT_RECALL_N_QUICK if quick else CONTEXT_RECALL_N_FULL


# Fix pass (2026-10-04 live-run finding, qwen3.8:27b): a thinking-by-
# default model can spend its ENTIRE small output budget on reasoning
# before emitting any real content -- `turn.text` then comes back
# genuinely EMPTY (never thinking text mistaken for the answer; see
# `gym_send.TurnResult`'s own docstring), which scored as a flat 0% with
# no way to tell why. Every per-attempt excerpt below (truncated to this
# many characters) is what makes that diagnosable from the saved JSON
# alone, and what `--show-replies` prints live.
SAMPLE_EXCERPT_CHARS = 200


def sample_excerpt(text: "Optional[str]") -> str:
    text = (text or "").strip()
    return text[:SAMPLE_EXCERPT_CHARS]


@dataclass
class ToolCallAccuracy:
    """Tool-call accuracy, repair rounds counted SEPARATELY (brief item 1:
    "counting repair rounds separately") -- `score` (the headline number)
    is `valid_first_try / attempted` ONLY; `repaired_score` is the more
    forgiving `(valid_first_try + valid_after_repair) / attempted`, shown
    alongside but never substituted for `score`. `samples`: one `sample_
    excerpt` per attempt (the tool call's own short repr, or the plain
    text reply when none was made) -- so a 0% is diagnosable, never a
    bare number."""
    attempted: int = 0
    valid_first_try: int = 0
    valid_after_repair: int = 0
    failed: int = 0
    repair_rounds: int = 0
    samples: "list" = field(default_factory=list)

    @property
    def score(self) -> Optional[float]:
        return (self.valid_first_try / self.attempted) if self.attempted else None

    @property
    def repaired_score(self) -> Optional[float]:
        if not self.attempted:
            return None
        return (self.valid_first_try + self.valid_after_repair) / self.attempted


@dataclass
class RatioScore:
    """The shared shape for edit_success/context_recall/instruction_
    adherence -- a plain `succeeded / attempted` ratio, `None` (never 0.0)
    when nothing was attempted. `samples`: one `sample_excerpt` of the
    actual reply per attempt -- see `ToolCallAccuracy.samples`."""
    attempted: int = 0
    succeeded: int = 0
    samples: "list" = field(default_factory=list)

    @property
    def score(self) -> Optional[float]:
        return (self.succeeded / self.attempted) if self.attempted else None


@dataclass
class GymResult:
    """One model's full gym card -- `dataclasses.asdict` IS the on-disk
    JSON shape (`to_dict`/`from_dict` below), so there is exactly one
    place that shape is defined. `digest`/`quantization`/`ollama_version`
    are whatever the catalog/version probe returned at MEASUREMENT time
    (brief item 1: "with ... the Ollama version, quant and fitted context
    they were measured at") -- never re-read live by a later `show`/
    `propose`, which only ever reads this file."""
    model: str
    model_ref: str
    host_name: str
    host_url: str
    digest: Optional[str] = None
    quantization: Optional[str] = None
    fitted_context: Optional[int] = None
    ollama_version: Optional[str] = None
    started_at: float = 0.0
    finished_at: float = 0.0
    quick: bool = False
    turns: int = 0
    errors: "list[str]" = field(default_factory=list)
    tool_call_accuracy: ToolCallAccuracy = field(default_factory=ToolCallAccuracy)
    edit_success: RatioScore = field(default_factory=RatioScore)
    context_recall: RatioScore = field(default_factory=RatioScore)
    instruction_adherence: RatioScore = field(default_factory=RatioScore)
    tokens_per_second: Optional[float] = None
    prefill_seconds: Optional[float] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("tool_call_accuracy", "edit_success", "context_recall", "instruction_adherence"):
            metric = getattr(self, key)
            d[key]["score"] = metric.score
        d["tool_call_accuracy"]["repaired_score"] = self.tool_call_accuracy.repaired_score
        return d


def overall_score(result: dict) -> Optional[float]:
    """A plain, unweighted mean of the four 0..1 ratio scores -- used only
    for the picker's one-glance suffix and `gym show`'s headline line;
    `gym_propose.py`'s per-role composite is the real ranking signal and
    uses its own weights, never this. `None` when every metric is `None`
    (nothing was ever measured for this model) rather than a misleading 0."""
    scores = []
    for key in ("tool_call_accuracy", "edit_success", "context_recall", "instruction_adherence"):
        metric = (result or {}).get(key) or {}
        s = metric.get("score")
        if isinstance(s, (int, float)) and not isinstance(s, bool):
            scores.append(s)
    return (sum(scores) / len(scores)) if scores else None


_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def _slug(raw: Optional[str], *, fallback: str) -> str:
    text = (raw or "").strip().lower()
    text = text.replace("sha256:", "")
    text = _SLUG_RE.sub("-", text).strip("-")
    return text or fallback


def host_slug(host_name: Optional[str]) -> str:
    return _slug(host_name, fallback="default")


def digest_slug(digest: Optional[str]) -> str:
    return _slug(digest, fallback="nodigest")


def gym_dir(state_dir) -> Path:
    return Path(state_dir) / GYM_DIR_NAME


def result_path(state_dir, host_name: Optional[str], digest: Optional[str]) -> Path:
    return gym_dir(state_dir) / host_slug(host_name) / f"{digest_slug(digest)}.json"


def save_result(state_dir, result: dict) -> Path:
    """Atomic tmp+replace write, the same idiom every other gym-adjacent
    store in this codebase uses (`ollama_calibrate.save_fit_store`,
    `ollama_capability.save_capability_cache`)."""
    path = result_path(state_dir, result.get("host_name"), result.get("digest"))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    import os
    os.replace(tmp, path)
    return path


def load_result(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def iter_results(state_dir) -> "list[dict]":
    """Every saved gym result on this machine, newest-measured-first --
    `[]` on a fresh box (never raises, same "a lost/missing store just
    means nothing is known yet" contract as every other `~/.halo/*.json`
    reader)."""
    base = gym_dir(state_dir)
    out: "list[dict]" = []
    try:
        for p in base.glob("*/*.json"):
            if p.name.endswith(".tmp"):
                continue
            data = load_result(p)
            if data is not None:
                out.append(data)
    except OSError:
        return []
    out.sort(key=lambda r: r.get("finished_at") or 0, reverse=True)
    return out


def find_results_for_model(state_dir, model_or_ref: str) -> "list[dict]":
    """Every saved result whose own `model`/`model_ref` names
    `model_or_ref` -- matched the SAME tag-aware way the Ollama provider
    matches a catalog row (`providers.ollama.ollama_names_match`), so an
    untagged lookup (`qwen3-coder:30b` vs. a stored `qwen3-coder:30b:
    latest`) still finds its own entry."""
    from halo_harness.providers.ollama import ollama_names_match
    bare = model_or_ref.split("@", 1)[0]
    for prefix in ("ol:", "hf:local/", "hf:mlx/"):
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
            break
    return [r for r in iter_results(state_dir)
            if ollama_names_match(r.get("model"), bare) or r.get("model_ref") == model_or_ref]


def picker_score_suffix(model_ref: str, state_dir=None) -> str:
    """Halo 2.0.3 round 5d (brief item 2: "the picker shows the gym score
    beside a model when one exists"): a short, LOCAL-FILE-ONLY lookup
    (never a network call -- safe to call from the picker's own
    synchronous row-building loop) -- "" when nothing is on file for this
    ref yet. Prefers the newest-measured result when more than one host
    scored the same model."""
    if state_dir is None:
        from halo_harness.config.paths import bridge_home
        state_dir = bridge_home()
    try:
        rows = find_results_for_model(state_dir, model_ref)
    except Exception:
        return ""
    if not rows:
        return ""
    score = overall_score(rows[0])
    if score is None:
        return ""
    tps = rows[0].get("tokens_per_second")
    tps_text = f", {tps:.0f} tok/s" if isinstance(tps, (int, float)) else ""
    return f"  gym {score:.2f}{tps_text}"


def format_card(result: dict, *, show_replies: bool = False) -> str:
    """`halo gym show [model]`'s per-model card -- plain lines, never a
    block/table (house style for anything printed from a CLI command).
    `show_replies` (`--show-replies`, fix pass): appends each metric's
    saved `samples` excerpts right under its own line -- off by default
    (the samples are ALWAYS stored in the result JSON regardless; this
    only controls whether the CLI also prints them)."""
    tca = result.get("tool_call_accuracy") or {}
    edit = result.get("edit_success") or {}
    ctx = result.get("context_recall") or {}
    instr = result.get("instruction_adherence") or {}

    def _replies(m: dict) -> "list[str]":
        samples = m.get("samples") or []
        return [f"    reply: {s!r}" for s in samples] if show_replies and samples else []

    def _pct(m: dict) -> str:
        s = m.get("score")
        return f"{s * 100:.0f}% ({m.get('succeeded', m.get('valid_first_try', 0))}/{m.get('attempted', 0)})" \
            if isinstance(s, (int, float)) else f"n/a (0/{m.get('attempted', 0)})"

    lines = [
        f"{result.get('model_ref', '?')}  (host {result.get('host_name', '?')}, "
        f"digest {digest_slug(result.get('digest'))[:12]}, quant {result.get('quantization') or '?'}, "
        f"fitted context {result.get('fitted_context') or '?'}, Ollama {result.get('ollama_version') or '?'})",
        f"  measured at {time.strftime('%Y-%m-%d %H:%M', time.localtime(result.get('finished_at') or 0))}"
        f"{' (--quick)' if result.get('quick') else ''}, {result.get('turns', 0)} real turn(s) sent",
        f"  tool-call accuracy: {_pct(tca)}"
        + (f", +{tca.get('valid_after_repair', 0)} more after repair ({tca.get('repair_rounds', 0)} repair round(s))"
           if tca.get("repair_rounds") else ""),
        *_replies(tca),
        f"  edit success:       {_pct(edit)}",
        *_replies(edit),
        f"  context recall:     {_pct(ctx)}",
        *_replies(ctx),
        f"  instruction adherence: {_pct(instr)}",
        *_replies(instr),
        f"  throughput: {result.get('tokens_per_second') if result.get('tokens_per_second') is not None else 'n/a'} tok/s, "
        f"prefill {result.get('prefill_seconds') if result.get('prefill_seconds') is not None else 'n/a'}s",
    ]
    overall = overall_score(result)
    if overall is not None:
        lines.append(f"  overall (unweighted mean of the four ratios): {overall:.2f}")
    if result.get("errors"):
        lines.append(f"  errors during the run (non-fatal): {'; '.join(result['errors'])}")
    return "\n".join(lines)
