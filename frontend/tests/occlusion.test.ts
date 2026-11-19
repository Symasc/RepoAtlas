import { describe, expect, it } from "vitest";

import { flowEdges } from "@/components/graph/flow";
import { defaultExpanded, layoutDistricts, summaryId } from "@/lib/graph/districtLayout";
import { districts, type District } from "@/lib/graph/districts";
import { floating, octilinear, rounded, sideways, type Point, type Route } from "@/lib/graph/geometry";
import { INSET, occlusions, polylineEnters, type Line, type Router } from "@/lib/graph/occlusion";
import { overviewView } from "@/lib/graph/overviewView";
import { buildOverview, type Box } from "@/lib/model/graph";

import { ecosystem300 } from "./fixtures/ecosystem";
import { nestedFixture } from "./fixtures/nested";

/**
 * How many lines at rest run under cards they are not drawn between. The small
 * cases pin the measure on a grid built by hand; the four states at the end only
 * print what the real layouts hold (grep the output for `OCCLUSION`), and assert
 * that the measure is the same however often and in whatever order it is taken.
 */

const card = (x: number, y = 0): Box => ({ x, y, width: 100, height: 50 });
/** A row of three cards with a gap of 100 between them: A, B, C. */
const row = new Map<string, Box>([
  ["A", card(0)],
  ["B", card(200)],
  ["C", card(400)],
]);
const line = (source: string, target: string, offset = 0): Line => ({ source, target, offset });

/** A router that draws a given polyline whatever the boxes. */
function through(...points: Point[]): Router {
  const first = points[0]!;
  const last = points[points.length - 1]!;
  return (): Route => ({
    path: rounded(points, 0),
    label: first,
    start: first,
    end: last,
    towards: points[points.length - 2] ?? first,
    points,
  });
}

