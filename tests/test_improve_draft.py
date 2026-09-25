"""tests.test_improve_draft -- H10 Part B2/B3: the ONE drafting model call
through the mock upstream (valid JSON; malformed -> one retry -> none),
memory frontmatter identical to fake_home's topic files, rule/skill shapes
(disable-model-invocation: true present), the provenance comment, "user
file without marker -> new name", "marker'd file -> diff".
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import SCENARIOS, MockUpstream, ScriptedTurns, _finish

test, TESTS = new_registry()


def _text_chunk(text: str, *, finish: str = "stop"):
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
    ]


def _make_session(fh, mock, *, scenario: str):
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session as _Session
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.providers.stream import ProviderCreds

    model_label = f"mock/{scenario}"
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model_label)
    model_ref = parse_model_ref(f"or:{model_label}")
    return _Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"), state_dir=Path(tempfile.mkdtemp(prefix="improve-session-")),
        model_label=model_label, session_context=session_ctx, openrouter_base_url=mock.base_url,
    )


from rolo_claude.improve.evidence import Cluster, Excerpt

_CLUSTER = Cluster("tool_error:Edit:not_found", "tool_error", "Edit: repeated not_found errors", count=2,
                    excerpts=[Excerpt("sessA", 3, "old_string not found", True)], sessions={"sessA"})


_VALID_REPLY = json.dumps([{
    "id": "c1", "kind": "memory", "title": "edit-not-found-pattern",
    "target": {"scope": "project", "path": "edit_not_found_pattern.md"},
    "body": "Edit keeps failing with not_found; double check old_string first.",
    "rationale": "Seen twice", "evidence": ["sessA#3"], "confidence": "med",
}])


@test
def test_draft_valid_json_produces_one_candidate(ctx: Ctx):
    from rolo_claude.improve.draft import draft_candidates

    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        scn = "improve-draft-valid"
        SCENARIOS[scn] = ScriptedTurns([_text_chunk(_VALID_REPLY)])
        session = _make_session(fh, mock, scenario=scn)
        candidates, error = draft_candidates(session, [_CLUSTER], max_candidates=8)
        ctx.check(f"no error, got {error!r}", error is None)
        ctx.check(f"exactly 1 candidate, got {len(candidates)}", len(candidates) == 1)
        ctx.check("kind memory", candidates[0].kind == "memory")
        ctx.check("evidence ref survived", candidates[0].evidence == ["sessA#3"])
        ctx.check("from_tool_output True (the cited excerpt was tool output)", candidates[0].from_tool_output is True)
    finally:
        mock.stop()


@test
def test_draft_malformed_then_retry_succeeds(ctx: Ctx):
    from rolo_claude.improve.draft import draft_candidates

    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        calls = []

        def _scn(handler, body):
            calls.append(body)
            if len(calls) == 1:
                _finish(handler, _text_chunk("not json at all, sorry"))
            else:
                _finish(handler, _text_chunk(_VALID_REPLY))

        scn = "improve-draft-retry"
        SCENARIOS[scn] = ScriptedTurns([_scn])
        session = _make_session(fh, mock, scenario=scn)
        candidates, error = draft_candidates(session, [_CLUSTER], max_candidates=8)
        ctx.check(f"2 model calls made (original + one retry), got {len(calls)}", len(calls) == 2)
        ctx.check(f"no error after the retry recovered, got {error!r}", error is None)
        ctx.check(f"1 candidate after retry, got {len(candidates)}", len(candidates) == 1)
    finally:
        mock.stop()


@test
def test_draft_malformed_twice_gives_no_candidates(ctx: Ctx):
    from rolo_claude.improve.draft import draft_candidates

    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        scn = "improve-draft-nogood"
        SCENARIOS[scn] = ScriptedTurns([_text_chunk("still not json")])
        session = _make_session(fh, mock, scenario=scn)
        candidates, error = draft_candidates(session, [_CLUSTER], max_candidates=8)
        ctx.check(f"empty candidates, got {candidates!r}", candidates == [])
        ctx.check(f"a 'no candidates' style error, got {error!r}", isinstance(error, str) and error)
    finally:
        mock.stop()


@test
def test_draft_empty_clusters_never_calls_the_model(ctx: Ctx):
    from rolo_claude.improve.draft import draft_candidates

    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        scn = "improve-draft-empty"

        def _boom(handler, body):
            raise AssertionError("must never call the model with no clusters")
        SCENARIOS[scn] = ScriptedTurns([_boom])
        session = _make_session(fh, mock, scenario=scn)
        candidates, error = draft_candidates(session, [], max_candidates=8)
        ctx.check("no candidates", candidates == [])
        ctx.check("clean error message", isinstance(error, str))
    finally:
        mock.stop()


# ============================================================================
# B2/B3: file shapes + provenance + collision/update rules (apply.py).
# ============================================================================

@test
def test_memory_frontmatter_matches_fake_homes_own_topic_files(ctx: Ctx):
    """Byte-shape parity with tests/helpers/fake_home.py's own
    MEMORY_TOPIC_1/2 fixtures (name/description/metadata.node_type/
    metadata.type/metadata.originSessionId/metadata.modified), verified by
    round-tripping through config/frontmatter.py's OWN parser."""
    from rolo_claude.config.frontmatter import parse as parse_frontmatter
    from rolo_claude.config.memory import MemoryStore

    class FakeSettings:
        auto_memory_enabled = True

    d = Path(tempfile.mkdtemp(prefix="improve-memshape-"))
    store = MemoryStore(d, FakeSettings())
    path = store.write(filename="a_topic.md", name="a-topic", description="A description",
                        type="feedback", body="Body text.", origin_session_id="sess123",
                        provenance_comment="<!-- rolo-claude improve: created=x sessions=sess123 "
                                            "evidence=1 model=m from_tool_output=false -->")
    raw = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(raw)
    ctx.check("name present", fm.get("name") == "a-topic")
    ctx.check("description present", fm.get("description") == "A description")
    metadata = fm.get("metadata")
    ctx.check("metadata is a dict", isinstance(metadata, dict))
    ctx.check("metadata.node_type == memory", metadata.get("node_type") == "memory")
    ctx.check("metadata.type == feedback", metadata.get("type") == "feedback")
    ctx.check("metadata.originSessionId == sess123", metadata.get("originSessionId") == "sess123")
    ctx.check("metadata.modified present (ISO)", isinstance(metadata.get("modified"), str) and "T" in metadata["modified"])
    ctx.check("provenance comment survives in the body", "rolo-claude improve:" in body)
    idx_text = (store.memory_dir_path / "MEMORY.md").read_text(encoding="utf-8")
    ctx.check("MEMORY.md index line present", "[a-topic](a_topic.md)" in idx_text)


