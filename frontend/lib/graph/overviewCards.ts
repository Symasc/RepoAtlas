import { formatCount, plural } from "@/lib/model/format";
import { FAMILIES, type Family, type ViewEdge } from "@/lib/model/graph";

import { summaryId } from "./districtLayout";
import type { District, DistrictKind } from "./districts";
import { INCLUSION_KINDS, type InclusionKind, type InclusionMark } from "./inclusionMarks";
import type { Isolation } from "./isolate";
import type { FamilyTallies, Tally } from "./lift";
import { visibleEdge, type OverviewView } from "./overviewView";

/**
 * What the overview's cards say, as plain values: a district's frame, a
 * collapsed district's summary, what the map leaves undrawn and why. Pure, so the
 * words are tested without a browser and the components only place them.
 */

/** Repository names a summary lists before "+N". */
export const SAMPLE = 3;

/** The link families, as a sentence names them. */
export const FAMILY_WORD: Record<Family, string> = {
  code: "code",
  http: "HTTP",
  dependency: "package",
  config: "configuration",
  submodule: "inclusion",
  nested: "nested-repository",
};

/** Code-unit order: the same on every machine, which a locale-aware comparison is not. */
const cmp = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

// -- an open district's frame -------------------------------------------------------------

export interface FrameFacts {
  id: string;
  label: string;
  kind: DistrictKind;
  members: number;
  /** Set only when the district was cut to a readable size. */
  part: { index: number; of: number } | null;
  /** For an inclusion tree: what kinds of declaration join its members, and how deep it goes. */
  inclusion: { kinds: InclusionKind[]; labels: string[]; depth: number } | null;
}

export function frameFacts(d: District, marks: ReadonlyMap<string, InclusionMark>): FrameFacts {
  let inclusion: FrameFacts["inclusion"] = null;
  if (d.kind === "tree") {
    const found = d.members.flatMap((m) => {
      const mark = marks.get(m);
      return mark === undefined ? [] : [mark];
    });
    const kinds = INCLUSION_KINDS.filter((k) => found.some((m) => m.kind === k));
    // by kind order, then by words; a label with no kind (an inclusion that states nothing) comes last
    const order = (kind: InclusionKind | null): number => (kind === null ? INCLUSION_KINDS.length : INCLUSION_KINDS.indexOf(kind));
    const labelled = new Map<string, InclusionKind | null>();
    for (const m of found) if (m.label !== null && !labelled.has(m.label)) labelled.set(m.label, m.kind);
    const labels = [...labelled].sort(([a, ka], [b, kb]) => order(ka) - order(kb) || cmp(a, b)).map(([label]) => label);
    const depths = found.map((m) => m.depth);
    inclusion = { kinds, labels, depth: depths.length === 0 ? 0 : Math.max(...depths) - Math.min(...depths) };
  }
  return {
    id: d.id,
    label: d.label,
    kind: d.kind,
    members: d.members.length,
    part: d.part === null ? null : { index: d.part.index, of: d.part.of },
    inclusion,
  };
}

export function frameTitle(f: FrameFacts): string {
  return `${f.label} · ${formatCount(f.members)}`;
}

/** What joins a tree's members, in words; null for a district that is not a tree. */
export function frameMeta(f: FrameFacts): string | null {
  if (f.inclusion === null) return null;
  const { labels, depth } = f.inclusion;
  if (labels.length === 0) return "Inclusion tree";
  return labels.join(" · ") + (depth > 0 ? ` · depth ${depth}` : "");
}

// -- a collapsed district's summary --------------------------------------------------------

export interface SummaryFacts {
  /** The summary's own node id. */
  id: string;
  /** The district it stands for. */
  district: string;
  label: string;
  kind: DistrictKind;
  members: number;
  /** The first members' names, in display order. */
  sample: string[];
  /** How many members the sample leaves out. */
  more: number;
  /** The lines to other cards that touch it: the links they carry, by family. */
  crossing: FamilyTallies;
  /** The links inside it, by family. */
  internal: FamilyTallies;
  /** The links to districts that are not shown, by family. */
  outside: FamilyTallies;
}

function copy(tallies: FamilyTallies): FamilyTallies {
  const out: FamilyTallies = {};
  for (const family of FAMILIES) {
    const tally = tallies[family];
    if (tally !== undefined) out[family] = { links: tally.links, count: tally.count };
  }
  return out;
}

export function summaryFacts(view: OverviewView, d: District, names: ReadonlyMap<string, string>): SummaryFacts {
  const id = summaryId(d.id);
  const crossing: FamilyTallies = {};
  for (const line of view.edges) {
    if (line.source !== id && line.target !== id) continue;
    const tally = (crossing[line.link.family] ??= { links: 0, count: 0 });
    tally.links += line.from.length;
    tally.count += line.link.count;
  }
  const sample = d.members.slice(0, SAMPLE).map((m) => names.get(m) ?? m);
  return {
    id,
    district: d.id,
    label: d.label,
    kind: d.kind,
    members: d.members.length,
    sample,
    more: d.members.length - sample.length,
    crossing,
    internal: copy(view.internal.get(d.id) ?? {}),
    outside: copy(view.outside.get(d.id) ?? {}),
  };
}

