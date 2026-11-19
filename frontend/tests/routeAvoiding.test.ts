import { describe, expect, it } from "vitest";

import { flowEdges } from "@/components/graph/flow";
import { defaultExpanded, layoutDistricts, summaryId } from "@/lib/graph/districtLayout";
import { districts, type District } from "@/lib/graph/districts";
import { floating, gutter } from "@/lib/graph/geometry";
import { INSET, occlusions, polylineEnters, routeAvoiding, type Line, type Router } from "@/lib/graph/occlusion";
import { overviewView } from "@/lib/graph/overviewView";
import { buildOverview, type Box, type ViewEdge } from "@/lib/model/graph";

import { ecosystem300 } from "./fixtures/ecosystem";
import { nestedFixture } from "./fixtures/nested";

/**
 * `routeAvoiding`: a line goes round the cards it would run under where there is
 * a way, and is `floating` otherwise. The small cases are built by hand; the four
 * states at the end are the real layouts (grep the output for `OCCLUSION_AFTER`).
 */

const card = (x: number, y = 0, width = 100, height = 50): Box => ({ x, y, width, height });
/** A row of three cards with a gap of 100 between them: A, B, C. */
const row = () =>
  new Map<string, Box>([
    ["A", card(0)],
    ["B", card(200)],
    ["C", card(400)],
  ]);
const line = (source: string, target: string, offset = 0): Line => ({ source, target, offset });
const avoiding =
  (cards: ReadonlyMap<string, Box>): Router =>
  (source, target, offset) =>
    routeAvoiding(source, target, offset, cards);
/** How many cards a line from `source` to `target` runs under with a router. */
const hits = (cards: ReadonlyMap<string, Box>, source: string, target: string, router: Router, offset = 0) =>
  occlusions([line(source, target, offset)], cards, router).pairs;

