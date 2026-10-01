"""tests.test_h9b_findings -- H9b fix pass: pinning tests for the
whole-tree review findings H9 left open (docs/harness/review-findings-h9-
tree.md), each driving a REAL agent.loop.Session (or the real CLI in a
subprocess) the way the finding's own "Verified" repro did, per finding.
Named `test_h9b_f<NN>_...` per the brief.
"""
from __future__ import annotations

import json
import os
import re
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, SkipTest, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, ScriptedTurns, _finish
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

REPO_DIR = Path(__file__).resolve().parent.parent
_HOOK_SCRIPT_ARGV = [sys.executable, "-m", "tests.helpers.hook_scripts"]

# H15 part 2 addendum 3.1: a believable default credential (never a real
# one) keeps every `or:mock/...` ref below resolving exactly as it did
# before parse_model_ref started refusing an auto-detected-disabled
# provider; each test here already scopes its OWN BRIDGE_TEST_HOME. A
# subprocess-CLI test's own `env` dict is unaffected (os.environ.setdefault
# here never touches it); such a test already builds its OWN explicit env.
ensure_default_provider_credentials()

test, TESTS = new_registry()


# ---- shared scripting helpers (same shapes as test_subagent_e2e.py/
# test_loop_h2_integration.py -- kept local so this file has no import-order
# dependency on another test module) -----------------------------------------

def _text_step(text: str) -> list:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]


def _tool_call_step(name: str, arguments: dict, call_id: str = "call_1") -> list:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments)}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def _new_session(*, mock, model, agents=None, permission_mode="auto", max_turns=10, cwd=None):
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.permissions import PermissionEngine
    from rolo_claude.providers.stream import ProviderCreds

    cwd = cwd or Path(tempfile.mkdtemp(prefix="rc-h9b-e2e-"))
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="rc-h9b-e2e-home-")))
    session_ctx = SessionContext(cwd=cwd, model_label=model, bare=True)
    model_ref = parse_model_ref(model)
    session = Session(
        cwd=cwd, model_ref=model_ref, model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="rc-h9b-e2e-state-")), model_label=model,
        session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=max_turns,
        permission_engine=PermissionEngine(mode=permission_mode, cwd=cwd),
        agents=(agents if agents is not None else {}), routes={},
    )
    return session


def _new_session_with_log(base_session, session_log, *, model=None, agents=None):
    """A SECOND, independent Session/AgentRuntime that reuses `base_
    session`'s own cwd/mock creds/state_dir but a caller-supplied
    `session_log` (typically a fresh `SessionLog` pointed at the SAME
    session_id with its nodes reloaded from disk via `.read_all()`) --
    simulates exactly what a `-c` resume gives a fresh process: brand new
    in-memory state, reading the SAME on-disk session directory `base_
    session` already wrote to. Deliberately NOT `_new_session()` again,
    which would repoint BRIDGE_TEST_HOME at a fresh temp dir and defeat the
    whole point of testing resume/reconstruction behavior (findings 27, 28)."""
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.permissions import PermissionEngine
    from rolo_claude.providers.stream import ProviderCreds

    model = model or base_session.model_ref.raw
    cwd = base_session.cwd
    return Session(
        cwd=cwd, model_ref=parse_model_ref(model), model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=base_session.creds.base_url, api_key="k"),
        state_dir=base_session.state_dir, model_label=model,
        session_context=SessionContext(cwd=cwd, model_label=model, bare=True),
        openrouter_base_url=base_session.openrouter_base_url, max_turns=10,
        permission_engine=PermissionEngine(mode="auto", cwd=cwd),
        agents=(agents if agents is not None else {}), routes={}, session_log=session_log,
    )


def _general_purpose_spec(**overrides):
    from rolo_claude.config.agents_md import AgentSpec
    kwargs = dict(name="general-purpose", description="general purpose sub-agent",
                  tools=None, disallowed_tools=["Agent", "Task"], body="You are a helpful sub-agent.")
    kwargs.update(overrides)
    return AgentSpec(**kwargs)


def _drain(session, prompt) -> list:
    return list(session.turn(prompt))


def _run_cli(fh, mock, prompt, extra_args=None, extra_env=None, timeout=30, model="or:mock/model"):
    env = dict(os.environ)
    env.update({
        "BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
        "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR),
    })
    if extra_env:
        env.update(extra_env)
    args = [sys.executable, "-m", "rolo_claude", "-p", prompt, "--model", model,
            "--cwd", str(fh["proj"])] + (extra_args or [])
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


# =============================================================================
# finding 3: sub-agent job registries owned/killed by the parent, SIGHUP
# =============================================================================

@test
def test_h9b_f03_child_background_bash_job_is_tracked_and_killed_by_parent_registry(ctx: Ctx):
    """Every sub-agent Session used to build its own PRIVATE JobRegistry, so
    a background Bash job started INSIDE a sub-agent was invisible to the
    parent's own kill_all() (Controller.quit(), `-p`'s own `finally`).
    `Session(job_registry=...)` now lets `agent/subagent.py`'s
    `_build_child_session` share the PARENT's registry with every child."""
    mock = MockUpstream().start()
    try:
        SCENARIOS["h9b-f03-parent"] = ScriptedTurns([
            _tool_call_step("Task", {"description": "bg agent", "prompt": "start a background job",
                                      "subagent_type": "general-purpose", "model": "or:mock/h9b-f03-child",
                                      "run_in_background": True}),
            _text_step("started the sub-agent"),
        ])
        SCENARIOS["h9b-f03-child"] = ScriptedTurns([
            _tool_call_step("Bash", {"command": "sleep 20", "run_in_background": True}, call_id="call_bg_bash"),
            _text_step("kicked off a background bash job"),
        ])

        session = _new_session(mock=mock, model="or:mock/h9b-f03-parent",
                                agents={"general-purpose": _general_purpose_spec()})
        _drain(session, "please run a long background job via a sub-agent")

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not session.job_registry.jobs:
            time.sleep(0.05)
        ctx.check("the CHILD's own background Bash job is visible on the PARENT's job_registry",
                  bool(session.job_registry.jobs))
        job_id = next(iter(session.job_registry.jobs))
        ctx.check("the job is genuinely still running (not already finished)",
                  session.job_registry.jobs[job_id].status == "running")

        # Simulates Controller.quit()/headless.py's own `finally` -- must
        # reach a job the CHILD started, using only the PARENT's own handle.
        session.job_registry.kill_all()
        ok = False
        deadline2 = time.monotonic() + 10.0
        while time.monotonic() < deadline2:
            if session.job_registry.jobs[job_id].status == "killed":
                ok = True
                break
            time.sleep(0.1)
        ctx.check("the parent's kill_all() killed the child-started job", ok)
    finally:
        mock.stop()


@test
def test_h9b_f03_child_background_bash_job_notice_reaches_the_parent(ctx: Ctx):
    """A job's completion notice used to be pushed onto the CHILD Session's
    own `_pending_job_notices` -- nothing ever drains that list again once
    a foreground child's single turn ends (a background child's own thread
    exits too), so the notice was silently lost forever. Sharing the
    registry means `JobRegistry.parent` is fixed to the true top-level
    session (whichever Session builds a fresh one -- every child now passes
    one in instead), so the notice lands where a NEXT turn can actually
    apply it."""
    mock = MockUpstream().start()
    try:
        SCENARIOS["h9b-f03b-parent"] = ScriptedTurns([
            _tool_call_step("Task", {"description": "bg agent", "prompt": "start a short background job",
                                      "subagent_type": "general-purpose", "model": "or:mock/h9b-f03b-child",
                                      "run_in_background": True}),
            _text_step("started the sub-agent"),
        ])
        SCENARIOS["h9b-f03b-child"] = ScriptedTurns([
            _tool_call_step("Bash", {"command": "sleep 0.3 && echo child-bash-notice-marker",
                                      "run_in_background": True}, call_id="call_bg_bash2"),
            _text_step("kicked off a short background bash job"),
        ])

        session = _new_session(mock=mock, model="or:mock/h9b-f03b-parent",
                                agents={"general-purpose": _general_purpose_spec()})
        _drain(session, "please run a short background job via a sub-agent")

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not session._pending_job_notices:
            time.sleep(0.1)
        ctx.check("the child-started job's notice reached the PARENT's own pending list",
                  bool(session._pending_job_notices))
        ctx.check("the notice carries the job's real output",
                  any("child-bash-notice-marker" in t for t in session._pending_job_notices))
        session.job_registry.kill_all()
    finally:
        mock.stop()


