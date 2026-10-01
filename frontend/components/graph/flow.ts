import { byRelation, type Box, type ViewEdge } from "@/lib/model/graph";

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

/** Gap between two lines drawn between the same two nodes. */
const PARALLEL = 6;

/**
 * React Flow edges. Links of different families between the same pair are
 * drawn side by side, never on one curve: a code link and a dependency are two
 * facts. A code link is drawn as one line per relationship it carries (imports,
 * calls, uses…), each in its own colour, side by side the same way; the parts
 * keep their link's id as `base`, and the middle one carries the link's count.
 */
export function flowEdges(
  edges: readonly ViewEdge[],
  how: {
    route: (edge: ViewEdge) => RouteKind;
    curve?: (edge: ViewEdge) => number;
    labelled?: boolean;
  },
): GraphEdge[] {
  const lines = edges.flatMap((edge) => {
    const parts = byRelation(edge.link);
    const middle = Math.floor((parts.length - 1) / 2);
    return parts.map((part, i) => ({
      edge,
      id: parts.length === 1 ? edge.id : `${edge.id}~${part.relation}`,
      part,
      badge: i === middle ? edge.link : null,
    }));
  });
  const pairs = new Map<string, string[]>();
  for (const { edge, id } of lines) {
    const pair = [edge.source, edge.target].sort().join("|");
    pairs.set(pair, [...(pairs.get(pair) ?? []), id]);
  }
  const offsets = new Map<string, number>();
  for (const ids of pairs.values()) {
    ids.forEach((id, i) => offsets.set(id, (i - (ids.length - 1) / 2) * PARALLEL));
  }
  return lines.map(({ edge, id, part, badge }) => {
    const route = how.route(edge);
    // a floating line's offset is measured across its own direction: two lines
    // running opposite ways between one pair must flip one sign to stay apart
    const flip = route === "float" && edge.source > edge.target ? -1 : 1;
    return {
      id,
      type: "link",
      source: edge.source,
      target: edge.target,
      zIndex: 0,
      data: {
        link: part.link,
        relation: part.relation,
        base: edge.id,
        badge,
        emphasis: "rest",
        labelled: how.labelled ?? false,
        offset: (offsets.get(id) ?? 0) * flip,
        route,
        curve: how.curve?.(edge) ?? 0,
        scope: edge.scope ?? null,
      },
    };
  });
}
