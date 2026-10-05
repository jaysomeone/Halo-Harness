# Qwen-at-work baseline -- Halo 2.0.2 round 5 (2026-10-03)

Mock-server baseline, not a live-account one: this round fixes concrete code defects the
research doc (`docs/harness/QWEN-RESEARCH.md`) found in the repo, found by reading
`halo_harness/**/*.py` against Qwen's own documented wire shapes and Databricks' own
documented restrictions -- not by live calls against a real Qwen endpoint (the owner's
work VM, the only place `databricks-openjev-qwen35-4b` is reachable, was not available to
this worker). Every row below is a `tests/test_qwen_*.py`/`tests/test_hooks.py`/
`tests/test_repair.py`/`tests/test_request_builder.py` pinning test, run against
`tests/helpers/mock_openai.py`/`tests/helpers/mock_databricks.py`, never a real account.
"Rate" is pass/fail of that one shape's own pinning test(s), not a volume percentage --
there is no live call volume to measure yet.

## Results

| Shape | Mechanism | Mock scenario / fixture | Result | Pinning test |
|---|---|---|---|---|
| Hermes `<tool_call>{json}</tool_call>` | `hooks.leak_parser` (`hermes_tool_call`, pre-existing) | inline text, no server | PASS (already worked) | `test_hooks.py::test_leak_parser_hermes_tool_call` |
| Qwen3-Coder XML `<function=N><parameter=K>V</parameter></function>` | `hooks.leak_parser` (`qwen3_coder_xml`, pre-existing) | inline text, no server | PASS (already worked) | `test_repair.py::test_extract_leaked_call_qwen3_coder_xml` |
| XML param value that looks like JSON stays a plain string (opencode#6918's failure CLASS) | `_tag_params` raw-text capture + `validate_and_coerce` | inline text, no server | PASS (verified Halo's own extractor cannot reproduce this bug) | `test_repair.py::test_qwen3_coder_xml_param_value_is_always_a_plain_string` |
| Bare Python-dict-literal call, single quotes + `True`/`False`/`None`, no wrapper | `hooks.leak_parser` (`python_repr_args`, **new this round** -- was declared, unimplemented) | inline text, no server | PASS (was a silent no-op before this round) | `test_hooks.py::test_leak_parser_python_repr_args` |
| `}</tool_call>` tail, no opening tag (Qwen3-Coder #475) | `hooks.leak_parser` (`missing_tool_call_opener`, **new this round** -- was declared, unimplemented) | inline text, no server | PASS (was a silent no-op before this round) | `test_hooks.py::test_leak_parser_missing_tool_call_opener` |
| Bare leading `</think>`, no opening tag (Thinking-2507/QwQ) | `hooks.think_tag_strip`, **extended this round** | inline text, no server | PASS (old regex only matched a paired block) | `test_hooks.py::test_think_tag_strip_bare_unpaired_closer` |
| `arguments` as a JSON string, decoded once on ingest, `json.dumps` (never repr) on replay | `oai_stream._finalize` / `providers.translate._flatten_messages` (pre-existing, verified not re-derived) | `providers.translate` unit call, no server | PASS (confirmed already correct by direct read + test) | `test_qwen_wire_shapes.py::test_tool_use_input_replays_as_real_json_never_python_repr` |
| Tool schema with `prefixItems` | `request.simplify_schema_for_databricks`, **new this round** (was an unhandled keyword) | unit call against the simplifier, no server | PASS | `test_request_builder.py::test_schema_simplifier_rewrites_prefix_items_to_items_plus_description` |
| Tool schema over 16 top-level properties | `request.simplify_schema_for_databricks` (pre-existing truncation cap) | unit call against the simplifier, no server | PASS (already correct; see QWEN-RESEARCH.md discrepancy note below) | `test_request_builder.py::test_schema_simplifier_caps_properties_and_strips_ref` |
| `databricks-openjev-qwen35-4b` classified decision-only, routed to `judge`, never the session model | `providers.profiles.decision_only_info`/`.decision_only`/`.tools_supported`, `Controller.set_model`, `headless.build_session`, picker group -- all **new this round** | `mock_databricks.py` `openjev-not-chat-model`/`tools-rejected-400` scenarios + direct unit calls | PASS | `test_qwen_decision_only.py` (8 tests), `test_qwen_tools_rejected.py` (8 tests) |
| Decision-only endpoint answers plainly when no tools are offered | the same classification -- a tools-less request is untouched | `mock_databricks.py::openjev-not-chat-model` (tools-less branch) | PASS | `test_qwen_wire_shapes.py::test_openjev_scenario_answers_plainly_when_no_tools_are_offered` |
| A live 400 on an UNTABLED endpoint that rejects `tools` outright | `providers.errors.is_tools_rejected_message` + `providers.learned_rules.learn_tools_rejected`/`learned_tools_rejected`, **new this round** | `mock_databricks.py::tools-rejected-400` | PASS | `test_qwen_tools_rejected.py::test_live_400_learns_tools_rejected_and_ends_the_turn_clearly` + the fresh-session follow-up |

## A research-doc discrepancy, resolved in the code's favor

`docs/harness/QWEN-RESEARCH.md` §2/§4.1 states the 16-key cap "is not enforced" by
`request.py`. Reading the current tree found this already implemented
(`simplify_schema_for_databricks`'s `max_keys: int = 16` truncates `properties` and prunes
`required` to match, pinned by the pre-existing `test_schema_simplifier_caps_properties_and_strips_ref`)
-- the research was accurate against an earlier point in the tree, not the one this round
started from. Per `plans/WORKER-RULES.md` ("follow the brief/code, not stale research, and
say so"): only `prefixItems` (genuinely missing) was fixed this round; the cap's existing
truncation behavior was left as-is rather than reworked into "split or move rarely used
keys under one object" (the brief's own alternative phrasing) -- truncation already
prevents the wire 400 Databricks' restriction causes, two existing tests
(`test_request_builder.py`, `test_mcp_compat_matrix.py`) already pin that exact behavior,
and reworking it carries real regression risk for no live-confirmed defect driving it.

## What stays unconfirmed until a real `halo bugreport`

Every row above is a code-level fix for a DOCUMENTED Qwen/Databricks shape (Qwen's own
model cards, Databricks' own docs, cited GitHub issues) or a design decision from the
owner's work-VM report's own wording ("openjev qwen"). None of it is a live Databricks
Qwen call. Specifically still open, per `docs/harness/QWEN-RESEARCH.md` §4.4:

1. Whether `databricks-openjev-qwen35-4b` is really the endpoint the owner hit (the name
   match is strong but not confirmed).
2. The exact HTTP status/body Databricks actually returns for THIS endpoint on a
   tool-bearing request -- `openjev-not-chat-model`'s 400 wording is this round's best
   defensive guess (Databricks' own generic strict-allowlist shape), not a captured one;
   a 200 that silently ignores `tools` is the other documented-plausible shape and would
   need different handling (not a 400 to catch at all -- a reply to just not act on).
3. Whether the resolved route was actually `databricks` (full profile applied) or an
   unrecognized-host fallback treated as plain OpenAI.
4. Whether `tools` were present on the FIRST failing turn or only a later one.
5. Halo version/build and whether `--session-id` resumption was involved.

`docs/MODELS.md`'s new "Qwen at work" section tells a user how to capture this with a real
`halo bugreport` the next time it happens.
