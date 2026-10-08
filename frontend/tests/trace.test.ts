import { describe, expect, it } from "vitest";

import type { ChildEntry, ImpactCandidate, ImpactResponse, LineReference } from "@/lib/api/generated";
import { fragmentGroups, tokenRuns } from "@/lib/code/fragments";
import { grammarFor, isString, tokenize, type CodeToken } from "@/lib/code/highlight";
import { columnLabelId, layoutTrace, traceFrameId, TRACE_ROWS, traceCardHeight } from "@/lib/graph/layout";
import { DIRECTORY_TRACE, depth, direction, placeKey, search, traceSeed, type Place } from "@/lib/model/location";
import { spansFor, symbolIndex, type SymbolIndex } from "@/lib/model/symbols";
import {
  boundaryLabel,
  buildTrace,
  chainOf,
  nameFromKey,
  reachOf,
  relationOfRow,
  rowId,
  SEED_ID,
  splitAt,
  unitLabel,
  type TraceGraph,
} from "@/lib/model/trace";

// -- the names in a file's source ---------------------------------------------------

const HERE = { repository: "app", path: "src/policy.ts" };
const ROLE = "sym:contracts:src/roles.ts:CourseRole:1";
const CAN_EDIT = "sym:app:src/policy.ts:canEdit:2";
const MODULE = "module:@fixture/contracts";

function ref(line: number, target: { key: string; name: string; type: string; repository?: string; path?: string }, end = line): LineReference {
  return {
    line_start: line,
    line_end: end,
    type: "USES",
    source: CAN_EDIT,
    target: { key: target.key, name: target.name, type: target.type, repository_slug: target.repository ?? null, path: target.path ?? null } as LineReference["target"],
  };
}

const role = { key: ROLE, name: "CourseRole", type: "symbol", repository: "contracts", path: "src/roles.ts" };
const contracts = { key: MODULE, name: "@fixture/contracts", type: "module" };
const SOURCE = [
  'import { CourseRole } from "@fixture/contracts";',
  "export function canEdit(role: CourseRole) {",
  "  return role === CourseRole.Editor; // CourseRole",
  "}",
];

function policyIndex(): SymbolIndex {
  const declarations: ChildEntry[] = [
    { id: "1", path: HERE.path, key: "sym:app:src/policy.ts:policy:1", name: "policy", type: "module", kind: "declaration", line_start: 1, line_end: 4 },
    { id: "2", path: HERE.path, key: CAN_EDIT, name: "canEdit", type: "function", kind: "declaration", line_start: 2, line_end: 4 },
  ];
  const references = [ref(1, contracts), ref(2, role), ref(3, role), ref(2, { key: CAN_EDIT, name: "canEdit", type: "function", ...HERE }, 4)];
  references[0]!.type = "IMPORTS";
  references[3]!.type = "EXPORTS";
  return symbolIndex(declarations, references, HERE);
}

/** Every span of one line as `name→key`. */
function linked(index: SymbolIndex, line: number, where?: Parameters<typeof spansFor>[3]): string[] {
  const text = SOURCE[line - 1]!;
  return spansFor(line, text, index, where).map((s) => `${text.slice(s.start, s.end)}→${s.key}`);
}

/** Comment and string offsets of a line, as a highlighter would mark them. */
function marked(text: string, kinds: { from: number; to: number; color: string }[]): CodeToken[] {
  const tokens: CodeToken[] = [];
  let at = 0;
  for (const k of kinds) {
    if (k.from > at) tokens.push({ content: text.slice(at, k.from), color: "var(--ra-code-foreground)" });
    tokens.push({ content: text.slice(k.from, k.to), color: k.color });
    at = k.to;
  }
  if (at < text.length) tokens.push({ content: text.slice(at), color: "var(--ra-code-foreground)" });
  return tokens;
}

