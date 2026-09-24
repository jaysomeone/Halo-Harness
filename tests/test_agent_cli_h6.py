"""tests.test_agent_cli_h6 -- H6 scope A/D: `--agent`, `@agent-<name>`,
`--continue`/`--resume`/`--fork-session`/`-n/--name`, and the `/agents`/
`/rename` slash commands, driven as REAL `python -m rolo_claude` subprocess
invocations against the mock upstream (the same pattern test_loop_headless.py
uses) -- exercises the actual cli.py argument parsing end to end, not just
the underlying library functions.

Test hygiene: `tests.helpers.mock_openai.SCENARIOS` is a single dict shared
by the WHOLE process (tests/run_all.py imports every test_*.py module into
one interpreter) -- every scenario here is registered under a fresh,
UNIQUE key (never the bare "model" name other suites' own scenarios use),
and `os.environ` is never mutated in this process (only in the SUBPROCESS's
own copied `env` dict) so nothing here can leak into a test file that
happens to run later in the same `run_all.py` invocation.
"""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, ScriptedTurns

REPO_DIR = Path(__file__).resolve().parent.parent

test, TESTS = new_registry()


def _text_step(text: str) -> list:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]


def _run_cli(fh, mock, prompt, *, steps=None, extra_args=None, timeout=30):
    """`steps`, when given, is a `ScriptedTurns`-style step list registered
    under a FRESH unique scenario key for THIS call only (see module
    docstring); omit it for a call that doesn't need the model at all
    (e.g. a `--resume` that should fail before ever reaching one)."""
    env = dict(os.environ)
    env.update({
        "BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
        "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR),
    })
    model_args = []
    if steps is not None:
        key = f"h6cli-{uuid.uuid4().hex[:12]}"
        SCENARIOS[key] = ScriptedTurns(steps)
        model_args = ["--model", f"or:mock/{key}"]
    args = ([sys.executable, "-m", "rolo_claude", "-p", prompt] + model_args
            + ["--cwd", str(fh["proj"]), "--output-format", "json"] + (extra_args or []))
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


def _find_index_json(fh) -> dict:
    """Reads the subprocess's OWN index.json by glob rather than
    recomputing `project_slug` in THIS process -- `run_print_mode`
    resolves `cwd` with `.resolve()` before hashing it, and on some
    filesystems (a tempdir's real path vs. its resolved/symlink-following
    form) that can produce a different slug than hashing `fh["proj"]`
    directly here would."""
    matches = list((fh["home"] / ".rolo-claude" / "sessions").glob("*/index.json"))
    if not matches:
        return {}
    return json.loads(matches[0].read_text(encoding="utf-8"))


def _write_agent_md(fh, name: str, description: str, body: str) -> None:
    path = fh["proj"] / ".claude" / "agents" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8")


# ---- --agent ---------------------------------------------------------------------

@test
def test_agent_flag_runs_the_session_as_that_agent(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "find where X is defined", steps=[_text_step("explored as requested")],
                           extra_args=["--agent", "Explore"])
        ctx.check(f"exit 0 (stderr: {result.stderr[-400:]!r})", result.returncode == 0)
        ctx.check("at least one request", len(mock.requests) >= 1)
        body = mock.requests[-1]["body"] or {}
        tool_names = {t.get("function", {}).get("name") for t in (body.get("tools") or [])}
        ctx.check("Write is not offered to Explore", "Write" not in tool_names)
        ctx.check("Edit is not offered to Explore", "Edit" not in tool_names)
        ctx.check("Read IS offered", "Read" in tool_names)
        system_text = json.dumps(body)
        ctx.check("Explore's own body text reached the system prompt", "read-only exploration sub-agent" in system_text)
    finally:
        mock.stop()


