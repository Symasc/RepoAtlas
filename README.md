# RepoAtlas

A self-hosted, local, multi-repository engineering intelligence system. Point it at one directory of Git
repositories; it discovers them, indexes them deterministically, and answers questions with file-and-line
evidence.

**Trust model**

> Repository evidence is authoritative. The LLM reasons over evidence but is never a source of repository facts.

Facts come from deterministic parsing. Where evidence is insufficient, RepoAtlas says so rather than inventing an
answer. See [docs/V1_SPEC.md](docs/V1_SPEC.md) for the specification and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.

## Status

**Slice A (the deterministic spine) is complete.** Slices B–E are not yet implemented; see the checklist in
[docs/V1_SPEC.md §23](docs/V1_SPEC.md#23-implementation-checklist).

| Working now | Not yet |
|---|---|
| `init`, `sync`, `status`, `repos`, `locate` | `ask`, `debug` (need Slice B) |
| Python parsing | TypeScript, Bash, Ruby, Salt, Jinja, Dockerfile (Slice C) |
| Exact/lexical search | Semantic search (Slice D) |
| Environment-variable and same-file symbol relationships | Cross-file and cross-repository relationships (Slice C) |
| — | Dependency versions, Docker (Slices C, E) |

No LLM is required for anything that currently works.

## Install

```bash
uv sync --all-groups
```

Requires Python 3.13+ (uv will install it) and `git`.

## Use

```bash
repoatlas init ~/work/repos     # discover and index every repository beneath the root
```

Then, from anywhere inside a managed repository, at any depth:

```bash
repoatlas sync                  # sync this repository only
repoatlas status                # this repository's index state
repoatlas locate "Where is SERVICE_HOST configured?"
```

From the configured root, commands are global:

```bash
repoatlas sync                  # sync everything
repoatlas status                # workspace-wide status
repoatlas repos                 # list managed repositories
```

From anywhere else, RepoAtlas refuses to guess and asks for an explicit repository:

```bash
repoatlas sync service-a        # works from any directory
```

Options: `--json` on every command, `--verbose` to print the resolved context, `--full` to force a rehash.

## Language support

Honest levels, not aspirations:

```
Python        structural (imports, functions, classes, methods, decorators, calls, env reads)
TypeScript    detected and indexed for text search; no structural parsing yet
JavaScript    detected and indexed for text search; no structural parsing yet
Bash          detected and indexed for text search; no structural parsing yet
Ruby          detected and indexed for text search; no structural parsing yet
Salt          detected and indexed for text search; no structural parsing yet
Jinja         detected and indexed for text search; no structural parsing yet
YAML / JSON   detected and indexed for text search; no structural parsing yet
Dockerfile    detected and indexed for text search; no structural parsing yet
```

Every file above is discovered, hashed, chunked and lexically searchable today. Only Python currently yields
symbols and relationships.

## Security

Repositories are untrusted input and are **never executed**: no shell scripts, no Python imports, no Ruby, no
Salt rendering, no npm lifecycle scripts, no Docker builds. Git is invoked only through a hardened read-only
wrapper with hooks disabled. Secret files (`.env`, `*.pem`, `*.key`, `id_rsa`, `id_ed25519`) are excluded before
any content is read.

## Development

```bash
uv run pytest          # 139 tests
uv run ruff check .
uv run ruff format --check .
uv run pyright
```