@test
def test_h9b_f03_sighup_kills_the_process_and_its_background_job_on_posix(ctx: Ctx):
    """Verified (finding 3): SIGHUP to a `-p` run -- what closing the
    terminal or an SSH drop actually sends -- used to exit -1 and leave the
    background job running (only SIGTERM was handled). cli.py now installs
    a SIGHUP handler too; this drives the REAL CLI in a subprocess and
    checks with `pgrep` that the background job is really gone afterward
    (the brief's own acceptance line, automated).

    WSL-verified test-authoring gotcha: a `# comment` marker does NOT
    survive into any process's own argv on real Linux -- `bash -lc "sleep
    30 # marker"` is a single simple command, so bash's own "one command"
    optimization execs directly into `sleep 30`, dropping "bash" (and the
    whole comment) from `ps`/`pgrep -f` entirely (confirmed empirically:
    `ps` showed a bare `sleep 10` with no trace of the marker). Using a
    compound script (`sleep 30; : marker`) instead defeats that
    optimization -- bash MUST stay alive to run the `:` no-op afterward, so
    `pgrep -f` reliably finds the marker in ITS OWN cmdline for the whole
    sleep duration, with a real `sleep` child underneath it either way."""
    if sys.platform == "win32" or not hasattr(signal, "SIGHUP"):
        raise SkipTest("SIGHUP is POSIX-only")
    if shutil.which("pgrep") is None:
        raise SkipTest("pgrep not available on this box")

    fh = build_fake_home()
    mock = MockUpstream().start()
    marker = f"h9b-sighup-marker-{uuid.uuid4().hex[:10]}"
    try:
        SCENARIOS["h9b-f03-sighup"] = ScriptedTurns([
            _tool_call_step("Bash", {"command": f"sleep 30; : {marker}", "run_in_background": True},
                             call_id="call_sighup_bg"),
            _text_step("started it"),
        ])
        env = dict(os.environ)
        env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                     "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
        proc = subprocess.Popen(
            [sys.executable, "-m", "rolo_claude", "-p", "start a background job then say ok",
             "--model", "or:mock/h9b-f03-sighup", "--cwd", str(fh["proj"]),
             "--permission-mode", "auto"],
            env=env, cwd=str(REPO_DIR), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            found = False
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                r = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
                if r.returncode == 0 and r.stdout.strip():
                    found = True
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.2)
            ctx.check("the background sleep genuinely started (pgrep found it)", found)

            proc.send_signal(signal.SIGHUP)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                ctx.check("process exited on SIGHUP within 15s", False)
                return

            gone = False
            deadline2 = time.monotonic() + 15.0
            while time.monotonic() < deadline2:
                r = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
                if r.returncode != 0 or not r.stdout.strip():
                    gone = True
                    break
                time.sleep(0.2)
            ctx.check(f"pgrep no longer finds the background job after SIGHUP (still found: {not gone})", gone)
        finally:
            if proc.poll() is None:
                proc.kill()
            subprocess.run(["pkill", "-f", marker], capture_output=True)
    finally:
        mock.stop()


# =============================================================================
# finding 12: the session's own PATH (settings env.PATH / the PATH
# rolo-claude was started with) survives a Debian/Kali /etc/profile login
# shell that unconditionally reassigns PATH -- both the foreground and
# background Bash paths
# =============================================================================

@test
def test_h9b_f12_session_path_survives_a_debian_profile_login_shell_reset(ctx: Ctx):
    """Verified bug (finding 12): `bash -lc` is a LOGIN shell -- it sources
    /etc/profile BEFORE ever running the harness's own -c script body.
    Debian/Kali's stock /etc/profile unconditionally reassigns PATH,
    discarding whatever PATH this process actually started with (settings
    env.PATH, a SessionStart env-file export, or just $PATH itself).
    Reproduces the review's OWN repro technique exactly: `unshare -rm` (no
    sudo, a real user-namespace + mount-namespace) bind-mounts the REAL
    Debian/Kali /etc/profile (tests/helpers/debian_etc_profile.txt, fetched
    verbatim from the Kali VM this harness targets) over WSL Ubuntu's own,
    so a plain `bash -lc` on WSL sees EXACTLY what `bash -lc` sees on real
    Kali/Debian. Covers BOTH the foreground and background Bash paths in
    one real `-p` CLI invocation, using the review's own example tool name
    (`rvtool`, from a PATH entry the profile reset would otherwise erase)."""
    if sys.platform == "win32":
        raise SkipTest("unshare/bind-mount is Linux-only")
    if shutil.which("unshare") is None:
        raise SkipTest("unshare not available on this box")
    probe = subprocess.run(["unshare", "-rm", "true"], capture_output=True, text=True)
    if probe.returncode != 0:
        raise SkipTest(f"unshare -rm not usable on this box (unprivileged user namespaces "
                        f"disabled?): {probe.stderr.strip()!r}")

    profile_path = REPO_DIR / "tests" / "helpers" / "debian_etc_profile.txt"
    fh = build_fake_home()

    # A PATH entry holding a tool the review's own repro names -- NOT on
    # the stock PATH list the Debian profile resets to, so finding it
    # proves the harness's OWN restored PATH won, not the profile's.
    marker_dir = Path(tempfile.mkdtemp(prefix="rc-f12-path-"))
    rvtool = marker_dir / "rvtool"
    rvtool.write_text("#!/bin/sh\necho RVTOOL_RAN_OK\n", encoding="utf-8")
    rvtool.chmod(0o755)
    bg_out_path = Path(tempfile.mkdtemp(prefix="rc-f12-bgout-")) / "bg.txt"

    mock = MockUpstream().start()
    try:
        def _final_text_after_a_beat(h, body):
            # Gives the background job's own near-instant `echo` a moment
            # to finish and flush BEFORE `-p` mode's own `finally:` block
            # (headless.py) calls `job_registry.kill_all()` on exit -- not
            # needed for correctness (the PATH fix itself is synchronous,
            # already true the instant the job's shell starts), only to
            # keep this check from racing this OWN test's file read.
            time.sleep(0.3)
            # -p's own plain-text stdout is only ever the model's FINAL
            # text (verified: tool_result content never reaches it on its
            # own) -- so the foreground check has to happen HERE, by
            # relaying what the tool_result for call_fg actually said back
            # through the (mock) model's own reply, the same way a real
            # model summarizing a tool's output would.
            fg_text = "(no call_fg tool_result found)"
            for m in (body or {}).get("messages") or []:
                if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id") == "call_fg":
                    fg_text = str(m.get("content") or "")
                    break
            _finish(h, [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                         {"choices": [{"index": 0, "delta": {"content": f"ran both -- foreground said: {fg_text}"}}]},
                         {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}])

        SCENARIOS["h9b-f12-path"] = ScriptedTurns([
            _tool_call_step("Bash", {"command": "rvtool"}, call_id="call_fg"),
            _tool_call_step("Bash", {"command": f"rvtool > {bg_out_path} 2>&1", "run_in_background": True},
                             call_id="call_bg"),
            _final_text_after_a_beat,
        ])
        env = dict(os.environ)
        env.update({
            "BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
            "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR),
            # The PATH this process was "started with" -- exactly what
            # finding 12 says a Debian/Kali login shell discards.
            "PATH": f"{marker_dir}:{os.environ.get('PATH', '')}",
        })
        inner = (f"mount --bind {shlex.quote(str(profile_path))} /etc/profile && "
                 f"exec {shlex.quote(sys.executable)} -m rolo_claude -p "
                 f"{shlex.quote('run rvtool in the foreground, then again in the background')} "
                 f"--model or:mock/h9b-f12-path --cwd {shlex.quote(str(fh['proj']))} --permission-mode auto")
        args = ["unshare", "-rm", "bash", "-c", inner]
        result = subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=30)
        ctx.check(f"exit 0 under the hostile Debian profile, got {result.returncode}, "
                  f"stdout={result.stdout!r} stderr={result.stderr[-800:]!r}", result.returncode == 0)
        ctx.check(f"the FOREGROUND Bash call found rvtool despite the profile's PATH reset, "
                  f"got {result.stdout!r}", "RVTOOL_RAN_OK" in result.stdout)

        bg_out = ""
        try:
            bg_out = bg_out_path.read_text(encoding="utf-8")
        except OSError:
            pass
        ctx.check(f"the BACKGROUND Bash call ALSO found rvtool despite the profile's PATH reset, "
                  f"got {bg_out!r}", "RVTOOL_RAN_OK" in bg_out)
    finally:
        mock.stop()


# =============================================================================
# finding 24: bin/rolo-claude resolves a SYMLINK to its real location
# (readlink -f) before deriving repo_root/PYTHONPATH, instead of using the
# symlink's own directory
# =============================================================================

