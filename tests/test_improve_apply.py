"""tests.test_improve_apply -- H10 Part B: the dismissed-hash store, apply
appending `improve_applied` + a fresh next-turn snapshot, headless
`improve --json` writing nothing vs `--apply` writing exactly one, `-p`
never drafting, settings.json/.claude.json checksums unchanged, and
`.credentials.json` never opened.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _run(argv, home: Path, cwd: Path, timeout=30):
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(home), "PYTHONPATH": str(REPO_DIR)})
    return subprocess.run([sys.executable, "-m", "halo_harness"] + argv, env=env, cwd=str(cwd),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


@test
def test_dismissed_hash_persists_across_calls(ctx: Ctx):
    from halo_harness.improve.dismissed import add_dismissed, is_dismissed, load_dismissed
    from halo_harness.improve.draft import Candidate

    home = Path(tempfile.mkdtemp(prefix="improve-dismissed-"))
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    cand = Candidate(id="c1", kind="rule", title="t", scope="project", path="p.md", body="b",
                      rationale="r", evidence=[], confidence="low")
    ctx.check("not dismissed initially", not is_dismissed(cand))
    from halo_harness.improve.apply import candidate_hash
    add_dismissed(candidate_hash(cand))
    ctx.check("dismissed after add_dismissed", is_dismissed(cand))
    # Fresh read from disk (simulates a restart -- load_dismissed() re-reads
    # ~/.halo/improve/dismissed.json every call).
    reloaded = load_dismissed()
    ctx.check("persisted hash present in a fresh load", candidate_hash(cand) in reloaded)
    # A DIFFERENT candidate (different body) is NOT dismissed.
    other = Candidate(id="c2", kind="rule", title="t", scope="project", path="p.md", body="different body",
                       rationale="r", evidence=[], confidence="low")
    ctx.check("a different body is a different hash, not dismissed", not is_dismissed(other))


@test
def test_apply_appends_improve_applied_and_next_turn_snapshot(ctx: Ctx):
    """A real Session (no live model call needed) applies a rule candidate;
    the log gets an `improve_applied` node, and `derive_request` on the
    NEXT turn includes the rule's body (the fresh claude_md snapshot)."""
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.derive import derive_request
    from halo_harness.agent.loop import Session as _Session
    from halo_harness.improve import apply as apply_mod
    from halo_harness.improve.draft import Candidate
    from halo_harness.model import ModelProfile, parse_model_ref

    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    session_ctx = SessionContext(cwd=fh["proj"], model_label="or:mock/model")
    model_ref = parse_model_ref("or:mock/model")
    session = _Session(cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(), creds=None,
                        state_dir=Path(tempfile.mkdtemp(prefix="improve-apply-session-")),
                        model_label="or:mock/model", session_context=session_ctx)
    session.log.append_user([{"type": "text", "text": "hello"}])

    cand = Candidate(id="c1", kind="rule", title="always-do-x", scope="project", path="always_do_x.md",
                      body="Always do X before Y.", rationale="r", evidence=["sess1#1"], confidence="high")
    result = apply_mod.apply_candidate(cand, cwd=fh["proj"], settings=session_ctx.settings,
                                        model_label="or:mock/model", session=session)
    ctx.check("applied file exists", result.path.exists())

    nodes = session.log.nodes()
    applied_nodes = [n for n in nodes if n.get("type") == "improve_applied"]
    ctx.check(f"exactly 1 improve_applied node, got {len(applied_nodes)}", len(applied_nodes) == 1)
    ctx.check("improve_applied names the right kind/path", applied_nodes[0]["kind"] == "rule"
              and applied_nodes[0]["path"] == str(result.path))
    ctx.check("improve_applied carries the candidate id", applied_nodes[0]["candidate_id"] == "c1")
    ctx.check("improve_applied carries the sha256", applied_nodes[0]["sha256"] == result.sha256)

    _, messages, _ = derive_request(session.log)
    all_text = "".join(b.get("text", "") for m in messages for b in m.get("content", []) if isinstance(b, dict))
    ctx.check("the NEXT turn's derived request includes the new rule's body",
              "Always do X before Y." in all_text)


