"""tests.test_prune -- agent/prune.py: deterministic tool-result pruning (a
2,000-char stub outside the 40k protection window; a result INSIDE it is
never touched -- H5b finding 1), prefix stability across turns via
`force_stub_ids`/`compute_stub_candidates` (H5b finding 2), and image
offload."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.agent.prune import (
    OLD_TOOL_RESULT_CLEARED, compute_stub_candidates, context_breakdown, estimate_text_tokens, prune_messages,
)

test, TESTS = new_registry()


def _user_tool_result(tool_use_id: str, text: str) -> dict:
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": text}]}


def _assistant_tool_use(tool_use_id: str, name: str = "Read") -> dict:
    return {"role": "assistant", "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": {}}]}


def _plain_user(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


@test
def test_small_tool_result_untouched(ctx: Ctx):
    msgs = [_plain_user("q"), _assistant_tool_use("t1"), _user_tool_result("t1", "short result")]
    out = prune_messages(msgs)
    ctx.check("small tool_result content unchanged", out[2]["content"][0]["content"] == "short result")


@test
def test_h5b_f01_oversized_in_window_result_reaches_the_model_in_full(ctx: Ctx):
    """H5b finding 1 (critical): a tool_result INSIDE the 40k-token
    protection window is never pruned here, no matter how large -- its own
    tool already capped it via tools/truncate.py's spill_and_truncate at
    the moment it was logged, which is the ONE place its size is this
    harness's business to bound. The old "any result over 8,192 chars gets
    head/tail-truncated to ~5,120 chars, regardless of position" rule
    silently cut a fresh 28,690-char Read down on the very next request,
    with no spill pointer, while the TUI kept showing the untouched log
    copy -- this is the regression test for exactly that bug, sized to the
    review's own 44 KB repro."""
    big = "A" * 22_000 + "MIDDLE" + "B" * 22_000  # ~44,006 chars, matches the review's own repro size
    msgs = [_plain_user("q"), _assistant_tool_use("t1"), _user_tool_result("t1", big)]
    out = prune_messages(msgs)
    result_text = out[2]["content"][0]["content"]
    ctx.check("a 44KB in-window tool_result reaches the wire byte-for-byte", result_text == big)
    ctx.check("MIDDLE (would have been the cut region under the old rule) is still present", "MIDDLE" in result_text)


@test
def test_result_at_or_under_threshold_untouched(ctx: Ctx):
    exact = "X" * 8192
    msgs = [_plain_user("q"), _assistant_tool_use("t1"), _user_tool_result("t1", exact)]
    out = prune_messages(msgs)
    ctx.check("exactly-at-threshold result is NOT truncated (> not >=)",
              out[2]["content"][0]["content"] == exact)


@test
def test_outside_protection_window_gets_stubbed(ctx: Ctx):
    # Build enough NEWER tool_result content (> 40,000 tokens ~ 160,000
    # chars) after an old small result that the old one falls outside the
    # protection window and gets stubbed, even though it is itself small.
    old_result = "old but important content " * 5  # well under 8192 chars
    msgs = [_plain_user("q0"), _assistant_tool_use("t0"), _user_tool_result("t0", old_result)]
    filler = "F" * 45000  # ~11,250 tokens each
    for i in range(1, 6):  # 5 * 11,250 ~= 56,250 tokens of newer content > 40,000
        tid = f"t{i}"
        msgs.append(_plain_user(f"q{i}"))
        msgs.append(_assistant_tool_use(tid))
        msgs.append(_user_tool_result(tid, filler))

    out = prune_messages(msgs)
    stubbed = out[2]["content"][0]["content"]
    ctx.check(f"old result outside the 40k window is stubbed with the marker, got {stubbed[-60:]!r}",
              OLD_TOOL_RESULT_CLEARED in stubbed)
    ctx.check("stub is much shorter than the original filler", len(stubbed) < 3000)
    # The newest result (t5) is well within the protection window, so rule 2
    # (the harsh stub) never applies to it -- it only ever gets rule 1's
    # gentler head/tail treatment (it IS > 8,192 chars on its own).
    newest = out[-1]["content"][0]["content"]
    ctx.check("newest tool_result (inside the window) is never stubbed by rule 2",
              OLD_TOOL_RESULT_CLEARED not in newest)
    ctx.check("newest tool_result keeps its own head", newest.startswith(filler[:4096]))


