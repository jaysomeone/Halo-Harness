"""tests.test_commands_registry -- rolo_claude/commands/{registry,custom,
skills}.py (U0 scope B): Registry.discover/resolve/complete/help_rows,
custom command namespace/$ARGUMENTS/$N/`!cmd` gating, skills discovery
across all three locations incl. synced naming + bare-name alias, and the
"skill wins over a same-named command, never a builtin" precedence rule.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.commands.registry import Registry, SlashCommand, substitute_arguments
from rolo_claude.commands.builtins import HeadlessFacade

test, TESTS = new_registry()


class _Env:
    """A temp cwd + temp home, with BRIDGE_TEST_HOME pointed at the home
    for the duration of one test."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="rolo-claude-cmds-"))
        self.cwd = self.root / "proj"
        self.home = self.root / "home"
        (self.cwd).mkdir(parents=True, exist_ok=True)
        (self.home).mkdir(parents=True, exist_ok=True)
        self._old = os.environ.get("BRIDGE_TEST_HOME")
        os.environ["BRIDGE_TEST_HOME"] = str(self.home)

    def close(self):
        if self._old is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = self._old

    def write(self, rel_path: str, text: str) -> Path:
        p = self.root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def discover(self) -> Registry:
        return Registry.discover(self.cwd, self.home)

    def facade(self, registry: Registry) -> HeadlessFacade:
        return HeadlessFacade(cwd=self.cwd, registry=registry)


# =============================================================================
# Registry itself (no filesystem).
# =============================================================================

@test
def test_registry_add_never_overwrites(ctx: Ctx):
    reg = Registry()
    a = SlashCommand(name="x", description="first")
    b = SlashCommand(name="x", description="second")
    ctx.check("first add succeeds", reg.add(a) is True)
    ctx.check("second add (same name) refused", reg.add(b) is False)
    ctx.check("first one still resolves", reg.resolve("x").description == "first")


@test
def test_registry_resolve_strips_leading_slash(ctx: Ctx):
    reg = Registry()
    reg.add(SlashCommand(name="help", description="d"))
    ctx.check("resolve('/help') works", reg.resolve("/help") is not None)
    ctx.check("resolve('help') works", reg.resolve("help") is not None)
    ctx.check("resolve('nope') is None", reg.resolve("nope") is None)


@test
def test_registry_complete_prefix(ctx: Ctx):
    reg = Registry()
    reg.add(SlashCommand(name="help", description=""))
    reg.add(SlashCommand(name="hello-custom", description=""))
    reg.add(SlashCommand(name="model", description=""))
    names = sorted(c.name for c in reg.complete("he"))
    ctx.check(f"prefix 'he' matches help+hello-custom, got {names!r}", names == ["hello-custom", "help"])


@test
def test_registry_help_rows_sorted(ctx: Ctx):
    reg = Registry()
    reg.add(SlashCommand(name="zeta", description="z"))
    reg.add(SlashCommand(name="alpha", description="a"))
    rows = reg.help_rows()
    ctx.check(f"sorted alphabetically, got {rows!r}", rows == [("/alpha", "a"), ("/zeta", "z")])


@test
def test_substitute_arguments_zero_based_and_arguments(ctx: Ctx):
    out = substitute_arguments("first=$0 second=$1 all=$ARGUMENTS", "one two")
    ctx.check(f"0-based $0/$1 + $ARGUMENTS, got {out!r}", out == "first=one second=two all=one two")


@test
def test_substitute_arguments_no_placeholder_appends(ctx: Ctx):
    out = substitute_arguments("plain body, no placeholders", "some args")
    ctx.check(f"ARGUMENTS: appended when unused, got {out!r}",
              out == "plain body, no placeholders\n\nARGUMENTS: some args")


@test
def test_substitute_arguments_no_args_no_append(ctx: Ctx):
    out = substitute_arguments("plain body", "")
    ctx.check("nothing appended when there were no args at all", out == "plain body")


# =============================================================================
# Builtins.
# =============================================================================

