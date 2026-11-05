import type {
  ConnectionGroup,
  ConnectionRequest,
  DeclarationView,
  DeploymentLinkCreateRequest,
  ExternalServiceFields,
  HistoryEntry,
  HttpConnectionPreviewRequest,
  HttpConnectionPreviewResponse,
  PreviewRow,
  ServiceDeclarationCreateRequest,
  ServiceDeclarationPreviewRequest,
  ServiceDeclarationPreviewResponse,
  ServiceDeclarationUpdateRequest,
  ServiceDeclarationView,
  ServiceHistoryEntry,
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
  identity: string;
}

export const NO_FILTERS: ConnectionFilters = {
  status: "",
  language: "",
  caller: "",
  text: "",
  declarationState: "",
  includeResolved: false,
  identity: "",
};

/** The unresolved decisions a user can filter by, each in the join's own words. */
export const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "http-service-unmatched", label: "No service identified (http-service-unmatched)" },
  { value: "http-base-unknown", label: "Host not stated (http-base-unknown)" },
  { value: "http-route-unmatched", label: "No proven route matches (http-route-unmatched)" },
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
  "route-unmatched": "No route this index proves the service serves has the request's method and shape.",
  "callers-excluded": "The requests at this origin are made by repositories it does not name.",
  "source-established": "The source already identifies this origin; a declaration never overrides it.",
  "no-request-matched": "No request at this origin was joined by it.",
  "repository-not-indexed": "The repository it names is no longer indexed.",
  "repository-serves-no-routes": "The repository no longer serves any route this index holds.",
  "origin-no-longer-requested": "No request is made to this origin any more.",
  "target-url-unresolved": "Its target does not resolve to a URL.",
  "caller-not-indexed": "The repository whose code it names is no longer indexed.",
  "scope-too-broad": "The scope reaches code that is not proven to read this base, so nothing is joined.",
  "scope-unidentified": "The scope's file or symbol is not one entity of the index, so nothing is joined.",
  "injected-root-unselected": "The request is injected in several roots and none was chosen.",
  "scope-not-found": "The file or symbol it names is gone, or no longer names one entity.",
  "base-no-longer-requested": "No request in its scope is written on this base any more.",
};

/** The join's reason for a refusal, in a sentence; the code itself is always kept beside it. */
export const REFUSAL_TEXT: Readonly<Record<string, string>> = {
  no_identity: "No deployment or declaration identifies a service at this origin.",
  host_not_stated: "The source does not state the host this request goes to.",
  route_unmatched: "A service is published here, but no route this index proves it serves matches this request.",
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
  "scope-too-broad": "scope too broad: this code is not proven to read the base",
  "scope-unidentified": "scope not identified: not one file or symbol",
  "injected-root-unselected": "injected in several roots: none chosen",
  "out-of-scope": "outside the scope",
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
  // a classified origin is not mapped to an indexed service until the classification is revoked
  if (group.external_service) return classifiedReason(group.external_service);
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
  known_external?: number;
  unresolved_identity?: number;
}): string {
  const parts = [`${plural(s.unresolved, "unresolved request")}`, `${s.declarable} can be mapped`];
  if (s.base_not_stated > 0) parts.push(`${s.base_not_stated} with no stated base`);
  if (s.linked_by_declaration > 0) parts.push(`${s.linked_by_declaration} joined by your declarations`);
  // the identity split is stated once a classification shows some request as a known external dependency
  const known = s.known_external ?? 0;
  if (known > 0) {
    parts.push(`${known} known external`, `${s.unresolved_identity ?? 0} unresolved identity`);
  }
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
  const stated = (entry.after ?? entry.before) as {
    url?: string | null;
    label?: string;
    scope?: DeclarationScope | null;
  } | null;
  const where = stated?.url ? ` ${stated.url}` : stated?.scope ? ` ${scopeLine(stated.scope)}` : "";
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
    ...(filters.identity ? { identity: filters.identity as IdentityName } : {}),
    include_resolved: filters.includeResolved,
  };
}

// ---------------------------------------------------------------------------------------------
// Scoped declarations: a base the source does not state, named by a file (and symbol) of its caller.

