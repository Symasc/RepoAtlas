*Read-only architecture study · not a release audit*

> **V2 note.** This is a snapshot of V1 at commit `c402463`, written to let a second architect draw the V2
> boundary. It is kept unedited as the record of what was there. Since it was written, the embedded reasoning
> subsystem it maps --- `repoatlas/llm/`, `services.reason`, `ask`, `debug`, `--no-llm`, `LLMSettings`,
> `REPOATLAS_LLM_BASE_URL` and exit code 4 --- has been removed. Its "Local-model only" classifications turned out
> to be right, and everything it marked reusable was reused. The embedding model has since gone too: FastEmbed,
> the `embeddings` table, the semantic stage and `EmbeddingSettings` no longer exist, and the schema history it
> describes was squashed into a single migration before the first release.

# RepoAtlas V1 Architecture Dossier

A forensic map of what V1 actually does, where its boundaries fall, and which parts are reusable — written so a second architect can draw the V2 boundary without guessing. Implementation and tests are treated as the source of truth; documentation is treated as a claim to be checked.

**Web version:** https://claude.ai/code/artifact/07284db3-ab74-4c7c-afc7-820a23a22d25

- **Branch** — v1-the-base
- **Commit** — c402463
- **Working tree** — clean
- **Source** — 94 modules / 20,790 lines
- **Tests** — 103 modules / 39,372 lines
- **Schema** — version 5

## Contents

1. Candidate studied
2. Executive architecture summary
3. Package / component map
4. CLI and application call chains
5. Repository / context model
6. Sync / indexing lifecycle
7. File admission and trust boundaries
8. Parser architecture
9. Domain fact and graph model
10. Linker and dependency model
11. SQLite / storage model
12. Retrieval architecture
13. Reasoning / Ollama architecture
14. Grounding and citation enforcement
15. Error, config and concurrency
16. Existing reusable capabilities
17. Stable and unstable identifiers
18. Evidence granularity today
19. Graph-query primitives
20. Machine-readable surfaces
21. Coupling matrix
22. Sequence diagrams
23. Tests as contracts
24. Assumptions V2 will break
25. Reusable trust mechanisms
26. Dangerous abstractions
27. Extraction seams
28. Leave untouched
29. Compatibility obligations
30. Top V2 constraints
31. Open design questions
32. File / function index

## 01 · Candidate studied

Everything below was read at one commit with a clean working tree. Nothing was modified, committed or pushed.

```
git branch --show-current   v1-the-base
git rev-parse HEAD          c40246374b6b2d2c0439a061c4b5361befddaacd
git status --short          (empty)
```

The fifteen most recent commits run from `b835843` "Slice A — the deterministic spine" through four hardening fixes at the tip: atomic per-file index state, Git enumeration refusal, Dockerfile `COPY --from` namespacing, concurrent first open, and a Python relative-import fix.

| Component | Version | Where recorded |
| --- | --- | --- |
| Python | 3.13.15 | `requires-python = ">=3.13"` |
| uv | 0.12.9 | host toolchain |
| SQLite | 3.53.1 | stdlib `sqlite3` |
| Git | 2.43.0 | subprocess only |
| fastembed | 0.8.0 | declared `>=0.5` |
| onnxruntime | 1.29.0 | transitive; no PyTorch |
| httpx | 0.28.1 | only the Ollama backend uses it |
| Typer / Pydantic / Rich | 0.27.2 / 2.13.5 / 15.0.0 | CLI, models, traceback rendering |
| tree-sitter | 0.26.0 + language pack 1.16.1 | TS/JS, Bash, Ruby |

Ollama is not a Python dependency. The backend speaks HTTP to `llm.base_url`, default `http://localhost:11434`, with model `gpt-oss:20b`. **[VERIFIED]** `repoatlas/config/settings.py:179-182`

## 02 · Executive architecture summary

RepoAtlas V1 is a deterministic indexer with a thin reasoning cap bolted on the end. The proportions matter for V2: of 94 production modules, **eight** live under `llm/`, and the layering test forbids that package from importing storage, sync, indexing, discovery, cli or services at all.

The system has one hard architectural spine and one soft edge.

### The spine, which is deterministic end to end

Working tree → `git ls-files` → containment boundary → hash → parse (pure) → per-file transaction → link → chunk → FTS → embed. Every fact carries repository, file, line range, parser and content hash. Nothing in this path imports the LLM layer, and nothing in it can be reached by the LLM layer.

### The edge, which is one step

`Reasoner.reason()` takes an already-built `EvidenceSet`, renders three markdown prompts, calls one backend method, validates the JSON against a Pydantic schema, allows exactly one repair attempt, and then runs `ground_answer()` to delete every claim the supplied evidence does not support. It touches no storage and holds no state.

> **The single most important structural fact for V2.** The trust rule is not enforced at the model boundary by prompting. It is enforced twice below it: retrieval assigns every candidate an `EvidenceTier` that fusion sorts on before any score, and grounding re-checks every citation against the exact set that was supplied. Both survive the removal of the local model entirely, because neither imports it.

What V1 does *not* have is any machine-facing surface between the CLI and the services. `repoatlas.services` exports five functions, and the CLI is their only caller. There is no server, no session, no request object, and no capability registry. **[NOT PRESENT IN V1]**

## 03 · Package / component map

Dependency direction is not a convention here; it is asserted by `tests/test_layering.py`, which parses every module's imports against a forbidden-package map and fails the build on a violation. The arrows below are the enforced ones.

```
                         cli/main.py  (Typer; 7 commands, no logic)
                              │
                              ▼
                    services/  (5 functions + Workspace)
        init_service   sync_service   status_service   query_service
                              │
        ┌─────────────┬───────┴────────┬──────────────┬─────────────┐
        ▼             ▼                ▼              ▼             ▼
   discovery/     context/          sync/         retrieval/      llm/
   finder         resolver          engine        retriever       reasoner
   ignore                           planner       ranking         grounding
   reconcile                        scanner       query           ollama
   languages                        lock                          prompts/
        │             │                │              │             │
        └─────────────┴────────┬───────┴──────────────┘             │
                               ▼                                    │
                   parsers/  graph/  dependencies/  indexing/        │
                   registry  linker  registry       chunking         │
                             keys                                    │
                             resolution                              │
                               │                                     │
                               ▼                                     │
                          storage/protocols.py                       │
                          storage/sqlite/  (only SQL in the tree)    │
                               │                                     │
                               ▼                                     ▼
                          models/  (Pydantic; imports nothing internal)
                               ▲
                          errors.py  security.py  containment.py  gitstate/

   rendering/text.py  ──consumes──▶ models/   (never queries; 7 render functions)
```

#### Enforced prohibitions

- `parsers/` may not import storage, llm, sync, retrieval, services, cli, indexing or graph. It is a pure function of `(path, content)`.
- `dependencies/` carries the same ban plus gitstate, discovery and context.
- `llm/` may not import storage, sync, indexing, discovery, cli or services.
- `rendering/` may not import storage, sync, indexing, retrieval, cli, services or llm.
- `models/` may import nothing internal at all.
- SQL text appears only under `storage/sqlite/`; `subprocess` only under `gitstate/`; no module defines a `RepoAtlasAgent`. **[VERIFIED]** `tests/test_layering.py:98-160`

| Package | Responsibility | Determ. | I/O | Owns state | Knows LLM |
| --- | --- | --- | --- | --- | --- |
| `cli/` | Typer app; resolve, call one service, render, exit | yes | stdout/stderr | no | only `QueryIntent`, `ReasoningResult` |
| `services/` | Workspace wiring; the five application entry points | yes | via stores | holds the connection | `query_service` only |
| `context/` | `RepositoryContextResolver`: cwd + selector → scope | yes | reads cwd, calls `git_toplevel` | no | no |
| `discovery/` | repository finding, ignore language, reconciliation, language detection | yes | filesystem walk | no | no |
| `gitstate/` | the only subprocess caller; hardened `git` wrapper | yes | subprocess | no | no |
| `sync/` | scanner, planner, engine, writer lock | yes | filesystem + SQLite | writes everything | no |
| `parsers/` | 8 language parsers behind one protocol | yes | **none** | no | no |
| `dependencies/` | 12 manifest/lock parsers, 3 ecosystems | yes | **none** | no | no |
| `graph/` | keys, resolution index, two-phase linker | yes | via stores | writes edges | no |
| `indexing/` | chunking only | yes | none | no | no |
| `storage/` | 9 protocols + SQLite implementation + 5 migrations | yes | SQLite | **owns the database** | no |
| `retrieval/` | query analysis, 6 stages, fusion, budget | yes | reads only | no | no |
| `embeddings/` | protocol + fastembed + hashing backends | hashing yes | model files | no | no |
| `llm/` | protocol, Ollama, null, scripted, prompts, reasoner, grounding | grounding yes | HTTP | no | is the LLM |
| `rendering/` | 7 text renderers | yes | none | no | formats answers |
| `models/` | frozen Pydantic domain models | yes | none | no | answer schema lives here |

## 04 · CLI and application call chains

All seven commands share one shape: `_open()` → `_resolve()` → one service → one renderer → `workspace.close()`. The CLI contains no indexing, retrieval or reasoning logic. `repoatlas/cli/main.py`

| Command | Context | Service | Retrieval | LLM | Renderer | Exit |
| --- | --- | --- | --- | --- | --- | --- |
| `init <dir>` | none — takes a path | `services.initialize` | no | no | `render_sync` | 0, or 1 if any repo unavailable |
| `sync [repo]` | `_resolve(...,"sync")` | `services.sync` | no | no | `render_sync` | 0, or 1 if any repo unavailable |
| `status [repo]` | `_resolve(...,"status")` | `services.status` | no | no | `render_status` | always 0 when the index opens |
| `repos` | **none** — always global | `services.list_repositories` | no | no | `render_repos` | 0 |
| `locate <q>` | `_resolve(...,"locate")` | `services.locate` | yes, default limits | no | `render_evidence` | 0 |
| `ask <q>` | `_resolve(...,"ask")` | `services.reason` | yes, default limits | yes | `render_answer` | 0, or 4 |
| `debug <q>` | `_resolve(...,"debug")` | `services.reason` | yes, `_DEBUG_LIMITS` | yes | `render_diagnosis` | 0, or 4 |

#### Compact chains, real names

```
locate
  open_workspace()                       # load_settings, open_database, build 8 stores
  → RepositoryContextResolver.resolve(cwd, --repo, command="locate")
  → services.locate(workspace, question, context)
  → Workspace.retriever(RetrievalLimits.from_settings)
  → Retriever.retrieve(question, context)
  → EvidenceSet
  → render_evidence(evidence)            # or model_dump_json with --json

ask
  … identical to locate up to context …
  → services.reason(workspace, question, context, QueryIntent.ASK)
      → build_model(settings.llm)            # llm/factory.py
      → asyncio.run(backend.available())     # GET /api/tags, before any work
      → retrieve_for(...)  → Retriever.retrieve(...)   # same retriever as locate
      → asyncio.run(Reasoner(backend).reason(question, evidence, intent))
          → build_messages()  → format_evidence()  → prompts/system.md + ask.md
          → backend.complete(messages, response_schema=RepoAtlasAnswer)
          → RepoAtlasAnswer.model_validate_json  (one repair attempt on failure)
          → ground_answer(answer, evidence)
      → (EvidenceSet, ReasoningResult)
  → render_answer(result.answer, evidence)

ask --no-llm
  → services.retrieve_for(...)   # branch taken *before* any model is built
  → render_evidence(evidence)    # byte-identical path to locate, wider limits for debug
```

> **Reusability read.** `services.locate` is one line: `workspace.retriever().retrieve(question, context)`. Everything a machine-facing caller would want from `locate` already exists as a typed Pydantic `EvidenceSet`; the renderer is a pure display function that takes that model and returns a string. The separation of capability from presentation is already complete for this command. It is *not* complete for dependencies (§27).

## 05 · Repository / context model

Three distinct notions are deliberately kept apart, and conflating them was the defect schema v5 exists to fix. `repoatlas/models/repository.py:25-95`

| Notion | Field | Unique | Used for |
| --- | --- | --- | --- |
| Identity | `slug` — canonical path relative to the managed root | yes, by construction | entity keys, fusion, dedup, grounding, cross-repo joins |
| Location | `path` — absolute path on this host | yes | enumeration, containment, relocation detection |
| Display name | `name` — directory basename | **no** | terminal output only |

A row id, a UUID and a remote URL were each considered and rejected in the code comments: allocation order breaks reindex equivalence, a UUID is unreadable in a citation, and a remote may be absent or shared between clones.

#### Resolution precedence — owned entirely by `RepositoryContextResolver.resolve()`

```
1. explicit selector (--repo or positional)
     match repo.name OR repo.slug across all repositories
       exactly one  → REPOSITORY scope
       more than one → ContextResolutionError (exit 2), candidates listed
       none          → fall back to exact canonical path match
       still none    → "Unknown repository" (exit 2)
2. walk cwd and every parent, canonicalized
     first path present in the managed set wins   → REPOSITORY scope
     (nearest wins, so nested repositories need no special case)
3. cwd == the configured root, exactly           → GLOBAL scope
4. cwd inside some other Git repository          → "not managed by RepoAtlas" (exit 2)
5. otherwise                                     → "Unable to determine repository" (exit 2)
```

There are exactly two valid *implicit* contexts: an enclosing managed repository, or the exact root. A directory under the root but outside any repository is refused rather than guessed at.

#### Who resolves

**Resolution is always performed by the caller, never by retrieval.** `Retriever.retrieve()` takes an already-built `RepositoryContext` and reads three things from it: `repository_ids` for scoping, `primary.slug` for the fusion boost, and `scope` to decide whether to run one unscoped pass or a primary-then-global pair. It never touches the resolver, the filesystem or the cwd. **[VERIFIED]** `repoatlas/retrieval/retriever.py:151-160, 736-745`

The same holds for `status` and `sync`: both receive a context and neither re-derives scope. `repos` takes no context at all and is unconditionally global.

#### Relocation, dirty state and HEAD

- **Relocation** is detected by `plan_repository_set()` and applied by `repositories.relocate()` inside one transaction, before registration, in both `init` and `sync`. A candidate whose slug is held by a still-existing registered repository is a *conflict*: `init` refuses outright, `sync` silently skips it and `status` reports it.
- **Missing directory** means the repository is forgotten, not kept: `forget_if_missing()` deletes the row and cascades away every fact. A move is a removal plus a discovery.
- **Branch, HEAD and dirty** come from `GitClient.state()` per sync and are stored as `branch`, `head_sha`, `is_dirty` plus the indexed-at-the-time copies `last_indexed_head` and `last_indexed_dirty`. The comparison between live and stored is what `status` reports as staleness.

## 06 · Sync / indexing lifecycle

One changed Python file, traced end to end with real names and real transaction boundaries.

