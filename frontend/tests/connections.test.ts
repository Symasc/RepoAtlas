import { describe, expect, it } from "vitest";

import type {
  ConnectionGroup,
  DeclarationView,
  HistoryEntry,
  HttpConnectionPreviewResponse,
} from "@/lib/api/generated";
import {
  createRequest,
  declarationDetail,
  EMPTY_MAP,
  filterFields,
  formProblems,
  groupTitle,
  historyLine,
  NO_FILTERS,
  pendingCount,
  previewLine,
  previewRequest,
  refusalText,
  summaryLine,
  unmappableReason,
} from "@/lib/model/connections";

const GROUP: ConnectionGroup = {
  key: "hcg1-0123456789abcdef0123",
  kind: "stated-origin",
  origin: "https://shop.example.test",
  loopback: false,
  requests: 4,
  declarable: 4,
  statuses: { "http-service-unmatched": 4 },
  languages: { python: 4 },
  callers: ["buyer", "batch"],
  reasons: { no_identity: 4 },
};
const LOOPBACK: ConnectionGroup = { ...GROUP, origin: "http://localhost:8000", loopback: true };

const PREVIEW: HttpConnectionPreviewResponse = {
  context: { scope: "global" },
  group: GROUP.key,
  origin: "https://shop.example.test",
  url: "https://shop.example.test/v1",
  repository: "shop",
  routes_served: 2,
  counts: { "would-link": 2, "no-matching-route": 2 },
  can_create: true,
  basis: "",
};

describe("what a group says about itself", () => {
  it("is titled by its origin, or by the base the source wrote", () => {
    expect(groupTitle(GROUP)).toBe("https://shop.example.test");
    expect(groupTitle({ ...GROUP, origin: null, kind: "base-not-stated", base_expression: "{base}" })).toBe(
      "{base} (base not stated)",
    );
  });

  it("is mappable only when the source left some of its requests to a declaration", () => {
    expect(unmappableReason(GROUP)).toBeNull();
    expect(unmappableReason({ ...GROUP, declarable: 0, kind: "base-not-stated", origin: null })).toMatch(
      /no origin to map/,
    );
    expect(unmappableReason({ ...GROUP, declarable: 0 })).toMatch(/never overrides/);
  });

  it("says why the join refused, in words, and keeps unknown codes out of prose", () => {
    expect(refusalText("no_identity")).toMatch(/No deployment or declaration/);
    expect(refusalText("something_new")).toBe("");
  });
});

describe("the mapping form", () => {
  it("needs a service, and a base path that starts with a slash", () => {
    expect(formProblems(GROUP, EMPTY_MAP)).toEqual(["Choose the service."]);
    expect(formProblems(GROUP, { ...EMPTY_MAP, repository: "shop", basePath: "v1" })).toEqual([
      "A base path starts with “/”.",
    ]);
    expect(formProblems(GROUP, { ...EMPTY_MAP, repository: "shop", basePath: "/v1?x=1" })).toContain(
      "A base path has no query, hole or space.",
    );
    expect(formProblems(GROUP, { ...EMPTY_MAP, repository: "shop", basePath: "/v1" })).toEqual([]);
  });

  it("makes a loopback origin name its callers and its environment", () => {
    const problems = formProblems(LOOPBACK, { ...EMPTY_MAP, repository: "shop" });
    expect(problems).toContain("Name the caller repositories this applies to.");
    expect(problems).toContain("Name the environment this loopback origin belongs to.");
    expect(
      formProblems(LOOPBACK, { ...EMPTY_MAP, repository: "shop", callers: ["buyer"], environment: "dev" }),
    ).toEqual([]);
  });

  it("asks for a preview only when the service and base path can be read", () => {
    expect(previewRequest(GROUP, EMPTY_MAP)).toBeNull();
    expect(previewRequest(GROUP, { ...EMPTY_MAP, repository: "shop", basePath: "bad path" })).toBeNull();
    expect(previewRequest(GROUP, { ...EMPTY_MAP, repository: "shop", basePath: "/v1", environment: " dev " })).toEqual({
      context: { scope: "global" },
      group: GROUP.key,
      repository: "shop",
      base_path: "/v1",
      callers: [],
      environment: "dev",
    });
  });

  it("sends exactly one write: an unverified link for the service, made from this group", () => {
    const request = createRequest(
      LOOPBACK,
      { repository: "shop", basePath: "", environment: "dev", callers: ["buyer"], note: " why " },
      { ...PREVIEW, url: "http://localhost:8000" },
    );
    expect(request.context).toEqual({ scope: "repository", repository: "shop" });
    expect(request.link).toMatchObject({
      url: "http://localhost:8000",
      environment: "dev",
      callers: ["buyer"],
      verification: "unverified",
      variables: {},
      conditions: [],
      target_id: null,
    });
    expect(request.provenance).toEqual({ via: "ui", note: "why", request_group: GROUP.key });
  });

  it("states a preview as the counts the sync would produce", () => {
    expect(previewLine(PREVIEW)).toBe("2 would link · 2 no matching route");
    expect(previewLine({ ...PREVIEW, counts: {} })).toBe("No request of this group is declarable.");
  });
});

describe("declarations", () => {
  const view = (state: DeclarationView["state"], extra: Partial<DeclarationView> = {}): DeclarationView => ({
    id: 1,
    repository: "shop",
    label: "mapped",
    verification: "unverified",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    state,
    ...extra,
  });

  it("counts the pending ones, which the next sync will apply", () => {
    expect(pendingCount([view("pending"), view("applied"), view("pending")])).toBe(2);
  });

  it("says what became of one: joined and declined, by reason", () => {
    expect(declarationDetail(view("applied", { joined: 2, declined: { "route-unmatched": 1 } }))).toBe(
      "2 requests joined · 1 request declined: route-unmatched",
    );
    expect(declarationDetail(view("pending"))).toBe("");
  });

  it("reads a history entry as an action on a declaration", () => {
    const entry: HistoryEntry = {
      id: 1,
      at: "2026-01-01T00:00:00Z",
      action: "revoke",
      subject_id: 7,
      repository: "shop",
      via: "ui",
      before: { url: "https://shop.example.test" },
    };
    expect(historyLine(entry)).toBe("revoked #7 (shop) https://shop.example.test");
  });
});

describe("the worklist", () => {
  it("summarises itself in one sentence, leaving out what is zero", () => {
    expect(
      summaryLine({ unresolved: 8, declarable: 6, base_not_stated: 1, linked_by_declaration: 0 }),
    ).toBe("8 unresolved requests · 6 can be mapped · 1 with no stated base");
    expect(
      summaryLine({ unresolved: 1, declarable: 0, base_not_stated: 0, linked_by_declaration: 2 }),
    ).toBe("1 unresolved request · 0 can be mapped · 2 joined by your declarations");
  });

  it("sends only the filters that are set", () => {
    expect(filterFields(NO_FILTERS)).toEqual({ include_resolved: false });
    expect(
      filterFields({ ...NO_FILTERS, status: "http-base-unknown", language: "python", text: " shop ", includeResolved: true }),
    ).toEqual({
      status: ["http-base-unknown"],
      language: "python",
      text: "shop",
      include_resolved: true,
    });
  });
});
