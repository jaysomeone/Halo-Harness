"""rolo_claude.improve.draft -- H10 Part B2: ONE drafting model call over
the evidence clusters, producing <= `improve.max_candidates` (8) candidates
as JSON. Model precedence: config `improve.model` -> else the session's
small model -> else the session model (`Session.call_small_model`, H10's
own generalization of `_call_model_for_hook` -- same one-shot, never-
logged, mock-interceptable plumbing). Malformed JSON gets ONE retry quoting
the schema error; still malformed -> "no candidates", never a crash.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Optional

CANDIDATE_KINDS = ("memory", "rule", "skill")
CANDIDATE_SCOPES = ("project", "user")
CONFIDENCE_LEVELS = ("low", "med", "high")

_SYSTEM_PROMPT = """You are the drafting step of rolo-claude's human-gated /improve command.

You will be given evidence clusters mined from recent session logs (repeated tool errors, \
repair-layer hits, loop-breaker trips, user corrections, Read failures, recurring tool \
sequences). Propose AT MOST {max_candidates} candidates that would make FUTURE sessions go \
better. Every candidate is reviewed by a human before anything is written -- you are drafting \
a PROPOSAL, not taking an action.

Reply with ONLY a single JSON array (no prose, no markdown fence) of objects shaped EXACTLY:
{{"id": "short-slug", "kind": "memory"|"rule"|"skill", "title": "...", \
"target": {{"scope": "project"|"user", "path": "..."}}, "body": "...", "rationale": "...", \
"evidence": ["<session_id>#<seq>", ...], "confidence": "low"|"med"|"high"}}

