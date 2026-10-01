# Agent investigation changes and evaluation

These changes address reproduced selection, presentation and request-contract
problems. They do not add a parser, graph edge type, answering model or MCP tool.
The baseline is `be1fd9f6296300d93f9d086f533bd95388856004`.

## Changes and compatibility

| Problem | Change | Implementation and regression coverage |
|---|---|---|
| Exact `Class.method` searches could put generic component matches and their graph neighbors before the named method. | Give exact qualified definitions priority within the existing trust and path boundaries. For a whole-query qualified lookup, seed graph expansion from its definitions. Retain alternative definitions across repositories. | `retrieval/retriever.py`, `retrieval/ranking.py`; `tests/capabilities/test_agent_efficiency.py` plus existing ranking tests. |
| A useful definition often arrived as a declaration requiring another call; a long chunk could hide the matching lines. | Optional `source_preview:true` supplies a contiguous window of complete indexed lines, selected for query terms within the granted character budget. The original span and omitted spans are explicit. Small definitions can arrive complete. | `models/search.py`, `capabilities/source_previews.py`, `capabilities/search.py`, `capabilities/service.py`; late-match, oversized-line, exact-source, omitted-line grounding and dirty-worktree regressions. |
| Unknown request fields were silently ignored, so a misspelled filter could broaden a query. | All MCP request models, including nested context, budget, target and verification models, reject extra fields. | Request models under `models/`; existing MCP `INVALID_REQUEST` response; schema and invalid-filter regressions. |
| A frontend caller with no local route handle could not be walked to possible providers. Repeating repository inventories was required. | Explicit `repoatlas_http_operations` workspace discovery with global scope, `discover:true`, and a non-root `route_prefix`. Paginate candidates, preserve canonical source identity and alternative providers, and retain repository-local statuses. | `models/http_inventory.py`, `capabilities/http_inventory.py`, `capabilities/service.py`; three-repository identity/pagination fixture and 10/50-repository scale test. |
| Loading the entire investigation contract consumed substantial context before retrieval. | A short core skill chooses focused tools; detailed contracts remain in an optional packaged reference. Explicitly explain direct-source fallbacks, `context_lines:0`, missing route handles and provenance versus entailment. | `skills/repoatlas-code-trace/`; existing skill-policy scenarios and wheel packaging checks. |
| Reported timing and token quantities were not comparable; streaming usage records could be counted repeatedly. | Offline session normalization with explicit record boundaries, run conditions, deduplicated usage categories, tool bytes and client intervals. Missing data remains unknown. | `tests/evaluation/transcripts.py`, `test_transcripts.py`. |

Paths in the implementation column are relative to `repoatlas/` unless prefixed
with `tests/` or `skills/`.

**Migration:** previously ignored extra input fields now return `INVALID_REQUEST`
with field locations. In particular, the dependencies filter is `name`, not
`package`. Remove or correct unknown fields; there is no automatic correction.
This intentional tightening may expose callers that relied on permissive input.
Settings files are unaffected. No index migration or reindex is needed.

Search preview is opt-in; existing search calls retain declaration behavior.
`preview` is additive response metadata (null in full responses when unused and
omitted in compact responses). Exact qualified-symbol ordering changes for both
presentations. Source preview uses the same database snapshot and ledger as
search, never current worktree text. If complete indexed lines cannot be proven
or a relevant full line cannot fit, existing shaping applies; a declaration is
still a declaration. A window is not proof about its omitted code. Grounding
accepts only the supplied lines.

HTTP inventory remains repository-scoped by default. Global scope alone retains
its previous refusal; callers must opt into discovery. `within_path` is not a
global filter. Each global row names its source `repository`; its `matched` or
`no_indexed_handler` status is computed within that repository. Compatibility
does not establish deployment identity, configured service URLs, ownership or a
runtime dependency. No cross-repository edges are created. Inspect configuration
before assigning a caller to any provider. Opaque cursors remain tied to the
request and indexed rows.

## Component measurements, 23 September 2026

These are local component observations, **not completed-agent benchmarks**.
The comparison used independent private SQLite copies and the same indexed LVL
source at `d7f7666ae5593816d1a57912b83a2305b30483f4`. No index sync occurred.
Five calls per arm used `RosterService.remove`, compact presentation, one item
and a 2,400-character per-item allowance. Baseline used search followed by
inspection of the returned exact method handle with zero context lines; the
branch used search with source preview.

