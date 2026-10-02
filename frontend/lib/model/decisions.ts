import type {
  CapabilityContext,
  CrossingStatus,
  HttpOperationsRequest,
  HttpOperationsResponse,
  OperationStatus,
  TopologyEdgeResponse,
} from "@/lib/api/generated";

/**
 * The workspace join's stored decisions, as the Requests views and a link's
 * Evidence show them. Pure, so the rules that matter are tested without a
 * browser: which rows are listed by default, what the header counts, how a
 * chain reads, and which request each read sends.
 *
 * Nothing is decided here. Statuses, reasons and places are the API's, as
 * given; this module orders them, counts them and says how much emphasis each
 * gets. A refusal is never turned into a graph link: nothing here feeds the
 * graph model.
 */

// -- the shapes the API writes ------------------------------------------------------
//
// The generated `HttpOperation` is untyped (its serializer writes only the fields
// a row has), so the row, crossing, hop and place shapes are declared here from
// the Python models and checked at runtime by `readRows`.

/** The hops of a crossing's chain, in the order the join walks them. */
export const HOP_KINDS = [
  "caller",
  "origin",
  "service_identity",
  "deployment_binding",
  "source_binding",
  "route",
  "handler",
] as const;
export type HopKind = (typeof HOP_KINDS)[number];

export const HOP_LABELS: Readonly<Record<HopKind, string>> = {
  caller: "Caller",
  origin: "Origin",
  service_identity: "Service identity",
  deployment_binding: "Deployment binding",
  source_binding: "Source binding",
  route: "Route",
  handler: "Handler",
};

export const HOP_STATES = ["proven", "unresolved", "ambiguous", "not_reached"] as const;
export type HopState = (typeof HOP_STATES)[number];

/** Every decision the join stores, in the contract's order: the link first. */
export const DECISIONS = [
  "linked",
  "http-provider-ambiguous",
  "http-deployment-source-unresolved",
  "http-deployment-binding-unresolved",
  "http-route-unmatched",
  "http-service-unmatched",
  "http-base-unknown",
  "http-claim-unread",
] as const satisfies readonly CrossingStatus[];

/**
 * What each decision means, from the contract. Each refusal keeps its own words:
 * they are different facts, never one "failed".
 */
export const DECISION_MEANING: Readonly<Record<CrossingStatus, string>> = {
  linked: "Every hop proven, to exactly one handler.",
  "http-provider-ambiguous": "Two handlers, two identities, or a rival identity or pattern fit the origin.",
  "http-deployment-source-unresolved": "A deployment resolves the origin's identity, and the identity reaches no code.",
  "http-deployment-binding-unresolved": "The origin fits an Ingress host no locally established deployment resolves.",
  "http-route-unmatched": "The service is published; the operation is not.",
  "http-service-unmatched": "No identity, pattern or published service has the origin.",
  "http-base-unknown": "The path is read, the host is not: counted and listed, never joined.",
  "http-claim-unread": "The request is not read.",
};

export interface HopPlace {
  repository: string;
  path: string;
  line_start?: number | null;
  line_end?: number | null;
  role: string;
  evidence_id?: string | null;
}

export interface CrossingHop {
  kind: HopKind;
  status: HopState;
  provenance?: HopPlace[];
  reason?: string;
  note?: string;
  detail?: string;
  handler?: string | null;
  withheld?: boolean;
}

export interface Crossing {
  decision_ref: string;
  status: CrossingStatus;
  method?: string;
  url?: string;
  variant?: string;
  chain: CrossingHop[];
  handler?: string | null;
  provider?: string | null;
  endpoint?: string | null;
}

export interface RequestRow {
  role: string;
  status: OperationStatus;
  method?: string | null;
  route?: string;
  path?: string | null;
  line_start?: number | null;
  line_end?: number | null;
  entity?: string | null;
  detail?: string;
  crossings?: Crossing[];
}

/** A row, crossing or hop the explorer cannot read as the contract states it. */
export class ShapeError extends Error {
  constructor(message: string) {
    super(`The API answered a request the explorer cannot read: ${message}.`);
    this.name = "ShapeError";
  }
}