Rules:
- "kind": "memory" for a fact/preference/correction worth remembering; "rule" for an \
instruction that should always apply; "skill" for a repeated multi-step workflow.
- "target.path" is a bare filename (no directories) for memory/rule, e.g. "deepseek_tool_calls.md"; \
for a skill, a short directory name, e.g. "grep-then-edit".
- "evidence" MUST only cite session#seq references that actually appear in the clusters below \
-- never invent one.
- "body" is the actual Markdown content to write (for a skill, keep it under 120 lines).
- Never propose editing CLAUDE.md, settings.json, or ~/.claude.json.
- If nothing in the evidence is worth a durable change, reply with an empty JSON array: []."""


def _cluster_text(clusters) -> str:
    lines = []
    for c in clusters:
        d = c.to_dict() if hasattr(c, "to_dict") else c
        lines.append(f"### {d['kind']}: {d['label']} (count={d['count']}, sessions={d['sessions']})")
        for ex in d["excerpts"]:
            tag = " [derived from tool output]" if ex["from_tool_output"] else ""
            lines.append(f"  - {ex['ref']}{tag}: {ex['text']}")
    return "\n".join(lines) if lines else "(no clusters)"


@dataclass
class Candidate:
    id: str
    kind: str
    title: str
    scope: str
    path: str
    body: str
    rationale: str
    evidence: "list[str]" = field(default_factory=list)
    confidence: str = "low"
    # H10 Part B: True iff ANY cited evidence excerpt originated in a
    # tool_result (a cluster excerpt's own `from_tool_output` flag) rather
    # than the user's own words -- carried onto the candidate so
    # `rolo_claude.improve.apply`'s provenance comment and the ImproveCard's
    # "derived from tool output" line never have to re-derive it from the
    # evidence refs a second time.
    from_tool_output: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title,
                "target": {"scope": self.scope, "path": self.path}, "body": self.body,
                "rationale": self.rationale, "evidence": self.evidence, "confidence": self.confidence,
                "from_tool_output": self.from_tool_output}


class DraftError(Exception):
    """Raised only for a truly unusable reply (malformed JSON after the one
    retry) -- callers treat this as "no candidates", never a crash."""


def _lenient_parse(text: str) -> Optional[list]:
    """Strict JSON first; `providers.hooks.args_repair`'s lenient pass
    (trailing commas, single quotes, Python-repr True/False/None) second --
    the SAME repair-layer parser `agent/repair.py`'s tool-call path uses,
    reused rather than duplicated. A markdown code fence around the array
    is stripped first (a small model's own habit even when told "no
    markdown fence")."""
    from rolo_claude.providers.hooks import args_repair

    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    try:
        obj = json.loads(stripped)
        return obj if isinstance(obj, list) else None
    except (ValueError, TypeError):
        pass
    # args_repair expects an OBJECT-shaped string (`{...}`) for its own
    # trailing-comma/quote fixes; wrap a bare array so the same repairs apply,
    # then unwrap.
    wrapped = args_repair("{\"candidates\": " + stripped + "}")
    if isinstance(wrapped, dict) and isinstance(wrapped.get("candidates"), list):
        return wrapped["candidates"]
    return None


def _coerce_candidate(raw: dict, valid_refs: "set[str]", tool_output_refs: "set[str]") -> Optional[Candidate]:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind not in CANDIDATE_KINDS:
        return None
    target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
    scope = target.get("scope") if target.get("scope") in CANDIDATE_SCOPES else "project"
    path = target.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    body = raw.get("body")
    if not isinstance(body, str) or not body.strip():
        return None
    evidence_in = raw.get("evidence") if isinstance(raw.get("evidence"), list) else []
    # Never invent a reference: only evidence refs that actually appeared
    # in a cluster excerpt survive (a model that hallucinates a session#seq
    # just gets that one entry silently dropped, not the whole candidate).
    evidence = [e for e in evidence_in if isinstance(e, str) and (not valid_refs or e in valid_refs)]
    from_tool_output = any(e in tool_output_refs for e in evidence)
    confidence = raw.get("confidence") if raw.get("confidence") in CONFIDENCE_LEVELS else "low"
    cid = raw.get("id")
    cid = cid if isinstance(cid, str) and cid.strip() else uuid.uuid4().hex[:8]
    title = raw.get("title")
    title = title if isinstance(title, str) and title.strip() else path
    rationale = raw.get("rationale")
    rationale = rationale if isinstance(rationale, str) else ""
    return Candidate(id=cid, kind=kind, title=title, scope=scope, path=path.strip(), body=body,
                      rationale=rationale, evidence=evidence, confidence=confidence,
                      from_tool_output=from_tool_output)


def parse_candidates(text: str, *, max_candidates: int = 8, valid_refs: Optional[set] = None,
                      tool_output_refs: Optional[set] = None) -> "list[Candidate]":
    parsed = _lenient_parse(text)
    if parsed is None:
        raise DraftError(f"reply was not a JSON array of candidates: {text[:200]!r}")
    out = []
    for raw in parsed:
        c = _coerce_candidate(raw, valid_refs or set(), tool_output_refs or set())
        if c is not None:
            out.append(c)
        if len(out) >= max_candidates:
            break
    return out


def resolve_drafting_model_ref(session, configured_model: Optional[str]):
    """config `improve.model` -> else the session's small model -> else the
    session model -- returns a `model.ModelRef`, or None to let
    `Session.call_small_model` use its own default fallback (small model or
    session model) unchanged."""
    if configured_model:
        from rolo_claude.model import parse_model_ref
        return parse_model_ref(configured_model)
    return None


def draft_candidates(session, clusters, *, max_candidates: int = 8,
                      configured_model: Optional[str] = None, timeout_s: float = 90.0) -> "tuple[list, Optional[str]]":
    """ONE model call (config `improve.model` -> else the small model ->
    else the session model), asking for <= `max_candidates` candidates.
    Malformed JSON -> ONE retry quoting the schema error -> else "no
    candidates" (never raises out to the caller). Returns
    `(candidates, error_or_none)`."""
    if not clusters:
        return [], "no evidence clusters in this window"
    model_ref = resolve_drafting_model_ref(session, configured_model)
    system_text = _SYSTEM_PROMPT.format(max_candidates=max_candidates)
    user_text = _cluster_text(clusters)
    cluster_dicts = [c.to_dict() if hasattr(c, "to_dict") else c for c in clusters]
    valid_refs = {ex["ref"] for c in cluster_dicts for ex in c["excerpts"]}
    tool_output_refs = {ex["ref"] for c in cluster_dicts for ex in c["excerpts"] if ex["from_tool_output"]}

    try:
        reply = session.call_small_model(system_text=system_text, user_text=user_text, max_tokens=4096,
                                          timeout_s=timeout_s, model_ref=model_ref)
    except Exception as e:
        return [], f"drafting model call failed: {type(e).__name__}: {e}"

    try:
        return parse_candidates(reply, max_candidates=max_candidates, valid_refs=valid_refs,
                                 tool_output_refs=tool_output_refs), None
    except DraftError as e:
        retry_prompt = (f"{user_text}\n\nYour previous reply could not be parsed as the required JSON "
                         f"array: {e}. Reply again with ONLY a valid JSON array matching the schema.")
        try:
            reply2 = session.call_small_model(system_text=system_text, user_text=retry_prompt, max_tokens=4096,
                                               timeout_s=timeout_s, model_ref=model_ref)
            return parse_candidates(reply2, max_candidates=max_candidates, valid_refs=valid_refs,
                                     tool_output_refs=tool_output_refs), None
        except DraftError as e2:
            return [], f"no candidates: {e2}"
        except Exception as e2:
            return [], f"drafting retry failed: {type(e2).__name__}: {e2}"