| Measurement | Baseline | Branch |
|---|---:|---:|
| Calls to receive the complete method | 2 | 1 |
| Compact structured-result bytes, summed per trial | 4,699 | 2,321 |
| Median in-process dispatch duration, summed per trial | 41.21 ms | 44.33 ms |
| Core skill UTF-8 bytes | 37,066 | 8,391 |
| All nine tool definitions, compact JSON bytes | 49,837 | 51,652 |
| Skill plus tool-definition bytes | 86,903 | 60,043 |

The method text and source span were identical across arms: lines 66–76 of
`services/identity/src/memberships/services/roster.service.ts`. Result bytes fell
50.6%, core skill bytes 77.4%, and the combined fixed byte measurement 30.9%.
Schemas grew by 1,815 bytes. Dispatch was slightly slower; no server or agent
speedup follows from these samples. Bytes are not tokens, and actual hosts may
inject only some tool schemas or render results differently. Loading the entire
optional reference would forfeit much of the skill saving.

The controlled workspace discovery fixture keeps one frontend, two compatible
backend candidates and unrelated repositories. Each size used five warm queries,
with setup measured separately. The table gives the nearest-rank 95th percentile
of only five samples (the maximum), not a stable tail-latency estimate.

| Repositories | Fixture setup | Median query | Sample p95 | Operations examined | Rows returned | Full result bytes |
|---|---:|---:|---:|---:|---:|---:|
| 10 | 302 ms | 2.85 ms | 3.35 ms | 33 | 4 | 6,888 |
| 50 | 1,366 ms | 14.34 ms | 15.85 ms | 153 | 4 | 20,409 |

Rows remain focused and provider ambiguity is retained. Work still scales with
all registered repositories: discovery reuses each repository's inventory and
filters afterward. Full context/freshness metadata also grows with repository
count. This is a deliberate small first implementation, not a claim of constant
cost or a complete solution for very large workspaces. Before further indexing
changes, measure larger real inventories and the cost of metadata separately.

## Repeatable agent benchmark

Use three arms: direct-source tools, baseline RepoAtlas, and this branch. Give
all arms the same task, source revisions, model version, thinking effort,
permitted source tools, output requirements and completion boundaries. Pin
facts/extractor version, vector count, server build and skill/schema hashes.
Keep warm-index investigations separate from cold-start setup plus investigation.
Randomize arm order, start fresh sessions and repeat each task at least five
times. Do not train the skill against answers from the held-out tasks.

Use twelve tasks, two in each of these categories:

1. Broad behavior questions with an unknown entry point, including LVL removal
   and a non-LVL lifecycle or background-job workflow.
2. Exact symbol and exact path lookups, with duplicate symbol names and unrelated
   tests, across Python and TypeScript.
3. Failure-branch and late-literal investigations in long source spans, including
   an oversized line that cannot support a complete-line citation.
4. Frontend/backend attribution with two route-compatible providers, distinct
   configured origins and an unresolved caller.
5. Shared package/SDK consumer impact across repositories, including ambiguous
   providers and a repository name collision.
6. Negative findings with unsupported syntax, unproven mounts or stale indexed
   source. Require a useful uncertainty statement and a justified fallback.

Before any model run, freeze a source-reviewed answer rubric for each task:
required facts, correct repository attribution, conditional conclusions,
unsupported claims to reject, and permissible unresolved questions. Record
source revisions and line references separately from expected prose. Review
answers blind to arm; adjudicate disputed judgments against source or controlled
tests. A grounded citation alone earns no correctness credit. Record reviewer
time and the agent's subsequent correction time and tool use.

Keep separate per-task and per-arm results for:

- Correct required facts, missing facts and unsupported/fabricated claims.
- Correct uncertainty and negative-scope handling; cross-repository attribution
  errors; citation validity distinct from claim entailment.
- Prompt-to-final-answer time and prompt-to-accepted-answer time, including
  verification/correction. Use the same first and last events in all arms.
- Recorded uncached input, cache creation, cache reads and output tokens,
  deduplicated by message id. Report billing only with reliable billing data.
- Tool counts by type, UTF-8 response bytes, repeated evidence, irrelevant
  retrieval, direct-source fallbacks and the reasons for those fallbacks.