describe("routeAvoiding on a grid built by hand", () => {
  it("goes round the card between its ends where floating runs under it", () => {
    const cards = row();
    expect(hits(cards, "A", "C", floating)).toBe(1);
    expect(hits(cards, "A", "C", avoiding(cards))).toBe(0);
    const route = routeAvoiding(cards.get("A")!, cards.get("C")!, 0, cards);
    expect(route.points.length).toBeGreaterThan(2);
    expect(route.points).not.toEqual(floating(cards.get("A")!, cards.get("C")!, 0).points);
  });

  it("goes the other way round when the first is taken", () => {
    const cards = row();
    // a card above B across the gutter over it: the route under it is the one that is free
    cards.set("U", card(200, -40, 100, 40));
    expect(hits(cards, "A", "C", floating)).toBe(1);
    expect(hits(cards, "A", "C", avoiding(cards))).toBe(0);
    const route = routeAvoiding(cards.get("A")!, cards.get("C")!, 0, cards);
    const lowest = Math.max(...route.points.map((p) => p.y));
    expect(lowest).toBeGreaterThan(50);
  });

  it("is floating, unchanged, where every route runs under some card (a tie goes to the earlier)", () => {
    const cards = row();
    cards.set("U", card(200, -40, 100, 40));
    cards.set("L", card(200, 52, 100, 40));
    const a = cards.get("A")!;
    const c = cards.get("C")!;
    for (const offset of [0, 8, -8]) {
      expect(hits(cards, "A", "C", floating, offset), `offset ${offset}`).toBe(1);
      expect(routeAvoiding(a, c, offset, cards), `offset ${offset}`).toEqual(floating(a, c, offset));
    }
  });

  it("leaves a line that runs under nothing exactly as floating draws it", () => {
    const cards = row();
    for (const [source, target, offset] of [["A", "B", 0], ["B", "C", 8], ["C", "B", -8], ["B", "A", 0]] as const) {
      const route = routeAvoiding(cards.get(source)!, cards.get(target)!, offset, cards);
      expect(route).toEqual(floating(cards.get(source)!, cards.get(target)!, offset));
    }
  });

  it("takes the other axis when that is the one that is free", () => {
    // A above-left of C: floating runs along x, down a vertical run in the middle, past the card there
    const cards = new Map<string, Box>([
      ["A", card(0, 0)],
      ["C", card(300, 200)],
      ["B", card(180, 90, 40, 20)],
    ]);
    const a = cards.get("A")!;
    const c = cards.get("C")!;
    expect(hits(cards, "A", "C", floating)).toBe(1);
    const route = routeAvoiding(a, c, 0, cards);
    expect(hits(cards, "A", "C", avoiding(cards))).toBe(0);
    expect(route).toEqual(floating(a, c, 0, "y"));
  });

  it("does not take a route through its own ends, nor count them as in the way", () => {
    const inner = (b: Box): Box => ({ x: b.x + INSET, y: b.y + INSET, width: b.width - 2 * INSET, height: b.height - 2 * INSET });
    // C is straight below A, with a wide card between them: the near gutter leaves A's side and runs along C's top
    const cards = new Map<string, Box>([
      ["A", card(0, 0)],
      ["M", card(-60, 100, 220, 40)],
      ["C", card(0, 220)],
    ]);
    const a = cards.get("A")!;
    const c = cards.get("C")!;
    for (const far of [false, true]) {
      const route = gutter(a, c, 0, far);
      expect(polylineEnters(route.points, inner(a)), `far = ${far}: through A`).toBe(false);
      expect(polylineEnters(route.points, inner(c)), `far = ${far}: through C`).toBe(false);
    }
    // the ends are never obstacles: with only them on the map the line is floating's
    const ends = new Map([["A", a], ["C", c]]);
    expect(routeAvoiding(a, c, 0, ends)).toEqual(floating(a, c, 0));
    // M is in the way of floating; whatever is chosen is no worse, and does not enter A or C
    const route = routeAvoiding(a, c, 0, cards);
    expect(hits(cards, "A", "C", avoiding(cards))).toBeLessThanOrEqual(hits(cards, "A", "C", floating));
    expect(polylineEnters(route.points, inner(a))).toBe(false);
    expect(polylineEnters(route.points, inner(c))).toBe(false);
  });

  it("is the same on every call", () => {
    const cards = row();
    cards.set("U", card(200, -40, 100, 40));
    const a = cards.get("A")!;
    const c = cards.get("C")!;
    const first = routeAvoiding(a, c, 8, cards);
    expect(routeAvoiding(a, c, 8, cards)).toEqual(first);
    expect(routeAvoiding(a, c, 8, new Map(cards))).toEqual(first);
  });

  it("runs from the source's border to the target's, and its path ends where its points do", () => {
    const cards = row();
    const a = cards.get("A")!;
    const c = cards.get("C")!;
    for (const route of [routeAvoiding(a, c, 0, cards), gutter(a, c, 0), gutter(a, c, 6, true), gutter(c, a, -6)]) {
      expect(route.points.length).toBeGreaterThanOrEqual(2);
      expect(route.points[0]).toEqual(route.start);
      expect(route.points[route.points.length - 1]).toEqual(route.end);
      expect(route.towards).not.toEqual(route.end);
      expect(route.path.startsWith(`M ${route.start.x} ${route.start.y}`)).toBe(true);
      expect(route.path.endsWith(`L ${route.end.x} ${route.end.y}`)).toBe(true);
    }
    // the border: out of A's right side, into C's top a gap above it
    const g = gutter(a, c, 0);
    expect(g.start.x).toBe(a.x + a.width);
    expect(g.start.y).toBeGreaterThanOrEqual(a.y);
    expect(g.start.y).toBeLessThanOrEqual(a.y + a.height);
    expect(g.end.x).toBeGreaterThanOrEqual(c.x);
    expect(g.end.x).toBeLessThanOrEqual(c.x + c.width);
    expect(g.end.y).toBeCloseTo(c.y - 3);
    // and into the bottom for the far face
    expect(gutter(a, c, 0, true).end.y).toBeCloseTo(c.y + c.height + 3);
    // a target to the left is left by A's left side
    expect(gutter(c, a, 0).start.x).toBe(c.x);
  });

  it("keeps parallel gutters apart, and clamps how far an offset moves one", () => {
    const a = card(0);
    const c = card(400);
    const xs = [-8, 0, 8].map((o) => gutter(a, c, o).points[1]!.x);
    expect(new Set(xs).size).toBe(3);
    // 24 off the source's side, moved at most 18; 14 off the target's face, moved at most 10
    expect(gutter(a, c, 0).points[1]!.x).toBe(a.x + a.width + 24);
    expect(gutter(a, c, 500).points[1]!.x).toBe(a.x + a.width + 24 + 18);
    expect(gutter(a, c, -500).points[1]!.x).toBe(a.x + a.width + 24 - 18);
    expect(gutter(a, c, 500).points).toEqual(gutter(a, c, 18).points);
    const base = gutter(a, c, 0).points[2]!.y;
    expect(Math.abs(gutter(a, c, 500).points[2]!.y - base)).toBe(10);
    expect(Math.abs(gutter(a, c, -500).points[2]!.y - base)).toBe(10);
  });
});

describe("flowEdges with avoid", () => {
  const cards = row();
  const edge = (source: string, target: string): ViewEdge => ({
    id: `${source}>${target}`,
    source,
    target,
    link: { kind: "observed", family: "code", count: 1, types: [], packages: [], examples: [] },
  });

  it("hands the obstacles to the lines that avoid, and none to the others", () => {
    const edges = [edge("A", "C")];
    const avoid = flowEdges(edges, { route: () => "avoid", obstacles: cards });
    expect(avoid.every((e) => e.data!.route === "avoid" && e.data!.obstacles === cards)).toBe(true);
    const float = flowEdges(edges, { route: () => "float", obstacles: cards });
    expect(float.every((e) => e.data!.route === "float" && e.data!.obstacles === null)).toBe(true);
    const none = flowEdges(edges, { route: () => "avoid" });
    expect(none.every((e) => e.data!.obstacles === null)).toBe(true);
  });

  it("flips the offset of a line running the other way, as it does for float", () => {
    const edges = [edge("A", "C"), edge("C", "A")];
    const offsets = (kind: "float" | "avoid") =>
      flowEdges(edges, { route: () => kind, obstacles: cards }).map((e) => e.data!.offset);
    expect(offsets("avoid")).toEqual(offsets("float"));
  });
});

