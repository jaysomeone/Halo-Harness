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
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()
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
    ctx.check("// form actually matches /etc/passwd itself", regex.match("/etc/passwd") is not None)
    ctx.check("// form does not match an unrelated absolute path",
              regex.match("/etc/hosts") is None and regex.match("/tmp/etc/passwd") is None)


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
    # finding 16: the negated path itself must actually be exercised -- a
    # `!`-rule only CANCELS the Read(**) allow for secret.txt, it is never
    # an independent positive match (the bug this test used to miss: the
    # negation was applied as "every OTHER path", denying everything).
    # secret.txt is IN the working dir, so it still resolves to "allow"
    # overall (default mode's own read_in_workdir row) -- the thing that
    # must be true is that the Read(**)/Read(!secret.txt) RULE PAIR itself
    # produced no match (`matched_rule is None`), not the mode-table
    # fallback rule wearing the Read(**) rule's clothes.
    excluded = engine.decide("Read", {"file_path": str(CWD / "secret.txt")}, tool=ReadTool())
    ctx.check(f"the negated path is NOT matched by the allow-rule pair (mode table decides it instead), "
              f"got action={excluded.action} source={excluded.source} matched_rule={excluded.matched_rule}",
              excluded.matched_rule is None and excluded.source == "mode")


@test
def test_negation_never_denies_other_paths_on_its_own(ctx: Ctx):
    """finding 6's core verified bug: a solitary `!`-rule (no positive
    rule alongside it in the same list) must never act as "every path
    except this one" -- it has NOTHING to cancel, so it must never fire
    at all."""
    engine = P.PermissionEngine(mode="auto", cwd=CWD, deny_rules=[
        P.parse_rule("Read(.env*)", source="user", base_dir=CWD),
        P.parse_rule("Read(!.env.example)", source="user", base_dir=CWD),
    ])
    d = engine.decide("Read", {"file_path": str(CWD / "README.md")}, tool=ReadTool())
    ctx.check(f"an unrelated file is NOT denied by the negation, got {d.action}", d.action == "allow")
    d_env = engine.decide("Read", {"file_path": str(CWD / ".env")}, tool=ReadTool())
    ctx.check(f".env itself is still denied by the positive rule, got {d_env.action}", d_env.action == "deny")
    d_example = engine.decide("Read", {"file_path": str(CWD / ".env.example")}, tool=ReadTool())
    ctx.check(f".env.example is exempted by the negation, got {d_example.action}", d_example.action == "allow")


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


@test
def test_mcp_rule_splits_on_first_double_underscore_not_last(ctx: Ctx):
    """finding 13: a TOOL name that itself contains "__" (e.g. sanitised
    from a name with two consecutive special chars) must still parse with
    the correct server -- rsplit (the LAST "__") used to grab too much
    into the server half."""
    r = P.parse_rule("mcp__srv__list__files", source="user")
    ctx.check(f"server is 'srv', not 'srv__list', got tool={r.tool!r} value={r.value!r}",
              r.tool == "srv" and r.value == "list__files")


@test
def test_mcp_tool_kind_deny_rule_removes_just_that_tool_from_catalog(ctx: Ctx):
    """finding 13 required outcome: "mcp__srv__tool deny rules remove the
    tool" -- a single-tool deny rule (kind mcp_tool) must remove ONLY
    that one name from the frozen catalog, not the whole server, and
    never a different tool of the same server."""
    deny = [P.parse_rule("mcp__srv__get_x", source="user")]
    removed = P.mcp_deny_tool_names(deny, ["mcp__srv__get_x", "mcp__srv__get_y", "mcp__other__get_x"])
    ctx.check(f"only the exact server+tool match is removed, got {removed}", removed == {"mcp__srv__get_x"})


