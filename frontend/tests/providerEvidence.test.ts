import { describe, expect, it } from "vitest";

import type {
  HttpConnectionPreviewResponse,
  PreviewProviderDiagnostics,
  ProviderLifecycle,
  UnpublishedMatch,
} from "@/lib/api/generated";
import { isGood, previewLine, wouldLinkCount } from "@/lib/model/connections";
import {
  absenceLine,
  boundLines,
  CATEGORY_ORDER,
  CATEGORY_TEXT,
  categoryLines,
  codeTags,
  EVIDENCE_LABEL,
  HINT_HEADING,
  HINT_LABEL,
  hintLine,
  hintParts,
  hintsByRequest,
  HYPOTHESIS_NOTE,
  moreHintsLine,
  NO_MATCH_NOTE,
  notExtractedLines,
  NOT_PROVEN,
  reachLines,
  requestAbsenceLine,
  rowEvidence,
  STATUS_TEXT,
  summaryLine as lifecycleLine,
  truncationLine,
} from "@/lib/model/providerEvidence";

const REPOSITORY = "pyprov";

/** What the interface must never say: a route is not claimed absent, and "not served" is never bare. */
const FORBIDDEN = /does not exist|no such route exists|\babsent\b|\bnot served\b/i;

const match = (extra: Partial<UnpublishedMatch> = {}): UnpublishedMatch => ({
  decision_ref: "hcd1-aaaaaaaaaaaaaaaaaaaa",
  route: "GET /orders/{oid}",
  status: "declared",
  path: "svc/orders.py",
  line: 7,
  category: "registration-unproven",
  code: "python-not-mounted",
  detail: "not registered by a proven application",
  ...extra,
});

/** One match for each category, both statuses, one with no category and one the table does not know. */
const MATCHES: UnpublishedMatch[] = [
  match(),
  match({ decision_ref: "hcd1-bbbbbbbbbbbbbbbbbbbb", category: "path-unresolved", code: "spec-cwd-relative", status: "unpublished" }),
  match({ decision_ref: "hcd1-bbbbbbbbbbbbbbbbbbbb", category: "identity-unresolved", code: "oas3-controllers-unresolved" }),
  match({ decision_ref: "hcd1-cccccccccccccccccccc", category: "framework-constraint", code: "nestjs-router-module" }),
  match({ decision_ref: "hcd1-dddddddddddddddddddd", category: "bound-reached", code: "fastify-not-registered" }),
  match({ decision_ref: "hcd1-eeeeeeeeeeeeeeeeeeee", category: "unclassified", code: "unclassified", path: null, line: null }),
  match({ decision_ref: "hcd1-ffffffffffffffffffff", category: null, code: "", detail: "" }),
  match({ decision_ref: "hcd1-gggggggggggggggggggg", category: "a-category-added-later" }),
];

const LIFECYCLE: ProviderLifecycle = {
  recognized: 12,
  published: 2,
  declared: 6,
  unpublished: 4,
  by_category: {
    "registration-unproven": 3,
    "path-unresolved": 1,
    "identity-unresolved": 2,
    "framework-constraint": 1,
    "bound-reached": 1,
    unclassified: 1,
    "a-category-added-later": 1,
    "identity-unresolved-empty": 0,
  },
  by_code: { "python-not-mounted": 3, "nestjs-router-module": 1, "never-seen": 0 },
  truncated: true,
  bounds_reached: [
    { bound: "fastify-helper-calls-per-file", limit: 256, path: "src/app.ts", scope: "file" },
    { bound: "python-mount-depth", scope: "repository" },
  ],
  bound_notes_omitted: 2,
  unpublished_omitted: 3,
  not_extracted: [
    { code: "express-router-without-mount", note: "A router with no mount in its own file serves no route that is recorded." },
  ],
};

const DIAGNOSTICS: PreviewProviderDiagnostics = {
  lifecycle: LIFECYCLE,
  routes_exposable: 4,
  routes_without_declaration_line: 2,
  exposures_truncated: true,
  unpublished_matches: MATCHES,
  unpublished_match_count: MATCHES.length + 3,
};

/** A diagnostics object that says nothing of interest: nothing recognized, nothing reached. */
const QUIET: PreviewProviderDiagnostics = {
  lifecycle: { recognized: 8, published: 2, declared: 6, unpublished: 0 },
  routes_exposable: 2,
  routes_without_declaration_line: 0,
  exposures_truncated: false,
  unpublished_matches: [],
  unpublished_match_count: 0,
};

