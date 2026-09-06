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
| 1 | Unexpected/internal failure — including a defect in RepoAtlas itself |
| 2 | Repository context could not be resolved (§6) |
| 3 | Not initialized, or index missing/unusable |
| 4 | Reasoning model unavailable or unusable |
| 5 | Another RepoAtlas synchronization is already in progress (§12) |

Human-readable messages go to stderr; primary output to stdout.

Code 5 is separate from code 1 on purpose: contention is an operational condition a wrapper script may
sensibly retry, where a runtime error is not.

#### What exit 3 covers, and what it does not

Exit 3 means *there is no index this command can use*. Five distinct states share it, and the message is what
tells them apart, because the remedy differs in each:

| State | Message says | Remedy |
|---|---|---|
| Not initialized | no root is configured | `repoatlas init <directory>` |
| Corrupt | the file is not a database, or its image is malformed | move it aside and re-`init` |
| Inaccessible | the index cannot be read or written | fix the path or the permissions |
| Unsupported schema | it was written by a newer RepoAtlas, is missing a required object, or is a shape no migration produces | match the build, or start a fresh index |
| **Not ours** | the file is a valid SQLite database that is not a RepoAtlas index, or belongs to another application | point `REPOATLAS_DB` elsewhere, or move the file aside |

An inaccessible index is never described as corrupt. A database whose permissions are merely wrong is intact, and
telling an operator to replace it would destroy a healthy index.

A database RepoAtlas does not own is never described as corrupt either, and the distinction is not cosmetic: it is
perfectly good SQLite belonging to something else, and "corrupt" would send an operator to repair or discard a file
with nothing wrong with it. The message names the *path* and never the contents — the likeliest cause is a
`REPOATLAS_DB` pointing at the wrong file, and a diagnostic that helpfully listed the tables would disclose the
schema of whatever that turned out to be.

**Exit 3 is not a catch-all.** An unexpected exception — a `TypeError`, a bug in a parser, a SQLite result code
RepoAtlas has not positively identified as an index condition — stays exit 1. Reporting a defect as "your index is
corrupt" would invite the user to delete their index *and* hide the bug.

Related states keep their own codes: writer-lock contention is 5, a migration another process is still applying is
1 (transient, retry), and a reasoning-model failure is 4. When more than one is true, the index is opened first, so
an unusable index is reported ahead of the model backend (which is never contacted), the writer lock, and
repository-context resolution.

**RepoAtlas never repairs, replaces, renames or deletes an index automatically**, including during `init`. A
refused database is left byte-for-byte as it was found.

#### The index is a file RepoAtlas must prove it owns

The configured index path is a path to *a file*, and nothing guarantees that file is RepoAtlas's. Before this was
enforced, a SQLite database containing a `payroll` table and `user_version = 0` was read as "a fresh RepoAtlas
database awaiting its first migration": `repoatlas init` created twenty-one tables in it, ran every migration and
stamped the schema version. No foreign row was destroyed, because no table name happened to collide.

> **Invariant.** `user_version` is a schema version, not proof of database ownership.

> **Invariant.** A non-empty SQLite database is never mutated unless RepoAtlas identity is already present, or its
> schema is conclusively recognized as a committed historical RepoAtlas schema.

> **Invariant.** Unknown SQLite state is refused before identity assignment or migration.

> **Invariant.** Read commands do not write ownership metadata merely to upgrade a legacy index.

> **Invariant.** Refusing a foreign database is byte-preserving: the file, its `user_version`, its `application_id`
> and its sibling files are exactly as they were found.

Identity is `PRAGMA application_id`, SQLite's own header field for naming the format a file belongs to. It is asked
about *before* `user_version`, because the two answer different questions — "is this a RepoAtlas index?" and "which
RepoAtlas schema is it?" — and only the first licenses a write.

| The file at the index path | RepoAtlas |
|---|---|
| does not exist | creates it, claims it, initializes the schema |
| is zero bytes | initializes it — SQLite reads zero bytes as an empty database, and it holds no foreign state |
| is valid SQLite with no user objects | initializes it — an empty container has no owner to displace |
| carries RepoAtlas's `application_id` | opens it, migrating if the schema version is behind |
| carries another application's `application_id` | **refuses**, unchanged |
| is unmarked, and its schema is exactly a committed RepoAtlas schema | recognizes it, and marks it on the next write |
| is unmarked, and its schema is anything else | **refuses**, unchanged |
| is stamped past this build's schema version | **refuses**, unchanged (the existing rule) |
| is not a SQLite database at all | **refuses** as corrupt, unchanged (the existing rule) |

"No user objects" excludes SQLite's own reserved `sqlite_*` bookkeeping and the connection's temporary objects, and
includes views, indexes and triggers — a database whose only object is a view has no tables and is plainly not empty.

**Historical indexes.** Every RepoAtlas index written before identity existed carries no marker, and none of them
may suddenly read as foreign. They are recognized by schema *shape*, compared in full against the states the
committed migrations actually produce — a shared table name is never evidence — and this includes the partially
applied migration Pass 2 recovers from. A recognized index is marked by the next command that writes, never by a
read: `status` on a pre-marker index leaves it byte-identical, and a read-only historical index stays readable
rather than failing on a metadata write nobody asked for.

### Constraints

- The CLI layer contains **no indexing logic**. It resolves context, calls a service, and renders (§ARCHITECTURE 1).
- `sync` never auto-registers a repository from **outside** the managed root (§6). Membership *within* the
  root is part of what a sync synchronizes (§12).

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

### Membership is re-derived, not snapshotted

> **Invariant.** A managed root is authoritative for repository membership; registration is not a permanent
> snapshot taken only at `init` time.

> **Invariant.** `status` cannot report `current` when a repository that current discovery would manage remains
> unregistered.

> **Invariant.** A successful `sync` leaves every newly discovered repository either indexed and current, or
> explicitly reported as failed.

> **Invariant.** Repository-set reconciliation and content synchronization use the same discovery semantics as
> initialization.

`init` establishes the **root**. It does not take a census that everything afterwards believes. A repository
created beneath the managed root the day after initialization is part of that root, and ordinary synchronization
finds it:

| Command | Repository set | Repository contents |
|---|---|---|
| `init` | discovers and registers everything beneath the root | indexes all of it |
| `sync` | rediscovers and reconciles, when the run covers the root | indexes the reconciled set |
| `status` | rediscovers and **reports**, at every scope, writing nothing | reports per-repository freshness |
| `repos` | lists the **registered** set | — |

Requiring a re-run of `init` to see a new repository asks the operator to already know the thing the tool exists
to tell them — and offers no signal that they should. `status` said *current*, `sync` said *complete*, and
`locate` said the symbol did not exist.

Discovery is one implementation, used three times. `sync` and `status` do not carry a simplified walk of their
own: a second walker would be a second set of answers to "what is in this root?", and the exclusions,
`.repoatlasignore` rules, nested-repository semantics, `.git`-file recognition, symlink policy, containment,
cycle detection and unreadable-path reporting would have to be right in both.

**Ignored and excluded directories do not make a workspace stale.** Only a repository discovery would legitimately
register counts, because the drift signal is computed from discovery's own output rather than from a separate
notion of what is out there.

### Recursion is not bounded by depth

"Recursively discover" means to the bottom of the tree. A repository is never omitted for being deeply nested, and
there is no configurable depth setting — a ceiling of six levels used to exist, was documented nowhere, and made
`init` exit 0 reporting a repository set it knew was truncated.