@test
def test_rule_and_skill_shapes(ctx: Ctx):
    from rolo_claude.improve import apply as apply_mod
    from rolo_claude.improve.draft import Candidate
    from rolo_claude.config.frontmatter import parse as parse_frontmatter

    cwd = Path(tempfile.mkdtemp(prefix="improve-shapes-"))
    rule = Candidate(id="r1", kind="rule", title="my-rule", scope="project", path="my_rule.md",
                      body="Body.", rationale="r", evidence=[], confidence="low")
    r = apply_mod.apply_candidate(rule, cwd=cwd, model_label="m")
    fm, _ = parse_frontmatter(r.path.read_text(encoding="utf-8"))
    ctx.check("rule under .claude/rules/", ".claude" in str(r.path.parts) and "rules" in str(r.path.parts))
    ctx.check("rule frontmatter has name", fm.get("name") == "my-rule")

    skill = Candidate(id="s1", kind="skill", title="my-skill-desc", scope="project", path="my-skill",
                       body="1. Do X.\n2. Do Y.", rationale="r", evidence=[], confidence="low")
    r2 = apply_mod.apply_candidate(skill, cwd=cwd, model_label="m")
    ctx.check("skill file is SKILL.md", r2.path.name == "SKILL.md")
    fm2, _ = parse_frontmatter(r2.path.read_text(encoding="utf-8"))
    ctx.check("disable-model-invocation: true present", fm2.get("disable-model-invocation") is True)
    ctx.check("skill name present", fm2.get("name") == "my-skill")


@test
def test_provenance_comment_shape(ctx: Ctx):
    from rolo_claude.improve.apply import PROVENANCE_MARKER, has_provenance_marker, provenance_comment

    comment = provenance_comment(sessions=["sessA", "sessB"], evidence_count=3, model="or:x", from_tool_output=True)
    ctx.check("starts with the marker", comment.startswith(PROVENANCE_MARKER))
    ctx.check("names both sessions", "sessA" in comment and "sessB" in comment)
    ctx.check("evidence=3", "evidence=3" in comment)
    ctx.check("model=or:x", "model=or:x" in comment)
    ctx.check("from_tool_output=true", "from_tool_output=true" in comment)
    ctx.check("has_provenance_marker recognizes it", has_provenance_marker(comment))
    ctx.check("has_provenance_marker rejects plain text", not has_provenance_marker("just a normal file"))


