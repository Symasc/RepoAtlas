# RepoAtlas — Architecture

**Status:** canonical. Companion to [`V1_SPEC.md`](V1_SPEC.md), which owns *what* V1 must do. This document owns
*how*: component boundaries, interfaces, data flow, storage decisions, algorithms and invariants.

Sections here are referenced from code review and commit messages (e.g. "per ARCHITECTURE §9"). If a design
decision changes, update this file in the same commit as the code.

---

## Table of contents

| §  | Section |
|----|---------|
| 1  | [Layering and boundaries](#1-layering-and-boundaries) |
| 2  | [Package map](#2-package-map) |
| 3  | [Storage schema](#3-storage-schema) |
| 4  | [Interfaces](#4-interfaces) |
| 5  | [Core domain models](#5-core-domain-models) |
| 6  | [The indexing pipeline](#6-the-indexing-pipeline) |
| 7  | [Repository context algorithm](#7-repository-context-algorithm) |
| 8  | [Git access policy](#8-git-access-policy) |
| 9  | [Incremental sync and the no-stale-facts invariant](#9-incremental-sync-and-the-no-stale-facts-invariant) |
| 10 | [Retrieval funnel](#10-retrieval-funnel) |
| 11 | [LLM contract and grounding enforcement](#11-llm-contract-and-grounding-enforcement) |
| 12 | [Security model](#12-security-model) |
| 13 | [Testing strategy](#13-testing-strategy) |
| 14 | [V2 seams](#14-v2-seams) |
| 15 | [Decision log](#15-decision-log) |

---

## 1. Layering and boundaries

Dependencies flow in one direction only:

```
                 cli/
                  │
                  ▼
             services/            (one module per command; orchestration only)
                  │
      ┌───────────┼────────────┬─────────────┬───────────┐
      ▼           ▼            ▼             ▼           ▼
  context/    discovery/    sync/       retrieval/     llm/
      │           │            │             │           │
      └───────────┴────┬───────┴─────────────┘           │
                       ▼                                 │
        parsers/  dependencies/  graph/  indexing/       │
                       │                                 │
                       ▼                                 │
                   storage/  ◄─────────────(never)───────┘
                       │
                       ▼
                   models/            (Pydantic; depends on nothing internal)
```

### Hard rules (invariants, enforced by a layering test)

1. **Parsers never touch storage and never touch the LLM.** A parser is a pure function of `(path, content)`.
2. **The LLM layer never touches storage.** It receives an already-built, bounded `EvidenceSet` and returns a
   validated answer object. It knows nothing about SQLite, FTS or embeddings.
3. **The CLI contains no indexing logic.** Each command: resolve context → call one service → render.
4. **No god object.** There is no `RepoAtlasAgent`. Orchestration lives in small per-command services.
5. **`models/` imports nothing from other RepoAtlas packages**, so it can be imported anywhere without cycles.
6. **`storage/` exposes Protocols; only `storage/sqlite/` knows SQL.** No SQL string appears outside that package.

A test walks the AST of every module and asserts the import graph respects this ordering. Violations fail CI, not
review.

---

## 2. Package map

```
RepoAtlas/
├── pyproject.toml
├── README.md
├── Dockerfile
├── docker-compose.yml
│
├── repoatlas/
│   ├── cli/            Typer app; one module per command; argument parsing + rendering only
│   ├── services/       per-command orchestration (init, sync, query, status)
│   ├── config/         Pydantic settings, YAML load/save, XDG paths, defaults
│   ├── discovery/      repository finding, ignore rules, language detection, manifest detection
│   ├── context/        RepositoryContextResolver — shared by every command (§7)
│   ├── gitstate/       hardened git CLI wrapper: branch, HEAD, dirty, file enumeration (§8)
│   ├── sync/           file-state diffing, sync planning, run orchestration (§9)
│   ├── indexing/       chunking, FTS population, embedding population, orphan GC
│   ├── embeddings/     EmbeddingModel protocol, fastembed backend, deterministic stub
│   │   ├── protocol.py     EmbeddingModel; a ranking aid, never a source of facts
│   │   ├── fastembed_backend.py  bge-small-en-v1.5 via ONNX; availability never downloads
│   │   ├── hashing.py      deterministic, offline stub used by every test
│   │   └── factory.py      config-driven selection
│   ├── parsers/        base protocol + registry + one module per language
│   │   ├── base.py         LanguageParser protocol, ParsedFile contract
│   │   ├── registry.py     extension/filename → parser dispatch
│   │   ├── treesitter.py   shared grammar loading, node helpers (lazy, cached)
│   │   ├── python.py       stdlib ast
│   │   ├── typescript.py   tree-sitter (ts, tsx, js, jsx, mjs, cjs)
│   │   ├── bash.py         tree-sitter
│   │   ├── ruby.py         tree-sitter
│   │   ├── salt.py         Jinja neutralization + lenient YAML + fallback line scan
│   │   ├── jinja.py        template relationships; owns neutralize()
│   │   ├── structured.py   YAML / JSON config keys; owns collect_config_keys()
│   │   └── dockerfile.py
│   ├── dependencies/   manifest and lockfile parsers; declared↔resolved join; name normalization
│   │   ├── base.py         DependencyParser protocol
│   │   ├── registry.py     filename → dependency parser dispatch
│   │   ├── normalize.py    PEP 503, npm scope-aware, gem
│   │   ├── python.py       pyproject, requirements, setup.py (ast), setup.cfg, uv/poetry locks
│   │   ├── node.py         package.json, package-lock, pnpm-lock, yarn.lock
│   │   └── ruby.py         Gemfile, gemspec, Gemfile.lock
│   ├── graph/          reference linker, cross-repo resolution, bounded traversal
│   │   ├── keys.py         natural keys for every entity tier
│   │   ├── resolution.py   ResolutionIndex — every match rule, in one place
│   │   └── linker.py       the two link passes
│   ├── retrieval/      query analysis, exact/semantic/graph retrievers, fusion, reranker seam
│   ├── storage/        Protocols + sqlite implementation + migrations
│   ├── llm/            ReasoningModel protocol, ollama backend, null backend, prompts, grounding
│   ├── rendering/      text renderer (the concise format) + json renderer
│   └── models/         Pydantic domain models shared everywhere
│
├── tests/
│   └── fixtures/       synthetic repositories (§13)
│
└── docs/
    ├── V1_SPEC.md
    └── ARCHITECTURE.md
```

Deviations from the layout suggested in the brief, and why:

- **`services/` added** — keeps orchestration out of `cli/` while avoiding a god object. Each command's logic is a
  small module with an explicit input/output type, directly unit-testable without invoking Typer.
- **`gitstate/` added** — every git invocation goes through one hardened wrapper (§8) rather than being scattered
  across `discovery/` and `sync/`. Makes the security posture auditable in one file.

---

## 3. Storage schema

**Decision: SQLite only in V1.** One database, WAL mode, at `${XDG_DATA_HOME:-~/.local/share}/repoatlas/atlas.db`
(overridable). Config lives separately at `${XDG_CONFIG_HOME:-~/.config}/repoatlas/config.yaml`.

Rationale: FTS5 gives mandatory exact search with zero extensions (verified: SQLite 3.45.1 here has FTS5); the
relationship graph at this scale is a well-indexed edge table with recursive CTEs for traversal; an embedded graph
database would add a dependency and an operational surface for a query pattern we can already express. Revisit only
if traversal profiling shows it dominates.

Migrations are numbered SQL files in `storage/sqlite/migrations/`, applied in order, version recorded in `meta`.

### Tables

```sql
repositories(
  id, name, path UNIQUE, branch, head_sha, is_dirty,
  languages_json, manifests_json,
  last_indexed_at, last_indexed_head, last_sync_status
)

files(
  id, repo_id → repositories ON DELETE CASCADE,
  rel_path, size, mtime_ns, content_hash,
  language, parser, parser_version, is_binary, indexed_at,
  UNIQUE(repo_id, rel_path)
)

entities(
  id, type, key,
  repo_id, file_id → files ON DELETE CASCADE,
  name, qualified_name, line_start, line_end, signature, meta_json
)

edges(
  id, src_id → entities, dst_id → entities, type, confidence,
  repo_id, file_id → files ON DELETE CASCADE,
  line_start, line_end, parser
)

unresolved_refs(
  id, repo_id, file_id → files ON DELETE CASCADE,
  src_id, edge_type, target_kind, target_key, line_start, line_end, confidence, meta_json
)

dependencies(
  id, repo_id → repositories ON DELETE CASCADE,
  file_id → files ON DELETE CASCADE,
  ecosystem, name, normalized_name,
  role,                            -- declared | resolved | provided
  constraint_text, version, group_name,
  line_start, line_end, parser
)

chunks(
  id, file_id → files ON DELETE CASCADE,
  line_start, line_end, text, token_estimate, content_hash
)

chunks_fts     -- FTS5, external content over chunks
entities_fts   -- FTS5, over name + qualified_name

embeddings(
  content_hash, model, dim, vector BLOB,   -- float16
  PRIMARY KEY (content_hash, model)
)

meta(key, value)   -- schema_version, parser_version, embedding_model, last_global_sync
```

### The two-tier entity design

`entities` holds both tiers, distinguished by whether `file_id` is set:

| Tier | `file_id` | Examples | Lifecycle |
|---|---|---|---|
| **File-scoped** | set | functions, classes, methods, Salt state IDs, Dockerfile stages | deleted with the file, by cascade |
| **Global / interned** | `NULL` | `repo:service-a`, `env:SERVICE_HOST`, `pkg:npm:@company/common`, `cfgkey:...`, `cmd:...` | deduplicated by `key`; garbage-collected when no edge references them |

`key` is a globally unique natural key, which is what makes interning and cross-file/cross-repo joins exact rather
than heuristic:

```
repo:service-a
file:service-a:src/config.py
sym:service-a:src/config.py:Config:18
env:SERVICE_HOST
pkg:npm:@company/common
pkg:pypi:requests
salt:deployment:service/init.sls:configure_service
```

### Provenance lives on the fact row

Both `entities` (file-scoped tier) and `edges` carry `repo_id`, `file_id`, `line_start`, `line_end` and `parser`;
`files` carries the `content_hash`. This is deliberate and load-bearing:

- It satisfies V1_SPEC §11 — every fact can cite `repo/path:line`.
- It makes stale-fact removal **exact and cheap**: everything a file asserted is
  `DELETE FROM edges WHERE file_id = ?` plus the cascade. There is no scan, no reference counting, no possibility
  of a fact surviving its source (§9).

An edge is provenanced by the file that *asserted* it, not by its endpoints. `service-a/src/app.py` importing
`common` produces an edge owned by `app.py`; deleting `app.py` removes the claim, while `common` itself is
untouched.

#### Why declared and resolved are separate rows

A package can be declared without being locked (no lockfile yet) and locked without being declared (a transitive
dependency). One row per package with both columns would force inventing one half of every such pair. Three roles
share one table instead: `declared` (a manifest's constraint), `resolved` (a lockfile's exact version) and
`provided` (the package this repository *is*). `provided` is the other half of cross-repository matching — an edge
from `repo-a` to `repo-b` exists only when `repo-a` declares a name `repo-b` provides, in the same ecosystem, and
only when exactly one repository provides it. The join back into a single view is a query, not a schema decision,
so both halves survive.

### Indexes

`files(repo_id, rel_path)`, `files(content_hash)`, `entities(key)`, `entities(file_id)`,
`entities(repo_id, type, name)`, `edges(src_id, type)`, `edges(dst_id, type)`, `edges(file_id)`,
`chunks(file_id)`, `unresolved_refs(target_kind, target_key)`,
`dependencies(repo_id, ecosystem, normalized_name)`, `dependencies(ecosystem, normalized_name, role)`.

### Vectors

`VectorIndex` is backed in V1 by brute-force cosine over a NumPy array loaded from the `embeddings` blobs, filtered
to the repositories in scope *before* the dot product. At ~10^5 chunks × 384 dims, float16 storage is ~77 MB and a
scoped query is single-digit milliseconds — not worth a dependency. The interface hides this completely, so
`sqlite-vec` or FAISS can replace it without touching a caller.

**Vectors are keyed by `(content_hash, model)`, not by `chunk_id`.** A file is reparsed as a whole, so its chunk
rows are deleted and reinserted with fresh ids even when the text did not move. Keying by `chunk_id` would
therefore re-embed every chunk of any touched file — editing one line of a large file would re-embed all of it —
which V1_SPEC §20 forbids outright ("never re-embed unchanged data"). Keying by the hash of the chunk text makes
re-embedding genuinely incremental, and deduplicates identical chunks across the whole managed set for free.

The cost is that cleanup is a sweep (`content_hash NOT IN (SELECT content_hash FROM chunks)`) rather than a
cascade. That is safe precisely because **a vector is not a fact**: it carries no provenance and is never cited, so
an orphaned vector wastes space but can never become a stale claim. Contrast entities and edges, which do cascade,
because they are facts and §9's no-stale-facts invariant is about them.

`chunks.content_hash` is the column this rests on, and it is also the reason the schema version participates in the
per-file `parser_version` (§9): bumping it reparses every file, which is how existing chunks acquire hashes.

The **embedding model id** is deliberately *not* part of that version. Vectors are keyed by the model that produced
them, so switching models finds none for the new id and re-embeds — without reparsing anything.

### FTS tokenization

`chunks_fts` uses `unicode61` with `tokenchars '_.-@$'` so `SERVICE_HOST`, `snake_case_name`, `@company/common` and
`module.attr` survive tokenization. Because no tokenizer is exact for every query shape, **lexical hits are
verified against the chunk text with a literal substring check before being reported as exact matches**
(V1_SPEC §14). FTS provides recall; verification provides precision.

---

## 4. Interfaces

All Protocols; concrete implementations are injected. Every one exists because something must be replaceable
without touching callers. Each has exactly one owning package in §2:

| Protocol | Owner |
|---|---|
| `RepositoryStore`, `FileStore`, `KnowledgeStore`, `SearchIndex`, `VectorIndex` | `storage/` |
| `LanguageParser` | `parsers/base.py` |
| `DependencyParser` | `dependencies/` |
| `EmbeddingModel` | `embeddings/` |
| `ReasoningModel` | `llm/` |
| `Reranker` | `retrieval/` |
| `Renderer` | `rendering/` |

### Storage

```python
class RepositoryStore(Protocol):
    def upsert(self, repo: Repository) -> int: ...
    def get_by_name(self, name: str) -> Repository | None: ...
    def get_by_path(self, path: Path) -> Repository | None: ...
    def list_all(self) -> list[Repository]: ...
    def delete(self, repo_id: int) -> None: ...


class FileStore(Protocol):
    def list_states(self, repo_id: int) -> dict[str, FileState]: ...  # rel_path → stat+hash
    def upsert(self, repo_id: int, file: IndexedFile) -> int: ...
    def delete_many(self, file_ids: Sequence[int]) -> None: ...  # cascades to all facts


class KnowledgeStore(Protocol):
    def replace_file_facts(
        self, file_id: int, facts: FileFacts
    ) -> None: ...  # atomic delete+insert
    def intern_entity(self, key: str, type: str, **attrs) -> int: ...
    def add_edges(self, edges: Sequence[EdgeRecord]) -> None: ...
    def neighbors(self, entity_ids, edge_types, direction, limit) -> list[EdgeRecord]: ...
    def gc_orphan_entities(self) -> int: ...


class SearchIndex(Protocol):
    def search_text(self, query: str, repo_ids, limit) -> list[ChunkHit]: ...
    def search_entities(self, query: str, repo_ids, types, limit) -> list[EntityHit]: ...


class VectorIndex(Protocol):
    def upsert(self, chunk_ids, vectors) -> None: ...
    def query(self, vector, repo_ids, limit) -> list[ChunkHit]: ...
    def delete(self, chunk_ids) -> None: ...
```

`replace_file_facts` is the single write path for parsed facts and is transactional: a sync that crashes mid-file
leaves either the old facts or the new ones, never a mixture.

### Parsing

```python
class LanguageParser(Protocol):
    name: str
    version: str  # bump ⇒ forces reparse (§9)

    def supports(self, path: Path) -> bool: ...
    def parse(self, path: Path, content: str) -> ParsedFile: ...


class DependencyParser(Protocol):
    ecosystem: str
    kind: Literal["manifest", "lockfile"]

    def supports(self, path: Path) -> bool: ...
    def parse(self, path: Path, content: str) -> DependencyReport: ...
```

### Reasoning and ranking

```python
class EmbeddingModel(Protocol):
    id: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
    def available(self) -> bool: ...
    def availability_detail(self) -> str: ...


class ReasoningModel(Protocol):
    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> ModelResponse: ...
    async def available(self) -> bool: ...


class Reranker(Protocol):
    def rerank(self, question: str, candidates: list[Candidate]) -> list[Candidate]: ...


class Renderer(Protocol):
    def render_answer(self, answer: RepoAtlasAnswer) -> str: ...
    def render_status(self, status: StatusReport) -> str: ...
    def render_repos(self, repos: list[Repository]) -> str: ...
```

`Reranker` ships as `NoopReranker` — the explicit V2 slot (§14). `ReasoningModel.available()` is what turns a
missing Ollama into a clean exit code 4 rather than a stack trace.

`embed_query` is separate from `embed` because retrieval models are asymmetric: bge-small-en-v1.5 expects a query
prefix that documents must not receive, and embedding both sides identically measurably degrades recall.

`EmbeddingModel.available()` is the same seam as `ReasoningModel.available()`, with one deliberate difference:
**it never goes to the network.** It probes the local cache and reports "not downloaded" rather than fetching
67 MB in the middle of a sync. Fetching is an explicit act — `repoatlas sync --embed` — exactly as a missing Ollama
model is reported with `ollama pull` rather than pulled automatically. Until then semantic retrieval is off and
every deterministic command is unaffected, because V1_SPEC §14 makes exact search the mandatory half and semantic
search the aid.

---

## 5. Core domain models

All Pydantic v2, all in `models/`.

```
Repository          name, path, branch, head_sha, is_dirty, languages, manifests, index state
IndexedFile         rel_path, size, mtime_ns, content_hash, language, parser, is_binary
FileState           size, mtime_ns, content_hash          (the sync diff unit)

ParsedFile          language, parser, parser_version, symbols, references, diagnostics
ParsedSymbol        kind, name, qualified_name, line_start, line_end, signature, parent, meta
ParsedReference     edge_type, target_kind, target_key, line_start, line_end, confidence, meta
ParseDiagnostic     severity, message, line

DeclaredDependency  ecosystem, name, normalized_name, constraint, group, manifest_path, line
ResolvedDependency  ecosystem, name, normalized_name, version, lockfile_path, line

RepositoryContext   scope (REPOSITORY | GLOBAL), primary, repositories, reason, cwd
EvidenceCandidate   repository, path, line_start, line_end, text, score, source, entity_id
EvidenceSet         candidates, question, context, token_budget

Evidence            repository, path, line_start, line_end, description
Finding             statement, confidence, evidence
RepoAtlasAnswer     summary, findings, unknowns, next_check
```

### The load-bearing decision: parsers emit unresolved references

`ParsedReference` carries a *raw target key*, never an entity ID:

```python
ParsedReference(
    edge_type="READS",
    target_kind="env",
    target_key="SERVICE_HOST",
    line_start=18,
    line_end=18,
    confidence=1.0,
)
```

Resolution — interning the target, deciding whether `from common import x` means a local module or a package from
another managed repository — happens in `graph/linker.py`, a single stage that runs after all files are persisted.

Consequences, all of them wanted:

- Parsers are pure functions with no I/O, so a parser test is `parse(fixture) == expected` with no database.
- All cross-repository reasoning lives in one auditable place, where the "exact normalized name only, never fuzzy"
  rule of V1_SPEC §9 is enforced once rather than in nine parsers.
- A reference whose target does not exist yet is written to `unresolved_refs` and re-linked on every subsequent
  link pass, so adding `common-lib` later retroactively creates the `service-a DEPENDS_ON common-lib` edge without
  reparsing `service-a`.

### The two link passes

Linking runs twice, and the split is what keeps the trust rule enforceable.

**Per file**, during indexing, creates only what one file can prove alone: its definitions, its interned global
targets (environment variables, modules, commands, config keys) and references to symbols it defines itself.
Everything else becomes a row in `unresolved_refs` — the durable record of a claim, provenanced to the file that
made it.

**Globally**, once at the end of a sync run, resolves those standing claims against everything currently indexed.
The pass first deletes every edge it previously created (they carry `parser = 'linker:global'`) and then rebuilds,
which makes it **idempotent**: running it twice gives the same graph as running it once. That is what lets an
incremental sync equal a full reindex (§9) even though cross-repository edges depend on repositories the current
sync never touched.

| Reference kind | How it resolves |
|---|---|
| environment variable, module, command, config key | interned as a global entity in the per-file pass |
| symbol defined in the same file | resolved directly in the per-file pass |
| symbol defined in another file | resolved **only through an import this file actually wrote** |
| module specifier | resolved to a file: Python dotted and relative, TS/JS relative with index resolution, Ruby `require_relative` |
| script / file path | joined against the referencing file's directory, then matched to a managed repository — including paths that climb out of one repository into another |
| Salt `include:` | dotted state path → `x/y.sls` or `x/y/init.sls` |
| Salt requisite | state ID, unique within the repository |
| `salt://` source | unique matching managed path, at reduced confidence |
| `pillar[...]` key | the config key that defines it, searched only in files this one provably includes |
| declared package | interned `pkg:<ecosystem>:<name>`, plus a repository edge when exactly one managed repository provides that exact normalized name |

Three rules hold across every row, and they are the trust rule in practice:

- **A computed target resolves to nothing.** `source "$CONFIG_FILE"`, `process.env[key]`, a templated Salt ID and a
  `setup.py` whose `install_requires` is built at runtime all produce no fact, because the value is not knowable
  without executing the repository.
- **An ambiguous target resolves to nothing.** Two managed repositories providing `common-lib` is a real situation;
  picking one would be a guess, so neither edge is created.
- **A symbol reference is resolved only through an import.** A `CALLS load_config` in `app.py` is wired to
  `config.py` because `app.py` wrote `from src.config import load_config`; the same call in a file that never
  imported the name stays unresolved. Name equality alone is not evidence (V1_SPEC §2).

Everything unresolved stays in `unresolved_refs` rather than being discarded, so adding `common-lib` to the managed
set later retroactively creates the `python-service DEPENDS_ON common-lib` edge without reparsing `python-service`.

---

## 6. The indexing pipeline

```
enumerate       gitstate: git ls-files --cached --others --exclude-standard
                          + built-in exclusions + .repoatlasignore + secret patterns
   ↓
diff            FileState comparison → unchanged / changed / new / deleted   (§9)
   ↓
delete          FileStore.delete_many(deleted) → cascade removes entities, edges,
                chunks, embeddings, FTS rows, unresolved_refs
   ↓
parse           registry dispatch → LanguageParser.parse(path, content)     (pure, no I/O)
   ↓
persist         KnowledgeStore.replace_file_facts(file_id, facts)           (transactional)
   ↓
dependencies    manifest + lockfile parsers → declared / resolved / provided rows
   ↓
link (file)     intern global targets; resolve intra-file symbols; record the
                remaining claims in unresolved_refs                          (§5)
   ↓
chunk           symbol-aligned where line ranges exist, else fixed windows
   ↓
index           FTS upsert; embed only chunks whose content_hash changed
   ↓
─── once per run, after every repository ────────────────────────────────────
   ↓
link (global)   drop the previous global edges; resolve every standing claim
                against the whole managed set; join declared packages to the
                repositories that provide them                               (§5)
   ↓
gc              drop global entities with no remaining edges
```

The global link stage runs at the end of any sync, including a single-repository sync, because a change in one
repository can create or invalidate a cross-repository edge in another. It reads the standing `unresolved_refs`
set and the file and symbol tables, all of which are indexed, and rebuilds only edges it owns.

### Chunking

Chunks are the unit of both lexical and semantic retrieval, so they carry line ranges for provenance. Preference
order: one chunk per top-level symbol where the parser supplied a range (bounded to a max size, split if larger),
otherwise fixed windows of ~40 lines with ~8 lines of overlap. Files over a size cap, detected-binary files, and
lockfiles are not chunked — lockfiles are represented by the dependency layer instead, which is both smaller and
more useful.

---

## 7. Repository context algorithm

Implemented once in `context/resolver.py` and used by every command (V1_SPEC §6).

```
resolve(cwd, explicit_repo) -> RepositoryContext

1. if explicit_repo is not None:
       exact name match among managed repositories        → REPOSITORY scope
       else exact canonical-path match                    → REPOSITORY scope
       else ambiguous                                     → error, list candidates   (exit 2)
       else unknown                                       → error, list repositories (exit 2)

2. canonical = cwd.resolve()                              # symlinks resolved
   for candidate in [canonical, *canonical.parents]:      # nearest first
       if candidate is a managed repository path          → REPOSITORY scope, that repo
                                                            [closest nested repo wins]

3. if canonical == configured_root.resolve()              → GLOBAL scope, all repositories

4. if an enclosing directory contains .git                → "This Git repository is not
                                                             managed by RepoAtlas."     (exit 2)

5. otherwise                                              → unresolved-context error    (exit 2)
```

### Why step 2 precedes step 3

If the configured root is itself a Git repository *and* is managed, being at the root resolves to that repository
rather than to global scope — the more specific answer. This is only reachable when the user has explicitly
initialized a root that is also a repository.

### There is no third implicit context

A directory under the root that is not inside a managed repository falls through steps 2 and 3 and produces the
step-5 error. There is deliberately **no subtree scope**: a partial scope inferred from proximity would silently
answer a question against an arbitrary subset of repositories, and a wrong-scope answer is worse than an error
that names the fix.

### Detail rules

- **Canonicalization**: `Path.resolve()` on both sides before comparison, so `~`, `..`, trailing slashes,
  relative paths, spaces and symlinks all compare correctly. Repository paths are stored canonicalized at
  discovery time.
- **Containment**: `Path.is_relative_to`, never string prefix comparison — `/work/repos-old` must not match
  `/work/repos`.
- **Nearest-first ordering** in step 2 gives "closest managed repository wins" with no special-casing.
- **`sync` never registers.** Step 4 reports and exits; adding repositories is `init`'s job alone.
- **Scope is data, not behavior**: `RepositoryContext` carries `scope`, `primary`, `repositories` and a
  human-readable `reason`, which `--verbose` prints. Commands branch on the value; none of them re-derives it.

---

## 8. Git access policy

Repositories are untrusted (§12). All git access goes through `gitstate/`, which:

- invokes the **`git` CLI via subprocess** — never libgit2 bindings, never repository code;
- passes hardening flags on **every** invocation:
  ```
  git --no-optional-locks -c core.hooksPath=/dev/null -c safe.directory=* ...
  ```
  `core.hooksPath=/dev/null` guarantees no repository-supplied hook can run even if a future command would
  normally trigger one; `--no-optional-locks` avoids mutating a repository we only read; `safe.directory=*` is
  required for the read-only container mount, where the repository is owned by a different uid;
- restricts itself to a **fixed allowlist of read-only plumbing**: `rev-parse`, `status --porcelain`, `ls-files`;
- uses `--` and argument arrays; no shell interpolation of repository-derived strings, ever;
- applies a timeout and treats any failure as "git state unknown" rather than raising.

### File enumeration

```
git ls-files --cached --others --exclude-standard -z
```

This returns tracked files plus untracked-but-not-ignored files, which is exactly the working tree that
V1_SPEC §5 declares authoritative — uncommitted changes included, `.gitignore` honored for free with real gitignore
semantics rather than a reimplementation. Built-in exclusions, `.repoatlasignore` and secret patterns are applied
on top.

Fallback when git is unavailable or the command fails: `os.walk` with the exclusion set, and repository state
recorded as unknown. Discovery still works; freshness reporting degrades honestly.

### Repository *detection* does not invoke git

Deciding whether a directory is a repository is a `.git` existence check (`gitstate.is_git_repository`), not a
`git rev-parse`. Discovery calls it once per directory walked, and a subprocess per directory would dominate
`init` on a root with hundreds of repositories. It is also strictly safer than invoking git — no process is
started at all. A `.git` file (worktree or submodule pointer) counts, same as a directory. `git_toplevel`, used
by context resolution to detect an *unmanaged* enclosing repository, walks upward with the same check, so that
path works even when the git binary is missing entirely.

---

## 9. Incremental sync and the no-stale-facts invariant

### Correctness dominates the optimization

The **content hash is the sole authority** on whether a file changed. The stat comparison (`size`, `mtime_ns`) is
only a permitted shortcut for skipping the hash computation, and the shortcut is **disabled entirely** whenever
anything makes stat untrustworthy:

| Trigger | Why stat cannot be trusted |
|---|---|
| `head_sha != last_indexed_head` | `checkout`, `switch`, `pull`, `rebase`, `reset` rewrite files with arbitrary mtimes, and can restore a file to a previous state with a previous size |
| dirty-state changed since last sync | editor/tool writes that stat may not distinguish |
| previous sync did not complete cleanly | recorded state may not reflect what was actually indexed |
| `schema_version` or `parser_version` changed | stored facts are stale by definition even though files are not — handled per file, see below |
| `--full` / `--rehash` passed | explicit user override |

Under any trigger, **every file is hashed**. Otherwise: stat match ⇒ skip; stat mismatch ⇒ hash ⇒ reparse only if
the hash actually changed. A coarse-mtime filesystem therefore costs an unnecessary rehash — never a missed update.

The rule stated positively: *we may skip work only when we can prove nothing changed. Whenever proof is
unavailable, we do the work.*

Schema and parser versions are handled **per file** rather than as a sixth global trigger: the version is stored on
the file row, so affected files are reparsed directly instead of hashing the entire tree to learn that nothing
moved. The embedding model is not part of that version at all — vectors are keyed by the model that produced them
(§3), so a model change re-embeds without reparsing anything.

### Deletion

Files present in storage but absent from the enumeration are deleted through `FileStore.delete_many`, cascading to
entities, edges, chunks, embeddings, FTS rows and unresolved references. Because provenance is on the fact row
(§3), this is exact — a fact cannot outlive its source file.

Branch switches need no diffing logic at all: the working-tree enumeration is authoritative, and the forced rehash
above covers the mtime hazard.

### Invariants, both tested

1. **No orphaned facts.** After any sync, no entity, edge, chunk, embedding or FTS row references a file that is
   not currently on disk with the recorded hash. Tested by walking the fact tables and stat-ing every referenced
   path.
2. **Incremental equals full.** A sync across any sequence of HEAD changes, edits, additions and deletions
   produces state equivalent to a full reindex of the same tree. Tested by running both paths over the same
   fixture and diffing normalized dumps of `files`, `entities` and `edges`.

Invariant 2 is the one that catches the entire class of stale-data bugs, and it is cheap to run over fixtures on
every commit.

### Concurrency

A sync takes an advisory lock row in `meta`. A second concurrent sync exits with a clear message rather than
interleaving writes. Reads (`locate`, `ask`) are unaffected — WAL permits concurrent readers.

---

## 10. Retrieval funnel

`retrieval/` turns a question plus a `RepositoryContext` into a bounded `EvidenceSet`. No LLM is involved in any
stage — this is the layer that makes the trust rule affordable.

```
question + RepositoryContext
   │
   ├─ 1. query analysis (deterministic)
   │     quoted strings · ALL_CAPS env candidates · dotted.qualified.names
   │     path-like tokens · package names · error-shaped substrings
   │
   ├─ 2. exact          literal-substring verified chunk matches      → DETERMINISTIC
   ├─ 3. lexical        chunks_fts recall over the whole question     → ASSOCIATIVE
   ├─ 4. symbol         exact entity-name lookup                      → DETERMINISTIC
   │                    entities_fts recall                           → ASSOCIATIVE
   ├─ 5. semantic       embed(question) → VectorIndex.query(scoped)   → ASSOCIATIVE
   ├─ 6. graph          ≤2 hops from the exact entity hits,           → DETERMINISTIC
   │                    capped fan-out, cross-repo only via typed edges
   │
   ├─ 7. fusion         tier first, then reciprocal-rank fusion
   │                    + primary-repository boost (inside a tier)
   ├─ 8. dedup          drop spans that repeat a higher-ranked one
   ├─ 9. rerank         Reranker (NoopReranker in V1)
   └─ 10. budget        hard caps on candidate count and total tokens
         ↓
      EvidenceSet (+ RetrievalTrace)
```

### Deterministic evidence outranks semantic similarity

This is the funnel's governing rule (V1_SPEC §14: *vector search must not replace exact search*), and it is
enforced by a type rather than by a weight. Every candidate carries an `EvidenceTier`:

| Tier | Sources | Why |
|---|---|---|
| `DETERMINISTIC` | literal-verified chunk matches, exact entity-name matches, graph expansion | facts about the code, each with provenance |
| `ASSOCIATIVE` | FTS recall, embedding neighbours | leads about what the question might have meant |

`fuse` sorts on `(tier, -score)`. Scores are therefore only ever compared **within** a tier, so no cosine
similarity — 0.99 or otherwise — can move an embedding neighbour above an exact identifier, an exact error string,
an exact symbol name, an environment variable or a graph-supported relationship. A span reached by several
retrievers takes its best tier: found deterministically once means deterministic.

The primary-repository boost is additive on the score, so it competes inside a tier and never promotes across one.
A V2 reranker is bound by the same rule: it may reorder within a tier, never across.

### Suppressing similarity when the question named something specific

When query analysis extracts literal terms — a quoted string, an environment variable, a dotted name, a path — and
the exact stage finds **none** of them anywhere in the index, the semantic stage is skipped entirely. The user
named something specific; the honest answer is that it does not exist. Returning spans that merely read like the
name would be plausible, unfounded output, which is the failure V1_SPEC §2 exists to prevent. A question with no
literal terms ("where is deployment configuration generated?") is exactly what semantic retrieval is for, and
nothing is suppressed there.

### Bounded graph expansion

Expansion is bounded on three axes at once — hop count (2), fan-out per hop, and total nodes — because a popular
hub such as `env:SERVICE_HOST` legitimately touches every repository, and an unbounded walk from it returns the
world.

Two hops is the useful minimum rather than an arbitrary depth. An interned entity has no location of its own, so
hop one reaches the hub and hop two reaches the files on the other side of it. That is precisely what makes "where
is SERVICE_HOST configured?" answerable from inside a service that only reads it.

Traversal follows **relationship** edges. `DEFINES` and `CONTAINS` are excluded even though they are perfectly good
facts: they are containment, so traversing them turns any file node into a fan-out over every symbol it declares,
and two hops of that buries the relationship the question was about. Crossing a repository boundary additionally
requires `DEPENDS_ON`, `REFERENCES`, `EXECUTES`, `SOURCES` or `IMPORTS` — every one produced by the linker's global
pass from an exact match — so a cross-repository answer always has a structural reason (V1_SPEC §9). Each reached
entity is labelled with the edge that reached it, because the reader asked about a relationship, not about what the
file happens to declare.

### Scoping, fusion and deduplication

Scoping happens at stages 2-6, *before* scoring — this is what satisfies "never scan every repo for every
question" (V1_SPEC §20), for the vector stage as much as the lexical one. In `REPOSITORY` scope the primary
repository is searched first; other repositories stay reachable, so primary scope is a bias rather than a wall.

Reciprocal-rank fusion is chosen over score normalization because BM25 ranks, cosine similarities, literal-match
scores and graph distances are not commensurable, and RRF needs no per-source calibration to combine them stably.

Deduplication drops a span contained in a higher-ranked one, or overlapping it by more than half of the shorter
span — chunk windows overlap by design, so the same lines arrive repeatedly. Spans are never merged into a
synthetic range: a citation must point at text that was actually retrieved (V1_SPEC §11).

### Instrumentation

Every stage records its duration and its effect on the candidate count into a `RetrievalTrace`, and the global link
pass fills a `LinkTrace` the same way. Both are attached to the result and shown under `--verbose` and `--json`.
They are **observations, never inputs**: nothing reads a trace to make a decision, so instrumentation cannot change
an answer. This is what lets retrieval and traversal performance be measured later without changing anything
structural (V1_SPEC §20).

`locate` stops here and renders the `EvidenceSet` directly — which is why it needs no LLM.

---

## 11. LLM contract and grounding enforcement

```
EvidenceSet
   ↓  prompt construction (evidence rendered with repo/path:line labels, token-budgeted)
ReasoningModel.complete(messages, response_schema=RepoAtlasAnswer)
   ↓  JSON-schema-constrained decoding where the backend supports it
Pydantic validation ──fail──► one repair attempt with the validation error
   ↓                              └──fail──► INSUFFICIENT EVIDENCE
citation verification  ◄── THE ENFORCEMENT POINT
   ↓
Renderer
```

### Citation verification

The trust rule (V1_SPEC §2) is enforced by code, not by prompt wording. After validation, a deterministic pass
checks every `Evidence` item against the evidence set that was actually supplied:

- an `Evidence` naming a repository or path **not present in the supplied set** is dropped;
- a line range outside the supplied span is clamped to it, or dropped if disjoint;
- a `Finding` left with **no surviving evidence is demoted to an `unknown`**;
- if every finding is demoted, the answer becomes `INSUFFICIENT EVIDENCE`.

Dropped citations are counted and surfaced under `--verbose`, which also makes the metric useful for evaluating
prompts and models: a rising drop rate is a regression signal.

A citation carrying no line numbers is kept: the file genuinely was supplied, and synthesising a span for it would
itself be invention. Only a *span* disjoint from everything shown is dropped.

### Where FACT and HYPOTHESIS come from

`debug` renders the two separately (V1_SPEC §15), and the split is **not** a confidence threshold on model output:

| Section | Source |
|---|---|
| `FACTS` | the retrieved `EvidenceSet` — deterministic, provenanced, RepoAtlas's own |
| `HYPOTHESES` | the model's findings — interpretations of those facts, each with confidence |
| `UNKNOWNS` | the model's stated unknowns, plus every finding demoted by verification |

This makes the trust rule visible in the output itself: the model never contributes a row to `FACTS`.

### Backends

| Backend | Purpose |
|---|---|
| `OllamaBackend` | V1 default. `POST /api/chat`, `format` = JSON schema, config-driven `model` and `base_url` |
| `NullBackend` | Deterministic. Returns `INSUFFICIENT EVIDENCE` with no findings. Selected by `llm.provider: none`, and used as a test double. **Not** what `--no-llm` uses: V1_SPEC §4 specifies evidence-only output for that flag, so it skips reasoning entirely rather than narrating around a model that was never consulted |
| `ScriptedBackend` | Test-only. Returns canned responses, including malformed ones, to exercise validation, repair and citation-dropping paths |

`available()` is checked before use, and before retrieval runs, so an unusable backend wastes no work and produces
the exit-code-4 message rather than a traceback. For Ollama, availability means *both* that the server responds and
that the configured model is pulled — a reachable server missing the model would otherwise fail at request time,
and the error names the `ollama pull` command that fixes it. Because the deterministic half of the system never calls a `ReasoningModel`, `init`, `sync`,
`status`, `repos` and `locate` remain fully functional with no LLM installed — which is the state of the current
development machine.

Prompts live in `llm/prompts/` as versioned templates, not inline strings, so prompt changes are reviewable diffs.

---

## 12. Security model

Repositories are **untrusted input**. Static analysis only.

| Vector | Mitigation |
|---|---|
| Repository Bash | Parsed with tree-sitter; never executed |
| Repository Python | Parsed with `ast`; never imported. `setup.py` included |
| Repository Ruby | Parsed with tree-sitter; never executed |
| Salt / Jinja | Jinja neutralized to placeholders; never rendered |
| npm lifecycle scripts | `package.json` read as JSON; npm never invoked |
| Dockerfiles | Parsed as text; never built or run |
| Discovered commands | Recorded as entities; never executed |
| Git hooks / config | `core.hooksPath=/dev/null`, read-only plumbing allowlist (§8) |
| Path traversal via symlinks | Paths canonicalized and asserted to remain within the repository root before reading |
| Zip-bomb / huge files | Size and line caps before content is read into memory |

### Secrets

Patterns `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa`, `id_ed25519` are excluded **at enumeration**, before content
is read. Nothing excluded there can reach chunks, embeddings, prompts or logs, because those stages only ever see
enumerated files.

That single choke point is also the seam for stronger scanning later: a content-based detector (entropy, provider
key patterns) plugs into the same filter and gains the same guarantee, with no changes downstream.

Logging never includes file content — only paths, line numbers and counts.

---

## 13. Testing strategy

**Synthetic fixtures only. Real work repositories are never used in automated tests** (V1_SPEC §19).

```
tests/fixtures/repos/
├── python-service/     Python + Bash; reads SERVICE_HOST; depends on common-lib
├── ts-service/         TypeScript; depends on @company/common (→ ts-common)
├── ts-common/          package.json name @company/common
├── deployment/         Salt SLS + Jinja + pillar; configures SERVICE_HOST
├── scripts/            Bash invoking python-service tooling
├── nested-outer/       contains nested-outer/tools/nested-inner as a separate repo
└── unmanaged/          a git repo deliberately outside the configured root
```

Fixtures are stored as plain directories; a session-scoped pytest fixture copies them to a temp path and runs
`git init` + commit so that HEAD, branches and dirty state are real. Branch-switch tests create and check out a
second branch, then assert invariant 2 from §9.

Test layers:

| Layer | Style |
|---|---|
| Parsers | Pure: `parse(fixture_content) == expected`. No database, no filesystem beyond reading the fixture |
| Context resolver | Table-driven over the full case matrix in V1_SPEC §19, including ten-deep paths, spaces, symlinks, nested repos and the unmanaged repo |
| Sync | Scenario-driven: mutate the temp fixture, sync, assert both §9 invariants |
| Retrieval | Fixed questions with expected evidence paths; asserts primary-repo bias and cross-repo traversal |
| LLM | `ScriptedBackend` only. Covers valid output, invalid output, repair, missing evidence, fabricated citations, unknown answers, citation rendering |
| Layering | AST walk over the import graph asserting §1 |
| E2E | The V1_SPEC §21 success scenario, driven through the Typer app with a temp home |

CI requires no network, no Ollama and no embedding-model download: `NullBackend`/`ScriptedBackend` cover the
reasoning layer, and semantic-search tests use a deterministic stub `EmbeddingModel` (hash-based pseudo-vectors)
with the real fastembed path exercised only in an opt-in marked test.

---

## 14. V2 seams

Every deferred capability (V1_SPEC §22) has a place to land that requires no restructuring:

| V2 capability | Seam |
|---|---|
| Advanced reranking | `Reranker` protocol, stage 9 of §10. Swap `NoopReranker` for a cross-encoder — bound by the tier rule: reorder within a tier, never across one |
| Version-specific documentation retrieval | A `DocumentationSource` protocol fed by `ResolvedDependency`. The retrieval funnel gains a source at stage 3; fusion and budgeting are unchanged |
| Internet research / web provenance | `Evidence` gains an optional origin discriminator; the citation verifier (§11) extends to verify web citations against the fetched set, with the same drop-and-demote logic |
| Git-history debugging, regression introduction | The provenance columns already pin every fact to `repo/file/lines`; history analysis joins against them without schema change |
| Blast-radius analysis | A traversal over existing `edges` — a new query, not new data |
| Alternative model backends (vLLM, llama.cpp, OpenAI-compatible) | `ReasoningModel` protocol + config-driven provider selection |
| Alternative vector backends | `VectorIndex` protocol; `sqlite-vec` or FAISS replaces `SqliteVectorIndex` without touching a caller |
| Performance work of any kind | `RetrievalTrace` and `LinkTrace` already report per-stage cost and candidate counts, so a regression is measurable before anything is restructured |

**Resolved dependency versions are stored in V1 despite being unused** precisely because the V2 documentation flow
needs them: `question mentions dependency → identify repository → determine resolved version → retrieve docs for
that exact version → combine with repository evidence`. Collecting them from day one means V2 does not require a
full reindex.

---

## 15. Decision log

| Decision | Alternative considered | Why |
|---|---|---|
| SQLite only | Embedded graph DB (Kùzu, Neo4j-embedded) | FTS5 + an indexed edge table with recursive CTEs covers V1's traversal needs; a graph DB adds a dependency and operational surface for no proven gain at this scale |
| stdlib `ast` for Python | tree-sitter for uniformity | `ast` is exact, zero-dependency, gives precise line ranges and decorator/call structure. Uniformity is not worth accuracy loss on the primary language |
| tree-sitter for TS/JS/Bash/Ruby | Hand-written parsers, or language-native tooling (tsc, ripper) | Language-native tooling means executing runtimes near untrusted code; tree-sitter is in-process, error-tolerant, and one dependency for four languages |
| Brute-force NumPy vectors | `sqlite-vec`, FAISS | ~77 MB / single-digit ms at target scale. Hidden behind `VectorIndex`, so upgrading is a one-file change if profiling demands it |
| RRF for fusion | Weighted score normalization | BM25, cosine and graph distance are not commensurable; RRF is stable without per-source calibration |
| `git ls-files` for enumeration | Reimplementing gitignore over `os.walk` | Real gitignore semantics for free, including nested and negated patterns, plus tracked/untracked distinction |
| git CLI subprocess | pygit2 / dulwich | No native dependency; the hardening flags (§8) are explicit and auditable in one wrapper |
| Parsers emit unresolved references | Parsers resolve to entity IDs directly | Keeps parsers pure and testable; confines cross-repo matching rules to one auditable place; enables retroactive linking via `unresolved_refs` |
| Citation verification in code | Prompt instructions alone | V1_SPEC §2 is a guarantee, and a guarantee cannot rest on model compliance |
| `services/` layer added | Command logic in `cli/` | Keeps Typer out of the testable path without creating a god object |
| No subtree context scope | Infer scope from repos beneath cwd | A silently-wrong scope is worse than an error naming the fix (§7) |
| `.git` marker check for repository detection | `git rev-parse` per directory | One subprocess per walked directory would dominate `init`; the marker check is faster and starts no process (§8) |
| Parser-version staleness handled per file | A sixth forced-rehash trigger | The stored `parser_version` is on the file row, so affected files can be reparsed directly instead of hashing the entire tree to learn that nothing changed (§9) |
| `ask`/`debug` fail with exit 4 when no model is reachable | Fall back to `locate` output | Silently answering a different question than the one asked is worse than a clear message naming the command that does work; `--no-llm` makes the fallback explicit and opt-in |
| `httpx` promoted to a core dependency | Keep it behind the `llm` extra | The Ollama backend is the V1 default; failing at import time behind an extra is a worse experience than one small dependency |
| Ollama availability includes "model is pulled" | Probe reachability only | A running server without the model fails at request time; checking early turns a mid-query failure into an actionable `ollama pull` message |
| Deterministic evidence outranks similarity via an `EvidenceTier`, not a weight | A large additive boost for exact matches | A weight is a value someone will tune until it is wrong. A tier is a guarantee: fusion compares scores only within a tier, so no cosine can displace an exact identifier — which is what V1_SPEC §14 actually requires |
| Vectors keyed by `(content_hash, model)` | Keyed by `chunk_id` with a cascade | Chunk rows are replaced wholesale on reparse, so `chunk_id` keying re-embeds unchanged text — forbidden by V1_SPEC §20. A vector carries no provenance and is never cited, so sweeping orphans is safe where facts must cascade (§3) |
| Semantic stage suppressed when named terms exist nowhere | Always run it | A question naming a specific identifier that is not in the index is answered by "it is not here", not by something that reads like it. Offering the latter is exactly the plausible-but-unfounded output §2 forbids |
| `EmbeddingModel.available()` never downloads | Load (and fetch) on first use | A 67 MB fetch inside `sync` is a multi-minute stall with no explanation. Reporting "not downloaded, run `sync --embed`" mirrors how a missing Ollama model is reported rather than pulled |
| Graph traversal excludes `DEFINES`/`CONTAINS` | Traverse every edge type | Containment edges turn one file node into a fan-out over every symbol it declares; two hops of that buries the relationship the question was about under an inventory of the neighbourhood |
| Graph evidence labelled by the edge that reached it | Reuse the entity's own description | The reader asked about a relationship. "exports (hop 1)" answers that; "defines builder" answers a different question |
| Instrumentation as models attached to results | A logging framework, or none | A trace that is part of the result is testable, serializable through `--json`, and provably inert — nothing reads it to decide anything, so it cannot change an answer |
| FACTS = retrieved evidence, HYPOTHESES = model findings | Split model findings by a confidence threshold | The model is never a source of facts (§2); mapping its output to "hypothesis" and retrieval to "fact" states that structurally instead of by numeric convention |
| tree-sitter promoted to a core dependency | Keep it behind the `parsers` extra | Six of the eight V1 languages need it; a parser that silently does not exist would make the index quietly incomplete, which is worse than one dependency (the same reasoning as `httpx`) |
| Jinja neutralized with same-length filler | Strip Jinja, or blank whole lines | Preserving every character position keeps YAML line *and* column marks identical to the original file, so a Salt fact cites the line a human would point at. Stripping shifts every position after the first `{{` |
| An `.sls` top-level key is a state or config data, decided structurally | Treat every `.sls` as states, or infer from the path | Salt itself distinguishes them only through the top file, which RepoAtlas cannot read. What *is* in the text is whether the key declares a `module.function`; that test is deterministic and is what lets `pillar['service']['host']` resolve to the line defining it |
| Two link passes, the global one idempotent by delete-and-rebuild | Incrementally patch cross-repository edges | Cross-repository edges depend on repositories the current sync never touched. Rebuilding from the standing claims is the only cheap way to keep "incremental sync equals full reindex" true for them (§9) |
| Cross-file symbol references resolved only through a written import | Resolve on name equality within the repository | Name equality is not evidence (V1_SPEC §2). The import is the proof, and it is already in the file, so requiring it costs nothing and removes a whole class of wrong edges |
| `salt://` sources resolved at confidence < 1.0 | Resolve at full confidence, or not at all | `file_roots` is not readable, so no `salt://` mapping is ever proven. Refusing to resolve would lose a genuinely useful link; resolving silently at 1.0 would overstate it. The confidence value carries the distinction |
| `unresolved_refs` retained after a successful resolution | Delete rows once resolved | The row is the file's durable claim, not a work queue. Keeping it is what makes the global pass rebuildable and what lets a repository added later gain its edges without a reparse |
