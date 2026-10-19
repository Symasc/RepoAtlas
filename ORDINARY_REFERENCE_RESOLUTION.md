# Ordinary Reference Resolution Roadmap

## Goal

Give every ordinary reference an accountable outcome: an evidence-backed graph
link, or an explicit explanation of why no link can be established. Improve
resolution of supported static constructs until no such reference is left
unexplained. Do not manufacture links to achieve a zero unresolved count.

An external dependency, a runtime-selected target, or multiple valid candidates
can legitimately have no indexed target. These references are **accounted for**,
not successfully linked. Reports must distinguish the two.

This is a proposed implementation roadmap, not a statement that the capabilities
below already exist. All examples and fixture names are invented. This document
contains no private repository inventory, source extracts, domains, absolute
host paths, or machine-specific counts.

## Scope

Cover ordinary claims handled by `ReferenceLinker._resolve`, their per-file
counterparts, and parser gaps that prevent those claims from being emitted:

| Target kind | Relationship types |
| --- | --- |
| `symbol` | `CALLS`, `REFERENCES`, `EXPORTS` |
| `class` | `REFERENCES` |
| `module` | `IMPORTS` |
| `config` | `READS` |
| `package` | `DEPENDS_ON` |
| `file` | `REFERENCES` |
| `script` | `EXECUTES`, `SOURCES` |
| `salt_state` | `REQUIRES`, `WATCHES`, `INCLUDES`, `REFERENCES` |
| `template` | `REFERENCES`, `IMPORTS`, `INCLUDES` |

The resolver also accepts `function` targets; cover them alongside symbol calls
and references even when a baseline corpus has no examples of that kind.

Do not expand this work into the separate `USES`, HTTP, GraphQL, persistence,
or value-mapping planners. Reuse their suitable identity and tracing utilities
where useful, but preserve their relationship semantics and accounting.

## Existing Behavior

The current flow is parser facts -> per-file links and standing claims -> global
resolution -> atomic replacement of global edges.

- [The linker](repoatlas/graph/linker.py) resolves what a file proves locally,
  records standing claims, and retries global resolution against indexed facts.
- [ResolutionIndex](repoatlas/graph/resolution.py) supplies ordinary module,
  file, symbol, Salt state, and template lookups. Ordinary imported-symbol
  lookup follows the recorded module and looks up the target name there; it
  does not use the richer declaration-use export trace.
- [ModuleResolver](repoatlas/graph/modules.py) supports configuration-backed
  aliases and workspace package entries for declaration uses. Its documented
  separation from ordinary imports, calls, and references is intentional.
- [PythonModules](repoatlas/graph/py_modules.py) and the existing
  [Node package links](repoatlas/graph/packages.py) and
  [Python package links](repoatlas/graph/py_packages.py) are reuse candidates.
  They are not assumed to be drop-in ordinary resolvers.
- Generic ordinary resolution returns an edge or `None`. It does not expose
  a complete per-claim failure taxonomy.

The `unresolved_refs` table holds durable **standing claims**, including claims
that a later global pass successfully links. Counting its rows is not a count of
references still unresolved. It also contains configuration facts and claims
owned by specialized planners that must not be mistaken for ordinary failures.

Existing tests deliberately constrain behavior. Consult
[module-resolution tests](tests/test_module_resolution.py),
[linking tests](tests/test_linking.py), and
[adversarial graph-truth tests](tests/adversarial/test_graph_truth.py) before
extending it. A change to those constraints needs explicit positive and negative
tests, not simply an updated expectation that more edges exist.

## Evidence Rules

1. Identify targets through language binding, module configuration, explicit
   paths, or declared dependency/provider evidence. Never use name similarity.
2. Require one justified target under the applicable language rules. Multiple
   candidates do not authorize choosing the first result from an index scan.
3. Respect shadowing, project boundaries, package export rules, and import form.
   A repository-wide name match is not proof of a call target.
4. Resolve from admitted indexed facts. Do not fetch packages, execute source,
   install dependencies, read excluded secrets, or inspect arbitrary files.
5. Preserve provenance, confidence, and the distinction between exact proof and
   existing qualified inference. In particular, inferred `salt://` roots must
   not silently become proven roots.
6. Bound traversals and candidate diagnostics. Report cycles or exhausted bounds
   instead of publishing an incomplete search as a definitive missing target.
7. Keep diagnostics separate from resolution decisions: wording must not decide
   whether a graph edge exists.