@test
def test_mcp_tool_kind_deny_rule_glob_in_tool_position(ctx: Ctx):
    deny = [P.parse_rule("mcp__srv__list_*", source="user")]
    removed = P.mcp_deny_tool_names(deny, ["mcp__srv__list_files", "mcp__srv__list_dirs", "mcp__srv__get_x"])
    ctx.check(f"glob in the tool position matches multiple tools of that server, got {removed}",
              removed == {"mcp__srv__list_files", "mcp__srv__list_dirs"})


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
        # finding 16: the mode table's coverage used to stop at Bash/Write
        # -- TodoWrite/ToolSearch (finding 12) must be always-allow in
        # EVERY mode, with no explicit rule needed.
        d_todo = engine.decide("TodoWrite", {"todos": []})
        ctx.check(f"{mode}: TodoWrite always allowed, got {d_todo.action}", d_todo.action == "allow")
        d_search = engine.decide("ToolSearch", {"query": "select:Read"})
        ctx.check(f"{mode}: ToolSearch always allowed, got {d_search.action}", d_search.action == "allow")


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


@test
def test_mcp_server_glob_deny_removes_catalog_names(ctx: Ctx):
    """H3 must-do: mcp__srv/mcp__* deny rules honoured for CATALOG removal
    (bare_deny_tool_names' MCP-aware sibling), glob-aware (finding 15)."""
    deny = [P.parse_rule("mcp__*", source="user")]
    removed = P.mcp_deny_tool_names(deny, ["Bash", "Read", "mcp__srv__get_x", "mcp__other__y"])
    ctx.check(f"mcp__* removes every mcp tool, got {removed}", removed == {"mcp__srv__get_x", "mcp__other__y"})
    deny_one = [P.parse_rule("mcp__gith*", source="user")]
    removed_one = P.mcp_deny_tool_names(deny_one, ["mcp__github__get_issue", "mcp__gitlab__get_mr"])
    ctx.check(f"a server glob only removes matching servers, got {removed_one}", removed_one == {"mcp__github__get_issue"})


@test
def test_decide_never_raises_on_a_nul_byte_path(ctx: Ctx):
    """finding 15: a path-resolution error (a NUL byte makes `Path.resolve()`
    raise ValueError at the OS level) must be caught at `decide()`'s single
    entry point -- never left to kill the whole -p turn -- in any manual
    mode or whenever a path rule exists (both conditions reproduced here)."""
    engine = P.PermissionEngine(mode="default", cwd=CWD, deny_rules=[
        P.parse_rule("Read(secret.txt)", source="user", base_dir=CWD),
    ])
    bad_path = str(CWD) + "/\x00evil"
    d = engine.decide("Read", {"file_path": bad_path}, tool=ReadTool())
    ctx.check(f"a NUL-byte path is denied with a clear reason, never an exception, got {d.action}/{d.reason}",
              d.action == "deny")


# ---- H2 finding 16: the 8 new pinning tests --------------------------------

@test
def test_new_1_deny_env_prefix_double_space_and_inner_star_wildcard(ctx: Ctx):
    """finding 1: quote-aware env/wrapper stripping (no ALLOW-only keep-
    list), whitespace collapse, and `x * y *` compiling as a real wildcard
    ending -- all three verified-broken deny scenarios in one test."""
    engine = P.PermissionEngine(mode="auto", cwd=CWD, deny_rules=[
        P.parse_rule("Bash(git push:*)", source="user", base_dir=CWD),
    ])
    d1 = engine.decide("Bash", {"command": 'GIT_SSH_COMMAND="ssh -i k" git push origin main'})
    ctx.check(f"quoted GIT_*= prefix does not defeat the deny, got {d1.action}", d1.action == "deny")
    d2 = engine.decide("Bash", {"command": "GIT_TERMINAL_PROMPT=0 git push"})
    ctx.check(f"bare env prefix does not defeat the deny, got {d2.action}", d2.action == "deny")

    engine_rf = P.PermissionEngine(mode="auto", cwd=CWD, deny_rules=[
        P.parse_rule("Bash(rm -rf:*)", source="user", base_dir=CWD),
    ])
    d3 = engine_rf.decide("Bash", {"command": "rm  -rf /tmp/x"})  # double space
    ctx.check(f"a double space does not defeat the deny, got {d3.action}", d3.action == "deny")

    engine_star = P.PermissionEngine(mode="auto", cwd=CWD, deny_rules=[
        P.parse_rule("Bash(git push * --force *)", source="user", base_dir=CWD),
    ])
    d4 = engine_star.decide("Bash", {"command": "git push origin --force"})
    ctx.check(f"x * y * compiles as a real wildcard, got {d4.action}", d4.action == "deny")