describe("each category reads as its sentence", () => {
  it("has a sentence for every category the server can name, each saying the declaration was found in source", () => {
    expect([...CATEGORY_ORDER].sort()).toEqual(Object.keys(CATEGORY_TEXT).sort());
    for (const category of CATEGORY_ORDER) expect(CATEGORY_TEXT[category], category).toMatch(/^found in source/);
  });

  it("words the six distinctions as the operator reads them", () => {
    expect(CATEGORY_TEXT["registration-unproven"]).toContain("registration not proven");
    expect(CATEGORY_TEXT["path-unresolved"]).toContain("method or full path could not be resolved");
    expect(CATEGORY_TEXT["identity-unresolved"]).toContain("application or router it belongs to could not be identified");
    expect(CATEGORY_TEXT["framework-constraint"]).toContain("withheld by a framework rule RepoAtlas applies");
    expect(CATEGORY_TEXT["bound-reached"]).toContain("an analysis limit was reached");
    expect(CATEGORY_TEXT.unclassified).toContain("could not classify why");
  });

  it("gives every non-zero category its line with its count, in a fixed order, and none for a zero", () => {
    const lines = categoryLines(LIFECYCLE);
    expect(lines.map((line) => [line.category, line.count])).toEqual([
      ["registration-unproven", 3],
      ["path-unresolved", 1],
      ["identity-unresolved", 2],
      ["framework-constraint", 1],
      ["bound-reached", 1],
      ["unclassified", 1],
      ["a-category-added-later", 1],
    ]);
    expect(lines[0]!.text).toBe(`3 declarations: ${CATEGORY_TEXT["registration-unproven"]}`);
    expect(lines[1]!.text).toBe(`1 declaration: ${CATEGORY_TEXT["path-unresolved"]}`);
  });

  it("reads a category it does not know as unclassified, never as a precise one", () => {
    const [line] = categoryLines({ by_category: { "a-category-added-later": 2 } });
    expect(line!.text).toContain(CATEGORY_TEXT.unclassified);
    expect(hintParts(match({ category: "a-category-added-later" })).category).toBe(CATEGORY_TEXT.unclassified);
    expect(hintParts(match({ category: null })).category).toBe(CATEGORY_TEXT.unclassified);
  });

  it("lists the codes the server recorded, leaving out a zero", () => {
    expect(codeTags(LIFECYCLE)).toEqual([
      { code: "python-not-mounted", count: 3 },
      { code: "nestjs-router-module", count: 1 },
    ]);
  });

  it("states the repository's recognized declarations and what became of them", () => {
    expect(lifecycleLine(REPOSITORY, LIFECYCLE)).toBe(
      "In pyprov's indexed source: 12 route declarations recognized · 2 served · 6 declared, not proven · 4 recognized, not published",
    );
    expect(lifecycleLine(REPOSITORY, { recognized: 1 })).toContain("1 route declaration recognized");
  });
});

