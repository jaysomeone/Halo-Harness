"""rolo_claude.providers.learned_rules -- the LEARNED per-endpoint
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
from pathlib import Path
from typing import Optional

_LOCK = threading.Lock()


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
    with _LOCK:
        rules = load_learned_rules(state_dir)
        key = _key(provider, model)
        row = dict(rules.get(key)) if isinstance(rules.get(key), dict) else {}
        if row.get("reasoning_effort_with_tools") == value:
            return  # already learned -- avoid a pointless disk write every step
        row["reasoning_effort_with_tools"] = value
        rules[key] = row
        path = _path(state_dir)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(path.name + f".tmp{os.getpid()}")
            tmp_path.write_text(json.dumps(rules, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp_path, path)
        except OSError:
            pass