@test
def test_new_2_redirection_ampersand_under_an_allow_rule(ctx: Ctx):
    """finding 11: `2>&1` must stay part of ONE segment (a redirection),
    never split into `2>` + `1` -- an allow rule for the whole command
    must still match it."""
    engine = P.PermissionEngine(mode="default", cwd=CWD, allow_rules=[
        P.parse_rule("Bash(npm test:*)", source="user", base_dir=CWD),
    ])
    d = engine.decide("Bash", {"command": "npm test 2>&1"})
    ctx.check(f"2>&1 does not break the allow-rule match, got {d.action}", d.action == "allow")
    segs = P.split_bash_segments("npm test 2>&1")
    ctx.check(f"2>&1 stays inside one segment, got {segs}", segs == ["npm test 2>&1"])


@test
def test_new_3_todowrite_always_allowed_in_print_mode_default(ctx: Ctx):
    """finding 12: TodoWrite must never land in the "other" mode-table
    category (which `-p` default would otherwise ask/deny for)."""
    engine = P.PermissionEngine(mode="default", cwd=CWD, print_mode=True)
    d = engine.decide("TodoWrite", {"todos": [{"content": "x", "status": "pending", "activeForm": "x"}]})
    ctx.check(f"TodoWrite allowed even in -p default (print mode), got {d.action}", d.action == "allow")


@test
def test_new_8_suggest_parse_decide_round_trips_to_allow(ctx: Ctx):
    """finding 13: a suggested rule must actually RE-ALLOW the exact call
    it was suggested for -- covers the outside-cwd Read (//-prefix, no
    doubled slash) and a multi-segment Bash command."""
    other_root = Path(tempfile.mkdtemp(prefix="perm-suggest-"))
    target = other_root / "outside.txt"
    engine = P.PermissionEngine(mode="default", cwd=CWD)
    read_input = {"file_path": str(target)}
    suggestion = engine.suggest_rule("Read", read_input)
    ctx.check(f"Read suggestion looks sane, got {suggestion!r}", suggestion is not None and suggestion.startswith("Read("))
    rule = P.parse_rule(suggestion, source="session", base_dir=CWD, action="allow")
    ctx.check(f"suggested Read rule parses cleanly, got {rule}", rule.kind != "invalid")
    engine2 = P.PermissionEngine(mode="default", cwd=CWD, allow_rules=[rule])
    d = engine2.decide("Read", read_input, tool=ReadTool())
    ctx.check(f"suggest -> parse -> decide round-trips to allow for Read, got {d.action}", d.action == "allow")

    bash_input = {"command": "cd /srv && make build"}
    bash_suggestion = engine.suggest_rule("Bash", bash_input)
    ctx.check(f"Bash suggestion targets the real (non-read-only) segment, got {bash_suggestion!r}",
              bash_suggestion is not None and "make" in bash_suggestion)
    bash_rules = [P.parse_rule(r.strip(), source="session", base_dir=CWD, action="allow")
                  for r in bash_suggestion.split(",")]
    engine3 = P.PermissionEngine(mode="default", cwd=CWD, allow_rules=bash_rules)
    d_bash = engine3.decide("Bash", bash_input)
    ctx.check(f"suggest -> parse -> decide round-trips to allow for Bash, got {d_bash.action}", d_bash.action == "allow")


# ---- H9 whole-tree review finding 21: Read is always allowed under this
# session's own tool-results/ spill directory --------------------------------

@test
def test_h9b_f21_read_on_own_spill_dir_allowed_in_every_mode_with_no_rules(ctx: Ctx):
    """Verified bug: the truncation hint's own "Use Read with offset/limit
    on <spill>" pointer asked in TUI default mode and was denied outright
    in -p default/acceptEdits/dontAsk -- there is no rule a user could even
    write to fix this in advance, since the spill path is random per
    session."""
    tool_results_dir = Path(tempfile.mkdtemp(prefix="h9b-f21-spill-")) / "tool-results"
    spill_file = tool_results_dir / "call_abc123.txt"
    for mode in ("default", "acceptEdits", "dontAsk", "auto", "plan", "bypassPermissions"):
        engine = P.PermissionEngine(mode=mode, cwd=CWD)
        engine.tool_results_dir = tool_results_dir
        d = engine.decide("Read", {"file_path": str(spill_file)})
        ctx.check(f"mode={mode!r}: Read on the spill dir is allowed, got {d.action} ({d.reason})",
                  d.action == "allow")


