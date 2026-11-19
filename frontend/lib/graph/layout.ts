import {
  FILE_ID,
  FILE_ROWS,
  FILES_ID,
  FRAME_ID,
  type Box,
  type FamilyCounts,
  type FileGraph,
  type OpenedGraph,
} from "@/lib/model/graph";
import { SEED_ID, type TraceCard, type TraceGraph } from "@/lib/model/trace";

/**
 * Sizes and positions, computed rather than measured: every node's height
 * follows from what it lists, so a layout is known before anything renders and
 * is checked for overlap in unit tests.
 */

const SIZE = {
  repository: { width: 288, height: 84 },
  card: { width: 236 },
  shared: { width: 236, height: 48 },
  peer: { width: 220, height: 56 },
  place: { width: 220, height: 52 },
  more: { width: 220, height: 44 },
  anchor: { width: 300, height: 108 },
  neighbour: { width: 244, height: 56 },
} as const;
export const REPOSITORY_SIZE = SIZE.repository;

export const CARD = { header: 44, row: 26, padding: 8, footer: 30 } as const;
const FRAME = { header: 64, padding: 24 } as const;
export const HUB_ID = "hub";

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

// -- a repository opened at a directory: a frame of cards, side columns ----------------

const GAP = { column: 24, row: 28, side: 150, stack: 14 } as const;
const MAX_COLUMNS = 5;

function hasSummary(summary: FamilyCounts): boolean {
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

const TRACE = {
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
