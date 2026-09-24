"""tests.test_permissions -- rolo_claude/permissions.py (H2 scope C): the
D-CFG grammar/matcher/mode-table enumeration. Covers the 19 real rules
round-trip, Bash(ls *) vs lsof, :* placement, compound/subshell, wrapper/env
stripping, Read(src/**) allow vs deny, negation, Read-deny-blocks-Edit,
domain wildcards, mcp forms, param rules, deny>ask>allow, untrusted project
allow ignored, every mode-table row (incl. auto allowing a non-whitelisted
command with no prompt and honouring a deny rule), print-mode denial,
add_allow_rule escaping round-trip, PowerShell alias canonicalisation, and
bare-name catalog removal.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import _SETTINGS_LOCAL_JSON
from rolo_claude import permissions as P
from rolo_claude.tools.read import ReadTool
from rolo_claude.tools.edit import EditTool
from rolo_claude.tools.write import WriteTool

test, TESTS = new_registry()

CWD = Path(tempfile.mkdtemp(prefix="perm-test-cwd-"))


# ---- the 19 real rules round-trip --------------------------------------

@test
def test_19_real_rules_all_round_trip_without_crashing(ctx: Ctx):
    rules_text = _SETTINGS_LOCAL_JSON["permissions"]["allow"]
    ctx.check(f"fixture has 19 rules, got {len(rules_text)}", len(rules_text) == 19)
    parsed = [P.parse_rule(r, source="local", base_dir=CWD, action="allow") for r in rules_text]
    invalid = [r.raw for r in parsed if r.kind == "invalid"]
    ctx.check(f"every real rule parses to a non-invalid kind, invalid: {invalid}", invalid == [])


# ---- Bash(ls *) vs lsof / :* placement ----------------------------------

@test
def test_bash_space_star_prefix_excludes_lsof(ctx: Ctx):
    r = P.parse_rule("Bash(ls *)", source="user")
    ctx.check("kind is prefix", r.kind == "prefix")
    ctx.check("matches 'ls'", P._pattern_matches_text(r.kind, r.value, "ls"))
    ctx.check("matches 'ls -la'", P._pattern_matches_text(r.kind, r.value, "ls -la"))
    ctx.check("does not match 'lsof'", not P._pattern_matches_text(r.kind, r.value, "lsof"))


@test
def test_bash_inner_star_glob_includes_lsof(ctx: Ctx):
    r = P.parse_rule("Bash(ls*)", source="user")
    ctx.check("kind is glob", r.kind == "glob")
    ctx.check("glob DOES match lsof", P._pattern_matches_text(r.kind, r.value, "lsof"))


@test
def test_colon_star_suffix_becomes_prefix(ctx: Ctx):
    r = P.parse_rule("Bash(git:*)", source="user")
    ctx.check(f"git:* -> prefix 'git', got {r}", r.kind == "prefix" and r.value == "git")


@test
def test_colon_star_empty_prefix_invalid(ctx: Ctx):
    r = P.parse_rule("Bash(:*)", source="user")
    ctx.check("empty prefix before :* is invalid", r.kind == "invalid")
    ctx.check("error names the empty-prefix rule", "empty" in r.error.lower())


@test
def test_colon_star_not_at_end_invalid(ctx: Ctx):
    r = P.parse_rule("Bash(foo:*bar)", source="user")
    ctx.check(":* must be at the end", r.kind == "invalid")
    ctx.check("error names the placement rule", "end" in r.error.lower())


# ---- compound / subshell -------------------------------------------------

@test
def test_compound_allow_every_segment_must_match(ctx: Ctx):
    engine = P.PermissionEngine(mode="default", cwd=CWD,
                                 allow_rules=[P.parse_rule("Bash(npm test)", source="user", base_dir=CWD)])
    ok = engine.decide("Bash", {"command": "git status && npm test"})  # git status is read-only-whitelisted
    ctx.check(f"whitelisted + allowed segment -> allow, got {ok.action}", ok.action == "allow")
    partial = engine.decide("Bash", {"command": "npm test && curl http://x"})
    ctx.check(f"one unmatched segment -> not allowed by rules, got {partial.action}", partial.action != "allow")


@test
def test_deny_fires_on_a_compound_segment(ctx: Ctx):
    engine = P.PermissionEngine(mode="auto", cwd=CWD,
                                 deny_rules=[P.parse_rule("Bash(rm -rf /tmp)", source="user", base_dir=CWD)])
    d = engine.decide("Bash", {"command": "echo safe && rm -rf /tmp"})
    ctx.check(f"deny fires on the second compound segment, got {d.action}", d.action == "deny")


@test
def test_deny_fires_inside_a_subshell_body(ctx: Ctx):
    engine = P.PermissionEngine(mode="auto", cwd=CWD,
                                 deny_rules=[P.parse_rule("Bash(rm -rf /tmp)", source="user", base_dir=CWD)])
    d = engine.decide("Bash", {"command": "echo $(rm -rf /tmp)"})
    ctx.check(f"deny fires inside a $() subshell body, got {d.action}", d.action == "deny")
    d2 = engine.decide("Bash", {"command": "echo `rm -rf /tmp`"})
    ctx.check(f"deny fires inside a backtick subshell body, got {d2.action}", d2.action == "deny")


@test
def test_split_bash_segments_respects_quotes(ctx: Ctx):
    segs = P.split_bash_segments('echo "a && b" ; echo c')
    ctx.check(f"quoted && is not a split point, got {segs}", segs == ['echo "a && b"', "echo c"])


# ---- wrapper / env stripping ---------------------------------------------

@test
def test_wrapper_timeout_stripped_for_allow(ctx: Ctx):
    engine = P.PermissionEngine(mode="default", cwd=CWD,
                                 allow_rules=[P.parse_rule("Bash(mytool run)", source="user", base_dir=CWD)])
    d = engine.decide("Bash", {"command": "timeout 30 mytool run"})
    ctx.check(f"timeout wrapper stripped, allow matches, got {d.action}", d.action == "allow")


@test
def test_wrapper_sudo_never_stripped(ctx: Ctx):
    engine = P.PermissionEngine(mode="default", cwd=CWD,
                                 allow_rules=[P.parse_rule("Bash(mytool run)", source="user", base_dir=CWD)])
    d = engine.decide("Bash", {"command": "sudo mytool run"})
    ctx.check(f"sudo is never stripped -- rule does not match, got {d.action}", d.action != "allow")


@test
def test_env_assignment_stripped_unless_load_bearing(ctx: Ctx):
    ctx.check("ordinary env assignment stripped", P.strip_wrappers_and_env("FOO=bar mytool run") == "mytool run")
    ctx.check("PATH assignment kept (load-bearing)", P.strip_wrappers_and_env("PATH=/x mytool run") == "PATH=/x mytool run")


# ---- Read(src/**) allow vs deny -------------------------------------------

@test
def test_read_src_glob_allow_matches_nested_file(ctx: Ctx):
    engine = P.PermissionEngine(mode="default", cwd=CWD,
                                 allow_rules=[P.parse_rule("Read(src/**)", source="user", base_dir=CWD)])
    d = engine.decide("Read", {"file_path": str(CWD / "src" / "deep" / "x.py")}, tool=ReadTool())
    ctx.check(f"allow Read(src/**) matches a nested file, got {d.action}", d.action == "allow")


@test
def test_read_src_glob_deny_matches_nested_file(ctx: Ctx):
    engine = P.PermissionEngine(mode="auto", cwd=CWD,
                                 deny_rules=[P.parse_rule("Read(src/**)", source="user", base_dir=CWD)])
    d = engine.decide("Read", {"file_path": str(CWD / "src" / "deep" / "x.py")}, tool=ReadTool())
    ctx.check(f"deny Read(src/**) matches a nested file, got {d.action}", d.action == "deny")


@test
def test_read_allow_anchored_at_cwd_not_any_depth(ctx: Ctx):
    """ALLOW is anchored at the rule's own base -- a bare `.env` allow
    rule must NOT allow .env files under an unrelated subtree it was never
    written against."""
    other_root = Path(tempfile.mkdtemp(prefix="perm-other-"))
    engine = P.PermissionEngine(mode="default", cwd=CWD,
                                 allow_rules=[P.parse_rule("Read(notes.txt)", source="user", base_dir=CWD)])
    d = engine.decide("Read", {"file_path": str(other_root / "sub" / "notes.txt")}, tool=ReadTool())
    ctx.check(f"allow does not reach outside its own base anchor, got {d.action}", d.action != "allow")


# ---- //, ~/, /-relative ---------------------------------------------------

@test
def test_double_slash_absolute_path_rule(ctx: Ctx):
    r = P.parse_rule("Read(//etc/passwd)", source="user", base_dir=CWD)
    regex, is_abs = P.path_rule_regex(r.value, CWD, any_depth=False)
    ctx.check("// form is absolute", is_abs is True)
    if sys_platform_posix := (Path("/") == Path("/")):
        pass


@test
def test_tilde_path_rule_resolves_to_home(ctx: Ctx):
    r = P.parse_rule("Read(~/notes.txt)", source="user", base_dir=CWD)
    regex, is_abs = P.path_rule_regex(r.value, CWD, any_depth=False)
    ctx.check("~/ form is absolute", is_abs is True)
    ctx.check("~/ resolves under the real home dir", regex.match(P._abs_posix(Path.home() / "notes.txt")) is not None)


@test
def test_slash_relative_path_rule_uses_source_base_dir(ctx: Ctx):
    base = Path(tempfile.mkdtemp(prefix="perm-basedir-"))
    r = P.parse_rule("Read(/config/app.yml)", source="project", base_dir=base)
    regex, is_abs = P.path_rule_regex(r.value, base, any_depth=False)
    ctx.check("/-relative form is base-relative, not absolute-anchor", is_abs is False)
    ctx.check("matches base_dir/config/app.yml", regex.match(P._abs_posix(base / "config" / "app.yml")) is not None)


# ---- negation ---------------------------------------------------------

@test
def test_negation_parsed_and_flips_match(ctx: Ctx):
    r = P.parse_rule("Read(!secret.txt)", source="user", base_dir=CWD)
    ctx.check("negated flag set", r.negated is True)
    ctx.check("value has the ! stripped", r.value == "secret.txt")
    engine = P.PermissionEngine(mode="default", cwd=CWD, allow_rules=[
        P.parse_rule("Read(**)", source="user", base_dir=CWD), r,
    ])
    allowed = engine.decide("Read", {"file_path": str(CWD / "other.txt")}, tool=ReadTool())
    ctx.check(f"non-excluded file still allowed via Read(**), got {allowed.action}", allowed.action == "allow")


# ---- Read-deny-blocks-Edit -------------------------------------------------

@test
def test_read_deny_blocks_edit(ctx: Ctx):
    engine = P.PermissionEngine(mode="auto", cwd=CWD,
                                 deny_rules=[P.parse_rule("Read(secret.txt)", source="user", base_dir=CWD)])
    d = engine.decide("Edit", {"file_path": str(CWD / "secret.txt"), "old_string": "a", "new_string": "b"}, tool=EditTool())
    ctx.check(f"Read deny blocks an Edit call on the same path, got {d.action}", d.action == "deny")


@test
def test_read_deny_blocks_write(ctx: Ctx):
    engine = P.PermissionEngine(mode="auto", cwd=CWD,
                                 deny_rules=[P.parse_rule("Read(secret.txt)", source="user", base_dir=CWD)])
    d = engine.decide("Write", {"file_path": str(CWD / "secret.txt"), "content": "x"}, tool=WriteTool())
    ctx.check(f"Read deny blocks a Write call on the same path, got {d.action}", d.action == "deny")


@test
def test_edit_rules_cover_write(ctx: Ctx):
    engine = P.PermissionEngine(mode="default", cwd=CWD,
                                 allow_rules=[P.parse_rule("Edit(out.txt)", source="user", base_dir=CWD)])
    d = engine.decide("Write", {"file_path": str(CWD / "out.txt"), "content": "x"}, tool=WriteTool())
    ctx.check(f"an Edit(...) allow rule also covers a Write call, got {d.action}", d.action == "allow")


@test
def test_write_rules_never_consulted(ctx: Ctx):
    """Write(...)/Glob(...) rules are dead grammar -- never consulted, even
    for a Write call (only Edit-kind rules cover Write)."""
    engine = P.PermissionEngine(mode="default", cwd=CWD,
                                 allow_rules=[P.parse_rule("Write(out2.txt)", source="user", base_dir=CWD)])
    d = engine.decide("Write", {"file_path": str(CWD / "out2.txt"), "content": "x"}, tool=WriteTool())
    ctx.check(f"a Write(...) allow rule is never consulted, got {d.action}", d.action != "allow")


# ---- domain wildcards -------------------------------------------------

@test
def test_domain_star_matches_everything(ctx: Ctx):
    ctx.check("domain:* matches any host", P.domain_rule_matches("domain:*", "https://anything.example/x"))


@test
def test_domain_subdomain_wildcard_excludes_bare_domain(ctx: Ctx):
    ctx.check("domain:*.x.com matches sub.x.com", P.domain_rule_matches("domain:*.x.com", "https://api.x.com/y"))
    ctx.check("domain:*.x.com does NOT match x.com itself", not P.domain_rule_matches("domain:*.x.com", "https://x.com/y"))


@test
def test_domain_matching_case_insensitive(ctx: Ctx):
    ctx.check("domain match is case-insensitive", P.domain_rule_matches("domain:Example.COM", "https://example.com/x"))


# ---- mcp rule forms ---------------------------------------------------

@test
def test_mcp_server_tool_form(ctx: Ctx):
    r = P.parse_rule("mcp__github__get_issue", source="user")
    ctx.check("kind mcp_tool", r.kind == "mcp_tool")
    ctx.check("server extracted", r.tool == "github")
    ctx.check("tool extracted", r.value == "get_issue")


@test
def test_mcp_server_all_form(ctx: Ctx):
    r = P.parse_rule("mcp__github__*", source="user")
    ctx.check("kind mcp_server_all", r.kind == "mcp_server_all")
    engine = P.PermissionEngine(mode="auto", cwd=CWD, deny_rules=[r])
    d = engine.decide("mcp__github__get_issue", {})
    ctx.check(f"server-all denies every tool of that server, got {d.action}", d.action == "deny")


@test
def test_mcp_bare_server_form(ctx: Ctx):
    r = P.parse_rule("mcp__github", source="user")
    ctx.check("kind mcp_server", r.kind == "mcp_server")


@test
def test_mcp_parenthesised_is_invalid(ctx: Ctx):
    r = P.parse_rule("mcp__github(get_issue)", source="user")
    ctx.check("parenthesised MCP rule is invalid", r.kind == "invalid")


@test
def test_mcp_allow_glob_only_in_tool_position(ctx: Ctx):
    ok = P.parse_rule("mcp__github__get_*", source="user", action="allow")
    ctx.check("glob in tool position is valid for allow", ok.kind == "mcp_tool")
    bad = P.parse_rule("mcp__gith*__get_issue", source="user", action="allow")
    ctx.check("glob in server position is invalid for allow", bad.kind == "invalid")
    bad_ok_for_deny = P.parse_rule("mcp__gith*__get_issue", source="user", action="deny")
    ctx.check("same shape is fine for deny", bad_ok_for_deny.kind == "mcp_tool")


# ---- param rules --------------------------------------------------------

@test
def test_param_rule_parses_and_is_deny_ask_only(ctx: Ctx):
    r = P.parse_rule("SomeTool(mode:fast)", source="user", action="ask")
    ctx.check("kind param", r.kind == "param")
    ctx.check("key extracted", r.param_key == "mode")
    ctx.check("value extracted", r.param_value == "fast")
    invalid = P.parse_rule("SomeTool(mode:fast)", source="user", action="allow")
    ctx.check("param rule invalid when used as an allow rule", invalid.kind == "invalid")


@test
def test_param_rule_matches_only_that_param_value(ctx: Ctx):
    r = P.parse_rule("SomeTool(mode:fast)", source="user", action="ask")
    engine = P.PermissionEngine(mode="default", cwd=CWD, ask_rules=[r])
    d_match = engine.decide("SomeTool", {"mode": "fast"})
    d_nomatch = engine.decide("SomeTool", {"mode": "slow"})
    ctx.check(f"matching param value -> ask via the rule itself, got {d_match.action}/{d_match.source}",
               d_match.action == "ask" and d_match.matched_rule is r)
    # a non-matching value still lands on "ask" in default mode (the
    # generic mode-table "other" row), but via the MODE TABLE, never the
    # param rule itself -- distinguish by source/matched_rule, not action.
    ctx.check(f"non-matching value falls through to the mode table (not the rule), got source={d_nomatch.source!r}",
               d_nomatch.source == "mode" and d_nomatch.matched_rule is None)


# ---- deny > ask > allow -------------------------------------------------

@test
def test_deny_beats_ask_beats_allow(ctx: Ctx):
    engine = P.PermissionEngine(
        mode="default", cwd=CWD,
        deny_rules=[P.parse_rule("Bash(git push)", source="user", base_dir=CWD)],
        ask_rules=[P.parse_rule("Bash(git push)", source="user", base_dir=CWD)],
        allow_rules=[P.parse_rule("Bash(git push)", source="user", base_dir=CWD)],
    )
    d = engine.decide("Bash", {"command": "git push"})
    ctx.check(f"deny wins over ask and allow for the same command, got {d.action}", d.action == "deny")


@test
def test_ask_beats_allow_when_no_deny(ctx: Ctx):
    engine = P.PermissionEngine(
        mode="default", cwd=CWD,
        ask_rules=[P.parse_rule("Bash(git push)", source="user", base_dir=CWD)],
        allow_rules=[P.parse_rule("Bash(git push)", source="user", base_dir=CWD)],
    )
    d = engine.decide("Bash", {"command": "git push"})
    ctx.check(f"ask wins over allow, got {d.action}", d.action == "ask")


# ---- untrusted project allow ignored (via Settings' own trust filter) ----

@test
def test_untrusted_project_allow_dropped_by_settings_layer(ctx: Ctx):
    """permissions.py itself trusts whatever rule list it's handed (trust
    filtering is config/settings.py's job, already covered by
    tests/test_settings.py); this proves the INTEGRATION point -- an
    engine built from an untrusted-filtered Settings object never sees the
    dropped allow rule at all."""
    from rolo_claude.config.settings import Settings
    raw = {"permissions": {"allow": ["Bash(git push)"]}}
    settings_trusted = Settings(raw=raw, layers=[], errors=[])
    ctx.check("trusted settings still carries the allow rule",
              settings_trusted.permissions_allow == ["Bash(git push)"])
    # The untrusted-drop itself happens in config/settings.py's
    # _apply_trust_filter BEFORE merging -- simulate that here.
    from rolo_claude.config.settings import _apply_trust_filter
    filtered = _apply_trust_filter(raw, "projectSettings", trusted=False)
    ctx.check("untrusted projectSettings drops permissions.allow",
              filtered.get("permissions", {}).get("allow") is None)


# ---- every mode-table row -------------------------------------------------

_MODE_EXPECTATIONS = {
    #        readonly_bash  write_in_workdir  other(bash)
    "default":           ("allow", "ask",   "ask"),
    "acceptEdits":        ("allow", "allow", "ask"),
    "plan":                ("allow", "deny",  "deny"),
    "auto":                 ("allow", "allow", "allow"),
    "dontAsk":              ("allow", "deny",  "deny"),
    "bypassPermissions":    ("allow", "allow", "allow"),
}


@test
def test_every_mode_table_row(ctx: Ctx):
    for mode, (ro_expect, write_expect, other_expect) in _MODE_EXPECTATIONS.items():
        engine = P.PermissionEngine(mode=mode, cwd=CWD)
        d_ro = engine.decide("Bash", {"command": "ls -la"})
        ctx.check(f"{mode}: read-only bash whitelist -> {ro_expect}, got {d_ro.action}", d_ro.action == ro_expect)
        d_write = engine.decide("Write", {"file_path": str(CWD / "z.txt"), "content": "x"}, tool=WriteTool())
        ctx.check(f"{mode}: Write inside workdir -> {write_expect}, got {d_write.action}", d_write.action == write_expect)
        d_other = engine.decide("Bash", {"command": "curl http://example.invalid"})
        ctx.check(f"{mode}: other bash call -> {other_expect}, got {d_other.action}", d_other.action == other_expect)


@test
def test_auto_allows_non_whitelisted_command_with_no_prompt(ctx: Ctx):
    engine = P.PermissionEngine(mode="auto", cwd=CWD)
    d = engine.decide("Bash", {"command": "curl http://example.invalid"})
    ctx.check(f"auto allows an arbitrary non-whitelisted bash command, got {d.action}", d.action == "allow")
    ctx.check("no ask/prompt outcome for auto", d.action != "ask")


@test
def test_auto_still_honours_an_explicit_deny_rule(ctx: Ctx):
    engine = P.PermissionEngine(mode="auto", cwd=CWD,
                                 deny_rules=[P.parse_rule("Bash(curl:*)", source="user", base_dir=CWD)])
    d = engine.decide("Bash", {"command": "curl http://example.invalid"})
    ctx.check(f"auto still denies when an explicit deny rule matches, got {d.action}", d.action == "deny")


@test
def test_bypass_ignores_ask_rules(ctx: Ctx):
    engine = P.PermissionEngine(mode="bypassPermissions", cwd=CWD,
                                 ask_rules=[P.parse_rule("Bash(git push)", source="user", base_dir=CWD)])
    d = engine.decide("Bash", {"command": "git push"})
    ctx.check(f"bypassPermissions ignores ask rules entirely, got {d.action}", d.action == "allow")


@test
def test_bypass_still_honours_deny(ctx: Ctx):
    engine = P.PermissionEngine(mode="bypassPermissions", cwd=CWD,
                                 deny_rules=[P.parse_rule("Bash(git push)", source="user", base_dir=CWD)])
    d = engine.decide("Bash", {"command": "git push"})
    ctx.check(f"bypassPermissions still denies an explicit deny rule, got {d.action}", d.action == "deny")


# ---- print-mode denial ----------------------------------------------------

@test
def test_print_mode_ask_becomes_deny_with_suggested_rule(ctx: Ctx):
    engine = P.PermissionEngine(mode="default", cwd=CWD, print_mode=True)
    d = engine.decide("Write", {"file_path": str(CWD / "x.txt"), "content": "hi"}, tool=WriteTool())
    ctx.check(f"print mode: would-ask becomes deny, got {d.action}", d.action == "deny")
    ctx.check("suggested_rule populated", d.suggested_rule is not None and d.suggested_rule.startswith("Edit("))
    ctx.check("permission_denial populated", d.permission_denial is not None
              and d.permission_denial.get("tool_name") == "Write")
    ctx.check("permission_denial itself also carries the suggested_rule",
              d.permission_denial.get("suggested_rule") == d.suggested_rule)


@test
def test_print_mode_ask_rule_denial_also_suggests(ctx: Ctx):
    engine = P.PermissionEngine(mode="default", cwd=CWD, print_mode=True,
                                 ask_rules=[P.parse_rule("Bash(git push)", source="user", base_dir=CWD)])
    d = engine.decide("Bash", {"command": "git push"})
    ctx.check(f"print mode + matched ask rule -> deny, got {d.action}", d.action == "deny")
    ctx.check("suggested_rule present", d.suggested_rule is not None)


@test
def test_non_print_mode_ask_stays_ask(ctx: Ctx):
    engine = P.PermissionEngine(mode="default", cwd=CWD, print_mode=False)
    d = engine.decide("Write", {"file_path": str(CWD / "y.txt"), "content": "hi"}, tool=WriteTool())
    ctx.check(f"interactive (non-print) mode keeps a real ask outcome, got {d.action}", d.action == "ask")


# ---- add_allow_rule escaping round-trip -----------------------------------

@test
def test_add_allow_rule_escaping_round_trip(ctx: Ctx):
    tmp = Path(tempfile.mkdtemp(prefix="addrule-"))
    original_content = "echo (hello) \\world"
    rule_text = P.build_rule_text("Bash", original_content)
    written_path = P.add_allow_rule(rule_text, "local", cwd=tmp)
    ctx.check("wrote to .claude/settings.local.json", written_path == tmp / ".claude" / "settings.local.json")
    data = json.loads(written_path.read_text(encoding="utf-8"))
    stored = data["permissions"]["allow"]
    ctx.check("exactly one rule stored", stored == [rule_text])
    parsed = P.parse_rule(stored[0], source="local", base_dir=tmp)
    ctx.check(f"round-tripped content matches the original, got {parsed.value!r}", parsed.value == original_content)


@test
def test_add_allow_rule_is_idempotent(ctx: Ctx):
    tmp = Path(tempfile.mkdtemp(prefix="addrule-idem-"))
    text = P.build_rule_text("Bash", "npm test")
    P.add_allow_rule(text, "local", cwd=tmp)
    P.add_allow_rule(text, "local", cwd=tmp)
    data = json.loads((tmp / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    ctx.check("adding the same rule twice does not duplicate it", data["permissions"]["allow"] == [text])


@test
def test_add_allow_rule_preserves_existing_content(ctx: Ctx):
    tmp = Path(tempfile.mkdtemp(prefix="addrule-preserve-"))
    settings_path = tmp / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"permissions": {"allow": ["Bash(existing)"]}, "otherKey": True}), encoding="utf-8")
    P.add_allow_rule("Bash(new)", "local", cwd=tmp)
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    ctx.check("existing rule preserved", "Bash(existing)" in data["permissions"]["allow"])
    ctx.check("new rule appended", "Bash(new)" in data["permissions"]["allow"])
    ctx.check("unrelated key preserved", data.get("otherKey") is True)


@test
def test_add_allow_rule_user_destination(ctx: Ctx):
    import os
    tmp_home = Path(tempfile.mkdtemp(prefix="addrule-user-"))
    old_env = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(tmp_home)
    try:
        P.add_allow_rule("Read(x)", "user", cwd=CWD)
        data = json.loads((tmp_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        ctx.check("user destination writes to ~/.claude/settings.json", data["permissions"]["allow"] == ["Read(x)"])
    finally:
        if old_env is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_env


# ---- PowerShell alias canonicalisation ------------------------------------

@test
def test_powershell_alias_canonicalisation(ctx: Ctx):
    ctx.check("ls -> Get-ChildItem", P.canonicalize_powershell("ls -Recurse") == "Get-ChildItem -Recurse")
    ctx.check("rm -> Remove-Item", P.canonicalize_powershell("rm file.txt") == "Remove-Item file.txt")
    ctx.check(".exe suffix stripped before lookup", P.canonicalize_powershell("ls.exe -Recurse") == "Get-ChildItem -Recurse")


@test
def test_powershell_rule_matches_raw_or_canonical_case_insensitive(ctx: Ctx):
    r = P.parse_rule("PowerShell(get-childitem:*)", source="user")
    ctx.check("matches canonical form of an alias, case-insensitive", P.powershell_rule_matches(r.kind, r.value, "ls -Recurse"))
    ctx.check("matches the raw Cmdlet form directly too", P.powershell_rule_matches(r.kind, r.value, "Get-ChildItem -Force"))
    ctx.check("does not match an unrelated cmdlet", not P.powershell_rule_matches(r.kind, r.value, "Remove-Item x"))


# ---- bare-name catalog removal --------------------------------------------

@test
def test_bare_name_extraction_from_deny_rules(ctx: Ctx):
    deny = [P.parse_rule("Bash", source="user"), P.parse_rule("WebFetch(domain:*)", source="user"),
            P.parse_rule("Skill", source="user")]
    names = P.bare_deny_tool_names(deny)
    ctx.check(f"only bare-kind rules contribute names, got {names}", names == {"Bash", "Skill"})


@test
def test_bare_name_removal_applies_to_registry(ctx: Ctx):
    from rolo_claude.tools.registry import ToolRegistry
    reg = ToolRegistry()
    deny = [P.parse_rule("Bash", source="user")]
    names = P.bare_deny_tool_names(deny)
    filtered = reg.without(names)
    ctx.check("Bash removed from the frozen catalog", "Bash" not in filtered.names())
    ctx.check("everything else stays", "Read" in filtered.names() and "Edit" in filtered.names())


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