describe("names in the source", () => {
  it("links only what the index names, never the file or the module declaration itself", () => {
    const index = policyIndex();
    expect([...index.targets.keys()].sort()).toEqual([CAN_EDIT, MODULE, ROLE].sort());
    expect(index.targets.get(CAN_EDIT)?.here).toBe(true);
    expect(index.targets.get(ROLE)).toMatchObject({ here: false, repository: "contracts", path: "src/roles.ts" });
  });

  it("links an import's specifier, in quotes, to the module it names", () => {
    const text = SOURCE[0]!;
    const at = text.indexOf('"');
    const tokens = marked(text, [{ from: at, to: text.lastIndexOf('"') + 1, color: "var(--ra-code-token-string-expression)" }]);
    expect(linked(policyIndex(), 1, tokenRuns(tokens))).toEqual([`CourseRole→${ROLE}`, `@fixture/contracts→${MODULE}`]);
    // only the specifier: a binding spelled like the module is not the module
    const lodash = 'import lodash from "lodash";';
    const index = symbolIndex([], [ref(1, { key: "module:lodash", name: "lodash", type: "module" })], HERE);
    const quoted = marked(lodash, [{ from: lodash.indexOf('"'), to: lodash.lastIndexOf('"') + 1, color: "var(--ra-code-token-string-expression)" }]);
    expect(spansFor(1, lodash, index, tokenRuns(quoted)).map((s) => lodash.slice(s.start, s.end))).toEqual(["lodash"]);
    expect(spansFor(1, lodash, index, tokenRuns(quoted))[0]!.start).toBe(lodash.indexOf('"') + 1);
  });

  it("links an import line's binding to what the file's own uses of that name reach", () => {
    const index = policyIndex();
    expect(index.imported.get("CourseRole")).toBe(ROLE);
    // the same word on a line that imports nothing is left alone unless the index names it there
    const bare = symbolIndex([], [ref(5, role)], HERE);
    expect(spansFor(1, "const CourseRole = 1;", bare)).toEqual([]);
  });

  it("links a use on the line the index records it, and a declaration where it is made", () => {
    const index = policyIndex();
    expect(linked(index, 2)).toEqual([`canEdit→${CAN_EDIT}`, `CourseRole→${ROLE}`]);
    expect(linked(index, 2).some((l) => l.startsWith("role→"))).toBe(false);
  });

  it("links a deployment's URL where the line declares it, quoted or not, and never a piece of it", () => {
    const api = { key: "http-deployment:https://api.example.com", name: "https://api.example.com", type: "http_deployment" };
    const index = symbolIndex([], [ref(1, api), ref(2, api), ref(3, api), ref(4, contracts)], HERE);
    // the whole line a string to the highlighter, as a YAML scalar or a JSON value is
    const string = { quiet: () => true, quoted: () => true };
    const spans = (line: number, text: string, where?: Parameters<typeof spansFor>[3]) =>
      spansFor(line, text, index, where).map((s) => `${text.slice(s.start, s.end)}→${s.key}`);
    expect(spans(1, "  - url: https://api.example.com", string)).toEqual([`https://api.example.com→${api.key}`]);
    expect(spans(2, '    { "url": "https://API.example.com/v2" },', string)).toEqual([`https://API.example.com→${api.key}`]);
    // its pieces are no names of it, on its own line or on an import's
    expect(spans(3, "com = example")).toEqual([]);
    expect(spans(4, 'import com from "@fixture/contracts";').map((l) => l.split("→")[0])).toEqual(["@fixture/contracts"]);
  });

  it("never links a name inside a comment or a string", () => {
    const text = SOURCE[2]!;
    const comment = text.indexOf("//");
    const tokens = marked(text, [{ from: comment, to: text.length, color: "var(--ra-code-token-comment)" }]);
    expect(linked(policyIndex(), 3, tokenRuns(tokens))).toEqual([`CourseRole→${ROLE}`]);
  });

  it("prefers the declaration a name stands for over a looser symbol, and never the module it comes from", () => {
    const shared = { key: "module:Thing", name: "Thing", type: "module" };
    const loose = { key: "sym:lib:src/a.ts:Thing:1", name: "Thing", type: "symbol", repository: "lib", path: "src/a.ts" };
    const thing = { key: "sym:lib:src/thing.ts:Thing:1", name: "Thing", type: "class", repository: "lib", path: "src/thing.ts" };
    const index = symbolIndex([], [ref(1, shared), ref(1, loose), ref(1, thing)], HERE);
    expect(spansFor(1, "new Thing()", index).map((s) => s.key)).toEqual([thing.key]);
  });

  it("links a name declared once in the file wherever it is written, and lists the lines", () => {
    const index = policyIndex();
    const lines = SOURCE.map((text, i) => ({ number: i + 1, keys: spansFor(i + 1, text, index).map((s) => s.key) }));
    const written = (key: string) => lines.filter((l) => l.keys.includes(key)).map((l) => l.number);
    expect(written(ROLE)).toEqual([1, 2, 3]);
    expect(written(CAN_EDIT)).toEqual([2]);
  });
});

