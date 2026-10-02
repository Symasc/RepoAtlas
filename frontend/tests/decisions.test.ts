import { describe, expect, it } from "vitest";

import type { HttpOperationsResponse } from "@/lib/api/generated";
import {
  byFile,
  crossingNamed,
  DECISION_MEANING,
  DECISIONS,
  decisionCounts,
  decisionRefOf,
  decisionRequest,
  HOP_KINDS,
  placeLabel,
  readRows,
  requestsRequest,
  ShapeError,
  SHOWN_STATUSES,
  stationsOf,
  type Crossing,
  type CrossingHop,
  type HopKind,
  type HopState,
} from "@/lib/model/decisions";

// The linked corpus's decisions, as M5b's Phase A froze them (m5b_expected.json).
const ROUTE_UNMATCHED = "hcd1-25680369ded384629e5e";
const SERVICE_UNMATCHED = "hcd1-1a63689c3b7bd6debe06";
const BASE_UNKNOWN = "hcd1-72d6b105266f94bea41e";
const LINKED = "hcd1-b208cc48f79ed1081fac";

function chain(states: HopState[], places: Partial<Record<number, string>> = {}, reasons: Partial<Record<number, string>> = {}): CrossingHop[] {
  return states.map((status, i) => {
    const at = places[i];
    const [repository, rest] = at ? [at.slice(0, at.indexOf("/")), at.slice(at.indexOf("/") + 1)] : ["", ""];
    const [path = "", line] = rest.split(":");
    return {
      kind: HOP_KINDS[i] as HopKind,
      status,
      ...(at ? { provenance: [{ repository, path, line_start: Number(line), line_end: Number(line), role: "at" }] } : {}),
      ...(reasons[i] ? { reason: reasons[i], note: `why ${reasons[i]}` } : {}),
    };
  });
}

const routeUnmatched: Crossing = {
  decision_ref: ROUTE_UNMATCHED,
  status: "http-route-unmatched",
  method: "GET",
  url: "https://http-pair.example/missing/7",
  chain: chain(
    ["proven", "proven", "proven", "proven", "proven", "unresolved", "not_reached"],
    { 0: "http-refused/client.py:5", 1: "http-refused/client.py:5", 2: "http-api/app.py:5", 3: "http-api/app.py:5", 4: "http-api/app.py:5" },
    { 5: "route_unmatched" },
  ),
};
const serviceUnmatched: Crossing = {
  decision_ref: SERVICE_UNMATCHED,
  status: "http-service-unmatched",
  method: "GET",
  url: "https://elsewhere.example/items/7",
  chain: chain(
    ["proven", "proven", "unresolved", "not_reached", "not_reached", "not_reached", "not_reached"],
    { 0: "http-refused/client.py:9", 1: "http-refused/client.py:9" },
    { 2: "no_identity" },
  ),
};
const baseUnknown: Crossing = {
  decision_ref: BASE_UNKNOWN,
  status: "http-base-unknown",
  method: "GET",
  url: "/items/{item_id}",
  chain: chain(
    ["proven", "unresolved", "not_reached", "not_reached", "not_reached", "not_reached", "not_reached"],
    { 0: "http-refused/client.py:13" },
    { 1: "host_not_stated" },
  ),
};
const linked: Crossing = {
  decision_ref: LINKED,
  status: "linked",
  method: "GET",
  url: "https://http-pair.example/items/{item_id}",
  chain: chain(Array(7).fill("proven") as HopState[], {
    0: "http-web/client.py:5",
    1: "http-web/client.py:5",
    2: "http-api/app.py:5",
    3: "http-api/app.py:5",
    4: "http-api/app.py:5",
    5: "http-api/app.py:8",
    6: "http-api/app.py:8",
  }),
};
// lib-a's request on the replica: the deployment binding fails, the source binding is still proven.
const bindingUnresolved: Crossing = {
  decision_ref: "hcd1-0000000000000000000a",
  status: "http-deployment-binding-unresolved",
  chain: chain(
    ["proven", "proven", "proven", "unresolved", "proven", "not_reached", "not_reached"],
    { 0: "lib-a/lib_a/web_w/billing.py:7", 3: "svc-c9/Tiltfile:3" },
    { 3: "binding_unresolved" },
  ),
};

function response(operations: unknown[]): Pick<HttpOperationsResponse, "operations"> {
  return { operations: operations as HttpOperationsResponse["operations"] };
}

const rows = [
  { role: "caller", status: "no_indexed_handler", method: "GET", route: "/missing/7", path: "client.py", line_start: 5, crossings: [routeUnmatched] },
  { role: "caller", status: "no_indexed_handler", method: "GET", route: "/items/7", path: "client.py", line_start: 9, crossings: [serviceUnmatched] },
];

// -- what each read asks ------------------------------------------------------------

