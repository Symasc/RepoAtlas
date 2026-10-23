import type {
  ConnectionGroup,
  DeclarationView,
  DeploymentLinkCreateRequest,
  HistoryEntry,
  HttpConnectionPreviewRequest,
  HttpConnectionPreviewResponse,
  PreviewRow,
} from "@/lib/api/generated";

import { plural } from "./format";

/** What a worklist's filters ask for. Empty means no filter. */
export interface ConnectionFilters {
  status: string;
  language: string;
  caller: string;
  text: string;
  declarationState: string;
  includeResolved: boolean;
}

export const NO_FILTERS: ConnectionFilters = {
  status: "",
  language: "",
  caller: "",
  text: "",
  declarationState: "",
  includeResolved: false,
};

/** The unresolved decisions a user can filter by, each in the join's own words. */
export const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "http-service-unmatched", label: "No service identified (http-service-unmatched)" },
  { value: "http-base-unknown", label: "Host not stated (http-base-unknown)" },
  { value: "http-route-unmatched", label: "Route not served (http-route-unmatched)" },
  { value: "http-provider-ambiguous", label: "Several providers fit (http-provider-ambiguous)" },
  { value: "http-deployment-source-unresolved", label: "Service known, code not (http-deployment-source-unresolved)" },
  { value: "http-deployment-binding-unresolved", label: "Host pattern unbound (http-deployment-binding-unresolved)" },
  { value: "http-claim-unread", label: "Request not read (http-claim-unread)" },
];

export const LANGUAGE_OPTIONS = [
  { value: "python", label: "Python" },
  { value: "typescript", label: "TypeScript / JavaScript" },
];

export type DeclarationStateName = DeclarationView["state"];

export const STATE_LABEL: Readonly<Record<DeclarationStateName, string>> = {
  pending: "Pending",
  applied: "Applied",
  ineffective: "Ineffective",
  invalidated: "Invalidated",
};

export const STATE_MEANING: Readonly<Record<DeclarationStateName, string>> = {
  pending: "Stated after the last sync: it takes effect at the next sync.",
  applied: "At least one request is joined through it.",
  ineffective: "The last sync considered it and joined nothing.",
  invalidated: "What it states no longer holds in the index.",
};

/** Why a declaration is ineffective or invalidated, in a sentence. */
export const REASON_TEXT: Readonly<Record<string, string>> = {
  "": "",
  "applies-at-next-sync": "Applies at the next sync.",
  ambiguous: "More than one handler of the service answers the request, so nothing is joined.",
  "route-unmatched": "The service serves no route with the request's method and shape.",
  "callers-excluded": "The requests at this origin are made by repositories it does not name.",
  "source-established": "The source already identifies this origin; a declaration never overrides it.",
  "no-request-matched": "No request at this origin was joined by it.",
  "repository-not-indexed": "The repository it names is no longer indexed.",
  "repository-serves-no-routes": "The repository no longer serves any route this index holds.",
  "origin-no-longer-requested": "No request is made to this origin any more.",
  "target-url-unresolved": "Its target does not resolve to a URL.",
};

/** The join's reason for a refusal, in a sentence; the code itself is always kept beside it. */
export const REFUSAL_TEXT: Readonly<Record<string, string>> = {
  no_identity: "No deployment or declaration identifies a service at this origin.",
  host_not_stated: "The source does not state the host this request goes to.",
  route_unmatched: "A service is published here, but it does not serve this route.",
  claim_unread: "The request could not be read.",
  source_unresolved: "The deployment's service is known, but the code behind it is not.",
  binding_unresolved: "The host fits an Ingress pattern no local deployment binds.",
  artifact_ambiguous: "Two builds could be the code this deployment runs.",
};

export function refusalText(code: string): string {
  return REFUSAL_TEXT[code] ?? "";
}

export const OUTCOME_TEXT: Readonly<Record<PreviewRow["outcome"], string>> = {
  "would-link": "would link",
  ambiguous: "ambiguous: more than one handler",
  "no-matching-route": "no matching route",
  "caller-excluded": "caller not named",
  "source-established": "the source decides this one",
  "not-declarable": "not declarable",
};

/** Whether an outcome is one the operator would be glad of. */
export function isGood(outcome: PreviewRow["outcome"]): boolean {
  return outcome === "would-link";
}

export function groupTitle(group: ConnectionGroup): string {
  if (group.origin) return group.origin;
  return group.base_expression ? `${group.base_expression} (base not stated)` : "Relative or unread";
}

/** Why a group cannot be mapped, or null when it can. */
export function unmappableReason(group: ConnectionGroup): string | null {
  if (group.declarable > 0) return null;
  if (group.kind === "base-not-stated") return "The source does not state this base, so there is no origin to map.";
  return "The source already identified this origin; a declaration never overrides what it establishes.";
}

