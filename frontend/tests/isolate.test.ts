import { describe, expect, it } from "vitest";

import type { RepositoryLink } from "@/lib/api/generated";
import { districts, wholeId, type District } from "@/lib/graph/districts";
import { districtLinks, isolable, isolate, wholeLabel } from "@/lib/graph/isolate";
import { liftEdges } from "@/lib/graph/lift";
import { toFreshness } from "@/lib/model/freshness";
import { buildOverview, repositoryId, type ViewEdge } from "@/lib/model/graph";
import type { RepositoryModel } from "@/lib/model/repositories";

import { ecosystem300 } from "./fixtures/ecosystem";

/**
 * Isolating a district: which districts are linked to it, on small hand-built
 * worlds one rule at a time, then the identities that must hold on the
 * 300-repository fixture for every district there is.
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

/** An observed link of IMPORTS, `source` to `target`. */
function plain(source: string, target: string): RepositoryLink {
  return { kind: "observed", source, target, count: 1, types: [{ type: "IMPORTS", count: 1 }], examples: [] };
}

function edgesOf(slugs: readonly string[], links: readonly RepositoryLink[]): ViewEdge[] {
  return buildOverview(slugs.map(repo), links).edges;
}

function dist(id: string, slugs: readonly string[], part: District["part"] = null): District {
  return { id, kind: "group", label: id, members: slugs.map(repositoryId), root: null, part };
}

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

const sortedIds = (list: Iterable<string>) => [...list].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));

// -- the name of a whole ---------------------------------------------------------------

describe("the name of a whole", () => {
  it("cuts a part's label at its last separator, not its first", () => {
    const part: District = { ...dist("X#2", ["x2"], { index: 2, of: 3 }), label: "a · b · 2 of 3" };
    expect(wholeLabel(part)).toBe("a · b");
  });

  it("leaves the label of a district that was not cut alone, whatever it holds", () => {
    expect(wholeLabel(dist("Y", ["y"]))).toBe("Y");
    expect(wholeLabel({ ...dist("Y", ["y"]), label: "t1-00 · vendor" })).toBe("t1-00 · vendor");
  });

  it("leaves a part's label alone when it has no separator to cut at", () => {
    expect(wholeLabel({ ...dist("X#1", ["x1"], { index: 1, of: 2 }), label: "plain" })).toBe("plain");
  });
});

// -- one rule at a time ----------------------------------------------------------------

