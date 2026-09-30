import type { CapabilityError, ErrorCategory } from "./generated";

/**
 * Refusals the explorer's own server makes, before or instead of reaching the
 * local API. They travel in the API's error shape, so the UI reads one kind of
 * error whichever side refused.
 */
export type ExplorerErrorCode =
  | "API_NOT_CONFIGURED"
  | "API_NOT_RUNNING"
  | "API_CONNECTION_INVALID"
  | "API_UNREACHABLE"
  | "API_TIMEOUT"
  | "HOST_NOT_ALLOWED"
  | "CROSS_SITE_REQUEST"
  | "UNSUPPORTED_MEDIA_TYPE"
  | "BODY_TOO_LARGE"
  | "UNKNOWN_ENDPOINT"
  | "INVALID_RESPONSE";

export interface ErrorEnvelope {
  ok: false;
  error: CapabilityError;
}

export function explorerError(
  code: ExplorerErrorCode,
  category: ErrorCategory,
  message: string,
  detail: string | null = null,
  retryable = false,
): CapabilityError {
  return { code, category, message, detail, retryable };
}

/** The local API is not there to ask: not configured, not started, or gone. */
export const UNAVAILABLE: ReadonlySet<string> = new Set<ExplorerErrorCode>([
  "API_NOT_CONFIGURED",
  "API_NOT_RUNNING",
  "API_CONNECTION_INVALID",
  "API_UNREACHABLE",
]);
