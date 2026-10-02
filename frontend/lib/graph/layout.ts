import {
  FILE_ID,
  FILE_ROWS,
  FILES_ID,
  FRAME_ID,
  repositoryId,
  type Box,
  type FamilyCounts,
  type FileGraph,
  type OpenedGraph,
  type ViewEdge,
} from "@/lib/model/graph";
import type { RepositoryModel } from "@/lib/model/repositories";
import { SEED_ID, type TraceCard, type TraceGraph } from "@/lib/model/trace";

/**
 * Sizes and positions, computed rather than measured: every node's height
 * follows from what it lists, so a layout is known before anything renders and
 * is checked for overlap in unit tests.
 */

export const SIZE = {
  repository: { width: 288, height: 84 },
  hub: { width: 184, height: 184 },
  card: { width: 236 },
  shared: { width: 236, height: 48 },
  peer: { width: 220, height: 56 },
  place: { width: 220, height: 52 },
  more: { width: 220, height: 44 },
  anchor: { width: 300, height: 108 },
  neighbour: { width: 244, height: 56 },
} as const;

export const CARD = { header: 44, row: 26, padding: 8, footer: 30 } as const;
export const FRAME = { header: 64, padding: 24 } as const;
export const HUB_ID = "hub";
export const GROUP_ID = "group:unlinked";

export type Layout = Map<string, Box>;

function boxAt(cx: number, cy: number, size: { width: number; height: number }): Box {
  return { x: cx - size.width / 2, y: cy - size.height / 2, width: size.width, height: size.height };
}

export function bounds(boxes: Iterable<Box>): Box | null {
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  for (const b of boxes) {
    x0 = Math.min(x0, b.x);
    y0 = Math.min(y0, b.y);
    x1 = Math.max(x1, b.x + b.width);
    y1 = Math.max(y1, b.y + b.height);
  }
  return x0 === Infinity ? null : { x: x0, y: y0, width: x1 - x0, height: y1 - y0 };
}

// -- the overview: a constellation around an anchor ---------------------------------

export interface Ring {
  id: string;
  /** Radii of the ellipse through the centres of this ring's nodes. */
  rx: number;
  ry: number;
}

export interface Constellation {
  layout: Layout;
  rings: Ring[];
  /** The repository at the centre; `null` puts the workspace hub there. */
  anchor: string | null;
  /** The frame holding repositories with no links, when others have some. */
  group: Box | null;
}

/** Rings are ellipses, wider than tall, as screens and cards are. */
export const ASPECT = 1.7;
/** Clear space kept between any two nodes. */
const MARGIN = { x: 72, y: 36 } as const;
const RING_STEP = 128;
/** Nodes per ring when a ring is filled by count rather than by links. */
const CAPACITY = [8, 14, 20, 26];

function overlaps(a: Box, b: Box): boolean {
  return (
    a.x < b.x + b.width + MARGIN.x &&
    b.x < a.x + a.width + MARGIN.x &&
    a.y < b.y + b.height + MARGIN.y &&
    b.y < a.y + a.height + MARGIN.y
  );
}

/** Even angles, starting at the left; four (or eight…) sit on the diagonals. */
export function evenAngles(n: number): number[] {
  const step = (2 * Math.PI) / Math.max(n, 1);
  const start = Math.PI + (n % 4 === 0 ? step / 2 : 0);
  return Array.from({ length: n }, (_, i) => start + i * step);
}

/**
 * Places `ids` at `angles` on the smallest ring at least `minimum` tall on which
 * none of them overlaps another or anything already `placed`.
 */
function ring(ids: readonly string[], angles: readonly number[], minimum: number, placed: readonly Box[]) {
  let ry = minimum;
  let boxes = new Map<string, Box>();
  for (let attempt = 0; attempt < 120; attempt++) {
    boxes = new Map(
      ids.map((id, k) => [id, boxAt(Math.cos(angles[k]!) * ry * ASPECT, Math.sin(angles[k]!) * ry, SIZE.repository)]),
    );
    const list = [...boxes.values()];
    const clash = list.some(
      (b, k) => placed.some((p) => overlaps(b, p)) || list.some((c, j) => j > k && overlaps(b, c)),
    );
    if (!clash) break;
    ry *= 1.06;
  }
  return { boxes, ry };
}

/** Undirected link weight between repositories: every kind counts towards nearness. */
function weights(edges: readonly ViewEdge[]): Map<string, Map<string, number>> {
  const w = new Map<string, Map<string, number>>();
  const bump = (a: string, b: string, n: number) => {
    const m = w.get(a) ?? new Map<string, number>();
    m.set(b, (m.get(b) ?? 0) + n);
    w.set(a, m);
  };
  for (const e of edges) {
    if (e.source === e.target) continue;
    bump(e.source, e.target, e.link.count);
    bump(e.target, e.source, e.link.count);
  }
  return w;
}