Traversal is still bounded, by things that are about the tree rather than about a number:

| Bound | Effect |
|---|---|
| default exclusions | `node_modules`, `.venv`, `dist`, `vendor`, … are not descended |
| `.repoatlasignore` | matched on the root-relative path, at any depth |
| `.git` | never descended into, whatever `exclusions` is set to |
| directory symlinks | not followed; when explicitly enabled, they must still resolve **inside** the managed root |
| directory identity | each directory is entered once, keyed on `(device, inode)`, so any cycle terminates |

A directory RepoAtlas is refused permission to read is skipped rather than fatal, and is **named in the output**: a
subtree that could not be read has not been shown to be empty.

Discovery order is deterministic — the repository set and its order depend on the tree, never on filesystem
enumeration order — and repeating `init` over the same tree changes nothing.

> **Invariant.** A valid, non-ignored repository beneath the managed root is not omitted solely because of nesting
> depth.

> **Invariant.** Traversal termination is guaranteed by filesystem and symlink rules, not by silently truncating
> discovery at an arbitrary depth.

### Nested repositories

A Git repository inside another Git repository is a **separate managed repository**. Files belonging to an inner
repository are never indexed as part of the outer one.

### Repository identity

**A repository's name is not its identity.** Two managed repositories may share a directory basename — `teamA/common`
and `teamB/common` are both legitimate — and they may additionally share a package name, folder names and symbol
names without becoming the same repository.

Every internal identity used for storage, graph nodes, entity keys, evidence, rank fusion, deduplication, grounding
and cross-repository linking must distinguish repositories unambiguously. The display name is a **presentation**
concern and must not appear in any of them.

Two invariants follow, and both are asserted in the adversarial suite:

- repository display names are never used as provenance identity;
- two repository sources with identical display name, relative path, line range and content remain **distinct**
  evidence sources.

A repository that is moved or renamed is a removal plus a discovery, not the same repository at a new path; the
removed identity must not survive anywhere. Where a display name is genuinely ambiguous — in `repos`, in evidence,
in `--repo` — output disambiguates and selection refuses rather than choosing. Ambiguity is reported, never resolved.

### Moving the managed root

The managed root may be renamed or moved. Re-running `init` against its new location reconciles the registered
repositories to it rather than registering a second copy of each.

A repository is identified by its path relative to the managed root, and moving the root does not change that: a
repository at `service-a` under the old root is the same repository at `service-a` under the new one. Its database
row, its identity and its indexed facts are carried across; only its recorded location changes.

| The root you point `init` at | RepoAtlas |
|---|---|
| the same root as before | reconciles as usual; no relocation is considered |
| the root, renamed or moved, original gone | **relocates** every repository, keeping identities and indexed facts |
| a copy of the root, original deleted | **relocates** — a copied-then-deleted workspace is still a move |
| a copy of the root, **original still present** | **refuses**, naming both locations, and changes nothing |
| a root whose repositories share slugs with unrelated registered ones, originals gone | relocates the rows and **replaces their contents** — `init` reindexes, so no stale fact is reported as current |
| a root in which a repository moved internally | not a relocation: the slug changed, so the old identity is retired and the new one discovered |

**Two live directories are never merged.** If a repository is registered at a path that still exists, and the root
being initialized contains a different repository with the same root-relative identity, RepoAtlas refuses. Both
directories are real, both claim one identity, and nothing in the identity distinguishes them — so choosing is the
operator's, not RepoAtlas's. The message names the registered path, the discovered path and the identity they are
competing for, exits non-zero, and leaves the index and the configured root exactly as they were.

**A relocation is all-or-nothing.** Every repository in the set moves in one transaction, and the configured root
is only rewritten once that transaction has committed. There is no state in which some repositories point at the
old root and some at the new one, and no state in which the configuration names one root while the index names the
other. An interrupted relocation leaves the workspace wholly at the old root, and re-running `init` reaches the
same result.

**Freshness is not assumed.** `init` reindexes what it discovers, so a relocated repository whose contents also
changed is brought up to date in the same run: modified, added and deleted files, a different HEAD, a new branch
and uncommitted changes are all detected normally. Relocation rebinds *where* a repository is; it never asserts
that its contents are unchanged.

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

### File enumeration when Git is unavailable

Repository *contents* are enumerated with `git ls-files`, which gives real `.gitignore` semantics without
reimplementing them. When git cannot answer — the binary is missing, or the command fails — RepoAtlas walks the
working tree itself rather than indexing nothing.

**The fallback is complete.** It enumerates every admissible file in the repository at any nesting depth. There is
no repository-relative depth ceiling; a file 500 directories below the repository root is indexed exactly like one
at the top. A previous build stopped at depth 64 and reported success, so a deeper file was absent from the index
with nothing said about it — the sync was not wrong about what it indexed, only about what it had looked at.

Both enumerators feed the same admission rules, so what is indexed does not depend on which one ran:

| Rule | Primary | Fallback |
|---|---|---|
| obvious-secret filenames (`.env`, `id_rsa`, `*.pem`, …) | refused | refused |
| built-in exclusions (`node_modules`, `.venv`, `dist`, …) | refused | refused |
| `.repoatlasignore` | applied | applied |
| `.gitignore` | applied by git | **not applied** — the one documented difference |
| `.git` internals | never listed | never walked |
| files inside a nested managed repository | owned by that repository | owned by that repository |
| symlinks | refused by containment | never followed |
| containment, size and binary rules | applied | applied |

**Traversal is safe by structure, not by a cutoff.** Symlinks are never followed, so the walk cannot leave the
repository or loop through one; directories already visited are not re-entered. Depth is not a security property
and is not treated as one.

**An unreadable directory is reported, not passed over.** If the operating system refuses a directory, the sync
still completes, and it says which directories it could not inspect. A successful sync therefore means RepoAtlas
inspected everything admissible, or named what it could not — never that it quietly stopped.

Operating-system path limits still apply. RepoAtlas fails only where the filesystem itself refuses a path, and
nesting depth and total path length are separate constraints.

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

**Execution targets must be statically proven.** A `EXECUTES` relationship may name only a target the source
itself contains:

> A Bash `EXECUTES` edge names only a target explicitly recoverable from static source syntax. Variable
> expansion or runtime command construction never becomes a deterministic target.

> Absence of a deterministic target means *unresolved*, not low-confidence invention.

| Proven — must resolve | Not proven — must resolve to nothing |
|---|---|
| `./deploy.sh`, `"./deploy.sh"`, `'./deploy.sh'` | `$CMD`, `"$CMD"`, `${SCRIPT}` |
| `python3 app.py`, `ruby tool.rb` | `"$BASE/$NAME"`, `${BASE}/script.sh`, `prefix-$VAR` |
| `source ./env.sh`, `. ./env.sh` | `source "$CONFIG"`, `python "$SCRIPT"` |
| `salt-call state.apply app` | `$(find_tool)`, `eval "$COMMAND"`, `"${!INDIRECT}"` |
| `timeout 30 ./migrate.sh` (wrapper grammar known) | `CMD=./x.sh` then `$CMD` — V1 does no constant propagation |

`cmd:$CMD` at confidence 0.5 is not an acceptable substitute for absence: the entity still exists and can still be
cited. Quoting is shell syntax and is stripped from the target — `'./deploy.sh'` names the file `./deploy.sh`.
A dynamic invocation still records what the file *does* prove: the environment read its expansion implies, and any
leading `NAME=value` assignments. Wrapper policy, the `sh -c` / `eval` rule and the full table live in
`ARCHITECTURE.md` §5.

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

