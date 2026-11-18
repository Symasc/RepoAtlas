import { describe, expect, it } from "vitest";

import type { EdgeType, RepositoryLink } from "@/lib/api/generated";
import { defaultExpanded, summaryId } from "@/lib/graph/districtLayout";
import { districts, type District } from "@/lib/graph/districts";
import { cardsOf, liftEdges, liftId, type FamilyTallies, type Lift } from "@/lib/graph/lift";
import { toFreshness } from "@/lib/model/freshness";
import { buildOverview, FAMILIES, repositoryId, type ViewEdge } from "@/lib/model/graph";
import type { RepositoryModel } from "@/lib/model/repositories";

import { ecosystem300 } from "./fixtures/ecosystem";

/**
 * Edge lifting: the rules on small hand-built worlds, one at a time, then the
 * identities that must hold on the 300-repository fixture at every expansion.
 */

// -- helpers ---------------------------------------------------------------------------

function repo(slug: string): RepositoryModel {
  return {
    slug,
    name: slug,
    branch: "main",
    headSha: null,
    files: 1,
    symbols: 1,
    relationships: 1,
    languages: [],
    manifests: [],
    lockfiles: [],
    lastIndexedAt: null,
    hue: 1,
    freshness: toFreshness({ state: "current_clean", depth: "live", repository_slug: slug }),
  };
}

/** `parent` includes `child`. */
function incl(parent: string, child: string): RepositoryLink {
  return {
    kind: "submodule",
    source: parent,
    target: child,
    count: 1,
    types: [{ type: "INCLUDES_REPOSITORY", count: 1 }],
    examples: [],
  };
}

/** Any other link, `source` to `target`, of the given kind and types. */
function link(
  kind: RepositoryLink["kind"],
  source: string,
  target: string,
  types: readonly (readonly [EdgeType, number])[],
  extra: Partial<RepositoryLink> = {},
): RepositoryLink {
  return {
    kind,
    source,
    target,
    count: types.reduce((sum, [, n]) => sum + n, 0),
    types: types.map(([type, count]) => ({ type, count })),
    examples: [],
    ...extra,
  };
}

/** An observed link of IMPORTS, `count` strong. */
function plain(source: string, target: string, count = 1): RepositoryLink {
  return link("observed", source, target, [["IMPORTS", count]]);
}

function edgesOf(slugs: readonly string[], links: readonly RepositoryLink[]): ViewEdge[] {
  return buildOverview(slugs.map(repo), links).edges;
}

function dist(id: string, slugs: readonly string[], part: District["part"] = null): District {
  return { id, kind: "group", label: id, members: slugs.map(repositoryId), root: null, part };
}

const idOf = (kind: string, source: string, target: string) => `${kind}:${source}->${target}`;

/** A seeded shuffle (a 32-bit LCG), so the order changes the same way on every run. */
function shuffled<T>(list: readonly T[], seed: number): T[] {
  let state = seed >>> 0;
  const next = () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state / 4294967296;
  };
  const out = [...list];
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(next() * (i + 1));
    [out[i], out[j]] = [out[j]!, out[i]!];
  }
  return out;
}

/** Everything a lift says, as plain data, for comparing two lifts. */
function snapshot(lift: Lift) {
  return {
    edges: lift.edges,
    internal: [...lift.internal],
    carriedBy: [...lift.carriedBy],
    cardOf: [...lift.cardOf],
    unplaced: lift.unplaced,
    nesting: lift.nesting,
  };
}

// -- one rule at a time ----------------------------------------------------------------