@test
def test_pruning_is_deterministic(ctx: Ctx):
    big = "Z" * 20000
    msgs = [_plain_user("q"), _assistant_tool_use("t1"), _user_tool_result("t1", big)]
    out1 = prune_messages(msgs)
    out2 = prune_messages(msgs)
    ctx.check("two independent prune_messages() calls on the same input agree byte-for-byte", out1 == out2)


@test
def test_input_messages_not_mutated(ctx: Ctx):
    big = "Q" * 20000
    msgs = [_plain_user("q"), _assistant_tool_use("t1"), _user_tool_result("t1", big)]
    prune_messages(msgs)
    ctx.check("caller's own list is never mutated", msgs[2]["content"][0]["content"] == big)


@test
def test_prefix_stability_once_stubbed_always_stubbed(ctx: Ctx):
    """Once a tool_result sits FAR outside the protection-window boundary
    (not the single item straddling it) and has been stubbed, appending
    MORE new turns afterward must reproduce the EXACT same stub bytes for
    it and leave the plain non-tool-result messages around it untouched --
    the cache-prefix-preserving property. Only the ONE message whose
    distance-from-end straddles the 40k-token boundary is allowed to change
    verdict (rule-1-truncated -> rule-2-stubbed) as new content pushes it
    further back; that is an inherent, one-time, monotonic transition of a
    size-bounded sliding window, not an instability."""
    old_result = "important early content"
    base = [_plain_user("q0"), _assistant_tool_use("t0"), _user_tool_result("t0", old_result)]
    filler = "F" * 45000
    for i in range(1, 6):
        tid = f"t{i}"
        base += [_plain_user(f"q{i}"), _assistant_tool_use(tid), _user_tool_result(tid, filler)]

    out_a = prune_messages(base)
    stub_a = out_a[2]["content"][0]["content"]
    ctx.check("first call stubs the old result", OLD_TOOL_RESULT_CLEARED in stub_a)

    # Append one more turn and re-prune from scratch.
    extended = base + [_plain_user("q6"), _assistant_tool_use("t6"), _user_tool_result("t6", filler)]
    out_b = prune_messages(extended)
    stub_b = out_b[2]["content"][0]["content"]
    ctx.check("the oldest, already-far-outside-the-window result is byte-identical after appending a new turn",
              stub_a == stub_b)
    ctx.check("the plain (non-tool-result) messages around it are byte-identical",
              out_a[0] == out_b[0] and out_a[1] == out_b[1] and out_a[3] == out_b[3])
    # t1 (also stubbed in both, per the running-token math) must match too.
    ctx.check("a second already-stubbed result is also byte-identical",
              out_a[5]["content"][0]["content"] == out_b[5]["content"][0]["content"]
              and OLD_TOOL_RESULT_CLEARED in out_a[5]["content"][0]["content"])
    # At most the single message straddling the boundary differs; count
    # divergences over the shared prefix to make that bound explicit.
    shared = min(len(out_a), len(out_b))
    diffs = sum(1 for i in range(shared) if out_a[i] != out_b[i])
    ctx.check(f"at most one message (the boundary-straddling one) changes verdict, got {diffs} diffs",
              diffs <= 1)


@test
def test_image_offloaded_after_n_turns(ctx: Ctx):
    msgs = []
    for i in range(6):
        msgs.append({"role": "user", "content": [
            {"type": "text", "text": f"look at this q{i}"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "xxx"}},
        ]})
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"ok a{i}"}]})
    out = prune_messages(msgs, image_offload_turns=3)
    first_turn_content = out[0]["content"]
    ctx.check("an old image (turn 0 of 6) is offloaded to a text placeholder",
              all(b.get("type") != "image" for b in first_turn_content))
    last_turn_content = out[-2]["content"]  # last user turn's message
    ctx.check("a recent image (within the last 3 turns) is kept",
              any(b.get("type") == "image" for b in last_turn_content))


