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

**V1 is complete.** All five slices — spine, reasoning, breadth, ranking and delivery — are implemented and
validated; see the checklist in [docs/V1_SPEC.md §23](docs/V1_SPEC.md#23-implementation-checklist).

| Working now | Not yet |
|---|---|
| `init`, `sync`, `status`, `repos`, `locate`, `ask`, `debug` | `watch` (optional) |
| Python, TypeScript/JavaScript, Bash, Ruby, Salt, Jinja, YAML/JSON, Dockerfile | — |
| Dependency detection across pypi, npm and gem — declared *and* resolved versions | — |
| Cross-file imports and cross-repository relationships | — |
| Exact search, semantic search, bounded graph expansion, rank fusion | — |
| Docker image and compose stack, read-only repository mounts | — |

### Retrieval

Exact identifiers, exact error strings, symbol names, environment variables and graph-supported relationships are
**deterministic evidence**; embedding neighbours and lexical recall are **associative**. Rank fusion compares
scores only within a tier, so no similarity score can displace a fact about the code. Semantic search is an aid,
never a replacement.

Embeddings use a local [fastembed](https://github.com/qdrant/fastembed) model (`BAAI/bge-small-en-v1.5`, ONNX, no
PyTorch, no paid API). It is **not** downloaded automatically — an ordinary `sync` never stalls on a 67 MB fetch.
Run it once when you want semantic retrieval:

```bash
repoatlas sync --embed
```

Until then semantic retrieval is off and everything else is unaffected. Re-embedding is incremental at chunk
granularity: text that survives a reparse byte-for-byte is never embedded twice.

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

## Agents

RepoAtlas serves its capabilities to an agent over the Model Context Protocol on stdio:

```bash
repoatlas mcp
```

Eight tools, all deterministic and local: `repositories`, `status`, `search`, `inspect`, `relationships`,
`dependencies`, `verify_grounding` and `sync`. No model runs behind them and no resource or prompt is exposed, so
repository source is reachable only through the evidence path that records what it supplies. The agent is the
reasoner; RepoAtlas supplies evidence and checks citations against it.

How an agent should use that surface is one document,
[`skills/repoatlas-investigation/SKILL.md`](skills/repoatlas-investigation/SKILL.md), which is vendor-neutral and
ships with the installed package. Setup is in [`docs/agent-integration.md`](docs/agent-integration.md).

## Docker

```bash
REPOS_DIR=/path/to/your/repos docker compose up -d
REPOS_DIR=/path/to/your/repos docker compose run --rm repoatlas init /repos
REPOS_DIR=/path/to/your/repos docker compose run --rm \
  -w /repos/some-service repoatlas locate "Where is SERVICE_HOST configured?"
```

Repositories are mounted **read-only** and the index lives on a separate named volume, so RepoAtlas cannot write
into the tree it is reading — enforced by the mount rather than promised by the code. The container runs as a
non-root user with a read-only root filesystem and all capabilities dropped. `ollama` runs alongside on loopback;
pull a model into it (`docker compose exec ollama ollama pull gpt-oss:20b`) before using `ask` or `debug`.

## Security

Repositories are untrusted input and are **never executed**: no shell scripts, no Python imports, no Ruby, no
Salt rendering, no npm lifecycle scripts, no Docker builds. Git is invoked only through a hardened read-only
wrapper with hooks disabled. Secret files (`.env`, `*.pem`, `*.key`, `id_rsa`, `id_ed25519`) are excluded before
any content is read — asserted separately at every stage that content could otherwise reach, including the model
prompt and `--verbose` output.

## Development

```bash
uv run pytest          # 635 tests
uv run pytest -m slow  # 10 more; downloads the embedding model once
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

Every command is prefixed with `uv run` so the environment is guaranteed to match `uv.lock`, but Pyright is not
*dependent* on it: `pyproject.toml` pins `venvPath`/`venv`, so a bare `pyright` from an activated shell analyses
the same environment and reports the same result. Pyright locates its interpreter by searching `PATH` and ignores
`VIRTUAL_ENV`, so without that setting the two invocations disagreed — `uv run pyright` was clean while a bare
`pyright` reported 95 unresolved imports for dependencies that were installed all along.