@test
def test_h9b_f24_bin_rolo_claude_runs_via_a_real_symlink_from_outside_the_checkout(ctx: Ctx):
    """Verified bug (finding 24): `dirname "$0"` alone only ever gives the
    directory of WHATEVER PATH invoked the script -- for the documented
    "no install at all: ln -s .../bin/rolo-claude ~/bin/" recipe, that is
    the SYMLINK's own directory, never the checkout it actually points at,
    so `repo_root`/PYTHONPATH pointed nowhere near `rolo_claude` and the
    real binary failed with "No module named rolo_claude" from any
    directory outside the checkout. This creates a REAL symlink (a Git-
    Bash `ln -s` on Windows silently makes a plain file COPY instead, not
    a real symlink -- confirmed via `stat`, hence POSIX-only here) in a
    temp dir OUTSIDE the checkout and runs it from a cwd OUTSIDE the
    checkout too, with $HOME pointed at an empty temp dir (so the
    `~/.local/bin/rolo-claude` console-script fast path -- which DOES
    exist for real on this box otherwise, see the "installed" branch's own
    comment -- never fires, forcing exactly the code path this finding's
    `readlink -f` fix touches)."""
    if sys.platform == "win32":
        raise SkipTest("a real symlink (not Git Bash's file-copy fallback) is POSIX-only")

    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        SCENARIOS["h9b-f24-symlink"] = ScriptedTurns([_text_step("ran via the symlink")])

        bin_dir = Path(tempfile.mkdtemp(prefix="rc-f24-bin-"))
        link_path = bin_dir / "rolo-claude"
        os.symlink(str(REPO_DIR / "bin" / "rolo-claude"), str(link_path))
        ctx.check("a REAL symlink was created (not a copy)", link_path.is_symlink())

        empty_home = Path(tempfile.mkdtemp(prefix="rc-f24-home-"))
        outside_cwd = Path(tempfile.mkdtemp(prefix="rc-f24-cwd-"))

        env = dict(os.environ)
        env.update({
            "HOME": str(empty_home),  # no ~/.local/bin/rolo-claude console script to fast-path into
            "BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
            "OPENROUTER_API_KEY": "test-key",
        })
        env.pop("PYTHONPATH", None)  # the fix itself must derive this, not inherit it from the test
        result = subprocess.run(
            [str(link_path), "-p", "say hi via the symlink", "--model", "or:mock/h9b-f24-symlink",
             "--cwd", str(fh["proj"]), "--permission-mode", "auto"],
            env=env, cwd=str(outside_cwd), capture_output=True, text=True, timeout=30,
        )
        ctx.check(f"exit 0 when invoked via a symlink from outside the checkout, "
                  f"got {result.returncode}, stdout={result.stdout!r} stderr={result.stderr[-800:]!r}",
                  result.returncode == 0)
        ctx.check("never the pre-fix failure mode", "No module named rolo_claude" not in result.stderr)
        ctx.check(f"the real answer came through, got {result.stdout!r}", "ran via the symlink" in result.stdout)
    finally:
        mock.stop()


# =============================================================================
# findings 4 + 5: ONE secrets sanitizer, CLAUDE_ENV_FILE sourced with the
# stripped env
# =============================================================================

@test
def test_h9b_f04_sanitize_text_covers_every_previously_missed_shape(ctx: Ctx):
    from rolo_claude.export_cli import sanitize_text

    fake_key = "sk-or-v1-" + "a1b2c3" * 6
    cases = [
        f'export OPENROUTER_API_KEY="{fake_key}"',            # quoted shell export
        f"export OPENROUTER_API_KEY='{fake_key}'",             # single-quoted shell export
        f'"OPENROUTER_API_KEY": "{fake_key}"',                 # JSON key form (settings.json env block)
        fake_key,                                                # bare sk-or-v1- token, no name at all
        "sk-proj-" + "b" * 20,                                   # sk-proj- shape
        "SLACK_BOT_TOKEN=xoxb-should-be-redacted-by-name",     # generic *TOKEN* name, not in the fixed list
        '"api_key": "lowercase-json-key-should-also-redact"',  # lowercase JSON key (the dead session_cli.py's
                                                                  # own _SECRET_KEYS case-insensitive coverage)
    ]
    for text in cases:
        cleaned = sanitize_text(text)
        ctx.check(f"the secret is gone from {text!r}, got {cleaned!r}",
                  fake_key not in cleaned and "xoxb-should-be-redacted" not in cleaned
                  and "lowercase-json-key-should-also-redact" not in cleaned)
        ctx.check(f"a redaction marker took its place in {cleaned!r}", "<redacted>" in cleaned)

    # No false positives: ordinary prose using "token"/"secret" as plain
    # English words (never immediately followed by `:`/`=`) must survive
    # untouched.
    prose = "This authentication token was issued yesterday and the secret sauce is great."
    ctx.check("plain prose is never touched", sanitize_text(prose) == prose)


@test
def test_h9b_f04_sanitize_node_stays_valid_json_for_a_settings_env_block(ctx: Ctx):
    """The whole POINT of round-tripping through json.dumps/json.loads in
    sanitize_node is that a redaction must never corrupt the JSON structure
    it's embedded in -- verified against a realistic settings.json `env`
    block nested inside a Read tool_result's own content string."""
    from rolo_claude.export_cli import sanitize_node

    fake_key = "sk-or-v1-" + "c3d4e5" * 6
    settings_blob = json.dumps({"env": {"OPENROUTER_API_KEY": fake_key, "PATH": "/usr/bin:/bin"}}, indent=2)
    node = {"type": "tool_result", "tool_use_id": "call_1",
            "content": f"Contents of settings.json:\n{settings_blob}"}
    cleaned = sanitize_node(node)
    ctx.check("sanitize_node did not fall back to the error placeholder",
              "_sanitize_error" not in cleaned)
    reserialized = json.dumps(cleaned)
    ctx.check(f"the secret is gone, got {reserialized!r}", fake_key not in reserialized)
    ctx.check("ordinary content (PATH) survives untouched", "/usr/bin:/bin" in reserialized)
    # And the redacted blob, re-extracted, must ITSELF still be valid JSON
    # (proves no stray unescaped quote/backslash was introduced).
    inner = cleaned["content"].split("\n", 1)[1]
    reparsed = json.loads(inner)
    ctx.check("the env block round-trips as valid JSON after redaction",
              reparsed["env"]["OPENROUTER_API_KEY"] == "<redacted>" and reparsed["env"]["PATH"] == "/usr/bin:/bin")


@test
def test_h9b_f04_export_cli_and_tui_export_use_the_same_sanitizer(ctx: Ctx):
    """H9 whole-tree review finding 4: ONE sanitizer for both `rolo-claude
    export --sanitize` (export_cli.sanitize_text) and the TUI's own
    `/export --sanitize` (controller.sanitize_transcript) -- not two
    independently-maintained regexes that drift (the TUI's OLD one missed
    `OPENROUTER_API_KEY=` entirely: no `\\b` between `_` and `KEY`)."""
    from rolo_claude.export_cli import sanitize_text
    from rolo_claude.controller import sanitize_transcript

    fake_key = "sk-or-v1-" + "f6a7b8" * 6
    cases = [
        f'export OPENROUTER_API_KEY="{fake_key}"',
        f"Authorization: Bearer {'x' * 24}",
        f'"DATABRICKS_TOKEN": "{fake_key}"',
    ]
    for text in cases:
        a, b = sanitize_text(text), sanitize_transcript(text)
        ctx.check(f"export_cli and controller sanitizers agree on {text!r}: {a!r} vs {b!r}", a == b)
        ctx.check(f"and both actually redacted it: {a!r}", fake_key not in a and "x" * 24 not in a or "Bearer" in text)


@test
def test_h9b_f04_session_cli_module_is_gone(ctx: Ctx):
    """The dead third sanitizer (session_cli.py -- nothing ever called it,
    per finding 4/34) is deleted outright, not just unused."""
    import importlib
    try:
        importlib.import_module("rolo_claude.session_cli")
        ctx.check("rolo_claude.session_cli must no longer exist", False)
    except ModuleNotFoundError:
        pass


@test
def test_h9b_f04_cli_export_sanitize_redacts_a_quoted_export_and_json_env_block(ctx: Ctx):
    """End to end through the real wired `rolo-claude export --sanitize`
    CLI (cli.py -> export_cli.cmd_export), the way a user actually runs it
    -- a Bash tool_result containing a QUOTED `export KEY="..."` line (the
    review's own verified repro: `cat ~/.bashrc`) and a settings.json `env`
    block, both in ONE session log."""
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    from rolo_claude.agent.log import SessionLog

    fake_key = "sk-or-v1-" + "112233" * 6
    log = SessionLog(fh["proj"], session_id="h9b-f04-cli-export")
    log.append_meta(model="or:mock/x", cwd=str(fh["proj"]), system_prompt_bytes=1, tools=[])
    log.append_user([{"type": "text", "text": "cat ~/.bashrc"}])
    log.append_assistant(content=[{"type": "tool_use", "id": "call_1", "name": "Bash",
                                    "input": {"command": "cat ~/.bashrc"}}], stop_reason="tool_use")
    log.append_tool_result(tool_use_id="call_1",
                            content=f'export OPENROUTER_API_KEY="{fake_key}"\nexport PATH="$PATH:/opt/x"',
                            is_error=False)
    settings_blob = json.dumps({"env": {"OPENROUTER_API_KEY": fake_key}})
    log.append_tool_result(tool_use_id="call_2", content=f"settings.json:\n{settings_blob}", is_error=False)

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_DIR)
    result = subprocess.run(
        [sys.executable, "-m", "rolo_claude", "export", "--sanitize", "--cwd", str(fh["proj"]),
         "--session", "h9b-f04-cli-export"],
        env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=30,
    )
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    ctx.check("the quoted export's secret is gone", fake_key not in result.stdout)
    ctx.check("the JSON env block's secret is gone too (same key, second occurrence)",
              result.stdout.count(fake_key) == 0)
    ctx.check("still valid JSONL after sanitizing",
              all(json.loads(l) for l in result.stdout.splitlines() if l.strip()))