@test
def test_h9b_f20_tool_result_messages_never_count_as_their_own_turn(ctx: Ctx):
    """H9 whole-tree review finding 20: a tool_result is ALSO sent as a
    `role: "user"` wire message -- verified bug: a screenshot in "fix the
    bug in this screenshot" was offloaded after just 3 TOOL STEPS of the
    SAME turn (each tool_result message wrongly counted as its own "user
    turn"), not `image_offload_turns` real turns later. One real user
    turn with a screenshot, 3 tool round-trips, all still the CURRENT
    turn -- the image must survive."""
    msgs = [
        {"role": "user", "content": [
            {"type": "text", "text": "fix the bug in this screenshot"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "xxx"}},
        ]},
        _assistant_tool_use("t1"), _user_tool_result("t1", "result 1"),
        _assistant_tool_use("t2"), _user_tool_result("t2", "result 2"),
        _assistant_tool_use("t3"), _user_tool_result("t3", "result 3"),
        {"role": "assistant", "content": [{"type": "text", "text": "fixed it"}]},
    ]
    out = prune_messages(msgs, image_offload_turns=3)
    first_msg_content = out[0]["content"]
    ctx.check("the screenshot from THIS turn survives 3 tool round-trips within the same turn",
              any(b.get("type") == "image" for b in first_msg_content))


@test
def test_h9b_f20_image_nested_inside_a_tool_result_is_also_offloaded(ctx: Ctx):
    """The other half of finding 20: an image returned BY A TOOL (nested
    inside a tool_result block's own `content` list) was never reached by
    the old top-level-only block loop -- never offloaded no matter how old."""
    old_tool_result_with_image = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "t0", "content": [
            {"type": "text", "text": "here is the screenshot"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "xxx"}},
        ]}],
    }
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "q0"}]},
        _assistant_tool_use("t0"), old_tool_result_with_image,
        {"role": "assistant", "content": [{"type": "text", "text": "a0"}]},
    ]
    # 6 more real turns, well past image_offload_turns=3, so the old nested
    # image is now "old" by the same measure a top-level one would be.
    for i in range(1, 7):
        msgs.append({"role": "user", "content": [{"type": "text", "text": f"q{i}"}]})
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]})
    out = prune_messages(msgs, image_offload_turns=3)
    nested_tool_result = out[2]["content"][0]
    ctx.check(f"the nested content is still a tool_result block, got {nested_tool_result!r}",
              nested_tool_result.get("type") == "tool_result")
    inner = nested_tool_result.get("content")
    ctx.check(f"the nested image was offloaded to a text placeholder, got {inner!r}",
              isinstance(inner, list) and all(b.get("type") != "image" for b in inner)
              and any(b.get("type") == "text" and "offloaded" in b.get("text", "") for b in inner))


@test
def test_estimate_text_tokens_chars_per_4(ctx: Ctx):
    ctx.check("estimate ~= chars/4", estimate_text_tokens("A" * 400) == 100)
    ctx.check("empty text is 0 tokens", estimate_text_tokens("") == 0)
    ctx.check("never raises on None", estimate_text_tokens(None) == 0)


@test
def test_context_breakdown_buckets_with_nothing_pruned(ctx: Ctx):
    """H5b finding 1: a single in-window tool_result, however large, is
    never pruned any more -- the `pruned` bucket for THIS shape is
    correctly 0, not a stale non-zero number from the old blanket rule."""
    msgs = [_plain_user("q"), _assistant_tool_use("t1"), _user_tool_result("t1", "A" * 20000)]
    pruned = prune_messages(msgs)
    bd = context_breakdown("SYSTEM PROMPT TEXT", msgs, [{"name": "Read"}], pruned)
    ctx.check("system bucket present", bd["system"] > 0)
    ctx.check("tools bucket present", bd["tools"] > 0)
    ctx.check("messages bucket reflects the (unpruned) size", bd["messages"] > 0)
    ctx.check("pruned bucket is 0 -- nothing outside the window to prune", bd["pruned"] == 0)
    ctx.check("total = system + tools + messages", bd["total"] == bd["system"] + bd["tools"] + bd["messages"])


