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

## What it shows

- **All repositories.** Every indexed repository, and the links the index
  records between them, one line per kind: code (solid), HTTP (dashed) and
  package dependency (double). Kinds are never merged into one line.
  Repositories with no links sit in a row of their own.
- **A repository, opened.** Click one: its directories become cards listing
  what is in them, its files share a card, and what they link to is drawn
  around them (other repositories and elsewhere in this one on the left;
  modules, packages, environment variables, commands and endpoints shared
  across the index on the right). At rest the level's total per kind is drawn;
  point at or focus a card to see its own lines. Click a card to go into it.

Where you are is in the URL (`?repo=<slug>&path=<dir>`), so Back climbs out
and a view can be shared.

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

`pnpm e2e` runs two pipelines from one production build, each with its own
`repoatlas serve`:

- `live` (ports 7337/7338): the index your environment configures. In CI, set
  `REPOATLAS_CONFIG` and `REPOATLAS_DB` to a scratch workspace first.
- `linked` (ports 7347/7348): the linked test corpus, built fresh by
  `python -m tests.topology_corpus` into the system temp directory, never into
  the checkout (RepoAtlas would discover its repositories on the next sync).
  Its repositories link to each other, so the lines between repositories are
  tested even when a live index has none.

It needs Playwright's Chromium (`pnpm exec playwright install chromium`).