@test
def test_h9b_f05_session_start_hook_env_file_line_referencing_the_real_key_sees_nothing(ctx: Ctx):
    """H9 whole-tree review finding 5: a SessionStart hook process itself
    already sees the stripped env (tool_child_env) -- the leak was in the
    SUBPROCESS that later SOURCES $CLAUDE_ENV_FILE (`_fire_session_start` ->
    `read_env_file_exports`), which used to default `base_env` to the
    harness's own raw, unstripped `os.environ` whenever the caller omitted
    it (the one caller in agent/loop.py did). A hook-written line that
    references `$OPENROUTER_API_KEY` must see NOTHING once that call passes
    `base_env=self.tool_env` (already stripped)."""
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.log import SessionLog
    from rolo_claude.agent.loop import Session
    from rolo_claude.hooks import HookDef, HookRunner
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.permissions import PermissionEngine
    from rolo_claude.providers.stream import ProviderCreds

    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
        real_secret = "sk-or-v1-REALSECRETVALUE-h9bf05-should-never-leak"
        session_id = "h9b-f05-envfile-leak-probe"
        session_log = SessionLog(fh["proj"], session_id=session_id)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_DIR)
        # The REAL process env this whole test runs in DOES carry the
        # secret (settings.effective_env's "shell env" layer reads real
        # os.environ) -- proving `tool_child_env` really did strip it out
        # of `session.tool_env`, and that THAT stripped dict, not this raw
        # one, is what the env-file-sourcing subprocess actually ran with.
        env["OPENROUTER_API_KEY"] = real_secret
        _old_openrouter_key = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = real_secret
        hook_runner = HookRunner(
            {"SessionStart": [HookDef(type="command",
                                       args=_HOOK_SCRIPT_ARGV + ["env_file_writer_secret_leak_probe"])]},
            cwd=fh["proj"], session_id=session_id, transcript_path=str(session_log.path), effective_env=env,
        )
        session_ctx = SessionContext(cwd=fh["proj"], model_label="or:mock/hook-envfile-leak")
        model_ref = parse_model_ref("or:mock/hook-envfile-leak")
        session = Session(
            cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
            creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="h9b-f05-state-")), model_label="or:mock/hook-envfile-leak",
            session_context=session_ctx, session_log=session_log, openrouter_base_url=mock.base_url, max_turns=10,
            permission_engine=PermissionEngine(mode="auto", cwd=fh["proj"]), hook_runner=hook_runner,
        )
        ctx.check("session.tool_env itself never carried the real key (tool_child_env stripped it)",
                  "OPENROUTER_API_KEY" not in session.tool_env)
        probe_value = session.tool_env.get("ROLO_H9B_LEAK_PROBE")
        ctx.check(f"the hook-written $OPENROUTER_API_KEY reference expanded to NOTHING, got {probe_value!r}",
                  probe_value == "")
        ctx.check("and definitely not the real secret value", probe_value != real_secret)
    finally:
        mock.stop()
        if _old_openrouter_key is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = _old_openrouter_key


# =============================================================================
# finding 9: `-p --output-format json` prints exactly ONE JSON object and
# keeps the real exit code
# =============================================================================

@test
def test_h9b_f09_background_notice_folds_into_the_one_json_result_object(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        SCENARIOS["h9b-f09-json"] = ScriptedTurns([
            _tool_call_step("Bash", {"command": "sleep 0.3 && echo f09-bg-marker", "run_in_background": True},
                             call_id="call_f09_bg"),
            _text_step("final answer text"),
        ])
        result = _run_cli(fh, mock, "start a short job then answer", model="or:mock/h9b-f09-json",
                           extra_args=["--output-format", "json", "--permission-mode", "auto"],
                           timeout=30)
        ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr[-500:]!r}", result.returncode == 0)
        stdout = result.stdout.strip()
        lines = [l for l in stdout.splitlines() if l.strip()]
        ctx.check(f"stdout is exactly ONE JSON object (one non-empty line), got {len(lines)}: {stdout!r}",
                  len(lines) == 1)
        try:
            obj = json.loads(stdout)
        except json.JSONDecodeError as e:
            ctx.check(f"the single line parses as JSON (no 'Extra data'), got {e}: {stdout!r}", False)
            return
        ctx.check("the real turn's own answer is the result text", obj.get("result") == "final answer text")
        ctx.check("is_error is False (a real success turn)", obj.get("is_error") is False)
        ctx.check("background_notices carries the job's completion",
                  any("f09-bg-marker" in n for n in obj.get("background_notices") or []))
    finally:
        mock.stop()


@test
def test_h9b_f09_background_notice_never_overwrites_a_failed_turns_exit_code(ctx: Ctx):
    """Verified bug: a turn that failed with `error_during_execution`,
    followed by a background job's own always-`end_turn` notice, used to
    print 2 JSON objects AND exit 0 (the notice's own success silently
    overwrote the real failure's exit code)."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        SCENARIOS["h9b-f09-fail"] = ScriptedTurns([
            _tool_call_step("Bash", {"command": "sleep 0.3 && echo f09-fail-marker", "run_in_background": True},
                             call_id="call_f09_fail_bg"),
            # A 400 (never >=500/429, and no OpenAI-404-style special case)
            # is NOT retryable (providers/errors.py's own is_retryable_
            # message) -- fails on the FIRST attempt, no multi-second
            # backoff wait, so this test stays fast and deterministic.
            lambda handler, body: __import__("tests.helpers.mock_openai", fromlist=["send_json_response"])
                .send_json_response(handler, 400, {"error": {"message": "mock upstream deliberate bad request"}}),
        ])
        result = _run_cli(fh, mock, "start a short job then hit an error", model="or:mock/h9b-f09-fail",
                           extra_args=["--output-format", "json", "--permission-mode", "auto"],
                           timeout=30)
        ctx.check(f"exit 1 on a genuinely failed turn, got {result.returncode}", result.returncode == 1)
        stdout = result.stdout.strip()
        lines = [l for l in stdout.splitlines() if l.strip()]
        ctx.check(f"still exactly ONE JSON object even with a background notice pending, got {len(lines)}",
                  len(lines) == 1)
        obj = json.loads(stdout)
        ctx.check("is_error is True -- never silently overwritten by the notice", obj.get("is_error") is True)
        ctx.check("subtype names the real failure", obj.get("subtype") == "error_during_execution")
    finally:
        mock.stop()


@test
def test_h9b_f09_text_mode_still_shows_both_the_answer_and_the_notice(ctx: Ctx):
    """The text-output format has no separate JSON field to carry a notice
    in -- it must still appear as extra visible text after the real
    answer, not get silently dropped by the `finish=False` refactor."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        SCENARIOS["h9b-f09-text"] = ScriptedTurns([
            _tool_call_step("Bash", {"command": "sleep 0.3 && echo f09-text-marker", "run_in_background": True},
                             call_id="call_f09_text_bg"),
            _text_step("the real final answer"),
        ])
        result = _run_cli(fh, mock, "start a short job then answer", model="or:mock/h9b-f09-text",
                           extra_args=["--permission-mode", "auto"], timeout=30)
        ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
        ctx.check("the real answer text is present", "the real final answer" in result.stdout)
        ctx.check("the background notice text is ALSO present", "f09-text-marker" in result.stdout)
    finally:
        mock.stop()


# =============================================================================
# finding 13: sub-agent usage/cost rolls up into the parent's CostMeter,
# --max-budget-usd, and stats
# =============================================================================

