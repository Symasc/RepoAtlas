import { describe, expect, it } from "vitest";

import { flowEdges } from "@/components/graph/flow";
import type { ChildEntry, ChildLink, RepositoryLink, TopologyChildrenResponse } from "@/lib/api/generated";
import { cardHeight, layoutOpened, rowsOf, sidesFor, strokeFor, type Layout } from "@/lib/graph/layout";
import type { Box } from "@/lib/model/graph";
import { buildOpened, buildOverview, FILES_ID, repositoryId } from "@/lib/model/graph";
import { toFreshness } from "@/lib/model/freshness";
import type { RepositoryModel } from "@/lib/model/repositories";

function repository(slug: string, hue = 1): RepositoryModel {
  return {
    slug,
    name: slug,
    branch: "main",
    headSha: null,
    files: 10,
    symbols: 20,
    relationships: 30,
    languages: ["TypeScript"],
    manifests: [],
    lockfiles: [],
    lastIndexedAt: null,
    hue,
    freshness: toFreshness({ state: "current_clean", depth: "live", repository_slug: slug }),
  };
}

function link(kind: RepositoryLink["kind"], source: string, target: string, count = 1): RepositoryLink {
  return { kind, source, target, count, types: [], examples: [], packages: [] };
}

const FRESHNESS = { depth: "stored", state: "indexed" } as const;

function entry(path: string, kind: ChildEntry["kind"], files?: number): ChildEntry {
  return { id: path, kind, name: path.split("/").pop()!, path, files: files ?? null };
}

function children(entries: ChildEntry[], links: ChildLink[], extra: Partial<TopologyChildrenResponse> = {}): TopologyChildrenResponse {
  return {
    context: { scope: "repository", repository: "app" } as never,
    repository: "app",
    path: "",
    target: "directory",
    entries,
    entries_total: entries.length,
    limits: { applied: 100, ceiling: 500, clamped: [] } as never,
    links,
    freshness: FRESHNESS,
    ...extra,
  };
}

function childLink(child: string, peer: ChildLink["peer"], kind: ChildLink["kind"] = "observed", count = 1, direction: ChildLink["direction"] = "outgoing"): ChildLink {
  return { kind, child, direction, peer, count, types: [], examples: [] };
}

function overlaps(a: Box, b: Box): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

function assertNoOverlap(layout: Layout) {
  const boxes = [...layout.entries()];
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      expect(overlaps(boxes[i]![1], boxes[j]![1]), `${boxes[i]![0]} overlaps ${boxes[j]![0]}`).toBe(false);
    }
  }
}

describe("the overview", () => {
  const repositories = ["api", "web", "lib", "alone"].map((slug) => repository(slug));

  it("draws each kind between two repositories as its own line, never summed", () => {
    const graph = buildOverview(repositories, [
      link("observed", "web", "lib", 3),
      link("dependency", "web", "lib", 2),
      link("http", "web", "api", 1),
    ]);
    expect(graph.edges.map((e) => [e.id, e.link?.count])).toEqual([
      ["observed:web->lib", 3],
      ["dependency:web->lib", 2],
      ["http:web->api", 1],
    ]);
    expect(graph.edges.every((e) => e.source.startsWith("repo:") && e.target.startsWith("repo:"))).toBe(true);
  });

  it("sets unlinked repositories apart rather than leaving them out", () => {
    const graph = buildOverview(repositories, [link("observed", "web", "lib")]);
    expect(graph.linked.map((r) => r.slug)).toEqual(["web", "lib"]);
    expect(graph.independent.map((r) => r.slug)).toEqual(["api", "alone"]);
  });

  it("ignores links to repositories it was not given, and self links", () => {
    const graph = buildOverview(repositories, [link("observed", "web", "elsewhere"), link("observed", "web", "web")]);
    expect(graph.edges).toEqual([]);
    expect(graph.independent).toHaveLength(4);
  });

  it("lays independent repositories out in rows without overlap", () => {
    const layout = rowsOf(repositories.concat(["e", "f"].map((s) => repository(s))), 0, 4);
    expect(layout.size).toBe(6);
    assertNoOverlap(layout);
  });
});