**Dockerfiles are read as text, never built.**

**Targets must be statically proven, under the instruction's own semantics.** The same rule §7.3 applies to shell
commands applies here, and for the same reason — a Dockerfile decides what an image is built from, what enters it
and what it runs:

> A Dockerfile target is emitted only when its identity is statically named by the instruction under Docker's
> semantics. A build or shell expression is not a target name.

> A computed Dockerfile target produces neither a deterministic edge nor a standing unresolved target claim.
> Confidence cannot convert an unproven target identity into a fact.

`unresolved_refs` holds **definite source claims awaiting linkage**, which is why it is not the softer option:
`COPY missing.py /app/` may legitimately stand as `unresolved file missing.py`, because the source names
`missing.py`; `COPY $SRC /app/` may not, because the source names nothing. Recording the expression there is the
same invented target one indirection away.

**`$` is not the test — the instruction is.** Docker expands `ADD`, `COPY`, `ENV`, `EXPOSE`, `FROM`, `LABEL`,
`ONBUILD`, `STOPSIGNAL`, `USER`, `VOLUME` and `WORKDIR` in the *builder*, in shell form and inside a JSON array
alike. `RUN`, `CMD` and `ENTRYPOINT` are expanded by the *command shell*, so their exec form — which invokes no
shell — expands nothing: `RUN ["$TOOL", "app.py"]` really does try to run a program named `$TOOL`, and recording
that is truthful. A blanket rejection of `$` would be a false-negative bug in its own right.

| Proven — must resolve | Not proven — must resolve to nothing |
|---|---|
| `RUN ./build.sh`, `RUN echo "$X"` (the command is `echo`) | `RUN $TOOL`, `RUN "${TOOL}"`, `RUN prefix-$TOOL` |
| `RUN ["python3", "app.py"]`, `RUN ["$TOOL", …]` (exec form expands nothing) | `RUN $(pick)`, `` RUN `pick` ``, `RUN <<EOF …` |
| `FROM python:3.13`, `FROM builder` (a declared stage) | `FROM $BASE`, `FROM image:${TAG}`, `FROM ${STAGE}` |
| `COPY src/app.py /app/`, `COPY ["a b.txt", "/app/"]`, `COPY \$FOO /quux` | `COPY $SRC /app`, `COPY ${BASE}/x.yaml /app/`, `COPY ["$SRC", "/app/"]` |
| `ENTRYPOINT ["python3", "app.py"]`, `CMD echo "$MSG"` | `ENTRYPOINT $RUNNER app.py`, `COPY --from=$STAGE …` |
| `RUN pip install requests` | `RUN $PIP install requests`; the `$PKG` in `pip install $PKG requests` |

**V1 performs no constant propagation.** `ARG BASE=python:3.13` followed by `FROM $BASE` yields no image. ARG
scope, redeclaration, build-time overrides, defaults, stage boundaries and the automatic platform args are a
separate feature; a partial version of it would fabricate an image whenever one of those rules applied.

Suppressing a computed target does not remove the evidence around it: `ARG TOOL` still records the variable,
`ENV HOST=$OTHER` still records `HOST`, and `FROM ${BASE}` still opens its stage.

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
- If **two or more** managed repositories provide the same exact name, create no edge at all. Choosing one would be
  a guess, and two repositories genuinely publishing `common-lib` is an ordinary situation.

**Granularity (V1 limit, confirmed by validation).** Cross-repository relationships are established between
**repositories and packages**, never between symbols. `from common_lib.settings import load_config` in one
repository does not produce an edge to the `load_config` defined in another, even when that repository is managed
and does define it; the reference stays in `unresolved_refs`. Resolving a bare package specifier to a file requires
each ecosystem's own module-resolution rules, and approximating them is exactly the "similar name, assumed
relationship" this section forbids. The consequence is a recall limit, not a correctness one — a cross-repository
reference never resolves to the wrong same-named definition — and "which repository provides X?" is answered from
the dependency graph and from retrieval. A per-ecosystem resolver is V2 work (§22).

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

**A range must be the smallest span that establishes the fact.**

> A deterministic entity or fact may cite only the smallest source span that actually establishes it.

This is a correctness requirement, not a tidiness one: a parser's line range is the authoritative supplied span,
so retrieval renders it and citation verification (§15) checks the model's claims against it. A range wider than
the fact's real source lets unrelated lines be presented — and validated — as support. Narrower is not safer
either: a span that omits part of the state offers evidence that does not establish the fact.

For Salt specifically:

> A Salt state's provenance ends on the last source line structurally belonging to that state. Top-level Jinja
> controls, sibling states, trailing comments and separator whitespace are not evidence for the preceding state.

> Jinja embedded inside a state's value remains part of that state's provenance.

Ranges are derived from parsed node **content**, never from a YAML end mark — a composite node's end mark points at
the next construct, which is a different question. See `ARCHITECTURE.md` §5.

---

## 12. Incremental synchronization and branch awareness

`repoatlas sync` must **not** re-index everything.

### The repository set is part of what is synchronized

A sync reconciles two things, and for a long time it only reconciled the second:

```
repository set        which repositories exist beneath the managed root
repository contents   which files, facts and relationships each one has
```

The set is re-derived from the managed root by the same discovery used at `init` (§5), inside the writer lock,
before any repository is indexed. Repositories discovery found and the index does not hold are registered and
indexed **in that same run** — a registered repository with no facts is a worse outcome than an undiscovered one,
because it is counted as managed. Repositories whose directory is gone are retired, which sync already did.

Reconciliation happens when the run **covers the managed root**: global scope, or — where the root is itself a
managed repository and global scope is therefore unreachable (§6) — a run scoped to that repository.
`repoatlas sync <repo>` is a statement about one repository and registers nothing, because a scoped run that
registered another repository would leave a row it is not permitted to index.

`status` detects the same drift at **every** scope and reconciles nothing. Detection writes nothing, so a
repository-scoped status can say "this repository is current, and the workspace is missing two" without
overstepping; reporting it only from the root would leave anyone who works inside their repositories exactly as
uninformed as before. Registering during a read command would make "check whether anything changed"
indistinguishable, in a terminal, from having run a sync.

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
- the **facts version** — the schema version, the parser and dependency-parser registries, and the chunking rules
  — differs from the one recorded on the file row. This one is handled *per file* rather than by rehashing the
  tree, since the version is stored on the row itself. The embedding model is deliberately not part of it: vectors
  are keyed by the model that produced them, so a model change re-embeds without reparsing anything;
- `--full` / `--rehash` was passed.

In those cases every file is hashed. Otherwise: stat match ⇒ skip, stat mismatch ⇒ hash ⇒ reparse only if the hash
changed. Filesystems with coarse mtime resolution cost an extra rehash, never a missed update.

### Branch awareness

Correctly handle `git checkout`, `git switch`, `git pull`, `git rebase`, `git reset`.

After `repoatlas sync`, the knowledge base must reflect the **current working tree**. Facts from the previous
branch must not remain.

### One writer, one complete state

A sync moves RepoAtlas from one **complete logical knowledge state** to another. File facts and the
cross-repository graph they justify are both part of that state, so:

- the global graph is **replaced atomically** — another process reads the previous complete graph or the new
  complete graph, never a moment with neither;
