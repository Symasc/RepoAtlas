import type { Box, ViewEdge } from "@/lib/model/graph";

import type { GraphEdge, RouteKind } from "./edges";
import type { GraphNode } from "./nodes";

type Unplaced = Omit<GraphNode, "position" | "width" | "height" | "style">;

/** A node at its computed box: the size is fixed, so nothing waits on measuring. */
export function place(node: Unplaced, box: Box, style?: GraphNode["style"]): GraphNode {
  return {
    ...node,
    position: { x: box.x, y: box.y },
    width: box.width,
    height: box.height,
    style: { width: box.width, height: box.height, ...style },
  } as GraphNode;
}

/** Gap between two families of link drawn between the same two nodes. */
const PARALLEL = 7;

/**
 * React Flow edges. Links of different families between the same pair are
 * drawn side by side, never on one curve: a code link and a dependency are two
 * facts.
 */
export function flowEdges(
  edges: readonly ViewEdge[],
  how: {
    route: (edge: ViewEdge) => RouteKind;
    curve?: (edge: ViewEdge) => number;
    labelled?: boolean;
  },
): GraphEdge[] {
  const pairs = new Map<string, string[]>();
  for (const edge of edges) {
    const pair = [edge.source, edge.target].sort().join("|");
    pairs.set(pair, [...(pairs.get(pair) ?? []), edge.id]);
  }
  const offsets = new Map<string, number>();
  for (const ids of pairs.values()) {
    ids.forEach((id, i) => offsets.set(id, (i - (ids.length - 1) / 2) * PARALLEL));
  }
  return edges.map((edge) => {
    const route = how.route(edge);
    // a floating line's offset is measured across its own direction: two lines
    // running opposite ways between one pair must flip one sign to stay apart
    const flip = route === "float" && edge.source > edge.target ? -1 : 1;
    return {
      id: edge.id,
      type: "link",
      source: edge.source,
      target: edge.target,
      zIndex: 0,
      data: {
        link: edge.link,
        emphasis: "rest",
        labelled: how.labelled ?? false,
        offset: (offsets.get(edge.id) ?? 0) * flip,
        route,
        curve: how.curve?.(edge) ?? 0,
        scope: edge.scope ?? null,
      },
    };
  });
}
