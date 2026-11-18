import type { EdgeType } from "@/lib/api/generated";
import { FAMILIES, type Family, type LinkData, type ViewEdge } from "@/lib/model/graph";

import { summaryId } from "./districtLayout";
import type { District } from "./districts";

/**
 * Edge lifting: which lines the overview draws when some districts are
 * collapsed. A collapsed district is one card, so a link to one of its
 * repositories is drawn to that card, and the links that now join the same two
 * cards in one family merge into one line. A link inside one collapsed district
 * is counted on that district instead; an inclusion (submodule) link that does
 * not stay inside one is counted as nesting, never drawn. Every input edge lands
 * in exactly one of unplaced, internal, nesting or a drawn line, so the counts
 * are conserved. Pure and total: the answer does not depend on the order edges
 * or districts arrive in (every tie is broken in code-unit order).
 */

/** Links, and the sum of their counts. */
export interface Tally {
  links: number;
  count: number;
}
export type FamilyTallies = Partial<Record<Family, Tally>>;

export interface LiftedEdge extends ViewEdge {
  /** Underlying ViewEdge ids, code-unit sorted; never empty. */
  from: string[];
  /** True when id is a lift id (an endpoint is a summary, or more than one edge merged). */
  lifted: boolean;
}

export interface Lift {
  edges: LiftedEdge[];
  /** Collapsed district id -> its internal links by family; only districts with at least one; keys code-unit sorted. */
  internal: Map<string, FamilyTallies>;
  /** Inclusion (submodule) links that are not internal: never lines. */
  nesting: Tally;
  /** Input edge ids with an endpoint in no district, or source === target; code-unit sorted. */
  unplaced: string[];
  /** Every input edge id -> the drawn edge id carrying it, or null; keys code-unit sorted. */
  carriedBy: Map<string, string | null>;
  /** Every member repository id -> the visible card standing for it; keys code-unit sorted. */
  cardOf: Map<string, string>;
}

/** Code-unit order: the same on every machine, which a locale-aware comparison is not. */
const cmp = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

/** Examples kept per merged line; mirrors the cap in lib/model/graph.ts. */
const EXAMPLES = 6;

/** The id of the line that merges every `family` link from one card to another. */
export function liftId(family: Family, source: string, target: string): string {
  return `lift:${family}:${source}->${target}`;
}

/** The two districts are named in code-unit order, so the message does not depend on the order they arrived in. */
function conflict(member: string, a: string, b: string): Error {
  const [first, second] = cmp(a, b) <= 0 ? [a, b] : [b, a];
  return new Error(`lift: ${member} is in ${first} and ${second}`);
}

/**
 * The card standing for each member repository: the repository itself in an open
 * district, the district's summary in a collapsed one. Throws when one repository
 * is a member of two districts.
 */
export function cardsOf(ds: readonly District[], expanded: ReadonlySet<string>): Map<string, string> {
  const holder = new Map<string, string>();
  const card = new Map<string, string>();
  for (const d of ds) {
    const standing = expanded.has(d.id) ? null : summaryId(d.id);
    for (const member of d.members) {
      const held = holder.get(member);
      if (held !== undefined) throw conflict(member, held, d.id);
      holder.set(member, d.id);
      card.set(member, standing ?? member);
    }
  }
  return new Map([...card].sort(([a], [b]) => cmp(a, b)));
}

function bump(tally: Tally, edge: ViewEdge): void {
  tally.links += 1;
  tally.count += edge.link.count;
}

function add(tallies: FamilyTallies, edge: ViewEdge): void {
  bump((tallies[edge.link.family] ??= { links: 0, count: 0 }), edge);
}

function copyLink(link: LinkData): LinkData {
  const copy: LinkData = { ...link, types: link.types.map((t) => ({ ...t })), examples: [...link.examples], packages: [...link.packages] };
  if (link.inclusions) copy.inclusions = link.inclusions.map((i) => ({ ...i }));
  return copy;
}