- a repository is recorded as synchronized **only after** that replacement commits, in the same transaction, so a
  run whose link pass failed reports `interrupted` rather than `current`;
- `init` and `sync` hold one **writer lock** per state database, so two mutating runs cannot interleave. The
  second waits up to `storage.lock_timeout_seconds` (default 30) and then exits 5 with a message naming the
  holder. Read commands never take it.

The mechanism and its crash behaviour are in `ARCHITECTURE.md` §9.

### Invariants (tested)

1. After any sync, **no fact exists whose file reference points to content not currently on disk**.
2. An incremental sync across any HEAD change yields **state equivalent to a full reindex** of the same tree.
3. A failed sync leaves the **previous complete graph** intact and does not report the affected repositories as
   current.

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

### Multiplicity and provenance

One repository may legitimately hold **several** dependency domains — a root `package.json` beside
`backend/package.json`, a `pyproject.toml` beside `requirements.txt` and `requirements-dev.txt` — and each states
something true.

**Multiple real dependency declarations must never be silently collapsed into one apparently authoritative
declaration or version.** RepoAtlas may report one declaration, several agreeing declarations, several differing
declarations, one resolved version, several independently resolved versions, or an unresolved state. It may not
choose one of them.

Consequences:

- every observation keeps its own source path, line, group and role;
- a manifest is paired with a lockfile only when the layout proves the pairing — in practice, when they sit in the
  same directory. A workspace root lockfile is not evidence about a nested member's version;
- **a repository may contain more than one resolved version of the same package**, and both survive;
- identical constraints in two files are *agreement*, not conflict, and both source paths are still recorded;
- multiplicity is reported as a count of distinct declarations or resolved versions. V1 performs no constraint
  intersection, so it never claims that two constraints are incompatible — only that they differ (§2);
- ecosystem is part of package identity: npm `foo-bar` and PyPI `foo_bar` normalize alike and are not the same
  dependency.

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

#### Exact search must not depend on how content was stored

`filenames` above is a requirement about the file, not about its contents. An indexed file must be findable by the
name it has on disk — its basename, its whole relative path, or a trailing sub-path on a segment boundary — whether
or not any text inside the repository happens to mention that name. Matching is exact in all three shapes:
`onfig.py` finds nothing, and RepoAtlas never falls back to fuzzy pathname search. When several files share a
name, **all** of them are returned, ordered from most to least specific; choosing one would be RepoAtlas answering
a question the repository leaves open.

A file matched this way is evidence about the file, so it cites the file and **no line number**. Provenance is
never synthesized to satisfy a renderer (§11).

#### An exact path must survive fusion

> **Invariant.** Fusion may combine relevance evidence, but it may not erase stronger deterministic match
> specificity.

> **Invariant.** An exact indexed path is more specific than an exact basename match.

> **Invariant.** A path-component suffix explicitly supplied by the user is stronger intent evidence than a
> basename or a textual mention of that path.

> **Invariant.** `EXACT_SUBPATH` does not imply uniqueness.

> **Invariant.** Exact complete path identity remains stronger than suffix-path identity.

> **Invariant.** Fusion and reranking may order candidates within a specificity class but may not erase the class
> itself.

> **Invariant.** Explicit repository/path qualification outranks inferred primary-repository bias.

> **Invariant.** Candidate merging preserves the strongest match provenance, independent of stage order.

Finding the file is not the same as ranking it. When the user supplies a complete repository-relative path and
that path exists, the file it names must **lead** — it may not be displaced by a file that merely shares its
basename, by a symbol whose name resembles the query, by word overlap, by an embedding neighbour, or by a document
that quotes the path. Those are all legitimate evidence and all remain in the answer; none of them is what was
asked for.

The competitors win on *quantity*: a file called `config.py` full of `config`-shaped symbols is reached by four
retrieval stages, and a file whose only claim is that its path is what the user typed is reached by one. Rank
fusion measures agreement between retrievers, so agreement is what it rewards — correctly, for questions where
nothing was named exactly. A query that names one file completely is not such a question, and specificity is
therefore compared **before** any score is (ARCHITECTURE.md §10).

Accepted spellings of a complete path are the repository-relative path itself, that path with a leading `./`, and
the path qualified by the repository's identity (`svc/src/config.py` — what RepoAtlas prints in a citation).
Path identity is byte-exact and case-sensitive; the folding the lexical index performs decides recall and never
identity. A path containing a space must be quoted.

**A trailing sub-path is explicit too.** `api/config.py` for `services/api/config.py` is a path the user typed, and
it must likewise lead — it may not be displaced by a basename match, a symbol name, word overlap, an embedding
neighbour or a document that quotes it. Anchoring only complete paths left this exact failure one level down: a
`README.md` quoting `api/config.py` two hundred times outranked both files whose paths actually end that way.

It is weaker than a complete path, and it is weaker in a specific way — it does not identify **one** file:

| The user types | Against `services/api/config.py` | Precedence |
|---|---|---|
| `services/api/config.py` | complete relative path | strongest; names one file |
| `svc/services/api/config.py` | repository-qualified complete path | strongest; names one file |
| `api/config.py` | trailing sub-path on a segment boundary | above everything unspelled; may name several files |
| `config.py` | basename | no precedence — says what it is called, not where it lives |
| `pi/config.py`, `myapi/config.py` | not a segment boundary | no match at all |
| `api/config` | not a path-shaped token | no path match |

When several files end the same way, **all** of them are anchored and none is declared the answer. Order among
them is decided by ordinary retrieval agreement, including the primary-repository bias — which orders candidates
inside the class and never creates one. A question answered from a sub-path is never reported as a complete-path
identification, and `--verbose` lists `named path(s)` and `suffix path(s)` separately so the difference is visible
from outside.

Sub-path matching is on path components, not characters. `pi/config.py` matches nothing, because a substring test
would turn identity into a guess. `..` never reaches path matching: query analysis extracts path-shaped tokens at a
word boundary, so `../api/config.py` is matched as the clean suffix `api/config.py` that it leaves behind.

Storage layout must likewise not decide discoverability. Chunk boundaries — including the character cap that
bounds a file with no line structure — exist to bound *evidence size*. A literal that straddles one of them is
still present in the repository, and exact search must still find it. Both properties are asserted in the
adversarial suite in each direction: what exists is found, and what does not exist is still refused (§2).

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

### When the question names something that does not exist

If the question contains a term that can only be code — a quoted string, an `ALL_CAPS` name, a dotted name, a
path, a `@scope/package`, a `snake_case` identifier — and exact search finds **none** of them anywhere in the
index, the entire associative half of the funnel is skipped: word-overlap recall, entity-name recall and semantic
similarity alike. The result is an empty evidence set, and the command says the term was not found.

Suppressing word matching matters as much as suppressing embeddings. A question about something absent still shares
its *other* words with unrelated files, and those hits arrive with a path and a line range — which reads as an
answer to the person who asked. Leaving that stage on would move the fabrication one step earlier rather than
removing it (§2).

Shapes that English prose also produces — a bare dashed word such as `auto-scaling`, a `camelCase` word such as
`TypeScript` — are still searched exactly, because §14 requires package and symbol names to be findable, but they
may **not** declare a question unanswerable on their own. A miss on one of those must not silence a question that
the rest of the funnel could have answered. The exception is a question that consists of nothing but such a token,
where the miss is the whole answer.

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

### Failure diagnostics must name the layer that failed

> Receiving an invalid model response is not equivalent to failing to reach the model server.

> An error message describes the deepest layer successfully reached before the failure.

