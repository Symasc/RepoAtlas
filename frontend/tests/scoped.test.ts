import { describe, expect, it } from "vitest";

import type {
  ConnectionGroup,
  ConnectionRequest,
  HistoryEntry,
  HttpConnectionPreviewResponse,
  PreviewRow,
} from "@/lib/api/generated";
import {
  baseKeyFor,
  canDeclareScoped,
  choiceLabel,
  choiceOf,
  EMPTY_SCOPE,
  maskedBase,
  OUTCOME_TEXT,
  previewLine,
  REASON_TEXT,
  historyLine,
  scopeButtonLabel,
  scopedCreateRequest,
  scopedPreviewRequest,
  scopeKey,
  scopeLine,
  scopeProblems,
  scopeSummary,
  unmappableReason,
  wouldLinkCount,
  type DeclarationScope,
  type ScopeChoice,
} from "@/lib/model/connections";

const BASE_KEY = "hbk1-0123456789abcdef0123";
const OTHER_KEY = "hbk1-fedcba9876543210fedc";

const GROUP: ConnectionGroup = {
  key: "hcg1-0123456789abcdef0123",
  kind: "base-not-stated",
  origin: null,
  base_expression: "{baseUrl}",
  loopback: false,
  requests: 3,
  declarable: 0,
  statuses: { "http-base-unknown": 3 },
  languages: { typescript: 3 },
  callers: ["web"],
  reasons: { host_not_stated: 3 },
};

const FILE: ScopeChoice = { repository: "web", path: "src/api.ts", symbol: null, covered: 3, too_broad: 0 };
const SYMBOL: ScopeChoice = { repository: "web", path: "src/api.ts", symbol: "UsersClient", covered: 2, too_broad: 1 };
const CHOICES = [FILE, SYMBOL];

const request = (extra: Partial<ConnectionRequest> = {}): ConnectionRequest => ({
  decision_ref: "hcd1-0123456789abcdef0123",
  method: "GET",
  route: "/users/{id}",
  site: { repository: "web", path: "src/api.ts", line_start: 10 },
  status: "http-base-unknown",
  language: "typescript",
  symbol: "UsersClient.get",
  base_key: BASE_KEY,
  scopable: true,
  ...extra,
});

const SCOPE: DeclarationScope = {
  path: "src/api.ts",
  symbol: "UsersClient",
  base_key: BASE_KEY,
  base_shown: "{baseUrl}",
  base_path: "/v1",
};

const row = (outcome: PreviewRow["outcome"]): PreviewRow => ({
  decision_ref: "hcd1-0123456789abcdef0123",
  route: "/users/{id}",
  site: { repository: "web", path: "src/api.ts", line_start: 10 },
  outcome,
});

const PREVIEW: HttpConnectionPreviewResponse = {
  context: { scope: "global" },
  group: GROUP.key,
  repository: "users-api",
  scope: SCOPE,
  routes_served: 3,
  counts: { "would-link": 1, "out-of-scope": 2 },
  rows: [row("would-link"), row("out-of-scope")],
  requirements: [],
  can_create: true,
  basis: "",
};

