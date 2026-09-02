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

**Slices A (the deterministic spine) and B (reasoning) are complete.** Slices C–E are not yet implemented; see
the checklist in [docs/V1_SPEC.md §23](docs/V1_SPEC.md#23-implementation-checklist).

| Working now | Not yet |
|---|---|
| `init`, `sync`, `status`, `repos`, `locate`, `ask`, `debug` | `watch` (optional) |
| Python parsing | TypeScript, Bash, Ruby, Salt, Jinja, Dockerfile (Slice C) |
| Exact/lexical search | Semantic search (Slice D) |
| Environment-variable and same-file symbol relationships | Cross-file and cross-repository relationships (Slice C) |
| — | Dependency versions, Docker (Slices C, E) |

`init`, `sync`, `status`, `repos` and `locate` need no LLM at all. `ask` and `debug` need a local reasoning
model; without one they exit 4 and name the command that works. `--no-llm` gives either of them evidence-only
output.

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
repoatlas ask "How is the service configured?"
repoatlas debug "SERVICE_HOST is empty at startup"
```

`locate` returns evidence. `ask` answers a question from that evidence. `debug` separates what RepoAtlas found
(`FACTS`, with file and line provenance) from what the model inferred (`HYPOTHESES`, with confidence), plus what
remains unknown and the next check worth making.

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

Options: `--json` on every command, `--verbose` to print the resolved context and grounding report, `--full` to
force a rehash, `--no-llm` for evidence-only `ask`/`debug`.

## Reasoning model

Configured in `~/.config/repoatlas/config.yaml`, and replaceable:

```yaml
llm:
  provider: ollama          # or "none" to disable reasoning entirely
  model: gpt-oss:20b
  base_url: http://localhost:11434
```

Set up Ollama with `ollama pull gpt-oss:20b`. RepoAtlas checks the server is reachable *and* the model is pulled
before running a query.

**The model reasons about evidence; it never supplies it.** Every citation in a model answer is verified against
the evidence that was actually retrieved. A citation naming a file, repository, or line range that was never
supplied is deleted, and a finding left without support is downgraded to an unknown — regardless of how confident
the model claimed to be. This is enforced in code, not by prompt wording. `--verbose` reports what was dropped.

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
uv run pytest          # 204 tests
uv run ruff check .
uv run ruff format --check .
uv run pyright
```