/** A scope the server offers for an unstated base: a file of a caller, or one symbol of it. */
export type ScopeChoice = NonNullable<ConnectionGroup["scope_choices"]>[number];
/** The words of a scoped declaration, as stored: path, symbol, base by hash, base shown masked. */
export type DeclarationScope = NonNullable<DeclarationView["scope"]>;

/** Why a request's base cannot be named by a scope, in a sentence; the code is kept beside it. */
export const SCOPE_REFUSAL_TEXT: Readonly<Record<string, string>> = {
  "base-text-truncated": "The base expression is too long to be named exactly.",
  "base-text-unreadable": "The base expression cannot be read from the source.",
  "host-expression": "The source states a scheme but not the host.",
  "injected-transport": "The request goes through an injected transport; its roots are not selectable here.",
};

/** What the form holds about a scope. `choice` is empty until the operator picks one: nothing is
 *  preselected, neither the scope nor the service. */
export interface ScopeForm {
  choice: string;
  repository: string;
  basePath: string;
  environment: string;
  note: string;
}

export const EMPTY_SCOPE: ScopeForm = { choice: "", repository: "", basePath: "", environment: "", note: "" };

/** A choice's identity inside the picker: its caller, file and symbol. */
export function scopeKey(choice: ScopeChoice): string {
  return [choice.repository, choice.path, choice.symbol ?? ""].join("\u0000");
}

/** The choice a picker value names; none for the empty value or one the server did not offer. */
export function choiceOf(choices: readonly ScopeChoice[], key: string): ScopeChoice | null {
  if (!key) return null;
  return choices.find((choice) => scopeKey(choice) === key) ?? null;
}

/** A choice as the picker shows it. The too-broad count is always stated, even when it is zero. */
export function choiceLabel(choice: ScopeChoice): { name: string; counts: string } {
  const covered = choice.covered ?? 0;
  const broad = choice.too_broad ?? 0;
  return {
    name: `${choice.repository} · ${choice.path} · ${choice.symbol ? choice.symbol : "whole file"}`,
    counts: `covers ${plural(covered, "request")} · ${broad} too broad`,
  };
}

/** The base text's identity (a hash, never the text) of the requests a choice reaches. */
export type ScopeBase = { key: string } | { problem: string };

/** The one base key the group's requests have inside this choice, read from the requests listed. */
export function baseKeyFor(choice: ScopeChoice, requests: readonly ConnectionRequest[]): ScopeBase {
  const keys = new Set<string>();
  for (const request of requests) {
    if (!request.scopable || !request.base_key) continue;
    if (request.site.repository !== choice.repository || request.site.path !== choice.path) continue;
    const symbol = request.symbol ?? "";
    if (choice.symbol && symbol !== choice.symbol && !symbol.startsWith(`${choice.symbol}.`)) continue;
    keys.add(request.base_key);
  }
  if (keys.size === 1) return { key: [...keys][0]! };
  return {
    problem:
      keys.size === 0
        ? "No listed request in this scope can be named by its base."
        : "More than one base text is written in this scope; choose a narrower one.",
  };
}

