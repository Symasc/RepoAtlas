import type { Box } from "@/lib/model/graph";

import { centre, floating, gutter, type Point, type Route } from "./geometry";

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

/** The cards a polyline enters (a card entered twice counts once): `inner` are the cards already shrunk by `INSET`. */
function hitsOf(points: readonly Point[], inner: readonly Box[]): number {
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
  let hit = 0;
  for (const box of inner) {
    if (box.x > right || box.x + box.width < left || box.y > bottom || box.y + box.height < top) continue;
    if (polylineEnters(points, box)) hit += 1;
  }
  return hit;
}

const sameBox = (a: Box, b: Box) => a.x === b.x && a.y === b.y && a.width === b.width && a.height === b.height;

/** The obstacle maps seen, shrunk once: a map is not changed after it is handed to `routeAvoiding`. */
const shrunkCards = new WeakMap<ReadonlyMap<string, Box>, Box[]>();

function shrunkOf(cards: ReadonlyMap<string, Box>): Box[] {
  let inner = shrunkCards.get(cards);
  if (inner === undefined) {
    inner = [];
    for (const box of cards.values()) {
      const small = shrunk(box, INSET);
      if (small !== null) inner.push(small);
    }
    shrunkCards.set(cards, inner);
  }
  return inner;
}

/**
 * `floating`, unless the line it draws runs under cards it does not join and a
 * route round them has fewer. The candidates, best first: `floating`; `floating`
 * along the other axis (where the boxes have a gap along it); a route through
 * the gutters beside the source and the target's near face; the same into the
 * target's far face. The one with the fewest cards entered wins, the earlier on
 * a tie, so a line is never worse than `floating` and one that is clear stays
 * as it is. `obstacles` are the cards of the layout; a box equal to the source's
 * or the target's is the line's own end and is not one.
 */
export function routeAvoiding(source: Box, target: Box, offset: number, obstacles: ReadonlyMap<string, Box>): Route {
  const own = [shrunk(source, INSET), shrunk(target, INSET)].filter((box): box is Box => box !== null);
  const inner = shrunkOf(obstacles).filter((box) => !own.some((mine) => sameBox(box, mine)));
  let best = floating(source, target, offset);
  let fewest = hitsOf(best.points, inner);
  if (fewest === 0) return best;
  const a = centre(source);
  const b = centre(target);
  const gapX = Math.abs(b.x - a.x) - (source.width + target.width) / 2;
  const gapY = Math.abs(b.y - a.y) - (source.height + target.height) / 2;
  // along the axis the default did not take, if the boxes are apart along it
  const other = gapX >= gapY ? "y" : "x";
  const apart = (other === "y" ? gapY : gapX) > 0;
  const candidates: (() => Route | null)[] = [
    () => (apart ? floating(source, target, offset, other) : null),
    () => gutter(source, target, offset, false),
    () => gutter(source, target, offset, true),
  ];
  for (const make of candidates) {
    const route = make();
    if (route === null) continue;
    // a gutter route must not run through its own ends
    if (own.some((mine) => polylineEnters(route.points, mine))) continue;
    const hit = hitsOf(route.points, inner);
    if (hit < fewest) {
      best = route;
      fewest = hit;
      if (fewest === 0) break;
    }
  }
  return best;
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