## Proposed Outcome Contract

The following codes are proposed, not current public API values. Final names
should follow established repository conventions and allow family subreasons.

| Outcome | Meaning | Graph behavior |
| --- | --- | --- |
| `linked` | A target is established, with its evidence basis and confidence. | Emit the justified relationship. |
| `external` | Evidence establishes a target outside the managed index, such as a declared third-party package. | No invented local edge. |
| `not-indexed` | A known target is outside admitted indexed coverage. | State the coverage boundary. |
| `target-not-found` | A complete supported lookup found no target in its searched scope. | No edge; report scope and lookup basis. |
| `dynamic` | A target depends on a runtime value that static facts cannot establish. | No guessed edge. |
| `ambiguous` | Applicable evidence leaves multiple valid targets. | No arbitrary choice. |
| `unsupported` | Syntax or resolution semantics are not modeled. | Identify the missing capability. |
| `evidence-unreadable` | Required evidence could not be read or parsed. | Do not claim absence. |
| `trace-bound` | A cycle or traversal budget prevents completing the proof. | No partial proof promoted to a link. |

Absence of a package provider alone does not prove that the dependency is external.
Use `target-not-found`, `not-indexed`, or another supported outcome according to
what the evidence actually establishes. Preserve incomplete-search information.

Each decision should identify the claim, its source location, relationship and
target kind, raw target spelling, outcome, subreason, searched scope, evidence
basis, and relevant resolution version. A linked decision identifies the target;
a refusal may include bounded candidates and the missing proof. Exported reports
must apply the existing privacy and source-admission rules.

One claim receives one terminal decision per resolution snapshot. A relationship
may involve multiple supporting facts or edges; those do not become extra claims.
Interned local links, standing ordinary claims, informational facts, and specialized
planner claims need separate accounting. For emitted ordinary claims:

```text
ordinary claims considered = linked claims + explicitly classified non-link claims
unexplained ordinary claims = 0
```

Do not add specialized planner decisions to this equation. Track parser omissions
separately: source constructs for which no claim was emitted cannot be repaired
or counted by a resolver-only change.

## Gap Matrix

The gaps below are work to characterize and implement, not assertions that every
unlinked reference in a live index has the same cause.

| Family | Current behavior or boundary | Work needed | Negative acceptance case |
| --- | --- | --- | --- |
| `CALLS` -> symbol/function | Same-file links and imported-name lookup; no full binding/receiver proof. | Preserve import aliases; trace exported names; model supported lexical bindings and provable receivers. | A shadowed import or unknown receiver must not link to a same-named method. |
| `IMPORTS` -> module | Ordinary lookup is simpler than declaration-use module lookup. | Configuration-backed aliases, project layouts, workspace/package entries, and explicit provider identity. | Duplicate providers, invalid exports, or conflicting entry conditions must not choose a target. |
| `REFERENCES` -> symbol | Includes decorator references; depends on emitted binding metadata. | Resolve aliased and qualified decorators through their actual imported/exported binding. | Runtime decorator factories or shadowed names remain explicit non-links when unproven. |
| `REFERENCES` -> class | Includes base-class references using imported-name lookup. | Trace aliases and re-exports of bases; preserve namespace/member identity. | A computed base or unrelated class with the same name must not link. |
| `EXPORTS` -> symbol | Ordinary symbol resolution is not a full language export-table traversal. | Distinguish local exports, renamed exports, defaults, explicit re-exports, and star ambiguity. | Two star-export paths naming different declarations must not collapse into one. |
| `READS` -> config | Pillar reads have specialized resolution; the generic fallback searches the source and explicit includes for one exact key. | Expose missing scope/key/uniqueness reasons; extend only from readable configuration evidence. | Never infer deployment-only pillar values or read excluded configuration to fill a gap. |
| `DEPENDS_ON` -> package | Generic dispatch has no package branch; dependency-to-provider planning is separate. | Reconcile parser claims with dependency records and provider decisions; account for claims without duplicate edges. | No provider is not automatically a missing declaration; multiple providers are not a unique link. |
| `REFERENCES` -> file | Static paths are resolved against indexed files, including explicit paths crossing managed roots. | Explain path bases, missing/generated/excluded targets, containment, and unsupported forms. | Refuse runtime substitutions and targets not admitted to the index. |
| `EXECUTES` / `SOURCES` -> script | Uses static path lookup, not shell runtime lookup. | Characterize source/execute path semantics, working-directory evidence, and supported literal forms. | Unknown working directory, dynamic commands, or `PATH` search must not become guessed file links. |
| `INCLUDES` -> salt_state | Resolves include paths using indexed Salt files. | Explain missing/ambiguous includes and configuration boundaries. | An unproven environment/root must not select an unrelated state file. |
| `REQUIRES` / `WATCHES` / `REFERENCES` -> salt_state | State lookup requires a unique indexed state in its current scope. | Characterize requisite identity and support narrower source-backed scope where needed. | Duplicate state identifiers must not link arbitrarily. |
| `REFERENCES` / `IMPORTS` / `INCLUDES` -> template | Static file paths and qualified `salt://` lookup. | Preserve each template language's loader/path rules; distinguish inferred Salt roots from exact paths. | A computed template name or ambiguous suffix must not produce a proven link. |