def _cost_chunk(prompt_tokens: int, completion_tokens: int, cost: float) -> dict:
    return {"choices": [], "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                                      "cost": cost}}


@test
def test_h9b_f13_child_usage_and_cost_roll_up_into_the_parents_cost_meter_and_log(ctx: Ctx):
    """Verified bug: a parent that spawned 2 children left 1 usage node in
    the parent log while the children's usage nodes existed only in
    subagents/*.jsonl, invisible to the parent's own CostMeter. Drives a
    REAL foreground child through run_agent_call and checks BOTH the live
    `parent.cost_meter` (what --max-budget-usd/the TUI status bar read) and
    `parent.log.nodes()` (what /stats -- Controller.session_stats, which
    reads ONLY the current session's own log -- reads)."""
    from rolo_claude.agent.subagent import run_agent_call
    from rolo_claude.controller import compute_session_stats

    mock = MockUpstream().start()
    try:
        SCENARIOS["h9b-f13-child"] = ScriptedTurns([
            [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
             {"choices": [{"index": 0, "delta": {"content": "child's own answer"}}]},
             {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
             _cost_chunk(1000, 500, 0.05)],
        ])
        session = _new_session(mock=mock, model="or:mock/h9b-f13-parent-unused",
                                agents={"general-purpose": _general_purpose_spec()})
        before_total = session.cost_meter.total_usd
        before_turns = session.cost_meter.turns

        _events, result = run_agent_call(
            runtime=session.agent_runtime, tool_id="toolu_f13", tool_name="Task",
            tool_input={"description": "spend", "prompt": "answer something",
                        "subagent_type": "general-purpose", "model": "or:mock/h9b-f13-child"},
        )
        ctx.check(f"the child call itself did not error, got {result.content!r}", not result.is_error)

        ctx.check(f"parent.cost_meter.total_usd increased by the child's real cost, "
                  f"was {before_total}, now {session.cost_meter.total_usd}",
                  session.cost_meter.total_usd >= before_total + 0.05 - 1e-9)
        ctx.check("parent.cost_meter.turns increased by the child's own turn count",
                  session.cost_meter.turns == before_turns + 1)

        usage_nodes = [n for n in session.log.nodes() if n.get("type") == "usage"]
        tagged = [n for n in usage_nodes if n.get("agent_id")]
        ctx.check(f"a rolled-up usage node, tagged with an agent_id, is in the PARENT's own log, "
                  f"got {usage_nodes!r}", len(tagged) == 1)
        ctx.check("the tagged node carries the child's real cost", tagged[0].get("cost_usd") == 0.05)
        ctx.check("and its real token counts", tagged[0]["usage"].get("input_tokens") == 1000
                  and tagged[0]["usage"].get("output_tokens") == 500)

        stats = compute_session_stats(session.log.nodes())
        ctx.check(f"compute_session_stats (the live /stats command's own data source) sees the "
                  f"rolled-up cost too, got {stats['total_cost_usd']}", stats["total_cost_usd"] >= 0.05 - 1e-9)
    finally:
        mock.stop()


@test
def test_h9b_f13_max_budget_usd_trips_on_subagent_spend_the_parent_alone_never_would(ctx: Ctx):
    """End to end through the real `-p --max-budget-usd` CLI path: the
    PARENT's own two calls are cheap (well under budget individually AND
    combined); only once the CHILD's real cost rolls in does the combined
    total cross the budget. Before the fix, this could never trip on
    sub-agent spend at all (the finding's own words)."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        SCENARIOS["h9b-f13-cli-parent"] = ScriptedTurns([
            [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
             {"choices": [{"index": 0, "delta": {"tool_calls": [
                 {"index": 0, "id": "call_spend", "type": "function",
                  "function": {"name": "Task", "arguments": json.dumps(
                      {"description": "spend", "prompt": "go", "subagent_type": "general-purpose",
                       "model": "or:mock/h9b-f13-cli-child"})}},
             ]}}]},
             {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
             _cost_chunk(50, 10, 0.001)],
            [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
             {"choices": [{"index": 0, "delta": {"content": "all done"}}]},
             {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
             _cost_chunk(50, 10, 0.001)],
        ])
        SCENARIOS["h9b-f13-cli-child"] = ScriptedTurns([
            [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
             {"choices": [{"index": 0, "delta": {"content": "expensive child work"}}]},
             {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
             _cost_chunk(1000, 500, 0.05)],
        ])
        env = dict(os.environ)
        env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
                     "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
        args = [sys.executable, "-m", "rolo_claude", "-p", "spend via a sub-agent then finish",
                "--model", "or:mock/h9b-f13-cli-parent", "--cwd", str(fh["proj"]),
                "--output-format", "json", "--permission-mode", "auto", "--max-budget-usd", "0.01"]
        result = subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=30)
        ctx.check(f"exit 1 (budget exceeded), got {result.returncode}, stderr={result.stderr[-500:]!r}",
                  result.returncode == 1)
        obj = json.loads(result.stdout.strip().splitlines()[-1])
        ctx.check(f"subtype is error_max_budget_usd, got {obj.get('subtype')!r}",
                  obj.get("subtype") == "error_max_budget_usd")
    finally:
        mock.stop()


# =============================================================================
# finding 27: `_resume_task` races a still-running background child instead
# of refusing, and `runtime.tasks` is in-memory-only so every task_id comes
# back "Unknown" after a `-c` resume
# =============================================================================

@test
def test_h9b_f27_resuming_a_still_running_background_task_is_refused_not_raced(ctx: Ctx):
    """Verified bug (finding 27, concurrent-resume half): `_resume_task`
    never checked whether a background task's OWN `_bg_run` thread was
    still appending to `agent-*.jsonl` -- a resume attempted while it ran
    would build a SECOND Session on top of the same log/meta.json files,
    racing writes. Uses a deliberately slow (0.8s) upstream reply so the
    background thread is provably still in flight when the resume attempt
    lands, then again once it has genuinely finished."""
    from rolo_claude.agent.subagent import run_agent_call

    def _slow_then_reply(h, body):
        time.sleep(0.8)
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": "slow child done"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])

    mock = MockUpstream().start()
    try:
        SCENARIOS["h9b-f27-slow-child"] = ScriptedTurns([_slow_then_reply])
        SCENARIOS["h9b-f27-resumed"] = ScriptedTurns([_text_step("resumed after completion")])
        session = _new_session(mock=mock, model="or:mock/h9b-f27-parent-unused",
                                agents={"general-purpose": _general_purpose_spec()})

        _events, result = run_agent_call(
            runtime=session.agent_runtime, tool_id="toolu_f27a", tool_name="Task",
            tool_input={"description": "slow bg", "prompt": "go slowly", "subagent_type": "general-purpose",
                        "model": "or:mock/h9b-f27-slow-child", "run_in_background": True},
        )
        ctx.check(f"background dispatch itself did not error, got {result.content!r}", not result.is_error)
        m = re.search(r"task_id=([0-9a-f]+)", result.content)
        ctx.check(f"a task_id was reported, got {result.content!r}", m is not None)
        task_id = m.group(1)

        ctx.check("the task is marked running_in_process right after background dispatch",
                  session.agent_runtime.tasks.get(task_id, {}).get("running_in_process") is True)

        _events2, result2 = run_agent_call(
            runtime=session.agent_runtime, tool_id="toolu_f27b", tool_name="Task",
            tool_input={"task_id": task_id, "prompt": "continue now, while it's still running"},
        )
        ctx.check(f"resuming a still-running background task is refused, got {result2.content!r}",
                  result2.is_error)
        ctx.check(f"the refusal names it as still running, not merely 'Unknown', got {result2.content!r}",
                  "still running" in result2.content and "Unknown" not in result2.content)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and session.agent_runtime.tasks.get(task_id, {}).get("running_in_process"):
            time.sleep(0.02)
        ctx.check("running_in_process clears once the background thread genuinely finishes",
                  session.agent_runtime.tasks.get(task_id, {}).get("running_in_process") is False)

        _events3, result3 = run_agent_call(
            runtime=session.agent_runtime, tool_id="toolu_f27c", tool_name="Task",
            tool_input={"task_id": task_id, "model": "or:mock/h9b-f27-resumed",
                        "prompt": "continue for real, now that it's done"},
        )
        ctx.check(f"resuming AFTER completion succeeds normally, got {result3.content!r}", not result3.is_error)
    finally:
        mock.stop()


@test
def test_h9b_f27_task_map_survives_a_simulated_resume_via_disk_hydration(ctx: Ctx):
    """Verified bug (finding 27, persistence half): `AgentRuntime.tasks`
    lives only in memory, so a `-c` resume (a brand NEW process, hence a
    brand NEW, empty `AgentRuntime`) made every task_id the model still
    remembered come back "Unknown". Builds a SECOND, independent
    Session/AgentRuntime pointed at the SAME cwd + session_id as the first
    -- exactly what `-c` gives a fresh process -- and checks that a task_id
    minted under the FIRST resumes cleanly under the SECOND, purely via
    `_hydrate_tasks_from_disk` reading `subagents/*.meta.json` back."""
    from rolo_claude.agent.log import SessionLog
    from rolo_claude.agent.subagent import run_agent_call

    mock = MockUpstream().start()
    try:
        SCENARIOS["h9b-f27b-child"] = ScriptedTurns([_text_step("first process's own background answer")])
        SCENARIOS["h9b-f27b-child-resumed"] = ScriptedTurns([_text_step("second process's resumed answer")])
        model_label = "or:mock/h9b-f27b-parent-unused"
        session1 = _new_session(mock=mock, model=model_label, agents={"general-purpose": _general_purpose_spec()})

        _events, result = run_agent_call(
            runtime=session1.agent_runtime, tool_id="toolu_f27d", tool_name="Task",
            tool_input={"description": "bg", "prompt": "go", "subagent_type": "general-purpose",
                        "model": "or:mock/h9b-f27b-child", "run_in_background": True},
        )
        ctx.check(f"background dispatch did not error, got {result.content!r}", not result.is_error)
        task_id = re.search(r"task_id=([0-9a-f]+)", result.content).group(1)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and session1.agent_runtime.tasks.get(task_id, {}).get("running_in_process"):
            time.sleep(0.02)
        ctx.check("the first process's own background task finished before simulating the resume",
                  session1.agent_runtime.tasks.get(task_id, {}).get("running_in_process") is False)

        # Simulate exactly what a `-c` resume gives a fresh process: the
        # SAME cwd + session_id (so `subagents/` resolves to the SAME
        # on-disk directory `_child_log_paths` wrote into) with nodes
        # reloaded from disk (same as headless.py's own --continue
        # bootstrap: `log._nodes = log.read_all()`, which is what routes
        # `Session.__init__` into its "RESUMING an existing log" branch
        # instead of the fresh-session one), but a brand new AgentRuntime
        # whose own `tasks` dict starts empty.
        resumed_log = SessionLog(session1.cwd, session_id=session1.log.session_id)
        resumed_log._nodes = resumed_log.read_all()
        session2 = _new_session_with_log(session1, resumed_log, model=model_label,
                                          agents={"general-purpose": _general_purpose_spec()})
        ctx.check("the simulated-resume Session starts with an EMPTY in-memory task map",
                  session2.agent_runtime.tasks == {})

        _events2, result2 = run_agent_call(
            runtime=session2.agent_runtime, tool_id="toolu_f27e", tool_name="Task",
            tool_input={"task_id": task_id, "model": "or:mock/h9b-f27b-child-resumed",
                        "prompt": "continue after the simulated resume"},
        )
        ctx.check(f"resuming a task_id minted by a DIFFERENT (earlier) AgentRuntime succeeds via disk "
                  f"hydration, got {result2.content!r}", not result2.is_error)
        ctx.check("the hydrated entry is flagged reconstructed (for TaskStop's own messaging)",
                  session2.agent_runtime.tasks.get(task_id, {}).get("reconstructed") is True)
    finally:
        mock.stop()


# =============================================================================
# finding 28: committed prune stubs are session state that was never
# logged, so a resume sent a DIFFERENT (larger) wire prefix than what the
# model actually saw
# =============================================================================

def _f28_plain_user(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _f28_tool_use(tool_use_id: str) -> dict:
    return {"role": "assistant", "content": [{"type": "tool_use", "id": tool_use_id, "name": "Read", "input": {}}]}


def _f28_tool_result(tool_use_id: str, text: str) -> dict:
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": text}]}


@test
def test_h9b_f28_prune_commits_are_logged_and_rebuilt_on_resume(ctx: Ctx):
    """Verified bug (finding 28): `Session._pruned_messages_for_wire`
    (agent/loop.py) commits batches of tool_use_ids into
    `self._prune_committed_stub_ids` -- purely in-memory, never logged --
    once the pending batch reaches PRUNE_REBALANCE_CHUNK_TOKENS (agent/
    prune.py, 20,000). A resumed process's OWN `__init__` reset that set to
    empty and never rebuilt it, so the SAME raw messages pruned identically
    before a `-c` restart would come back with every previously-stubbed
    tool_result at FULL SIZE again -- a wire prefix the model never
    actually saw, breaking prompt-cache alignment and violating "a request
    must be re-derivable from the log alone" (plan rule 1).

    Sizing (exact, `compute_stub_candidates`'s own chars/4 estimator, no
    rounding): three 30,000-char OLD tool_results (7,500 tokens each,
    22,500 combined -- over the 20,000 rebalance chunk on their own) sit
    behind 5 filler turns of 36,000 chars (9,000 tokens each). Walking
    newest-first, the running "newer tokens" total crosses the 40,000-token
    protection window exactly between the newest filler turn (t1: running
    total 4*9,000 = 36,000 <= 40,000, so t1 and everything newer stays
    protected) and the oldest one (t0c: running total 5*9,000 = 45,000 >
    40,000, so t0c/t0b/t0 are all candidates) -- so the very first
    `_pruned_messages_for_wire` call commits exactly {t0, t0b, t0c} in one
    batch, none of the filler turns."""
    from rolo_claude.agent.log import SessionLog

    old1, old2, old3 = "A" * 30_000, "B" * 30_000, "C" * 30_000
    raw_messages = [
        _f28_plain_user("q0"), _f28_tool_use("t0"), _f28_tool_result("t0", old1),
        _f28_tool_use("t0b"), _f28_tool_result("t0b", old2),
        _f28_tool_use("t0c"), _f28_tool_result("t0c", old3),
    ]
    filler = "F" * 36_000
    for i in range(1, 6):
        raw_messages += [_f28_plain_user(f"q{i}"), _f28_tool_use(f"t{i}"), _f28_tool_result(f"t{i}", filler)]

    mock = MockUpstream().start()
    try:
        session1 = _new_session(mock=mock, model="or:mock/h9b-f28-unused")

        pruned1 = session1._pruned_messages_for_wire(raw_messages)
        ctx.check(f"the batch committed immediately (combined candidate tokens > the 20k chunk), "
                  f"got {session1._prune_committed_stub_ids!r}",
                  session1._prune_committed_stub_ids == {"t0", "t0b", "t0c"})
        ctx.check("pending is empty again right after a commit", session1._prune_pending_stub_tokens == {})

        commit_nodes = [n for n in session1.log.nodes() if n.get("type") == "prune_commit"]
        ctx.check(f"exactly one prune_commit node was logged, got {commit_nodes!r}", len(commit_nodes) == 1)
        ctx.check("it carries exactly the committed ids, sorted",
                  commit_nodes[0].get("stub_ids") == ["t0", "t0b", "t0c"])

        # Simulate a `-c` resume: SAME cwd + session_id (so it reads the
        # exact file just appended to), nodes reloaded from disk the same
        # way headless.py's own --continue bootstrap does (`log._nodes =
        # log.read_all()`) -- a brand new Session/`__init__` run, NOT
        # `_new_session()` again (which would repoint BRIDGE_TEST_HOME).
        resumed_log = SessionLog(session1.cwd, session_id=session1.log.session_id)
        resumed_log._nodes = resumed_log.read_all()
        session2 = _new_session_with_log(session1, resumed_log)

        ctx.check(f"the resumed process rebuilt the SAME committed set from the log alone, "
                  f"got {session2._prune_committed_stub_ids!r}",
                  session2._prune_committed_stub_ids == session1._prune_committed_stub_ids)

        pruned2 = session2._pruned_messages_for_wire(raw_messages)
        ctx.check("the resumed session's wire prefix for the SAME raw messages is byte-identical "
                  "to what the original process already sent -- not a larger, un-stubbed one",
                  pruned2 == pruned1)
    finally:
        mock.stop()


# =============================================================================
# finding 26: a child that ends abnormally (error/max_turns/blocked) is
# marked is_error on its ToolResult, not handed back as if it were a
# normal, complete answer
# =============================================================================

@test
def test_h9b_f26_child_max_turns_exhaustion_is_marked_is_error_not_a_normal_answer(ctx: Ctx):
    """Verified bug (finding 26): `_final_text_from_log` alone can't tell
    the parent whether a child actually finished normally -- a child that
    exhausted max_turns (or hit a provider error, or got blocked) used to
    hand back whatever text was logged last (typically mid-reasoning, or
    nothing at all) with NO signal it wasn't a complete, normal answer.
    `_child_turn_outcome` now reads the child's own last `turn_done`
    reason: a child scripted to ALWAYS call a tool (ScriptedTurns' own
    step-index clamps to the last step, so the SAME tool call replays
    forever) with `max_turns=1` exhausts on its very first turn, never
    producing a final text answer at all."""
    from rolo_claude.agent.subagent import run_agent_call

    mock = MockUpstream().start()
    try:
        SCENARIOS["h9b-f26-child"] = ScriptedTurns([
            _tool_call_step("Bash", {"command": "echo still going"}, call_id="call_loop"),
        ])
        session = _new_session(mock=mock, model="or:mock/h9b-f26-parent-unused",
                                agents={"general-purpose": _general_purpose_spec(max_turns=1)})

        _events, result = run_agent_call(
            runtime=session.agent_runtime, tool_id="toolu_f26", tool_name="Task",
            tool_input={"description": "never finishes", "prompt": "keep going forever",
                        "subagent_type": "general-purpose", "model": "or:mock/h9b-f26-child"},
        )
        ctx.check(f"a child that exhausted max_turns is marked is_error, got is_error={result.is_error}",
                  result.is_error is True)
        ctx.check(f"the text names the real reason, not a bare stale answer, got {result.content!r}",
                  "did not finish normally" in result.content and "max_turns" in result.content)
    finally:
        mock.stop()


# =============================================================================
# NEW from H9 (not a numbered finding): MCP stdio server subprocess
# lifecycle -- must die with the harness process, both on a normal `-p`
# exit and on SIGHUP, same contract as a background Bash job (finding 3)
# =============================================================================

def _h9b_mcp_only_claude_json(home: Path) -> None:
    """Mirrors tests/test_mcp_e2e.py's own `_write_fake_mcp_only_claude_json`
    (kept local, not cross-imported, per this file's own convention of
    small private per-file helpers) -- ONE real stdio server, spawned
    directly (no shell) via the installed `mcp` SDK's own `stdio_client`,
    so its argv (`... -m tests.helpers.fake_mcp_server`) is exactly what a
    real process's cmdline shows -- reliably `pgrep -f`-able, unlike a
    `bash -lc "... # comment"` command (see the finding-3 SIGHUP test's own
    docstring for that specific, empirically-confirmed gotcha)."""
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {"fake": {"type": "stdio", "command": sys.executable,
                                 "args": ["-m", "tests.helpers.fake_mcp_server"]}},
    }), encoding="utf-8")


@test
def test_h9b_mcp_stdio_server_dies_with_the_process_normal_exit_and_sighup(ctx: Ctx):
    """NEW from H9: an MCP stdio server subprocess must never outlive the
    harness process that spawned it -- on an ordinary `-p` exit (already
    covered indirectly by every other MCP e2e test's own clean exit code,
    but never actually pgrep-CONFIRMED before) and, like a background Bash
    job (finding 3), on SIGHUP (what closing the terminal/an SSH drop
    actually sends). Both checks run in ONE test against the SAME fake
    server module name so a single `pgrep -f` pattern covers both."""
    if sys.platform == "win32" or not hasattr(signal, "SIGHUP"):
        raise SkipTest("SIGHUP is POSIX-only")
    if shutil.which("pgrep") is None:
        raise SkipTest("pgrep not available on this box")

    server_marker = "tests.helpers.fake_mcp_server"

    # ---- part 1: normal exit -------------------------------------------
    fh1 = build_fake_home()
    _h9b_mcp_only_claude_json(fh1["home"])
    mock1 = MockUpstream().start()
    try:
        SCENARIOS["h9b-mcp-lifecycle-normal"] = ScriptedTurns([_text_step("done, no tool needed")])
        result = _run_cli(fh1, mock1, "just say hi", model="or:mock/h9b-mcp-lifecycle-normal",
                           extra_args=["--permission-mode", "auto"], timeout=30)
        ctx.check(f"normal -p exit 0, got {result.returncode}, stderr={result.stderr[-500:]!r}",
                  result.returncode == 0)
        gone = False
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            r = subprocess.run(["pgrep", "-f", server_marker], capture_output=True, text=True)
            if r.returncode != 0 or not r.stdout.strip():
                gone = True
                break
            time.sleep(0.2)
        ctx.check(f"the MCP server is gone after a normal -p exit (still found: {not gone})", gone)
    finally:
        mock1.stop()

    # ---- part 2: SIGHUP mid-session --------------------------------------
    fh2 = build_fake_home()
    _h9b_mcp_only_claude_json(fh2["home"])
    mock2 = MockUpstream().start()

    def _slow_reply(h, body):
        time.sleep(3.0)
        _finish(h, [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                     {"choices": [{"index": 0, "delta": {"content": "slow"}}]},
                     {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}])

    try:
        SCENARIOS["h9b-mcp-lifecycle-sighup"] = ScriptedTurns([_slow_reply])
        env = dict(os.environ)
        env.update({"BRIDGE_TEST_HOME": str(fh2["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock2.base_url,
                     "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR)})
        proc = subprocess.Popen(
            [sys.executable, "-m", "rolo_claude", "-p", "say hi slowly",
             "--model", "or:mock/h9b-mcp-lifecycle-sighup", "--cwd", str(fh2["proj"]),
             "--permission-mode", "auto"],
            env=env, cwd=str(REPO_DIR), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            found = False
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                r = subprocess.run(["pgrep", "-f", server_marker], capture_output=True, text=True)
                if r.returncode == 0 and r.stdout.strip():
                    found = True
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.2)
            ctx.check("the MCP server genuinely started before SIGHUP (pgrep found it)", found)

            proc.send_signal(signal.SIGHUP)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                ctx.check("process exited on SIGHUP within 15s", False)
                return

            gone = False
            deadline2 = time.monotonic() + 15.0
            while time.monotonic() < deadline2:
                r = subprocess.run(["pgrep", "-f", server_marker], capture_output=True, text=True)
                if r.returncode != 0 or not r.stdout.strip():
                    gone = True
                    break
                time.sleep(0.2)
            ctx.check(f"the MCP server is gone after SIGHUP (still found: {not gone})", gone)
        finally:
            if proc.poll() is None:
                proc.kill()
            subprocess.run(["pkill", "-f", server_marker], capture_output=True)
    finally:
        mock2.stop()


# =============================================================================
# finding 14: `@server:resource` mention resolution never blocks prompt
# submission -- regex first, off the UI thread, cached, content capped
# =============================================================================

class _StubResource:
    def __init__(self, uri):
        self.uri = uri


class _StubMcpManager:
    """A minimal, duck-typed stand-in for McpManager -- `resources_calls`
    counts real invocations (for the cache/regex-first assertions),
    `resources_delay_s` simulates a slow/hung `resources/list` RPC."""

    def __init__(self, *, resources_delay_s: float = 0.0):
        self.resources_calls = 0
        self.resources_delay_s = resources_delay_s
        self._resources = [("srv", _StubResource("srv://note"))]
        self.read_calls = 0

    def resources(self):
        self.resources_calls += 1
        if self.resources_delay_s:
            time.sleep(self.resources_delay_s)
        return list(self._resources)

    def read_resource(self, server, uri):
        self.read_calls += 1
        return type("Result", (), {"contents": [type("C", (), {"text": "stub resource content", "blob": None})()]})()


@test
def test_h9b_f14_plain_prompt_with_no_at_mention_never_calls_resources(ctx: Ctx):
    """The regex runs FIRST -- a prompt with no `@name:uri`-shaped text at
    all must never trigger the live `resources()` RPC in the first place."""
    from rolo_claude.mcp.mentions import extract_server_resource_mentions
    stub = _StubMcpManager()
    for text in ("fix the failing test", "why is the build red?", "thanks", "email me @ some point please"):
        found = extract_server_resource_mentions(text, mcp_manager=stub)
        ctx.check(f"no mentions found in {text!r}", found == [])
    ctx.check(f"resources() was never called for any of these, got {stub.resources_calls} call(s)",
              stub.resources_calls == 0)


@test
def test_h9b_f14_resources_listing_is_cached_across_calls(ctx: Ctx):
    from rolo_claude.mcp.mentions import extract_server_resource_mentions
    stub = _StubMcpManager()
    for _ in range(5):
        extract_server_resource_mentions("see @srv:srv://note", mcp_manager=stub)
    ctx.check(f"5 calls with a real mention, but resources() ran only ONCE (cached), "
              f"got {stub.resources_calls}", stub.resources_calls == 1)


@test
def test_h9b_f14_ingest_at_mentions_never_blocks_on_a_hung_mcp_server(ctx: Ctx):
    """End to end through the REAL `Controller.ingest_at_mentions` (the
    live TUI submit/steer call site) with a stub manager whose
    `resources()` sleeps for 2s, simulating a hung server -- the call
    itself must return almost immediately; the snapshot lands later, once
    the background thread's fetch actually completes."""
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.controller import Controller
    from rolo_claude.model import ModelProfile, parse_model_ref

    cwd = Path(tempfile.mkdtemp(prefix="h9b-f14-"))
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="h9b-f14-home-")))
    session_ctx = SessionContext(cwd=cwd, model_label="or:mock/x", bare=True)
    model_ref = parse_model_ref("or:mock/x")
    stub = _StubMcpManager(resources_delay_s=2.0)
    session = Session(cwd=cwd, model_ref=model_ref, model_profile=ModelProfile(), creds=None,
                       state_dir=Path(tempfile.mkdtemp(prefix="h9b-f14-state-")), model_label="or:mock/x",
                       session_context=session_ctx, mcp_manager=stub)
    controller = Controller(session=session, cwd=cwd)

    t0 = time.monotonic()
    controller.ingest_at_mentions("please check @srv:srv://note for context")
    elapsed = time.monotonic() - t0
    ctx.check(f"ingest_at_mentions returned almost immediately despite a 2s-hung server, "
              f"took {elapsed:.2f}s", elapsed < 0.5)

    deadline = time.monotonic() + 10.0
    nodes = []
    while time.monotonic() < deadline:
        nodes = [n for n in session.log.nodes() if n.get("kind") == "at_mention"]
        if nodes:
            break
        time.sleep(0.05)
    ctx.check(f"the snapshot eventually lands once the background fetch completes, got {nodes}", len(nodes) == 1)
    ctx.check("the real resource content made it into the snapshot",
              "stub resource content" in nodes[0]["content"][0]["text"])