/** One line's link, from the links it merges (in id order). */
function combine(family: Family, members: readonly ViewEdge[]): LinkData {
  const counts = new Map<EdgeType, number>();
  const packages = new Set<string>();
  const examples = new Set<string>();
  let count = 0;
  for (const { link } of members) {
    count += link.count;
    for (const t of link.types) counts.set(t.type, (counts.get(t.type) ?? 0) + t.count);
    for (const name of link.packages) packages.add(name);
    for (const ref of link.examples) if (examples.size < EXAMPLES) examples.add(ref);
  }
  const first = members[0]!.link;
  const link: LinkData = {
    kind: first.kind,
    family,
    count,
    types: [...counts].map(([type, n]) => ({ type, count: n })).sort((a, b) => b.count - a.count || cmp(a.type, b.type)),
    packages: [...packages],
    examples: [...examples],
  };
  if (members.length === 1 && first.nestedPath !== undefined) link.nestedPath = first.nestedPath;
  return link;
}

interface Group {
  family: Family;
  source: string;
  target: string;
  members: ViewEdge[];
}

export function liftEdges(edges: readonly ViewEdge[], ds: readonly District[], expanded: ReadonlySet<string>): Lift {
  const owner = new Map<string, District>();
  for (const d of ds) {
    for (const member of d.members) {
      const held = owner.get(member);
      if (held !== undefined) throw conflict(member, held.id, d.id);
      owner.set(member, d);
    }
  }
  const cardOf = cardsOf(ds, expanded);

  const internal = new Map<string, FamilyTallies>();
  const internalOf = (id: string): FamilyTallies => {
    let tallies = internal.get(id);
    if (tallies === undefined) {
      tallies = {};
      internal.set(id, tallies);
    }
    return tallies;
  };
  const nesting: Tally = { links: 0, count: 0 };
  const unplaced: string[] = [];
  const carried = new Map<string, string | null>();
  const groups = new Map<string, Group>();

  let previous: string | undefined;
  for (const edge of [...edges].sort((a, b) => cmp(a.id, b.id))) {
    if (edge.id === previous) throw new Error(`lift: duplicate edge id ${edge.id}`);
    previous = edge.id;
    const from = owner.get(edge.source);
    const to = owner.get(edge.target);
    if (!from || !to || edge.source === edge.target) {
      unplaced.push(edge.id);
      carried.set(edge.id, null);
    } else if (from === to && !expanded.has(from.id)) {
      add(internalOf(from.id), edge);
      carried.set(edge.id, null);
    } else if (edge.link.family === "submodule") {
      bump(nesting, edge);
      carried.set(edge.id, null);
    } else {
      const family = edge.link.family;
      const source = cardOf.get(edge.source)!;
      const target = cardOf.get(edge.target)!;
      const key = `${family}\u0000${source}\u0000${target}`;
      const group = groups.get(key);
      if (group) group.members.push(edge);
      else groups.set(key, { family, source, target, members: [edge] });
    }
  }

  const lines: LiftedEdge[] = [];
  for (const { family, source, target, members } of groups.values()) {
    const only = members[0]!;
    if (members.length === 1 && only.source === source && only.target === target) {
      lines.push({
        id: only.id,
        source: only.source,
        target: only.target,
        link: copyLink(only.link),
        ...(only.scope ? { scope: only.scope } : {}),
        from: [only.id],
        lifted: false,
      });
      carried.set(only.id, only.id);
    } else {
      const id = liftId(family, source, target);
      lines.push({ id, source, target, link: combine(family, members), from: members.map((m) => m.id), lifted: true });
      for (const member of members) carried.set(member.id, id);
    }
  }
  lines.sort(
    (a, b) => FAMILIES.indexOf(a.link.family) - FAMILIES.indexOf(b.link.family) || cmp(a.source, b.source) || cmp(a.target, b.target),
  );

  return {
    edges: lines,
    internal: new Map([...internal].sort(([a], [b]) => cmp(a, b))),
    nesting,
    unplaced: unplaced.sort(cmp),
    carriedBy: new Map([...carried].sort(([a], [b]) => cmp(a, b))),
    cardOf,
  };
}
