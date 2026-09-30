import { explorerError, UNAVAILABLE } from "./errors";
import type { CapabilityError, EndpointName, Endpoints } from "./generated";

/** A refusal from the local API or from the explorer's server, in one shape. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly error: CapabilityError,
  ) {
    super(error.message);
    this.name = "ApiError";
  }

  get code(): string {
    return this.error.code;
  }

  get retryable(): boolean {
    return this.error.retryable === true;
  }

  /** The local API is not there to ask, as opposed to having refused this request. */
  get unavailable(): boolean {
    return UNAVAILABLE.has(this.error.code);
  }
}

/**
 * Calls one endpoint through the explorer's own `/api/ra` route. The browser
 * never holds the API's address or token; it only ever talks to its own origin.
 */
export async function call<N extends EndpointName>(
  name: N,
  request: Endpoints[N]["request"],
  signal?: AbortSignal,
): Promise<Endpoints[N]["response"]> {
  const response = await fetch(`/api/ra/${name}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(request),
    signal,
  });
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new ApiError(
      response.status,
      explorerError(
        "INVALID_RESPONSE",
        "internal",
        `The explorer's server answered ${response.status} without a JSON body.`,
      ),
    );
  }
  if (isEnvelope(payload) && payload.ok === true && response.ok) {
    return payload.result as Endpoints[N]["response"];
  }
  if (isEnvelope(payload) && payload.ok === false && isError(payload.error)) {
    throw new ApiError(response.status, payload.error);
  }
  throw new ApiError(
    response.status,
    explorerError("INVALID_RESPONSE", "internal", "The answer was not a RepoAtlas envelope."),
  );
}

type Envelope = { ok: true; result: unknown } | { ok: false; error: unknown };

function isEnvelope(value: unknown): value is Envelope {
  return typeof value === "object" && value !== null && typeof (value as { ok?: unknown }).ok === "boolean";
}

function isError(value: unknown): value is CapabilityError {
  if (typeof value !== "object" || value === null) return false;
  const { code, category, message } = value as Record<string, unknown>;
  return typeof code === "string" && typeof category === "string" && typeof message === "string";
}
