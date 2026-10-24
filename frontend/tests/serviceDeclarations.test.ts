import { describe, expect, it } from "vitest";

import type {
  ConnectionGroup,
  ExternalServiceFields,
  ServiceDeclarationPreviewResponse,
  ServiceDeclarationView,
  ServiceHistoryEntry,
} from "@/lib/api/generated";
import {
  authenticatesBasis,
  authTargetLine,
  canClassify,
  classifiedReason,
  classifyCreateRequest,
  classifyFields,
  classifyFormFrom,
  classifyPreviewRequest,
  classifyProblems,
  classifyUpdateRequest,
  EMPTY_CLASSIFY,
  externalServices,
  externalTag,
  filterFields,
  IDENTITY_OPTIONS,
  identityLabel,
  NO_FILTERS,
  serviceHistoryLine,
  servicePreviewLine,
  summaryLine,
  unmappableReason,
  type ClassifyForm,
} from "@/lib/model/connections";

const GROUP: ConnectionGroup = {
  key: "hcg1-0123456789abcdef0123",
  kind: "stated-origin",
  origin: "https://login.example.test",
  loopback: false,
  requests: 2,
  declarable: 2,
  statuses: { "http-service-unmatched": 2 },
  languages: { python: 2 },
  callers: ["worker"],
  reasons: { no_identity: 2 },
};

const FIELDS: ExternalServiceFields = {
  kind: "external_service",
  origin: "https://login.example.test",
  label: "Login provider",
  provider: "Example Identity",
  category: "identity",
  note: null,
  authenticates: [{ repository: "portal", environment: "prod" }],
};

const VIEW = (
  state: ServiceDeclarationView["state"] = "applied",
  extra: Partial<ServiceDeclarationView> = {},
): ServiceDeclarationView => ({
  id: 4,
  fields: FIELDS,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  state,
  ...extra,
});

const FORM: ClassifyForm = {
  ...EMPTY_CLASSIFY,
  label: "  Login provider ",
  provider: "Example Identity",
  category: "identity",
  authenticates: [{ repository: "portal", environment: " prod " }],
};

describe("what a request's identity is called", () => {
  it("names the three identities, and reads a missing one as unresolved", () => {
    expect(identityLabel("known-external")).toBe("known external dependency");
    expect(identityLabel("identified")).toBe("identified");
    expect(identityLabel("unresolved")).toBe("unresolved service identity");
    expect(identityLabel(undefined)).toBe("unresolved service identity");
    expect(identityLabel(null)).toBe("unresolved service identity");
  });

  it("offers exactly those three to filter by, and sends the chosen one", () => {
    expect(IDENTITY_OPTIONS.map((o) => o.value).sort()).toEqual(["identified", "known-external", "unresolved"]);
    expect(filterFields(NO_FILTERS)).toEqual({ include_resolved: false });
    expect(filterFields({ ...NO_FILTERS, identity: "known-external" })).toEqual({
      identity: "known-external",
      include_resolved: false,
    });
  });
});

