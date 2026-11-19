import type { ViewEdge } from "@/lib/model/graph";

import { wholeId, type District } from "./districts";

/**
 * Isolating a district: which districts are linked to it. A district that was cut
 * into parts is isolated whole, so its parts are the core together and are never
 * neighbours of each other. Two districts are linked when some connection, of any
 * family and an inclusion too, has one end in each; what is not linked to the core
 * is hidden. Pure and total: lists follow the order of the districts they are
 * given in, so the answer does not depend on the order edges arrive in.
 */

/** A district's name without the part it is: "a · b · 2 of 3" is "a · b". */
export function wholeLabel(d: District): string {
  if (d.part === null) return d.label;
  const cut = d.label.lastIndexOf(" · ");
  return cut < 0 ? d.label : d.label.slice(0, cut);
}

const cmp = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

/**
 * By district id, the other districts a connection joins it to. Every district is
 * a key, the relation is symmetric, and no district is linked to itself. A
 * connection with an end in no district, or inside one district, links nothing.
 * Throws when a repository is a member of two districts.
 */
export function districtLinks(ds: readonly District[], edges: readonly ViewEdge[]): Map<string, Set<string>> {
  const owner = new Map<string, string>();
  for (const d of ds) {
    for (const member of d.members) {
      const held = owner.get(member);
      if (held !== undefined) {
        const [first, second] = cmp(held, d.id) <= 0 ? [held, d.id] : [d.id, held];
        throw new Error(`isolate: ${member} is in ${first} and ${second}`);
      }
      owner.set(member, d.id);
    }
  }
  const links = new Map<string, Set<string>>(ds.map((d) => [d.id, new Set<string>()]));
  for (const edge of edges) {
    const from = owner.get(edge.source);
    const to = owner.get(edge.target);
    if (from === undefined || to === undefined || from === to) continue;
    links.get(from)!.add(to);
    links.get(to)!.add(from);
  }
  return links;
}

export interface Isolation {
  /** The whole that is isolated: the id of a district that was not cut, or the id a cut district's parts share. */
  focus: string;
  /** Its name, without a part. */
  label: string;
  /** The district ids of the focus: every part of it, or the district itself. */
  core: string[];
  /** The districts outside the core that a connection joins to it. */
  neighbours: string[];
  /** Every other district. */
  hidden: string[];
}

/**
 * What isolating `focus` shows. `focus` is a district id or the id a cut
 * district's parts share; a part resolves to its whole. Null for anything else.
 * Each list is in the order of `ds`.
 */
export function isolate(
  ds: readonly District[],
  links: ReadonlyMap<string, ReadonlySet<string>>,
  focus: string,
): Isolation | null {
  const hit = ds.find((d) => d.id === focus) ?? ds.find((d) => wholeId(d) === focus);
  if (hit === undefined) return null;
  const whole = wholeId(hit);
  const inCore = new Set(ds.filter((d) => wholeId(d) === whole).map((d) => d.id));
  const linked = new Set<string>();
  for (const id of inCore) for (const other of links.get(id) ?? []) if (!inCore.has(other)) linked.add(other);
  return {
    focus: whole,
    label: wholeLabel(hit),
    core: ds.filter((d) => inCore.has(d.id)).map((d) => d.id),
    neighbours: ds.filter((d) => linked.has(d.id)).map((d) => d.id),
    hidden: ds.filter((d) => !inCore.has(d.id) && !linked.has(d.id)).map((d) => d.id),
  };
}

/** The wholes whose isolation hides at least one district: the others have nothing to hide. */
export function isolable(ds: readonly District[], links: ReadonlyMap<string, ReadonlySet<string>>): Set<string> {
  const out = new Set<string>();
  for (const whole of new Set(ds.map(wholeId))) {
    if (isolate(ds, links, whole)!.hidden.length > 0) out.add(whole);
  }
  return out;
}
