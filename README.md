# RepoAtlas

RepoAtlas shows a coding agent what a change may affect before it makes the change.

It indexes the Git repositories on your machine and records how their code connects: imports and calls,
configuration and environment variables, packages that one repository provides and another consumes, and HTTP
requests matched to the routes that serve them. Give it a proposed change, such as a function, a file, a
configuration key or an environment variable, and it returns the places plausibly connected to it, ranked. Each
comes with the indexed path that explains why, including paths into other repositories.

The agent reads that source, decides what actually needs to change, and lets the compiler and tests confirm it.

RepoAtlas is deterministic and local. It parses source and runs no model of its own. Indexing makes no network
requests, and it never executes the code it reads.

## Why

When an agent changes a function, a config key or an API contract, text search finds the obvious call sites.
What it misses are the connections that do not share a spelling. One example is a consumer in another repository
that imports the function through a published package. Another is a service that reads the same environment
variable, and another is a client whose request reaches the handler being changed. RepoAtlas finds those paths in
the source and shows the evidence for each, so the agent investigates the right code, including code it would not
have thought to search.

## What it answers

| Question | What RepoAtlas returns |
|---|---|
| What might this change affect? | Ranked impact candidates, each with the indexed step and the file and lines behind it, across repositories |
| Where is this implemented, and what uses it? | Indexed source with file and line numbers, and the exact graph facts around one entity |
| Which repository provides this package, and who depends on it? | Declared, resolved and provided package facts from manifests and lockfiles |
| Which code calls this route, and what serves it? | The HTTP requests a repository makes and the routes it serves, matched by method and path |
| Is the index current? | How the index stands against your working trees |

Every piece of source RepoAtlas hands over carries an evidence id, and the citations in an agent's final answer
can be checked against the evidence actually supplied.

## An example

An agent is about to rename `createPaymentClient` in a shared `contracts` package. It asks RepoAtlas what that
may affect, and gets back something like this (illustrative, abbreviated):

```
seed    createPaymentClient   contracts/src/payment-client.ts:63

 1  createClients    web/src/api/clients.ts:20        uses (call)  exact  contracts -> web
 2  loadInvoice      billing/src/invoices.ts:42       uses (call)  exact  via package @acme/contracts, repository billing
 3  createServices   web/src/app/services.tsx:11      uses (call)  exact  via createClients

tests    web/src/__tests__/render.tsx:21, billing/test/invoices.spec.ts:8
omitted  314 candidates past the budget; this list is not exhaustive
```

Each line names the file and lines to read, and the step that connects it to the change. A candidate is a
reason to look, not a verdict that something will break.

## What it is not

- **Not a breakage oracle.** Candidates are plausible, source-backed paths. The compiler, the tests and the running
  code decide what actually breaks.
- **Not a substitute for reading source.** RepoAtlas points at the lines; the agent reads them.
- **Not an answer engine.** It runs no model and does no reasoning. The agent on the other end does both.
- **Not a runtime tracer.** Every link comes from static source. A matched route shows that the code agrees, not
  that a request reaches a live deployment.
- **Not a grep replacement.** Text search still has its place. RepoAtlas adds the connections text search cannot
  see.

## Quick start

You need Python 3.13 or later (uv will install it) and `git`.

```bash
git clone <this repository> RepoAtlas && cd RepoAtlas
uv sync
uv run repoatlas init ~/work/repos    # discover and index every Git repository under this root
```

Then connect your agent. RepoAtlas serves its tools over the Model Context Protocol on stdio:

```bash
repoatlas mcp
```

Register that command in your MCP client, using the absolute path to `.venv/bin/repoatlas` with the argument
`mcp`. Then give the agent the skills in [`skills/`](skills/). The full setup is in
[`docs/agent-integration.md`](docs/agent-integration.md).

The index does not refresh itself. After you change code, run `repoatlas sync`.

## Using it with an agent

The MCP server exposes ten tools. Five skills teach an agent which tools to use for which task:

| Skill | For |
|---|---|
| [`repoatlas-change-impact`](skills/repoatlas-change-impact/SKILL.md) | what a proposed change may affect |
| [`repoatlas-investigation`](skills/repoatlas-investigation/SKILL.md) | where and how existing code works |
| [`repoatlas-dependencies`](skills/repoatlas-dependencies/SKILL.md) | which packages are declared, resolved and provided, and by whom |
| [`repoatlas-http`](skills/repoatlas-http/SKILL.md) | which code calls a route, and what serves it |
| [`repoatlas-index`](skills/repoatlas-index/SKILL.md) | what is indexed, how current it is, and updating it |

The skills are vendor-neutral and ship inside the installed package. The detailed contract they share is
[`skills/repoatlas-investigation/references/contracts.md`](skills/repoatlas-investigation/references/contracts.md).

## Command line

The CLI covers indexing and quick lookups:

```bash
repoatlas init ~/work/repos        # index every repository under a root (once)
repoatlas sync                     # update the index
repoatlas status                   # how the index stands against the working tree
repoatlas repos                    # list indexed repositories
repoatlas locate "Where is SERVICE_HOST configured?"   # ranked file-and-line evidence
repoatlas mcp                      # serve the tools to an agent
```

Inside a repository, `sync` and `status` apply to that repository; at the configured root, they apply to all of
them. From anywhere else, name the repository: `repoatlas sync service-a`. Every command accepts `--json`.

Semantic search is optional. `repoatlas sync --embed` downloads a small local embedding model once (about 67 MB)
and uses it from then on. Until you run it, semantic retrieval is off and nothing else changes.

## What it understands

| | |
|---|---|
| Parsed structurally (definitions, imports, calls, environment reads) | Python, TypeScript, JavaScript, Bash |
| Indexed and searchable as text | Ruby, Salt, Jinja, YAML, JSON, Dockerfile, documentation |
| Package facts | PyPI, npm and RubyGems manifests and lockfiles |
| HTTP routes | OpenAPI 3, Swagger 2, FastAPI, Flask, Express, NestJS |
| HTTP calls | fetch and request wrappers, requests, httpx, axios |

Across repositories, RepoAtlas links code through the packages one repository provides and another declares,
and through HTTP where a service's own source states where it is published. What each extractor reads, and what
it does not, is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Safety

- Repositories are treated as untrusted input and are never executed: no scripts, imports, package lifecycle
  hooks or builds. Git runs through a read-only wrapper with hooks disabled.
- Secret files (`.env`, `*.pem`, `*.key`, `id_rsa`, `id_ed25519`) are excluded before any content is read.
- Installation is the only step that downloads anything. The parser grammars are pinned, hash-verified package
  dependencies, so indexing needs no network. Their licenses are in
  [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
- `repoatlas sync` changes the index only, never your source, and nothing syncs on its own.

## Docker

```bash
REPOS_DIR=/path/to/your/repos docker compose up -d
REPOS_DIR=/path/to/your/repos docker compose run --rm repoatlas init /repos
```

Repositories are mounted read-only and the index lives on a separate volume. The container runs as a non-root
user with a read-only root filesystem and all capabilities dropped.

## Documentation

- [`docs/agent-integration.md`](docs/agent-integration.md): connecting an agent over MCP
- [`skills/`](skills/): the agent skills
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): how RepoAtlas is built
- [`docs/V1_SPEC.md`](docs/V1_SPEC.md): the original specification

## Development

```bash
uv sync --all-groups
uv run pytest            # the default suite
uv run pytest -m slow    # slower tests; downloads the embedding model once
uv run ruff check .
uv run ruff format --check .
uv run pyright
```