```
services.sync(workspace, context, full=False)
│
├─ workspace.writer_lock()          flock on atlas.db.lock, 30s bound   [LOCK]
│
├─ if the run covers the managed root:
│    plan_repository_set(root, discovery_settings, stored)
│    ┌ TRANSACTION ─────────────────────────────────────────────────┐
│    │ repositories.relocate(...)   for each relocation             │
│    │ register(plan.added, ...)    only the new ones               │
│    └──────────────────────────────────────────────────────────────┘
│    repositories.list_all()        re-read, never spliced
│
└─ SyncEngine.run(repositories, full, scope, embed_repo_ids)
   │
   ├─ FOR EACH repository:
   │    forget_if_missing(repo)           → delete + cascade, skip
   │    ensure_repository_entity(repo)    → intern repo:<slug>
   │    sync_repository(repo, full)
   │    │
   │    ├─ ensure_repository_identity()   assign slug if pre-v5
   │    ├─ git.state(repo_path)           branch / head_sha / is_dirty
   │    ├─ decide_rehash(repo, state, force)
   │    │     forced when: --full · first index · previous sync not OK
   │    │                · HEAD moved · dirty-state changed · git unknown
   │    ├─ RepositoryBoundary(repo_path)  ONE boundary for the whole pass
   │    ├─ scan_repository(...)           ← §7; raises EnumerationUnavailable
   │    ├─ files.list_states(repo_id)     rel_path → (file_id, hash, size,
   │    │                                             mtime_ns, parser_version)
   │    ├─ repositories.mark_sync_started(repo_id)      [TX] → 'interrupted'
   │    ├─ plan_sync(scanned, known, rehash, facts_version, hash_of)
   │    │     parser_version differs      → changed   (no hash needed)
   │    │     size+mtime match, no rehash → unchanged (shortcut)
   │    │     else hash_within(boundary)  → compare; None counts as changed
   │    ├─ files.delete_many(deleted_ids) [TX] deletion before anything else
   │    │
   │    ├─ FOR EACH new|changed file:  _index_file()
   │    │   ┌ PURE — no database, no lock held ────────────────────┐
   │    │   │ boundary.read_content(rel_path, max_bytes=cap)       │
   │    │   │ oversized → stream digest only, record, stop         │
   │    │   │ hash_bytes(raw); is_binary = NUL in first 8 KiB      │
   │    │   │ registry.for_path() → parser or None                 │
   │    │   │ parser.parse(path, content)      → ParsedFile        │
   │    │   │ dependency_registry.for_path() → DependencyReport(s) │
   │    │   │ chunk_content(content, symbols, settings)            │
   │    │   └──────────────────────────────────────────────────────┘
   │    │   ┌ TRANSACTION — the completeness marker ───────────────┐
   │    │   │ files.upsert(repo_id, record)        → file_id       │
   │    │   │ knowledge.replace_file_facts(file_id, FileFacts)     │
   │    │   │     deletes entities, edges, unresolved_refs,        │
   │    │   │     chunks, FTS rows, dependency rows for this file  │
   │    │   │     then inserts the file entity + every symbol      │
   │    │   │ linker.link_file(...)   → DEFINES + interned edges   │
   │    │   │                           + unresolved_refs claims   │
   │    │   │ dependencies.replace_for_file(file_id, records)      │
   │    │   └──────────────────────────────────────────────────────┘
   │    │
   │    ├─ knowledge.gc_orphan_entities()
   │    └─ repositories.update_detected_metadata(languages, manifests, lockfiles)
   │       → returns (RepositorySyncReport, SyncCompletion)   ← completion HELD BACK
   │
   ├─ linker.prepare_global()          reads everything, WRITES NOTHING
   │    list_file_rows() · list_symbols() · ResolutionIndex.build()
   │    list_unresolved_refs() → resolve each → planned EdgeRecords
   │    _plan_dependencies(index) → cross-repository DEPENDS_ON plan
   │
   ├─ ┌ TRANSACTION — the run's completeness boundary ──────────────┐
   │  │ linker.commit_global(plan)                                  │
   │  │   delete_edges_by_parser("linker:global")                   │
   │  │   _materialize(plan.dependencies)  intern endpoints         │
   │  │   add_edges(all)                                            │
   │  │   gc_orphan_entities()                                      │
   │  │ repositories.mark_many_synced(completions)  → 'ok'          │
   │  └─────────────────────────────────────────────────────────────┘
   │
   └─ embed_pending(embed_repo_ids)    OUTSIDE the transaction; may fail benignly
        vectors.missing_hashes(model_id, repo_ids)
        vectors.gc_orphan_vectors()
        embeddings.available()? → batch embed → vectors.upsert_many()
```

#### Pure computation versus persistent mutation

| Pure — no database | Persistent mutation |
| --- | --- |
| every `LanguageParser.parse` | `files.upsert`, `delete_many` |
| every `DependencyParser.parse` | `knowledge.replace_file_facts`, `add_edges`, `add_unresolved_refs`, `intern_entity`, `gc_orphan_entities` |
| `chunk_content` | `dependencies.replace_for_file` |
| `plan_sync`, `decide_rehash`, `facts_version` | `repositories.mark_sync_started / _failed / mark_many_synced / relocate / assign_slug / update_detected_metadata / delete` |
| `ResolutionIndex.build` and all of `prepare_global` | `commit_global` — the only write in the whole link pass |
| hashing, containment checks, ignore matching | `vectors.upsert_many`, `gc_orphan_vectors` |

#### The crash-atomic per-file transaction

Introduced at commit `7bebd2f`. The file row carries the content hash the *next* run's planner compares. Committing the row separately from the facts derived from that content would publish a claim — *this content is indexed* — that nothing later contradicts, because the bytes really have not changed. Every subsequent sync would then skip the file, `--full` included, since `--full` forces a rehash and the hash is unchanged.

So the commit *is* the completeness marker, and plain `sync` is the entire recovery path. There is no repair command. `repoatlas/sync/engine.py:14-31, 407-476`

The transaction helper is reentrant and **joins** rather than committing early: an inner `with self._db.transaction()` inside an outer one is a no-op on commit, and the outermost block owns it. Consequence for anyone reading the sync path: `mark_many_synced` opens its own transaction but is nested inside `run`'s, so the repository is not durably `ok` until `run`'s block exits. **[VERIFIED]** `repoatlas/storage/sqlite/database.py:67-92`

## 07 · File admission and trust boundaries

The admission order is fixed in `scan_repository()` and every decision is made **from the path alone, before any content is read**. `repoatlas/sync/scanner.py:141-224`

```
git ls-files --cached --others --exclude-standard -z
   │  returns tracked files PLUS untracked-not-ignored files
   │  a TRACKED file is listed whether or not .gitignore matches it
   │  exclusion sources: root .gitignore · subdir .gitignore
   │                     .git/info/exclude · core.excludesFile (HOST config)
   │
   │  listing is None  ──▶ raise EnumerationUnavailable   ← FAIL CLOSED
   │  listing.warnings ──▶ carried up as sync diagnostics ("could not read N dirs")
   ▼
1. is_secret_path(rel_path)          basename, casefolded, fnmatchcase
                                     .env .env.* *.pem *.key id_rsa id_ed25519
                                     NOT configurable, NOT negatable
2. _is_excluded(rel_path, config)    any path part in DEFAULT_EXCLUSIONS
                                     .git node_modules .venv venv dist build
                                     target .next coverage .cache __pycache__ vendor
3. ignore.matches(rel_path)          .repoatlasignore, gitignore-like, case-SENSITIVE
                                     read through the boundary, capped at 1 MiB
4. _belongs_to_nested_repository()   resolved-path comparison against managed subrepos
5. boundary.stat(rel_path)           ← CONTAINMENT: decides from the filesystem
                                       None for anything that is not a contained
                                       regular file (symlink out, vanished, refused)
   ▼
ScannedFile(rel_path, size, mtime_ns)      ← still no bytes read
   ▼
plan_sync classification → only new|changed reach _index_file
   ▼
boundary.read_content(rel_path, max_bytes=indexing.max_file_bytes)   ← FIRST READ
   OVERSIZED → streamed for a digest + 8 KiB prefix, never allocated whole,
               recorded with NO facts (replace_file_facts with the file entity only)
   binary (NUL in first 8 KiB) → recorded, never parsed
   undecodable UTF-8           → recorded, never parsed
   ▼
parse → chunks → FTS → embeddings → retrieval → prompt
```

> **The exact eligibility point.** Content becomes eligible for chunks, embeddings, retrieval and prompts only after it has passed the secret policy, the built-in exclusions, `.repoatlasignore`, nested-repository ownership, the containment `stat`, the size cap, the binary sniff and the UTF-8 decode — in that order. Everything before `read_content` is decided on the name and the filesystem, so an excluded file is never opened. This is the boundary V2 must not move.

#### Why enumeration fails closed

The `EnumerationUnavailable` docstring is itself an architecture decision record. Reproducing git's answer without git needs a `.git/index` parser (v2/3/4, extensions, split index, worktree indirection), a complete gitignore engine (nested scope, negation, the rule that a re-include cannot resurrect a file under an excluded directory, anchoring, `**`, classes, escapes), and git's configuration cascade to resolve `core.excludesFile` across system, global, XDG and repository config including `include`/`includeIf`. The last is decisive: it is *host* configuration outside the repository, and this code path exists precisely when git cannot be asked. So the repository is recorded `failed`, nothing is indexed, a previous good index is kept, and `status` reports `unavailable (git could not enumerate the repository)`. `repoatlas/sync/scanner.py:53-113`

#### Trust boundaries, enforcement points and their tests

| Threat | Enforced at | Failure mode | Test suite |
| --- | --- | --- | --- |
| Repository content is untrusted input | whole design; parsers are pure, no execution | would be arbitrary code execution | `test_hostile_content.py`, `test_layering.py` |
| Ignored/secret content reaching the index | `security.is_secret_path` before all ignore handling | credentials in chunks, embeddings, prompts | `test_secret_exclusion_integrity.py` |
| Git unavailable → indexing a different set than reported | `scan_repository` raises before the first candidate | silent superset; sync exits 0 | `test_git_enumeration_refusal.py` |
| Symlink or `..` escaping the repository | `RepositoryBoundary.stat / read_content / read_into` | host files indexed and cited | `test_filesystem_containment.py` |
| Path-based TOCTOU (index path vs live object) | boundary decides from the descriptor, not the path | ancestor swapped between check and read | `test_filesystem_containment.py`, `test_bounded_file_reads.py` |
| Unbounded read / zip bomb | `max_file_bytes` applied by the reader on the descriptor | memory exhaustion | `test_bounded_file_reads.py` |
| Arbitrary git invocation | `gitstate/client.py` is the only `subprocess` caller | hook execution, config injection | `test_git_execution_boundary.py`, `test_layering.py` |
| Writing to a database RepoAtlas does not own | `identity.establish` before any pragma that writes | 21 tables created in someone's database | `test_database_identity.py` |
| Model inventing evidence | `ground_answer` after generation | fabricated provenance under a trusted heading | `test_answer_trust_model.py`, `test_trust_boundary.py` |
| Prompt injection from repository text | JSON-encoded evidence + system prompt + grounding | forged evidence block indistinguishable from real | `test_hostile_content.py`, `test_trust_boundary.py` |
| Ollama returning a non-contract response | `OllamaBackend._envelope / _content`, layered `ModelFailure` | traceback, or garbage parsed as an answer | `test_ollama_tags_protocol.py`, `test_ollama_failure_diagnostics.py` |

Every row above except the last two sits **below** the LLM layer and is independent of it. That is the set V2 inherits unchanged. **[VERIFIED]**

## 08 · Parser architecture

`LanguageParser` is a runtime-checkable Protocol with three attributes and two methods, and no base class. `repoatlas/parsers/base.py`

```
class LanguageParser(Protocol):
    name: str        # provenance stamped on every fact it produces
    version: str     # bumped when OUTPUT SHAPE changes → forces reparse
    language: str    # label on the file row, reported by `repos`
    def supports(self, path: Path) -> bool: ...
    def parse(self, path: Path, content: str) -> ParsedFile: ...   # must not raise
```

| Language | Module | `name` | `version` | Technique |
| --- | --- | --- | --- | --- |
| Python | `parsers/python.py` | `python-ast` | 3 | stdlib `ast` |
| TypeScript / JavaScript | `parsers/typescript.py` | `tree-sitter-typescript` | 2 | tree-sitter |
| Bash | `parsers/bash.py` | `tree-sitter-bash` | 2 | tree-sitter + word analysis |
| Ruby | `parsers/ruby.py` | `tree-sitter-ruby` | 2 | tree-sitter |
| Salt SLS | `parsers/salt.py` | `salt-sls` | 2 | YAML + Jinja-aware scan |
| Jinja | `parsers/jinja.py` | `jinja-scan` | 2 | lexical scan, never rendered |
| Dockerfile | `parsers/dockerfile.py` | `dockerfile` | 4 | instruction scan + `dockerwords.py` |
| YAML / JSON | `parsers/structured.py` | `structured-config` | 1 | safe load, key flattening |
| GraphQL | `parsers/graphql.py` | `graphql` | 1 | graphql-core 3.2.6 parse; schema coordinates; no schema built |
| GraphQL codegen and graphql-config | `parsers/graphql_codegen.py` (inside `structured.py`) | `structured-config` | 9 | composed YAML nodes; one claim per output or project |
| GraphQL schema fetches | `parsers/graphql_artifacts.py` (inside `structured.py`) | `structured-config` | 9 | `package.json` scripts, `shlex` words; never run |
| ariadne-codegen | `parsers/python_project.py` | `python-project` | 5 | `[tool.ariadne-codegen]` read as TOML |
| GraphQL in TS / Python | `parsers/ts_graphql.py`, `parsers/py_graphql.py` (inside the TS and Python parsers) | `tree-sitter-typescript` / `python-ast` | 18 / 16 | embedded documents, schema construction, resolver maps, Python fetch scripts, Python enum members |
| Prisma | `parsers/prisma.py` | `prisma` | 1 | bounded tokenizer and recursive descent of the documented grammar |
| SQL | `parsers/sql.py` | `sql` | 1 | sqlglot 30.19.0 (PostgreSQL), bounded `ALTER TYPE` grammar over its tokens |
| Persistence in TS / Python / dashboards | `parsers/ts_persistence.py`, `parsers/py_persistence.py`, `parsers/dashboards.py` (inside the TS, Python and structured parsers) | `tree-sitter-typescript` / `python-ast` / `structured-config` | 18 / 16 / 10 | clients, queries, typed mappings and comparisons |

Dispatch order matters exactly once: `SaltParser` must precede `StructuredParser` so an `.sls` file is never treated as ordinary YAML. `ParserRegistry.for_path()` returns the first parser that claims the path.

#### Output model

`ParsedFile{language, parser, parser_version, symbols, references, diagnostics}`, all frozen tuples.

- **Symbol** — `ParsedSymbol{kind: EntityType, name, qualified_name, line_start, line_end, signature, parent}`. The span is optional for exactly one case: the whole of an empty file, which is a real importable module with no lines to name.
- **Unresolved reference** — `ParsedReference{edge_type, target_kind, target_key, line_start, line_end, confidence, source_symbol, meta}`. `target_key` is *raw text as written in the source*. The parser never guesses what it points at; `meta` carries whatever the linker will need (import level, package prefix, alias, `via: COPY`).

#### Deterministic immediately versus waiting for the linker