@test
def test_h9b_f21_read_outside_the_spill_dir_is_unaffected(ctx: Ctx):
    """The built-in allow is scoped to the spill dir alone -- an ordinary
    Read elsewhere still goes through the normal rule/mode-table path,
    same as before this fix existed."""
    tool_results_dir = Path(tempfile.mkdtemp(prefix="h9b-f21-spill2-")) / "tool-results"
    other_dir = Path(tempfile.mkdtemp(prefix="h9b-f21-other-"))
    other_file = other_dir / "not-a-spill.txt"
    engine = P.PermissionEngine(mode="default", cwd=CWD)
    engine.tool_results_dir = tool_results_dir
    d = engine.decide("Read", {"file_path": str(other_file)})
    ctx.check(f"an unrelated path is NOT covered by the built-in allow, got {d.action} ({d.reason})",
              "tool-result" not in d.reason)


@test
def test_h9b_f21_explicit_user_deny_rule_still_wins_over_the_spill_dir_allow(ctx: Ctx):
    """The built-in allow is a convenience, never an override of the ONE
    real gate this harness keeps: the user's own explicit rules."""
    tool_results_dir = Path(tempfile.mkdtemp(prefix="h9b-f21-spill3-")) / "tool-results"
    spill_file = tool_results_dir / "call_xyz.txt"
    engine = P.PermissionEngine(mode="auto", cwd=CWD)
    # An ABSOLUTE-path deny rule targeting the spill dir specifically (the
    # engine's own `_suggest_abs_path_value` -- the SAME helper a real
    # suggested-rule flow uses -- so this is a platform-correct `//`-form
    # on POSIX and a `C:/...`-form on Windows, never a guess at either).
    abs_value = engine._suggest_abs_path_value(tool_results_dir)
    deny_rule = P.parse_rule(f"Read({abs_value}/**)", source="session", base_dir=CWD, action="deny")
    engine.deny_rules = [deny_rule]
    engine.tool_results_dir = tool_results_dir
    d = engine.decide("Read", {"file_path": str(spill_file)})
    ctx.check(f"an explicit user deny rule still wins, got {d.action} ({d.reason})", d.action == "deny")


@test
def test_h9b_f21_real_session_wires_tool_results_dir_from_its_own_log(ctx: Ctx):
    """End to end: a real Session sets this up automatically -- no caller
    has to remember to."""
    import os
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import ModelProfile, parse_model_ref

    # H15 Part D2.1: `Session`'s own `SessionLog` ALWAYS resolves its
    # storage root via `bridge_home()` -- independent of the `state_dir=`
    # passed to `Session` itself -- scoped here, the one test in this file
    # that builds a real Session.
    old_state_dir = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_STATE_DIR"] = str(Path(tempfile.mkdtemp(prefix="h9b-f21-state-env-")))
    try:
        cwd = Path(tempfile.mkdtemp(prefix="h9b-f21-session-"))
        session_ctx = SessionContext(cwd=cwd, model_label="or:mock/x", bare=True)
        model_ref = parse_model_ref("or:mock/x")
        engine = P.PermissionEngine(mode="default", cwd=cwd)
        session = Session(cwd=cwd, model_ref=model_ref, model_profile=ModelProfile(), creds=None,
                           state_dir=Path(tempfile.mkdtemp(prefix="h9b-f21-state-")), model_label="or:mock/x",
                           session_context=session_ctx, permission_engine=engine)
        ctx.check("tool_results_dir was wired automatically", engine.tool_results_dir is not None)
        ctx.check(f"it matches THIS session's own log dir/session_id, got {engine.tool_results_dir}",
                  engine.tool_results_dir == session.log.dir / session.log.session_id / "tool-results")
        spill_file = engine.tool_results_dir / "call_1.txt"
        d = engine.decide("Read", {"file_path": str(spill_file)})
        ctx.check(f"and Read on it is allowed, got {d.action}", d.action == "allow")
    finally:
        if old_state_dir is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = old_state_dir


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
