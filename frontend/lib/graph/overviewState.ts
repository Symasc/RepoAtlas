import type { OverviewState } from "@/lib/model/location";

import { defaultExpanded, type DefaultExpansion } from "./districtLayout";
import type { District } from "./districts";
import { isolate, type Isolation } from "./isolate";

/**
 * What the overview's address asks for, settled against the districts there are:
 * which districts are on the map, which are open, and the shelf the rows wrap at.
 * Pure. A stated `expand` is exactly that set (what is not a district is ignored);
 * an absent one is the adaptive default for the pane, plus the districts of the
 * repositories the reader arrived on, plus the isolated district when there is one.
 * `expand` is the open state of the whole map, districts not shown included: a
 * press changes the districts on the map and keeps the rest. The pane is read for
 * the shelf and the level only, never to change a stated set. Every state written
 * back is code-unit sorted.
 */

export interface ResolvedOverview {
  /** The districts on the map, in layout order: the isolated district's parts, then the districts linked to it; every district when none is isolated. */
  shown: District[];
  /** Ids of the open districts of the whole map: a subset of the districts' ids. */
  expanded: Set<string>;
  /** Whether the address stated the set, rather than leaving it to the default. */
  explicit: boolean;
  /** What isolating asked for, or null for the whole map (also when the address names no district). */
  isolation: Isolation | null;
  shelf: number;
  level: number;
  readable: boolean;
}

const cmp = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

const sorted = (ids: Iterable<string>): string[] => [...new Set(ids)].sort(cmp);

// The default costs a few layouts per candidate step, and an isolated map needs two (the whole map's, for the
// open set, and the shown districts', for the shelf): a toggle changes neither, so the last few are kept.
const KEPT = 4;
const kept = new Map<string, DefaultExpansion>();

function adaptive(districts: readonly District[], pane: { width: number; height: number }): DefaultExpansion {
  // what the default reads of a district: its id, whether it is a part, and how many it holds
  const key = `${pane.width}x${pane.height}\u0000${districts.map((d) => `${d.id}\u0001${d.part ? 1 : 0}\u0001${d.members.length}`).join("\u0000")}`;
  const held = kept.get(key);
  if (held !== undefined) return held;
  const value = defaultExpanded(districts, pane);
  kept.set(key, value);
  if (kept.size > KEPT) kept.delete(kept.keys().next().value!);
  return value;
}

export function resolveOverview(input: {
  districts: readonly District[];
  state: OverviewState;
  pane: { width: number; height: number };
  /** Repository node ids that were selected, or came from, on arrival. */
  pinned: readonly string[];
  /** `districtLinks` of the districts and the edges: what isolating needs to find a district's neighbours. */
  links: ReadonlyMap<string, ReadonlySet<string>>;
}): ResolvedOverview {
  const { districts, state, pane, pinned, links } = input;
  const isolation = state.isolate === null ? null : isolate(districts, links, state.isolate);
  const byId = new Map(districts.map((d) => [d.id, d]));
  const shown = isolation === null ? [...districts] : [...isolation.core, ...isolation.neighbours].map((id) => byId.get(id)!);
  const whole = adaptive(districts, pane);
  // the shelf, the level and whether it is readable are those of the districts on the map
  const base = isolation === null ? whole : adaptive(shown, pane);
  const expanded =
    state.expand !== null
      ? new Set(state.expand.filter((id) => byId.has(id)))
      : new Set([
          ...whole.expanded,
          ...districts.filter((d) => pinned.some((p) => d.members.includes(p))).map((d) => d.id),
          ...(isolation?.core ?? []),
        ]);
  return { shown, expanded, explicit: state.expand !== null, isolation, shelf: base.shelf, level: base.level, readable: base.readable };
}

/** What isolating keeps in the address: the whole that is isolated, or nothing. */
const isolateOf = (r: ResolvedOverview): string | null => r.isolation?.focus ?? null;

/** The state with one district on the map opened if it was closed and closed if it was open; an id not on the map changes nothing. */
export function toggleState(r: ResolvedOverview, districtId: string): OverviewState {
  const next = new Set(r.expanded);
  if (r.shown.some((d) => d.id === districtId)) {
    if (next.has(districtId)) next.delete(districtId);
    else next.add(districtId);
  }
  return { expand: sorted(next), isolate: isolateOf(r) };
}

/** Every district on the map open; those not shown keep their state. */
export function expandAllState(r: ResolvedOverview): OverviewState {
  return { expand: sorted([...r.expanded, ...r.shown.map((d) => d.id)]), isolate: isolateOf(r) };
}

/** Every district on the map closed; those not shown keep their state. */
export function collapseAllState(r: ResolvedOverview): OverviewState {
  const onMap = new Set(r.shown.map((d) => d.id));
  return { expand: sorted([...r.expanded].filter((id) => !onMap.has(id))), isolate: isolateOf(r) };
}

/**
 * The state that isolates a district (a part isolates its whole): the core is open,
 * and everything open stays open. Null for an id that is not a district.
 */
export function isolateState(r: ResolvedOverview, ds: readonly District[], districtId: string): OverviewState | null {
  // the core of a whole does not depend on what is linked to it
  const isolation = isolate(ds, new Map(), districtId);
  if (isolation === null) return null;
  return { expand: sorted([...r.expanded, ...isolation.core]), isolate: isolation.focus };
}

/** The whole map again, with what is open kept as it is. */
export function showAllState(r: ResolvedOverview): OverviewState {
  return { expand: sorted(r.expanded), isolate: null };
}