@test
def test_builtins_all_22_registered_with_expected_kinds(ctx: Ctx):
    env = _Env()
    try:
        reg = env.discover()
        # H5 scope B: /compact moved from "ui" to "core" -- it now fully
        # executes a real compaction (or a clear no-session message)
        # instead of describing what the TUI would do.
        expected_ui = {"clear", "plan", "resume", "export", "exit"}
        expected_prompt = {"init"}
        for name in ("help", "clear", "compact", "cost", "context", "model", "mcp", "memory", "permissions",
                      "plan", "resume", "status", "config", "skills", "agents", "effort", "init", "doctor",
                      "export", "add-dir", "theme", "exit"):
            cmd = reg.resolve(name)
            ctx.check(f"builtin /{name} registered", cmd is not None)
            ctx.check(f"/{name} source is builtin", cmd.source == "builtin")
            if name in expected_ui:
                ctx.check(f"/{name} kind is ui", cmd.kind == "ui")
            elif name in expected_prompt:
                ctx.check(f"/{name} kind is prompt", cmd.kind == "prompt")
            else:
                ctx.check(f"/{name} kind is core", cmd.kind == "core")
    finally:
        env.close()


# =============================================================================
# Custom commands.
# =============================================================================

@test
def test_custom_command_namespaced_by_subdir(ctx: Ctx):
    env = _Env()
    try:
        env.write("proj/.claude/commands/git/commit.md",
                   "---\ndescription: Make a commit\n---\nCommit with message $ARGUMENTS\n")
        reg = env.discover()
        cmd = reg.resolve("git:commit")
        ctx.check("git:commit discovered", cmd is not None)
        ctx.check("description from frontmatter", cmd.description == "Make a commit")
        ctx.check("kind is prompt", cmd.kind == "prompt")
        ctx.check("source is custom", cmd.source == "custom")
    finally:
        env.close()


@test
def test_custom_command_project_wins_over_user(ctx: Ctx):
    env = _Env()
    try:
        env.write("proj/.claude/commands/review.md", "---\ndescription: project version\n---\nbody\n")
        env.write("home/.claude/commands/review.md", "---\ndescription: user version\n---\nbody\n")
        reg = env.discover()
        cmd = reg.resolve("review")
        ctx.check(f"project copy wins, got {cmd.description!r}", cmd.description == "project version")
    finally:
        env.close()


@test
def test_custom_command_bang_cmd_requires_allowed_tools(ctx: Ctx):
    env = _Env()
    try:
        env.write("proj/.claude/commands/nogrant.md", "---\ndescription: no grant\n---\nOutput: !`echo hi-there`\n")
        env.write("proj/.claude/commands/granted.md",
                   "---\ndescription: granted\nallowed-tools: Bash(echo:*)\n---\nOutput: !`echo hi-there`\n")
        reg = env.discover()
        facade = env.facade(reg)

        denied = reg.resolve("nogrant").run("", facade)
        ctx.check(f"ungated !`cmd` refused, got {denied!r}", "not permitted" in denied)

        allowed = reg.resolve("granted").run("", facade)
        ctx.check(f"gated !`cmd` substitutes real stdout, got {allowed!r}", "hi-there" in allowed)
    finally:
        env.close()


# =============================================================================
# Skills.
# =============================================================================

@test
def test_project_skill_discovery(ctx: Ctx):
    env = _Env()
    try:
        env.write("proj/.claude/skills/reviewer/SKILL.md", "---\ndescription: Review code\n---\nReview: $ARGUMENTS\n")
        reg = env.discover()
        cmd = reg.resolve("reviewer")
        ctx.check("project skill discovered", cmd is not None)
        ctx.check("source is skill", cmd.source == "skill")
        ctx.check("kind is prompt", cmd.kind == "prompt")
    finally:
        env.close()


@test
def test_skill_user_invocable_false_is_hidden(ctx: Ctx):
    env = _Env()
    try:
        env.write("proj/.claude/skills/hidden/SKILL.md", "---\ndescription: hidden\nuser-invocable: false\n---\nbody\n")
        reg = env.discover()
        ctx.check("user-invocable: false skill NOT registered as a slash command", reg.resolve("hidden") is None)
    finally:
        env.close()


