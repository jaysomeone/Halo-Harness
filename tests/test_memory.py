"""tests.test_memory -- config/memory.py: MEMORY.md line/byte cap +
truncation marker, topic-file frontmatter round-trip, index_text formatting,
disabled-via-settings/env/bare.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from halo_harness.config.memory import MemoryStore

test, TESTS = new_registry()


class FakeSettings:
    auto_memory_enabled = True


@test
def test_memory_md_loaded_and_mentions_media_server(ctx: Ctx):
    """This is the exact content path the H0 acceptance check exercises
    live (the owner's real MEMORY.md mentions a home media server by name
    and a LAN host) -- proven here hermetically against the fixture's
    equivalent, synthetic content."""
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    store = MemoryStore(fh["proj"], FakeSettings())
    idx = store.load_index()
    ctx.check("MEMORY.md exists", idx.exists)
    ctx.check("mentions the media server's name", "mediabox" in idx.text)
    ctx.check("mentions the LAN host", "lan-host.lan" in idx.text)


@test
def test_memory_md_capped_at_200_lines(ctx: Ctx):
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    store = MemoryStore(fh["proj"], FakeSettings())
    idx = store.load_index()
    line_count = idx.text.count("\n")
    ctx.check(f"line count near the 200-line cap (+1 for the marker), got {line_count}", line_count <= 202)
    ctx.check("truncation marker appended", "truncated" in idx.text)


@test
def test_memory_md_25kb_cap(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="memory-25kb-"))
    mem_dir = d / "memory"
    mem_dir.mkdir(parents=True)
    # 100 lines, each ~500 bytes -> ~50KB, well over the 25KB cap but under
    # the 200-line cap, so the BYTE cap (not the line cap) is what fires.
    big_lines = [f"line {i}: " + ("x" * 480) for i in range(100)]
    (mem_dir / "MEMORY.md").write_text("\n".join(big_lines), encoding="utf-8")

    class FakeSettingsWithDir:
        auto_memory_enabled = True
        auto_memory_directory = str(mem_dir)

    store = MemoryStore(d, FakeSettingsWithDir())
    idx = store.load_index()
    encoded_len = idx.text.encode("utf-8")
    ctx.check(f"result stays under ~25.1KB after the marker, got {len(encoded_len)} bytes", len(encoded_len) < 25856)
    ctx.check("truncation marker present", "truncated" in idx.text)
    ctx.check("cut on a line boundary (no partial 'line N:' fragment at the end before the marker)",
              "\n[..." in idx.text)


@test
def test_topic_file_frontmatter_round_trip(ctx: Ctx):
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    store = MemoryStore(fh["proj"], FakeSettings())
    topics = {t.name: t for t in store.entries()}
    ctx.check("project_claude_bridge topic present", "project_claude_bridge" in topics)
    ctx.check("project_media_server topic present", "project_media_server" in topics)
    media = topics["project_media_server"]
    ctx.check(f"type == project, got {media.type!r}", media.type == "project")
    ctx.check("description mentions the media server's name", "mediabox" in media.description)


@test
def test_topic_file_without_frontmatter_still_included(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="memory-no-frontmatter-"))
    mem_dir = d / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "plain_topic.md").write_text("no frontmatter here at all", encoding="utf-8")

    class FakeSettingsWithDir:
        auto_memory_enabled = True
        auto_memory_directory = str(mem_dir)

    store = MemoryStore(d, FakeSettingsWithDir())
    topics = store.entries()
    ctx.check("still appears with filename-derived name", any(t.name == "plain_topic" for t in topics))
    ctx.check("description defaults to empty string", next(t for t in topics if t.name == "plain_topic").description == "")


@test
def test_index_text_formatting(ctx: Ctx):
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    store = MemoryStore(fh["proj"], FakeSettings())
    text = store.index_text()
    ctx.check("one line per topic file", text.count("\n") == 1)  # 2 topics -> 1 internal newline
    ctx.check("format is '- name (type): description'", text.startswith("- "))


@test
def test_memory_excludes_memory_md_itself_from_topics(ctx: Ctx):
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    store = MemoryStore(fh["proj"], FakeSettings())
    names = [t.path.name for t in store.entries()]
    ctx.check("MEMORY.md itself never appears as a topic file", "MEMORY.md" not in names)


@test
def test_disabled_via_settings_flag(ctx: Ctx):
    fh = build_fake_home()

    class DisabledSettings:
        auto_memory_enabled = False

    store = MemoryStore(fh["proj"], DisabledSettings())
    idx = store.load_index()
    ctx.check("disabled -> not enabled", not store.enabled)
    ctx.check("disabled -> empty index", idx.text == "" and idx.exists is False and idx.topics == [])


@test
def test_disabled_via_env_var(ctx: Ctx):
    fh = build_fake_home()
    old = os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY")
    try:
        os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        store = MemoryStore(fh["proj"], FakeSettings())
        ctx.check("CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 disables memory", not store.enabled)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)
        else:
            os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = old


@test
def test_disabled_via_bare(ctx: Ctx):
    fh = build_fake_home()
    store = MemoryStore(fh["proj"], FakeSettings(), bare=True)
    ctx.check("bare=True disables memory", not store.enabled)


@test
def test_missing_memory_dir_returns_empty_not_error(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="memory-missing-"))
    store = MemoryStore(d, FakeSettings())
    idx = store.load_index()
    ctx.check("missing memory dir -> exists False", idx.exists is False)
    ctx.check("missing memory dir -> no crash, empty topics", idx.topics == [])


@test
def test_write_and_update_index_write_real_files(ctx: Ctx):
    """H10 Part B: `write`/`update_index` were H0-era stubs ("no tool
    exists yet to call this") -- `/improve`'s own memory candidates are the
    first real caller, so this now proves the real implementation instead
    of the placeholder NotImplementedError."""
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    store = MemoryStore(fh["proj"], FakeSettings())
    path = store.write(filename="x.md", name="n", description="d", type="feedback", body="body text",
                        origin_session_id="sess")
    ctx.check("write() returns the real path", path == store.memory_dir_path / "x.md")
    ctx.check("the file actually exists", path.exists())
    content = path.read_text(encoding="utf-8")
    ctx.check("frontmatter name present", "name: n" in content)
    ctx.check("frontmatter type: feedback present", "type: feedback" in content)
    ctx.check("body present", "body text" in content)
    idx_path = store.update_index(filename="y.md", title="other-topic", description="another one")
    ctx.check("update_index returns MEMORY.md's path", idx_path == store.memory_dir_path / "MEMORY.md")
    idx_text = idx_path.read_text(encoding="utf-8")
    ctx.check("both x.md (from write's own index append) and y.md are indexed",
              "(x.md)" in idx_text and "(y.md)" in idx_text)
    # write() refuses to silently overwrite an existing file.
    try:
        store.write(filename="x.md", name="n2", description="d2", type="feedback", body="body2")
        ctx.check("write() must raise FileExistsError on a name collision", False)
    except FileExistsError:
        ctx.check("write() raises FileExistsError on a name collision", True)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