@test
def test_user_authored_file_without_marker_gets_a_new_name(ctx: Ctx):
    from rolo_claude.improve import apply as apply_mod
    from rolo_claude.improve.draft import Candidate

    cwd = Path(tempfile.mkdtemp(prefix="improve-collide-"))
    user_path = cwd / ".claude" / "rules" / "existing.md"
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text("# hand-written by rolo\n", encoding="utf-8")

    cand = Candidate(id="c1", kind="rule", title="t", scope="project", path="existing.md",
                      body="new content", rationale="r", evidence=[], confidence="low")
    target = apply_mod.resolve_target_path(cand, cwd=cwd)
    ctx.check("never targets the user file directly", target.path != user_path)
    ctx.check("renamed_from records the original", target.renamed_from == str(user_path))
    apply_mod.apply_candidate(cand, cwd=cwd, model_label="m")
    ctx.check("user file content is untouched", user_path.read_text(encoding="utf-8") == "# hand-written by rolo\n")
    ctx.check("the new file exists at the renamed path", target.path.exists())


@test
def test_marker_file_update_produces_a_diff(ctx: Ctx):
    from rolo_claude.improve import apply as apply_mod
    from rolo_claude.improve.draft import Candidate

    cwd = Path(tempfile.mkdtemp(prefix="improve-diff-"))
    first = Candidate(id="c1", kind="rule", title="t", scope="project", path="r.md",
                       body="Line one.\nLine two.", rationale="r", evidence=[], confidence="low")
    apply_mod.apply_candidate(first, cwd=cwd, model_label="m")

    second = Candidate(id="c1", kind="rule", title="t", scope="project", path="r.md",
                        body="Line one.\nLine TWO edited.", rationale="r", evidence=[], confidence="low")
    target = apply_mod.resolve_target_path(second, cwd=cwd)
    ctx.check("target IS an update (provenance-marked)", target.is_update)
    comment = apply_mod.provenance_comment(sessions=[], evidence_count=0, model="m", from_tool_output=False)
    diff = apply_mod.diff_preview(target, second, comment)
    diff_text = "".join(diff)
    ctx.check("diff shows the removed line", "-Line two." in diff_text)
    ctx.check("diff shows the added line", "+Line TWO edited." in diff_text)


@test
def test_resolve_drafting_model_ref_precedence(ctx: Ctx):
    """config `improve.model` -> else the small model -> else the session
    model: this function ONLY ever resolves the first leg (an explicit
    config value) -- returning None otherwise defers to
    `Session.call_small_model`'s own `small_model_ref or model_ref`
    default, so there is exactly ONE place that fallback logic lives."""
    from rolo_claude.improve.draft import resolve_drafting_model_ref
    from rolo_claude.model import ModelRef

    ref = resolve_drafting_model_ref(session=None, configured_model="or:deepseek/deepseek-v4-flash")
    ctx.check("configured model resolves to a real ModelRef", isinstance(ref, ModelRef))
    ctx.check("provider is openrouter", ref.provider == "openrouter")
    ctx.check("model is the bare id", ref.model == "deepseek/deepseek-v4-flash")

    ref_none = resolve_drafting_model_ref(session=None, configured_model=None)
    ctx.check("no configured model -> None (session's own default fallback applies)", ref_none is None)


@test
def test_tool_result_ok_field_mirrors_is_error(ctx: Ctx):
    """H10 Part A: `tool_result.ok` is a convenience mirror of `not
    is_error`, always present."""
    import tempfile as _tempfile
    from pathlib import Path as _Path
    from rolo_claude.agent.log import SessionLog

    log = SessionLog(_Path(_tempfile.mkdtemp(prefix="ok-field-")), session_id="s")
    ok_node = log.append_tool_result(tool_use_id="t1", content="fine", is_error=False)
    err_node = log.append_tool_result(tool_use_id="t2", content="broken", is_error=True)
    ctx.check("ok=True mirrors is_error=False", ok_node["ok"] is True)
    ctx.check("ok=False mirrors is_error=True", err_node["ok"] is False)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
