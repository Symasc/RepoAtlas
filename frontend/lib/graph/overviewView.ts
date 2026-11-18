import type { ViewEdge } from "@/lib/model/graph";

import { layoutDistricts, type DistrictLayout } from "./districtLayout";
import type { District } from "./districts";
import { liftEdges, type Lift } from "./lift";

/**
 * The overview as one value: where the cards go (the districts laid out) and what
 * lines join them (the edges lifted onto the visible cards). Pure, and a function
 * of districts, edges, which districts are open and the shelf alone: what is
 * selected is not an input, so selecting never moves the map. A selection is
 * mapped to what is visible afterwards, by lookups that change nothing.
 */

export interface OverviewView extends Lift {
  layout: DistrictLayout;
}

export function overviewView(input: {
  districts: readonly District[];
  edges: readonly ViewEdge[];
  expanded: ReadonlySet<string>;
  shelf: number;
}): OverviewView {
  const { districts, edges, expanded, shelf } = input;
  return { ...liftEdges(edges, districts, expanded), layout: layoutDistricts(districts, expanded, shelf) };
}

/**
 * The card standing for what is selected: a repository maps to itself when its
 * district is open and to the district's summary when it is collapsed; a summary
 * id stands for itself. Anything else is not on the map.
 */
export function visibleSelection(view: OverviewView, selected: string | null): string | null {
  if (selected === null) return null;
  const card = view.cardOf.get(selected);
  if (card !== undefined) return card;
  return view.layout.cards.has(selected) ? selected : null;
}

/**
 * The line standing for an edge: the drawn line carrying it, or null when it is
 * counted instead (internal, nesting, unplaced). A drawn line's own id stands for
 * itself; anything else is not on the map.
 */
export function visibleEdge(view: OverviewView, edgeId: string | null): string | null {
  if (edgeId === null) return null;
  if (view.carriedBy.has(edgeId)) return view.carriedBy.get(edgeId) ?? null;
  return view.edges.some((e) => e.id === edgeId) ? edgeId : null;
}
