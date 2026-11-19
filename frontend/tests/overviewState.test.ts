import { describe, expect, it } from "vitest";

import { defaultExpanded } from "@/lib/graph/districtLayout";
import { districts, wholeId } from "@/lib/graph/districts";
import { districtLinks, isolate } from "@/lib/graph/isolate";
import {
  collapseAllState,
  expandAllState,
  isolateState,
  resolveOverview,
  showAllState,
  toggleState,
} from "@/lib/graph/overviewState";
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
const edges = buildOverview(repositories, links).edges;
const ds = districts(repositories, edges);
const adjacency = districtLinks(ds, edges);
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
      const none = resolveOverview({ districts: ds, state: ABSENT, pane, pinned: [], links: adjacency });
      expect([...none.expanded].sort(), "no pin").toEqual([...base.expanded].sort());
      expect(none.explicit).toBe(false);
      expect(none.shelf).toBe(base.shelf);
      expect(none.level).toBe(base.level);
      expect(none.readable).toBe(base.readable);
      expect(none.shown.map((d) => d.id)).toEqual(ids);

      const pinned = resolveOverview({ districts: ds, state: ABSENT, pane, pinned: [closed.members[0]!], links: adjacency });
      expect([...pinned.expanded].sort()).toEqual([...new Set([...base.expanded, closed.id])].sort());
      // a district id is not a repository: pinning one opens nothing
      const wrong = resolveOverview({ districts: ds, state: ABSENT, pane, pinned: [closed.id], links: adjacency });
      expect(wrong.expanded.has(closed.id)).toBe(false);
    }
  });

  it("opens exactly the districts stated, at any pane, whatever was pinned", () => {
    for (const pane of PANES) {
      const closed = closedAtRest(pane);
      // three districts, none of them the one the pin would open
      const stated = ids.filter((id) => id !== closed.id).slice(0, 3);
      expect(stated).toHaveLength(3);
      const r = resolveOverview({
        districts: ds,
        state: { expand: stated, isolate: null },
        pane,
        pinned: [closed.members[0]!],
        links: adjacency,
      });
      expect(sortedIds(r.expanded)).toEqual(sortedIds(stated));
      expect(r.explicit).toBe(true);
      // the shelf is still the pane's
      expect(r.shelf).toBe(defaultExpanded(ds, pane).shelf);
    }
  });

  it("opens nothing for an empty list, not the default", () => {
    for (const pane of PANES) {
      const r = resolveOverview({
        districts: ds,
        state: { expand: [], isolate: null },
        pane,
        pinned: [ds[0]!.members[0]!],
        links: adjacency,
      });
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
      links: adjacency,
    });
    expect([...r.expanded]).toEqual([ids[1]]);
    for (const id of r.expanded) expect(ids).toContain(id);
  });
});

