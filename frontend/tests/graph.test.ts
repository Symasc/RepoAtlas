import { describe, expect, it } from "vitest";

import { flowEdges } from "@/components/graph/flow";
import { blend } from "@/components/graph/useTween";
import type { ChildEntry, ChildLink, RepositoryLink, TopologyChildrenResponse } from "@/lib/api/generated";
import { arrowhead, centre, floating, LANE_INSET, sideways } from "@/lib/graph/geometry";
import {
  cardHeight,
  CARD,
  constellation,
  evenAngles,
  GROUP_ID,
  HUB_ID,
  layoutFile,
  layoutOpened,
  strokeFor,
  type Layout,
} from "@/lib/graph/layout";
import { toFreshness } from "@/lib/model/freshness";
import {
  buildFile,
  buildOpened,
  buildOverview,
  byRelation,
  childOf,
  familyOf,
  relationOf,
  FILE_ID,
  FILES_ID,
  FRAME_ID,
  repositoryId,
  SIDE_LIMIT,
  type Box,
  type LinkData,
} from "@/lib/model/graph";
import { depth, direction, search } from "@/lib/model/location";
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

function children(
  entries: ChildEntry[],
  links: ChildLink[],
  extra: Partial<TopologyChildrenResponse> = {},
): TopologyChildrenResponse {
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

function childLink(
  child: string,
  peer: ChildLink["peer"],
  kind: ChildLink["kind"] = "observed",
  count = 1,
  direction: ChildLink["direction"] = "outgoing",
  types: ChildLink["types"] = [],
): ChildLink {
  return { kind, child, direction, peer, count, types, examples: [`ref:${child}:${count}`] };
}

const local = (path: string): ChildLink["peer"] => ({ kind: "local", repository: "app", path });
const shared = (entity_type: string): ChildLink["peer"] => ({ kind: "shared", entity_type: entity_type as never });
const other = (slug: string): ChildLink["peer"] => ({ kind: "repository", repository: slug });

function overlaps(a: Box, b: Box): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

function contains(outer: Box, inner: Box): boolean {
  return (
    inner.x >= outer.x &&
    inner.y >= outer.y &&
    inner.x + inner.width <= outer.x + outer.width &&
    inner.y + inner.height <= outer.y + outer.height
  );
}

/** No two things overlap; a frame or a group holds its members, so it is left out. */
function assertNoOverlap(layout: Layout, skip: (id: string) => boolean = () => false) {
  const boxes = [...layout.entries()].filter(([id]) => !skip(id));
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      expect(overlaps(boxes[i]![1], boxes[j]![1]), `${boxes[i]![0]} overlaps ${boxes[j]![0]}`).toBe(false);
    }
  }
}

const distance = (box: Box) => Math.hypot(centre(box).x, centre(box).y);

describe("link families", () => {
  it("keeps HTTP and dependencies apart, and tells configuration from code", () => {
    expect(familyOf("http", null)).toBe("http");
    expect(familyOf("dependency", "package")).toBe("dependency");
    expect(familyOf("observed", "env_var")).toBe("config");
    expect(familyOf("observed", "config")).toBe("config");
    expect(familyOf("observed", null, [{ type: "READS", count: 2 }])).toBe("config");
    expect(familyOf("observed", null, [{ type: "READS", count: 2 }, { type: "IMPORTS", count: 1 }])).toBe("code");
    expect(familyOf("observed", "module", [{ type: "IMPORTS", count: 4 }])).toBe("code");
  });
});