A diagnostic is output, and output has to be true. Backend failures are classified by how far the request got —
configuration, transport, server, protocol, model output — and a failure at one layer may never be described in the
vocabulary of the layer below it. Only a connection that was never established may say "could not reach".

| Situation | Reported as |
|---|---|
| Nothing listening, or the connect timed out | could not reach the server |
| Connected, then HTTP 4xx/5xx, or the response never finished | the server was reached and failed |
| The server listed its models; the configured one is absent | the server is running, the model is not installed |
| A body arrived that is not the API format | the server answered, but not in the expected format |
| A valid envelope carrying unusable model text | the model returned invalid output |
| The **server reported** generation stopped at its output limit | the response was truncated; raise the output limit |
| Model output invalid and the repair also invalid | both attempts named, with what was wrong with each |
| A valid answer citing unsupplied evidence | `INSUFFICIENT EVIDENCE` — a grounding outcome, not a backend failure |

Truncation is reported only when the server states it, never inferred from text that looks unfinished: a model that
emitted confident nonsense will emit more of it with a larger budget, so "raise the output limit" would be a guess
presented as a diagnosis. A backend failure during the repair attempt is reported as a backend failure, not as the
model having answered badly. Every backend failure keeps exit code 4; the *message* carries the cause.

Error messages never contain a response body. Model output is written from repository content, so failures are
described by shape — a byte count, the names of the fields present, a parse position — and Ollama's documented
`{"error": "..."}` string is the only server text ever quoted, bounded (§17).

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

The same applies where the *build* differs. A parser fix, a new parser or a change to how chunks are cut makes
stored facts stale while every file on disk is untouched, so HEAD, mtime and content hash all still agree and no
other signal notices. `status` compares the facts version recorded on each file row against the running build and
reports `stale (indexed by an older build)`. An ordinary `sync` repairs it — no `--full`, no manual step.

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

**Matching is case-insensitive, and mandatory.** Three invariants:

> Built-in obvious-secret filename checks are security policy, not user preference, and cannot be negated by
> `.repoatlasignore`.

> An excluded secret file is rejected before its contents are parsed, chunked, embedded, retrieved, serialized into
> prompts, or logged — and before it is opened at all.

> Filename-case variation cannot bypass a built-in secret exclusion.

| Pattern | Excluded, in any case | Still indexed |
|---|---|---|
| `.env` | `.env`, `.ENV`, `.Env`, `.eNv` | `env`, `env.py`, `.envrc`, `dotenv.md` |
| `.env.*` | `.env.local`, `.ENV.PRODUCTION`, `.Env.Production` | `my.env.backup` |
| `*.pem` | `server.pem`, `server.PEM`, `tls.private.PEM` | `server.pem.txt`, `pem_notes.md` |
| `*.key` | `private.KEY`, `service.production.KEY` | `monkey.py`, `keyboard.txt`, `key_rotation.md` |
| `id_rsa` | `id_rsa`, `ID_RSA`, `Id_Rsa` | `id_rsa.pub`, `id_rsa_notes.txt` |
| `id_ed25519` | `id_ed25519`, `ID_ED25519`, `Id_Ed25519` | `id_ed25519.pub` |

Matching is on the **basename**, case-folded, with glob semantics — a real suffix, not a substring search.
Over-exclusion silently loses real evidence, so `monkey.py` is not a `*.key` and `id_rsa_notes.txt` is not a private
key. `.repoatlasignore` keeps its own gitignore-like, **case-sensitive** semantics: the two systems answer different
questions and are not merged.

**The guarantee, stated exactly.** RepoAtlas excludes files matching its built-in obvious-secret *filename* policy
before their contents enter the indexing pipeline. It is **not** true that "RepoAtlas never indexes secrets": a
credential pasted into `app.py` is indexed like every other line of that file. V1 performs no entropy analysis, no
provider-specific key patterns and no content-based secret detection of any kind.

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

### Adversarial validation

Beyond the matrix above, `tests/adversarial/` holds a second ecosystem built to make RepoAtlas fail, and
`tests/evaluation/` holds a persistent retrieval dataset. Both follow one rule that the matrix above does not need
and these do:

> **Expected results are written by hand from the fixture construction. RepoAtlas output never defines an
> expectation.**

Recording what the system currently says turns the suite into a description of present behavior — bugs included —
and every later regression then looks like the specification. The adversarial fixtures therefore encode what must
**not** be found as carefully as what must, and include questions that have no answer at all, which is the case
most suites never test. See `ARCHITECTURE.md` §13.

---

## 20. Performance principles

Do not prematurely optimize. But **never**:

```
scan every repo for every question
re-index everything after one file changes
re-embed unchanged data
send entire repositories to the model
use the LLM during deterministic parsing
read a repository file into memory without a limit
```

Use incremental state and bounded retrieval.

The last rule is a memory bound rather than a speed one, and it is a security control: a repository decides how
large its files are. `indexing.max_file_bytes` (default 1 MiB) is an **inclusive** maximum on the size of a file
whose *content* is indexed, and it is enforced by the reader on the descriptor it reads from — never on a size
observed earlier, and never after the bytes have arrived. A file past it is recorded as a path and a size, and
contributes no parser facts, chunks, embeddings or prompt content. Change-detection hashing is a separate,
streaming path that may legitimately consume a huge file in fixed blocks without holding it. See
`ARCHITECTURE.md` §12.

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
      exit codes 0/1/2/3/4/5, `--json`, `--verbose`
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

### Slice D — Ranking — **COMPLETE**

Delivered 2026-09-02. 373 tests green; ruff, ruff format and pyright clean.

- [x] **14. Semantic retrieval** — `EmbeddingModel` protocol, fastembed `BAAI/bge-small-en-v1.5` (ONNX, no
      PyTorch), deterministic offline `HashingEmbedding` for tests, config-driven factory, `SqliteVectorIndex`
      (brute-force NumPy cosine over float16, scoped **before** the dot product), and re-embedding that is
      incremental at *chunk* granularity — vectors are keyed by chunk content hash, so text that survives a
      reparse byte-for-byte is never embedded twice (§20)
- [x] **15. Context-aware retrieval** — the full funnel: query analysis, exact, lexical, symbol, semantic and
      bounded graph expansion, tier-then-RRF fusion, primary-repository bias, deduplication, hard item and token
      budgets, `NoopReranker` seam, plus per-stage instrumentation on both retrieval and the global link pass

**The core retrieval principle is enforced by a type, not a weight.** Every candidate carries an `EvidenceTier`;
`DETERMINISTIC` covers literal-verified matches, exact entity-name matches and graph expansion, `ASSOCIATIVE`
covers FTS recall and embedding neighbours. Fusion sorts on `(tier, -score)`, so scores are only ever compared
within a tier and **no similarity score can displace an exact identifier, an exact error string, an exact symbol
name, an environment variable or a graph-supported relationship**. The primary-repository boost is additive on
the score, so it competes inside a tier and never promotes across one. A V2 reranker is bound by the same rule.

**Semantic search is suppressed** when the question names literal terms and none of them exist anywhere in the
index: the honest answer is that they do not exist, not something that reads like them (§2). Prose questions,
which is what semantic retrieval is for, are unaffected.

**Two latent defects were found and fixed while wiring the graph stage.** Interned entities — environment
variables, packages — were unreachable through `search_entities` because of an inner join to `repositories`,
which they belong to by construction not at all. They are first-class searchable entities (§10) and the hubs
graph expansion travels through, so the join is now outer and repository scoping keeps them in. Exact entity
lookup was also added alongside FTS, so a spelled-out identifier is found regardless of tokenization.

