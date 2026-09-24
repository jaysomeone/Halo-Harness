"""tests.test_prune -- agent/prune.py: deterministic tool-result pruning
(head/tail on any oversized result, a 2,000-char stub outside the 40k
protection window), prefix stability across turns, and image offload."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.agent.prune import (
    OLD_TOOL_RESULT_CLEARED, context_breakdown, estimate_text_tokens, prune_messages,
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
def test_oversized_tool_result_head_tail_truncated(ctx: Ctx):
    big = "A" * 5000 + "MIDDLE" + "B" * 5000
    msgs = [_plain_user("q"), _assistant_tool_use("t1"), _user_tool_result("t1", big)]
    out = prune_messages(msgs)
    pruned_text = out[2]["content"][0]["content"]
    ctx.check("truncated result is shorter than the original", len(pruned_text) < len(big))
    ctx.check("starts with the original head", pruned_text.startswith("A" * 4096))
    ctx.check("ends with the original tail", pruned_text.endswith("B" * 1024))
    ctx.check("MIDDLE (the cut region) is gone", "MIDDLE" not in pruned_text)


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
def test_estimate_text_tokens_chars_per_4(ctx: Ctx):
    ctx.check("estimate ~= chars/4", estimate_text_tokens("A" * 400) == 100)
    ctx.check("empty text is 0 tokens", estimate_text_tokens("") == 0)
    ctx.check("never raises on None", estimate_text_tokens(None) == 0)


@test
def test_context_breakdown_buckets(ctx: Ctx):
    msgs = [_plain_user("q"), _assistant_tool_use("t1"), _user_tool_result("t1", "A" * 20000)]
    pruned = prune_messages(msgs)
    bd = context_breakdown("SYSTEM PROMPT TEXT", msgs, [{"name": "Read"}], pruned)
    ctx.check("system bucket present", bd["system"] > 0)
    ctx.check("tools bucket present", bd["tools"] > 0)
    ctx.check("messages bucket reflects the PRUNED size", bd["messages"] > 0)
    ctx.check("pruned bucket is positive (rule 1 removed real content)", bd["pruned"] > 0)
    ctx.check("total = system + tools + messages", bd["total"] == bd["system"] + bd["tools"] + bd["messages"])


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