describe("highlighted lines", () => {
  it("cuts a line at every token and every name, keeping each name's pieces together", () => {
    const text = "a.CourseRole()";
    const tokens = marked(text, [{ from: 2, to: 8, color: "var(--ra-code-token-function)" }]);
    const groups = fragmentGroups(text, tokens, [{ start: 2, end: 12, key: ROLE }]);
    expect(groups.map((g) => [g.key ?? null, g.fragments.map((f) => f.text)])).toEqual([
      [null, ["a."]],
      [ROLE, ["Course", "Role"]],
      [null, ["()"]],
    ]);
    expect(groups.flatMap((g) => g.fragments).map((f) => f.text).join("")).toBe(text);
  });

  it("reads a quoted string as a string, and a tag name as a name, though the theme colours both alike", () => {
    expect(isString({ content: '"@fixture/contracts"', color: "var(--ra-code-token-string-expression)" })).toBe(true);
    expect(isString({ content: "`a ${b}`", color: "var(--ra-code-token-string-expression)" })).toBe(true);
    expect(isString({ content: "Button", color: "var(--ra-code-token-string-expression)" })).toBe(false);
    expect(isString({ content: "'", color: "var(--ra-code-token-string)" })).toBe(true);
  });

  it("chooses a grammar by name, then by extension, and none for what it does not know", () => {
    expect(grammarFor("app/src/policy.ts")).toBe("typescript");
    expect(grammarFor("web/App.TSX")).toBe("tsx");
    expect(grammarFor("Dockerfile.prod")).toBe("dockerfile");
    expect(grammarFor("Gemfile")).toBe("ruby");
    expect(grammarFor("api/schema.graphqls")).toBe("graphql");
    expect(grammarFor("web/queries.gql")).toBe("graphql");
    expect(grammarFor("prisma/schema.prisma")).toBe("prisma");
    expect(grammarFor("LICENSE")).toBeNull();
    expect(grammarFor("notes.unknownext")).toBeNull();
  });

  it("colours an import's specifier so it is read as a string", async () => {
    // warm: a cold grammar's first line can run out of time on a loaded machine
    await tokenize("const warm = 1;", "typescript");
    const lines = await tokenize(SOURCE.join("\n"), "typescript");
    expect(lines).not.toBeNull();
    const runs = tokenRuns(lines![0]);
    const text = SOURCE[0]!;
    expect(runs.quoted(text.indexOf("@fixture"))).toBe(true);
    expect(runs.quiet(text.indexOf("CourseRole"))).toBe(false);
  });
});

// -- a trace ---------------------------------------------------------------------

function candidate(
  id: number,
  from: number,
  distance: number,
  name: string,
  repository: string,
  path: string,
  line: number,
  reading = "uses",
  extra: Partial<ImpactCandidate> = {},
): ImpactCandidate {
  return {
    id,
    from,
    distance,
    name,
    repository,
    path,
    entity: `sym:${repository}:${path}:${name}:${line}`,
    type: "function",
    basis: "edge",
    chain: "",
    step: { reading, edge: reading === "calls" ? "CALLS" : "USES", lines: [line, line], at: `${repository}/${path}` },
    ...extra,
  } as ImpactCandidate;
}

function impact(candidates: ImpactCandidate[], tests: ImpactCandidate[] = []): ImpactResponse {
  return {
    seed: { status: "resolved", term: ROLE, entity: ROLE, repository: "contracts", path: "src/roles.ts", lines: [1, 4], granularity: "declaration", type: "symbol", unit: "contracts:." },
    candidates,
    tests,
    summary: { policy: "p", reached: candidates.length + tests.length },
    context: {},
    freshness: {},
    investigation_id: "i",
  } as unknown as ImpactResponse;
}

const CHAIN = impact(
  [
    candidate(1, 0, 1, "canEdit", "app", "src/policy.ts", 3),
    candidate(2, 0, 1, "isOwner", "app", "src/aliases.ts", 4),
    candidate(3, 0, 1, "policyValue", "app", "src/aliases.ts", 6),
    candidate(4, 0, 1, "ok", "refusals", "ok/src/use.ts", 2),
    candidate(5, 1, 2, "guard", "app", "src/guard.ts", 3, "calls"),
    candidate(6, 2, 2, "audit", "app", "src/guard.ts", 9, "calls"),
  ],
  [candidate(7, 5, 3, "guard.test.ts", "app", "src/__tests__/guard.test.ts", 3, "uses", { type: "test" } as Partial<ImpactCandidate>)],
);