Audit claim emission alongside these families. Unsupported languages, skipped
constructs, missing import metadata, and alias loss are different defects from
an emitted claim resolving to no target. In particular, reproduce Python renamed
import behavior before calling alias claim loss a confirmed bug.

## Synthetic Examples

These snippets describe required test scenarios. Their expected links assume the
stated companion declarations and configuration are admitted to a synthetic index.

### Python Aliases, Decorators, and Bases

```python
from toy_app.codec import parse_record as decode
from toy_app.audit import audit_action as tracked
from toy_app.workers import BaseWorker as Parent

@tracked
class RecordWorker(Parent):
    def run(self, payload):
        return decode(payload)
```

Expected: the call, decorator reference, and base reference reach their respective
declarations by imported identity. Fixture files define those declarations.
Add a companion test rebinding `decode` locally: it must no longer link to the
imported function unless a separate binding proof justifies that target.

### Aliases and Re-exports

```typescript
import { parseRecord as decode } from "@/codec";
decode(payload);
```

In this fixture, configuration maps `@/*` to `src/*`; the codec entry re-exports
`parseRecord` from `./parsers/record`. Expected: the import identifies the entry
module, while the call identifies the actual function declaration. Keep the two
relationship meanings distinct. Without readable alias configuration, explain
the unsupported or unestablished module identity instead of guessing `src/codec`.

Also test `import decode from "./codec"` and
`import * as codec from "./codec"; codec.parseRecord(payload)` against real default
and named exports in the synthetic files. A default expression without an
identifiable declaration must not be forced into a symbol edge.

### Packages and Generated Entries

```typescript
import { parseRecord } from "toycodec";
parseRecord(payload);
```

Use a fixture dependency declaration and one matching managed provider whose
entry is provable. Test two matching providers as an ambiguity. Test an entry
`./dist/index.js` with no indexed output: map to source only when the provider's
own build facts justify that mapping. A missing generated artifact alone does
not authorize rewriting `dist` to `src`.

### Runtime Targets

```python
handler = handlers[request_kind]
handler(payload)
```

Unless indexed facts establish one binding, expected outcome: `dynamic` or a
more precise unsupported-binding subreason. A function named `handler` elsewhere
is not evidence. Apply the same rule to computed imports and template names.

### Paths, Scripts, and Templates

```sh
./scripts/check-records.sh
. ./scripts/common.sh
"$TOOL_SCRIPT"
```

For literal commands, require an established path base and indexed target.
The variable-selected command must remain unlinked without a static value proof.
Test `../../shared/check-records.sh` with an explicit managed target and valid
containment; never replace it with a basename search across repositories.

Test literal `views/record.html` and `salt://templates/record.conf` separately.
Loader-root evidence decides the former; the latter must retain any qualified
inference already used by Salt resolution.

### Configuration and State Identity

```jinja
{{ pillar['worker']['timeout'] }}
```

An explicitly included synthetic state file defines the exact key path.
Expected: link only when the applicable scope establishes one definition. Missing
keys and multiple definitions receive distinct outcomes. Add synthetic includes,
`require`, and `watch` claims with unique and duplicate state identifiers.

## Implementation Sequence

### A. Diagnostics and Accounting

- Define the decision model and family-specific reasons before extending links.
- Cover every ordinary dispatch branch, including unsupported kinds and missing
  origin evidence, without treating module facts as failed references.
- Report linked, classified non-link, and unexplained claims separately. Retain
  compatibility of existing summary fields unless a reviewed change requires it.
- Decide how ordinary decisions are exposed through established reporting/store
  patterns. Avoid a parallel diagnostic database or an unrelated config mechanism.