@test
def test_headless_json_writes_nothing_apply_writes_exactly_one(ctx: Ctx):
    home = Path(tempfile.mkdtemp(prefix="improve-headless-home-"))
    cwd = Path(tempfile.mkdtemp(prefix="improve-headless-cwd-"))
    result = _run(["improve", "--json", "--cwd", str(cwd)], home, cwd)
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    obj = json.loads(result.stdout)
    ctx.check("no candidates with an empty project", obj["candidates"] == [])
    claude_dir = cwd / ".claude"
    ctx.check("--json with no candidates writes NOTHING under cwd/.claude", not claude_dir.exists())

    # --apply writes EXACTLY the one named candidate, nothing else.
    cand_path = cwd / "cands.json"
    cand_path.write_text(json.dumps([
        {"id": "a", "kind": "rule", "title": "ta", "target": {"scope": "project", "path": "a.md"},
         "body": "A", "rationale": "r", "evidence": [], "confidence": "low"},
        {"id": "b", "kind": "rule", "title": "tb", "target": {"scope": "project", "path": "b.md"},
         "body": "B", "rationale": "r", "evidence": [], "confidence": "low"},
    ]), encoding="utf-8")
    result2 = _run(["improve", "--apply", f"{cand_path}#a", "--cwd", str(cwd)], home, cwd)
    ctx.check(f"exit 0, got {result2.returncode}", result2.returncode == 0)
    rules_dir = cwd / ".claude" / "rules"
    written = sorted(p.name for p in rules_dir.glob("*.md")) if rules_dir.is_dir() else []
    ctx.check(f"exactly one file written (a.md, not b.md), got {written}", written == ["a.md"])


@test
def test_bare_disables_everything(ctx: Ctx):
    home = Path(tempfile.mkdtemp(prefix="improve-bare-home-"))
    cwd = Path(tempfile.mkdtemp(prefix="improve-bare-cwd-"))
    result = _run(["improve", "--bare", "--cwd", str(cwd)], home, cwd)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("nothing written under .claude", not (cwd / ".claude").exists())


@test
def test_dash_p_never_drafts_or_writes(ctx: Ctx):
    """`-p "/improve"` must return the "needs the interactive TUI" message
    and never touch the filesystem or call a drafting model."""
    home = Path(tempfile.mkdtemp(prefix="improve-dashp-home-"))
    cwd = Path(tempfile.mkdtemp(prefix="improve-dashp-cwd-"))
    result = _run(["-p", "/improve", "--model", "or:mock/nonexistent-must-not-be-called"], home, cwd)
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    ctx.check("names the interactive TUI", "interactive TUI" in result.stdout)
    ctx.check("points at the real headless surface", "halo improve" in result.stdout)
    ctx.check("nothing written under .claude", not (cwd / ".claude").exists())


@test
def test_settings_and_claude_json_checksums_unchanged(ctx: Ctx):
    fh = build_fake_home()
    settings_path = fh["claude_dir"] / "settings.json" if "claude_dir" in fh else fh["home"] / ".claude" / "settings.json"
    claude_json_path = fh["home"] / ".claude.json"
    before_settings = _sha256_file(settings_path)
    before_claude_json = _sha256_file(claude_json_path)

    home = fh["home"]
    result = _run(["improve", "--json", "--cwd", str(fh["proj"])], home, fh["proj"])
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)

    after_settings = _sha256_file(settings_path)
    after_claude_json = _sha256_file(claude_json_path)
    ctx.check("settings.json checksum unchanged", before_settings == after_settings)
    ctx.check(".claude.json checksum unchanged", before_claude_json == after_claude_json)


