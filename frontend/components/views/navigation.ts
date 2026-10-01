import type { Direction } from "@/components/graph/transition";
import type { Place } from "@/lib/model/location";

/** What a level view is told about where it is, and what it may do about it. */
export interface Navigation {
  place: Place;
  /** The selected node, from the address. */
  selected: string | null;
  /** How the reader arrived here, and from where. */
  arrival: { direction: Direction; previous: Place | null };
  /**
   * Somewhere else. `via` is the node being gone into (the view zooms into it,
   * and it stays selected here for the way back); `select` is chosen there on arrival.
   */
  go: (to: Place, options?: { via?: string | null; select?: string | null }) => void;
  select: (id: string | null) => void;
  /** Whether `id` is selected now, in an event handler: ahead of any render. */
  isSelected: (id: string) => boolean;
}