describe("a scope choice", () => {
  it("is named by its caller, file and symbol, and nothing is chosen until the operator picks", () => {
    expect(scopeKey(FILE)).not.toBe(scopeKey(SYMBOL));
    expect(choiceOf(CHOICES, scopeKey(SYMBOL))).toBe(SYMBOL);
    // the empty form picks nothing: no scope, no service, so no preview can be asked
    expect(EMPTY_SCOPE.choice).toBe("");
    expect(EMPTY_SCOPE.repository).toBe("");
    expect(choiceOf(CHOICES, EMPTY_SCOPE.choice)).toBeNull();
    expect(choiceOf(CHOICES, "web\u0000other.ts\u0000")).toBeNull();
    expect(scopedPreviewRequest(GROUP, null, null, EMPTY_SCOPE)).toBeNull();
    expect(scopeProblems(null, null, EMPTY_SCOPE)).toEqual(["Choose the scope.", "Choose the service."]);
  });

  it("shows how many requests it covers and how many are too broad, zero included", () => {
    expect(choiceLabel(FILE)).toEqual({
      name: "web · src/api.ts · whole file",
      counts: "covers 3 requests · 0 too broad",
    });
    expect(choiceLabel(SYMBOL)).toEqual({
      name: "web · src/api.ts · UsersClient",
      counts: "covers 2 requests · 1 too broad",
    });
  });

  it("reads the base's identity from the requests inside it, never from outside", () => {
    expect(baseKeyFor(FILE, [request(), request({ symbol: null })])).toEqual({ key: BASE_KEY });
    // another file, another repository, a symbol sharing only a prefix, or a request not scopable
    const outside = [
      request({ site: { repository: "web", path: "src/other.ts" } }),
      request({ site: { repository: "mobile", path: "src/api.ts" } }),
      request({ symbol: "UsersClientExtra.get" }),
      request({ scopable: false }),
    ];
    expect(baseKeyFor(SYMBOL, outside)).toHaveProperty("problem");
    expect(baseKeyFor(SYMBOL, [request({ symbol: "UsersClient" })])).toEqual({ key: BASE_KEY });
    expect(baseKeyFor(SYMBOL, [request(), request({ base_key: OTHER_KEY })])).toHaveProperty("problem");
  });

  it("asks the scoped preview with the file, symbol and base hash, and no base text or origin", () => {
    const form = { ...EMPTY_SCOPE, choice: scopeKey(SYMBOL), repository: "users-api", basePath: "/v1", environment: " dev " };
    const asked = scopedPreviewRequest(GROUP, SYMBOL, { key: BASE_KEY }, form);
    expect(asked).toEqual({
      context: { scope: "global" },
      group: GROUP.key,
      repository: "users-api",
      environment: "dev",
      scope: { caller: "web", path: "src/api.ts", symbol: "UsersClient", base_key: BASE_KEY, base_path: "/v1" },
    });
    // the base path is the scope's own, never the request's
    expect(asked).not.toHaveProperty("base_path");
    expect(JSON.stringify(asked)).not.toContain("baseUrl");
    // nothing is asked for a base path the server would refuse, a missing service, or an unnamed base
    expect(scopedPreviewRequest(GROUP, SYMBOL, { key: BASE_KEY }, { ...form, basePath: "v1" })).toBeNull();
    expect(scopedPreviewRequest(GROUP, SYMBOL, { key: BASE_KEY }, { ...form, repository: "" })).toBeNull();
    expect(scopedPreviewRequest(GROUP, SYMBOL, { problem: "no base" }, form)).toBeNull();
    expect(scopedPreviewRequest(GROUP, FILE, { key: BASE_KEY }, { ...form, basePath: "" })?.scope?.symbol).toBeNull();
  });

  it("stores the declaration the preview described: the caller, its scope, no URL, the worklist group", () => {
    const form = { ...EMPTY_SCOPE, repository: "users-api", environment: "dev", note: " why " };
    const created = scopedCreateRequest(GROUP, SYMBOL, form, PREVIEW);
    expect(created?.context).toEqual({ scope: "repository", repository: "users-api" });
    expect(created?.link).toMatchObject({
      url: null,
      target_id: null,
      callers: ["web"],
      environment: "dev",
      variables: {},
      conditions: [],
      verification: "unverified",
      scope: SCOPE,
    });
    expect(created?.provenance).toEqual({ via: "ui", note: "why", request_group: GROUP.key });
    // a preview that did not describe a scope cannot be turned into a write
    expect(scopedCreateRequest(GROUP, SYMBOL, form, { ...PREVIEW, scope: null })).toBeNull();
  });
});

describe("the preview outcomes", () => {
  // Exhaustive by type: a new outcome in the Python model fails the typecheck here until it is listed.
  const ALL: Record<PreviewRow["outcome"], true> = {
    "would-link": true,
    ambiguous: true,
    "no-matching-route": true,
    "caller-excluded": true,
    "source-established": true,
    "not-declarable": true,
    "scope-too-broad": true,
    "scope-unidentified": true,
    "injected-root-unselected": true,
    "out-of-scope": true,
  };

  it("have a short plain text each, the scoped ones included", () => {
    for (const outcome of Object.keys(ALL) as PreviewRow["outcome"][]) {
      expect(OUTCOME_TEXT[outcome], outcome).toEqual(expect.any(String));
      expect(OUTCOME_TEXT[outcome].length, outcome).toBeGreaterThan(0);
    }
    expect(Object.keys(OUTCOME_TEXT).sort()).toEqual(Object.keys(ALL).sort());
    expect(new Set(Object.values(OUTCOME_TEXT)).size).toBe(Object.keys(ALL).length);
    expect(previewLine(PREVIEW)).toBe("1 would link · 2 outside the scope");
    expect(previewLine({ ...PREVIEW, counts: { "scope-too-broad": 1 } })).toMatch(/^1 scope too broad/);
  });

  it("explain a scoped declaration that stopped matching", () => {
    for (const reason of ["scope-not-found", "base-no-longer-requested", "scope-too-broad", "caller-not-indexed"]) {
      expect(REASON_TEXT[reason], reason).toBeTruthy();
    }
  });
});

