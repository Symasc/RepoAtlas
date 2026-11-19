#!/usr/bin/env node
/**
 * Starts every server the end-to-end run needs, once, in parallel, and keeps them up.
 *
 *   pnpm e2e:servers            start them yourself, then `RA_E2E_REUSE=1 pnpm e2e`
 *   pnpm e2e                    Playwright runs this same script as its one web server
 *
 * Four `repoatlas serve` processes (live 7337, linked 7347, scoped 7357, r1 7367), each on a
 * corpus built fresh by its own command, and four explorers (7338, 7348, 7358, 7368), each
 * reading one API's connection file. One production build serves all four explorers: it is
 * made first (into `.next-e2e`) unless it is already newer than everything it is built from,
 * and `RA_E2E_REBUILD=1` forces it.
 *
 * Why not a Playwright `webServer` array: it starts its entries one after another and probes
 * each URL first, and where connecting to a closed loopback port hangs for minutes instead of
 * being refused, every probe of a port that is not up yet costs that long. Here readiness is
 * polled with a short timeout per attempt, so a closed port costs a fraction of a second.
 *
 * Prints `e2e servers ready` when all eight answer, then stays up until SIGINT or SIGTERM.
 * If any server exits, before or after that, everything is stopped and the exit status is 1.
 */
import { spawn } from "node:child_process";
import { existsSync, readdirSync, rmSync, statSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath } from "node:url";

import { E2E_STATE, assertScratchDatabase } from "./isolation.ts";

const APP = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const REPOSITORY = path.join(APP, "..");
const STATE = E2E_STATE;
const PYTHON = process.env.REPOATLAS_PYTHON ?? path.join(REPOSITORY, ".venv/bin/python");
const DIST_DIR = ".next-e2e";

/** Per attempt, so a port that hangs on connect never blocks the poll for long. */
const ATTEMPT_TIMEOUT_MS = 1500;
const POLL_PAUSE_MS = 300;
const READY_TIMEOUT_MS = 300_000;
/** How long the servers get to exit after SIGTERM before the whole group is killed. */
const GRACE_MS = 8000;

/**
 * One entry per pipeline: the corpus module that builds its index, its directory under the
 * state directory, and the API and explorer ports. The directory names are the ones
 * `playwright.config.ts` exports (LIVE, STATE/linked, SCOPED, R1) and the specs read.
 */
const PIPELINES = [
  { name: "live", module: "tests.topology_corpus", api: 7337, explorer: 7338 },
  { name: "linked", module: "tests.topology_corpus", api: 7347, explorer: 7348 },
  { name: "scoped", module: "tests.scoped_e2e_corpus", api: 7357, explorer: 7358 },
  { name: "r1", module: "tests.r1_e2e_corpus", api: 7367, explorer: 7368 },
].map((pipeline) => ({
  ...pipeline,
  dir: path.join(STATE, pipeline.name),
  connectionFile: path.join(STATE, `${pipeline.name}-api.json`),
}));

// refuse before anything starts if any e2e database could be the operator index
for (const { dir } of PIPELINES) assertScratchDatabase(path.join(dir, "state/atlas.db"), STATE);

/** Every process the corpus and its server touch is pointed away from this machine's own state. */
function apiServer({ name, module, api, dir, connectionFile }) {
  return {
    name: `api:${name}`,
    // built fresh by the command itself: the config is also loaded by every worker, so it
    // must not touch the disk
    command:
      `rm -rf "${dir}" && "${PYTHON}" -m ${module} "${dir}" && ` +
      `"${PYTHON}" -m repoatlas.cli.main serve --port ${api}`,
    cwd: REPOSITORY,
    env: {
      REPOATLAS_API_CONNECTION_FILE: connectionFile,
      REPOATLAS_CONFIG: path.join(dir, "config.yaml"),
      REPOATLAS_DB: path.join(dir, "state/atlas.db"),
      XDG_CONFIG_HOME: path.join(STATE, "xdg-config"),
      XDG_DATA_HOME: path.join(STATE, "xdg-data"),
    },
    // an unauthenticated GET is refused with 401, which is how it says it is up
    url: `http://127.0.0.1:${api}/v1`,
    anyResponse: true,
  };
}

/** Serves the build made before any server starts. */
function explorerServer({ name, explorer, connectionFile }) {
  return {
    name: `explorer:${name}`,
    command: `pnpm exec next start --hostname 127.0.0.1 --port ${explorer}`,
    cwd: APP,
    env: { REPOATLAS_API_CONNECTION_FILE: connectionFile, RA_DIST_DIR: DIST_DIR },
    url: `http://localhost:${explorer}`,
    anyResponse: false,
  };
}

const SERVERS = [...PIPELINES.map(apiServer), ...PIPELINES.map(explorerServer)];

const log = (message) => console.log(`[e2e] ${message}`);

// ---- the build -------------------------------------------------------------------------

/** The newest modification time under a file or directory; 0 when it does not exist. */
function newestMtime(target) {
  if (!existsSync(target)) return 0;
  const stat = statSync(target);
  if (!stat.isDirectory()) return stat.mtimeMs;
  let newest = stat.mtimeMs;
  for (const entry of readdirSync(target)) {
    newest = Math.max(newest, newestMtime(path.join(target, entry)));
  }
  return newest;
}

