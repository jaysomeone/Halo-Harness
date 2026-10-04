"""halo_harness.providers.learned_rules -- the LEARNED per-endpoint
`reasoning_effort_with_tools` rule (1.0.1 part 2, item 22 remainder): when a
live 400 proves a Databricks endpoint with no `model_table.json` row of its
own ALSO rejects `reasoning_effort` alongside tools (the same shape the
gpt-6 table rows already document), the harness remembers "none" for that
exact endpoint -- in `Session.provider_profile` for the REST OF THIS
SESSION (so every later step sends it correctly the first time, never
paying for the failing request again), and in this per-endpoint cache on
disk so a FUTURE session against the same endpoint never has to re-learn
it live either.

Persisted at `<state_dir>/learned-rules.json`:
`{"<provider>:<model>": {"reasoning_effort_with_tools": "none"}}`. A
`model_table.json` row (including an explicit `null` override there)
always wins over this cache -- learning only ever fills a gap the static
table hasn't been updated for yet, it never overrides a verified row (see
`providers.profiles.resolve_profile`'s own `row.get(..., learned)` order).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

_LOCK = threading.Lock()

# 2.0.2 review finding 32: a learned "tools_rejected" row never expired --
# one transient 400 (a flaky endpoint, a provider-side rollout) made that
# endpoint permanently unusable for tools until someone hand-edited
# learned-rules.json. A day is long enough that this never re-pays the
# round trip on every session against an endpoint that genuinely doesn't
# support tools, short enough that a since-fixed/transient one self-heals
# with no action needed (never add a safety/permanent-block knob here --
# describe behaviour, let it recover on its own).
TOOLS_REJECTED_TTL_S = 24 * 60 * 60


def _path(state_dir) -> Path:
    return Path(state_dir) / "learned-rules.json"


def load_learned_rules(state_dir) -> dict:
    """Never raises -- a missing/corrupt cache degrades to "nothing
    learned yet", exactly like every other cache file this harness reads."""
    try:
        data = json.loads(_path(state_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _key(provider: str, model: str) -> str:
    return f"{provider}:{model}"


def learned_reasoning_effort_with_tools(state_dir, provider: str, model: str) -> Optional[str]:
    row = load_learned_rules(state_dir).get(_key(provider, model))
    return row.get("reasoning_effort_with_tools") if isinstance(row, dict) else None


def learn_reasoning_effort_with_tools(state_dir, provider: str, model: str, value: str) -> None:
    """Idempotent, best-effort (never raises -- a write failure just means
    the NEXT session re-learns the same fact live, same as before this
    feature existed)."""
    _learn(state_dir, provider, model, "reasoning_effort_with_tools", value)


def _learn(state_dir, provider: str, model: str, field: str, value) -> None:
    """Shared by every learned-rule writer below: merge one field into
    this endpoint's row and save, skipping the disk write when the value
    is already what's cached (same idempotence every existing caller
    already relied on)."""
    with _LOCK:
        rules = load_learned_rules(state_dir)
        key = _key(provider, model)
        row = dict(rules.get(key)) if isinstance(rules.get(key), dict) else {}
        if row.get(field) == value:
            return  # already learned -- avoid a pointless disk write every step
        row[field] = value
        rules[key] = row
        path = _path(state_dir)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(path.name + f".tmp{os.getpid()}")
            tmp_path.write_text(json.dumps(rules, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp_path, path)
        except OSError:
            pass


def learned_tools_rejected(state_dir, provider: str, model: str) -> bool:
    """Halo 2.0.2 round 5 (Qwen-at-work brief, item 4): True once a prior
    LIVE request against this exact endpoint already proved it rejects
    `tools` outright (`providers.errors.is_tools_rejected_message` on a
    real 400/404 -- see `agent/loop.py`'s `_step`, the call site that
    writes this). `resolve_profile`'s `tools_supported` consults this for
    any Databricks endpoint with no `model_table.json` row/name-pattern
    classification at all, the same gap-filling role `learned_
    reasoning_effort_with_tools` already plays for that field -- "the
    mechanism 2.0.3 extends" per the round 5 brief.

    2.0.2 review finding 32: expires after `TOOLS_REJECTED_TTL_S` (an
    older row with no `tools_rejected_at` timestamp at all -- written
    before this fix existed -- is treated as expired too, so it is
    re-learned fresh on the very next live call rather than staying
    stuck forever)."""
    row = load_learned_rules(state_dir).get(_key(provider, model))
    if not (isinstance(row, dict) and row.get("tools_rejected")):
        return False
    learned_at = row.get("tools_rejected_at")
    if not isinstance(learned_at, (int, float)) or (time.time() - learned_at) > TOOLS_REJECTED_TTL_S:
        return False
    return True


def learn_tools_rejected(state_dir, provider: str, model: str) -> None:
    """Idempotent-ish, best-effort: always refreshes `tools_rejected_at`
    (unlike `_learn`'s own skip-if-unchanged shortcut, which would leave
    a stale timestamp in place and defeat the TTL above) -- a write
    failure just means the NEXT session re-learns the same fact live,
    same as before this feature existed."""
    with _LOCK:
        rules = load_learned_rules(state_dir)
        key = _key(provider, model)
        row = dict(rules.get(key)) if isinstance(rules.get(key), dict) else {}
        row["tools_rejected"] = True
        row["tools_rejected_at"] = time.time()
        rules[key] = row
        path = _path(state_dir)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(path.name + f".tmp{os.getpid()}")
            tmp_path.write_text(json.dumps(rules, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp_path, path)
        except OSError:
            pass