Exit gate: a synthetic baseline reconciles all emitted ordinary claims and records
why each non-link remains open. Existing graph output stays unchanged.

### B. Module and Binding Identity

- Characterize ordinary language lookup against the existing module/package
  abstractions; share suitable mechanisms without changing edge semantics.
- Add import alias, relative import, configured alias, package-entry, project-root,
  and namespace cases under documented language rules.
- Tie cross-repository targets to explicit dependency/provider evidence. Never
  convert workspace-wide name matching into import resolution.
- Version changed resolution behavior and parser facts where applicable.

Exit gate: module targets and imported bindings agree with the supported language
rules, including duplicate providers, unreadable facts, and boundary refusals.

### C. Calls and Symbol References

- Build on B to trace names through renamed, default, namespace, and re-export
  bindings for calls, decorators, base classes, and exports.
- Add supported local binding and receiver proofs conservatively. Treat instance
  methods, inheritance, closures, and runtime dispatch as separate characterized
  capabilities, not a global name fallback.
- Preserve bounded traces, cycle detection, shadowing, and source provenance.

Exit gate: every supported synthetic call/reference points to the correct
declaration; adversarial lookalikes remain explicit non-links.

### D. Remaining Families

- Reconcile package claims with dependency planning and its provider refusals.
- Characterize path bases and containment for files and scripts.
- Add template-loader, state-identity, and configuration-scope diagnostics before
  extending these families with additional source-backed facts.
- Keep inferred links visibly qualified; test missing, excluded, generated,
  ambiguous, and runtime-only targets separately.

This family work can proceed independently after A's outcome contract is stable.
It must reuse B where module identity is part of the required proof.

Exit gate: every matrix row has a positive case when supported and negative cases
for each applicable refusal, with no duplicate dependency accounting.

### E. Coverage and Integration

- Audit parser emission with a source-to-claim synthetic corpus. Extend supported
  syntax deliberately and label unmodeled constructs as coverage limits.
- Check per-file and global linking together, not just remaining global claims.
- Update architecture and user-facing reporting when behavior changes.
- Reconcile classifications and links after repeated, incremental, and full runs.

Exit gate: zero unexplained emitted ordinary claims; no silent claim loss for the
documented supported syntax; no guessed links for unsupported/runtime behavior.

## Verification Gates

Use existing parser, linking, module-resolution, dependency, and adversarial test
homes where suitable. Add fixtures only when existing neutral helpers cannot
express the required scenario.

| Gate | Required check |
| --- | --- |
| Binding correctness | Aliases, shadowing, decorators, bases, namespaces, defaults, and re-exports reach only their actual declarations. |
| Conservative refusal | Duplicate providers, conflicting entries, computed targets, unknown receivers, unreadable evidence, cycles, and trace bounds produce explicit outcomes. |
| Coverage | Supported source constructs emit the intended claims; unsupported constructs are documented separately from resolver failures. |
| Accounting | Every considered ordinary claim has one terminal decision; candidates and edges do not inflate claim counts. |
| Stability | Repeated global resolution is deterministic; a full run and equivalent incremental changes produce equivalent links and decisions. |
| Invalidation | Changed import configuration, package declarations, exports, parsers, and resolution versions invalidate dependent results. |
| Atomicity | A failed planning or commit step cannot publish a partial replacement graph or mismatched decisions. |
| Security | No fetching, source execution, secret reads, admission bypass, or unproven filesystem traversal. |
| Privacy | The complete patch, fixtures, documentation, and generated artifacts contain only neutral synthetic examples. |

Run focused tests after each behavior change; finish with the repository's required
lint, type, and integration gates. Do not replace negative tests simply because
the goal is more links. Prove the new behavior while preserving refusal guarantees.

## Completion and Privacy Checklist

- [ ] Every ordinary family in the scope table is covered.
- [ ] Each supported static scenario produces an evidence-backed link.
- [ ] Each legitimate non-link has a specific, auditable outcome.
- [ ] Linked and accounted-for non-linked counts remain distinct.
- [ ] Parser omissions and excluded specialized facts are separately reported.
- [ ] Incremental/full equivalence, invalidation, bounds, and atomicity gates pass.
- [ ] Examples use invented names; no private source or inventories are copied.
- [ ] The entire patch and generated artifacts pass the privacy review.

These boxes describe completion of the future resolver work, not completion of
this document. Live acceptance reports and machine-specific inventories belong
outside the repository. The public regression corpus must stand on its own.