| Becomes an edge during `_index_file` | Becomes an `unresolved_refs` row |
| --- | --- |
| `DEFINES` from the file entity to every symbol | module specifiers (also interned, so both happen) |
| references whose `target_kind` is `ENV_VAR`, `MODULE`, `COMMAND` or `CONFIG` — interned by natural key | config keys |
| references to a symbol this same file defines | file/script paths (`_PATH_KINDS`) |
|  | symbol references, but **only** when the file imported that name; the import specifier rides along on the claim |
|  | package names, salt states, templates |

#### Provenance attachment

Line spans come from the parser and are copied onto the `EdgeRecord` or `UnresolvedRefRecord`; `repo_id` and `file_id` are stamped by `link_file`; `parser` is `parsed.parser` for per-file edges and the literal `"linker:global"` for everything the global pass creates. That constant is what lets the global pass delete exactly its own output and rebuild.

#### The parser-version mechanism

```
facts_version(registry, dependency_registry) =
    "schema:5"
    "||" + "|".join(sorted(f"{p.name}:{p.version}"))          # 8 language parsers
    "||deps:" + "|".join(sorted(f"{p.name}:{p.version}"))      # 12 dependency parsers
    "||chunks:" + CHUNKING_VERSION
```

The whole composite string is stored on **every file row** as `files.parser_version`. `plan_sync` compares it before it looks at size, mtime or hash, and a mismatch classifies the file `changed` with no hashing at all. This is why a semantic parser change reparses files whose bytes are untouched: HEAD, mtime and content hash all still agree, and nothing else would notice. `status` calls `files.count_outdated()` against the same value and reports `stale (indexed by an older build)`.

The embedding model is deliberately excluded from the composite. Vectors are keyed by `(content_hash, model)`, so switching models simply finds no vectors for the new id and re-embeds without reparsing anything. **[VERIFIED]** `repoatlas/sync/planner.py:25-47`

## 09 · Domain fact and graph model

Two tiers of entity, distinguished by one column. `file_id IS NOT NULL` means file-scoped and cascades away with its file; `file_id IS NULL` means interned, shared, unique by key, and garbage-collected when no edge references it.

| Concept | Key scheme | Table | Provenance | Lifecycle |
| --- | --- | --- | --- | --- |
| Repository | `repo:<slug>` | `repositories` + interned `entities` | path, branch, head_sha, dirty | created by `register`; deleted by `forget_if_missing` |
| File | `file:<slug>:<rel_path>` | `files` + file-scoped `entities` | content_hash, size, mtime_ns, parser, parser_version | upserted per sync; delete cascades everything |
| Symbol | `sym:<slug>:<rel_path>:<qualified_name>:<line>` | `entities` (file-scoped) | line_start, line_end, signature | replaced wholesale on reparse |
| Environment variable | `env:<NAME>` | `entities` (interned) | none of its own; edges carry it | interned on demand, GC'd when unreferenced |
| Module | `module:<specifier>` | `entities` (interned) | none | same |
| Command | `cmd:<name>` | `entities` (interned) | none | same |
| Config key | `cfgkey:<dotted.path>` | `entities` (interned) | none | same |
| Package | `pkg:<ecosystem>:<normalized_name>` | `entities` (interned) | none | materialized by the global pass |
| Edge | surrogate `id` | `edges` | **repo_id, file_id, line_start, line_end, parser, confidence** | file edges die with the file; global edges are deleted and rebuilt every run |
| Unresolved reference | surrogate `id` | `unresolved_refs` | repo_id, file_id, src_id, lines, meta_json | written per file; re-read by every global pass; never consumed |
| Chunk | surrogate `id`; natural `content_hash` | `chunks` + `chunks_fts` | file_id, repo_id, line_start, line_end | replaced on reparse; new row id, same content hash |
| Embedding | `(content_hash, model)` | `embeddings` WITHOUT ROWID | model, dim | survives reparse when text is byte-identical; orphans GC'd |
| Dependency | surrogate `id` | `dependencies` | repo_id, file_id, line_start, line_end, parser | replaced per file; grouped into `DependencyPackage` on read |

**"Claim" is the code's word for an unresolved reference.** `link_file` builds `UnresolvedRefRecord`s it calls claims, and `prepare_global` re-reads all of them on every run. They are a *standing* record, not a queue: a claim that resolves still stays in the table, which is why an `unresolved_refs` row does not mean "unresolved" — it means "asserted, resolved afresh each pass". **[VERIFIED]**

#### Edge vocabulary — 17 declared, and how each is used

| Edge | Produced by | Traversed | Cross-repo | Meaning |
| --- | --- | --- | --- | --- |
| `DEFINES` | `link_file`, every parser | **no** | no | file declares symbol — containment, deliberately excluded |
| `CONTAINS` | declared, structural | **no** | no | containment; same exclusion |
| `IMPORTS` | Python/TS parsers → linker | yes | **yes** | module or file import |
| `EXPORTS` | TS, Bash (`export VAR`), Docker `ENV` | yes | no | makes a name available outward |
| `REFERENCES` | Docker `COPY`, Salt, generic paths | yes | **yes** | names a file or object |
| `CALLS` | Python/TS/Ruby parsers | yes | no | invocation; excluded cross-repo to stop name-only wandering |
| `DEPENDS_ON` | global pass from `dependencies` | yes | **yes** | declared/resolved package, or a managed repo that provides it |
| `EXECUTES` | Bash, Dockerfile `RUN` | yes | **yes** | runs a command or script |
| `SOURCES` | Bash `source`/`.` | yes | **yes** | inlines another script |
| `INCLUDES` | Salt `include`, Jinja | yes | no | template/state inclusion |
| `REQUIRES` | Salt requisites | yes | no | ordering requisite |
| `WATCHES` | Salt requisites | yes | no | watch requisite |
| `READS` | every language, env access | yes | no | reads an environment variable |
| `WRITES` | env assignment | yes | no | sets an environment variable |
| `CONFIGURES` | Salt, structured config | yes | no | supplies configuration to a target |
| `MANAGES` | Salt states | yes | no | state manages a resource |
| `GENERATES` | templates | yes | no | produces a file |

**Two exclusions are load-bearing.** `_EXPANSION_EDGES` omits `DEFINES` and `CONTAINS`, because traversing containment turns any file node into a fan-out over every symbol it declares and two hops of that buries the relationship under an inventory. `_CROSS_REPOSITORY_EDGES` admits only `DEPENDS_ON`, `REFERENCES`, `EXECUTES`, `SOURCES` and `IMPORTS` — every one produced by the global pass from an exact match — so a cross-repository answer always has a structural reason. `repoatlas/retrieval/retriever.py:49-89`

`confidence` is a real column on `edges` with default 1.0, set from `ParsedReference.confidence`, and used in exactly one place: `expand_neighbors` orders by `confidence DESC, id`. It is not part of ranking, and nothing filters on it. **[VERIFIED]**

## 10 · Linker and dependency model

Two passes, split for correctness rather than for speed. `repoatlas/graph/linker.py:1-29`

#### Per-file pass — `link_file()`

Creates only what one file can prove alone: `DEFINES` to its own symbols, edges to interned global targets, and references to symbols it defines itself. Everything else becomes an `UnresolvedRefRecord`. A symbol reference is recorded **only when this file imported that name**, and the import's specifier is carried on the claim so the global pass has something exact to resolve against. Without the import there is nothing to resolve and the reference is dropped rather than guessed.

#### Global pass — `prepare_global()` then `commit_global()`

```
prepare_global()                         WRITES NOTHING
  stage "index"        list_file_rows() + list_symbols() → ResolutionIndex.build()
  stage "resolve"      list_unresolved_refs() → _resolve(claim, origin, index, siblings)
  stage "dependencies" _plan_dependencies(index) → PendingEdge list

commit_global(plan)                      ONE TRANSACTION
  stage "swap"         delete_edges_by_parser("linker:global")
                       _materialize(plan.dependencies)   ← interning happens here
                       add_edges(edges)
                       gc_orphan_entities()
```

The pass is **idempotent by construction**: it deletes every edge it previously created and rebuilds from the standing claims, which is what makes an incremental sync equal a full reindex. Before the split, the delete committed on its own and the insert followed later, so any failure in the gap published a knowledge base with no cross-repository graph while `status` still called it current.

#### `ResolutionIndex` — the lookup structures

| Structure | Shape | Answers |
| --- | --- | --- |
| `files_by_repo` | `{repo_id: {rel_path: FileRow}}` | exact path within a repository |
| `repo_roots` | `[(Path, repo_id)]`, longest first | which repository owns an absolute path — nested wins |
| `repo_slugs` / `repo_names` | `{repo_id: str}` | key construction (slugs) versus display |
| `symbols_by_file` | `{file_id: {name: [SymbolRow]}}` | cross-file symbol resolution, by name and qualified name |
| `salt_states_by_repo` | `{repo_id: {name: [SymbolRow]}}` | Salt requisite targets |
| `files_by_name` | `{basename: [FileRow]}` | basename fallback |

#### Three worked resolutions

**A. Python import → file.** `from . import helper` in `pkg/main.py` produces two references. One targets `module:.` (interned immediately, plus a claim); the other targets `module:.helper` with `meta={"level":"1","package":"."}`. In the global pass `_resolve_module` reads `meta["package"]` and routes to `resolve_package_submodule(origin, ".", "helper")`, which first resolves the package, checks its basename is an `__init__` file, and only then resolves `.helper`. Result: `IMPORTS → file:<slug>:pkg/helper.py` alongside the interned `module:.helper`. The absolute form `from pkg.sub import helper` keeps one import claim for `pkg.sub` with the fromlist name. After that package import resolves, the linker checks whether `helper` is a child module; if so, it adds `IMPORTS → file:<slug>:pkg/sub/helper.py` alongside the package edge. Calls through the imported name use that same checked child when present and otherwise fall back to the package module, preserving package attributes.

**B. Environment-variable chain.** `os.environ["SERVICE_HOST"]` in a Python file and `export SERVICE_HOST=…` in a Bash file both intern `env:SERVICE_HOST` during their own per-file transactions and attach `READS` and `EXPORTS` edges to it. No global work is needed — the interned key *is* the join. This is why two-hop expansion is the useful minimum: hop one reaches the hub, hop two reaches the files on the other side.

**C. File reference from a Dockerfile or Bash script.** `COPY assets/main.js /app/main.js` emits a claim with `target_kind=FILE`, `target_key="assets/main.js"`, `meta={"parser":"dockerfile","via":"COPY"}`. The global pass calls `index.resolve_repo_relative_path(origin, target_key)` and produces `REFERENCES → file:<slug>:assets/main.js`. `COPY --from=builder assets/main.js …` emits **no file claim at all** — the operand names a path inside a previous build stage, not in the repository, and the parser refuses at the source. `./bin/tool` in Bash resolves the same way; `~/tool`, `"~/tool"` and `$HOME/tool` produce a `cmd:` entity or an `env:HOME` read and never a repository file.

#### Where the linker refuses to guess

- a symbol reference with no matching import — dropped, not claimed;
- a variable or interpolated operand — no claim;
- a package name matched to a managed repository only on **exact normalized name within the same ecosystem**;
- a relative submodule whose package does not resolve to an `__init__` file;
- a claim whose origin file row has vanished — counted unresolved, no edge.

#### Dependency intelligence

| Ecosystem | Parsers | Roles produced |
| --- | --- | --- |
| PyPI | `pyproject` (PEP 621 *and* Poetry), `requirements-txt`, `setup-py` (AST, never imported), `setup-cfg`, `python-lock` (uv.lock) | declared, resolved, provided |
| npm | `package-json`, `package-lock`, `pnpm-lock`, `yarn-lock` | declared, resolved, provided |
| gem | `gemfile`, `gemspec`, `gemfile-lock` | declared, resolved, provided |

The distinctions V1 keeps: **ecosystem** is part of identity, because npm `foo-bar` and PyPI `foo_bar` normalize to the same string and are not the same dependency. **declared** carries a `constraint_text` and a group; **resolved** carries a concrete `version`; **provided** is what this repository itself publishes, and it is what makes a repository-local dependency detectable — `_plan_dependencies` asks `dependencies.providers(ecosystem, normalized_name)` and, when a managed repository provides the name, emits a cross-repository `DEPENDS_ON` to `repo:<slug>` instead of to `pkg:<eco>:<name>`.

Docker `FROM` images also become `DEPENDS_ON` claims with `ecosystem: docker`. Bash and Salt participate through `EXECUTES` and `INCLUDES`, not through the dependency tables.

> **The callable surface for dependency questions already exists and is not wired to anything.** `SqliteDependencyStore.list_packages(repo_ids)` returns `list[DependencyPackage]` — a frozen Pydantic model grouping every observation of one package in one repository, keyed `(repository, ecosystem, normalized_name)`, never reduced to a single winning row. `rendering.text.render_dependencies()` formats it. Neither has a CLI command or a service function; their only callers are tests. **[VERIFIED]** `storage/sqlite/stores.py:942, rendering/text.py:283`

## 11 · SQLite / storage model

One database, WAL, five migrations, schema version 5. `Database` is the only object that opens SQLite, and SQL text exists only under `storage/sqlite/`.

| Table | Identity | Key FKs | Indexes | Delete | Written by |
| --- | --- | --- | --- | --- | --- |
| `repositories` | `id`; natural `path` UNIQUE, `slug` UNIQUE where non-empty | — | slug (partial unique) | root of every cascade | `SqliteRepositoryStore` |
| `files` | `id`; natural `(repo_id, rel_path)` | repo → CASCADE | `(repo_id, rel_path)`, `content_hash`, `basename` (generated column) | cascades to everything below | `SqliteFileStore` |
| `entities` | `id`; natural `key` unique *only* where `file_id IS NULL` | repo, file → CASCADE | `key`, partial unique on interned `key` | file-scoped cascade; interned by GC | knowledge store, linker |
| `edges` | `id` | src, dst → entities CASCADE; repo, file CASCADE | `(src_id,type)`, `(dst_id,type)`, `file_id` | cascade, or `delete_edges_by_parser` | `link_file`, `commit_global` |
| `unresolved_refs` | `id` | repo, file, src → CASCADE | `(target_kind,target_key)`, `file_id` | cascade with the file | `link_file` |
| `chunks` | `id`; natural `content_hash` + `(span_index, split_index)` | file, repo → CASCADE | `file_id`, `repo_id` | cascade | knowledge store |
| `chunks_fts` | external-content FTS5 over `chunks.text` | `content_rowid = id` | tokenizer `unicode61 remove_diacritics 2 tokenchars '_.-@$'` | three triggers keep it in step | triggers only |
| `entities_fts` | external-content FTS5 over `name`, `qualified_name` | `content_rowid = id` | same tokenizer | three triggers | triggers only |
| `embeddings` | **`(content_hash, model)` PRIMARY KEY, WITHOUT ROWID** | **none** — deliberately no FK | `model` | explicit `gc_orphan_vectors` | `SqliteVectorIndex` |
| `dependencies` | `id` | repo, file → CASCADE | `(repo_id,ecosystem,normalized_name)`, `file_id`, `(ecosystem,normalized_name,role)` | cascade; replaced per file | `SqliteDependencyStore` |
| `meta` | `key` | — | — | never | `Database.set_meta` |

