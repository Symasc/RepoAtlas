# RepoAtlas — V1 Specification

**Status:** canonical. This document is the source of truth for V1 scope, requirements and progress.
Do not restate these requirements in conversation; reference sections instead (e.g. "see §6 Directory-aware
behavior"). If a requirement changes, **update this file first**, then implement.

Companion document: [`ARCHITECTURE.md`](ARCHITECTURE.md) — component boundaries, interfaces, schema, algorithms.

**Governing principle:**

> RepoAtlas knows facts through **deterministic inspection**, finds evidence through **retrieval**, and uses the LLM
> **only to reason about that evidence**.

---

## Table of contents

| §  | Section |
|----|---------|
| 1  | [Purpose and scope](#1-purpose-and-scope) |
| 2  | [The trust rule](#2-the-trust-rule) |
| 3  | [Technology and constraints](#3-technology-and-constraints) |
| 4  | [CLI surface](#4-cli-surface) |
| 5  | [Repository discovery](#5-repository-discovery) |
| 6  | [Directory-aware behavior](#6-directory-aware-behavior) |
| 7  | [Language parsing](#7-language-parsing) |
| 8  | [Knowledge representation](#8-knowledge-representation) |
| 9  | [Cross-repository relationships](#9-cross-repository-relationships) |
| 10 | [Environment variables](#10-environment-variables) |
| 11 | [Provenance](#11-provenance) |
| 12 | [Incremental synchronization and branch awareness](#12-incremental-synchronization-and-branch-awareness) |
| 13 | [Dependency detection](#13-dependency-detection) |
| 14 | [Search and retrieval](#14-search-and-retrieval) |
| 15 | [LLM contract](#15-llm-contract) |
| 16 | [Output format](#16-output-format) |
| 17 | [Security and secrets](#17-security-and-secrets) |
| 18 | [Docker](#18-docker) |
| 19 | [Testing requirements](#19-testing-requirements) |
| 20 | [Performance principles](#20-performance-principles) |
| 21 | [V1 success scenario](#21-v1-success-scenario) |
| 22 | [Deferred to V2 and beyond](#22-deferred-to-v2-and-beyond) |
| 23 | [Implementation checklist](#23-implementation-checklist) |

---

## 1. Purpose and scope

RepoAtlas is a **self-hosted, local, multi-repository engineering intelligence system**. The user points it at one
directory containing many Git repositories:

```bash
repoatlas init ~/work/repos
```

RepoAtlas recursively discovers the Git repositories beneath that root, indexes them, derives useful relationships
between them, and maintains that knowledge incrementally.

### Target repository content

Primary languages:

```
Python
TypeScript / JavaScript
Bash / shell
```

Also present:

```
Ruby
Salt SLS
Jinja
YAML
JSON
Dockerfiles
```

### Questions V1 must be able to answer

```
Where is this behavior implemented?
Where does this configuration value originate?
What repositories depend on this component?
What is the most likely source of this error?
What package/version are we actually using?
What code path is responsible for this behavior?
```

### Scope boundary

V1 is **local repository intelligence only** and is **read-only**. Internet research and version-specific online
documentation retrieval are V2 (§22).

---

## 2. The trust rule

> The LLM may reason about evidence, but it must not invent evidence.

RepoAtlas learns repository facts through deterministic inspection wherever possible. The LLM is the reasoning
layer at the *end* of the pipeline. It is **not** the repository knowledge source.

The following must always come from deterministic parsing/indexing, never from model inference:

```
imports
functions
classes
dependency versions
environment-variable usage
Salt relationships
file locations
Git state
```

If evidence is insufficient, the system says:

```
UNKNOWN
```

or:

```
INSUFFICIENT EVIDENCE
```

rather than inventing an answer.

This rule is enforced **by code, not by prompt wording** — see §15 and `ARCHITECTURE.md` §11 (grounding
enforcement).

---

## 3. Technology and constraints

### Required

| Concern | Choice |
|---|---|
| Language | Python **3.13+** |
| Package/env manager | **uv** (`requires-python = ">=3.13"`) |
| CLI framework | **Typer** |
| Data modelling / validation | **Pydantic v2** |
| Testing | **pytest** |
| Linting / formatting | **ruff** |
| Type checking | **pyright** (mypy acceptable alternative) |
| Syntax parsing | **tree-sitter** where appropriate (see §7) |
| Embeddings | local model, **fastembed** (`BAAI/bge-small-en-v1.5`), ONNX, no PyTorch |
| LLM backend (V1) | **Ollama**, default model `gpt-oss:20b` |
| Storage | **SQLite** (WAL) with FTS5 |

### Configuration-driven model selection

The model backend **must be replaceable**. Do not hardcode RepoAtlas around Ollama or `gpt-oss`.

```yaml
llm:
  provider: ollama
  model: gpt-oss:20b
  base_url: http://localhost:11434
```

Future backends may include vLLM, llama.cpp, OpenAI-compatible local servers, Qwen models, and future open-weight
models. The embedding backend is likewise replaceable behind an interface.

### Non-goals / prohibitions

- **No LangChain** or other large agent framework unless there is a concrete, stated reason.
- **No Rust** in V1 unless profiling later proves it necessary.
- No paid APIs required for any V1 functionality.

### Environment notes (recorded 2026-09-01)

| Item | State | Consequence |
|---|---|---|
| System Python | 3.12.3 | uv installs and manages CPython 3.13 |
| `uv` | not installed | Installed as the first step of implementation |
| Ollama | not installed | `OllamaBackend` implemented but unexercised locally; `NullBackend` used in tests |
| SQLite | 3.45.1, FTS5 present | Exact search requires no extension |

**LLM availability is not required for the deterministic half of the system.** `init`, `sync`, `status`, `repos`
and `locate` must work fully with no LLM configured or reachable. `ask` and `debug` fail with a clear message and
exit code 4 when no reasoning model is available.

---

## 4. CLI surface

```bash
repoatlas init <directory>
repoatlas sync [repository]
repoatlas status [repository]
repoatlas repos
repoatlas ask "<question>"
repoatlas locate "<question>"
repoatlas debug "<question>"
```

Optional if straightforward:

```bash
repoatlas watch
```

### Command reference

| Command | Argument | Context behavior | LLM required |
|---|---|---|---|
| `init` | `<directory>` (required) | Resolves and persists the root; performs initial index | no |
| `sync` | `[repository]` optional | §6 resolution | no |
| `status` | `[repository]` optional | §6 resolution; repo-scoped or global | no |
| `repos` | — | Always global | no |
| `locate` | `"<question>"` | §6 resolution; primary repo bias | no (evidence-only mode) |
| `ask` | `"<question>"` | §6 resolution; primary repo bias | yes |
| `debug` | `"<question>"` | §6 resolution; primary repo bias | yes |

### Global options

```
--repo <name>        explicit repository (equivalent to positional where accepted)
--json               machine-readable output instead of the concise text format
--verbose / -v       diagnostics to stderr
--no-llm             force evidence-only mode (locate-style output) for ask/debug
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Runtime error (I/O, storage, unexpected failure) |
| 2 | Repository context could not be resolved (§6) |
| 3 | Not initialized, or index missing/unusable |
| 4 | Reasoning model unavailable or unusable |

Human-readable messages go to stderr; primary output to stdout.

### Constraints

- The CLI layer contains **no indexing logic**. It resolves context, calls a service, and renders (§ARCHITECTURE 1).
- `sync` never auto-registers a repository (§6).

---

## 5. Repository discovery

```bash
repoatlas init ~/work/repos
```

RepoAtlas must:

1. Resolve the supplied root to a canonical absolute path.
2. Recursively discover Git repositories beneath it.
3. Handle nested directory layouts.
4. Avoid indexing repositories twice.
5. Record repository metadata.
6. Detect languages.
7. Detect dependency manifests and lockfiles.
8. Perform the initial index.
9. Persist the configured root.

The user must **not** manually register repositories.

### Nested repositories

A Git repository inside another Git repository is a **separate managed repository**. Files belonging to an inner
repository are never indexed as part of the outer one.

### Default exclusions

```
.git
node_modules
.venv
venv
dist
build
target
.next
coverage
.cache
__pycache__
vendor
```

### `.repoatlasignore`

Supported with gitignore-like semantics where practical. Placed at the RepoAtlas root and/or inside individual
repositories.

Ignore precedence, most specific last:

```
built-in defaults → .gitignore (via git) → .repoatlasignore → secret patterns (§17, always applied)
```

### Repository metadata

Track at least:

```
name
canonical absolute path
current branch
HEAD SHA
dirty/clean state
detected languages
dependency manifests
lockfiles
last indexed timestamp
last indexed HEAD
```

**The current working tree is authoritative. Uncommitted changes matter.**

---

## 6. Directory-aware behavior

This is a **core V1 requirement**, not a convenience.

One reusable resolver is shared by `sync`, `status`, `ask`, `locate` and `debug`:

```python
class RepositoryContextResolver:
    def resolve(
        self,
        cwd: Path,
        explicit_repo: str | None = None,
    ) -> RepositoryContext: ...
```

### Resolution priority

```
1. Explicit repository argument
2. Current directory is inside a managed repository
3. Current directory equals the configured RepoAtlas root
4. Error
```

### The two valid implicit contexts

Without an explicit repository argument, exactly two directories resolve:

1. a directory **inside a managed repository** → that repository, or
2. the **exact configured RepoAtlas root** → global scope.

**Every other directory is an error.** This explicitly includes a directory *under* the root that is not inside a
managed repository. There is no subtree scope, no partial scope, and no proximity heuristic. RepoAtlas never
guesses scope.

### Case: inside a managed repository

Depth must not matter. From:

```
/work/repos/service-a/src/a/b/c/d/e/f/g/h
```

```bash
repoatlas sync
```

must identify `service-a` and sync **only** that repository. Resolve by walking upward, or via Git
repository-root resolution.

### Case: at the configured RepoAtlas root

If the configured root is `/work/repos`:

```bash
cd /work/repos
repoatlas sync
```

must sync **all** managed repositories.

### Case: outside managed repositories

```bash
cd /home/user
repoatlas sync
```

must exit non-zero (code 2) with:

```
Unable to determine repository.

You are not inside a RepoAtlas-managed repository and the current
directory is not the configured repository root.

Specify a repository:

  repoatlas sync service-a
```

**Do not silently sync everything.**

### Case: inside an unrelated Git repository

If the enclosing Git repository is not managed by RepoAtlas (code 2):

```
This Git repository is not managed by RepoAtlas.
```

Do **not** automatically register it during `sync`.

### Case: explicit repository

Works from anywhere, and overrides current-directory inference:

```bash
repoatlas sync service-a
```

An explicit name that matches no managed repository is an error listing the available repositories. An ambiguous
name is an error listing the candidates.

### Case: nested repositories

Given:

```
/work/repos/platform/             Git repo
/work/repos/platform/tools/foo/   separate Git repo
```

with the current directory inside `foo`, choose `foo`. **The closest managed repository wins.**

### Query context semantics

The same rules govern `ask`, `locate`, `debug` and `status`.

- Inside a repository, that repository is the **primary** search context — but RepoAtlas must still follow
  justified cross-repository relationships. A question asked inside `service-a` may retrieve evidence from
  `common-lib` when `service-a DEPENDS_ON common-lib`.
- At the configured root, queries have **global multi-repository scope**.
- Outside the managed tree, require explicit context rather than guessing.

### Edge cases that must be handled

```
paths with spaces
trailing slashes
relative paths
symlinked paths (canonicalize before comparison)
case sensitivity per platform
the root itself being a Git repository
```

---

## 7. Language parsing

Parsers are adapters behind one protocol:

```python
class LanguageParser(Protocol):
    def supports(self, path: Path) -> bool: ...
    def parse(self, path: Path, content: str) -> ParsedFile: ...
```

Recognize at least: Python, TypeScript, JavaScript, Bash, Ruby, Salt SLS, Jinja, YAML, JSON, Dockerfile.

**Rules**

- Prefer deterministic parsing. Never use LLM inference for basic source structure.
- Parsers are pure functions of `(path, content)`: no storage access, no LLM access, no filesystem traversal,
  no execution.
- Parsers emit *unresolved* references; resolution to graph edges happens in the linker (§8, ARCHITECTURE §5).
- A parse failure produces a diagnostic and partial results — never an aborted sync.

### 7.1 Python

Parsed with the **stdlib `ast`** module (exact, zero dependencies, accurate line ranges).

Extract at least:

```
imports (absolute and relative, with aliases)
modules
functions
async functions
classes
methods
basic decorators
basic call/reference names
```

Plus environment-variable reads (§10): `os.getenv(...)`, `os.environ[...]`, `os.environ.get(...)`.

### 7.2 TypeScript / JavaScript

Parsed with **tree-sitter**.

Extract at least:

```
imports
exports
functions
classes
interfaces where practical
types where practical
calls
package imports
relative imports
```

Plus environment-variable reads: `process.env.X`, `process.env["X"]`.

### 7.3 Bash

**Bash is first-class code**, not a config format. Parsed with tree-sitter.

Extract at least:

```
functions
source / .
executed commands
environment variables read
environment variables exported
script references
Python/Ruby script invocation
Salt invocation references
```

**Never execute scripts. Static analysis only.**

### 7.4 Ruby

Parsed with tree-sitter. Extract basic:

```
require
require_relative
modules
classes
methods
gem references
basic calls where reliable
```

### 7.5 Salt (`.sls`)

Salt is important. **Do not treat `.sls` as ordinary YAML.** SLS files are Jinja-templated YAML and frequently do
not parse as YAML until the Jinja layer is neutralized.

Strategy: neutralize Jinja into placeholders, parse the remaining YAML leniently, and separately extract Jinja
facts from the raw text. If YAML parsing still fails, fall back to a line-oriented scan that recovers state IDs and
requisites at reduced confidence, recording a diagnostic.

Extract where practical:

```
state IDs
state module/function
include
extend
require
require_in
watch
watch_in
onchanges
onchanges_in
source
template
pillar references
grain references
salt execution function references
```

**Only create relationships that can be supported deterministically.**

### 7.6 Jinja

Capture useful relationships:

```
includes
imports
extends
pillar references
grain references
salt calls
```

**Do not execute or render templates.**

### 7.7 YAML / JSON

Treat as structured configuration. Store useful keys/paths for search where appropriate, with line provenance.
Very large or generated documents (e.g. lockfiles) are handled by the dependency layer (§13), not indexed as prose.

### 7.8 Dockerfile

Extract at least:

```
FROM
RUN
COPY
ADD
ENV
ARG
ENTRYPOINT
CMD
WORKDIR
```

---

## 8. Knowledge representation

The graph must **not** be language-specific.

### Entity types

```
Repository
File
Symbol
Function
Class
Module
Script
Command
Package
Dependency
SaltState
Template
Config
EnvironmentVariable
Service
Test
```

### Relationship types

```
CONTAINS
DEFINES
IMPORTS
EXPORTS
REFERENCES
CALLS
DEPENDS_ON
EXECUTES
SOURCES
INCLUDES
REQUIRES
WATCHES
READS
WRITES
CONFIGURES
MANAGES
GENERATES
```

**Do not overengineer the ontology.** Add a type only when a concrete retrieval need requires distinguishing it.

Every relationship carries a **confidence** value and full provenance (§11).

---

## 9. Cross-repository relationships

Create high-confidence relationships automatically where the evidence is deterministic:

```
local npm dependency
local Python package dependency
shell script referencing another repo
Salt source relationship
shared package mapping
```

Example:

```
repo-a package.json:  dependency @company/common
repo-b package.json:  name       @company/common
```

produces:

```
repo-a DEPENDS_ON repo-b
```

**Rules**

- Match on **exact normalized package names** within the same ecosystem (PEP 503 normalization for Python; npm
  scope-aware comparison for Node; gem names for Ruby).
- **Never** infer a relationship from similar or fuzzily-matching names.
- If the target is not present in the managed set, record an unresolved reference so the edge can be created later
  when that repository is added — never invent the target.

---

## 10. Environment variables

Environment variables are **first-class searchable entities**, interned globally by name.

Capture at least:

```
READS      code reads the variable
EXPORTS    a script or Dockerfile sets it into the environment
CONFIGURES a deployment/config layer determines its value
```

across all supported languages.

The goal: `os.getenv("SERVICE_HOST")` in a Python service and a Salt state that configures `SERVICE_HOST` must be
connectable through the graph, so `locate "Where is SERVICE_HOST configured?"` returns both with file-and-line
evidence.

---

## 11. Provenance

**Every deterministic fact must retain provenance.** At minimum:

```
repository
file
line or line range where available
parser
content hash
```

Answers must be able to cite these locations, e.g.:

```
service-a/src/config.py:18
deployment/service/init.sls:42-48
```

A fact without provenance is a bug. Provenance is stored on the fact row itself (not derived at query time), which
is also what makes stale-fact removal exact (§12).

---

## 12. Incremental synchronization and branch awareness

`repoatlas sync` must **not** re-index everything.

Track per file:

```
unchanged
changed
new
deleted
```

Using:

```
Git state
HEAD
working-tree state
content hashes
```

Actions:

| State | Action |
|---|---|
| unchanged | skip |
| changed | remove stale facts → reparse → insert new facts → update search indexes |
| new | index |
| deleted | remove facts, graph relationships and search entries |

### Correctness dominates the optimization

**The content hash is the sole authority on whether a file changed.** The stat check (size + `mtime_ns`) is only a
permitted shortcut for skipping the hash, and it is **disabled** whenever anything makes stat untrustworthy:

- `head_sha` differs from `last_indexed_head` — `checkout`, `switch`, `pull`, `rebase`, `reset` can all rewrite
  files while preserving or back-dating mtime;
- the repository's dirty/clean state changed since the last sync;
- the previous sync did not complete cleanly;
- the schema version, parser version, or embedding model recorded in storage changed;
- `--full` / `--rehash` was passed.

In those cases every file is hashed. Otherwise: stat match ⇒ skip, stat mismatch ⇒ hash ⇒ reparse only if the hash
changed. Filesystems with coarse mtime resolution cost an extra rehash, never a missed update.

### Branch awareness

Correctly handle `git checkout`, `git switch`, `git pull`, `git rebase`, `git reset`.

After `repoatlas sync`, the knowledge base must reflect the **current working tree**. Facts from the previous
branch must not remain.

### Invariants (tested)

1. After any sync, **no fact exists whose file reference points to content not currently on disk**.
2. An incremental sync across any HEAD change yields **state equivalent to a full reindex** of the same tree.

Stale-data bugs are serious: a deleted symbol must not continue appearing in answers.

---

## 13. Dependency detection

### Python

```
pyproject.toml
uv.lock
poetry.lock
requirements.txt
requirements-*.txt
setup.py          (static inspection only — never executed)
setup.cfg
```

### Node

```
package.json
pnpm-lock.yaml
package-lock.json
yarn.lock
```

### Ruby

```
Gemfile
Gemfile.lock
```

Also record reliable dependency references found in `Dockerfile`, Bash and Salt.

### Dependency model

Distinguish:

```
package
ecosystem
declared version/constraint
resolved version
manifest
lockfile
repository
```

Example:

```
declared:  foo ^4.1
resolved:  foo 4.7.2
```

**Store both.** The resolved version becomes essential in V2 for version-specific documentation retrieval (§22).

`setup.py` is parsed statically via `ast`; it is never imported or executed (§17).

---

## 14. Search and retrieval

### Exact search — mandatory

Exact/lexical search must work well for:

```
errors
symbol names
environment variables
configuration keys
package names
filenames
commands
Salt state IDs
```

**Vector search must not replace exact search.** Where the lexical index cannot guarantee exactness (tokenizer
limits), results are verified against file content before being presented as exact matches.

### Semantic search

Local semantic retrieval with a local embedding model, backend replaceable, no paid APIs. Helps with:

```
Where is deployment configuration generated?
What controls startup?
Where does this value come from?
```

### Retrieval funnel

```
question
    ↓
directory context
    ↓
exact/symbol search
    ↓
semantic search
    ↓
graph expansion
    ↓
ranking/deduplication
    ↓
small evidence set
    ↓
LLM
```

Combine: repository context, exact matches, symbol matches, semantic similarity, graph relationships, dependency
relationships.

**Do not send entire repositories to the LLM.** Reduce the candidate set before reasoning; the evidence set is
bounded by both item count and token budget.

Design the ranking layer so a **reranker can be added later** without changing callers.

---

## 15. LLM contract

### The model may

```
interpret questions
compare evidence
reason about evidence
generate hypotheses
rank possible causes
summarize findings
suggest the next diagnostic action
```

### The model may not invent

```
files
symbols
versions
imports
repository relationships
configuration
errors
dependency facts
```

### Facts vs hypotheses

Internally distinguish:

```
FACT
OBSERVATION
HYPOTHESIS
```

Facts require provenance. Hypotheses require supporting facts.

```
FACT
service/config.py:18 reads SERVICE_HOST

FACT
deployment/init.sls:42 configures SERVICE_HOST

HYPOTHESIS
incorrect deployment configuration may explain the runtime failure
confidence: 0.76
```

### Structured output

```python
class Evidence(BaseModel):
    repository: str
    path: str
    line_start: int | None = None
    line_end: int | None = None
    description: str


class Finding(BaseModel):
    statement: str
    confidence: float
    evidence: list[Evidence]


class RepoAtlasAnswer(BaseModel):
    summary: str
    findings: list[Finding]
    unknowns: list[str]
    next_check: str | None = None
```

Validate model output before rendering. On validation failure: one repair attempt, then return
`INSUFFICIENT EVIDENCE` rather than rendering unvalidated output.

### Citation verification (enforcement of §2)

After validation, a **deterministic pass verifies every citation against the evidence set that was actually
supplied to the model**:

- an `Evidence` item naming a repository/path not present in the supplied evidence set is **dropped**;
- a line range outside the supplied span is clamped or dropped;
- a `Finding` left with no surviving evidence is **demoted to an `unknown`**;
- if all findings are demoted, the answer becomes `INSUFFICIENT EVIDENCE`.

The trust rule is therefore enforced by code, not by prompt wording.

---

## 16. Output format

Default terminal output must be **short**, emphasizing:

```
answer
evidence
confidence
next action
```

Example:

```
CAUSE

SERVICE_HOST is populated by the Salt deployment layer.

EVIDENCE

deployment/service/init.sls:42
  configures SERVICE_HOST

service/config.py:18
  reads SERVICE_HOST

CONFIDENCE

HIGH

NEXT

Inspect the pillar value used by the Salt state.
```

Avoid giant generic explanations.

### Per-command bias

- **`locate`** — bias toward definitions, implementation locations, configuration origins, references, scripts,
  states, files. Keep prose minimal. Works without an LLM (evidence-only mode).
- **`ask`** — general engineering question, using current-directory repository context automatically.
- **`debug`** — bias toward exact error strings, related code, configuration, dependencies, execution
  relationships, likely causes. Clearly separate **facts**, **hypotheses**, **unknowns** and the **next diagnostic
  check**.

### `status`

Inside a repository, shows that repository. At the RepoAtlas root, shows global status:

```
Root: /work/repos
Repositories: 37
Indexed files: 83,214
Symbols: 281,442
Relationships: 603,118
Last sync: ...
Index state: current
```

### `repos`

```
service-a       Python, Bash
service-b       TypeScript
deployment      Salt, Jinja, Bash
common          Python, TypeScript
tools           Python, Ruby
```

### Index freshness

RepoAtlas must track whether its index may be stale and must **not knowingly present stale information as
current**. Where a repository's HEAD or dirty state differs from what was indexed, say so.

V1 may require an explicit `repoatlas sync` before freshness is guaranteed. Automatic pre-query sync is **not**
required.

### `--json`

Every command supports `--json`, emitting the validated Pydantic models directly for machine consumption.

---

## 17. Security and secrets

**Repositories are untrusted input. STATIC ANALYSIS ONLY.**

Never:

```
execute repository Bash
import repository Python modules
execute Ruby
render/execute Salt
run npm lifecycle scripts
run Dockerfiles
run discovered commands
```

`setup.py` is read with `ast`, never imported. Templates are neutralized, never rendered. Git is invoked only for
read-only plumbing commands, with hooks disabled (see `ARCHITECTURE.md` §8).

Repositories should be mounted read-only in Docker.

### Secrets

Ignore obvious secret files:

```
.env
.env.*
*.pem
*.key
id_rsa
id_ed25519
```

Do not knowingly send secrets into:

```
embeddings
LLM prompts
logs
```

Exclusion is applied **before** content is read for chunking, so secret content never enters the chunk store,
embeddings, prompts or logs. Design so stronger secret scanning (entropy/pattern detection on file content) can be
added later at the same seam.

---

## 18. Docker

Provide `Dockerfile` and `docker-compose.yml`.

Services:

```
repoatlas
ollama
```

- Repositories mounted **read-only**.
- RepoAtlas state persisted in a **separate** volume.
- **Do not** add SearXNG or other research infrastructure in V1.

---

## 19. Testing requirements

Use **synthetic fixture repositories**. **Do not use actual work repositories in automated tests.**

### Discovery

```
single repository
multiple repositories
nested paths
nested Git repos
ignored paths
```

### Context

```
repo root
one directory deep
ten directories deep
configured RepoAtlas root
unrelated directory
unmanaged Git repo
explicit repo
explicit repo overrides cwd
closest nested repo
unknown repo
paths with spaces
relative paths
symlink behavior where practical
```

### Parsers

```
Python
TypeScript
JavaScript
Bash
Ruby
Salt
Jinja
YAML
JSON
Dockerfile
```

### Dependencies

```
Python
Node
Ruby
declared vs resolved versions
```

### Sync

```
unchanged
changed
new
deleted
branch switch
dirty working tree
stale relationship removal
```

### Retrieval

```
exact search
symbol lookup
environment variable
semantic question
primary repo bias
cross-repo traversal
```

### LLM

```
valid structured output
invalid structured output
missing evidence
unknown answer
citation rendering
```

LLM tests use the deterministic `NullBackend`/scripted backend — no network, no Ollama dependency in CI.

---

## 20. Performance principles

Do not prematurely optimize. But **never**:

```
scan every repo for every question
re-index everything after one file changes
re-embed unchanged data
send entire repositories to the model
use the LLM during deterministic parsing
```

Use incremental state and bounded retrieval.

---

## 21. V1 success scenario

Fixture repositories:

```
/work/repos/
├── python-service/
├── ts-service/
├── deployment/
└── scripts/
```

with relationships:

```
Python reads SERVICE_HOST
Salt configures SERVICE_HOST
Salt obtains the value from configuration/pillar
Bash invokes Python tooling
TypeScript depends on another local repo
```

Then prove:

1. `repoatlas init /work/repos` discovers everything.
2. From deeply inside `python-service`:
   ```bash
   repoatlas locate "Where is SERVICE_HOST configured?"
   ```
   infers `python-service` as primary context, follows relationships into `deployment`, and returns file-backed
   evidence.
3. Modify one deployment file; from deep inside `deployment`, `repoatlas sync` syncs **only** `deployment`.
4. From `/work/repos`, `repoatlas sync` syncs everything.
5. From `/home/user`, `repoatlas sync` errors (exit 2) and requests an explicit repository.

---

## 22. Deferred to V2 and beyond

### V2 — do not implement yet

```
internet research
SearXNG
version-specific documentation fetching
documentation cache
GitHub issues/PRs
web provenance
advanced reranking
Git-history debugging
regression introduction analysis
blast-radius analysis
dependency compatibility research
```

Design interfaces so V2 can add:

```
question mentions dependency
        ↓
identify repository
        ↓
determine resolved dependency version
        ↓
retrieve official docs for EXACT version
        ↓
retrieve repository evidence
        ↓
reason over both
        ↓
grounded answer
```

This is why resolved versions are stored in V1 (§13) despite being otherwise unused.

### Not in V1 at all

```
automatic code changes
automatic bug fixes
commits
pushes
git pull
central team server
web UI
IDE integration
distributed indexing
multi-agent architecture
```

**V1 is read-only.**

---

## 23. Implementation checklist

Status markers: `[ ]` not started · `[~]` in progress · `[x]` done.

Work proceeds in **slices**, not in strict phase order — the point is to prove a working vertical slice before
building breadth. Do not build all parsers before the spine works end to end.

### Slice A — Spine (no LLM required) — **COMPLETE**

Proves: `directory → discovery → context → indexing → exact retrieval → evidence`.

Delivered 2026-09-02. 139 tests green; ruff, ruff format and pyright clean.

- [x] **1. Project foundation** — uv + CPython 3.13.15, `pyproject.toml`, package skeleton, ruff, pyright, pytest
- [x] **2. Configuration + CLI** — Pydantic settings, YAML config load/save, XDG paths, Typer app,
      exit codes 0/1/2/3/4, `--json`, `--verbose`
- [x] **3. Repository discovery** — recursive walk, nested repos, exclusions, `.repoatlasignore`, language
      detection, manifest/lockfile detection, `init`
- [x] **4. Shared directory-context resolver** — §6 in full; 23 cases covering depth, nesting, symlinks,
      spaces, relative paths, unmanaged repos and every error path
- [x] **5. File-state tracking** — hardened git wrapper, `git ls-files` enumeration, blake2b hashing,
      `files` table
- [x] **6. Incremental sync** — §12 including all five forced-rehash triggers and both invariants
- [x] **7. Parser abstraction** — `LanguageParser` protocol, registry/dispatch, `ParsedFile` / `ParsedSymbol` /
      `ParsedReference` contract, diagnostics. Landed **before** any concrete parser.
- [x] **8a. Python parser** — stdlib `ast`, implemented *through* the abstraction; no Python-specific path
      exists in the spine
- [x] **11. Persistent knowledge representation** — entities (two-tier), edges, provenance, cascade deletion,
      orphan GC, migrations
- [x] **13. Exact search** — FTS5 chunk + entity indexes, identifier-aware tokenization, literal-substring
      exactness verification
- [x] **18a. `status`, `repos`, `locate`** — evidence-only rendering, no LLM required
- [x] **A-gate:** fixture repos + tests for discovery, context, sync and exact retrieval all green, plus the
      §21 success scenario driven end to end through the CLI

**Deferred within Slice A, by design:**

- Cross-file and cross-repository symbol resolution. The linker resolves environment variables, modules and
  commands as interned entities, and symbol references within a file. Resolving a reference *across* files needs
  the import graph, which arrives with phase 12 in Slice C. Until then such references are counted as unresolved
  rather than guessed — see §2.
- `watch` (optional in §4) is not implemented.

### Slice B — Reasoning — **COMPLETE**

Delivered 2026-09-02. 204 tests green; ruff, ruff format and pyright clean.

- [x] **16. Ollama model abstraction** — `ReasoningModel` protocol, `OllamaBackend`, `NullBackend`,
      `ScriptedBackend` for tests, config-driven selection via `llm.provider`, availability detection
      (server reachable **and** model pulled) → exit code 4
- [x] **17. Structured grounded answers** — versioned prompt templates, JSON-schema-constrained decoding,
      Pydantic validation, one repair retry, then `INSUFFICIENT EVIDENCE` rather than unvalidated output,
      and **citation verification** (§15)
- [x] **18b. `ask` and `debug`** — `debug` additionally searches the question verbatim so unquoted error
      strings match; FACT/HYPOTHESIS/UNKNOWN separation; concise renderers; `--no-llm` evidence-only mode

**Verified:** a model answer citing a file it was never shown is stripped and demoted to an unknown, proven
end to end over a real HTTP socket as well as in unit tests. Confidence asserted by the model does not protect
an unsupported citation.

**Note on `--no-llm`:** V1_SPEC §4 specifies evidence-only output, so the flag skips reasoning entirely rather
than routing through `NullBackend`. `NullBackend` remains the `llm.provider: none` backend and a test double.
`ARCHITECTURE.md` §11 was corrected to match.

### Slice C — Breadth — **COMPLETE**

Delivered 2026-09-02. 312 tests green; ruff, ruff format and pyright clean.

- [x] **8b. TypeScript / JavaScript parser** — tree-sitter (ts, tsx, js, jsx, mjs, cjs); imports with
      package/relative distinction, exports, classes, interfaces, types, methods, heritage, calls,
      `require`/dynamic `import`, and `process.env` in both forms
- [x] **8c. Bash parser** — tree-sitter, first-class treatment: functions, `source`, executed commands and
      scripts, interpreter invocations, Salt state invocations, and exported vs read environment variables
- [x] **9. Ruby / Salt / Jinja / structured config parsers** — Ruby via tree-sitter; Salt via Jinja
      neutralization → lenient YAML → line-scan fallback at reduced confidence, with the state/pillar-data
      split; Jinja relationships from the raw text; YAML/JSON keys with line provenance; Dockerfile
- [x] **10. Dependency detection and version resolution** — twelve parsers across three ecosystems,
      `declared` / `resolved` / `provided` rows with provenance, and the join view that keeps both halves
- [x] **12. Cross-repository relationships** — exact normalized-name matching (PEP 503, npm scope-aware, gem),
      the two link passes, and `unresolved_refs` retained so a repository added later gains its edges without
      a reparse

**The Slice A deferral is closed.** Cross-file and cross-repository symbol resolution now works, through the
import graph rather than through name equality: a call is wired to a definition in another file only when the
calling file actually imported that name. `ARCHITECTURE.md` §5 replaces the Slice A table with the full set of
resolution rules and the three refusals — computed targets, ambiguous targets and unimported names all resolve
to nothing.

**Verified end to end** against the §21 fixture set: Python reads `SERVICE_HOST`, Salt configures it, Salt
obtains the value from the pillar file, Bash invokes Python tooling *across a repository boundary*, and both
`python-service` and `ts-service` are linked to `common-lib` — one through PEP 503 normalization
(`common_lib` ≡ `common-lib`), one through a scoped npm name.

**Deferred within Slice C, by design:**

- `watch` (optional in §4) is still not implemented.
- Salt `extend:` produces a reference but the linker does not apply the override semantics; V1 records the
  relationship, not the merged result.

### Slice D — Ranking

- [ ] **14. Semantic retrieval** — fastembed, chunk embeddings, `VectorIndex`, incremental re-embedding only of
      changed chunks
- [ ] **15. Context-aware retrieval** — query analysis, graph expansion, rank fusion, primary-repo bias,
      deduplication, token budget, `NoopReranker` seam

### Slice E — Delivery

- [ ] **19. Docker** — `Dockerfile`, `docker-compose.yml`, read-only repo mount, separate state volume
- [ ] **20. End-to-end tests and hardening** — the §21 success scenario as an automated test, full §19 matrix,
      performance sanity checks

### Cross-cutting, verified continuously

- [x] Provenance present on every fact type (§11) — `tests/test_storage.py`, `tests/test_retrieval.py`
- [x] No-stale-facts invariants hold after every sync scenario (§12) — `tests/test_sync.py`
- [x] Secret exclusion applied before content is read (§17) — `tests/test_sync.py`
- [x] Layering rules hold — parsers touch neither storage nor LLM; CLI contains no indexing logic; SQL appears
      only in `storage/sqlite/`; only `gitstate/` invokes git (`ARCHITECTURE.md` §1) — `tests/test_layering.py`
- [x] Both parser layers are pure functions of `(path, content)` — no I/O and no execution anywhere in
      `parsers/` or `dependencies/`, which is what makes reading an untrusted `setup.py` safe (§17) —
      `tests/test_layering.py`
- [x] The global link pass is idempotent, and every edge it owns is rebuilt rather than patched, so an
      incremental sync still equals a full reindex once cross-repository edges exist (§12) —
      `tests/test_linking.py`, `tests/test_sync.py`
