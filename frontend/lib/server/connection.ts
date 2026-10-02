import { open } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";

import { CONTRACT } from "@/lib/api/generated";

/**
 * How the explorer's server finds `repoatlas serve`: the connection file the
 * API writes, named by this variable and nothing else (docs/local-api.md).
 */
const CONNECTION_FILE_ENV = "REPOATLAS_API_CONNECTION_FILE";

/** The most of a connection file read; a record is ~150 bytes, as the API itself assumes. */
const CEILING = 4096;
const MIN_TOKEN_LENGTH = 32;
/** `URL.hostname` spells IPv6 loopback with its brackets. */
const LOOPBACK = new Set(["127.0.0.1", "[::1]"]);

/** Only the variables it reads: a plain object in tests, `process.env` in the server. */
export type Environment = Readonly<Record<string, string | undefined>>;

export interface Connection {
  /** `http://127.0.0.1:<port>` or `http://[::1]:<port>`, never anything else. */
  readonly origin: string;
  readonly token: string;
}

export type ConnectionErrorCode = "API_NOT_CONFIGURED" | "API_NOT_RUNNING" | "API_CONNECTION_INVALID";

export class ConnectionError extends Error {
  constructor(
    readonly code: ConnectionErrorCode,
    message: string,
    readonly detail: string,
  ) {
    super(message);
    this.name = "ConnectionError";
  }
}

export function connectionFilePath(env: Environment): string | null {
  const value = env[CONNECTION_FILE_ENV]?.trim();
  if (!value) return null;
  if (value === "~") return homedir();
  if (value.startsWith("~/")) return path.join(homedir(), value.slice(2));
  return path.resolve(value);
}

/**
 * Reads the connection file on every call, so a restarted server's new port and
 * token are picked up without restarting the explorer. The path stays on the
 * server: errors name the variable, not the location on disk.
 */
export async function readConnection(env: Environment = process.env): Promise<Connection> {
  const file = connectionFilePath(env);
  if (file === null) {
    throw new ConnectionError(
      "API_NOT_CONFIGURED",
      `${CONNECTION_FILE_ENV} is not set.`,
      `Start the explorer with \`repoatlas ui\`, or set ${CONNECTION_FILE_ENV} for it to the connection file \`repoatlas serve\` reports, then restart it.`,
    );
  }
  let raw: Buffer;
  try {
    raw = await readBounded(file);
  } catch (error) {
    if (isMissing(error)) {
      throw new ConnectionError(
        "API_NOT_RUNNING",
        "RepoAtlas's local API is not running.",
        `There is no connection file where ${CONNECTION_FILE_ENV} points. Start the API with \`repoatlas serve\`.`,
      );
    }
    throw invalid("it cannot be read");
  }
  if (raw.length > CEILING) throw invalid("it is larger than a connection record");
  return parseConnection(raw.toString("utf8"));
}

export function parseConnection(text: string): Connection {
  let record: unknown;
  try {
    record = JSON.parse(text);
  } catch {
    throw invalid("it is not JSON");
  }
  if (typeof record !== "object" || record === null || Array.isArray(record)) {
    throw invalid("it is not a JSON object");
  }
  const { contract, url, token } = record as Record<string, unknown>;
  if (contract !== CONTRACT) throw invalid(`its contract is not ${CONTRACT}`);
  if (typeof url !== "string" || !URL.canParse(url)) throw invalid("its url is not a URL");
  const parsed = new URL(url);
  if (
    parsed.protocol !== "http:" ||
    !LOOPBACK.has(parsed.hostname) ||
    parsed.port === "" ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    parsed.pathname !== "/" ||
    parsed.search !== "" ||
    parsed.hash !== ""
  ) {
    throw invalid("its url is not a loopback http://host:port");
  }
  if (typeof token !== "string" || token.length < MIN_TOKEN_LENGTH || !/^[\x21-\x7e]+$/.test(token)) {
    throw invalid("its token is malformed");
  }
  return { origin: parsed.origin, token };
}

async function readBounded(file: string): Promise<Buffer> {
  const handle = await open(file, "r");
  try {
    const buffer = Buffer.alloc(CEILING + 1);
    const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
    return buffer.subarray(0, bytesRead);
  } finally {
    await handle.close();
  }
}

function isMissing(error: unknown): boolean {
  return error instanceof Error && "code" in error && error.code === "ENOENT";
}

function invalid(reason: string): ConnectionError {
  return new ConnectionError(
    "API_CONNECTION_INVALID",
    "The connection file can't be used.",
    `The file ${CONNECTION_FILE_ENV} names is not a RepoAtlas connection record: ${reason}.`,
  );
}
