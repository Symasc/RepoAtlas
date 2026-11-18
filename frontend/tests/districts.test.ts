import { describe, expect, it } from "vitest";

import type { RepositoryLink } from "@/lib/api/generated";
import {
  DISTRICT_MAX,
  HUB_MIN,
  HUB_SHARE,
  MIN_DIR_GROUP,
  MIN_GROUP,
  WIDE_ROOT,
  districts,
  hubThreshold,
  oversize,
  type District,
} from "@/lib/graph/districts";
import { toFreshness } from "@/lib/model/freshness";
import { buildOverview, repositoryId } from "@/lib/model/graph";
import type { RepositoryModel } from "@/lib/model/repositories";

import { ecosystem300, FIXTURE_PARTS } from "./fixtures/ecosystem";

/**
 * The district rules, on the 300-repository fixture (its numbers are the
 * fixture's, frozen) and on small hand-built worlds, one rule at a time.
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

/** `parent` includes `child`, stated at `path` when given. */
function incl(parent: string, child: string, path?: string): RepositoryLink {
  return {
    kind: "submodule",
    source: parent,
    target: child,
    count: 1,
    types: [{ type: "INCLUDES_REPOSITORY", count: 1 }],
    examples: [],
    ...(path === undefined
      ? {}
      : { inclusions: [{ declared_at: ".gitmodules", format: "gitmodules", method: "path" as const, path }], inclusions_total: 1 }),
  };
}

/** Any other link, `source` to `target`. */
function plain(source: string, target: string, count = 1): RepositoryLink {
  return { kind: "observed", source, target, count, types: [{ type: "IMPORTS", count }], examples: [] };
}

function world(slugs: readonly string[], links: readonly RepositoryLink[]) {
  const repositories = slugs.map(repo);
  return { repositories, edges: buildOverview(repositories, links).edges };
}

const ids = (...slugs: string[]) => slugs.map(repositoryId);
const find = (ds: readonly District[], id: string) => ds.find((d) => d.id === id);
const sizes = (ds: readonly District[]) => ds.map((d) => [d.id, d.members.length]);

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

/** A root `r` including children `c01`, `c02`, … with the given directories, `dir` "" meaning a bare name. */
function wide(spec: readonly (readonly [string, number])[]) {
  const children: string[] = [];
  const links: RepositoryLink[] = [];
  for (const [dir, n] of spec) {
    for (let i = 0; i < n; i++) {
      const slug = `c${String(children.length + 1).padStart(2, "0")}`;
      children.push(slug);
      links.push(incl("r", slug, dir === "" ? slug : `${dir}/${slug}`));
    }
  }
  return world(["r", ...children], links);
}

// -- the fixture -----------------------------------------------------------------------

