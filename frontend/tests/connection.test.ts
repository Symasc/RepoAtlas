import { mkdtempSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { CONTRACT } from "@/lib/api/generated";
import {
  ConnectionError,
  connectionFilePath,
  parseConnection,
  readConnection,
} from "@/lib/server/connection";

const TOKEN = "test-token-0123456789abcdef0123456789abcdef";

/** The refusal a call raises: always API_CONNECTION_INVALID, with the reason in its detail. */
async function refused(call: () => unknown): Promise<string> {
  try {
    await call();
  } catch (error) {
    expect(error).toBeInstanceOf(ConnectionError);
    expect((error as ConnectionError).code).toBe("API_CONNECTION_INVALID");
    return (error as ConnectionError).detail;
  }
  throw new Error("expected the connection to be refused");
}

function record(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({ contract: CONTRACT, url: "http://127.0.0.1:7317", token: TOKEN, pid: 42, ...overrides });
}

describe("a connection record", () => {
  it("names a loopback origin and a token", () => {
    expect(parseConnection(record())).toEqual({ origin: "http://127.0.0.1:7317", token: TOKEN });
    expect(parseConnection(record({ url: "http://[::1]:7317" })).origin).toBe("http://[::1]:7317");
  });

  it.each([
    ["another contract", { contract: "repoatlas-local-api/2" }],
    ["a LAN address", { url: "http://192.168.1.9:7317" }],
    ["a public host", { url: "http://example.com:7317" }],
    ["https", { url: "https://127.0.0.1:7317" }],
    ["no port", { url: "http://127.0.0.1" }],
    ["a path", { url: "http://127.0.0.1:7317/v1" }],
    ["credentials", { url: "http://user:pw@127.0.0.1:7317" }],
    ["a short token", { token: "short" }],
    ["a token with a space", { token: `${TOKEN} x` }],
    ["no token", { token: undefined }],
  ])("is refused with %s", async (_, overrides) => {
    expect(await refused(() => parseConnection(record(overrides)))).toMatch(/connection record/);
  });

  it("is refused when it is not a JSON object", async () => {
    for (const text of ["", "[]", "null", "{", '"x"']) {
      expect(await refused(() => parseConnection(text))).toMatch(/not (JSON|a JSON object)/);
    }
  });
});

describe("the connection file", () => {
  it("is named only by the environment variable, with ~ expanded", () => {
    expect(connectionFilePath({})).toBeNull();
    expect(connectionFilePath({ REPOATLAS_API_CONNECTION_FILE: "  " })).toBeNull();
    expect(connectionFilePath({ REPOATLAS_API_CONNECTION_FILE: "~/x/api.json" })).toBe(
      path.join(homedir(), "x/api.json"),
    );
  });

  it("is read no further than a record could reach", async () => {
    const dir = mkdtempSync(path.join(tmpdir(), "ra-conn-"));
    const file = path.join(dir, "api.json");
    writeFileSync(file, record({ padding: "x".repeat(10_000) }));
    expect(await refused(() => readConnection({ REPOATLAS_API_CONNECTION_FILE: file }))).toMatch(
      /larger than a connection record/,
    );
  });

  it("is read fresh on every call, so a restarted server is found", async () => {
    const dir = mkdtempSync(path.join(tmpdir(), "ra-conn-"));
    const file = path.join(dir, "api.json");
    const env = { REPOATLAS_API_CONNECTION_FILE: file };
    writeFileSync(file, record());
    expect((await readConnection(env)).origin).toBe("http://127.0.0.1:7317");
    writeFileSync(file, record({ url: "http://127.0.0.1:7400" }));
    expect((await readConnection(env)).origin).toBe("http://127.0.0.1:7400");
  });
});