describe("declaring a scope", () => {
  it("is possible once the preview has no requirement and at least one request would link", () => {
    expect(canDeclareScoped(PREVIEW, [], false)).toBe(true);
  });

  it("stays disabled without a would-link preview", () => {
    expect(canDeclareScoped(undefined, [], false)).toBe(false);
    expect(canDeclareScoped({ ...PREVIEW, counts: { "out-of-scope": 2 }, rows: [row("out-of-scope")] }, [], false)).toBe(false);
    expect(canDeclareScoped({ ...PREVIEW, counts: {}, rows: [] }, [], false)).toBe(false);
    expect(wouldLinkCount({ ...PREVIEW, counts: {}, rows: [] })).toBe(0);
    expect(wouldLinkCount(PREVIEW)).toBe(1);
  });

  it("stays disabled while the preview states a requirement, the server refuses, or the form has a problem", () => {
    expect(canDeclareScoped({ ...PREVIEW, requirements: ["The symbol names two entities."], can_create: false }, [], false)).toBe(false);
    expect(canDeclareScoped({ ...PREVIEW, can_create: false }, [], false)).toBe(false);
    expect(canDeclareScoped(PREVIEW, ["Choose the service."], false)).toBe(false);
    expect(canDeclareScoped(PREVIEW, [], true)).toBe(false);
    expect(canDeclareScoped({ ...PREVIEW, scope: null }, [], false)).toBe(false);
  });
});

describe("what is displayed of a base", () => {
  it("is the masked base: a plain name in braces, or {…}, never source text", () => {
    expect(maskedBase("{baseUrl}")).toBe("{baseUrl}");
    expect(maskedBase("{…}")).toBe("{…}");
    for (const raw of ['{os.environ["API_URL"]}', "{this.baseUrl}", "https://api.example.test", "baseUrl", "", null, undefined]) {
      expect(maskedBase(raw), String(raw)).toBe("{…}");
    }
  });

  it("is masked in the preview line, the declaration listing and the history, whatever the server sent", () => {
    const leaky: DeclarationScope = { ...SCOPE, base_shown: '{requests.get("https://secret.example/token")}' };
    expect(scopeLine(SCOPE)).toBe("{baseUrl}/v1 in src/api.ts#UsersClient");
    expect(scopeLine(leaky)).toBe("{…}/v1 in src/api.ts#UsersClient");
    expect(scopeSummary(leaky).join(" ")).not.toContain("secret");
    expect(scopeSummary(SCOPE)).toEqual(["file src/api.ts", "symbol UsersClient", "base {baseUrl}", "base path /v1"]);
    expect(scopeSummary({ ...SCOPE, symbol: null, base_path: "" })).toEqual([
      "file src/api.ts",
      "whole file",
      "base {baseUrl}",
      "no base path",
    ]);
    const entry: HistoryEntry = {
      id: 1,
      at: "2026-01-01T00:00:00Z",
      action: "create",
      subject_id: 4,
      repository: "users-api",
      via: "ui",
      after: { scope: leaky },
    };
    expect(historyLine(entry)).toBe("declared #4 (users-api) {…}/v1 in src/api.ts#UsersClient");
  });
});

describe("a base the source does not state", () => {
  it("still has no origin to map, and its scope button is not a mapping", () => {
    expect(unmappableReason(GROUP)).toMatch(/no origin to map/);
    expect(scopeButtonLabel(GROUP)).toBe("Declare the base of {baseUrl} for a scope");
    expect(scopeButtonLabel(GROUP)).not.toMatch(/Map .* to a service/);
    expect(scopeButtonLabel({ ...GROUP, base_expression: "self.base_url" })).not.toMatch(/Map .* to a service/);
    expect(scopeButtonLabel({ ...GROUP, base_expression: "self.base_url" })).toBe("Declare the base of {…} for a scope");
  });
});