/** The repository with the most partners (then the most links): the natural centre. */
export function mostConnected(ids: readonly string[], w: Map<string, Map<string, number>>): string | null {
  let best: string | null = null;
  let score: [number, number] = [-1, -1];
  for (const id of [...ids].sort()) {
    const m = w.get(id);
    if (!m) continue;
    const s: [number, number] = [m.size, [...m.values()].reduce((a, b) => a + b, 0)];
    if (s[0] > score[0] || (s[0] === score[0] && s[1] > score[1])) {
      best = id;
      score = s;
    }
  }
  return best;
}

/** Fills rings by count, outward, each the smallest that clears the last. */
function rings(ids: readonly string[], placed: Box[], firstMinimum: number, out: Ring[], layout: Layout): void {
  let start = 0;
  let minimum = firstMinimum;
  for (let k = 0; start < ids.length; k++) {
    const size = CAPACITY[Math.min(k, CAPACITY.length - 1)]!;
    const slice = ids.slice(start, start + size);
    const { boxes, ry } = ring(slice, evenAngles(slice.length), minimum, placed);
    for (const [id, box] of boxes) {
      layout.set(id, box);
      placed.push(box);
    }
    out.push({ id: `orbit:${out.length}`, rx: ry * ASPECT, ry });
    minimum = ry + RING_STEP;
    start += size;
  }
}

/** Every repository joined to `seed` by links, at any distance. */
function component(seed: string, w: Map<string, Map<string, number>>): string[] {
  const queue = [seed];
  const seen = new Set(queue);
  for (let i = 0; i < queue.length; i++) {
    for (const id of (w.get(queue[i]!) ?? new Map<string, number>()).keys()) {
      if (!seen.has(id)) {
        seen.add(id);
        queue.push(id);
      }
    }
  }
  return queue.sort();
}

/**
 * One component around `anchor`, centred on the origin: its partners on the
 * first ring, theirs on the next, each inside its parent's wedge so lines run
 * outward rather than across. Rings are recorded in `out` when it is given.
 */
