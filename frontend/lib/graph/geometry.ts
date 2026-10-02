import type { Box } from "@/lib/model/graph";

/**
 * Where a line runs between two nodes. Pure, so a route's ends are tested
 * against the boxes they must stop at.
 */

export interface Point {
  x: number;
  y: number;
}

export interface Route {
  path: string;
  /** Where a count tag sits: the middle of the route's middle run. */
  label: Point;
  /** Where the route leaves its source. */
  start: Point;
  /** The route's last point, and a point behind it that gives its direction there. */
  end: Point;
  towards: Point;
}

/** Room left at the target's border for the arrowhead. */
const ARRIVAL_GAP = 3;
/** A lane leaves a frame below its header and above its bottom edge. */
export const LANE_INSET = { top: 76, bottom: 24 } as const;

export function centre(b: Box): Point {
  return { x: b.x + b.width / 2, y: b.y + b.height / 2 };
}

/** Where a ray from `from` (inside `b`) towards `toward` leaves `b`, grown by `gap`. */
function exitPoint(b: Box, from: Point, toward: Point, gap = 0): Point {
  const dx = toward.x - from.x;
  const dy = toward.y - from.y;
  const x0 = b.x - gap;
  const x1 = b.x + b.width + gap;
  const y0 = b.y - gap;
  const y1 = b.y + b.height + gap;
  let t = Infinity;
  if (dx > 0) t = Math.min(t, (x1 - from.x) / dx);
  else if (dx < 0) t = Math.min(t, (x0 - from.x) / dx);
  if (dy > 0) t = Math.min(t, (y1 - from.y) / dy);
  else if (dy < 0) t = Math.min(t, (y0 - from.y) / dy);
  if (!Number.isFinite(t) || t < 0) return from;
  return { x: from.x + dx * t, y: from.y + dy * t };
}

function round(n: number): number {
  return Math.round(n * 10) / 10;
}

function at(p: Point): string {
  return `${round(p.x)} ${round(p.y)}`;
}

/** tan 22.5°: how far a parallel line's 45° bend moves so its gap holds on the diagonal. */
const BEND_SHIFT = Math.SQRT2 - 1;
/** How round a bend is, at most. */
const BEND_RADIUS = 10;
/** The straight run a line keeps at each end before it bends, so it leaves and arrives square. */
const MIN_RUN = 14;

export type Axis = "x" | "y";

/** Corners cut back by up to `radius` along both of their sides, and rounded. */
function rounded(points: readonly Point[], radius: number): string {
  let d = `M ${at(points[0]!)}`;
  for (let i = 1; i < points.length - 1; i++) {
    const p = points[i - 1]!;
    const c = points[i]!;
    const n = points[i + 1]!;
    const into = Math.hypot(c.x - p.x, c.y - p.y);
    const out = Math.hypot(n.x - c.x, n.y - c.y);
    if (into < 0.01 || out < 0.01) continue;
    const r = Math.min(radius, into / 2, out / 2);
    const a = { x: c.x - ((c.x - p.x) / into) * r, y: c.y - ((c.y - p.y) / into) * r };
    const b = { x: c.x + ((n.x - c.x) / out) * r, y: c.y + ((n.y - c.y) / out) * r };
    d += ` L ${at(a)} Q ${at(c)} ${at(b)}`;
  }
  return `${d} L ${at(points[points.length - 1]!)}`;
}

/**
 * A line drawn as a transit map draws it: a straight run along `axis`, one 45°
 * diagonal across it, and a straight run in, its bends rounded. Where the
 * diagonal has no room, the middle crosses at 90° instead. `shift` is how far
 * this line sits across `axis` from the middle of its parallel set (already in
 * its ends): its bends move with it, so parallel lines keep one gap all along.
 */
export function octilinear(start: Point, end: Point, axis: Axis = "x", shift = 0): Route {
  const u = (p: Point) => (axis === "x" ? p.x : p.y);
  const v = (p: Point) => (axis === "x" ? p.y : p.x);
  const point = (along: number, across: number): Point => (axis === "x" ? { x: along, y: across } : { x: across, y: along });
  const du = u(end) - u(start);
  const dv = v(end) - v(start);
  const dir = Math.sign(du) || 1;
  const side = Math.sign(dv);
  let points: Point[];
  if (Math.abs(dv) < 0.5 || Math.abs(du) < 1) {
    points = [start, end];
  } else if (Math.abs(dv) <= Math.abs(du) - 2 * MIN_RUN) {
    const mid = (u(start) + u(end)) / 2 - dir * side * BEND_SHIFT * shift;
    const half = Math.abs(dv) / 2;
    points = [start, point(mid - dir * half, v(start)), point(mid + dir * half, v(end)), end];
  } else {
    const mid = (u(start) + u(end)) / 2 - dir * side * shift;
    points = [start, point(mid, v(start)), point(mid, v(end)), end];
  }
  // the count sits on the middle run: the diagonal, or the whole line
  const [a, b] = points.length === 4 ? [points[1]!, points[2]!] : [start, end];
  const last = points[points.length - 2]!;
  return {
    path: rounded(points, BEND_RADIUS),
    label: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
    start,
    end,
    towards: last.x === end.x && last.y === end.y ? start : last,
  };
}