**Deferred within Slice D, by design:**

- Fetching the embedding model is an explicit act (`repoatlas sync --embed`), not something an ordinary sync does
  silently. Until it is fetched, `available()` reports "not downloaded" and semantic retrieval stays off while
  every deterministic command is unaffected — the same posture as a missing Ollama model (§3).
- `watch` (optional in §4) is still not implemented.

### Slice E — Delivery and validation — **COMPLETE**

Delivered 2026-09-02. 635 tests green (plus 10 behind the `slow` marker, run separately); ruff, ruff format and
pyright clean; the image built and the stack started when the slice was delivered. The adversarial pass that
followed raised the suite to 935 and could not re-check the container: see the note at the end of this section.

- [x] **19. Docker** — multi-stage `Dockerfile` (uv build stage, `python:3.13-slim` runtime, non-root, `git` as the
      only added binary, dependencies installed `--frozen` from `uv.lock`) and `docker-compose.yml` with the
      `repoatlas` and `ollama` services, the repository tree mounted **read-only**, state on a **separate named
      volume**, a read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, and the model server published
      on loopback only. No SearXNG or other research infrastructure (§22)
- [x] **20. End-to-end tests and hardening** — the §21 scenario driven through the **installed console script as a
      subprocess**, the full §19 matrix, and performance sanity measured through the Slice D instrumentation

**Slice E is validation, not packaging.** What it added beyond the two phases above:

- [x] **The real embedding backend is exercised.** `tests/test_real_embeddings.py` runs `BAAI/bge-small-en-v1.5`
      through factory → sync → vector index → retrieval funnel, and asserts the two properties the deterministic
      stub structurally cannot have: semantically related text outscores unrelated text without sharing tokens, and
      the asymmetric query prefix genuinely changes the vector. The precedence guarantee is re-checked with the real
      model, so the tier rule is not an artifact of the stub. Behind the `slow` marker; CI still never downloads
- [x] **Content-addressed reuse and collection when several chunks share one hash.** Identical text across
      repositories is embedded once; adding another copy embeds nothing; deleting one sharer keeps the vector the
      others still need; the vector is swept only when the last sharer goes; two models keep independent vectors for
      the same hash. This is the case a `chunk_id` cascade would get wrong
- [x] **The precedence guarantee is asserted at the final retrieval boundary**, not only inside `fuse`. Against an
      embedding model that scores every text 1.0, and against a reranker that deliberately inverts the tiers. The
      order is re-imposed by `enforce_precedence` before budgeting, so a hostile order can neither outrank
      deterministic evidence nor starve it out of the token budget, and cannot drop a file the user named by its
      full path below one that merely shares its basename
- [x] **Exact-path ranking specificity**, against ten files sharing one basename, one of them stuffed with
      matching symbols and imports and another quoting the target path a hundred times. Full paths, nested paths,
      quoted paths, paths with spaces, dots, dashes and non-ASCII characters; repository-qualified paths against
      the primary-repository bias; the negative controls (`src/config.p`, `sr/config.py`, `x/src/config.py`,
      a bare basename, a suffix-only path, a case-folded path, a path that does not exist); order-independent
      candidate merging over every stage permutation; and the whole thing again through the installed CLI in text
      and `--json`
- [x] **Stale-index stress**: diverged branches, content-only branch changes, directory deletion, a deleted file
      that returns, renames, moves into a deeper path, moves *between repositories*, two files swapping content,
      uncommitted edits, untracked files, staged deletions, reverting to clean, and dependency edits in both
      directions — including a provider that appears only later and gains its edge from a standing claim. Every
      scenario ends by comparing a fact snapshot against a from-scratch index of the same tree
- [x] **Secret exclusion asserted stage by stage** — files, chunks, FTS, embeddings, entities, evidence sets, the
      model prompt (inspected in the messages actually handed to the backend) and `--verbose` output — with one
      canary per pattern, plus the converse: files with *similar* names are not excluded, because over-exclusion
      loses real evidence silently
- [x] **Malformed input fails conservatively.** A matrix of broken YAML, Salt and Jinja against every parser that
      claims the extension: nothing raises, no reported line falls outside the file, anything recovered is marked
      below full confidence, a YAML alias bomb stays linear, an unsafe `!!python/object/apply:` tag is composed but
      never constructed (asserted with a filesystem canary), and a repository full of broken files still syncs with
      its other facts intact
- [x] **Performance sanity from the Slice D traces** — measured, not optimized (see below)

**Three real defects were found by this work and fixed.** All three were provenance or precedence bugs that the
happy path never reached:

1. A Jinja template symbol spanned one line past the end of its file (`content.count("\n") + 1` where a trailing
   newline terminates the last line rather than starting another), so a citation could name a line that does not
   exist.
2. A YAML parse failure at end-of-stream reported a diagnostic line one past the file, for the same reason. Both
   are now clamped in `parsers/marks.py`, which is where that single invariant now lives.
3. A Salt requisite naming nothing — `- pkg:` with an empty value — produced a reference to the empty string, an
   edge to a state that cannot exist. It now yields no reference at all.

None was caught by the existing suite because each requires input no fixture had: a file whose last line lacks a
trailing newline, a truncated YAML document, an empty requisite value.

**Measured on the fixture set** (7 repositories, 25 files) — recorded so a future regression is comparable, not as
a target:

| Operation | Cost |
|---|---|
| Full index (`init`) | ~0.09 s |
| No-op sync (nothing changed) | ~0.04 s |
| Global link pass | ~1.5 ms for 29 standing claims over 25 files / 75 symbols |
| Retrieval, whole funnel | 1.7–2.2 ms wall, of which ~1.3–1.5 ms is traced across the nine stages |
| Slowest retrieval stage | graph expansion, ~0.4–0.8 ms |
| Repeated retrieval (12 runs) | flat: first four 1.5 ms, last four 1.4 ms |
| Docker image build | ~1 min cold, ~12 s warm |

Nothing here is unreasonable, so nothing was optimized (§20).

**Deferred within Slice E, by design:**

- `watch` (optional in §4) is still not implemented. It is the one item in §4 marked optional and the only one V1
  does not ship.
- The `ollama` service starts with no model pulled. `ask` therefore reports exit 4 with the `ollama pull` message,
  which is the designed behavior (§3) rather than a gap; pulling a 20 GB model is a deployment step, not a build
  step.
- Salt `extend:` still records the relationship rather than the merged override semantics (unchanged from Slice C).

**Container verification, recorded honestly (2026-09-02).** `Dockerfile` and `docker-compose.yml` were built and
started during Slice E. The adversarial pass that followed ran on a machine whose WSL distro has no Docker
integration enabled, so no image was rebuilt and no stack was started afterwards. The 15 delivery invariants in
`tests/test_docker.py` are parsed out of the two files and still pass; they are a static check, and a static check
is not a running container. Anyone taking V1 to a real deployment should rebuild and start the stack first.

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
- [x] Deterministic evidence outranks semantic similarity, for every deterministic source and at any similarity
      score (§14) — `tests/test_retrieval_ranking.py`
- [x] Unchanged text is never re-embedded, and orphaned vectors are swept (§20) — `tests/test_embeddings.py`
- [x] Retrieval and graph traversal are instrumented per stage, and the trace never changes the answer (§20) —
      `tests/test_retrieval_ranking.py`
