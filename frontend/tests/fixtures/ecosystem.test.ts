import { describe, expect, it } from "vitest";

import type { RepositoryLink } from "@/lib/api/generated";
import { buildOverview } from "@/lib/model/graph";

import { ecosystem300, FIXTURE_PARTS } from "./ecosystem";

const { repositories, links } = ecosystem300();
const parts = FIXTURE_PARTS;

/**
 * The fingerprint of the links, pinned once the shape is accepted: paste the
 * value the "FIXTURE_FINGERPRINT" line prints. While null, nothing is compared.
 */
const EXPECTED_FINGERPRINT: string | null = "67b9a8c1";

/** FNV-1a, 32 bits, as eight hex digits. */
function fnv1a32hex(text: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

const touching = (slug: string) => links.filter((l) => l.source === slug || l.target === slug);
const within = (members: readonly string[], list: readonly RepositoryLink[] = links) => {
  const set = new Set(members);
  return list.filter((l) => set.has(l.source) && set.has(l.target));
};

/** Shortest distance from `root` along `edges`, followed from source to target. */
function depths(root: string, edges: readonly RepositoryLink[]): Map<string, number> {
  const seen = new Map<string, number>([[root, 0]]);
  const queue = [root];
  for (let i = 0; i < queue.length; i++) {
    const at = queue[i]!;
    for (const e of edges) {
      if (e.source !== at || seen.has(e.target)) continue;
      seen.set(e.target, seen.get(at)! + 1);
      queue.push(e.target);
    }
  }
  return seen;
}

describe("the 300-repository ecosystem fixture", () => {
  it("has the parts' sizes, and 300 repositories in all", () => {
    expect(parts.t1).toHaveLength(63);
    expect(parts.t2).toHaveLength(37);
    expect(parts.t3).toHaveLength(40);
    expect(parts.mesh).toHaveLength(88);
    expect(parts.communities.map((c) => c.length)).toEqual([15, 15, 15, 15, 14, 14]);
    expect(parts.hubs).toHaveLength(3);
    expect(parts.small).toHaveLength(29);
    expect(parts.smallComponents.map((c) => c.length)).toEqual([2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 3, 3]);
    expect(parts.independent).toHaveLength(40);
    expect(repositories).toHaveLength(300);
  });

  it("names every repository once, and the parts cover them exactly", () => {
    const slugs = repositories.map((r) => r.slug);
    expect(new Set(slugs).size).toBe(300);
    const named = [...parts.t1, ...parts.t2, ...parts.t3, ...parts.mesh, ...parts.hubs, ...parts.small, ...parts.independent];
    expect(named).toHaveLength(300);
    expect([...named].sort()).toEqual([...slugs].sort());
    expect(slugs).toEqual([...slugs].sort());
  });

  it("joins only known repositories, never one to itself, and never the same link twice", () => {
    const known = new Set(repositories.map((r) => r.slug));
    for (const l of links) {
      expect(known.has(l.source), `${l.source} is unknown`).toBe(true);
      expect(known.has(l.target), `${l.target} is unknown`).toBe(true);
      expect(l.source, "a self link").not.toBe(l.target);
      expect(l.count).toBeGreaterThanOrEqual(1);
    }
    const identities = links.map((l) => `${l.kind}:${l.source}->${l.target}`);
    expect(new Set(identities).size).toBe(identities.length);
  });

  it("is deterministic: two calls are deep-equal", () => {
    expect(ecosystem300()).toEqual(ecosystem300());
  });

  it("gives repositories with no links none at all", () => {
    for (const slug of parts.independent) expect(touching(slug), slug).toEqual([]);
    const overview = buildOverview(repositories, links);
    expect(overview.edges).toHaveLength(links.length);
    expect(overview.independent.map((r) => r.slug).sort()).toEqual([...parts.independent].sort());
    expect(overview.linked).toHaveLength(260);
  });

  it("builds T1 as a binary inclusion tree of depth 5 declared in .gitmodules", () => {
    const edges = within(parts.t1).filter((l) => l.kind === "submodule");
    expect(edges).toHaveLength(62);
    const reach = depths("t1-00", edges);
    expect(reach.size).toBe(63);
    expect(Math.max(...reach.values())).toBe(5);
    for (const slug of parts.t1) {
      expect(edges.filter((l) => l.source === slug).length, slug).toBeLessThanOrEqual(2);
    }
    for (const l of edges) {
      expect(l.inclusions).toHaveLength(1);
      expect(l.inclusions_total).toBe(1);
      expect(l.inclusions![0]).toMatchObject({ method: "path", format: "gitmodules", path: `vendor/${l.target}` });
    }
  });

  it("builds T2 as a west root with 30 direct children and a chain to depth 5", () => {
    const edges = within(parts.t2).filter((l) => l.kind === "submodule");
    expect(edges).toHaveLength(36);
    expect(edges.filter((l) => l.source === "t2-00")).toHaveLength(30);
    const reach = depths("t2-00", edges);
    expect(reach.size).toBe(37);
    expect(Math.max(...reach.values())).toBe(5);
    for (const l of edges) expect(l.inclusions![0]!.format).toBe("west");
  });

  it("builds T3 as a git-subrepo tree of depth 5, one shared child and one loop", () => {
    const tree = within(parts.t3).filter((l) => l.kind === "submodule");
    // 39 tree edges, and the loop back
    expect(tree).toHaveLength(40);
    const reach = depths("t3-00", tree);
    expect(reach.size).toBe(40);
    expect(Math.max(...reach.values())).toBe(5);
    for (const l of tree) expect(l.inclusions![0]!.format).toBe("git-subrepo");

    // the shared child has a parent in T3 and another in T1
    const parents = links.filter((l) => l.kind === "submodule" && l.target === "t3-09").map((l) => l.source);
    expect(parents.sort()).toEqual(["t1-45", "t3-04"]);

    // the loop: t3-01 leads down to t3-39, which includes it back
    expect(reach.get("t3-39")).toBe(5);
    expect(tree.some((l) => l.source === "t3-39" && l.target === "t3-01")).toBe(true);
    expect(depths("t3-01", tree).has("t3-39")).toBe(true);
  });

  it("builds the mesh as six dense communities, neighbours joined by one bridge each", () => {
    const talk = links.filter((l) => l.kind === "observed" || l.kind === "http");
    const meshLinks = within(parts.mesh, talk);
    const community = new Map<string, number>();
    parts.communities.forEach((members, c) => members.forEach((m) => community.set(m, c)));

    // what leaves the mesh is a library dependency, a hub dependency, or a light code link to a tree
    const hubs = new Set(parts.hubs);
    const libraries = new Set(parts.libraries);
    const trees = new Set([...parts.t1, ...parts.t2, ...parts.t3]);
    for (const l of links) {
      if (community.has(l.source) === community.has(l.target)) continue;
      const outside = community.has(l.source) ? l.target : l.source;
      const hubDependency = l.kind === "dependency" && hubs.has(l.target);
      const libraryDependency = l.kind === "dependency" && libraries.has(l.target);
      const lightCode = l.kind === "observed" && l.count <= 30 && trees.has(outside);
      expect(hubDependency || libraryDependency || lightCode, `${l.kind}:${l.source}->${l.target}`).toBe(true);
    }

    for (const members of parts.communities) {
      for (const slug of members) {
        const partners = new Set<string>();
        for (const l of within(members, talk)) {
          if (l.source === slug) partners.add(l.target);
          if (l.target === slug) partners.add(l.source);
        }
        expect(partners.size, slug).toBeGreaterThanOrEqual(3);
      }
    }

    for (let i = 0; i < parts.communities.length; i++) {
      for (let j = i + 1; j < parts.communities.length; j++) {
        const between = meshLinks.filter((l) => {
          const a = community.get(l.source)!;
          const b = community.get(l.target)!;
          return (a === i && b === j) || (a === j && b === i);
        });
        expect(between, `communities ${i} and ${j}`).toHaveLength(j === i + 1 ? 1 : 0);
      }
    }

    // weights range widely, into the thousands for code
    const codeCounts = meshLinks.filter((l) => l.kind === "observed").map((l) => l.count);
    expect(Math.max(...codeCounts)).toBeGreaterThanOrEqual(1000);
    expect(Math.min(...codeCounts)).toBeLessThanOrEqual(10);
    expect(meshLinks.some((l) => l.kind === "http")).toBe(true);
    for (const l of meshLinks.filter((x) => x.kind === "observed")) {
      expect(l.types!.reduce((sum, t) => sum + t.count, 0), `${l.source}->${l.target}`).toBe(l.count);
    }
  });

  it("makes each hub a dependency of 60 or more repositories, across every part", () => {
    const pools = [parts.t1, parts.t2, parts.t3, parts.mesh];
    for (const hub of parts.hubs) {
      const incoming = touching(hub);
      expect(incoming.every((l) => l.kind === "dependency" && l.target === hub), hub).toBe(true);
      const sources = new Set(incoming.map((l) => l.source));
      expect(sources.size, hub).toBe(incoming.length);
      expect(sources.size, hub).toBeGreaterThanOrEqual(60);
      for (const pool of pools) expect(pool.some((slug) => sources.has(slug)), `${hub} from ${pool[0]}`).toBe(true);
      expect(incoming.every((l) => (l.packages ?? []).length > 0)).toBe(true);
    }
  });

  it("pairs every 3rd inclusion of T1 and T3, and every 4th of T2, with a parallel link", () => {
    const cases: [string, readonly string[], number, number][] = [
      ["t1", parts.t1, 3, 62],
      ["t2", parts.t2, 4, 36],
      // the 39 tree inclusions come first; the loop back is pushed after them
      ["t3", parts.t3, 3, 39],
    ];
    for (const [name, part, every, total] of cases) {
      const inclusions = within(part)
        .filter((l) => l.kind === "submodule")
        .slice(0, total);
      expect(inclusions, name).toHaveLength(total);
      const chosen = inclusions.filter((_, i) => (i + 1) % every === 0);
      // inside one tree, anything that is not an inclusion is a parallel link
      const parallels = within(part).filter((l) => l.kind !== "submodule");
      expect(parallels, name).toHaveLength(chosen.length);
      chosen.forEach((inc, k) => {
        const between = parallels.filter((l) => l.source === inc.source && l.target === inc.target);
        // code and dependency, alternating
        expect(
          between.map((l) => l.kind),
          `${name} ${inc.source}->${inc.target}`,
        ).toEqual([k % 2 === 0 ? "observed" : "dependency"]);
      });
    }
  });

  it("gives six libraries 5 to 12 dependents in the other parts, never as many as a hub or the 26 threshold", () => {
    const bodies = [parts.t1, parts.t2, parts.t3, parts.mesh];
    expect(parts.libraries).toEqual(["t1-03", "t1-10", "t2-05", "t3-02", "m3-04", "m0-11"]);
    for (const library of parts.libraries) {
      const own = bodies.find((body) => body.includes(library))!;
      const elsewhere = new Set(bodies.filter((body) => body !== own).flat());
      const incoming = links.filter((l) => l.kind === "dependency" && l.target === library);
      const outside = incoming.filter((l) => !own.includes(l.source));
      const sources = new Set(outside.map((l) => l.source));
      expect(sources.size, library).toBe(outside.length);
      expect(sources.size, library).toBeGreaterThanOrEqual(5);
      expect(sources.size, library).toBeLessThanOrEqual(12);
      for (const source of sources) expect(elsewhere.has(source), `${source} -> ${library}`).toBe(true);
      // the one other dependency a library may have is its parent's parallel link
      expect(incoming.length - outside.length, library).toBeLessThanOrEqual(1);
      expect(incoming.length, library).toBeLessThan(26);
    }

    // fifteen light code links between different parts
    const partOf = (slug: string) => bodies.findIndex((body) => body.includes(slug));
    const between = links.filter(
      (l) => l.kind === "observed" && partOf(l.source) >= 0 && partOf(l.target) >= 0 && partOf(l.source) !== partOf(l.target),
    );
    expect(between).toHaveLength(15);
    for (const l of between) {
      expect(l.count, `${l.source}->${l.target}`).toBeGreaterThanOrEqual(1);
      expect(l.count, `${l.source}->${l.target}`).toBeLessThanOrEqual(30);
    }
  });

  it("sorts T2's root children into directories of 12, 8, 5, 2 and 3; the chain stays in modules/", () => {
    const dirname = (path: string) => path.slice(0, path.lastIndexOf("/"));
    const rootChildren = links.filter((l) => l.kind === "submodule" && l.source === "t2-00");
    expect(rootChildren).toHaveLength(30);
    const counts = new Map<string, number>();
    for (const l of rootChildren) {
      const path = l.inclusions![0]!.path;
      expect(path.endsWith(`/${l.target}`), path).toBe(true);
      counts.set(dirname(path), (counts.get(dirname(path)) ?? 0) + 1);
    }
    expect([...counts]).toEqual([
      ["modules/hal", 12],
      ["modules/lib", 8],
      ["modules/crypto", 5],
      ["bootloader", 2],
      ["tools", 3],
    ]);
    // children 1..12 are the hal ones, in order
    rootChildren.slice(0, 12).forEach((l) => expect(l.inclusions![0]!.path).toBe(`modules/hal/${l.target}`));
    const chain = links.filter((l) => l.kind === "submodule" && parts.t2.includes(l.source) && l.source !== "t2-00");
    expect(chain).toHaveLength(6);
    for (const l of chain) expect(l.inclusions![0]!.path).toBe(`modules/${l.target}`);
  });

  it("has a fingerprint of its links", () => {
    const fingerprint = fnv1a32hex(JSON.stringify(links));
    console.info("FIXTURE_FINGERPRINT " + fingerprint + " " + links.length);
    if (EXPECTED_FINGERPRINT !== null) expect(fingerprint).toBe(EXPECTED_FINGERPRINT);
  });

  it("keeps the small components to themselves: pairs and triples, each its own", () => {
    parts.smallComponents.forEach((members, k) => {
      const own = within(members);
      expect(own, `component ${k}`).toHaveLength(members.length - 1);
      for (const slug of members) {
        for (const l of touching(slug)) {
          expect(members.includes(l.source) && members.includes(l.target), `${l.source}->${l.target}`).toBe(true);
        }
      }
      expect(depths(members[0]!, own).size, `component ${k}`).toBe(members.length);
    });
  });
});