/**
 * Border to border between two boxes, along the axis with the wider gap
 * between them: out of the facing side, octilinear across, into the facing
 * side. `offset` moves the line across its own direction (to separate parallel
 * kinds), as a normal to the run would: a line running back the other way
 * with the same offset sits on the other side. Boxes that overlap on both axes
 * are joined straight, centre towards centre.
 */
export function floating(source: Box, target: Box, offset = 0): Route {
  const a = centre(source);
  const b = centre(target);
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const gapX = Math.abs(dx) - (source.width + target.width) / 2;
  const gapY = Math.abs(dy) - (source.height + target.height) / 2;
  if (gapX <= 0 && gapY <= 0) {
    const start = exitPoint(source, a, b);
    const end = exitPoint(target, b, a, ARRIVAL_GAP);
    return { path: `M ${at(start)} L ${at(end)}`, label: { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 }, start, end, towards: start };
  }
  if (gapX >= gapY) {
    const sx = Math.sign(dx) || 1;
    const shift = offset * sx;
    const start = { x: sx > 0 ? source.x + source.width : source.x, y: clamp(a.y + shift, source.y + 4, source.y + source.height - 4) };
    const end = {
      x: sx > 0 ? target.x - ARRIVAL_GAP : target.x + target.width + ARRIVAL_GAP,
      y: clamp(b.y + shift, target.y + 4, target.y + target.height - 4),
    };
    return octilinear(start, end, "x", shift);
  }
  const sy = Math.sign(dy) || 1;
  const shift = -offset * sy;
  const start = { x: clamp(a.x + shift, source.x + 4, source.x + source.width - 4), y: sy > 0 ? source.y + source.height : source.y };
  const end = {
    x: clamp(b.x + shift, target.x + 4, target.x + target.width - 4),
    y: sy > 0 ? target.y - ARRIVAL_GAP : target.y + target.height + ARRIVAL_GAP,
  };
  return octilinear(start, end, "y", shift);
}

function clamp(n: number, low: number, high: number): number {
  return high < low ? (low + high) / 2 : Math.min(Math.max(n, low), high);
}

/**
 * Side to side, leaving and arriving horizontally: a column of things to the
 * left or right of what links to them. A `lane` end (a frame) is met at the
 * other end's height, so the lines from one frame run as parallel lanes rather
 * than a fan from one point.
 */
export function sideways(source: Box, target: Box, offset = 0, lane: "source" | "target" | null = null): Route {
  const sc = centre(source);
  const tc = centre(target);
  const rightward = tc.x >= sc.x;
  const sx = rightward ? source.x + source.width : source.x;
  const tx = rightward ? target.x - ARRIVAL_GAP : target.x + target.width + ARRIVAL_GAP;
  let sy = sc.y + offset;
  let ty = tc.y + offset;
  if (lane === "source") sy = clamp(ty, source.y + LANE_INSET.top, source.y + source.height - LANE_INSET.bottom);
  if (lane === "target") ty = clamp(sy, target.y + LANE_INSET.top, target.y + target.height - LANE_INSET.bottom);
  return octilinear({ x: sx, y: sy }, { x: tx, y: ty }, "x", offset);
}

/** A closed arrowhead whose tip is `end`, pointing away from `towards`. */
export function arrowhead(end: Point, towards: Point, size: number): string {
  const dx = end.x - towards.x;
  const dy = end.y - towards.y;
  const length = Math.hypot(dx, dy) || 1;
  const ux = dx / length;
  const uy = dy / length;
  const bx = end.x - ux * size;
  const by = end.y - uy * size;
  const half = size * 0.5;
  return `M ${at(end)} L ${at({ x: bx - uy * half, y: by + ux * half })} L ${at({ x: bx + uy * half, y: by - ux * half })} Z`;
}