The embeddings table changed shape in migration 003: it was `chunk_id`-keyed with a cascade, and is now content-hash-keyed with none. That is what makes a chunk whose text survives a reparse byte-for-byte keep its vector even though its row was deleted and reinserted with a new id — and it is why orphan vectors need an explicit GC rather than a cascade.

#### Ownership, versioning and first open

```
open_database(path)
  with _contention_reported(path), opening_errors(path):
    connect(isolation_level="DEFERRED"); row_factory = sqlite3.Row
    PRAGMA busy_timeout = 10000
    _establish_ownership(connection, path)     ← BEFORE any pragma that writes
    with _closed_on_failure(connection):
      _refuse_future_schema(connection, path)  ← user_version > 5 → exit 3, unchanged
      _ensure_wal(connection, path)            ← bounded retry loop, see below
      PRAGMA foreign_keys = ON; PRAGMA synchronous = NORMAL
      _apply_migrations(connection)
      if meta.schema_version != "5": write it   ← conditional, so a healthy open
                                                  never takes the write lock
```

- `application_id = 0x52504154` ("RPAT") is the ownership marker. `user_version` is treated as a schema version and explicitly *not* as proof of ownership — that conflation is the defect migration 005-era hardening fixed, where a database containing a `payroll` table and `user_version = 0` was read as a fresh RepoAtlas database and given 21 tables.
- Ownership is established **before the version check and before any write**, because `PRAGMA journal_mode` changes the file header.
- `_ensure_wal` exists because SQLite never runs a busy handler for the read-to-write promotion inside `PRAGMA journal_mode = wal`, so `busy_timeout` is not consulted. It retries with randomized exponential backoff between 1 ms and 50 ms against the same 10-second deadline.
- `_contention_reported` is scoped to **opening only**. Contention during ordinary sync writes is not classified and reaches the CLI as an unhandled `sqlite3.OperationalError`. **[VERIFIED]**

#### Concurrency model

| Mechanism | Scope | Held by | Bound |
| --- | --- | --- | --- |
| Advisory writer lock (`flock`) | one state database, file `atlas.db.lock` | `init` and `sync` only | `storage.lock_timeout_seconds`, default 30 s → exit 5 |
| SQLite `busy_timeout` | the connection | every statement | 10 s |
| WAL | the file | readers never block | — |

Readers deliberately take no lock: `locate`, `status`, `repos` and `ask` run unimpeded during a sync. The writer lock is held by an open file description, so the kernel releases it on exit, on an uncaught exception and on `SIGKILL` — there is no stale-lock state to reason about and no recovery code.

#### Answerable from the database versus requiring computation

| Direct SQL | Needs computation |
| --- | --- |
| repository list, metadata, indexed counts | index freshness — needs a live `git.state()` call plus `facts_version` comparison |
| file rows, hashes, parser versions | repository-set drift — needs a filesystem walk (`plan_repository_set`) |
| entities by key, by name, by FTS | ranked evidence — needs the whole funnel |
| edges in/out of an entity, filtered by type | semantic neighbours — needs a query embedding and a brute-force pass |
| unresolved refs; chunk text and spans | cross-repository `DEPENDS_ON` — materialized only by `commit_global` |
| dependency rows, and `DependencyPackage` grouping | any answer requiring a reparse |

## 12 · Retrieval architecture

One class, `Retriever`, one public method, `retrieve(question, context, *, literal_phrase=False)`, 813 lines. It reads three stores and never writes. No LLM is involved at any stage, which is why `locate` works with no model installed.

#### Stage order, with the real method names

```
analyze(question)                                 retrieval/query.py
   quoted · env_vars · dotted · paths · packages · code_names
   · ambiguous_packages (dashed/camel) · pasted_literals · identifiers · terms
   literal_terms  = everything searched exactly
   definite_terms = the subset whose absence proves the question unanswerable

_scopes(context, primary_ids)
   GLOBAL      → [None]                     one unscoped pass
   REPOSITORY  → [primary_ids, None]        primary first, then everything

1. _stage_exact(literal_terms)     search_literal   DETERMINISTIC / EXACT_CONTENT
2. _stage_files(literal+terms)     find_files_by_path
                                   specificity 0/1/2 → EXACT_PATH / EXACT_SUBPATH
                                                      / EXACT_BASENAME
                                   returns (candidates, proven_terms)
   ── _suppress_associative(definite_terms, exact, proven) ────────────────
      if the question named definite terms and NONE of them exist,
      every associative stage below is switched off and says so.
3. _stage_lexical(question)        search_text (FTS)  ASSOCIATIVE / LEXICAL
4. _stage_symbols(...)             find_entities_by_name → DETERMINISTIC / EXACT_NAME
                                                           and GRAPH SEEDS
                                   search_entities      → ASSOCIATIVE / LEXICAL
                                                           unless name matches exactly
5. _stage_semantic(question)       vectors.query        ASSOCIATIVE / SIMILAR
6. _stage_graph(entity_ids)        _expand()            DETERMINISTIC / RELATED

fuse(ranked_lists, primary_repository=slug, primary_boost)
deduplicate(fused)
reranker.rerank(...)  →  enforce_precedence(...)
apply_budget(..., max_items=final, token_budget=...)
   → EvidenceSet(candidates, trace=RetrievalTrace(...))
```

#### The three independent ranking dimensions

| Dimension | Type | Question it answers | Values |
| --- | --- | --- | --- |
| `EvidenceTier` | `IntEnum`, lower wins | Is this a fact about the code or a guess about the question? | `DETERMINISTIC = 0`, `ASSOCIATIVE = 1` |
| `MatchClass` | `IntEnum`, lower is more specific | How precisely did the query identify it? | `EXACT_PATH 0`, `EXACT_SUBPATH 1`, `EXACT_BASENAME 2`, `EXACT_NAME 3`, `EXACT_CONTENT 4`, `RELATED 5`, `LEXICAL 6`, `SIMILAR 7` |
| Fused score | float, higher wins | How strongly do the retrievers agree? | reciprocal-rank fusion, `k = 60` |

The composite sort key in `fuse()` is:

```
sorted(scores, key=lambda key: (
    tiers[key],                              # 1. tier, absolutely
    anchored.get(key[:2], _UNNAMED),         # 2. path anchor: 0 exact, 1 suffix, 2 none
    -scores[key],                            # 3. fused agreement
    _ordering(key),                          # 4. stable tie-break, spans before files
))
```

> **The anchor is a rank position, not a bonus, and it is a property of the *file*, not of the single candidate.** `_anchored_files()` maps `(repository_slug, path) → position` for every file the query named by path, and every candidate from that file inherits it. So "why does `src/config.py` read `DATABASE_URL`?" answers with the assignment *inside* the named file rather than with the filename match alone. Only `EXACT_PATH` and `EXACT_SUBPATH` earn a position; `EXACT_BASENAME` deliberately does not, because a basename says what a file is called, not where it lives.

#### Merge rules inside fusion

- Each ranked list contributes **one** reciprocal rank per span, its best — otherwise a file that becomes many chunks sharing one line range would make the score a measure of file size.
- A span's tier and match class are both merged with `min`, so a span seen once as an exact path and four times as a lexical neighbour is an exact path either way round, independent of stage order.
- The representative candidate is the strongest claim: best tier first, then highest raw score. The merged tier and match are stamped onto the returned copy.
- The identity key is `(repository_slug or repository, path, line_start, line_end)`. Keying on the display name previously fused `common/index.ts:1` from two different repositories into one candidate.

#### Bounds and biases, with defaults

| Setting | Default | Effect |
| --- | --- | --- |
| `per_source` | 30 | cap per stage per scope, before fusion |
| `final` | 12 | hard item cap on the evidence set |
| `token_budget` | 6000 | second bound; estimate is `len(text) // 4` |
| `graph_hops` | 2 | two is the useful minimum: hop 1 reaches an interned hub, hop 2 the files beyond it |
| `graph_fanout` | 25 | per-hop breadth; query limit is `fanout × len(frontier)` |
| `graph_seeds` | 20 | seeds taken from exact entity-name hits only |
| `primary_boost` | 0.5 | added to the score, so it competes inside a tier and never across one |
| `semantic_limit` | 30 | vector candidates fetched |
| `semantic_floor` | 0.25 | cosine below which a hit is discarded |
| `_DEBUG_LIMITS` | 40 / 18 / 8000 | `debug` only, plus `literal_phrase=True` |

#### Semantic behaviour

Vector search is brute force over a scoped candidate set: one SQL join pulls every `(embedding, chunk, file, repository)` row for the model and optional repo ids, then NumPy normalizes and takes a dot product. There is no ANN index. Two short-circuits keep `locate` instant and offline: if `vectors.count(model_id) == 0` the model is never loaded, and an `EmbeddingUnavailable` is caught and reported rather than raised.

#### Graph expansion

Bounded on three axes at once — hops, per-hop fan-out and total visited nodes — because a hub like `env:SERVICE_HOST` legitimately touches every repository. Seeds come only from **exact entity-name matches**; a fuzzy FTS entity hit is never a seed, because expansion labels everything it reaches `DETERMINISTIC` and seeding from a guess would launder it into the strongest tier. Traversal order is preserved into the candidate list, so first-hop entities precede second-hop ones and fusion reads proximity as strength.

#### Deduplication and budget

`deduplicate()` drops a candidate contained in a higher-ranked one, or overlapping more than 50% of the shorter span of the *same file of the same repository*. Spans are never merged into a synthetic range. A file-level candidate overlaps nothing, so a filename match and a line-level match both survive — they are different facts.

`enforce_precedence()` re-imposes tier and anchor order *after* the reranker seam and *before* budgeting, because a budget spent on embedding neighbours would starve deterministic evidence as effectively as ranking it lower. `NoopReranker` is what V1 ships.

#### Suppression — the honest-absence rule

If a question names definite terms and none of them exists in chunk text *or* as a file identity, every associative stage is switched off and the trace says `suppressed: none of the named terms exist (…)`. The `proven_files` half is why `_stage_files` runs before the decision: `src/config.py` is not absent because no file *mentions* it.

## 13 · Reasoning / Ollama architecture

Eight modules, and the whole of the reasoning path is one call from `query_service.reason()`.

```
llm/protocol.py    Message · ToolSpec · ModelResponse · ModelFailure · ReasoningModel
                   ModelError hierarchy: ModelConfigurationError · ModelUnavailable
                   · ModelServerError · ModelProtocolError · (model-absent)
llm/factory.py     build_model(LLMSettings) → "ollama" | "none"/"null"
llm/ollama.py      OllamaBackend — httpx, POST /api/chat, GET /api/tags
llm/null.py        NullBackend — always unavailable, deterministic
llm/scripted.py    ScriptedBackend — replays canned responses, records prompts
llm/prompts/       system.md (71 lines) · ask.md · debug.md · repair.md · loader.py
llm/reasoner.py    QueryIntent · format_evidence · build_messages · Reasoner
llm/grounding.py   ground_answer + GroundingReport   ← deterministic, no model
```

#### The `ask` trace

```
cli.ask
 └─ _reasoning_command(question, repo, QueryIntent.ASK, no_llm, json, verbose)
     ├─ _open() → open_workspace()
     ├─ _resolve() → RepositoryContext
     ├─ if no_llm: services.retrieve_for(...) → render_evidence → RETURN
     └─ services.reason(workspace, question, context, intent)
         ├─ _build(workspace) → build_model(settings.llm) → OllamaBackend
         ├─ asyncio.run(backend.available())        GET {base_url}/api/tags
         │     false → ReasoningModelUnavailableError (exit 4) BEFORE retrieval
         ├─ retrieve_for(...) → Retriever.retrieve(...) → EvidenceSet
         └─ asyncio.run(Reasoner(backend).reason(question, evidence, intent))
             ├─ build_messages()
             │    system  = prompts/system.md
             │    user    = prompts/ask.md rendered with question, scope note,
             │              format_evidence(evidence)  ← JSON, not delimiters
             ├─ backend.complete(messages, response_schema=RepoAtlasAnswer)
             │    POST /api/chat {model, messages, stream:false,
             │                    options:{temperature:0.0},
             │                    format: RepoAtlasAnswer.model_json_schema()}
             ├─ RepoAtlasAnswer.model_validate_json(_extract_json(content))
             │    on failure → ONE repair turn (assistant echo + repair.md)
             │    second failure → INSUFFICIENT EVIDENCE, both problems named
             └─ ground_answer(answer, evidence) → ReasoningResult
     → render_answer(result.answer, evidence)   [or render_diagnosis for debug]
```

#### Which pieces are repository intelligence, and which exist only because V1 owns a local model

| Piece | Classification | Reasoning |
| --- | --- | --- |
| Retrieval funnel, tiers, match classes, budget | **Repository intelligence** | no LLM import; `locate` uses it alone |
| `EvidenceSet` / `EvidenceCandidate` | **Repository intelligence** | lives in `models/`, which imports nothing internal |
| `ground_answer` and `SuppliedEvidence` | **Repository intelligence** | pure function of (answer, evidence); no model, no I/O; would validate any external agent's citations unchanged |
| `RepoAtlasAnswer` / `Hypothesis` / `Evidence` | Boundary contract | a schema for *an* answer, not for Ollama's answer |
| `format_evidence` | Boundary contract | JSON serialization of evidence for any consumer |
| `Reasoner.reason` orchestration | **Local-model only** | prompt assembly, repair turn, response salvage |
| `prompts/*.md` | **Local-model only** | instructions to a model RepoAtlas drives |
| `OllamaBackend`, `NullBackend`, `build_model` | **Local-model only** | transport and backend selection |
| `ModelFailure` taxonomy, exit 4 | **Local-model only** | describes a server RepoAtlas contacted |
| `render_answer` / `render_diagnosis` | **Local-model only** | human presentation of a model answer |

#### Ollama coupling inventory

| Reference | Class | Note |
| --- | --- | --- |
| `llm/ollama.py` — 538 lines, all endpoints | B reasoning-layer | `/api/chat`, `/api/tags`, `done_reason == "length"` |
| `llm/factory.py:20-21` | B reasoning-layer | the only construction site |
| `llm/__init__.py` re-export | B reasoning-layer | convenience only |
| `config/settings.py:174-182` `LLMSettings` | C CLI/config | `provider` defaults to `"ollama"`, model `gpt-oss:20b` |
| `REPOATLAS_LLM_BASE_URL` env override | C CLI/config | exists for the compose stack |
| `reasoner.py:176` — "Ollama: options.num_predict" | C CLI/config | a hint string in a diagnostic |
| `protocol.py:4, 199` comments | C CLI/config | documentation only |
| `llm/scripted.py` | D test-only | never reachable from `build_model` |
| `embeddings/fastembed_backend.py:14` comment | D test-only | an analogy, not a dependency |

**Class A — domain or core coupling — is empty.** No module outside `llm/` and `config/` mentions Ollama, and the layering test forbids `llm/` from being imported by storage, sync, indexing, discovery or services. `query_service` is the single import site in the application layer.

> **How hard is "no built-in reasoning backend" as a first-class mode?** Structurally, it already exists: `provider: none` returns `NullBackend`, which reports unavailable, and `ask`/`debug` then exit 4 with a message naming `locate`. Every other command is unaffected because none of them import `llm/`. What would need decisions rather than code is presentational: `ask --no-llm` currently renders the same evidence view as `locate`, so the two commands become indistinguishable, and exit 4 is phrased as a failure rather than as a supported configuration. **[INFERRED]** from the import graph and the factory, not from any existing switch.