// -- the real layouts ------------------------------------------------------------------------

const PANE = { width: 1440, height: 810 } as const;

function measure(ds: readonly District[], edges: ReturnType<typeof buildOverview>["edges"], expanded: Set<string>, shelf: number) {
  const view = overviewView({ districts: ds, edges, expanded, shelf });
  const cards = view.layout.cards;
  const lines: Line[] = flowEdges(view.edges, { route: () => "avoid", obstacles: cards }).map((e) => ({
    source: e.source,
    target: e.target,
    offset: e.data!.offset,
  }));
  const owner = new Map<string, string>();
  for (const d of ds) {
    for (const member of d.members) owner.set(member, d.id);
    owner.set(summaryId(d.id), d.id);
  }
  const district = (id: string) => owner.get(id) ?? null;
  const router = avoiding(cards);
  const before = occlusions(lines, cards, floating, district);
  const after = occlusions(lines, cards, router, district);
  // every line, on its own: never under more cards than floating draws it under
  const worse: string[] = [];
  const broken: string[] = [];
  for (const l of lines) {
    const from = cards.get(l.source);
    const to = cards.get(l.target);
    if (from === undefined || to === undefined) continue;
    const was = occlusions([l], cards, floating).pairs;
    const now = occlusions([l], cards, router).pairs;
    if (now > was) worse.push(`${l.source} > ${l.target}: ${was} -> ${now}`);
    const route = router(from, to, l.offset);
    const last = route.points[route.points.length - 1];
    if (
      route.points.length < 2 ||
      route.points[0]!.x !== route.start.x ||
      route.points[0]!.y !== route.start.y ||
      last!.x !== route.end.x ||
      last!.y !== route.end.y
    ) {
      broken.push(`${l.source} > ${l.target}`);
    }
  }
  return { lines, before, after, worse, broken };
}

function report(state: string, m: ReturnType<typeof measure>): void {
  // read off the run by the one who decides what to do about it: only the district count is asserted
  console.log(
    `OCCLUSION_AFTER ${JSON.stringify({
      state,
      lines: m.lines.length,
      before: { pairs: m.before.pairs, lines: m.before.lines, sameDistrictPairs: m.before.sameDistrictPairs },
      after: { pairs: m.after.pairs, lines: m.after.lines, sameDistrictPairs: m.after.sameDistrictPairs },
    })}`,
  );
}

function check(state: string, m: ReturnType<typeof measure>, again: () => ReturnType<typeof measure>): void {
  expect(m.lines.length).toBeGreaterThan(0);
  expect(m.worse, "a line is under more cards than floating draws it under").toEqual([]);
  expect(m.broken, "a route that does not run from its start to its end").toEqual([]);
  expect(m.after.pairs).toBeLessThanOrEqual(m.before.pairs);
  expect(m.after.lines).toBeLessThanOrEqual(m.before.lines);
  // a line between two cards of a district does not run under a third of it
  expect(m.after.sameDistrictPairs, `${state}: same-district pairs left`).toBe(0);
  const second = again();
  expect(second.after).toEqual(m.after);
  report(state, m);
}

describe("lines avoid cards in the real layouts", () => {
  const nested = nestedFixture();
  const nestedEdges = buildOverview(nested.repositories, nested.links).edges;
  const nestedDs = districts(nested.repositories, nestedEdges);
  const big = ecosystem300();
  const bigEdges = buildOverview(big.repositories, big.links).edges;
  const bigDs = districts(big.repositories, bigEdges);

  it("the nested fixture, all open", { timeout: 60_000 }, () => {
    const expanded = new Set(nestedDs.map((d) => d.id));
    const shelf = layoutDistricts(nestedDs, expanded).shelf;
    const run = () => measure(nestedDs, nestedEdges, expanded, shelf);
    check("nested, all open", run(), run);
  });

  it("the 300 fixture, nothing open", { timeout: 60_000 }, () => {
    const expanded = new Set<string>();
    const shelf = layoutDistricts(bigDs, expanded).shelf;
    const run = () => measure(bigDs, bigEdges, expanded, shelf);
    check("300, nothing open", run(), run);
  });

  it("the 300 fixture, the default at 1440x810", { timeout: 60_000 }, () => {
    const chosen = defaultExpanded(bigDs, PANE);
    const run = () => measure(bigDs, bigEdges, chosen.expanded, chosen.shelf);
    check("300, default at 1440x810", run(), run);
  });

  it("the 300 fixture, all open, at the default's shelf", { timeout: 120_000 }, () => {
    const { shelf } = defaultExpanded(bigDs, PANE);
    const expanded = new Set(bigDs.map((d) => d.id));
    const run = () => measure(bigDs, bigEdges, expanded, shelf);
    check("300, all open", run(), run);
  });
});
