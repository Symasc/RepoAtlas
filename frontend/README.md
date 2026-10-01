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

The explorer is built to be read, not to show every edge at once. It shows the
summary first and the detail when you ask for it.

- **All repositories.** A constellation. The selected repository, or else the
  one with the most partners, sits at the centre. Its partners ring it, and
  theirs ring them. Repositories linked only among themselves form their own
  small constellations beside it. Repositories with no links sit in their own
  group. When nothing links at all, the repositories ring the workspace hub,
  which says so.
- **A repository, opened.** The directory you are in is a frame around its
  cards: one per directory, listing what is in it, and one for its files.
  Each card counts its links per family instead of drawing them. At rest, only
  the level's totals are drawn: lanes from the frame to other repositories and
  elsewhere in this one (left), and to modules, packages, environment
  variables, commands and endpoints shared across the index (right).
- **A file.** The file is the anchor. What links to it is on the left, and what
  it links to is on the right. Only the strongest few on each side are shown;
  "+N more" expands the rest.

Lines are drawn per family and never merged into one line:

| Family | Line |
|---|---|
| code (imports, uses, calls…) | solid |
| HTTP | dashed |
| package dependency | double |
| configuration and environment variables | dotted |
| the connection being explained | thick, in the accent colour |

Every line has an arrowhead for its direction. Its width grows with its count.
Node families differ by shape, icon and border as well as colour. The legend's
shapes button names them.

### Pointing, selecting, opening

| Action | Result |
|---|---|
| Point at a node (or focus it) | Previews its links; everything else fades. |
| First press | Selects it: its links stay lit with counts, and the inspector opens. |
| Second press, "Open", or Enter on the selected node | Goes in, zooming into it. |
| Location trail, or Back | Climbs out, zooming out onto where you came from. That node arrives selected. |
| Escape, or a press on empty canvas | Clears the selection. |

The inspector, docked beside the canvas (below it on a narrow window), holds
the detail:

- **Connections:** every link, by direction and family, strongest first.
- **Contents** or **Files:** what a directory holds.
- **Code:** a file's indexed source, with line numbers.
- **Details:** the facts the index has: size and lines from `inspect`, language,
  declarations.
- **Evidence:** for a chosen connection, the exact stored relationships behind
  it (`topology/edge`) and the lines that assert them. Evidence and source for
  one inspected thing share one investigation.

Where you are is in the URL (`?repo=<slug>&path=<dir or file>&sel=<node>`), so
a view can be shared. Selecting replaces the history entry and going somewhere
adds one, so Back climbs out.

Motion is limited to viewport transforms and opacity, from about 250ms to
560ms, with no animated backgrounds. With reduced motion, everything moves
instantly.

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
