"""tests.test_settings -- config/settings.py: precedence, list-merge,
ignored defaultMode from project/local, malformed JSON, --settings base
dirs, the 19 real rules round-trip losslessly.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from halo_harness.config.settings import resolve_settings

test, TESTS = new_registry()


def _isolated_project(tag: str = "") -> Path:
    d = Path(tempfile.mkdtemp(prefix=f"settings-test-{tag}-"))
    (d / ".claude").mkdir(parents=True, exist_ok=True)
    return d


@test
def test_precedence_local_beats_project_beats_user(ctx: Ctx):
    home = _isolated_project("home")
    proj = _isolated_project("proj")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"model": "user-model"}), encoding="utf-8")
    (proj / ".claude" / "settings.json").write_text(json.dumps({"model": "project-model"}), encoding="utf-8")
    (proj / ".claude" / "settings.local.json").write_text(json.dumps({"model": "local-model"}), encoding="utf-8")

    settings = resolve_settings(proj)
    ctx.check(f"local wins over project/user, got {settings.model!r}", settings.model == "local-model")


@test
def test_list_concatenation_across_layers(ctx: Ctx):
    home = _isolated_project("home2")
    proj = _isolated_project("proj2")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(a)"]}}), encoding="utf-8")
    (proj / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(b)"]}}), encoding="utf-8")
    (proj / ".claude" / "settings.local.json").write_text(json.dumps({"permissions": {"allow": ["Bash(c)"]}}), encoding="utf-8")

    settings = resolve_settings(proj)
    allow = settings.permissions_allow
    ctx.check(f"all three concatenated, got {allow!r}", set(allow) == {"Bash(a)", "Bash(b)", "Bash(c)"})
    ctx.check("length 3 (no accidental dup)", len(allow) == 3)


@test
def test_list_dedup_preserves_first_seen_order(ctx: Ctx):
    home = _isolated_project("home3")
    proj = _isolated_project("proj3")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["X", "Y"]}}), encoding="utf-8")
    (proj / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Y", "Z"]}}), encoding="utf-8")

    settings = resolve_settings(proj)
    ctx.check(f"deduped preserving first-seen order, got {settings.permissions_allow!r}",
              settings.permissions_allow == ["X", "Y", "Z"])


@test
def test_project_local_default_mode_filtered(ctx: Ctx):
    home = _isolated_project("home4")
    proj = _isolated_project("proj4")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (proj / ".claude" / "settings.local.json").write_text(
        json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}), encoding="utf-8",
    )
    settings = resolve_settings(proj)
    ctx.check("bypassPermissions from local settings is IGNORED",
              settings.permissions_default_mode is None)


@test
def test_project_local_default_mode_allowed_values_pass_through(ctx: Ctx):
    home = _isolated_project("home5")
    proj = _isolated_project("proj5")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (proj / ".claude" / "settings.local.json").write_text(
        json.dumps({"permissions": {"defaultMode": "acceptEdits"}}), encoding="utf-8",
    )
    settings = resolve_settings(proj)
    ctx.check("acceptEdits from local settings is allowed through",
              settings.permissions_default_mode == "acceptEdits")


@test
def test_user_scope_default_mode_auto_allowed(ctx: Ctx):
    home = _isolated_project("home6")
    proj = _isolated_project("proj6")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"defaultMode": "auto"}}), encoding="utf-8")
    settings = resolve_settings(proj)
    ctx.check("auto from USER settings is honored (only project/local are filtered)",
              settings.permissions_default_mode == "auto")


@test
def test_malformed_json_degrades_to_empty_layer_with_error(ctx: Ctx):
    home = _isolated_project("home7")
    proj = _isolated_project("proj7")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text("{not valid json", encoding="utf-8")
    (proj / ".claude" / "settings.json").write_text(json.dumps({"model": "still-works"}), encoding="utf-8")

    settings = resolve_settings(proj)
    ctx.check("other layers still load despite one bad file", settings.model == "still-works")
    ctx.check("error recorded", len(settings.errors) == 1)
    ctx.check("error path points at the bad file", str(settings.errors[0].path).endswith("settings.json"))


@test
def test_settings_flag_inline_json_base_dir_is_cwd(ctx: Ctx):
    proj = _isolated_project("proj8")
    settings = resolve_settings(proj, settings_flag=json.dumps({"model": "flag-model"}))
    ctx.check("inline JSON --settings wins (higher precedence than user/project/local)",
              settings.model == "flag-model")
    flag_layer = next(l for l in settings.layers if l.name == "flagSettings")
    ctx.check(f"base_dir is cwd for inline JSON, got {flag_layer.base_dir}", Path(flag_layer.base_dir) == proj)


@test
def test_settings_flag_file_path_base_dir_is_file_parent(ctx: Ctx):
    proj = _isolated_project("proj9")
    other_dir = _isolated_project("other9")
    settings_file = other_dir / "my-settings.json"
    settings_file.write_text(json.dumps({"model": "file-model"}), encoding="utf-8")

    settings = resolve_settings(proj, settings_flag=str(settings_file))
    ctx.check("file-path --settings wins", settings.model == "file-model")
    flag_layer = next(l for l in settings.layers if l.name == "flagSettings")
    ctx.check(f"base_dir is the file's own parent, got {flag_layer.base_dir}", Path(flag_layer.base_dir) == other_dir)


@test
def test_setting_sources_drops_unlisted_scopes(ctx: Ctx):
    home = _isolated_project("home10")
    proj = _isolated_project("proj10")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"model": "user-model"}), encoding="utf-8")
    (proj / ".claude" / "settings.json").write_text(json.dumps({"model": "project-model"}), encoding="utf-8")

    settings = resolve_settings(proj, setting_sources=["user"])
    ctx.check(f"only user scope included, got {settings.model!r}", settings.model == "user-model")


@test
def test_env_merge_per_key_highest_wins(ctx: Ctx):
    home = _isolated_project("home11")
    proj = _isolated_project("proj11")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"env": {"A": "user", "B": "user"}}), encoding="utf-8")
    (proj / ".claude" / "settings.json").write_text(json.dumps({"env": {"B": "project", "C": "project"}}), encoding="utf-8")

    settings = resolve_settings(proj)
    ctx.check("A from user survives", settings.env.get("A") == "user")
    ctx.check("B overridden by project", settings.env.get("B") == "project")
    ctx.check("C from project present", settings.env.get("C") == "project")


@test
def test_the_19_real_rules_round_trip_losslessly(ctx: Ctx):
    """the owner's real settings.local.json (copied verbatim by fake_home) has 19
    allow rules with escaped parens/doubled backslashes -- confirm JSON
    round-trips them without mangling (the actual unescaping into a Rule is
    permissions.py's job, U0/H1 -- this only proves settings.py hands the
    raw strings through unmodified). Both the owner's real file AND this fixture
    put it at the HOME level (~/.claude/settings.local.json) -- resolve
    against cwd=home, matching the owner's own usual cwd==home usage (finding B),
    not the separate `proj/` fixture (which has its own synthetic
    settings.local.json for the unrelated defaultMode-filtering tests)."""
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    settings = resolve_settings(fh["home"])
    allow = settings.permissions_allow
    ctx.check(f"19 rules present, got {len(allow)}", len(allow) == 19)
    ctx.check("a plain rule survives", "Bash(netstat -ano)" in allow)
    ctx.check("an escaped-paren PowerShell rule survives with its backslashes intact",
              any("GetCurrent" in r and r"\(" in r for r in allow))


@test
def test_auto_memory_flat_keys_not_nested(ctx: Ctx):
    home = _isolated_project("home12")
    proj = _isolated_project("proj12")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryEnabled": False, "autoMemoryDirectory": "/tmp/mem"}), encoding="utf-8",
    )
    settings = resolve_settings(proj)
    ctx.check("autoMemoryEnabled read from the flat key", settings.auto_memory_enabled is False)
    ctx.check("autoMemoryDirectory read from the flat key", settings.auto_memory_directory == "/tmp/mem")


@test
def test_defaults_when_nothing_set(ctx: Ctx):
    home = _isolated_project("home13")
    proj = _isolated_project("proj13")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    settings = resolve_settings(proj)
    ctx.check("auto_memory_enabled defaults True", settings.auto_memory_enabled is True)
    ctx.check("instruction_files defaults to claude-md-or-agents-md",
              settings.instruction_files == "claude-md-or-agents-md")
    ctx.check("respect_gitignore defaults True", settings.respect_gitignore is True)
    ctx.check("disable_all_hooks defaults False", settings.disable_all_hooks is False)


@test
def test_h0_14_case8_untrusted_layer_drops_allow_env_hooks_automemdir(ctx: Ctx):
    """H0 #14 case 8 (finding 6): an UNTRUSTED project/local layer's
    `permissions.allow`/`additionalDirectories`, `env`, `hooks`, and
    `autoMemoryDirectory` must never reach `Settings.raw` -- `permissions.
    deny`/`ask` from the SAME untrusted layer still must."""
    home = _isolated_project("home14a")
    proj = _isolated_project("proj14a")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (proj / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash"], "deny": ["Bash(rm -rf /)"], "ask": ["WebFetch"],
                         "additionalDirectories": ["/etc"]},
        "env": {"SOME_SECRET": "leak-me"},
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "evil"}]}]},
        "autoMemoryDirectory": "/tmp/untrusted-memory",
    }), encoding="utf-8")

    untrusted = resolve_settings(proj, trusted=False)
    ctx.check("allow DROPPED from an untrusted layer", untrusted.permissions_allow == [])
    ctx.check("additionalDirectories DROPPED", untrusted.permissions_additional_directories == [])
    ctx.check("env DROPPED (no secret leak from an untrusted repo)", untrusted.env == {})
    ctx.check("hooks DROPPED (no arbitrary command execution from an untrusted repo)", untrusted.hooks == {})
    ctx.check("autoMemoryDirectory DROPPED", untrusted.auto_memory_directory is None)
    ctx.check("deny SURVIVES from an untrusted layer", untrusted.permissions_deny == ["Bash(rm -rf /)"])
    ctx.check("ask SURVIVES from an untrusted layer", untrusted.permissions_ask == ["WebFetch"])

    trusted = resolve_settings(proj, trusted=True)
    ctx.check("the SAME layer, trusted, keeps allow", trusted.permissions_allow == ["Bash"])
    ctx.check("the SAME layer, trusted, keeps env", trusted.env == {"SOME_SECRET": "leak-me"})


@test
def test_h0_14_case8_policy_beats_flag_settings(ctx: Ctx):
    """H0 #14 case 8: policySettings (managed) outranks flagSettings
    (--settings) -- the documented precedence order, not merely "whichever
    was added to the merge last by accident"."""
    home = _isolated_project("home14b")
    proj = _isolated_project("proj14b")
    managed_dir = Path(tempfile.mkdtemp(prefix="settings-test-managed-"))
    (managed_dir / "managed-settings.json").write_text(json.dumps({"model": "policy-model"}), encoding="utf-8")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    os.environ["BRIDGE_TEST_MANAGED_DIR"] = str(managed_dir)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    try:
        settings = resolve_settings(proj, settings_flag=json.dumps({"model": "flag-model"}))
        ctx.check(f"policy beats an inline --settings flag, got {settings.model!r}", settings.model == "policy-model")
    finally:
        os.environ.pop("BRIDGE_TEST_MANAGED_DIR", None)