describe("an opened repository", () => {
  const app = repository("app");
  const entries = [
    entry("src", "directory", 11),
    entry(".github", "directory", 2),
    entry("docs", "directory", 4),
    entry("package.json", "file"),
    entry("README.md", "file"),
  ];

  it("gives each directory a card and gathers the files on one", () => {
    const graph = buildOpened({ repository: app, path: "", children: children(entries, []), previews: new Map() });
    // hidden directories after the rest
    expect(graph.directories.map((d) => d.name)).toEqual(["docs", "src", ".github"]);
    expect(graph.files?.entries.map((e) => e.name)).toEqual(["package.json", "README.md"]);
    expect(graph.chain.map((c) => c.name)).toEqual(["app"]);
  });

  it("draws a link to a sibling between the two cards, and from the files card", () => {
    const graph = buildOpened({
      repository: app,
      path: "",
      children: children(entries, [
        childLink("src", { kind: "local", repository: "app", path: "docs" }, "observed", 5),
        childLink("src", { kind: "local", repository: "app", path: "package.json" }, "observed", 2),
        childLink("package.json", { kind: "local", repository: "app", path: "README.md" }),
      ]),
      previews: new Map(),
    });
    const links = graph.edges.filter((e) => e.role === "link");
    expect(links.map((e) => [e.source, e.target, e.link?.count])).toEqual([
      ["dir:src", "dir:docs", 5],
      ["dir:src", FILES_ID, 2],
    ]);
  });

  it("points an incoming link at the child it reaches", () => {
    const graph = buildOpened({
      repository: app,
      path: "",
      children: children(entries, [
        childLink("src", { kind: "repository", repository: "web", path: "client" }, "observed", 1, "incoming"),
      ]),
      previews: new Map(),
    });
    const links = graph.edges.filter((e) => e.role === "link");
    expect(links.map((e) => [e.scope, e.source, e.target])).toEqual([
      ["detail", "peer:web", "dir:src"],
      ["aggregate", "peer:web", "chain:"],
    ]);
    expect(graph.peers).toEqual([{ id: "peer:web", repository: "web", path: "client" }]);
  });

  it("gathers shared things by type, keeping kinds apart, with the level's totals", () => {
    const graph = buildOpened({
      repository: app,
      path: "",
      children: children(entries, [
        childLink("src", { kind: "shared", entity_type: "package" }, "dependency", 7),
        childLink("docs", { kind: "shared", entity_type: "package" }, "dependency", 3),
        childLink("src", { kind: "shared", entity_type: "http_endpoint" }, "http", 2),
      ]),
      previews: new Map(),
    });
    expect(graph.shared.map((s) => [s.label, s.count])).toEqual([
      ["Packages", 10],
      ["HTTP endpoints", 2],
    ]);
    const links = graph.edges.filter((e) => e.role === "link");
    // each card's own line, drawn on demand
    expect(links.filter((e) => e.scope === "detail").map((e) => [e.id, e.link?.count])).toEqual([
      ["dependency:dir:src->shared:package", 7],
      ["dependency:dir:docs->shared:package", 3],
      ["http:dir:src->shared:http_endpoint", 2],
    ]);
    // and the level's total per kind, drawn at rest: summed within a kind, never across
    expect(links.filter((e) => e.scope === "aggregate").map((e) => [e.id, e.link?.count])).toEqual([
      ["dependency:chain:=>shared:package", 10],
      ["http:chain:=>shared:http_endpoint", 2],
    ]);
  });

  it("lands a link inside a card on that card", () => {
    const graph = buildOpened({
      repository: app,
      path: "",
      children: children(entries, [childLink("src", { kind: "local", repository: "app", path: "docs/guide/x.md" })]),
      previews: new Map(),
    });
    expect(graph.edges.filter((e) => e.role === "link").map((e) => [e.source, e.target])).toEqual([
      ["dir:src", "dir:docs"],
    ]);
  });

  it("gives somewhere else in the repository a node that opens where it is", () => {
    const graph = buildOpened({
      repository: app,
      path: "src",
      children: children(
        [entry("src/a", "directory", 1)],
        [
          childLink("src/a", { kind: "local", repository: "app", path: "packages/shared" }),
          childLink("src/a", { kind: "local", repository: "app", path: "turbo.json" }),
        ],
      ),
      previews: new Map(),
    });
    expect(graph.away).toEqual([
      { id: "away:packages/shared", path: "packages/shared", opens: "packages" },
      { id: "away:turbo.json", path: "turbo.json", opens: "" },
    ]);
    expect(graph.undrawn).toBe(0);
  });

  it("counts, rather than hides, a link it cannot place", () => {
    const graph = buildOpened({
      repository: app,
      path: "",
      children: children(entries, [
        childLink("not-a-child", { kind: "shared", entity_type: "module" }),
        childLink("src", { kind: "repository", repository: null }),
      ]),
      previews: new Map(),
    });
    expect(graph.undrawn).toBe(2);
  });

  it("draws the chain down to the path, and containment from its last step", () => {
    const graph = buildOpened({
      repository: app,
      path: "src/lib",
      children: children([entry("src/lib/a", "directory", 1)], []),
      previews: new Map(),
    });
    expect(graph.chain.map((c) => c.path)).toEqual(["", "src", "src/lib"]);
    expect(graph.edges.filter((e) => e.role === "tree").map((e) => e.id)).toEqual([
      "tree:chain:->chain:src",
      "tree:chain:src->chain:src/lib",
      "tree:chain:src/lib->dir:src/lib/a",
    ]);
  });

  it("is laid out without overlaps, previews loaded or not", () => {
    const many = Array.from({ length: 12 }, (_, i) => entry(`d${i}`, "directory", i + 1)).concat(entries);
    const links = [
      childLink("d0", { kind: "shared", entity_type: "module" }),
      childLink("d1", { kind: "shared", entity_type: "package" }, "dependency"),
      childLink("d2", { kind: "repository", repository: "web", path: null }),
      childLink("d3", { kind: "repository", repository: "api", path: null }),
      childLink("d4", { kind: "local", repository: "app", path: "elsewhere/one" }),
    ];
    const previews = new Map(
      many
        .filter((e) => e.kind === "directory")
        .map((e) => [e.path, children([entry(`${e.path}/x`, "file"), entry(`${e.path}/y`, "file")], [], { entries_total: 9 })]),
    );
    for (const loaded of [new Map(), previews]) {
      const graph = buildOpened({ repository: app, path: "", children: children(many, links), previews: loaded });
      const layout = layoutOpened(graph);
      expect(layout.size).toBe(
        1 + graph.directories.length + 1 + graph.shared.length + graph.peers.length + graph.away.length,
      );
      assertNoOverlap(layout);
    }
  });

  it("sizes a card by what it lists", () => {
    expect(cardHeight(5, true)).toBeGreaterThan(cardHeight(5, false));
    expect(cardHeight(1, false)).toBeLessThan(cardHeight(5, false));
  });
});