@test
def test_h9b_f14_oversized_resource_content_is_skipped_like_at_path(ctx: Ctx):
    from rolo_claude.mcp.mentions import read_server_resource_snapshots

    class _HugeContentManager:
        def resources(self):
            return [("srv", _StubResource("srv://huge"))]

        def read_resource(self, server, uri):
            huge_text = "x" * 250_000  # over the 200,000-byte cap
            return type("Result", (), {"contents": [type("C", (), {"text": huge_text, "blob": None})()]})()

    snapshots = read_server_resource_snapshots("see @srv:srv://huge", mcp_manager=_HugeContentManager())
    ctx.check(f"the oversized resource is skipped entirely (never truncated into the log), got "
              f"{len(snapshots)} snapshot(s)", snapshots == [])


# =============================================================================
# finding 17: turn()'s finally drains _pending_worker_writes too; manual
# /compact and /clear are busy for queue_log_write's purposes
# =============================================================================

@test
def test_h9b_f17_turn_finally_drains_a_write_queued_during_a_slow_stop_hook(ctx: Ctx):
    """Verified bug: `_apply_pending_steers_events` (the normal drain
    point for a `queue_log_write`-queued write) runs BEFORE the Stop hook
    inside `_turn_body`, never after -- a write queued while a SLOW Stop
    hook is still running has no safe point left in THIS turn at all; it
    used to sit stranded in `_pending_worker_writes` until the NEXT turn's
    own first safe point, applied only after that next turn's first model
    reply had already been derived without it."""
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.log import SessionLog
    from rolo_claude.agent.loop import Session
    from rolo_claude.hooks import HookDef, HookRunner
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.permissions import PermissionEngine
    from rolo_claude.providers.stream import ProviderCreds

    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        SCENARIOS["h9b-f17-stop"] = ScriptedTurns([_text_step("final answer before a slow stop hook")])
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
        session_id = "h9b-f17-session"
        session_log = SessionLog(fh["proj"], session_id=session_id)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_DIR)
        env["HOOK_SLEEP_S"] = "2.0"
        hook_runner = HookRunner(
            {"Stop": [HookDef(type="command", args=_HOOK_SCRIPT_ARGV + ["sleep"])]},
            cwd=fh["proj"], session_id=session_id, transcript_path=str(session_log.path), effective_env=env,
        )
        session_ctx = SessionContext(cwd=fh["proj"], model_label="or:mock/h9b-f17-stop")
        model_ref = parse_model_ref("or:mock/h9b-f17-stop")
        session = Session(
            cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
            creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="h9b-f17-state-")), model_label="or:mock/h9b-f17-stop",
            session_context=session_ctx, session_log=session_log, openrouter_base_url=mock.base_url, max_turns=10,
            permission_engine=PermissionEngine(mode="auto", cwd=fh["proj"]), hook_runner=hook_runner,
        )

        observed = {}

        def _queue_late_write():
            time.sleep(0.5)  # well inside the 2s Stop-hook sleep window
            observed["busy_while_hook_sleeps"] = session.busy
            observed["queued"] = session.queue_log_write(
                "snapshot", {"blocks": [{"type": "text", "text": "@late.txt\nqueued-during-slow-stop-hook-marker"}],
                             "snapshot_kind": "at_mention"})

        t = threading.Thread(target=_queue_late_write, daemon=True)
        t.start()
        list(session.turn("answer, then a slow Stop hook runs"))
        t.join(timeout=10)

        ctx.check("the session really was busy while the Stop hook slept",
                  observed.get("busy_while_hook_sleeps") is True)
        ctx.check("queue_log_write really queued it (busy) rather than applying immediately",
                  observed.get("queued") is True)
        nodes = [n for n in session.log.nodes() if n.get("kind") == "at_mention"]
        texts = [n["content"][0]["text"] for n in nodes]
        ctx.check(f"the late write landed via turn()'s own finally (not a later turn), got {texts}",
                  any("queued-during-slow-stop-hook-marker" in t for t in texts))
    finally:
        mock.stop()


