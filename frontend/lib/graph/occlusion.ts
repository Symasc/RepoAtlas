import type { Box } from "@/lib/model/graph";

import type { Point, Route } from "./geometry";

/**
 * How many lines run under cards they are not drawn between. A line at rest is
 * drawn under the cards, so one that crosses a card it does not join is hidden
 * there and reads as joining it. Pure and total: measured on a layout, so a
 * router that avoids the cards can be told from one that does not.
 */

/** A card is shrunk by this much on every side before a line is tested against it: touching its border is not entering it. */
export const INSET = 2;

/** What of a line decides its route: the cards it joins and how far it sits across its own direction. */
export interface Line {
  source: string;
  target: string;
  offset: number;
}

/** How a line is routed between the boxes of its two ends: `floating`, for the lines drawn at rest. */
export type Router = (source: Box, target: Box, offset: number) => Route;

export interface Occlusions {
  /** (line, card) pairs: a line's polyline enters a card that is not one of its ends. A card entered twice by one line counts once. */
  pairs: number;
  /** The lines with at least one such card. */
  lines: number;
  /** The pairs whose line joins two cards of one district and runs under a third card of that same district. */
  sameDistrictPairs: number;
}

function shrunk(box: Box, by: number): Box | null {
  const width = box.width - 2 * by;
  const height = box.height - 2 * by;
  return width > 0 && height > 0 ? { x: box.x + by, y: box.y + by, width, height } : null;
}

/** Whether the closed segment `a`-`b` has a point in the closed box (Liang-Barsky). */
function segmentEnters(a: Point, b: Point, box: Box): boolean {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const edges: [number, number][] = [
    [-dx, a.x - box.x],
    [dx, box.x + box.width - a.x],
    [-dy, a.y - box.y],
    [dy, box.y + box.height - a.y],
  ];
  let from = 0;
  let to = 1;
  for (const [p, q] of edges) {
    if (p === 0) {
      if (q < 0) return false;
      continue;
    }
    const t = q / p;
    if (p < 0) from = Math.max(from, t);
    else to = Math.min(to, t);
    if (from > to) return false;
  }
  return true;
}

/** Whether a polyline has a point in the closed box. */
export function polylineEnters(points: readonly Point[], box: Box): boolean {
  if (points.length === 1) {
    const p = points[0]!;
    return p.x >= box.x && p.x <= box.x + box.width && p.y >= box.y && p.y <= box.y + box.height;
  }
  for (let i = 1; i < points.length; i++) {
    if (segmentEnters(points[i - 1]!, points[i]!, box)) return true;
  }
  return false;
}

/**
 * The lines that run under cards they do not join. `cards` holds every card's
 * box by id (a line's ends are among them; a line whose end is on no card is
 * left out). `district` says which district a card is in, for `sameDistrictPairs`;
 * without it nothing is counted as one.
 */
export function occlusions(
  lines: readonly Line[],
  cards: ReadonlyMap<string, Box>,
  router: Router,
  district: (card: string) => string | null = () => null,
): Occlusions {
  const targets = [...cards].flatMap(([id, box]) => {
    const inner = shrunk(box, INSET);
    return inner === null ? [] : [{ id, box: inner }];
  });
  let pairs = 0;
  let occluded = 0;
  let sameDistrictPairs = 0;
  for (const line of lines) {
    const from = cards.get(line.source);
    const to = cards.get(line.target);
    if (from === undefined || to === undefined) continue;
    const { points } = router(from, to, line.offset);
    let left = Infinity;
    let top = Infinity;
    let right = -Infinity;
    let bottom = -Infinity;
    for (const p of points) {
      left = Math.min(left, p.x);
      top = Math.min(top, p.y);
      right = Math.max(right, p.x);
      bottom = Math.max(bottom, p.y);
    }
    const home = district(line.source);
    const joined = home !== null && home === district(line.target);
    let hit = 0;
    for (const { id, box } of targets) {
      if (id === line.source || id === line.target) continue;
      // a card the line's bounding box misses cannot be entered
      if (box.x > right || box.x + box.width < left || box.y > bottom || box.y + box.height < top) continue;
      if (!polylineEnters(points, box)) continue;
      hit += 1;
      if (joined && district(id) === home) sameDistrictPairs += 1;
    }
    pairs += hit;
    if (hit > 0) occluded += 1;
  }
  return { pairs, lines: occluded, sameDistrictPairs };
}