describe("edges on the canvas", () => {
  it("leave and enter by the sides that face each other", () => {
    const left = { x: 0, y: 0, width: 100, height: 50 };
    const right = { x: 300, y: 0, width: 100, height: 50 };
    const below = { x: 20, y: 200, width: 100, height: 50 };
    const linkEdge = { id: "e", source: "a", target: "b", role: "link" as const };
    expect(sidesFor(linkEdge, left, right)).toEqual({ from: "right", to: "left" });
    expect(sidesFor(linkEdge, right, left)).toEqual({ from: "left", to: "right" });
    expect(sidesFor(linkEdge, left, below)).toEqual({ from: "bottom", to: "top" });
    expect(sidesFor({ ...linkEdge, role: "tree" }, right, left)).toEqual({ from: "bottom", to: "top" });
  });

  it("separate two kinds between the same pair, and leave a lone link centred", () => {
    const repositories = ["web", "lib", "api"].map((slug) => repository(slug));
    const graph = buildOverview(repositories, [
      link("observed", "web", "lib"),
      link("dependency", "lib", "web"),
      link("http", "web", "api"),
    ]);
    const layout: Layout = new Map(
      repositories.map((r, i) => [repositoryId(r.slug), { x: i * 400, y: 0, width: 200, height: 80 }]),
    );
    const edges = flowEdges(graph.edges, layout, true);
    const offsets = Object.fromEntries(edges.map((e) => [e.id, e.type === "link" ? e.data?.offset : null]));
    expect(offsets["observed:web->lib"]).toBe(-(offsets["dependency:lib->web"] as number));
    expect(offsets["observed:web->lib"]).not.toBe(0);
    expect(offsets["http:web->api"]).toBe(0);
  });

  it("grow in width with the count, within bounds", () => {
    expect(strokeFor(1)).toBe(1);
    expect(strokeFor(100)).toBeGreaterThan(strokeFor(10));
    expect(strokeFor(1_000_000)).toBe(3.5);
  });
});