describe("the reads a Requests view and an Evidence open send", () => {
  it("lists in repository scope, every caller status but join_only, showing other repositories and allocating no evidence", () => {
    const request = requestsRequest({ repository: "http-refused", path: "" });
    expect(request).toEqual({
      context: { scope: "repository", repository: "http-refused" },
      role: "caller",
      status: ["matched", "ambiguous", "no_indexed_handler", "unresolved"],
      cross_repository: true,
      include_provenance: false,
      limit: 100,
    });
    expect(request.status).not.toContain("join_only");
    expect(request).not.toHaveProperty("decision_ref");
  });

  it("reads the join-only rows only when asked, and a directory or file as within_path", () => {
    const request = requestsRequest({ repository: "svc-c2", path: "src/models" }, { hidden: true, cursor: "tc1.x", limit: 1 });
    expect(request.status).toEqual(["join_only"]);
    expect(request.within_path).toBe("src/models");
    expect(request.cursor).toBe("tc1.x");
    expect(request.limit).toBe(1);
    expect(request.include_provenance).toBe(false);
    expect(request.context).toEqual({ scope: "repository", repository: "svc-c2" });
  });

  it("escalates to global scope with provenance only for one decision, named by its exact ref", () => {
    expect(decisionRequest(LINKED, "inv-1")).toEqual({
      context: { scope: "global" },
      decision_ref: LINKED,
      include_provenance: true,
      investigation_id: "inv-1",
    });
    expect(decisionRequest(LINKED, null)).not.toHaveProperty("investigation_id");
  });

  it("finds a link's decision by the ref its own resolution names, and by nothing else", () => {
    expect(decisionRefOf({ resolution: { http: { decision_ref: LINKED, called_at: "http-web/client.py:5" } } })).toBe(LINKED);
    expect(decisionRefOf({ resolution: { http: { called_at: "http-web/client.py:5" } } })).toBeNull();
    expect(decisionRefOf({ resolution: { http: { decision_ref: "hcd1-short" } } })).toBeNull();
    expect(decisionRefOf({ resolution: null })).toBeNull();
  });

  it("takes from a lookup exactly the crossing with that ref", () => {
    const read = readRows(response(rows));
    expect(crossingNamed(read, SERVICE_UNMATCHED)?.crossing.decision_ref).toBe(SERVICE_UNMATCHED);
    expect(crossingNamed(read, SERVICE_UNMATCHED)?.row.line_start).toBe(9);
    expect(crossingNamed(read, LINKED)).toBeNull();
    expect(() => crossingNamed(readRows(response([rows[0], rows[0]])), ROUTE_UNMATCHED)).toThrow(ShapeError);
  });
});

// -- reading what the API wrote -----------------------------------------------------

describe("a row is read as the contract states it, or not at all", () => {
  it("reads rows, crossings and hops as written", () => {
    const [first] = readRows(response(rows));
    expect(first?.crossings?.[0]?.chain.map((h) => h.status)).toEqual(["proven", "proven", "proven", "proven", "proven", "unresolved", "not_reached"]);
    expect(readRows(response([{ role: "caller", status: "matched" }]))[0]?.crossings).toEqual([]);
  });

  it("refuses a hop that was not reached and still cites a place", () => {
    const bad = { ...serviceUnmatched, chain: serviceUnmatched.chain.map((h, i) => (i === 6 ? { ...h, provenance: [{ repository: "x", path: "y", role: "at" }] } : h)) };
    expect(() => readRows(response([{ ...rows[1], crossings: [bad] }]))).toThrow(/not reached and still cites a place/);
  });

  it("refuses hops out of the join's order, an unknown state or decision, and a missing ref", () => {
    const swapped = { ...linked, chain: [linked.chain[1], linked.chain[0], ...linked.chain.slice(2)] };
    expect(() => readRows(response([{ ...rows[0], crossings: [swapped] }]))).toThrow(ShapeError);
    const unknownState = { ...linked, chain: linked.chain.map((h, i) => (i === 2 ? { ...h, status: "failed" } : h)) };
    expect(() => readRows(response([{ ...rows[0], crossings: [unknownState] }]))).toThrow(ShapeError);
    expect(() => readRows(response([{ ...rows[0], crossings: [{ ...linked, status: "refused" }] }]))).toThrow(ShapeError);
    expect(() => readRows(response([{ ...rows[0], crossings: [{ ...linked, decision_ref: "" }] }]))).toThrow(ShapeError);
    expect(() => readRows(response([{ ...rows[0], crossings: [{ ...linked, chain: linked.chain.slice(0, 6) }] }]))).toThrow(ShapeError);
  });
});

// -- the header ---------------------------------------------------------------------