describe("what is grouped under a request", () => {
  it("groups the possible matches by the request they resemble and invents no request", () => {
    const grouped = hintsByRequest(DIAGNOSTICS);
    expect([...grouped.keys()].sort()).toEqual([...new Set(MATCHES.map((m) => m.decision_ref))].sort());
    expect(grouped.get("hcd1-bbbbbbbbbbbbbbbbbbbb")!.map((m) => m.category)).toEqual(["path-unresolved", "identity-unresolved"]);
    expect(grouped.get("hcd1-not-a-request")).toBeUndefined();
  });

  it("is empty without diagnostics or without a match", () => {
    expect(hintsByRequest(null).size).toBe(0);
    expect(hintsByRequest(undefined).size).toBe(0);
    expect(hintsByRequest({ lifecycle: {} }).size).toBe(0);
    expect(hintsByRequest(QUIET).size).toBe(0);
  });

  it("labels each match not proven, with its route, place, category sentence and code", () => {
    const line = hintLine(match());
    expect(line).toContain(NOT_PROVEN);
    expect(line).toContain("GET /orders/{oid}");
    expect(line).toContain("svc/orders.py:7");
    expect(line).toContain(STATUS_TEXT.declared);
    expect(line).toContain(CATEGORY_TEXT["registration-unproven"]);
    expect(line).toContain("python-not-mounted");
    // a match with no place, no code and no detail leaves them out rather than printing blanks
    const bare = hintParts(match({ path: null, line: null, code: "", detail: "" }));
    expect(bare).toMatchObject({ place: null, code: null, detail: null });
    expect(hintParts(match({ line: null })).place).toBe("svc/orders.py");
  });

  it("says how many possible matches the server found and did not list", () => {
    expect(moreHintsLine(DIAGNOSTICS)).toBe("3 more possible matches not listed.");
    expect(moreHintsLine({ ...DIAGNOSTICS, unpublished_match_count: MATCHES.length + 1 })).toBe(
      "1 more possible match not listed.",
    );
    expect(moreHintsLine({ ...DIAGNOSTICS, unpublished_match_count: MATCHES.length })).toBeNull();
    expect(moreHintsLine(QUIET)).toBeNull();
  });

  it("puts a possible match under its request, and says 'no match' only of a request that has none to show", () => {
    const unmatched = { decision_ref: "hcd1-nothing", outcome: "no-matching-route" } as const;
    const hinted = { decision_ref: MATCHES[0]!.decision_ref, outcome: "no-matching-route" } as const;
    const good = { decision_ref: "hcd1-good", outcome: "would-link" } as const;
    const whole: PreviewProviderDiagnostics = { ...QUIET, unpublished_matches: [match()], unpublished_match_count: 1 };

    expect(rowEvidence(whole, REPOSITORY, hinted)).toEqual({ matches: [match()], absence: null });
    expect(rowEvidence(whole, REPOSITORY, unmatched)).toEqual({ matches: [], absence: requestAbsenceLine(REPOSITORY, 0) });
    // a request that would link is not described as unmatched
    expect(rowEvidence(whole, REPOSITORY, good)).toEqual({ matches: [], absence: null });
    // the server cut its list: a request with none shown may still have one, so nothing is said of it
    expect(rowEvidence({ ...whole, unpublished_match_count: 6 }, REPOSITORY, unmatched)).toEqual({
      matches: [],
      absence: null,
    });
    // no diagnostics, no evidence
    expect(rowEvidence(null, REPOSITORY, unmatched)).toEqual({ matches: [], absence: null });
    expect(rowEvidence(undefined, REPOSITORY, hinted)).toEqual({ matches: [], absence: null });
  });

  it("states no proof for a request that matched nothing", () => {
    const line = requestAbsenceLine(REPOSITORY, 0);
    expect(line).toContain(
      "No route this declaration can reach in pyprov, and no unproven declaration the index recognized there, matches this request.",
    );
    expect(line).toContain("this is not proof that pyprov has no such route");
    expect(line).not.toContain("No route this index recognizes");
  });

  it("names the served routes it could not compare only when there are some, singular or plural", () => {
    expect(requestAbsenceLine(REPOSITORY, 0)).not.toContain("could not be compared");
    expect(requestAbsenceLine(REPOSITORY, 0)).not.toContain("no-declaration-line");
    expect(requestAbsenceLine(REPOSITORY, 1)).toContain(
      " 1 served route built from a prefix could not be compared (no-declaration-line), so one of them may answer it.",
    );
    expect(requestAbsenceLine(REPOSITORY, 3)).toContain(
      " 3 served routes built from a prefix could not be compared (no-declaration-line), so one of them may answer it.",
    );
    // the caveat stays last, whatever the count
    for (const unreached of [0, 1, 3]) {
      expect(requestAbsenceLine(REPOSITORY, unreached)).toMatch(/RepoAtlas does not read every way a route can be declared, so this is not proof that pyprov has no such route\.$/);
    }
  });

  it("passes the number of routes no declaration reaches from the diagnostics to the line", () => {
    const unmatched = { decision_ref: "hcd1-nothing", outcome: "no-matching-route" } as const;
    const base: PreviewProviderDiagnostics = { ...QUIET, unpublished_matches: [], unpublished_match_count: 0 };
    expect(rowEvidence({ ...base, routes_without_declaration_line: 1 }, REPOSITORY, unmatched).absence).toBe(
      requestAbsenceLine(REPOSITORY, 1),
    );
    expect(rowEvidence({ ...base, routes_without_declaration_line: 2 }, REPOSITORY, unmatched).absence).toBe(
      requestAbsenceLine(REPOSITORY, 2),
    );
    expect(rowEvidence({ ...base, routes_without_declaration_line: undefined }, REPOSITORY, unmatched).absence).toBe(
      requestAbsenceLine(REPOSITORY, 0),
    );
  });

  it("says nothing of a request when the server offered only the first routes to the declaration", () => {
    const unmatched = { decision_ref: "hcd1-nothing", outcome: "no-matching-route" } as const;
    const base: PreviewProviderDiagnostics = { ...QUIET, unpublished_matches: [], unpublished_match_count: 0 };
    expect(rowEvidence({ ...base, exposures_truncated: true }, REPOSITORY, unmatched)).toEqual({ matches: [], absence: null });
    expect(
      rowEvidence({ ...base, exposures_truncated: true, routes_without_declaration_line: 2 }, REPOSITORY, unmatched).absence,
    ).toBeNull();
    // the same diagnostics without the cap do say it
    expect(rowEvidence({ ...base, exposures_truncated: false }, REPOSITORY, unmatched).absence).not.toBeNull();
  });

  it("says nothing of a request when the server cut its list of possible matches", () => {
    const unmatched = { decision_ref: "hcd1-nothing", outcome: "no-matching-route" } as const;
    const cut: PreviewProviderDiagnostics = {
      ...QUIET,
      routes_without_declaration_line: 1,
      unpublished_matches: [match()],
      unpublished_match_count: 2,
    };
    expect(rowEvidence(cut, REPOSITORY, unmatched)).toEqual({ matches: [], absence: null });
    expect(rowEvidence({ ...cut, unpublished_match_count: 1 }, REPOSITORY, unmatched).absence).not.toBeNull();
  });
});

