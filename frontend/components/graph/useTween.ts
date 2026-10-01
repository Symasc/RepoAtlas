"use client";

import { useEffect, useState } from "react";

import type { Layout } from "@/lib/graph/layout";
import type { Box } from "@/lib/model/graph";

import { easeInOut } from "./transition";

function mix(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}

/** Every box in `to`, part of the way from where it was in `from`; new boxes start where they end. */
export function blend(from: Layout, to: Layout, t: number): Layout {
  if (t >= 1) return to;
  const out: Layout = new Map();
  for (const [id, box] of to) {
    const a: Box | undefined = from.get(id);
    out.set(
      id,
      a
        ? { x: mix(a.x, box.x, t), y: mix(a.y, box.y, t), width: mix(a.width, box.width, t), height: mix(a.height, box.height, t) }
        : box,
    );
  }
  return out;
}

interface Motion {
  key: string;
  from: Layout;
  to: Layout;
  t: number;
}

/**
 * A layout that glides when its `key` changes (the overview re-centring on a
 * newly selected repository) and snaps for any other change. Positions only,
 * a few dozen nodes, for half a second: React Flow redraws the lines as their
 * ends move.
 */
export function useTween(target: Layout, key: string, reduced: boolean, duration = 560): Layout {
  const [motion, setMotion] = useState<Motion>({ key, from: target, to: target, t: 1 });
  let current = motion;
  if (motion.key !== key) {
    current = { key, from: blend(motion.from, motion.to, easeInOut(motion.t)), to: target, t: reduced ? 1 : 0 };
    setMotion(current);
  }
  const running = current.t < 1;
  useEffect(() => {
    if (!running) return;
    const start = performance.now();
    let frame = requestAnimationFrame(function step(now) {
      const t = Math.min(1, (now - start) / duration);
      setMotion((m) => ({ ...m, t }));
      if (t < 1) frame = requestAnimationFrame(step);
    });
    return () => cancelAnimationFrame(frame);
  }, [running, key, duration]);
  return current.t >= 1 ? target : blend(current.from, target, easeInOut(current.t));
}