export const DECISION_REF = /^hcd1-[0-9a-f]{20}$/;

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readPlace(value: unknown): HopPlace {
  if (!isObject(value) || typeof value.repository !== "string" || typeof value.path !== "string") {
    throw new ShapeError("a hop's place has no repository or path");
  }
  return value as unknown as HopPlace;
}

function readHop(value: unknown, at: number): CrossingHop {
  if (!isObject(value)) throw new ShapeError("a hop is not an object");
  if (value.kind !== HOP_KINDS[at]) throw new ShapeError(`hop ${at + 1} is ${String(value.kind)}, not ${HOP_KINDS[at]}`);
  if (!(HOP_STATES as readonly unknown[]).includes(value.status)) {
    throw new ShapeError(`the ${HOP_KINDS[at]} hop's state ${String(value.status)} is not one the join writes`);
  }
  const places = value.provenance === undefined ? [] : value.provenance;
  if (!Array.isArray(places)) throw new ShapeError("a hop's provenance is not a list");
  if (value.status === "not_reached" && places.length > 0) {
    throw new ShapeError(`the ${HOP_KINDS[at]} hop was not reached and still cites a place`);
  }
  return { ...(value as unknown as CrossingHop), provenance: places.map(readPlace) };
}

function readCrossing(value: unknown): Crossing {
  if (!isObject(value) || typeof value.decision_ref !== "string" || !DECISION_REF.test(value.decision_ref)) {
    throw new ShapeError("a crossing has no decision_ref");
  }
  if (!(DECISIONS as readonly unknown[]).includes(value.status)) {
    throw new ShapeError(`decision ${String(value.status)} is not one the join stores`);
  }
  const chain = value.chain;
  if (!Array.isArray(chain) || chain.length !== HOP_KINDS.length) {
    throw new ShapeError(`crossing ${value.decision_ref} has no seven-hop chain`);
  }
  return { ...(value as unknown as Crossing), chain: chain.map(readHop) };
}

/** The response's rows, each checked against the contract; one that is not is an error, never guessed at. */
export function readRows(response: Pick<HttpOperationsResponse, "operations">): RequestRow[] {
  return (response.operations ?? []).map((value) => {
    if (!isObject(value) || typeof value.role !== "string" || typeof value.status !== "string") {
      throw new ShapeError("a row has no role or status");
    }
    const crossings = value.crossings === undefined ? [] : value.crossings;
    if (!Array.isArray(crossings)) throw new ShapeError("a row's crossings are not a list");
    return { ...(value as unknown as RequestRow), crossings: crossings.map(readCrossing) };
  });
}

// -- what each read asks ------------------------------------------------------------

/** The caller statuses a Requests view lists by default: every one but `join_only`. */
export const SHOWN_STATUSES = [
  "matched",
  "ambiguous",
  "no_indexed_handler",
  "unresolved",
] as const satisfies readonly OperationStatus[];

/** Requests only the join reads: queryable, out of the default presentation. */
export const HIDDEN_STATUS = "join_only" satisfies OperationStatus;

export const REQUESTS_PAGE = 100;

/** Where a Requests view is: a repository, and a directory or file in it ("" for all of it). */
export interface RequestsScope {
  repository: string;
  path: string;
}

/**
 * One page of a Requests view's rows. Repository scope, with another repository's
 * part of each chain shown and no evidence allocated: a normal read never
 * escalates. `hidden` reads the join-only rows instead.
 */
export function requestsRequest(
  scope: RequestsScope,
  options: { hidden?: boolean; cursor?: string | null; limit?: number } = {},
): HttpOperationsRequest {
  const context: CapabilityContext = { scope: "repository", repository: scope.repository };
  return {
    context,
    role: "caller",
    status: options.hidden ? [HIDDEN_STATUS] : [...SHOWN_STATUSES],
    cross_repository: true,
    include_provenance: false,
    ...(scope.path ? { within_path: scope.path } : {}),
    limit: options.limit ?? REQUESTS_PAGE,
    ...(options.cursor ? { cursor: options.cursor } : {}),
  };
}

/**
 * One stored decision, exactly, by its ref: the escalation an explicit Evidence
 * open makes. Global scope, so every repository's part of the chain is shown and
 * its lines are supplied as evidence.
 */