describe("a trace", () => {
  const graph = buildTrace(CHAIN);

  it("puts each step's distance in its own column, a card per file reached there", () => {
    expect(graph.columns.map((c) => [c.distance, c.cards.map((card) => `${card.repository}/${card.path}`)])).toEqual([
      [1, ["app/src/aliases.ts", "app/src/policy.ts", "refusals/ok/src/use.ts"]],
      [2, ["app/src/guard.ts"]],
      [3, ["app/src/__tests__/guard.test.ts"]],
    ]);
    const aliases = graph.columns[0]!.cards[0]!;
    expect(aliases.rows.map((r) => [r.candidate.name, r.line, r.reading])).toEqual([
      ["isOwner", 4, "uses"],
      ["policyValue", 6, "uses"],
    ]);
    expect(graph.columns[2]!.cards[0]!.test).toBe(true);
    expect(graph.listed).toBe(7);
  });

  it("names the repositories it stands in, the seed's first", () => {
    expect(graph.repositories).toEqual(["contracts", "app", "refusals"]);
    expect(graph.seed).toMatchObject({ name: "CourseRole", repository: "contracts", lines: [1, 4] });
  });

  it("draws one line per pair of cards and family, counting and naming the steps it carries", () => {
    const from = (id: string) => graph.edges.filter((e) => e.source === id).map((e) => [e.target, e.link.count, e.link.label]);
    expect(from(SEED_ID)).toHaveLength(3);
    const guard = graph.cardOf.get(rowId(5))!;
    // canEdit → guard and isOwner → audit leave different cards: two lines into guard.ts
    expect(graph.edges.filter((e) => e.target === guard).map((e) => [e.source, e.link.family, e.link.label])).toEqual([
      [graph.cardOf.get(rowId(1)), "code", "calls"],
      [graph.cardOf.get(rowId(2)), "code", "calls"],
    ]);
    const seedToAliases = graph.edges.find((e) => e.source === SEED_ID && e.target === graph.cardOf.get(rowId(2)))!;
    expect([seedToAliases.link.count, seedToAliases.link.label]).toEqual([2, "uses"]);
    // the line is the relationship's, and counts every step it carries by type
    expect(seedToAliases.id.endsWith(":uses")).toBe(true);
    expect(seedToAliases.link.types).toEqual([{ type: "USES", count: 2 }]);
    expect(relationOfRow(graph.rows.get(rowId(5))!)).toBe("calls");
  });

  it("follows a step back to the seed", () => {
    const test = graph.rows.get(rowId(7))!;
    expect(chainOf(graph, test).map((r) => r.candidate.name)).toEqual(["canEdit", "guard", "guard.test.ts"]);
  });

  it("splits where a step was read back into its repository and file, the longest name first", () => {
    expect(splitAt("app-web/src/a.ts", ["app", "app-web"])).toEqual({ repository: "app-web", path: "src/a.ts" });
    expect(splitAt("app/src/a.ts", ["app", "app-web"])).toEqual({ repository: "app", path: "src/a.ts" });
    expect(splitAt("elsewhere/a.ts", ["app"])).toBeNull();
    expect(splitAt(null, ["app"])).toBeNull();
  });

  it("reads units, boundaries and keys as a reader names them", () => {
    expect(unitLabel("contracts:.")).toBe("contracts");
    expect(unitLabel("refusals:ok")).toBe("refusals / ok");
    expect(boundaryLabel("contracts:. -> refusals:ok")).toBe("contracts → refusals / ok");
    expect(nameFromKey(ROLE)).toBe("CourseRole");
    expect(nameFromKey("sym:api:schema.graphql:Query.items(first:):4")).toBe("Query.items(first:)");
    expect(nameFromKey("file:app:src/policy.ts")).toBe("policy.ts");
    expect(nameFromKey("contracts:src/legacy/")).toBe("legacy");
  });

  it("never counts what changes as something a change reaches", () => {
    // a file whose six declarations nothing else uses: impact reaches the six, lists none
    const alone = { ...impact([]), summary: { policy: "p", reached: 6, returned: 0 }, omitted: { by_reason: {}, by_unit: {} } } as ImpactResponse;
    expect(reachOf(alone)).toEqual({ outside: 0, beyond: 0, within: 6 });
    // seven listed, two named only as a file, three past the limit, four of its own
    const cut = {
      ...CHAIN,
      unresolved: [{ entity: "file:app:a.ts" }, { entity: "file:app:b.ts" }],
      omitted: { by_reason: { budget: 3 }, by_unit: {} },
      summary: { policy: "p", reached: 16, returned: 7 },
    } as unknown as ImpactResponse;
    expect(reachOf(cut)).toEqual({ outside: 12, beyond: 3, within: 4 });
  });
});