describe("lifting edges in small worlds", () => {
  // A = {a1, a2} collapsed, B = {b1} open
  const slugs = ["a1", "a2", "b1"];
  const A = dist("A", ["a1", "a2"]);
  const B = dist("B", ["b1"]);
  const collapsedA = new Set(["B"]);

  it("merges the links of one family between the same two cards into one line", () => {
    const edges = edgesOf(slugs, [plain("a1", "b1", 3), plain("a2", "b1", 4)]);
    const lift = liftEdges(edges, [A, B], collapsedA);
    expect(lift.edges).toHaveLength(1);
    const line = lift.edges[0]!;
    expect(line.id).toBe("lift:code:summary:A->repo:b1");
    expect(line.id).toBe(liftId("code", summaryId("A"), "repo:b1"));
    expect([line.source, line.target]).toEqual(["summary:A", "repo:b1"]);
    expect(line.link.count).toBe(7);
    expect(line.link.family).toBe("code");
    expect(line.from).toEqual([idOf("observed", "a1", "b1"), idOf("observed", "a2", "b1")]);
    expect(line.lifted).toBe(true);
    expect(line.scope).toBeUndefined();
    expect(lift.carriedBy.get(idOf("observed", "a1", "b1"))).toBe(line.id);
    expect(lift.carriedBy.get(idOf("observed", "a2", "b1"))).toBe(line.id);
  });

  it("never sums two families into one line", () => {
    const edges = edgesOf(slugs, [plain("a1", "b1", 3), link("http", "a1", "b1", [["CALLS_HTTP_ROUTE", 2]])]);
    const lift = liftEdges(edges, [A, B], collapsedA);
    expect(lift.edges.map((e) => [e.id, e.link.count])).toEqual([
      ["lift:code:summary:A->repo:b1", 3],
      ["lift:http:summary:A->repo:b1", 2],
    ]);
  });

  it("never merges the two directions", () => {
    const edges = edgesOf(slugs, [plain("a1", "b1", 3), plain("b1", "a1", 5)]);
    const lift = liftEdges(edges, [A, B], collapsedA);
    expect(lift.edges.map((e) => [e.id, e.link.count])).toEqual([
      ["lift:code:repo:b1->summary:A", 5],
      ["lift:code:summary:A->repo:b1", 3],
    ]);
    const back = lift.edges.find((e) => e.id === "lift:code:repo:b1->summary:A")!;
    expect([back.source, back.target]).toEqual(["repo:b1", "summary:A"]);
  });

  it("counts a link inside a collapsed district on the district, and draws nothing", () => {
    const edges = edgesOf(slugs, [plain("a1", "a2", 9)]);
    const lift = liftEdges(edges, [A, B], collapsedA);
    expect(lift.internal.get("A")).toEqual({ code: { links: 1, count: 9 } });
    expect(lift.internal.size).toBe(1);
    expect(lift.carriedBy.get(idOf("observed", "a1", "a2"))).toBeNull();
    expect(lift.edges).toEqual([]);
    expect(lift.nesting).toEqual({ links: 0, count: 0 });
  });

  it("counts an inclusion inside a collapsed district as internal, not as nesting", () => {
    const edges = edgesOf(slugs, [incl("a1", "a2")]);
    const lift = liftEdges(edges, [A, B], collapsedA);
    expect(lift.internal.get("A")).toEqual({ submodule: { links: 1, count: 1 } });
    expect(lift.nesting).toEqual({ links: 0, count: 0 });
    expect(lift.edges).toEqual([]);
  });

  it("counts an inclusion across districts as nesting, whatever is open, and never draws it", () => {
    const edges = edgesOf(slugs, [incl("b1", "a1")]);
    for (const open of [[], ["A"], ["B"], ["A", "B"]]) {
      const lift = liftEdges(edges, [A, B], new Set(open));
      expect(lift.nesting, open.join()).toEqual({ links: 1, count: 1 });
      expect(lift.edges, open.join()).toEqual([]);
      expect(lift.internal.size, open.join()).toBe(0);
      expect(lift.carriedBy.get(idOf("submodule", "b1", "a1")), open.join()).toBeNull();
    }
  });

  it("passes a link between open cards through whole, under its own id", () => {
    const named = { name: "p", ecosystem: "npm", declarations: 1, declared_at: "package.json" };
    const original = link("observed", "a1", "a2", [["IMPORTS", 2], ["CALLS", 1]], { examples: ["a1/x.ts:3"], packages: [named] });
    const edges = edgesOf(slugs, [original]);
    const lift = liftEdges(edges, [A, B], new Set(["A", "B"]));
    expect(lift.edges).toHaveLength(1);
    const line = lift.edges[0]!;
    expect(line.id).toBe(idOf("observed", "a1", "a2"));
    expect(line.lifted).toBe(false);
    expect(line.from).toEqual([line.id]);
    expect(line.link).toEqual(edges[0]!.link);
    expect(line.link).not.toBe(edges[0]!.link);
    expect(line.link.types).not.toBe(edges[0]!.link.types);
    expect(line.link.examples).not.toBe(edges[0]!.link.examples);
    expect(edges[0]!.link.packages).toEqual(["p"]);
    expect(line.link.packages).toEqual(["p"]);
    expect(line.link.packages).not.toBe(edges[0]!.link.packages);
    expect(lift.carriedBy.get(line.id)).toBe(line.id);
    expect(lift.internal.size).toBe(0);
  });

  it("keeps two parts of one district apart: a link between collapsed parts is a line", () => {
    const parts = [dist("T#1", ["t1"], { index: 1, of: 2 }), dist("T#2", ["t2"], { index: 2, of: 2 })];
    const edges = edgesOf(["t1", "t2"], [plain("t1", "t2", 2)]);
    const lift = liftEdges(edges, parts, new Set());
    expect(lift.edges.map((e) => e.id)).toEqual(["lift:code:summary:T#1->summary:T#2"]);
    expect(lift.internal.size).toBe(0);
  });

  it("lists an edge naming a repository in no district, or a self-link, as unplaced", () => {
    const edges = edgesOf(["a1", "a2", "ghost"], [plain("ghost", "a1"), plain("a1", "a2")]);
    const selfLink: ViewEdge = { ...edges[1]!, id: "observed:a1->a1", target: edges[1]!.source };
    const lift = liftEdges([...edges, selfLink], [A], new Set(["A"]));
    expect(lift.unplaced).toEqual(["observed:a1->a1", idOf("observed", "ghost", "a1")]);
    expect(lift.carriedBy.get(idOf("observed", "ghost", "a1"))).toBeNull();
    expect(lift.carriedBy.get("observed:a1->a1")).toBeNull();
    expect(lift.edges.map((e) => e.id)).toEqual([idOf("observed", "a1", "a2")]);
  });

  it("fails closed when a repository is a member of two districts", () => {
    const clash = [dist("A", ["a1", "a2"]), dist("B", ["a2"])];
    const edges = edgesOf(slugs, [plain("a1", "a2")]);
    expect(() => liftEdges(edges, clash, new Set())).toThrow("lift: repo:a2 is in A and B");
    expect(() => cardsOf(clash, new Set())).toThrow("lift: repo:a2 is in A and B");
  });

  it("names the two clashing districts in code-unit order, whatever order they arrive in", () => {
    const clash = [dist("A", ["a1", "a2"]), dist("B", ["a2"])];
    const swapped = [clash[1]!, clash[0]!];
    const edges = edgesOf(slugs, [plain("a1", "a2")]);
    const message = "lift: repo:a2 is in A and B";
    expect(() => liftEdges(edges, swapped, new Set())).toThrow(message);
    expect(() => cardsOf(swapped, new Set())).toThrow(message);
  });

  it("fails closed when two input edges share an id", () => {
    const edges = edgesOf(slugs, [plain("a1", "b1", 3)]);
    expect(() => liftEdges([...edges, edges[0]!], [A, B], collapsedA)).toThrow(`lift: duplicate edge id ${edges[0]!.id}`);
    expect(() => liftEdges([edges[0]!, { ...edges[0]! }], [A, B], new Set(["A", "B"]))).toThrow("lift: duplicate edge id");
  });

  it("merges the types of the links under a line, strongest first, ties by type name", () => {
    const edges = edgesOf(slugs, [
      link("observed", "a1", "b1", [["IMPORTS", 3]]),
      link("observed", "a2", "b1", [["CALLS", 4], ["IMPORTS", 2]]),
    ]);
    const line = liftEdges(edges, [A, B], collapsedA).edges[0]!;
    expect(line.link.types).toEqual([{ type: "IMPORTS", count: 5 }, { type: "CALLS", count: 4 }]);
    expect(line.link.count).toBe(9);

    const tied = edgesOf(slugs, [
      link("observed", "a1", "b1", [["IMPORTS", 2]]),
      link("observed", "a2", "b1", [["CALLS", 2]]),
    ]);
    expect(liftEdges(tied, [A, B], collapsedA).edges[0]!.link.types).toEqual([
      { type: "CALLS", count: 2 },
      { type: "IMPORTS", count: 2 },
    ]);
  });

  it("keeps the first six distinct examples, taken in member-id order", () => {
    const edges = edgesOf(slugs, [
      link("observed", "a2", "b1", [["IMPORTS", 1]], { examples: ["x4", "x5", "x6", "x7", "x8"] }),
      link("observed", "a1", "b1", [["IMPORTS", 1]], { examples: ["x1", "x2", "x3", "x4"] }),
    ]);
    const line = liftEdges(edges, [A, B], collapsedA).edges[0]!;
    expect(line.link.examples).toEqual(["x1", "x2", "x3", "x4", "x5", "x6"]);
  });

  it("joins package names without repeats, in first-seen order", () => {
    const named = (name: string) => ({ name, ecosystem: "npm", declarations: 1, declared_at: "package.json" });
    const edges = edgesOf(slugs, [
      link("dependency", "a1", "b1", [["DEPENDS_ON", 1]], { packages: [named("p"), named("q")] }),
      link("dependency", "a2", "b1", [["DEPENDS_ON", 1]], { packages: [named("q"), named("r")] }),
    ]);
    const line = liftEdges(edges, [A, B], collapsedA).edges[0]!;
    expect(line.id).toBe("lift:dependency:summary:A->repo:b1");
    expect(line.link.packages).toEqual(["p", "q", "r"]);
  });

  it("draws a configuration link apart from code, and a nested link as a line", () => {
    const edges = edgesOf(slugs, [
      link("observed", "a1", "b1", [["READS", 2]]),
      plain("a2", "b1", 1),
      link("nested", "a1", "b1", [], { count: 1, nested_path: "vendor/b1" }),
    ]);
    const lift = liftEdges(edges, [A, B], collapsedA);
    expect(lift.edges.map((e) => [e.id, e.link.family, e.link.count])).toEqual([
      ["lift:code:summary:A->repo:b1", "code", 1],
      ["lift:config:summary:A->repo:b1", "config", 2],
      ["lift:nested:summary:A->repo:b1", "nested", 1],
    ]);
    expect(lift.nesting).toEqual({ links: 0, count: 0 });
    expect(lift.edges[2]!.link.nestedPath).toBe("vendor/b1");
    expect(lift.edges[0]!.link.nestedPath).toBeUndefined();
  });

  it("keeps no nested path on a line that merges two nested links", () => {
    const edges = edgesOf(slugs, [
      link("nested", "a1", "b1", [], { count: 1, nested_path: "vendor/b1" }),
      link("nested", "a2", "b1", [], { count: 1, nested_path: "third_party/b1" }),
    ]);
    expect(edges.every((e) => e.link.nestedPath !== undefined)).toBe(true);
    const lift = liftEdges(edges, [A, B], collapsedA);
    expect(lift.edges).toHaveLength(1);
    const line = lift.edges[0]!;
    expect(line.id).toBe("lift:nested:summary:A->repo:b1");
    expect(line.from).toHaveLength(2);
    expect(line.link.count).toBe(2);
    expect(line.link.nestedPath).toBeUndefined();
  });

  it("names every member's card: itself when its district is open, the summary when not", () => {
    const cards = cardsOf([A, B], collapsedA);
    expect([...cards]).toEqual([["repo:a1", "summary:A"], ["repo:a2", "summary:A"], ["repo:b1", "repo:b1"]]);
    expect([...cardsOf([A, B], new Set(["A", "B"]))]).toEqual([
      ["repo:a1", "repo:a1"],
      ["repo:a2", "repo:a2"],
      ["repo:b1", "repo:b1"],
    ]);
  });

  it("leaves its inputs unchanged", () => {
    const edges = edgesOf(slugs, [
      plain("a1", "b1", 3),
      plain("a2", "b1", 4),
      plain("a1", "a2", 2),
      incl("b1", "a1"),
    ]);
    const ds = [A, B];
    const expanded = new Set(["B"]);
    const before = structuredClone({ edges, ds, expanded: [...expanded] });
    liftEdges(edges, ds, expanded);
    expect({ edges, ds, expanded: [...expanded] }).toEqual(before);
    // a lifted line's link is its own: changing it changes no input
    const line = liftEdges(edges, ds, expanded).edges[0]!;
    line.link.types.push({ type: "CALLS", count: 1 });
    line.link.examples.push("changed");
    expect({ edges, ds, expanded: [...expanded] }).toEqual(before);
  });

  it("gives the same lift whatever order edges and districts arrive in", () => {
    const world = ["a1", "a2", "b1", "c1", "c2", "ghost"];
    const links = [
      plain("a1", "b1", 3),
      plain("a2", "b1", 4),
      link("http", "a1", "b1", [["CALLS_HTTP_ROUTE", 2]]),
      plain("b1", "c1", 1),
      plain("c1", "c2", 6),
      incl("b1", "a1"),
      incl("c1", "c2"),
      plain("ghost", "a1", 1),
      plain("a2", "c2", 5),
    ];
    const edges = edgesOf(world, links);
    const ds = [A, B, dist("C", ["c1", "c2"])];
    for (const open of [[], ["B"], ["C"], ["A", "C"], ["A", "B", "C"]]) {
      const base = snapshot(liftEdges(edges, ds, new Set(open)));
      for (const seed of [1, 2, 3]) {
        const other = liftEdges(shuffled(edges, seed), shuffled(ds, seed + 100), new Set(open));
        expect(snapshot(other), `${open.join()} seed ${seed}`).toEqual(base);
      }
    }
  });
});