describe("the overview", () => {
  const repositories = ["api", "web", "lib", "alone"].map((slug) => repository(slug));

  it("draws each kind between two repositories as its own line, never summed", () => {
    const graph = buildOverview(repositories, [
      link("observed", "web", "lib", 3),
      link("dependency", "web", "lib", 2),
      link("http", "web", "api", 1),
    ]);
    expect(graph.edges.map((e) => [e.id, e.link.count, e.link.family])).toEqual([
      ["observed:web->lib", 3, "code"],
      ["dependency:web->lib", 2, "dependency"],
      ["http:web->api", 1, "http"],
    ]);
    expect(graph.edges.every((e) => e.source.startsWith("repo:") && e.target.startsWith("repo:"))).toBe(true);
  });

  it("lists each link at both ends, in that end's direction", () => {
    const graph = buildOverview(repositories, [link("http", "web", "api", 4)]);
    const web = graph.connections.get(repositoryId("web"))!;
    const api = graph.connections.get(repositoryId("api"))!;
    expect(web.map((c) => [c.direction, c.partner.label, c.node, c.edge])).toEqual([
      ["outgoing", "api", "repo:api", "http:web->api"],
    ]);
    expect(api.map((c) => [c.direction, c.partner.label, c.node])).toEqual([["incoming", "web", "repo:web"]]);
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
});

describe("the constellation", () => {
  const many = Array.from({ length: 12 }, (_, i) => repository(`r${String(i).padStart(2, "0")}`));
  const chain = [
    link("observed", "r00", "r01", 50),
    link("observed", "r00", "r02", 40),
    link("http", "r00", "r03", 5),
    link("dependency", "r01", "r04", 3),
    link("observed", "r01", "r05", 2),
    link("observed", "r02", "r06", 1),
    link("observed", "r07", "r08", 9), // apart from r00's component
  ];

  it("rings the workspace hub when no repository has a link: never a straight row", () => {
    const four = ["a", "b", "c", "d"].map((s) => repository(s));
    const shape = constellation(four, [], null);
    expect(shape.anchor).toBeNull();
    expect(shape.layout.has(HUB_ID)).toBe(true);
    const ys = four.map((r) => Math.round(centre(shape.layout.get(repositoryId(r.slug))!).y));
    expect(new Set(ys).size).toBeGreaterThan(1);
    assertNoOverlap(shape.layout);
  });

  it("centres the repository with the most partners, its partners on the first ring", () => {
    const edges = buildOverview(many, chain).edges;
    const shape = constellation(many, edges, null);
    expect(shape.anchor).toBe("repo:r00");
    const at = (slug: string) => distance(shape.layout.get(repositoryId(slug))!);
    expect(at("r00")).toBe(0);
    for (const near of ["r01", "r02", "r03"]) {
      for (const far of ["r04", "r05", "r06"]) expect(at(near)).toBeLessThan(at(far));
    }
  });

  it("re-centres on the selected repository", () => {
    const edges = buildOverview(many, chain).edges;
    const shape = constellation(many, edges, "repo:r04");
    expect(shape.anchor).toBe("repo:r04");
    expect(distance(shape.layout.get("repo:r04")!)).toBe(0);
    expect(distance(shape.layout.get("repo:r01")!)).toBeLessThan(distance(shape.layout.get("repo:r00")!));
  });

  it("places every repository once, overlapping nothing, the unlinked in their own group beneath", () => {
    const edges = buildOverview(many, chain).edges;
    const shape = constellation(many, edges, null);
    for (const r of many) expect(shape.layout.has(repositoryId(r.slug)), r.slug).toBe(true);
    assertNoOverlap(shape.layout, (id) => id === GROUP_ID);
    const group = shape.group!;
    const linkedBottom = Math.max(
      ...["r00", "r01", "r02", "r03", "r04", "r05", "r06", "r07", "r08"].map((s) => {
        const b = shape.layout.get(repositoryId(s))!;
        return b.y + b.height;
      }),
    );
    expect(group.y).toBeGreaterThan(linkedBottom);
    for (const s of ["r09", "r10", "r11"]) expect(contains(group, shape.layout.get(repositoryId(s))!)).toBe(true);
  });

  it("gives a component linked only among itself its own small constellation, beside the main one", () => {
    const edges = buildOverview(many, chain).edges;
    const shape = constellation(many, edges, null);
    const main = ["r00", "r01", "r02", "r03", "r04", "r05", "r06"].map((s) => shape.layout.get(repositoryId(s))!);
    const right = Math.max(...main.map((b) => b.x + b.width));
    const [a, b] = ["r07", "r08"].map((s) => shape.layout.get(repositoryId(s))!);
    expect(a!.x).toBeGreaterThan(right);
    expect(b!.x).toBeGreaterThan(right);
    // a pair sits side by side, near each other
    expect(Math.abs(centre(a!).y - centre(b!).y)).toBeLessThan(1);
  });

  it("rings an unlinked repository chosen as the anchor with all the rest", () => {
    const edges = buildOverview(many, chain).edges;
    const shape = constellation(many, edges, "repo:r10");
    expect(distance(shape.layout.get("repo:r10")!)).toBe(0);
    expect(shape.group).toBeNull();
    assertNoOverlap(shape.layout);
  });

  it("scales to dozens of repositories without overlap", () => {
    const lots = Array.from({ length: 40 }, (_, i) => repository(`x${String(i).padStart(2, "0")}`));
    const links = lots.slice(1, 25).map((r, i) => link("observed", i % 3 === 0 ? "x00" : `x${String(i).padStart(2, "0")}`, r.slug));
    const shape = constellation(lots, buildOverview(lots, links).edges, null);
    expect([...shape.layout.keys()].filter((k) => k.startsWith("repo:"))).toHaveLength(40);
    assertNoOverlap(shape.layout, (id) => id === GROUP_ID);
  });

  it("puts four evenly spaced things on the diagonals", () => {
    const degrees = evenAngles(4).map((a) => Math.round((a * 180) / Math.PI) % 360);
    expect(degrees).toEqual([225, 315, 45, 135]);
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

  it("gives each directory a card and gathers the files on one, hidden directories last", () => {
    const graph = buildOpened({ repository: app, path: "", children: children(entries, []), previews: new Map() });
    expect(graph.directories.map((d) => d.name)).toEqual(["docs", "src", ".github"]);
    expect(graph.files?.entries.map((e) => e.name)).toEqual(["package.json", "README.md"]);
    expect(graph.frame).toMatchObject({ id: FRAME_ID, files: 19, directories: 3, leaves: 2 });
  });

  it("draws a link between two cards only on demand, and counts it on both", () => {
    const graph = buildOpened({
      repository: app,
      path: "",
      children: children(entries, [childLink("src", local("docs/guide.md"), "observed", 7)]),
      previews: new Map(),
    });
    expect(graph.edges.map((e) => [e.id, e.scope, e.link.count])).toEqual([["code:dir:src->dir:docs", "detail", 7]]);
    expect(graph.directories.find((d) => d.name === "src")!.summary).toEqual({ code: 7 });
    expect(graph.directories.find((d) => d.name === "docs")!.summary).toEqual({ code: 7 });
    expect(graph.connections.get("dir:docs")!.map((c) => [c.direction, c.partner.label])).toEqual([["incoming", "src"]]);
  });

  it("totals what leaves the level per family at rest, a card's own line on demand", () => {
    const graph = buildOpened({
      repository: app,
      path: "",
      children: children(entries, [
        childLink("src", shared("module"), "observed", 20),
        childLink("docs", shared("module"), "observed", 5),
        childLink("src", shared("env_var"), "observed", 3),
        childLink("package.json", shared("package"), "dependency", 4),
      ]),
      previews: new Map(),
    });
    const by = (id: string) => graph.edges.find((e) => e.id === id);
    expect(by("code:frame=>shared:module")).toMatchObject({ scope: "aggregate", link: { count: 25 } });
    expect(by("code:dir:src->shared:module")).toMatchObject({ scope: "detail", link: { count: 20 } });
    expect(by("config:frame=>shared:env_var")).toMatchObject({ scope: "aggregate", link: { count: 3, family: "config" } });
    expect(by("dependency:files->shared:package")).toMatchObject({ scope: "detail" });
    expect(graph.shared.map((s) => [s.label, s.count])).toEqual([
      ["External modules", 25],
      ["Packages", 4],
      ["Environment variables", 3],
    ]);
  });

  it("draws an incoming link from the other end", () => {
    const graph = buildOpened({
      repository: app,
      path: "",
      children: children(entries, [childLink("src", other("web"), "http", 2, "incoming")]),
      previews: new Map(),
    });
    expect(graph.edges.map((e) => e.id).sort()).toEqual(["http:peer:web->dir:src", "http:peer:web=>frame"]);
    expect(graph.peers).toEqual([{ id: "peer:web", repository: "web", count: 2 }]);
  });

  it("names places elsewhere, and folds what does not fit into one node it can list", () => {
    const opened = [entry("apps", "directory", 3)];
    const links = Array.from({ length: SIDE_LIMIT + 3 }, (_, i) =>
      childLink("apps", local(`packages/p${String(i).padStart(2, "0")}`), "observed", 100 - i),
    );
    const graph = buildOpened({ repository: app, path: "apps", children: children(opened, links, { path: "apps" }), previews: new Map() });
    expect(graph.places).toHaveLength(SIDE_LIMIT - 1);
    expect(graph.places[0]).toMatchObject({ id: "place:packages/p00", count: 100 });
    expect(graph.more).toEqual([{ id: "more:left", side: "left", nodes: 4, count: 92 + 91 + 90 + 89 }]);
    expect(graph.edges.find((e) => e.id === "code:dir:apps->more:left")?.link.count).toBe(92 + 91 + 90 + 89);
    expect(graph.connections.get("more:left")!.map((c) => c.partner.label)).toEqual(["p08", "p09", "p10", "p11"]);
  });

  it("ignores links within one card, and counts the ones it cannot place", () => {
    const graph = buildOpened({
      repository: app,
      path: "",
      children: children(entries, [
        childLink("src", local("src/a.ts")),
        childLink("missing", local("docs")),
        childLink("src", { kind: "repository" }),
      ]),
      previews: new Map(),
    });
    expect(graph.edges).toEqual([]);
    expect(graph.undrawn).toBe(2);
  });

  it("lays the cards inside the frame and the side columns outside it, overlapping nothing", () => {
    const graph = buildOpened({
      repository: app,
      path: "",
      children: children(entries, [
        childLink("src", shared("module"), "observed", 20),
        childLink("src", other("web"), "http", 2),
        childLink("docs", local("../elsewhere/x.md"), "observed", 1),
      ]),
      previews: new Map(),
    });
    const layout = layoutOpened(graph);
    const frame = layout.get(FRAME_ID)!;
    for (const id of ["dir:src", "dir:docs", "dir:.github", FILES_ID]) expect(contains(frame, layout.get(id)!), id).toBe(true);
    for (const id of ["shared:module", "peer:web"]) expect(overlaps(frame, layout.get(id)!), id).toBe(false);
    expect(layout.get("peer:web")!.x).toBeLessThan(frame.x);
    expect(layout.get("shared:module")!.x).toBeGreaterThan(frame.x + frame.width);
    assertNoOverlap(layout, (id) => id === FRAME_ID);
  });

  it("makes room for a card's link counts", () => {
    expect(cardHeight(5, true, true) - cardHeight(5, true, false)).toBe(CARD.footer);
  });
});

describe("a file", () => {
  const app = repository("app");
  const declarations: ChildEntry[] = [
    { id: "d1", key: "sym:a", kind: "declaration", name: "load", path: "src/a.ts", line_start: 3 },
    { id: "d2", key: "sym:b", kind: "declaration", name: "save", path: "src/a.ts", line_start: 9 },
  ];
  const file = (links: ChildLink[]) =>
    children(declarations, links, { path: "src/a.ts", target: "file", entries_total: 2 });

  it("puts what links to it on the left and what it links to on the right", () => {
    const graph = buildFile({
      repository: app,
      path: "src/a.ts",
      children: file([
        childLink("sym:a", local("src/b.ts"), "observed", 4),
        childLink("sym:b", local("src/b.ts"), "observed", 2),
        childLink("src/a.ts", local("src/main.ts"), "observed", 3, "incoming"),
        childLink("sym:a", shared("env_var"), "observed", 1),
        childLink("sym:a", { kind: "local", repository: "app", path: "src/a.ts", key: "sym:b" }),
      ]),
    });
    expect(graph.right.map((n) => [n.id, n.count])).toEqual([
      ["file:src/b.ts", 6],
      ["shared:env_var", 1],
    ]);
    expect(graph.left.map((n) => n.id)).toEqual(["file:src/main.ts"]);
    expect(graph.within).toBe(1);
    // one row per neighbour, family and direction, whichever declarations hold it
    const b = graph.connections.find((c) => c.node === "file:src/b.ts")!;
    expect(b).toMatchObject({ direction: "outgoing", via: "several declarations", link: { count: 6 } });
    expect(graph.edges.find((e) => e.id === "config:file->shared:env_var")).toBeDefined();
  });

  it("shows the strongest neighbours and folds the rest, which the inspector still lists", () => {
    const links = Array.from({ length: 12 }, (_, i) => childLink("sym:a", local(`lib/f${i}.ts`), "observed", 20 - i));
    const graph = buildFile({ repository: app, path: "src/a.ts", children: file(links), limit: { left: 7, right: 7 } });
    expect(graph.right).toHaveLength(6);
    expect(graph.more).toEqual([{ id: "more:right", side: "right", nodes: 6, count: 14 + 13 + 12 + 11 + 10 + 9 }]);
    expect(graph.connections).toHaveLength(12);
    const layout = layoutFile(graph);
    const anchor = layout.get(FILE_ID)!;
    for (const n of graph.right) expect(layout.get(n.id)!.x).toBeGreaterThan(anchor.x + anchor.width);
    assertNoOverlap(layout);
  });
});

describe("line geometry", () => {
  const a: Box = { x: 0, y: 0, width: 100, height: 40 };
  const b: Box = { x: 300, y: 200, width: 100, height: 40 };

  it("runs a floating line from border to border", () => {
    const route = floating(a, b);
    const [, sx, sy] = /^M (\S+) (\S+)/.exec(route.path)!.map(Number);
    // leaves through a's border, arrives just short of b's
    expect(sx === 100 || sy === 40).toBe(true);
    const onTop = Math.abs(route.end.y - (b.y - 3)) < 0.01 && route.end.x >= b.x && route.end.x <= b.x + b.width;
    const onLeft = Math.abs(route.end.x - (b.x - 3)) < 0.01 && route.end.y >= b.y && route.end.y <= b.y + b.height;
    expect(onTop || onLeft).toBe(true);
  });

  it("bows a curved line away from the centre", () => {
    const left: Box = { x: -250, y: -120, width: 100, height: 40 };
    const right: Box = { x: 150, y: -120, width: 100, height: 40 };
    const route = floating(left, right, 0, 0.2);
    expect(route.label.y).toBeLessThan(-100);
  });

  it("meets a frame as a lane at the other end's height", () => {
    const frame: Box = { x: 0, y: 0, width: 600, height: 500 };
    const side: Box = { x: 800, y: 200, width: 200, height: 40 };
    const route = sideways(frame, side, 0, "source");
    expect(route.path.startsWith("M 600 220 ")).toBe(true);
    const high: Box = { x: 800, y: -300, width: 200, height: 40 };
    expect(sideways(frame, high, 0, "source").path.startsWith(`M 600 ${LANE_INSET.top} `)).toBe(true);
  });

  it("points the arrowhead at the end", () => {
    expect(arrowhead({ x: 10, y: 0 }, { x: 0, y: 0 }, 4).startsWith("M 10 0 L 6 ")).toBe(true);
  });

  it("separates families drawn between one pair, whichever way each runs", () => {
    const edges = flowEdges(
      [
        { id: "code:a->b", source: "a", target: "b", link: { kind: "observed", family: "code", count: 1, types: [], packages: [], examples: [] } },
        { id: "http:b->a", source: "b", target: "a", link: { kind: "http", family: "http", count: 1, types: [], packages: [], examples: [] } },
      ],
      { route: () => "float" },
    );
    const [one, two] = edges.map((e) => e.data!.offset);
    // opposite directions: equal signs here put them on opposite sides
    expect(one).toBe(two);
    expect(one).not.toBe(0);
  });

  it("names a code link's relationship by what it does, and every other family as itself", () => {
    expect(["IMPORTS", "EXPORTS", "CALLS", "EXECUTES", "USES", "REFERENCES", "DEFINES"].map((t) => relationOf("code", t))).toEqual([
      "imports",
      "imports",
      "calls",
      "calls",
      "uses",
      "uses",
      "other",
    ]);
    expect(relationOf("http", "CALLS_HTTP_ROUTE")).toBe("http");
    expect(relationOf("dependency", "DEPENDS_ON")).toBe("dependency");
    expect(relationOf("config", "READS")).toBe("config");
  });

  it("splits a code link into one part per relationship, in a fixed order, counts kept", () => {
    const link: LinkData = {
      kind: "observed",
      family: "code",
      count: 19,
      types: [
        { type: "USES", count: 9 },
        { type: "IMPORTS", count: 6 },
        { type: "CALLS", count: 3 },
        { type: "EXPORTS", count: 1 },
      ],
      packages: [],
      examples: ["e1"],
    };
    const parts = byRelation(link);
    expect(parts.map((p) => [p.relation, p.link.count, p.link.types.map((t) => t.type)])).toEqual([
      ["imports", 7, ["IMPORTS", "EXPORTS"]],
      ["calls", 3, ["CALLS"]],
      ["uses", 9, ["USES"]],
    ]);
    // one relationship, or another family: the link itself, whole
    const single: LinkData = { ...link, types: [{ type: "CALLS", count: 19 }] };
    expect(byRelation(single)).toEqual([{ relation: "calls", link: single }]);
    const http: LinkData = { ...link, kind: "http", family: "http" };
    expect(byRelation(http)).toEqual([{ relation: "http", link: http }]);
  });

  it("draws each relationship of a code link as its own line, side by side, the count on one", () => {
    const code: LinkData = {
      kind: "observed",
      family: "code",
      count: 10,
      types: [
        { type: "IMPORTS", count: 4 },
        { type: "CALLS", count: 6 },
      ],
      packages: [],
      examples: [],
    };
    const edges = flowEdges(
      [
        { id: "code:a->b", source: "a", target: "b", link: code },
        { id: "http:a->b", source: "a", target: "b", link: { ...code, kind: "http", family: "http", types: [] } },
      ],
      { route: () => "side" },
    );
    expect(edges.map((e) => [e.id, e.data!.relation, e.data!.base, e.data!.link.count, e.data!.badge?.count ?? null])).toEqual([
      ["code:a->b~imports", "imports", "code:a->b", 4, 10],
      ["code:a->b~calls", "calls", "code:a->b", 6, null],
      ["http:a->b", "http", "http:a->b", 10, 10],
    ]);
    // three lines between one pair, none on top of another
    expect(new Set(edges.map((e) => e.data!.offset)).size).toBe(3);
  });

  it("thickens with the count, within bounds", () => {
    expect(strokeFor(1)).toBeLessThan(strokeFor(100));
    expect(strokeFor(1e9)).toBe(3.5);
  });
});

describe("moving between places", () => {
  it("knows deeper from back out", () => {
    const overview = { repository: null, path: "" };
    const root = { repository: "app", path: "" };
    const deep = { repository: "app", path: "src/a.ts" };
    expect([depth(overview), depth(root), depth(deep)]).toEqual([0, 1, 3]);
    expect(direction(overview, root)).toBe("in");
    expect(direction(deep, root)).toBe("out");
    expect(direction(deep, { repository: "app", path: "src/b.ts" })).toBe("across");
    expect(direction(root, root)).toBe("none");
    expect(search(deep, "file:x")).toBe("?repo=app&path=src%2Fa.ts&sel=file%3Ax");
    expect(search(overview)).toBe("");
  });

  it("finds the child a deeper place sits in", () => {
    expect(childOf("", "src/a/b.ts")).toBe("src");
    expect(childOf("src", "src/a/b.ts")).toBe("src/a");
    expect(childOf("src", "lib/a.ts")).toBeNull();
    expect(childOf("src", "src")).toBeNull();
  });

  it("glides each box part of the way", () => {
    const from: Layout = new Map([["n", { x: 0, y: 0, width: 10, height: 10 }]]);
    const to: Layout = new Map([
      ["n", { x: 100, y: 50, width: 10, height: 10 }],
      ["new", { x: 5, y: 5, width: 1, height: 1 }],
    ]);
    const half = blend(from, to, 0.5);
    expect(half.get("n")).toEqual({ x: 50, y: 25, width: 10, height: 10 });
    expect(half.get("new")).toEqual({ x: 5, y: 5, width: 1, height: 1 });
    expect(blend(from, to, 1)).toBe(to);
  });
});