#### What is actually sent to the model

`format_evidence()` emits `{"evidence": [ … ]}` as indented JSON. Each block carries `id` (`E1`, `E2`, … — positional, per request), `repository`, `repository_path` (the slug), `path`, `start_line`, `end_line`, `why_retrieved` (the candidate's description), `content`, and `content_truncated`.

- **No tier, no match class, no score, no source, no entity id, no content hash.** The model is told *why* a block was retrieved in prose and nothing about how it ranked.
- `start_line` and `end_line` are `null` for a file-level candidate, and the system prompt says such a block proves only that the file exists.
- Repository content is a JSON *string value*, so quotes, newlines and braces are escaped by the encoder. The previous `[E9] repository=… path=…` delimiter format could be forged by a file that contained those lines.
- Ordering is the ranked order; no re-sorting happens at serialization.

#### Payload size

Two independent caps compose. `apply_budget` bounds the set at 12 items and 6000 estimated tokens (18 / 8000 for `debug`), and `_MAX_BLOCK_CHARS = 2000` bounds any single block. A worst-case `ask` payload is therefore roughly **12 × 2000 ≈ 24 KB** of content plus JSON overhead and the 71-line system prompt; `debug` reaches about 36 KB. The chunker's own `max_chunk_chars = 8000` means a single chunk can exceed the per-block cap and be truncated with `content_truncated: true`.

Compression before sending: fusion collapses duplicate spans, `deduplicate` removes contained and >50%-overlapping spans, the budget truncates the tail, and each block's body is trimmed. Everything dropped is dropped *silently from the model's point of view* — only `EvidenceSet.truncated` and the trace record that it happened, and neither is serialized into the prompt.

## 14 · Grounding and citation enforcement

`ground_answer(answer, evidence) → GroundingReport` is a pure function. It builds two indexes from the supplied set and then deletes everything unsupported.

```
SuppliedEvidence.spans      {(slug, path): [(start, end), …]}   ← identity-keyed
SuppliedEvidence.by_display {(display_name, path): (identity, …)}
allowed = every legitimate spelling of a supplied file:
          path · repo/path · basename · repository · slug · slug/path
          · every trailing sub-path · every trailing slug segment
          + every reference token found in the USER'S OWN QUESTION
```

| Model behaviour | Enforcement | Outcome |
| --- | --- | --- |
| Cites a file never supplied | `_resolve_source` finds no key | citation dropped; hypothesis dropped entirely |
| Cites a real file that was not in this evidence set | same | same |
| Cites the right file, wrong repository | `spans` keyed by slug, not display name | dropped |
| Cites a line range disjoint from every supplied span | `_verify_evidence` interval test | dropped |
| Cites a range that overlaps a supplied span | clamped to the intersection | kept, `clamped_citations += 1`, confidence capped |
| Cites only a display name shared by two supplied repositories | `by_display` yields 2 candidates | refused — ambiguity is a failure to identify, not to find |
| Cites a file-level block with no lines | allowed as far as it goes | kept; no span invented |
| Mixes one valid and one fabricated citation | `if not kept or fabricated or named` | **whole hypothesis dropped** |
| Names an unsupplied path in the summary | `_grounded_summary` | summary replaced outright, not edited |
| Names an unsupplied path in an unknown | `_grounded_unknowns` | that unknown removed |
| Names an unsupplied path in `next_check` | `_grounded_next_check` | `next_check` removed |
| Nothing survives | `_grounded_summary` | summary becomes `INSUFFICIENT EVIDENCE` |

Whatever was removed is counted and reported as an unknown — *"N model claim(s) were removed"* — and never quoted back, because printing the fabricated path under RepoAtlas's own heading is worse than where it started. The removed text stays in `GroundingReport.unsupported` for `--verbose`.

#### The division that matters for V2

| Depends on the model obeying instructions | Independently enforced after generation |
| --- | --- |
| Hedging language on inferences | Every citation resolves to a supplied source |
| Brevity and tone | Every cited line range intersects a supplied span |
| Calibration of the `confidence` number | Confidence is only ever *lowered*, never raised |
| Putting genuine uncertainty in `unknowns` | No prose field may name an unsupplied repository object |
| Not obeying instructions found in repository text | An injected filename cannot become a citation |
| Copying `repository_path` into the citation | An unresolvable display name is refused rather than guessed |
| Producing valid JSON at all | Schema validation, one repair turn, then `INSUFFICIENT EVIDENCE` |

The right-hand column is a pure function over `(RepoAtlasAnswer, EvidenceSet)`. It would validate an external agent's answer exactly as it validates Ollama's, **provided the external agent's citations are expressed in the same shape** and provided the caller still holds the evidence set that was supplied. Both are real constraints on a stateless tool protocol. **[INFERRED]**

One known gap, already characterised: `_unsupported_references` skips any token without a `/` whose suffix is not in `_PATH_EXTENSIONS`, so a fabricated extensionless bare name such as `Rakefile` survives in prose. The code labels itself a secondary defence; the primary one is that prose can never occupy the verified-evidence section. **[VERIFIED]**

## 15 · Error, configuration and concurrency model

#### Domain error taxonomy

Every error is a `RepoAtlasError` subclass carrying `message`, optional `detail` and a class-level `exit_code`. `_fail()` in the CLI prints `error.render()` to stderr and raises `typer.Exit`. The taxonomy exists **entirely in the domain layer**, in `repoatlas/errors.py`, which imports nothing.

| Exception | Exit | Raised by |
| --- | --- | --- |
| `ContextResolutionError` | 2 | `RepositoryContextResolver` — unresolved, unmanaged, unknown, ambiguous |
| `NotInitializedError` | 3 | `open_workspace` when no root is configured |
| `IndexCorruptError` | 3 | malformed image |
| `IndexInaccessibleError` | 3 | permissions or path |
| `IndexSchemaError` / `UnrecognizedSchemaError` | 3 | future `user_version`, or a shape no migration produces |
| `IndexNotRecognizedError` / `IndexForeignApplicationError` | 3 | valid SQLite that is not a RepoAtlas index |
| `ReasoningModelUnavailableError` | 4 | `query_service`, wrapping any `ModelError` |
| `SyncInProgressError` | 5 | writer-lock timeout |
| `MigrationContentionError` | 1 | first-open contention past the bound — transient, retry |
| `MigrationFailedError` | 1 | a migration raised |
| `RepositoryRootConflictError` | 1 | `init` would give one identity to two live repositories |
| Enumeration failure | 1 | *not* an exception at the CLI: `EnumerationUnavailable` is caught inside `SyncEngine.run`, becomes a per-repository report, and the command exits 1 via `report.unavailable` |

**Answer to the question the brief asks:** yes — these are reusable domain types, defined before and independently of CLI rendering. The CLI only maps `exit_code` and calls `render()`. A machine-facing caller could catch the same classes and read `message`, `detail` and `exit_code` without touching Typer. The one exception is the reasoning layer, where `ModelError`'s rich layered taxonomy (`ModelFailure.CONFIGURATION / TRANSPORT / SERVER / MODEL_ABSENT / PROTOCOL / OUTPUT / TRUNCATED / REPAIR`) is **flattened to a single exit code 4** when `query_service` wraps it. The category survives on the exception object; nothing downstream reads it. **[VERIFIED]**

#### Configuration

```
config file   $XDG_CONFIG_HOME/repoatlas/config.yaml   (override: REPOATLAS_CONFIG)
database      $XDG_DATA_HOME/repoatlas/atlas.db        (override: REPOATLAS_DB,
                                                        or settings.storage.database)
env overrides REPOATLAS_LLM_BASE_URL · REPOATLAS_SYNC_LOCK_TIMEOUT   ← only these two
Settings = repos · discovery · indexing · embeddings · retrieval
           · storage · llm · output · grounding      (all frozen Pydantic)
```

`load_settings()` falls back to defaults when no file exists; `save_settings()` is called by `init` only, and deliberately *after* the repository rows are committed. Configuration enters the system at exactly one place: `open_workspace()` builds a `Workspace` dataclass and every service takes that workspace.

| Core RepoAtlas configuration | V1 local-reasoning configuration |
| --- | --- |
| `repos.root`, `discovery.*`, `indexing.*` | `llm.provider`, `llm.model`, `llm.base_url`, `llm.timeout_seconds` |
| `storage.database`, `storage.lock_timeout_seconds` | `REPOATLAS_LLM_BASE_URL` |
| `retrieval.*` — all nine bounds and biases | `output.verbosity` (rendering of answers) |
| `embeddings.*` — independent of the LLM | `grounding.require_evidence` |

`output.max_evidence` sits in the core column but is **not consulted by the retriever**, which reads `retrieval.final` instead. Raising it does not widen a `locate` result. **[VERIFIED]**

#### Concurrency assumptions that were fine for a CLI

| Assumption | Where | Why a server changes it |
| --- | --- | --- |
| One command equals one process; the connection dies with it | `Workspace.close()`, `open_database` | a long-lived connection must handle schema drift, WAL growth and reconnection |
| One `sqlite3.Connection`, no thread affinity handling | `Database.__init__` | Python's sqlite3 defaults to same-thread checking; concurrent tool calls need per-thread or pooled connections |
| Transaction depth is per-`Database` instance state | `Database._depth` | a shared instance across concurrent requests would interleave depth counters |
| The repository set is read once per command | `Workspace.resolver()` calls `list_all()` each time | cheap now; per-call cost grows with request rate |
| Embedding backend built lazily, cached on the workspace | `Workspace.embeddings()` | fastembed loads an ONNX session; a per-request workspace would reload it |
| Working tree may change between calls | everywhere | a server can hand out evidence whose lines have moved since retrieval |
| Git is invoked per repository, per sync, as a subprocess | `gitstate/client.py` | process spawn cost becomes visible at request rate |
| Writer lock is held for a whole `init`/`sync` | `Workspace.writer_lock()` | a server serializes all writes behind one lock, bounded at 30 s |
| No caching anywhere | — | there is no cache to invalidate, which is an advantage worth keeping deliberately |
| `asyncio.run()` is called *inside* a sync service function, twice per `ask` | `query_service.reason` | cannot be called from a running event loop; an async server must restructure this |

#### Performance boundaries measured or asserted by tests

| Property | Value | Source |
| --- | --- | --- |
| Scale-test shape | 10 and 50 repositories × 20 files, plus a shared provider repo | `test_scale.py:169` |
| Discovery depth | unbounded — `max_depth` was removed as always-wrong | `DiscoverySettings` docstring |
| No-change sync | must reparse **zero** files | `test_scale.py:128` |
| Retrieval cost | asserted not to scale with repository count | `test_scale.py:189` |
| Vector search | brute force; one SQL join then a NumPy dot product over the scoped set | `vectors.py:135-175` |
| Embedding | only chunk hashes with no vector for the model; batch 32 | `engine.embed_pending` |
| Global link | **reads every file row and every symbol on every sync**, regardless of scope | `prepare_global` |

Cheap enough for an interactive tool call: `status`, `repos`, `locate`, entity and edge lookups, dependency listing. Expensive enough that an agent should not call it in a loop: `sync` and `init` — both take the writer lock, and the global link pass is O(all files + all symbols) even for a single-repository sync. First `--embed` also pays a 67 MB model download.

## 16 · Existing reusable capabilities

Operations V1 already supports on its deterministic substrate. Nothing here is invented; every row names a real entry point.

| Capability | Implementation | Inputs | Outputs | Machine-friendly | LLM | Mutates | Reuse difficulty |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Evidence retrieval | `services.locate` → `Retriever.retrieve` | question, `RepositoryContext`, optional `RetrievalLimits` | `EvidenceSet` + `RetrievalTrace` | yes, frozen Pydantic | no | no | **none** — already a typed service |
| Intent-biased retrieval | `services.retrieve_for` | + `QueryIntent` | `EvidenceSet` | yes | no | no | none |
| Exact path / suffix / basename lookup | `SqliteSearchIndex.find_files_by_path` | names, repo scope, limit | `list[FileHit]` with `specificity` 0/1/2 | yes | no | no | low — store method, no service |
| Literal content search | `search_literal` | term, scope, limit | `list[ChunkHit]`, substring-verified | yes | no | no | low |
| Full-text recall | `search_text` | question, scope, limit | `list[ChunkHit]` | yes | no | no | low |
| Entity lookup by exact name | `find_entities_by_name` | names, scope, limit | `list[EntityHit]` with `entity_id` | yes | no | no | low |
| Entity full-text | `search_entities` | question, scope, type filter, limit | `list[EntityHit]` | yes | no | no | low |
| One-hop neighbours with repo ids | `SqliteKnowledgeStore.expand_neighbors` | entity ids, edge types, limit | `list[NeighborEdge]` | yes | no | no | low |
| Describe entities as citations | `describe_entities` | entity ids | `list[EntityDescription]` | yes | no | no | low |
| Semantic neighbours | `SqliteVectorIndex.query` | vector, model, scope, limit, floor | `list[VectorHit]` | yes | no | no | low; needs an embedding backend |
| Dependency inventory | `SqliteDependencyStore.list_packages` | repo ids or `None` | `list[DependencyPackage]` | yes, rich model | no | no | **trapped** — no CLI, no service |
| Who provides a package | `dependencies.providers` | ecosystem, normalized name | `list[(repo_id, name)]` | partly — raw ids | no | no | low |
| Repository metadata | `services.list_repositories` | none | `list[Repository]` | yes | no | no | none |
| Index status and drift | `services.status` | `RepositoryContext` | `StatusReport` | yes | no | no | none; performs a filesystem walk and a git call |
| Context resolution | `RepositoryContextResolver.resolve` | cwd, selector, command label | `RepositoryContext` | yes | no | no | none |
| Synchronize | `services.sync` | context, `full` | `SyncReport` + `LinkTrace` | yes | no | **yes** | none; needs the writer lock |
| Initialize a root | `services.initialize` | root path, optional config path | `(SyncReport, count)` | yes | no | **yes** | none; writes the config file too |
| Citation validation | `llm.grounding.ground_answer` | `RepoAtlasAnswer`, `EvidenceSet` | `GroundingReport` | yes | no | no | **none** — pure function, reusable as is |
| Evidence serialization | `llm.reasoner.format_evidence` | `EvidenceSet` | JSON string | yes | no | no | none |
| Grounded reasoning | `services.reason` | question, context, intent | `(EvidenceSet, ReasoningResult)` | yes | **yes** | no | none |

**Not present at any level:** file inspection by path (there is no "read me this file's indexed content" call — chunks are reachable only through a search stage), shortest path between entities, incoming-versus-outgoing edge direction as a caller-visible distinction, and any traversal from a *file* rather than from an entity id. **[NOT PRESENT IN V1]**

## 17 · Stable and unstable identifiers

| Identifier | Form | Survives sync | Survives rebuild | Survives relocation | Human-meaningful | Safe to expose |
| --- | --- | --- | --- | --- | --- | --- |
| `repositories.id` | AUTOINCREMENT | yes | **no** | yes | no | **no** |
| `repository.slug` | root-relative path | yes | **yes** | **no** — it is defined by position under the root | yes | **yes** |
| `repository.name` | basename | yes | yes | yes | yes | **no** — not unique |
| `repository.path` | absolute | yes | yes | no | yes | host-revealing |
| `files.id` | AUTOINCREMENT | **no** — reallocated on every reparse | no | no | no | **no** |
| file entity key | `file:<slug>:<rel_path>` | yes | **yes** | no (slug moves) | yes | **yes** |
| symbol entity key | `sym:<slug>:<path>:<qualified_name>:<line>` | only while the line does not move | yes, for the same content | no | yes | qualified — see below |
| `entities.id` | AUTOINCREMENT | **no** for file-scoped; yes for interned until GC | no | no | no | **no** — but it is exposed today |
| `env:`, `module:`, `cmd:`, `cfgkey:`, `pkg:` | natural keys | yes | **yes** | yes | yes | **yes** |
| `chunks.id` | AUTOINCREMENT | **no** | no | no | no | **no** |
| `chunks.content_hash` | blake2b-256 | **yes** for identical text | yes | yes | no | yes, as an opaque token |
| `files.content_hash` | blake2b-256 | yes | yes | yes | no | yes |
| `edges.id`, `unresolved_refs.id` | AUTOINCREMENT | **no** — global edges are deleted and rebuilt every run | no | no | no | **no** |
| Evidence block `id` (`E1`…) | positional within one prompt | **no** — per request | no | no | no | **no** |

> **There are no stable evidence identifiers in V1.** `EvidenceCandidate` has no id field. Its de facto identity is the tuple `(repository_slug, path, line_start, line_end)` — that is exactly what `_key()` in fusion, `identity` on the model, and `SuppliedEvidence.spans` in grounding all use. The only surrogate that leaks out is `entity_id`, a raw `entities.id`, and for a file-scoped entity that number changes on the next reparse of its file. **[VERIFIED]**

Two consequences worth carrying forward. The symbol key embeds a line number, so moving a function down a file changes its key — stable across a no-op sync, not across an edit. And the slug is a *position*, so moving a repository under the managed root is a change of identity by design; relocation rewrites the row, and every key built from the old slug is regenerated because the schema version participates in `facts_version`.

## 18 · Evidence granularity available today

| Unit | Retrievable today | Through what | Notes |
| --- | --- | --- | --- |
| File identity (path only, no lines) | **yes** | `find_files_by_path` → file-level `EvidenceCandidate` | a first-class shape: both line fields are `None` and grounding treats it as "the file exists" |
| Line span with text | **yes** | every chunk-producing stage | the default unit of evidence |
| Chunk as a addressable object | internal only | `chunks` table, `ChunkHit` | no id reaches the caller; text arrives inside a candidate |
| Entity (symbol, env var, package, config key) | **yes** | `find_entities_by_name`, `describe_entities` | `entity_id` is carried on the candidate |
| Edge as an object | internal only | `expand_neighbors` → `NeighborEdge` | never surfaces; expansion converts it to a *reason string* on the reached entity, e.g. `"reads (hop 1)"` |
| Graph path | **no** | — | only the hop number survives; the traversed edge chain is discarded |
| Dependency declaration | **yes**, internally | `list_packages` → `DependencyPackage.observations` | each observation carries role, version/constraint, file and line |
| Whole file content | **no** | — | no read-file capability exists at any layer |
| Repository summary | **yes** | `Repository`, `RepositoryStatus` | counts, languages, manifests, lockfiles, git state |

The smallest unit V1 can return *without new indexing work* is a line span with its text, and the smallest *addressable* unit is a file. Everything finer — a chunk by id, an edge by id, a path through the graph — exists in the database but has no service and no stable handle. **[VERIFIED]**

## 19 · Graph-query primitives

| Question | Status | Where |
| --- | --- | --- |
| Edges touching an entity, either direction | **implemented, reusable** | `KnowledgeStore.neighbors(entity_ids, edge_types, limit)` |
| One hop with both endpoints' repositories | **implemented, reusable** | `expand_neighbors(...)` → `NeighborEdge` with `crosses_repositories` |
| Edge-type filtering | **implemented** | parameter on both methods |
| Two hops | embedded only | `Retriever._expand` loops `expand_neighbors` and applies seeds, visited set, fan-out cap and cross-repository rule |
| Bounded traversal | embedded only | same method; bounds come from `RetrievalLimits`, not from the store |
| Cross-repository filtering | embedded only | `_CROSS_REPOSITORY_EDGES` is a module constant in `retriever.py`, applied in the loop |
| Direction as a caller-visible concept | **not present** | both store methods query `src_id IN (…) OR dst_id IN (…)`; `NeighborEdge.other()` exists but expansion ignores direction entirely |
| Shortest path | **not present** | — |
| Dependency graph as a traversal | partial | `DEPENDS_ON` edges exist and are traversable; the dependency *tables* are queried separately by `list_packages`, and the two are not joined for callers |
| Traversal from a file or a path | **not present** | seeds are entity ids obtained from a name lookup |

> **A finding worth flagging: `KnowledgeStore.neighbors()` has zero callers** in production and in tests. It is a fully implemented, typed, edge-type-filtered graph primitive that nothing uses — retrieval went to `expand_neighbors` instead because it needed endpoint repositories. Whatever V2 does, this method is either the seed of a graph capability or dead code, and it should not stay in its current state. **[VERIFIED]**

## 20 · Machine-readable surfaces

| Operation | `--json` | Typed model before rendering | What JSON actually emits |
| --- | --- | --- | --- |
| `init` | yes | `SyncReport` | the full report; the human path also prints root and count, which JSON omits |
| `sync` | yes | `SyncReport` (with `LinkTrace`) | the full report |
| `status` | yes | `StatusReport` (with `RepositoryStatus[]`) | the full report including drift lists and `index_state` strings |
| `repos` | yes | `list[Repository]` | a JSON array of repositories |
| `locate` | yes | `EvidenceSet` | question, scope, primary_repository, truncated, candidates[], trace |
| `ask` | yes | `ReasoningResult` | **only `result.answer`** — the evidence set, grounding report, repair flag and model name are dropped |
| `debug` | yes | `ReasoningResult` | same restriction |
| dependencies | — | `list[DependencyPackage]` | **no command exists** |

Every command already computes a frozen Pydantic model and hands it to a renderer; the renderer is never the only representation. That is the single most reusable property of the CLI layer. The two gaps are: `ask`/`debug` JSON discards the evidence the answer cites, and the dependency model has no command at all.

`EvidenceCandidate` in JSON carries `repository`, `repository_slug`, `path`, `line_start`, `line_end`, `text`, `score`, `source`, `tier`, `match`, `description` and `entity_id` — strictly more than the prompt serialization gets. `--verbose` additionally prints stage timings, candidate counts per stage, named and suffix paths, token estimate and grounding counters to stderr, all of which come from `RetrievalTrace` and `GroundingReport`; those objects are on the models and are simply not printed on the JSON path.

## 21 · Coupling matrix

**[none]** no dependency · **[light]** through an interface or one call site · **[strong]** direct, pervasive

| Subsystem | Filesystem | SQLite | Git | Embeddings | LLM | CLI rendering |
| --- | --- | --- | --- | --- | --- | --- |
| Repository discovery | **[strong]** | **[none]** | **[light]** | **[none]** | **[none]** | **[none]** |
| Scanner | **[strong]** | **[none]** | **[strong]** | **[none]** | **[none]** | **[none]** |
| Parsers | **[none]** | **[none]** | **[none]** | **[none]** | **[none]** | **[none]** |
| Dependency parsers | **[none]** | **[none]** | **[none]** | **[none]** | **[none]** | **[none]** |
| Linker | **[none]** | **[strong]** | **[none]** | **[none]** | **[none]** | **[none]** |
| Storage | **[light]** | **[strong]** | **[none]** | **[none]** | **[none]** | **[none]** |
| Retrieval | **[none]** | **[strong]** | **[none]** | **[light]** | **[none]** | **[none]** |
| Embeddings | **[light]** | **[none]** | **[none]** | **[strong]** | **[none]** | **[none]** |
| Grounding | **[none]** | **[none]** | **[none]** | **[none]** | **[light]** | **[none]** |
| Reasoning orchestration | **[none]** | **[none]** | **[none]** | **[none]** | **[strong]** | **[none]** |
| Ollama backend | **[none]** | **[none]** | **[none]** | **[none]** | **[strong]** | **[none]** |
| Sync engine | **[strong]** | **[strong]** | **[strong]** | **[light]** | **[none]** | **[none]** |
| Services | **[light]** | **[strong]** | **[light]** | **[light]** | **[light]** | **[none]** |
| CLI | **[light]** | **[none]** | **[none]** | **[none]** | **[light]** | **[strong]** |

#### The surprising entries

- **Grounding is light on LLM, not strong.** It imports `RepoAtlasAnswer`, `Hypothesis` and `Evidence` from `models/`, not from `llm/`. Its physical location under `llm/` overstates its coupling; the module would compile and pass its tests with the backend deleted.
- **Retrieval is light on embeddings, not strong.** It holds an optional `EmbeddingModel | None` and short-circuits on a zero vector count before the model is ever loaded.
- **Services is light on LLM.** One import in one module, one call site, and the check happens before any retrieval work is done.
- **The CLI is the only subsystem strongly coupled to rendering**, and rendering is forbidden from importing anything that queries.

> **Does a clean "tool server without a local LLM" boundary already exist?** Structurally, yes — and it is unusually clean. Cut the graph between `services/` and `llm/` and everything below stays whole: discovery, scanning, parsing, linking, storage, retrieval, embeddings and grounding all keep working, and the layering test already forbids the reverse edges. What does *not* exist is a boundary in the other direction: there is no callable surface between the CLI and the services, so a server would be adding a new front, not extracting an old one. **[INFERRED]** from the enforced import map.

## 22 · Sequence diagrams

#### A. `repoatlas sync`

```
CLI          services      Workspace     lock      SyncEngine    scanner/parsers   stores        linker
 │ sync()
 ├─ _open() ───────────────▶ open_workspace
 │                            └─ open_database ──────────────────────────────────▶ ownership,
 │                                                                                  WAL, migrate
 ├─ _resolve() ──▶ resolver.resolve(cwd) ─────────────────────────────────────────▶ list_all
 ├─ services.sync(ws, ctx, full)
 │       ├──────────────────▶ writer_lock() ──▶ flock (30 s bound)
 │       ├─ plan_repository_set(root) ────────────────────────▶ filesystem walk
 │       ├─ TX: relocate + register ─────────────────────────────────────────────▶ repositories
 │       └─ SyncEngine.run(repos, full, scope, embed_ids)
 │              │ per repository
 │              ├─ ensure_repository_entity ─────────────────────────────────────▶ intern
 │              ├─ git.state ────────────▶ subprocess git
 │              ├─ scan_repository ──────▶ git ls-files ─▶ secret/exclude/ignore/
 │              │                                          nested/containment
 │              ├─ mark_sync_started ────────────────────────────────────────────▶ 'interrupted'
 │              ├─ plan_sync ────────────▶ hash_within (only where needed)
 │              ├─ delete_many(deleted) ─────────────────────────────────────────▶ cascade
 │              │ per changed file
 │              ├─ read_content ─────────▶ boundary
 │              ├─ parse / deps / chunk ─▶ PURE, no DB
 │              └─ TX ───────────────────────────────────────────────────────────▶ upsert file
 │                                                                                  replace facts
 │                                                                       ─────────▶ link_file
 │                                                                                  replace deps
 │              ├─ gc_orphan_entities, update_detected_metadata
 │              ├─ prepare_global ───────────────────────────────────────────────▶ read all rows
 │              ├─ TX: commit_global + mark_many_synced ─────────────────────────▶ 'ok'
 │              └─ embed_pending ────────▶ embeddings backend (may fail benignly)
 ├─ render_sync(report) → stdout
 └─ exit 1 if report.unavailable else 0
```

#### B. `repoatlas locate`

```
CLI        services     Retriever      query.py   SearchIndex   VectorIndex   KnowledgeStore  ranking
 │ locate(q)
 ├─ _open ─────────────▶ open_workspace (no lock taken)
 ├─ _resolve ─────────▶ RepositoryContext
 └─ services.locate ──▶ retrieve(q, ctx)
        ├──────────────────────────▶ analyze(q) → literal / definite terms
        ├─ _stage_exact ─────────────────────────▶ search_literal    DET / EXACT_CONTENT
        ├─ _stage_files ─────────────────────────▶ find_files_by_path  → proven terms
        ├─ _suppress_associative(definite, exact, proven)
        ├─ _stage_lexical ───────────────────────▶ search_text       ASSOC / LEXICAL
        ├─ _stage_symbols ───────────────────────▶ find_entities_by_name → SEEDS
        │                                          search_entities
        ├─ _stage_semantic ──────────────────────────────────▶ count → embed_query → query
        ├─ _stage_graph ─────────────────────────────────────────────────▶ expand_neighbors ×hops
        │                                                                  describe_entities
        ├────────────────────────────────────────────────────────────────────────▶ fuse
        ├────────────────────────────────────────────────────────────────────────▶ deduplicate
        ├────────────────────────────────────────────────────────────────────────▶ rerank →
        │                                                                           enforce_precedence
        └────────────────────────────────────────────────────────────────────────▶ apply_budget
 └─ render_evidence(EvidenceSet) → stdout        [--json → model_dump_json]
```

#### C. `repoatlas ask`

```
CLI      query_service   factory   OllamaBackend   Retriever   Reasoner   prompts   grounding
 │ ask(q)
 ├─ _open, _resolve
 └─ services.reason(ws, q, ctx, ASK)
        ├─ build_model(settings.llm) ──▶ OllamaBackend(base_url, model)
        ├─ available() ────────────────▶ GET /api/tags
        │      false → ReasoningModelUnavailableError → exit 4   (no retrieval done)
        ├─ retrieve_for(...) ─────────────────────────▶ full funnel of diagram B
        └─ Reasoner.reason(q, evidence, ASK)
               ├─ build_messages ──────────────────────────────▶ system.md + ask.md
               │      format_evidence → {"evidence":[{id,repository,repository_path,
               │                          path,start_line,end_line,why_retrieved,
               │                          content,content_truncated}]}
               ├─ complete(response_schema=RepoAtlasAnswer) ──▶ POST /api/chat
               │                                                 format = JSON schema
               │                                                 temperature 0.0
               ├─ model_validate_json
               │      failure → repair turn (assistant echo + repair.md) → retry once
               │      failure → INSUFFICIENT EVIDENCE with both problems named
               └─ ground_answer(answer, evidence) ─────────────────────────▶ drop / clamp /
                                                                             demote / count
 └─ render_answer(result.answer, evidence)     [--json → answer only]

ask --no-llm  ── branches at services.retrieve_for, before build_model is reached;
                 output is render_evidence, i.e. byte-identical to locate.
```

#### D. One relationship, end to end

```
python-service/src/config.py       scripts/deploy.sh
  os.environ["SERVICE_HOST"]         export SERVICE_HOST="http://…"
        │                                   │
   PythonParser.parse                  BashParser.parse
        │ ParsedReference(READS,            │ ParsedReference(EXPORTS,
        │   ENV_VAR, "SERVICE_HOST")        │   ENV_VAR, "SERVICE_HOST")
        ▼                                   ▼
   link_file: target_kind in _INTERNED_KINDS
        └─ intern_entity(key="env:SERVICE_HOST")  ← get-or-create, ONE row
        └─ add_edges(READS  file→env)        └─ add_edges(EXPORTS file→env)
                                    (both inside their own per-file transactions)

   retrieval, asked "where is SERVICE_HOST configured?" from inside python-service
        analyze → env_vars = ("SERVICE_HOST",)          ← definite term
        _stage_symbols → find_entities_by_name(["SERVICE_HOST"])
              → entity_id of env:SERVICE_HOST           ← becomes a GRAPH SEED
        _expand hop 1 → READS/EXPORTS edges to both files
        _expand hop 2 → whatever those files touch
        describe_entities → candidates, DETERMINISTIC / RELATED,
                            description "exports (hop 1)"
        fuse: primary_boost lifts python-service within the tier; the deploy script
              is still returned, because the tier is the same and the edge is a fact.
```

## 23 · Tests that define architectural contracts

These are the suites a V2 refactor must keep green. Counts are files, not cases; the adversarial directory alone holds 2,521 selected cases.

| Contract | Suites |
| --- | --- |
| Layering, purity, no god object, SQL confinement, git confinement | `tests/test_layering.py` |
| Trust rule end to end; answer trust model | `test_answer_trust_model.py`, `test_trust_boundary.py`, `test_unanswerable.py` |
| Grounding and citation verification | `test_answer_trust_model.py`, `tests/test_llm.py`, `test_hostile_content.py` |
| Filesystem containment | `test_filesystem_containment.py`, `test_bounded_file_reads.py`, `test_scanner_unbounded_depth.py` |
| Crash recovery and sync atomicity | `test_interrupted_sync_recovery.py`, `test_sync_atomicity.py`, `test_failure_recovery.py` |
| No stale facts | `tests/test_sync.py`, `test_stale_torture.py`, `tests/test_stale_index.py` |
| Git fail-closed enumeration | `test_git_enumeration_refusal.py`, `test_git_execution_boundary.py` |
| Secret exclusion and ignore semantics | `test_secret_exclusion_integrity.py`, `test_repoatlasignore_case_semantics.py`, `tests/test_secrets.py` |
| Retrieval precedence and exactness | `test_exact_path_ranking.py`, `test_suffix_path_ranking.py`, `test_exact_retrieval_integrity.py`, `test_unicode_exact_retrieval.py`, `tests/test_retrieval_ranking.py` |
| Evidence tiers and graph truth | `test_graph_truth.py`, `test_tier_integrity.py` |
| SQLite ownership, migrations, concurrency | `test_database_identity.py`, `test_migration_atomicity.py`, `test_first_open_contention.py`, `test_unusable_index_errors.py` |
| Repository identity, relocation, reconciliation | `test_repository_identity.py`, `test_repository_root_relocation.py`, `test_repository_set_reconciliation.py`, `test_recursive_repository_discovery.py` |
| Context resolution | `test_context_torture.py`, `tests/test_context.py` |
| Docker namespace and target integrity | `test_dockerfile_copy_namespace.py`, `test_dockerfile_target_integrity.py` |
| Bash execution targets and tilde handling | `test_bash_execution_integrity.py`, `test_bash_tilde_targets.py`, `test_bash_invocation.py` |
| Python import semantics | `test_python_relative_submodules.py`, `tests/test_parsers_code.py`, `tests/test_linking.py` |
| Salt and provenance integrity | `test_salt_jinja_torture.py`, `test_salt_provenance_integrity.py`, `test_provenance_eof_integrity.py`, `test_chunk_bounds.py` |
| Dependency integrity | `test_dependency_integrity.py`, `tests/test_dependencies.py` |
| Ollama protocol and diagnostics | `test_ollama_tags_protocol.py`, `test_ollama_failure_diagnostics.py`, `tests/test_ollama.py` |
| Retrieval quality as a corpus | `tests/evaluation/` — 47 cases, asserts **0 forbidden-evidence results and 0 tier violations** |
| Scale and performance | `test_scale.py`, `tests/test_performance.py` |

The evaluation harness is the one that most directly encodes retrieval *quality* rather than mechanics, and it runs against both the hashing and fastembed backends. A V2 that changes ranking must keep `forbid` and `tier` at zero in both.

## 24 · Assumptions V2 will break

Each of these is visible in code, not supposed.

| Assumption | Evidence in V1 | What a persistent external agent does to it |
| --- | --- | --- |
| **One question, one bundle.** | `Retriever.retrieve` returns a complete `EvidenceSet`; there is no cursor, no "more", no follow-up call | an agent will want to widen, narrow or drill into a result it already has |
| **No investigation state.** | nothing persists between commands except the index; `Workspace` dies with the process | a multi-turn investigation has no place to record what has already been examined |
| **Exactly one model call, plus at most one repair.** | `Reasoner.reason` is linear; `ToolSpec` exists but "V1 issues no tool calls" | an external agent *is* the loop, and will call back repeatedly |
| **The evidence set is consumed immediately.** | grounding needs the same `EvidenceSet` object that was serialized; nothing stores it | validating a later answer requires re-supplying or re-deriving the exact set |
| **Evidence has no durable identity.** | `E1…En` are positional per request; `entity_id` is a mutable rowid | an agent cannot say "give me more around evidence 3" across calls |
| **One current repository context.** | `RepositoryContext` is derived from `Path.cwd()` at command start | a server has no cwd; context must be an explicit parameter of every call |
| **The token budget is decided internally.** | `apply_budget` uses `RetrievalLimits` from settings; no caller passes a budget per query | the agent, not RepoAtlas, knows its own remaining context window |
| **Ranking assumes a complete natural-language question.** | `analyze()` runs stopword filtering, prose-shape heuristics and a "lone token is a lookup" rule | an agent issuing `SERVICE_HOST` or `src/config.py` as a bare probe hits different code paths than a sentence |
| **The model never asks for more evidence.** | no tool-call path; `next_check` is prose, not a request | progressive disclosure has no mechanism to attach to |
| **Output is for a terminal.** | seven renderers; `--json` exists but `ask`/`debug` drop the evidence from it | a machine consumer needs the answer *and* what it cites in one payload |
| **Freshness is the operator's problem.** | "V1 may require an explicit `repoatlas sync`"; `locate` carries no staleness marker | an agent citing a stale index has no signal in the payload that it is stale |
| **Errors are exit codes.** | rich `ModelFailure` categories collapse to exit 4 | a protocol wants the category, not the number |

## 25 · Reusable trust mechanisms

Where each belongs relative to a future client boundary, and why.

| Mechanism | Side | Reason |
| --- | --- | --- |
| Repository containment (`RepositoryBoundary`) | **below** | decides from file descriptors, not paths; a client that could bypass it could read the host |
| Git fail-closed enumeration | **below** | the guarantee is "the set indexed is the set reported"; a client cannot re-establish it |
| Secret filename exclusion | **below** | applies before any read; must be unreachable and unconfigurable from outside |
| Database identity and refusal | **below** | protects a file the client does not know about |
| Fact provenance on every row | **below** | it is what makes a citation checkable at all |
| Parser static-proof rules (no execution, no I/O) | **below** | enforced by a layering test; a client cannot re-impose it |
| Evidence tiers and match classes | **below**, exposed as data | the ordering guarantee must be produced by RepoAtlas; the values may be shown to the client so it can reason about strength |
| Graph confidence | below, currently near-inert | stored and used only for expansion ordering; expose only if it gains meaning |
| Citation verification (`ground_answer`) | **below, but newly at the edge** | pure function today; in V2 it is the only thing standing between an external agent's prose and a trusted citation, so it must run *after* the agent answers, not before |
| Stale / current index state | **below, must become visible** | `status` computes it correctly; today no retrieval payload carries it |
| Structured domain errors | **below** | already model-level; the client should see categories, not exit codes |
| Writer lock and one-writer invariant | **below** | a client must not be able to start two syncs |
| Prompt construction and repair | **above** | belongs to whoever owns the model |
| Token budgeting | **contested** | the item cap protects RepoAtlas's own work; the token cap models a context window RepoAtlas no longer owns |

## 26 · Dangerous abstractions

Things V1 has that would be tempting to expose and should not be, each with the coupling it would create.

| Abstraction | Why exposing it couples V2 to implementation |
| --- | --- |
| `entities.id` — and it is **already exposed** as `EvidenceCandidate.entity_id` in `--json` | a file-scoped entity's row id is reallocated on every reparse of its file, and interned ids vanish at GC. A client that stored one and came back would address a different fact or nothing. |
| `files.id`, `chunks.id`, `edges.id`, `unresolved_refs.id` | all AUTOINCREMENT, all reallocated by ordinary sync; global edge ids change on every run by design |
| FTS rowids | they are chunk ids by another name, with the same lifetime |
| Evidence block ids `E1…En` | positional within one serialization; they mean nothing outside the request that produced them |
| The fused `score` float | a reciprocal-rank sum plus a boost. It is not calibrated, not comparable between queries, and changes whenever a stage's candidate count changes. Clients would inevitably threshold on it. |
| Raw store objects (`SqliteKnowledgeStore` and friends) | they take and return SQL-shaped rows and assume a single connection with reentrant transaction depth; they are not a concurrency-safe API |
| `Workspace` as a handle | a mutable dataclass holding a live connection, a lazily-built embedding model and a settings object that `sync --embed` rewrites in place |
| Ollama response types and `ModelFailure` spellings | `done_reason == "length"`, `/api/tags` body shape — one provider's protocol, and V2's client is not that provider |
| CLI-formatted strings from `rendering/text.py` | terminal layout with wrapping, alignment and conditional repository prefixes; parsing them back is a contract nobody wrote down |
| `index_state` strings such as `"stale (indexed by an older build)"` | human sentences doing the work of an enum. They are produced by `_index_state()` and compared with `startswith("stale")` inside `status` itself, which is the tell. |
| `parser_version` composite string | an internal cache key. Its format is `"schema:5\|\|name:v\|…\|\|deps:…\|\|chunks:N"` and it changes whenever any parser version does. |
| `RetrievalLimits` as a client-tunable object | the bounds interact — `graph_seeds` with `per_source`, budget with `enforce_precedence` — so exposing all nine invites configurations that quietly break the precedence guarantee |

## 27 · Missing service boundaries and extraction seams

Deterministic functionality that exists and is hard to reach. No implementation proposed — only the seam.

| Trapped capability | Current location | Why it is hard to reuse | Conceptual seam | Must stay below it | Regression suites |
| --- | --- | --- | --- | --- | --- |
| Dependency inventory and dependency questions | `SqliteDependencyStore.list_packages` + `render_dependencies` | no CLI command, no service function; only tests call it | a dependency query service beside `status_service` | normalization rules, ecosystem identity, provenance columns | `test_dependency_integrity.py`, `tests/test_dependencies.py` |
| Bounded graph traversal | `Retriever._expand`, 45 lines inside the funnel | seeds, hop budget, fan-out cap and the cross-repository rule are interleaved with candidate construction; `_CROSS_REPOSITORY_EDGES` is a retrieval-module constant | a traversal primitive that takes seeds and bounds and returns edges plus reached entities, with the retriever as its first caller | edge-type exclusions, cross-repository rule, `expand_neighbors` | `test_graph_truth.py`, `test_tier_integrity.py` |
| Evidence budgeting | `apply_budget`, called once at the end of `retrieve` | the function is already pure and separable; what is fused is the *decision* that a caller cannot choose its own budget | budget as a per-call parameter rather than a settings-derived constant | `enforce_precedence` must still run before it | `test_exact_path_ranking.py`, evaluation harness |
| Retrieval stages as individually callable probes | six private `_stage_*` methods | each takes a `stages` list to append timings to and a shared `scopes` list; suppression is computed between stages two and three | a search primitive per stage returning candidates, with the funnel composing them | the tier and match-class assignments, which are made inside the stages | `test_exact_retrieval_integrity.py`, `test_suffix_path_ranking.py` |
| Index freshness as a value | `status_service._index_state()`, returns a sentence | private function, string-typed, and `status` re-parses its own output with `startswith("stale")` | a freshness enum computed once, rendered separately | `facts_version`, `git.state`, `count_outdated` | `tests/test_stale_index.py`, `test_stale_torture.py` |
| Repository-set drift detection | `status_service._drift()` | private; performs a filesystem walk on every `status` call at every scope | a workspace-drift query separate from per-repository status | `plan_repository_set`, which must stay read-only here | `test_repository_set_reconciliation.py` |
| Answer validation for a non-local model | `ground_answer` under `llm/` | the function is already pure; its *package* implies it belongs to the local backend, and it requires the caller to still hold the exact `EvidenceSet` | grounding as a first-class verification service; evidence identity durable enough to re-supply | `SuppliedEvidence` identity rules, slug-keyed spans | `test_answer_trust_model.py`, `test_trust_boundary.py` |
| Evidence serialization | `reasoner.format_evidence` | lives beside prompt construction and is shaped for one prompt; drops tier, match, score and entity id | an evidence wire format decided independently of who reads it | the JSON-as-data rule that defeats delimiter forgery | `test_hostile_content.py` |
| Context resolution without a cwd | `RepositoryContextResolver.resolve(cwd, …)` | cwd is a required positional; the CLI passes `Path.cwd()` | explicit-context construction as the primary path, cwd inference as one adapter | the five-step precedence and its refusals | `test_context_torture.py` |
| Structured failure categories | `ModelFailure` on `ModelError` | flattened to exit 4 by `query_service`; nothing downstream reads the category | carry the category through the result, not only the exception | the layered diagnosis text | `test_ollama_failure_diagnostics.py` |
| Sync contention classification | absent — `_contention_reported` wraps opening only | an external lock holder makes an ordinary sync raise a bare `OperationalError` | extend contention classification to write boundaries | `is_contention` result-code matching, already written | `test_first_open_contention.py`, `test_unusable_index_errors.py` |

## 28 · Components that should probably remain untouched

| Component | Technical reason to leave it alone |
| --- | --- |
| `parsers/` and the `LanguageParser` protocol | already a pure function of `(path, content)` with zero I/O, enforced by test. Adding the eighth parser required no spine change. There is nothing to extract because nothing is trapped. |
| `dependencies/` and its registry | same purity, plus a stricter import ban. `setup.py` is parsed by AST and never imported, which is the case that makes the rule load-bearing. |
| `containment.py` | 514 lines that decide from descriptors rather than paths, defeating ancestor-swap races. Rewriting this is how containment bugs are introduced. |
| `security.py` | one module, one question, deliberately placed below every caller so there is exactly one implementation of the secret policy. |
| `sync/lock.py` | the `flock`-on-an-open-file-description design has no stale state and needs no recovery code. Every alternative needs a liveness heuristic, and every heuristic is wrong somewhere. |
| `storage/sqlite/database.py` open path | ownership before version check before any write, with a measured retry loop for the one promotion `busy_timeout` cannot cover. The ordering is the correctness. |
| `graph/keys.py` | seven small functions that are the reason cross-file joins are exact. Changing a key format invalidates every stored fact. |
| `models/` | frozen Pydantic, imports nothing internal, already the machine-readable layer. It is the natural wire format, not an obstacle to one. |
| `llm/grounding.py` logic | the algorithm is correct and adversarially tested. What may need to move is *where it sits* and *when it runs*, not what it does. |
| The two-phase linker | prepare-then-swap exists because the one-phase version published an empty graph on failure. Any restructuring must preserve both the atomicity and the idempotence. |
| The per-file transaction | the commit is the completeness marker for the next run's planner. Splitting it reintroduces a permanently-skipped file. |

## 29 · Compatibility obligations

| Surface | What breaks if changed |
| --- | --- |
| CLI command names and arguments | the console script is `repoatlas = repoatlas.cli.main:run`. Scripts and the container `HEALTHCHECK` (`repoatlas status --json`) depend on the current names. |
| Exit codes 0–5 | documented in V1_SPEC §4 and asserted by tests. Code 5 exists specifically so a wrapper can retry contention; collapsing it removes that distinction. |
| `--json` shapes | they are Pydantic `model_dump_json` of public models. Renaming a field on `EvidenceCandidate`, `StatusReport` or `SyncReport` is a wire break. |
| Database file and `application_id` | an existing `atlas.db` is a user asset. Migrations are forward-only and there is no downgrade; a schema bump makes the index unreadable by older builds, which is intentional and must stay explicit. |
| Schema version 5 and the migration chain | five migrations must continue to apply in order to reach the current shape from any earlier one. |
| Parser versions | bumping any of the twenty parser versions forces a reparse of every affected file on the next sync. That is correct behaviour, but it is a cost the operator pays, and `status` announces it as `stale (indexed by an older build)`. |
| Entity key formats | keys are stored, not derived at read time. Changing `file:`, `sym:`, `env:` or `pkg:` spelling requires a schema bump so every fact is rebuilt. |
| Config file location and keys | `$XDG_CONFIG_HOME/repoatlas/config.yaml`, written by `init`. Unknown keys would fail Pydantic validation on load. |
| `REPOATLAS_CONFIG`, `REPOATLAS_DB`, `REPOATLAS_LLM_BASE_URL`, `REPOATLAS_SYNC_LOCK_TIMEOUT` | the container image sets the first three; compose sets the third. |
| `ask` and `debug` existing at all | removing them breaks anyone whose workflow is "one command, grounded answer, no agent". Their `--no-llm` mode is currently the documented escape hatch when no model is installed, and it is what the exit-4 message points at. |
| `--no-llm` | referenced in README, in the exit-4 diagnostic and in tests. It is the only flag that turns a reasoning command into a retrieval command. |
| Docker image contract | entrypoint `repoatlas`, `/repos` read-only, `/state` writable, non-root user, healthcheck on `status --json`. |

What changing `ask` would break is stated above; whether it should change is not a question this study answers.

## 30 · Top V2 constraints implied by V1

Fourteen statements, each derived from a fact established above. None is generic advice.

1. **There is no callable surface between the CLI and the services, so V2 adds a front rather than extracting one.** `repoatlas.services` exports five functions whose only caller is `cli/main.py`; every one takes a `Workspace` built by `open_workspace()` and most take a `RepositoryContext` built from `Path.cwd()`.
2. **Retrieval owns candidate generation, graph expansion, fusion, dedup and budgeting in one method, so exposing traversal independently requires extracting `_expand` and its two module constants from `retriever.py`.** The cross-repository edge rule lives in the retrieval module, not in the store.
3. **Evidence has no durable identity, so progressive disclosure cannot be built on references to a previous result without inventing one.** The only stable handle in an `EvidenceCandidate` today is the tuple `(repository_slug, path, line_start, line_end)`; `entity_id` is a rowid that a reparse reallocates.
4. **Grounding requires the exact `EvidenceSet` that was supplied, so a stateless protocol must either round-trip the evidence or make the set re-derivable.** `SuppliedEvidence` is built from the candidates themselves, keyed by slug and path, with the supplied spans as the only allowed line ranges.
5. **Entity keys are stable across incremental sync and across a fresh rebuild of the same tree, but not across repository relocation, because the slug is a position under the managed root.** Any client-visible identifier should be built from `slug` and `rel_path`, never from a row id.
6. **The symbol key embeds a line number, so a symbol's identity changes when the definition moves.** A V2 handle for "this function" must be derived from name and file, or accept that it is a snapshot.
7. **Context resolution is always done by the caller and never by retrieval, which is why an explicit-context API is a small change and a cwd-free one is already almost true.** `Retriever.retrieve` reads only `repository_ids`, `primary.slug` and `scope` from the context it is handed.
8. **The trust guarantee is enforced by ordering inside `fuse()` and re-imposed by `enforce_precedence()` after the reranker seam, so any V2 component that reorders candidates must run before that call or it silently defeats the rule.** The seam exists precisely because a future cross-encoder is expected to be opaque.
9. **Suppression is a whole-funnel decision made once, between the file stage and the lexical stage, so a per-stage API would have to decide where that judgement lives.** "None of the named terms exist" currently silences lexical, symbol-FTS and semantic retrieval together.
10. **The token budget models a context window RepoAtlas will no longer own.** `apply_budget` enforces 12 items and 6000 estimated tokens from settings; an external agent knows its own remaining budget and RepoAtlas does not.
11. **A single-repository sync still rebuilds the whole global graph and reads every file row and symbol, so sync is not a cheap per-call operation at any scope.** `prepare_global` is unconditional in `SyncEngine.run`.
12. **One writer per state database, enforced by an `flock` with a 30-second bound, so a server must serialize all mutating calls and surface exit-5-equivalent contention rather than queueing indefinitely.**
13. **`asyncio.run()` is called inside synchronous service functions, twice per `ask`, so an async server cannot call `services.reason` from a running event loop without restructuring it.**
14. **Freshness is computed by `status` and carried in no retrieval payload, so an agent citing an index built by an older parser build has no signal in the response.** `_index_state()` knows five distinct states and returns them as English sentences.
15. **The `llm/` package is already unreachable from every deterministic subsystem, enforced by a test, so removing the local reasoning backend is a subtraction rather than a refactor.** What is not solved is what `ask`, `debug`, `--no-llm` and exit 4 should mean afterwards.

## 31 · Open design questions

Decisions the V1 code cannot settle. Listed, not answered.

1. **Does server state persist between calls?** V1 has none. If it does, what is its lifetime, and does an investigation become a first-class object?
2. **Do evidence items need durable identifiers?** If yes, derived from what — content hash, slug plus span, or a new scheme — and stable across which operations?
3. **Is the evidence set round-tripped for grounding, or re-derived?** This decides whether validation can be stateless.
4. **Who owns the budget?** If the agent does, does RepoAtlas keep a hard ceiling anyway, and is the item cap separable from the token cap?
5. **Does `ask` remain?** And if it does, is it the same command, a compatibility shim, or the reference implementation of the answer contract?
6. **Is synchronization automatic, on demand, or refused?** V1 requires an explicit sync and says so; a long-running server invites a different answer.
7. **Should retrieval expose stages individually, or only the composed funnel?** Individual stages give the agent control and give up the suppression and precedence guarantees that are currently made across stages.
8. **What is the transport?** Nothing in V1 constrains it. The only related fact is that the container is interactive rather than a daemon.
9. **Is one workspace per process still the model?** Multi-root, multi-database or per-request workspaces each change the connection and lock story.
10. **Does the client see tiers and match classes?** Exposing them lets an agent reason about evidence strength; it also makes them a contract.
11. **Should freshness be part of every response?** And does a stale index refuse, warn, or answer silently as it does now?
12. **What happens to the seven renderers?** They are the only consumers of several models, and they encode judgements — conditional repository prefixes, ambiguity disambiguation — that a machine format would need to re-decide.

## 32 · File / function index

| Concept | File | Class / function |
| --- | --- | --- |
| CLI application, 7 commands | `repoatlas/cli/main.py` | `app`, `init`, `sync`, `status`, `repos`, `locate`, `ask`, `debug`, `_reasoning_command`, `_resolve`, `_fail`, `run` |
| Workspace wiring | `repoatlas/services/workspace.py` | `Workspace`, `open_workspace`, `Workspace.retriever`, `sync_engine`, `writer_lock`, `embeddings`, `resolver` |
| Application services | `repoatlas/services/` | `initialize`, `sync`, `status`, `list_repositories`, `locate`, `retrieve_for`, `reason` |
| Context precedence | `repoatlas/context/resolver.py` | `RepositoryContextResolver.resolve`, `_resolve_explicit` |
| Repository identity | `repoatlas/models/repository.py` | `repository_slug`, `Repository`, `SyncStatus`, `FileState` |
| Discovery and reconciliation | `repoatlas/discovery/` | `finder`, `plan_repository_set`, `register`, `IgnoreRules`, `detect_language` |
| Secret policy | `repoatlas/security.py` | `is_secret_path`, `SECRET_PATTERNS` |
| Containment | `repoatlas/containment.py` | `RepositoryBoundary.stat`, `read_content`, `read_into`, `ContentStatus`, `FileContent` |
| Enumeration | `repoatlas/sync/scanner.py` | `scan_repository`, `EnumerationUnavailable`, `hash_within`, `hash_bytes` |
| Git wrapper | `repoatlas/gitstate/client.py` | `GitClient.state`, `list_files`, `git_toplevel` |
| Sync planning | `repoatlas/sync/planner.py` | `facts_version`, `decide_rehash`, `plan_sync`, `SyncPlan` |
| Sync execution | `repoatlas/sync/engine.py` | `SyncEngine.run`, `sync_repository`, `_index_file`, `_index_oversized`, `embed_pending`, `ensure_repository_entity`, `forget_if_missing` |
| Writer lock | `repoatlas/sync/lock.py` | `writer_lock`, `lock_path`, `DEFAULT_TIMEOUT_SECONDS` |
| Parser contract | `repoatlas/parsers/base.py`, `registry.py` | `LanguageParser`, `ParserRegistry.for_path`, `ParserRegistry.version`, `default_parsers` |
| Parser output | `repoatlas/models/parsing.py` | `ParsedFile`, `ParsedSymbol`, `ParsedReference`, `EntityType`, `EdgeType`, `ParseDiagnostic` |
| Entity keys | `repoatlas/graph/keys.py` | `repository_key`, `file_key`, `symbol_key`, `env_key`, `module_key`, `package_key`, `command_key`, `config_key`, `global_key` |
| Linking | `repoatlas/graph/linker.py` | `ReferenceLinker.link_file`, `prepare_global`, `commit_global`, `_resolve_module`, `_plan_dependencies`, `GLOBAL_PARSER` |
| Resolution index | `repoatlas/graph/resolution.py` | `ResolutionIndex.build`, `resolve_repo_relative_path`, `resolve_package_submodule` |
| Chunking | `repoatlas/indexing/chunking.py` | `chunk_content`, `CHUNKING_VERSION` |
| Storage protocols | `repoatlas/storage/protocols.py` | `RepositoryStore`, `FileStore`, `KnowledgeStore`, `DependencyStore`, `VectorIndex`, `SearchIndex`, and the record models |
| Database lifecycle | `repoatlas/storage/sqlite/database.py` | `open_database`, `Database.transaction`, `_ensure_wal`, `_contention_reported`, `SCHEMA_VERSION`, `BUSY_TIMEOUT_MS` |
| Ownership and migrations | `repoatlas/storage/sqlite/identity.py`, `migrator.py` | `establish`, `Ownership`, `APPLICATION_ID`, `apply_migrations`, `user_version` |
| Stores | `repoatlas/storage/sqlite/stores.py` | `SqliteRepositoryStore`, `SqliteFileStore`, `SqliteKnowledgeStore`, `SqliteDependencyStore`, `expand_neighbors`, `neighbors`, `describe_entities`, `list_packages` |
| Search index | `repoatlas/storage/sqlite/search.py` | `SqliteSearchIndex.search_literal`, `search_text`, `find_files_by_path`, `find_entities_by_name`, `search_entities` |
| Vectors | `repoatlas/storage/sqlite/vectors.py` | `SqliteVectorIndex.query`, `missing_hashes`, `upsert_many`, `gc_orphan_vectors` |
| Query analysis | `repoatlas/retrieval/query.py` | `analyze`, `QueryAnalysis.literal_terms`, `definite_terms` |
| Retrieval funnel | `repoatlas/retrieval/retriever.py` | `Retriever.retrieve`, `_stage_exact`, `_stage_files`, `_stage_lexical`, `_stage_symbols`, `_stage_semantic`, `_stage_graph`, `_expand`, `_suppress_associative`, `RetrievalLimits`, `_EXPANSION_EDGES`, `_CROSS_REPOSITORY_EDGES` |
| Ranking | `repoatlas/retrieval/ranking.py` | `fuse`, `_anchored_files`, `enforce_precedence`, `deduplicate`, `apply_budget`, `Reranker`, `NoopReranker` |
| Evidence models | `repoatlas/models/evidence.py` | `EvidenceTier`, `MatchClass`, `EvidenceSource`, `EvidenceCandidate`, `EvidenceSet`, `Evidence`, `Hypothesis`, `RepoAtlasAnswer` |
| Embeddings | `repoatlas/embeddings/` | `EmbeddingModel`, `build_embedding_model`, `FastEmbedModel`, `HashingEmbedding`, `EmbeddingUnavailable` |
| Reasoning | `repoatlas/llm/reasoner.py` | `Reasoner.reason`, `build_messages`, `format_evidence`, `QueryIntent`, `ReasoningResult` |
| Model interface and backends | `repoatlas/llm/` | `ReasoningModel`, `ModelFailure`, `ModelResponse`, `build_model`, `OllamaBackend`, `NullBackend`, `ScriptedBackend` |
| Grounding | `repoatlas/llm/grounding.py` | `ground_answer`, `GroundingReport`, `SuppliedEvidence`, `_verify_evidence`, `_resolve_source`, `_unsupported_references` |
| Prompts | `repoatlas/llm/prompts/` | `system.md`, `ask.md`, `debug.md`, `repair.md`, `loader.render` |
| Rendering | `repoatlas/rendering/text.py` | `render_repos`, `render_status`, `render_sync`, `render_dependencies`, `render_evidence`, `render_answer`, `render_diagnosis` |
| Errors and exit codes | `repoatlas/errors.py` | `ExitCode`, `RepoAtlasError` and 14 subclasses |
| Configuration | `repoatlas/config/settings.py` | `Settings`, `load_settings`, `save_settings`, `config_path`, `database_path`, `DEFAULT_EXCLUSIONS`, `SECRET_PATTERNS` |
| Telemetry | `repoatlas/models/telemetry.py` | `RetrievalTrace`, `LinkTrace`, `StageTiming`, `Stopwatch` |
| Layering enforcement | `tests/test_layering.py` | `_FORBIDDEN`, `test_layering_rules_hold`, `test_parsers_do_not_perform_io`, `test_only_gitstate_invokes_git` |

---

*Read-only study of RepoAtlas at `c402463`. Nothing in the repository was modified. Claims marked **[VERIFIED]** were read in code or observed in test output; **[INFERRED]** marks a conclusion drawn from structure rather than stated anywhere; **[NOT PRESENT IN V1]** records an absence rather than proposing a replacement.*
