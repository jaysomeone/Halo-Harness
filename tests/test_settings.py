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
from rolo_claude.config.settings import resolve_settings

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
    """rolo's real settings.local.json (copied verbatim by fake_home) has 19
    allow rules with escaped parens/doubled backslashes -- confirm JSON
    round-trips them without mangling (the actual unescaping into a Rule is
    permissions.py's job, U0/H1 -- this only proves settings.py hands the
    raw strings through unmodified). Both rolo's real file AND this fixture
    put it at the HOME level (~/.claude/settings.local.json) -- resolve
    against cwd=home, matching rolo's own usual cwd==home usage (finding B),
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


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
