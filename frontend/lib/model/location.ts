/**
 * Where the reader is: `?repo=<slug>&path=<dir or file>&trace=<seed>&isolate=<district>&expand=<districts>&sel=<node>`.
 * No repository is the overview; a path is a directory or a file inside one; a
 * trace is the chain of what a change to one thing reaches, followed from where
 * it was asked. The selection is part of the address but not of the history:
 * choosing a thing replaces the entry, going somewhere pushes one, so Back climbs
 * out and lands on what was chosen there.
 *
 * The overview's own state is in the address too, and only there: `isolate` and
 * `expand` are written when the place is the overview and ignored (never written)
 * anywhere else. Their values are district ids with the `district:` prefix removed
 * (an id never holds a comma: its parts are `encodeURIComponent`ed). `expand`
 * absent means the adaptive default, `expand=` means nothing is expanded, and
 * otherwise it is exactly the districts listed, comma-separated, code-unit sorted.
 * `isolate` absent or empty is the whole map. Everything is encoded and decoded by
 * `URLSearchParams` alone, once: a token is never decoded again.
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

/**
 * What the overview is asked to show, as the address states it and not yet as it
 * resolves against the districts: `expand` null is the adaptive default, a list is
 * exactly those district ids; `isolate` null is the whole map.
 */
export interface OverviewState {
  expand: readonly string[] | null;
  isolate: string | null;
}

export const DEFAULT_OVERVIEW: OverviewState = { expand: null, isolate: null };

/** What every district id starts with; the address leaves it out. */
export const DISTRICT_PREFIX = "district:";

/** Whether the place is the overview: no repository, and not a trace asked from the workspace. */
export function isOverview(place: Place): boolean {
  return place.repository === null && !place.trace;
}

const cmp = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

function token(id: string): string {
  return id.startsWith(DISTRICT_PREFIX) ? id.slice(DISTRICT_PREFIX.length) : id;
}

function districtId(value: string): string {
  return value.startsWith(DISTRICT_PREFIX) ? value : `${DISTRICT_PREFIX}${value}`;
}

/**
 * The overview state an address states. Total: no string makes it throw. The
 * values come out of `URLSearchParams.get` already decoded, and are only split
 * and given their prefix back.
 */
export function readOverviewState(params: { get(name: string): string | null }): OverviewState {
  const stated = params.get("expand");
  const expand =
    stated === null
      ? null
      : [...new Set(stated.split(",").filter((t) => t !== "").map(districtId))];
  const isolated = params.get("isolate");
  return { expand, isolate: isolated === null || isolated === "" ? null : districtId(isolated) };
}

export function search(
  place: Place,
  selected: string | null = null,
  overview: OverviewState = DEFAULT_OVERVIEW,
): string {
  const params = new URLSearchParams();
  if (place.repository !== null) params.set("repo", place.repository);
  if (place.repository !== null && place.path) params.set("path", place.path);
  if (place.trace) params.set("trace", place.trace);
  if (isOverview(place)) {
    const isolated = overview.isolate ? token(overview.isolate) : "";
    if (isolated) params.set("isolate", isolated);
    if (overview.expand !== null) {
      params.set("expand", [...new Set(overview.expand.map(token).filter((t) => t !== ""))].sort(cmp).join(","));
    }
  }
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