/** Returns why the build must be made, or null when the existing one is current. */
function reasonToBuild() {
  if (process.env.RA_E2E_REBUILD === "1") return "RA_E2E_REBUILD=1";
  const stamp = path.join(APP, DIST_DIR, "BUILD_ID");
  if (!existsSync(stamp)) return `${DIST_DIR}/BUILD_ID does not exist`;
  const built = statSync(stamp).mtimeMs;
  const inputs = ["app", "components", "lib", "public", "package.json", "pnpm-lock.yaml"];
  inputs.push(...readdirSync(APP).filter((entry) => entry.startsWith("next.config.")));
  inputs.push("tsconfig.json");
  for (const input of inputs) {
    if (newestMtime(path.join(APP, input)) >= built) return `${input} is newer than the build`;
  }
  return null;
}

// ---- processes -------------------------------------------------------------------------

/** Every launched process, each the leader of its own process group. */
const launched = [];
let stopping = false;

function relay(stream, sink, prefix) {
  let rest = "";
  stream.setEncoding("utf8");
  stream.on("data", (chunk) => {
    const lines = (rest + chunk).split("\n");
    rest = lines.pop() ?? "";
    for (const line of lines) sink.write(`${prefix}${line}\n`);
  });
  stream.on("end", () => {
    if (rest) sink.write(`${prefix}${rest}\n`);
  });
}

/** Runs `sh -c command` in a group of its own, its output prefixed with the server's name. */
function launch({ name, command, cwd, env }, onExit) {
  const child = spawn("sh", ["-c", command], {
    cwd,
    env: { ...process.env, ...env },
    detached: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  launched.push(child);
  relay(child.stdout, process.stdout, `[${name}] `);
  relay(child.stderr, process.stderr, `[${name}] `);
  child.on("error", (error) => fatal(`${name} could not start: ${error.message}`));
  child.on("exit", (code, signal) => onExit(signal ? `signal ${signal}` : `code ${code}`, code));
  return child;
}

function signalAll(signal) {
  for (const child of launched) {
    if (child.pid === undefined) continue;
    try {
      process.kill(-child.pid, signal);
    } catch {
      // the group is already gone
    }
  }
}

/** Stops every server and exits. Once only; a second signal skips the grace period. */
async function shutdown(status) {
  if (stopping) return;
  stopping = true;
  signalAll("SIGTERM");
  const deadline = Date.now() + GRACE_MS;
  const running = () => launched.some((c) => c.exitCode === null && c.signalCode === null);
  while (running() && Date.now() < deadline) await sleep(100);
  signalAll("SIGKILL");
  process.exit(status);
}

function fatal(message) {
  if (stopping) return;
  console.error(`[e2e] ${message}; stopping every server`);
  void shutdown(1);
}

for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(signal, () => {
    if (stopping) {
      signalAll("SIGKILL");
      process.exit(1);
    }
    log(`${signal}: stopping every server`);
    void shutdown(0);
  });
}

// ---- readiness -------------------------------------------------------------------------

async function isUp({ url, anyResponse }) {
  try {
    const response = await fetch(url, {
      signal: AbortSignal.timeout(ATTEMPT_TIMEOUT_MS),
      headers: { Accept: "*/*" },
    });
    await response.body?.cancel();
    // the same answers Playwright takes for "up": 2xx and 3xx, and 400 to 403
    return anyResponse || (response.status >= 200 && response.status < 404);
  } catch {
    return false;
  }
}

async function waitUntilUp(server) {
  const started = Date.now();
  while (!stopping) {
    if (await isUp(server)) {
      log(`${server.name} is up after ${((Date.now() - started) / 1000).toFixed(1)} s`);
      return;
    }
    if (Date.now() - started > READY_TIMEOUT_MS) {
      fatal(`${server.name} did not answer ${server.url} within ${READY_TIMEOUT_MS / 1000} s`);
      return;
    }
    await sleep(POLL_PAUSE_MS);
  }
}

// ---- main ------------------------------------------------------------------------------

async function build() {
  const reason = reasonToBuild();
  if (reason === null) {
    log(`skipping the explorer build: ${DIST_DIR} is newer than everything it is built from`);
    return;
  }
  log(`building the explorer: ${reason}`);
  const started = Date.now();
  const status = await new Promise((resolve) => {
    launch(
      {
        name: "build",
        command: "pnpm exec next build",
        cwd: APP,
        env: { REPOATLAS_API_CONNECTION_FILE: PIPELINES[0].connectionFile, RA_DIST_DIR: DIST_DIR },
      },
      (_description, code) => resolve(code),
    );
  });
  if (stopping) return;
  if (status !== 0) {
    fatal(`the explorer build failed (exit status ${status})`);
    return;
  }
  log(`built the explorer in ${((Date.now() - started) / 1000).toFixed(1)} s`);
}

let launchedAllReady = false;

async function main() {
  // a connection file from an earlier run would point an explorer at a server that is gone
  for (const { connectionFile } of PIPELINES) rmSync(connectionFile, { force: true });
  await build();
  if (stopping) return;
  for (const server of SERVERS) {
    launch(server, (description) =>
      fatal(`${server.name} exited (${description})${launchedAllReady ? "" : " before it was up"}`),
    );
  }
  log(`started ${SERVERS.length} servers; waiting for each to answer`);
  await Promise.all(SERVERS.map(waitUntilUp));
  if (stopping) return;
  launchedAllReady = true;
  console.log("e2e servers ready");
}

main().catch((error) => {
  console.error(`[e2e] ${error instanceof Error ? (error.stack ?? error.message) : error}`);
  void shutdown(1);
});