describe("the classify form", () => {
  it("needs a label and says so before asking the server", () => {
    expect(classifyProblems(GROUP.origin, EMPTY_CLASSIFY)).toEqual(["Give the service a label."]);
    expect(classifyProblems(GROUP.origin, { ...EMPTY_CLASSIFY, label: "   " })).toEqual(["Give the service a label."]);
    expect(classifyProblems(GROUP.origin, FORM)).toEqual([]);
  });

  it("refuses an origin that is absent, has a path, a credential or a wildcard", () => {
    expect(classifyProblems(null, FORM)).toEqual(["This group states no origin to classify."]);
    for (const bad of [
      "https://login.example.test/authorize",
      "https://user@login.example.test",
      "https://*.example.test",
      "login.example.test",
      "https://login.example.test?x=1",
    ]) {
      expect(classifyProblems(bad, FORM), bad).toEqual([
        "The origin is scheme://host[:port], with no path, query, credential or wildcard.",
      ]);
    }
    expect(classifyProblems("http://portal:8080", FORM)).toEqual([]);
  });

  it("refuses a target row without a repository, and a repository named twice", () => {
    expect(
      classifyProblems(GROUP.origin, { ...FORM, authenticates: [{ repository: "", environment: "" }] }),
    ).toEqual(["Choose a repository for each authentication target, or remove the row."]);
    expect(
      classifyProblems(GROUP.origin, {
        ...FORM,
        authenticates: [
          { repository: "portal", environment: "prod" },
          { repository: "portal", environment: " prod" },
        ],
      }),
    ).toEqual(["A repository and environment is named once."]);
    // the same repository in two environments is two targets
    expect(
      classifyProblems(GROUP.origin, {
        ...FORM,
        authenticates: [
          { repository: "portal", environment: "prod" },
          { repository: "portal", environment: "dev" },
        ],
      }),
    ).toEqual([]);
  });

  it("states the fields trimmed, with empty words as null", () => {
    expect(classifyFields("https://login.example.test", FORM)).toEqual({
      kind: "external_service",
      origin: "https://login.example.test",
      label: "Login provider",
      provider: "Example Identity",
      category: "identity",
      note: null,
      authenticates: [{ repository: "portal", environment: "prod" }],
    });
    expect(
      classifyFields("https://login.example.test", { ...FORM, provider: " ", authenticates: [{ repository: "portal", environment: "" }] }),
    ).toMatchObject({ provider: null, authenticates: [{ repository: "portal", environment: null }] });
  });

  it("fills the form from a stored service to edit it", () => {
    expect(classifyFormFrom(FIELDS)).toEqual({
      label: "Login provider",
      provider: "Example Identity",
      category: "identity",
      note: "",
      authenticates: [{ repository: "portal", environment: "prod" }],
    });
  });

  it("asks for a preview only when the form has no problem, naming the group or the edited id", () => {
    expect(classifyPreviewRequest(GROUP.origin, EMPTY_CLASSIFY, { group: GROUP.key })).toBeNull();
    expect(classifyPreviewRequest(null, FORM)).toBeNull();
    const request = classifyPreviewRequest(GROUP.origin, FORM, { group: GROUP.key });
    expect(request).toMatchObject({ context: { scope: "global" }, group: GROUP.key });
    expect(request?.fields).toMatchObject({ origin: GROUP.origin, label: "Login provider" });
    expect(request).not.toHaveProperty("id");
    const edit = classifyPreviewRequest(GROUP.origin, FORM, { id: 4 });
    expect(edit).toMatchObject({ id: 4 });
    expect(edit).not.toHaveProperty("group");
  });

  it("sends one create with the group as provenance, and an update with the same identity", () => {
    const create = classifyCreateRequest("https://login.example.test", FORM, GROUP.key);
    expect(create.context).toEqual({ scope: "global" });
    expect(create.provenance).toEqual({ via: "ui", note: null, request_group: GROUP.key });
    expect(create.fields).toMatchObject({ kind: "external_service", origin: "https://login.example.test" });

    const update = classifyUpdateRequest(4, "https://login.example.test", { ...FORM, label: "Renamed" });
    expect(update.id).toBe(4);
    expect(update.fields).toMatchObject({ origin: "https://login.example.test", label: "Renamed" });
    expect(update.provenance).toEqual({ via: "ui", note: null });
  });

  it("states the preview in the words the interface commits to", () => {
    const preview: ServiceDeclarationPreviewResponse = {
      context: { scope: "global" },
      kind: "external_service",
      subject: "https://login.example.test",
      counts: { would_be_known_external: 2, stay_identified: 0, requests_reached: 2 },
      can_create: true,
    };
    expect(servicePreviewLine(preview)).toBe(
      "2 requests would be shown as a known external dependency; their decision stays http-service-unmatched; nothing is joined.",
    );
    expect(servicePreviewLine({ ...preview, counts: { would_be_known_external: 1, stay_identified: 3 } })).toBe(
      "1 request would be shown as a known external dependency; their decision stays http-service-unmatched; nothing is joined. 3 requests the source already identifies stay identified.",
    );
  });
});