describe("the state a press writes", () => {
  const pane = PANES[0];
  const at = (expand: string[] | null) =>
    resolveOverview({ districts: ds, state: { expand, isolate: null }, pane, pinned: [], links: adjacency });

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

describe("resolving an isolated district", () => {
  const wholes = [...new Set(ds.map(wholeId))];
  /** A whole to isolate, preferring the first inclusion tree, and a pane at which the default leaves some of it closed. */
  function closedFocus() {
    const order = ["district:tree:t1-00", ...wholes];
    for (const whole of order) {
      const iso = isolate(ds, adjacency, whole);
      if (iso === null || iso.neighbours.length === 0 || iso.hidden.length === 0) continue;
      for (const pane of PANES) {
        const open = defaultExpanded(ds, pane).expanded;
        if (iso.core.some((id) => !open.has(id))) return { whole, iso, pane, open };
      }
    }
    throw new Error("no district to isolate has parts the default leaves closed, at any pane");
  }
  const { whole, iso, pane, open } = closedFocus();
  const resolve = (state: OverviewState, pinned: readonly string[] = []) =>
    resolveOverview({ districts: ds, state, pane, pinned, links: adjacency });
  const shownIds = (r: { shown: { id: string }[] }) => r.shown.map((d) => d.id);

  it("shows the district's parts and then the districts linked to it, and keeps the rest out", () => {
    const r = resolve({ expand: null, isolate: whole });
    expect(r.isolation).toEqual(iso);
    expect(shownIds(r)).toEqual([...iso.core, ...iso.neighbours]);
    for (const id of iso.hidden) expect(shownIds(r)).not.toContain(id);
    // the shelf is that of the districts on the map, not of the whole map
    expect(r.shelf).toBe(defaultExpanded(r.shown, pane).shelf);
    expect(r.level).toBe(defaultExpanded(r.shown, pane).level);
  });

  it("opens the default of the whole map, the districts pinned, and the isolated district, when nothing is stated", () => {
    const pinned = ds.find((d) => !open.has(d.id) && !iso.core.includes(d.id))!;
    expect(pinned, "a district the default leaves closed, apart from the isolated one").toBeDefined();
    const r = resolve({ expand: null, isolate: whole }, [pinned.members[0]!]);
    expect(sortedIds(r.expanded)).toEqual(sortedIds(new Set([...open, ...iso.core, pinned.id])));
    expect(r.explicit).toBe(false);
    // with nothing pinned, the core is still opened, and nothing else is
    expect(sortedIds(resolve({ expand: null, isolate: whole }).expanded)).toEqual(sortedIds(new Set([...open, ...iso.core])));
  });

  it("opens exactly the districts stated, the isolated one not forced open", () => {
    const stated = ids.filter((id) => !iso.core.includes(id)).slice(0, 3);
    // even a repository of the isolated district, pinned, opens nothing that is not stated
    const r = resolve({ expand: stated, isolate: whole }, [ds.find((d) => d.id === iso.core[0])!.members[0]!]);
    expect(sortedIds(r.expanded)).toEqual(sortedIds(stated));
    for (const id of iso.core) expect(r.expanded.has(id)).toBe(false);
    expect(r.explicit).toBe(true);
    // districts not shown stay in what is stated
    const hidden = resolve({ expand: [iso.hidden[0]!], isolate: whole });
    expect([...hidden.expanded]).toEqual([iso.hidden[0]]);
  });

  it("shows the whole map for an isolate that names no district", () => {
    const stated = ids.slice(0, 2);
    for (const expand of [null, stated]) {
      const none = resolve({ expand, isolate: null });
      for (const unknown of ["district:nope", "district:tree:t1-00#99", "tree:t1-00"]) {
        const r = resolve({ expand, isolate: unknown });
        expect(r.isolation, unknown).toBeNull();
        expect(shownIds(r), unknown).toEqual(ids);
        expect(sortedIds(r.expanded), unknown).toEqual(sortedIds(none.expanded));
        expect(r.shelf, unknown).toBe(none.shelf);
        // and a press writes no isolate for it
        expect(toggleState(r, ids[0]!).isolate, unknown).toBeNull();
      }
    }
  });

  it("isolates the whole when the address names one of its parts", () => {
    const part = ds.find((d) => d.part !== null && wholeId(d) === whole);
    expect(part, "the isolated district is cut into parts").toBeDefined();
    for (const name of [whole, ...iso.core]) {
      const r = resolve({ expand: null, isolate: name });
      expect(r.isolation, name).toEqual(iso);
      expect(shownIds(r), name).toEqual([...iso.core, ...iso.neighbours]);
    }
  });

  it("keeps the isolation through a press, and changes only what is on the map", () => {
    const hiddenId = iso.hidden[0]!;
    const shownNeighbour = iso.neighbours[0]!;
    const r = resolve({ expand: [hiddenId, shownNeighbour], isolate: whole });
    // Collapse all closes what is shown and keeps a district that is not
    const collapsed = collapseAllState(r);
    expect(collapsed.expand).toEqual([hiddenId]);
    expect(collapsed.isolate).toBe(whole);
    // Expand all opens what is shown and keeps a district that is not, whatever its state
    const expandedAll = expandAllState(resolve({ expand: [hiddenId], isolate: whole }));
    expect(expandedAll.expand).toEqual(sortedIds([hiddenId, ...iso.core, ...iso.neighbours]));
    expect(expandedAll.isolate).toBe(whole);
    // a toggle changes one district on the map, and one that is not on it changes nothing
    const toggled = toggleState(r, shownNeighbour);
    expect(toggled.expand).toEqual([hiddenId]);
    expect(toggled.isolate).toBe(whole);
    expect(toggleState(r, hiddenId).expand).toEqual(sortedIds([hiddenId, shownNeighbour]));
    expect(toggleState(r, hiddenId).isolate).toBe(whole);
  });

  it("isolates and comes back with what was open kept open", () => {
    const r0 = resolve({ expand: null, isolate: null });
    const part = iso.core[iso.core.length - 1]!;
    const isolated = isolateState(r0, ds, part)!;
    expect(isolated.isolate).toBe(whole);
    expect(isolated.expand).toEqual(sortedIds(new Set([...r0.expanded, ...iso.core])));
    const r1 = resolve(isolated);
    expect(r1.isolation).toEqual(iso);
    expect(sortedIds(r1.expanded)).toEqual(isolated.expand);
    const shown = showAllState(r1);
    expect(shown.isolate).toBeNull();
    expect(shown.expand).toEqual(sortedIds(r1.expanded));
    const r2 = resolve(shown);
    expect(r2.isolation).toBeNull();
    expect(shownIds(r2)).toEqual(ids);
    for (const id of r0.expanded) expect(r2.expanded.has(id), id).toBe(true);
    for (const id of iso.core) expect(r2.expanded.has(id), id).toBe(true);
    // what is not a district is not isolated
    expect(isolateState(r0, ds, "district:nope")).toBeNull();
    expect(isolateState(r0, ds, "")).toBeNull();
  });
});

describe("any isolated address resolves", () => {
  it("never throws, and every state it writes names only districts", () => {
    let seed = 7;
    const next = (n: number) => {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
      return seed % n;
    };
    const tokens = ids.map((id) => id.slice("district:".length));
    const alphabet = ["%", "#", ",", ":", "district:", "tree:", "shared", "é", "\uD800", " ", ""];
    const pieces = [...tokens, ...alphabet];
    for (let i = 0; i < 200; i++) {
      const text = Array.from({ length: next(6) }, () => pieces[next(pieces.length)]).join(next(2) ? "," : "");
      const focus = next(3) === 0 ? text : tokens[next(tokens.length)]!;
      const state = readOverviewState(new URLSearchParams({ expand: text, isolate: focus }));
      const pane = PANES[next(PANES.length)]!;
      const r = resolveOverview({ districts: ds, state, pane, pinned: [], links: adjacency });
      for (const id of [...r.expanded, ...r.shown.map((d) => d.id)]) expect(ids).toContain(id);
      const written = [
        toggleState(r, ids[next(ids.length)]!),
        expandAllState(r),
        collapseAllState(r),
        showAllState(r),
        isolateState(r, ds, focus) ?? isolateState(r, ds, ids[0]!)!,
      ];
      for (const w of written) {
        for (const id of w.expand ?? []) expect(ids).toContain(id);
        if (w.isolate !== null) expect(ids.map((id) => id.replace(/#\d+$/, ""))).toContain(w.isolate);
      }
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
      const r = resolveOverview({ districts: ds, state, pane, pinned: [], links: adjacency });
      for (const id of r.expanded) expect(ids).toContain(id);
      expect(() => [toggleState(r, ids[0]!), expandAllState(r), collapseAllState(r)]).not.toThrow();
    }
  });
});
