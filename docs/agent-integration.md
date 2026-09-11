# Agent integration

RepoAtlas serves its capabilities to an agent over the Model Context Protocol on
stdio. This document covers setup. The behaviour an agent should follow lives in
one place, [`skills/repoatlas-investigation/SKILL.md`](../skills/repoatlas-investigation/SKILL.md),
and is not repeated here.

## 1. Index a workspace

Indexing is an operator action, done once with the existing CLI:

```bash
repoatlas init ~/work/repos
```

There is no MCP tool that initializes an index, and the server does not create
one. A server started against an unindexed workspace starts, and its tools report
`NOT_INITIALIZED` rather than failing silently.

Keeping the index current is also explicit. `repoatlas sync` on the command line
and the `repoatlas_sync` tool do the same thing; nothing syncs on its own.

## 2. Point an MCP client at the server

Configure the client to launch:

```bash
repoatlas mcp
```

It speaks JSON-RPC over stdin and stdout and runs until the transport ends. There
is no daemon, no socket, no pidfile and no port. Operational output goes to
stderr, because on this transport stdout carries protocol and nothing else.

The command takes no arguments. It reads the same configuration the CLI reads, so
`REPOATLAS_CONFIG` and `REPOATLAS_DB` work as they do everywhere else, and the
server is simply another client of the local installation. There is no
MCP-specific configuration file, database or root setting.

Nothing is inferred from the directory the client happens to launch the server in.
Every tool call states its own repository scope.

## 3. Load the skill

Give the agent the canonical skill. It is vendor-neutral and lives at the
repository root:

```
skills/repoatlas-investigation/SKILL.md
```

An installed copy ships inside the package, so a `pip`- or `uv`-installed
RepoAtlas carries it too:

```
<site-packages>/repoatlas/skills/repoatlas-investigation/SKILL.md
```

Both are the same file; the wheel maps the root artifact into the package rather
than keeping a second copy. If a client has its own convention for where skills
or instructions live, point it at this file rather than transcribing it, so there
is one source of behavioural truth.

## 4. What the agent gets

Eight tools, and no resources or prompts:

```
repoatlas_repositories   repoatlas_status         repoatlas_search    repoatlas_inspect
repoatlas_relationships  repoatlas_dependencies   repoatlas_verify_grounding   repoatlas_sync
```

Seven read. `repoatlas_sync` is the only one that changes anything, and what it
changes is the index, never repository source.

Repository source is reachable only through the evidence-producing tools, which
enforce admission and record what they supply. No file resource is exposed, so
there is no second route to source text that bypasses that.

## 5. Investigations are process-local

An investigation is one reasoning task. The agent omits `investigation_id` to
start one and echoes the returned id to continue it. One server process serves
many investigations at once and they cannot read each other's evidence.

Nothing is persisted. When the server process ends, its investigations end with
it, and an id from a previous process comes back `UNKNOWN_INVESTIGATION` rather
than as a silently empty one. Evidence ids such as `E1` mean something only
inside the investigation that issued them.

## 6. No model, no network

The MCP server runs no model and makes no network request. Retrieval, traversal,
dependency reads and grounding verification are all deterministic and local, and
a full session including a sync completes with outbound networking unavailable.
`repoatlas ask` and `repoatlas debug` do consult a model, but they are CLI
commands and are not exposed as tools: over MCP the agent is the reasoner.
