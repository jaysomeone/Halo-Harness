"""rolo_claude.agent.sessions -- `--continue`/`--resume`/`--session-id`/
`--fork-session` resolution (H6 scope D) for print mode (headless.py).
The TUI's own `--resume` picker surface is `Controller.list_sessions`/
`resume` (U5); this module is the PRINT-MODE side (no picker UI, so
`--resume` with no value falls back to the latest session) plus
`index.json` bookkeeping shared by both.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Optional

from rolo_claude.config.paths import bridge_home, project_slug


def sessions_dir(cwd) -> Path:
    return bridge_home() / "sessions" / project_slug(cwd)


def is_valid_session_id(value: str) -> bool:
    """`--session-id <uuid>` [bin sec.1]: must parse as a UUID -- rolo-
    claude's own ids are `uuid.uuid4().hex` (no dashes), which `uuid.UUID`
    accepts just as well as the dashed form, so both spellings validate."""
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def resolve_continue(cwd) -> Optional[str]:
    """`-c/--continue`: the most-recently-modified session id for `cwd`,
    or None when this project has no sessions yet."""
    directory = sessions_dir(cwd)
    if not directory.is_dir():
        return None
    candidates = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0].stem if candidates else None


def _read_nodes(path: Path) -> list:
    out = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def _first_user_text(nodes: list) -> str:
    for node in nodes:
        if node.get("type") == "user":
            for block in node.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text", "")
    return ""


def _title_for(nodes: list) -> str:
    for node in reversed(nodes):
        if node.get("type") == "meta" and isinstance(node.get("title"), str) and node["title"]:
            return node["title"]
    return ""


def resolve_resume(cwd, value: Optional[str]) -> "tuple[Optional[str], Optional[str]]":
    """`(session_id, error)` for `-r/--resume [value]` [bin: "session ID,
    or name, or transcript.jsonl"]: an exact id (its `.jsonl` exists under
    this project), a path to a `.jsonl` transcript (id = its filename), or
    a case-insensitive substring matched against a stored title / the
    first user message / the id itself (most-recently-modified match
    wins). Empty/None `value` means "no picker in print mode" -- falls
    back to the latest session, same as `--continue`."""
    if not value:
        latest = resolve_continue(cwd)
        return (latest, None) if latest else (None, "no sessions found for this directory")

    candidate_path = Path(value)
    if candidate_path.suffix == ".jsonl" and candidate_path.is_file():
        return candidate_path.stem, None

    directory = sessions_dir(cwd)
    direct = directory / f"{value}.jsonl"
    if direct.is_file():
        return value, None

    if not directory.is_dir():
        return None, f"no session matches {value!r}"

    needle = value.lower()
    for path in sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        nodes = _read_nodes(path)
        title, summary = _title_for(nodes), _first_user_text(nodes)
        if needle in title.lower() or needle in summary.lower() or needle in path.stem.lower():
            return path.stem, None
    return None, f"no session matches {value!r}"


def list_sessions(cwd) -> list:
    """`[{id, mtime, title, summary}, ...]`, newest first -- a headless
    (`/resume` with no argument, in `-p`) equivalent of `Controller.
    list_sessions` (the TUI's own directory listing for its interactive
    picker), enriched with `index.json` titles."""
    directory = sessions_dir(cwd)
    out: list = []
    if not directory.is_dir():
        return out
    index = load_index(cwd)
    for path in sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        sid = path.stem
        entry = index.get(sid, {})
        title = entry.get("title") or _title_for(_read_nodes(path))
        out.append({"id": sid, "mtime": path.stat().st_mtime, "title": title,
                     "summary": entry.get("first_prompt", "")})
    return out


def fork_session(cwd, source_id: str) -> str:
    """Copy `source_id`'s full log into a brand-new session id (brief D:
    "copy the log under a new id before appending") and return the new
    id -- a plain byte-for-byte file copy (SessionLog is append-only, so
    this is always safe for a CLI-scoped single-writer use)."""
    directory = sessions_dir(cwd)
    directory.mkdir(parents=True, exist_ok=True)
    src = directory / f"{source_id}.jsonl"
    new_id = uuid.uuid4().hex
    dst = directory / f"{new_id}.jsonl"
    if src.is_file():
        dst.write_bytes(src.read_bytes())
    return new_id


# ---- index.json (brief D: "first prompt/started/last/turns/cost") ---------

def index_path(cwd) -> Path:
    return sessions_dir(cwd) / "index.json"


def load_index(cwd) -> dict:
    try:
        return json.loads(index_path(cwd).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_index_entry(cwd, session_id: str, **fields) -> None:
    """Merge `fields` into `index.json[session_id]`. Best-effort -- a
    write failure never raises (matches SessionLog's own "best-effort
    disk write" contract, same reasoning: a full disk must not crash a
    session that is otherwise working fine)."""
    path = index_path(cwd)
    data = load_index(cwd)
    entry = data.get(session_id, {})
    entry.update({k: v for k, v in fields.items() if v is not None})
    data[session_id] = entry
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    except OSError:
        pass


def record_session_start(cwd, session_id: str, first_prompt: str) -> None:
    update_index_entry(cwd, session_id, first_prompt=(first_prompt or "")[:200],
                        started=time.time(), last=time.time(), turns=0)


def record_session_turn(cwd, session_id: str, *, cost_usd: Optional[float] = None) -> None:
    entry = load_index(cwd).get(session_id, {})
    update_index_entry(cwd, session_id, last=time.time(), turns=int(entry.get("turns", 0)) + 1, cost_usd=cost_usd)


def set_title(cwd, session_id: str, title: str) -> None:
    """`/rename`'s own write path -- `index.json`'s `title` field (read by
    `Controller.list_sessions`'s picker and `/resume`'s own listing)."""
    update_index_entry(cwd, session_id, title=(title or "").strip()[:80])


def generate_title(first_user_text: str, *, small_model_caller=None) -> str:
    """A short session title (brief F: "session titles via the small
    model" + `/rename`'s default) -- `small_model_caller(prompt, timeout_s)
    -> str` is normally `hooks.build_prompt_caller(small_ref, small_profile,
    creds, state_dir)` bound to the SMALL model (headless.py builds it once
    per session start); None (a bare Session, a unit test, or no small
    model configured) falls back to a deterministic first-line-of-prompt
    title so one always exists."""
    text = (first_user_text or "").strip()
    fallback = (text.splitlines()[0][:60] if text else "New session") or "New session"
    if small_model_caller is None or not text:
        return fallback
    try:
        prompt = ("Summarise the following user request as a short session title: 3-6 words, no "
                   "quotes, no trailing punctuation, plain text only.\n\n" + text[:2000])
        title = (small_model_caller(prompt, 15.0) or "").strip().strip('"').strip("'")
        title = title.splitlines()[0].strip() if title else ""
        return title[:60] if title else fallback
    except Exception:
        return fallback
