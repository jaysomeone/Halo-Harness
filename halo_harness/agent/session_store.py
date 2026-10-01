"""rolo_claude.agent.session_store -- the harness's OWN transcript store,
under ~/.rolo-claude/sessions/<slug>/<session_id>.jsonl (plan D8). Never
touches Claude Code's own ~/.claude/projects/ transcripts -- read-only
there, if ever (H0 doesn't read them at all).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Optional

from rolo_claude.config.paths import bridge_home, project_slug


class SessionStore:
    def __init__(self, cwd, session_id: Optional[str] = None):
        self.cwd = cwd
        self.slug = project_slug(cwd)
        self.session_id = session_id or uuid.uuid4().hex
        self.dir = bridge_home() / "sessions" / self.slug
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{self.session_id}.jsonl"

    def _append(self, record: dict) -> None:
        record.setdefault("ts", time.time())
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # transcript storage is best-effort; never break a turn over it

    def append_meta(self, meta: dict) -> None:
        self._append({"type": "meta", **meta})

    def append_message(self, message: dict) -> None:
        self._append({"type": "message", "message": message})

    def append_usage(self, usage: dict, cost_usd) -> None:
        self._append({"type": "usage", "usage": usage, "cost_usd": cost_usd})

    def read_all(self) -> list:
        if not self.path.exists():
            return []
        records = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return records

    @classmethod
    def latest_for_cwd(cls, cwd) -> "Optional[SessionStore]":
        """The most recently modified session file for this cwd's slug, or
        None if there isn't one yet -- backs a future `--continue`."""
        slug = project_slug(cwd)
        session_dir = bridge_home() / "sessions" / slug
        if not session_dir.is_dir():
            return None
        candidates = sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            return None
        session_id = candidates[0].stem
        return cls(cwd, session_id=session_id)
