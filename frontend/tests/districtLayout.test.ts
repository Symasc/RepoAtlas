import { describe, expect, it } from "vitest";

import {
  CARD_CAP,
  DISTRICT,
  READABLE_ZOOM,
  columns,
  defaultExpanded,
  frameSize,
  layoutDistricts,
  sizeLevels,
  summaryId,
  visibleCards,
  type DistrictLayout,
} from "@/lib/graph/districtLayout";
import { districts, type District } from "@/lib/graph/districts";
import { fitZoom } from "@/lib/graph/fit";
import { REPOSITORY_SIZE } from "@/lib/graph/layout";
import { buildOverview, repositoryId, type Box } from "@/lib/model/graph";

import { ecosystem300 } from "./fixtures/ecosystem";

/**
 * Where the districts go: the geometry on small hand-made districts, then the
 * properties that matter on the fixture's own (overlap, gaps, stability when
 * one district opens, and what opens at rest on three screens). One
 * `DISTRICT_SCALE` line per screen reports the numbers; none is a target.
 */

function intersects(a: Box, b: Box): boolean {
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

const round = (n: number) => Math.round(n * 100) / 100;
const round4 = (n: number) => Math.round(n * 10000) / 10000;

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

/** A district of `n` repositories named after it. */
function district(id: string, n: number): District {
  return {
    id,
    kind: "unlinked",
    label: id,
    members: Array.from({ length: n }, (_, i) => repositoryId(`${id}-${i}`)),
    root: null,
    part: null,
  };
}

const VIEWPORTS = [
  { width: 1440, height: 810 },
  { width: 1920, height: 1080 },
  { width: 2560, height: 1440 },
] as const;

describe("the geometry of a district", () => {
  it("counts columns for a frame about 16:9, exactly even where the square root is whole", () => {
    expect([1, 2, 3, 5, 8, 18, 21, 24, 27, 28].map(columns)).toEqual([1, 2, 2, 2, 3, 4, 4, 4, 4, 5]);
    expect(columns(0)).toBe(1);
  });

  it("sizes a frame from its padding, header, cards and gaps", () => {
    expect(frameSize(1)).toEqual({ width: 328, height: 164, columns: 1, rows: 1 });
    expect(frameSize(8)).toEqual({ width: 1000, height: 388, columns: 3, rows: 3 });
    expect(REPOSITORY_SIZE).toEqual({ width: 288, height: 84 });
    expect(DISTRICT).toEqual({ gap: 96, header: 40, padding: 20, cardGap: { x: 48, y: 28 } });
  });

  it("lays an open district's cards row by row from under its header, in member order", () => {
    const d = district("a", 8);
    const { cards, frames, extent } = layoutDistricts([d], new Set(["a"]));
    expect(frames.get("a")).toEqual({ x: 0, y: 0, width: 1000, height: 388 });
    expect(cards.size).toBe(8);
    expect(cards.get(d.members[0]!)).toEqual({ x: 20, y: 60, width: 288, height: 84 });
    expect(cards.get(d.members[1]!)).toEqual({ x: 356, y: 60, width: 288, height: 84 });
    expect(cards.get(d.members[3]!)).toEqual({ x: 20, y: 172, width: 288, height: 84 });
    expect(cards.get(d.members[7]!)).toEqual({ x: 356, y: 284, width: 288, height: 84 });
    expect(extent).toEqual({ x: 0, y: 0, width: 1000, height: 388 });
  });

  it("stands a collapsed district for by one summary the size of a card, with no frame", () => {
    const d = district("a", 8);
    const { cards, frames } = layoutDistricts([d], new Set());
    expect(frames.size).toBe(0);
    expect([...cards]).toEqual([[summaryId("a"), { x: 0, y: 0, width: 288, height: 84 }]]);
    expect(summaryId("a")).toBe("summary:a");
  });

  it("wraps a unit that would pass the shelf, and keeps one that ends on it", () => {
    const ds = [district("a", 1), district("b", 1), district("c", 1)];
    const wrapped = layoutDistricts(ds, new Set(), 700);
    expect(wrapped.shelf).toBe(700);
    expect([...wrapped.cards.values()]).toEqual([
      { x: 0, y: 0, width: 288, height: 84 },
      { x: 384, y: 0, width: 288, height: 84 },
      { x: 0, y: 180, width: 288, height: 84 },
    ]);
    const exact = layoutDistricts(ds, new Set(), 3 * 288 + 2 * 96);
    expect([...exact.cards.values()].map((b) => b.y)).toEqual([0, 0, 0]);
    expect([...exact.cards.values()].map((b) => b.x)).toEqual([0, 384, 768]);
  });

  it("takes the default shelf from the units' area, never less than the widest unit", () => {
    const ds = [district("a", 1), district("b", 1), district("c", 1)];
    const shape = layoutDistricts(ds, new Set());
    expect(shape.shelf).toBeCloseTo(Math.sqrt(3 * (288 + 96) * (84 + 96) * (16 / 9)), 6);
    expect([...shape.cards.values()].map((b) => [b.x, b.y])).toEqual([[0, 0], [0, 180], [0, 360]]);
    const wide = layoutDistricts([district("w", 30)], new Set(["w"]));
    expect(wide.shelf).toBe(wide.frames.get("w")!.width);
  });

  it("starts the next row below the tallest unit of the row, 96 apart", () => {
    const ds = [district("a", 8), district("b", 1), district("c", 1)];
    const shape = layoutDistricts(ds, new Set(["a"]), 1500);
    // a: 1000 wide, 388 tall; b beside it at 1096; c does not fit and wraps below a
    expect(shape.cards.get(summaryId("b"))).toEqual({ x: 1096, y: 0, width: 288, height: 84 });
    expect(shape.cards.get(summaryId("c"))).toEqual({ x: 0, y: 388 + 96, width: 288, height: 84 });
  });
});

describe("the districts of the 300-repository fixture, laid out", () => {
  const { repositories, links } = ecosystem300();
  const edges = buildOverview(repositories, links).edges;
  const ds = districts(repositories, edges);
  const allOpen = new Set(ds.map((d) => d.id));
  const atRest = (viewport: { width: number; height: number }) => defaultExpanded(ds, viewport);

  const states: [string, ReadonlySet<string>][] = [
    ["nothing open", new Set()],
    ["the default at 1440x810", atRest(VIEWPORTS[0]).expanded],
    ["everything open", allOpen],
  ];

  /** Every box that sits in the row: open frames and summaries. */
  const units = (shape: DistrictLayout, expanded: ReadonlySet<string>): Box[] => [
    ...ds.filter((d) => expanded.has(d.id)).map((d) => shape.frames.get(d.id)!),
    ...ds.filter((d) => !expanded.has(d.id)).map((d) => shape.cards.get(summaryId(d.id))!),
  ];

  it("is the same twice, and the same whatever order the world arrives in", () => {
    for (const [name, expanded] of states) {
      const first = layoutDistricts(ds, expanded);
      const again = layoutDistricts(ds, expanded);
      expect([...again.cards], name).toEqual([...first.cards]);
      expect([...again.frames], name).toEqual([...first.frames]);
      expect(again.extent, name).toEqual(first.extent);
      expect(again.shelf, name).toBe(first.shelf);
    }
    const base = layoutDistricts(ds, states[1]![1]);
    for (const seed of [1, 2, 3]) {
      const other = districts(shuffled(repositories, seed), shuffled(edges, seed + 100));
      expect(other).toEqual(ds);
      const shape = layoutDistricts(other, states[1]![1]);
      expect([...shape.cards]).toEqual([...base.cards]);
      expect([...shape.frames]).toEqual([...base.frames]);
    }
  });

  it("overlaps nothing: frames and summaries apart, cards in their frame and clear of each other", () => {
    for (const [name, expanded] of states) {
      const shape = layoutDistricts(ds, expanded);
      const boxes = units(shape, expanded);
      expect(boxes, name).toHaveLength(ds.length);
      for (let i = 0; i < boxes.length; i++) {
        for (let j = i + 1; j < boxes.length; j++) {
          expect(intersects(boxes[i]!, boxes[j]!), `${name}: units ${i} and ${j}`).toBe(false);
        }
      }
      for (const d of ds) {
        const frame = shape.frames.get(d.id);
        for (const member of d.members) {
          const card = shape.cards.get(member);
          if (expanded.has(d.id)) {
            expect(frame, `${name}: ${d.id} has a frame`).toBeDefined();
            expect(card, `${name}: ${member}`).toBeDefined();
            expect(contains(frame!, card!), `${name}: ${member} in ${d.id}`).toBe(true);
          } else {
            expect(card, `${name}: ${member} is hidden`).toBeUndefined();
          }
        }
      }
      const cards = [...shape.cards.entries()];
      expect(cards.length, name).toBe(visibleCards(ds, expanded));
      for (let i = 0; i < cards.length; i++) {
        for (let j = i + 1; j < cards.length; j++) {
          expect(intersects(cards[i]![1], cards[j]![1]), `${name}: ${cards[i]![0]} and ${cards[j]![0]}`).toBe(false);
        }
      }
      // a card is clear of every frame but its own
      const owner = new Map(ds.flatMap((d) => d.members.map((m) => [m, d.id] as const)));
      for (const [districtId, frame] of shape.frames) {
        for (const [id, card] of shape.cards) {
          if (owner.get(id) === districtId) continue;
          expect(intersects(frame, card), `${name}: ${id} against ${districtId}`).toBe(false);
        }
      }
    }
  });

  it("sets row neighbours exactly one gap apart, top-aligned", () => {
    for (const [name, expanded] of states) {
      const boxes = units(layoutDistricts(ds, expanded), expanded);
      const rows = new Map<number, Box[]>();
      for (const box of boxes) rows.set(box.y, [...(rows.get(box.y) ?? []), box]);
      for (const [y, row] of rows) {
        row.sort((a, b) => a.x - b.x);
        expect(row[0]!.x, `${name}: row at ${y} starts at the left`).toBe(0);
        for (let i = 1; i < row.length; i++) {
          expect(row[i]!.x - (row[i - 1]!.x + row[i - 1]!.width), `${name}: row at ${y}`).toBe(DISTRICT.gap);
        }
      }
      const ys = [...rows.keys()].sort((a, b) => a - b);
      expect(ys[0]).toBe(0);
      ys.slice(1).forEach((y, i) => {
        const above = rows.get(ys[i]!)!;
        expect(y, `row ${i + 1}`).toBe(ys[i]! + Math.max(...above.map((b) => b.height)) + DISTRICT.gap);
      });
    }
  });

  it("moves no box of an earlier district when a later one opens or closes, the shelf held", () => {
    const base = atRest(VIEWPORTS[0]);
    const before = layoutDistricts(ds, base.expanded, base.shelf);
    ds.forEach((d, index) => {
      const toggled = new Set(base.expanded);
      if (!toggled.delete(d.id)) toggled.add(d.id);
      const after = layoutDistricts(ds, toggled, base.shelf);
      for (const earlier of ds.slice(0, index)) {
        const ids = base.expanded.has(earlier.id) ? [earlier.id, ...earlier.members] : [summaryId(earlier.id)];
        for (const id of ids) {
          const a = before.frames.get(id) ?? before.cards.get(id);
          const b = after.frames.get(id) ?? after.cards.get(id);
          expect(a, `${id} before`).toBeDefined();
          expect(b, `${id} after toggling ${d.id}`).toEqual(a);
        }
      }
    });
  });

  it("opens, on each screen, a readable set within the card cap", () => {
    for (const viewport of VIEWPORTS) {
      const result = atRest(viewport);
      const shape = layoutDistricts(ds, result.expanded, result.shelf);
      expect(shape.extent, `${viewport.width}`).toEqual(layoutDistricts(ds, result.expanded).extent);
      expect(result.readable, `${viewport.width}`).toBe(true);
      expect(visibleCards(ds, result.expanded), `${viewport.width}`).toBeLessThanOrEqual(CARD_CAP);
      expect(fitZoom(shape.extent, viewport), `${viewport.width}`).toBeGreaterThanOrEqual(READABLE_ZOOM);
    }
  });

  it("opens every district up to a size, and no further set that would still be readable", () => {
    const levels = sizeLevels(ds);
    expect(levels).toEqual([...new Set(ds.map((d) => d.members.length))].filter((n) => n <= CARD_CAP).sort((a, b) => a - b));
    for (const viewport of VIEWPORTS) {
      const result = atRest(viewport);
      const opened = new Set(ds.filter((d) => d.members.length <= (levels[result.level - 1] ?? 0)).map((d) => d.id));
      expect(result.expanded, `${viewport.width}: the level's districts`).toEqual(opened);
      const next = levels[result.level];
      if (next === undefined) continue;
      const larger = new Set(ds.filter((d) => d.members.length <= next).map((d) => d.id));
      const shape = layoutDistricts(ds, larger);
      const cards = visibleCards(ds, larger);
      const zoom = fitZoom(shape.extent, viewport);
      expect(cards > CARD_CAP || zoom < READABLE_ZOOM, `${viewport.width}: opening size ${next} (${cards} cards, zoom ${zoom})`).toBe(true);
    }
  });

  it("opens nothing when even the summaries cannot be read, and says so", () => {
    const result = defaultExpanded(ds, { width: 400, height: 300 });
    expect(result).toMatchObject({ level: 0, readable: false });
    expect(result.expanded.size).toBe(0);
  });

  it("stops at a lower card cap", () => {
    const result = defaultExpanded(ds, VIEWPORTS[2], { cardCap: ds.length });
    // 17 districts, so the only room is for the one district of a single card
    expect(result.level).toBe(1);
    expect(result.readable).toBe(true);
    expect([...result.expanded]).toEqual(["district:tree:t2-00"]);
  });

  it("reports the default on each screen", () => {
    for (const viewport of VIEWPORTS) {
      const result = atRest(viewport);
      const shape = layoutDistricts(ds, result.expanded, result.shelf);
      const zoom = fitZoom(shape.extent, viewport);
      expect(zoom).toBeGreaterThan(0);
      console.info(
        `DISTRICT_SCALE ${JSON.stringify({
          viewport,
          districts: ds.length,
          levels: sizeLevels(ds).length,
          level: result.level,
          readable: result.readable,
          expanded: result.expanded.size,
          visibleCards: visibleCards(ds, result.expanded),
          cardCap: CARD_CAP,
          readableZoom: READABLE_ZOOM,
          fitZoom: round4(zoom),
          shelf: round(result.shelf),
          extent: { width: round(shape.extent.width), height: round(shape.extent.height) },
        })}`,
      );
    }
  });
});