describe("occlusions on a grid built by hand", () => {
  it("counts the one line that passes under the card between its ends", () => {
    const result = occlusions([line("A", "C")], row, floating);
    expect(result).toEqual({ pairs: 1, lines: 1, sameDistrictPairs: 0 });
  });

  it("counts nothing for lines between neighbours", () => {
    const result = occlusions([line("A", "B"), line("B", "C"), line("C", "B")], row, floating);
    expect(result).toEqual({ pairs: 0, lines: 0, sameDistrictPairs: 0 });
  });

  it("never counts a card that is one of the line's own ends", () => {
    // the polyline starts and ends inside its own cards
    expect(occlusions([line("A", "B")], row, through({ x: 50, y: 25 }, { x: 250, y: 25 })).pairs).toBe(0);
  });

  it("counts a line once per card, however often it enters it", () => {
    const zigzag = through({ x: 100, y: 25 }, { x: 250, y: 25 }, { x: 250, y: 70 }, { x: 250, y: 25 }, { x: 400, y: 25 });
    expect(occlusions([line("A", "C")], row, zigzag)).toEqual({ pairs: 1, lines: 1, sameDistrictPairs: 0 });
  });

  it("counts a line once for each card it runs under", () => {
    const four = new Map(row).set("D", card(600));
    const result = occlusions([line("A", "D")], four, floating);
    expect(result).toEqual({ pairs: 2, lines: 1, sameDistrictPairs: 0 });
  });

  it("counts nothing for a line that goes round the card", () => {
    const round = through({ x: 100, y: 25 }, { x: 150, y: -40 }, { x: 350, y: -40 }, { x: 400, y: 25 });
    expect(occlusions([line("A", "C")], row, round).pairs).toBe(0);
  });

  it("does not count a line that only touches a card's border, within the inset", () => {
    expect(INSET).toBe(2);
    // along the top edge, and one pixel inside it: both inside the inset
    for (const y of [0, 1]) {
      expect(occlusions([line("A", "C")], row, through({ x: 100, y }, { x: 400, y })).pairs, `y = ${y}`).toBe(0);
    }
    // two pixels in is on the shrunk card's edge, and enters it
    expect(occlusions([line("A", "C")], row, through({ x: 100, y: INSET }, { x: 400, y: INSET })).pairs).toBe(1);
  });

  it("tests every segment of a bent line, not only its ends", () => {
    const bent = through({ x: 100, y: 25 }, { x: 150, y: 25 }, { x: 250, y: 40 }, { x: 350, y: 25 }, { x: 400, y: 25 });
    expect(occlusions([line("A", "C")], row, bent).pairs).toBe(1);
  });

  it("leaves out a line whose end is on no card", () => {
    expect(occlusions([line("A", "nowhere"), line("nowhere", "C")], row, floating)).toEqual({
      pairs: 0,
      lines: 0,
      sameDistrictPairs: 0,
    });
  });

  it("hands the line's offset to the router", () => {
    const seen: number[] = [];
    const spy: Router = (source, target, offset) => {
      seen.push(offset);
      return floating(source, target, offset);
    };
    occlusions([line("A", "C", 8), line("A", "B", -8)], row, spy);
    expect(seen).toEqual([8, -8]);
  });

  it("says how many pairs lie in one district: the line's ends and the card under it", () => {
    const districtsOf = (map: Record<string, string>) => (id: string) => map[id] ?? null;
    const home = districtsOf({ A: "d", B: "d", C: "d" });
    expect(occlusions([line("A", "C")], row, floating, home).sameDistrictPairs).toBe(1);
    // the card under the line is in another district
    const other = districtsOf({ A: "d", B: "e", C: "d" });
    expect(occlusions([line("A", "C")], row, floating, other)).toEqual({ pairs: 1, lines: 1, sameDistrictPairs: 0 });
    // the line joins two districts
    const across = districtsOf({ A: "d", B: "d", C: "e" });
    expect(occlusions([line("A", "C")], row, floating, across).sameDistrictPairs).toBe(0);
    // no district given: none counted
    expect(occlusions([line("A", "C")], row, floating).sameDistrictPairs).toBe(0);
  });

  it("is the same on every call, and whatever order the lines come in", () => {
    const lines = [line("A", "C"), line("A", "B"), line("C", "A", 8), line("B", "C")];
    const first = occlusions(lines, row, floating);
    expect(occlusions(lines, row, floating)).toEqual(first);
    expect(occlusions([...lines].reverse(), row, floating)).toEqual(first);
    expect(first.pairs).toBe(2);
    expect(first.lines).toBe(2);
  });

  it("is measured on routes whose points run from their start to their end", () => {
    const a = card(0);
    const b = card(400, 200);
    const routes = [
      floating(a, b),
      floating(a, b, 8),
      floating(b, a, -8),
      floating(a, card(40, 10)),
      sideways(a, b),
      sideways(a, b, 4, "source"),
      octilinear({ x: 0, y: 0 }, { x: 200, y: 60 }),
      octilinear({ x: 0, y: 0 }, { x: 40, y: 200 }, "y", 3),
    ];
    for (const route of routes) {
      expect(route.points.length).toBeGreaterThanOrEqual(2);
      expect(route.points[0]).toEqual(route.start);
      expect(route.points[route.points.length - 1]).toEqual(route.end);
    }
  });

  it("finds an enclosed point and a crossing segment alike", () => {
    const box: Box = { x: 10, y: 10, width: 20, height: 20 };
    expect(polylineEnters([{ x: 15, y: 15 }], box)).toBe(true);
    expect(polylineEnters([{ x: 0, y: 0 }], box)).toBe(false);
    expect(polylineEnters([{ x: 0, y: 20 }, { x: 40, y: 20 }], box)).toBe(true);
    expect(polylineEnters([{ x: 0, y: 5 }, { x: 40, y: 5 }], box)).toBe(false);
    expect(polylineEnters([{ x: 0, y: 0 }, { x: 40, y: 40 }], box)).toBe(true);
    expect(polylineEnters([{ x: 0, y: 40 }, { x: 40, y: 0 }], box)).toBe(true);
    expect(polylineEnters([{ x: 0, y: 0 }, { x: 0, y: 40 }], box)).toBe(false);
  });
});