/** `3 code links, 1 HTTP link`: the families that have links, in reading order. */
function tallyList(tallies: FamilyTallies): string {
  return FAMILIES.flatMap((family) => {
    const n = tallies[family]?.links ?? 0;
    return n > 0 ? [`${formatCount(n)} ${FAMILY_WORD[family]} ${n === 1 ? "link" : "links"}`] : [];
  }).join(", ");
}

/** The whole of a summary, in one sentence, for a reader who does not see its lines. */
export function summaryLabel(f: SummaryFacts): string {
  let text = `${f.label}: ${plural(f.members, "repository", "repositories")}`;
  const crossing = tallyList(f.crossing);
  if (crossing !== "") text += `; to other districts: ${crossing}`;
  const internal = tallyList(f.internal);
  if (internal !== "") text += `; inside: ${internal}`;
  const outside = tallyList(f.outside);
  if (outside !== "") text += `; to districts not shown: ${outside}`;
  return text;
}

// -- what the map does not draw ------------------------------------------------------------

export interface NotDrawn {
  /** Inclusions, shown by nesting instead of a line. */
  nesting: Tally;
  /** Links inside collapsed districts, summed over every family. */
  internal: Tally;
  /** How many collapsed districts hold them. */
  internalDistricts: number;
  /**
   * Links with exactly one end shown, summed over every family. Links with both ends hidden are counted by the lift
   * as `beyond` and are not reported here, because the note describes what is on screen.
   */
  outside: Tally;
  /** Links with an end on no card. */
  unplaced: number;
}

export function notDrawn(view: OverviewView): NotDrawn {
  const internal: Tally = { links: 0, count: 0 };
  for (const tallies of view.internal.values()) {
    for (const tally of Object.values(tallies)) {
      internal.links += tally.links;
      internal.count += tally.count;
    }
  }
  const outside: Tally = { links: 0, count: 0 };
  for (const tallies of view.outside.values()) {
    for (const tally of Object.values(tallies)) {
      outside.links += tally.links;
      outside.count += tally.count;
    }
  }
  return {
    nesting: { links: view.nesting.links, count: view.nesting.count },
    internal,
    internalDistricts: view.internal.size,
    outside,
    unplaced: view.unplaced.length,
  };
}

/** One line for the status bar: what is counted but not drawn; null when nothing is left out. */
export function notDrawnText(n: NotDrawn, ambiguous: number): string | null {
  const parts: string[] = [];
  if (ambiguous > 0) parts.push(`${plural(ambiguous, "package")} with more than one possible publisher`);
  if (n.nesting.links > 0) parts.push(`${plural(n.nesting.links, "inclusion")}, shown by nesting`);
  if (n.internal.links > 0) {
    parts.push(`${plural(n.internal.links, "link")} inside ${plural(n.internalDistricts, "collapsed district")}`);
  }
  if (n.outside.links > 0) parts.push(`${plural(n.outside.links, "link")} to districts not shown`);
  if (n.unplaced > 0) parts.push(`${plural(n.unplaced, "link")} with an end not on the map`);
  return parts.length === 0 ? null : `Not drawn: ${parts.join(" · ")}`;
}

export type NotDrawnReason =
  | { kind: "inside"; district: District }
  | { kind: "nesting" }
  | { kind: "hidden" }
  | { kind: "unplaced" };

/** Why a connection has no line of its own, or null when it is drawn. Decided in the order `liftEdges` decides. */
export function whyNotDrawn(view: OverviewView, ds: readonly District[], edge: ViewEdge): NotDrawnReason | null {
  if (visibleEdge(view, edge.id) !== null) return null;
  if (view.unplaced.includes(edge.id)) return { kind: "unplaced" };
  const from = ds.find((d) => d.members.includes(edge.source));
  const to = ds.find((d) => d.members.includes(edge.target));
  if (from !== undefined && to !== undefined && !(view.shown.has(from.id) && view.shown.has(to.id))) return { kind: "hidden" };
  if (from !== undefined && from === to && !view.layout.frames.has(from.id)) return { kind: "inside", district: from };
  if (edge.link.family === "submodule") return { kind: "nesting" };
  return { kind: "unplaced" };
}

export function whyNotDrawnText(r: NotDrawnReason): string {
  switch (r.kind) {
    case "inside":
      return `The chosen connection is not drawn: both ends are inside ${r.district.label}, which is collapsed`;
    case "nesting":
      return "The chosen connection is not drawn: it is an inclusion, shown by nesting";
    case "hidden":
      return "The chosen connection is not drawn: an end is in a district not shown";
    case "unplaced":
      return "The chosen connection is not drawn: an end is not on the map";
  }
}

// -- isolating a district ------------------------------------------------------------------

/** The notice above an isolated map: what is shown and how much is not. */
export function isolationText(i: Isolation): string {
  return `Isolated: ${i.label} and ${plural(i.neighbours.length, "linked district")} · ${plural(i.hidden.length, "district")} not shown`;
}

/** The status when the selected repository is in a district that is not on the map. */
export function hiddenSelectionText(name: string): string {
  return `The selected repository, ${name}, is in a district not shown`;
}
