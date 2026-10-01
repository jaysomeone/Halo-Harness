"""halo_harness.improve.dismissed -- the `d` (dismiss forever) key's own
persistence: `~/.halo/improve/dismissed.json`, a flat list of
`candidate_hash` (sha256 of kind+scope+path+body) values. A dismissed
candidate is never re-shown by a LATER /improve run, across restarts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def dismissed_path() -> Path:
    from halo_harness.config.paths import bridge_home
    return bridge_home() / "improve" / "dismissed.json"


def load_dismissed() -> "set[str]":
    p = dismissed_path()
    try:
        if not p.exists():
            return set()
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def add_dismissed(hash_hex: str) -> Path:
    p = dismissed_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    current = load_dismissed()
    current.add(hash_hex)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(sorted(current), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def is_dismissed(candidate, dismissed: "set[str] | None" = None) -> bool:
    from halo_harness.improve.apply import candidate_hash
    dismissed = dismissed if dismissed is not None else load_dismissed()
    return candidate_hash(candidate) in dismissed
