import { describe, expect, it } from "vitest";

import { defaultExpanded, layoutDistricts, summaryId } from "@/lib/graph/districtLayout";
import { districts } from "@/lib/graph/districts";
import { liftEdges } from "@/lib/graph/lift";
import { overviewView, visibleEdge, visibleSelection } from "@/lib/graph/overviewView";
import { buildOverview, FAMILIES, type Family } from "@/lib/model/graph";

import { ecosystem300 } from "./fixtures/ecosystem";

/**
 * The overview view on the 300-repository fixture: the layout and the lifted
 * lines agree with each other and with their parts, and a selection is only ever
 * looked up, never a reason for the map to change.
 */

const VIEWPORTS = [
  { width: 1440, height: 810 },
  { width: 1920, height: 1080 },
  { width: 2560, height: 1440 },
] as const;

describe("the overview view of the 300-repository fixture", () => {
  const { repositories, links } = ecosystem300();
  const edges = buildOverview(repositories, links).edges;
  const ds = districts(repositories, edges);
  const members = ds.flatMap((d) => d.members);

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

  it("is its layout and its lift, side by side", () => {
    for (const [name, expanded] of states) {
      const shelf = layoutDistricts(ds, expanded).shelf;
      const view = overviewView({ districts: ds, edges, expanded, shelf });
      const shape = layoutDistricts(ds, expanded, shelf);
      expect([...view.layout.cards], name).toEqual([...shape.cards]);
      expect([...view.layout.frames], name).toEqual([...shape.frames]);
      expect(view.layout.extent, name).toEqual(shape.extent);
      expect(view.layout.shelf, name).toBe(shelf);
      const { layout, ...lifted } = view;
      expect(layout).toBe(view.layout);
      expect(lifted, name).toEqual(liftEdges(edges, ds, expanded));
    }
  });

  it("draws every line between two cards that are on the map", () => {
    for (const [name, expanded] of states) {
      const view = overviewView({ districts: ds, edges, expanded, shelf: layoutDistricts(ds, expanded).shelf });
      for (const line of view.edges) {
        expect(view.layout.cards.has(line.source), `${name}: ${line.id} source`).toBe(true);
        expect(view.layout.cards.has(line.target), `${name}: ${line.id} target`).toBe(true);
      }
      for (const card of view.cardOf.values()) expect(view.layout.cards.has(card), `${name}: ${card}`).toBe(true);
      expect(view.cardOf.size, name).toBe(members.length);
    }
  });

  describe("what a selection stands for", () => {
    const open = ds[0]!;
    const expanded = new Set([open.id]);
    const view = overviewView({ districts: ds, edges, expanded, shelf: layoutDistricts(ds, expanded).shelf });

    it("is nothing for nothing, and a summary for itself", () => {
      expect(visibleSelection(view, null)).toBeNull();
      for (const d of ds.filter((x) => !expanded.has(x.id))) expect(visibleSelection(view, summaryId(d.id))).toBe(summaryId(d.id));
    });

    it("is the repository itself when its district is open, and its district's summary when not", () => {
      for (const member of open.members) expect(visibleSelection(view, member)).toBe(member);
      for (const d of ds.filter((x) => !expanded.has(x.id))) {
        for (const member of d.members) expect(visibleSelection(view, member)).toBe(summaryId(d.id));
      }
    });

    it("is the summary of the member's own part, for a district that was cut", () => {
      const parts = ds.filter((d) => d.part !== null && !expanded.has(d.id));
      expect(parts.length).toBeGreaterThan(1);
      for (const part of parts) {
        const siblings = parts.filter((p) => p !== part);
        for (const member of part.members) {
          expect(visibleSelection(view, member)).toBe(summaryId(part.id));
          for (const sibling of siblings) expect(visibleSelection(view, member)).not.toBe(summaryId(sibling.id));
        }
      }
    });

    it("is nothing for what is not on the map", () => {
      expect(visibleSelection(view, "repo:nowhere")).toBeNull();
      expect(visibleSelection(view, "summary:nowhere")).toBeNull();
      expect(visibleSelection(view, "")).toBeNull();
    });
  });

  describe("what an edge stands for", () => {
    const view = overviewView({ districts: ds, edges, expanded: new Set(), shelf: layoutDistricts(ds, new Set()).shelf });

    it("is nothing for nothing, and nothing for an id that is not an edge", () => {
      expect(visibleEdge(view, null)).toBeNull();
      expect(visibleEdge(view, "observed:nowhere->nothing")).toBeNull();
      expect(visibleEdge(view, "lift:code:summary:nowhere->summary:nothing")).toBeNull();
    });

    it("is the line carrying it, for every link that is drawn", () => {
      const lifted = view.edges.filter((l) => l.from.length > 1);
      expect(lifted.length).toBeGreaterThan(0);
      for (const line of view.edges) for (const id of line.from) expect(visibleEdge(view, id), id).toBe(line.id);
    });

    it("is nothing for a link that is counted instead of drawn", () => {
      expect(view.internal.size).toBeGreaterThan(0);
      expect(view.nesting.links).toBeGreaterThan(0);
      const counted = [...view.carriedBy].filter(([, line]) => line === null).map(([id]) => id);
      expect(counted.length).toBeGreaterThan(0);
      for (const id of counted) expect(visibleEdge(view, id), id).toBeNull();
    });

    it("is itself, for a line's own id", () => {
      expect(view.edges.length).toBeGreaterThan(0);
      for (const line of view.edges) expect(visibleEdge(view, line.id), line.id).toBe(line.id);
    });
  });

  it("never moves the map: looking a selection up changes nothing", () => {
    const expanded = defaultExpanded(ds, VIEWPORTS[0]).expanded;
    const view = overviewView({ districts: ds, edges, expanded, shelf: layoutDistricts(ds, expanded).shelf });
    const before = structuredClone(view);
    for (const member of members) visibleSelection(view, member);
    for (const d of ds.slice(0, 5)) visibleSelection(view, summaryId(d.id));
    for (const edge of edges) visibleEdge(view, edge.id);
    for (const line of view.edges) visibleEdge(view, line.id);
    expect(view).toEqual(before);
  });

  it("reports how many lines each screen draws at rest", () => {
    for (const viewport of VIEWPORTS) {
      const { expanded, shelf } = defaultExpanded(ds, viewport);
      const view = overviewView({ districts: ds, edges, expanded, shelf });
      const byFamily: Partial<Record<Family, number>> = {};
      for (const family of FAMILIES) {
        const n = view.edges.filter((l) => l.link.family === family).length;
        if (n > 0) byFamily[family] = n;
      }
      const internalLinks = [...view.internal.values()].reduce(
        (sum, tallies) => sum + Object.values(tallies).reduce((s, t) => s + t.links, 0),
        0,
      );
      console.info(
        "OVERVIEW_LINES " +
          JSON.stringify({ viewport, lines: view.edges.length, byFamily, internalLinks, nestingLinks: view.nesting.links }),
      );
    }
  });
});