export function decisionRequest(ref: string, investigation: string | null): HttpOperationsRequest {
  return {
    context: { scope: "global" },
    decision_ref: ref,
    include_provenance: true,
    ...(investigation ? { investigation_id: investigation } : {}),
  };
}

/** The decision a handler link stands for, as the link's own resolution names it; `null` when none does. */
export function decisionRefOf(edge: Pick<TopologyEdgeResponse, "resolution">): string | null {
  const http = edge.resolution?.http;
  if (!isObject(http)) return null;
  const ref = http.decision_ref;
  return typeof ref === "string" && DECISION_REF.test(ref) ? ref : null;
}

/** The one crossing a lookup by `ref` returned, or `null` when the index no longer stores it. */
export function crossingNamed(rows: readonly RequestRow[], ref: string): { row: RequestRow; crossing: Crossing } | null {
  const found = rows.flatMap((row) => (row.crossings ?? []).filter((c) => c.decision_ref === ref).map((crossing) => ({ row, crossing })));
  if (found.length > 1) throw new ShapeError(`decision ${ref} came back more than once`);
  return found[0] ?? null;
}

// -- what the header counts ---------------------------------------------------------

export interface DecisionCount {
  status: CrossingStatus;
  /** On the rows listed by default. */
  shown: number;
  /** On the join-only rows, which are not listed until asked for. */
  hidden: number;
}

/**
 * Every decision in scope, by exact status: the listed rows' and the join-only
 * rows' together, each counted once, so hiding a row never hides its decision.
 */
export function decisionCounts(
  shown: Partial<Record<string, number>> | undefined,
  hidden: Partial<Record<string, number>> | undefined,
): DecisionCount[] {
  return DECISIONS.map((status) => ({ status, shown: shown?.[status] ?? 0, hidden: hidden?.[status] ?? 0 })).filter(
    (c) => c.shown + c.hidden > 0,
  );
}

// -- how a chain reads --------------------------------------------------------------

/**
 * How much a station is drawn: in full up to and including the first hop that is
 * not proven, plainly after it, and subdued where the join never got.
 */
export type Tone = "emphasis" | "plain" | "subdued";

export interface Station {
  kind: HopKind;
  label: string;
  state: HopState;
  tone: Tone;
  places: HopPlace[];
  reason: string;
  note: string;
  detail: string;
  withheld: boolean;
  handler: string | null;
  /** Whether its places can be opened out: a proven or unresolved hop that has any. */
  expandable: boolean;
}

/** The seven stations of a crossing, in the join's order, each in its exact state. */
export function stationsOf(crossing: Crossing): Station[] {
  const stop = crossing.chain.findIndex((hop) => hop.status === "unresolved" || hop.status === "ambiguous");
  return crossing.chain.map((hop, i) => {
    const places = hop.provenance ?? [];
    const tone: Tone = stop === -1 || i <= stop ? "emphasis" : hop.status === "not_reached" ? "subdued" : "plain";
    return {
      kind: hop.kind,
      label: HOP_LABELS[hop.kind],
      state: hop.status,
      tone,
      places,
      reason: hop.reason ?? "",
      note: hop.note ?? "",
      detail: hop.detail ?? "",
      withheld: hop.withheld === true,
      handler: hop.handler ?? null,
      expandable: (hop.status === "proven" || hop.status === "unresolved") && places.length > 0,
    };
  });
}

/** `repository/path:line` (or `–end`), or the file alone when the hop is the whole file. */
export function placeLabel(place: HopPlace): string {
  const at = `${place.repository}/${place.path}`;
  if (place.line_start == null) return at;
  const end = place.line_end != null && place.line_end !== place.line_start ? `–${place.line_end}` : "";
  return `${at}:${place.line_start}${end}`;
}

/** The rows of a Requests view grouped by file, in the API's order. */
export function byFile(rows: readonly RequestRow[]): { path: string; rows: RequestRow[] }[] {
  const groups = new Map<string, RequestRow[]>();
  for (const row of rows) {
    const path = row.path ?? "";
    groups.set(path, [...(groups.get(path) ?? []), row]);
  }
  return [...groups].map(([path, rows]) => ({ path, rows }));
}