describe("what is said of the whole repository", () => {
  it("says no declaration was recognized only when none was, and never as proof", () => {
    expect(absenceLine(REPOSITORY, { recognized: 8 })).toBeNull();
    expect(absenceLine(REPOSITORY, { recognized: 1 })).toBeNull();
    for (const lifecycle of [{ recognized: 0 }, {}] as ProviderLifecycle[]) {
      const line = absenceLine("emptyprov", lifecycle);
      expect(line).toContain("No route declaration was recognized in emptyprov's indexed source");
      expect(line).toContain("not proof");
    }
    expect(notExtractedLines(LIFECYCLE).map((item) => item.code)).toEqual(["express-router-without-mount"]);
    expect(notExtractedLines({})).toEqual([]);
  });

  it("states a route no declaration can reach only when there is one, and the cap only when it was hit", () => {
    expect(reachLines("nestok", { ...QUIET, routes_without_declaration_line: 0, exposures_truncated: false })).toEqual([]);
    expect(reachLines("nestok", { ...QUIET, routes_without_declaration_line: 1 })).toEqual([
      "1 route nestok serves is built from a prefix and states no single source line, so a declaration cannot reach it (no-declaration-line).",
    ]);
    const plural = reachLines("nestok", { ...QUIET, routes_without_declaration_line: 2 });
    expect(plural).toHaveLength(1);
    expect(plural[0]).toContain("2 routes nestok serves are built from a prefix");
    expect(plural[0]).toContain("cannot reach them (no-declaration-line)");
    expect(reachLines("nestok", { ...QUIET, exposures_truncated: true })).toEqual([
      "Only the first 2,000 routes were offered to this declaration; others were not considered.",
    ]);
    expect(reachLines("nestok", { ...QUIET, routes_without_declaration_line: 1, exposures_truncated: true })).toHaveLength(2);
  });

  it("says analysis limits were reached only when they were, then names each, counting what is not listed", () => {
    expect(truncationLine("fhelp", { truncated: false })).toBeNull();
    expect(truncationLine("fhelp", {})).toBeNull();
    expect(truncationLine("fhelp", { truncated: true })).toBe(
      "Analysis limits were reached in fhelp; these counts may be incomplete.",
    );
    expect(boundLines(LIFECYCLE)).toEqual([
      "fastify-helper-calls-per-file (limit 256) in src/app.ts",
      "python-mount-depth in the repository",
      "2 more analysis limits reached, not listed.",
      "3 more recognized declarations counted above, not listed.",
    ]);
    expect(boundLines({})).toEqual([]);
  });

  it("explains a 'no matching route' as no proof, naming the repository", () => {
    const note = NO_MATCH_NOTE(REPOSITORY);
    expect(note).toContain("no route this index proves pyprov serves matches the request");
    expect(note).toContain("not proof that pyprov has no such route");
  });
});