// -- the real layouts ------------------------------------------------------------------------

const PANE = { width: 1440, height: 810 } as const;

/** Lines at rest, as the overview draws them, measured on a layout. */
function measure(ds: readonly District[], edges: ReturnType<typeof buildOverview>["edges"], expanded: Set<string>, shelf: number) {
  const view = overviewView({ districts: ds, edges, expanded, shelf });
  const lines: Line[] = flowEdges(view.edges, { route: () => "float" }).map((e) => ({
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
  return { view, lines, district, result: occlusions(lines, view.layout.cards, floating, district) };
}

function report(state: string, result: { pairs: number; sameDistrictPairs: number }): void {
  // read off the run by the one who decides what to do about it: nothing here asserts a number
  console.log(`OCCLUSION ${JSON.stringify({ state, pairs: result.pairs, sameDistrictPairs: result.sameDistrictPairs })}`);
}

describe("occlusions in the real layouts", () => {
  const nested = nestedFixture();
  const nestedEdges = buildOverview(nested.repositories, nested.links).edges;
  const nestedDs = districts(nested.repositories, nestedEdges);
  const big = ecosystem300();
  const bigEdges = buildOverview(big.repositories, big.links).edges;
  const bigDs = districts(big.repositories, bigEdges);

  it("the nested fixture, all open", () => {
    const expanded = new Set(nestedDs.map((d) => d.id));
    const shelf = layoutDistricts(nestedDs, expanded).shelf;
    const { view, lines, result } = measure(nestedDs, nestedEdges, expanded, shelf);
    expect(view.edges.length).toBeGreaterThan(0);
    expect(lines.length).toBeGreaterThan(0);
    expect(result.lines).toBeLessThanOrEqual(lines.length);
    expect(result.sameDistrictPairs).toBeLessThanOrEqual(result.pairs);
    expect(measure(nestedDs, nestedEdges, expanded, shelf).result).toEqual(result);
    report("nested, all open", result);
  });

  it("the 300 fixture, nothing open", () => {
    const expanded = new Set<string>();
    const shelf = layoutDistricts(bigDs, expanded).shelf;
    const { lines, result } = measure(bigDs, bigEdges, expanded, shelf);
    expect(lines.length).toBeGreaterThan(0);
    expect(result.lines).toBeLessThanOrEqual(lines.length);
    expect(result.sameDistrictPairs).toBeLessThanOrEqual(result.pairs);
    expect(measure(bigDs, bigEdges, expanded, shelf).result).toEqual(result);
    report("300, nothing open", result);
  });

  it("the 300 fixture, the default at 1440x810", () => {
    const chosen = defaultExpanded(bigDs, PANE);
    const { lines, result } = measure(bigDs, bigEdges, chosen.expanded, chosen.shelf);
    expect(lines.length).toBeGreaterThan(0);
    expect(result.lines).toBeLessThanOrEqual(lines.length);
    expect(result.sameDistrictPairs).toBeLessThanOrEqual(result.pairs);
    expect(measure(bigDs, bigEdges, chosen.expanded, chosen.shelf).result).toEqual(result);
    report("300, default at 1440x810", result);
  });

  it("the 300 fixture, all open, at the default's shelf", () => {
    const { shelf } = defaultExpanded(bigDs, PANE);
    const expanded = new Set(bigDs.map((d) => d.id));
    const { lines, result } = measure(bigDs, bigEdges, expanded, shelf);
    expect(lines.length).toBeGreaterThan(0);
    expect(result.lines).toBeLessThanOrEqual(lines.length);
    expect(result.sameDistrictPairs).toBeLessThanOrEqual(result.pairs);
    expect(measure(bigDs, bigEdges, expanded, shelf).result).toEqual(result);
    report("300, all open", result);
  });
});
