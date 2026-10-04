"""tests.test_h15_state_dir_audit -- H15 Part D2: state-dir scoping audit.

D2.1: rather than hand-auditing ~40 test modules that each build a real
Session/Controller/build_session (each with its own, inconsistently
complete `_Env`), `tests/run_all.py` itself now snapshots and restores
every BRIDGE_*/OPENROUTER_*/DATABRICKS_*/ANTHROPIC_*/TYPESAFE_* env var
AROUND EACH MODULE -- so whatever one module leaves behind (an incomplete
`_Env.__exit__`, an exception that skipped cleanup, no `_Env` at all)
structurally cannot reach the NEXT module's own `home()`/`bridge_home()`
resolution. This makes "never relying on module order" true at the
harness level instead of per-module discipline. D2.2: the REAL SESSIONS
GUARD now also catches a new DIRECTORY, not only a new `.jsonl` file.
D2.3: the `!cmd` inline-shell permission card is pinned here too.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_GUARDED_PREFIXES = ("BRIDGE_", "HALO_", "OPENROUTER_", "DATABRICKS_", "ANTHROPIC_", "TYPESAFE_", "OLLAMA_")


class _Env:
    """This module's own tests mutate the SAME guarded env vars they're
    testing the guard logic for -- snapshot/restore all of them around
    every test, same house rule as everywhere else."""

    def __enter__(self):
        self._saved = {k: v for k, v in os.environ.items() if k.startswith(_GUARDED_PREFIXES)}
        for k in list(os.environ):
            if k.startswith(_GUARDED_PREFIXES):
                os.environ.pop(k, None)
        return self

    def __exit__(self, *exc):
        for k in list(os.environ):
            if k.startswith(_GUARDED_PREFIXES) and k not in self._saved:
                os.environ.pop(k, None)
        for k, v in self._saved.items():
            os.environ[k] = v


# ---------------------------------------------------------------------------
# D2.1: tests/run_all.py's own per-module env guard.
# ---------------------------------------------------------------------------

@test
def test_restore_guarded_env_removes_whatever_a_module_left_behind(ctx: Ctx):
    """The exact bug: a module sets BRIDGE_STATE_DIR and never cleans it
    up -- the NEXT module must never see it."""
    import tests.run_all as run_all_mod
    with _Env():
        before = run_all_mod._snapshot_guarded_env()
        ctx.check(f"nothing guarded set yet, got {before}", before == {})
        # Simulate a sloppy module: sets state, "forgets" to clean up.
        os.environ["BRIDGE_STATE_DIR"] = "/tmp/some-module-own-fake-home/.halo"
        os.environ["DATABRICKS_TOKEN"] = "leaked-token"
        run_all_mod._restore_guarded_env(before)
        ctx.check("BRIDGE_STATE_DIR is gone", "BRIDGE_STATE_DIR" not in os.environ)
        ctx.check("DATABRICKS_TOKEN is gone", "DATABRICKS_TOKEN" not in os.environ)


@test
def test_restore_guarded_env_puts_back_whatever_was_there_before(ctx: Ctx):
    """A module that runs INSIDE an already-legitimately-scoped outer
    environment (e.g. this very suite run, launched with BRIDGE_TEST_HOME
    already set by the harness) must not have that stripped out from under
    it -- restore means restore, not "always clear"."""
    import tests.run_all as run_all_mod
    with _Env():
        os.environ["BRIDGE_TEST_HOME"] = "/tmp/outer-legitimate-home"
        before = run_all_mod._snapshot_guarded_env()
        os.environ["BRIDGE_TEST_HOME"] = "/tmp/a-module-own-different-home"
        os.environ["OPENROUTER_API_KEY"] = "sk-or-module-local"
        run_all_mod._restore_guarded_env(before)
        ctx.check(f"BRIDGE_TEST_HOME restored to the OUTER value, got {os.environ.get('BRIDGE_TEST_HOME')!r}",
                  os.environ.get("BRIDGE_TEST_HOME") == "/tmp/outer-legitimate-home")
        ctx.check("the module's own OPENROUTER_API_KEY is gone", "OPENROUTER_API_KEY" not in os.environ)


@test
def test_restore_guarded_env_never_touches_unrelated_vars(ctx: Ctx):
    import tests.run_all as run_all_mod
    with _Env():
        os.environ["SOME_UNRELATED_VAR"] = "untouched"
        before = run_all_mod._snapshot_guarded_env()
        run_all_mod._restore_guarded_env(before)
        ctx.check("an unrelated env var is never touched", os.environ.get("SOME_UNRELATED_VAR") == "untouched")
        os.environ.pop("SOME_UNRELATED_VAR", None)


@test
def test_main_loop_restores_env_after_each_module_even_on_import_error(ctx: Ctx):
    """End-to-end: run_all.main() itself must leave the guarded env exactly
    as it found it at the MODULE level, even when a module fails to
    import. 2.0.0 fixpass finding 2: main() now ALSO sets its own
    whole-run BRIDGE_TEST_HOME once, by design, before the first module is
    even imported, when neither test seam was already set -- that ONE var
    surviving is the intended behavior (it's what keeps every module,
    including the very first one, off the real ~/.halo), not a leak;
    anything ELSE surviving still is exactly what this test guards
    against. 2.0.0 fixpass item G: narrowed back to JUST that one var --
    main() never sets BRIDGE_STATE_DIR itself (only BRIDGE_TEST_HOME), so
    allowing it through here too used to mask a real leak.

    W6b section E: `_ensure_whole_run_test_seams()` sets two more, at the
    SAME "before the first module is even imported" point and for the
    exact same reason (closing a WSL hang in a module that never scopes
    these itself) -- `BRIDGE_TEST_NO_BACKGROUND_NET` and `BRIDGE_TEST_
    CC_AUTH_STATUS` are now ALSO intended whole-run survivors, not a
    leak."""
    import io
    import sys as sys_mod
    from contextlib import redirect_stdout
    import tests.run_all as run_all_mod
    with _Env():
        real_discover = run_all_mod.discover_test_modules
        real_snapshot = run_all_mod._real_sessions_snapshot

        def _fake_discover():
            return ["tests._h15_does_not_exist_module"]

        run_all_mod.discover_test_modules = _fake_discover
        run_all_mod._real_sessions_snapshot = lambda: set()
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = run_all_mod.main()
            ctx.check(f"a bad import is a failure exit code, got {rc}", rc == 1)
            leftover = run_all_mod._snapshot_guarded_env()
            ctx.check(f"nothing but main()'s OWN whole-run seam vars survives, got {leftover}",
                      set(leftover) <= {"BRIDGE_TEST_HOME", "BRIDGE_TEST_NO_BACKGROUND_NET",
                                         "BRIDGE_TEST_CC_AUTH_STATUS"})
        finally:
            run_all_mod.discover_test_modules = real_discover
            run_all_mod._real_sessions_snapshot = real_snapshot


# ---------------------------------------------------------------------------
# D2.2: the REAL SESSIONS GUARD catches a new directory, not only a file.
# ---------------------------------------------------------------------------

@test
def test_real_sessions_snapshot_includes_bare_directories(ctx: Ctx):
    import tests.run_all as run_all_mod
    fake_home = Path(tempfile.mkdtemp(prefix="h15-real-sessions-guard-"))
    sessions_dir = fake_home / ".halo" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "real-slug-with-a-file").mkdir()
    (sessions_dir / "real-slug-with-a-file" / "abc123.jsonl").write_text("{}\n", encoding="utf-8")
    (sessions_dir / "a-slug-with-no-session-file-at-all").mkdir()  # the H15 D2 bug: empty dir, old guard missed this

    real_home_fn = Path.home
    try:
        Path.home = staticmethod(lambda: fake_home)
        snapshot = run_all_mod._real_sessions_snapshot()
    finally:
        Path.home = real_home_fn
    ctx.check(f"the .jsonl file is tracked, got {snapshot}",
              any(p.endswith("abc123.jsonl") for p in snapshot))
    ctx.check(f"the FILE-carrying slug directory is ALSO tracked, got {snapshot}",
              any(p.endswith("real-slug-with-a-file") for p in snapshot))
    ctx.check(f"the EMPTY slug directory (the actual bug) is tracked too, got {snapshot}",
              any(p.endswith("a-slug-with-no-session-file-at-all") for p in snapshot))


@test
def test_real_sessions_snapshot_diff_flags_a_new_empty_directory(ctx: Ctx):
    """The guard's own before/after diff (what main() actually checks)
    catches a brand-new empty slug directory appearing mid-run."""
    import tests.run_all as run_all_mod
    fake_home = Path(tempfile.mkdtemp(prefix="h15-real-sessions-guard-diff-"))
    sessions_dir = fake_home / ".halo" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "already-real").mkdir()

    real_home_fn = Path.home
    try:
        Path.home = staticmethod(lambda: fake_home)
        before = run_all_mod._real_sessions_snapshot()
        (sessions_dir / "C--Users-rolo-AppData-Local-Temp-some-test-artifact").mkdir()
        after = run_all_mod._real_sessions_snapshot()
    finally:
        Path.home = real_home_fn
    leaked = sorted(after - before)
    ctx.check(f"exactly the new directory is flagged, got {leaked}",
              len(leaked) == 1 and leaked[0].endswith("some-test-artifact"))


@test
def test_real_sessions_snapshot_also_tracks_the_legacy_rolo_claude_dir(ctx: Ctx):
    """2.0.0 rename: the default state dir moved from `~/.rolo-claude` to
    `~/.halo`, but a box that hasn't launched the new build even once yet
    (so `bridge_home()`'s own one-time migration hasn't fired) still has
    its real session history under the OLD name -- the guard must keep
    watching BOTH real directories, never just the new default, so a test
    that leaks a session into either one is still caught."""
    import tests.run_all as run_all_mod
    fake_home = Path(tempfile.mkdtemp(prefix="h15-real-sessions-guard-legacy-"))
    sessions_dir = fake_home / ".rolo-claude" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "legacy-slug").mkdir()
    (sessions_dir / "legacy-slug" / "def456.jsonl").write_text("{}\n", encoding="utf-8")

    real_home_fn = Path.home
    try:
        Path.home = staticmethod(lambda: fake_home)
        snapshot = run_all_mod._real_sessions_snapshot()
    finally:
        Path.home = real_home_fn
    ctx.check(f"a session under the LEGACY ~/.rolo-claude/sessions is tracked too, got {snapshot}",
              any(p.endswith("def456.jsonl") for p in snapshot))


@test
def test_real_sessions_snapshot_unions_both_the_new_and_legacy_dirs(ctx: Ctx):
    """Both real directories feed the SAME snapshot set at once -- a fake
    home with session content under both `~/.halo/sessions` and the legacy
    `~/.rolo-claude/sessions` (e.g. mid-migration, or two different tools
    on the same box at different versions) is never silently limited to
    just one of them."""
    import tests.run_all as run_all_mod
    fake_home = Path(tempfile.mkdtemp(prefix="h15-real-sessions-guard-both-"))
    new_dir = fake_home / ".halo" / "sessions"
    old_dir = fake_home / ".rolo-claude" / "sessions"
    new_dir.mkdir(parents=True)
    old_dir.mkdir(parents=True)
    (new_dir / "new-slug").mkdir()
    (new_dir / "new-slug" / "new111.jsonl").write_text("{}\n", encoding="utf-8")
    (old_dir / "old-slug").mkdir()
    (old_dir / "old-slug" / "old222.jsonl").write_text("{}\n", encoding="utf-8")

    real_home_fn = Path.home
    try:
        Path.home = staticmethod(lambda: fake_home)
        snapshot = run_all_mod._real_sessions_snapshot()
    finally:
        Path.home = real_home_fn
    ctx.check(f"the new dir's session is tracked, got {snapshot}",
              any(p.endswith("new111.jsonl") for p in snapshot))
    ctx.check(f"the legacy dir's session is ALSO tracked, got {snapshot}",
              any(p.endswith("old222.jsonl") for p in snapshot))


# ---------------------------------------------------------------------------
# D2.3: the `!cmd` inline-shell card routes through reevaluate_pending_
# permission just like a live tool-call ask.
# ---------------------------------------------------------------------------

@test
def test_controller_register_and_discard_pending_permission(ctx: Ctx):
    from halo_harness.controller import Controller

    class _FakeSession:
        def __init__(self):
            self._permission_waiters = {}

    ctrl = Controller(session=_FakeSession(), cwd=Path.cwd(), state_dir=Path(tempfile.mkdtemp()), routes={})
    ctrl.register_pending_permission("inline-1", "Bash", {"command": "ls"})
    ctx.check("a slot now exists", "inline-1" in ctrl.session._permission_waiters)
    slot = ctrl.session._permission_waiters["inline-1"]
    ctx.check(f"carries the right tool_name/tool_input, got {slot}",
              slot["tool_name"] == "Bash" and slot["tool_input"] == {"command": "ls"})
    ctrl.discard_pending_permission("inline-1")
    ctx.check("the slot is gone after discard", "inline-1" not in ctrl.session._permission_waiters)


@test
def test_inline_slot_is_reachable_through_reevaluate_pending_permission(ctx: Ctx):
    """The actual point of D2.3: once registered, Shift+Tab's own
    Controller.reevaluate_pending_permission call resolves the inline ask
    exactly like a live tool-call one -- re-running the REAL permission
    engine, not a blind allow."""
    from halo_harness.controller import Controller
    from halo_harness.permissions import PermissionEngine, parse_rule

    class _FakeSession:
        def __init__(self, engine):
            self._permission_waiters = {}
            self.permission_engine = engine

        def reevaluate_pending_permission(self, request_id):
            slot = self._permission_waiters.get(request_id)
            if slot is None:
                return None
            decision = self.permission_engine.decide(slot["tool_name"], slot["tool_input"], slot["tool"])
            if decision.action not in ("allow", "deny"):
                return None
            slot["decision"] = decision
            slot["event"].set()
            return decision.action

    rule = parse_rule("Bash(rm -rf /:*)", source="user", base_dir=Path.cwd())
    engine = PermissionEngine(mode="default", cwd=Path.cwd(), deny_rules=[rule])
    session = _FakeSession(engine)
    ctrl = Controller(session=session, cwd=Path.cwd(), state_dir=Path(tempfile.mkdtemp()), routes={})
    ctrl.register_pending_permission("inline-2", "Bash", {"command": "rm -rf /"})
    action = ctrl.reevaluate_pending_permission("inline-2")
    ctx.check(f"the deny rule actually applies to the inline ask, got {action!r}", action == "deny")


@test
def test_permission_card_on_resolved_externally_fires_for_an_inline_card(ctx: Ctx):
    from halo_harness.tui.widgets.cards import PermissionCard
    decisions, external_decisions = [], []
    card = PermissionCard(request_id="inline-3", summary="Bash(ls)", reason="", suggested_rule=None,
                           on_decide=decisions.append, on_resolved_externally=external_decisions.append)
    card.resolve_externally("allow")
    ctx.check(f"on_decide itself is NOT called (unchanged finding-4 contract), got {decisions}", decisions == [])
    ctx.check(f"on_resolved_externally IS called for an inline card, got {external_decisions}",
              len(external_decisions) == 1 and external_decisions[0]["action"] == "allow")
    ctx.check("the card is done", card.done is True)


@test
def test_permission_card_without_on_resolved_externally_is_unaffected(ctx: Ctx):
    """Regression guard: a live tool-call card (on_resolved_externally
    omitted) must behave EXACTLY as finding 4 left it."""
    from halo_harness.tui.widgets.cards import PermissionCard
    decisions = []
    card = PermissionCard(request_id="live-1", summary="Write(x)", reason="", suggested_rule=None,
                           on_decide=decisions.append)
    card.resolve_externally("deny")
    ctx.check(f"on_decide never called, got {decisions}", decisions == [])
    ctx.check("the card is done", card.done is True)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
