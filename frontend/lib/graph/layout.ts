import {
  FILES_ID,
  PREVIEW_ROWS,
  repositoryId,
  type Box,
  type OpenedGraph,
  type ViewEdge,
} from "@/lib/model/graph";
import type { RepositoryModel } from "@/lib/model/repositories";

/**
 * Sizes and positions, computed rather than measured: every node's height
 * follows from what it lists, so a layout is known before anything renders and
 * cannot overlap. The layered engine (./elk) places linked repositories; the
 * rest is placed here.
 */

export const SIZE = {
  repository: { width: 264, height: 84 },
  chainRepository: { width: 264, height: 64 },
  chainPath: { width: 220, height: 52 },
  card: { width: 232 },
  shared: { width: 232, height: 44 },
  peer: { width: 200, height: 56 },
} as const;

export const CARD = { header: 54, row: 26, padding: 10 } as const;

const GAP = { column: 28, row: 32, chain: 26, belowChain: 76, side: 150, stack: 14, independent: 96 };
const MAX_COLUMNS = 5;

export type Layout = Map<string, Box>;

/** Height of a card listing `rows` entries, plus a "more" row when some are not listed. */
export function cardHeight(rows: number, more: boolean): number {
  return CARD.header + (rows + (more ? 1 : 0)) * CARD.row + CARD.padding;
}

/** Repositories in centred rows, starting at `top`. */
export function rowsOf(repositories: readonly RepositoryModel[], top = 0, perRow = 4): Layout {
  const layout: Layout = new Map();
  const { width, height } = SIZE.repository;
  for (let start = 0, row = 0; start < repositories.length; start += perRow, row++) {
    const slice = repositories.slice(start, start + perRow);
    const span = slice.length * width + (slice.length - 1) * GAP.column * 2;
    slice.forEach((r, i) => {
      layout.set(repositoryId(r.slug), {
        x: -span / 2 + i * (width + GAP.column * 2),
        y: top + row * (height + GAP.row * 1.5),
        width,
        height,
      });
    });
  }
  return layout;
}

/** The overview's independent row goes beneath whatever the engine laid out. */
export function belowAll(layout: Layout, gap = GAP.independent): number {
  let bottom = -Infinity;
  for (const box of layout.values()) bottom = Math.max(bottom, box.y + box.height);
  return bottom === -Infinity ? 0 : bottom + gap;
}

export function layoutOpened(graph: OpenedGraph): Layout {
  const layout: Layout = new Map();
  let y = 0;
  graph.chain.forEach((step, i) => {
    const size = i === 0 ? SIZE.chainRepository : SIZE.chainPath;
    layout.set(step.id, { x: -size.width / 2, y, width: size.width, height: size.height });
    y += size.height + GAP.chain;
  });
  y += GAP.belowChain - GAP.chain;

  const cards: { id: string; height: number }[] = graph.directories.map((d) => ({
    id: d.id,
    height:
      d.preview === null
        ? cardHeight(3, false)
        : cardHeight(Math.max(d.preview.length, 1), d.previewTotal > d.preview.length),
  }));
  if (graph.files) {
    cards.push({
      id: FILES_ID,
      height: cardHeight(graph.files.entries.length, graph.files.total > graph.files.entries.length),
    });
  }

  const { width } = SIZE.card;
  const columns = Math.min(MAX_COLUMNS, Math.max(cards.length, 1));
  const top = y;
  let left = Infinity;
  let right = -Infinity;
  for (let start = 0; start < cards.length; start += columns) {
    const row = cards.slice(start, start + columns);
    const span = row.length * width + (row.length - 1) * GAP.column;
    let tallest = 0;
    row.forEach((card, i) => {
      const x = -span / 2 + i * (width + GAP.column);
      layout.set(card.id, { x, y, width, height: card.height });
      tallest = Math.max(tallest, card.height);
      left = Math.min(left, x);
      right = Math.max(right, x + width);
    });
    y += tallest + GAP.row;
  }
  if (cards.length === 0) {
    left = -SIZE.chainRepository.width / 2;
    right = SIZE.chainRepository.width / 2;
  }

  graph.shared.forEach((node, i) => {
    layout.set(node.id, {
      x: right + GAP.side,
      y: top + i * (SIZE.shared.height + GAP.stack),
      width: SIZE.shared.width,
      height: SIZE.shared.height,
    });
  });
  // the left column: other repositories, then elsewhere in this one
  [...graph.peers, ...graph.away].forEach((node, i) => {
    layout.set(node.id, {
      x: left - GAP.side - SIZE.peer.width,
      y: top + i * (SIZE.peer.height + GAP.stack),
      width: SIZE.peer.width,
      height: SIZE.peer.height,
    });
  });
  return layout;
}

export type Side = "top" | "right" | "bottom" | "left";

/**
 * Which sides an edge leaves and enters by: containment always runs down; a
 * link leaves towards its target, horizontally when the two are side by side.
 */
export function sidesFor(edge: ViewEdge, source: Box, target: Box): { from: Side; to: Side } {
  if (edge.role === "tree") return { from: "bottom", to: "top" };
  if (target.x >= source.x + source.width) return { from: "right", to: "left" };
  if (target.x + target.width <= source.x) return { from: "left", to: "right" };
  return target.y >= source.y ? { from: "bottom", to: "top" } : { from: "top", to: "bottom" };
}

/** Link stroke width: 1px for one relationship, up to 3.5px for thousands. */
export function strokeFor(count: number): number {
  return Math.min(3.5, 1 + Math.log10(Math.max(count, 1)));
}

export { PREVIEW_ROWS };