@test
def test_finding_15_permission_rule_tracks_its_own_layer_base_dir(ctx: Ctx):
    """finding 15: every settings source used to get `base_dir=cwd`, even
    though a rule that came from userSettings should resolve a relative
    path VALUE against `~/.claude`, not the project cwd (verified: a
    user-level `Read(/x)` resolved under cwd instead of the real home)."""
    home = _isolated_project("home15")
    proj = _isolated_project("proj15")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Read(user-notes.txt)"]}}), encoding="utf-8")
    (proj / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Read(project-notes.txt)"]}}), encoding="utf-8")

    settings = resolve_settings(proj)
    ctx.check("both rules present in the merged allow list",
              set(settings.permissions_allow) == {"Read(user-notes.txt)", "Read(project-notes.txt)"})
    user_base = settings.permission_rule_base_dir("allow", "Read(user-notes.txt)")
    proj_base = settings.permission_rule_base_dir("allow", "Read(project-notes.txt)")
    ctx.check(f"the user-sourced rule's base_dir is ~/.claude, got {user_base}",
              user_base is not None and Path(user_base).resolve() == (home / ".claude").resolve())
    ctx.check(f"the project-sourced rule's base_dir is the project's .claude, got {proj_base}",
              proj_base is not None and Path(proj_base).resolve() == (proj / ".claude").resolve())

    from halo_harness.permissions import build_rules_from_settings
    _deny, _ask, allow_rules = build_rules_from_settings(settings, cwd=proj)
    by_value = {r.value: r for r in allow_rules}
    ctx.check("both parsed Rules carry their OWN source base_dir, not always cwd",
              by_value["user-notes.txt"].base_dir.resolve() == (home / ".claude").resolve()
              and by_value["project-notes.txt"].base_dir.resolve() == (proj / ".claude").resolve())


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
