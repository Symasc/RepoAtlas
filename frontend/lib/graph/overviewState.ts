import type { OverviewState } from "@/lib/model/location";

import { defaultExpanded, type DefaultExpansion } from "./districtLayout";
import type { District } from "./districts";

/**
 * What the overview's address asks for, settled against the districts there are:
 * which districts are open, and the shelf the rows wrap at. Pure. A stated
 * `expand` is exactly that set (what is not a district is ignored); an absent one
 * is the adaptive default for the pane, plus the districts of the repositories the
 * reader arrived on. The pane is read for the shelf and the level only, never to
 * change a stated set. Every state written back is code-unit sorted.
 */

export interface ResolvedOverview {
  /** The districts on the map, in layout order. */
  shown: District[];
  /** Ids of the open districts: a subset of the districts' ids. */
  expanded: Set<string>;
  /** Whether the address stated the set, rather than leaving it to the default. */
  explicit: boolean;
  shelf: number;
  level: number;
  readable: boolean;
}

const cmp = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

const sorted = (ids: Iterable<string>): string[] => [...new Set(ids)].sort(cmp);

// the default costs a few layouts per candidate step: a toggle does not change it, so it is kept for the last input
let kept: { districts: readonly District[]; width: number; height: number; value: DefaultExpansion } | null = null;

function adaptive(districts: readonly District[], pane: { width: number; height: number }): DefaultExpansion {
  if (kept && kept.districts === districts && kept.width === pane.width && kept.height === pane.height) return kept.value;
  const value = defaultExpanded(districts, pane);
  kept = { districts, width: pane.width, height: pane.height, value };
  return value;
}

export function resolveOverview(input: {
  districts: readonly District[];
  state: OverviewState;
  pane: { width: number; height: number };
  /** Repository node ids that were selected, or came from, on arrival. */
  pinned: readonly string[];
}): ResolvedOverview {
  const { districts, state, pane, pinned } = input;
  const shown = [...districts];
  const base = adaptive(districts, pane);
  const known = new Set(shown.map((d) => d.id));
  const expanded =
    state.expand !== null
      ? new Set(state.expand.filter((id) => known.has(id)))
      : new Set([
          ...base.expanded,
          ...shown.filter((d) => pinned.some((p) => d.members.includes(p))).map((d) => d.id),
        ]);
  return { shown, expanded, explicit: state.expand !== null, shelf: base.shelf, level: base.level, readable: base.readable };
}

/** The state with one district opened if it was closed and closed if it was open; an id not on the map changes nothing. */
export function toggleState(r: ResolvedOverview, districtId: string): OverviewState {
  const next = new Set(r.expanded);
  if (r.shown.some((d) => d.id === districtId)) {
    if (next.has(districtId)) next.delete(districtId);
    else next.add(districtId);
  }
  return { expand: sorted(next), isolate: null };
}

/** Every district on the map open. */
export function expandAllState(r: ResolvedOverview): OverviewState {
  return { expand: sorted([...r.expanded, ...r.shown.map((d) => d.id)]), isolate: null };
}

/** Every district on the map closed. */
export function collapseAllState(r: ResolvedOverview): OverviewState {
  const onMap = new Set(r.shown.map((d) => d.id));
  return { expand: sorted([...r.expanded].filter((id) => !onMap.has(id))), isolate: null };
}