describe("the header counts every decision in scope, by its exact status", () => {
  it("adds the join-only rows' decisions to the listed rows', each once", () => {
    // http-refused: two listed refusals, one on its join-only row.
    expect(decisionCounts({ "http-route-unmatched": 1, "http-service-unmatched": 1 }, { "http-base-unknown": 1 })).toEqual([
      { status: "http-route-unmatched", shown: 1, hidden: 0 },
      { status: "http-service-unmatched", shown: 1, hidden: 0 },
      { status: "http-base-unknown", shown: 0, hidden: 1 },
    ]);
    // The replica's svc-c2: every decision on a join-only row, the links first.
    expect(decisionCounts({}, { "http-base-unknown": 4, "http-route-unmatched": 1, linked: 2 })).toEqual([
      { status: "linked", shown: 0, hidden: 2 },
      { status: "http-route-unmatched", shown: 0, hidden: 1 },
      { status: "http-base-unknown", shown: 0, hidden: 4 },
    ]);
    expect(decisionCounts(undefined, undefined)).toEqual([]);
  });

  it("never merges two refusals into one", () => {
    const every = Object.fromEntries(DECISIONS.map((d) => [d, 1]));
    const counted = decisionCounts(every, undefined);
    expect(counted.map((c) => c.status)).toEqual([...DECISIONS]);
    expect(new Set(DECISIONS.map((d) => DECISION_MEANING[d])).size).toBe(DECISIONS.length);
  });

  it("lists by default every caller status but join_only", () => {
    expect(SHOWN_STATUSES).not.toContain("join_only");
    expect([...SHOWN_STATUSES].sort()).toEqual(["ambiguous", "matched", "no_indexed_handler", "unresolved"]);
  });
});

// -- the stepper --------------------------------------------------------------------

describe("a chain reads as seven stations, emphasis stopping at the first hop not proven", () => {
  const tones = (c: Crossing) => stationsOf(c).map((s) => s.tone);

  it("emphasizes every hop of a link", () => {
    const stations = stationsOf(linked);
    expect(stations.map((s) => s.kind)).toEqual([...HOP_KINDS]);
    expect(stations.every((s) => s.state === "proven" && s.tone === "emphasis" && s.expandable)).toBe(true);
  });

  it("stops at the route a published service does not serve, the handler subdued and placeless", () => {
    const stations = stationsOf(routeUnmatched);
    expect(tones(routeUnmatched)).toEqual([...Array(6).fill("emphasis"), "subdued"]);
    expect(stations[5]).toMatchObject({ kind: "route", state: "unresolved", reason: "route_unmatched", note: "why route_unmatched" });
    expect(stations[6]).toMatchObject({ kind: "handler", state: "not_reached", places: [], expandable: false });
  });

  it("stops at the identity no one publishes and at the host the source does not state", () => {
    expect(tones(serviceUnmatched)).toEqual(["emphasis", "emphasis", "emphasis", "subdued", "subdued", "subdued", "subdued"]);
    expect(tones(baseUnknown)).toEqual(["emphasis", "emphasis", ...Array(5).fill("subdued")]);
    expect(stationsOf(baseUnknown)[1]).toMatchObject({ state: "unresolved", reason: "host_not_stated", places: [], expandable: false });
  });

  it("keeps a proven hop after the stop plain, not emphasized, and the hops never reached subdued", () => {
    expect(tones(bindingUnresolved)).toEqual(["emphasis", "emphasis", "emphasis", "emphasis", "plain", "subdued", "subdued"]);
    expect(stationsOf(bindingUnresolved)[3]).toMatchObject({ state: "unresolved", expandable: true });
  });

  it("stops at an ambiguous hop the same way, and does not open it out", () => {
    const ambiguous: Crossing = {
      ...routeUnmatched,
      status: "http-provider-ambiguous",
      chain: chain(["proven", "proven", "ambiguous", "not_reached", "not_reached", "not_reached", "not_reached"], { 2: "a/x.py:1" }),
    };
    expect(tones(ambiguous)).toEqual(["emphasis", "emphasis", "emphasis", ...Array(4).fill("subdued")]);
    expect(stationsOf(ambiguous)[2]).toMatchObject({ state: "ambiguous", expandable: false });
  });
});

describe("places and files", () => {
  it("names a place by repository, path and lines, or the whole file", () => {
    expect(placeLabel({ repository: "svc-c7", path: "src/app.ts", role: "source_in" })).toBe("svc-c7/src/app.ts");
    expect(placeLabel({ repository: "svc-c7", path: "Tiltfile", line_start: 3, line_end: 3, role: "bound_at" })).toBe("svc-c7/Tiltfile:3");
    expect(placeLabel({ repository: "a", path: "b.ts", line_start: 3, line_end: 5, role: "at" })).toBe("a/b.ts:3–5");
  });

  it("groups rows by file in the API's order", () => {
    const read = readRows(response([...rows, { ...rows[0], path: "other.py" }]));
    expect(byFile(read).map((g) => [g.path, g.rows.length])).toEqual([
      ["client.py", 2],
      ["other.py", 1],
    ]);
  });
});