describe("districts of the 300-repository fixture", () => {
  const { repositories, links } = ecosystem300();
  const edges = buildOverview(repositories, links).edges;
  const everyone = repositories.map((r) => repositoryId(r.slug)).sort();
  const unbound = districts(repositories, edges, { bound: false });
  const bound = districts(repositories, edges);
  const groupId = unbound.find((d) => d.kind === "group")!.id;

  it("keeps the constants the rules are stated with", () => {
    expect([HUB_MIN, HUB_SHARE, WIDE_ROOT, MIN_DIR_GROUP, MIN_GROUP, DISTRICT_MAX]).toEqual([8, 0.1, 24, 3, 4, 24]);
    expect(hubThreshold(260)).toBe(26);
    expect(hubThreshold(81)).toBe(9);
    expect(hubThreshold(80)).toBe(8);
    expect(hubThreshold(110)).toBe(11);
    expect(hubThreshold(20)).toBe(HUB_MIN);
  });

  it("puts every repository in exactly one district, cut or not", () => {
    for (const ds of [unbound, bound]) {
      const members = ds.flatMap((d) => d.members);
      expect(new Set(members).size).toBe(members.length);
      expect([...members].sort()).toEqual(everyone);
      for (const d of ds) expect(d.members.length, d.id).toBeGreaterThan(0);
    }
    for (const d of unbound) expect(d.part, d.id).toBeNull();
  });

  it("gives every district its own id, cut or not", () => {
    for (const ds of [unbound, bound]) {
      const idList = ds.map((d) => d.id);
      expect(new Set(idList).size).toBe(idList.length);
    }
  });

  it("uncut: three hubs, three families, the mesh, the small groups and the unlinked", () => {
    const leader = find(unbound, groupId)!.members[0]!;
    expect(sizes(unbound)).toEqual([
      ["district:shared", 3],
      ["district:tree:t1-00", 63],
      ["district:tree:t3-00", 40],
      ["district:tree:t2-00", 1],
      ["district:tree:t2-00/modules%2Fhal", 18],
      ["district:tree:t2-00/modules%2Flib", 8],
      ["district:tree:t2-00/modules%2Fcrypto", 5],
      ["district:tree:t2-00/tools", 3],
      ["district:tree:t2-00:other", 2],
      [`district:group:${leader.slice("repo:".length)}`, 88],
      ["district:small", 29],
      ["district:unlinked", 40],
    ]);
    expect(unbound.some((d) => d.kind === "loose")).toBe(false);
  });

  it("shares only the three hubs, never a library", () => {
    const shared = find(unbound, "district:shared")!;
    expect([...shared.members].sort()).toEqual(ids(...FIXTURE_PARTS.hubs));
    for (const library of FIXTURE_PARTS.libraries) expect(shared.members).not.toContain(repositoryId(library));
    expect(shared).toMatchObject({ kind: "shared", label: "Used by many", root: null, part: null });
  });

  it("keeps T1 whole, and T3 whole with its shared child and its loop", () => {
    const t1 = find(unbound, "district:tree:t1-00")!;
    expect([...t1.members].sort()).toEqual(ids(...FIXTURE_PARTS.t1));
    expect(t1.members.slice(0, 3)).toEqual(ids("t1-00", "t1-01", "t1-03"));
    expect(t1).toMatchObject({ kind: "tree", label: "t1-00", root: "repo:t1-00" });

    const t3 = find(unbound, "district:tree:t3-00")!;
    expect([...t3.members].sort()).toEqual(ids(...FIXTURE_PARTS.t3));
    for (const slug of ["t3-09", "t3-39", "t3-01"]) expect(t3.members).toContain(repositoryId(slug));
    expect(t3.root).toBe("repo:t3-00");
  });

  it("splits T2's wide root by the directories its children are included in", () => {
    const family = unbound.filter((d) => d.root === "repo:t2-00");
    expect(family.map((d) => d.label)).toEqual([
      "t2-00",
      "t2-00 · modules/hal",
      "t2-00 · modules/lib",
      "t2-00 · modules/crypto",
      "t2-00 · tools",
      "t2-00 · other",
    ]);
    expect(family[0]!.members).toEqual(ids("t2-00"));
    // each child, then what hangs below it: t2-01 holds the whole chain and two more
    expect(find(unbound, "district:tree:t2-00/modules%2Fhal")!.members.slice(0, 8)).toEqual(
      ids("t2-01", "t2-31", "t2-32", "t2-33", "t2-34", "t2-35", "t2-36", "t2-02"),
    );
    // bootloader holds only two children: too few for a district of its own
    expect(find(unbound, "district:tree:t2-00:other")!.members).toEqual(ids("t2-26", "t2-27"));
    expect(find(unbound, "district:tree:t2-00/tools")!.members).toEqual(ids("t2-28", "t2-29", "t2-30"));
    expect([...family.flatMap((d) => d.members)].sort()).toEqual(ids(...FIXTURE_PARTS.t2));
  });

  it("makes one group of the mesh, led by its most connected repository", () => {
    const group = find(unbound, groupId)!;
    expect([...group.members].sort()).toEqual(ids(...FIXTURE_PARTS.mesh));
    const leader = group.members[0]!.slice("repo:".length);
    expect(group.id).toBe(`district:group:${leader}`);
    expect(group.label).toBe(`${leader} and 87 others`);
    expect(group).toMatchObject({ kind: "group", root: null });
  });

  it("gathers the pairs and triples, triples first, and lists the unlinked by slug", () => {
    const small = find(unbound, "district:small")!;
    expect(small.members).toEqual(ids(...FIXTURE_PARTS.small.slice(20), ...FIXTURE_PARTS.small.slice(0, 20)));
    expect(small.label).toBe("Small groups");
    const unlinked = find(unbound, "district:unlinked")!;
    expect(unlinked.members).toEqual(ids(...FIXTURE_PARTS.independent));
    expect(unlinked.label).toBe("No links recorded with the others");
  });

  it("cuts what is over the maximum into equal parts, and nothing else", () => {
    expect(sizes(bound)).toEqual([
      ["district:shared", 3],
      ["district:tree:t1-00#1", 21],
      ["district:tree:t1-00#2", 21],
      ["district:tree:t1-00#3", 21],
      ["district:tree:t3-00#1", 20],
      ["district:tree:t3-00#2", 20],
      ["district:tree:t2-00", 1],
      ["district:tree:t2-00/modules%2Fhal", 18],
      ["district:tree:t2-00/modules%2Flib", 8],
      ["district:tree:t2-00/modules%2Fcrypto", 5],
      ["district:tree:t2-00/tools", 3],
      ["district:tree:t2-00:other", 2],
      [groupId, 88],
      ["district:small#1", 15],
      ["district:small#2", 14],
      ["district:unlinked#1", 20],
      ["district:unlinked#2", 20],
    ]);
    expect(bound).toHaveLength(17);
    expect(oversize(bound)).toEqual([groupId]);
    expect(oversize(unbound)).toEqual([
      "district:tree:t1-00",
      "district:tree:t3-00",
      groupId,
      "district:small",
      "district:unlinked",
    ]);
  });

  it("names each part, and the parts in order read as the district they cut", () => {
    const parts = bound.filter((d) => d.id.startsWith("district:tree:t1-00#"));
    expect(parts.map((d) => [d.label, d.part])).toEqual([
      ["t1-00 · 1 of 3", { index: 1, of: 3 }],
      ["t1-00 · 2 of 3", { index: 2, of: 3 }],
      ["t1-00 · 3 of 3", { index: 3, of: 3 }],
    ]);
    for (const d of parts) expect(d).toMatchObject({ kind: "tree", root: "repo:t1-00" });
    expect(parts.flatMap((d) => d.members)).toEqual(find(unbound, "district:tree:t1-00")!.members);
    for (const id of ["district:tree:t3-00", "district:small", "district:unlinked"]) {
      const whole = find(unbound, id)!;
      const cut = bound.filter((d) => d.id.startsWith(`${id}#`));
      expect(cut.flatMap((d) => d.members), id).toEqual(whole.members);
      expect(cut.map((d) => d.part?.of), id).toEqual(cut.map(() => cut.length));
    }
  });

  it("never cuts a small group apart", () => {
    const parts = bound.filter((d) => d.kind === "small");
    expect(parts.map((d) => d.label)).toEqual(["Small groups · 1 of 2", "Small groups · 2 of 2"]);
    for (const component of FIXTURE_PARTS.smallComponents) {
      const holding = parts.filter((p) => component.some((slug) => p.members.includes(repositoryId(slug))));
      expect(holding, component.join(",")).toHaveLength(1);
      expect(holding[0]!.members.filter((m) => component.includes(m.slice("repo:".length))).length).toBe(component.length);
    }
  });

  it("does not depend on the order repositories and edges arrive in", () => {
    for (const seed of [1, 2, 3]) {
      const reordered = (options: { bound: boolean }) =>
        districts(shuffled(repositories, seed), shuffled(edges, seed + 100), options);
      expect(reordered({ bound: true }), `seed ${seed}`).toEqual(bound);
      expect(reordered({ bound: false }), `seed ${seed}`).toEqual(unbound);
    }
  });
});