describe("a classified origin", () => {
  it("replaces the Map control with the reason, naming the classification", () => {
    expect(unmappableReason({ ...GROUP, external_service: VIEW() })).toBe(
      "Classified as external service #4; revoke it to map this origin to an indexed service.",
    );
    expect(classifiedReason({ id: 9 })).toBe(
      "Classified as external service #9; revoke it to map this origin to an indexed service.",
    );
    // in any state: a classification that is not applied still blocks a mapping
    expect(unmappableReason({ ...GROUP, external_service: VIEW("ineffective") })).toMatch(/^Classified as external service #4/);
  });

  it("leaves an origin that is not classified exactly as it was", () => {
    expect(unmappableReason(GROUP)).toBeNull();
    expect(unmappableReason({ ...GROUP, external_service: null })).toBeNull();
    expect(unmappableReason({ ...GROUP, declarable: 0 })).toMatch(/never overrides/);
  });

  it("is tagged as a known external dependency while it is applied, and says its state otherwise", () => {
    expect(externalTag(VIEW("applied"))).toBe("known external dependency: Login provider");
    expect(externalTag(VIEW("ineffective"))).toBe("external service Login provider (ineffective)");
    expect(externalTag(VIEW("invalidated"))).toBe("external service Login provider (invalidated)");
  });

  it("can be classified only from a stated, non-loopback origin with unmatched requests, once", () => {
    expect(canClassify(GROUP)).toBe(true);
    expect(canClassify({ ...GROUP, external_service: VIEW() })).toBe(false);
    expect(canClassify({ ...GROUP, loopback: true })).toBe(false);
    expect(canClassify({ ...GROUP, kind: "base-not-stated", origin: null })).toBe(false);
    expect(canClassify({ ...GROUP, statuses: { "http-route-unmatched": 2 } })).toBe(false);
    expect(canClassify({ ...GROUP, statuses: { "http-route-unmatched": 1, "http-service-unmatched": 1 } })).toBe(true);
  });

  it("lists the external services among a response's declarations", () => {
    expect(externalServices([VIEW(), VIEW("ineffective", { id: 5 })]).map((v) => v.id)).toEqual([4, 5]);
    expect(externalServices([])).toEqual([]);
  });
});

describe("what authentication rests on", () => {
  it("is the operator's statement unless the response itself says it is from the source", () => {
    expect(authenticatesBasis("declared")).toBe("operator-declared");
    expect(authenticatesBasis(undefined)).toBe("operator-declared");
    expect(authenticatesBasis(null)).toBe("operator-declared");
    expect(authenticatesBasis("anything else")).toBe("operator-declared");
    expect(authenticatesBasis("source")).toBe("source-backed");
  });

  it("shows a target as its repository and its environment, or any", () => {
    expect(authTargetLine({ repository: "portal", environment: "prod" })).toBe("portal (prod)");
    expect(authTargetLine({ repository: "portal", environment: null })).toBe("portal (any environment)");
    expect(authTargetLine({ repository: "portal" })).toBe("portal (any environment)");
  });
});

describe("the worklist's summary with identities", () => {
  const base = { unresolved: 8, declarable: 6, base_not_stated: 1, linked_by_declaration: 0 };

  it("adds the known external and unresolved identity counts once a request is known external", () => {
    expect(summaryLine({ ...base, known_external: 2, unresolved_identity: 6 })).toBe(
      "8 unresolved requests · 6 can be mapped · 1 with no stated base · 2 known external · 6 unresolved identity",
    );
  });

  it("is unchanged while nothing is known external", () => {
    const old = "8 unresolved requests · 6 can be mapped · 1 with no stated base";
    expect(summaryLine(base)).toBe(old);
    expect(summaryLine({ ...base, known_external: 0, unresolved_identity: 8 })).toBe(old);
  });
});

describe("the audit trail", () => {
  it("reads an entry as an action on an external service", () => {
    const entry: ServiceHistoryEntry = {
      id: 3,
      at: "2026-01-01T00:00:00Z",
      action: "revoke",
      kind: "external_service",
      subject_id: 4,
      subject: "https://login.example.test",
      via: "ui",
    };
    expect(serviceHistoryLine(entry)).toBe("revoked external service #4 https://login.example.test");
    expect(serviceHistoryLine({ ...entry, action: "create" })).toBe(
      "declared external service #4 https://login.example.test",
    );
  });
});
