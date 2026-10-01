# The local API (`repoatlas-local-api/1`)

`repoatlas serve` makes the RepoAtlas capabilities available over HTTP on loopback, for a local app's
**server** process, such as the RepoAtlas frontend's Next.js server. That process holds the token and calls
this API on its browser's behalf. The browser never holds the token and never talks to this port.

It is a second transport beside `repoatlas mcp`, and like that one it only carries requests and responses.
Every answer comes from the same capability service over the same local index.

## Starting it

```
repoatlas serve [--host 127.0.0.1] [--port 7317]
```

- **Host:** loopback only: `127.0.0.1`, `::1` or `localhost`, which binds `127.0.0.1`. Any other host is
  refused with exit code 2 before anything is opened.
- **Port:** `--port 0` picks a free port. A port already in use is refused with exit code 2.
- **Token:** `REPOATLAS_API_TOKEN` when set. It must be at least 32 printable ASCII characters with no
  spaces, or the server refuses to start (exit 2). When the variable is unset, a random token is generated.
  The token is never printed.
- **Stopping:** Ctrl-C or SIGTERM stops it cleanly.

On start it prints the URL and the connection file's path to stderr, never the token.

## Finding it: the connection file

The server writes a connection file:

```json
{"contract": "repoatlas-local-api/1", "url": "http://127.0.0.1:7317", "token": "…", "pid": 12345}
```

- **Path:** `REPOATLAS_API_CONNECTION_FILE` when set, otherwise `api.json` in the directory holding the index
  database.
- **Permissions:** written atomically and readable by its owner only (mode 0600).
- **Removal:** removed when the server stops, unless another server has replaced it since.

**A client locates the API only through `REPOATLAS_API_CONNECTION_FILE`.** It reads `url` and `token` from
that file, and checks that `contract` is the one it was built for. Discovering the file without that
variable, for example through a launcher that starts both processes, is deferred.

## Requests

```
POST /v1/<name>
Host: 127.0.0.1:<port>
Authorization: Bearer <token>
Content-Type: application/json
Content-Length: <n>

<the capability's request, as a JSON object>
```

- **Host:** must be `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`. This is the defence against
  DNS rebinding.
- **Body:** at most 1 MiB, sent with `Content-Length`; chunked bodies are refused. The body is validated by
  the capability's own request model, and is passed on unchanged.
- **Connections:** one request per connection (HTTP/1.0). Requests are answered one at a time, in arrival
  order, on the single thread that owns the index connection. A connection silent for 30 seconds is dropped.

`GET /v1` answers with the contract and the served names.

### Served

| Name | Capability |
|---|---|
| `repositories`, `status` | what is indexed, and how current it is |
| `search`, `inspect` | find evidence; read indexed source and recorded evidence |
| `relationships`, `impact` | what one entity is connected to; what a change may reach |
| `dependencies`, `http_operations` | declared packages; HTTP operations made and served |
| `topology/overview` | which repositories are linked to which, by kind: `observed`, `http`, `dependency` |
| `topology/children` | one page of a directory's or file's children, and how every child links |
| `topology/edge` | one exact stored edge: its evidence, crossing, and multiplicity |
| `topology/files` | indexed files whose paths hold every term of a query, best-named first |
| `topology/references` | what each line of one file declares and refers to, in source order |

**Not served:** `sync`, the only write, and `verify_grounding`, which checks an agent's answer. Both remain
agent tools over MCP. The topology capabilities are served here only; they are not MCP tools.

## Responses

A success is `200 {"ok": true, "result": <response>}`.

A refusal is `{"ok": false, "error": {"code", "category", "message", "detail", "retryable", "exit_code",
"context"}}`, answered with one of these statuses:

| Status | When | `code` |
|---|---|---|
| 400 | a capability refusal of category `invalid_request` or `context` | the capability's |
| 400 | malformed JSON, or a body that is not an object | `INVALID_JSON` |
| 400 | a `Content-Length` that is not a length | `INVALID_BODY` |
| 400 or other | HTTP the server cannot parse: request line, headers | `MALFORMED_HTTP` |
| 400 | the body does not validate; `context.fields` lists `{field, error}`, never the input | `INVALID_REQUEST` |
| 401 | no token, or the wrong one | `UNAUTHORIZED` |
| 403 | the Host header names anything but this server | `HOST_NOT_ALLOWED` |
| 404 | a capability refusal of category `not_found` | the capability's |
| 404 | an unserved path, including any query string | `UNKNOWN_ENDPOINT` |
| 405 | a method the path does not answer (`Allow` names the one it does) | `METHOD_NOT_ALLOWED` |
| 409 | category `session`: an unknown or expired investigation, a full ledger | the capability's |
| 411 | no `Content-Length` | `LENGTH_REQUIRED` |
| 413 | a body over 1 MiB | `BODY_TOO_LARGE` |
| 415 | a content type other than `application/json` | `UNSUPPORTED_MEDIA_TYPE` |
| 503 | category `contention` or `index` | the capability's |
| 500 | category `internal` | the capability's |

The host and token are checked before the route, so a request without the token cannot tell a served path
from an unserved one.

## The schema

```
python -m repoatlas.api.schema
```

prints one JSON document generated from the registry the server routes with. For each endpoint it gives the
path, the request schema and the **full** response schema, together with the error envelope and the index.
A client generates its types from it.

**Presentation:** `search`, `inspect`, `relationships` and `impact` accept `presentation`, and `impact`
defaults to `compact`. The compact projections are not described by the schema, so a client that wants the
described shape passes `"presentation": "full"`. The transport never rewrites a request to add it.

## Investigations

An investigation is the caller's, and nothing in the transport starts, ends or rewrites one:

- a request that carries `investigation_id` has it passed on exactly;
- every evidence-producing response returns the `investigation_id` its evidence was recorded in.

`topology/edge` follows the same rule as `relationships`. **Without** `investigation_id`, it starts a new
investigation, records the edge's asserting lines there, and returns its id. **With** one, it reuses that
investigation exactly, and an unknown id is refused (409).

So an edge reference taken from `topology/overview` or `topology/children` reaches source directly:
`topology/edge {edge_ref}`, then `inspect {target: {evidence_id}, investigation_id}`, with no search
beforehand. `topology/overview`, `topology/children`, `topology/files` and `topology/references` record
no evidence and start no investigation, so a client may ask them as often as it likes, a search box on
every keystroke included.

Investigations live in the server process: at most 32, each expiring after an hour idle, and none survives
a restart.

## Edge references

An `edge_ref` names one stored edge exactly: its endpoints, type, asserting file and lines, parser,
confidence, and the SHA-256 of how the linker resolved it. It is **opaque**: a client passes it back
unchanged and never parses it.

- Rows identical in every field are one fact, reported with `multiplicity`.
- Rows that differ only in their resolution are different facts with different references.
- A reference stops resolving when its edge is gone, for example after a sync, and is then refused as
  `EDGE_NOT_FOUND`. So is a reference outside the request's scope, so existence never leaks across scope.

## Files and references

`topology/files {query}` matches every whitespace-separated term against each file's repository-relative
path, ignoring ASCII case; `%` and `_` are literal. Files whose own name holds the last term come first
(named exactly that, then starting with it, then containing it), then the shortest paths. `total` counts
every match in scope, and `limit` (at most 50) caps how many are listed.

`topology/references {path}` reads one file: its declarations, and every located edge the file asserts,
containment left out, in source order. A reference's lines are lines of that file, and its `target` is the
entity the index resolved it to, wherever that is. Like `topology/children`, it lists references into other
repositories only with `cross_repository`; otherwise it counts them in `other_repositories`.