@test
def test_context_breakdown_buckets_with_real_pruning(ctx: Ctx):
    """Same buckets, but with enough content that rule 2 (the protection
    window) genuinely stubs an old result -- `pruned` must reflect that."""
    old_result = "old but important content " * 5
    msgs = [_plain_user("q0"), _assistant_tool_use("t0"), _user_tool_result("t0", old_result)]
    filler = "F" * 45000
    for i in range(1, 6):
        tid = f"t{i}"
        msgs += [_plain_user(f"q{i}"), _assistant_tool_use(tid), _user_tool_result(tid, filler)]
    pruned = prune_messages(msgs)
    bd = context_breakdown("SYSTEM PROMPT TEXT", msgs, [{"name": "Read"}], pruned)
    ctx.check("pruned bucket is positive (rule 2 stubbed the old result)", bd["pruned"] > 0)
    ctx.check("total = system + tools + messages", bd["total"] == bd["system"] + bd["tools"] + bd["messages"])


# ---- H5b finding 2: compute_stub_candidates / force_stub_ids --------------

def _turn_with_filler(tid: str, i: int, filler: str) -> list:
    return [_plain_user(f"q{i}"), _assistant_tool_use(tid), _user_tool_result(tid, filler)]


@test
def test_compute_stub_candidates_matches_what_prune_messages_would_pick(ctx: Ctx):
    old_result = "old but important content " * 5
    msgs = [_plain_user("q0"), _assistant_tool_use("t0"), _user_tool_result("t0", old_result)]
    filler = "F" * 45000
    for i in range(1, 6):
        msgs += _turn_with_filler(f"t{i}", i, filler)
    candidates = compute_stub_candidates(msgs)
    ctx.check("t0 (well outside the window) is a candidate", "t0" in candidates)
    ctx.check("t5 (the newest, inside the window) is never a candidate", "t5" not in candidates)
    ctx.check("candidate token estimate is positive", candidates["t0"] > 0)


@test
def test_force_stub_ids_stubs_exactly_the_given_set_no_token_math(ctx: Ctx):
    """`force_stub_ids` stubs precisely what it's given, even a tiny result
    well inside the protection window that raw token math would never pick
    on its own -- proving the caller's persisted decision, not a fresh
    recomputation, is what actually governs the output."""
    msgs = [_plain_user("q"), _assistant_tool_use("t1"), _user_tool_result("t1", "tiny result")]
    out = prune_messages(msgs, force_stub_ids=frozenset({"t1"}))
    ctx.check("the forced id is stubbed despite being tiny and recent",
              OLD_TOOL_RESULT_CLEARED in out[2]["content"][0]["content"])


@test
def test_force_stub_ids_empty_set_prunes_nothing(ctx: Ctx):
    old_result = "old content " * 5
    msgs = [_plain_user("q0"), _assistant_tool_use("t0"), _user_tool_result("t0", old_result)]
    filler = "F" * 45000
    for i in range(1, 6):
        msgs += _turn_with_filler(f"t{i}", i, filler)
    out = prune_messages(msgs, force_stub_ids=frozenset())
    ctx.check("nothing stubbed when the caller's own committed set is empty",
              OLD_TOOL_RESULT_CLEARED not in out[0]["content"][0].get("content", "")
              and old_result == out[2]["content"][0]["content"])


@test
def test_h5b_f02_stable_force_stub_ids_gives_byte_identical_prefix_across_steps(ctx: Ctx):
    """H5b finding 2: this is the regression test for "the stub boundary
    slides forward one result per step" -- with a STABLE, caller-committed
    `force_stub_ids` (the whole point of `_pruned_messages_for_wire` on
    Session), appending ONE more turn must reproduce the SAME bytes for
    every earlier message; only the newly-appended turn differs."""
    old_result = "old but important content " * 5
    base = [_plain_user("q0"), _assistant_tool_use("t0"), _user_tool_result("t0", old_result)]
    filler = "F" * 45000
    for i in range(1, 6):
        base += _turn_with_filler(f"t{i}", i, filler)

    committed = frozenset(compute_stub_candidates(base))
    ctx.check("something was committed", bool(committed))
    out_a = prune_messages(base, force_stub_ids=committed)

    extended = base + _turn_with_filler("t6", 6, filler)
    out_b = prune_messages(extended, force_stub_ids=committed)  # SAME committed set -- not recomputed

    shared = min(len(out_a), len(out_b))
    ctx.check("every shared message is byte-identical (stable prefix)",
              all(out_a[i] == out_b[i] for i in range(shared)))
    ctx.check("only the newly-appended turn is new", len(out_b) == len(out_a) + 3)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