function overlaps(a: { x: number; y: number; width: number; height: number }, b: typeof a): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

describe("a trace's layout", () => {
  function cards(graph: TraceGraph) {
    return graph.columns.flatMap((c) => c.cards);
  }

  it("sets the seed left of every column, and overlaps no card, frame or label", () => {
    const graph = buildTrace(CHAIN);
    const layout = layoutTrace(graph);
    const seed = layout.get(SEED_ID)!;
    const boxes = [
      seed,
      ...cards(graph).map((c) => layout.get(c.id)!),
      ...graph.columns.map((c) => layout.get(columnLabelId(c.distance))!),
    ];
    for (const card of cards(graph)) expect(layout.get(card.id)!.x).toBeGreaterThan(seed.x + seed.width);
    boxes.forEach((a, i) => boxes.slice(i + 1).forEach((b) => expect(overlaps(a, b)).toBe(false)));
    // frames hold their cards, and only cards of their repository
    for (const column of graph.columns) {
      for (const card of column.cards) {
        const frame = layout.get(traceFrameId(column.distance, card.repository ?? ""))!;
        const box = layout.get(card.id)!;
        expect(box.y).toBeGreaterThanOrEqual(frame.y);
        expect(box.y + box.height).toBeLessThanOrEqual(frame.y + frame.height);
      }
    }
  });

  it("frames nothing when the chain never leaves one repository", () => {
    const local = buildTrace(impact([candidate(1, 0, 1, "a", "contracts", "src/a.ts", 1), candidate(2, 1, 2, "b", "contracts", "src/b.ts", 2)]));
    expect(local.repositories).toEqual(["contracts"]);
    expect([...layoutTrace(local).keys()].some((k) => k.startsWith("rf:"))).toBe(false);
  });

  it("folds a card's rows past the limit into a count", () => {
    const many = buildTrace(impact(Array.from({ length: TRACE_ROWS + 3 }, (_, i) => candidate(i + 1, 0, 1, `f${i}`, "app", "src/x.ts", i + 1))));
    const card = many.columns[0]!.cards[0]!;
    expect(card.rows).toHaveLength(TRACE_ROWS + 3);
    expect(traceCardHeight(card)).toBe(
      traceCardHeight({ ...card, rows: card.rows.slice(0, TRACE_ROWS) }) + 28,
    );
  });
});

describe("going into a trace", () => {
  const file: Place = { repository: "app", path: "src/policy.ts" };

  it("is one deeper than where it was asked from, so the view zooms in", () => {
    const traced: Place = { ...file, trace: ROLE };
    expect(depth(traced)).toBe(depth(file) + 1);
    expect(direction(file, traced)).toBe("in");
    expect(direction(traced, file)).toBe("out");
    expect(placeKey(traced)).not.toBe(placeKey(file));
  });

  it("keeps the trace in the address", () => {
    expect(new URLSearchParams(search({ ...file, trace: ROLE }, "c:1")).get("trace")).toBe(ROLE);
    expect(new URLSearchParams(search(file, null)).has("trace")).toBe(false);
  });

  it("asks about a key as it is, and a directory as impact lists one", () => {
    expect(traceSeed({ ...file, trace: ROLE })).toBe(ROLE);
    expect(traceSeed({ repository: "contracts", path: "", trace: `${DIRECTORY_TRACE}src/legacy` })).toBe("contracts:src/legacy/");
    expect(traceSeed({ repository: null, path: "", trace: `${DIRECTORY_TRACE}src` })).toBeNull();
    expect(traceSeed(file)).toBeNull();
  });
});