// -- the fixture -----------------------------------------------------------------------

describe("lifting the edges of the 300-repository fixture", () => {
  const { repositories, links } = ecosystem300();
  const edges = buildOverview(repositories, links).edges;
  const ds = districts(repositories, edges);
  const byId = new Map(edges.map((e) => [e.id, e]));
  const VIEWPORTS = [
    { width: 1440, height: 810 },
    { width: 1920, height: 1080 },
    { width: 2560, height: 1440 },
  ] as const;

  /** A seeded subset of the districts, half of them open on average: a cut district can be partly open. */
  function subset(seed: number): Set<string> {
    let state = seed >>> 0;
    const next = () => {
      state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
      return state / 4294967296;
    };
    return new Set(ds.filter(() => next() < 0.5).map((d) => d.id));
  }

  const states: [string, ReadonlySet<string>][] = [
    ["nothing open", new Set<string>()],
    ...VIEWPORTS.map((v): [string, ReadonlySet<string>] => [`default at ${v.width}x${v.height}`, defaultExpanded(ds, v).expanded]),
    ["everything open", new Set(ds.map((d) => d.id))],
    ...Array.from({ length: 20 }, (_, i): [string, ReadonlySet<string>] => [`random subset ${i + 1}`, subset(i + 1)]),
  ];

  const sum = (xs: readonly number[]) => xs.reduce((a, b) => a + b, 0);

  it("starts from the fixture's own numbers", () => {
    expect(new Set(edges.map((e) => e.id)).size).toBe(edges.length);
    expect(edges.filter((e) => e.link.family === "submodule")).toHaveLength(62 + 36 + 39 + 2);
    expect(edges.filter((e) => e.link.family === "submodule")).toHaveLength(139);
  });

  it("conserves every family's links and counts, at every expansion", () => {
    for (const [name, expanded] of states) {
      const lift = liftEdges(edges, ds, expanded);
      expect(lift.unplaced, name).toEqual([]);
      for (const family of FAMILIES) {
        const input = edges.filter((e) => e.link.family === family);
        const drawn = lift.edges.filter((e) => e.link.family === family);
        const inside = [...lift.internal.values()].map((t: FamilyTallies) => t[family] ?? { links: 0, count: 0 });
        const unplaced = lift.unplaced.map((id) => byId.get(id)!).filter((e) => e.link.family === family);
        const nested = family === "submodule" ? lift.nesting : { links: 0, count: 0 };
        expect(input.length, `${name}: ${family} links`).toBe(
          sum(drawn.map((e) => e.from.length)) + sum(inside.map((t) => t.links)) + nested.links + unplaced.length,
        );
        expect(sum(input.map((e) => e.link.count)), `${name}: ${family} count`).toBe(
          sum(drawn.map((e) => e.link.count)) + sum(inside.map((t) => t.count)) + nested.count + sum(unplaced.map((e) => e.link.count)),
        );
      }
    }
  });

  it("carries each link on the line between its ends' visible cards", () => {
    for (const [name, expanded] of states) {
      const lift = liftEdges(edges, ds, expanded);
      expect(lift.carriedBy.size, name).toBe(edges.length);
      const lines = new Map(lift.edges.map((l) => [l.id, l]));
      expect(lines.size, name).toBe(lift.edges.length);
      for (const [id, line] of lift.carriedBy) {
        if (line === null) continue;
        const input = byId.get(id)!;
        const drawn = lines.get(line);
        expect(drawn, `${name}: ${id}`).toBeDefined();
        expect(drawn!.link.family, `${name}: ${id}`).toBe(input.link.family);
        expect(drawn!.source, `${name}: ${id}`).toBe(lift.cardOf.get(input.source));
        expect(drawn!.target, `${name}: ${id}`).toBe(lift.cardOf.get(input.target));
        expect(drawn!.from, `${name}: ${id}`).toContain(id);
      }
      // the lines' `from` lists are disjoint, and together are exactly the carried links
      const carried = [...lift.carriedBy].filter(([, line]) => line !== null).map(([id]) => id);
      expect(lift.edges.flatMap((l) => l.from).sort(), name).toEqual(carried.sort());
      expect(new Set(lift.edges.map((l) => `${l.link.family}\u0000${l.source}\u0000${l.target}`)).size, name).toBe(lift.edges.length);
      for (const l of lift.edges) {
        expect(l.link.family, `${name}: ${l.id}`).not.toBe("submodule");
        expect(l.source, `${name}: ${l.id}`).not.toBe(l.target);
        expect(l.lifted, `${name}: ${l.id}`).toBe(l.id.startsWith("lift:"));
        expect(l.from.length, `${name}: ${l.id}`).toBeGreaterThan(0);
      }
    }
  });

  it("counts as internal exactly the links inside each collapsed district", () => {
    for (const [name, expanded] of states) {
      const lift = liftEdges(edges, ds, expanded);
      for (const d of ds) {
        if (expanded.has(d.id)) {
          expect(lift.internal.has(d.id), `${name}: ${d.id} is open`).toBe(false);
          continue;
        }
        const members = new Set(d.members);
        const expected: Record<string, { links: number; count: number }> = {};
        for (const e of edges) {
          if (!members.has(e.source) || !members.has(e.target)) continue;
          const cell = (expected[e.link.family] ??= { links: 0, count: 0 });
          cell.links += 1;
          cell.count += e.link.count;
        }
        expect(lift.internal.get(d.id) ?? {}, `${name}: ${d.id}`).toEqual(expected);
      }
    }
  });

  it("with everything open, draws every link but the inclusions as itself and nests the rest", () => {
    const lift = liftEdges(edges, ds, new Set(ds.map((d) => d.id)));
    expect(lift.internal.size).toBe(0);
    expect(lift.nesting.links).toBe(139);
    const expected = edges.filter((e) => e.link.family !== "submodule");
    expect(lift.edges.map((l) => l.id).sort()).toEqual(expected.map((e) => e.id).sort());
    for (const line of lift.edges) {
      const original = byId.get(line.id)!;
      expect(line.lifted, line.id).toBe(false);
      expect(line.from, line.id).toEqual([line.id]);
      expect([line.source, line.target], line.id).toEqual([original.source, original.target]);
      expect(line.link, line.id).toEqual(original.link);
    }
  });

  it("with nothing open, draws only between summaries", () => {
    const lift = liftEdges(edges, ds, new Set());
    expect(lift.edges.length).toBeGreaterThan(0);
    for (const line of lift.edges) {
      expect(line.source.startsWith("summary:"), line.id).toBe(true);
      expect(line.target.startsWith("summary:"), line.id).toBe(true);
    }
  });

  it("is the same twice, and the same whatever order the world arrives in", () => {
    for (const [name, expanded] of states) {
      const base = snapshot(liftEdges(edges, ds, expanded));
      expect(snapshot(liftEdges(edges, ds, expanded)), name).toEqual(base);
      for (const seed of [1, 2, 3]) {
        const other = liftEdges(shuffled(edges, seed), shuffled(ds, seed + 100), expanded);
        expect(snapshot(other), `${name}, seed ${seed}`).toEqual(base);
      }
    }
  });

  it("sorts its keys and lines in code-unit order", () => {
    const lift = liftEdges(edges, ds, new Set());
    for (const keys of [[...lift.internal.keys()], [...lift.carriedBy.keys()], [...lift.cardOf.keys()], lift.unplaced]) {
      expect(keys).toEqual([...keys].sort());
    }
    const cmp = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);
    const order = lift.edges.map((l) => [FAMILIES.indexOf(l.link.family), l.source, l.target] as const);
    expect(order).toEqual([...order].sort((a, b) => a[0] - b[0] || cmp(a[1], b[1]) || cmp(a[2], b[2])));
    expect(lift.cardOf.size).toBe(sum(ds.map((d) => d.members.length)));
  });
});