@test
def test_h9b_f17_manual_compact_treats_itself_as_busy_for_queue_log_write(ctx: Ctx):
    """Verified bug: a manual `/compact` never set `_busy` at all, so a
    live `@mention`/`!cmd` typed on the UI thread while it ran took
    `queue_log_write`'s "not busy" branch and wrote straight into the log,
    racing `_run_compaction`'s own concurrent writes. Checked from INSIDE
    the real compaction call (the mock summarisation request's own
    handler, closing over `session`) -- deterministic, no thread-timing
    guesswork needed."""
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.permissions import PermissionEngine
    from rolo_claude.providers.stream import ProviderCreds

    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        session_ctx = SessionContext(cwd=fh["proj"], model_label="or:mock/h9b-f17-compact", bare=True)
        model_ref = parse_model_ref("or:mock/h9b-f17-compact")
        session = Session(
            cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
            creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="h9b-f17-compact-state-")), model_label="or:mock/h9b-f17-compact",
            session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=10,
            permission_engine=PermissionEngine(mode="auto", cwd=fh["proj"]),
        )
        list(session.turn("hello"))  # some real history for /compact to work on

        observed = {}

        def _scn_check_busy(handler, body):
            observed["busy_during_compaction"] = session.busy
            from tests.helpers.mock_openai import _finish
            _HEADINGS = ("Primary Request and Intent", "Key Technical Concepts", "Files and Code",
                         "Errors and Fixes", "Pending Jobs", "Current Work", "Next Step", "Critical Context")
            summary = "\n".join(f"## {h}\nsome content." for h in _HEADINGS)
            _finish(handler, [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"content": summary}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])

        SCENARIOS["h9b-f17-compact"] = _scn_check_busy
        from rolo_claude.model import parse_model_ref as _pmr
        session.model_ref = _pmr("or:mock/h9b-f17-compact")

        events_out = []
        session._pump_compaction(None, events_out.append)

        ctx.check(f"session.busy was True DURING the compaction call, got {observed}",
                  observed.get("busy_during_compaction") is True)
        ctx.check("session.busy is False again once _pump_compaction returns (via _end_busy_period)",
                  session.busy is False)

        # And queue_log_write genuinely queues (rather than applying
        # immediately) whenever busy is on, proving the mechanism this
        # fix relies on actually engages during compaction, not just that
        # the flag happens to read True.
        session._busy.set()
        try:
            queued = session.queue_log_write(
                "snapshot", {"blocks": [{"type": "text", "text": "@mid-compact.txt\nmid-compact-marker"}],
                             "snapshot_kind": "at_mention"})
            ctx.check("queue_log_write queues (doesn't apply immediately) while busy", queued is True)
        finally:
            session._end_busy_period()
        nodes = [n for n in session.log.nodes() if n.get("kind") == "at_mention"]
        ctx.check("and _end_busy_period (now called by _pump_compaction's own finally) really applies it",
                  any("mid-compact-marker" in n["content"][0]["text"] for n in nodes))
    finally:
        mock.stop()