function radial(anchor: string, w: Map<string, Map<string, number>>, out: Ring[] | null): Layout {
  const layout: Layout = new Map();
  const centre = boxAt(0, 0, SIZE.repository);
  layout.set(anchor, centre);
  const placed: Box[] = [centre];

  // breadth-first from the anchor, strongest partners first
  const level = new Map<string, number>([[anchor, 0]]);
  const parent = new Map<string, string>();
  const order: string[] = [anchor];
  for (let i = 0; i < order.length; i++) {
    const at = order[i]!;
    const next = [...(w.get(at) ?? new Map<string, number>()).entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    for (const [id] of next) {
      if (level.has(id)) continue;
      level.set(id, level.get(at)! + 1);
      parent.set(id, at);
      order.push(id);
    }
  }
  const kids = new Map<string, string[]>();
  for (const id of order.slice(1)) {
    const p = parent.get(id)!;
    kids.set(p, [...(kids.get(p) ?? []), id]);
  }
  // a wedge as wide as the subtree it holds
  const weightOf = new Map<string, number>();
  for (const id of [...order].reverse()) {
    const children = kids.get(id) ?? [];
    weightOf.set(id, Math.max(1, children.reduce((sum, c) => sum + weightOf.get(c)!, 0)));
  }
  const wedge = new Map<string, [number, number]>();
  const first = kids.get(anchor) ?? [];
  const total = first.reduce((sum, id) => sum + weightOf.get(id)!, 0);
  let cursor = first.length > 0 ? Math.PI - (Math.PI * weightOf.get(first[0]!)!) / total : 0;
  for (const id of first) {
    const span = (2 * Math.PI * weightOf.get(id)!) / total;
    wedge.set(id, [cursor, cursor + span]);
    cursor += span;
  }
  const depth = Math.max(...level.values());
  let minimum = SIZE.repository.height + 40;
  for (let k = 1; k <= depth; k++) {
    const members = order.filter((id) => level.get(id) === k);
    if (k > 1) {
      for (const p of order.filter((id) => level.get(id) === k - 1)) {
        const [a, b] = wedge.get(p)!;
        const children = kids.get(p) ?? [];
        const sum = children.reduce((s, c) => s + weightOf.get(c)!, 0);
        let at = a;
        for (const c of children) {
          const span = ((b - a) * weightOf.get(c)!) / sum;
          wedge.set(c, [at, at + span]);
          at += span;
        }
      }
    }
    const angles = members.map((id) => {
      const [a, b] = wedge.get(id)!;
      return (a + b) / 2;
    });
    const { boxes, ry } = ring(members, angles, minimum, placed);
    for (const [id, box] of boxes) {
      layout.set(id, box);
      placed.push(box);
    }
    out?.push({ id: `orbit:${out.length}`, rx: ry * ASPECT, ry });
    minimum = ry + RING_STEP;
  }
  return layout;
}

/**
 * Small constellations beside the main one, each moved there whole: in a
 * column to its right, so the overview keeps the shape of a wide screen.
 */
function pack(clusters: readonly Layout[], placed: Box[], layout: Layout): void {
  if (clusters.length === 0) return;
  const gap = { x: 110, y: 90 };
  const span = bounds(placed)!;
  const boxes = clusters.map((c) => bounds(c.values())!);
  // rows of clusters, no wider than the widest one or two side by side
  const limit = Math.max(...boxes.map((b) => b.width), 720);
  const rows: { cluster: Layout; box: Box }[][] = [[]];
  clusters.forEach((cluster, i) => {
    const row = rows[rows.length - 1]!;
    const width = row.reduce((sum, r) => sum + r.box.width + gap.x, 0) + boxes[i]!.width;
    if (row.length > 0 && width > limit) rows.push([]);
    rows[rows.length - 1]!.push({ cluster, box: boxes[i]! });
  });
  const heights = rows.map((row) => Math.max(...row.map((r) => r.box.height)));
  const total = heights.reduce((sum, h) => sum + h, 0) + gap.y * (rows.length - 1);
  let top = span.y + span.height / 2 - total / 2;
  const left = span.x + span.width + 160;
  rows.forEach((row, k) => {
    let x = left;
    for (const { cluster, box } of row) {
      const dx = x - box.x;
      const dy = top + (heights[k]! - box.height) / 2 - box.y;
      for (const [id, b] of cluster) {
        const moved = { ...b, x: b.x + dx, y: b.y + dy };
        layout.set(id, moved);
        placed.push(moved);
      }
      x += box.width + gap.x;
    }
    top += heights[k]! + gap.y;
  });
}

/**
 * The overview's layout. The anchor (the selected repository, else the one with
 * the most partners) sits at the centre of its component's constellation;
 * components linked only among themselves form their own, smaller, beside it.
 * Repositories with no links at all sit in their own group below those; when
 * no repository has a link, they ring the workspace hub instead.
 */
export function constellation(
  repositories: readonly RepositoryModel[],
  edges: readonly ViewEdge[],
  requested: string | null,
): Constellation {
  const ids = repositories.map((r) => repositoryId(r.slug));
  const w = weights(edges);
  const linked = ids.filter((id) => (w.get(id)?.size ?? 0) > 0);
  const anchor = requested !== null && ids.includes(requested) ? requested : mostConnected(linked, w);
  const layout: Layout = new Map();
  const out: Ring[] = [];

  if (anchor === null) {
    const hub = boxAt(0, 0, SIZE.hub);
    layout.set(HUB_ID, hub);
    rings(ids, [hub], SIZE.hub.height / 2 + 70, out, layout);
    return { layout, rings: out, anchor: null, group: null };
  }

  const placed: Box[] = [];

  if (!w.has(anchor)) {
    const centre = boxAt(0, 0, SIZE.repository);
    layout.set(anchor, centre);
    placed.push(centre);
    // an unlinked repository chosen as the anchor: the rest ring it, linked
    // ones kept together so their lines stay short
    rings(
      [...linked, ...ids.filter((id) => id !== anchor && !linked.includes(id))],
      placed,
      SIZE.repository.height + 60,
      out,
      layout,
    );
    return { layout, rings: out, anchor, group: null };
  }

  // the anchor's component, ring by ring around it
  const main = radial(anchor, w, out);
  for (const [id, box] of main) {
    layout.set(id, box);
    placed.push(box);
  }

  // components linked only among themselves: each its own small constellation,
  // beside the main one, largest first
  const seen = new Set(main.keys());
  const clusters: Layout[] = [];
  for (const id of linked) {
    if (seen.has(id)) continue;
    const members = component(id, w);
    for (const m of members) seen.add(m);
    clusters.push(radial(mostConnected(members, w)!, w, null));
  }
  clusters.sort((a, b) => b.size - a.size);
  pack(clusters, placed, layout);

  // no links at all: their own group, beneath
  const independent = ids.filter((id) => !w.has(id));
  let group: Box | null = null;
  if (independent.length > 0) {
    const { width, height } = SIZE.repository;
    const gap = { x: 28, y: 24 };
    const above = bounds(placed)!;
    // as many to a row as fit under what is above, at least four
    const fit = Math.floor((above.width + gap.x) / (width + gap.x));
    const columns = Math.min(independent.length, Math.max(4, fit));
    const inner = columns * width + (columns - 1) * gap.x;
    const centreX = above.x + above.width / 2;
    const top = above.y + above.height + 96;
    const label = 40;
    independent.forEach((id, i) => {
      const column = i % columns;
      const row = Math.floor(i / columns);
      layout.set(id, {
        x: centreX - inner / 2 + column * (width + gap.x),
        y: top + label + row * (height + gap.y),
        width,
        height,
      });
    });
    const rows = Math.ceil(independent.length / columns);
    group = { x: centreX - inner / 2 - 20, y: top, width: inner + 40, height: label + rows * height + (rows - 1) * gap.y + 20 };
    layout.set(GROUP_ID, group);
  }
  return { layout, rings: out, anchor, group };
}

// -- a repository opened at a directory: a frame of cards, side columns ----------------

const GAP = { column: 24, row: 28, side: 150, stack: 14 } as const;
const MAX_COLUMNS = 5;

export function hasSummary(summary: FamilyCounts): boolean {
  return Object.values(summary).some((n) => (n ?? 0) > 0);
}

/** Height of a card listing `rows` entries, a "more" row, and a footer of link counts. */
export function cardHeight(rows: number, more: boolean, summary: boolean): number {
  return CARD.header + (rows + (more ? 1 : 0)) * CARD.row + CARD.padding + (summary ? CARD.footer : 0);
}

/** A column of nodes, centred on `cy`. */
function column(ids: readonly { id: string; height: number }[], x: number, width: number, cy: number, layout: Layout) {
  const total = ids.reduce((sum, n) => sum + n.height, 0) + Math.max(ids.length - 1, 0) * GAP.stack;
  let y = cy - total / 2;
  for (const n of ids) {
    layout.set(n.id, { x, y, width, height: n.height });
    y += n.height + GAP.stack;
  }
}

export function layoutOpened(graph: OpenedGraph): Layout {
  const layout: Layout = new Map();
  const cards: { id: string; height: number }[] = graph.directories.map((d) => ({
    id: d.id,
    height:
      d.preview === null
        ? cardHeight(3, false, hasSummary(d.summary))
        : cardHeight(Math.max(d.preview.length, 1), d.previewTotal > d.preview.length, hasSummary(d.summary)),
  }));
  if (graph.files) {
    const shown = Math.min(graph.files.entries.length, FILE_ROWS);
    cards.push({ id: FILES_ID, height: cardHeight(shown, graph.files.total > shown, hasSummary(graph.files.summary)) });
  }

  const { width } = SIZE.card;
  const rows = Math.max(Math.ceil(cards.length / MAX_COLUMNS), 1);
  const columns = Math.max(Math.ceil(cards.length / rows), 1);
  const inner = columns * width + (columns - 1) * GAP.column;
  let y = FRAME.header;
  for (let start = 0; start < cards.length; start += columns) {
    const row = cards.slice(start, start + columns);
    const span = row.length * width + (row.length - 1) * GAP.column;
    let tallest = 0;
    row.forEach((card, i) => {
      layout.set(card.id, { x: -span / 2 + i * (width + GAP.column), y, width, height: card.height });
      tallest = Math.max(tallest, card.height);
    });
    y += tallest + GAP.row;
  }
  const height = cards.length === 0 ? FRAME.header + 48 : y - GAP.row + FRAME.padding;
  const frameWidth = Math.max(inner, 280) + FRAME.padding * 2;
  const frame: Box = { x: -frameWidth / 2, y: 0, width: frameWidth, height };
  layout.set(FRAME_ID, frame);

  const cy = Math.min(frame.height / 2, 320);
  const leftNodes = [
    ...graph.peers.map((n) => ({ id: n.id, height: SIZE.peer.height })),
    ...graph.places.map((n) => ({ id: n.id, height: SIZE.place.height })),
    ...graph.more.filter((m) => m.side === "left").map((m) => ({ id: m.id, height: SIZE.more.height })),
  ];
  const rightNodes = [
    ...graph.shared.map((n) => ({ id: n.id, height: SIZE.shared.height })),
    ...graph.more.filter((m) => m.side === "right").map((m) => ({ id: m.id, height: SIZE.more.height })),
  ];
  column(leftNodes, frame.x - GAP.side - SIZE.peer.width, SIZE.peer.width, cy, layout);
  column(rightNodes, frame.x + frame.width + GAP.side, SIZE.shared.width, cy, layout);
  return layout;
}

// -- a file: what links to it on the left, what it links to on the right -------------

const NEIGHBOUR_GAP = 210;

export function layoutFile(graph: FileGraph): Layout {
  const layout: Layout = new Map();
  const anchor = boxAt(0, 0, SIZE.anchor);
  layout.set(FILE_ID, anchor);
  for (const side of ["left", "right"] as const) {
    const nodes = [
      ...graph[side].map((n) => ({ id: n.id, height: SIZE.neighbour.height })),
      ...graph.more.filter((m) => m.side === side).map((m) => ({ id: m.id, height: SIZE.more.height })),
    ];
    const x =
      side === "left"
        ? anchor.x - NEIGHBOUR_GAP - SIZE.neighbour.width
        : anchor.x + anchor.width + NEIGHBOUR_GAP;
    column(nodes, x, SIZE.neighbour.width, 0, layout);
  }
  return layout;
}

// -- a trace: the seed, then a column per step ----------------------------------------

export const TRACE = {
  seed: { width: 320, height: 168 },
  card: { width: 300, header: 50, row: 30, more: 28, padding: 10 },
  frame: { header: 38, padding: 12 },
  label: { height: 24, above: 40 },
  gap: { column: 170, card: 14, frame: 26 },
} as const;
/** Rows a trace card lists before folding the rest into a count. */
export const TRACE_ROWS = 6;

export function traceCardHeight(card: TraceCard): number {
  const shown = Math.min(card.rows.length, TRACE_ROWS);
  const more = card.rows.length > TRACE_ROWS ? TRACE.card.more : 0;
  return TRACE.card.header + shown * TRACE.card.row + more + TRACE.card.padding;
}

export function columnLabelId(distance: number): string {
  return `col:${distance}`;
}

export function traceFrameId(distance: number, repository: string): string {
  return `rf:${distance}:${repository}`;
}

/**
 * The seed at the left, each step's column to its right, every column centred
 * on the seed. When the chain stands in more than one repository, a column's
 * cards sit in a frame per repository, so where it crosses is where the frames
 * change; within one repository the frames would only repeat its name.
 */
export function layoutTrace(graph: TraceGraph): Layout {
  const layout: Layout = new Map();
  const seed = boxAt(0, 0, TRACE.seed);
  layout.set(SEED_ID, seed);
  const framed = graph.repositories.length > 1;
  let x = seed.x + seed.width + TRACE.gap.column;
  for (const column of graph.columns) {
    const groups: { repository: string; cards: TraceCard[] }[] = [];
    for (const card of column.cards) {
      const key = framed ? (card.repository ?? "") : "";
      const last = groups[groups.length - 1];
      if (last && last.repository === key) last.cards.push(card);
      else groups.push({ repository: key, cards: [card] });
    }
    const inner = (cards: TraceCard[]) =>
      cards.reduce((sum, c) => sum + traceCardHeight(c), 0) + TRACE.gap.card * (cards.length - 1);
    const pad = framed ? TRACE.frame.header + TRACE.frame.padding : 0;
    const heights = groups.map((g) => inner(g.cards) + (framed ? pad + TRACE.frame.padding : 0));
    const total = heights.reduce((a, b) => a + b, 0) + TRACE.gap.frame * (groups.length - 1);
    let y = -total / 2;
    const width = TRACE.card.width + (framed ? TRACE.frame.padding * 2 : 0);
    groups.forEach((group, i) => {
      if (framed) layout.set(traceFrameId(column.distance, group.repository), { x, y, width, height: heights[i]! });
      let at = y + pad;
      for (const card of group.cards) {
        const height = traceCardHeight(card);
        layout.set(card.id, { x: x + (framed ? TRACE.frame.padding : 0), y: at, width: TRACE.card.width, height });
        at += height + TRACE.gap.card;
      }
      y += heights[i]! + TRACE.gap.frame;
    });
    layout.set(columnLabelId(column.distance), {
      x,
      y: -total / 2 - TRACE.label.above,
      width,
      height: TRACE.label.height,
    });
    x += width + TRACE.gap.column;
  }
  return layout;
}

/**
 * Link stroke width, in three weights as a transit map has: 2.5px for a few
 * relationships, 3.5px for hundreds, 5px from a thousand up.
 */
export function strokeFor(count: number): number {
  return count >= 1000 ? 5 : count >= 10 ? 3.5 : 2.5;
}
