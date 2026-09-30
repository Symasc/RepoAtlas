import { explorerError, type ErrorEnvelope } from "@/lib/api/errors";
import { ENDPOINT_NAMES, type CapabilityError, type EndpointName } from "@/lib/api/generated";

import { ConnectionError, readConnection, type Environment } from "./connection";

/**
 * `/api/ra/<name>` → `POST <api>/v1/<name>`, with the bearer token added here.
 *
 * Adding the token is what makes this route a door: the API refuses callers
 * without it, and this route supplies it to whoever reaches the route. So it
 * answers only what a page served by this app on this machine could send —
 * a loopback Host header (a rebound DNS name arrives with the attacker's), no
 * cross-site request, a JSON body — and forwards only to the loopback address
 * the connection file names, only for endpoints the contract lists.
 */

/** The API's own body ceiling (docs/local-api.md). */
export const MAX_BODY = 1 << 20;
export const UPSTREAM_TIMEOUT_MS = 120_000;

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);
const ENDPOINTS: ReadonlySet<string> = new Set(ENDPOINT_NAMES);
const JSON_HEADERS = {
  "content-type": "application/json",
  "cache-control": "no-store",
} as const;

export interface ProxyOptions {
  env?: Environment;
  fetch?: typeof globalThis.fetch;
}

export function isEndpoint(name: string): name is EndpointName {
  return ENDPOINTS.has(name);
}

/**
 * The host name of a Host header, lowercased: `[::1]:7318` → `[::1]`. Null for
 * anything that is not exactly `host[:port]`, such as `evil@localhost` or
 * `localhost/x`, which a URL parser would otherwise read as `localhost`.
 */
export function hostName(header: string | null): string | null {
  if (!header) return null;
  const value = header.trim().toLowerCase();
  if (!URL.canParse(`http://${value}`)) return null;
  const url = new URL(`http://${value}`);
  // the parser drops a default :80, so compare without it
  return url.host === value.replace(/:80$/, "") ? url.hostname : null;
}

/** Why this request may not be forwarded, or null when it may. */
export function refusal(request: Request): CapabilityError | null {
  const host = request.headers.get("host");
  const name = hostName(host);
  if (name === null || !LOOPBACK_HOSTS.has(name)) return hostRefusal();
  const site = request.headers.get("sec-fetch-site");
  if (site !== null && site !== "same-origin" && site !== "none") return crossSite();
  const origin = request.headers.get("origin");
  if (origin !== null && !sameOrigin(origin, host)) return crossSite();
  const type = request.headers.get("content-type")?.split(";")[0]?.trim().toLowerCase();
  if (type !== "application/json") {
    return explorerError(
      "UNSUPPORTED_MEDIA_TYPE",
      "invalid_request",
      "The explorer's API route accepts only application/json.",
    );
  }
  const length = request.headers.get("content-length");
  if (length !== null && Number(length) > MAX_BODY) return tooLarge();
  return null;
}

export async function proxy(
  request: Request,
  name: string,
  options: ProxyOptions = {},
): Promise<Response> {
  const refused = refusal(request);
  if (refused !== null) return failure(statusOf(refused), refused);
  if (!isEndpoint(name)) {
    return failure(
      404,
      explorerError("UNKNOWN_ENDPOINT", "not_found", "The local API serves no endpoint by that name."),
    );
  }
  const body = await readBody(request);
  if (body === null) return failure(413, tooLarge());

  let connection;
  try {
    connection = await readConnection(options.env ?? process.env);
  } catch (error) {
    if (!(error instanceof ConnectionError)) throw error;
    const retryable = error.code === "API_NOT_RUNNING";
    return failure(503, explorerError(error.code, "context", error.message, error.detail, retryable));
  }

  let upstream: Response;
  try {
    upstream = await (options.fetch ?? globalThis.fetch)(`${connection.origin}/v1/${name}`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${connection.token}`,
        "content-type": "application/json",
      },
      body,
      redirect: "error",
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "TimeoutError") {
      return failure(
        504,
        explorerError(
          "API_TIMEOUT",
          "contention",
          "RepoAtlas's local API took too long to answer.",
          `No answer within ${UPSTREAM_TIMEOUT_MS / 1000} seconds.`,
          true,
        ),
      );
    }
    return failure(
      503,
      explorerError(
        "API_UNREACHABLE",
        "context",
        "RepoAtlas's local API is not answering.",
        "The connection file names a server that is no longer running. Start it again with `repoatlas serve`.",
        true,
      ),
    );
  }

  if (!upstream.headers.get("content-type")?.startsWith("application/json")) {
    return failure(
      502,
      explorerError("INVALID_RESPONSE", "internal", "The local API answered with something other than JSON."),
    );
  }
  return new Response(upstream.body, { status: upstream.status, headers: JSON_HEADERS });
}

/** The body, or null past MAX_BODY: read in chunks so an unbounded stream stops early. */
async function readBody(request: Request): Promise<Uint8Array<ArrayBuffer> | null> {
  if (request.body === null) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BODY) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body;
}

function sameOrigin(origin: string, host: string | null): boolean {
  if (host === null || !URL.canParse(origin)) return false;
  const url = new URL(origin);
  return url.protocol === "http:" && url.host === host.trim().toLowerCase();
}

function statusOf(error: CapabilityError): number {
  switch (error.code) {
    case "UNSUPPORTED_MEDIA_TYPE":
      return 415;
    case "BODY_TOO_LARGE":
      return 413;
    default:
      return 403;
  }
}

function hostRefusal(): CapabilityError {
  return explorerError(
    "HOST_NOT_ALLOWED",
    "invalid_request",
    "The explorer answers only requests addressed to localhost.",
    "A request for any other host name is how a DNS-rebinding page reaches a local server.",
  );
}

function crossSite(): CapabilityError {
  return explorerError(
    "CROSS_SITE_REQUEST",
    "invalid_request",
    "The explorer answers only its own pages.",
    "This request came from another site.",
  );
}

function tooLarge(): CapabilityError {
  return explorerError(
    "BODY_TOO_LARGE",
    "invalid_request",
    `A request body may be at most ${MAX_BODY / (1 << 20)} MiB.`,
  );
}

function failure(status: number, error: CapabilityError): Response {
  const envelope: ErrorEnvelope = { ok: false, error };
  return new Response(JSON.stringify(envelope), { status, headers: JSON_HEADERS });
}
