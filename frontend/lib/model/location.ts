/**
 * Where the reader is: `?repo=<slug>&path=<dir or file>&sel=<node>`. No
 * repository is the overview; a path is a directory or a file inside one. The
 * selection is part of the address but not of the history: choosing a thing
 * replaces the entry, going somewhere pushes one, so Back climbs out and lands
 * on what was chosen there.
 */

export interface Place {
  repository: string | null;
  path: string;
}

export function placeKey(place: Place): string {
  return `${place.repository ?? ""}\u0000${place.path}`;
}

/** 0 the overview, 1 a repository's root, then one per segment. */
export function depth(place: Place): number {
  if (place.repository === null) return 0;
  return 1 + (place.path ? place.path.split("/").length : 0);
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
  if (selected) params.set("sel", selected);
  const query = params.toString();
  return query ? `?${query}` : "";
}
