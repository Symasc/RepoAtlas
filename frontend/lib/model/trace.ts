import type { ImpactCandidate, ImpactResponse } from "@/lib/api/generated";

import type { Family, LinkData, ViewEdge } from "./graph";

/**
 * A trace: the chain of what a change to one thing reaches, as impact walked it.
 * Pure, so the shape is tested without a browser. The seed stands at the left;
 * every step away is a column to its right; in a column, each file the chain
 * reaches is one card listing the things in it and the line where each is
 * reached. A card belongs to the column of its step, so a file reached at two
 * distances appears twice, once where each step lands. Lines run from the card
 * holding a candidate's predecessor to the card holding it.
 */

export const SEED_ID = "seed";

export interface TraceRow {
  /** `c:<candidate id>`, the row's selection id. */
  id: string;
  candidate: ImpactCandidate;
  /** The line of the step that reaches it, in the file that asserts the step. */
  line: number | null;
  /** How the change travels into it: "uses", "calls", "imports"… */
  reading: string;
  crossing: "package" | "http" | null;
  test: boolean;
}

export interface TraceCard {
  id: string;
  /** The repository holding it, `null` for something no repository owns. */
  repository: string | null;
  /** The file, or `null` for a thing with no file (an environment variable, an endpoint). */
  path: string | null;
  name: string;
  distance: number;
  rows: TraceRow[];
  /** Only tests reach here. */
  test: boolean;
}

export interface TraceColumn {
  distance: number;
  cards: TraceCard[];
}

export interface TraceSeedView {
  name: string;
  type: string | null;
  repository: string | null;
  path: string | null;
  lines: [number, number] | null;
  unit: string | null;
  /** The term asked about, as the reader gave it. */
  term: string;
}

export interface TraceGraph {
  seed: TraceSeedView;
  columns: TraceColumn[];
  edges: ViewEdge[];
  /** Repositories the chain stands in, the seed's first. */
  repositories: string[];
  /** Card holding each row, by row id. */
  cardOf: Map<string, string>;
  rows: Map<string, TraceRow>;
  /** Everything listed, tests included. */
  listed: number;
}

export function rowId(candidate: number): string {
  return `c:${candidate}`;
}

function pair(lines: unknown): [number, number] | null {
  if (!Array.isArray(lines) || lines.length < 2) return null;
  const [a, b] = lines;
  return typeof a === "number" && typeof b === "number" ? [a, b] : null;
}

function cardId(candidate: ImpactCandidate, distance: number): string {
  if (candidate.path) return `f:${distance}:${candidate.repository ?? ""}:${candidate.path}`;
  return `e:${distance}:${candidate.entity}`;
}

function nameOf(candidate: ImpactCandidate): string {
  if (candidate.path) return candidate.path.slice(candidate.path.lastIndexOf("/") + 1);
  return candidate.name;
}

function rowOf(candidate: ImpactCandidate, test: boolean): TraceRow {
  const step = candidate.step;
  return {
    id: rowId(candidate.id),
    candidate,
    line: pair(step.lines)?.[0] ?? null,
    reading: step.reading,
    crossing: step.crossing ? "package" : step.http ? "http" : null,
    test,
  };
}

function familyOfStep(row: TraceRow): Family {
  if (row.crossing === "http" || row.candidate.step.edge?.includes("HTTP")) return "http";
  if (row.candidate.step.edge === "DEPENDS_ON") return "dependency";
  if (row.candidate.step.edge === "READS" || row.candidate.step.edge === "CONFIGURES") return "config";
  return "code";
}

function byLine(a: TraceRow, b: TraceRow): number {
  return (a.line ?? 0) - (b.line ?? 0) || a.candidate.name.localeCompare(b.candidate.name);
}

