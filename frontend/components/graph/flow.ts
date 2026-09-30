import { sidesFor, type Layout } from "@/lib/graph/layout";
import type { Box, ViewEdge } from "@/lib/model/graph";

import type { GraphEdge } from "./edges";
import type { GraphNode } from "./nodes";

type Unplaced = Omit<GraphNode, "position" | "width" | "height" | "style">;

/** A node at its computed box: the size is fixed, so nothing waits on measuring. */
export function place(node: Unplaced, box: Box): GraphNode {
  return {
    ...node,
    position: { x: box.x, y: box.y },
    width: box.width,
    height: box.height,
    style: { width: box.width, height: box.height },
  } as GraphNode;
}

/** Gap between two kinds of link drawn between the same two nodes. */
const PARALLEL = 6;

/**
 * React Flow edges, each leaving and entering by the sides that face each other.
 * Links of different kinds between the same pair are drawn side by side, never
 * on one curve: a code link and a dependency are two facts.
 */
export function flowEdges(edges: readonly ViewEdge[], layout: Layout, labelled: boolean): GraphEdge[] {
  const pairs = new Map<string, string[]>();
  for (const edge of edges) {
    if (edge.role !== "link") continue;
    const pair = [edge.source, edge.target].sort().join("|");
    pairs.set(pair, [...(pairs.get(pair) ?? []), edge.id]);
  }
  const offsets = new Map<string, number>();
  for (const ids of pairs.values()) {
    ids.forEach((id, i) => offsets.set(id, (i - (ids.length - 1) / 2) * PARALLEL));
  }
  const result: GraphEdge[] = [];
  for (const edge of edges) {
    const source = layout.get(edge.source);
    const target = layout.get(edge.target);
    if (!source || !target) continue;
    const { from, to } = sidesFor(edge, source, target);
    const base = {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      sourceHandle: `s-${from}`,
      targetHandle: `t-${to}`,
    };
    if (edge.role === "tree") {
      result.push({ ...base, type: "tree", data: { emphasis: "rest" }, zIndex: 0 });
    } else if (edge.link) {
      result.push({
        ...base,
        type: "link",
        data: {
          link: edge.link,
          emphasis: "rest",
          labelled,
          offset: offsets.get(edge.id) ?? 0,
          scope: edge.scope ?? null,
        },
        zIndex: 0,
      });
    }
  }
  return result;
}
