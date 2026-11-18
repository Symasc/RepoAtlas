import type { Box } from "@/lib/model/graph";

import type { District } from "./districts";
import { fitZoom } from "./fit";
import { bounds, REPOSITORY_SIZE } from "./layout";

/**
 * Where the districts go. Each is either open (a frame holding its repositories
 * as cards in rows) or collapsed to one summary the size of a card; the units
 * are set left to right in district order and wrap at a shelf width. Computed,
 * not measured, so positions are known before anything renders and are checked
 * in unit tests. A unit's place depends only on the units before it, so opening
 * or closing one district never moves one ahead of it.
 */

/** The zoom below which a card cannot be read. */
export const READABLE_ZOOM = 0.75;
/** The most cards (repositories and summaries) in view at rest. */
export const CARD_CAP = 48;
export const DISTRICT = { gap: 96, header: 40, padding: 20, cardGap: { x: 48, y: 28 } } as const;

/** The id of the box standing for a collapsed district. */
export function summaryId(districtId: string): string {
  return `summary:${districtId}`;
}

/** Cards per row of an open district: about a 16:9 frame, never more than there are cards. */
export function columns(n: number): number {
  if (n <= 1) return 1;
  // the smallest c with c >= sqrt(n * (16 / 9) * (84 + 28) / (288 + 48)), compared in integers so that no float rounds it
  const cellWidth = REPOSITORY_SIZE.width + DISTRICT.cardGap.x;
  const cellHeight = REPOSITORY_SIZE.height + DISTRICT.cardGap.y;
  let c = 1;
  while (c < n && c * c * 9 * cellWidth < n * 16 * cellHeight) c += 1;
  return c;
}

/** An open district's frame, for `n` cards, and how its cards are arranged. */
export function frameSize(n: number): { width: number; height: number; columns: number; rows: number } {
  const c = columns(n);
  const rows = Math.max(1, Math.ceil(n / c));
  const { width, height } = REPOSITORY_SIZE;
  return {
    width: 2 * DISTRICT.padding + c * width + (c - 1) * DISTRICT.cardGap.x,
    height: DISTRICT.header + 2 * DISTRICT.padding + rows * height + (rows - 1) * DISTRICT.cardGap.y,
    columns: c,
    rows,
  };
}

export interface DistrictLayout {
  /** Repository cards (by node id) and the summaries of collapsed districts (by `summaryId`). */
  cards: Map<string, Box>;
  /** Open districts' frames, by district id. */
  frames: Map<string, Box>;
  /** The width the rows wrapped at. */
  shelf: number;
  extent: Box;
}

export function layoutDistricts(ds: readonly District[], expanded: ReadonlySet<string>, shelf?: number): DistrictLayout {
  const units = ds.map((d) => {
    const open = expanded.has(d.id);
    const { width, height } = open ? frameSize(d.members.length) : REPOSITORY_SIZE;
    return { d, open, width, height };
  });
  const wrapAt =
    shelf ??
    Math.max(
      ...units.map((u) => u.width),
      Math.sqrt(units.reduce((sum, u) => sum + (u.width + DISTRICT.gap) * (u.height + DISTRICT.gap), 0) * (16 / 9)),
    );

  const cards = new Map<string, Box>();
  const frames = new Map<string, Box>();
  let x = 0;
  let y = 0;
  let rowHeight = 0;
  for (const { d, open, width, height } of units) {
    if (x > 0 && x + width > wrapAt) {
      y += rowHeight + DISTRICT.gap;
      x = 0;
      rowHeight = 0;
    }
    if (open) {
      frames.set(d.id, { x, y, width, height });
      const c = columns(d.members.length);
      d.members.forEach((member, i) => {
        cards.set(member, {
          x: x + DISTRICT.padding + (i % c) * (REPOSITORY_SIZE.width + DISTRICT.cardGap.x),
          y: y + DISTRICT.header + DISTRICT.padding + Math.floor(i / c) * (REPOSITORY_SIZE.height + DISTRICT.cardGap.y),
          width: REPOSITORY_SIZE.width,
          height: REPOSITORY_SIZE.height,
        });
      });
    } else {
      cards.set(summaryId(d.id), { x, y, width, height });
    }
    x += width + DISTRICT.gap;
    rowHeight = Math.max(rowHeight, height);
  }
  const extent = bounds([...frames.values(), ...cards.values()]) ?? { x: 0, y: 0, width: 0, height: 0 };
  return { cards, frames, shelf: wrapAt, extent };
}

/** Cards in view: each open district's repositories, one summary for each collapsed one. */
export function visibleCards(ds: readonly District[], expanded: ReadonlySet<string>): number {
  return ds.reduce((sum, d) => sum + (expanded.has(d.id) ? d.members.length : 1), 0);
}

/** The distinct district sizes up to `cardCap`, smallest first: the steps `defaultExpanded` climbs. */
export function sizeLevels(ds: readonly District[], cardCap: number = CARD_CAP): number[] {
  return [...new Set(ds.map((d) => d.members.length))].filter((n) => n <= cardCap).sort((a, b) => a - b);
}

export interface DefaultExpansion {
  expanded: Set<string>;
  /** The shelf the chosen state was laid out with. */
  shelf: number;
  /** How many size steps are open: 0 is nothing expanded, k opens every district of at most `sizeLevels(ds)[k - 1]`. */
  level: number;
  /** Whether the chosen state keeps the card cap and the readable zoom. */
  readable: boolean;
}

/**
 * Which districts open at rest: the largest set of the smallest districts that
 * still fits the card cap and fits the viewport at a readable zoom. Opens
 * districts by size, smallest first, and stops before the first step that
 * fails.
 */
export function defaultExpanded(
  ds: readonly District[],
  viewport: { width: number; height: number },
  options: { readableZoom?: number; cardCap?: number } = {},
): DefaultExpansion {
  const readableZoom = options.readableZoom ?? READABLE_ZOOM;
  const cardCap = options.cardCap ?? CARD_CAP;
  const sizes = sizeLevels(ds, cardCap);
  const state = (size: number) => {
    const expanded = new Set(ds.filter((d) => d.members.length <= size).map((d) => d.id));
    const shape = layoutDistricts(ds, expanded);
    const passes = visibleCards(ds, expanded) <= cardCap && fitZoom(shape.extent, viewport) >= readableZoom;
    return { expanded, shelf: shape.shelf, passes };
  };
  let chosen = state(0);
  let level = 0;
  if (chosen.passes) {
    for (let k = 1; k <= sizes.length; k++) {
      const next = state(sizes[k - 1]!);
      if (!next.passes) break;
      chosen = next;
      level = k;
    }
  }
  return { expanded: chosen.expanded, shelf: chosen.shelf, level, readable: chosen.passes };
}
