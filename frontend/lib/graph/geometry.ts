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
  /** Where a count badge sits: the route's midpoint. */
  label: Point;
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
export function exitPoint(b: Box, from: Point, toward: Point, gap = 0): Point {
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

/**
 * Border to border along the line between two centres, moved `offset` across
 * itself (to separate parallel kinds) and bent by `curve`, a fraction of its
 * length, away from `away` (the constellation's centre), so lines between
 * neighbours on a ring bow outward instead of cutting across the middle.
 */
export function floating(source: Box, target: Box, offset = 0, curve = 0, away: Point = { x: 0, y: 0 }): Route {
  const a = centre(source);
  const b = centre(target);
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const length = Math.hypot(dx, dy) || 1;
  const n = { x: -dy / length, y: dx / length };
  const a2 = { x: a.x + n.x * offset, y: a.y + n.y * offset };
  const b2 = { x: b.x + n.x * offset, y: b.y + n.y * offset };
  const mid = { x: (a2.x + b2.x) / 2, y: (a2.y + b2.y) / 2 };
  let sign = 1;
  if (curve !== 0) {
    const plus = Math.hypot(mid.x + n.x - away.x, mid.y + n.y - away.y);
    const minus = Math.hypot(mid.x - n.x - away.x, mid.y - n.y - away.y);
    sign = plus >= minus ? 1 : -1;
  }
  const control = { x: mid.x + n.x * length * curve * sign, y: mid.y + n.y * length * curve * sign };
  const start = exitPoint(source, a2, control);
  const end = exitPoint(target, b2, control, ARRIVAL_GAP);
  return {
    path: `M ${at(start)} Q ${at(control)} ${at(end)}`,
    label: {
      x: 0.25 * start.x + 0.5 * control.x + 0.25 * end.x,
      y: 0.25 * start.y + 0.5 * control.y + 0.25 * end.y,
    },
    end,
    towards: control.x === end.x && control.y === end.y ? start : control,
  };
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
  const reach = Math.max(36, Math.abs(tx - sx) * 0.5) * (rightward ? 1 : -1);
  const start = { x: sx, y: sy };
  const end = { x: tx, y: ty };
  const c1 = { x: sx + reach, y: sy };
  const c2 = { x: tx - reach, y: ty };
  return {
    path: `M ${at(start)} C ${at(c1)} ${at(c2)} ${at(end)}`,
    label: { x: (start.x + 3 * c1.x + 3 * c2.x + end.x) / 8, y: (start.y + 3 * c1.y + 3 * c2.y + end.y) / 8 },
    end,
    towards: c2,
  };
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
