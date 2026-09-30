# RepoAtlas explorer

A local web app that draws the repositories RepoAtlas has indexed on this
machine. It reads the live index through `repoatlas serve` (see
[docs/local-api.md](../docs/local-api.md)); it never opens the database and
ships no data of its own.

```
browser ──► Next.js server (/api/ra/*) ──► repoatlas serve (loopback) ──► the index
```

## Run it

1. Start the local API. It prints where it wrote its connection file:

   ```sh
   repoatlas serve
   ```

2. Point the explorer at that file and start it (Node 22, pnpm 11):

   ```sh
   cd frontend
   pnpm install
   REPOATLAS_API_CONNECTION_FILE=~/.local/share/repoatlas/api.json pnpm dev
   ```

   Or put `REPOATLAS_API_CONNECTION_FILE=...` in `frontend/.env.local`.

3. Open <http://localhost:7318>.

The connection file is read on every request, so restarting `repoatlas serve`
needs no restart here. If the API is not running, the explorer says so and
shows the command to start it.

## The `/api/ra` route

The browser only ever talks to its own origin. The route adds the API's bearer
token on the server, which makes it the one thing that must refuse other
callers: it answers only a loopback `Host` (a DNS-rebinding page arrives with
its own name), no cross-site request, JSON bodies up to 1 MiB, and only the
endpoints the contract lists (`sync` and `verify_grounding` are not among
them). It forwards only to a loopback `http://host:port` named by the
connection file. The token and the file's path never reach the browser.

## Checks

| Command | What it checks |
|---|---|
| `pnpm dto:check` | `lib/api/generated.ts` matches `python -m repoatlas.api.schema` |
| `pnpm typecheck` | strict TypeScript |
| `pnpm lint` | ESLint (Next's rules), no warnings allowed |
| `pnpm test` | unit tests: the route's refusals, the connection file, the models |
| `pnpm e2e` | Playwright against a production build and a real `repoatlas serve` |
| `pnpm check` | the first four together |

`pnpm dto` regenerates the types after the contract changes. It runs the
repository's `.venv/bin/python`, or `REPOATLAS_PYTHON`.

`pnpm e2e` starts its own API (port 7337) and explorer (port 7338) on the
index your environment configures. To run it on the fixtures instead, index
`tests/fixtures/repos` into a scratch workspace and set `REPOATLAS_CONFIG`
and `REPOATLAS_DB` to it first. It needs Playwright's Chromium
(`pnpm exec playwright install chromium`).