- [x] Associative evidence cannot outrank deterministic evidence in a returned `EvidenceSet`, under a saturating
      embedding model or a hostile reranker, and cannot displace it from the token budget either (§14) —
      `tests/test_retrieval_ranking.py`
- [x] Identical text is embedded once however many chunks share it, and its vector survives until the last sharer
      is gone (§20) — `tests/test_embeddings.py`
- [x] Every incremental sync scenario yields the same facts as a full reindex of the same tree, across branches,
      deletions, renames, cross-repository moves, dirty trees and dependency edits (§12) —
      `tests/test_stale_index.py`
- [x] No secret file reaches enumeration, chunks, FTS, embeddings, entities, evidence, a prompt or a log (§17) —
      `tests/test_secrets.py`
- [x] Malformed Salt, Jinja and YAML fail conservatively: nothing raises, no line number falls outside the file,
      and recovered facts are marked below full confidence (§2, §7) — `tests/test_malformed_input.py`
- [x] The container mounts repositories read-only and keeps state on a separate volume (§18) —
      `tests/test_docker.py`, verified against a real build and a running stack during Slice E. **Not re-verified
      after 2026-09-02**: no Docker daemon is reachable from the development WSL distro, so the delivery
      invariants are currently asserted statically only
- [x] The §21 success scenario passes through the installed console script, with real exit codes (§21) —
      `tests/test_e2e.py`, `tests/adversarial/test_cli_scenario.py`
- [x] No git command RepoAtlas runs can execute repository-controlled code — content filters, hooks, fsmonitor,
      pagers, credential helpers, diff drivers or aliases (§17, `ARCHITECTURE.md` §8) —
      `tests/adversarial/test_git_execution_boundary.py`
- [x] A valid citation cannot launder unsupported prose, no free-text field may name a repository object that was
      not supplied, and repository text cannot forge prompt structure (§15, `ARCHITECTURE.md` §11) —
      `tests/adversarial/test_answer_trust_model.py`

### Adversarial validation pass — **COMPLETE**

Completed 2026-09-02, on top of Slice E and adding no features. Objective: find realistic ways to make RepoAtlas
return stale, incorrect, unsupported, ambiguous, misleading or overconfident output. 935 tests green (276 of them
adversarial), plus 12 behind `slow` and 3 behind `scale`, run separately.

- [x] **A nine-repository adversarial ecosystem** (`tests/adversarial/fixtures/`) with real relationships *and*
      deliberate misinformation: documentation that contradicts the code, two repositories both providing
      `common-lib`, one function name defined three times, references to files that do not exist, prompt-injection
      text addressed to the model, fake secrets, invalid UTF-8, a 200 KB single-line file, a 4000-entry generated
      JSON document, and identifiers guaranteed to appear nowhere
- [x] **Ground truth written by hand** (`GROUND_TRUTH.md`) from the fixture construction, never from RepoAtlas
      output. It records what must be found, what must **not** be found, and which questions have no answer
- [x] **A persistent evaluation dataset** (`tests/evaluation/retrieval_cases.yaml`, 26 cases) with required,
      alternate and forbidden evidence per case, reported per category and deliberately never as one combined
      accuracy number
- [x] **Seven defects found and fixed**, each with a permanent regression test (listed below)
- [x] **Two weaknesses measured and documented rather than tuned away** — cross-repository symbol resolution (§9)
      and prose competing with structure in intra-tier ranking (`ARCHITECTURE.md` §10)

**Defects found by this pass.** All seven were reachable from ordinary use and invisible to the existing suite:

1. **Wrapped commands lost their target.** `env LOG_LEVEL=debug ./notify.sh` recorded `EXECUTES cmd:env` — true of
   the text, useless about the system. Transparent wrappers (`env`, `exec`, `nohup`, `command`) are now unwrapped
   to the real target, stopping at the first flag not known to pass through; `sudo` is excluded because its flags
   consume arguments.
2. **Suppression was semantic-only.** A question naming an identifier that exists nowhere still returned FTS hits
   — with paths and line numbers — because only the embedding stage was silenced. Suppression now covers every
   associative stage.
3. **Package names were never literal terms.** `@company/common` was split into `company` and `common`, so §14's
   requirement that exact search work for package names was not met, and a miss could not be detected.
4. **A `snake_case` identifier that exists nowhere returned files.** Only visible under the real embedding model;
   the hashing stub hid it. `snake_case` is now a definite term, `camelCase` a searched-but-not-definite one.
5. **Tier laundering through graph expansion.** Fuzzy entity-name hits seeded the graph walk, so one approximate
   name match produced a dozen `DETERMINISTIC` candidates that outranked every honest associative one. Expansion is
   now seeded by exact matches only. Real-model semantic recall improved from 1/4 to 3/4 at k=3 as a side effect.
6. **A 200 KB single line became one chunk**, consuming an entire evidence budget. Chunks are now capped in
   characters as well as lines, splitting rather than truncating.
7. **Rank fusion counted volume as agreement.** Chunks of a file with no line breaks all carry the same line range,
   and each added its own reciprocal rank — so a 4000-entry generated JSON document containing none of the
   question's words ranked **first** for "where does the backend get its hostname?", pushing the answer to rank 6.
   Each ranked list now contributes one reciprocal rank per span. Defect 6 created this one, which is what an
   adversarial pass is for.

**Two failures traced to the fixture, not the product**, and were fixed in the fixture: a file whose docstring
contained a literal the ground truth claimed it did not, and a race test that mutated a file's mtime without
changing its content, so the file was never re-read. Neither produced a change to RepoAtlas.

**Measured retrieval quality** on the 26-case dataset with `BAAI/bge-small-en-v1.5`, reported per category:

| Category | n | r@1 | r@3 | r@5 | r@10 |
|---|---|---|---|---|---|
| exact | 8 | 8/8 | 8/8 | 8/8 | 8/8 |
| semantic | 4 | 0/4 | 3/4 | 4/4 | 4/4 |
| dependencies | 2 | 1/2 | 2/2 | 2/2 | 2/2 |
| cross-repo | 2 | 0/2 | 1/2 | 1/2 | 2/2 |
| misleading | 2 | 0/2 | 1/2 | 1/2 | 2/2 |
| salt / bash / ambiguity | 3 | 3/3 | 3/3 | 3/3 | 3/3 |
| unanswerable | 5 | — | — | — | — |

Forbidden-evidence hits: 0. Unanswerable questions correctly refused: 5/5. Deterministic-precedence violations: 0.
Cross-repository expectations met: 2/2.

### Remediation pass 6 — synchronization atomicity and writer serialization — **COMPLETE**

Completed 2026-09-02. Two findings from the independent review, fixed together because they are one
transaction/lifecycle problem.

1. **The global link rebuild was not atomic.** It committed `DELETE FROM edges WHERE parser = 'linker:global'`,
   did all its resolution work, and committed the replacement later. Anything failing in that gap published a
   knowledge base with **no cross-repository graph at all**, while `status` reported `current`. Reproduced: 2
   global edges and `svc → lib` before, 0 edges and `repo:svc` with no neighbours after, `status` still
   `'current'`.
2. **Concurrent syncs were not serialized.** This document and `ARCHITECTURE.md` both described an advisory sync
   lock; none existed. Two real `repoatlas sync` processes over an 8-repository tree produced
   `UNIQUE constraint failed: entities.key`, `FOREIGN KEY constraint failed`, raw Python tracebacks — and, in
   about a third of runs, **exit code 0 over a silently doubled graph** (32 global edges where 16 is correct).