describe("the words the interface must never use", () => {
  /** Every string this module can produce, for a diagnostics object that holds every case. */
  function everything(): string[] {
    const texts: string[] = [
      ...Object.values(CATEGORY_TEXT),
      ...Object.values(STATUS_TEXT),
      EVIDENCE_LABEL,
      HINT_LABEL,
      HINT_HEADING,
      NOT_PROVEN,
      HYPOTHESIS_NOTE,
      NO_MATCH_NOTE(REPOSITORY),
      requestAbsenceLine(REPOSITORY, 0),
      requestAbsenceLine(REPOSITORY, 1),
      requestAbsenceLine(REPOSITORY, 3),
      lifecycleLine(REPOSITORY, LIFECYCLE),
      lifecycleLine(REPOSITORY, {}),
      absenceLine(REPOSITORY, {}) ?? "",
      truncationLine(REPOSITORY, LIFECYCLE) ?? "",
      ...categoryLines(LIFECYCLE).map((line) => line.text),
      ...boundLines(LIFECYCLE),
      ...reachLines(REPOSITORY, DIAGNOSTICS),
      ...reachLines(REPOSITORY, { ...DIAGNOSTICS, routes_without_declaration_line: 1 }),
      moreHintsLine(DIAGNOSTICS) ?? "",
      ...notExtractedLines(LIFECYCLE).map((item) => item.note ?? ""),
      ...MATCHES.map(hintLine),
    ];
    for (const m of MATCHES) {
      const parts = hintParts(m);
      texts.push(parts.status, parts.category, parts.route, parts.place ?? "", parts.code ?? "", parts.detail ?? "");
    }
    return texts;
  }

  it("covers every status, every category and the cases that make a sentence", () => {
    const texts = everything();
    expect(texts.length).toBeGreaterThan(40);
    for (const sentence of [...Object.values(CATEGORY_TEXT), ...Object.values(STATUS_TEXT)]) {
      expect(texts).toContain(sentence);
    }
    expect(MATCHES.map((m) => m.status)).toEqual(expect.arrayContaining(["declared", "unpublished"]));
  });

  it("never says a route does not exist, is absent, or is plainly 'not served'", () => {
    for (const text of everything()) expect(text, text).not.toMatch(FORBIDDEN);
  });

  it("the guard itself catches the phrases it guards against", () => {
    for (const bad of ["Route not served", "the route does not exist", "No such route exists", "it is absent", "a route is not served"]) {
      expect(bad).toMatch(FORBIDDEN);
    }
    for (const good of ["declared in source, not proven served", "not published as served", "this is not proof"]) {
      expect(good).not.toMatch(FORBIDDEN);
    }
  });
});

describe("hints change no decision", () => {
  const BASE: HttpConnectionPreviewResponse = {
    context: { scope: "global" },
    group: "hcg1-0123456789abcdef0123",
    origin: "https://orders.example.test",
    url: "https://orders.example.test",
    repository: REPOSITORY,
    routes_served: 2,
    counts: { "would-link": 1, "no-matching-route": 2 },
    can_create: true,
    basis: "",
    rows: [
      { decision_ref: "hcd1-1", outcome: "would-link", route: "/a", method: "GET", site: { repository: "buyer", path: "a.py" } },
      { decision_ref: "hcd1-2", outcome: "no-matching-route", route: "/b", method: "GET", site: { repository: "buyer", path: "b.py" } },
    ],
  };
  const DIAGNOSED: HttpConnectionPreviewResponse = { ...BASE, provider_diagnostics: DIAGNOSTICS };

  it("gives the same preview line and would-link count with and without provider diagnostics", () => {
    expect(previewLine(DIAGNOSED)).toBe(previewLine(BASE));
    expect(previewLine(DIAGNOSED)).toBe("1 would link · 2 no matching route");
    expect(wouldLinkCount(DIAGNOSED)).toBe(wouldLinkCount(BASE));
    expect(wouldLinkCount(DIAGNOSED)).toBe(1);
  });

  it("leaves every row's goodness to its outcome, whatever hints it has", () => {
    const hinted = { ...BASE, rows: [{ ...BASE.rows![1]!, decision_ref: MATCHES[0]!.decision_ref }] };
    const withHints: HttpConnectionPreviewResponse = { ...hinted, provider_diagnostics: DIAGNOSTICS };
    expect(hintsByRequest(withHints.provider_diagnostics).has(MATCHES[0]!.decision_ref)).toBe(true);
    for (const row of withHints.rows!) expect(isGood(row.outcome)).toBe(false);
    expect(previewLine(withHints)).toBe(previewLine(hinted));
    expect(wouldLinkCount(withHints)).toBe(wouldLinkCount(hinted));
  });
});
