import path from "node:path";

import { defineConfig, devices } from "@playwright/test";

import { E2E_STATE, assertScratchDatabase } from "./e2e/isolation";

/**
 * End to end against the real pipeline, three times over:
 *
 * - `live`: `repoatlas serve` on the index this environment configures (yours
 *   locally; in CI, a scratch workspace set by REPOATLAS_CONFIG and REPOATLAS_DB).
 * - `linked`: `repoatlas serve` on the linked test corpus, built fresh for the
 *   run: repositories that depend on, import from and call each other, which a
 *   live index may not have, so the lines between repositories are tested.
 *
 * - `scoped`: `repoatlas serve` on a small corpus whose requests write a base the
 *   source never states, built fresh for the run. Its test declares a scope, syncs
 *   and revokes, so it has an index of its own.
 *
 * - `r1`: `repoatlas serve` on the R1 provider-evidence corpus (tests/r1_e2e_corpus.py), built
 *   fresh for the run: services that declare routes they do not serve, which the HTTP
 *   Connections preview shows as hints. Its test only reads, but it has an index of its own.
 *
 * One production build of the explorer serves all, each reading its own
 * connection file. Nothing is mocked except where a test says so.
 *
 * Servers: by default Playwright starts everything fresh, through one launcher
 * (`e2e/servers.mjs`) that builds the explorer if it is stale and starts all eight servers in
 * parallel. `RA_E2E_REUSE=1` reuses servers you started yourself with `pnpm e2e:servers`. Their
 * state persists across runs, so restart the launcher after a run that mutates state, such as
 * the scoped project, which declares a scope, syncs and revokes.
 */
const APP = import.meta.dirname;
export const REPOSITORY = path.join(APP, "..");
/**
 * Outside the checkout: the linked corpus is a set of Git repositories, and
 * RepoAtlas discovers repositories nested at any depth, so under the working
 * tree they would join the next sync of the workspace it sits in.
 */
export const STATE = E2E_STATE;
export const PYTHON = process.env.REPOATLAS_PYTHON ?? path.join(REPOSITORY, ".venv/bin/python");
const LINKED = path.join(STATE, "linked");
/** The live server's own synthetic index: built fresh for each run, never the operator's. */
export const LIVE = path.join(STATE, "live");
export const LIVE_ENV: Record<string, string> = {
  REPOATLAS_CONFIG: path.join(LIVE, "config.yaml"),
  REPOATLAS_DB: path.join(LIVE, "state/atlas.db"),
  XDG_CONFIG_HOME: path.join(STATE, "xdg-config"),
  XDG_DATA_HOME: path.join(STATE, "xdg-data"),
};
// refuse before any server starts if any e2e database could be the operator index; the
// linked and scoped entries are checked below, once their state paths are defined
assertScratchDatabase(path.join(LIVE, "state/atlas.db"), STATE);
/**
 * The scoped corpus (tests/scoped_e2e_corpus.py): a base the source never states, written in two
 * files of one caller. Its own index and server, because its test declares and syncs: nothing
 * else may share what it changes. `scoped.spec.ts` runs `repoatlas sync` on it with `SCOPED_ENV`.
 */
export const SCOPED = path.join(STATE, "scoped");
/** What points a RepoAtlas process at the scoped corpus and away from this machine's own state. */
export const SCOPED_ENV: Record<string, string> = {
  REPOATLAS_CONFIG: path.join(SCOPED, "config.yaml"),
  REPOATLAS_DB: path.join(SCOPED, "state/atlas.db"),
  XDG_CONFIG_HOME: path.join(STATE, "xdg-config"),
  XDG_DATA_HOME: path.join(STATE, "xdg-data"),
};

/** The R1 provider-evidence corpus (tests/r1_e2e_corpus.py): its own index, connection file and ports. */
export const R1 = path.join(STATE, "r1");

assertScratchDatabase(path.join(LINKED, "state/atlas.db"), STATE);
assertScratchDatabase(path.join(SCOPED, "state/atlas.db"), STATE);
assertScratchDatabase(path.join(R1, "state/atlas.db"), STATE);
const PORTS = {
  liveApi: 7337,
  live: 7338,
  linkedApi: 7347,
  linked: 7348,
  scopedApi: 7357,
  scoped: 7358,
  r1Api: 7367,
  r1: 7368,
} as const;

/** `RA_E2E_REUSE=1`: use the servers `pnpm e2e:servers` started, instead of starting fresh ones. */
const REUSE = Boolean(process.env.RA_E2E_REUSE);

export default defineConfig({
  testDir: "e2e",
  // each API answers every request on one thread; keep the load honest
  workers: 1,
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  reporter: [["list"]],
  timeout: 60_000,
  expect: { timeout: 30_000 },
  use: { trace: "retain-on-failure" },
  projects: [
    {
      name: "live",
      testIgnore: /(linked|scoped|r1)\.spec\.ts/,
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1536, height: 960 },
        baseURL: `http://localhost:${PORTS.live}`,
      },
    },
    {
      name: "linked",
      testMatch: /linked\.spec\.ts/,
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1536, height: 960 },
        baseURL: `http://localhost:${PORTS.linked}`,
      },
    },
    {
      name: "scoped",
      testMatch: /scoped\.spec\.ts/,
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1536, height: 960 },
        baseURL: `http://localhost:${PORTS.scoped}`,
      },
    },
    {
      name: "r1",
      testMatch: /r1\.spec\.ts/,
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1536, height: 960 },
        baseURL: `http://localhost:${PORTS.r1}`,
      },
    },
  ],
  // One entry: the launcher starts all eight servers at once (e2e/servers.mjs) and prints
  // "e2e servers ready" when each answers. Without `url` Playwright probes nothing, which is the
  // point: a probe of a closed loopback port can hang for minutes. With RA_E2E_REUSE it probes
  // the last explorer once, to find the servers you started with `pnpm e2e:servers`.
  webServer: {
    command: "node e2e/servers.mjs",
    cwd: APP,
    ...(REUSE ? { url: `http://localhost:${PORTS.r1}` } : {}),
    wait: { stdout: /e2e servers ready/ },
    reuseExistingServer: REUSE,
    timeout: 900_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 10_000 },
  },
});
