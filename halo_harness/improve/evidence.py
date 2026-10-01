"""halo_harness.improve.evidence -- H10 Part B1: failure clusters from the
telemetry scan of the last `--since` (default 7d; current project slug
unless `--all-projects`), feeding `halo_harness.improve.draft`'s ONE
drafting model call.

Six cluster classes, each keeping <= 6 excerpts of <= 600 chars with
`session_id#seq` references and a boolean `from_tool_output` (the excerpt
originated in a tool_result -- WebFetch/MCP/file/a tool's own error text --
rather than in the user's own words): repeated tool errors of one
error_class per tool; repair-layer hits per model; loop-breaker trips; user
corrections (a `user` node right after a tool error, or an assistant/user
text starting with a plain correction cue -- one constant list, never a
classifier); Read ENOENT/wrong-cwd patterns; the same 3-tool sequence
recurring in >= 3 sessions (skill candidates). `from_tool_output` is shown
on an ImproveCard as "derived from tool output", nothing more -- it is
never used to filter, block or reweight a cluster.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from halo_harness import telemetry

MAX_EXCERPTS = 6
MAX_EXCERPT_CHARS = 600

# (d): a PLAIN constant list, never a classifier -- matched against the
# first few words of a user/assistant text, case-insensitively.
CORRECTION_CUES = ("no", "don't", "stop", "wrong", "instead", "actually", "not that", "use ")

_MIN_TOOL_ERROR_REPEATS = 2
_MIN_REPAIR_REPEATS = 2
_MIN_SEQUENCE_SESSIONS = 3


@dataclass
class Excerpt:
    session_id: str
    seq: Optional[int]
    text: str
    from_tool_output: bool = False

    @property
    def ref(self) -> str:
        return f"{self.session_id}#{self.seq if self.seq is not None else '?'}"


@dataclass
class Cluster:
    cluster_id: str
    kind: str  # tool_error | repair | loop_breaker | user_correction | read_enoent | tool_sequence
    label: str
    count: int = 0
    excerpts: "list[Excerpt]" = field(default_factory=list)
    sessions: "set[str]" = field(default_factory=set)

    def add(self, ex: Excerpt) -> None:
        self.count += 1
        self.sessions.add(ex.session_id)
        if len(self.excerpts) < MAX_EXCERPTS:
            self.excerpts.append(ex)

    def to_dict(self) -> dict:
        return {
            "cluster_id": self.cluster_id, "kind": self.kind, "label": self.label, "count": self.count,
            "sessions": sorted(self.sessions),
            "excerpts": [{"ref": e.ref, "text": e.text, "from_tool_output": e.from_tool_output}
                         for e in self.excerpts],
        }


def _truncate(text) -> str:
    text = text if isinstance(text, str) else str(text)
    return text if len(text) <= MAX_EXCERPT_CHARS else text[:MAX_EXCERPT_CHARS] + "…"


def _text_of_user_node(node: dict) -> str:
    return "".join(b.get("text", "") for b in node.get("content") or []
                   if isinstance(b, dict) and b.get("type") == "text")


def _is_correction_text(text: str) -> bool:
    t = text.strip().lower()
    return any(t.startswith(cue) for cue in CORRECTION_CUES)


def build_clusters(*, since: str = "7d", slug: Optional[str] = None, all_projects: bool = False,
                    sessions_dir=None) -> "list[Cluster]":
    """One raw-node pass per candidate session file (telemetry.scan's own
    cached SessionSummary keeps only counters, never excerpt text)."""
    tool_error: dict = {}
    repair: dict = {}
    loop_breaker: dict = {}
    correction = Cluster("user_correction", "user_correction", "User corrections after a tool error or a fix-it reply")
    read_enoent = Cluster("read_enoent", "read_enoent", "Read failures (file not found / wrong cwd)")
    sequence_sessions: dict = {}  # trigram -> set(session_id)
    sequence_example: dict = {}  # trigram -> Excerpt

    files = telemetry.candidate_session_files(sessions_dir, since=since, slug=slug, all_projects=all_projects)
    for path in files:
        session_id = path.stem
        nodes, _corrupt = telemetry.read_session_lines(path)
        current_model = None
        last_tool_error = False  # did the PREVIOUS node in this turn's tool traffic error out
        tool_name_run: list = []
        session_trigrams: set = set()
        for node in nodes:
            if not isinstance(node, dict):
                continue
            ntype = node.get("type")
            seq = node.get("seq")
            if ntype == "usage" and node.get("model"):
                current_model = node["model"]
            elif ntype == "assistant":
                tool_meta = node.get("tool_meta") or {}
                for block in node.get("content") or []:
                    if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                        continue
                    name = block.get("name") or "?"
                    tool_name_run.append(name)
                    if len(tool_name_run) >= 3:
                        tri = " -> ".join(tool_name_run[-3:])
                        if tri not in session_trigrams:
                            session_trigrams.add(tri)
                            sequence_sessions.setdefault(tri, set()).add(session_id)
                            sequence_example.setdefault(tri, Excerpt(session_id, seq, tri, False))
                    meta = tool_meta.get(block.get("id"))
                    if meta and meta.get("repaired") and meta.get("repair_kind") not in (None, "none"):
                        key = (current_model or "?", meta["repair_kind"])
                        c = repair.setdefault(key, Cluster(
                            f"repair:{key[0]}:{key[1]}", "repair",
                            f"Repair layer: {key[1]} hits for {key[0]}"))
                        c.add(Excerpt(session_id, seq, _truncate(f"{name}({block.get('input')})"), False))
                # An assistant's own text reply, when it reads as a
                # correction-style reply to ITSELF (rare -- the common case
                # below is the USER correcting the model), is not tracked
                # separately; (d) is scoped to genuine user turns per the
                # brief's own wording ("a user node ... or an assistant
                # answer whose text starts with a correction cue" -- the
                # assistant form is folded in here too for completeness).
                text = "".join(b.get("text", "") for b in node.get("content") or []
                                if isinstance(b, dict) and b.get("type") == "text")
                if text and _is_correction_text(text):
                    correction.add(Excerpt(session_id, seq, _truncate(text), False))
            elif ntype == "tool_result":
                tool = node.get("tool") or "?"
                is_error = bool(node.get("is_error"))
                last_tool_error = is_error
                if is_error:
                    error_class = node.get("error_class") or "other"
                    key = (tool, error_class)
                    c = tool_error.setdefault(key, Cluster(
                        f"tool_error:{tool}:{error_class}", "tool_error",
                        f"{tool}: repeated {error_class} errors"))
                    content = node.get("content")
                    c.add(Excerpt(session_id, seq, _truncate(content), True))
                    if error_class == "loop_breaker":
                        lb = loop_breaker.setdefault(tool, Cluster(
                            f"loop_breaker:{tool}", "loop_breaker", f"{tool}: loop-breaker trips"))
                        lb.add(Excerpt(session_id, seq, _truncate(content), True))
                    if tool == "Read" and error_class == "not_found":
                        read_enoent.add(Excerpt(session_id, seq, _truncate(content), True))
            elif ntype == "user":
                kind = node.get("kind")
                # A real new prompt (kind is None) OR a mid-turn steer
                # (kind="steer" -- the user's own live redirect, agent/
                # loop.py's `Session.turn`-adjacent steer path) both count
                # as "the user's own words" for this cluster; every OTHER
                # tagged kind (compaction/continuation/agent_notice/
                # job_notice) is synthetic, never a real user correction.
                if kind in (None, "steer"):
                    text = _text_of_user_node(node)
                    if text and (last_tool_error or _is_correction_text(text)):
                        correction.add(Excerpt(session_id, seq, _truncate(text), False))
                if kind is None:
                    last_tool_error = False

    clusters: list = []
    for c in tool_error.values():
        if c.count >= _MIN_TOOL_ERROR_REPEATS:
            clusters.append(c)
    for c in repair.values():
        if c.count >= _MIN_REPAIR_REPEATS:
            clusters.append(c)
    clusters.extend(loop_breaker.values())
    if correction.count:
        clusters.append(correction)
    if read_enoent.count:
        clusters.append(read_enoent)
    for tri, sess in sequence_sessions.items():
        if len(sess) >= _MIN_SEQUENCE_SESSIONS:
            c = Cluster(f"tool_sequence:{tri}", "tool_sequence", f"Recurring tool sequence: {tri}")
            c.sessions = set(sess)
            c.count = len(sess)
            ex = sequence_example.get(tri)
            if ex is not None:
                c.excerpts.append(ex)
            clusters.append(c)

    clusters.sort(key=lambda c: (-c.count, c.cluster_id))
    return clusters
