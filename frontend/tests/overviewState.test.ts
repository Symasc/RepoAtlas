import { describe, expect, it } from "vitest";

import { defaultExpanded } from "@/lib/graph/districtLayout";
import { districts } from "@/lib/graph/districts";
import { collapseAllState, expandAllState, resolveOverview, toggleState } from "@/lib/graph/overviewState";
import { buildOverview } from "@/lib/model/graph";
import { readOverviewState, type OverviewState } from "@/lib/model/location";

import { ecosystem300 } from "./fixtures/ecosystem";

/**
 * What the address asks of the overview, settled against the 300-repository
 * fixture's districts: the default, a stated set, and the state a press writes.
 */

const PANES = [
  { width: 1440, height: 810 },
  { width: 1920, height: 1080 },
  { width: 2560, height: 1440 },
] as const;

const { repositories, links } = ecosystem300();
const ds = districts(repositories, buildOverview(repositories, links).edges);
const ids = ds.map((d) => d.id);
const ABSENT: OverviewState = { expand: null, isolate: null };
const sortedIds = (list: Iterable<string>) => [...list].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));

/** A district the default leaves closed at `pane`, so that pinning it is not a no-op. */
function closedAtRest(pane: { width: number; height: number }) {
  const open = defaultExpanded(ds, pane).expanded;
  const closed = ds.find((d) => !open.has(d.id));
  expect(closed, "a district the default leaves closed").toBeDefined();
  return closed!;
}

describe("resolving the overview", () => {
  it("opens the default, and the district of what was pinned, when nothing is stated", () => {
    for (const pane of PANES) {
      const base = defaultExpanded(ds, pane);
      const closed = closedAtRest(pane);
      const none = resolveOverview({ districts: ds, state: ABSENT, pane, pinned: [] });
      expect([...none.expanded].sort(), "no pin").toEqual([...base.expanded].sort());
      expect(none.explicit).toBe(false);
      expect(none.shelf).toBe(base.shelf);
      expect(none.level).toBe(base.level);
      expect(none.readable).toBe(base.readable);
      expect(none.shown.map((d) => d.id)).toEqual(ids);

      const pinned = resolveOverview({ districts: ds, state: ABSENT, pane, pinned: [closed.members[0]!] });
      expect([...pinned.expanded].sort()).toEqual([...new Set([...base.expanded, closed.id])].sort());
      // a district id is not a repository: pinning one opens nothing
      const wrong = resolveOverview({ districts: ds, state: ABSENT, pane, pinned: [closed.id] });
      expect(wrong.expanded.has(closed.id)).toBe(false);
    }
  });

  it("opens exactly the districts stated, at any pane, whatever was pinned", () => {
    for (const pane of PANES) {
      const closed = closedAtRest(pane);
      // three districts, none of them the one the pin would open
      const stated = ids.filter((id) => id !== closed.id).slice(0, 3);
      expect(stated).toHaveLength(3);
      const r = resolveOverview({ districts: ds, state: { expand: stated, isolate: null }, pane, pinned: [closed.members[0]!] });
      expect(sortedIds(r.expanded)).toEqual(sortedIds(stated));
      expect(r.explicit).toBe(true);
      // the shelf is still the pane's
      expect(r.shelf).toBe(defaultExpanded(ds, pane).shelf);
    }
  });

  it("opens nothing for an empty list, not the default", () => {
    for (const pane of PANES) {
      const r = resolveOverview({ districts: ds, state: { expand: [], isolate: null }, pane, pinned: [ds[0]!.members[0]!] });
      expect(r.expanded.size).toBe(0);
      expect(r.explicit).toBe(true);
    }
  });

  it("ignores what is not a district", () => {
    const r = resolveOverview({
      districts: ds,
      state: { expand: ["district:nope", ids[1]!, "tree:x", "", ids[1]!], isolate: null },
      pane: PANES[0],
      pinned: [],
    });
    expect([...r.expanded]).toEqual([ids[1]]);
    for (const id of r.expanded) expect(ids).toContain(id);
  });
});

describe("the state a press writes", () => {
  const pane = PANES[0];
  const at = (expand: string[] | null) => resolveOverview({ districts: ds, state: { expand, isolate: null }, pane, pinned: [] });

  it("toggles one district in and out, keeping the others, sorted", () => {
    const r = at([ids[3]!, ids[1]!]);
    const added = toggleState(r, ids[2]!);
    expect(added.expand).toEqual(sortedIds([ids[1]!, ids[2]!, ids[3]!]));
    const removed = toggleState(r, ids[1]!);
    expect(removed.expand).toEqual([ids[3]]);
    // toggling twice is a round trip
    const back = toggleState(at(added.expand as string[]), ids[2]!);
    expect(back.expand).toEqual(sortedIds([ids[1]!, ids[3]!]));
    // an id that is not on the map changes nothing
    expect(toggleState(r, "district:nope").expand).toEqual(sortedIds([ids[1]!, ids[3]!]));
    expect(toggleState(r, "district:nope").expand).toEqual(sortedIds(r.expanded));
  });

  it("writes the default as an explicit set once it is pressed", () => {
    const r = at(null);
    const next = toggleState(r, ids[0]!);
    expect(next.expand).not.toBeNull();
    expect(sortedIds(next.expand!)).toEqual(sortedIds(r.expanded.has(ids[0]!) ? [...r.expanded].filter((i) => i !== ids[0]) : [...r.expanded, ids[0]!]));
  });

  it("expands every district, and collapses every district", () => {
    for (const expand of [null, [], [ids[0]!], ids]) {
      const r = at(expand);
      expect(expandAllState(r).expand).toEqual(sortedIds(ids));
      expect(collapseAllState(r).expand).toEqual([]);
    }
  });
});

describe("any address resolves", () => {
  it("never throws for what the address reader returns", () => {
    let seed = 99;
    const next = (n: number) => {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
      return seed % n;
    };
    const alphabet = ["%", "#", ",", ":", "district:", "tree:", "shared", "é", "日", "\uD800", "a", " "];
    const pieces = [...ids.map((id) => id.slice("district:".length)), ...alphabet];
    for (let i = 0; i < 200; i++) {
      const text = Array.from({ length: next(8) }, () => pieces[next(pieces.length)]).join(next(2) ? "," : "");
      const state = readOverviewState(new URLSearchParams({ expand: text, isolate: text }));
      const pane = PANES[next(PANES.length)]!;
      const r = resolveOverview({ districts: ds, state, pane, pinned: [] });
      for (const id of r.expanded) expect(ids).toContain(id);
      expect(() => [toggleState(r, ids[0]!), expandAllState(r), collapseAllState(r)]).not.toThrow();
    }
  });
});