describe("isolating in small worlds", () => {
  const A = dist("A", ["a"]);
  const B = dist("B", ["b"]);
  const C = dist("C", ["c"]);
  const D = dist("D", ["d"]);
  const E = dist("E", ["e"]);

  it("resolves a whole to itself, a part to its whole, and anything else to nothing", () => {
    const parts = [dist("X#1", ["x1"], { index: 1, of: 2 }), dist("X#2", ["x2"], { index: 2, of: 2 })];
    const ds = [A, ...parts];
    const links = districtLinks(ds, edgesOf(["a", "x1", "x2"], [plain("a", "x1")]));
    expect(isolate(ds, links, "A")!.focus).toBe("A");
    expect(isolate(ds, links, "X")!.focus).toBe("X");
    expect(isolate(ds, links, "X#2")!.focus).toBe("X");
    expect(isolate(ds, links, "X#1")!.focus).toBe("X");
    expect(isolate(ds, links, "nope")).toBeNull();
    expect(isolate(ds, links, "")).toBeNull();
    expect(isolate(ds, links, "district:A")).toBeNull();
  });

  it("names the neighbours by any link, an inclusion too, and not the districts two hops away", () => {
    const ds = [A, B, C, D, E];
    const edges = edgesOf(["a", "b", "c", "d", "e"], [plain("a", "b"), plain("b", "c"), incl("a", "d")]);
    const iso = isolate(ds, districtLinks(ds, edges), "A")!;
    expect(iso.core).toEqual(["A"]);
    expect(iso.neighbours).toEqual(["B", "D"]);
    expect(iso.hidden).toEqual(["C", "E"]);
    expect(iso.label).toBe("A");
  });

  it("counts a link that only comes in as a link to the district", () => {
    const ds = [A, B, C];
    const edges = edgesOf(["a", "b", "c"], [plain("b", "a")]);
    const iso = isolate(ds, districtLinks(ds, edges), "A")!;
    expect(iso.neighbours).toEqual(["B"]);
    expect(iso.hidden).toEqual(["C"]);
    // and the other way round, from B
    expect(isolate(ds, districtLinks(ds, edges), "B")!.neighbours).toEqual(["A"]);
  });

  it("isolates a cut district whole: its parts are the core, never each other's neighbours", () => {
    const parts = [dist("X#1", ["x1"], { index: 1, of: 2 }), dist("X#2", ["x2"], { index: 2, of: 2 })];
    const Y = dist("Y", ["y"]);
    const Z = dist("Z", ["z"]);
    const ds = [Y, ...parts, Z];
    const edges = edgesOf(["x1", "x2", "y", "z"], [plain("x1", "x2"), plain("x2", "y")]);
    const links = districtLinks(ds, edges);
    expect(links.get("X#1")).toEqual(new Set(["X#2"]));
    for (const id of ["X", "X#1", "X#2"]) {
      const iso = isolate(ds, links, id)!;
      expect(iso.core, id).toEqual(["X#1", "X#2"]);
      expect(iso.neighbours, id).toEqual(["Y"]);
      expect(iso.hidden, id).toEqual(["Z"]);
    }
  });

  it("makes no neighbour of a self-link, a link inside a district, or a link with an end in no district", () => {
    const AA = dist("AA", ["a", "a2"]);
    const edges = edgesOf(["a", "a2", "b", "ghost"], [plain("a", "a2"), plain("ghost", "a"), plain("b", "ghost")]);
    const selfLink: ViewEdge = { ...edges[0]!, id: "observed:a->a", source: repositoryId("a"), target: repositoryId("a") };
    const ds = [AA, B];
    const links = districtLinks(ds, [...edges, selfLink]);
    expect(links.get("AA")).toEqual(new Set());
    expect(links.get("B")).toEqual(new Set());
    const iso = isolate(ds, links, "AA")!;
    expect(iso.neighbours).toEqual([]);
    expect(iso.hidden).toEqual(["B"]);
  });

  it("links every district, in both directions, and none to itself", () => {
    const ds = [A, B, C];
    const links = districtLinks(ds, edgesOf(["a", "b", "c"], [plain("a", "b")]));
    expect([...links.keys()]).toEqual(["A", "B", "C"]);
    expect(links.get("A")).toEqual(new Set(["B"]));
    expect(links.get("B")).toEqual(new Set(["A"]));
    expect(links.get("C")).toEqual(new Set());
  });

  it("fails closed when a repository is a member of two districts", () => {
    const clash = [dist("A", ["a1", "a2"]), dist("B", ["a2"])];
    const edges = edgesOf(["a1", "a2"], [plain("a1", "a2")]);
    expect(() => districtLinks(clash, edges)).toThrow("isolate: repo:a2 is in A and B");
    expect(() => districtLinks([clash[1]!, clash[0]!], edges)).toThrow("isolate: repo:a2 is in A and B");
  });

  it("is isolable only when isolating hides something", () => {
    // A is linked to everything else; B and C are linked to A alone
    const ds = [A, B, C];
    const edges = edgesOf(["a", "b", "c"], [plain("a", "b"), plain("a", "c")]);
    const links = districtLinks(ds, edges);
    expect(isolate(ds, links, "A")!.hidden).toEqual([]);
    expect([...isolable(ds, links)].sort()).toEqual(["B", "C"]);
  });

  it("is isolable by its whole: a cut district is one entry", () => {
    const parts = [dist("X#1", ["x1"], { index: 1, of: 2 }), dist("X#2", ["x2"], { index: 2, of: 2 })];
    const ds = [A, ...parts];
    const links = districtLinks(ds, edgesOf(["a", "x1", "x2"], [plain("x1", "x2")]));
    expect([...isolable(ds, links)].sort()).toEqual(["A", "X"]);
  });
});

