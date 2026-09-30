import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { CONTRACT } from "@/lib/api/generated";
import { hostName, MAX_BODY, proxy, refusal } from "@/lib/server/proxy";

const TOKEN = "test-token-0123456789abcdef0123456789abcdef";
const API = "http://127.0.0.1:7399";

function request(headers: Record<string, string> = {}, body: BodyInit | null = "{}"): Request {
  return new Request("http://localhost:7318/api/ra/repositories", {
    method: "POST",
    headers: { host: "localhost:7318", "content-type": "application/json", ...headers },
    body,
  });
}

function connectionEnv(record: object = { contract: CONTRACT, url: API, token: TOKEN, pid: 1 }) {
  const dir = mkdtempSync(path.join(tmpdir(), "ra-proxy-"));
  const file = path.join(dir, "api.json");
  writeFileSync(file, JSON.stringify(record));
  return { REPOATLAS_API_CONNECTION_FILE: file };
}

async function errorOf(response: Response): Promise<{ code: string; category: string }> {
  const payload = (await response.json()) as { ok: boolean; error: { code: string; category: string } };
  expect(payload.ok).toBe(false);
  return payload.error;
}

afterEach(() => vi.restoreAllMocks());

describe("the Host header", () => {
  it.each([
    ["localhost:7318", "localhost"],
    ["127.0.0.1:7318", "127.0.0.1"],
    ["[::1]:7318", "[::1]"],
    ["LOCALHOST:7318", "localhost"],
    ["localhost", "localhost"],
  ])("reads %s as %s", (header, name) => {
    expect(hostName(header)).toBe(name);
  });

  it.each(["evil@localhost", "localhost/x", "localhost:7318/x", "", "a b"])(
    "refuses to read %j as a host",
    (header) => {
      expect(hostName(header)).toBeNull();
    },
  );

  it.each(["attacker.example:7318", "localhost.attacker.example", "192.168.1.5:7318", "evil@localhost"])(
    "refuses %s, which is how a rebound DNS name arrives",
    (host) => {
      expect(refusal(request({ host }))?.code).toBe("HOST_NOT_ALLOWED");
    },
  );

  it.each(["localhost:7318", "127.0.0.1:7318", "[::1]:7318"])("accepts %s", (host) => {
    expect(refusal(request({ host, origin: `http://${host}` }))).toBeNull();
  });
});

describe("cross-site requests", () => {
  it.each(["cross-site", "same-site"])("refuses Sec-Fetch-Site: %s", (site) => {
    expect(refusal(request({ "sec-fetch-site": site }))?.code).toBe("CROSS_SITE_REQUEST");
  });

  it("refuses an Origin that is not this host", () => {
    expect(refusal(request({ origin: "http://attacker.example" }))?.code).toBe("CROSS_SITE_REQUEST");
    expect(refusal(request({ origin: "http://localhost:9999" }))?.code).toBe("CROSS_SITE_REQUEST");
  });

  it("accepts this page's own requests", () => {
    expect(
      refusal(request({ "sec-fetch-site": "same-origin", origin: "http://localhost:7318" })),
    ).toBeNull();
  });
});

describe("the body", () => {
  it("must be JSON, which a form or a no-cors fetch cannot send", async () => {
    const response = await proxy(request({ "content-type": "text/plain" }), "repositories");
    expect(response.status).toBe(415);
    expect((await errorOf(response)).code).toBe("UNSUPPORTED_MEDIA_TYPE");
  });

  it("is refused by its declared length before it is read", async () => {
    const response = await proxy(request({ "content-length": String(MAX_BODY + 1) }), "repositories");
    expect(response.status).toBe(413);
  });

  it("is refused past the ceiling even without a declared length", async () => {
    const chunk = new Uint8Array(64 * 1024);
    let sent = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        sent += chunk.byteLength;
        controller.enqueue(chunk);
        if (sent > 4 * MAX_BODY) controller.close();
      },
    });
    const streamed = new Request("http://localhost:7318/api/ra/repositories", {
      method: "POST",
      headers: { host: "localhost:7318", "content-type": "application/json" },
      body: stream,
      duplex: "half",
    } as RequestInit);
    const response = await proxy(streamed, "repositories", { env: connectionEnv() });
    expect(response.status).toBe(413);
    expect(sent).toBeLessThan(2 * MAX_BODY);
  });
});

describe("forwarding", () => {
  it("refuses an endpoint the contract does not list, without touching the API", async () => {
    const fetch = vi.fn();
    for (const name of ["sync", "verify_grounding", "../v1/status", "topology"]) {
      const response = await proxy(request(), name, { env: connectionEnv(), fetch });
      expect(response.status).toBe(404);
      expect((await errorOf(response)).code).toBe("UNKNOWN_ENDPOINT");
    }
    expect(fetch).not.toHaveBeenCalled();
  });

  it("sends the body to the connection file's address with the token added", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(
      async () =>
        new Response('{"ok":true,"result":{}}', {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    );
    const body = JSON.stringify({ context: { scope: "global" } });
    const response = await proxy(request({}, body), "topology/overview", {
      env: connectionEnv(),
      fetch,
    });
    expect(response.status).toBe(200);
    expect(await response.text()).toBe('{"ok":true,"result":{}}');
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe(`${API}/v1/topology/overview`);
    expect(new Headers(init?.headers).get("authorization")).toBe(`Bearer ${TOKEN}`);
    expect(new TextDecoder().decode(init?.body as Uint8Array)).toBe(body);
    expect(init?.redirect).toBe("error");
  });

  it("passes the API's refusals through with their status", async () => {
    const refusal = '{"ok":false,"error":{"code":"EDGE_NOT_FOUND","category":"not_found","message":"x"}}';
    const fetch = vi.fn<typeof globalThis.fetch>(
      async () => new Response(refusal, { status: 404, headers: { "content-type": "application/json" } }),
    );
    const response = await proxy(request(), "topology/edge", { env: connectionEnv(), fetch });
    expect(response.status).toBe(404);
    expect(await response.text()).toBe(refusal);
  });

  it("reports an unset connection variable as unconfigured", async () => {
    const response = await proxy(request(), "status", { env: {} });
    expect(response.status).toBe(503);
    expect((await errorOf(response)).code).toBe("API_NOT_CONFIGURED");
  });

  it("reports a missing connection file as a server that is not running", async () => {
    const env = { REPOATLAS_API_CONNECTION_FILE: path.join(tmpdir(), "no-such-dir", "api.json") };
    const response = await proxy(request(), "status", { env });
    expect(response.status).toBe(503);
    expect((await errorOf(response)).code).toBe("API_NOT_RUNNING");
  });

  it("reports a server that has gone as unreachable", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () => {
      throw new TypeError("fetch failed");
    });
    const response = await proxy(request(), "status", { env: connectionEnv(), fetch });
    expect(response.status).toBe(503);
    expect((await errorOf(response)).code).toBe("API_UNREACHABLE");
  });

  it("refuses to forward to an address that is not loopback", async () => {
    const fetch = vi.fn();
    const env = connectionEnv({ contract: CONTRACT, url: "http://192.168.1.9:7317", token: TOKEN });
    const response = await proxy(request(), "status", { env, fetch });
    expect((await errorOf(response)).code).toBe("API_CONNECTION_INVALID");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("never returns the token or the connection file's path", async () => {
    const env = connectionEnv({ contract: "other/1", url: API, token: TOKEN });
    const text = await (await proxy(request(), "status", { env })).text();
    expect(text).not.toContain(TOKEN);
    expect(text).not.toContain(env.REPOATLAS_API_CONNECTION_FILE!);
  });
});