@test
def test_synced_skill_naming_and_bare_alias(ctx: Ctx):
    env = _Env()
    try:
        import json
        manifest = {"lastUpdated": "now", "skills": [
            {"name": "dataviz", "skillId": "abc", "description": "Charts", "source": "claude.ai", "updatedAt": None},
        ]}
        env.write("home/.claude/skills/synced/bucket1/manifest.json", json.dumps(manifest))
        env.write("home/.claude/skills/synced/bucket1/dataviz/SKILL.md", "---\ndescription: Charts\n---\nMake a chart: $ARGUMENTS\n")
        reg = env.discover()
        namespaced = reg.resolve("anthropic-skills:dataviz")
        alias = reg.resolve("dataviz")
        ctx.check("namespaced form resolves", namespaced is not None)
        ctx.check("bare alias resolves to the SAME command", alias is namespaced)
        ctx.check("namespaced description from manifest", namespaced.description == "Charts")
    finally:
        env.close()


@test
def test_synced_skill_not_double_registered_when_cwd_nested_under_home(ctx: Ctx):
    """Regression: the project-skills ANCESTOR WALK (cwd up to the
    filesystem root) can itself reach the real home directory when cwd is
    nested under it (rolo's actual layout: a repo under
    C:\\Users\\rolo\\Documents\\...) -- that ancestor's own
    `.claude/skills/synced/` must NOT be re-discovered under a bogus
    `synced:<bucket>:<name>` name; only the ONE proper
    `anthropic-skills:<name>` (+ bare alias) registration may exist."""
    root = Path(tempfile.mkdtemp(prefix="rolo-claude-nested-"))
    home = root / "home"
    cwd = home / "work" / "proj"  # nested UNDER home, unlike _Env's sibling layout
    cwd.mkdir(parents=True, exist_ok=True)
    old = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    try:
        import json
        manifest = {"skills": [{"name": "dataviz", "description": "Charts"}]}
        bucket_dir = home / ".claude" / "skills" / "synced" / "bucket1"
        bucket_dir.mkdir(parents=True, exist_ok=True)
        (bucket_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (bucket_dir / "dataviz").mkdir(parents=True, exist_ok=True)
        (bucket_dir / "dataviz" / "SKILL.md").write_text("---\ndescription: Charts\n---\nbody\n", encoding="utf-8")

        reg = Registry.discover(cwd, home)
        bogus = [n for n in reg.all() if n.name.startswith("synced:")]
        ctx.check(f"no bogus 'synced:...' entries, got {[c.name for c in bogus]!r}", not bogus)
        ctx.check("the proper anthropic-skills:dataviz entry exists", reg.resolve("anthropic-skills:dataviz") is not None)
        ctx.check("its bare alias still works", reg.resolve("dataviz") is not None)
    finally:
        if old is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old


@test
def test_synced_skill_alias_skipped_on_builtin_collision(ctx: Ctx):
    env = _Env()
    try:
        import json
        manifest = {"skills": [{"name": "help", "description": "a synced skill that happens to be named help"}]}
        env.write("home/.claude/skills/synced/bucket1/manifest.json", json.dumps(manifest))
        env.write("home/.claude/skills/synced/bucket1/help/SKILL.md", "---\ndescription: synced help\n---\nbody\n")
        reg = env.discover()
        ctx.check("bare /help still resolves to the BUILTIN", reg.resolve("help").source == "builtin")
        ctx.check("namespaced form still registers", reg.resolve("anthropic-skills:help").source == "skill")
    finally:
        env.close()


@test
def test_skill_wins_over_same_named_custom_command(ctx: Ctx):
    env = _Env()
    try:
        env.write("proj/.claude/commands/review.md", "---\ndescription: custom command version\n---\nbody\n")
        env.write("proj/.claude/skills/review/SKILL.md", "---\ndescription: skill version\n---\nbody\n")
        reg = env.discover()
        cmd = reg.resolve("review")
        ctx.check(f"skill wins over a same-named custom command, got source={cmd.source!r}", cmd.source == "skill")
    finally:
        env.close()


@test
def test_skill_never_overrides_builtin(ctx: Ctx):
    env = _Env()
    try:
        env.write("proj/.claude/skills/help/SKILL.md", "---\ndescription: a skill named help\n---\nbody\n")
        reg = env.discover()
        ctx.check("builtin /help survives a same-named project skill", reg.resolve("help").source == "builtin")
    finally:
        env.close()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