// -- the fixture -----------------------------------------------------------------------

describe("isolating in the 300-repository fixture", () => {
  const { repositories, links: given } = ecosystem300();
  const edges = buildOverview(repositories, given).edges;
  const ds = districts(repositories, edges);
  const links = districtLinks(ds, edges);
  const wholes = [...new Set(ds.map(wholeId))];
  const ownerOf = new Map(ds.flatMap((d) => d.members.map((m) => [m, d.id] as const)));

  it("starts from a fixture with cut districts and linked ones", () => {
    expect(wholes.length).toBeGreaterThan(5);
    expect(wholes.length).toBeLessThan(ds.length);
  });

  it("splits every district into its core, its neighbours and the rest: no overlap, none missing", () => {
    for (const whole of wholes) {
      const iso = isolate(ds, links, whole)!;
      expect(iso.focus, whole).toBe(whole);
      expect(iso.core.length, whole).toBeGreaterThan(0);
      const all = [...iso.core, ...iso.neighbours, ...iso.hidden];
      expect(new Set(all).size, whole).toBe(all.length);
      expect(sortedIds(all), whole).toEqual(sortedIds(ds.map((d) => d.id)));
      expect(iso.core, whole).toEqual(ds.filter((d) => wholeId(d) === whole).map((d) => d.id));
    }
  });

  it("finds as neighbours the districts at the other end of the lines that touch the core", () => {
    const none = liftEdges(edges, ds, new Set());
    let withLines = 0;
    for (const whole of wholes) {
      const iso = isolate(ds, links, whole)!;
      const core = new Set(iso.core);
      const touching = new Set<string>();
      for (const line of none.edges) {
        const a = line.source.slice("summary:".length);
        const b = line.target.slice("summary:".length);
        if (core.has(a) && !core.has(b)) touching.add(b);
        if (core.has(b) && !core.has(a)) touching.add(a);
      }
      // the lines are every family but inclusion: a neighbour joined by inclusions alone has none
      const joinedOnlyByInclusion = iso.neighbours.filter(
        (n) =>
          !edges.some((e) => {
            const [from, to] = [ownerOf.get(e.source), ownerOf.get(e.target)];
            return e.link.family !== "submodule" && ((core.has(from!) && to === n) || (core.has(to!) && from === n));
          }),
      );
      expect(sortedIds(touching), whole).toEqual(sortedIds(iso.neighbours.filter((n) => !joinedOnlyByInclusion.includes(n))));
      if (touching.size > 0) withLines += 1;
    }
    expect(withLines).toBeGreaterThan(0);
  });

  it("gives the same answer whatever order districts and edges arrive in, in the order of the districts given", () => {
    for (const seed of [1, 2, 3]) {
      const ds2 = shuffled(ds, seed);
      const links2 = districtLinks(ds2, shuffled(edges, seed + 100));
      for (const whole of wholes) {
        const base = isolate(ds, links, whole)!;
        const other = isolate(ds2, links2, whole)!;
        expect(other.focus, `${whole}, seed ${seed}`).toBe(base.focus);
        expect(other.label, `${whole}, seed ${seed}`).toBe(base.label);
        for (const key of ["core", "neighbours", "hidden"] as const) {
          expect(new Set(other[key]), `${whole} ${key}, seed ${seed}`).toEqual(new Set(base[key]));
          expect(other[key], `${whole} ${key}, seed ${seed}`).toEqual(ds2.filter((d) => base[key].includes(d.id)).map((d) => d.id));
        }
      }
    }
  });

  it("offers isolation to exactly the wholes that leave something out", () => {
    const offered = isolable(ds, links);
    expect([...offered].sort()).toEqual(wholes.filter((w) => isolate(ds, links, w)!.hidden.length > 0).sort());
    expect(offered.size).toBeGreaterThan(0);
  });
});