// -- one rule at a time ----------------------------------------------------------------

describe("districts of small worlds", () => {
  it("leaves a repository linked only to the districts above in 'Linked elsewhere'", () => {
    const { repositories, edges } = world(["r", "c", "y", "z"], [incl("r", "c"), plain("y", "c")]);
    const ds = districts(repositories, edges);
    expect(ds.map((d) => [d.id, d.kind, d.label, d.members])).toEqual([
      ["district:tree:r", "tree", "r", ids("r", "c")],
      ["district:loose", "loose", "Linked elsewhere", ids("y")],
      ["district:unlinked", "unlinked", "No links recorded with the others", ids("z")],
    ]);
  });

  it("ignores edges to repositories that are not listed, and edges to oneself", () => {
    const { repositories, edges } = world(["a", "b"], [incl("a", "b"), plain("b", "a")]);
    const toGhost = { ...edges[0]!, target: "repo:ghost" };
    const toSelf = { ...edges[1]!, target: edges[1]!.source };
    const ds = districts(repositories, [toGhost, toSelf]);
    expect(ds.map((d) => [d.kind, d.members])).toEqual([["unlinked", ids("a", "b")]]);
  });

  it("takes a pure inclusion loop as one family, its smallest slug the root", () => {
    const links = [incl("b", "a"), incl("a", "b")];
    for (const slugs of [["a", "b"], ["b", "a"]]) {
      const { repositories, edges } = world(slugs, links);
      const ds = districts(repositories, edges);
      expect(ds).toEqual([
        { id: "district:tree:a", kind: "tree", label: "a", members: ids("a", "b"), root: "repo:a", part: null },
      ]);
    }
  });

  it("takes what hangs below a loop into the loop's family, and a loop below a root into the root's", () => {
    const below = world(["a", "b", "c"], [incl("a", "b"), incl("b", "a"), incl("b", "c")]);
    expect(districts(below.repositories, below.edges).map((d) => [d.id, d.members])).toEqual([
      ["district:tree:a", ids("a", "b", "c")],
    ]);
    const under = world(["r", "a", "b"], [incl("r", "a"), incl("a", "b"), incl("b", "a")]);
    expect(districts(under.repositories, under.edges).map((d) => [d.id, d.members])).toEqual([
      ["district:tree:r", ids("r", "a", "b")],
    ]);
  });

  it("makes a source loop's smallest slug a root even beside a real root", () => {
    const { repositories, edges } = world(["r", "c", "x", "y"], [incl("r", "c"), incl("y", "x"), incl("x", "y")]);
    const ds = districts(repositories, edges);
    expect(ds.map((d) => [d.id, d.members])).toEqual([
      ["district:tree:r", ids("r", "c")],
      ["district:tree:x", ids("x", "y")],
    ]);
  });

  it("gives a child included at the same depth by two roots to the smaller root", () => {
    const direct = world(["r2", "r1", "c"], [incl("r2", "c"), incl("r1", "c")]);
    expect(districts(direct.repositories, direct.edges).map((d) => [d.id, d.members])).toEqual([
      ["district:tree:r1", ids("r1", "c")],
      ["district:tree:r2", ids("r2")],
    ]);
    const deeper = world(
      ["r1", "r2", "a", "b", "c"],
      [incl("r2", "b"), incl("r1", "a"), incl("b", "c"), incl("a", "c")],
    );
    expect(districts(deeper.repositories, deeper.edges).map((d) => [d.id, d.members])).toEqual([
      ["district:tree:r1", ids("r1", "a", "c")],
      ["district:tree:r2", ids("r2", "b")],
    ]);
  });

  it("gives a child nearer one root than another to the nearer", () => {
    const { repositories, edges } = world(
      ["r1", "r2", "x", "y", "c"],
      [incl("r1", "x"), incl("x", "y"), incl("y", "c"), incl("r2", "c")],
    );
    expect(districts(repositories, edges).map((d) => [d.id, d.members])).toEqual([
      ["district:tree:r1", ids("r1", "x", "y")],
      ["district:tree:r2", ids("r2", "c")],
    ]);
  });

  it("hangs a child under the smallest of its parents that share its root, in pre-order", () => {
    const { repositories, edges } = world(
      ["r", "p1", "p2", "c"],
      [incl("r", "p2"), incl("r", "p1"), incl("p2", "c"), incl("p1", "c")],
    );
    expect(districts(repositories, edges)[0]!.members).toEqual(ids("r", "p1", "c", "p2"));
  });

  describe("a wide root", () => {
    it("splits by directory at 24 children when two directories hold three or more", () => {
      const { repositories, edges } = wide([["x", 12], ["y", 12]]);
      const ds = districts(repositories, edges);
      expect(sizes(ds)).toEqual([["district:tree:r", 1], ["district:tree:r/x", 12], ["district:tree:r/y", 12]]);
      expect(ds.map((d) => d.label)).toEqual(["r", "r · x", "r · y"]);
      for (const d of ds) expect(d).toMatchObject({ kind: "tree", root: "repo:r", part: null });
      expect(ds[1]!.members).toEqual(ids(...Array.from({ length: 12 }, (_, i) => `c${String(i + 1).padStart(2, "0")}`)));
    });

    it("does not split at 23 children", () => {
      const { repositories, edges } = wide([["x", 12], ["y", 11]]);
      expect(sizes(districts(repositories, edges))).toEqual([["district:tree:r", 24]]);
    });

    it("sends the children of small directories, and of none, to 'other'", () => {
      const { repositories, edges } = wide([["x", 12], ["y", 10], ["z", 2]]);
      expect(sizes(districts(repositories, edges))).toEqual([
        ["district:tree:r", 1],
        ["district:tree:r/x", 12],
        ["district:tree:r/y", 10],
        ["district:tree:r:other", 2],
      ]);
      const bare = wide([["", 3], ["x", 11], ["y", 10]]);
      const ds = districts(bare.repositories, bare.edges);
      expect(ds.at(-1)).toMatchObject({ id: "district:tree:r:other", label: "r · other", members: ids("c01", "c02", "c03") });
    });

    it("orders the directories by size, then by name", () => {
      const { repositories, edges } = wide([["b", 10], ["a", 10], ["c", 4]]);
      expect(districts(repositories, edges).map((d) => d.id)).toEqual([
        "district:tree:r",
        "district:tree:r/a",
        "district:tree:r/b",
        "district:tree:r/c",
      ]);
    });

    it("does not split when only one directory is big enough, and cuts the family instead", () => {
      const { repositories, edges } = wide([["x", 22], ["y", 2]]);
      expect(sizes(districts(repositories, edges, { bound: false }))).toEqual([["district:tree:r", 25]]);
      const ds = districts(repositories, edges);
      expect(sizes(ds)).toEqual([["district:tree:r#1", 13], ["district:tree:r#2", 12]]);
      expect(ds.map((d) => d.label)).toEqual(["r · 1 of 2", "r · 2 of 2"]);
    });

    it("does not split 30 children with single-segment paths, and cuts the family in two", () => {
      const { repositories, edges } = wide([["", 30]]);
      expect(sizes(districts(repositories, edges, { bound: false }))).toEqual([["district:tree:r", 31]]);
      const ds = districts(repositories, edges);
      expect(sizes(ds)).toEqual([["district:tree:r#1", 16], ["district:tree:r#2", 15]]);
      expect(ds.map((d) => d.part)).toEqual([{ index: 1, of: 2 }, { index: 2, of: 2 }]);
    });

    it("keeps a directory named 'other' apart from the remainder", () => {
      const { repositories, edges } = wide([["other", 12], ["x", 10], ["z", 2]]);
      const ds = districts(repositories, edges);
      expect(ds.map((d) => d.id)).toEqual(["district:tree:r", "district:tree:r/other", "district:tree:r/x", "district:tree:r:other"]);
      expect(sizes(ds).map((s) => s[1])).toEqual([1, 12, 10, 2]);
      expect(new Set(ds.map((d) => d.id)).size).toBe(ds.length);
    });

    it("keeps a root whose slug has a slash apart from a directory of another root", () => {
      const children = Array.from({ length: 24 }, (_, i) => `c${String(i + 1).padStart(2, "0")}`);
      const links = [
        ...children.map((slug, i) => incl("a", slug, `${i < 12 ? "b" : "x"}/${slug}`)),
        incl("a/b", "k"),
      ];
      const { repositories, edges } = world(["a", "a/b", "k", ...children], links);
      const ds = districts(repositories, edges);
      const idList = ds.map((d) => d.id);
      expect(idList).toContain("district:tree:a/b");
      expect(idList).toContain("district:tree:a%2Fb");
      expect(find(ds, "district:tree:a%2Fb")!.members).toEqual(ids("a/b", "k"));
      expect(new Set(idList).size).toBe(idList.length);
    });

    it("keeps the same districts whatever order the world arrives in", () => {
      const { repositories, edges } = wide([["x", 12], ["y", 10], ["z", 2]]);
      const base = districts(repositories, edges);
      for (const seed of [1, 2, 3]) {
        expect(districts(shuffled(repositories, seed), shuffled(edges, seed + 7))).toEqual(base);
      }
    });
  });

  describe("shared repositories", () => {
    const dependents = (n: number) => {
      const slugs = Array.from({ length: n }, (_, i) => `d${i + 1}`);
      return world(["h", ...slugs], slugs.map((s) => plain(s, "h")));
    };

    it("takes a repository 8 others depend on, and leaves theirs as 'Linked elsewhere'", () => {
      const { repositories, edges } = dependents(8);
      const ds = districts(repositories, edges);
      expect(ds.map((d) => [d.id, d.members])).toEqual([
        ["district:shared", ids("h")],
        ["district:loose", ids("d1", "d2", "d3", "d4", "d5", "d6", "d7", "d8")],
      ]);
    });

    it("does not take one that 7 depend on: the star is a group, led by its centre", () => {
      const { repositories, edges } = dependents(7);
      const ds = districts(repositories, edges);
      expect(ds.map((d) => [d.id, d.label, d.members[0]])).toEqual([["district:group:h", "h and 7 others", "repo:h"]]);
    });

    it("counts a dependent once however many links it has", () => {
      const slugs = Array.from({ length: 7 }, (_, i) => `d${i + 1}`);
      const links = [...slugs.map((s) => plain(s, "h")), plain("d1", "h", 9), { ...plain("d2", "h"), kind: "dependency" as const }];
      const { repositories, edges } = world(["h", ...slugs], links);
      expect(edges).toHaveLength(9);
      expect(districts(repositories, edges).some((d) => d.kind === "shared")).toBe(false);
    });
  });

  it("leads a group by partners, then by link weight, then by slug", () => {
    // a - b - c - d: b and c have two partners each; c's links weigh more
    const { repositories, edges } = world(
      ["a", "b", "c", "d"],
      [plain("a", "b", 5), plain("c", "b", 1), plain("c", "d", 9)],
    );
    const [group] = districts(repositories, edges);
    expect(group).toMatchObject({ id: "district:group:c", label: "c and 3 others", kind: "group" });
    expect(group!.members).toEqual(ids("c", "b", "d", "a"));

    const tied = world(["a", "b", "c", "d"], [plain("a", "b"), plain("b", "c"), plain("c", "d")]);
    expect(districts(tied.repositories, tied.edges)[0]!.id).toBe("district:group:b");
  });

  it("orders groups by size, then by leader, and the small ones triples first", () => {
    const four = ["g1", "g2", "g3", "g4"];
    const five = ["f1", "f2", "f3", "f4", "f5"];
    const links = [
      ...four.slice(1).map((s) => plain("g1", s)),
      ...five.slice(1).map((s) => plain("f1", s)),
      plain("p1", "p2"),
      plain("a1", "a2"),
      plain("q1", "q2"),
      plain("q2", "q3"),
    ];
    const { repositories, edges } = world([...four, ...five, "p1", "p2", "a1", "a2", "q1", "q2", "q3"], links);
    const ds = districts(repositories, edges);
    expect(ds.map((d) => d.id)).toEqual(["district:group:f1", "district:group:g1", "district:small"]);
    expect(ds[2]!.members).toEqual(ids("q1", "q2", "q3", "a1", "a2", "p1", "p2"));
  });

  it("does not depend on the order of the world", () => {
    const cases = [
      world(["r", "c", "y", "z"], [incl("r", "c"), plain("y", "c")]),
      world(["a", "b", "c"], [incl("a", "b"), incl("b", "a"), incl("b", "c")]),
      world(["r1", "r2", "a", "b", "c"], [incl("r2", "b"), incl("r1", "a"), incl("b", "c"), incl("a", "c")]),
      world(["a", "b", "c", "d"], [plain("a", "b", 5), plain("c", "b", 1), plain("c", "d", 9)]),
    ];
    for (const { repositories, edges } of cases) {
      const base = districts(repositories, edges);
      for (const seed of [1, 2, 3]) {
        expect(districts(shuffled(repositories, seed), shuffled(edges, seed + 50))).toEqual(base);
      }
    }
  });
});