@test
def test_credentials_json_sentinel_never_opened(ctx: Ctx):
    """A sentinel `.credentials.json` that raises if ever opened -- proves
    the whole evidence/draft/apply path never reads it, matching this
    harness's own hard rule."""
    fh = build_fake_home()
    creds_path = fh["home"] / ".claude" / ".credentials.json"
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_text('{"sentinel": true}', encoding="utf-8")

    real_open = open
    opened_paths = []

    def _guarded_open(file, *args, **kwargs):
        p = str(file)
        if p.endswith(".credentials.json"):
            opened_paths.append(p)
            raise AssertionError(f".credentials.json was opened: {p}")
        return real_open(file, *args, **kwargs)

    import builtins
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    from halo_harness.improve import evidence as evidence_mod

    original_open = builtins.open
    builtins.open = _guarded_open
    try:
        evidence_mod.build_clusters(since="all", slug="proj")
    finally:
        builtins.open = original_open
    ctx.check("no .credentials.json read during evidence scanning", opened_paths == [])


@test
def test_has_provenance_marker_recognizes_the_legacy_1_0_1_marker(ctx: Ctx):
    """2.0.0 fixpass finding 6: PROVENANCE_MARKER changed from
    `<!-- rolo-claude improve: ... -->` to `<!-- halo improve: ... -->` --
    a file 1.0.1's /improve wrote must still be recognized as
    already-provenanced, or every such file looks user-authored to 2.0.0."""
    from halo_harness.improve.apply import PROVENANCE_MARKER, PROVENANCE_MARKER_LEGACY, has_provenance_marker

    legacy_text = (f"body text\n\n{PROVENANCE_MARKER_LEGACY} created=2026-01-01T00:00:00Z sessions=s1 "
                   f"evidence=1 model=x from_tool_output=false -->\n")
    new_text = (f"body text\n\n{PROVENANCE_MARKER} created=2026-01-01T00:00:00Z sessions=s1 "
                f"evidence=1 model=x from_tool_output=false -->\n")
    ctx.check("a 1.0.1-written file is recognized as provenanced", has_provenance_marker(legacy_text))
    ctx.check("a 2.0.0-written file is recognized too", has_provenance_marker(new_text))
    ctx.check("a genuinely user-authored file is not", not has_provenance_marker("just a plain file\n"))
    ctx.check("PROVENANCE_MARKER itself is the NEW text (a fresh write always uses it)",
              PROVENANCE_MARKER == "<!-- halo improve:")


@test
def test_next_available_updates_a_1_0_1_written_file_in_place_not_a_collision(ctx: Ctx):
    """The actual observable bug: a rule file 1.0.1's /improve wrote,
    targeted again by a fresh candidate with the SAME name, must be
    treated as an UPDATE (is_update=True, same path) -- never renamed
    around as if it were a collision with an "unrecognized" user file
    (which would have written a colliding `foo-2.md` right beside it)."""
    from halo_harness.improve.apply import PROVENANCE_MARKER_LEGACY, _next_available

    tmp = Path(tempfile.mkdtemp(prefix="improve-legacy-marker-"))
    target = tmp / "foo.md"
    target.write_text(f"old body\n\n{PROVENANCE_MARKER_LEGACY} created=2026-01-01T00:00:00Z sessions=s1 "
                       f"evidence=1 model=x from_tool_output=false -->\n", encoding="utf-8")
    path, is_update, renamed_from = _next_available(target, is_dir=False)
    ctx.check(f"same path reused, got {path}", path == target)
    ctx.check("treated as an update, not a fresh/colliding file", is_update is True)
    ctx.check("no rename-around-a-collision happened", renamed_from is None)
    ctx.check("foo-2.md was never created", not (tmp / "foo-2.md").exists())


def _hermetic_child_env() -> dict:
    """2.0.0 fixpass item G: never forward a stray BRIDGE_STATE_DIR
    (would let bridge_home() escape this test's own BRIDGE_TEST_HOME
    scoping) or HALO_* (would out-rank the legacy BRIDGE_* name a
    fixture deliberately sets, per env_compat's own precedence) from
    the parent process into a spawned child -- same hermeticity
    tests/test_init_cli.py::_run already has, applied at each of this
    file's own `env = dict(os.environ)` call sites."""
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    return env


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