/** A base path is empty or starts with a slash, with no query, hole or space. */
function basePathProblem(basePath: string): string | null {
  if (basePath && !basePath.startsWith("/")) return "A base path starts with “/”.";
  if (/[?#{}\s]/.test(basePath)) return "A base path has no query, hole or space.";
  return null;
}

/** What the form can tell before asking the server, which remains the authority. */
export function scopeProblems(choice: ScopeChoice | null, base: ScopeBase | null, form: ScopeForm): string[] {
  const problems: string[] = [];
  if (!choice) problems.push("Choose the scope.");
  else if (base && "problem" in base) problems.push(base.problem);
  if (!form.repository) problems.push("Choose the service.");
  const path = basePathProblem(form.basePath);
  if (path) problems.push(path);
  return problems;
}

/** Ask what a scoped declaration would do. Null until the scope, its base and the service are chosen.
 *  Only the caller's file, symbol and the base's hash are sent: never the base text. */
export function scopedPreviewRequest(
  group: ConnectionGroup,
  choice: ScopeChoice | null,
  base: ScopeBase | null,
  form: ScopeForm,
): HttpConnectionPreviewRequest | null {
  if (!choice || !base || "problem" in base || !form.repository || basePathProblem(form.basePath)) return null;
  return {
    context: { scope: "global" },
    group: group.key,
    repository: form.repository,
    environment: form.environment.trim() || null,
    scope: {
      caller: choice.repository,
      path: choice.path,
      symbol: choice.symbol ?? null,
      base_key: base.key,
      base_path: form.basePath,
    },
  };
}

/** The one write a scoped declaration is: an ordinary deployment link with a scope and no URL, for
 *  the one caller, stored exactly as the preview said it would be. Null when the preview did not. */
export function scopedCreateRequest(
  group: ConnectionGroup,
  choice: ScopeChoice,
  form: ScopeForm,
  preview: HttpConnectionPreviewResponse,
): DeploymentLinkCreateRequest | null {
  if (!preview.scope) return null;
  return {
    context: { scope: "repository", repository: form.repository },
    link: {
      label: `Scoped from HTTP connections: ${scopeLine(preview.scope)}`.slice(0, 300),
      url: null,
      environment: form.environment.trim() || null,
      callers: [choice.repository],
      target_id: null,
      variables: {},
      conditions: [],
      verification: "unverified",
      scope: preview.scope,
    },
    provenance: { via: "ui", note: form.note.trim() || null, request_group: group.key },
  };
}

/** How many requests a preview says would link. */
export function wouldLinkCount(preview: HttpConnectionPreviewResponse): number {
  const counted = preview.counts?.["would-link"] ?? 0;
  const listed = (preview.rows ?? []).filter((row) => isGood(row.outcome)).length;
  return Math.max(counted, listed);
}

/** Declare is possible only once the scoped preview has no requirement and at least one request would link. */
export function canDeclareScoped(
  preview: HttpConnectionPreviewResponse | undefined,
  problems: readonly string[],
  pending: boolean,
): boolean {
  if (!preview || pending || problems.length > 0) return false;
  if (!preview.can_create || (preview.requirements ?? []).length > 0 || !preview.scope) return false;
  return wouldLinkCount(preview) > 0;
}

const SHOWN_BASE = /^\{(?:[A-Za-z_$][\w$]*|…)\}$/;

/** A base as the interface shows it: a plain name in braces, or `{…}`. Anything else the server might
 *  send is masked again here, so source text of a base expression is never displayed. */
export function maskedBase(value: string | null | undefined): string {
  return value && SHOWN_BASE.test(value) ? value : "{…}";
}

/** The name of the control that opens a scoped declaration. It never reads "Map … to a service": that
 *  is the stated-origin mapping, and a base the source does not state has no origin to map. */
export function scopeButtonLabel(group: ConnectionGroup): string {
  return `Declare the base of ${maskedBase(group.base_expression)} for a scope`;
}

/** A scope in one line: the masked base and its route prefix, in the file (and symbol) it is written in. */
export function scopeLine(scope: DeclarationScope): string {
  return `${maskedBase(scope.base_shown)}${scope.base_path ?? ""} in ${scope.path}${scope.symbol ? `#${scope.symbol}` : ""}`;
}

/** A scope as separate statements, for a declaration's own listing. */
export function scopeSummary(scope: DeclarationScope): string[] {
  return [
    `file ${scope.path}`,
    scope.symbol ? `symbol ${scope.symbol}` : "whole file",
    `base ${maskedBase(scope.base_shown)}`,
    scope.base_path ? `base path ${scope.base_path}` : "no base path",
  ];
}

// ---------------------------------------------------------------------------------------------
// External services: an origin no indexed repository answers, classified by the operator. A
// classification is a statement read when the worklist is built; it never joins a request, so a
// request keeps its decision and chain and only its identity is shown differently.

/** Who answers a request, as the index and the operator's classifications say. */
export type IdentityName = NonNullable<ConnectionRequest["identity"]>;

export const IDENTITY_LABEL: Readonly<Record<IdentityName, string>> = {
  "known-external": "known external dependency",
  identified: "identified",
  unresolved: "unresolved service identity",
};

/** A request's identity in words. A response without the field is an unresolved identity. */
export function identityLabel(identity: IdentityName | null | undefined): string {
  return IDENTITY_LABEL[identity ?? "unresolved"];
}

/** The identities the worklist can be filtered by. */
export const IDENTITY_OPTIONS: { value: IdentityName; label: string }[] = [
  { value: "unresolved", label: IDENTITY_LABEL.unresolved },
  { value: "known-external", label: IDENTITY_LABEL["known-external"] },
  { value: "identified", label: IDENTITY_LABEL.identified },
];

/** An external service's state against the stored decisions (derived on every read, never stored). */
export type ExternalStateName = ServiceDeclarationView["state"];

export const EXTERNAL_STATE_LABEL: Readonly<Record<ExternalStateName, string>> = {
  applied: "Applied",
  ineffective: "Ineffective",
  invalidated: "Invalidated",
};

export const EXTERNAL_STATE_MEANING: Readonly<Record<ExternalStateName, string>> = {
  applied: "The unmatched requests at this origin are shown as a known external dependency.",
  ineffective: "It changes no request now; the reason says why.",
  invalidated: "What it states no longer holds in the index.",
};

/** Why an external service is ineffective or invalidated, in a sentence. */
export const EXTERNAL_REASON_TEXT: Readonly<Record<string, string>> = {
  "": "",
  "index-not-built": "No sync has stored the join's decisions yet.",
  "origin-no-longer-requested": "No request is made to this origin any more.",
  "source-identifies-origin": "The source now identifies a service at this origin; a classification never overrides it.",
  "origin-declared-as-deployment": "A deployment link now states this origin as a deployment.",
  "authenticated-service-not-indexed": "A repository it authenticates for is no longer indexed.",
};

/** The fields of an external service, or null for any other kind of declaration. */
export function externalOf(view: ServiceDeclarationView): ExternalServiceFields | null {
  const fields = view.fields;
  return isExternalFields(fields) ? fields : null;
}

function isExternalFields(fields: ServiceDeclarationView["fields"]): fields is ExternalServiceFields {
  return fields.kind === "external_service";
}

/** The external services in a response, newest first as the server lists them. */
export function externalServices(views: readonly ServiceDeclarationView[]): ServiceDeclarationView[] {
  return views.filter((view) => externalOf(view) !== null);
}

/** The group header's tag for a classified origin. Only an applied classification makes requests
 *  known external dependencies; one that is not applied says its state instead. */
export function externalTag(view: ServiceDeclarationView): string {
  const label = externalOf(view)?.label ?? "";
  return view.state === "applied"
    ? `known external dependency: ${label}`
    : `external service ${label} (${view.state})`;
}

/** What replaces the Map control on a classified origin. */
export function classifiedReason(view: Pick<ServiceDeclarationView, "id">): string {
  return `Classified as external service #${view.id}; revoke it to map this origin to an indexed service.`;
}

/** Whether a group can be classified: it states a non-loopback origin, some of its requests were
 *  left http-service-unmatched, and it is not classified already. The server remains the authority. */
export function canClassify(group: ConnectionGroup): boolean {
  return (
    group.kind === "stated-origin" &&
    !!group.origin &&
    !group.loopback &&
    !group.external_service &&
    (group.statuses["http-service-unmatched"] ?? 0) > 0
  );
}

/** What the response says an authentication relation rests on. The classification is the operator's
 *  statement, so it reads "operator-declared" unless the response itself says `source`. */
export function authenticatesBasis(basis: string | null | undefined): string {
  return basis === "source" ? "source-backed" : "operator-declared";
}

/** One authenticated target: its repository, and its environment or any. */
export function authTargetLine(target: { repository: string; environment?: string | null }): string {
  return `${target.repository} (${target.environment ? target.environment : "any environment"})`;
}

/** One row of the authentication target list: both texts, as typed. */
export interface AuthTargetForm {
  repository: string;
  environment: string;
}

export interface ClassifyForm {
  label: string;
  provider: string;
  category: string;
  note: string;
  authenticates: AuthTargetForm[];
}

export const EMPTY_CLASSIFY: ClassifyForm = { label: "", provider: "", category: "", note: "", authenticates: [] };

// the server's own limits (repoatlas/models/service_declarations.py), stated here to refuse early
const MAX_LABEL = 300;
const MAX_WORDS = 100;
const MAX_NOTE = 500;
const MAX_TARGETS = 20;
const ORIGIN_SHAPE = /^https?:\/\/[^\s/?#@*]+$/i;

/** The classify form filled from a stored external service, for editing it. */
export function classifyFormFrom(fields: ExternalServiceFields): ClassifyForm {
  return {
    label: fields.label,
    provider: fields.provider ?? "",
    category: fields.category ?? "",
    note: fields.note ?? "",
    authenticates: (fields.authenticates ?? []).map((t) => ({
      repository: t.repository,
      environment: t.environment ?? "",
    })),
  };
}

/** What the form can tell before asking the server, which remains the authority: a label, an
 *  origin that is scheme://host[:port], and a target list the server would accept. */
export function classifyProblems(origin: string | null | undefined, form: ClassifyForm): string[] {
  const problems: string[] = [];
  if (!origin) problems.push("This group states no origin to classify.");
  else if (!ORIGIN_SHAPE.test(origin)) {
    problems.push("The origin is scheme://host[:port], with no path, query, credential or wildcard.");
  }
  const label = form.label.trim();
  if (!label) problems.push("Give the service a label.");
  else if (label.length > MAX_LABEL) problems.push(`A label is at most ${MAX_LABEL} characters.`);
  if (form.provider.trim().length > MAX_WORDS) problems.push(`A provider is at most ${MAX_WORDS} characters.`);
  if (form.category.trim().length > MAX_WORDS) problems.push(`A category is at most ${MAX_WORDS} characters.`);
  if (form.note.trim().length > MAX_NOTE) problems.push(`A note is at most ${MAX_NOTE} characters.`);
  if (form.authenticates.length > MAX_TARGETS) {
    problems.push(`At most ${MAX_TARGETS} authentication targets.`);
  }
  if (form.authenticates.some((t) => !t.repository)) {
    problems.push("Choose a repository for each authentication target, or remove the row.");
  }
  const named = form.authenticates.map((t) => `${t.repository}\u0000${t.environment.trim()}`);
  if (new Set(named).size !== named.length) {
    problems.push("A repository and environment is named once.");
  }
  return problems;
}

/** The fields a classification states, as the server stores them. */
export function classifyFields(origin: string, form: ClassifyForm): ExternalServiceFields {
  return {
    kind: "external_service",
    origin,
    label: form.label.trim(),
    provider: form.provider.trim() || null,
    category: form.category.trim() || null,
    note: form.note.trim() || null,
    authenticates: form.authenticates.map((t) => ({
      repository: t.repository,
      environment: t.environment.trim() || null,
    })),
  };
}

/** Ask what a classification would do. Null until the form has no problem the page can see. The
 *  worklist group it is made from goes in `group`; an edit names the declaration in `id`. */
export function classifyPreviewRequest(
  origin: string | null | undefined,
  form: ClassifyForm,
  options: { group?: string | null; id?: number | null } = {},
): ServiceDeclarationPreviewRequest | null {
  if (!origin || classifyProblems(origin, form).length > 0) return null;
  return {
    context: { scope: "global" },
    fields: classifyFields(origin, form),
    ...(options.group ? { group: options.group } : {}),
    ...(options.id != null ? { id: options.id } : {}),
  };
}

/** The one write a classification is. The group it was made from is its provenance. */
export function classifyCreateRequest(
  origin: string,
  form: ClassifyForm,
  group: string | null,
): ServiceDeclarationCreateRequest {
  return {
    context: { scope: "global" },
    fields: classifyFields(origin, form),
    provenance: { via: "ui", note: null, request_group: group },
  };
}

/** An edit: the same origin (what a classification is about never changes), the other words new. */
export function classifyUpdateRequest(
  id: number,
  origin: string,
  form: ClassifyForm,
): ServiceDeclarationUpdateRequest {
  return {
    context: { scope: "global" },
    id,
    fields: classifyFields(origin, form),
    provenance: { via: "ui", note: null },
  };
}

/** A classification's preview as a sentence, in the words the interface commits to. */
export function servicePreviewLine(preview: ServiceDeclarationPreviewResponse): string {
  const known = preview.counts?.would_be_known_external ?? 0;
  const stay = preview.counts?.stay_identified ?? 0;
  const line = `${plural(known, "request")} would be shown as a known external dependency; their decision stays http-service-unmatched; nothing is joined.`;
  return stay > 0 ? `${line} ${plural(stay, "request")} the source already identifies stay identified.` : line;
}

/** One entry of the external services' audit trail as a sentence. */
export function serviceHistoryLine(entry: ServiceHistoryEntry): string {
  return `${ACTION_TEXT[entry.action]} external service #${entry.subject_id} ${entry.subject}`;
}