# =============================================================================
# finding 29: compute_session_stats counts real prompts only, and includes
# cache_read/cache_creation tokens
# =============================================================================

@test
def test_h9b_f29_only_real_prompts_count_as_turns(ctx: Ctx):
    """Verified bug: a background notice, a Stop-hook continuation, and a
    steer were EACH counted as their own "turn" (every "user"-type log
    node counted, no matter its actual origin) -- a single real prompt
    with a background notice, a continuation, and a steer applied on top
    of it used to report 4 turns for what was genuinely one."""
    from rolo_claude.agent.log import SessionLog
    from rolo_claude.controller import compute_session_stats

    cwd = Path(tempfile.mkdtemp(prefix="h9b-f29-"))
    log = SessionLog(cwd, session_id="h9b-f29-session")
    log.append_meta(model="or:mock/x", cwd=str(cwd), system_prompt_bytes=1, tools=[])
    log.append_user([{"type": "text", "text": "the one real prompt"}])  # untagged -- the real case
    log.append_user([{"type": "text", "text": "a background job finished"}], kind="job_notice")
    log.append_user([{"type": "text", "text": "a sub-agent finished"}], kind="agent_notice")
    log.append_user([{"type": "text", "text": "Please continue."}], kind="continuation")
    log.append_user([{"type": "text", "text": "actually, do X instead"}], kind="steer")
    log.append_user([{"type": "text", "text": "compaction summary"}], kind="compaction_summary")
    log.append_user([{"type": "text", "text": "re-appended tail"}], kind="compaction_tail")
    stats = compute_session_stats(log.nodes())
    ctx.check(f"exactly ONE real turn counted, got {stats['turns']}", stats["turns"] == 1)


@test
def test_h9b_f29_untagged_legacy_user_nodes_still_count_same_as_before(ctx: Ctx):
    """Backward compatibility: a session log written before this tagging
    existed has NO `kind` field on any "user" node at all -- every one of
    them must still count, exactly like before this fix, so an old
    session's reported turn count doesn't suddenly change."""
    from rolo_claude.controller import compute_session_stats

    legacy_nodes = [
        {"type": "meta", "model": "or:mock/x"},
        {"type": "user", "content": [{"type": "text", "text": "q1"}]},
        {"type": "assistant", "content": [{"type": "text", "text": "a1"}]},
        {"type": "user", "content": [{"type": "text", "text": "q2 (was really a notice, pre-fix)"}]},
        {"type": "assistant", "content": [{"type": "text", "text": "a2"}]},
    ]
    stats = compute_session_stats(legacy_nodes)
    ctx.check(f"both untagged legacy nodes still count, got {stats['turns']}", stats["turns"] == 2)


@test
def test_h9b_f29_cache_tokens_are_tracked_and_displayed(ctx: Ctx):
    from rolo_claude.agent.log import SessionLog
    from rolo_claude.controller import compute_session_stats, format_cache_tokens_suffix

    cwd = Path(tempfile.mkdtemp(prefix="h9b-f29-cache-"))
    log = SessionLog(cwd, session_id="h9b-f29-cache-session")
    log.append_meta(model="or:mock/claude-ish", cwd=str(cwd), system_prompt_bytes=1, tools=[])
    log.append_usage({"input_tokens": 40, "output_tokens": 20,
                       "cache_read_input_tokens": 150000, "cache_creation_input_tokens": 5000}, cost_usd=0.01)
    stats = compute_session_stats(log.nodes())
    bucket = stats["per_model"]["or:mock/claude-ish"]
    ctx.check(f"cache_read_input_tokens tracked, got {bucket}", bucket["cache_read_input_tokens"] == 150000)
    ctx.check(f"cache_creation_input_tokens tracked, got {bucket}", bucket["cache_creation_input_tokens"] == 5000)
    suffix = format_cache_tokens_suffix(bucket)
    ctx.check(f"the display suffix names both, got {suffix!r}", "150000" in suffix and "5000" in suffix)
    ctx.check("a bucket with no cache activity gets an empty suffix",
              format_cache_tokens_suffix({"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}) == "")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