export interface MapForm {
  repository: string;
  basePath: string;
  environment: string;
  callers: string[];
  note: string;
}

export const EMPTY_MAP: MapForm = { repository: "", basePath: "", environment: "", callers: [], note: "" };

/** What the dialog can tell before asking the server: a loopback origin needs its callers and an
 *  environment, a base path starts with a slash. The server remains the authority. */
export function formProblems(group: ConnectionGroup, form: MapForm): string[] {
  const problems: string[] = [];
  if (!form.repository) problems.push("Choose the service.");
  if (form.basePath && !form.basePath.startsWith("/")) problems.push("A base path starts with “/”.");
  if (/[?#{}\s]/.test(form.basePath)) problems.push("A base path has no query, hole or space.");
  if (group.loopback) {
    if (form.callers.length === 0) problems.push("Name the caller repositories this applies to.");
    if (!form.environment.trim()) problems.push("Name the environment this loopback origin belongs to.");
  }
  return problems;
}

export function previewRequest(group: ConnectionGroup, form: MapForm): HttpConnectionPreviewRequest | null {
  if (!form.repository || /[?#{}\s]/.test(form.basePath) || (form.basePath && !form.basePath.startsWith("/"))) {
    return null;
  }
  return {
    context: { scope: "global" },
    group: group.key,
    repository: form.repository,
    base_path: form.basePath,
    callers: form.callers as HttpConnectionPreviewRequest["callers"],
    environment: form.environment.trim() || null,
  };
}

/** The one write a mapping is: an ordinary deployment link for the service, made from this group. */
export function createRequest(
  group: ConnectionGroup,
  form: MapForm,
  preview: HttpConnectionPreviewResponse,
): DeploymentLinkCreateRequest {
  return {
    context: { scope: "repository", repository: form.repository },
    link: {
      label: `Mapped from HTTP connections: ${group.origin ?? ""}`.slice(0, 300),
      url: preview.url,
      environment: form.environment.trim() || null,
      callers: form.callers,
      target_id: null,
      variables: {},
      conditions: [],
      verification: "unverified",
    },
    provenance: { via: "ui", note: form.note.trim() || null, request_group: group.key },
  };
}

export function previewLine(preview: HttpConnectionPreviewResponse): string {
  const counts = preview.counts ?? {};
  const parts = Object.entries(counts)
        .filter(([, n]) => (n ?? 0) > 0)
    .map(([outcome, n]) => `${n} ${OUTCOME_TEXT[outcome as PreviewRow["outcome"]] ?? outcome}`);
  return parts.length === 0 ? "No request of this group is declarable." : parts.join(" · ");
}

export function declarationDetail(d: DeclarationView): string {
  const bits: string[] = [];
  const joined = d.joined ?? 0;
  if (joined > 0) bits.push(`${plural(joined, "request")} joined`);
  for (const [reason, n] of Object.entries(d.declined ?? {})) bits.push(`${plural(n ?? 0, "request")} declined: ${reason}`);
  return bits.join(" · ");
}

export function pendingCount(declarations: DeclarationView[]): number {
  return declarations.filter((d) => d.state === "pending").length;
}

/** The worklist's headline figures as one sentence. */
export function summaryLine(s: {
  unresolved: number;
  declarable: number;
  base_not_stated: number;
  linked_by_declaration: number;
}): string {
  const parts = [`${plural(s.unresolved, "unresolved request")}`, `${s.declarable} can be mapped`];
  if (s.base_not_stated > 0) parts.push(`${s.base_not_stated} with no stated base`);
  if (s.linked_by_declaration > 0) parts.push(`${s.linked_by_declaration} joined by your declarations`);
  return parts.join(" · ");
}

export const ACTION_TEXT: Readonly<Record<HistoryEntry["action"], string>> = {
  create: "declared",
  update: "changed",
  revoke: "revoked",
  import: "imported",
};

/** One history entry as a sentence: what was done, to what, and any reason given. */
export function historyLine(entry: HistoryEntry): string {
  const stated = (entry.after ?? entry.before) as { url?: string | null; label?: string } | null;
  const where = stated?.url ? ` ${stated.url}` : "";
  return `${ACTION_TEXT[entry.action]} #${entry.subject_id} (${entry.repository})${where}`;
}

/** A filter set as the request's fields, leaving out what is empty. */
export function filterFields(filters: ConnectionFilters) {
  return {
    ...(filters.status ? { status: [filters.status as never] } : {}),
    ...(filters.language ? { language: filters.language } : {}),
    ...(filters.caller ? { caller: filters.caller } : {}),
    ...(filters.text.trim() ? { text: filters.text.trim() } : {}),
    ...(filters.declarationState ? { declaration_state: filters.declarationState as never } : {}),
    include_resolved: filters.includeResolved,
  };
}
