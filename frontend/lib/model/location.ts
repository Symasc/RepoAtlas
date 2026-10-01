/**
 * Where the reader is: `?repo=<slug>&path=<dir or file>&trace=<seed>&sel=<node>`.
 * No repository is the overview; a path is a directory or a file inside one; a
 * trace is the chain of what a change to one thing reaches, followed from where
 * it was asked. The selection is part of the address but not of the history:
 * choosing a thing replaces the entry, going somewhere pushes one, so Back climbs
 * out and lands on what was chosen there.
 */

export interface Place {
  repository: string | null;
  path: string;
  /** An entity key, or `dir:<path>` for a directory of the place's repository. */
  trace?: string | null;
}

/** The prefix a directory's trace is written with: directories have no entity key. */
export const DIRECTORY_TRACE = "dir:";

export function placeKey(place: Place): string {
  return `${place.repository ?? ""}\u0000${place.path}\u0000${place.trace ?? ""}`;
}

/** 0 the overview, 1 a repository's root, then one per segment; a trace one deeper still. */
export function depth(place: Place): number {
  if (place.repository === null) return place.trace ? 1 : 0;
  return 1 + (place.path ? place.path.split("/").length : 0) + (place.trace ? 1 : 0);
}

export function direction(from: Place, to: Place): "in" | "out" | "across" | "none" {
  if (placeKey(from) === placeKey(to)) return "none";
  const a = depth(from);
  const b = depth(to);
  if (b > a) return "in";
  if (b < a) return "out";
  return "across";
}

export function search(place: Place, selected: string | null = null): string {
  const params = new URLSearchParams();
  if (place.repository !== null) params.set("repo", place.repository);
  if (place.repository !== null && place.path) params.set("path", place.path);
  if (place.trace) params.set("trace", place.trace);
  if (selected) params.set("sel", selected);
  const query = params.toString();
  return query ? `?${query}` : "";
}

/**
 * What impact is asked about for a trace, across the workspace: an entity key,
 * or a directory as impact itself lists one, `<repository>:<path>/`.
 */
export function traceSeed(place: Place): string | null {
  if (!place.trace) return null;
  if (place.trace.startsWith(DIRECTORY_TRACE)) {
    if (place.repository === null) return null;
    return `${place.repository}:${place.trace.slice(DIRECTORY_TRACE.length)}/`;
  }
  return place.trace;
}