Fixed by: prepare-then-swap global linking (one transaction for delete + intern + insert + GC), an `flock` writer
lock per state database shared by `init` and `sync`, and marking repositories synchronized only inside that swap
transaction. `Database.transaction()` became reentrant so a caller can group store writes; `intern_entity` became
a real upsert; opening the database stopped being a write; migrations apply under `BEGIN IMMEDIATE`. See
`ARCHITECTURE.md` §9. Regression suite: `tests/adversarial/test_sync_atomicity.py` (32 cases, including four
injected failure points and real concurrent processes killed with `SIGTERM` and `SIGKILL`).

### Remediation pass 7 — Bash execution-fact integrity — **COMPLETE**

Completed 2026-09-03. One finding: the Bash parser recorded deterministic `EXECUTES` facts for command targets it
could not statically prove.

Reproduced on a fresh fixture: `$CMD` became `EXECUTES cmd:$CMD` at **confidence 1.0**, `"$DEPLOY_CMD"` became
`cmd:"$DEPLOY_CMD"` with the quotes in the entity key, `'./quoted-script.sh'` became a quoted `COMMAND` instead of
a script path, `bash -c "./x.sh"` created a cross-file edge while `sh -c './x.sh'` created none, and
`/usr/bin/python3 app.py` silently dropped `app.py`. The shipped adversarial ecosystem itself carried `cmd:$CMD`
and `cmd:"$SCRIPT"` entities, contradicting its own `GROUND_TRUTH.md`.

Fixed by deciding static proof from the tree-sitter syntax tree rather than from the text: a target resolves only
when every node under it is literal, which rejects every expansion form and strips quoting from the ones that
remain. Wrapper unwrapping became a per-wrapper option grammar that fails closed on unknown flags, extended to
`timeout`, `nice`, `time` and `setsid`, with `sudo`, `xargs` and `chroot` deliberately recorded as themselves.
`sh -c`, `bash -c`, `python -c` and `eval` payloads are never followed. See `ARCHITECTURE.md` §5. Regression
suite: `tests/adversarial/test_bash_execution_integrity.py` (195 cases, of which 87 failed before the fix),
covering the parser, the database and incremental literal↔dynamic transitions.

### Remediation pass 8 — secret exclusion integrity — **COMPLETE**

Completed 2026-09-03. One P2 security finding: the built-in obvious-secret filename policy matched
case-sensitively, so `.ENV`, `.Env`, `ID_RSA`, `Id_Rsa`, `server.PEM` and `private.KEY` were indexed.

Reproduced end to end: **20 of 26** secret-named files in a fresh fixture reached the `files` table, with 20 chunk
rows, 20 FTS hits, 20 stored vectors and secret paths returned as retrieval evidence. A question naming a canary put
the file's content into the LLM prompt three times over, along with the path and the variable name. The shipped
`GROUND_TRUTH` for the policy was never wrong; the matcher was.

Root cause: `fnmatch.fnmatch` normalizes its input with `os.path.normcase`, which lowercases on Windows and macOS
and does nothing on Linux — so identical code enforced the policy on one platform and not on another.

Fixed by moving the policy to `repoatlas/security.py` as the single authority and matching with
`fnmatch.fnmatchcase` over a case-folded basename. The check now runs *first* in enumeration, ahead of every user
ignore rule, so it cannot be negated; the git dirty-state comparison obeys it too, so an excluded file is never
opened at all. Already-indexed secrets are removed by an ordinary `repoatlas sync` through the existing
no-stale-facts path — no policy version, no flag, no manual database deletion. See `ARCHITECTURE.md` §12.
Regression suite: `tests/adversarial/test_secret_exclusion_integrity.py` (97 cases, of which 49 failed before the
fix), covering the predicate, the reader, every database stage, the embedding backend's own inputs, the assembled
prompt, operator output, renames, branches, and an upgrade from a contaminated database.

### Remediation pass 9 — Salt provenance range integrity — **COMPLETE**

Completed 2026-09-03. One P3 correctness finding: Salt state symbols received line ranges extending past the state
they represent.

Reproduced on fresh fixtures: `install_nginx` in the review's own example cited lines **3-5 as 3-7**, absorbing a
blank line and the `{% endif %}` of its enclosing conditional. Every boundary leaked — a state before a sibling took
the separator blank lines, a state before a comment took the comment, a state at EOF took the trailing blank lines,
and worst, a state followed by a comment and a guarded neighbour cited *the neighbour's own `{% if %}`* as its
evidence. Verified persisted: `entities.line_start`/`line_end` and `locate` both showed the over-wide span.

Root cause: PyYAML's `end_mark` for a composite node is the position of the **next token**, reached after the
scanner consumes intervening blank lines and comments. The existing correction — subtract one when the mark sits in
column 0 — removed only the last line of that overshoot.

Fixed by deriving the end from node **content** instead: recursively, the last line any leaf of the subtree
occupies. The calculation moved into `repoatlas/parsers/marks.py` as the single authority shared by the Salt and
YAML/JSON parsers, since Salt uses the latter's key collector for the pillar half of an `.sls` tree. Block scalars
are trimmed over blank lines only, so a `#` inside a literal block stays content. The line-scan recovery path gained
the same model from indentation. See `ARCHITECTURE.md` §5. Regression suite:
`tests/adversarial/test_salt_provenance_integrity.py` (59 cases, of which 30 failed before the fix), asserting exact
ranges and exact covered text through the parser, the database, retrieval and the grounding span.

### Remediation pass 10 — Ollama failure classification and diagnostics — **COMPLETE**

Completed 2026-09-03. The final finding from the independent review: Ollama failures were classified too broadly, so
a reachable server returning malformed or truncated output could be reported as though RepoAtlas could not reach
Ollama at all.

Reproduced against a **real socket** HTTP server, not a mocked transport — the distinction the finding is about is
invisible to a mock, because a mock never separates "no connection" from "a connection that returned something
useless". HTTP 200 with a non-JSON body produced `Could not reach Ollama at http://…`; a read timeout after a
successful connect produced the same; `/api/tags` answering HTTP 500 produced `No Ollama server responded`. Two
envelope shapes (`{"message": 123}`, a JSON array) escaped as an unhandled `AttributeError` and a Python traceback
at exit 1. A wrong envelope silently produced empty content and blamed the evidence. A response the server flagged
as `done_reason: "length"` was rendered as `INSUFFICIENT EVIDENCE` at exit 0 with no hint that the output limit had
been hit. HTTP error details dumped the raw response body.

Root cause: `except (httpx.HTTPError, json.JSONDecodeError)` in one clause, mapping a connection failure and a JSON
parse failure onto one message; the envelope was then read with `body.get(...)` on an unvalidated body; and the
reasoner's repair path caught `ModelUnavailable` alongside parse errors, so the same server outage produced exit 4
or exit 0 depending only on whether the first response happened to parse.

Fixed with a generic `ModelError` taxonomy in `protocol.py` — configuration / transport / server / model-absent /
protocol / output / truncated / repair, each with `category`, `retryable` and structured fields — translated by the
Ollama backend, which is the only module that knows its own protocol's spelling. See `ARCHITECTURE.md` §11.
Regression suite: `tests/adversarial/test_ollama_failure_diagnostics.py` (64 cases, of which the transport, server
and protocol layers run against a real HTTP server bound to an ephemeral port).
