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

/** The selection a file level reads as "this line": a place opened at its line lands on it. */
export const LINE_SELECT = "line:";

/** Opens a file at a line (or at its top), as following a reference does. */
export function openAt(nav: Navigation, place: { repository: string; path: string; line_start?: number | null }): void {
  nav.go(
    { repository: place.repository, path: place.path },
    { select: place.line_start != null ? `${LINE_SELECT}${place.line_start}` : null },
  );
}