- Cold indexing/setup cost, warm retrieval cost, model/tool/client intervals
  when individually observable, and missing instrumentation.

Report individual runs plus median and range or interquartile range. Do not
sum overlapping tool intervals into wall time, count cached input as new context,
or infer inference time from uninstrumented gaps. Do not use one aggregate score.
The existing YAML evaluation suite tests capability contracts; its passing is
necessary but does not establish efficient agent reasoning or final accuracy.

### Recording and normalization

The offline normalizer currently accepts the Claude JSONL transcript shape from
the investigation. Other hosts need an explicit adapter, not inferred usage.
Supply a manifest such as:

```json
{
  "task": "held-out-api-consumer-impact",
  "arm": "branch",
  "repetition": 1,
  "repository_revisions": {"frontend": "<sha>", "backend": "<sha>"},
  "model": "<exact-model-version>",
  "thinking_effort": "<configured-effort-or-null>",
  "output_requirements": "Trace behavior, source citations, uncertainty and gaps",
  "index_state": "warm",
  "build_revision": "<server-build-sha>",
  "facts_version": "<reported-version>",
  "vector_count": 0,
  "skill_hash": "<sha256>",
  "schema_hash": "<sha256>"
}
```

Replace placeholders with observations; use JSON null for unknown effort.
Optional `setup_seconds`, `review_seconds`, `correction_seconds` and
`correctness` hold separately measured results. Select only the task's records,
excluding prior attempts or later requests to export a report:

```bash
python -m tests.evaluation.transcripts session.jsonl \
  --start 10 --stop 120 --manifest run.json --output metrics.json
```

Record boundaries are zero-based and stop-exclusive. Output records those
boundaries and the input log's SHA-256. The normalizer retains no
source or thinking text. Client request/result timestamps are not server
durations. Missing usage or results produce null totals, with observed usage
subtotals reported separately. Streaming usage updates are flagged. The final
assistant text timestamp is an observed boundary, not verified user delivery.
Repeated/irrelevant retrieval and answer quality still require rubric review.

### Acceptance and next steps

1. **Contract and grounding gate:** all existing default tests pass under isolated
   settings, branch coverage stays at least 80%, and static checks pass. Unknown
   fields must fail before retrieval. All new preview citations must match indexed
   bytes; omitted/unprovable lines must fail grounding. No changes to shared indexes.
2. **Selection and presentation gate:** duplicate qualified definitions remain
   alternatives; explicit paths and trust tiers keep precedence. Held-out short
   methods must arrive complete in one search when they fit. Late matching lines
   must be shown when a complete matching line fits. Compare payloads at equal
   useful evidence, including provenance and omissions.
3. **Workspace gate:** retain both compatible providers, keep their citations in
   the correct repository, and never label compatibility as a runtime connection.
   Pagination must return each row once. At 10/50 repositories only relevant rows
   may be returned; publish setup, query and full-response growth separately.
4. **Completed-work gate before claiming a product win:** no new severe false
   statements or scope-attribution errors; no decrease in required-fact coverage
   on any task without explicit review. Target at least 20% lower median newly
   supplied input tokens across the twelve tasks, and at least 10% lower median
   prompt-to-accepted-answer time, with no category's median time worsening more
   than 10%. Report all misses rather than changing the rubric. These are proposed
   acceptance targets, not achieved results.
5. **Only then select further work:** if global metadata dominates payload,
   propose a measured compact inventory response; if inventory scans dominate,
   narrow existing storage reads before adding facts. Alias resolution remains
   deferred until held-out import/navigation tasks justify its cost. Avoid a
   broad graph or parser expansion without evidence of missing facts.

Run tests with temporary defaults because several environment sentinels otherwise
see an operator's existing config:

```bash
test_state=$(mktemp -d)
REPOATLAS_CONFIG="$test_state/config.yaml" REPOATLAS_DB="$test_state/atlas.db" \
  XDG_CONFIG_HOME="$test_state/config" XDG_DATA_HOME="$test_state/data" \
  python -m pytest --cov --cov-report=term-missing
python -m pytest -m scale tests/adversarial/test_http_discovery_scale.py -s
```

The scale tests report measurements without machine-dependent timing assertions.
The default suite excludes `slow` and `scale`; selecting the new scale test does
not constitute running every optional benchmark or an actual agent experiment.
