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

function api(port: number, connectionFile: string, env: Record<string, string> = {}, build = "") {
  return {
    command: `${build}"${PYTHON}" -m repoatlas.cli.main serve --port ${port}`,
    cwd: REPOSITORY,
    // an unauthenticated GET is refused with 401, which is how it says it is up
    url: `http://127.0.0.1:${port}/v1`,
    env: { REPOATLAS_API_CONNECTION_FILE: connectionFile, ...env },
    reuseExistingServer: false,
    timeout: 180_000,
  };
}

function explorer(port: number, connectionFile: string, build: boolean) {
  return {
    command: `${build ? "pnpm exec next build && " : ""}pnpm exec next start --hostname 127.0.0.1 --port ${port}`,
    cwd: APP,
    url: `http://localhost:${port}`,
    env: { REPOATLAS_API_CONNECTION_FILE: connectionFile, RA_DIST_DIR: ".next-e2e" },
    reuseExistingServer: false,
    timeout: 300_000,
  };
}

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
  // started in order: the first explorer builds, the second serves the same build
  webServer: [
    api(
      PORTS.liveApi,
      path.join(STATE, "live-api.json"),
      LIVE_ENV,
      // built fresh by the command itself, like the linked and scoped corpora
      `rm -rf "${LIVE}" && "${PYTHON}" -m tests.topology_corpus "${LIVE}" && `,
    ),
    api(
      PORTS.linkedApi,
      path.join(STATE, "linked-api.json"),
      {
        REPOATLAS_CONFIG: path.join(LINKED, "config.yaml"),
        REPOATLAS_DB: path.join(LINKED, "state/atlas.db"),
        // the corpus and its server never touch this machine's RepoAtlas state
        XDG_CONFIG_HOME: path.join(STATE, "xdg-config"),
        XDG_DATA_HOME: path.join(STATE, "xdg-data"),
      },
      // built fresh by the command itself: the config is also loaded by every
      // worker, so it must not touch the disk
      `rm -rf "${LINKED}" && "${PYTHON}" -m tests.topology_corpus "${LINKED}" && `,
    ),
    // the scoped corpus: its own index, connection file and ports, built fresh like the linked one
    api(
      PORTS.scopedApi,
      path.join(STATE, "scoped-api.json"),
      SCOPED_ENV,
      `rm -rf "${SCOPED}" && "${PYTHON}" -m tests.scoped_e2e_corpus "${SCOPED}" && `,
    ),
    // the R1 provider-evidence corpus: read-only in its test, built fresh like the others
    api(
      PORTS.r1Api,
      path.join(STATE, "r1-api.json"),
      {
        REPOATLAS_CONFIG: path.join(R1, "config.yaml"),
        REPOATLAS_DB: path.join(R1, "state/atlas.db"),
        XDG_CONFIG_HOME: path.join(STATE, "xdg-config"),
        XDG_DATA_HOME: path.join(STATE, "xdg-data"),
      },
      `rm -rf "${R1}" && "${PYTHON}" -m tests.r1_e2e_corpus "${R1}" && `,
    ),
    explorer(PORTS.live, path.join(STATE, "live-api.json"), true),
    explorer(PORTS.linked, path.join(STATE, "linked-api.json"), false),
    explorer(PORTS.scoped, path.join(STATE, "scoped-api.json"), false),
    explorer(PORTS.r1, path.join(STATE, "r1-api.json"), false),
  ],
});
