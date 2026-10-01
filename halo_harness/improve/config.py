"""halo_harness.improve.config -- ImproveConfig: `~/.halo/config.json`
key `"improve"`. Every field has a built-in default (nothing here can be
"missing"); `halo config set improve.model or:...` (theme.py's own
dotted-path `set_config_value`) is the one documented way to change it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

_DEFAULT_HINT_THRESHOLD = {"repairs": 3, "edit_failures": 2, "loop_breaker": 1}


@dataclass
class ImproveConfig:
    enabled: bool = True
    hint: bool = True
    model: Optional[str] = None  # config improve.model -> else the small model -> else the session model
    since_days: int = 7
    max_candidates: int = 8
    hint_threshold: dict = field(default_factory=lambda: dict(_DEFAULT_HINT_THRESHOLD))


def load_improve_config() -> ImproveConfig:
    """Reads `~/.halo/config.json`'s `"improve"` key fresh every
    call (matches `theme.load_config`'s own "read fresh" contract) --
    missing keys fall back to `ImproveConfig`'s own defaults; an
    unrecognized/malformed value for a field is ignored (falls back to the
    default) rather than raising, since a hand-edited config.json must
    never be able to crash the harness."""
    from halo_harness.theme import load_config

    raw = load_config().get("improve")
    raw = raw if isinstance(raw, dict) else {}
    cfg = ImproveConfig()
    if isinstance(raw.get("enabled"), bool):
        cfg.enabled = raw["enabled"]
    if isinstance(raw.get("hint"), bool):
        cfg.hint = raw["hint"]
    model = raw.get("model")
    cfg.model = model if isinstance(model, str) and model else None
    since_days = raw.get("since_days")
    if isinstance(since_days, int) and not isinstance(since_days, bool) and since_days > 0:
        cfg.since_days = since_days
    max_candidates = raw.get("max_candidates")
    if isinstance(max_candidates, int) and not isinstance(max_candidates, bool) and max_candidates > 0:
        cfg.max_candidates = min(max_candidates, 8)
    threshold = raw.get("hint_threshold")
    if isinstance(threshold, dict):
        merged = dict(_DEFAULT_HINT_THRESHOLD)
        for k in ("repairs", "edit_failures", "loop_breaker"):
            v = threshold.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                merged[k] = v
        cfg.hint_threshold = merged
    return cfg


def since_str(cfg: ImproveConfig) -> str:
    """`cfg.since_days` (an int) -> the "7d"/"30d"/"all" vocabulary
    `halo_harness.telemetry.scan` accepts -- any OTHER day count still works
    (telemetry.scan defaults unknown strings to 7 days via its own
    `_SINCE_DAYS` table), but the two round values get their exact label
    rather than silently falling back to 7."""
    if cfg.since_days == 30:
        return "30d"
    if cfg.since_days <= 0:
        return "all"
    return "7d" if cfg.since_days == 7 else f"{cfg.since_days}d"
