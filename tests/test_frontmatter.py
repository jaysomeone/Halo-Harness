"""tests.test_frontmatter -- config/frontmatter.py's YAML-subset parser."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.config.frontmatter import parse

test, TESTS = new_registry()


@test
def test_no_frontmatter_passthrough(ctx: Ctx):
    fm, body = parse("just a plain markdown file\nwith no frontmatter\n")
    ctx.check("empty dict when no frontmatter", fm == {})
    ctx.check("body unchanged", body == "just a plain markdown file\nwith no frontmatter\n")


@test
def test_basic_scalars(ctx: Ctx):
    fm, body = parse('---\nname: deploy\ncount: 3\nactive: true\ndisabled: FALSE\nnothing: null\n---\nBody text\n')
    ctx.check("name is string", fm.get("name") == "deploy")
    ctx.check("count is int", fm.get("count") == 3 and isinstance(fm.get("count"), int))
    ctx.check("active True (case-insensitive)", fm.get("active") is True)
    ctx.check("disabled False (case-insensitive)", fm.get("disabled") is False)
    ctx.check("nothing is None", fm.get("nothing") is None)
    ctx.check("body after closing ---", body == "Body text")


@test
def test_quoted_strings(ctx: Ctx):
    fm, _ = parse('---\na: "quoted value"\nb: \'single quoted\'\nc: unquoted\n---\n')
    ctx.check('double-quoted stripped', fm.get("a") == "quoted value")
    ctx.check('single-quoted stripped', fm.get("b") == "single quoted")
    ctx.check('unquoted kept as-is', fm.get("c") == "unquoted")


@test
def test_inline_list(ctx: Ctx):
    fm, _ = parse('---\ntags: [Bash(git *), Read, "with space"]\nempty: []\n---\n')
    ctx.check("inline list parsed", fm.get("tags") == ["Bash(git *)", "Read", "with space"])
    ctx.check("empty inline list -> []", fm.get("empty") == [])


@test
def test_block_list(ctx: Ctx):
    fm, _ = parse('---\narguments:\n  - env\n  - target\nname: deploy\n---\n')
    ctx.check("block list parsed", fm.get("arguments") == ["env", "target"])
    ctx.check("key after block list still parses", fm.get("name") == "deploy")


@test
def test_one_level_nested_mapping(ctx: Ctx):
    fm, _ = parse(
        "---\n"
        "name: t\n"
        "metadata:\n"
        "  type: project\n"
        "  originSessionId: abc-123\n"
        "  modified: 2026-09-20\n"
        "---\n"
    )
    ctx.check("metadata is a dict", isinstance(fm.get("metadata"), dict))
    ctx.check("metadata.type", fm.get("metadata", {}).get("type") == "project")
    ctx.check("metadata.originSessionId", fm.get("metadata", {}).get("originSessionId") == "abc-123")
    ctx.check("metadata.modified", fm.get("metadata", {}).get("modified") == "2026-09-20")
    ctx.check("sibling key after nested mapping", fm.get("name") == "t")


@test
def test_block_scalar_literal(ctx: Ctx):
    fm, _ = parse(
        "---\n"
        "body: |\n"
        "  line one\n"
        "  line two\n"
        "name: x\n"
        "---\n"
    )
    ctx.check("literal block preserves newlines", fm.get("body") == "line one\nline two")
    ctx.check("key after block scalar", fm.get("name") == "x")


@test
def test_block_scalar_folded(ctx: Ctx):
    fm, _ = parse(
        "---\n"
        "body: >\n"
        "  line one\n"
        "  line two\n"
        "---\n"
    )
    ctx.check("folded block joins with spaces", fm.get("body") == "line one line two")


@test
def test_comment_lines_skipped(ctx: Ctx):
    fm, _ = parse("---\n# a comment\nname: x\n# another\n---\n")
    ctx.check("comment lines don't become keys", fm == {"name": "x"})


@test
def test_round_trip_rolo_style_frontmatter(ctx: Ctx):
    """The exact frontmatter shape the owner's real memory topic files use."""
    text = (
        "---\n"
        "name: project_media_server\n"
        'description: Home media server "mediabox" runs on lan-host.lan\n'
        "metadata:\n"
        "  type: project\n"
        "  originSessionId: sess-1\n"
        "  modified: 2026-08-12\n"
        "---\n"
        "Body content about mediabox.\n"
    )
    fm, body = parse(text)
    ctx.check("name", fm.get("name") == "project_media_server")
    ctx.check("description", fm.get("description") == 'Home media server "mediabox" runs on lan-host.lan')
    ctx.check("metadata.type", fm.get("metadata", {}).get("type") == "project")
    ctx.check("body", body == "Body content about mediabox.")


@test
def test_malformed_input_never_raises(ctx: Ctx):
    try:
        fm, body = parse("---\nno closing delimiter\nmore text\n")
        ctx.check("no closing '---' -> treated as no frontmatter", fm == {})
    except Exception as e:
        ctx.check(f"parse() must never raise, got {e!r}", False)

    try:
        parse("")
    except Exception as e:
        ctx.check(f"parse('') must never raise, got {e!r}", False)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
