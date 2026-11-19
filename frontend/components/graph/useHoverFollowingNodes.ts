"use client";

import { useState } from "react";

/** The hover that survives `nodes`: the same id while its node is still there, else none. */
export function nextHover(hovered: string | null, nodes: readonly { id: string }[]): string | null {
  return hovered !== null && !nodes.some((n) => n.id === hovered) ? null : hovered;
}

/** The id being pointed at, cleared when its node leaves `nodes`. */
export function useHoverFollowingNodes<T extends { id: string }>(
  nodes: readonly T[],
): [string | null, (id: string | null) => void] {
  const [hovered, setHovered] = useState<string | null>(null);
  // a hovered node that leaves the canvas (a summary opened by its own button) sends no mouse-leave
  const [hoverNodes, setHoverNodes] = useState(nodes);
  if (hoverNodes !== nodes) {
    setHoverNodes(nodes);
    const next = nextHover(hovered, nodes);
    if (next !== hovered) setHovered(next);
  }
  return [hovered, setHovered];
}