@test
def test_agent_flag_custom_project_agent(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        _write_agent_md(fh, "reviewer", "Reviews code", "You are a strict code reviewer. Be terse.")
        result = _run_cli(fh, mock, "review this", steps=[_text_step("reviewing")],
                           extra_args=["--agent", "reviewer"])
        ctx.check(f"exit 0 (stderr: {result.stderr[-400:]!r})", result.returncode == 0)
        body = mock.requests[-1]["body"] or {}
        ctx.check("custom agent body used as system prompt", "strict code reviewer" in json.dumps(body))
    finally:
        mock.stop()


@test
def test_agent_flag_unknown_name_reports_and_continues(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "hi", steps=[_text_step("ok anyway")],
                           extra_args=["--agent", "totally-not-a-real-agent"])
        ctx.check("still exits 0 (falls back rather than crashing)", result.returncode == 0)
        ctx.check("stderr mentions the unknown agent", "totally-not-a-real-agent" in result.stderr)
    finally:
        mock.stop()


# ---- @agent-<name> mention --------------------------------------------------------

@test
def test_agent_mention_in_prompt_injects_instruction(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "@agent-Explore please find the config loader",
                           steps=[_text_step("using the sub-agent now")])
        ctx.check(f"exit 0 (stderr: {result.stderr[-400:]!r})", result.returncode == 0)
        body = mock.requests[-1]["body"] or {}
        whole = json.dumps(body)
        ctx.check("the forcing instruction reached the request", 'subagent_type=\\"Explore\\"' in whole
                  or 'subagent_type="Explore"' in whole)
    finally:
        mock.stop()


# ---- --continue / --resume / --fork-session / --name -----------------------------

@test
def test_continue_reuses_the_latest_session(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        first = _run_cli(fh, mock, "remember: my favourite colour is teal",
                          steps=[_text_step("my favourite colour is teal")])
        ctx.check(f"first call exit 0 (stderr: {first.stderr[-400:]!r})", first.returncode == 0)
        sid = json.loads(first.stdout)["session_id"]

        second = _run_cli(fh, mock, "what colour did I just say?", steps=[_text_step("it's teal")],
                           extra_args=["--continue"])
        ctx.check(f"second call exit 0 (stderr: {second.stderr[-400:]!r})", second.returncode == 0)
        ctx.check("same session_id reused", json.loads(second.stdout)["session_id"] == sid)
        body = mock.requests[-1]["body"] or {}
        ctx.check("prior turn's content reached the resumed request", "teal" in json.dumps(body))
    finally:
        mock.stop()


@test
def test_resume_by_explicit_id(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        first = _run_cli(fh, mock, "remember the codeword banana", steps=[_text_step("noted: banana")])
        sid = json.loads(first.stdout)["session_id"]

        second = _run_cli(fh, mock, "what was the codeword?", steps=[_text_step("banana")],
                           extra_args=["-r", sid])
        ctx.check(f"exit 0 (stderr: {second.stderr[-400:]!r})", second.returncode == 0)
        ctx.check("resumed the exact session", json.loads(second.stdout)["session_id"] == sid)
    finally:
        mock.stop()


@test
def test_resume_unknown_id_fails_cleanly(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "hi", extra_args=["-r", "not-a-real-session-name"])
        ctx.check("non-zero exit for an unresolvable --resume", result.returncode != 0)
        ctx.check("no upstream request was ever made", len(mock.requests) == 0)
    finally:
        mock.stop()


@test
def test_fork_session_produces_a_new_id_with_full_history(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        first = _run_cli(fh, mock, "hello there", steps=[_text_step("original reply")])
        original_id = json.loads(first.stdout)["session_id"]

        forked = _run_cli(fh, mock, "continue from the fork", steps=[_text_step("forked reply")],
                           extra_args=["--continue", "--fork-session"])
        forked_id = json.loads(forked.stdout)["session_id"]
        ctx.check("fork produced a DIFFERENT session id", forked_id != original_id)

        # Glob under fh["home"] directly (never mutate os.environ here --
        # it is PROCESS-WIDE and would leak into every later test/subprocess
        # in this same run, e.g. making a later --name test write into
        # THIS test's fake home instead of its own).
        matches = list((fh["home"] / ".rolo-claude" / "sessions").glob(f"*/{original_id}.jsonl"))
        ctx.check("original session log untouched by the fork", len(matches) == 1)
    finally:
        mock.stop()


@test
def test_name_flag_sets_the_title(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "do a thing", steps=[_text_step("ok")],
                           extra_args=["--name", "My Custom Title"])
        sid = json.loads(result.stdout)["session_id"]
        index = _find_index_json(fh)
        ctx.check("title recorded in index.json", index.get(sid, {}).get("title") == "My Custom Title")
    finally:
        mock.stop()


# ---- /agents, /rename slash commands ----------------------------------------------

@test
def test_slash_agents_lists_builtins(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "/agents", extra_args=["--output-format", "text"])
        ctx.check(f"exit 0 (stderr: {result.stderr[-400:]!r})", result.returncode == 0)
        ctx.check("general-purpose listed", "general-purpose" in result.stdout)
        ctx.check("Explore listed", "Explore" in result.stdout)
        ctx.check("Plan listed", "Plan" in result.stdout)
    finally:
        mock.stop()


@test
def test_slash_rename_sets_title(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "/rename Weekend Project", extra_args=["--output-format", "json"])
        ctx.check(f"exit 0 (stderr: {result.stderr[-400:]!r})", result.returncode == 0)
        sid = json.loads(result.stdout)["session_id"]
        index = _find_index_json(fh)
        ctx.check("title recorded via /rename", index.get(sid, {}).get("title") == "Weekend Project")
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