export function buildTrace(impact: ImpactResponse): TraceGraph {
  const seed = impact.seed;
  const view: TraceSeedView = {
    name:
      seed.granularity === "subtree"
        ? (seed.path ?? seed.term)
        : seed.granularity === "file" && seed.path
          ? seed.path.slice(seed.path.lastIndexOf("/") + 1)
          : nameFromKey(seed.entity ?? seed.term),
    type: seed.type ?? null,
    repository: seed.repository ?? null,
    path: seed.path ?? null,
    lines: pair(seed.lines),
    unit: seed.unit ?? null,
    term: seed.term,
  };
  const everyone = [
    ...(impact.candidates ?? []).map((c) => ({ c, test: false })),
    ...(impact.tests ?? []).map((c) => ({ c, test: true })),
  ];
  const byId = new Map(everyone.map(({ c }) => [c.id, c]));

  const cards = new Map<string, TraceCard>();
  const cardOf = new Map<string, string>();
  const rows = new Map<string, TraceRow>();
  for (const { c, test } of everyone) {
    const id = cardId(c, c.distance);
    let card = cards.get(id);
    if (!card) {
      card = { id, repository: c.repository ?? null, path: c.path ?? null, name: nameOf(c), distance: c.distance, rows: [], test };
      cards.set(id, card);
    }
    const row = rowOf(c, test);
    card.rows.push(row);
    card.test &&= test;
    cardOf.set(row.id, id);
    rows.set(row.id, row);
  }
  for (const card of cards.values()) card.rows.sort(byLine);

  // one line per (from card, to card, family), counting the steps it carries
  const lines = new Map<string, ViewEdge & { readings: Set<string> }>();
  for (const { c } of everyone) {
    const to = cardOf.get(rowId(c.id))!;
    const before = c.from === 0 ? null : byId.get(c.from);
    const from = before ? cardOf.get(rowId(before.id)) : SEED_ID;
    if (!from || from === to) continue;
    const row = rows.get(rowId(c.id))!;
    const family = familyOfStep(row);
    const id = `${from}=>${to}:${family}`;
    const existing = lines.get(id);
    if (existing) {
      existing.link.count += 1;
      existing.readings.add(row.reading);
      continue;
    }
    const link: LinkData = {
      kind: family === "http" ? "http" : family === "dependency" ? "dependency" : "observed",
      family,
      count: 1,
      types: c.step.edge ? [{ type: c.step.edge, count: 1 }] : [],
      packages: c.step.crossing ? [c.step.crossing.package] : [],
      examples: [],
    };
    lines.set(id, { id, source: from, target: to, link, readings: new Set([row.reading]) });
  }
  const edges: ViewEdge[] = [...lines.values()].map(({ readings, ...edge }) => ({
    ...edge,
    link: { ...edge.link, label: [...readings].sort().join(" · ") },
  }));

  const columns = new Map<number, TraceCard[]>();
  for (const card of cards.values()) columns.set(card.distance, [...(columns.get(card.distance) ?? []), card]);
  const repositories = [
    ...new Set([view.repository, ...[...cards.values()].map((c) => c.repository)].filter((r): r is string => !!r)),
  ];
  const order = (a: TraceCard, b: TraceCard) =>
    Number(a.test) - Number(b.test) ||
    repositories.indexOf(a.repository ?? "") - repositories.indexOf(b.repository ?? "") ||
    (a.path ?? a.name).localeCompare(b.path ?? b.name);
  return {
    seed: view,
    columns: [...columns.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([distance, list]) => ({ distance, cards: list.sort(order) })),
    edges,
    repositories,
    cardOf,
    rows,
    listed: everyone.length,
  };
}

/**
 * A unit as a reader names it: impact writes `<repository>:<manifest folder>`,
 * with `.` for a repository's root.
 */
export function unitLabel(unit: string): string {
  const colon = unit.indexOf(":");
  if (colon === -1) return unit;
  const repository = unit.slice(0, colon);
  const folder = unit.slice(colon + 1);
  return folder === "." || folder === "" ? repository : `${repository} / ${folder}`;
}

/** A boundary a step crosses, `<unit> -> <unit>`, in the same words. */
export function boundaryLabel(boundary: string): string {
  return boundary.split(" -> ").map(unitLabel).join(" → ");
}

/** A readable name from an entity key: `sym:repo:path:Name:12` reads `Name`. */
export function nameFromKey(key: string): string {
  // a directory, as impact lists one: `<repository>:<path>/`
  if (key.endsWith("/")) return key.slice(0, -1).split(/[:/]/).pop()!;
  const parts = key.split(":");
  if (parts[0] === "sym" && parts.length >= 5) return parts[parts.length - 2]!;
  if (parts[0] === "file" && parts.length >= 3) return parts.slice(2).join(":").split("/").pop()!;
  return parts.length > 1 ? parts.slice(1).join(":") : key;
}

/** The predecessors of a candidate back to the seed, nearest last. */
export function chainOf(graph: TraceGraph, row: TraceRow): TraceRow[] {
  const out: TraceRow[] = [];
  let at: TraceRow | undefined = row;
  const seen = new Set<string>();
  while (at && !seen.has(at.id)) {
    seen.add(at.id);
    out.unshift(at);
    at = at.candidate.from === 0 ? undefined : graph.rows.get(rowId(at.candidate.from));
  }
  return out;
}

/** `repository/path` as a step's `at` writes it, split back; `null` when it names neither. */
export function splitAt(at: string | null | undefined, repositories: readonly string[]): { repository: string; path: string } | null {
  if (!at) return null;
  const owner = [...repositories].sort((a, b) => b.length - a.length).find((r) => at.startsWith(`${r}/`));
  return owner ? { repository: owner, path: at.slice(owner.length + 1) } : null;
}
